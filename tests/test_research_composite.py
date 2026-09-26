"""Unit tests for idx_scraper.research.composite (lapisan faktor hold-check).

Pure functions only — no DB. Expected values dihitung manual.
"""

from __future__ import annotations

import pytest

from idx_scraper.research.composite import (
    FACTOR_WEIGHTS,
    MAX_ADJUSTMENT,
    apply_factor_layer,
    factor_adjustment,
    ranks_from_rows,
    verdict_for,
)

# --------------------------------------------------------------------------- #
# ranks_from_rows
# --------------------------------------------------------------------------- #


def test_ranks_from_rows_percentile_ordering():
    # 4 emiten: vol naik AAA < BBB < CCC < DDD -> vol_pct 0.25/0.5/0.75/1.0
    rows = [
        {"code": "AAA", "vol_21d": 0.01, "turn_21d": 10.0, "dist_52w": -0.05},
        {"code": "BBB", "vol_21d": 0.02, "turn_21d": 12.0, "dist_52w": 0.00},
        {"code": "CCC", "vol_21d": 0.03, "turn_21d": 14.0, "dist_52w": 0.05},
        {"code": "DDD", "vol_21d": 0.04, "turn_21d": 16.0, "dist_52w": 0.10},
    ]
    ranks = ranks_from_rows(rows)
    assert ranks["AAA"]["vol_pct"] == pytest.approx(0.25)
    assert ranks["DDD"]["vol_pct"] == pytest.approx(1.0)
    # turnover searah: AAA terendah
    assert ranks["AAA"]["turnover_pct"] == pytest.approx(0.25)
    # dist_52w searah: DDD tertinggi
    assert ranks["DDD"]["dist_52w_pct"] == pytest.approx(1.0)


def test_ranks_from_rows_skips_incomplete():
    rows = [
        {"code": "AAA", "vol_21d": 0.01, "turn_21d": None, "dist_52w": -0.05},
        {"code": "BBB", "vol_21d": 0.02, "turn_21d": 12.0, "dist_52w": 0.00},
    ]
    ranks = ranks_from_rows(rows)
    assert "AAA" not in ranks
    # BBB jadi satu-satunya -> percentile 1.0 di semua faktor
    assert ranks["BBB"]["vol_pct"] == pytest.approx(1.0)


def test_ranks_from_rows_empty():
    assert ranks_from_rows([]) == {}


# --------------------------------------------------------------------------- #
# factor_adjustment
# --------------------------------------------------------------------------- #


def test_adjustment_zero_at_market_median():
    # Semua faktor di median (0.5) -> F=0 -> adjustment 0
    adj, reasons = factor_adjustment(
        {"vol_pct": 0.5, "turnover_pct": 0.5, "dist_52w_pct": 0.5}
    )
    assert adj == pytest.approx(0.0)
    assert reasons == []


def test_adjustment_most_negative_for_high_vol_liquid_far_from_high():
    # Kasus terburuk: vol & turnover persentil 1.0, jauh dari puncak 52w (0.0)
    adj, _ = factor_adjustment(
        {"vol_pct": 1.0, "turnover_pct": 1.0, "dist_52w_pct": 0.0}
    )
    assert adj == pytest.approx(-MAX_ADJUSTMENT)


def test_adjustment_most_positive_for_low_vol_illiquid_at_high():
    adj, _ = factor_adjustment(
        {"vol_pct": 0.0, "turnover_pct": 0.0, "dist_52w_pct": 1.0}
    )
    assert adj == pytest.approx(+MAX_ADJUSTMENT)


def test_adjustment_weights_are_respected():
    # Hanya vol di ekstrem atas; kontribusinya = -w_vol * MAX_ADJUSTMENT
    adj, _ = factor_adjustment(
        {"vol_pct": 1.0, "turnover_pct": 0.5, "dist_52w_pct": 0.5}
    )
    assert adj == pytest.approx(-FACTOR_WEIGHTS["vol"] * MAX_ADJUSTMENT)


def test_adjustment_missing_factor_gives_zero():
    adj, reasons = factor_adjustment({"vol_pct": 0.9})
    assert adj == 0.0
    assert reasons == []


def test_adjustment_reasons_fire_at_thresholds():
    _, reasons = factor_adjustment(
        {"vol_pct": 0.9, "turnover_pct": 0.1, "dist_52w_pct": 0.9}
    )
    assert any("Volatilitas" in r for r in reasons)
    assert any("Likuiditas rendah" in r for r in reasons)
    assert any("puncak 52-minggu" in r for r in reasons)
    # Tidak ada alasan di tengah
    _, mid = factor_adjustment(
        {"vol_pct": 0.5, "turnover_pct": 0.5, "dist_52w_pct": 0.5}
    )
    assert mid == []


# --------------------------------------------------------------------------- #
# verdict_for + apply_factor_layer
# --------------------------------------------------------------------------- #


def test_verdict_bands():
    assert verdict_for(80) == "STRONG HOLD"
    assert verdict_for(75) == "STRONG HOLD"
    assert verdict_for(60) == "HOLD"
    assert verdict_for(40) == "TRIM"
    assert verdict_for(10) == "EXIT"


def test_apply_factor_layer_flips_verdict_down():
    # Base 78 (STRONG HOLD); faktor terburuk -10 -> 68 -> HOLD
    score, verdict, reasons, adj = apply_factor_layer(
        78.0,
        "STRONG HOLD",
        ["Sinyal teknikal BUY"],
        {"vol_pct": 1.0, "turnover_pct": 1.0, "dist_52w_pct": 0.0},
    )
    assert score == pytest.approx(68.0)
    assert verdict == "HOLD"
    assert adj == pytest.approx(-10.0)
    assert len(reasons) == 4  # 1 teknikal + 3 faktor


def test_apply_factor_layer_flips_verdict_up():
    # Base 50 (TRIM batas bawah? 50 -> TRIM); faktor terbaik +10 -> 60 -> HOLD
    score, verdict, _, adj = apply_factor_layer(
        50.0,
        "TRIM",
        [],
        {"vol_pct": 0.0, "turnover_pct": 0.0, "dist_52w_pct": 1.0},
    )
    assert score == pytest.approx(60.0)
    assert verdict == "HOLD"
    assert adj == pytest.approx(10.0)


def test_apply_factor_layer_no_coverage_is_noop():
    score, verdict, reasons, adj = apply_factor_layer(60.0, "HOLD", ["x"], None)
    assert (score, verdict, reasons, adj) == (60.0, "HOLD", ["x"], 0.0)


def test_apply_factor_layer_clamped_at_bounds():
    # Base 100 + terburuk -> tetap <= 100; base 0 + terbaik -> tetap >= 0
    s1, _, _, _ = apply_factor_layer(
        100.0, "STRONG HOLD", [], {"vol_pct": 1.0, "turnover_pct": 1.0, "dist_52w_pct": 0.0}
    )
    assert s1 == pytest.approx(90.0)
    s2, _, _, _ = apply_factor_layer(
        0.0, "EXIT", [], {"vol_pct": 0.0, "turnover_pct": 0.0, "dist_52w_pct": 1.0}
    )
    assert s2 == pytest.approx(10.0)
