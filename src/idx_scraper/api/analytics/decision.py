"""decision: pecahan api/analytics.py (FAI-19)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..database import get_cursor
from ._common import WIB, _f, _research_cache_get, _research_cache_put, logger
from .broker_flow import get_stock_foreign_flow
from .factors import get_sentiment
from .market import get_market_regime
from .screener import get_stock_events


def _market_return(
    closes: dict[str, list[dict[str, Any]]],
    idx: dict[tuple[str, str], int],
    date: str,
    horizon: int,
) -> float | None:
    """Return pasar equal-weight (persen) untuk window (T+1 -> T+1+horizon)
    yang dimulai setelah tanggal kejadian ``date``. Rata-rata emiten yang
    punya close di kedua ujung. None bila < 5 emiten (sampel terlalu kecil).

    Dipakai untuk alpha: forward return kejadian dikurangi angka ini.
    """
    rets: list[float] = []
    for code, rows in closes.items():
        i = idx.get((code, date))
        if i is None:
            continue
        j = i + 1 + horizon
        if j >= len(rows):
            continue
        entry = rows[i + 1].get("close")
        exit_ = rows[j].get("close")
        if entry is None or exit_ is None or entry <= 0:
            continue
        rets.append((exit_ / entry - 1.0) * 100.0)
    if len(rets) < 5:
        return None
    return sum(rets) / len(rets)


def get_stock_decision(code: str) -> dict[str, Any]:
    """Ruang Keputusan satu emiten: verdict gabungan + level pembatalan.

    Satu halaman untuk menjawab "apa posisi saya terhadap emiten ini": verdict
    hold-check (teknikal + valuasi + lapisan IC/broker) dijadikan "dasar",
    lalu diperkaya konteks yang TIDAK mengubah verdict: regime IHSG, sentimen
    aliran (arus asing + order-book), jejak asing 10 sesi, base rate event
    study, dan level invalidasi teknikal yang bisa dipegang user awam.

    Semua lapisan dibaca dari sumber yang sudah ada (hold-check, sentimen,
    event study, regime) — tidak ada hitungan baru yang klaim presisi.
    Cache 30 menit: verdict bergerak harian, konteks intraday cukup segar.
    """
    code = code.strip().upper()
    cache_key = f"stock_decision:{code}"
    cached = _research_cache_get(cache_key)
    if cached is not None:
        return cached

    from ..services import get_hold_check

    # --- 1) verdict dasar: satu baris hold-check untuk emiten ini ----------
    hold = get_hold_check([code])
    hc = hold[0] if hold else None

    # --- 2) regime pasar ---------------------------------------------------
    regime = get_market_regime()

    # --- 3) sentimen aliran emiten (semua items, termasuk netral) ----------
    sent_row: dict[str, Any] | None = None
    market_gauge: dict[str, Any] | None = None
    try:
        sent = get_sentiment(limit=15)
        market_gauge = sent.get("market")
        items = sent.get("items") or []
        for it in items:
            if str(it.get("code")).upper() == code:
                sent_row = it
                break
    except Exception as e:
        logger.warning(f"[warn] sentimen dilewati di decision: {e}")

    # --- 4) jejak asing 10 sesi (dari modul pure foreign_flow) -------------
    foreign_10: list[dict[str, Any]] = []
    flip: dict[str, Any] | None = None
    try:
        ff = get_stock_foreign_flow(code, days=10, peer_days=10, peer_limit=3)
        foreign_10 = [
            {"date": p["date"], "net": p["net"], "flip": p["flip"]} for p in ff.get("flow", [])
        ]
        flip = ff.get("flip_summary", {}).get("last_flip")
    except Exception as e:
        logger.warning(f"[warn] foreign flow dilewati di decision: {e}")

    # --- 5) base rate event study emiten -----------------------------------
    events: list[dict[str, Any]] = []
    try:
        ev = get_stock_events(code)
        if ev:
            events = ev.get("events", [])
    except Exception as e:
        logger.warning(f"[warn] event study dilewati di decision: {e}")

    # --- 6) level invalidasi teknikal (pure dari frame emiten) -------------
    invalidation: dict[str, Any] | None = None
    try:
        with get_cursor() as cur:
            df, _ = _load_frame_for_decision(cur, code)
        if not df.empty:
            from ...analysis import calculate_indicators

            ind = calculate_indicators(df.copy())
            last = ind.iloc[-1]
            close = _f(last["close"])
            ma_l = _f(last.get("MA_Long"))
            bb_l = _f(last.get("BB_Lower"))
            window = ind["close"].tail(60)
            mean = _f(window.mean())
            std = _f(window.std())
            rsi = _f(last.get("RSI"))
            if close is not None:
                invalidation = {
                    "close": close,
                    "ma50": ma_l,
                    "bb_lower": bb_l,
                    "mean60": mean,
                    "std60": std,
                    "rsi": rsi,
                    # Level & deskripsi untuk user awam; None bila tak bisa
                    # dihitung (histori terlalu pendek).
                    "ma50_level": round(ma_l, 1) if ma_l else None,
                    "mean60_level": round(mean, 1) if mean else None,
                }
    except Exception as e:
        logger.warning(f"[warn] level invalidasi dilewati di decision: {e}")

    out: dict[str, Any] = {
        "code": code,
        "generated_at": datetime.now(WIB).isoformat(timespec="seconds"),
        "hold_check": hc,
        "regime": regime,
        "sentiment": sent_row,
        "market_gauge": market_gauge,
        "foreign_10": foreign_10,
        "last_flip": flip,
        "events": events,
        "invalidation": invalidation,
    }
    _research_cache_put(cache_key, out)
    return out


def _load_frame_for_decision(cur: Any, code: str):
    """Frame OHLC emiten via services._build_frame (import malas hindari siklus)."""
    from ..services import _build_frame

    return _build_frame(cur, code)


def get_stock_ownership(code: str) -> dict[str, Any]:
    """Kepemilikan emiten + aksi pemilik (gratis, keterbukaan IDX).

    Baca ``research.ownership``: snapshot terbaru (komposisi pemilik + free
    float) dan snapshot sebelumnya untuk diff **aksi pemilik** (siapa
    menambah/mengurangi/masuk/keluar). Logika derive pure di
    ``ownership.py``. Cache 1 jam (data berubah lambat / bulanan).

    Returns ``{code, has_data, as_of, prev_date, holders, free_float_pct,
    controller, changes}``. Tidak pernah raise; tabel belum ada -> has_data
    False.
    """
    from ..ownership import controller_names, free_float_pct, owner_changes

    code = code.upper()
    cache_key = f"ownership:{code}"
    cached = _research_cache_get(cache_key)
    if cached is not None:
        return cached

    empty: dict[str, Any] = {
        "code": code,
        "has_data": False,
        "as_of": None,
        "prev_date": None,
        "holders": [],
        "free_float_pct": None,
        "controller": [],
        "changes": [],
    }

    with get_cursor() as cur:
        cur.execute("select to_regclass('research.ownership') as t")
        row = cur.fetchone()
        if row is None or row["t"] is None:
            _research_cache_put(cache_key, empty, ttl=600.0)
            return empty

        cur.execute(
            "select max(snapshot_date) as d from research.ownership where code = %s",
            (code,),
        )
        latest = cur.fetchone()
        latest_date = latest["d"] if latest else None
        if latest_date is None:
            _research_cache_put(cache_key, empty, ttl=600.0)
            return empty

        cur.execute(
            """select holder_name, category, shares, pct, is_controller
               from research.ownership
               where code = %s and snapshot_date = %s
               order by pct desc nulls last""",
            (code, latest_date),
        )
        holders = [
            {
                "holder_name": r["holder_name"],
                "category": r["category"],
                "shares": _f(r["shares"]),
                "pct": _f(r["pct"]),
                "is_controller": bool(r["is_controller"]),
            }
            for r in cur.fetchall()
        ]

        cur.execute(
            """select max(snapshot_date) as d from research.ownership
               where code = %s and snapshot_date < %s""",
            (code, latest_date),
        )
        prev_row = cur.fetchone()
        prev_date = prev_row["d"] if prev_row else None
        prev_holders: list[dict[str, Any]] = []
        if prev_date is not None:
            cur.execute(
                """select holder_name, category, shares, pct, is_controller
                   from research.ownership
                   where code = %s and snapshot_date = %s""",
                (code, prev_date),
            )
            prev_holders = [
                {
                    "holder_name": r["holder_name"],
                    "category": r["category"],
                    "shares": _f(r["shares"]),
                    "pct": _f(r["pct"]),
                    "is_controller": bool(r["is_controller"]),
                }
                for r in cur.fetchall()
            ]

    out = {
        "code": code,
        "has_data": bool(holders),
        "as_of": str(latest_date),
        "prev_date": str(prev_date) if prev_date is not None else None,
        "holders": holders,
        "free_float_pct": free_float_pct(holders),
        "controller": controller_names(holders),
        "changes": owner_changes(prev_holders, holders) if prev_date is not None else [],
    }
    _research_cache_put(cache_key, out, ttl=3600.0)
    return out
