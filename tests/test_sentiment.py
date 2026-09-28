"""Unit tests for idx_scraper.research.sentiment (sentimen dari data sendiri)."""

from __future__ import annotations

from idx_scraper.research.sentiment import (
    absorption_component,
    build,
    emiten_sentiment,
    foreign_component,
    label_for,
    market_gauge,
    orderbook_component,
    percentile_ranks,
)

# --------------------------------------------------------------------------- #
# Komponen
# --------------------------------------------------------------------------- #


def test_percentile_ranks_span_zero_to_one():
    ranks = percentile_ranks({"A": -0.1, "B": 0.0, "C": 0.1})
    assert ranks["A"] < ranks["B"] < ranks["C"]
    assert ranks["C"] == 1.0
    assert ranks["A"] > 0.0
    assert percentile_ranks({}) == {}


def test_foreign_component_maps_rank_symmetrically():
    assert foreign_component(0.5) == 0.0
    assert foreign_component(1.0) == 1.0
    assert foreign_component(0.0) == -1.0
    assert foreign_component(0.25) == -0.5
    assert foreign_component(None) is None


def test_components_are_clamped():
    assert orderbook_component(1.5) == 1.0
    assert absorption_component(-1.5) == -1.0
    assert orderbook_component(None) is None
    assert absorption_component(None) is None


def test_label_bands():
    assert label_for(0.5) == "AKUMULASI KUAT"
    assert label_for(0.2) == "AKUMULASI"
    assert label_for(0.0) == "NETRAL"
    assert label_for(-0.2) == "DISTRIBUSI"
    assert label_for(-0.5) == "DISTRIBUSI KUAT"


# --------------------------------------------------------------------------- #
# Skor emiten
# --------------------------------------------------------------------------- #


def test_no_data_is_neutral_but_empty():
    s = emiten_sentiment()
    assert s.score == 0.0
    assert s.label == "NETRAL"
    assert s.components == {}  # pembeda "tidak tahu" vs "netral sungguhan"


def test_top_foreign_rank_and_thick_bid_scores_positive():
    s = emiten_sentiment(foreign_rank=0.95, ob_imbalance=0.5, ob_absorption=-0.3)
    assert s.score > 0
    assert s.label in ("AKUMULASI", "AKUMULASI KUAT")
    assert any("paling masuk" in r for r in s.reasons)
    assert any("akumulasi diam-diam" in r for r in s.reasons)


def test_bottom_foreign_rank_and_thick_offer_scores_negative():
    s = emiten_sentiment(foreign_rank=0.05, ob_imbalance=-0.5)
    assert s.score < 0
    assert s.label in ("DISTRIBUSI", "DISTRIBUSI KUAT")
    assert any("distribusi diam-diam" in r for r in s.reasons)


def test_weights_renormalized_when_component_missing():
    """Satu komponen saja -> skor = komponen itu (tidak diredam bobotnya)."""
    assert emiten_sentiment(ob_imbalance=0.8).score == 0.8
    assert emiten_sentiment(foreign_rank=1.0).score == 1.0


def test_median_foreign_rank_is_neutral():
    s = emiten_sentiment(foreign_rank=0.5)
    assert s.score == 0.0
    assert s.label == "NETRAL"


def test_score_is_bounded():
    s = emiten_sentiment(foreign_rank=1.0, ob_imbalance=1.0, ob_absorption=1.0)
    assert -1.0 <= s.score <= 1.0


# --------------------------------------------------------------------------- #
# Gauge pasar
# --------------------------------------------------------------------------- #


def test_market_gauge_neutral_at_defaults():
    g = market_gauge(breadth_up=0.5, index_pct=0.0, foreign_breadth=0.5)
    assert g["score"] == 50.0
    assert g["label"] == "NETRAL"


def test_market_gauge_risk_on():
    g = market_gauge(breadth_up=0.8, index_pct=1.5, foreign_breadth=0.7)
    assert g["score"] > 60
    assert g["label"] == "RISK-ON"


def test_market_gauge_risk_off():
    g = market_gauge(breadth_up=0.2, index_pct=-1.5, foreign_breadth=0.3)
    assert g["score"] < 40
    assert g["label"] == "RISK-OFF"


def test_market_gauge_without_components_is_neutral():
    g = market_gauge(None, None, None)
    assert g["score"] == 50.0
    assert g["components"] == {}


def test_market_gauge_renormalizes_missing_component():
    only_breadth = market_gauge(breadth_up=1.0, index_pct=None, foreign_breadth=None)
    assert only_breadth["score"] == 100.0
    assert set(only_breadth["components"]) == {"breadth"}


# --------------------------------------------------------------------------- #
# Agregasi build()
# --------------------------------------------------------------------------- #


def _rows():
    return [
        {
            "code": "ACCS", "name": "Accumulation Co", "trade_date": "2026-09-24",
            "close": 1000, "percent": 3.0, "value": 50_000_000_000,
            "foreign_net": 10_000_000_000, "volume": 5_000_000,
        },
        {
            "code": "MID", "name": "Middle Co", "trade_date": "2026-09-24",
            "close": 1500, "percent": 0.5, "value": 30_000_000_000,
            "foreign_net": 0.0, "volume": 2_000_000,
        },
        {
            "code": "DIST", "name": "Distribution Co", "trade_date": "2026-09-24",
            "close": 2000, "percent": -2.0, "value": 40_000_000_000,
            "foreign_net": -8_000_000_000, "volume": 2_000_000,
        },
    ]


def test_build_splits_accumulation_and_distribution():
    ob = {"ACCS": {"ob_imbalance": 0.5, "ob_absorption": -0.3}}
    out = build(_rows(), ob_by_code=ob, index_pct=1.0, limit=5)
    # ACCS (arus masuk terkuat + bid tebal) memimpin; MID di atas rata-rata pasar
    assert [i["code"] for i in out["accumulation"]] == ["ACCS", "MID"]
    assert [i["code"] for i in out["distribution"]] == ["DIST"]
    assert out["market"]["up"] == 2 and out["market"]["down"] == 1
    assert out["stats"]["analyzed"] == 3


def test_build_foreign_rank_is_cross_sectional():
    out = build(_rows(), ob_by_code={}, limit=5)
    by_code = {i["code"]: i for i in out["accumulation"] + out["distribution"]}
    # ACCS arus masuk tertinggi -> persentil 1.0; DIST terendah -> 1/3
    assert by_code["ACCS"]["foreign_rank"] == 1.0
    assert by_code["DIST"]["foreign_rank"] < by_code["ACCS"]["foreign_rank"]


def test_build_skips_rows_without_any_component():
    rows = [{"code": "X", "name": None, "close": 100, "percent": 1.0, "value": 0, "foreign_net": None}]
    out = build(rows, ob_by_code={})
    assert out["stats"]["analyzed"] == 0
    assert out["accumulation"] == [] and out["distribution"] == []


def test_build_min_value_filters_illiquid_from_lists():
    rows = _rows()
    rows.append(
        {
            "code": "TINY", "name": "Tiny Co", "close": 50, "percent": 8.0,
            "value": 2_000_000_000, "foreign_net": 1_900_000_000, "volume": 40_000,
        }
    )
    out = build(rows, ob_by_code={}, min_value=10_000_000_000)
    assert all(i["code"] != "TINY" for i in out["accumulation"] + out["distribution"])
    assert out["stats"]["analyzed"] == 4  # tetap dihitung di statistik


def test_build_computes_foreign_breadth():
    out = build(_rows(), ob_by_code={})
    assert out["market"]["foreign_breadth"] == round(1 / 3, 4)  # ACCS saja yang positif
    assert out["market"]["foreign_to_value"] is not None


def test_build_without_orderbook_still_works():
    out = build(_rows(), ob_by_code=None)
    assert out["stats"]["analyzed"] == 3
    assert len(out["accumulation"]) >= 1
