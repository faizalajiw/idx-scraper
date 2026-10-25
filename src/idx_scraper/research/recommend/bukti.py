"""Bukti smart money (sintesis jejak asing & pola)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .param import _f, pattern_label


def smart_money_evidence(
    *,
    buy_patterns: Iterable[str],
    sell_patterns: Iterable[str],
    histories: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Alasan + peringatan dari pola jejak smart money (pure) — **tanpa poin**.

    ``histories`` = peta ``pattern_id -> ringkasan track record`` (bentuk
    keluaran ``smart_money.pattern_history``: ``confidence``, ``horizon_days``,
    ``aligned_hit_rate``, ``reliable``, ``edge_pct``).

    Pola **tidak** menggeser skor di sini: bobotnya, kalau ada, datang dari
    walk-forward lewat bendera komponen (``PATTERN_COMPONENTS``). Yang dilakukan
    fungsi ini adalah menyatakan buktinya dalam kata — termasuk jujur saat
    catatan historisnya masih terlalu tipis untuk diklaim. Pola sisi jual jadi
    peringatan: sinyal jual yang menyala bukan kabar baik untuk kandidat beli.

    Returns ``{reasons, warnings, best_confidence}``.
    """
    hist = histories or {}
    reasons: list[str] = []
    warnings: list[str] = []
    best_conf: str | None = None

    for pid in buy_patterns:
        h = hist.get(str(pid)) or {}
        conf = str(h.get("confidence") or "lemah")
        reliable = bool(h.get("reliable", False))
        label = str(h.get("label") or pattern_label(str(pid)))
        if not reliable:
            reasons.append(
                f"Pola {label} menyala, tapi catatan historisnya masih terlalu "
                "kecil untuk diklaim — tidak menambah skor."
            )
            continue
        rate = _f(h.get("aligned_hit_rate"))
        days = h.get("horizon_days")
        edge = _f(h.get("edge_pct"))
        detail = []
        if rate is not None:
            detail.append(f"{rate * 100:.0f}% searah")
        if days:
            detail.append(f"dalam {days} hari bursa")
        if edge is not None:
            detail.append(f"alpha {edge:+.2f}%")
        tail = f" ({', '.join(detail)})" if detail else ""
        reasons.append(
            f"Pola {label} — keyakinan {conf}{tail}. Bukti ini ditampilkan, "
            "tapi belum ikut menimbang skor (bobotnya belum lolos walk-forward)."
        )
        if best_conf is None or _conf_rank(conf) < _conf_rank(best_conf):
            best_conf = conf

    for pid in sell_patterns:
        h = hist.get(str(pid)) or {}
        label = str(h.get("label") or pattern_label(str(pid)))
        warnings.append(f"Pola {label} (sisi jual) juga menyala — sinyal beli jadi lemah.")

    return {
        "reasons": reasons,
        "warnings": warnings,
        "best_confidence": best_conf,
    }


def _conf_rank(conf: str) -> int:
    return {"tinggi": 0, "sedang": 1, "lemah": 2}.get(conf, 3)
