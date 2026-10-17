"""Tutup lubang sesi: hari kerja tanpa baris di ``research.raw_eod`` -> coba ulang.

Latar: IDX membalas 503 sesaat untuk sekitar 10% request, bahkan di dalam
jendela retensi 2 tahunnya. Kalau satu tanggal terlanjur dilewati, ia tinggal
lubang di panel riset — dan karena ``raw_eod`` hanya boleh punya satu baris per
(code, trade_date), lubang itu tidak muncul lagi di backfill berikutnya yang
melewati tanggal yang sudah terisi.

Yang dilakukan skrip ini:
1. Daftar hari kerja di rentang (default: min..max ``trade_date`` yang ada)
   yang belum punya satu baris pun.
2. Untuk tiap tanggal: fetch ulang (3 percobaan, backoff 5/20 s).
3. Hasil diklasifikasikan jujur:
   - dapat baris            -> diingest (gate mutu sama dengan backfill lain)
   - 200 tanpa baris        -> LIBUR bursa
   - 503 / status lain      -> LUBANG di sisi IDX (bukan libur!) — dicatat dan
     dutarakan di ringkasan supaya terlihat, bukan disembunyikan.
4. ``prices_pit`` disegarkan bila ada yang terisi.

Pemakaian:
    # lihat dulu tanpa menyentuh IDX
    .venv/Scripts/python.exe -m scripts.check_eod_gaps --dry-run

    # tarik lubang (jeda 4 s); --limit untuk menguji cepat
    .venv/Scripts/python.exe -m scripts.check_eod_gaps --limit 10
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
from datetime import date, timedelta

import psycopg

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from idx_scraper.client import IDXClient
from scripts.backfill_raw_eod import (
    _num,
    _ohl,
    _prev_close,
    refresh_prices_pit,
)

_PERCOBAAN = (0, 30, 90)  # backoff sabar: 403/503 sesaat pernah pulih setelah 60-120s
_BLOCK_BACKOFF = (0, 60, 120)
_TERLALU_TERBUKA = 4  # 503 yang muncul beruntun = IDX sedang menolak, berhenti


def load_env() -> None:
    env_path = _REPO_ROOT / ".env"
    if env_path.exists():
        import dotenv

        dotenv.load_dotenv(str(env_path), override=False)


def hilang(cur, lo: date, hi: date) -> list[date]:
    cur.execute("select distinct trade_date from research.raw_eod")
    ada = {r[0] for r in cur.fetchall()}
    out: list[date] = []
    d = lo
    while d <= hi:
        if d.weekday() < 5 and d not in ada:
            out.append(d)
        d += timedelta(days=1)
    return out


def fetch(client: IDXClient, ds: str) -> tuple[list | None, object]:
    """>=1 baris, atau None + status terakhir transport."""
    rows = None
    status = None
    for i, jeda in enumerate(_PERCOBAAN):
        if jeda:
            time.sleep(jeda)
        try:
            rows = client.fetch_stock_summary(ds)
            status = getattr(client.session, "last_status", None)
        except Exception as exc:
            rows, status = None, f"EXC:{type(exc).__name__}"
            print(f"    exception {exc!r} (percobaan {i + 1})", flush=True)
        if rows:
            return rows, status
    return None, status


def main() -> None:
    load_env()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--start-date", type=date.fromisoformat, default=None, help="YYYY-MM-DD")
    ap.add_argument("--end-date", type=date.fromisoformat, default=None, help="YYYY-MM-DD")
    ap.add_argument("--pace", type=float, default=4.0, help="jeda antar request (detik)")
    ap.add_argument("--limit", type=int, default=0, help="proses N tanggal pertama saja")
    ap.add_argument(
        "--dates",
        default="",
        help="tanggal spesifik dipisah koma (YYYY-MM-DD); menimpa pemindaian rentang",
    )
    ap.add_argument("--dry-run", action="store_true", help="hanya daftar lubang, tanpa fetch")
    args = ap.parse_args()

    dsn = os.environ["DATABASE_URL"]
    conn = psycopg.connect(dsn, autocommit=True)
    cur = conn.cursor()
    cur.execute("select min(trade_date), max(trade_date) from research.raw_eod")
    lo, hi = cur.fetchone()
    lo = args.start_date or lo
    hi = args.end_date or hi

    if args.dates:
        lubang = [date.fromisoformat(s.strip()) for s in args.dates.split(",") if s.strip()]
    else:
        lubang = hilang(cur, lo, hi)
    print(f"[gap] rentang {lo} .. {hi} | hari kerja tanpa data: {len(lubang)}", flush=True)
    print("      " + ", ".join(str(x) for x in lubang), flush=True)
    if args.dry_run or not lubang:
        return
    if args.limit:
        lubang = lubang[: args.limit]

    client = IDXClient()
    client.warmup()
    terisi: list[str] = []
    libur: list[str] = []
    gagal: list[tuple[str, object]] = []
    tolak_beruntun = 0

    for i, day in enumerate(lubang, 1):
        cur.execute("select 1 from research.raw_eod where trade_date = %s limit 1", (day,))
        if cur.fetchone():
            print(f"  {i}/{len(lubang)} {day} -> sudah ada, dilewati", flush=True)
            continue
        rows, status = fetch(client, day.strftime("%Y%m%d"))
        if rows:
            tolak_beruntun = 0
            n = 0
            for r in rows:
                reason = cur.execute(
                    "select research.eod_reject_reason("
                    "%s::numeric,%s::numeric,%s::numeric,%s::numeric,%s::bigint)",
                    (r.open, r.high, r.low, r.close, r.volume),
                ).fetchone()[0]
                if reason is not None:
                    cur.execute(
                        """insert into research.quarantine_eod (code, trade_date, payload, reason)
                           values (%s, %s, %s, %s)""",
                        (r.code, day, json.dumps(r.model_dump(mode="json")), reason),
                    )
                    continue
                cur.execute(
                    """insert into research.raw_eod
                       (code, trade_date, open, high, low, close, prev_close,
                        volume, value, frequency, source,
                        name, foreign_buy, foreign_sell, foreign_net)
                       values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        r.code,
                        day,
                        _ohl(r.open),
                        _ohl(r.high),
                        _ohl(r.low),
                        _num(r.close),
                        _num(_prev_close(r)),
                        _num(r.volume),
                        _num(r.value),
                        _num(r.frequency),
                        r.source,
                        r.name,
                        _num(r.foreign_buy),
                        _num(r.foreign_sell),
                        _num(r.foreign_net),
                    ),
                )
                n += 1
            terisi.append(f"{day}({n})")
            print(f"  {i}/{len(lubang)} {day} -> terisi {n} baris", flush=True)
        elif status == 200:
            libur.append(str(day))
            tolak_beruntun = 0
            print(f"  {i}/{len(lubang)} {day} -> libur bursa", flush=True)
        else:
            gagal.append((str(day), status))
            print(f"  {i}/{len(lubang)} {day} -> LUBANG IDX (status {status})", flush=True)
            if status in (403, 429):
                tolak_beruntun += 1
                if tolak_beruntun >= 2:
                    print(
                        "[gap] IDX menolak beruntun (403/429) — berhenti, coba lagi nanti.",
                        flush=True,
                    )
                    break
        time.sleep(args.pace)

    client.close()
    print(
        f"[gap] terisi={len(terisi)} | libur={len(libur)} | lubang IDX={len(gagal)}",
        flush=True,
    )
    if terisi:
        print("      terisi:", ", ".join(terisi), flush=True)
    if gagal:
        print("      lubang:", ", ".join(f"{d}:{s}" for d, s in gagal), flush=True)
    if terisi:
        refresh_prices_pit(dsn)


if __name__ == "__main__":
    main()
