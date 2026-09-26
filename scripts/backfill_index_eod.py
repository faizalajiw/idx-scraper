"""Backfill EOD index (IHSG/COMPOSITE) dari Yahoo Finance.

Masalah yang ditambal:
- index_quotes hanya berisi snapshot polling (~menit terakhir sesi; snapshot
  15:59 WIB ≠ closing auction 16:00). Dashboard baca tabel ini untuk angka
  IHSG, jadi close-nya selisih beberapa poin dari Yahoo / IDX.
- index_summary_daily kosong, padahal reader lain (RRG benchmark) butuh seri
  harian panjang.

Yang dilakukan script ini:
1. Unduh daily ^JKSE dari Yahoo untuk rentang tanggal yang diinginkan.
2. Upsert ke index_summary_daily on (code, date) — idempotent.
3. Untuk tanggal EOD terbaru, tulis SATU snapshot ke index_quotes per tanggal
   (idempotent via natural key source+code+captured_at) dengan captured_at
   pas 15:59:59.999 WIB — menjorok ke dalam sesi, sehingga jadi baris
   "terbaru" yang dibaca dashboard, dan menimpa nilai polling 15:59 lewat
   kolom `current` (dashboard menampilkan `current ?? close`).

Jalankan (Windows bash, dari idx-scraper/):
    set -a && . ./.env && set +a
    PYTHONPATH=src .venv/Scripts/python.exe -m scripts.backfill_index_eod --days 90
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import datetime, time, timedelta, timezone

import psycopg
import yfinance as yf

WIB = timezone(timedelta(hours=7))
# Setelah 15:59:59.999 WIB dianggap "close resmi" hari itu (pasca closing auction).
CLOSE_SNAPSHOT_T = time(15, 59, 59, 999000)

# ---
# PENTING: Yahoo ^JKSE tidak punya volume resmi IDX (angka ~20-30 juta bukan
# total lembar bursa). Kolom volume/value sengaja dibiarkan NULL agar angka
# bursa tidak tercemar angka Yahoo yang salah — volume sumber IDX masuk lewat
# jalur lain kalau tersedia.
# ---


def backfill_index_eod(dsn: str, days: int) -> tuple[int, int]:
    """Run the full backfill against `dsn`. Returns (upserted, new_snapshots).

    Idempotent: safe to run berulang (dipanggil juga oleh scheduler harian di
    cli.cmd_serve jam 16:10 WIB).
    """
    bars = _fetch_yahoo_index_eod(days)
    if not bars:
        print("[abort] tidak ada data ^JKSE dari Yahoo.")
        return 0, 0
    print(f"yahoo ^JKSE: {len(bars)} bar ({bars[0]['date']} .. {bars[-1]['date']})")

    upserted = 0
    snapshots = 0
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn, conn.cursor() as cur:
        cur.execute("set time zone 'Asia/Jakarta'")

        # 0) Pastikan upsert idempotent: unique index (code, date) + dedupe lama.
        #    (Pola yang sama dipakai storage.py untuk stock_summary_daily.)
        cur.execute(
            """delete from index_summary_daily a
               using index_summary_daily b
               where a.code = b.code and a.date = b.date and a.ctid > b.ctid"""
        )
        cur.execute(
            """create unique index if not exists ux_index_summary_daily_code_date
               on index_summary_daily(code, date)"""
        )

        # 1) EOD harian -> index_summary_daily (idempotent on (code, date))
        for b in bars:
            cur.execute(
                """insert into index_summary_daily
                       (code, close, open, high, low, date, captured_at)
                   values (%s, %s, %s, %s, %s, %s, now())
                   on conflict (code, date) do update set
                     close = excluded.close, open = excluded.open,
                     high = excluded.high, low = excluded.low,
                     captured_at = now()""",
                ("COMPOSITE", b["close"], b["open"], b["high"], b["low"], b["date"]),
            )
            upserted += 1

        # 2) Snapshot "close resmi" ke index_quotes untuk tanggal-tanggal baru
        #    (yang belum punya snapshot pas 15:59:59.999 WIB).
        for b in bars:
            captured = datetime.combine(b["date"], CLOSE_SNAPSHOT_T, tzinfo=WIB)
            cur.execute(
                """select 1 from index_quotes
                   where source='Yahoo' and code='COMPOSITE' and captured_at = %s""",
                (captured,),
            )
            if cur.fetchone():
                continue  # sudah pernah ditulis — idempotent
            prev = next(
                (x["close"] for x in reversed(bars) if x["date"] < b["date"]), None
            )
            change = (b["close"] - prev) if prev else None
            percent = (change / prev * 100) if (prev and change is not None) else None
            cur.execute(
                """insert into index_quotes
                       (source, code, close, change, percent, current, captured_at, metadata)
                   values (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    "Yahoo",
                    "COMPOSITE",
                    b["close"],
                    change,
                    percent,
                    b["close"],
                    captured,
                    '{"src": "yahoo-eod", "note": "official close (closing auction)"}',
                ),
            )
            snapshots += 1

        # 3) Verifikasi
        cur.execute(
            """select date, close from index_summary_daily
               where code='COMPOSITE' order by date desc limit 3"""
        )
        print("index_summary_daily terakhir:")
        for r in cur.fetchall():
            print(" ", r)
        cur.execute(
            """select close, current, captured_at from index_quotes
               where code='COMPOSITE' order by captured_at desc limit 2"""
        )
        print("index_quotes terakhir (dashboard baca baris pertama):")
        for r in cur.fetchall():
            print(" ", r)

    print(f"[done] index_summary_daily upsert={upserted}, snapshot close baru={snapshots}")
    return upserted, snapshots


def _fetch_yahoo_index_eod(days: int) -> list[dict]:
    tk = yf.Ticker("^JKSE")
    df = tk.history(period=f"{days}d", interval="1d", auto_adjust=False)
    if df.empty:
        return []
    rows: list[dict] = []
    for idx, r in df.iterrows():
        close = float(r["Close"])
        if math.isnan(close):
            continue
        d = idx.date() if hasattr(idx, "date") else idx
        rows.append(
            {
                "date": d,
                "open": float(r["Open"]) if r["Open"] == r["Open"] else None,
                "high": float(r["High"]) if r["High"] == r["High"] else None,
                "low": float(r["Low"]) if r["Low"] == r["Low"] else None,
                "close": close,
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(prog="backfill_index_eod")
    parser.add_argument("--days", type=int, default=90, help="rentang hari historis (default 90)")
    args = parser.parse_args()

    dsn = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not dsn:
        print("[err] DATABASE_URL/SUPABASE_DB_URL belum diisi di .env", file=sys.stderr)
        return 1

    backfill_index_eod(dsn, args.days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
