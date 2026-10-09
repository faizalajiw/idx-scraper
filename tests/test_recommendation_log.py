"""Tests for the recommendation log transforms (no DB required)."""

from __future__ import annotations

import pandas as pd

from idx_scraper.research.recommendation_log import (
    build_records,
    candidates_for_date,
    metrics_frame,
    summarize_log,
)


def synthetic_panel(days: int = 130) -> dict:
    """Panel satu emiten dengan tren naik lambat + net asing 5% nilai (flat)."""
    rows = []
    base = pd.Timestamp("2026-01-01")
    for i in range(days):
        close = 1000.0 * (1.0 + 0.002 * i)
        rows.append(
            {
                "date": (base + pd.Timedelta(days=i)).strftime("%Y-%m-%d"),
                "close": round(close, 2),
                "high": round(close * 1.005, 2),
                "low": round(close * 0.995, 2),
                "volume": 1_000_000.0,
                "value": 1.0e9,
                "net_idr": 5.0e7,
            }
        )
    return {"AAAA": {"name": "Anak A", "rows": rows}}


def metrics_row(**overrides) -> pd.DataFrame:
    row = {
        "code": "AAAA",
        "name": "Anak A",
        "date": pd.Timestamp("2026-05-01"),
        "close": 1000.0,
        "signal": "BUY",
        "trend_up": True,
        "rsi": 60.0,
        "mom_20d": 4.0,
        "vol_daily": 0.02,
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
    row.update(overrides)
    return pd.DataFrame([row])


# ------------------------------------------------------------------ metrics


def test_metrics_frame_computes_indicators_and_patterns():
    m = metrics_frame(synthetic_panel())
    assert not m.empty
    assert {"code", "date", "close", "signal", "rsi", "bb_lower", "hist_days"} <= set(m.columns)
    # hist_days adalah hitungan kumulatif per emiten, bukan indeks global.
    assert int(m["hist_days"].max()) == len(m)
    # Warmup SMA50 -> baris awal belum punya sinyal terhitung.
    assert m["rsi"].isna().any()
    # Tren naik lambat + net asing 5% nilai => akumulasi diam-diam terdeteksi.
    flagged = m["pattern_buy"].apply(lambda ids: "silent_accumulation" in (ids or ()))
    assert bool(flagged.any())


def test_metrics_frame_empty_panel_is_safe():
    m = metrics_frame({})
    assert m.empty
    assert "code" in m.columns


# ------------------------------------------------------------------ candidates


def test_candidates_for_date_scores_and_keeps_patterns():
    histories = {
        "initiation": {
            "confidence": "tinggi",
            "reliable": True,
            "horizon_days": 21,
            "aligned_hit_rate": 0.58,
            "edge_pct": 2.6,
        }
    }
    metrics = metrics_row(pattern_buy=("initiation",))
    out = candidates_for_date(metrics, pd.Timestamp("2026-05-01"), histories=histories)
    assert len(out) == 1
    c = out[0]
    assert c["code"] == "AAAA"
    assert c["grade"] in ("A", "B")
    assert c["patterns"] == ["initiation"]
    assert c["stop"] < c["entry_ref"] < c["target"]


def test_candidates_for_date_empty_for_unknown_date():
    assert candidates_for_date(metrics_row(), pd.Timestamp("1999-01-01")) == []


def test_candidates_for_date_ignores_unvalidated_layers():
    metrics = metrics_row()
    plain = candidates_for_date(metrics, pd.Timestamp("2026-05-01"))
    with_adj = candidates_for_date(
        metrics,
        pd.Timestamp("2026-05-01"),
        factor_adj_by_code={"AAAA": 10.0},
        factor_validated=False,
    )
    assert plain[0]["score"] == with_adj[0]["score"]


def test_build_records_covers_each_date():
    metrics = pd.concat(
        [
            metrics_row( date=pd.Timestamp("2026-05-01")),
            metrics_row(date=pd.Timestamp("2026-05-04")),
        ],
        ignore_index=True,
    )
    records = build_records(metrics, months=1200)
    assert {r["date"].isoformat() for r in records} == {"2026-05-01", "2026-05-04"}
    assert all(r["grade"] in ("A", "B", "C") for r in records)
    assert records[0]["entry_ref"] is not None and records[0]["stop"] is not None


# ------------------------------------------------------------------ summary


def test_summarize_log_aggregates_by_grade_and_horizon():
    log = pd.DataFrame(
        [
            {"code": "AAAA", "date": pd.Timestamp("2026-05-01"), "grade": "A",
             "score": 80.0, "close": 1000.0},
            {"code": "BBBB", "date": pd.Timestamp("2026-05-01"), "grade": "B",
             "score": 60.0, "close": 500.0},
            {"code": "CCCC", "date": pd.Timestamp("2026-05-04"), "grade": "C",
             "score": 45.0, "close": 250.0},
        ]
    )
    outcomes = pd.DataFrame(
        [
            {"code": "AAAA", "date": pd.Timestamp("2026-05-01"), "fwd_5": 0.04,
             "abn_5": 0.02, "mfe_5": 0.06, "mae_5": -0.01, "fwd_10": 0.08,
             "abn_10": 0.03, "mfe_10": 0.09, "mae_10": -0.02, "fwd_21": 0.1,
             "abn_21": 0.04, "mfe_21": 0.12, "mae_21": -0.03},
            {"code": "BBBB", "date": pd.Timestamp("2026-05-01"), "fwd_5": -0.02,
             "abn_5": -0.01, "mfe_5": 0.01, "mae_5": -0.03, "fwd_10": None,
             "abn_10": None, "mfe_10": None, "mae_10": None, "fwd_21": None,
             "abn_21": None, "mfe_21": None, "mae_21": None},
            {"code": "CCCC", "date": pd.Timestamp("2026-05-04"), "fwd_5": None,
             "abn_5": None, "mfe_5": None, "mae_5": None, "fwd_10": None,
             "abn_10": None, "mfe_10": None, "mae_10": None, "fwd_21": None,
             "abn_21": None, "mfe_21": None, "mae_21": None},
        ]
    )
    r = summarize_log(log, outcomes, horizons=(5, 10, 21), recent_limit=10)
    assert r["candidates"] == 3
    assert r["grade_a"] == 1 and r["grade_b"] == 1 and r["grade_c"] == 1
    a5 = next(x for x in r["by_grade"] if x["grade"] == "A" and x["horizon"] == 5)
    assert a5["n"] == 1
    assert a5["hit_rate"] == 1.0
    assert a5["mean_fwd"] == 0.04
    assert a5["mean_abnormal"] == 0.02
    b5 = next(x for x in r["by_grade"] if x["grade"] == "B" and x["horizon"] == 5)
    assert b5["hit_rate"] == 0.0
    # Horizon yang belum terealisasi tidak dihitung sebagai 0.
    b21 = next(x for x in r["by_grade"] if x["grade"] == "B" and x["horizon"] == 21)
    assert b21["n"] == 0 and b21["hit_rate"] is None


def test_summarize_log_empty_is_safe():
    r = summarize_log(pd.DataFrame(), pd.DataFrame())
    assert r["candidates"] == 0
    assert r["by_grade"] == []
    assert r["recent"] == []
    assert r["first_date"] is None
