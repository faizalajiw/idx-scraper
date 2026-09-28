"""Backfill EOD index (IHSG/COMPOSITE) dari IDX live — bukan Yahoo.

Kenapa IDX-live, bukan Yahoo:
- Dashboard baca index_quotes (current ?? close). Angka Yahoo ^JKSE selisih
  beberapa poin dari close resmi IDX (closing auction 16:00). Biar sama persis
  dengan halaman idx.co.id, ambil langsung dari IDX GetIndexList.

Semantik field IDX GetIndexList untuk COMPOSITE:
    Closing  -> prev close (referensi Change)
    Current  -> nilai indeks hari ini (close resmi pasca closing auction)
    Change   -> Current - prevClose ; Percent -> Change / prevClose * 100
Kita simpan close=current (angka resmi hari ini), source='IDX', captured_at
16:00 WIB supaya jadi baris terbaru yang menang atas snapshot polling lama.

Idempotent: aman dijalankan berulang (dipanggil scheduler harian di
cli._job_index_eod_close jam 16:10 WIB). Butuh Chrome profile yang sudah
lolos Cloudflare (IDX_CHROME_PROFILE_DIR) sama seperti scrape EOD lainnya.

Jalankan (Windows PowerShell, dari idx-scraper/):
    Get-Content .env | ... ; $env:PYTHONPATH="src"
    $env:IDX_CHROME_PROFILE_DIR = Join-Path $env:LOCALAPPDATA "idx-scraper-chrome-p3"
    python -m scripts.backfill_index_eod_idx
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, time, timedelta, timezone

import psycopg

from idx_scraper.client import IDXClient

WIB = timezone(timedelta(hours=7))
# Closing auction — strictly after snapshot polling 15:59:xx, so this row wins.
CLOSE_T = time(16, 0, 0)


def _fetch_composite_idx() -> "tuple[float, float | None, float | None] | None":
    """Return (close_today, change, percent) COMPOSITE dari IDX live, atau None."""
    client = IDXClient()
    try:
        for q in client.fetch_index_list():
            if q.code.upper() in {"COMPOSITE", "IHSG"}:
                if q.current is None:
                    return None
                return (
                    float(q.current),
                    float(q.change) if q.change is not None else None,
                    float(q.percent) if q.percent is not None else None,
                )
        return None
    finally:
        try:
            client.close()
        except Exception:
            pass


def backfill_index_eod_idx(dsn: str) -> tuple[int, int]:
    """Tulis close resmi IDX hari ini ke index_quotes + index_summary_daily.

    Returns (summary_upserts, quote_snapshots) — masing-masing 0/1 (satu hari).
    """
    comp = _fetch_composite_idx()
    if comp is None:
        print("[abort] COMPOSITE tidak ditemukan / current kosong dari IDX.")
        return 0, 0
    close_today, change, percent = comp

    today = datetime.now(WIB).date()
    captured = datetime.combine(today, CLOSE_T, tzinfo=WIB)

    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn, conn.cursor() as cur:
        cur.execute("set time zone 'Asia/Jakarta'")

        # index_quotes: baris close resmi IDX (idempotent via source+code+captured_at)
        cur.execute(
            """insert into index_quotes
                   (source, code, close, change, percent, current, captured_at, metadata)
               values (%s, %s, %s, %s, %s, %s, %s, %s)
               on conflict (source, code, captured_at) do update set
                 close = excluded.close, change = excluded.change,
                 percent = excluded.percent, current = excluded.current,
                 metadata = excluded.metadata""",
            (
                "IDX",
                "COMPOSITE",
                close_today,
                change,
                percent,
                close_today,
                captured,
                '{"src": "idx-live", "note": "official close GetIndexList"}',
            ),
        )

        # index_summary_daily: close harian juga angka IDX resmi (idempotent on code+date)
        cur.execute(
            """create unique index if not exists ux_index_summary_daily_code_date
               on index_summary_daily(code, date)"""
        )
        cur.execute(
            """insert into index_summary_daily (code, close, date, captured_at)
               values (%s, %s, %s, now())
               on conflict (code, date) do update set
                 close = excluded.close, captured_at = now()""",
            ("COMPOSITE", close_today, today),
        )

    print(
        f"index EOD (IDX live) {today}: close={close_today} "
        f"change={change} percent={percent}"
    )
    return 1, 1


def main() -> None:
    dsn = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not dsn:
        print("[err] DATABASE_URL/SUPABASE_DB_URL belum diisi", file=sys.stderr)
        return
    backfill_index_eod_idx(dsn)


if __name__ == "__main__":
    main()
