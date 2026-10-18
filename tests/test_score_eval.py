"""Unit tests for idx_scraper.research.score_eval (harness walk-forward).

Tes di sini tidak menyentuh DB atau jaringan — yang diuji hanya fungsi murni.

Tujuannya bukan menguji kode baru, tapi mengunci satu klaim yang selama ini
tidak pernah dibuktikan: **angka OOS di harness ini benar-benar out-of-sample**,
yaitu setiap jendela test berada SETELAH jendela train-nya dan tidak pernah
tumpang tindih dengan jendela test lain.
"""

from __future__ import annotations

from itertools import pairwise

import pandas as pd

from idx_scraper.research.recommend import COMPONENT_NOTES, HORIZONS
from idx_scraper.research.score_eval import attach_abnormal, walk_forward


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
