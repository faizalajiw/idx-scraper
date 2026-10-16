"""Unit tests for idx_scraper.research.ic_history (derive & map bobot faktor)."""

from __future__ import annotations

import math

import pytest

from idx_scraper.research.composite import apply_factor_layer, factor_adjustment
from idx_scraper.research.ic_history import (
    _storable,
    derive_weights,
    save_ic_summary,
    weights_from_rows_by_key,
)

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


# --------------------------------------------------------------------------- #
# Nilai yang tak bisa ditampung NUMERIC -> NULL (bukan menggagalkan upsert)
# --------------------------------------------------------------------------- #


class _CurPalsu:
    """Cursor palsu: hanya merekam parameter setiap upsert."""

    def __init__(self):
        self.params: list[tuple] = []

    def execute(self, sql, params=None):
        self.params.append(params)


def test_storable_membuang_nilai_di_luar_batas_kolom():
    assert _storable(5, 999_999.0) == 999_999.0  # masih masuk numeric(12,6)
    assert _storable(5, 1_000_000.0) is None
    assert _storable(6, 101.0) is None  # hit_rate/weight numeric(8,6)
    assert _storable(6, 0.833) == 0.833
    assert _storable(7, 6) == 6  # n_days: bukan kolom berbatas
    assert _storable(1, "vol_21d") == "vol_21d"  # factor: bukan numerik


def test_save_ic_summary_inf_nan_dan_raksasa_jadi_null():
    """Regresi 16 Okt 2026: satu nilai inf membuat seluruh upsert gagal.

    inf (IC konstan, std=0), t-stat raksasa dari varian NW yang runtuh, dan
    NaN (faktor tanpa coverage) semuanya harus jadi NULL; nilai wajar utuh.
    """
    cur = _CurPalsu()
    ringkas = [
        {
            "factor": "ob_absorption",
            "mean_ic": -0.0954,
            "icir": -1.273,
            "t_stat": -math.inf,
            "hit_rate": 0.833,
            "n_days": 6,
        },
        {
            "factor": "ob_imbalance",
            "mean_ic": -0.1627,
            "icir": -1.106,
            "t_stat": -3.494e8,
            "hit_rate": 0.833,
            "n_days": 6,
        },
        {"factor": "mom_12_1", "n_days": 3, "mean_ic": math.nan},
    ]
    rows, _ = save_ic_summary(cur, "2026-10-16", {10: ringkas})

    assert rows == 3
    assert [p[5] for p in cur.params] == [None, None, None]  # inf, 3.5e8, NaN
    assert cur.params[0][3] == pytest.approx(-0.0954)  # mean_ic wajar utuh
    assert cur.params[0][4] == pytest.approx(-1.273)  # icir wajar utuh
    assert cur.params[1][3] == pytest.approx(-0.1627)
    assert cur.params[2][3] is None  # NaN -> NULL
