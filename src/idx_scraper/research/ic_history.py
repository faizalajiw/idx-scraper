"""Persistensi hasil IC analysis -> research.factor_ic_history.

- ``derive_weights``     : pilih faktor eligible (|IC|>=0.05 & |ICIR|>=0.5),
                           bobot proporsional |mean_ic|, jumlah = 1.
- ``save_ic_summary``    : upsert idempoten (run_date, factor, horizon).
- ``load_latest_weights``: bobot untuk composite; satu-satunya fungsi yang
                           menyentuh DB (menerima cursor -> mudah dites).
"""

from __future__ import annotations

import math
from typing import Any

ELIGIBLE_ABS_IC = 0.05
ELIGIBLE_ABS_ICIR = 0.5

# Horizon yang jadi acuan bobot composite (k=10: t-stat paling stabil di
# IC analysis 2026-09-26). Bisa dioverride lewat env.
WEIGHT_HORIZON = 10

# Faktor yang boleh jadi bagian composite (white-list, menjaga semantik tanda
# di composite.factor_adjustment: vol & turnover negatif, dist_52w positif).
COMPOSITE_FACTORS = {
    "vol_21d": ("vol", -1.0),
    "turnover_21d": ("turnover", -1.0),
    "dist_52w_high": ("dist_52w", +1.0),
}

_UPSERT_SQL = """
insert into research.factor_ic_history
    (run_date, factor, horizon, mean_ic, icir, t_stat, hit_rate, n_days,
     eligible, weight)
values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
on conflict (run_date, factor, horizon) do update set
    mean_ic = excluded.mean_ic,
    icir = excluded.icir,
    t_stat = excluded.t_stat,
    hit_rate = excluded.hit_rate,
    n_days = excluded.n_days,
    eligible = excluded.eligible,
    weight = excluded.weight,
    generated_at = now()
"""


def derive_weights(
    summary_rows: list[dict[str, Any]],
    horizon: int = WEIGHT_HORIZON,
    min_abs_ic: float = ELIGIBLE_ABS_IC,
    min_abs_icir: float = ELIGIBLE_ABS_ICIR,
) -> dict[str, float]:
    """Pilih faktor eligible dari ringkasan IC dan bagi bobot proporsional |IC|.

    ``summary_rows``: baris output ``ic.summarize`` (dict dengan key
    ``factor, mean_ic, icir``). Faktor komposit yang tidak eligible
    mendapat bobot 0 (tetap direkam di histori). Faktor di luar
    ``COMPOSITE_FACTORS`` diabaikan.

    Pure & testable. Kalau tidak ada satu pun eligible -> {} (pemanggil
    fallback ke bobot default).
    """
    per_factor: dict[str, float] = {}
    for r in summary_rows:
        factor = r["factor"]
        if factor not in COMPOSITE_FACTORS:
            continue
        if r.get("horizon") is not None and r.get("horizon") != horizon:
            continue
        ic, icir = r.get("mean_ic"), r.get("icir")
        if ic is None or icir is None:
            per_factor[factor] = 0.0
            continue
        ic, icir = float(ic), float(icir)
        eligible = abs(ic) >= min_abs_ic and abs(icir) >= min_abs_icir
        per_factor[factor] = abs(ic) if eligible else 0.0

    total = sum(per_factor.values())
    if total <= 0:
        return {}
    # Faktor tidak eligible (bobot 0) di-drop agar konsisten dengan
    # load_latest_weights yang hanya mengembalikan bobot > 0.
    return {f: w / total for f, w in per_factor.items() if w > 0}


def save_ic_summary(
    cur: Any,
    run_date: Any,
    summaries: dict[int, list[dict[str, Any]]],
    horizon_for_weights: int = WEIGHT_HORIZON,
) -> tuple[int, dict[str, float]]:
    """Rekam semua horizon + derive & simpan bobot untuk horizon acuan.

    Returns (rows_written, weights_derived). Idempoten: run ulang tanggal
    yang sama menimpa (on conflict do update).
    """
    rows: list[tuple] = []
    for horizon, summary in summaries.items():
        weights = (
            derive_weights(summary, horizon=horizon_for_weights)
            if horizon == horizon_for_weights
            else {}
        )
        for r in summary:
            factor = r["factor"]
            ic = r.get("mean_ic")
            icir = r.get("icir")
            eligible = bool(
                horizon == horizon_for_weights
                and factor in COMPOSITE_FACTORS
                and ic is not None
                and icir is not None
                and abs(float(ic)) >= ELIGIBLE_ABS_IC
                and abs(float(icir)) >= ELIGIBLE_ABS_ICIR
            )
            rows.append(
                (
                    run_date,
                    factor,
                    horizon,
                    ic,
                    icir,
                    r.get("t_stat"),
                    r.get("hit_rate"),
                    r.get("n_days"),
                    eligible,
                    weights.get(factor, 0.0),
                )
            )
    for row in rows:
        # NaN (faktor tanpa coverage) -> NULL: numerik NaN di Postgres meracuni
        # agregasi (avg/max ikut NaN), NULL diabaikan seperti semestinya.
        clean = tuple(
            None if isinstance(v, float) and math.isnan(v) else v for v in row
        )
        cur.execute(_UPSERT_SQL, clean)
    weights = derive_weights(
        summaries.get(horizon_for_weights, []), horizon=horizon_for_weights
    )
    return len(rows), weights


def load_latest_weights(cur: Any) -> dict[str, float]:
    """Bobot faktor dari run factor_ic_history TERBARU ({factor: weight}).

    Mengembalikan dict ber-key nama FAKTOR (vol_21d, ...) hanya untuk bobot
    > 0. Tabel kosong / kosong-bobot -> {} (composite pakai default).
    """
    cur.execute(
        """select factor, weight
           from research.factor_ic_history
           where run_date = (select max(run_date) from research.factor_ic_history)
             and weight > 0"""
    )
    return {str(r["factor"]): float(r["weight"]) for r in cur.fetchall()}


def weights_by_key(cur: Any) -> dict[str, float]:
    """Bobot terbaru dengan key komposit (vol/turnover/dist_52w) untuk composite."""
    latest = load_latest_weights(cur)
    out: dict[str, float] = {}
    for factor, weight in latest.items():
        key = COMPOSITE_FACTORS.get(factor, (None,))[0]
        if key:
            out[key] = weight
    return out


def self_test() -> None:
    """Sanity-check mapping bobot komposit (tanpa DB)."""
    rows = [
        {"factor": "vol_21d", "mean_ic": -0.08, "icir": -0.9},
        {"factor": "turnover_21d", "mean_ic": -0.04, "icir": -0.9},
        {"factor": "dist_52w_high", "mean_ic": 0.05, "icir": 0.5},
        {"factor": "rev_1d", "mean_ic": 0.20, "icir": 3.0},
    ]
    w = derive_weights(rows)
    assert set(w) == {"vol_21d", "dist_52w_high"}, w
    assert abs(w["vol_21d"] - 0.08 / (0.08 + 0.05)) < 1e-9
    assert weights_from_rows_by_key(w) == {
        "vol": w["vol_21d"],
        "dist_52w": w["dist_52w_high"],
    }
    print("self_test OK:", w)


def weights_from_rows_by_key(weights: dict[str, float]) -> dict[str, float]:
    """(Faktor -> bobot) -> (key komposit -> bobot). Pure, helper test/CLI."""
    out: dict[str, float] = {}
    for factor, weight in weights.items():
        key = COMPOSITE_FACTORS.get(factor, (None,))[0]
        if key:
            out[key] = weight
    return out


if __name__ == "__main__":
    self_test()
