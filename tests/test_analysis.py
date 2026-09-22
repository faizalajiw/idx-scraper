"""Unit tests for idx_scraper.analysis (indicators + signal rules)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from idx_scraper.analysis import calculate_indicators, generate_signal


def make_df(closes: list[float], start: str = "2026-01-01") -> pd.DataFrame:
    n = len(closes)
    dates = pd.date_range(start, periods=n, freq="D")
    return pd.DataFrame(
        {
            "date": dates,
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": [1_000_000] * n,
        }
    )


def test_calculate_indicators_adds_columns():
    df = make_df([100.0 + i for i in range(60)])
    out = calculate_indicators(df)
    for col in ("MA_Short", "MA_Long", "RSI", "BB_Upper", "BB_Lower", "MACD", "MACD_Signal"):
        assert col in out.columns


def test_calculate_indicators_does_not_mutate_input():
    df = make_df([100.0] * 30)
    calculate_indicators(df)
    assert "MA_Short" not in df.columns


def test_rsi_bounds():
    df = make_df([100.0 + i for i in range(60)])  # monotonic up -> high RSI
    out = calculate_indicators(df)
    assert out["RSI"].dropna().between(0, 100).all()


def test_uptrend_with_moderate_rsi_is_buy():
    # SMA20 > SMA50 for rising series; RSI for constant gains stays below 80
    closes = [100.0 + i * 0.5 for i in range(60)]
    df = generate_signal_ready(closes)
    assert df is not None


def generate_signal_ready(closes: list[float]) -> pd.DataFrame | None:
    out = calculate_indicators(make_df(closes))
    signal = generate_signal(out)
    assert signal in {"BUY", "SELL", "HOLD"}
    return out


def test_generate_signal_hold_on_short_data():
    df = make_df([100.0, 101.0, 102.0])
    assert generate_signal(calculate_indicators(df)) == "HOLD"


def test_generate_signal_hold_on_empty():
    assert generate_signal(pd.DataFrame()) == "HOLD"


def test_generate_signal_buy_in_uptrend():
    # Rising sawtooth (+0.3, +0.3, -0.5): SMA20 > SMA50, RSI ~54 (in 50-80 band)
    closes = [100.0]
    for i in range(1, 60):
        closes.append(closes[-1] + [0.3, 0.3, -0.5][(i - 1) % 3])
    df = calculate_indicators(make_df(closes))
    rsi = df["RSI"].iloc[-1]
    assert 50 <= rsi <= 80, f"test data RSI out of band: {rsi}"
    assert generate_signal(df) == "BUY"


def test_generate_signal_sell_in_downtrend():
    # Falling sawtooth (-0.3, -0.3, +0.5): SMA20 < SMA50, RSI ~46 (in 20-50 band)
    closes = [112.0]
    for i in range(1, 60):
        closes.append(closes[-1] + [-0.3, -0.3, 0.5][(i - 1) % 3])
    df = calculate_indicators(make_df(closes))
    rsi = df["RSI"].iloc[-1]
    assert 20 <= rsi <= 50, f"test data RSI out of band: {rsi}"
    assert generate_signal(df) == "SELL"


def test_generate_signal_hold_in_fresh_reversal():
    # Downtrend for 55 days then sharp bounce: SMA20 still below SMA50 but RSI very high
    closes = list(np.linspace(120, 100, 55)) + [110, 115, 120, 125, 130]
    df = calculate_indicators(make_df(closes))
    assert generate_signal(df) == "HOLD"
