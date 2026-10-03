"""Unit tests: skor Aktivitas Broker (research.broker_activity) + faktor flow.

Semua pure computation — tanpa DB. Yang diuji: gate IC, arah dari tanda IC,
skor komposit, gate coverage, streak per emiten, dan konsentrasi broker.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from idx_scraper.research.broker_activity import (
    BREADTH_THRESHOLD,
    BROKER_FACTORS,
    BROKER_MAX_ADJUSTMENT,
    ROTATION_DELTA_EPS,
    apply_broker_layer,
    composite_score_history,
    composite_scores,
    concentration,
    eligible_factors,
    latest_factor_rows,
    percentile_ranks,
    rotation_phase,
    sector_score_history,
    select_factors,
    verdict_adjustment,
)
from idx_scraper.research.factors import compute_factors


def _panel_one_code(n: int = 30, close: float = 100.0, value: float = 1_000_000.0,
                    net: float = 1000.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "code": ["AAAA"] * n,
            "date": pd.date_range("2026-01-01", periods=n, freq="B"),
            "close": [close] * n,
            "volume": [10_000] * n,
            "value": [value] * n,
            "foreign_net": [net] * n,
        }
    )


# --------------------------------------------------------------------------- #
# Faktor flow baru (notasi rupiah, rolling per emiten)
# --------------------------------------------------------------------------- #


def test_flow_factors_are_notional_and_use_21d_turnover_denominator():
    # net = 1000 saham x close 100 = Rp100.000/hari; value 21h = Rp1jt/hari.
    out = compute_factors(_panel_one_code(30))
    last = out.iloc[-1]

    # 5 hari = Rp500.000 / Rp1.000.000 = 0,5
    assert last["flow_net_5d"] == 0.5
    # 21 hari = Rp2.100.000 / Rp1.000.000 = 2,1
    assert last["flow_net_21d"] == 2.1
    # arus stabil (tidak berubah) -> percepatan nol
    assert last["flow_accel"] == 0.0
    # 21 hari semuanya net beli -> konsistensi maksimum
    assert last["flow_consistency_21d"] == 1.0
    # aktivitas = Rp2.100.000 / (21 x Rp1.000.000) = 0,1
    assert last["flow_activity_21d"] == 0.1
    assert last["foreign_streak"] == 30.0


def test_flow_factors_warmup_stays_nan():
    out = compute_factors(_panel_one_code(30))
    # Pembagi rata-rata value 21 hari belum valid sebelum baris ke-21.
    assert out["flow_net_5d"].iloc[:20].isna().all()
    assert out["flow_net_21d"].iloc[:20].isna().all()
    assert out["flow_activity_21d"].iloc[:20].isna().all()
    # Percepatan = (net 5 hari) − (net 5 hari sebelumnya), jadi ikut terbendung
    # oleh pembagi 21 hari yang sama -> valid mulai baris ke-21 juga.
    assert out["flow_accel"].iloc[:20].isna().all()
    assert out["flow_accel"].iloc[20] == 0.0  # arus konstan -> tidak berakselerasi
    # Bukan NaN yang diisi 0: baris pertama yang valid benar-benar ada isinya.
    assert not pd.isna(out["flow_net_5d"].iloc[20])


def test_streak_does_not_bleed_across_codes():
    # Kode pertama berakhir +, kode kedua mulai +: dengan pergeseran mentah
    # (tanpa groupby) streak kode kedua akan salah mulai dari 2.
    panel = pd.DataFrame(
        {
            "code": ["AAAA"] * 3 + ["BBBB"] * 3,
            "date": list(pd.date_range("2026-01-01", periods=3, freq="B")) * 2,
            "close": [100.0] * 6,
            "volume": [10_000] * 6,
            "value": [1_000_000.0] * 6,
            "foreign_net": [100.0, -100.0, 100.0, 100.0, 100.0, 100.0],
        }
    )
    out = compute_factors(panel).set_index("code")
    assert out.loc["AAAA", "foreign_streak"].tolist() == [1.0, -1.0, 1.0]
    assert out.loc["BBBB", "foreign_streak"].tolist() == [1.0, 2.0, 3.0]


def test_flow_columns_present_when_foreign_net_missing():
    # Panel tanpa foreign_net -> kolom flow tetap ada tapi NaN (skema stabil).
    panel = _panel_one_code(25).drop(columns=["foreign_net"])
    out = compute_factors(panel)
    for col in ("flow_net_5d", "flow_net_21d", "foreign_streak"):
        assert col in out.columns
        assert out[col].isna().all()


# --------------------------------------------------------------------------- #
# Gate IC
# --------------------------------------------------------------------------- #


def _ic(factor: str, mean_ic: float | None, icir: float | None, horizon: int = 10) -> dict:
    return {
        "factor": factor,
        "horizon": horizon,
        "mean_ic": mean_ic,
        "icir": icir,
        "t_stat": 2.0,
        "hit_rate": 0.6,
        "n_days": 120,
    }


def test_select_factors_enforces_ic_gate():
    rows = [
        _ic("flow_net_21d", 0.08, 0.9),   # lolos
        _ic("flow_net_5d", 0.04, 3.0),    # IC terlalu kecil
        _ic("flow_accel", 0.20, 0.3),     # ICIR terlalu kecil
        _ic("ob_imbalance", None, None),  # tanpa data
        _ic("mom_21d", 0.30, 3.0),        # di luar keluarga broker
    ]
    picked = select_factors(rows)
    by_factor = {p["factor"]: p for p in picked}

    assert "mom_21d" not in by_factor
    assert by_factor["flow_net_21d"]["eligible"] is True
    assert by_factor["flow_net_21d"]["weight"] == 1.0
    assert by_factor["flow_net_21d"]["direction"] == 1.0
    for f in ("flow_net_5d", "flow_accel", "ob_imbalance"):
        assert by_factor[f]["eligible"] is False
        assert by_factor[f]["weight"] == 0.0
        assert by_factor[f]["direction"] == 0.0

    # Faktor lolos diurutkan paling depan.
    assert picked[0]["factor"] == "flow_net_21d"


def test_select_factors_direction_follows_ic_sign():
    rows = [_ic("flow_net_21d", -0.09, -1.2), _ic("ob_absorption", 0.06, 0.8)]
    by_factor = {p["factor"]: p for p in select_factors(rows)}
    assert by_factor["flow_net_21d"]["direction"] == -1.0
    assert by_factor["ob_absorption"]["direction"] == 1.0
    # Bobot proporsional |IC|, jumlah 1.
    total = sum(p["weight"] for p in by_factor.values())
    assert abs(total - 1.0) < 1e-9
    assert by_factor["flow_net_21d"]["weight"] > by_factor["ob_absorption"]["weight"]


def test_select_factors_ignores_other_horizons():
    rows = [_ic("flow_net_21d", 0.09, 1.0, horizon=5)]
    assert select_factors(rows, horizon=10) == []
    assert len(select_factors(rows, horizon=5)) == 1


def test_no_eligible_factor_means_no_active_factors():
    rows = [_ic("flow_net_21d", 0.01, 0.1), _ic("ob_imbalance", 0.02, 0.2)]
    assert eligible_factors(select_factors(rows)) == []


# --------------------------------------------------------------------------- #
# Skor komposit
# --------------------------------------------------------------------------- #


def _selected(factor: str, direction: float, weight: float = 1.0) -> dict:
    return {
        "factor": factor,
        "label": factor,
        "eligible": True,
        "direction": direction,
        "weight": weight,
    }


def test_composite_score_orders_by_percentile():
    latest = pd.DataFrame({"code": ["AAAA", "BBBB", "CCCC", "DDDD"], "flow_net_5d": [1.0, 2.0, 3.0, 4.0]})
    out = composite_scores(latest, [_selected("flow_net_5d", 1.0)]).set_index("code")
    assert out.loc["DDDD", "score"] == 100.0
    assert out.loc["AAAA", "score"] == 25.0
    assert out.loc["BBBB", "score"] == 50.0
    assert (out["coverage"] == 1.0).all()
    # Driver menjelaskan skor tertinggi.
    assert out.loc["DDDD", "drivers"][0]["percentile"] == 1.0


def test_composite_score_negates_when_ic_is_negative():
    latest = pd.DataFrame({"code": ["AAAA", "BBBB", "CCCC", "DDDD"], "flow_net_5d": [1.0, 2.0, 3.0, 4.0]})
    out = composite_scores(latest, [_selected("flow_net_5d", -1.0)]).set_index("code")
    assert out.loc["AAAA", "score"] == 75.0
    assert out.loc["DDDD", "score"] == 0.0


def test_composite_score_weights_are_respected():
    latest = pd.DataFrame({"code": ["AAAA", "BBBB"], "a": [1.0, 2.0], "b": [2.0, 1.0]})
    # a berbobot 3x b, arah sama -> AAAA kalah di a, menang di b, tapi bobot a dominan.
    out = composite_scores(
        latest, [_selected("a", 1.0, 0.75), _selected("b", 1.0, 0.25)]
    ).set_index("code")
    # BBBB persentil 1,0 di faktor a (bobot 0,75) dan 0,5 di b (bobot 0,25):
    # kontribusi = 0,75*(2*1,0−1) + 0,25*(2*0,5−1) = 0,75 -> skor 87,5.
    assert out.loc["BBBB", "score"] == 87.5
    assert out.loc["BBBB", "score"] > out.loc["AAAA", "score"]  # faktor berbobot besar menang


def test_composite_score_drops_low_coverage_names():
    latest = pd.DataFrame(
        {
            "code": ["AAAA", "BBBB"],
            "a": [1.0, np.nan],   # BBBB tidak punya nilai a
            "b": [1.0, 1.0],
        }
    )
    selected = [_selected("a", 1.0, 0.5), _selected("b", 1.0, 0.5)]
    # BBBB coverage = 0,5 -> dibuang saat ambang 0,75, dipertahankan saat 0,5.
    assert composite_scores(latest, selected, min_coverage=0.75)["code"].tolist() == ["AAAA"]
    assert set(composite_scores(latest, selected, min_coverage=0.5)["code"]) == {"AAAA", "BBBB"}


def test_composite_score_missing_factor_is_neutral_not_zero():
    # Emiten tanpa nilai faktor tidak "dihukum": kontribusinya nol (persentil 0,5).
    latest = pd.DataFrame({"code": ["AAAA", "BBBB"], "a": [1.0, np.nan]})
    out = composite_scores(latest, [_selected("a", 1.0)], min_coverage=0.0).set_index("code")
    assert out.loc["BBBB", "score"] == 50.0
    assert out.loc["BBBB", "coverage"] == 0.0


def test_composite_score_empty_without_active_factors():
    latest = pd.DataFrame({"code": ["AAAA"], "flow_net_5d": [1.0]})
    assert composite_scores(latest, []).empty
    assert composite_scores(latest, [dict(_selected("flow_net_5d", 0.0), eligible=False, weight=0.0)]).empty
    assert composite_scores(pd.DataFrame(), [_selected("flow_net_5d", 1.0)]).empty


def test_percentile_ranks_keeps_nan():
    out = percentile_ranks(pd.Series([1.0, 2.0, np.nan]))
    assert out.iloc[0] == 0.5
    assert pd.isna(out.iloc[2])


# --------------------------------------------------------------------------- #
# Snapshot & konsentrasi
# --------------------------------------------------------------------------- #


def test_latest_factor_rows_uses_global_max_date():
    panel = pd.DataFrame(
        {
            "code": ["AAAA", "AAAA", "BBBB", "BBBB"],
            "date": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-01", "2026-01-02"]),
            "close": [10.0, 11.0, 20.0, 21.0],
            "flow_net_5d": [0.1, 0.2, 0.3, 0.4],
        }
    )
    snap, as_of = latest_factor_rows(panel, ["flow_net_5d"])
    assert str(as_of.date()) == "2026-01-02"
    assert snap["code"].tolist() == ["AAAA", "BBBB"]
    assert snap["flow_net_5d"].tolist() == [0.2, 0.4]


def test_latest_factor_rows_empty_panel():
    snap, as_of = latest_factor_rows(pd.DataFrame(), ["flow_net_5d"])
    assert snap.empty
    assert as_of is None


def test_concentration_ratios_and_hhi():
    rows = [
        {"broker_code": "BB", "value": 50.0},
        {"broker_code": "AA", "value": 100.0},
        {"broker_code": "CC", "value": 25.0},
        {"broker_code": "DD", "value": 25.0},
    ]
    out = concentration(rows, top_n=3)
    assert out["n_brokers"] == 4
    assert out["total_value"] == 200.0
    assert out["cr1"] == 0.5
    assert out["cr3"] == 0.875
    # Hanya ada 4 broker -> top-5 sudah mencakup seluruh pasar.
    assert out["cr5"] == 1.0
    assert abs(out["hhi"] - 0.34375) < 1e-12
    assert [t["broker_code"] for t in out["top"]] == ["AA", "BB", "CC"]
    assert out["top"][0]["share"] == 0.5


def test_concentration_handles_empty_and_zero():
    out = concentration([])
    assert out["n_brokers"] == 0
    assert out["hhi"] is None
    assert out["top"] == []
    # Baris dengan nilai nol/negatif/tanpa nilai dibuang, bukan dihitung.
    assert concentration([{"broker_code": "X", "value": 0.0}])["n_brokers"] == 0


def test_broker_factor_family_is_declared():
    # Skor hanya boleh dibentuk dari keluarga ini — jaga agar tidak melebar.
    assert "flow_net_5d" in BROKER_FACTORS
    assert "ob_absorption" in BROKER_FACTORS
    assert "mom_21d" not in BROKER_FACTORS


# --------------------------------------------------------------------------- #
# Lapisan verdict Hold Check
# --------------------------------------------------------------------------- #


def test_adjustment_inactive_when_not_validated():
    # Belum lolos gate IC -> lapisan harus diam, apa pun skornya.
    for s in (0.0, 50.0, 100.0):
        assert verdict_adjustment(s, validated=False) == (0.0, None)


def test_adjustment_neutral_and_missing_scores():
    assert verdict_adjustment(None, validated=True) == (0.0, None)
    assert verdict_adjustment(50.0, validated=True) == (0.0, None)  # median pasar


def test_adjustment_maps_median_to_zero_and_extremes_to_budget():
    max_adj = BROKER_MAX_ADJUSTMENT
    assert verdict_adjustment(100.0, validated=True)[0] == max_adj
    assert verdict_adjustment(0.0, validated=True)[0] == -max_adj
    # linier: 75 -> setengah anggaran
    assert verdict_adjustment(75.0, validated=True)[0] == max_adj / 2.0
    # di-cap: skor di luar [0,100] tidak memperbesar anggaran
    assert verdict_adjustment(999.0, validated=True)[0] == max_adj
    assert verdict_adjustment(-999.0, validated=True)[0] == -max_adj


def test_adjustment_reason_only_when_meaningful():
    _, strong = verdict_adjustment(90.0, validated=True)
    _, weak = verdict_adjustment(10.0, validated=True)
    _, neutral = verdict_adjustment(60.0, validated=True)
    assert strong is not None and "mendukung" in strong
    assert weak is not None and "melemah" in weak
    # Di antara ambang: dorongan tetap ada, tapi tidak perlu bullet alasan.
    assert neutral is None


def test_apply_broker_layer_flips_verdict_up():
    # 52 = TRIM; dorongan +8 -> 60 = HOLD.
    score, verdict, reasons, adj = apply_broker_layer(
        52.0, "TRIM", ["alasan teknikal"], 100.0, True
    )
    assert adj == BROKER_MAX_ADJUSTMENT
    assert score == 60.0
    assert verdict == "HOLD"
    assert reasons[0] == "alasan teknikal"  # alasan lama dipertahankan
    assert len(reasons) == 2


def test_apply_broker_layer_flips_verdict_down():
    # 56 = HOLD; dorongan -8 -> 48 = TRIM.
    score, verdict, _, adj = apply_broker_layer(56.0, "HOLD", [], 0.0, True)
    assert adj == -BROKER_MAX_ADJUSTMENT
    assert score == 48.0
    assert verdict == "TRIM"


def test_apply_broker_layer_is_noop_without_validation_or_score():
    base = (44.0, "TRIM", ["a"])
    for broker_score, ok in ((80.0, False), (None, True)):
        score, verdict, reasons, adj = apply_broker_layer(
            *base, broker_score=broker_score, validated=ok
        )
        assert (score, verdict, reasons, adj) == (*base, 0.0)
        # reasons harus objek yang sama (tidak disalin saat no-op)
        assert reasons is base[2]


def test_apply_broker_layer_clamped_at_bounds():
    score, verdict, _, _ = apply_broker_layer(98.0, "STRONG HOLD", [], 100.0, True)
    assert score == 100.0 and verdict == "STRONG HOLD"
    score, verdict, _, _ = apply_broker_layer(3.0, "EXIT", [], 0.0, True)
    assert score == 0.0 and verdict == "EXIT"


# --------------------------------------------------------------------------- #
# Riwayat skor per emiten (halaman detail emiten)
# --------------------------------------------------------------------------- #


def _two_day_panel(factor: str = "flow_net_5d") -> pd.DataFrame:
    """2 emiten x 2 tanggal; nilai faktor naik 100x di tanggal kedua."""
    return pd.DataFrame(
        {
            "code": ["AAAA", "BBBB"] * 2,
            "date": pd.to_datetime(["2026-09-24"] * 2 + ["2026-09-25"] * 2),
            "close": [100.0, 50.0, 100.0, 50.0],
            factor: [1.0, 2.0, 100.0, 200.0],
        }
    )


def test_history_percentiles_are_computed_per_date():
    # Nilai AAAA meloncat 1 -> 100 antar tanggal; kalau percentile dihitung
    # global (bukan per tanggal) skornya akan bergeser. Skala pasar relatifnya
    # TIDAK berubah, jadi skornya harus persis sama di kedua tanggal.
    hist = composite_score_history(
        _two_day_panel(), [_selected("flow_net_5d", 1.0)], "AAAA"
    )
    assert len(hist) == 2
    assert hist["score"].tolist() == [50.0, 50.0]
    assert hist["coverage"].tolist() == [1.0, 1.0]
    assert hist.iloc[0]["drivers"][0]["percentile"] == 0.5



def test_history_tracks_the_winner_and_loser_differently():
    hist_a = composite_score_history(
        _two_day_panel(), [_selected("flow_net_5d", 1.0)], "AAAA"
    )
    hist_b = composite_score_history(
        _two_day_panel(), [_selected("flow_net_5d", 1.0)], "BBBB"
    )
    assert hist_a["score"].tolist() == [50.0, 50.0]
    assert hist_b["score"].tolist() == [100.0, 100.0]
    # Arah negatif membalik urutannya.
    flipped = composite_score_history(
        _two_day_panel(), [_selected("flow_net_5d", -1.0)], "AAAA"
    )
    assert flipped["score"].tolist() == [50.0, 50.0]
    assert composite_score_history(
        _two_day_panel(), [_selected("flow_net_5d", -1.0)], "BBBB"
    )["score"].tolist() == [0.0, 0.0]


def test_history_is_case_insensitive():
    hist = composite_score_history(
        _two_day_panel(), [_selected("flow_net_5d", 1.0)], "aaaa"
    )
    assert len(hist) == 2


def test_history_skips_dates_below_coverage_gate():
    panel = pd.DataFrame(
        {
            "code": ["AAAA", "BBBB", "AAAA", "BBBB"],
            "date": pd.to_datetime(["2026-09-24"] * 2 + ["2026-09-25"] * 2),
            "a": [1.0, 2.0, 1.0, 2.0],
            "b": [1.0, 2.0, np.nan, 2.0],  # AAAA kehilangan faktor b di hari ke-2
        }
    )
    selected = [_selected("a", 1.0, 0.5), _selected("b", 1.0, 0.5)]
    # coverage AAAA hari ke-2 = 0,5 -> dibuang saat ambang 0,75, dipertahankan di 0,5.
    assert len(composite_score_history(panel, selected, "AAAA", min_coverage=0.75)) == 1
    assert len(composite_score_history(panel, selected, "AAAA", min_coverage=0.5)) == 2


def test_history_respects_lookback():
    panel = pd.DataFrame(
        {
            "code": ["AAAA"] * 5,
            "date": pd.date_range("2026-09-01", periods=5, freq="B"),
            "flow_net_5d": [1.0, 2.0, 3.0, 4.0, 5.0],
        }
    )
    hist = composite_score_history(panel, [_selected("flow_net_5d", 1.0)], "AAAA", lookback=3)
    assert len(hist) == 3
    # Urut menaik dan hanya 3 tanggal terakhir.
    assert hist["date"].tolist() == list(pd.date_range("2026-09-03", periods=3, freq="B"))


def test_history_last_point_matches_latest_cross_section_score():
    # Invariant lintas halaman: skor hari terakhir di kartu detail emiten harus
    # sama persis dengan skor emiten itu di halaman ranking pasar.
    panel = _two_day_panel()
    selected = [_selected("flow_net_5d", 1.0)]
    latest, _ = latest_factor_rows(panel, ["flow_net_5d", "close"])
    ranking = composite_scores(latest, selected).set_index("code")
    for code in ("AAAA", "BBBB"):
        hist = composite_score_history(panel, selected, code)
        assert hist.iloc[-1]["score"] == ranking.loc[code, "score"]


# --------------------------------------------------------------------------- #
# Agregasi sektor & kuadran rotasi
# --------------------------------------------------------------------------- #


def _sector_panel(codes: list[str], values: list[float], dates: list[str] | None = None) -> pd.DataFrame:
    dates = dates or ["2026-09-25"] * len(codes)
    return pd.DataFrame(
        {
            "code": codes,
            "date": pd.to_datetime(dates),
            "close": [100.0] * len(codes),
            "flow_net_5d": values,
        }
    )


def test_sector_history_aggregates_median_and_breadth():
    # Persentil lintas 4 emiten: A=0,25 B=0,50 C=0,75 D=1,00 -> skor 37,5/50/62,5/100.
    panel = _sector_panel(["AAAA", "BBBB", "CCCC", "DDDD"], [1.0, 2.0, 3.0, 4.0])
    sector_of = {"AAAA": "X", "BBBB": "X", "CCCC": "X", "DDDD": "Y"}
    out = sector_score_history(panel, [_selected("flow_net_5d", 1.0)], sector_of, min_names=3)

    # Sektor Y (1 emiten) dibuang oleh gate min_names.
    assert out["sector"].tolist() == ["X"]
    row = out.iloc[0]
    assert row["n_names"] == 3
    assert row["median_score"] == 50.0  # median [37,5; 50; 62,5]
    # Breadth = porsi emiten dengan skor >= ambang; hanya CCCC (62,5) yang lolos.
    assert row["breadth"] == pytest.approx(1 / 3)


def test_sector_history_min_names_gate():
    panel = _sector_panel(["AAAA", "BBBB", "CCCC", "DDDD"], [1.0, 2.0, 3.0, 4.0])
    sector_of = {"AAAA": "X", "BBBB": "X", "CCCC": "X", "DDDD": "Y"}
    loose = sector_score_history(panel, [_selected("flow_net_5d", 1.0)], sector_of, min_names=1)
    assert set(loose["sector"]) == {"X", "Y"}
    y = loose[loose["sector"] == "Y"].iloc[0]
    assert y["n_names"] == 1
    assert y["median_score"] == 100.0
    assert y["breadth"] == 1.0  # skor 100 >= ambang breadth


def test_sector_history_excludes_requested_sectors_and_unmapped_codes():
    panel = _sector_panel(["AAAA", "BBBB", "CCCC", "DDDD"], [1.0, 2.0, 3.0, 4.0])
    sector_of = {"AAAA": "X", "BBBB": "X", "CCCC": "X"}  # DDDD sengaja tidak dipetakan
    out = sector_score_history(
        panel, [_selected("flow_net_5d", 1.0)], sector_of, min_names=1
    )
    assert out["sector"].tolist() == ["X"]
    assert out.iloc[0]["n_names"] == 3

    dropped = sector_score_history(
        panel,
        [_selected("flow_net_5d", 1.0)],
        sector_of,
        min_names=1,
        exclude_sectors={"X"},
    )
    assert dropped.empty


def test_sector_history_scores_each_date_against_its_own_market():
    # Nilai naik 100x di tanggal kedua, tapi posisi relatif antar emiten tetap:
    # skor agregatnya harus sama di kedua tanggal.
    panel = _sector_panel(
        ["AAAA", "BBBB", "CCCC"] * 2,
        [1.0, 2.0, 3.0, 100.0, 200.0, 300.0],
        ["2026-09-24"] * 3 + ["2026-09-25"] * 3,
    )
    sector_of = {"AAAA": "X", "BBBB": "X", "CCCC": "X"}
    out = sector_score_history(panel, [_selected("flow_net_5d", 1.0)], sector_of, min_names=3)
    medians = out["median_score"].tolist()
    # 3 emiten -> persentil 1/3, 2/3, 1 -> skor 33,3 / 66,7 / 100; median 200/3.
    assert len(medians) == 2
    assert medians[0] == pytest.approx(medians[1])  # posisi relatif tidak berubah
    assert medians[0] == pytest.approx(200 / 3)
    assert out["date"].tolist() == sorted(out["date"])


def test_sector_history_empty_cases():
    cols = ["date", "sector", "n_names", "median_score", "breadth"]
    sel = [_selected("flow_net_5d", 1.0)]
    panel = _sector_panel(["AAAA"], [1.0])
    assert sector_score_history(pd.DataFrame(), sel, {"AAAA": "X"}).empty
    assert sector_score_history(panel, [], {"AAAA": "X"}).empty
    assert sector_score_history(panel, sel, {}, min_names=1).empty
    assert sector_score_history(panel, sel, {"AAAA": "X"}, lookback=0).empty
    assert list(sector_score_history(panel, sel, {"AAAA": "X"}).columns) == cols


def test_rotation_phase_quadrants():
    # Level dipisah di 50 (median pasar), delta di 0.
    assert rotation_phase(60.0, 2.0) == "AKUMULASI"
    assert rotation_phase(60.0, -2.0) == "MEMUDAR"
    assert rotation_phase(40.0, 2.0) == "MEMBAIK"
    assert rotation_phase(40.0, -2.0) == "TERPURUK"
    # Batas level: tepat 50 dihitung tinggi.
    assert rotation_phase(50.0, 1.0) == "AKUMULASI"
    assert rotation_phase(49.9, 1.0) == "MEMBAIK"


def test_rotation_phase_flat_change_is_not_claimed_as_a_direction():
    # Delta nol (atau di dalam toleransi) -> arahnya tidak diketahui. Melabelinya
    # MEMUDAR/TERPURUK berarti mengklaim ada arah padahal perubahannya nol.
    assert rotation_phase(60.0, 0.0) == "STABIL"
    assert rotation_phase(40.0, 0.0) == "STABIL"
    assert rotation_phase(60.0, ROTATION_DELTA_EPS) == "STABIL"
    assert rotation_phase(60.0, -ROTATION_DELTA_EPS) == "STABIL"
    # Di luar toleransi, arahnya baru diklaim.
    assert rotation_phase(60.0, ROTATION_DELTA_EPS + 0.1) == "AKUMULASI"
    assert rotation_phase(60.0, -ROTATION_DELTA_EPS - 0.1) == "MEMUDAR"
    assert rotation_phase(40.0, ROTATION_DELTA_EPS + 0.1) == "MEMBAIK"
    assert rotation_phase(40.0, -ROTATION_DELTA_EPS - 0.1) == "TERPURUK"


def test_rotation_phase_missing_inputs():
    assert rotation_phase(None, 1.0) is None
    assert rotation_phase(60.0, None) is None


def test_breadth_threshold_is_above_market_median():
    # Ambang akumulasi harus di atas 50 (median pasar), kalau tidak "breadth"
    # cuma mengukur separuh pasar.
    assert BREADTH_THRESHOLD > 50.0


def test_history_market_median_is_the_cross_section_median():
    # Panel 2 emiten: median cross-section dua nilai percentile-rank.
    hist = composite_score_history(
        _two_day_panel(), [_selected("flow_net_5d", 1.0)], "AAAA"
    )
    # _score_matrix pakai percentile rank -> dua emiten = {50, 100}; median 75.
    assert hist["market_median"].tolist() == [75.0, 75.0]
    # Kolom baru tidak mengubah skor/coverage yang sudah diuji test lain.
    assert hist["score"].tolist() == [50.0, 50.0]


def test_history_empty_cases():
    cols = ["date", "score", "coverage", "drivers", "market_median"]
    sel = [_selected("flow_net_5d", 1.0)]
    panel = _two_day_panel()
    assert composite_score_history(pd.DataFrame(), sel, "AAAA").empty
    assert composite_score_history(panel, [], "AAAA").empty
    assert composite_score_history(panel, sel, "ZZZZ").empty
    assert composite_score_history(panel, sel, "AAAA", lookback=0).empty
    assert list(composite_score_history(pd.DataFrame(), sel, "AAAA").columns) == cols
    # Panel tanpa kolom date/code tidak boleh meledak.
    assert composite_score_history(pd.DataFrame({"x": [1]}), sel, "AAAA").empty
