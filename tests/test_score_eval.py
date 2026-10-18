"""Unit tests for idx_scraper.research.score_eval (harness walk-forward).

Tes di sini tidak menyentuh DB atau jaringan — yang diuji hanya fungsi murni.

Tujuannya bukan menguji kode baru, tapi mengunci dua klaim yang selama ini tidak
pernah dibuktikan: (1) angka OOS di harness ini benar-benar out-of-sample —
setiap jendela test berada SETELAH jendela train-nya dan tidak tumpang tindih
dengan jendela test lain; (2) gerbang edge hanya memberi bobot pada komponen
yang buktinya lolos ambang.
"""

from __future__ import annotations

from itertools import pairwise

import pandas as pd
import pytest

from idx_scraper.research.recommend import COMPONENT_NOTES, HORIZONS
from idx_scraper.research.score_eval import (
    attach_abnormal,
    edge_table,
    walk_forward,
    weights_from_edges,
)


def _synthetic_prices(sessions: int = 160, codes: int = 25) -> pd.DataFrame:
    """Panel harga deterministik (tanpa angka acak) untuk menguji harness.

    Kode bernama ``C000``..``C0NN`` supaya tes lain bisa memilih baris
    berdasarkan indeks kode (mis. hanya kode genap) tanpa bergantung urutan.
    """
    dates = pd.date_range("2025-01-01", periods=sessions, freq="B")
    rows = [
        {
            "code": f"C{i:03d}",
            "date": d,
            "close": 100.0 + i + (j * (1 + i % 3)) / 10.0,
        }
        for i in range(codes)
        for j, d in enumerate(dates)
    ]
    return pd.DataFrame(rows)


def _feats_from_prices(
    prices: pd.DataFrame, horizons: tuple[int, ...] = HORIZONS
) -> pd.DataFrame:
    """Frame fitur lengkap: ``code,date,close,setup`` + semua kunci
    ``COMPONENT_NOTES`` (0.0) + kolom ``abn_{k}`` dari ``attach_abnormal``."""
    base = prices.copy()
    base["setup"] = "uji"
    for name in COMPONENT_NOTES:
        base[name] = 0.0
    return attach_abnormal(base, tuple(horizons), prices)


def _folds(report: dict) -> list[dict]:
    return report["by_horizon"]["21"]["fold_detail"]


def _edge_row(edge: float, t_stat: float, days: int, name: str = "c") -> pd.DataFrame:
    """Satu baris tabel edge — untuk menguji gerbang tanpa menghitung edge."""
    return pd.DataFrame([{"edge": edge, "t_stat": t_stat, "days": days}], index=[name])


def test_walk_forward_test_window_always_after_train():
    report = walk_forward(
        _feats_from_prices(_synthetic_prices(160, 25)), train=100, test=25, step=25
    )
    folds = _folds(report)
    assert folds
    for fold in folds:
        assert fold["train"][1] < fold["test"][0]


def test_walk_forward_test_windows_do_not_overlap():
    report = walk_forward(
        _feats_from_prices(_synthetic_prices(160, 25)), train=100, test=25, step=25
    )
    folds = _folds(report)
    assert len(folds) >= 2
    spans = [set(pd.date_range(fold["test"][0], fold["test"][1])) for fold in folds]
    for left, right in pairwise(spans):
        assert not (left & right)


def test_walk_forward_fold_count_matches_geometry():
    report = walk_forward(
        _feats_from_prices(_synthetic_prices(160, 25)), train=100, test=25, step=25
    )
    # 160 sesi, train 100, test/step 25 -> start 100 (125<=160), 125 (150<=160), 150 (175>160)
    assert len(_folds(report)) == 2


def test_walk_forward_reports_zero_folds_when_sessions_too_short():
    report = walk_forward(
        _feats_from_prices(_synthetic_prices(80, 25)), train=100, test=25, step=25
    )
    assert report["by_horizon"]["21"]["folds"] == 0
    assert report["by_horizon"]["21"]["note"]


def test_walk_forward_train_dates_exclude_test_dates():
    report = walk_forward(
        _feats_from_prices(_synthetic_prices(160, 25)), train=100, test=25, step=25
    )
    for fold in _folds(report):
        train_dates = set(fold["train_dates"])
        test_start, test_end = fold["test"]
        assert not [d for d in train_dates if test_start <= d <= test_end]


def test_edge_table_skips_sessions_below_min_names():
    """Universe lebih kecil dari MIN_NAMES_PER_DAY: tak ada sesi terukur.

    Bendera dinyalakan di satu nama supaya yang diuji benar-benar ambang jumlah
    nama per sesi — bukan sekadar "tidak ada nama tertandai".
    """
    feats = _feats_from_prices(_synthetic_prices(40, 5))
    name = next(iter(COMPONENT_NOTES))
    feats.loc[feats["code"] == "C000", name] = 1.0

    table = edge_table(feats, 21)
    assert (table["days"] == 0).all()
    assert table["edge"].isna().all()


def test_edge_table_counts_session_with_single_flagged_name():
    """Satu nama tertandai per sesi sudah cukup jadi satu observasi edge.

    Sesi yang labelnya belum berakhir tidak ikut: untuk horizon 21 tanpa jeda,
    22 sesi terakhir belum punya hasil, jadi ``days`` bukan 60 melainkan 38.
    """
    feats = _feats_from_prices(_synthetic_prices(60, 25), HORIZONS)
    name = next(iter(COMPONENT_NOTES))
    flagged_rows = feats.groupby("date").head(1).index  # tepat satu baris per tanggal
    feats.loc[flagged_rows, name] = 1.0

    table = edge_table(feats, 21)
    assert int(table.loc[name, "days"]) == 38  # 60 sesi − (21 horizon + 1 jeda)
    assert int(table.loc[name, "flagged"]) == 38


def test_weights_zero_when_t_below_gate():
    assert weights_from_edges(_edge_row(0.05, 1.0, 40)) == {"c": 0.0}


def test_weights_zero_when_edge_below_min_edge():
    assert weights_from_edges(_edge_row(0.0005, 5.0, 40)) == {"c": 0.0}


def test_weights_zero_when_days_below_min_days():
    assert weights_from_edges(_edge_row(0.05, 5.0, 5)) == {"c": 0.0}


def test_weights_clamped_shrunk_and_rounded_to_half_point():
    # clip(0.05 x 600, ±15) = 15 ; 15 x 0.5 = 7.5 -> bulat 0,5 = 7.5
    assert weights_from_edges(_edge_row(0.05, 5.0, 40)) == {"c": 7.5}


def test_weights_nan_edge_is_zero_not_crash():
    assert weights_from_edges(_edge_row(float("nan"), 5.0, 40)) == {"c": 0.0}


def test_edge_table_without_feature_columns_does_not_silently_report_zero_edge():
    """Kolom fitur hilang harus ketahuan, bukan jadi tabel kosong yang terlihat sah."""
    feats = _feats_from_prices(_synthetic_prices(60, 25), HORIZONS)
    bare = feats[["code", "date", "close", "setup", "abn_21"]]
    with pytest.raises(KeyError):
        edge_table(bare, 21)


def _planted_frame(sessions: int = 160, codes: int = 25) -> pd.DataFrame:
    """Panel harga dengan abnormal DITANAM: kode berindeks GENAP underperform
    ~0,2%/sesi relatif pasar, kode ganjil sebaliknya.

    Deterministis (tanpa angka acak) supaya tesnya tidak flaky. Variasi kecil
    antar sesi sengaja dipertahankan: deret edge yang rata sempurna membuat
    t-stat tak terdefinisi (``_nw_tstat`` mengembalikan tak-hingga). Besaran t di
    sini tidak realistis — data direkayasa supaya **tanda** edge-nya jelas, bukan
    untuk mengukur kekuatan bukti.
    """
    dates = pd.date_range("2025-01-01", periods=sessions, freq="B")
    rows = []
    for i in range(codes):
        sign = -1.0 if i % 2 == 0 else 1.0
        level = 100.0
        for j, d in enumerate(dates):
            rows.append({"code": f"C{i:03d}", "date": d, "close": level})
            level *= 1.0 + sign * 0.002 * (1.0 + 0.5 * ((j % 4) - 1.5))
    return _feats_from_prices(pd.DataFrame(rows), HORIZONS)


def _flat_frame(sessions: int = 160, codes: int = 25) -> pd.DataFrame:
    """Panel harga yang SEMUA emitannya bergerak persis sama: abnormal tiap emiten
    nol, jadi tidak ada edge yang mungkin terukur — apa pun benderanya."""
    dates = pd.date_range("2025-01-01", periods=sessions, freq="B")
    rows = [
        {"code": f"C{i:03d}", "date": d, "close": 100.0 + j}
        for i in range(codes)
        for j, d in enumerate(dates)
    ]
    return _feats_from_prices(pd.DataFrame(rows), HORIZONS)


def _flag_even_codes(feats: pd.DataFrame, name: str) -> pd.DataFrame:
    """Nyalakan satu komponen hanya di kode berindeks genap."""
    out = feats.copy()
    out[name] = 0.0
    out.loc[out["code"].str[-1].astype(int) % 2 == 0, name] = 1.0
    return out


def test_planted_component_get_negative_weight():
    """Komponen yang menandai kode-kode yang underperform harus dapat bobot
    negatif. Kalau tandanya terbalik, harness salah baca arah edge."""
    name = next(iter(COMPONENT_NOTES))
    feats = _flag_even_codes(_planted_frame(), name)
    report = walk_forward(feats, horizons=(21,), train=100, test=25, step=25)
    assert report["by_horizon"]["21"]["weights_all"][name] < 0


def test_pure_noise_gets_zero_weight():
    """Bendera menyala, tapi abnormal rata di seluruh emiten: tidak ada edge,
    jadi tidak ada komponen yang boleh dapat bobot."""
    name = next(iter(COMPONENT_NOTES))
    feats = _flag_even_codes(_flat_frame(), name)
    report = walk_forward(feats, horizons=(21,), train=100, test=25, step=25)
    per_horizon = report["by_horizon"]["21"]
    assert per_horizon["weights"] == {}
    assert not any(per_horizon["weights_all"].values())


def test_verdict_records_reason_for_every_component():
    """Komponen yang dimatikan harus menyebut alasannya — tidak ada yang hilang
    tanpa penjelasan."""
    feats = _flag_even_codes(_planted_frame(), next(iter(COMPONENT_NOTES)))
    report = walk_forward(feats, horizons=(21,), train=100, test=25, step=25)
    verdict = report["by_horizon"]["21"]["verdict"]
    assert verdict
    for name, entry in verdict.items():
        assert entry["status"] in {"dipakai", "dimatikan"}
        if entry["status"] == "dimatikan":
            assert entry.get("reason"), name
