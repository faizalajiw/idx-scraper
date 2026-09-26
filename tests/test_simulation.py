"""Unit tests for the backtest service primitives (no database required).

Only the pure helpers are covered here: series slicing, strategy selection,
parameter validation, benchmark construction, and metric math. The DB-backed
``run_backtest`` path is exercised end-to-end against Postgres instead.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from idx_scraper.api import simulation as sim


def make_series(**closes_by_code: list[float]) -> sim.SeriesMap:
    """Build a SeriesMap from {code: [close, ...]} on consecutive days."""
    start = date(2026, 1, 1)
    return {
        code: [(start + timedelta(days=i), float(c)) for i, c in enumerate(closes)]
        for code, closes in closes_by_code.items()
    }


# ------------------------------------------------------------------ slicing


def test_closes_respects_as_of_day_and_tail():
    series = make_series(A=[10.0, 20.0, 30.0, 40.0])
    points = series["A"]
    assert sim._closes(points, points[1][0]) == [10.0, 20.0]
    assert sim._closes(points, points[-1][0], n=2) == [30.0, 40.0]


def test_closes_before_first_bar_is_empty():
    series = make_series(A=[10.0, 20.0])
    assert sim._closes(series["A"], date(2025, 12, 31)) == []


# ------------------------------------------------------------------ strategies


def test_equal_weight_spreads_capital_and_handles_empty():
    assert sim._equal_weight([]) == {}
    assert sim._equal_weight(["A", "B", "C", "D"]) == {
        "A": 0.25,
        "B": 0.25,
        "C": 0.25,
        "D": 0.25,
    }


def test_ma_cross_selects_uptrend_only():
    series = make_series(
        UP=[float(i) for i in range(1, 61)],
        DOWN=[float(i) for i in range(60, 0, -1)],
    )
    day = series["UP"][-1][0]
    assert sim._select_ma_cross(series, day, short=5, long=20) == ["UP"]


def test_ma_cross_skips_codes_without_enough_history():
    series = make_series(SHORT=[float(i) for i in range(1, 11)])
    day = series["SHORT"][-1][0]
    assert sim._select_ma_cross(series, day, short=5, long=20) == []


def test_momentum_ranks_and_truncates():
    series = make_series(
        FAST=[100.0, 100.0, 140.0],  # +40% over 2 bars
        MID=[100.0, 100.0, 110.0],  # +10%
        SLOW=[100.0, 100.0, 95.0],  # -5%
    )
    day = series["FAST"][-1][0]
    assert sim._select_momentum(series, day, lookback=2, top_n=1) == ["FAST"]
    assert sim._select_momentum(series, day, lookback=2, top_n=2) == ["FAST", "MID"]


def test_rsi_oversold_picks_declining_series():
    series = make_series(
        DOWN=[float(c) for c in range(100, 85, -1)],  # 15 bars, all lower -> RSI 0
        UP=[float(c) for c in range(85, 100)],  # 15 bars, all higher -> RSI 100
    )
    day = series["DOWN"][-1][0]
    assert sim._select_rsi_oversold(series, day, period=14, threshold=35) == ["DOWN"]
    # Boundary: an all-down series has RSI exactly 0, excluded by a 0 threshold.
    assert sim._select_rsi_oversold(series, day, period=14, threshold=0) == []


def test_rsi_needs_enough_history():
    series = make_series(TINY=[100.0, 99.0, 98.0])
    assert sim._rsi([100.0, 99.0, 98.0], period=14) is None
    day = series["TINY"][-1][0]
    assert sim._select_rsi_oversold(series, day, period=14, threshold=35) == []


def test_rebalance_options_match_the_engine_enum():
    from idx_scraper.backtest import RebalanceFreq

    ids = {o["id"] for o in sim.REBALANCE_OPTIONS}
    assert ids == {f.value for f in RebalanceFreq}
    assert sim.DEFAULT_REBALANCE in ids
    for option in sim.REBALANCE_OPTIONS:
        assert option["label"] and option["description"]


def test_config_exposes_strategies_costs_and_cadences():
    cfg = sim.get_config()
    assert set(cfg) == {
        "strategies",
        "cost_defaults",
        "rebalance_options",
        "rebalance_default",
        "max_codes",
        "max_days",
    }
    assert cfg["cost_defaults"] == sim.DEFAULT_COSTS


def test_cost_resolution_falls_back_and_honours_explicit_zero():
    assert sim._resolve_costs(None) == sim.DEFAULT_COSTS
    assert sim._resolve_costs({"commission_pct": 0.0})["commission_pct"] == 0.0
    resolved = sim._resolve_costs({"slippage_pct": 0.004})
    assert resolved["slippage_pct"] == 0.004
    assert resolved["commission_pct"] == sim.DEFAULT_COSTS["commission_pct"]


@pytest.mark.parametrize("costs", [{"bogus": 1}, {"slippage_pct": -1}, {"commission_pct": 0.9}])
def test_cost_resolution_rejects_invalid_input(costs):
    with pytest.raises(ValueError):
        sim._resolve_costs(costs)


def test_buy_and_hold_benchmark_charges_entry_friction():
    series = make_series(A=[100.0, 100.0])
    days = [d for d, _ in series["A"]]
    free = sim._buy_and_hold_curve(series, days, 1000.0)
    charged = sim._buy_and_hold_curve(
        series, days, 1000.0, commission_pct=0.01, slippage_pct=0.01
    )
    assert free[0][1] == pytest.approx(1000.0)
    # 1% commission shrinks the deployed capital; 1% slippage raises the entry
    # price by the same factor, so the two cancel at a flat price.
    assert charged[0][1] == pytest.approx(1000.0 * 0.99 / 1.01)


def test_strategy_registry_is_complete_and_labelled():
    ids = {s["id"] for s in sim.list_strategies()}
    assert ids == {"ma_cross", "momentum_top", "rsi_oversold"}
    for info in sim.list_strategies():
        assert info["name"] and info["description"]
        for p in info["params"]:
            assert p["key"] and p["label"]
            assert p["min"] <= p["default"] <= p["max"]


# ------------------------------------------------------------------ params


def test_resolve_params_uses_defaults_and_overrides():
    spec = sim.STRATEGIES["ma_cross"]
    assert sim._resolve_params(spec, None) == {"short": 20.0, "long": 50.0}
    assert sim._resolve_params(spec, {"short": 10}) == {"short": 10.0, "long": 50.0}


@pytest.mark.parametrize(
    "params",
    [{"nope": 1}, {"short": 1}, {"long": 999}, {"short": "abc"}],
)
def test_resolve_params_rejects_invalid_input(params):
    with pytest.raises(ValueError):
        sim._resolve_params(sim.STRATEGIES["ma_cross"], params)


# ------------------------------------------------------------------ benchmark


def test_buy_and_hold_curve_weights_equally():
    series = make_series(A=[100.0, 110.0, 120.0], B=[100.0, 100.0, 90.0])
    days = [d for d, _ in series["A"]]
    curve = sim._buy_and_hold_curve(series, days, 1000.0)
    assert [round(eq, 2) for _, eq in curve] == [1000.0, 1050.0, 1050.0]


def test_buy_and_hold_curve_empty_when_no_series():
    assert sim._buy_and_hold_curve({}, [date(2026, 1, 1)], 1000.0) == []


# ------------------------------------------------------------------ metrics


def test_metrics_return_drawdown_and_sharpe():
    d = date(2026, 1, 1)
    curve = [
        (d, 100.0),
        (d + timedelta(days=1), 120.0),
        (d + timedelta(days=2), 90.0),
        (d + timedelta(days=3), 150.0),
    ]
    m = sim._compute_metrics(curve, 100.0)
    assert m.final_equity == 150.0
    assert m.total_return == pytest.approx(0.5)
    assert m.max_drawdown == pytest.approx(0.25)
    assert m.annualized_return is not None and m.annualized_return > 0
    assert m.volatility is not None and m.volatility > 0
    assert m.sharpe is not None


def test_metrics_flat_curve_is_safe():
    d = date(2026, 1, 1)
    m = sim._compute_metrics([(d, 100.0), (d + timedelta(days=1), 100.0)], 100.0)
    assert m.total_return == 0.0
    assert m.max_drawdown == 0.0
    assert m.volatility == 0.0
    assert m.sharpe is None  # zero volatility -> undefined, surfaced as None


def test_metrics_empty_curve_falls_back_to_initial_cash():
    m = sim._compute_metrics([], 500.0)
    assert m.final_equity == 500.0
    assert m.total_return == 0.0
    assert m.annualized_return is None


def test_metrics_dict_serializes_none():
    out = sim._metrics_dict(sim._compute_metrics([], 100.0))
    assert out["sharpe"] is None
    assert set(out) == {
        "final_equity",
        "total_return",
        "annualized_return",
        "max_drawdown",
        "volatility",
        "sharpe",
    }
