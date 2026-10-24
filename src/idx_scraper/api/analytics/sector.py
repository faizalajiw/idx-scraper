"""sector: pecahan api/analytics.py (FAI-19)."""

from __future__ import annotations

from typing import Any

import pandas as pd

from ..sector_map import FALLBACK_SECTOR, sector_for
from ._common import _research_cache_get, _research_cache_put
from .broker_activity import _broker_activity_meta, broker_activity_snapshot


def _sector_peers(
    code: str,
    scores: pd.DataFrame,
    as_of: Any,
    limit: int = 15,
) -> dict[str, Any] | None:
    """Skor emiten lain di sektor yang sama, dari cross-section terakhir.

    Pembanding diambil dari emiten yang SUDAH punya skor. Emiten sektor ini yang
    skornya belum terbentuk tidak muncul — bukan dianggap 0, karena "tidak ada
    data" dan "skornya jelek" adalah dua hal berbeda.

    Emiten di bucket fallback ``Lainnya`` ditandai ``comparable=False``:
    "Lainnya" bukan sektor, jadi membandingkan skor di dalamnya tidak berarti.
    Returns None kalau emiten tidak punya skor sama sekali.
    """
    if scores.empty or "code" not in scores.columns:
        return None
    if not (scores["code"] == code).any():
        return None

    sector = sector_for(code)
    if sector == FALLBACK_SECTOR:
        return {
            "name": sector,
            "comparable": False,
            "peer_count": 0,
            "my_rank": None,
            "median_score": None,
            "peers": [],
        }

    members = scores[scores["code"].map(lambda c: sector_for(str(c)) == sector)]
    if members.empty:
        return None

    ordered = members.sort_values("score", ascending=False).reset_index(drop=True)
    # Peringkat dihitung sebelum pemotongan daftar: emiten peringkat 18 harus
    # tetap dilaporkan 18 walau hanya 15 baris yang ditampilkan.
    pos = ordered.index[ordered["code"] == code]
    shown = ordered.head(limit)

    names = _broker_activity_meta([str(c) for c in shown["code"]], as_of)
    return {
        "name": sector,
        "comparable": True,
        "peer_count": len(members),
        "my_rank": int(pos[0]) + 1 if len(pos) else None,
        "median_score": round(float(members["score"].median()), 1),
        "peers": [
            {
                "code": str(r["code"]),
                "name": names.get(str(r["code"]), {}).get("name"),
                "score": round(float(r["score"]), 1),
                "coverage": round(float(r["coverage"]), 2),
                "is_self": str(r["code"]) == code,
            }
            for _, r in shown.iterrows()
        ],
    }


def _rotation_delta(history: pd.DataFrame, sessions: int) -> float | None:
    """Perubahan median skor sektor vs ``sessions`` sesi sebelumnya (None bila kurang)."""
    if len(history) <= sessions:
        return None
    return round(
        float(history["median_score"].iloc[-1] - history["median_score"].iloc[-1 - sessions]),
        1,
    )


def get_sector_rotation(lookback: int = 60, min_names: int = 3) -> dict[str, Any]:
    """Rotasi sektor dari skor aktivitas broker (proksi aliran dana).

    Rotasi berbasis JEJAK ALIRAN: sektor mana yang skor akumulasinya sedang naik.

    Tiap tanggal diagregasi per sektor (median skor + breadth), lalu rotasi
    diukur dari perubahan median — bukan levelnya. Sektor dengan kurang dari
    ``min_names`` emiten berskor dibuang, dan emiten di bucket fallback
    "Lainnya" tidak diikutkan karena itu bukan sektor (jumlahnya dilaporkan
    terpisah sebagai ``unmapped_names`` supaya tetap jujur berapa yang tercakup).
    """
    lookback = max(5, min(lookback, 250))
    min_names = max(2, min(min_names, 50))
    cache_key = f"sector_rotation:{lookback}:{min_names}"
    cached = _research_cache_get(cache_key)
    if cached is not None:
        return cached

    from ...research import broker_activity as ba

    snap = broker_activity_snapshot()
    as_of = snap["as_of"]
    out: dict[str, Any] = {
        "as_of": str(as_of.date()) if as_of is not None else None,
        "validated": bool(snap["validated"]),
        "reason": snap["reason"],
        "horizon": snap["horizon"],
        "ic_run_date": snap["ic_run_date"],
        "lookback": lookback,
        "min_names": min_names,
        "unmapped_names": 0,
        "sectors": [],
    }

    if not snap["validated"]:
        _research_cache_put(cache_key, out)
        return out

    panel: pd.DataFrame = snap["panel"]
    scores: pd.DataFrame = snap["scores"]
    if panel.empty or scores.empty:
        out["reason"] = (
            "Skor sudah tervalidasi, tapi panel faktor belum cukup panjang untuk "
            "menghitung rotasi sektor."
        )
        _research_cache_put(cache_key, out)
        return out

    codes = sorted({str(c).upper() for c in panel["code"]})
    sector_of = {c: sector_for(c) for c in codes}
    # Berapa emiten berskor yang belum punya sektor sebenarnya — dilaporkan,
    # tidak disembunyikan, supaya cakupan rotasi bisa dinilai apa adanya.
    out["unmapped_names"] = sum(
        1 for c in (str(x).upper() for x in scores["code"]) if sector_for(c) == FALLBACK_SECTOR
    )

    hist = ba.sector_score_history(
        panel,
        snap["factors"],
        sector_of,
        lookback=lookback,
        min_names=min_names,
        exclude_sectors={FALLBACK_SECTOR},
    )
    if hist.empty:
        out["reason"] = (
            "Belum ada sektor dengan cukup emiten berskor untuk dihitung "
            f"(minimal {min_names} emiten per sektor)."
        )
        _research_cache_put(cache_key, out)
        return out

    sectors: list[dict[str, Any]] = []
    for sector, group in hist.groupby("sector"):
        history = group.sort_values("date")
        latest = history.iloc[-1]
        level = round(float(latest["median_score"]), 1)
        delta_5d = _rotation_delta(history, 5)
        sectors.append(
            {
                "sector": str(sector),
                "n_names": int(latest["n_names"]),
                "median_score": level,
                "breadth": round(float(latest["breadth"]), 2),
                "delta_5d": delta_5d,
                "delta_21d": _rotation_delta(history, 21),
                # Fase ditentukan dari perubahan 5 sesi: pertanyaan "sedang ke mana"
                # lebih relevan untuk rotasi daripada "sedang di mana".
                "phase": ba.rotation_phase(level, delta_5d),
                "history": [round(float(v), 1) for v in history["median_score"].tail(30)],
            }
        )

    # Urut dari yang paling sedang menguat (rotasi masuk), bukan dari level.
    sectors.sort(key=lambda s: s["delta_5d"] if s["delta_5d"] is not None else -999.0, reverse=True)
    out["sectors"] = sectors
    _research_cache_put(cache_key, out)
    return out
