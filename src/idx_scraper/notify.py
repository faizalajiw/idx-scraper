"""Telegram notifier untuk trading signals IDX.

Cara pakai:
1. Bikin bot via @BotFather di Telegram -> dapat TOKEN.
2. Chat dulu sama bot lo (kirim /start), lalu ambil chat_id
   (mis. via https://api.telegram.org/bot<TOKEN>/getUpdates).
3. Isi .env:
       TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
       TELEGRAM_CHAT_ID=987654321
4. Jalankan `idx alerts` (sekali) atau biarkan `idx serve` mengirim
   otomatis tiap IDX_ALERT_INTERVAL detik.

Anti-spam: sinyal terakhir per ticker disimpan di file state JSON
(default .signal_state.json). Alert hanya dikirim saat sinyal BERUBAH
(mis. HOLD -> BUY, BUY -> SELL).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pandas as pd

from .analysis import calculate_indicators, generate_signal

TELEGRAM_API = "https://api.telegram.org"
SIG_EMOJI = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}


class TelegramNotifier:
    """Kirim pesan ke Telegram via Bot API. Nonaktif kalau env belum diisi."""

    def __init__(
        self,
        bot_token: str | None = None,
        chat_id: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def _post(self, method: str, payload: dict) -> dict | None:
        """POST ke Bot API. Balikin JSON response atau None kalau gagal."""
        if not self.enabled:
            return None
        from curl_cffi import requests as cf_requests

        url = f"{TELEGRAM_API}/bot{self.bot_token}/{method}"
        try:
            r = cf_requests.post(url, json=payload, timeout=self.timeout)
            if r.status_code != 200:
                print(f"[warn] telegram {method} status {r.status_code}: {r.text[:200]}", file=sys.stderr)
                return None
            return r.json()
        except Exception as e:  # jaringan/timeout — jangan bikin scraper mati
            print(f"[warn] telegram {method} gagal: {e}", file=sys.stderr)
            return None

    def send_message(self, text: str) -> bool:
        """Kirim 1 pesan (HTML). True kalau sukses."""
        if not self.enabled:
            print("[info] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID belum diisi — skip kirim", file=sys.stderr)
            return False
        resp = self._post(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        return bool(resp and resp.get("ok"))

    def test_connection(self) -> bool:
        """Panggil getMe untuk validasi token."""
        resp = self._post("getMe", {})
        if resp and resp.get("ok"):
            me = resp["result"]
            print(f"telegram OK — bot @{me.get('username')}")
            return True
        print("telegram gagal — cek TELEGRAM_BOT_TOKEN", file=sys.stderr)
        return False


def format_signal_message(signals: list[dict]) -> str:
    """Format daftar sinyal jadi satu pesan Telegram (HTML).

    signals: list of {code, signal, close, rsi, ...} — lihat scan_signals().
    """
    lines = ["<b>📡 IDX Signal Alert</b>", f"<i>{time.strftime('%Y-%m-%d %H:%M WIB', time.gmtime(time.time() + 7 * 3600))}</i>", ""]
    for s in signals:
        emoji = SIG_EMOJI.get(s["signal"], "")
        rsi = f"RSI {s['rsi']:.0f}" if s.get("rsi") is not None else "RSI -"
        lines.append(f"{emoji} <b>{s['signal']}</b> <code>{s['code']}</code> @ {s['close']:,.0f} ({rsi})")
    lines.append("")
    lines.append("<i>Rule-based (SMA20/50 + RSI). Bukan nasihat keuangan.</i>")
    return "\n".join(lines)


def scan_signals(storage, codes: list[str], min_days: int = 25) -> list[dict]:
    """Hitung sinyal non-HOLD terbaru untuk daftar kode."""
    out: list[dict] = []
    for code in codes:
        try:
            rows = storage.load_price_history(code, limit=60)
            if len(rows) < min_days:
                continue
            df = pd.DataFrame(rows)
            df = calculate_indicators(df)
            signal = generate_signal(df)
            if signal == "HOLD":
                continue
            last = df.iloc[-1]
            rsi = last.get("RSI")
            out.append({
                "code": code,
                "signal": signal,
                "close": float(last["close"]),
                "rsi": float(rsi) if rsi is not None and pd.notna(rsi) else None,
            })
        except Exception as e:
            print(f"[warn] scan signal {code}: {e}", file=sys.stderr)
    return out


class SignalState:
    """State sinyal terakhir per ticker (file JSON) — untuk dedup alert."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or os.getenv("IDX_SIGNAL_STATE", ".signal_state.json"))
        self._data: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"[warn] state file rusak, mulai dari kosong: {e}", file=sys.stderr)
            self._data = {}

    def save(self) -> None:
        self.path.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")

    def changed(self, signals: list[dict]) -> list[dict]:
        """Saring sinyal yang beda dari state terakhir, lalu update state."""
        fresh: list[dict] = []
        for s in signals:
            code, sig = s["code"], s["signal"]
            if self._data.get(code) != sig:
                fresh.append(s)
                self._data[code] = sig
        if fresh:
            self.save()
        return fresh


def run_alert_check(storage, codes: list[str], notifier: TelegramNotifier, state: SignalState) -> int:
    """Scan sinyal, kirim yang berubah via Telegram. Return jumlah alert terkirim."""
    signals = scan_signals(storage, codes)
    fresh = state.changed(signals)
    if not fresh:
        return 0
    ok = notifier.send_message(format_signal_message(fresh))
    return len(fresh) if ok else 0
