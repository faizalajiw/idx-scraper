"""broker_activity: pecahan api/analytics.py (FAI-19)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from ... import db
from ..database import get_cursor
from ._common import WIB, _f, _research_cache_get, _research_cache_put

# Jendela histori yang dibangun ulang untuk faktor aliran. 260 hari kalender
# (~178 hari bursa) menutup rolling 252 hari momentum DAN warmup 21 hari flow;
# panel IC penuh (~18 bulan) terlalu mahal untuk dibangun di jalur request.
_BROKER_ACTIVITY_LOOKBACK_DAYS = 260


def _broker_activity_empty(market: dict[str, Any], reason: str) -> dict[str, Any]:
    """Payload saat skor belum bisa dibentuk — eksplisit, tanpa angka rekaan."""
    return {
        "as_of": None,
        "validated": False,
        "reason": reason,
        "horizon": None,
        "ic_run_date": None,
        "eligible_count": 0,
        "factors": [],
        "rows": [],
        "market": market,
    }


def _broker_activity_meta(codes: list[str], as_of: Any) -> dict[str, dict[str, Any]]:
    """Nama + perubahan harga untuk emiten terpilih (satu query, bukan per kode)."""
    if not codes or as_of is None:
        return {}
    day = as_of.date() if hasattr(as_of, "date") else as_of
    with get_cursor() as cur:
        cur.execute(
            """select code, name, close, change, percent
               from research.latest_pit
               where trade_date = %s and code = any(%s)""",
            (day, codes),
        )
        return {
            str(r["code"]).upper(): {
                "name": r["name"],
                "close": _f(r["close"]),
                "change": _f(r["change"]),
                "percent": _f(r["percent"]),
            }
            for r in cur.fetchall()
        }


def broker_activity_snapshot() -> dict[str, Any]:
    """Snapshot skor aktivitas broker terbaru — SATU hitung untuk semua konsumen.

    Dipakai tiga tempat: halaman Aktivitas Broker (`get_broker_activity`), filter
    Screener (`min_broker_score`), dan lapisan verdict Hold Check. Sengaja satu
    sumber supaya angka di ketiga halaman tidak pernah berbeda. Cache 1 jam
    (in-process, di-reset `clear_research_cache` tiap pipeline refresh).

    Returns dict:

    - ``validated``     : ada >= 1 faktor aliran yang lolos gate IC
    - ``reason``        : kenapa belum tervalidasi (None bila tervalidasi)
    - ``factors``       : hasil ``select_factors`` (termasuk yang gagal gate)
    - ``scores``        : DataFrame code/close/score/coverage/drivers; KOSONG bila
                          belum tervalidasi — jangan dipakai sebagai skor 0
    - ``by_code``       : {CODE: {score, coverage, drivers}} untuk lookup cepat

    Skor hanya dari faktor yang lolos gate (|mean IC| >= 0,05 & |ICIR| >= 0,5),
    arah mengikuti tanda IC — lihat ``research.broker_activity``.
    """
    cached = _research_cache_get("broker_activity_snapshot")
    if cached is not None:
        return cached

    from ...research import broker_activity as ba
    from ...research.ic_history import WEIGHT_HORIZON

    horizon = WEIGHT_HORIZON  # horizon acuan bobot, sama dengan IC bulanan
    snap: dict[str, Any] = {
        "as_of": None,
        "horizon": horizon,
        "ic_run_date": None,
        "validated": False,
        "reason": (
            "Belum ada run IC tersimpan. Jalankan `idx ic` agar faktor aliran "
            "bisa diuji dan diberi bobot."
        ),
        "eligible_count": 0,
        "factors": [],
        "scores": pd.DataFrame(),
        "by_code": {},
        # Panel penuh disimpan supaya endpoint per-emiten bisa menghitung riwayat
        # per tanggal TANPA query ulang. Preseden yang sama: _event_study_bundle
        # juga menyimpan panelnya di cache.
        "panel": pd.DataFrame(),
    }

    dsn = db.dsn() if db.tersedia() else None
    if not dsn:
        snap["reason"] = "DATABASE_URL belum diisi"
        _research_cache_put("broker_activity_snapshot", snap)
        return snap

    # 1) Baris IC run terbaru -> faktor mana yang lolos gate.
    latest_run = None
    ic_rows: list[dict[str, Any]] = []
    with get_cursor() as cur:
        cur.execute("select to_regclass('research.factor_ic_history') as t")
        reg = cur.fetchone()
        if reg and reg["t"]:
            cur.execute("select max(run_date) as d from research.factor_ic_history")
            row = cur.fetchone()
            latest_run = row["d"] if row else None
        if latest_run:
            cur.execute(
                """select factor, horizon, mean_ic, icir, t_stat, hit_rate, n_days
                   from research.factor_ic_history
                   where run_date = %s""",
                (latest_run,),
            )
            ic_rows = [
                {
                    "factor": r["factor"],
                    "horizon": int(r["horizon"]),
                    "mean_ic": _f(r["mean_ic"]),
                    "icir": _f(r["icir"]),
                    "t_stat": _f(r["t_stat"]),
                    "hit_rate": _f(r["hit_rate"]),
                    "n_days": int(r["n_days"]) if r["n_days"] is not None else None,
                }
                for r in cur.fetchall()
            ]

    snap["ic_run_date"] = str(latest_run) if latest_run else None
    selected = ba.select_factors(ic_rows, horizon=horizon)
    snap["factors"] = selected

    active = ba.eligible_factors(selected)
    if not active:
        if ic_rows:
            snap["reason"] = (
                "Tidak ada faktor aliran/order-book yang lolos gate "
                "(|IC| >= 0,05 & |ICIR| >= 0,5) pada run IC terakhir, jadi skor "
                "akumulasi belum bisa dibentuk."
            )
        _research_cache_put("broker_activity_snapshot", snap)
        return snap

    # 2) Nilai faktor terbaru per emiten (satu hari bursa untuk semua kode).
    from ...research.ic import build_factor_panel

    start = (datetime.now(WIB) - timedelta(days=_BROKER_ACTIVITY_LOOKBACK_DAYS)).strftime(
        "%Y-%m-%d"
    )
    panel = build_factor_panel(dsn, start=start, min_history=60)
    latest, as_of = ba.latest_factor_rows(panel, [*ba.BROKER_FACTORS, "close"])
    scored = ba.composite_scores(latest, selected)

    snap["validated"] = True
    snap["eligible_count"] = len(active)
    snap["as_of"] = as_of
    snap["scores"] = scored
    snap["panel"] = panel
    snap["by_code"] = {
        str(r["code"]): {
            "score": round(float(r["score"]), 1),
            "coverage": round(float(r["coverage"]), 2),
            "drivers": list(r["drivers"]),
        }
        for _, r in scored.iterrows()
    }
    if scored.empty:
        # Tervalidasi tapi tidak ada emiten dengan histori cukup: skor tetap
        # kosong (bukan 0) — konsumen harus memperlakukannya sebagai "tidak ada".
        snap["reason"] = (
            "Faktor sudah tervalidasi, tapi belum ada emiten dengan cukup data "
            "aliran untuk dihitung (butuh histori flow >= 21 hari)."
        )
    _research_cache_put("broker_activity_snapshot", snap)
    return snap


def broker_scores_by_code() -> tuple[dict[str, dict[str, Any]], bool]:
    """Skor broker terbaru per emiten + status validasi (Screener & Hold Check).

    Nilai balikannya read-only: dict dalam snapshot di-cache dan dipakai bersama.
    """
    snap = broker_activity_snapshot()
    return dict(snap.get("by_code") or {}), bool(snap.get("validated"))


def get_broker_activity(limit: int = 25) -> dict[str, Any]:
    """Skor aktivitas broker per emiten + konsentrasi broker pasar.

    Membaca ``broker_activity_snapshot`` (satu hitung bersama Screener & Hold
    Check) lalu menambahkan nama/perubahan harga, urutan, dan struktur broker
    pasar. Kalau skor belum tervalidasi, ``validated`` False dan ``rows`` kosong:
    halaman menampilkan status "belum tervalidasi" alih-alih skor tanpa dasar.
    """
    limit = max(1, min(limit, 200))
    cache_key = f"broker_activity:{limit}"
    cached = _research_cache_get(cache_key)
    if cached is not None:
        return cached

    from ..services import get_broker_concentration

    market = get_broker_concentration(limit=10)
    snap = broker_activity_snapshot()
    scored: pd.DataFrame = snap["scores"]
    as_of = snap["as_of"]

    def _empty(reason: str) -> dict[str, Any]:
        out = _broker_activity_empty(market, reason)
        out["ic_run_date"] = snap["ic_run_date"]
        out["horizon"] = snap["horizon"]
        out["factors"] = snap["factors"]
        out["eligible_count"] = snap["eligible_count"]
        out["as_of"] = str(as_of.date()) if as_of is not None else None
        _research_cache_put(cache_key, out)
        return out

    if not snap["validated"]:
        return _empty(snap["reason"] or "Skor aktivitas broker belum tervalidasi.")
    if scored.empty:
        return _empty(snap["reason"] or "Belum ada emiten dengan data aliran cukup.")

    top = scored.sort_values("score", ascending=False).head(limit)
    meta = _broker_activity_meta(top["code"].tolist(), as_of)

    rows = []
    for _, r in top.iterrows():
        code = str(r["code"])
        m = meta.get(code, {})
        rows.append(
            {
                "code": code,
                "name": m.get("name"),
                "close": m.get("close") if m.get("close") is not None else _f(r["close"]),
                "change": m.get("change"),
                "percent": m.get("percent"),
                "score": round(float(r["score"]), 1),
                "coverage": round(float(r["coverage"]), 2),
                "drivers": list(r["drivers"]),
            }
        )

    out = {
        "as_of": str(as_of.date()) if as_of is not None else None,
        "validated": True,
        "reason": None,
        "horizon": snap["horizon"],
        "ic_run_date": snap["ic_run_date"],
        "eligible_count": snap["eligible_count"],
        "factors": snap["factors"],
        "rows": rows,
        "market": market,
    }
    _research_cache_put(cache_key, out)
    return out


def get_stock_broker_activity(code: str, lookback: int = 60) -> dict[str, Any]:
    """Skor aktivitas broker SATU emiten + riwayat skor & driver-nya.

    Nilai terkini dibaca dari snapshot pasar supaya PERSIS sama dengan angka di
    halaman ranking. Riwayatnya dihitung ulang per tanggal dari panel yang sama
    (``research.broker_activity.composite_score_history``) — tiap tanggal
    di-score terhadap pasar HARI ITU, jadi garis riwayatnya sebanding antar waktu
    dan tidak tergeser oleh perubahan pasar hari ini.

    Emiten yang tidak masuk cross-section terakhir tetap mendapat riwayat;
    ``current`` None berarti "tidak ada skor untuk hari terakhir", bukan nol.
    """
    code = code.strip().upper()
    lookback = max(2, min(lookback, 250))
    cache_key = f"broker_activity_stock:{code}:{lookback}"
    cached = _research_cache_get(cache_key)
    if cached is not None:
        return cached

    from ...research import broker_activity as ba

    snap = broker_activity_snapshot()

    # Impor lokal: sector.py memanggil balik broker_activity (snapshot/meta);
    # impor modul-level akan melingkar.
    from .sector import _sector_peers

    as_of = snap["as_of"]
    out: dict[str, Any] = {
        "code": code,
        "as_of": str(as_of.date()) if as_of is not None else None,
        "validated": bool(snap["validated"]),
        "reason": snap["reason"],
        "horizon": snap["horizon"],
        "ic_run_date": snap["ic_run_date"],
        "eligible_count": snap["eligible_count"],
        "sector": None,
        "current": None,
        "history": [],
    }

    if not snap["validated"]:
        _research_cache_put(cache_key, out)
        return out

    history = ba.composite_score_history(snap["panel"], snap["factors"], code, lookback=lookback)
    out["history"] = [
        {
            "date": str(pd.Timestamp(r["date"]).date()),
            "score": round(float(r["score"]), 1),
            "coverage": round(float(r["coverage"]), 2),
            "drivers": list(r["drivers"]),
            # Baseline pasar hari itu — garis pembanding di chart timeline.
            "market_median": round(float(r["market_median"]), 1)
            if r.get("market_median") is not None and pd.notna(r["market_median"])
            else None,
        }
        for _, r in history.iterrows()
    ]

    scores: pd.DataFrame = snap["scores"]
    if not scores.empty and (scores["code"] == code).any():
        ordered = scores.sort_values("score", ascending=False).reset_index(drop=True)
        universe = len(ordered)
        rank = int(ordered.index[ordered["code"] == code][0]) + 1
        row = ordered.loc[rank - 1]
        out["current"] = {
            "score": round(float(row["score"]), 1),
            "coverage": round(float(row["coverage"]), 2),
            "rank": rank,
            "universe": universe,
            # 1.0 = peringkat teratas; 0.0 = terbawah.
            "percentile": round(1.0 - (rank - 1) / max(universe, 1), 4),
            "drivers": list(row["drivers"]),
        }
        out["sector"] = _sector_peers(code, scores, as_of)

    _research_cache_put(cache_key, out)
    return out
