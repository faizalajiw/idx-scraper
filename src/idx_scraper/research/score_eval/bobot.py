"""Edge komponen, bobot dari edge, skor, dan band tier."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ..events import _nw_tstat
from ..recommend import (
    NEUTRAL_SCORE,
)
from .param import (
    _ABN,
    _FEATURES,
    BAND_SHARES,
    EDGE_GATE_T,
    EDGE_TO_POINTS,
    MAX_COMPONENT_POINTS,
    MIN_EDGE,
    MIN_EDGE_DAYS,
    MIN_NAMES_PER_DAY,
    SHRINK,
)


def edge_table(feats: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """Edge tiap komponen pada satu horizon: mean(abn | bendera) − mean(abn universe).

    Diukur per sesi (cross-sectional), lalu diagregasi: rata-rata edge harian,
    t-stat Newey-West (lag 5, karena return tumpang tindih antar hari), hit-rate
    hari yang edgenya positif, jumlah nama tertandai, dan jumlah sesi valid.

    Sesi dilewati bila: universe < ``MIN_NAMES_PER_DAY``, atau bendera tidak
    menandai satu nama pun di sesi itu. **Satu nama per hari sudah cukup**: tiap
    sesi menyumbang satu observasi edge harian, tak peduli berapa nama yang
    ditandai — jadi komponen langka (mis. ``pattern_initiation`` yang menyala
    ~1 nama/hari) tetap terukur, tidak terbuang seperti kalau ambangnya
    "minimal 3 nama per hari". Yang membatasi komponen langka adalah jumlah
    HARI terukur (``MIN_EDGE_DAYS`` di ``weights_from_edges``), bukan jumlah nama
    per hari.
    """
    cols = [
        "days",
        "flagged",
        "mean_abn_flagged",
        "mean_abn_universe",
        "edge",
        "edge_pooled",
        "t_stat",
        "hit_rate",
        "coverage",
    ]
    if feats.empty or f"{_ABN}_{horizon}" not in feats.columns:
        return pd.DataFrame(columns=cols, index=pd.Index([], name="component"))

    abn_col = f"{_ABN}_{horizon}"
    daily: dict[str, list[float]] = {f: [] for f in _FEATURES}
    daily_flagged: dict[str, list[float]] = {f: [] for f in _FEATURES}
    flagged: dict[str, int] = {f: 0 for f in _FEATURES}
    universe_abn: list[float] = []

    flags = feats[[*_FEATURES]].to_numpy(dtype=float)
    abn_all = pd.to_numeric(feats[abn_col], errors="coerce").to_numpy(dtype=float)
    dates = pd.to_datetime(feats["date"]).to_numpy()

    for d in pd.unique(dates):
        mask_day = dates == d
        a = abn_all[mask_day]
        ok = np.isfinite(a)
        if int(ok.sum()) < MIN_NAMES_PER_DAY:
            continue
        a_ok = a[ok]
        f_day = flags[mask_day][ok]
        base = float(a_ok.mean())
        universe_abn.append(base)
        for j, name in enumerate(_FEATURES):
            sel = f_day[:, j] > 0.5
            n_sel = int(sel.sum())
            if n_sel < 1:
                continue
            flag_mean = float(a_ok[sel].mean())
            daily[name].append(flag_mean - base)
            daily_flagged[name].append(flag_mean)
            flagged[name] += n_sel

    # Edge gabungan (pooled, tanpa memisah sesi) — angka yang bisa dibandingkan
    # langsung dengan signal_log/events (mean abnormal seluruh baris tertandai).
    finite = np.isfinite(abn_all)
    pooled_universe = float(abn_all[finite].mean()) if finite.any() else None
    edge_pooled: dict[str, float | None] = {}
    for j, name in enumerate(_FEATURES):
        sel = (flags[:, j] > 0.5) & finite
        if int(sel.sum()) < 1 or pooled_universe is None:
            edge_pooled[name] = None
        else:
            edge_pooled[name] = round(float(abn_all[sel].mean()) - pooled_universe, 6)

    rows = []
    for name in _FEATURES:
        e = pd.Series(daily[name], dtype=float)
        if e.empty:
            rows.append(
                {
                    "component": name,
                    "days": 0,
                    "flagged": flagged[name],
                    "mean_abn_flagged": None,
                    "mean_abn_universe": None,
                    "edge": None,
                    "edge_pooled": edge_pooled[name],
                    "t_stat": None,
                    "hit_rate": None,
                    "coverage": None,
                }
            )
            continue
        rows.append(
            {
                "component": name,
                "days": len(e),
                "flagged": int(flagged[name]),
                "mean_abn_flagged": round(float(np.mean(daily_flagged[name])), 6),
                "mean_abn_universe": round(float(np.mean(universe_abn)), 6)
                if universe_abn
                else None,
                "edge": round(float(e.mean()), 6),
                "edge_pooled": edge_pooled[name],
                "t_stat": _round_opt(_nw_tstat(e)),
                "hit_rate": round(float((e > 0).mean()), 4),
                "coverage": round(flagged[name] / max(1, int(feats.shape[0])), 4),
            }
        )
    return pd.DataFrame(rows, columns=["component", *cols]).set_index("component")


def weights_from_edges(
    edges: pd.DataFrame,
    *,
    gate_t: float = EDGE_GATE_T,
    min_edge: float = MIN_EDGE,
    min_days: int = MIN_EDGE_DAYS,
    shrink: float = SHRINK,
    edge_to_points: float = EDGE_TO_POINTS,
    max_points: float = MAX_COMPONENT_POINTS,
) -> dict[str, float]:
    """Ubah tabel edge -> bobot poin (pure). Yang tidak lolos gate berbobot 0,0.

    ``bobot = clamp(edge x edge_to_points, +-max_points) x shrink`` untuk edge
    yang |t| >= ``gate_t`` DAN |edge| >= ``min_edge`` DAN terukur di >= ``min_days``
    sesi; selain itu 0,0. Ambang jumlah HARI (bukan jumlah nama per hari) yang
    menjaga komponen langka tetap bisa lolos selama buktinya cukup panjang.
    """
    out: dict[str, float] = {}
    for name, row in edges.iterrows():
        edge = row.get("edge")
        t = row.get("t_stat")
        days = row.get("days")
        if days is None or int(days) < int(min_days):
            out[str(name)] = 0.0
            continue
        if edge is None or t is None or not np.isfinite(float(edge)) or not np.isfinite(float(t)):
            out[str(name)] = 0.0
            continue
        if abs(float(t)) < float(gate_t) or abs(float(edge)) < float(min_edge):
            out[str(name)] = 0.0
            continue
        pts = float(np.clip(float(edge) * float(edge_to_points), -max_points, max_points))
        out[str(name)] = round(pts * float(shrink) * 2.0) / 2.0  # bulat 0,5
    return out


def score_frame(feats: pd.DataFrame, weights: dict[str, float]) -> pd.Series:
    """Skor komposit per baris: ``NEUTRAL + Σ bobot x bendera``, dipangkas 0..100."""
    if feats.empty:
        return pd.Series(dtype=float)
    total = np.full(len(feats), NEUTRAL_SCORE, dtype=float)
    for name, w in weights.items():
        if not w or name not in feats.columns:
            continue
        total += float(w) * feats[name].to_numpy(dtype=float)
    return pd.Series(np.clip(total, 0.0, 100.0), index=feats.index)


def tier_mix(scores: pd.Series, shares: tuple[float, float, float] = BAND_SHARES) -> dict[str, Any]:
    """Sebaran tier bila grade = **peringkat relatif pool** (bukan ambang absolut).

    Melaporkan skor ambang dan jumlah baris di atas tiap share (C/B/A). Ini juga
    bukti kalibrasi: pada distribusi ber-titik-massa (banyak skor persis sama),
    share nyata jauh melebihi share nominal — justru alasan grade ditentukan
    lewat peringkat pool harian, bukan ambang absolut hasil persentil.
    """
    s = pd.to_numeric(scores, errors="coerce").dropna()
    n = int(s.shape[0])
    if n == 0:
        return {"n": 0}
    out: dict[str, Any] = {"n": n}
    for label, share in zip(("c", "b", "a"), shares, strict=True):
        cut = float(np.quantile(s, float(share)))
        rows = int((s >= cut).sum())
        out[label] = {"min_score": round(cut, 2), "rows": rows, "share": round(rows / n, 4)}
    return out


def _round_opt(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if not np.isfinite(f) else round(f, 6)
