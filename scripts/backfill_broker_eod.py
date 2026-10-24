"""CLI: backfill broker summary EOD (bandarmologi) -> research.broker_daily.

Logika pindah ke idx_scraper.jobs.backfill_broker_eod (FAI-20); berkas ini
tinggal pembungkus tipis supaya `python -m scripts.backfill_broker_eod`
tetap jalan.

Jalankan manual (Windows bash, dari idx-scraper/):
    set -a && . ./.env && set +a
    PYTHONPATH=src .venv/Scripts/python.exe -m scripts.backfill_broker_eod --date 20260925
"""

from __future__ import annotations

import argparse
import sys

from idx_scraper import db
from idx_scraper.jobs.backfill_broker_eod import backfill_broker_eod


def main() -> int:
    parser = argparse.ArgumentParser(prog="backfill_broker_eod")
    parser.add_argument("--date", help="YYYYMMDD (default: hari ini WIB)")
    args = parser.parse_args()

    dsn = db.dsn() if db.tersedia() else None
    if not dsn:
        print("[err] DATABASE_URL belum diisi di .env", file=sys.stderr)
        return 1

    n, d = backfill_broker_eod(dsn, args.date)
    print(f"[done] broker {d}: {n} baris tersimpan")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
