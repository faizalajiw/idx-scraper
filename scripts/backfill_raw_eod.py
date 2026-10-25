"""
Phase 1 backfill: seed research.raw_eod from IDX GetStockSummary (loop by date).

- Full-bursa EOD, one IDX request per trading day (not per ticker).
- Weekends are dropped up front; holidays are decided from the transport
  status, never from an empty body alone: HTTP 200 with no rows = market
  holiday, 503 = outside IDX's rolling ~2-year retention window, 403/429 =
  Cloudflare block (never counted as a holiday), None/exception = genuine
  failure. The status is cleared before every retry attempt so a stale 200
  left over from an earlier day cannot pass a failed request off as "libur".
- Flags worth knowing: ``--pace N`` sets the sleep between requests (default
  4 s; 1-2 s bursts are what trigger 403), ``--ulang`` re-pulls dates already
  in raw_eod and updates every column instead of ``on conflict do nothing``.
- Structural quality gate (research.eod_reject_reason) routes bad rows to
  research.quarantine_eod; clean rows go to research.raw_eod (append-only).
- The >35% jump check is deferred to Phase 3 (needs corporate_actions), so a
  legit split isn't wrongly quarantined here.
- After raw_eod is populated, refresh research.prices_pit from the latest
  knowledge_date per (code, trade_date).

Run (bash):
  cd /d/Project/idx-scraper && set -a && . ./.env && set +a \
    && .venv/Scripts/python.exe -m scripts.backfill_raw_eod --years 1
"""

from __future__ import annotations

import argparse
import configparser
import contextlib
import json
import os
import pathlib
import sys
import time
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone

from idx_scraper import db
from idx_scraper.client import IDXClient

WIB = timezone(timedelta(hours=7))  # IDX business timezone
SLEEP_SECONDS = 4.0  # jeda ≥4 s: burst 1,2 s terbukti memicu 403 Cloudflare


def klasifikasi(rows: list | None, status: object) -> str:
    """Petakan hasil satu hari ke "data" | "libur" | "luar-jendela" | "diblokir" | "gagal".

    503 = di luar jendela retensi IDX (±2 tahun bergulir), bukan gangguan;
    403/429 = Cloudflare memblokir — jangan pernah dihitung libur bursa.
    """
    if rows:
        return "data"
    if status == 200:
        return "libur"
    if status == 503:
        return "luar-jendela"
    if status in (403, 429):
        return "diblokir"
    return "gagal"


def tanggal_perlu(days: Iterable[date], ada: set[date], ulang: bool) -> list[date]:
    """Tanggal yang perlu ditarik: semua bila --ulang, selain itu yang belum ada di raw_eod."""
    return list(days) if ulang else [d for d in days if d not in ada]


def trading_days(start: date, end: date):
    """Yield weekdays from start to end inclusive (holidays filtered at fetch time)."""
    d = start
    one = timedelta(days=1)
    while d <= end:
        if d.weekday() < 5:  # 0=Mon .. 4=Fri
            yield d
        d += one


def _num(v):
    return v if v is not None else None


def _ohl(v):
    """Null out zeroed OHL from non-traded days so they don't look like real prices."""
    return v if v else None


def _prev_close(r):
    """IDX sometimes omits PreviousPrice but still sends Change; derive prev = close - change (same as cli.py)."""
    if r.previous is not None:
        return r.previous
    if r.close is not None and r.change is not None:
        return r.close - r.change
    return None


assert _prev_close(type("R", (), {"previous": None, "close": 110, "change": 10})) == 100
assert _prev_close(type("R", (), {"previous": 95, "close": 110, "change": 10})) == 95


def load_env() -> None:
    """Load .env file into os.environ if not already set."""
    env_path = pathlib.Path(__file__).parent.parent / ".env"
    if env_path.exists():
        import dotenv

        dotenv.load_dotenv(str(env_path), override=False)
    # Fallback: manual parse if python-dotenv not available
    if "DATABASE_URL" not in os.environ:
        # Best-effort baca .env manual; kalau rusak, biarkan DATABASE_URL kosong
        # dan biarkan db yang melapor DSN hilang.
        with contextlib.suppress(configparser.Error):
            config = configparser.ConfigParser()
            config.read(str(env_path))
            if "DATABASE_URL" in config and "DATABASE_URL" in config["DEFAULT"]:
                os.environ["DATABASE_URL"] = config["DEFAULT"]["DATABASE_URL"]


BLOCK_BACKOFF = (
    0,
    30,
    90,
)  # percobaan ulang saat IDX menolak; setelah itu BERHENTI dengan tanggal resume

_INSERT_RAW_EOD = """
insert into research.raw_eod
   (code, trade_date, open, high, low, close, prev_close,
    volume, value, frequency, source,
    name, foreign_buy, foreign_sell, foreign_net)
values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
"""

# Kolom yang diperbarui saat menarik ulang tanggal yang sudah ada (--ulang).
_UPSERT_SET = """
do update set
    open=excluded.open, high=excluded.high, low=excluded.low,
    close=excluded.close, prev_close=excluded.prev_close,
    volume=excluded.volume, value=excluded.value, frequency=excluded.frequency,
    source=excluded.source, name=excluded.name,
    foreign_buy=excluded.foreign_buy, foreign_sell=excluded.foreign_sell,
    foreign_net=excluded.foreign_net, ingested_at=now()
"""

_REJECT_REASON = (
    "select research.eod_reject_reason(%s::numeric,%s::numeric,%s::numeric,%s::numeric,%s::bigint)"
)


def ambil_hari(
    client,
    date_str: str,
    backoff=BLOCK_BACKOFF,
    tidur=time.sleep,
) -> tuple[list | None, object]:
    """Ambil satu tanggal dengan backoff berjenjang; kembalikan (rows, status).

    ``tidur`` disuntik agar tes tidak menunggu 120 s; ``backoff`` boleh diganti tes.
    Status dikosongkan sebelum tiap percobaan: exception transport yang ditelan
    ``client._get_json`` mengembalikan ``[]`` tanpa memperbarui ``last_status``,
    sehingga status basi dari hari sukses sebelumnya tak boleh lolos sebagai
    "libur" (gagal transport ≠ libur bursa).
    """
    rows: list | None = None
    status: object = None
    for jeda in backoff:
        if jeda:
            print(f"  [{date_str}] status {status} -> tunggu {jeda}s", flush=True)
            tidur(jeda)
        client.session.last_status = None
        try:
            rows = client.fetch_stock_summary(date_str)
            status = getattr(client.session, "last_status", None)
        except Exception as exc:
            rows, status = None, f"EXC:{type(exc).__name__}"
            print(f"  [{date_str}] exception: {exc!r}", flush=True)
        # Berhenti hanya saat data ada atau IDX jelas menjawab 200 (libur). 503
        # sesaat di dalam jendela retensi (~4-10% request) masih diulang.
        if rows or status == 200:
            break
    return rows, status


def backfill(
    years: float | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    pace: float = SLEEP_SECONDS,
    ulang: bool = False,
) -> None:
    load_env()
    dsn = db.dsn() if db.tersedia() else None
    if not dsn:
        print("[backfill] ERROR: DATABASE_URL not found in environment", file=sys.stderr)
        sys.exit(1)

    if start_date is not None and end_date is not None:
        # Mode catch-up: jalankan untuk rentang tanggal tertentu
        start = start_date
        end = end_date
        print(f"[backfill] MODE CATCH-UP: {start} -> {end}")
    elif years is not None:
        # Mode default: backfill berdasarkan jumlah tahun dari hari ini
        end = datetime.now(WIB).date()
        start = end - timedelta(days=round(365.25 * years))
        print(f"[backfill] MODE DEFAULT: {start} -> {end} ({years} tahun)")
    else:
        print(
            "[backfill] ERROR: neither --years nor --start-date/--end-date provided",
            file=sys.stderr,
        )
        sys.exit(1)

    client = IDXClient()
    client.warmup()

    days = list(trading_days(start, end))
    print(f"[backfill] range {start} -> {end} ({len(days)} weekdays)")

    inserted = 0
    quarantined = 0
    holidays = 0
    outside = 0
    gagal = 0
    skipped = 0
    day_count = 0

    on_conflict = " on conflict (code, trade_date) " + (_UPSERT_SET if ulang else "do nothing")

    with db.connect() as conn:
        ada = {row[0] for row in conn.execute("select distinct trade_date from research.raw_eod")}
        to_fetch = tanggal_perlu(days, ada, ulang)

        for d in to_fetch:
            date_str = d.strftime("%Y%m%d")
            day_count += 1

            # Percobaan beruntun dengan backoff berjenjang; perkirakan status transport
            # alih-alih mengira respons kosong sebagai hari libur.
            rows, status = ambil_hari(client, date_str)

            ember = klasifikasi(rows, status)
            if ember == "diblokir":
                print(
                    f"[backfill] DIHENTIKAN di {d.isoformat()} (status {status}) "
                    f"— IDX menolak beruntun. Lanjutkan: --start-date {d.isoformat()}",
                    flush=True,
                )
                break
            if ember == "libur":
                holidays += 1
            elif ember == "luar-jendela":
                outside += 1
            elif ember == "gagal":
                gagal += 1

            if rows:
                with conn.cursor() as cur:
                    for r in rows:
                        reason = cur.execute(
                            _REJECT_REASON,
                            (r.open, r.high, r.low, r.close, r.volume),
                        ).fetchone()[0]

                        if reason is not None:
                            cur.execute(
                                """insert into research.quarantine_eod (code, trade_date, payload, reason)
                                   values (%s, %s, %s, %s)""",
                                (r.code, d, json.dumps(r.model_dump(mode="json")), reason),
                            )
                            quarantined += 1
                            continue

                        cur.execute(
                            _INSERT_RAW_EOD + on_conflict,
                            (
                                r.code,
                                d,
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
                        inserted += 1

            if day_count % 20 == 0:
                print(
                    f"  .. {day_count}/{len(to_fetch)} | "
                    f"raw+{inserted} karantina+{quarantined} libur={holidays} "
                    f"luar-jendela={outside} gagal={gagal}"
                )
            time.sleep(pace)

    client.close()
    skipped = len(days) - day_count
    print(
        f"[backfill] selesai. raw_eod +{inserted}, karantina +{quarantined}, libur {holidays}, "
        f"luar jendela {outside}, gagal {gagal}, dilewati {skipped}"
    )

    refresh_prices_pit(dsn)


def refresh_prices_pit(dsn: str) -> None:
    """Populate prices_pit from the newest knowledge_date per (code, trade_date)."""
    with db.connect() as conn:
        n = conn.execute(
            """
            insert into research.prices_pit
                (code, trade_date, knowledge_date, open, high, low, close, volume, value,
                 name, prev_close, foreign_buy, foreign_sell, foreign_net)
            select distinct on (code, trade_date)
                code, trade_date, ingested_at, open, high, low, close, volume, value,
                name, prev_close, foreign_buy, foreign_sell, foreign_net
            from research.raw_eod
            order by code, trade_date, ingested_at desc
            on conflict (code, trade_date, knowledge_date) do nothing
            """
        ).rowcount
        print(f"[prices_pit] upserted rows: {n}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--years", type=float, default=None, help="years of history to backfill (default mode)"
    )
    ap.add_argument(
        "--start-date",
        type=lambda s: date.fromisoformat(s),
        help="start date for catch-up mode (YYYY-MM-DD)",
    )
    ap.add_argument(
        "--end-date",
        type=lambda s: date.fromisoformat(s),
        help="end date for catch-up mode (YYYY-MM-DD)",
    )
    ap.add_argument(
        "--pace",
        type=float,
        default=SLEEP_SECONDS,
        help="jeda antar request IDX (detik, default 4.0)",
    )
    ap.add_argument(
        "--ulang",
        action="store_true",
        help="tarik ulang tanggal yang sudah ada di raw_eod (upsert: perbarui semua kolom)",
    )
    args = ap.parse_args()

    if args.start_date is not None and args.end_date is not None:
        # Mode catch-up: jalankan untuk rentang tanggal tertentu
        backfill(
            start_date=args.start_date, end_date=args.end_date, pace=args.pace, ulang=args.ulang
        )
    elif args.years is not None:
        # Mode default: backfill berdasarkan jumlah tahun dari hari ini
        backfill(years=args.years, pace=args.pace, ulang=args.ulang)
    else:
        print("ERROR: provide either --years or --start-date/--end-date", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
