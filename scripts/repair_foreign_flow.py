"""Tambal kolom ``foreign_*`` yang NULL pada baris ``research.raw_eod`` yang sudah ada.

Kenapa ada: 192.613 kolom ``foreign_buy/sell/net`` bolong selama sembilan bulan
(201 sesi, 2025-09-24 -> 2026-07-29) tanpa satu pun alarm. Baris lama ini masuk
ke ``raw_eod`` sebelum pemetaan ``foreign_*`` ada di jalur ingest. Tidak ada
proses yang menarik ulang tanggal yang **sudah ada tapi kolomnya kosong** —
``_backfill_hari_lampau`` dan ``check_eod_gaps`` hanya mengejar tanggal yang
hilang seluruhnya, sehingga celahnya bertahan berbulan-bulan.

Skrip ini menarik ulang tiap sesi bertarget dan **hanya** menulis kolom foreign;
OHLCV tidak pernah disentuh. Idempoten lewat penjaga ``foreign_net is null`` +
``ingested_at=now()`` (menciptakan versi PIT baru tiap tambalan).

Bukti historis (audit 17 Okt 2026, sudah dijalankan):
- 201 sesi (2025-09-24 -> 2026-07-29), 192.613 kolom ``foreign_*`` NULL; ditambal 100%.
- Pemetaan ``source``: 455.482 baris semuanya ``source='IDX'`` — bukan jalur Yahoo
  (baris Yahoo selalu ``value``/``frequency`` NULL, kolom foreign ini bukan).
- Hash per sesi untuk open/high/low/close/prev_close/volume/value/frequency/name
  pada 201 sesi: **0 berubah** — tambalan tidak menyentuh OHLCV.
- Bug penyebab tanggal tampak "libur permanen" sudah diperbaiki di
  ``scripts/backfill_raw_eod.py`` (gagal transport ≠ libur bursa).

Prosedur verifikasi ulang (pra/pasca per sesi): hitung hash kolom OHLCV sebelum
dan sesudah tambalan, harapkan identik. Contoh:

    select trade_date,
           md5(string_agg(code || open || high || low || close || prev_close
               || volume || value || frequency || name, '' order by code)) as h
      from research.raw_eod where trade_date between %s and %s group by trade_date;

Jalankan pra dan pasca; untuk 201 sesi tiap hash harus sama (0 berubah).

Pemakaian:
    .venv/Scripts/python.exe -m scripts.repair_foreign_flow \\
        --start-date 2025-09-24 --end-date 2026-07-29 --pace 4

    # uji cepat (hanya beberapa sesi pertama)
    .venv/Scripts/python.exe -m scripts.repair_foreign_flow \\
        --start-date 2026-07-01 --end-date 2026-07-02 --limit 5
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date

import psycopg

from idx_scraper.client import IDXClient
from scripts.backfill_raw_eod import (
    _num,
    ambil_hari,
    load_env,
    refresh_prices_pit,
    trading_days,
)

SLEEP_SECONDS = 4.0  # jeda ≥4 s: burst 1,2 s terbukti memicu 403 Cloudflare

# Hanya kolom foreign yang boleh ditulis; penjaga foreign_net is null + ingested_at=now()
# menjaga idempotensi dan menciptakan versi PIT baru tanpa menyentuh OHLCV.
_UPDATE_FOREIGN = """
update research.raw_eod
   set foreign_buy=%s, foreign_sell=%s, foreign_net=%s, ingested_at=now()
 where code=%s and trade_date=%s and foreign_net is null
"""

_HITUNG_NULL = (
    "select count(*) from research.raw_eod where trade_date=%s and foreign_net is null"
)


def baris_perlu_tambal(rows, tanggal: date) -> list[tuple]:
    """Bentuk payload tambalan dari baris EOD IDX untuk satu tanggal.

    Murni: baris yang ``foreign_buy/sell/net`` semuanya ``None`` dilewati;
    sisanya jadi ``(fbuy, fsell, fnet, code, tanggal)`` untuk ``executemany``.
    """
    return [
        (_num(r.foreign_buy), _num(r.foreign_sell), _num(r.foreign_net), r.code, tanggal)
        for r in rows
        if not (r.foreign_buy is None and r.foreign_sell is None and r.foreign_net is None)
    ]


def tambal(
    start: date,
    end: date,
    pace: float = SLEEP_SECONDS,
    limit: int = 0,
) -> None:
    load_env()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("[repair] ERROR: DATABASE_URL tidak ditemukan di environment", file=sys.stderr)
        sys.exit(1)

    days = list(trading_days(start, end))
    if limit:
        days = days[:limit]
    print(f"[repair] rentang {start} .. {end} | hari kerja diproses: {len(days)}", flush=True)

    client = IDXClient()
    client.warmup()

    total_sebelum = 0
    total_terisi = 0

    with psycopg.connect(dsn, autocommit=True) as conn:
        for i, d in enumerate(days, 1):
            sebelum = conn.execute(_HITUNG_NULL, (d,)).fetchone()[0]
            total_sebelum += sebelum
            if sebelum == 0:
                # Sudah bersih: jangan buang request ke IDX (Step 5 tetap cetak 0/0).
                print(f"  {i}/{len(days)} {d} -> terisi 0/0 (tak ada kolom NULL; 0 request)",
                      flush=True)
                continue

            rows, status = ambil_hari(client, d.strftime("%Y%m%d"))
            if status in (403, 429):
                print(
                    f"[repair] DIHENTIKAN di {d.isoformat()} (status {status}) — IDX menolak "
                    f"beruntun. Lanjutkan: --start-date {d.isoformat()} --end-date {end.isoformat()}",
                    flush=True,
                )
                client.close()
                sys.exit(1)

            payload = baris_perlu_tambal(rows or [], d)
            if payload:
                conn.executemany(_UPDATE_FOREIGN, payload)

            sesudah = conn.execute(_HITUNG_NULL, (d,)).fetchone()[0]
            terisi = sebelum - sesudah
            total_terisi += terisi
            print(f"  {i}/{len(days)} {d} -> terisi {terisi}/{sebelum} (status {status})",
                  flush=True)

            time.sleep(pace)

    client.close()
    print(f"[repair] selesai. terisi {total_terisi}/{total_sebelum} kolom foreign", flush=True)

    refresh_prices_pit(dsn)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--start-date", type=date.fromisoformat, required=True, help="YYYY-MM-DD")
    ap.add_argument("--end-date", type=date.fromisoformat, required=True, help="YYYY-MM-DD")
    ap.add_argument("--pace", type=float, default=SLEEP_SECONDS, help="jeda antar request IDX (detik)")
    ap.add_argument("--limit", type=int, default=0, help="proses N sesi pertama saja (uji cepat)")
    args = ap.parse_args()

    if args.end_date < args.start_date:
        print("[repair] ERROR: --end-date lebih awal dari --start-date", file=sys.stderr)
        sys.exit(1)

    tambal(args.start_date, args.end_date, pace=args.pace, limit=args.limit)


if __name__ == "__main__":
    main()
