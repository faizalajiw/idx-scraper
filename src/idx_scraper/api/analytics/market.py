"""market: pecahan api/analytics.py (FAI-19)."""

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Any

import pandas as pd

from ... import db
from ...analysis import calculate_adx, calculate_indicators, classify_regime
from ...freshness import expected_session
from ...research.regime import gate
from ..database import get_cursor
from ..sector_map import sector_for
from ._common import WIB, _f, _latest_eod_date, _research_cache_get, _research_cache_put


def get_market_regime() -> dict[str, Any]:
    """Regime IHSG (TRENDING/RANGING/TRANSITION + volatilitas) dari ADX(14).

    Sumber: ``index_summary_daily`` (close resmi harian, di-backfill dari
    Yahoo). Fallback: medan breadth equal-weight dari ``research.latest_pit``
    kalau seri indeks terlalu pendek. Output dipakai banner konteks di semua
    halaman analisis.
    """
    with get_cursor() as cur:
        cur.execute(
            """select date, open, high, low, close
               from index_summary_daily where code = 'COMPOSITE'
               order by date"""
        )
        rows = cur.fetchall()

    df = pd.DataFrame(
        [
            (str(r["date"]), _f(r["open"]), _f(r["high"]), _f(r["low"]), _f(r["close"]))
            for r in rows
        ],
        columns=["date", "open", "high", "low", "close"],
    )
    source = "index_summary_daily"
    if len(df) < 40:  # ADX(14) + smoothing butuh sejarah yang layak
        source = "breadth_latest_pit"
        with get_cursor() as cur:
            cur.execute(
                """select trade_date as date,
                          avg(close) as close,
                          avg(close) as high,
                          avg(close) as low
                   from research.latest_pit
                   group by trade_date order by trade_date"""
            )
            rows = cur.fetchall()
        df = pd.DataFrame(
            [
                (str(r["date"]), _f(r["close"]), _f(r["close"]), _f(r["close"]), _f(r["close"]))
                for r in rows
            ],
            columns=["date", "open", "high", "low", "close"],
        )

    if df.empty:
        out: dict[str, Any] = {
            "regime": None,
            "source": None,
            "as_of": None,
            "generated_at": datetime.now(WIB).isoformat(timespec="seconds"),
        }
        out["gate"] = _gate_for(out, datetime.now(WIB))
        return out

    adx_df = calculate_adx(df)
    out = classify_regime(adx_df)
    out["source"] = source
    out["as_of"] = df["date"].iloc[-1][:10]
    out["generated_at"] = datetime.now(WIB).isoformat(timespec="seconds")
    # Nama regime diwarnai UI; arah tren penting untuk konteks sinyal.
    out["dir_hint"] = (
        "up"
        if out["regime"] == "TRENDING_UP"
        else "down"
        if out["regime"] == "TRENDING_DOWN"
        else None
    )
    out["gate"] = _gate_for(out, datetime.now(WIB))
    return out


def _gate_for(regime: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Gerbang rezim untuk banner: sesi indeks basi tidak pernah memblokir (D1).

    Dipisah dari `get_market_regime` supaya aturannya bisa diuji tanpa DB
    dan tanpa jam palsu.
    """
    as_of = regime.get("as_of")
    expected = expected_session(now).isoformat()
    return gate(
        regime.get("regime"),
        regime.get("adx"),
        regime.get("vol_state"),
        regime_fresh=bool(as_of) and as_of >= expected,
    )


def get_regime_history(days: int = 90) -> dict[str, Any]:
    """Histori regime harian + agregat jangka panjang dari research.regime_daily."""
    dsn = db.dsn()

    from ...research.regime import backfill, summary

    # Idempoten & murah: backfill menghitung ulang dari index_summary_daily
    # (SQL window, sekilas saja) sehingga histori selalu terkini tanpa job.
    # Tabel belum siap -> tetap sajikan yang ada.
    with contextlib.suppress(Exception):
        backfill(dsn)

    with db.connect(timeout=10) as conn, conn.cursor() as cur:
        cur.execute(
            """select trade_date, regime, adx, realized_vol
               from research.regime_daily where regime is not null
               order by trade_date desc limit %s""",
            (int(days),),
        )
        rows = [
            {
                "date": str(r[0]),
                "regime": r[1],
                "adx": _f(r[2]),
                "realized_vol": _f(r[3]),
            }
            for r in cur.fetchall()
        ]
    rows.reverse()  # urut naik utk timeline
    return {
        "recent": rows,
        "summary": summary(dsn),
        "generated_at": datetime.now(WIB).isoformat(timespec="seconds"),
    }


def get_sector_analysis(date: str | None = None) -> dict[str, Any]:
    """Average % change, value, and foreign net grouped by sector (curated map)."""
    with get_cursor() as cur:
        d = date or _latest_eod_date(cur)
        if not d:
            return {"date": None, "sectors": []}
        cur.execute(
            """select code, name, percent, value, foreign_net
               from research.latest_pit where trade_date = %s""",
            (d,),
        )
        rows = cur.fetchall()

    buckets: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        sector = sector_for(r["code"])
        buckets.setdefault(sector, []).append(r)

    sectors: list[dict[str, Any]] = []
    for sector, items in buckets.items():
        pcts = [_f(i["percent"]) for i in items if _f(i["percent"]) is not None]
        values = [_f(i["value"]) for i in items if _f(i["value"]) is not None]
        fnets = [_f(i["foreign_net"]) for i in items if _f(i["foreign_net"]) is not None]
        top = max(
            (i for i in items if _f(i["percent"]) is not None),
            key=lambda i: _f(i["percent"]) or 0,
            default=None,
        )
        sectors.append(
            {
                "sector": sector,
                "stock_count": len(items),
                "avg_percent": round(sum(pcts) / len(pcts), 2) if pcts else None,
                "total_value": sum(values) if values else None,
                "total_foreign_net": sum(fnets) if fnets else None,
                "gainers": sum(1 for p in pcts if p > 0),
                "losers": sum(1 for p in pcts if p < 0),
                "top_stock": {"code": top["code"], "percent": _f(top["percent"])} if top else None,
            }
        )

    sectors.sort(
        key=lambda s: s["avg_percent"] if s["avg_percent"] is not None else -999, reverse=True
    )
    return {"date": d, "sectors": sectors}


def _frame_from_rows(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """OHLCV frame dari baris research.latest_pit yang sudah terurut tanggal."""
    df = pd.DataFrame(
        [
            (str(r["trade_date"]), _f(r["close"]), _f(r["high"]), _f(r["low"]), _f(r["volume"]))
            for r in rows
        ],
        columns=["date", "close", "high", "low", "volume"],
    )
    df["close"] = pd.to_numeric(df["close"])
    return df


def _valuation_frame(cur: Any, code: str) -> pd.DataFrame | None:
    cur.execute(
        """select trade_date as date, close, high, low, volume from research.latest_pit
           where code = %s order by trade_date""",
        (code,),
    )
    rows = cur.fetchall()
    if len(rows) < 30:
        return None
    df = pd.DataFrame(
        [
            (str(r["date"]), _f(r["close"]), _f(r["high"]), _f(r["low"]), _f(r["volume"]))
            for r in rows
        ],
        columns=["date", "close", "high", "low", "volume"],
    )
    df["close"] = pd.to_numeric(df["close"])
    return df


def get_valuation(min_days: int = 40) -> dict[str, Any]:
    """Classify watchlist stocks as potentially under/overvalued vs their own trend.

    No fundamentals are available from free IDX endpoints, so "undervalued" here
    means: price is stretched below its own statistical band (z-score of price
    vs 60-day mean is low) while the medium trend is intact — i.e. quality dip.
    "Overvalued" is the mirror image: price far above band with stretched RSI.
    This is a mean-reversion *screen*, not a fairness opinion.
    """
    cached = _research_cache_get("valuation")
    if cached is not None:
        return cached

    with get_cursor() as cur:
        cur.execute("select distinct code from research.latest_pit order by code")
        codes = [r["code"] for r in cur.fetchall()]

    undervalued: list[dict[str, Any]] = []
    overvalued: list[dict[str, Any]] = []

    with get_cursor() as cur:
        for code in codes:
            df = _valuation_frame(cur, code)
            if df is None or len(df) < min_days:
                continue
            close = df["close"]
            window = close.tail(60)
            mean = window.mean()
            std = window.std()
            if not std or pd.isna(std) or mean <= 0:
                continue
            z = (close.iloc[-1] - mean) / std

            # Momentum & RSI for context
            momo = (close.iloc[-1] / close.iloc[-21] - 1) * 100 if len(close) > 21 else None
            dfi = calculate_indicators(
                df.rename(columns={}).assign(open=df["close"], high=df["close"], low=df["close"])
            )
            rsi = _f(dfi["RSI"].iloc[-1])
            trend_up = (
                bool(dfi["MA_Short"].iloc[-1] > dfi["MA_Long"].iloc[-1]) if len(dfi) > 50 else None
            )

            row = {
                "code": code,
                "name": None,
                "close": _f(close.iloc[-1]),
                "z_score": round(float(z), 2),
                "momentum_pct": round(float(momo), 2) if momo is not None else None,
                "rsi": rsi,
                "trend_up": trend_up if trend_up is not None else False,
                "target_price": round(float(mean), 2),
            }
            if z <= -1.0:
                undervalued.append(row)
            elif z >= 1.5:
                overvalued.append(row)

    undervalued.sort(key=lambda r: r["z_score"])
    overvalued.sort(key=lambda r: r["z_score"], reverse=True)

    with get_cursor() as cur:
        cur.execute("select max(trade_date) from research.latest_pit")
        max_date = cur.fetchone()["max"]

        cur.execute(
            """select distinct on (code) code, name from research.latest_pit
               where trade_date = %s
               order by code, knowledge_date desc""",
            (max_date,),
        )
        names = {r["code"]: r["name"] for r in cur.fetchall()}
    for lst in (undervalued, overvalued):
        for r in lst:
            r["name"] = names.get(r["code"])

    result = {"undervalued": undervalued, "overvalued": overvalued}
    # ~10s saat dingin (loop per emiten); data hanya berubah tiap EOD, dan
    # serve loop memanggil /api/cache/clear setiap pipeline refresh.
    _research_cache_put("valuation", result, ttl=600.0)
    return result
