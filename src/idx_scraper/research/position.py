"""Rencana posisi (lapis 3): dari persen modal ke rupiah, lot, dan rugi maksimum.

Fungsi murni: tanpa DB dan tanpa cache. Dipakai papan kandidat
(``api/analytics``) sekarang dan jurnal posisi (P6) nanti, supaya papan dan
halaman emiten tidak pernah punya dua definisi ukuran posisi.

Sumber kebenaran aturan: ``docs/superpowers/specs/2026-10-18-spec-lapis-3-rencana-posisi-design.md``.
Porsi dasar tetap milik :func:`recommend.position_size_pct` (risiko dibagi jarak
stop); modul ini hanya menambahkan plafon, pembulatan lot, dan bahasanya.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import pandas as pd

from .recommend import DEFAULT_RISK_PCT, MAX_POSITION_PCT, position_size_pct

#: Satu lot Bursa Efek Indonesia = 100 lembar. Lot selalu dibulatkan ke bawah:
#: rencana yang tidak bisa dibeli bukan rencana.
ONE_LOT = 100

#: Plafon likuiditas: 2% dari nilai transaksi harian. Sengaja angka yang sama
#: dengan ``api.simulation.DEFAULT_COSTS["max_adv_pct"]`` (model biaya backtest),
#: dijaga tes ``test_adv_pct_sama_dengan_model_biaya_backtest``.
ADV_PCT = 0.02

#: Pasar VOLATILE memotong porsi separuh (spec bagian 4).
VOLATILE_CUT = 0.5

#: Jumlah sesi terakhir untuk nilai transaksi harian yang "biasa".
TYPICAL_SESSIONS = 5

#: Urutan pemeriksaan plafon saat melaporkan yang benar-benar memotong.
CAP_ORDER = ("per_name", "liquidity", "regime", "portfolio")


@dataclass(frozen=True)
class PositionLimits:
    """Batas yang boleh memotong rencana. Angka spec bagian 4, bukan tebakan."""

    max_position_pct: float = MAX_POSITION_PCT
    adv_pct: float = ADV_PCT
    max_open_positions: int = 6
    max_exposure_pct: float = 80.0
    max_concurrent_risk_pct: float = 6.0
    volatile_cut: float = VOLATILE_CUT
    typical_sessions: int = TYPICAL_SESSIONS


DEFAULT_LIMITS = PositionLimits()


def _num(value: Any) -> float | None:
    """Angka finit atau None. Bool ditolak (True bukan 1 di konteks ini)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _int_of(value: Any) -> int | None:
    n = _num(value)
    return None if n is None else int(n)


def _concurrent_risk(risk_pct: float, open_positions: int | None) -> float | None:
    """Risiko serentak yang diturunkan: posisi terbuka x risiko per transaksi."""
    if open_positions is None:
        return None
    return round(risk_pct * int(open_positions), 1)


def typical_day_value(
    metrics: pd.DataFrame,
    code: str,
    sessions: int = TYPICAL_SESSIONS,
) -> float | None:
    """Median nilai transaksi ``sessions`` sesi terakhir satu emiten.

    None bila emiten tidak ada di panel atau riwayat nilainya kosong: pemanggil
    memperlakukan None sebagai "plafon likuiditas tidak diketahui", bukan nol.
    """
    if metrics is None or not isinstance(metrics, pd.DataFrame) or metrics.empty:
        return None
    if "code" not in metrics.columns or "value" not in metrics.columns:
        return None
    rows = metrics[metrics["code"].astype(str).str.upper() == str(code).upper()]
    rows = rows.dropna(subset=["value"])
    if rows.empty:
        return None
    if "date" in rows.columns:
        rows = rows.sort_values("date")
    tail = rows["value"].tail(max(1, int(sessions)))
    if tail.empty:
        return None
    return float(tail.median())


def _empty_plan(limits: PositionLimits, note: str, concurrent: float | None) -> dict[str, Any]:
    """Kerangka kembalian saat ukuran tidak bisa dihitung (stop tidak valid)."""
    return {
        "pct": None,
        "pct_raw": None,
        "binding_cap": None,
        "caps": {
            "per_name": float(limits.max_position_pct),
            "liquidity": None,
            "regime": None,
            "portfolio": None,
        },
        "concurrent_risk_pct": concurrent,
        "rupiah": None,
        "lots": None,
        "risk_rupiah": None,
        "note": note,
    }


def position_plan(
    *,
    entry: float | None,
    stop: float | None,
    modal: float | None = None,
    risk_pct: float | None = None,
    typical_day_value: float | None = None,
    vol_state: str | None = None,
    open_positions: int | None = None,
    exposure_pct: float | None = None,
    limits: PositionLimits = DEFAULT_LIMITS,
) -> dict[str, Any]:
    """Rencana posisi satu nama: persen modal, rupiah, lot, dan rugi di stop.

    Parameter ``typical_day_value`` sengaja bernama sama dengan fungsi modul ini
    (spec memakai nama itu): di dalam badan fungsi ini yang dimaksud selalu nilai
    yang sudah dihitung pemanggil, bukan fungsinya.
    """
    risk = _num(risk_pct)
    if risk is None or risk <= 0:
        risk = float(DEFAULT_RISK_PCT)

    m = _num(modal)
    if m is not None and m <= 0:
        m = None

    open_n = _int_of(open_positions)
    expo = _num(exposure_pct)

    pct_raw = position_size_pct(entry=entry, stop=stop, risk_pct=risk, max_pct=math.inf)
    if pct_raw is None:
        return _empty_plan(
            limits,
            "Stop tidak valid: rencana ukuran tidak bisa dihitung.",
            _concurrent_risk(risk, open_n),
        )

    caps: dict[str, float | None] = {"per_name": float(limits.max_position_pct)}

    adv = _num(typical_day_value)
    if m is None or adv is None or adv <= 0:
        caps["liquidity"] = None
    else:
        caps["liquidity"] = round(limits.adv_pct * adv / m * 100, 1)

    if str(vol_state or "").strip().upper() == "VOLATILE":
        caps["regime"] = round(pct_raw * limits.volatile_cut, 1)
    else:
        caps["regime"] = None

    count_full = open_n is not None and open_n >= limits.max_open_positions
    room: float | None = None
    if expo is not None:
        room = max(0.0, float(limits.max_exposure_pct) - expo)
    if count_full:
        room = 0.0
    caps["portfolio"] = round(room, 1) if room is not None else None

    pct = round(min([pct_raw] + [v for v in caps.values() if v is not None]), 1)

    binding: str | None = None
    # Yang benar-benar memotong = plafon yang angkanya sama dengan hasil akhir.
    # Dua plafon bisa sama-sama memotong (mis. VOLATILE lalu buku penuh): yang
    # dipanggil harus yang menyentuh angka akhir, bukan yang kebetulan pertama
    # lebih kecil dari porsi mentah.
    for name in CAP_ORDER:
        value = caps.get(name)
        if value is not None and value < pct_raw and round(value, 1) == pct:
            binding = name
            break

    lots: int | None = None
    rupiah: float | None = None
    risk_rupiah: float | None = None
    if m is not None:
        e = _num(entry)
        s = _num(stop)
        if e is not None and s is not None and e > 0:
            budget = m * pct / 100
            lots = int(budget // (ONE_LOT * e))
            rupiah = round(lots * ONE_LOT * e, 2)
            risk_rupiah = round(lots * ONE_LOT * (e - s), 2)

    notes: list[str] = []
    if binding == "liquidity":
        notes.append("Dipotong plafon likuiditas (2% nilai transaksi harian).")
    elif binding == "regime":
        notes.append("Dipotong plafon rezim: pasar VOLATILE, porsi separuh.")
    elif binding == "per_name":
        notes.append("Dipotong plafon per nama.")
    elif binding == "portfolio":
        if count_full:
            notes.append(
                f"Sudah penuh: {open_n} posisi terbuka dari batas "
                f"{limits.max_open_positions}."
            )
        else:
            notes.append("Dipotong plafon eksposur: ruang sisa tidak cukup.")
    else:
        notes.append("Tidak ada plafon yang memotong.")

    if m is None:
        notes.append("Tanpa modal: porsi hanya dalam persen.")
    elif lots == 0 and pct > 0:
        # Porsi nol sudah dijelaskan plafon yang memotongnya; menyebut modal di
        # situ menuduh hal yang salah.
        notes.append("Modal kurang untuk 1 lot.")

    if open_n is None and expo is None:
        notes.append("Posisi terbuka dan eksposur belum diisi, plafon portofolio dilewati.")
    elif open_n is None:
        notes.append("Posisi terbuka belum diisi, plafon portofolio dilewati.")
    elif expo is None:
        notes.append("Eksposur belum diisi, plafon portofolio dilewati.")

    return {
        "pct": pct,
        "pct_raw": pct_raw,
        "binding_cap": binding,
        "caps": caps,
        "concurrent_risk_pct": _concurrent_risk(risk, open_n),
        "rupiah": rupiah,
        "lots": lots,
        "risk_rupiah": risk_rupiah,
        "note": " ".join(notes),
    }
