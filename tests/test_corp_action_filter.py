"""Unit tests: corp-action filter — SELL palsu ex-dividend.

Skenario inti: saham downtrend (SELL sah) turun mekanis Rp X/saham pada
ex-date dividend. Bila sinyal SELL hilang setelah close terakhir dikembalikan
ke basis total-return (+div), SELL itu mekanis -> dinetralkan jadi HOLD.
"""

from __future__ import annotations

import pandas as pd

from idx_scraper.analysis import calculate_indicators, generate_signal, is_mechanical_sell
from idx_scraper.api import services


def _raw(closes: list[float], start: str = "2026-01-01") -> pd.DataFrame:
    n = len(closes)
    return pd.DataFrame(
        {
            "date": pd.date_range(start, periods=n, freq="B"),
            "close": closes,
            "volume": [1_000_000] * n,
        }
    )


def _sell_closes() -> list[float]:
    # Falling sawtooth (-0.3, -0.3, +0.5): SMA20 < SMA50, RSI di band 20-50.
    closes = [112.0]
    for i in range(1, 60):
        closes.append(closes[-1] + [-0.3, -0.3, 0.5][(i - 1) % 3])
    return closes


def _buy_closes() -> list[float]:
    closes = [100.0]
    for i in range(1, 60):
        closes.append(closes[-1] + [0.3, 0.3, -0.5][(i - 1) % 3])
    return closes


def _sell_frame() -> pd.DataFrame:
    return calculate_indicators(_raw(_sell_closes()))


# --------------------------------------------------------------------------- #
# is_mechanical_sell (pure)
# --------------------------------------------------------------------------- #


def test_baseline_frame_is_sell():
    df = _sell_frame()
    assert 20 <= df["RSI"].iloc[-1] <= 50
    assert generate_signal(df) == "SELL"


def test_mechanical_sell_detected_on_ex_div_drop():
    # Dividen cukup besar: pada basis total-return RSI lewat 50 -> SELL hilang.
    df = _sell_frame()
    assert is_mechanical_sell(df, 3.0) is True


def test_small_dividend_still_real_sell():
    # Dividen kecil tidak mengubah rekomendasi -> tekanan jual sungguhan.
    df = _sell_frame()
    assert is_mechanical_sell(df, 0.5) is False


def test_no_dividend_is_never_mechanical():
    df = _sell_frame()
    assert is_mechanical_sell(df, None) is False
    assert is_mechanical_sell(df, 0) is False
    assert is_mechanical_sell(df, -1.0) is False


def test_buy_signal_is_never_mechanical():
    df = calculate_indicators(_raw(_buy_closes()))
    assert generate_signal(df) == "BUY"
    assert is_mechanical_sell(df, 3.0) is False


def test_empty_frame_is_safe():
    assert is_mechanical_sell(pd.DataFrame(), 5.0) is False


# --------------------------------------------------------------------------- #
# Guard di get_signals (DB di-mock lewat monkeypatch)
# --------------------------------------------------------------------------- #


class _FakeCursor:
    """Cursor minim: query dividend selalu dijawab baris yang diberikan."""

    def __init__(self, row: dict | None):
        self._row = row

    def execute(self, query: str, params=None):
        self.last_query = query

    def fetchone(self):
        return self._row

    def fetchall(self):
        return [self._row] if self._row else []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_market(monkeypatch, closes: list[float], div_row: dict | None) -> None:
    monkeypatch.setattr(services, "get_cursor", lambda: _FakeCursor(div_row))
    monkeypatch.setattr(services, "_build_frame", lambda cur, code: (_raw(closes), False))


def test_get_signals_neutralizes_mechanical_sell(monkeypatch):
    _patch_market(monkeypatch, _sell_closes(), {"cash_amount": 3.0})
    rows = services.get_signals(["TEST"])
    assert len(rows) == 1
    r = rows[0]
    assert r["raw_signal"] == "SELL"
    assert r["signal"] == "HOLD"
    assert r["div_adjusted"] is True
    assert r["div_cash"] == 3.0


def test_get_signals_keeps_real_sell_without_dividend(monkeypatch):
    _patch_market(monkeypatch, _sell_closes(), None)
    r = services.get_signals(["TEST"])[0]
    assert r["signal"] == "SELL"
    assert r["raw_signal"] == "SELL"
    assert r["div_adjusted"] is False
    assert r["div_cash"] is None


def test_get_signals_keeps_real_sell_when_div_too_small(monkeypatch):
    _patch_market(monkeypatch, _sell_closes(), {"cash_amount": 0.5})
    r = services.get_signals(["TEST"])[0]
    assert r["signal"] == "SELL"
    assert r["div_adjusted"] is False
    assert r["div_cash"] == 0.5
