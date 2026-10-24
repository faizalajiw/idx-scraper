"""Backfill EOD index (IHSG/COMPOSITE) dari IDX live — bukan Yahoo.

Logika pindah ke idx_scraper.jobs.backfill_index_eod_idx (FAI-20); berkas ini
tinggal pembungkus tipis supaya `python -m scripts.backfill_index_eod_idx`
tetap jalan.

Batas: hanya untuk HARI INI — GetIndexList selalu berarti "sekarang". Tanggal
lampau tidak bisa ditambal di sini; saham lewat `idx eod <YYYYMMDD>`, close
indeks hanya lewat scripts/backfill_index_eod.py (Yahoo, provenance beda).

Jalankan (dari idx-scraper/; .env memuat DATABASE_URL):
    $env:PYTHONPATH="src"
    python -m scripts.backfill_index_eod_idx
"""

from __future__ import annotations

import sys

from idx_scraper import db
from idx_scraper.jobs.backfill_index_eod_idx import backfill_index_eod_idx


def main() -> None:
    dsn = db.dsn() if db.tersedia() else None
    if not dsn:
        print("[err] DATABASE_URL belum diisi", file=sys.stderr)
        return
    backfill_index_eod_idx(dsn)


if __name__ == "__main__":
    main()
