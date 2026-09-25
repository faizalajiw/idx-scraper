"""
Phase 4: point-in-time backtest engine.

A backtest that peeks at the future is worthless. This module enforces the
bitemporal rule at the query layer: every read is "as of" a knowledge instant,
so a strategy standing on day T can only ever see data that was known at T.

  PITReader(dsn).as_of(T)                -> full market snapshot known at T
  PITReader(dsn).history(code, T)        -> a code's series up to T (adjusted)
  Backtester(reader).run(strategy, ...)  -> walk trading days, mark-to-market

Prices are backward split/bonus-adjusted using only corp actions recorded by T
(see research.prices_asof_adj). raw_eod stays the source of truth.

Quick check:
  cd /d/Project/idx-scraper && set -a && . ./.env && set +a \
    && PYTHONPATH=src .venv/Scripts/python.exe -m idx_scraper.backtest --demo
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable

import psycopg

WIB = timezone(timedelta(hours=7))


@dataclass
class Bar:
    code: str
    trade_date: date
    close: float          # adjusted close (backward split/bonus)
    raw_close: float
    volume: int


class PITReader:
    """Read-only, look-ahead-free access to the research price layer."""

    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or os.environ["DATABASE_URL"]
        self._conn = psycopg.connect(self.dsn, autocommit=True)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "PITReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def trading_days(self, start: date, end: date) -> list[date]:
        rows = self._conn.execute(
            """select distinct trade_date from research.prices_pit
               where trade_date between %s and %s order by trade_date""",
            (start, end),
        ).fetchall()
        return [r[0] for r in rows]

    def as_of(
        self,
        sim_date: date,
        codes: Iterable[str] | None = None,
        knowledge: datetime | None = None,
    ) -> dict[str, Bar]:
        """Latest adjusted bar per code as of a simulation day, keyed by code.

        Two independent look-ahead cuts:
          * calendar   -> only bars with trade_date <= sim_date are visible.
          * restatement-> among versions of a bar, use the newest known at
                          `knowledge`. Defaults to now() = best current
                          knowledge (correct for single-ingest backfilled data;
                          pass an explicit instant once live restatements exist).
        """
        k = knowledge or datetime.now(WIB)
        params: list = [k, sim_date]
        code_filter = ""
        if codes is not None:
            code_list = list(codes)
            if not code_list:
                return {}
            code_filter = "and code = any(%s)"
            params.append(code_list)
        rows = self._conn.execute(
            f"""select code, trade_date, adj_close, raw_close, volume
                from research.prices_asof_adj(%s)
                where trade_date <= %s {code_filter}""",
            params,
        ).fetchall()
        latest: dict[str, Bar] = {}
        for code, td, adj, raw, vol in rows:
            b = latest.get(code)
            if b is None or td > b.trade_date:
                latest[code] = Bar(code, td, float(adj), float(raw), int(vol or 0))
        return latest

    def price_on(self, code: str, d: date, knowledge: datetime | None = None) -> float | None:
        """Adjusted close for a code on a specific trade_date, as known at `knowledge`."""
        k = knowledge or datetime.now(WIB)
        row = self._conn.execute(
            """select adj_close from research.prices_asof_adj(%s)
               where code = %s and trade_date = %s""",
            (k, code, d),
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def history(self, code: str, upto: date, knowledge: datetime | None = None) -> list[Bar]:
        """Adjusted series for a code from the beginning up to `upto`, known at `knowledge`."""
        k = knowledge or datetime.now(WIB)
        rows = self._conn.execute(
            """select trade_date, adj_close, raw_close, volume
               from research.prices_asof_adj(%s)
               where code = %s and trade_date <= %s
               order by trade_date""",
            (k, code, upto),
        ).fetchall()
        return [Bar(code, td, float(adj), float(raw), int(vol or 0)) for td, adj, raw, vol in rows]


# ----------------------------------------------------------------------------
# Backtester — walks trading days and marks a simple long-only cash+positions
# portfolio to market. Strategy returns target weights per code each day.
# ----------------------------------------------------------------------------

# strategy(day, reader) -> {code: target_weight}. Weights sum to <= 1; the rest
# stays in cash. Weights are applied at that day's adjusted close.
Strategy = Callable[[date, PITReader], dict[str, float]]


@dataclass
class Portfolio:
    cash: float
    positions: dict[str, float] = field(default_factory=dict)  # code -> shares
    equity_curve: list[tuple[date, float]] = field(default_factory=list)


def _rebalance(pf: Portfolio, prices: dict[str, Bar], targets: dict[str, float]) -> None:
    # current total equity marked at today's prices
    equity = pf.cash + sum(
        sh * prices[c].close for c, sh in pf.positions.items() if c in prices
    )
    # liquidate positions not in target or repriced below
    for code in list(pf.positions.keys()):
        if code not in targets and code in prices:
            pf.cash += pf.positions[code] * prices[code].close
            del pf.positions[code]
    # set each target position to its desired notional
    for code, w in targets.items():
        px = prices.get(code)
        if px is None or px.close <= 0:
            continue
        target_notional = equity * w
        cur_notional = pf.positions.get(code, 0.0) * px.close
        delta_notional = target_notional - cur_notional
        delta_shares = delta_notional / px.close
        pf.cash -= delta_shares * px.close
        pf.positions[code] = pf.positions.get(code, 0.0) + delta_shares


@dataclass
class BacktestResult:
    equity_curve: list[tuple[date, float]]
    final_equity: float
    total_return: float
    max_drawdown: float


class Backtester:
    def __init__(self, reader: PITReader):
        self.reader = reader

    def run(
        self,
        strategy: Strategy,
        start: date,
        end: date,
        initial_cash: float = 100_000_000.0,
    ) -> BacktestResult:
        days = self.reader.trading_days(start, end)
        pf = Portfolio(cash=initial_cash)
        for d in days:
            targets = strategy(d, self.reader) or {}
            # prices as known at end of this trading day (no future peeking)
            codes = set(targets) | set(pf.positions)
            prices = self.reader.as_of(d, codes) if codes else {}
            _rebalance(pf, prices, targets)
            equity = pf.cash + sum(
                sh * prices[c].close for c, sh in pf.positions.items() if c in prices
            )
            pf.equity_curve.append((d, equity))

        curve = pf.equity_curve
        final = curve[-1][1] if curve else initial_cash
        peak = initial_cash
        mdd = 0.0
        for _, eq in curve:
            peak = max(peak, eq)
            mdd = max(mdd, (peak - eq) / peak if peak else 0.0)
        return BacktestResult(
            equity_curve=curve,
            final_equity=final,
            total_return=(final / initial_cash - 1.0),
            max_drawdown=mdd,
        )


def _demo() -> None:
    """Buy-and-hold BBRI over the backfilled window as a smoke test."""
    with PITReader() as reader:
        days = reader.trading_days(date(2000, 1, 1), date(2100, 1, 1))
        if not days:
            print("no data")
            return
        start, end = days[0], days[-1]

        def strategy(d: date, r: PITReader) -> dict[str, float]:
            return {"BBRI": 1.0}

        res = Backtester(reader).run(strategy, start, end)
        print(f"window: {start} -> {end} ({len(res.equity_curve)} days)")
        print(f"final equity : {res.final_equity:,.0f}")
        print(f"total return : {res.total_return:+.2%}")
        print(f"max drawdown : {res.max_drawdown:.2%}")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="run buy-and-hold BBRI smoke test")
    args = ap.parse_args()
    if args.demo:
        _demo()
    else:
        ap.print_help()
