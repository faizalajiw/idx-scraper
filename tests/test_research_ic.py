"""Unit tests for idx_scraper.research (faktor + IC engine).

Semua expected value dihitung manual pada seri kecil yang deterministik —
bukan snapshot output fungsi.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from idx_scraper.research.factors import compute_factors
from idx_scraper.research.ic import (
    _shift_by_pos,
    _spearman,
    forward_returns,
    quintile_returns,
    rank_ic,
    summarize,
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def make_panel(prices_by_code: dict[str, list[float]], start: str = "2026-01-01") -> pd.DataFrame:
    """Panel kecil: satu seri close per kode, tanggal bursa simulasi harian."""
    frames = []
    n = max(len(v) for v in prices_by_code.values())
    dates = pd.date_range(start, periods=n, freq="B")
    for code, closes in prices_by_code.items():
        frames.append(
            pd.DataFrame(
                {
                    "code": code,
                    "date": dates[: len(closes)],
                    "close": closes,
                    "volume": [1_000_000] * len(closes),
                    "value": [1_000_000.0 * c for c in closes],
                }
            )
        )
    return pd.concat(frames, ignore_index=True)


# --------------------------------------------------------------------------- #
# Factors — no look-ahead & warmup NaN
# --------------------------------------------------------------------------- #


def test_mom_5d_manual_value():
    closes = [100.0, 101, 102, 103, 104, 110]
    panel = make_panel({"AAA": closes})
    out = compute_factors(panel)
    # mom_5d di baris terakhir = 110/100 - 1
    assert out["mom_5d"].iloc[-1] == pytest.approx(0.10)
    # warmup: 5 baris pertama NaN
    assert out["mom_5d"].iloc[:5].isna().all()


def test_rev_1d_is_negative_of_return():
    closes = [100.0, 110.0, 99.0]
    panel = make_panel({"AAA": closes})
    out = compute_factors(panel)
    ret = [110 / 100 - 1, 99 / 110 - 1]
    got = out["rev_1d"].dropna().tolist()
    assert got[0] == pytest.approx(-ret[0])
    assert got[1] == pytest.approx(-ret[1])


def test_factors_do_not_use_future_data():
    """Ubah close di masa depan tidak boleh mengubah faktor di masa lalu."""
    closes = [100.0 + i for i in range(30)]
    panel_a = make_panel({"AAA": closes})
    panel_b = make_panel({"AAA": closes[:-5] + [9999.0] * 5})
    out_a = compute_factors(panel_a)
    out_b = compute_factors(panel_b)
    past = slice(0, len(closes) - 5)
    pd.testing.assert_frame_equal(out_a.iloc[past], out_b.iloc[past])


def test_streak_runs():
    # net: +100, +50, -10, -20, -30, +5 -> streak: +1,+2,-1,-2,-3,+1
    net = [100.0, 50.0, -10.0, -20.0, -30.0, 5.0]
    n = len(net)
    dates = pd.date_range("2026-01-01", periods=n, freq="B")
    panel = pd.DataFrame(
        {
            "code": "AAA",
            "date": dates,
            "close": [100.0] * n,
            "volume": [1_000_000] * n,
            "value": [100_000_000.0] * n,
            "foreign_net": net,
        }
    )
    out = compute_factors(panel)
    got = out["foreign_streak"].tolist()
    assert got == [1.0, 2.0, -1.0, -2.0, -3.0, 1.0]


def test_foreign_nan_stays_nan():
    net = [100.0, None, 50.0]
    n = len(net)
    panel = pd.DataFrame(
        {
            "code": "AAA",
            "date": pd.date_range("2026-01-01", periods=n, freq="B"),
            "close": [100.0] * n,
            "volume": [1_000_000] * n,
            "value": [100_000_000.0] * n,
            "foreign_net": net,
        }
    )
    out = compute_factors(panel)
    assert pd.isna(out["foreign_streak"].iloc[1])
    assert out["foreign_streak"].iloc[2] == 1.0  # streak reset setelah gap


# --------------------------------------------------------------------------- #
# Forward returns & IC
# --------------------------------------------------------------------------- #


def test_forward_returns_with_one_day_fill_lag():
    # Harga: idx0..4 = 100,110,121,100,100 ; nlags=1, k=1
    # Baris T=idx0: entry di idx1 (110), exit idx2 (121) -> +10%
    panel = make_panel({"AAA": [100.0, 110.0, 121.0, 100.0, 100.0]})
    out = forward_returns(panel, [1], nlags=1)
    assert out["fwd_1"].iloc[0] == pytest.approx(121.0 / 110.0 - 1)
    # Baris terakhir tidak punya exit -> NaN
    assert pd.isna(out["fwd_1"].iloc[-1])


def test_forward_returns_zero_lag_uses_same_day():
    # nlags=0: entry = close hari faktor itu sendiri (standar IC teoretis)
    panel = make_panel({"AAA": [100.0, 110.0, 121.0, 100.0, 100.0]})
    out = forward_returns(panel, [1], nlags=0)
    assert out["fwd_1"].iloc[0] == pytest.approx(110.0 / 100.0 - 1)


def test_shift_by_pos():
    panel = make_panel({"AAA": [1.0, 2.0, 3.0], "BBB": [4.0, 5.0, 6.0]})
    panel = panel.sort_values(["code", "date"]).reset_index(drop=True)
    pos = panel.groupby("code").cumcount() + 1
    got = _shift_by_pos(panel, "close", pos)
    # tiap baris mengambil close dari baris BERIKUTNYA di emiten yang sama
    a = got.iloc[:3].to_numpy(dtype=float)
    b = got.iloc[3:].to_numpy(dtype=float)
    assert np.array_equal(a, np.array([2.0, 3.0, np.nan]), equal_nan=True)
    assert np.array_equal(b, np.array([5.0, 6.0, np.nan]), equal_nan=True)


def test_spearman_perfect_and_inverse():
    x = pd.Series([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], dtype=float)
    y_up = x * 2.0
    y_down = -x
    assert _spearman(x, y_up) == pytest.approx(1.0)
    assert _spearman(x, y_down) == pytest.approx(-1.0)


def test_rank_ic_perfect_predictor():
    # Faktor = return masa depan itu sendiri -> IC harus 1.0 tiap hari.
    # Butuh >= 10 nama per hari (threshold _spearman).
    rng = np.random.default_rng(42)
    n_days, per_day = 20, 15
    rows = []
    for d in range(n_days):
        fwd = rng.normal(0, 0.02, per_day)
        rows.append(
            pd.DataFrame(
                {
                    "date": pd.Timestamp("2026-01-01") + pd.Timedelta(days=d),
                    "f": fwd,
                    "fwd_5": fwd,
                }
            )
        )
    df = pd.concat(rows, ignore_index=True)
    ic = rank_ic(df, ["f"], 5, min_names=5)
    got = ic["f"].dropna().astype(float)
    assert len(got) == n_days
    assert got.sub(1.0).abs().max() < 1e-9


def test_summarize_newey_west_lag_semantics():
    # IC konstan 0.1 selama 10 hari -> mean 0.1, std 0, ICIR/t-stat = inf bermakna
    ic_df = pd.DataFrame({"date": pd.date_range("2026-01-01", periods=10, freq="B"), "f": [0.1] * 10})
    s = summarize(ic_df, ["f"])
    row = s.iloc[0]
    assert row["mean_ic"] == pytest.approx(0.1)
    assert row["ic_std"] == pytest.approx(0.0, abs=1e-12)
    assert math.isinf(row["icir"]) and row["icir"] > 0
    assert math.isinf(row["t_stat"]) and row["t_stat"] > 0


def test_summarize_t_stat_white_noise():
    # 40 hari IC i.i.d. (white noise) -> t-stat NW harus dekat t-stat klasik
    rng = np.random.default_rng(3)
    vals = rng.normal(0.05, 0.1, 40)
    ic_df = pd.DataFrame({"date": pd.date_range("2026-01-01", periods=40, freq="B"), "f": vals})
    s = summarize(ic_df, ["f"])
    row = s.iloc[0]
    sem = vals.std(ddof=1) / math.sqrt(40)
    klasik = vals.mean() / sem
    assert abs(row["t_stat"] - klasik) / abs(klasik) < 0.30


def test_quintile_monotonic():
    # Faktor = fwd return (perfect) -> kuintil teratas harus paling tinggi
    rng = np.random.default_rng(7)
    n_days, per_day = 40, 30
    rows = []
    for d in range(n_days):
        f = rng.normal(size=per_day)
        rows.append(
            pd.DataFrame(
                {
                    "date": pd.Timestamp("2026-01-01") + pd.Timedelta(days=d),
                    "f": f,
                    "fwd_5": f * 0.02 + rng.normal(0, 1e-9, per_day),  # ~monotonic
                }
            )
        )
    df = pd.concat(rows, ignore_index=True)
    q = quintile_returns(df, "f", 5, q=5)
    assert len(q) == 5
    assert q["mean_fwd_ret"].is_monotonic_increasing
