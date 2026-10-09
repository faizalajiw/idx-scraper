"""Tests for the pure recommendation scorer (research/recommend.py).

Kontrak yang diuji (2026-10-09):

* Skor berasal dari ``score_components`` + ``COMPONENT_WEIGHTS`` (hasil
  walk-forward ``research.score_eval``) — bukan poin yang ditulis tangan.
* **Grade bukan bagian dari skor satu emiten.** Grade = peringkat relatif pool
  hari itu (``assign_grades`` + ``BAND_SHARES``), karena distribusi skor
  ber-titik-massa membuat ambang absolut tidak lagi berarti "top N%".
* Pola jejak smart money menjadi alasan/peringatan; bobotnya hanya boleh datang
  dari komponen yang lolos walk-forward, bukan dari kata keyakinan.
"""

from __future__ import annotations

from idx_scraper.research.recommend import (
    COMPONENT_WEIGHTS,
    DEFAULT_HORIZON,
    assign_grades,
    candidate_features,
    candidate_rank_key,
    entry_plan,
    pattern_label,
    position_size_pct,
    rank_candidates,
    safe_horizon,
    score_candidate,
    score_components,
    smart_money_evidence,
    tier_sizes,
)


def base_metrics(**overrides):
    m = {
        "code": "TEST",
        "name": "Test Tbk",
        "close": 1000.0,
        "signal": "BUY",
        "trend_up": True,
        "rsi": 60.0,
        "mom_20d": 4.0,
        "vol_daily": 0.02,
        "atr_pct": 2.5,
        "dist_52w_pct": -1.0,
        "vol_ratio": 2.0,
        "bb_lower": 980.0,
        "range_low_20": None,
        "value": 5.0e9,
        "z_score": 0.5,
        "hist_days": 200,
        "pattern_buy": (),
        "pattern_sell": (),
    }
    m.update(overrides)
    return m


# --------------------------------------------------------------- horizon


def test_validated_horizons_punya_komposisi_berbobot():
    hz = tuple(k for k in sorted(COMPONENT_WEIGHTS) if COMPONENT_WEIGHTS[k])
    assert hz, "harus ada horizon yang punya minimal satu komponen berbobot"
    assert DEFAULT_HORIZON in hz


def test_safe_horizon_jatuh_ke_default_bila_tak_dikenal():
    assert safe_horizon(None) == DEFAULT_HORIZON
    assert safe_horizon(999) == DEFAULT_HORIZON
    assert safe_horizon(21) == 21


def test_components_per_horizon_is_different():
    feats = {name: 1.0 for name in COMPONENT_WEIGHTS[DEFAULT_HORIZON]}
    feats.update({name: 1.0 for name in COMPONENT_WEIGHTS[5]})
    assert all(score_components({}, h) == 50.0 for h in (5, 10, 21))
    scores = {h: score_components(feats, h) for h in (5, 10, 21)}
    assert len(set(scores.values())) > 1, "komposisi tiap horizon harus berbeda"


# --------------------------------------------------------------- entry plan


def test_entry_plan_breakout_has_levels_and_rr_two():
    plan = entry_plan(
        close=1000.0,
        signal="BUY",
        trend_up=True,
        rsi=60.0,
        bb_lower=980.0,
        dist_52w_pct=-1.0,
        vol_ratio=2.0,
        vol_daily=0.02,
        range_low_20=None,
    )
    assert plan is not None
    assert plan["setup"] == "breakout"
    assert plan["stop"] < plan["entry_ref"] < plan["target"]
    assert plan["rr"] == 2.0
    assert plan["stop"] == 940.0


def test_entry_plan_uses_structural_low_when_close_by():
    plan = entry_plan(
        close=1000.0,
        signal="BUY",
        trend_up=True,
        rsi=60.0,
        bb_lower=990.0,
        dist_52w_pct=-10.0,
        vol_ratio=1.0,
        vol_daily=0.03,
        range_low_20=950.0,
    )
    assert plan is not None
    assert plan["stop"] == 950.0


def test_entry_plan_far_low_falls_back_to_volatility():
    plan = entry_plan(
        close=1000.0,
        signal="HOLD",
        trend_up=True,
        rsi=55.0,
        bb_lower=1000.0,
        dist_52w_pct=-20.0,
        vol_ratio=1.0,
        vol_daily=0.02,
        range_low_20=600.0,
    )
    assert plan is not None
    assert plan["stop"] == 940.0


def test_entry_plan_pullback_zone_lies_below_close():
    plan = entry_plan(
        close=1000.0,
        signal="HOLD",
        trend_up=True,
        rsi=40.0,
        bb_lower=960.0,
        dist_52w_pct=-8.0,
        vol_ratio=1.0,
        vol_daily=0.02,
        range_low_20=950.0,
    )
    assert plan is not None
    assert plan["setup"] == "pullback"
    assert plan["entry_low"] == 960.0
    assert plan["entry_high"] == 1000.0
    assert plan["entry_ref"] == 980.0


def test_entry_plan_rejects_missing_or_invalid_close():
    assert entry_plan(
        close=None, signal="BUY", trend_up=True, rsi=60.0, bb_lower=None,
        dist_52w_pct=None, vol_ratio=None, vol_daily=None, range_low_20=None,
    ) is None
    assert entry_plan(
        close=-5.0, signal="BUY", trend_up=True, rsi=60.0, bb_lower=None,
        dist_52w_pct=None, vol_ratio=None, vol_daily=None, range_low_20=None,
    ) is None


# --------------------------------------------------------------- smart money


def test_smart_money_evidence_only_rewards_reliable_records():
    histories = {
        "initiation": {
            "confidence": "tinggi",
            "reliable": True,
            "horizon_days": 5,
            "aligned_hit_rate": 0.6,
            "edge_pct": 2.0,
        },
        "silent_accumulation": {
            "confidence": "sedang",
            "reliable": False,
            "horizon_days": 21,
        },
    }
    ev = smart_money_evidence(
        buy_patterns=["initiation", "silent_accumulation"],
        sell_patterns=[],
        histories=histories,
    )
    assert "points" not in ev, "pola tidak boleh menggeser skor lewat poin tertulis"
    assert ev["best_confidence"] == "tinggi"
    assert any("Inisiasi" in r for r in ev["reasons"])
    assert any("terlalu" in r.lower() or "kecil" in r.lower() for r in ev["reasons"])


def test_smart_money_evidence_sell_pattern_warns_and_subtracts():
    histories = {
        "silent_distribution": {
            "confidence": "tinggi",
            "reliable": True,
            "horizon_days": 5,
        }
    }
    ev = smart_money_evidence(
        buy_patterns=[], sell_patterns=["silent_distribution"], histories=histories
    )
    assert "points" not in ev
    assert ev["warnings"]
    assert pattern_label("silent_distribution") == "Distribusi diam-diam"


# --------------------------------------------------------------- skor kandidat


def test_score_candidate_membawa_horizon_tanpa_grade():
    c = score_candidate(base_metrics())
    assert c is not None
    assert c["horizon_days"] == DEFAULT_HORIZON
    # Grade ditentukan lewat peringkat pool harian, bukan di sini.
    assert "grade" not in c
    feats = candidate_features(base_metrics(), setup=c["setup"])
    assert c["score"] == score_components(feats, DEFAULT_HORIZON)


def test_score_candidate_horizon_dapat_dipilih():
    h5 = score_candidate(base_metrics(), horizon=5)
    h21 = score_candidate(base_metrics(), horizon=21)
    assert h5 is not None and h21 is not None
    assert h5["horizon_days"] == 5 and h21["horizon_days"] == 21
    assert h5["score"] == score_components(
        candidate_features(base_metrics(), setup=h5["setup"]), 5
    )
    # Horizon tak dikenal jatuh ke default, bukan menghasilkan skor kosong.
    weird = score_candidate(base_metrics(), horizon=999)
    assert weird is not None and weird["horizon_days"] == DEFAULT_HORIZON


def test_pola_menjadi_alasan_tanpa_menggeser_skor():
    histories = {
        "initiation": {
            "confidence": "tinggi",
            "reliable": True,
            "horizon_days": 21,
            "aligned_hit_rate": 0.58,
            "edge_pct": 2.6,
        }
    }
    plain = score_candidate(base_metrics())
    with_pattern = score_candidate(
        base_metrics(pattern_buy=("initiation",)), histories=histories
    )
    assert plain is not None and with_pattern is not None
    assert with_pattern["score"] == plain["score"]
    assert with_pattern["horizon_days"] == DEFAULT_HORIZON
    assert with_pattern["patterns"] == ["initiation"]
    assert with_pattern["layers"]["smart_money"] is True
    assert any("Inisiasi" in r for r in with_pattern["reasons"])


def test_sell_signal__illiquidity__short_history_are_not_candidates():
    assert score_candidate(base_metrics(signal="SELL")) is None
    assert score_candidate(base_metrics(value=1.0e8)) is None
    assert score_candidate(base_metrics(hist_days=30)) is None


def test_factor_layer_only_counts_when_validated():
    plain = score_candidate(base_metrics())
    ignored = score_candidate(base_metrics(), factor_adj=10.0, factor_validated=False)
    applied = score_candidate(base_metrics(), factor_adj=10.0, factor_validated=True)
    assert plain is not None and ignored is not None and applied is not None
    assert ignored["score"] == plain["score"]
    assert applied["score"] == plain["score"] + 10.0
    assert applied["layers"]["factor"] is True


def test_broker_layer_only_counts_when_validated():
    plain = score_candidate(base_metrics())
    ignored = score_candidate(base_metrics(), broker_adj=-8.0, broker_validated=False)
    applied = score_candidate(base_metrics(), broker_adj=-8.0, broker_validated=True)
    assert plain is not None and ignored is not None and applied is not None
    assert ignored["score"] == plain["score"]
    assert applied["score"] == plain["score"] - 8.0
    assert applied["layers"]["broker"] is True


def test_overextended_price_is_flagged_tanpa_mengarang_poin():
    plain = score_candidate(base_metrics())
    stretched = score_candidate(base_metrics(z_score=2.2))
    assert plain is not None and stretched is not None
    # z-score tidak punya bobot di komposisi saat ini -> skor tidak berubah,
    # tapi peringatannya tetap muncul supaya pembaca tahu risikonya.
    assert stretched["score"] == plain["score"]
    assert any("overbought" in w.lower() or "waspada" in w.lower() for w in stretched["warnings"])


# --------------------------------------------------------------- grade = peringkat pool


def make_rows(n: int) -> list[dict]:
    return [
        {"code": f"C{i:03d}", "score": float(i), "rr": 2.0, "metrics": {"value": 1.0e9}}
        for i in range(n)
    ]


def test_tier_sizes_memberi_share_dan_minimal_satu():
    assert tier_sizes(200) == (4, 20, 50)          # 2% / 10% / 25%
    assert tier_sizes(1) == (1, 2, 3)              # pool kecil tetap terwakili
    assert tier_sizes(0)[0] >= 1


def test_assign_grades_membagi_pool_dan_membuang_sisanya():
    graded = assign_grades(make_rows(200))
    a, b, c = tier_sizes(200)
    assert len(graded) == c, "hanya 25% teratas yang masuk papan"
    labels = [g["grade"] for g in graded]
    assert labels.count("A") == a
    assert labels.count("B") == b - a
    assert labels.count("C") == c - b
    assert [g["rank"] for g in graded] == list(range(1, len(graded) + 1))
    assert all(g["pool"] == 200 for g in graded)
    scores = [g["score"] for g in graded]
    assert scores == sorted(scores, reverse=True)


def test_assign_grades_pool_kecil_tetap_dapat_grade():
    assert [g["grade"] for g in assign_grades(make_rows(1))] == ["A"]
    assert [g["grade"] for g in assign_grades(make_rows(2))] == ["A", "B"]


def test_tie_break_deterministik_bukan_urutan_masuk():
    def cand(code: str, value: float) -> dict:
        return {"code": code, "score": 50.0, "rr": 2.0, "metrics": {"value": value}}

    first = assign_grades([cand("BBB", 1.0e9), cand("AAA", 5.0e9)])
    reversed_input = assign_grades([cand("AAA", 5.0e9), cand("BBB", 1.0e9)])
    # Skor sama -> likuiditas lebih tinggi tampil lebih dulu, dan hasilnya tidak
    # bergantung urutan data masuk.
    assert [g["code"] for g in first] == ["AAA", "BBB"]
    assert [g["code"] for g in reversed_input] == ["AAA", "BBB"]


def test_candidate_rank_key_aman_tanpa_metrics():
    key = candidate_rank_key({"code": "AAA", "score": 50.0, "rr": 2.0})
    assert key == (50.0, 2.0, 0.0, "AAA")


# --------------------------------------------------------------- sizing


def test_position_size_scales_with_stop_distance_and_is_capped():
    assert position_size_pct(entry=1000.0, stop=900.0, risk_pct=1.0) == 10.0
    assert position_size_pct(entry=1000.0, stop=980.0, risk_pct=1.0) == 25.0
    assert position_size_pct(entry=1000.0, stop=1000.0) is None
    assert position_size_pct(entry=None, stop=900.0) is None


# --------------------------------------------------------------- ranking


def test_rank_candidates_orders_by_score_then_rr_and_filters_grade():
    rows = [
        {"code": "B1", "grade": "B", "score": 60.0, "rr": 1.0},
        {"code": "A2", "grade": "A", "score": 72.0, "rr": 2.0},
        {"code": "A1", "grade": "A", "score": 80.0, "rr": 1.5},
        {"code": "C1", "grade": "C", "score": 44.0, "rr": 3.0},
    ]
    ranked = rank_candidates(rows)
    assert [r["code"] for r in ranked] == ["A1", "A2", "B1", "C1"]
    only_ab = rank_candidates(rows, min_grade="B")
    assert [r["code"] for r in only_ab] == ["A1", "A2", "B1"]
