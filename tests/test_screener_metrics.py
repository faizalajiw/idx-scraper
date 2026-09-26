"""Unit tests: signal_series + metrik riset screener (ATR%, 52w, days since signal)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from idx_scraper.analysis import calculate_indicators, generate_signal, signal_series


def make_df(closes: list[float], start: str = "2026-01-01") -> pd.DataFrame:
    n = len(closes)
    return pd.DataFrame(
        {
            "date": pd.date_range(start, periods=n, freq="B"),
            "close": closes,
            "volume": [1_000_000] * n,
        }
    )


# --------------------------------------------------------------------------- #
# signal_series
# --------------------------------------------------------------------------- #


def test_signal_series_matches_generate_signal():
    """Rule per-baris dan generate_signal harus setuju di baris terakhir."""
    for seed in range(8):
        rng = np.random.default_rng(seed)
        closes = list(100 + np.cumsum(rng.normal(0, 1.2, 80)))
        df = calculate_indicators(make_df(closes))
        assert generate_signal(df) == signal_series(df).iloc[-1]


def test_signal_series_values_and_warmup():
    # Sawtooth naik: BUY mulai saat MA50 valid (idx 49); sebelum itu HOLD
    closes = [100.0]
    for i in range(1, 60):
        closes.append(closes[-1] + [0.3, 0.3, -0.5][(i - 1) % 3])
    df = calculate_indicators(make_df(closes))
    sigs = signal_series(df)
    assert sigs.iloc[:49].eq("HOLD").all()  # warmup MA50
    assert sigs.iloc[-1] == "BUY"


def test_signal_series_downtrend_sell():
    closes = [112.0]
    for i in range(1, 60):
        closes.append(closes[-1] + [-0.3, -0.3, 0.5][(i - 1) % 3])
    df = calculate_indicators(make_df(closes))
    assert signal_series(df).iloc[-1] == "SELL"


def test_signal_series_empty():
    assert signal_series(pd.DataFrame()).empty


# --------------------------------------------------------------------------- #
# Metrik screener (dihitung dengan rumus yang sama seperti get_screener)
# --------------------------------------------------------------------------- #


def test_days_since_signal_counts_trading_rows():
    """Hari sejak sinyal = jumlah baris sejak sinyal non-HOLD terakhir."""
    # Deret sinyal sintetis: sinyal BUY di idx 5, lalu 10 baris HOLD.
    sigs = pd.Series(["HOLD"] * 5 + ["BUY"] + ["HOLD"] * 10)
    non_hold = sigs[sigs != "HOLD"]
    days = int(len(sigs) - 1 - non_hold.index[-1])
    assert days == 10


def test_days_since_signal_overbought_uptrend_holds():
    # Uptrend naik tajam di ujung -> RSI >80 -> HOLD, meski SMA20>SMA50.
    # Jumlah hari sejak sinyal = panjang ekor HOLD.
    closes = [100.0]
    for i in range(1, 60):
        closes.append(closes[-1] + [0.3, 0.3, -0.5][(i - 1) % 3])
    closes += [closes[-1] + 1.5 * (i + 1) for i in range(15)]  # ralli tajam
    df = calculate_indicators(make_df(closes))
    sigs = signal_series(df)
    assert sigs.iloc[-1] == "HOLD"  # overbought
    non_hold = sigs[sigs != "HOLD"]
    days = int(len(sigs) - 1 - non_hold.index[-1]) if len(non_hold) else None
    assert days is not None and days >= 1


def test_days_since_signal_none_when_never_signaled():
    closes = [100.0] * 70  # flat -> RSI NaN-ish, tidak pernah BUY/SELL
    df = calculate_indicators(make_df(closes))
    sigs = signal_series(df)
    non_hold = sigs[sigs != "HOLD"]
    assert len(non_hold) == 0


def test_atr_pct_manual_value():
    """ATR(14) pada seri constant-range: TR konstan 2.0 -> ATR=2 -> 2/100=2%."""
    n = 30
    closes = [100.0] * n
    highs = [101.0] * n
    lows = [99.0] * n
    dates = pd.date_range("2026-01-01", periods=n, freq="B")
    df = pd.DataFrame(
        {
            "date": dates,
            "close": closes,
            "high": highs,
            "low": lows,
            "volume": [1_000_000] * n,
        }
    )
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(14).mean()
    atr_pct = float(atr.iloc[-1] / df["close"].iloc[-1] * 100)
    assert atr_pct == pytest.approx(2.0)


def test_dist_52w_at_high_is_zero():
    closes = list(np.linspace(50, 100, 70)) + [100.0] * 10
    df = make_df(closes)
    close = df["close"]
    hi_252 = close.rolling(252, min_periods=63).max()
    dist = float(close.iloc[-1] / hi_252.iloc[-1] - 1.0)
    assert dist == pytest.approx(0.0)


def test_dist_52w_below_high_negative():
    closes = [100.0] * 70 + [80.0]
    df = make_df(closes)
    close = df["close"]
    hi_252 = close.rolling(252, min_periods=63).max()
    dist = float(close.iloc[-1] / hi_252.iloc[-1] - 1.0)
    assert dist == pytest.approx(-0.20)
