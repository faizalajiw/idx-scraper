"""Unit tests for the backtest engine's cost / slippage / liquidity model.

Pure portfolio math — no database and no ``PITReader`` involved.
"""

from __future__ import annotations

from datetime import date

import pytest

from idx_scraper.backtest import (
    Bar,
    CostModel,
    Portfolio,
    _execute,
    _liquidity_cap,
    _rebalance,
)

TRADE_DATE = date(2026, 1, 1)


def make_bar(
    code: str = "AAAA",
    close: float = 100.0,
    volume: int | None = 1_000_000,
    raw_close: float | None = None,
) -> Bar:
    return Bar(
        code=code,
        trade_date=TRADE_DATE,
        close=close,
        raw_close=close if raw_close is None else raw_close,
        volume=volume,
    )


# ------------------------------------------------------------------ cost model


def test_default_cost_model_is_frictionless():
    model = CostModel()
    assert model.is_frictionless
    assert (model.commission_pct, model.sell_tax_pct, model.slippage_pct) == (0.0, 0.0, 0.0)


@pytest.mark.parametrize(
    "model",
    [
        CostModel(commission_pct=0.0015),
        CostModel(sell_tax_pct=0.001),
        CostModel(slippage_pct=0.001),
        CostModel(max_adv_pct=0.02),
    ],
)
def test_any_friction_disables_frictionless_flag(model):
    assert not model.is_frictionless


# ------------------------------------------------------------------ basics


def test_frictionless_rebalance_matches_naive_math():
    pf = Portfolio(cash=1000.0)
    _rebalance(pf, {"AAAA": make_bar()}, {"AAAA": 0.5}, CostModel())
    assert pf.positions["AAAA"] == pytest.approx(5.0)
    assert pf.cash == pytest.approx(500.0)
    assert pf.turnover == pytest.approx(500.0)
    assert pf.fees == 0.0
    assert pf.slippage_cost == 0.0


def test_position_absent_from_targets_is_fully_liquidated():
    pf = Portfolio(cash=0.0, positions={"AAAA": 5.0})
    _rebalance(pf, {"AAAA": make_bar()}, {}, CostModel())
    assert "AAAA" not in pf.positions
    assert pf.cash == pytest.approx(500.0)


def test_code_without_a_bar_today_is_left_untouched():
    pf = Portfolio(cash=0.0, positions={"AAAA": 5.0})
    _rebalance(pf, {}, {"AAAA": 0.0}, CostModel())
    assert pf.positions["AAAA"] == 5.0


# ------------------------------------------------------------------ commission


def test_commission_is_charged_on_the_buy_side():
    costs = CostModel(commission_pct=0.01)  # 1% to keep the math obvious
    pf = Portfolio(cash=10_000.0)
    _rebalance(pf, {"AAAA": make_bar()}, {"AAAA": 0.5}, costs)
    assert pf.positions["AAAA"] == pytest.approx(50.0)
    assert pf.fees == pytest.approx(50.0)  # 5000 notional * 1%
    assert pf.cash == pytest.approx(10_000.0 - 5000.0 - 50.0)


def test_sell_tax_is_charged_on_sales_only():
    costs = CostModel(commission_pct=0.01, sell_tax_pct=0.002)
    pf = Portfolio(cash=0.0, positions={"AAAA": 50.0})
    _rebalance(pf, {"AAAA": make_bar()}, {}, costs)
    # 50 shares * 100 = 5000 notional: 1% commission + 0.2% tax = 60
    assert pf.fees == pytest.approx(60.0)
    assert pf.cash == pytest.approx(5000.0 - 60.0)


# ------------------------------------------------------------------ slippage


def test_buys_fill_above_the_close():
    costs = CostModel(slippage_pct=0.01)
    pf = Portfolio(cash=10_000.0)
    _rebalance(pf, {"AAAA": make_bar()}, {"AAAA": 0.5}, costs)
    assert pf.positions["AAAA"] == pytest.approx(50.0)
    assert pf.cash == pytest.approx(10_000.0 - 50.0 * 101.0)
    assert pf.slippage_cost == pytest.approx(50.0)  # 50 shares * 1 rupiah


def test_sells_fill_below_the_close():
    costs = CostModel(slippage_pct=0.01)
    pf = Portfolio(cash=0.0, positions={"AAAA": 50.0})
    _rebalance(pf, {"AAAA": make_bar()}, {}, costs)
    assert "AAAA" not in pf.positions
    assert pf.cash == pytest.approx(50.0 * 99.0)
    assert pf.slippage_cost == pytest.approx(50.0)


def test_slippage_is_asymmetric_between_buy_and_sell():
    costs = CostModel(slippage_pct=0.01)
    buy = Portfolio(cash=10_000.0)
    _rebalance(buy, {"AAAA": make_bar()}, {"AAAA": 1.0}, costs)
    sell = Portfolio(cash=0.0, positions={"AAAA": buy.positions["AAAA"]})
    _rebalance(sell, {"AAAA": make_bar()}, {}, costs)
    # Round-tripping at an unchanged close must lose money, never gain it.
    assert sell.cash < 10_000.0


# ------------------------------------------------------------------ liquidity


def test_liquidity_cap_limits_the_order_size():
    bar = make_bar(volume=1_000, raw_close=100.0)  # 100k traded notional
    costs = CostModel(max_adv_pct=0.01)  # 1% -> only 1k tradable
    assert _liquidity_cap(bar, costs) == pytest.approx(1000.0)

    pf = Portfolio(cash=1_000_000.0)
    _rebalance(pf, {"AAAA": bar}, {"AAAA": 1.0}, costs)
    assert pf.positions["AAAA"] == pytest.approx(10.0)  # 1k / 100
    assert pf.turnover == pytest.approx(1000.0)


def test_liquidity_cap_is_disabled_by_zero_rate_or_missing_volume():
    costs = CostModel(max_adv_pct=0.01)
    assert _liquidity_cap(make_bar(volume=1_000), CostModel()) == float("inf")
    assert _liquidity_cap(make_bar(volume=None), costs) == float("inf")
    assert _liquidity_cap(make_bar(volume=0), costs) == float("inf")


def test_liquidity_uncapped_fills_whole_order():
    pf = Portfolio(cash=1_000_000.0)
    _rebalance(pf, {"AAAA": make_bar()}, {"AAAA": 1.0}, CostModel(max_adv_pct=0.01))
    # 1M equity / 100 -> 10k shares needs 1M notional; 100M traded * 1% = 1M cap.
    assert pf.positions["AAAA"] == pytest.approx(10_000.0)


# ------------------------------------------------------------------ guards


def test_cannot_spend_more_cash_than_available():
    costs = CostModel(commission_pct=0.1)
    pf = Portfolio(cash=1000.0)
    _rebalance(pf, {"AAAA": make_bar()}, {"AAAA": 1.0}, costs)
    assert pf.cash >= -1e-6
    # equity is 1000 and fees eat 10%, so fewer than 10 shares are affordable
    assert pf.positions["AAAA"] < 10.0


def test_cannot_sell_more_than_held():
    pf = Portfolio(cash=0.0, positions={"AAAA": 5.0})
    trade = _execute(pf, "AAAA", make_bar(), -100.0, CostModel())
    assert trade is not None
    assert (trade.side, trade.shares) == ("sell", pytest.approx(5.0))
    assert "AAAA" not in pf.positions or pf.positions["AAAA"] == pytest.approx(0.0)


def test_execute_returns_a_trade_describing_the_fill():
    pf = Portfolio(cash=1000.0)
    trade = _execute(pf, "AAAA", make_bar(close=100.0), 5.0, CostModel(slippage_pct=0.01))
    assert trade is not None
    assert (trade.code, trade.side) == ("AAAA", "buy")
    assert trade.shares == pytest.approx(5.0)
    assert trade.price == pytest.approx(101.0)  # slippage included
    assert trade.notional == pytest.approx(500.0)  # reference value, close-based
    assert trade.slippage == pytest.approx(5.0)
    assert trade.fee == 0.0


def test_execute_returns_none_when_nothing_can_trade():
    pf = Portfolio(cash=1000.0)
    assert _execute(pf, "AAAA", make_bar(), 0.0, CostModel()) is None
    assert _execute(pf, "AAAA", make_bar(close=0.0), 5.0, CostModel()) is None


def test_dust_rebalances_are_ignored():
    # Equity 1M, one position sitting 0.05 IDR off a 50% target.
    pf = Portfolio(cash=500_000.0, positions={"AAAA": 5000.0005})
    _rebalance(pf, {"AAAA": make_bar()}, {"AAAA": 0.5}, CostModel(commission_pct=0.001))
    assert pf.turnover == 0.0
    assert pf.fees == 0.0
    assert pf.cash == 500_000.0


def test_zero_price_bar_is_never_traded():
    pf = Portfolio(cash=1000.0)
    _rebalance(pf, {"AAAA": make_bar(close=0.0)}, {"AAAA": 1.0}, CostModel())
    assert pf.positions == {}
    assert pf.cash == 1000.0
