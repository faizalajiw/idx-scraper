"""
Phase 1 backfill: seed research.raw_eod from IDX GetStockSummary (loop by date).

- Full-bursa EOD, one IDX request per trading day (not per ticker).
- Skips weekends and holidays (IDX returns empty on non-trading days).
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
import json
import os
import sys
import time
import pathlib
from datetime import date, datetime, timedelta, timezone

import psycopg

from idx_scraper.client import IDXClient

WIB = timezone(timedelta(hours=7))  # IDX business timezone
SLEEP_SECONDS = 1.2  # anti-throttle between IDX requests


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
        try:
            import configparser
            config = configparser.ConfigParser()
            config.read(str(env_path))
            if "DATABASE_URL" in config and "DATABASE_URL" in config["DEFAULT"]:
                os.environ["DATABASE_URL"] = config["DEFAULT"]["DATABASE_URL"]
        except Exception:
            pass


def backfill(years: float | None = None, start_date: date | None = None, end_date: date | None = None) -> None:
    load_env()
    dsn = os.environ.get("DATABASE_URL")
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
        print("[backfill] ERROR: neither --years nor --start-date/--end-date provided", file=sys.stderr)
        sys.exit(1)

    client = IDXClient()
    client.warmup()

    days = list(trading_days(start, end))
    print(f"[backfill] range {start} -> {end} ({len(days)} weekdays)")

    inserted = 0
    quarantined = 0
    empty_days = 0
    day_count = 0

    with psycopg.connect(dsn, autocommit=True) as conn:
        for d in days:
            date_str = d.strftime("%Y%m%d")
            day_count += 1
            try:
                rows = client.fetch_stock_summary(date_str)
            except Exception as e:
                print(f"[warn] {date_str} fetch failed: {e}", file=sys.stderr)
                time.sleep(SLEEP_SECONDS)
                continue

            if not rows:
                empty_days += 1
                time.sleep(SLEEP_SECONDS)
                continue

            with conn.cursor() as cur:
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
                            (r.code, d, json.dumps(r.model_dump(mode="json")), reason),
                        )
                        quarantined += 1
                        continue

                    cur.execute(
                        """insert into research.raw_eod
                           (code, trade_date, open, high, low, close, prev_close,
                            volume, value, frequency, source,
                            name, foreign_buy, foreign_sell, foreign_net)
                           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (r.code, d, _ohl(r.open), _ohl(r.high), _ohl(r.low),
                         _num(r.close), _num(_prev_close(r)), _num(r.volume),
                         _num(r.value), _num(r.frequency), r.source,
                         r.name, _num(r.foreign_buy), _num(r.foreign_sell),
                         _num(r.foreign_net)),
                    )
                    inserted += 1

            if day_count % 20 == 0:
                print(f"  .. {day_count}/{len(days)} days | "
                      f"raw={inserted} quarantine={quarantined} empty={empty_days}")
            time.sleep(SLEEP_SECONDS)

    client.close()
    print(f"[backfill] done. raw_eod +{inserted}, quarantine +{quarantined}, "
          f"empty/holiday days {empty_days}")

    refresh_prices_pit(dsn)


def refresh_prices_pit(dsn: str) -> None:
    """Populate prices_pit from the newest knowledge_date per (code, trade_date)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
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
    ap.add_argument("--years", type=float, default=None, help="years of history to backfill (default mode)")
    ap.add_argument("--start-date", type=lambda s: date.fromisoformat(s), help="start date for catch-up mode (YYYY-MM-DD)")
    ap.add_argument("--end-date", type=lambda s: date.fromisoformat(s), help="end date for catch-up mode (YYYY-MM-DD)")
    args = ap.parse_args()
    
    if args.start_date is not None and args.end_date is not None:
        # Mode catch-up: jalankan untuk rentang tanggal tertentu
        backfill(start_date=args.start_date, end_date=args.end_date)
    elif args.years is not None:
        # Mode default: backfill berdasarkan jumlah tahun dari hari ini
        backfill(years=args.years)
    else:
        print("ERROR: provide either --years or --start-date/--end-date", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
