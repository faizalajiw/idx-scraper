"""
Phase 3: intraday capture — one IDX request snapshots the whole market.

GetStockSummary returns every ticker at once; during market hours its Close is
the last-traded price and Volume/Value are cumulative day-to-date. We keep the
top-N most liquid emiten (by day value) and append a tick per snapshot into the
monthly-partitioned research.intraday_ticks. 5-minute bars come from the
research.intraday_bars_5m view.

Because it's a single request, this is cheap enough to poll every ~30-60s.

Run once (manual test):
  cd /d/Project/idx-scraper && set -a && . ./.env && set +a \
    && PYTHONPATH=src .venv/Scripts/python.exe -m scripts.intraday_capture --top 200
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta, timezone

import psycopg

from idx_scraper.client import IDXClient

WIB = timezone(timedelta(hours=7))


def _rank_liquid(rows, top_n: int):
    """Top-N tickers by day trading value; skip non-traded (value falsy)."""
    traded = [r for r in rows if r.value]
    traded.sort(key=lambda r: r.value, reverse=True)
    return traded[:top_n]


def capture_once(conn, client: IDXClient, top_n: int) -> int:
    date_str = datetime.now(WIB).strftime("%Y%m%d")
    rows = client.fetch_stock_summary(date_str)
    if not rows:
        return 0

    picked = _rank_liquid(rows, top_n)
    ts = datetime.now(WIB)

    # One partition per month; ensure it exists before writing.
    conn.execute("select research.ensure_intraday_partition(%s)", (ts.date(),))

    with conn.cursor() as cur:
        cur.executemany(
            """insert into research.intraday_ticks (code, ts, last, volume, value)
               values (%s, %s, %s, %s, %s)
               on conflict (code, ts) do nothing""",
            [(r.code, ts, r.close, r.volume, r.value) for r in picked],
        )
    return len(picked)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=200, help="number of most-liquid emiten to keep")
    args = ap.parse_args()

    client = IDXClient()
    client.warmup()
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        n = capture_once(conn, client, args.top)
        print(f"[intraday] captured {n} ticks at {datetime.now(WIB).isoformat()}")
    client.close()


if __name__ == "__main__":
    main()
