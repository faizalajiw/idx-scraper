"""Composite factor score — faktor terbukti dari IC analysis masuk hold-check.

Sumber bobot: IC analysis IDX (lihat ``idx_scraper.research.ic``, hasil run
2026-09-26, horizon 5-10 hari bursa, fill T+1, t-stat Newey-West):

    vol_21d       IC -0.074 (k=5) / -0.072 (k=10), t=-2.1  -> terkuat
    turnover_21d  IC -0.052 / -0.065,             t=-2.9
    dist_52w_high IC +0.050 / +0.043,             t=+1.2

Bobot proporsional terhadap |IC| ternormalisasi (jumlah = 1):

    vol 0.42 / turnover 0.29 / dist_52w 0.29

Semantik: setiap faktor dikonversi ke **percentile cross-sectional melawan
seluruh pasar** (bukan cuma watchlist — ranking antar 3-5 saham tidak
bermakna), lalu diarahkan sesuai tanda IC:

- volatilitas tinggi  -> kontribusi NEGATIF (low-vol anomaly IDX)
- turnover tinggi     -> kontribusi NEGATIF (kuintil terlikuid underperform)
- dekat puncak 52w    -> kontribusi POSITIF (momentum posisi)

Skor faktor F e [-1, +1], dipetakan ke penyesuaian skor hold-check e
[-MAX_ADJUSTMENT, +MAX_ADJUSTMENT] poin. Teknikal tetap faktor dominan;
lapisan ini hanya menggeser verdict saat faktor ekstrem.
"""

from __future__ import annotations

import time
from typing import Any

# --- bobot default (dari IC analysis 2026-09-26, lihat docstring modul) ---
# Di-production bobot dibaca dari research.factor_ic_history (run IC bulanan
# terbaru, lihat ic_history.py); konstanta ini fallback kalau tabel kosong.
FACTOR_WEIGHTS = {"vol": 0.42, "turnover": 0.29, "dist_52w": 0.29}
MAX_ADJUSTMENT = 10.0
MIN_MARKET_HISTORY = 60  # emiten dengan histori lebih pendek di-skip dari ranking

# Ambang percentile untuk menarik alasan ke UI (di luar ini tidak coment).
REASON_THRESHOLD = 0.70

_MARKET_RANKS_SQL = """
with px as (
    select code, trade_date, adj_close
    from research.prices_asof_adj(now())
    where trade_date > current_date - interval '18 months'
),
r as (
    select code, adj_close,
           adj_close / lag(adj_close) over w - 1.0 as ret,
           row_number() over w_desc as rn,
           count(*) over (partition by code) as n_rows
    from px
    window w as (partition by code order by trade_date),
           w_desc as (partition by code order by trade_date desc)
),
agg_px as (
    select code,
           max(n_rows) as n_rows,
           stddev_samp(ret) filter (where rn <= 22 and ret is not null) as vol_21d,
           max(adj_close) filter (where rn <= 252) as hi_252,
           max(adj_close) filter (where rn = 1) as last_close
    from r
    group by code
),
val as (
    select code,
           avg(value) filter (where rn <= 21) as turn_21d
    from (
        select code, value,
               row_number() over (partition by code order by trade_date desc) as rn
        from research.prices_asof(now())
        where value is not null and value > 0
          and trade_date > current_date - interval '18 months'
    ) v
    group by code
)
select a.code,
       a.n_rows,
       a.vol_21d,
       ln(v.turn_21d)      as turn_21d,
       a.last_close / a.hi_252 - 1.0 as dist_52w
from agg_px a
left join val v using (code)
where a.n_rows >= %s
  and a.vol_21d is not null
  and a.hi_252 is not null and a.hi_252 > 0
"""

# Cache in-process: query pasar butuh ~1s dan faktor harian tidak berubah
# sepanjang hari bursa. Key = TTL; di-reset otomatis lewat TTL.
_CACHE: dict[str, Any] = {"at": 0.0, "data": {}}
_WEIGHTS_CACHE: dict[str, Any] = {"at": 0.0, "data": None}
_CACHE_TTL_SECONDS = 1800.0


def ranks_from_rows(rows: list[Any]) -> dict[str, dict[str, float]]:
    """Normalisasi baris SQL -> {code: {vol_pct, turnover_pct, dist_52w_pct}}.

    Pure & testable: menerima baris dengan akses dict ATAU index
    (psycopg Row mendukung keduanya). Percentile = share emiten dengan nilai
    <= emiten itu (rank pct, 0..1).
    """
    def _get(r: Any, key: str, idx: int) -> Any:
        return r[key] if isinstance(r, dict) else r[idx]

    parsed: dict[str, tuple[float, float, float]] = {}
    for r in rows:
        code = _get(r, "code", 0)
        vol = _get(r, "vol_21d", 2)
        turn = _get(r, "turn_21d", 3)
        dist = _get(r, "dist_52w", 4)
        if vol is None or turn is None or dist is None:
            continue
        parsed[str(code).upper()] = (float(vol), float(turn), float(dist))

    if not parsed:
        return {}

    codes = sorted(parsed)
    out: dict[str, dict[str, float]] = {}
    for i, key in enumerate(("vol", "turnover", "dist_52w")):
        vals = sorted((parsed[c][i], c) for c in codes)
        pct_by_code = {
            c: (rank + 1) / len(vals) for rank, (_, c) in enumerate(vals)
        }
        for c in codes:
            out.setdefault(c, {})[f"{key}_pct"] = pct_by_code[c]
    return out


def market_factor_ranks(
    cur: Any, use_cache: bool = True
) -> dict[str, dict[str, float]]:
    """Percentile faktor seluruh emiten (butuh cursor dengan set time zone WIB).

    Hasil di-cache 30 menit — query menyapu ~18 bulan data seluruh pasar dan
    faktor harian tidak berubah intraday.
    """
    now = time.monotonic()
    if use_cache and _CACHE["data"] and (now - _CACHE["at"]) < _CACHE_TTL_SECONDS:
        return _CACHE["data"]

    cur.execute(_MARKET_RANKS_SQL, (MIN_MARKET_HISTORY,))
    ranks = ranks_from_rows(cur.fetchall())
    if ranks:
        _CACHE["at"] = now
        _CACHE["data"] = ranks
    return ranks


def latest_weights(cur: Any) -> dict[str, float]:
    """Bobot komposit dari run IC terbaru (research.factor_ic_history).

    Kosong / query gagal -> fallback konstanta FACTOR_WEIGHTS. Cache 30 menit
    bersama cache ranking (siklus update sama-sama harian).
    """
    now = time.monotonic()
    if use_cache_weights() and _WEIGHTS_CACHE["data"] is not None and (
        now - _WEIGHTS_CACHE["at"]
    ) < _CACHE_TTL_SECONDS:
        return _WEIGHTS_CACHE["data"]

    try:
        from .ic_history import weights_by_key

        weights = weights_by_key(cur)
    except Exception:
        weights = {}
    effective = weights if weights else dict(FACTOR_WEIGHTS)
    _WEIGHTS_CACHE["at"] = now
    _WEIGHTS_CACHE["data"] = effective
    return effective


def use_cache_weights() -> bool:
    """Hook sederhana untuk menonaktifkan cache bobot di test (env-driven)."""
    import os

    return os.getenv("IDX_COMPOSITE_NO_CACHE", "") == ""


def invalidate_caches() -> None:
    """Kosongkan cache ranking & bobot (dipakai test / job IC bulanan)."""
    _CACHE["at"] = 0.0
    _CACHE["data"] = {}
    _WEIGHTS_CACHE["at"] = 0.0
    _WEIGHTS_CACHE["data"] = None


def factor_adjustment(
    pcts: dict[str, float], weights: dict[str, float] | None = None
) -> tuple[float, list[str]]:
    """Map percentile -> (penyesuaian skor, alasan UI). Pure & testable.

    Penyesuaian = MAX_ADJUSTMENT * F, dengan
        F = -w_vol*(2*vol_pct-1) - w_turn*(2*turn_pct-1) + w_dist*(2*dist_pct-1)
    Tanda mengikuti arah IC (lihat docstring modul). ``weights`` = bobot dari
    run IC terbaru (key vol/turnover/dist_52w); None -> FACTOR_WEIGHTS.
    """
    w = weights if weights else FACTOR_WEIGHTS
    vol = pcts.get("vol_pct")
    turn = pcts.get("turnover_pct")
    dist = pcts.get("dist_52w_pct")
    if vol is None or turn is None or dist is None:
        return 0.0, []

    f = (
        -w.get("vol", 0.0) * (2.0 * vol - 1.0)
        - w.get("turnover", 0.0) * (2.0 * turn - 1.0)
        + w.get("dist_52w", 0.0) * (2.0 * dist - 1.0)
    )
    adj = MAX_ADJUSTMENT * f

    reasons: list[str] = []
    if vol >= REASON_THRESHOLD:
        reasons.append(
            f"Volatilitas 21h di persentil pasar {vol:.0%} — riwayat return "
            f"lebih lemah (IC -0.07)"
        )
    elif vol <= 1.0 - REASON_THRESHOLD:
        reasons.append(
            f"Volatilitas 21h rendah (persentil pasar {vol:.0%}) — profil "
            f"return historis lebih baik"
        )
    if turn >= REASON_THRESHOLD:
        reasons.append(
            f"Likuiditas sangat tinggi (persentil pasar {turn:.0%}) — kuintil "
            f"terlikuid historis underperform"
        )
    elif turn <= 1.0 - REASON_THRESHOLD:
        reasons.append(
            f"Likuiditas rendah (persentil pasar {turn:.0%}) — historis "
            f"outperform, tapi perhatikan biaya eksekusi"
        )
    if dist >= REASON_THRESHOLD:
        reasons.append(
            f"Dekat puncak 52-minggu (persentil {dist:.0%}) — momentum posisi "
            f"positif (IC +0.05)"
        )
    elif dist <= 1.0 - REASON_THRESHOLD:
        reasons.append(
            f"Jauh di bawah puncak 52-minggu (persentil {dist:.0%}) — momentum "
            f"posisi lemah"
        )
    return adj, reasons


def verdict_for(score: float) -> str:
    """Band verdict — satu sumber kebenaran untuk hold-check."""
    if score >= 75.0:
        return "STRONG HOLD"
    if score >= 55.0:
        return "HOLD"
    if score >= 35.0:
        return "TRIM"
    return "EXIT"


def apply_factor_layer(
    score: float,
    verdict: str,
    reasons: list[str],
    pcts: dict[str, float] | None,
    weights: dict[str, float] | None = None,
) -> tuple[float, str, list[str], float]:
    """Terapkan lapisan faktor IC ke satu emiten (pure, testable).

    Returns ``(new_score, new_verdict, new_reasons, adj)``. Tanpa percentile
    (emiten di luar coverage ranking) atau penyesuaian nol -> masukan
    dikembalikan apa adanya.
    """
    if not pcts:
        return score, verdict, reasons, 0.0
    adj, factor_reasons = factor_adjustment(pcts, weights)
    if not adj:
        return score, verdict, reasons, 0.0
    new_score = max(0.0, min(100.0, score + adj))
    new_reasons = [*reasons, *factor_reasons]
    return new_score, verdict_for(new_score), new_reasons, adj
