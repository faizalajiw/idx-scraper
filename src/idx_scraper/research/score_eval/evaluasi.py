"""Evaluasi: IC, desil, monotonisitas, dan harness walk-forward."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from ..events import _nw_tstat
from ..recommend import (
    HORIZONS,
)
from .bobot import _round_opt, edge_table, score_frame, tier_mix, weights_from_edges
from .param import (
    _ABN,
    _FEATURES,
    BAND_SHARES,
    MIN_FOLD_CONSISTENCY,
    MIN_NAMES_PER_DAY,
    STEP_SESSIONS,
    TEST_SESSIONS,
    TRAIN_SESSIONS,
)


def _spearman(x: pd.Series, y: pd.Series) -> float | None:
    """Korelasi rank Spearman (pure pandas)."""
    pair = pd.concat([x, y], axis=1, keys=["x", "y"]).dropna()
    if len(pair) < 10:
        return None
    rx, ry = pair["x"].rank(), pair["y"].rank()
    dx, dy = rx - rx.mean(), ry - ry.mean()
    denom = math.sqrt(float((dx * dx).sum()) * float((dy * dy).sum()))
    if denom == 0:
        return None
    return float((dx * dy).sum() / denom)


def score_ic(
    feats: pd.DataFrame, horizon: int, score: pd.Series, min_names: int = MIN_NAMES_PER_DAY
) -> dict[str, Any]:
    """IC harian skor vs abnormal return, lalu mean/ICIR/t (Newey-West)."""
    abn_col = f"{_ABN}_{horizon}"
    if feats.empty or abn_col not in feats.columns:
        return {"days": 0, "mean_ic": None, "icir": None, "t_stat": None, "hit_rate": None}
    abn = pd.to_numeric(feats[abn_col], errors="coerce")
    df = pd.DataFrame({"date": pd.to_datetime(feats["date"]), "score": score, "abn": abn})
    daily: list[float] = []
    for _, day in df.groupby("date"):
        if int(day["abn"].notna().sum()) < min_names:
            continue
        ic = _spearman(day["score"], day["abn"])
        if ic is not None:
            daily.append(ic)
    s = pd.Series(daily, dtype=float)
    if s.empty:
        return {"days": 0, "mean_ic": None, "icir": None, "t_stat": None, "hit_rate": None}
    mean = float(s.mean())
    std = float(s.std(ddof=1)) if len(s) > 1 else 0.0
    return {
        "days": len(s),
        "mean_ic": round(mean, 4),
        "icir": round(mean / std, 3) if std > 0 else None,
        "t_stat": _round_opt(_nw_tstat(s)),
        "hit_rate": round(float((s > 0).mean()), 4),
    }


def decile_table(
    feats: pd.DataFrame,
    horizon: int,
    score: pd.Series,
    q: int = 10,
    min_names: int = MIN_NAMES_PER_DAY,
) -> pd.DataFrame:
    """Mean abnormal return per desil skor (rata-rata lintas sesi) — cek monotonisitas."""
    abn_col = f"{_ABN}_{horizon}"
    if feats.empty or abn_col not in feats.columns:
        return pd.DataFrame(columns=["decile", "n", "mean_abn"])
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(feats["date"]),
            "score": pd.to_numeric(score, errors="coerce"),
            "abn": pd.to_numeric(feats[abn_col], errors="coerce"),
        }
    ).dropna()
    per_day: list[pd.Series] = []
    counts: list[pd.Series] = []
    for _, day in df.groupby("date"):
        if len(day) < min_names:
            continue
        try:
            bucket = pd.qcut(day["score"].rank(method="first"), q, labels=False)
        except ValueError:
            continue
        per_day.append(day.groupby(bucket)["abn"].mean())
        counts.append(day.groupby(bucket)["abn"].size())
    if not per_day:
        return pd.DataFrame(columns=["decile", "n", "mean_abn"])
    means = pd.concat(per_day, axis=1).mean(axis=1)
    ns = pd.concat(counts, axis=1).sum(axis=1)
    out = pd.DataFrame(
        {
            "decile": means.index.astype(int) + 1,
            "n": ns.values.astype(int),
            "mean_abn": means.values,
        }
    )
    return out.reset_index(drop=True)


def monotonicity(deciles: pd.DataFrame) -> dict[str, Any]:
    """Ukur monotonisitas: Spearman(desil, mean_abn) + spread desil teratas-bawah."""
    if deciles.empty or len(deciles) < 2:
        return {"spearman": None, "top_minus_bottom": None}
    sp = _spearman(deciles["decile"].astype(float), deciles["mean_abn"].astype(float))
    spread = float(deciles.iloc[-1]["mean_abn"] - deciles.iloc[0]["mean_abn"])
    return {
        "spearman": round(sp, 3) if sp is not None else None,
        "top_minus_bottom": round(spread, 6),
    }


# --------------------------------------------------------------------------- #
# Walk-forward (pure)
# --------------------------------------------------------------------------- #


def session_dates(feats: pd.DataFrame) -> list[pd.Timestamp]:
    return sorted(pd.to_datetime(feats["date"]).unique())


def stable_weights(
    fold_weights: dict[str, list[float]],
    n_folds: int,
    *,
    min_ratio: float = MIN_FOLD_CONSISTENCY,
) -> tuple[dict[str, float], dict[str, dict[str, Any]]]:
    """Bobot yang lolos **stabilitas tanda lintas fold** (pure).

    Aturan: sebuah komponen hanya dibakukan bila (a) ia lolos gate di minimal
    ``min_ratio`` bagian fold, dan (b) semua fold yang lolos itu bersepakat soal
    TANDA. Bobot = median bobot fold yang searah (median, bukan rata-rata:
    lebih tahan satu fold ekstrem). Selain itu bobot 0,0 — komponen yang tidak
    stabil tidak "dibulatkan jadi kecil", ia dimatikan, dan alasan ini dicatat
    di ``verdict`` supaya bisa dibaca (bukan disembunyikan).
    """
    need = max(1, math.ceil(float(min_ratio) * max(1, n_folds)))
    weights: dict[str, float] = {}
    verdict: dict[str, dict[str, Any]] = {}
    for name, samples in fold_weights.items():
        nonzero = [float(v) for v in samples if float(v) != 0.0]
        pos = [v for v in nonzero if v > 0]
        neg = [v for v in nonzero if v < 0]
        if len(nonzero) < need:
            weights[name] = 0.0
            verdict[name] = {
                "status": "dimatikan",
                "reason": "lolos gate di terlalu sedikit fold",
                "nonzero_folds": len(nonzero),
                "need": need,
            }
            continue
        same = pos if len(pos) >= len(neg) else neg
        if len(same) < need:
            weights[name] = 0.0
            verdict[name] = {
                "status": "dimatikan",
                "reason": "tanda bobot tidak konsisten antar fold",
                "nonzero_folds": len(nonzero),
                "need": need,
            }
            continue
        med = float(np.median(same))
        weights[name] = round(med * 2.0) / 2.0
        verdict[name] = {
            "status": "dipakai",
            "median": med,
            "nonzero_folds": len(nonzero),
            "same_sign_folds": len(same),
        }
    return weights, verdict


def walk_forward(
    feats: pd.DataFrame,
    *,
    horizons: tuple[int, ...] = HORIZONS,
    train: int = TRAIN_SESSIONS,
    test: int = TEST_SESSIONS,
    step: int = STEP_SESSIONS,
    min_consistency: float = MIN_FOLD_CONSISTENCY,
) -> dict[str, Any]:
    """Uji & tentukan bobot per horizon lewat fold train -> test yang tak tumpang tindih.

    Untuk tiap horizon: tiap fold mengukur edge komponen **hanya di train**,
    menurunkan bobot, lalu menilai komposit itu di **test** (IC, desil,
    monotonisitas) — angka yang benar-benar out-of-sample. Bobot yang dibakukan
    harus lolos filter stabilitas tanda lintas fold (``stable_weights``).

    Grade **tidak** dikalibrasi di sini: ia adalah peringkat relatif pool harian
    (``recommend.BAND_SHARES``), jadi tidak ada ambang absolut yang perlu
    diturunkan dari train — cukup dilaporkan sebarannya (``tier_mix``).
    """
    dates = session_dates(feats)
    out: dict[str, Any] = {
        "sessions": len(dates),
        "train": int(train),
        "test": int(test),
        "step": int(step),
        "by_horizon": {},
    }
    for k in horizons:
        if len(dates) < train + test:
            out["by_horizon"][str(k)] = {"folds": 0, "note": "sesi belum cukup untuk satu fold"}
            continue

        fold_rows: list[dict[str, Any]] = []
        weight_samples: dict[str, list[float]] = {f: [] for f in _FEATURES}
        test_scores: list[pd.Series] = []
        test_feats: list[pd.DataFrame] = []

        start = train
        while start + test <= len(dates):
            train_dates = dates[start - train : start]
            test_dates = dates[start : start + test]
            f_train = feats[pd.to_datetime(feats["date"]).isin(train_dates)]
            f_test = feats[pd.to_datetime(feats["date"]).isin(test_dates)]

            edges = edge_table(f_train, k)
            weights = weights_from_edges(edges)
            scores_train = score_frame(f_train, weights)

            scores_test = score_frame(f_test, weights)
            ic = score_ic(f_test, k, scores_test)
            dec = decile_table(f_test, k, scores_test)
            mono = monotonicity(dec)

            fold_rows.append(
                {
                    "train": (str(train_dates[0].date()), str(train_dates[-1].date())),
                    "train_dates": [str(d)[:10] for d in train_dates],
                    "test": (str(test_dates[0].date()), str(test_dates[-1].date())),
                    "n_test": len(f_test),
                    "active_components": int(sum(1 for w in weights.values() if w)),
                    "train_tier_mix": tier_mix(scores_train),
                    "ic": ic,
                    "monotonicity": mono,
                }
            )
            for f in _FEATURES:
                weight_samples[f].append(float(weights.get(f, 0.0)))
            test_scores.append(scores_test)
            test_feats.append(f_test)
            start += step

        pooled_feats = pd.concat(test_feats, ignore_index=True) if test_feats else pd.DataFrame()
        pooled_scores = (
            pd.concat(test_scores, ignore_index=True) if test_scores else pd.Series(dtype=float)
        )
        pooled_ic = score_ic(pooled_feats, k, pooled_scores) if not pooled_feats.empty else {}
        pooled_dec = (
            decile_table(pooled_feats, k, pooled_scores)
            if not pooled_feats.empty
            else pd.DataFrame()
        )

        stable, verdict = stable_weights(weight_samples, len(fold_rows), min_ratio=min_consistency)
        mean_weights = {
            f: round(float(np.mean(weight_samples[f])), 2) if weight_samples[f] else 0.0
            for f in _FEATURES
        }
        matrix = pd.DataFrame(
            weight_samples, index=[f"fold{i + 1}" for i in range(len(fold_rows))]
        ).T
        full_edges = edge_table(feats, k)

        out["by_horizon"][str(k)] = {
            "folds": len(fold_rows),
            "fold_detail": fold_rows,
            "weights": {f: w for f, w in stable.items() if w},
            "weights_all": stable,
            "verdict": verdict,
            "mean_fold_weights": mean_weights,
            "fold_weights": {f: [round(float(v), 2) for v in weight_samples[f]] for f in _FEATURES},
            "labels": (matrix.index.tolist(), matrix.columns.tolist()),
            "band_shares": {"c": BAND_SHARES[0], "b": BAND_SHARES[1], "a": BAND_SHARES[2]},
            "full_sample_edges": full_edges.reset_index().to_dict(orient="records"),
            "pooled_oos_ic": pooled_ic,
            "pooled_oos_deciles": pooled_dec.to_dict(orient="records"),
            "pooled_oos_monotonicity": monotonicity(pooled_dec),
            "pooled_oos_rows": len(pooled_feats),
        }
    return out


def frozen_eval(
    feats: pd.DataFrame,
    weights_by_horizon: dict[int, dict[str, float]],
    *,
    horizons: tuple[int, ...] = HORIZONS,
) -> dict[str, Any]:
    """Terapkan bobot yang DIBakukan ke seluruh sampel — deskriptif, bukan bukti OOS.

    Bobot dibakukan dari sampel ini juga, jadi angkanya hanya boleh dibaca
    sebagai gambaran, bukan pengujian. Bukti tetap di ``walk_forward``.
    """
    out: dict[str, Any] = {"note": "deskriptif (bobot diturunkan dari sampel yang sama)"}
    for k in horizons:
        w = weights_by_horizon.get(k) or {}
        scores = score_frame(feats, w)
        dec = decile_table(feats, k, scores)
        out[str(k)] = {
            "ic": score_ic(feats, k, scores),
            "deciles": dec.to_dict(orient="records"),
            "monotonicity": monotonicity(dec),
        }
    return out
