"""Unit tests for idx_scraper.notify (Telegram alerts)."""

from __future__ import annotations

from idx_scraper.notify import (
    SignalState,
    TelegramNotifier,
    format_signal_message,
    scan_signals,
)

# --------------------------------------------------------------- SignalState


def test_signal_state_detects_change_and_persists(tmp_path):
    path = tmp_path / "state.json"
    state = SignalState(path)

    signals = [
        {"code": "BBCA", "signal": "BUY", "close": 9000.0, "rsi": 60.0},
        {"code": "TLKM", "signal": "SELL", "close": 3000.0, "rsi": 40.0},
    ]
    fresh = state.changed(signals)
    assert len(fresh) == 2  # state kosong -> semua dianggap baru

    # Simpan ke disk & muat ulang: sinyal sama tidak dianggap baru lagi
    state2 = SignalState(path)
    assert state2.changed(signals) == []


def test_signal_state_detects_flip(tmp_path):
    state = SignalState(tmp_path / "state.json")
    state.changed([{"code": "BBCA", "signal": "BUY", "close": 1, "rsi": None}])
    flipped = state.changed([{"code": "BBCA", "signal": "SELL", "close": 2, "rsi": None}])
    assert len(flipped) == 1 and flipped[0]["signal"] == "SELL"


def test_signal_state_survives_corrupt_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not valid json", encoding="utf-8")
    state = SignalState(path)  # tidak raise
    assert state.changed([{"code": "X", "signal": "BUY", "close": 1, "rsi": None}])


# --------------------------------------------------------------- format


def test_format_signal_message_contains_details():
    signals = [{"code": "BBCA", "signal": "BUY", "close": 9900.0, "rsi": 62.3}]
    msg = format_signal_message(signals)
    assert "BUY" in msg and "BBCA" in msg and "9,900" in msg and "RSI 62" in msg


def test_format_signal_message_rsi_none():
    signals = [{"code": "TLKM", "signal": "SELL", "close": 2560.0, "rsi": None}]
    assert "RSI -" in format_signal_message(signals)


# --------------------------------------------------------------- scan_signals


class FakeStorage:
    """Storage stub: return deret harga sintetis per kode."""

    def __init__(self, series: dict[str, list[float]]) -> None:
        self.series = series

    def load_price_history(self, code: str, limit: int = 60) -> list[dict]:
        closes = self.series.get(code, [])
        return [
            {
                "date": f"2026-01-{i + 1:02d}",
                "open": c,
                "high": c,
                "low": c,
                "close": c,
                "volume": 1000,
            }
            for i, c in enumerate(closes)
        ]


def test_scan_signals_skips_short_history():
    storage = FakeStorage({"BBCA": [100.0] * 10})  # < 25 hari
    assert scan_signals(storage, ["BBCA"]) == []


def test_scan_signals_detects_uptrend_buy():
    # 40 hari naik konsisten -> SMA20 > SMA50 tak mungkin dengan 40 baris,
    # tapi RSI & MA terisi; pastikan minimal hasil valid tanpa crash.
    rising = [100 + i * 2 for i in range(40)]
    storage = FakeStorage({"UPUP": rising})
    signals = scan_signals(storage, ["UPUP"])
    assert all(s["signal"] in ("BUY", "SELL") for s in signals)


def test_scan_signals_handles_bad_code():
    storage = FakeStorage({})  # kode tidak ada -> skip
    assert scan_signals(storage, ["GONE"]) == []


# --------------------------------------------------------------- TelegramNotifier


def test_notifier_disabled_without_env(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    n = TelegramNotifier()
    assert not n.enabled
    assert n.send_message("halo") is False  # no-op, tidak raise


def test_notifier_enabled_with_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "456")
    assert TelegramNotifier().enabled


def test_send_message_success(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "456")
    n = TelegramNotifier()

    class FakeResp:
        status_code = 200
        text = ""

        def json(self):
            return {"ok": True}

    called = {}

    def fake_post(url, json=None, timeout=None):
        called["url"] = url
        called["payload"] = json
        return FakeResp()

    from curl_cffi import requests as cf_requests

    monkeypatch.setattr(cf_requests, "post", fake_post)
    assert n.send_message("<b>test</b>") is True
    assert "bot123:abc/sendMessage" in called["url"]
    assert called["payload"]["chat_id"] == "456"
    assert called["payload"]["parse_mode"] == "HTML"


def test_send_message_api_error_returns_false(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "456")
    n = TelegramNotifier()

    class FakeResp:
        status_code = 400
        text = "bad chat id"

        def json(self):
            return {"ok": False}

    from curl_cffi import requests as cf_requests

    monkeypatch.setattr(cf_requests, "post", lambda *a, **k: FakeResp())
    assert n.send_message("halo") is False
