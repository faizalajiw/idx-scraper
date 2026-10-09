"""State latch untuk alert kandidat beli grade A (anti-spam).

Sama seperti ``SmartMoneyState`` dan ``RuleState``: alert dikirim **sekali per
transisi**, bukan tiap kali board dihitung ulang. Papan rekomendasi dihitung
tiap refresh pipeline, jadi tanpa latch Telegram akan menerima pesan yang sama
puluhan kali sehari.

Aturan "berubah" yang memicu alert:

- masuk **grade A** (``None``/``B``/``C`` -> ``A``) — kandidat dengan bukti
  terkuat, inilah yang layak dibunyikan;
- turun dari A ke bawah tidak dikirim (bukan berita), tapi **re-arm**: kode
  yang kembali masuk A nanti akan dibunyikan lagi.

**Baseline dulu**: run pertama untuk sebuah kode hanya menyimpan grade-nya
tanpa mengirim — kalau tidak, run pertama akan membanjiri Telegram dengan
seluruh grade A hari itu sekaligus. State dipersist ke JSON (default
``data/recommendation_state.json``) agar restart worker tidak mengirim ulang.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Grade yang layak di-alert. Hanya A — kalau semua grade dibunyikan, alertnya
#: berhenti bermakna.
ALERT_GRADE = "A"


@dataclass(frozen=True)
class RecommendationEvent:
    """Satu transisi grade ke A yang layak dikirim ke Telegram."""

    code: str
    name: str | None
    grade: str
    score: float | None
    previous_grade: str | None
    entry_ref: float | None
    stop: float | None
    target: float | None
    rr: float | None
    horizon_days: int | None
    date: str | None
    row: dict[str, Any]


class RecommendationState:
    """Latch per-emiten untuk kandidat beli (menyimpan grade terakhir)."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(
            path or os.getenv("IDX_RECOMMENDATION_STATE") or self._default_path()
        )
        self._data: dict[str, str] = {}
        self._load()

    @staticmethod
    def _default_path() -> Path:
        return Path(__file__).resolve().parents[2] / "data" / "recommendation_state.json"

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

    def evaluate(self, rows: list[dict[str, Any]]) -> list[RecommendationEvent]:
        """Saring kandidat yang BARU masuk grade A, lalu persist state baru.

        ``rows`` = daftar baris papan rekomendasi (``{code, name, grade, score,
        entry_ref, stop, target, rr, horizon_days, ...}``). Baris di bawah grade
        alert tetap dicatat sebagai re-arm (grade-nya disimpan) tanpa event.
        """
        events: list[RecommendationEvent] = []
        changed = False
        for item in rows:
            code = str(item.get("code", "")).upper()
            if not code:
                continue
            grade = str(item.get("grade") or "")
            prev_raw = self._data.get(code)  # None = belum pernah dilihat
            first_time = prev_raw is None

            if not first_time and grade == ALERT_GRADE and prev_raw != ALERT_GRADE:
                events.append(
                    RecommendationEvent(
                        code=code,
                        name=item.get("name"),
                        grade=grade,
                        score=item.get("score"),
                        previous_grade=prev_raw or None,
                        entry_ref=item.get("entry_ref"),
                        stop=item.get("stop"),
                        target=item.get("target"),
                        rr=item.get("rr"),
                        horizon_days=item.get("horizon_days"),
                        date=item.get("date"),
                        row=item,
                    )
                )

            if prev_raw != grade:
                self._data[code] = grade
                changed = True

        if changed:
            self.save()
        return events
