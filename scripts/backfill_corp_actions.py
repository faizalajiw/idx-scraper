"""
Phase 2: ingest corporate actions (splits, dividends) into research.corporate_actions.

Source = yfinance (tagged source='Yahoo'). IDX's official corp-action endpoints
return 503, so Yahoo is used as the metadata source for now; split ratios and
ex-dates are public corporate facts identical across providers. When an official
IDX feed becomes available, rows can be re-verified/overwritten by ex_date.

Split ratio semantics (matches research.prices_adjusted):
  yfinance split value 5.0  -> forward split, action_type='split',        ratio=5.0
  yfinance split value 0.25 -> reverse split, action_type='reverse_split', ratio=0.25
Dividends: action_type='dividend', cash_amount=<per share>, ratio=NULL.

Run (bash):
  cd /d/Project/idx-scraper && set -a && . ./.env && set +a \
    && PYTHONPATH=src .venv/Scripts/python.exe -m scripts.backfill_corp_actions
"""

from __future__ import annotations

import os
import sys
import time

import psycopg
import yfinance as yf

SLEEP_SECONDS = 0.4  # light pacing between yfinance ticker fetches


def universe(conn) -> list[str]:
    """Distinct emiten codes we actually hold prices for."""
    rows = conn.execute("select distinct code from research.raw_eod order by code").fetchall()
    return [r[0] for r in rows]


def ingest() -> None:
    dsn = os.environ["DATABASE_URL"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        codes = universe(conn)
        print(f"[corp] universe: {len(codes)} emiten")

        n_split = 0
        n_div = 0
        n_err = 0
        with conn.cursor() as cur:
            for i, code in enumerate(codes, 1):
                try:
                    t = yf.Ticker(f"{code}.JK")
                    splits = t.splits
                    divs = t.dividends
                except Exception as e:
                    n_err += 1
                    print(f"[warn] {code}: {e}", file=sys.stderr)
                    time.sleep(SLEEP_SECONDS)
                    continue

                # Delisted/unknown tickers yield None instead of an empty Series.
                if splits is None:
                    splits = []
                else:
                    splits = list(splits.items())
                if divs is None:
                    divs = []
                else:
                    divs = list(divs.items())

                for ts, val in splits:
                    ratio = float(val)
                    if ratio <= 0 or ratio == 1.0:
                        continue
                    action = "split" if ratio > 1.0 else "reverse_split"
                    cur.execute(
                        """insert into research.corporate_actions
                           (code, ex_date, action_type, ratio, source)
                           values (%s, %s, %s, %s, 'Yahoo')
                           on conflict (code, ex_date, action_type) do update
                             set ratio = excluded.ratio, ingested_at = now()""",
                        (code, ts.date(), action, ratio),
                    )
                    n_split += 1

                for ts, val in divs:
                    cash = float(val)
                    if cash <= 0:
                        continue
                    cur.execute(
                        """insert into research.corporate_actions
                           (code, ex_date, action_type, cash_amount, source)
                           values (%s, %s, 'dividend', %s, 'Yahoo')
                           on conflict (code, ex_date, action_type) do update
                             set cash_amount = excluded.cash_amount, ingested_at = now()""",
                        (code, ts.date(), cash),
                    )
                    n_div += 1

                if i % 50 == 0:
                    print(f"  .. {i}/{len(codes)} | splits={n_split} divs={n_div} err={n_err}")
                time.sleep(SLEEP_SECONDS)

    print(f"[corp] done. splits={n_split}, dividends={n_div}, errors={n_err}")


if __name__ == "__main__":
    ingest()
