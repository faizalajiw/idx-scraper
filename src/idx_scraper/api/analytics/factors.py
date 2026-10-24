"""factors: pecahan api/analytics.py (FAI-19)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ... import db
from ..database import get_cursor
from ._common import WIB, _f, _latest_index, _research_cache_get, _research_cache_put, logger
from .screener import _latest_orderbook_factors


def get_factors_overview() -> dict[str, Any]:
    """Ringkasan kalibrasi faktor: run terbaru + bobot aktif + histori panjang."""
    cached = _research_cache_get("factors_overview")
    if cached is not None:
        return cached

    with get_cursor() as cur:
        cur.execute("select max(run_date) as d from research.factor_ic_history")
        latest = cur.fetchone()["d"]
        if not latest:
            from ...research.factors import FACTOR_DEFINITIONS

            return {
                "latest_run": None,
                "weights": {},
                "factors": [],
                "history": [],
                "definitions": dict(FACTOR_DEFINITIONS),
            }

        cur.execute(
            """select factor, horizon, mean_ic, icir, t_stat, hit_rate, n_days,
                      eligible, weight
               from research.factor_ic_history
               where run_date = %s
               order by horizon, abs(mean_ic) desc nulls last""",
            (latest,),
        )
        factors = [
            {
                "factor": r["factor"],
                "horizon": int(r["horizon"]),
                "mean_ic": _f(r["mean_ic"]),
                "icir": _f(r["icir"]),
                "t_stat": _f(r["t_stat"]),
                "hit_rate": _f(r["hit_rate"]),
                "n_days": int(r["n_days"]) if r["n_days"] is not None else None,
                "eligible": bool(r["eligible"]),
                "weight": _f(r["weight"]),
            }
            for r in cur.fetchall()
        ]
        cur.execute(
            """select run_date, count(*) as rows, count(*) filter (where eligible) as eligible
               from research.factor_ic_history group by run_date order by run_date desc limit 12"""
        )
        history = [
            {
                "run_date": str(r["run_date"]),
                "rows": int(r["rows"]),
                "eligible": int(r["eligible"]),
            }
            for r in cur.fetchall()
        ]

    # Bobot komposit aktif: dari run terbaru utk horizon acuan (10), fallback
    # konstanta composite kalau tidak ada yang eligible.
    weights = {f["factor"]: f["weight"] for f in factors if f["horizon"] == 10 and f["weight"]}
    if not weights:
        from ...research.composite import FACTOR_WEIGHTS

        weights = {
            "vol_21d": FACTOR_WEIGHTS["vol"],
            "turnover_21d": FACTOR_WEIGHTS["turnover"],
            "dist_52w_high": FACTOR_WEIGHTS["dist_52w"],
        }

    from ...research.factors import FACTOR_DEFINITIONS

    out = {
        "latest_run": str(latest),
        "weights": weights,
        "factors": factors,
        "history": history,
        "definitions": dict(FACTOR_DEFINITIONS),
    }
    _research_cache_put("factors_overview", out)
    return out


def get_sentiment(limit: int = 15) -> dict[str, Any]:
    """Sentimen posisi/aliran dari data yang sudah tersimpan (cache 1 jam).

    Menggabungkan tiga sumber yang sudah kita miliki — arus asing
    (``latest_pit.foreign_net``), ketimpangan & absorption buku intraday
    (``research.orderbook``), dan breadth pasar — menjadi skor emiten
    ``-1..+1`` plus gauge pasar ``0..100``. Tidak ada sumber/scraping baru, dan
    snapshot intraday hari T memang sudah tertutup pada close T (no look-ahead).
    """
    cache_key = f"sentiment:{limit}"
    cached = _research_cache_get(cache_key)
    if cached is not None:
        return cached

    from ...research import sentiment

    dsn = db.dsn()

    with get_cursor() as cur:
        idx = _latest_index(cur)
    index_pct = idx["percent"] if idx else None

    # Order-book adalah enhancement: capture bisa tidak tersedia / tipis.
    try:
        ob = _latest_orderbook_factors()
    except Exception as e:
        logger.warning(f"[warn] sentimen tanpa faktor order-book: {e}")
        ob = {}

    out = sentiment.compute(dsn, index_pct=index_pct, ob_by_code=ob, limit=limit)
    _research_cache_put(cache_key, out)
    return out


def get_signal_track() -> dict[str, Any]:
    """Track record sinyal BUY/SELL dari research.signal_log (cache 1 jam).

    Kalau log masih kosong, backfill dijalankan sekali (idempoten) supaya
    halaman punya isi tanpa harus menunggu job harian. Sinyal dinilai di close
    T+1 dan dibandingkan dengan pasar equal-weight pada window yang sama.
    """
    cached = _research_cache_get("signal_track")
    if cached is not None:
        return cached

    from ...research import signal_log as sl

    dsn = db.dsn()

    try:
        if sl.load_log(dsn).empty:
            sl.backfill(dsn)
    except Exception as e:  # DB belum siap -> tetap sajikan apa adanya
        logger.warning(f"[warn] backfill jejak sinyal dilewati: {e}")

    out = sl.evaluate(dsn, horizons=sl.DEFAULT_HORIZONS)
    out["generated_at"] = datetime.now(WIB).isoformat(timespec="seconds")
    _research_cache_put("signal_track", out)
    return out
