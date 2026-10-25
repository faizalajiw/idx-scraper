"""Universe + fitur kandidat (pure)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ..recommend import (
    MIN_DAY_VALUE,
    MIN_MARKET_HISTORY,
    candidate_features,
    entry_plan,
)
from ..signal_log import attach_outcomes
from .param import _ABN, _FEATURES


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
