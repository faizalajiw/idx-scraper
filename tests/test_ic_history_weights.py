"""Unit tests for idx_scraper.research.ic_history (derive & map bobot faktor)."""

from __future__ import annotations

import pytest

from idx_scraper.research.composite import apply_factor_layer, factor_adjustment
from idx_scraper.research.ic_history import derive_weights, weights_from_rows_by_key

# --------------------------------------------------------------------------- #
# derive_weights
# --------------------------------------------------------------------------- #


def test_derive_weights_eligible_only():
    rows = [
        {"factor": "vol_21d", "mean_ic": -0.08, "icir": -0.9},
        {"factor": "turnover_21d", "mean_ic": -0.04, "icir": -0.9},  # |IC| < 0.05
        {"factor": "dist_52w_high", "mean_ic": 0.05, "icir": 0.5},  # pas di ambang
        {"factor": "rev_1d", "mean_ic": 0.30, "icir": 3.0},  # di luar white-list
    ]
    w = derive_weights(rows)
    assert set(w) == {"vol_21d", "dist_52w_high"}
    assert w["vol_21d"] == pytest.approx(0.08 / 0.13)
    assert w["dist_52w_high"] == pytest.approx(0.05 / 0.13)


def test_derive_weights_icir_threshold():
    # |IC| besar tapi ICIR kecil -> tidak eligible
    rows = [{"factor": "vol_21d", "mean_ic": -0.10, "icir": -0.3}]
    assert derive_weights(rows) == {}


def test_derive_weights_none_eligible_returns_empty():
    rows = [{"factor": "vol_21d", "mean_ic": -0.01, "icir": -0.1}]
    assert derive_weights(rows) == {}


def test_derive_weights_horizon_filter():
    rows = [
        {"factor": "vol_21d", "mean_ic": -0.08, "icir": -0.9, "horizon": 10},
        {"factor": "dist_52w_high", "mean_ic": 0.05, "icir": 0.5, "horizon": 5},
    ]
    w = derive_weights(rows, horizon=10)
    # baris horizon 5 difilter keluar -> hanya vol_21d
    assert set(w) == {"vol_21d"}
    assert w["vol_21d"] == pytest.approx(1.0)


def test_weights_by_key_mapping():
    w = {"vol_21d": 0.6, "dist_52w_high": 0.4}
    assert weights_from_rows_by_key(w) == {"vol": 0.6, "dist_52w": 0.4}


# --------------------------------------------------------------------------- #
# apply_factor_layer dengan bobot custom (dari DB)
# --------------------------------------------------------------------------- #


def test_factor_adjustment_uses_custom_weights():
    pcts = {"vol_pct": 1.0, "turnover_pct": 1.0, "dist_52w_pct": 0.0}
    # Bobot nol untuk semuanya -> penyesuaian 0
    adj0, _ = factor_adjustment(pcts, weights={"vol": 0.0, "turnover": 0.0, "dist_52w": 0.0})
    assert adj0 == 0.0
    # Bobot penuh ke vol saja -> -MAX_ADJUSTMENT
    adj1, _ = factor_adjustment(pcts, weights={"vol": 1.0, "turnover": 0.0, "dist_52w": 0.0})
    assert adj1 == pytest.approx(-10.0)


def test_apply_factor_layer_with_stored_weights():
    # base 78 STRONG HOLD; bobot vol 1.0, emiten vol persentil 100% -> -10 -> HOLD
    score, verdict, _, adj = apply_factor_layer(
        78.0,
        "STRONG HOLD",
        [],
        {"vol_pct": 1.0, "turnover_pct": 0.5, "dist_52w_pct": 0.5},
        weights={"vol": 1.0, "turnover": 0.0, "dist_52w": 0.0},
    )
    assert score == pytest.approx(68.0)
    assert verdict == "HOLD"
    assert adj == pytest.approx(-10.0)


def test_apply_factor_layer_partial_weights():
    # Bobot hanya turnover: turnover 0.5 (median) -> kontribusi 0 walau ada bobot
    score, _, _, adj = apply_factor_layer(
        60.0,
        "HOLD",
        [],
        {"vol_pct": 0.5, "turnover_pct": 0.5, "dist_52w_pct": 0.5},
        weights={"turnover": 1.0},
    )
    assert score == pytest.approx(60.0)
    assert adj == 0.0
