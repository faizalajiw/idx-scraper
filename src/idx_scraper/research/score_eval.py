"""Kalibrasi walk-forward komposisi skor kandidat beli (pure + DB + CLI).

Menjawab pertanyaan yang menentukan apakah skor Rekomendasi layak dipercaya:
**"komponen mana yang benar-benar punya edge, berapa bobotnya, dan untuk horizon
berapa — diukur di data yang BELUM dilihat saat bobotnya ditentukan?"**

Kenapa modul ini ada
--------------------
AUDIT §15 menunjukkan ambang grade hasil kalibrasi **belum monoton** terhadap
abnormal return: skor lama adalah campuran poin yang ditulis tangan (sinyal
teknikal +30, tren +12, RSI +8, penalti −12, ...) tanpa bukti out-of-sample
bahwa setiap potongan poin itu bergerak searah hasil. Ambangnya sudah
dikalibrasi; **komposisinya belum pernah diuji**. Modul ini menguji dan
menentukan ulang komposisinya per horizon.

Aturan yang dipegang (lihat skill ``backtesting-discipline``)
-------------------------------------------------------------
1. **Satu definisi komponen.** Fitur diambil dari
   ``recommend.candidate_features`` — fungsi yang sama yang menyusun skor nyata.
   Kalau definisinya berbeda, bobot hasil ukur tidak menggambarkan skor yang
   dipakai.
2. **Walk-forward, bukan in-sample.** Tiap fold: ukur edge komponen **hanya di
   jendela train**, lalu terapkan bobotnya ke jendela test berikutnya yang belum
   dilihat. Jendela test tidak tumpang tindih.
3. **Eksekusi T+1, abnormal vs pasar.** Outcome memakai ``signal_log
   .attach_outcomes`` (entry close T+1, ``abn_k`` = return dikurangi pasar
   equal-weight window yang sama) — disiplin yang sama dengan ``signal_log``,
   ``events`` dan ``recommendation_log``.
4. **Gate, bukan keyakinan.** Komponen hanya dapat bobot bila |t| Newey-West
   >= ``EDGE_GATE_T`` DAN |edge| >= ``MIN_EDGE`` di train. Yang tidak lolos
   berbobot 0 — bukan dibulatkan jadi poin kecil.
5. **Shrinkage eksplisit.** Sampel hanya ~1 tahun; bobot train dikalikan
   ``SHRINK`` sebelum dipakai. Edge hasil ukur yang tipis tidak boleh jadi
   keyakinan penuh.
5b. **Stabilitas tanda lintas fold.** Komponen yang lolos gate di satu fold
   tapi berbalik tanda di fold lain dimatikan (``stable_weights``): bobot = 0
   beserta alasannya, bukan dibulatkan jadi poin kecil.
6. **Horizon terpisah.** Komposisi diukur & ditentukan per horizon
   (5/10/21 hari). Satu skor untuk semua horizon adalah asumsi, bukan hasil.

Cara baca hasilnya
------------------
Angka yang **menjadi bukti** adalah statistik OOS per fold (bagian
``walk_forward``); jalur ``frozen`` hanya deskriptif karena bobotnya
diturunkan dari sampel yang sama. Kalau tidak ada komponen yang lolos gate,
hasilnya sah: skor jatuh ke netral (``NEUTRAL_SCORE``) dan itu dilaporkan apa
adanya, bukan ditambal dengan poin rekaan.

Contoh pakai:
    set -a && . ./.env && set +a
    PYTHONPATH=src .venv/Scripts/python.exe -m idx_scraper.research.score_eval \\
        --horizons 5 10 21 --train 100 --test 25 --json out_score_eval.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from typing import Any

import numpy as np
import pandas as pd
import psycopg

from .events import _nw_tstat
from .recommend import (
    COMPONENT_NOTES,
    HORIZONS,
    MIN_DAY_VALUE,
    MIN_MARKET_HISTORY,
    NEUTRAL_SCORE,
    candidate_features,
    entry_plan,
)
from .recommendation_log import load_candidate_panel, metrics_frame
from .signal_log import attach_outcomes

# --- parameter walk-forward ------------------------------------------------- #

TRAIN_SESSIONS = 100      # panjang jendela train (hari bursa)
TEST_SESSIONS = 25        # panjang jendela test per fold (tidak tumpang tindih)
STEP_SESSIONS = 25        # langkah antar fold

MIN_NAMES_PER_DAY = 20    # sesi dengan kandidat lebih sedikit dari ini dilewati
MIN_EDGE_DAYS = 20        # bendera harus terukur di >= N sesi agar boleh dapat bobot
EDGE_GATE_T = 1.5         # |t| Newey-West minimum (di train) agar komponen dapat bobot
MIN_EDGE = 0.0015         # |edge| minimum (0,15%) agar komponen dapat bobot
EDGE_TO_POINTS = 600.0    # 1% edge -> 6 poin skor
MAX_COMPONENT_POINTS = 15.0
SHRINK = 0.5              # pengecilan bobot train (sampel pendek)
MIN_FOLD_CONSISTENCY = 2 / 3   # bagian fold minimum yang harus sependapat tanda
BAND_SHARES = (0.75, 0.90, 0.98)   # bagian pool harian untuk band C / B / A

_ABN = "abn"  # prefix kolom outcome di attach_outcomes

_FEATURES: tuple[str, ...] = tuple(COMPONENT_NOTES)


# --------------------------------------------------------------------------- #
# Universe + fitur (pure)
# --------------------------------------------------------------------------- #


def candidate_universe(
    metrics: pd.DataFrame,
    *,
    min_value: float = MIN_DAY_VALUE,
    min_history: int = MIN_MARKET_HISTORY,
) -> pd.DataFrame:
    """Baris yang benar-benar bisa jadi kandidat: likuid, cukup histori, bukan SELL.

    Pengukuran edge **harus** memakai universe yang sama dengan yang diskor;
    kalau tidak, edge yang "terukur" datang dari nama yang tidak pernah bisa
    masuk papan.
    """
    if metrics.empty:
        return metrics.copy()
    df = metrics.copy()
    df = df[pd.to_numeric(df["hist_days"], errors="coerce") >= int(min_history)]
    df = df[pd.to_numeric(df["value"], errors="coerce") >= float(min_value)]
    df = df[pd.to_numeric(df["close"], errors="coerce") > 0]
    if "signal" in df.columns:
        df = df[df["signal"] != "SELL"]
    return df.reset_index(drop=True)


def feature_frame(universe: pd.DataFrame) -> pd.DataFrame:
    """Tambah kolom fitur komponen + ``setup`` ke universe (pure).

    Baris yang tidak bisa dihitung level entry-nya (``entry_plan`` None)
    dibuang: sama seperti ``score_candidate``, tanpa pembatalan yang bisa
    dihitung baris itu tidak akan pernah muncul di papan.
    """
    if universe.empty:
        return pd.DataFrame(columns=["code", "date", "close", *_FEATURES, "setup"])
    rows: list[dict[str, Any]] = []
    for r in universe.itertuples(index=False):
        m = {
            "code": r.code,
            "signal": getattr(r, "signal", None),
            "trend_up": bool(getattr(r, "trend_up", False)),
            "rsi": getattr(r, "rsi", None),
            "dist_52w_pct": getattr(r, "dist_52w_pct", None),
            "vol_ratio": getattr(r, "vol_ratio", None),
            "vol_daily": getattr(r, "vol_daily", None),
            "z_score": getattr(r, "z_score", None),
            "mom_20d": getattr(r, "mom_20d", None),
            "value": getattr(r, "value", None),
            "close": getattr(r, "close", None),
            "bb_lower": getattr(r, "bb_lower", None),
            "range_low_20": getattr(r, "range_low_20", None),
            "pattern_buy": tuple(getattr(r, "pattern_buy", None) or ()),
            "pattern_sell": tuple(getattr(r, "pattern_sell", None) or ()),
        }
        plan = entry_plan(
            close=m["close"],
            signal=str(m["signal"]) if m["signal"] else None,
            trend_up=m["trend_up"],
            rsi=m["rsi"],
            bb_lower=m["bb_lower"],
            dist_52w_pct=m["dist_52w_pct"],
            vol_ratio=m["vol_ratio"],
            vol_daily=m["vol_daily"],
            range_low_20=m["range_low_20"],
        )
        if plan is None:
            continue
        feats = candidate_features(m, setup=plan["setup"])
        rows.append(
            {
                "code": str(r.code).upper(),
                "date": pd.Timestamp(r.date),
                "close": float(m["close"]),
                "setup": plan["setup"],
                **feats,
            }
        )
    return pd.DataFrame(rows)


def benchmark_prices(panel: dict[str, dict[str, Any]]) -> pd.DataFrame:
    """Panel harga SELURUH pasar (semua emiten, semua sesi) untuk benchmark (pure).

    Penting: benchmark pasar HARUS dari panel penuh, bukan dari universe
    kandidat. Kalau dihitung dari universe yang sudah disaring, dua hal rusak:
    (a) return harian per emiten meloncat karena baris yang keluar-masuk saringan
    dianggap berurutan, dan (b) "pasar" jadi rata-rata emiten likuid saja.
    Keduanya menggelembungkan ``abn`` jadi artefak, bukan edge.
    """
    rows = [
        {"code": code, "date": row["date"], "close": row["close"]}
        for code, entry in panel.items()
        for row in entry.get("rows", [])
        if row.get("close") is not None
    ]
    return pd.DataFrame(rows)


def attach_abnormal(
    feats: pd.DataFrame,
    horizons: tuple[int, ...],
    prices: pd.DataFrame,
) -> pd.DataFrame:
    """Tempelkan ``abn_k`` (abnormal vs pasar, entry T+1) ke frame fitur (pure).

    ``prices`` = panel penuh (``benchmark_prices``); outcome dihitung di sana
    supaya benchmark se-pasar, lalu di-join strict ``(code, date)`` ke universe.
    """
    if feats.empty:
        return feats
    if prices.empty:
        out = feats.copy()
        for k in horizons:
            out[f"{_ABN}_{k}"] = np.nan
        return out
    out = attach_outcomes(prices, horizons=tuple(horizons), nlags=1)
    cols = [f"{_ABN}_{k}" for k in horizons]
    return feats.merge(out[["code", "date", *cols]], on=["code", "date"], how="left")


# --------------------------------------------------------------------------- #
# Edge per komponen (pure)
# --------------------------------------------------------------------------- #


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
    cols = ["days", "flagged", "mean_abn_flagged", "mean_abn_universe", "edge",
            "edge_pooled", "t_stat", "hit_rate", "coverage"]
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
            rows.append({"component": name, "days": 0, "flagged": flagged[name],
                         "mean_abn_flagged": None, "mean_abn_universe": None,
                         "edge": None, "edge_pooled": edge_pooled[name],
                         "t_stat": None, "hit_rate": None, "coverage": None})
            continue
        rows.append(
            {
                "component": name,
                "days": len(e),
                "flagged": int(flagged[name]),
                "mean_abn_flagged": round(float(np.mean(daily_flagged[name])), 6),
                "mean_abn_universe": round(float(np.mean(universe_abn)), 6) if universe_abn else None,
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
    for label, share in zip(("c", "b", "a"), shares):
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


# --------------------------------------------------------------------------- #
# Statistik OOS (pure)
# --------------------------------------------------------------------------- #


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


def score_ic(feats: pd.DataFrame, horizon: int, score: pd.Series, min_names: int = MIN_NAMES_PER_DAY) -> dict[str, Any]:
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
    out = pd.DataFrame({"decile": means.index.astype(int) + 1, "n": ns.values.astype(int),
                        "mean_abn": means.values})
    return out.reset_index(drop=True)


def monotonicity(deciles: pd.DataFrame) -> dict[str, Any]:
    """Ukur monotonisitas: Spearman(desil, mean_abn) + spread desil teratas-bawah."""
    if deciles.empty or len(deciles) < 2:
        return {"spearman": None, "top_minus_bottom": None}
    sp = _spearman(deciles["decile"].astype(float), deciles["mean_abn"].astype(float))
    spread = float(deciles.iloc[-1]["mean_abn"] - deciles.iloc[0]["mean_abn"])
    return {"spearman": round(sp, 3) if sp is not None else None,
            "top_minus_bottom": round(spread, 6)}


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
            verdict[name] = {"status": "dimatikan", "reason": "lolos gate di terlalu sedikit fold",
                             "nonzero_folds": len(nonzero), "need": need}
            continue
        same = pos if len(pos) >= len(neg) else neg
        if len(same) < need:
            weights[name] = 0.0
            verdict[name] = {"status": "dimatikan", "reason": "tanda bobot tidak konsisten antar fold",
                             "nonzero_folds": len(nonzero), "need": need}
            continue
        med = float(np.median(same))
        weights[name] = round(med * 2.0) / 2.0
        verdict[name] = {"status": "dipakai", "median": med,
                         "nonzero_folds": len(nonzero), "same_sign_folds": len(same)}
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
            train_dates = dates[start - train:start]
            test_dates = dates[start:start + test]
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
        pooled_scores = pd.concat(test_scores, ignore_index=True) if test_scores else pd.Series(dtype=float)
        pooled_ic = score_ic(pooled_feats, k, pooled_scores) if not pooled_feats.empty else {}
        pooled_dec = decile_table(pooled_feats, k, pooled_scores) if not pooled_feats.empty else pd.DataFrame()

        stable, verdict = stable_weights(weight_samples, len(fold_rows), min_ratio=min_consistency)
        mean_weights = {
            f: round(float(np.mean(weight_samples[f])), 2) if weight_samples[f] else 0.0
            for f in _FEATURES
        }
        matrix = pd.DataFrame(weight_samples, index=[f"fold{i + 1}" for i in range(len(fold_rows))]).T
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


# --------------------------------------------------------------------------- #
# Data + CLI
# --------------------------------------------------------------------------- #


def load_features(dsn: str, sessions: int = 300) -> pd.DataFrame:
    """Muat panel kandidat dari DB -> universe -> fitur + abnormal (siap diuji).

    Memakai panel & metrik yang PERSIS sama dengan papan Rekomendasi
    (``recommendation_log.load_candidate_panel`` + ``metrics_frame``).
    """
    panel = load_candidate_panel(dsn, sessions=sessions)
    metrics = metrics_frame(panel)
    universe = candidate_universe(metrics)
    feats = feature_frame(universe)
    prices = benchmark_prices(panel)
    prices["date"] = pd.to_datetime(prices["date"])
    return attach_abnormal(feats, HORIZONS, prices)


def format_weights(weights_by_horizon: dict[int, dict[str, float]]) -> str:
    """Cetak konstanta Python siap-tempel untuk ``recommend.py`` (deterministik)."""
    lines = ["COMPONENT_WEIGHTS: dict[int, dict[str, float]] = {"]
    for k, w in weights_by_horizon.items():
        lines.append(f"    {k}: {{")
        for name in _FEATURES:
            val = w.get(name, 0.0)
            if val:
                lines.append(f'        "{name}": {val},')
        lines.append("    },")
    lines.append("}")
    lines.append("")
    lines.append("# grade = peringkat relatif pool harian (bukan ambang skor absolut)")
    lines.append(f"BAND_SHARES: tuple[float, float, float] = {tuple(BAND_SHARES)}")
    return "\n".join(lines)


def print_report(report: dict[str, Any]) -> None:
    print("=" * 78)
    print("WALK-FORWARD KOMPOSISI SKOR KANDIDAT BELI (PIT, entry T+1, abn vs pasar)")
    print("=" * 78)
    print(
        f"  sesi={report['sessions']}  train={report['train']}  test={report['test']}  "
        f"step={report['step']}"
    )
    for k, h in report["by_horizon"].items():
        print(f"\n--- horizon {k} hari ---")
        if not h.get("folds"):
            print(f"  {h.get('note', 'tidak ada fold')}")
            continue
        print(f"  fold={h['folds']}  baris test OOS={h['pooled_oos_rows']}")
        for fold in h["fold_detail"]:
            ic = fold["ic"]
            mo = fold["monotonicity"]
            ic_s = f"IC={ic.get('mean_ic'):+.4f}" if ic.get("mean_ic") is not None else "IC=n/a"
            t_s = f"t={ic.get('t_stat'):+.1f}" if ic.get("t_stat") is not None else "t=n/a"
            mo_s = f"rho={mo.get('spearman'):+.2f}" if mo.get("spearman") is not None else "rho=n/a"
            print(
                f"    {fold['test'][0]}..{fold['test'][1]}  n={fold['n_test']:<5} "
                f"aktif={fold['active_components']:<2} {ic_s} {t_s} {mo_s}"
            )
        pic = h["pooled_oos_ic"]
        pmo = h["pooled_oos_monotonicity"]
        print("  pooled OOS:")
        print(
            f"    IC={pic.get('mean_ic')} ICIR={pic.get('icir')} t={pic.get('t_stat')} "
            f"hit={pic.get('hit_rate')} hari={pic.get('days')}"
        )
        print(f"    monotonisitas: rho={pmo.get('spearman')} spread Q10-Q1={pmo.get('top_minus_bottom')}")
        for row in h["pooled_oos_deciles"]:
            print(f"      desil {row['decile']:<2} n={row['n']:<6} abn={row['mean_abn']:+.2%}")
        if h["fold_detail"]:
            mix = h["fold_detail"][-1].get("train_tier_mix") or {}
            print(f"  sebaran tier di train fold terakhir (n={mix.get('n')}): " + ", ".join(
                f"{lab.upper()} {mix[lab]['rows']} ({mix[lab]['share']:.0%} >= {mix[lab]['min_score']})"
                for lab in ("c", "b", "a") if isinstance(mix.get(lab), dict)
            ))
        print("  bobot per fold (0 = tidak lolos gate di fold itu):")
        fw = h["fold_weights"]
        for name in _FEATURES:
            samples = fw.get(name) or []
            if not any(samples):
                continue
            cells = "  ".join(f"{v:+5.1f}" for v in samples)
            vd = (h["verdict"] or {}).get(name, {})
            mark = "DIPAKAI " if vd.get("status") == "dipakai" else "dimatikan"
            note = "" if vd.get("status") == "dipakai" else f"  ({vd.get('reason')})"
            print(f"    {name:<32} {cells}  -> {mark}{note}")
        if not h["weights"]:
            print("    (tidak ada komponen yang lolos gate + stabilitas tanda)")
        print(f"  bobot stabil yang dibakukan: {h['weights']}")
        print(
            "  grade = peringkat relatif pool harian; share band "
            f"C/B/A = {BAND_SHARES[0]:.0%}/{BAND_SHARES[1]:.0%}/{BAND_SHARES[2]:.0%}"
        )
        print("  edge seluruh sampel (deskriptif, in-sample — pembanding signal_log):")
        for row in h["full_sample_edges"]:
            if row.get("edge") is None:
                continue
            if abs(float(row["edge"])) < 0.002 and abs(float(row.get("edge_pooled") or 0)) < 0.002:
                continue
            print(
                f"    {row['component']:<32} edge={row['edge']:+.3%} "
                f"pooled={row['edge_pooled']:+.3%} t={row['t_stat']} "
                f"hit={row['hit_rate']} hari={row['days']}"
            )
        thin = [
            str(row["component"])
            for row in h["full_sample_edges"]
            if int(row.get("days") or 0) < MIN_EDGE_DAYS
        ]
        if thin:
            print(
                f"  {len(thin)} komponen belum bisa dinilai: terukur di < {MIN_EDGE_DAYS} sesi "
                f"(mis. {', '.join(thin[:4])}) — bobotnya 0 bukan karena lemah, tapi "
                "karena datanya belum cukup panjang."
            )
    print("\nCatatan: angka bukti = pooled OOS di atas; jalur 'frozen' hanya deskriptif.")
    print(
        "Komponen tanpa gate |t|>=1,5 & |edge|>=0,15% & >=20 sesi terukur di train "
        "berbobot 0. Grade sendiri bukan ambang skor: ia peringkat relatif pool harian."
    )


def main() -> int:
    ap = argparse.ArgumentParser(prog="score_eval")
    ap.add_argument("--horizons", type=int, nargs="+", default=list(HORIZONS))
    ap.add_argument("--train", type=int, default=TRAIN_SESSIONS)
    ap.add_argument("--test", type=int, default=TEST_SESSIONS)
    ap.add_argument("--step", type=int, default=STEP_SESSIONS)
    ap.add_argument("--sessions", type=int, default=300)
    ap.add_argument("--json", type=str, default=None, help="tulis laporan penuh ke berkas JSON")
    ap.add_argument("--frozen", action="store_true", help="tambahan evaluasi deskriptif bobot beku")
    args = ap.parse_args()

    dsn = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not dsn:
        print("[err] DATABASE_URL/SUPABASE_DB_URL belum diisi", file=sys.stderr)
        return 1

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("select 1")  # gagal cepat kalau DSN salah

    feats = load_features(dsn, sessions=args.sessions)
    if feats.empty:
        print("[err] tidak ada baris kandidat — cek rentang data", file=sys.stderr)
        return 1

    horizons = tuple(args.horizons)
    report = walk_forward(feats, horizons=horizons, train=args.train, test=args.test, step=args.step)

    weights_by_horizon = {
        int(k): {n: float(v) for n, v in h.get("weights_all", {}).items()}
        for k, h in report["by_horizon"].items()
    }
    report["constants"] = format_weights(weights_by_horizon)
    print_report(report)

    if args.frozen:
        report["frozen"] = frozen_eval(feats, weights_by_horizon, horizons=horizons)
        print("\n" + "=" * 78)
        print("FROZEN (deskriptif — bobot diturunkan dari sampel yang sama)")
        print("=" * 78)
        for k, v in report["frozen"].items():
            if k == "note":
                continue
            print(f"  horizon {k}: IC={v['ic'].get('mean_ic')} t={v['ic'].get('t_stat')} "
                  f"rho={v['monotonicity'].get('spearman')}")

    print("\n" + "=" * 78)
    print("KONSTANTA SIAP-TEMPEL (recommend.py)")
    print("=" * 78)
    print(report["constants"])

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)
        print(f"\n[ok] laporan penuh ditulis ke {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
