"""Rencana eksekusi (entry plan) per kandidat."""

from __future__ import annotations

from typing import Any

from .param import (
    BREAKOUT_NEAR_HIGH_PCT,
    RSI_HEALTHY_LOW,
    STOP_MIN_DISTANCE,
    STOP_VOL_MULT,
    TARGET_RR,
    _b,
    _f,
)


def entry_plan(
    *,
    close: float | None,
    signal: str | None,
    trend_up: bool | None,
    rsi: float | None,
    bb_lower: float | None,
    dist_52w_pct: float | None,
    vol_ratio: float | None,
    vol_daily: float | None,
    range_low_20: float | None,
) -> dict[str, Any] | None:
    """Level eksekusi untuk satu kandidat: zona entry, stop, target, R/R (pure).

    Tiga gaya masuk, dipilih dari data (bukan asumsi):

    - ``pullback``  : tren menengah masih naik, tapi harga sedang di/di bawah
      band bawah atau RSI di bawah netral — masuk di area diskon dalam tren.
    - ``breakout``  : harga dekat puncak 52-minggu (<= 3%) dan volume
      mengembang (>= 1,5x rata-rata 20 sesi) — masuk saat level ditembus.
    - ``trend``     : tren naik tanpa kondisi khusus di atas — referensi entry
      adalah close terakhir.

    Stop: low 20 sesi terakhir bila level itu masuk akal (di bawah close dan
    tidak lebih jauh dari 25%), kalau tidak ``close * (1 - max(5%, 3 x vol)``.
    Target = ``entry_ref + 2 x (entry_ref - stop)`` (R/R 2,0).

    Returns ``{setup, entry_low, entry_high, entry_ref, stop, target, rr,
    note}`` atau ``None`` bila level tidak bisa dihitung (tanpa close / stop
    tidak valid) — tanpa level pembatalan, kandidat tidak boleh ditampilkan.
    """
    c = _f(close)
    if c is None or c <= 0:
        return None

    bb = _f(bb_lower)
    vol_d = _f(vol_daily)
    rng_low = _f(range_low_20)
    dist = _f(dist_52w_pct)
    vr = _f(vol_ratio)
    r = _f(rsi)
    up = _b(trend_up)

    pullback = up and (
        (bb is not None and c <= bb * 1.02) or (r is not None and r <= RSI_HEALTHY_LOW)
    )
    breakout = dist is not None and dist >= BREAKOUT_NEAR_HIGH_PCT and vr is not None and vr >= 1.5

    if pullback:
        setup = "pullback"
        low = bb if bb is not None and 0 < bb <= c else c * 0.98
        entry_low, entry_high = round(low, 2), round(c, 2)
        entry_ref = round((entry_low + entry_high) / 2.0, 2)
        note = (
            "Tren masih naik tapi harga sedang di area diskon — masuk bertahap "
            f"di area Rp {entry_low:,.0f}-{entry_high:,.0f}."
        )
    elif breakout:
        setup = "breakout"
        entry_low, entry_high = round(c, 2), round(c * 1.01, 2)
        entry_ref = round(c, 2)
        note = (
            "Harga menempel puncak 52-minggu dengan volume mengembang — masuk "
            f"saat level Rp {entry_ref:,.0f} ditembus, jangan kejar di atasnya."
        )
    elif up:
        setup = "trend"
        entry_low, entry_high = round(c * 0.99, 2), round(c, 2)
        entry_ref = round(c, 2)
        note = f"Tren naik tanpa kondisi khusus — referensi entry Rp {entry_ref:,.0f}."
    else:
        # Bukan tren naik & bukan setup khusus: tetap boleh dikandidatkan bila
        # skornya kuat (mis. pola akumulasi), tapi gaya masuknya netral.
        setup = "netral"
        entry_low, entry_high = round(c * 0.98, 2), round(c, 2)
        entry_ref = round(c, 2)
        note = (
            "Belum ada konfirmasi tren — entry referensi "
            f"Rp {entry_ref:,.0f}, tunggu konfirmasi arah."
        )

    # --- stop: struktural dulu, volatilitas sebagai cadangan ---
    stop: float | None = None
    if rng_low is not None and 0 < rng_low < c and (c - rng_low) / c <= 0.25:
        stop = rng_low
    else:
        dist_frac = STOP_MIN_DISTANCE
        if vol_d is not None and vol_d > 0:
            dist_frac = max(STOP_MIN_DISTANCE, STOP_VOL_MULT * vol_d)
        if dist_frac >= 0.5:  # volatilitas ekstrem -> level tak bermakna
            return None
        stop = c * (1.0 - dist_frac)

    stop = round(float(stop), 2)
    if stop >= entry_ref or stop <= 0:
        return None

    target = round(entry_ref + TARGET_RR * (entry_ref - stop), 2)
    rr = round((target - entry_ref) / (entry_ref - stop), 2)
    return {
        "setup": setup,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "entry_ref": entry_ref,
        "stop": stop,
        "target": target,
        "rr": rr,
        "note": note,
    }
