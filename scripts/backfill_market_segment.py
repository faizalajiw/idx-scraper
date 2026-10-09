"""CLI: agregat pasar reguler vs non-reguler -> research.market_segment_daily.

Sumber angka: /primary/TradingSummary/GetStockSummary?date=YYYYMMDD. Satu baris
emiten memuat Volume/Value/Frequency (reguler) DAN NonRegularVolume/Value/
Frequency (tunai + negosiasi) — keduanya dijumlahkan per tanggal. Idempoten per
tanggal (upsert on trade_date).

Jalankan manual (Windows bash, dari idx-scraper/):
    set -a && . ./.env && set +a
    PYTHONPATH=src .venv/Scripts/python.exe -m scripts.backfill_market_segment --date 20261005
    PYTHONPATH=src .venv/Scripts/python.exe -m scripts.backfill_market_segment --from 20260901 --to 20260930
    PYTHONPATH=src .venv/Scripts/python.exe -m scripts.backfill_market_segment --rebuild
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import psycopg

from idx_scraper.market_segment import aggregate_day, ensure_table, upsert_day

WIB = timezone(timedelta(hours=7))

# IDX membalas 429 kalau ditembak rapat — throttle polos. 429 membuat
# `_get_json` mengembalikan None, jadi "payload kosong" bisa berarti "kena
# rate limit", bukan "hari libur". Karena itu: jeda antar tanggal + retry
# dengan backoff sebelum menyerah.
_DELAY_BETWEEN_DAYS = 2.5
_RETRY_DELAYS = (6.0, 15.0, 30.0)


def _fetch_day(client: Any, d: str) -> list[Any]:
    """GetStockSummary satu tanggal dengan retry saat kena throttle/kosong."""
    delays = _RETRY_DELAYS
    for attempt in range(len(delays) + 1):
        rows = client.fetch_stock_summary(d)
        if rows:
            return rows
        if attempt < len(delays):
            time.sleep(delays[attempt])
    return []


def backfill_from_idx(dsn: str, date_strs: list[str]) -> tuple[int, int]:
    """Fetch GetStockSummary per tanggal lalu upsert agregatnya.

    Returns (tanggal_terisi, total_baris_emiten). Tanggal libur / payload kosong
    dilewati tanpa menulis.
    """
    from idx_scraper.client import IDXClient

    client = IDXClient()
    days = rows_total = 0
    try:
        for d in date_strs:
            rows = _fetch_day(client, d)
            if not rows:
                print(f"[skip] {d}: tidak ada data (libur / belum final)")
                time.sleep(_DELAY_BETWEEN_DAYS)
                continue
            trade_date = f"{d[:4]}-{d[4:6]}-{d[6:]}"
            with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn, conn.cursor() as cur:
                upsert_day(cur, trade_date, aggregate_day(rows))
            days += 1
            rows_total += len(rows)
            print(f"[ok] {d}: {len(rows)} emiten -> agregat tersimpan")
            time.sleep(_DELAY_BETWEEN_DAYS)
    finally:
        client.close()
    return days, rows_total


def rebuild_from_raw(dsn: str) -> int:
    """Hitung ulang agregat dari research.raw_eod yang tersimpan (tanpa IDX)."""
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn, conn.cursor() as cur:
        ensure_table(cur)
        cur.execute(
            """select trade_date, code, volume, value, frequency,
                      nr_volume, nr_value, nr_freq
               from research.raw_eod
               order by trade_date"""
        )
        by_date: dict[str, list] = {}
        for d, code, vol, val, freq, nrv, nrvl, nrf in cur.fetchall():
            by_date.setdefault(d.isoformat(), []).append(
                SimpleNamespace(
                    code=code,
                    volume=vol,
                    value=val,
                    frequency=freq,
                    non_regular_volume=nrv,
                    non_regular_value=nrvl,
                    non_regular_frequency=nrf,
                )
            )
        for trade_date, day_rows in by_date.items():
            upsert_day(cur, trade_date, aggregate_day(day_rows))
    return len(by_date)


def _daterange(d_from: str, d_to: str) -> list[str]:
    # Tanggal bursa = tanggal WIB; diberi tz eksplisit supaya tidak terbaca
    # sebagai waktu lokal mesin (perilaku hasilnya tetap sama).
    start = datetime.strptime(d_from, "%Y%m%d").replace(tzinfo=WIB)
    end = datetime.strptime(d_to, "%Y%m%d").replace(tzinfo=WIB)
    out: list[str] = []
    while start <= end:
        out.append(start.strftime("%Y%m%d"))
        start += timedelta(days=1)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(prog="backfill_market_segment")
    parser.add_argument("--date", help="satu tanggal YYYYMMDD")
    parser.add_argument("--from", dest="d_from", help="awal rentang YYYYMMDD")
    parser.add_argument("--to", dest="d_to", help="akhir rentang YYYYMMDD")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="hitung ulang dari research.raw_eod (tanpa kontak IDX)",
    )
    args = parser.parse_args()

    dsn = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not dsn:
        print("[err] DATABASE_URL/SUPABASE_DB_URL belum diisi di .env", file=sys.stderr)
        return 1

    if args.rebuild:
        n = rebuild_from_raw(dsn)
        print(f"[done] rebuild {n} tanggal dari research.raw_eod")
        return 0

    if args.date:
        dates = [args.date]
    elif args.d_from and args.d_to:
        dates = _daterange(args.d_from, args.d_to)
    else:
        dates = [datetime.now(WIB).strftime("%Y%m%d")]

    days, rows = backfill_from_idx(dsn, dates)
    print(f"[done] {days}/{len(dates)} tanggal terisi, {rows} baris emiten")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())