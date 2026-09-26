"""Unit tests for the rebalance-cadence scheduling in the backtest engine.

Covers both the calendar bucketing (``_rebalance_days``) and the resulting
behaviour from ``Backtester.run`` using a fake reader, so no database is needed.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from idx_scraper.backtest import (
    Backtester,
    Bar,
    CostModel,
    Portfolio,
    RebalanceFreq,
    _execute,
    _period_key,
    _rebalance_days,
)

MONDAY = date(2026, 1, 5)


def make_bar(code: str = "AAAA", close: float = 100.0, volume: int = 1_000_000) -> Bar:
    return Bar(code=code, trade_date=MONDAY, close=close, raw_close=close, volume=volume)


class FakeReader:
    """Minimal PITReader stand-in: latest bar per code at or before a day."""

    def __init__(self, prices: dict[str, dict[date, float]]):
        self._prices = prices
        self._days = sorted({d for bars in prices.values() for d in bars})

    def trading_days(self, start: date, end: date) -> list[date]:
        return [d for d in self._days if start <= d <= end]

    def as_of(self, d: date, codes) -> dict[str, Bar]:
        out: dict[str, Bar] = {}
        for code in codes:
            bars = self._prices.get(code, {})
            known = [day for day in bars if day <= d]
            if not known:
                continue
            latest = max(known)
            price = bars[latest]
            # Ample volume so the liquidity cap never interferes here.
            out[code] = Bar(code, latest, price, price, 1_000_000)
        return out


def weekdays(count: int, start: date = MONDAY) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < count:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


# ------------------------------------------------------------------ schedule


def test_daily_schedule_covers_every_trading_day():
    days = weekdays(7)
    assert _rebalance_days(days, RebalanceFreq.DAILY) == set(range(7))


def test_weekly_schedule_lands_on_the_first_trading_day_of_each_week():
    days = weekdays(15)  # three Mon-Fri weeks
    assert _rebalance_days(days, RebalanceFreq.WEEKLY) == {0, 5, 10}


def test_monthly_schedule_lands_on_the_first_trading_day_of_each_month():
    days = weekdays(30)  # 2026-01-05 .. 2026-02-13 -> two calendar months
    assert _rebalance_days(days, RebalanceFreq.MONTHLY) == {0, 20}


def test_schedule_starts_mid_week_still_rebalances_immediately():
    tue, wed, thu, fri = date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8), date(2026, 1, 9)
    nxt = date(2026, 1, 12)
    assert _rebalance_days([tue, wed, thu, fri, nxt], RebalanceFreq.WEEKLY) == {0, 4}
    assert _rebalance_days([tue, wed, thu, fri, nxt], RebalanceFreq.MONTHLY) == {0}


def test_holiday_shifts_the_trigger_to_the_next_session():
    # Week 1: Monday missing (holiday) -> Tuesday triggers. Week 2: Monday back.
    week1 = [date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8), date(2026, 1, 9)]
    week2 = [date(2026, 1, 12), date(2026, 1, 13)]
    assert _rebalance_days(week1 + week2, RebalanceFreq.WEEKLY) == {0, 4}


def test_empty_schedule_is_safe():
    assert _rebalance_days([], RebalanceFreq.WEEKLY) == set()


def test_period_keys_group_days_inside_the_same_period():
    assert _period_key(MONDAY, RebalanceFreq.WEEKLY) == _period_key(
        MONDAY + timedelta(days=4), RebalanceFreq.WEEKLY
    )
    assert _period_key(MONDAY, RebalanceFreq.WEEKLY) != _period_key(
        MONDAY + timedelta(days=7), RebalanceFreq.WEEKLY
    )
    assert _period_key(date(2026, 1, 31), RebalanceFreq.MONTHLY) != _period_key(
        date(2026, 2, 1), RebalanceFreq.MONTHLY
    )


# ------------------------------------------------------------------ behaviour


def flip_flop_strategy(days: list[date]):
    """Alternates between two codes on consecutive days — maximum churn."""
    pick = {d: ("AAAA" if i % 2 == 0 else "BBBB") for i, d in enumerate(days)}

    def strategy(d: date, _reader) -> dict[str, float]:
        return {pick[d]: 1.0}

    return strategy


@pytest.mark.parametrize(
    ("freq", "expected_days"),
    [
        (RebalanceFreq.DAILY, 30),
        (RebalanceFreq.WEEKLY, 6),
        (RebalanceFreq.MONTHLY, 2),
    ],
)
def test_rebalance_day_counts_follow_the_cadence(freq, expected_days):
    days = weekdays(30)
    reader = FakeReader({code: {d: 100.0 for d in days} for code in ("AAAA", "BBBB")})
    result = Backtester(reader).run(
        flip_flop_strategy(days), days[0], days[-1], initial_cash=1000.0, rebalance=freq
    )
    assert result.rebalance_days == expected_days


def test_less_frequent_rebalancing_cuts_turnover():
    days = weekdays(30)
    reader = FakeReader({code: {d: 100.0 for d in days} for code in ("AAAA", "BBBB")})
    strategy = flip_flop_strategy(days)

    def turnover(freq):
        run = Backtester(reader).run(
            strategy, days[0], days[-1], initial_cash=1000.0, rebalance=freq
        )
        return run.turnover

    daily, weekly, monthly = turnover(RebalanceFreq.DAILY), turnover(RebalanceFreq.WEEKLY), turnover(RebalanceFreq.MONTHLY)
    assert monthly <= weekly < daily
    # Daily churn on an alternating signal should dwarf a monthly schedule.
    assert daily > monthly * 5


def test_less_frequent_rebalancing_cuts_fees():
    days = weekdays(30)
    reader = FakeReader({code: {d: 100.0 for d in days} for code in ("AAAA", "BBBB")})
    strategy = flip_flop_strategy(days)
    costs = CostModel(commission_pct=0.002, sell_tax_pct=0.001)

    fees = {
        freq: Backtester(reader).run(
            strategy, days[0], days[-1], initial_cash=1000.0, costs=costs, rebalance=freq
        ).total_fees
        for freq in (RebalanceFreq.DAILY, RebalanceFreq.WEEKLY, RebalanceFreq.MONTHLY)
    }
    assert fees[RebalanceFreq.MONTHLY] <= fees[RebalanceFreq.WEEKLY] < fees[RebalanceFreq.DAILY]


# ------------------------------------------------------------------ ledger


def test_ledger_records_exactly_one_event_per_schedule_day():
    days = weekdays(10)
    reader = FakeReader({code: {d: 100.0 for d in days} for code in ("AAAA", "BBBB")})
    result = Backtester(reader).run(
        flip_flop_strategy(days), days[0], days[-1], initial_cash=1000.0,
        rebalance=RebalanceFreq.WEEKLY,
    )
    assert len(result.rebalances) == result.rebalance_days == 2
    assert [event.day for event in result.rebalances] == [days[0], days[5]]


def test_ledger_event_totals_add_up_to_the_run_totals():
    days = weekdays(20)
    reader = FakeReader({code: {d: 100.0 for d in days} for code in ("AAAA", "BBBB")})
    costs = CostModel(commission_pct=0.002, sell_tax_pct=0.001, slippage_pct=0.001)
    result = Backtester(reader).run(
        flip_flop_strategy(days), days[0], days[-1], initial_cash=1000.0,
        costs=costs, rebalance=RebalanceFreq.WEEKLY,
    )
    assert sum(e.turnover for e in result.rebalances) == pytest.approx(result.turnover)
    assert sum(e.fees for e in result.rebalances) == pytest.approx(result.total_fees)
    per_trade_slippage = sum(t.slippage for e in result.rebalances for t in e.trades)
    assert per_trade_slippage == pytest.approx(result.total_slippage)


def test_ledger_positions_describe_the_book_after_each_rebalance():
    days = weekdays(10)
    reader = FakeReader({code: {d: 100.0 for d in days} for code in ("AAAA", "BBBB")})
    result = Backtester(reader).run(
        flip_flop_strategy(days), days[0], days[-1], initial_cash=1000.0,
        rebalance=RebalanceFreq.DAILY,
    )

    first = result.rebalances[0]
    assert [h.code for h in first.holdings] == ["AAAA"]  # day 0 picks AAAA
    assert first.holdings[0].value == pytest.approx(1000.0)
    assert first.cash == pytest.approx(0.0)

    second = result.rebalances[1]  # alternating signal -> a full swap
    assert {(t.code, t.side) for t in second.trades} == {
        ("AAAA", "sell"),
        ("BBBB", "buy"),
    }
    assert [h.code for h in second.holdings] == ["BBBB"]


def test_ledger_cash_reconciles_with_the_trades_it_reports():
    days = weekdays(6)
    reader = FakeReader({code: {d: 100.0 for d in days} for code in ("AAAA", "BBBB")})
    result = Backtester(reader).run(
        flip_flop_strategy(days), days[0], days[-1], initial_cash=1000.0,
        costs=CostModel(commission_pct=0.001), rebalance=RebalanceFreq.MONTHLY,
    )
    assert len(result.rebalances) == 1
    event = result.rebalances[0]
    spent = sum(t.shares * t.price + t.fee for t in event.trades if t.side == "buy")
    received = sum(t.shares * t.price - t.fee for t in event.trades if t.side == "sell")
    assert event.cash == pytest.approx(1000.0 - spent + received)


def test_ledger_reports_a_full_exit_when_the_strategy_goes_to_cash():
    days = weekdays(6)
    reader = FakeReader({"AAAA": {d: 100.0 for d in days}})

    def strategy(d: date, _reader) -> dict[str, float]:
        return {"AAAA": 1.0} if d == days[0] else {}

    result = Backtester(reader).run(
        strategy, days[0], days[-1], initial_cash=1000.0,
        rebalance=RebalanceFreq.WEEKLY,
    )
    assert [h.code for h in result.rebalances[0].holdings] == ["AAAA"]
    final = result.rebalances[-1]
    assert final.holdings == []
    assert [(t.code, t.side) for t in final.trades] == [("AAAA", "sell")]
    assert final.cash == pytest.approx(1000.0)  # flat price, no costs


def test_execute_trade_record_matches_the_book_move():
    pf = Portfolio(cash=1000.0)
    trade = _execute(pf, "AAAA", make_bar(close=100.0), 4.0, CostModel(slippage_pct=0.01))
    assert trade is not None
    assert (trade.side, trade.code) == ("buy", "AAAA")
    assert trade.shares == pytest.approx(4.0)
    assert trade.price == pytest.approx(101.0)
    assert trade.slippage == pytest.approx(4.0)
    assert pf.positions["AAAA"] == pytest.approx(4.0)
    assert pf.cash == pytest.approx(1000.0 - 4.0 * 101.0)


def test_targets_are_still_held_between_rebalance_days():
    """Off-schedule days must not flatten the book back to cash."""
    days = weekdays(10)
    reader = FakeReader({code: {d: 100.0 for d in days} for code in ("AAAA", "BBBB")})
    result = Backtester(reader).run(
        flip_flop_strategy(days), days[0], days[-1], initial_cash=1000.0,
        rebalance=RebalanceFreq.MONTHLY,
    )
    # One rebalance, then nothing traded: the whole curve is flat at the cost-free
    # value for identical prices.
    assert result.rebalance_days == 1
    assert result.turnover == pytest.approx(1000.0)  # single 100% entry
    assert all(eq == pytest.approx(1000.0) for _, eq in result.equity_curve)
