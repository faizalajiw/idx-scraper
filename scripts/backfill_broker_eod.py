"""CLI: backfill broker summary EOD (bandarmologi) -> research.broker_daily.

Sumber: /primary/TradingSummary/GetBrokerSummary?date=YYYYMMDD — agregat
transaksi per broker firm seluruh pasar. Idempoten per tanggal.

Jalankan manual (Windows bash, dari idx-scraper/):
    set -a && . ./.env && set +a
    PYTHONPATH=src .venv/Scripts/python.exe -m scripts.backfill_broker_eod --date 20260925
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg

from idx_scraper.broker_store import parse_broker_rows, upsert_broker_rows

WIB = timezone(timedelta(hours=7))
ENDPOINT = "/primary/TradingSummary/GetBrokerSummary"


def fetch_broker_payload(client: Any, date_str: str) -> dict[str, Any] | None:
    raw = client._get_json(f"{ENDPOINT}?date={date_str}&start=0&length=9999")
    return raw if isinstance(raw, dict) else None


def backfill_broker_eod(dsn: str, date_str: str | None = None) -> tuple[int, str]:
    """Fetch + upsert broker summary untuk satu tanggal (default: hari ini WIB).

    Returns (rows_tersimpan, date_YYYYMMDD). Hari libur / payload kosong ->
    (0, date) tanpa menulis apa pun.
    """
    d = date_str or datetime.now(WIB).strftime("%Y%m%d")
    from idx_scraper.client import IDXClient

    client = IDXClient()
    try:
        payload = fetch_broker_payload(client, d)
    finally:
        client.close()
    rows = parse_broker_rows(payload)
    if not rows:
        return 0, d
    trade_date = f"{d[:4]}-{d[4:6]}-{d[6:]}"
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn, conn.cursor() as cur:
        n = upsert_broker_rows(cur, trade_date, rows)
    return n, d


def main() -> int:
    parser = argparse.ArgumentParser(prog="backfill_broker_eod")
    parser.add_argument("--date", help="YYYYMMDD (default: hari ini WIB)")
    args = parser.parse_args()

    dsn = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not dsn:
        print("[err] DATABASE_URL/SUPABASE_DB_URL belum diisi di .env", file=sys.stderr)
        return 1

    n, d = backfill_broker_eod(dsn, args.date)
    print(f"[done] broker {d}: {n} baris tersimpan")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
