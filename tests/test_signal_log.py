"""Unit tests for idx_scraper.research.signal_log (jejak sinyal / track record)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from idx_scraper.analysis import calculate_indicators, signal_series
from idx_scraper.research.signal_log import (
    _recent_ex_div_cash,
    attach_outcomes,
    compute_signal_rows,
    summarize_outcomes,
)


def make_panel(closes: list[float], code: str = "AAA") -> pd.DataFrame:
    dates = pd.date_range("2025-01-01", periods=len(closes), freq="B")
    return pd.DataFrame({"code": code, "date": dates, "close": closes})


def up_series(n: int = 140) -> list[float]:
    """Tren naik landai: 3 gain +0,4% & 1 pullback -0,6% per 4 hari.

    Konstruksi ini menjaga RSI ~67 (di band BUY 50-80) sambil SMA20 > SMA50.
    """
    p, out = 100.0, []
    for i in range(n):
        p *= 0.994 if i % 4 == 3 else 1.004
        out.append(round(p, 3))
    return out


def down_series(n: int = 140) -> list[float]:
    """Kebalikan up_series: RSI ~33 & SMA20 < SMA50 -> SELL."""
    p, out = 200.0, []
    for i in range(n):
        p *= 1.006 if i % 4 == 3 else 0.996
        out.append(round(p, 3))
    return out


# --------------------------------------------------------------------------- #
# Deteksi
# --------------------------------------------------------------------------- #


def test_rows_match_signal_series_invariant():
    """Baris yang dicatat = tepat posisi non-HOLD setelah warmup."""
    closes = up_series() + down_series()
    panel = make_panel(closes)
    min_history = 60
    rows = compute_signal_rows(panel, min_history=min_history)

    ind = calculate_indicators(panel[["date", "close"]])
    sig = signal_series(ind)
    expected = {
        (pd.Timestamp(ind["date"].iloc[i]), str(sig.iloc[i]))
        for i in range(len(ind))
        if i >= min_history and sig.iloc[i] != "HOLD"
    }
    got = {(pd.Timestamp(r.date), r.signal) for r in rows.itertuples()}
    assert got == expected
    assert set(rows["signal"]) <= {"BUY", "SELL"}


def test_both_signal_types_appear():
    up = compute_signal_rows(make_panel(up_series()), min_history=60)
    down = compute_signal_rows(make_panel(down_series()), min_history=60)
    assert "BUY" in set(up["signal"])
    assert "SELL" in set(down["signal"])


def test_warmup_gate_blocks_short_history():
    assert compute_signal_rows(make_panel(up_series(40)), min_history=60).empty


def test_untraded_day_signals_are_skipped():
    """Volume 0 = harga carry-over; sinyalnya artefak & tak bisa dieksekusi."""
    closes = up_series()
    panel = make_panel(closes)
    panel["volume"] = [1_000_000 if i < 60 else 0 for i in range(len(closes))]
    assert compute_signal_rows(panel, min_history=60).empty
    assert not compute_signal_rows(panel, min_history=60, require_volume=False).empty


def test_traded_volume_keeps_signals():
    closes = up_series()
    panel = make_panel(closes)
    panel["volume"] = [1_000_000] * len(closes)
    with_vol = compute_signal_rows(panel, min_history=60)
    without = compute_signal_rows(make_panel(closes), min_history=60)
    pd.testing.assert_frame_equal(with_vol.reset_index(drop=True), without.reset_index(drop=True))


def test_no_lookahead_on_signal_dates():
    """Menambah data masa depan tidak boleh mengubah sinyal tanggal lama."""
    closes = up_series() + down_series()
    full = compute_signal_rows(make_panel(closes), min_history=60)
    truncated = compute_signal_rows(make_panel(closes[:-25]), min_history=60)
    pd.testing.assert_frame_equal(
        truncated.reset_index(drop=True),
        full[full["date"] <= pd.Timestamp(truncated["date"].max())].reset_index(drop=True),
    )


# --------------------------------------------------------------------------- #
# Filter SELL palsu ex-dividend
# --------------------------------------------------------------------------- #


def test_recent_ex_div_window():
    from datetime import date

    divs = [(date(2025, 3, 1), 25.0), (date(2025, 6, 1), 50.0)]
    # tepat 3 hari setelah ex-date 1 Juni -> dalam window 7 hari
    assert _recent_ex_div_cash(divs, pd.Timestamp("2025-06-04")) == 50.0
    # 20 hari setelah ex-date terakhir -> di luar window
    assert _recent_ex_div_cash(divs, pd.Timestamp("2025-06-21")) is None
    # sebelum ex-date pertama
    assert _recent_ex_div_cash(divs, pd.Timestamp("2025-01-01")) is None


def test_mechanical_sell_is_dropped(monkeypatch):
    panel = make_panel(down_series())
    base = compute_signal_rows(panel, min_history=60)
    sells = base[base["signal"] == "SELL"]
    assert not sells.empty
    sell_date = sells["date"].iloc[-1].date()

    # Guard di-stub True: SELL tepat setelah ex-dividend dianggap palsu.
    monkeypatch.setattr(
        "idx_scraper.research.signal_log.is_mechanical_sell", lambda _df, _cash: True
    )
    guarded = compute_signal_rows(
        panel, min_history=60, dividends={"AAA": [(sell_date, 12.0)]}
    )
    assert "SELL" not in set(guarded["signal"])
    assert guarded.empty or set(guarded["signal"]) <= {"BUY"}


def test_real_sell_survives_without_dividend():
    rows = compute_signal_rows(make_panel(down_series()), min_history=60)
    assert "SELL" in set(rows["signal"])


# --------------------------------------------------------------------------- #
# Outcome (forward return, MFE/MAE, abnormal)
# --------------------------------------------------------------------------- #


def test_forward_return_and_excursions():
    panel = make_panel([100.0, 101.0, 102.0, 103.0, 104.0, 105.0])
    out = attach_outcomes(panel, horizons=(2,), nlags=1)

    # Row 0: entry di pos 1 (101), exit di pos 3 (103), window = [101, 102, 103].
    row0 = out.iloc[0]
    assert row0["fwd_2"] == pytest.approx(103.0 / 101.0 - 1.0)
    assert row0["mfe_2"] == pytest.approx(103.0 / 101.0 - 1.0)  # puncak window
    assert row0["mae_2"] == pytest.approx(0.0)  # entry = titik terendah window

    # Row 1: entry di pos 2 (102), exit di pos 4 (104).
    row1 = out.iloc[1]
    assert row1["fwd_2"] == pytest.approx(104.0 / 102.0 - 1.0)


def test_mfe_mae_capture_excursion():
    # Row 0: entry di pos 1 (101); window pos 1..3 = [101, 130, 90].
    panel = make_panel([100.0, 101.0, 130.0, 90.0, 95.0])
    out = attach_outcomes(panel, horizons=(2,), nlags=1)
    r = out.iloc[0]
    assert r["mfe_2"] == pytest.approx(130.0 / 101.0 - 1.0)
    assert r["mae_2"] == pytest.approx(90.0 / 101.0 - 1.0)


def test_single_code_abnormal_is_zero():
    """Pasar equal-weight dari panel satu emiten = return emiten itu sendiri."""
    panel = make_panel(up_series() + down_series())
    out = attach_outcomes(panel, horizons=(5, 10), nlags=1)
    abn = out["abn_5"].dropna()
    assert not abn.empty
    assert abn.abs().max() < 1e-9


def test_last_rows_have_no_future_label():
    panel = make_panel([100.0] * 40 + [101.0, 102.0, 103.0])
    out = attach_outcomes(panel, horizons=(5,), nlags=1)
    assert pd.isna(out["fwd_5"].iloc[-1])
    assert pd.isna(out["mfe_5"].iloc[-1])


# --------------------------------------------------------------------------- #
# Agregasi
# --------------------------------------------------------------------------- #


def test_summarize_outcomes_shape():
    panel = make_panel(up_series() + down_series() + down_series(60))
    log = pd.DataFrame(
        {
            "code": ["AAA", "AAA"],
            "date": pd.to_datetime(["2025-04-01", "2025-05-01"]),
            "signal": ["BUY", "SELL"],
            "close": [150.0, 140.0],
            "rsi": [60.0, 40.0],
            "sma_short": [148.0, 142.0],
            "sma_long": [145.0, 145.0],
            "trend_up": [True, False],
            "regime": ["TRENDING_UP", None],
        }
    )
    out = attach_outcomes(panel, horizons=(5, 10))
    summary = summarize_outcomes(log, out, horizons=(5, 10))

    assert summary["signals"] == 2
    assert summary["buy"] == 1 and summary["sell"] == 1
    assert len(summary["overall"]) == 4  # 2 sinyal x 2 horizon
    assert len(summary["by_regime"]) >= 2  # TIDAK DIKETAHUI ikut masuk
    assert summary["recent"][0]["fwd_5"] is None or isinstance(
        summary["recent"][0]["fwd_5"], float
    )
    for row in summary["overall"]:
        assert {"signal", "horizon", "n", "hit_rate", "mean_fwd", "avg_mfe", "avg_mae"} <= set(row)


def test_summarize_empty_log():
    summary = summarize_outcomes(pd.DataFrame(), pd.DataFrame(), horizons=(5,))
    assert summary["signals"] == 0
    assert summary["overall"] == []


def test_hit_rate_and_means():
    panel = make_panel([100.0, 100.0, 110.0, 121.0, 133.1])
    log = pd.DataFrame(
        {
            "code": ["AAA"],
            "date": pd.to_datetime(["2025-01-01"]),
            "signal": ["BUY"],
            "close": [100.0],
            "rsi": [60.0],
            "sma_short": [100.0],
            "sma_long": [99.0],
            "trend_up": [True],
            "regime": [None],
        }
    )
    out = attach_outcomes(panel, horizons=(2,), nlags=1)
    s = summarize_outcomes(log, out, horizons=(2,))["overall"][0]
    assert s["n"] == 1
    assert s["hit_rate"] == 1.0
    assert s["mean_fwd"] == pytest.approx(133.1 / 110.0 - 1.0)
    assert s["avg_mfe"] is not None and s["avg_mfe"] >= s["mean_fwd"]


def test_returns_dataframe_columns_sorted():
    rows = compute_signal_rows(make_panel(up_series()), min_history=60)
    assert list(rows.columns) == [
        "code", "date", "signal", "close", "rsi", "sma_short", "sma_long", "trend_up",
    ]
    assert np.issubdtype(rows["date"].dtype, np.datetime64)
