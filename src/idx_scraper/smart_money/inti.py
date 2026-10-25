"""Smart money — dasar bersama: ambang kalibrasi, helper baris, verdict,
dan level konsolidasi.

Semua fungsi murni (tanpa akses DB) mengikuti pola ``foreign_flow`` dan
``broker_flow``: menerima iterable baris, mengembalikan dict/list.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

# ------------------------------------------------------------------ konstanta

# Ambang dari kalibrasi data (lihat docstring modul).
FLAT_RANGE_MAX_PCT = 10.0  # range 10 sesi di bawah ini = harga "tenang"
RISING_MIN_PCT = 8.0  # perubahan 10 sesi di atas ini = "naik jelas"
VOL_SPIKE_RATIO = 2.0  # volume hari ini >= 2x rata20 = lonjakan
MEANINGFUL_NETVAL_PCT = 5.0  # net asing jml / nilai jml >= 5% = bermakna
LARGE_NETVAL_PCT = 16.0  # >= 16% = aliran besar (p90)
INITIATION_NET_PCT = 2.0  # inisiasi: net hari ini >= 2% nilai hari itu

PRICE_WINDOW = 10  # jendela "terakhir" utk verdict & pola (sesi)
VOLUME_BASELINE_WINDOW = 20  # baseline volume utk rasio lonjakan
RANGE_WINDOW = 10  # jendela range utk deteksi "flat"
MIN_WINDOW_DAYS = 5  # min. hari ber-net di jendela agar boleh verdict

VERDICT_ACCUMULATION = "akumulasi"
VERDICT_DISTRIBUTION = "distribusi"
VERDICT_NEUTRAL = "netral"

# Lantai likuiditas radar: jumlah nilai transaksi di jendela harus >= ini
# (Rp 500 Jt — di atas p10 nilai-5-sesi ≈ Rp 180 Jt, jadi memotong emiten
# yang paling sulit diperdagangkan tanpa membuang emiten kecil yang likuid).
RADAR_MIN_WINDOW_VALUE = 5.0e8

# ------------------------------------------------------------------ helper


def _f(value: Any) -> float | None:
    """Ambil nilai float; None/NaN/inf/invalid -> None (bukan 0)."""
    if value is None:
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _clean_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Normalisasi baris + urut tanggal menaik (defensif; endpoint sudah urut).

    ``date`` boleh str ISO / ``date`` / ``Timestamp`` — dinormalkan ke str ISO
    supaya sort lexicographic selalu benar (YYYY-MM-DD).
    """
    out: list[dict[str, Any]] = []
    for r in rows:
        d = r.get("date")
        if d is None:
            continue
        dstr = str(d)
        if len(dstr) >= 10:
            dstr = dstr[:10]
        out.append(
            {
                "date": dstr,
                "net_idr": _f(r.get("net_idr")),
                "value": _f(r.get("value")),
                "close": _f(r.get("close")),
                "high": _f(r.get("high")),
                "low": _f(r.get("low")),
                "volume": _f(r.get("volume")),
            }
        )
    out.sort(key=lambda x: x["date"])
    return out


def _streak(rows: list[dict[str, Any]]) -> tuple[int, str, str | None]:
    """Streak arah net asing terakhir (hari berturut-turut, sampai hari paling
    baru). Returns ``(streak, side, start_date)``; side =
    ``"net_buy"``/``"net_sell"``/``"flat"``; ``start_date`` = tanggal sesi
    PERTAMA streak yang sedang berjalan (None bila streak 0) — ini yang
    menjawab "sejak kapan" tanpa harus menghitung sendiri di UI. Hari net 0
    atau tanpa data memutus streak — konsisten dengan
    ``foreign_flow.daily_flow``.
    """
    streak = 0
    side = "flat"
    start_date: str | None = None
    for r in reversed(rows):
        net = r["net_idr"]
        if net is None:
            break
        s = 1 if net > 0 else (-1 if net < 0 else 0)
        if s == 0:
            break
        if side == "flat":
            side = "net_buy" if s > 0 else "net_sell"
        elif (s > 0 and side == "net_sell") or (s < 0 and side == "net_buy"):
            break
        streak += 1
        # baris ditelusuri mundur, jadi nilai terakhir = hari paling awal
        start_date = r["date"]
    return streak, side, start_date


def _window_stats(rows: list[dict[str, Any]], days: int) -> dict[str, Any]:
    """Agregat N sesi terakhir (sesi bursa, bukan hari kalender).

    ``netval_pct`` = jumlah net asing / jumlah nilai transaksi * 100 — porsi
    nilai transaksi yang benar-benar dibeli/dijual asing di jendela. None jika
    tidak ada nilai (tak bisa bagi). Hari tanpa net tidak dianggap net 0.
    """
    window = rows[-days:]
    net_sum = sum(r["net_idr"] for r in window if r["net_idr"] is not None)
    value_sum = sum(r["value"] for r in window if r["value"] is not None)
    n_net = sum(1 for r in window if r["net_idr"] is not None)
    netval_pct = (net_sum / value_sum * 100.0) if value_sum and value_sum > 0 else None
    return {
        "net_sum_idr": net_sum,
        "value_sum": value_sum if value_sum > 0 else None,
        "netval_pct": netval_pct,
        "n_net_days": n_net,
    }


# ------------------------------------------------------------------ verdict


def verdict(rows: Iterable[Mapping[str, Any]], days: int = PRICE_WINDOW) -> dict[str, Any]:
    """Verdict jejak smart money satu emiten — satu jawaban plain-language.

    Rules (terdokumentasi, tanpa angka magic):

    - ``side``: ``netval_pct`` >= +5 -> ``akumulasi``; <= -5 -> ``distribusi``;
      selain itu ``netral``. (5% = p75 |netval10| data nyata — hanya 1 dari 4
      emiten-sesi yang sampai di situ.)
    - ``strength``: |netval_pct| >= 16 -> ``besar`` (p90); >= 5 -> ``menengah``;
      netral -> None.
    - ``insufficient``: True jika < :data:`MIN_WINDOW_DAYS` hari ber-net di
      jendela — UI wajib menampilkan "data belum cukup", bukan netral palsu.

    Returns ``side, strength, streak, streak_side, streak_start_date,
    net_sum_idr, netval_pct, n_net_days, date, insufficient``. Tidak pernah
    raise — emiten tanpa data menghasilkan struktur kosong yang aman
    di-render.

    ``days``: lebar jendela agregasi (default 10). Requirement "hari
    bernilai" berskala dengan jendela (``min(5, days)``) supaya jendela pendek
    (mis. radar 2 hari) tidak selamanya ``insufficient``.
    """
    cleaned = _clean_rows(rows)
    streak, streak_side, streak_start = _streak(cleaned)
    stats = _window_stats(cleaned, days)
    netval = stats["netval_pct"]
    min_days = min(MIN_WINDOW_DAYS, days)
    insufficient = stats["n_net_days"] < min_days or netval is None

    if insufficient:
        side = VERDICT_NEUTRAL
        strength = None
    elif netval >= MEANINGFUL_NETVAL_PCT:
        side = VERDICT_ACCUMULATION
        strength = "besar" if netval >= LARGE_NETVAL_PCT else "menengah"
    elif netval <= -MEANINGFUL_NETVAL_PCT:
        side = VERDICT_DISTRIBUTION
        strength = "besar" if netval <= -LARGE_NETVAL_PCT else "menengah"
    else:
        side = VERDICT_NEUTRAL
        strength = None

    return {
        "side": side,
        "strength": strength,
        "streak": streak,
        "streak_side": streak_side,
        "streak_start_date": streak_start if streak > 0 else None,
        "net_sum_idr": stats["net_sum_idr"] or None,
        "netval_pct": round(netval, 2) if netval is not None else None,
        "n_net_days": stats["n_net_days"],
        "date": cleaned[-1]["date"] if cleaned else None,
        "insufficient": insufficient,
    }


# ------------------------------------------------------------------ level

RANGE_LOOKBACK = 20  # jendela konsolidasi untuk level pembatalan


def consolidation_range(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Rentang konsolidasi N sesi terakhir — level pembatalan untuk ikut.

    Membalas pertanyaan "kalau mau ikut, patokannya di mana?":

    - ``low``/``high``: low & high N sesi terakhir (window default 20).
    - ``support``/``resistance``: sama dengan low/high (label yang bisa
      dipegang user awam).
    - ``breakout_above``: ``high`` — penembusan di atasnya = konfirmasi.
    - ``invalidation_below``: ``low`` — jebol di bawahnya = pembatalan.
    - ``range_pct``: lebar rentang dalam % harga.

    Returns None jika < 5 sesi ber-harga (histori terlalu pendek — UI tidak
    boleh menampilkan level yang tidak bisa dihitung).
    """
    cleaned = _clean_rows(rows)
    window = [
        r
        for r in cleaned[-RANGE_LOOKBACK:]
        if r["close"] is not None and r["low"] is not None and r["high"] is not None
    ]
    if len(window) < 5:
        return None
    low = min(r["low"] for r in window)
    high = max(r["high"] for r in window)
    last_close = window[-1]["close"]
    if last_close is None or last_close <= 0:
        return None
    return {
        "lookback": len(window),
        "low": round(low, 2),
        "high": round(high, 2),
        "support": round(low, 2),
        "resistance": round(high, 2),
        "range_pct": round((high - low) / last_close * 100.0, 2),
    }
