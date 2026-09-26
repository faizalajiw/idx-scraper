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


def backfill(years: float) -> None:
    dsn = os.environ["DATABASE_URL"]
    end = datetime.now(WIB).date()
    start = end - timedelta(days=round(365.25 * years))

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
                         _num(r.close), _num(r.previous), _num(r.volume),
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
    ap.add_argument("--years", type=float, default=1.0, help="years of history to backfill")
    args = ap.parse_args()
    backfill(args.years)


if __name__ == "__main__":
    main()
