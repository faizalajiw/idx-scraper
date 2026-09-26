"""Unit tests for idx_scraper.research.events (event study engine)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from idx_scraper.research.events import (
    _nw_tstat,
    detect_events,
    event_forward_returns,
    run_event_study,
)


def make_panel(prices: dict[str, list[float]], volumes: dict[str, list[int]] | None = None):
    n = max(len(v) for v in prices.values())
    dates = pd.date_range("2026-01-01", periods=n, freq="B")
    frames = []
    for code, closes in prices.items():
        vols = (volumes or {}).get(code, [1_000_000] * len(closes))
        frames.append(
            pd.DataFrame(
                {
                    "code": code,
                    "date": dates[: len(closes)],
                    "close": closes,
                    "volume": vols,
                }
            )
        )
    return pd.concat(frames, ignore_index=True)


# --------------------------------------------------------------------------- #
# Deteksi
# --------------------------------------------------------------------------- #


def test_detect_jump_up():
    # 30 hari flat lalu lonjakan +5% dengan volume 3x
    closes = [100.0] * 30 + [105.0]
    vols = [1_000_000] * 30 + [3_000_000]
    panel = make_panel({"AAA": closes}, {"AAA": vols})
    ev = detect_events(panel, "jump_up", min_gap=1)
    assert len(ev) == 1
    assert ev.iloc[0]["date"] == panel["date"].iloc[-1]


def test_detect_jump_requires_volume():
    closes = [100.0] * 30 + [105.0]
    vols = [1_000_000] * 31  # volume biasa -> bukan event
    panel = make_panel({"AAA": closes}, {"AAA": vols})
    assert detect_events(panel, "jump_up", min_gap=1).empty


def test_min_gap_dedup():
    # Dua lonjakan (idx 25 dan idx 29, terpisah 4 hari kalender) -> hanya yang
    # pertama yang lolos ambang min_gap=10 hari bursa (~14 hari kalender).
    closes = [100.0] * 25 + [105.0, 105.0, 105.0, 110.4] + [110.0] * 5
    vols = [1_000_000] * 25 + [3_000_000] * 9
    panel = make_panel({"AAA": closes}, {"AAA": vols})
    ev = detect_events(panel, "jump_up", min_gap=10)
    assert len(ev) == 1
    assert ev.iloc[0]["date"] == panel["date"].iloc[25]


def test_event_detection_no_lookahead():
    """Event di baris T tidak boleh berubah ketika data masa depan berubah."""
    closes = [100.0] * 30 + [105.0] + [110.0, 90.0]
    vols = [1_000_000] * 30 + [3_000_000] * 3
    panel_a = make_panel({"AAA": closes}, {"AAA": vols})
    panel_b = make_panel({"AAA": closes[:-2] + [500.0, 500.0]}, {"AAA": vols})
    ev_a = detect_events(panel_a, "jump_up", min_gap=1)
    ev_b = detect_events(panel_b, "jump_up", min_gap=1)
    pd.testing.assert_frame_equal(ev_a, ev_b)


def test_ma_cross_up_detection():
    # Turun panjang lalu naik tajam: cross up di sekitar pergantian arah
    down = list(np.linspace(120, 100, 40))
    up = [100 + i * 1.5 for i in range(1, 25)]
    panel = make_panel({"AAA": down + up})
    ev = detect_events(panel, "ma_cross_up", min_gap=1)
    assert not ev.empty
    # cross down tidak boleh ada di seri yang naik di akhir
    ev_down = detect_events(panel, "ma_cross_down", min_gap=1)
    last_down_date = ev_down["date"].max() if not ev_down.empty else None
    assert last_down_date is None or last_down_date < pd.Timestamp("2026-02-01")


# --------------------------------------------------------------------------- #
# Forward return & abnormal
# --------------------------------------------------------------------------- #


def test_forward_return_entry_next_day():
    # Event di baris idx 30 (105); entry = close idx31 (110), exit idx 33
    closes = [100.0] * 30 + [105.0] + [110.0, 115.5, 115.5]
    vols = [1_000_000] * 30 + [3_000_000] * 4
    panel = make_panel({"AAA": closes}, {"AAA": vols})
    ev = detect_events(panel, "jump_up", min_gap=1)
    fwd = event_forward_returns(panel, ev, horizon=2, market=False)
    assert len(fwd) == 1
    expected = 115.5 / 110.0 - 1
    assert fwd.iloc[0]["fwd"] == pytest.approx(expected)


def test_abnormal_vs_equal_weight_market():
    # AAA naik +5% di hari event; BBB flat -> abnormal = fwd AAA - fwd pasar
    closes_a = [100.0] * 30 + [105.0] + [110.0, 110.0, 110.0]
    closes_b = [100.0] * 33
    panel = make_panel(
        {"AAA": closes_a, "BBB": closes_b},
        # volume AAA harus spike di hari event, kalau tidak jump tidak terdeteksi
        {"AAA": [1_000_000] * 30 + [3_000_000] * 4, "BBB": [1_000_000] * 33},
    )
    ev = detect_events(panel, "jump_up", min_gap=1)
    fwd = event_forward_returns(panel, ev, horizon=2, market=True)
    # fwd AAA = 0 (110 -> 110); pasar = 0 (100 -> 100) -> abnormal ~ 0
    assert fwd.iloc[0]["abnormal"] == pytest.approx(0.0, abs=1e-9)


def test_nw_tstat_constant_series():
    s = pd.Series([0.01] * 10)
    t = _nw_tstat(s)
    assert t is not None and t > 0


def test_run_event_study_smoke():
    rng = np.random.default_rng(11)
    closes = list(100 + np.cumsum(rng.normal(0, 1, 120)))
    vols = [1_000_000] * 120
    panel = make_panel({"AAA": closes}, {"AAA": vols})
    r = run_event_study(panel, "jump_up", horizon=5, min_gap=5)
    assert r.n_events >= 0  # tidak meledak; angka tergantung random path
    assert r.horizon == 5
