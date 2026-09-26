"""Unit tests: ADX (Wilder) + klasifikasi regime pasar."""

from __future__ import annotations

import numpy as np
import pandas as pd

from idx_scraper.analysis import (
    ADX_TREND,
    calculate_adx,
    classify_regime,
)


def make_ohlc(closes: list[float], spread: float = 1.0) -> pd.DataFrame:
    n = len(closes)
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=n, freq="B"),
            "close": closes,
            "high": [c + spread for c in closes],
            "low": [c - spread for c in closes],
            "open": closes,
        }
    )


def _adx_of(closes: list[float], spread: float = 1.0) -> pd.DataFrame:
    return calculate_adx(make_ohlc(closes, spread))


# --------------------------------------------------------------------------- #
# calculate_adx
# --------------------------------------------------------------------------- #


def test_adx_warmup_is_nan():
    closes = [100 + (i % 3) for i in range(60)]  # sideways kecil
    out = _adx_of(closes)
    assert out["ADX"].iloc[:13].isna().all()
    assert pd.notna(out["ADX"].iloc[-1])


def test_adx_low_on_tight_range():
    # Sideways sempit: +DM dan -DM saling menghapus -> ADX rendah (< 25)
    rng = np.random.default_rng(5)
    closes = list(100 + rng.normal(0, 0.15, 120).cumsum())
    out = _adx_of(closes, spread=0.5)
    assert out["ADX"].iloc[-1] < ADX_TREND


def test_adx_high_on_strong_trend():
    # Trend naik konsisten tanpa pullback: +DM dominan -> ADX tinggi
    closes = [100.0 + i * 1.0 for i in range(120)]
    out = _adx_of(closes, spread=0.3)
    assert out["ADX"].iloc[-1] >= ADX_TREND
    assert out["DI_Plus"].iloc[-1] > out["DI_Minus"].iloc[-1]


def test_adx_di_dominance_direction():
    down = [200.0 - i * 1.0 for i in range(120)]
    out = _adx_of(down, spread=0.3)
    assert out["DI_Minus"].iloc[-1] > out["DI_Plus"].iloc[-1]


def test_adx_no_lookahead():
    """ADX pada data masa lalu tidak berubah saat masa depan berubah."""
    closes = list(100 + np.cumsum(np.random.default_rng(1).normal(0, 1, 100)))
    a = _adx_of(closes)
    b = _adx_of(closes[:-10] + [500.0] * 10)
    pd.testing.assert_frame_equal(a.iloc[:-10], b.iloc[:-10])


# --------------------------------------------------------------------------- #
# classify_regime
# --------------------------------------------------------------------------- #


def test_classify_trending_up():
    closes = [100.0 + i * 1.0 for i in range(120)]
    out = _adx_of(closes, spread=0.3)
    r = classify_regime(out)
    assert r["regime"] == "TRENDING_UP"
    assert r["adx"] >= ADX_TREND
    assert r["vol_state"] in ("VOLATILE", "NORMAL", "QUIET")


def test_classify_trending_down():
    closes = [200.0 - i * 1.0 for i in range(120)]
    out = _adx_of(closes, spread=0.3)
    r = classify_regime(out)
    assert r["regime"] == "TRENDING_DOWN"


def test_classify_ranging():
    rng = np.random.default_rng(5)
    closes = list(100 + rng.normal(0, 0.15, 120).cumsum())
    out = _adx_of(closes, spread=0.5)
    r = classify_regime(out)
    assert r["regime"] in ("RANGING", "TRANSITION")
    assert r["realized_vol_annual"] is not None


def test_classify_insufficient_data():
    out = _adx_of([100.0] * 10)
    r = classify_regime(out)
    assert r["regime"] is None
    assert r["adx"] is None


# --------------------------------------------------------------------------- #
# get_market_regime (DB) — smoke test keterisolutan
# --------------------------------------------------------------------------- #


def test_market_regime_shape():
    from unittest.mock import MagicMock, patch

    from idx_scraper.api import analytics

    fake_rows = [
        {
            "date": str((pd.Timestamp("2026-01-01") + pd.Timedelta(days=d)).date()),
            "open": 100.0,
            "high": 102.0,
            "low": 99.0,
            "close": 100.0 + d * 0.5,
        }
        for d in range(45)  # >= 40: tidak memicu fallback breadth
    ]
    fake_cur = MagicMock()
    fake_cur.fetchall.return_value = fake_rows
    fake_cm = MagicMock()
    fake_cm.__enter__.return_value = fake_cur
    with patch.object(analytics, "get_cursor", return_value=fake_cm):
        out = analytics.get_market_regime()
    assert out["source"] == "index_summary_daily"
    assert "as_of" in out
    # 28 baris < 40 -> dianggap terlalu pendek... 28 < 40 memicu fallback
    # breadth; karena kedua cursor sama-sama mock, hasil tetap terhitung.
    assert out["regime"] in (None, "TRENDING_UP", "TRENDING_DOWN", "RANGING", "TRANSITION")
