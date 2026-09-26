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

Trading frictions are opt-in through ``CostModel``: brokerage commission, the
IDX final sales tax, adverse slippage, and a participation cap against the day's
traded value. The default model is frictionless, so the engine stays pure math
and callers choose how realistic to be (see idx_scraper.api.simulation).

Every rebalance is also recorded as a ``Rebalance`` (day, the fills it made, the
resulting book and cash), so callers can inspect churn instead of only seeing a
turnover total.

Quick check:
  cd /d/Project/idx-scraper && set -a && . ./.env && set +a \
    && PYTHONPATH=src .venv/Scripts/python.exe -m idx_scraper.backtest --demo
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Self

import psycopg

WIB = timezone(timedelta(hours=7))


class RebalanceFreq(str, Enum):
    """How often a strategy may recompute its targets and trade.

    Between rebalance days the portfolio is left alone: positions drift with the
    market instead of being forced back to their target weights, which is what
    keeps turnover (and therefore cost) from exploding. Subclassing ``str``
    keeps these values JSON-friendly.
    """

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


def _period_key(d: date, freq: RebalanceFreq) -> tuple[int, ...]:
    """Calendar bucket for a trading day. Days sharing a bucket must not rebalance.

    Weeks follow the ISO calendar (so the first trading day of a week triggers),
    which makes the schedule survive market holidays without a holiday table.
    """
    if freq is RebalanceFreq.WEEKLY:
        iso = d.isocalendar()
        return (iso[0], iso[1])
    if freq is RebalanceFreq.MONTHLY:
        return (d.year, d.month)
    return (d.toordinal(),)  # daily: every trading day is its own bucket


def _rebalance_days(days: list[date], freq: RebalanceFreq) -> set[int]:
    """Indices of ``days`` the engine should rebalance on.

    The first simulated day always counts, otherwise a weekly or monthly run
    would sit in cash until its first period boundary.
    """
    if not days:
        return set()
    if freq is RebalanceFreq.DAILY:
        return set(range(len(days)))

    out = {0}
    previous = _period_key(days[0], freq)
    for i, d in enumerate(days[1:], start=1):
        current = _period_key(d, freq)
        if current != previous:
            out.add(i)
        previous = current
    return out


@dataclass(frozen=True)
class CostModel:
    """Trading frictions applied on every rebalance.

    All rates are fractions (0.0015 = 0.15%). Defaults are zero so the engine
    stays neutral; realistic IDX numbers live in ``api.simulation``.

    commission_pct  brokerage fee, charged on both buys and sells.
    sell_tax_pct    IDX final income tax, charged on sales only.
    slippage_pct    adverse price move per side: you buy a little above and sell
                    a little below the close.
    max_adv_pct     liquidity cap as a share of the day's traded notional
                    (volume x raw close). 0 disables it; codes without volume
                    data are never capped so a position can always be exited.
    """

    commission_pct: float = 0.0
    sell_tax_pct: float = 0.0
    slippage_pct: float = 0.0
    max_adv_pct: float = 0.0

    @property
    def is_frictionless(self) -> bool:
        return not (
            self.commission_pct
            or self.sell_tax_pct
            or self.slippage_pct
            or self.max_adv_pct
        )


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

    def __enter__(self) -> Self:
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


# Rebalance deltas smaller than this share of equity are ignored: float noise
# would otherwise trigger endless dust trades (and their fees).
MIN_TRADE_FRACTION = 1e-4


@dataclass
class Trade:
    """One executed fill. ``shares`` is a positive magnitude; ``side`` signs it."""

    code: str
    side: str       # "buy" | "sell"
    shares: float
    price: float    # execution price, slippage already baked in
    notional: float  # shares x close (the reference value fees are charged on)
    fee: float
    slippage: float  # cash lost versus trading exactly at the close


@dataclass
class Holding:
    """A position in the book at the end of a rebalance."""

    code: str
    shares: float
    price: float

    @property
    def value(self) -> float:
        return self.shares * self.price


@dataclass
class Rebalance:
    """Snapshot of one rebalance: what was traded and what the book looks like."""

    day: date
    trades: list[Trade]
    holdings: list[Holding]
    cash: float
    turnover: float  # notional traded on this day only
    fees: float      # fees paid on this day only


@dataclass
class Portfolio:
    cash: float
    positions: dict[str, float] = field(default_factory=dict)  # code -> shares
    equity_curve: list[tuple[date, float]] = field(default_factory=list)
    fees: float = 0.0           # commission + sell tax actually paid
    slippage_cost: float = 0.0  # erosion from trading away from the close
    turnover: float = 0.0       # gross notional traded (buys + sells)
    rebalance_days: int = 0     # days the engine actually recomputed targets
    rebalances: list[Rebalance] = field(default_factory=list)


def _execution_price(mark: float, shares_delta: float, costs: CostModel) -> float:
    """Adverse fill: buys execute above the close, sells below it."""
    if shares_delta > 0:
        return mark * (1.0 + costs.slippage_pct)
    if shares_delta < 0:
        return mark * (1.0 - costs.slippage_pct)
    return mark


def _liquidity_cap(bar: Bar, costs: CostModel) -> float:
    """Max notional tradable in one code on one day (``math.inf`` = no limit).

    Participation is capped against the day's traded notional (volume x raw
    close). Rows without volume are treated as uncapped so a strategy can still
    exit when the data has gaps.
    """
    if costs.max_adv_pct <= 0:
        return math.inf
    traded = float(bar.volume or 0) * float(bar.raw_close or bar.close or 0.0)
    if traded <= 0:
        return math.inf
    return traded * costs.max_adv_pct


def _execute(
    pf: Portfolio, code: str, bar: Bar, shares_delta: float, costs: CostModel
) -> Trade | None:
    """Trade ``shares_delta`` (signed) at an adverse price, returning the fill.

    Honours the day's liquidity cap and the cash balance, so a large order in an
    illiquid name is only partially filled and finishes over the following days.
    Returns ``None`` when nothing could be traded.
    """
    mark = float(bar.close)
    if shares_delta == 0 or mark <= 0:
        return None

    cap = _liquidity_cap(bar, costs)
    if math.isfinite(cap):
        shares_delta = math.copysign(min(abs(shares_delta), cap / mark), shares_delta)

    exec_px = _execution_price(mark, shares_delta, costs)

    if shares_delta > 0:
        # never spend cash we do not have (commission included)
        per_share = exec_px * (1.0 + costs.commission_pct)
        affordable = pf.cash / per_share if per_share > 0 and pf.cash > 0 else 0.0
        shares_delta = min(shares_delta, affordable)
    else:
        # never sell more shares than we hold
        held = pf.positions.get(code, 0.0)
        shares_delta = -min(-shares_delta, max(held, 0.0))

    if shares_delta == 0:
        return None

    notional = abs(shares_delta) * mark
    fee = notional * costs.commission_pct
    if shares_delta < 0:
        fee += notional * costs.sell_tax_pct
    slippage = abs(shares_delta) * abs(exec_px - mark)

    pf.positions[code] = pf.positions.get(code, 0.0) + shares_delta
    pf.cash -= shares_delta * exec_px + fee
    pf.fees += fee
    pf.slippage_cost += slippage
    pf.turnover += notional
    return Trade(
        code=code,
        side="buy" if shares_delta > 0 else "sell",
        shares=abs(shares_delta),
        price=exec_px,
        notional=notional,
        fee=fee,
        slippage=slippage,
    )


def _rebalance(
    pf: Portfolio,
    prices: dict[str, Bar],
    targets: dict[str, float],
    costs: CostModel,
) -> list[Trade]:
    """Move the book towards ``targets`` and return the fills it took to get there."""
    # current total equity marked at today's prices
    equity = pf.cash + sum(
        sh * prices[c].close for c, sh in pf.positions.items() if c in prices
    )
    min_trade = equity * MIN_TRADE_FRACTION
    fills: list[Trade] = []

    # 1) Reductions first (exits and trims) so proceeds can fund the additions.
    for code in list(pf.positions):
        bar = prices.get(code)
        if bar is None or bar.close <= 0:
            continue  # no bar today: cannot trade, keep holding
        delta_value = equity * targets.get(code, 0.0) - pf.positions[code] * bar.close
        if delta_value <= -min_trade:
            trade = _execute(pf, code, bar, delta_value / bar.close, costs)
            if trade is not None:
                fills.append(trade)
        if abs(pf.positions.get(code, 0.0)) < 1e-9:
            pf.positions.pop(code, None)

    # 2) Additions, capped by liquidity and by the cash actually available.
    for code, weight in targets.items():
        bar = prices.get(code)
        if bar is None or bar.close <= 0:
            continue
        delta_value = equity * weight - pf.positions.get(code, 0.0) * bar.close
        if delta_value >= min_trade:
            trade = _execute(pf, code, bar, delta_value / bar.close, costs)
            if trade is not None:
                fills.append(trade)

    return fills


def _snapshot(
    day: date, pf: Portfolio, prices: dict[str, Bar], trades: list[Trade]
) -> Rebalance:
    """Capture the book as it stands immediately after a rebalance.

    Held codes without a bar today are left out of ``holdings``, mirroring how
    the equity curve is marked (in practice every held code has a carried-forward
    bar, so this is only a guard).
    """
    holdings = [
        Holding(code, shares, float(prices[code].close))
        for code, shares in sorted(pf.positions.items())
        if shares and code in prices
    ]
    return Rebalance(
        day=day,
        trades=trades,
        holdings=holdings,
        cash=pf.cash,
        turnover=sum(t.notional for t in trades),
        fees=sum(t.fee for t in trades),
    )


@dataclass
class BacktestResult:
    equity_curve: list[tuple[date, float]]
    final_equity: float
    total_return: float
    max_drawdown: float
    total_fees: float = 0.0
    total_slippage: float = 0.0
    turnover: float = 0.0
    rebalance_days: int = 0
    rebalances: list[Rebalance] = field(default_factory=list)


class Backtester:
    def __init__(self, reader: PITReader):
        self.reader = reader

    def run(
        self,
        strategy: Strategy,
        start: date,
        end: date,
        initial_cash: float = 100_000_000.0,
        costs: CostModel | None = None,
        rebalance: RebalanceFreq | str = RebalanceFreq.DAILY,
        record_ledger: bool = True,
    ) -> BacktestResult:
        model = costs or CostModel()
        frequency = RebalanceFreq(rebalance)
        days = self.reader.trading_days(start, end)
        scheduled = _rebalance_days(days, frequency)
        pf = Portfolio(cash=initial_cash)
        targets: dict[str, float] = {}
        for i, d in enumerate(days):
            # Off-schedule days keep last period's targets and just hold: the
            # book drifts with the market instead of being traded back to weight.
            fresh = None
            if i in scheduled:
                fresh = strategy(d, self.reader) or {}
                targets = fresh
                pf.rebalance_days += 1
            # prices as known at end of this trading day (no future peeking)
            codes = set(targets) | set(pf.positions)
            prices = self.reader.as_of(d, codes) if codes else {}
            if fresh is not None:
                fills = _rebalance(pf, prices, targets, model)
                if record_ledger:
                    pf.rebalances.append(_snapshot(d, pf, prices, fills))
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
            total_fees=pf.fees,
            total_slippage=pf.slippage_cost,
            turnover=pf.turnover,
            rebalance_days=pf.rebalance_days,
            rebalances=pf.rebalances,
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
