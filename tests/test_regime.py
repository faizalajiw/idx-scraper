"""Unit tests: ADX (Wilder) + klasifikasi regime pasar + gerbang rezim."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from idx_scraper.analysis import (
    ADX_TREND,
    calculate_adx,
    classify_regime,
)
from idx_scraper.health import WIB
from idx_scraper.research.regime import gate


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


def _fake_regime_rows(days: int = 45, rising: bool = True) -> list[dict]:
    return [
        {
            "date": str((pd.Timestamp("2026-01-01") + pd.Timedelta(days=d)).date()),
            "open": 100.0,
            "high": 102.0,
            "low": 99.0,
            "close": 100.0 + d * 0.5 if rising else 200.0 - d * 0.5,
        }
        for d in range(days)  # >= 40: tidak memicu fallback breadth
    ]


def _regime_with_mock(rows: list[dict]):
    from unittest.mock import MagicMock, patch

    from idx_scraper.api.analytics import market

    fake_cur = MagicMock()
    fake_cur.fetchall.return_value = rows
    fake_cm = MagicMock()
    fake_cm.__enter__.return_value = fake_cur
    with patch.object(market, "get_cursor", return_value=fake_cm):
        from idx_scraper.api import analytics

        return analytics.get_market_regime()


def test_market_regime_shape():
    out = _regime_with_mock(_fake_regime_rows())
    assert out["source"] == "index_summary_daily"
    assert "as_of" in out
    # 28 baris < 40 -> dianggap terlalu pendek... 28 < 40 memicu fallback
    # breadth; karena kedua cursor sama-sama mock, hasil tetap terhitung.
    assert out["regime"] in (None, "TRENDING_UP", "TRENDING_DOWN", "RANGING", "TRANSITION")


# --------------------------------------------------------------------------- #
# gate (spec 6.5 + keputusan pemilik Q5)
# --------------------------------------------------------------------------- #


def test_gerbang_menolak_saat_tren_turun_dengan_adx_kuat():
    g = gate("TRENDING_DOWN", 31.5)
    assert g["blocked"] is True and "tren turun" in g["reason"]


def test_gerbang_tidak_menolak_saat_adx_di_bawah_ambang_tren():
    assert gate("TRENDING_DOWN", 20.0)["blocked"] is False


def test_gerbang_tidak_menolak_saat_rezim_transisi_atau_none():
    assert gate("TRANSITION", 30.0)["blocked"] is False
    assert gate(None, None)["blocked"] is False


def test_gerbang_tidak_menolak_saat_ranging():
    assert gate("RANGING", 18.0)["blocked"] is False


def test_gerbang_volatile_ditandai_tapi_tidak_memblokir():
    g = gate("TRENDING_UP", 30.0, "VOLATILE")
    assert g["blocked"] is False and g["volatile"] is True


def test_gerbang_basi_tidak_memblokir_tapi_ditandai():
    g = gate("TRENDING_DOWN", 31.5, "NORMAL", regime_fresh=False)
    assert g["blocked"] is False and g["regime_fresh"] is False
    assert "basi" in g["reason"]


def test_alasan_gerbang_selalu_terisi():
    semua = ("TRENDING_DOWN", "TRENDING_UP", "RANGING", "TRANSITION", None)
    alasan = [gate(rezim, 30.0)["reason"] for rezim in semua]
    assert all(a for a in alasan)


# --------------------------------------------------------------------------- #
# gerbang di banner rezim: kesegaran sesi indeks ikut menentukan
# --------------------------------------------------------------------------- #


def test_gerbang_banner_memblokir_saat_tren_turun_di_sesi_terakhir():
    from idx_scraper.api import analytics

    out = {"regime": "TRENDING_DOWN", "adx": 31.5, "vol_state": "NORMAL", "as_of": "2026-10-16"}
    g = analytics._gate_for(out, datetime(2026, 10, 18, 9, 0, tzinfo=WIB))
    assert g["blocked"] is True


def test_gerbang_banner_tidak_memblokir_saat_sesi_indeks_basi():
    from idx_scraper.api import analytics

    out = {"regime": "TRENDING_DOWN", "adx": 31.5, "vol_state": "NORMAL", "as_of": "2026-10-02"}
    g = analytics._gate_for(out, datetime(2026, 10, 18, 9, 0, tzinfo=WIB))
    assert g["blocked"] is False and "basi" in g["reason"]


def test_banner_rezim_membawa_gerbang():
    out = _regime_with_mock(_fake_regime_rows())
    assert "gate" in out
    # Sesi mock jauh di masa lalu -> rezim basi: ditandai, tapi layar tidak dibisukan.
    assert out["gate"]["regime_fresh"] is False
    assert out["gate"]["blocked"] is False
