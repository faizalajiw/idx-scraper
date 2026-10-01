"""State latch untuk alert jejak smart money (anti-spam).

Verdict jejak smart money per emiten hanya berubah setelah data EOD baru
masuk (jendela 10 sesi digeser 1 hari). Alert dikirim **sekali per
transisi**, persis seperti ``SignalState`` di :mod:`notify` dan
``RuleState`` di :mod:`alert_rules`: setelah sebuah emiten ter-alert, ia
diam sampai verdictnya berubah lagi.

Aturan "berubah" yang dianggap bermakna (yang memicu alert):
- dari netral/kosong -> akumulasi  (pemain besar mulai masuk)
- dari netral/kosong -> distribusi (pemain besar mulai keluar)
- akumulasi <-> distribusi        (balik arah)
- netral -> netral / belum punya data -> tidak di-alert (bukan berita).

Nilai yang disimpan per kode adalah ``side`` terakhir yang **non-netral**;
emiten netral disimpan sebagai ``""`` supaya kita tahu "pernah dihitung".
State dipersist ke JSON (default ``data/smart_money_state.json``) agar
restart worker tidak mengirim ulang.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .smart_money import (
    VERDICT_ACCUMULATION,
    VERDICT_DISTRIBUTION,
    VERDICT_NEUTRAL,
)

#: Sisi yang "bermakna" (layak di-alert). Netral & kosong tidak.
_NON_NEUTRAL = {VERDICT_ACCUMULATION, VERDICT_DISTRIBUTION}


@dataclass(frozen=True)
class SmartMoneyEvent:
    """Satu transisi verdict yang layak dikirim ke Telegram."""

    code: str
    name: str | None
    side: str
    net_sum_idr: float | None
    netval_pct: float | None
    streak: int | None
    previous_side: str | None  # None = belum pernah dihitung / baru
    date: str | None
    verdict: dict[str, Any]


class SmartMoneyState:
    """Latch per-emiten untuk jejak smart money.

    ``evaluate(verdicts)`` diberi daftar ``{code, name, verdict}`` (sisi
    non-netral ATAU netral), membandingkan dengan state terakhir, dan
    mengembalikan daftar :class:`SmartMoneyEvent` untuk emiten yang
    verdictnya berubah bermakna, lalu persist state baru.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(
            path or os.getenv("IDX_SMART_MONEY_STATE") or self._default_path()
        )
        self._data: dict[str, str] = {}
        self._load()

    @staticmethod
    def _default_path() -> Path:
        return Path(__file__).resolve().parents[2] / "data" / "smart_money_state.json"

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = {str(k): str(v) for k, v in raw.items()}
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            self._data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def _side_of(verdict: dict[str, Any]) -> str:
        if verdict.get("insufficient"):
            return VERDICT_NEUTRAL
        return str(verdict.get("side", VERDICT_NEUTRAL))

    def evaluate(self, verdicts: list[dict[str, Any]]) -> list[SmartMoneyEvent]:
        """Saring emiten dengan transisi bermakna, lalu persist state baru.

        ``verdicts``: daftar ``{code, name, verdict}`` — ``verdict`` hasil
        :func:`smart_money.verdict`.

        **Baseline dulu, alert belakangan**: emiten yang BARU PERTAMA KALI
        dilihat hanya di-seed ke state (tidak mengirim event) — kalau tidak,
        run pertama akan membanjiri Telegram dengan semua emiten non-netral
        di watchlist sekaligus. Alert hanya dikirim saat verdict berubah
        DARI state yang sudah tercatat:

        - netral -> akumulasi/distribusi  (pemain besar mulai gerak)
        - akumulasi <-> distribusi        (balik arah)

        Emiten netral/kosong tidak mengirim event tapi tetap dicatat (biar
        perubahan berikutnya terdeteksi). Mengembalikan daftar event.
        """
        events: list[SmartMoneyEvent] = []
        changed = False
        for item in verdicts:
            code = str(item.get("code", "")).upper()
            if not code:
                continue
            v = item.get("verdict") or {}
            side = self._side_of(v)
            prev_raw = self._data.get(code)  # None = belum pernah dilihat
            first_time = prev_raw is None
            meaningful_now = side in _NON_NEUTRAL
            prev_side = prev_raw if prev_raw in _NON_NEUTRAL else None

            # Baseline: run pertama untuk kode ini -> catat, jangan kirim.
            if not first_time and meaningful_now and prev_side != side:
                events.append(
                    SmartMoneyEvent(
                        code=code,
                        name=item.get("name"),
                        side=side,
                        net_sum_idr=v.get("net_sum_idr"),
                        netval_pct=v.get("netval_pct"),
                        streak=v.get("streak"),
                        previous_side=prev_side,
                        date=v.get("date"),
                        verdict=v,
                    )
                )

            # State baru = sisi non-netral sekarang, atau "" jika netral/kosong.
            new_state = side if meaningful_now else ""
            if prev_raw != new_state:
                self._data[code] = new_state
                changed = True

        if changed:
            self.save()
        return events
