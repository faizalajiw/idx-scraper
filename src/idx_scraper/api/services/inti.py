"""Service layer — helper bersama, overview pasar, session movers, dan
watchlist.

Semua perhitungan bisnis API hidup di paket ``services`` ini; UI hanya
menyajikan hasilnya.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from ..database import get_cursor

logger = logging.getLogger(__name__)

WIB = timezone(timedelta(hours=7))


def _today() -> str:
    return datetime.now(WIB).strftime("%Y-%m-%d")


def _norm_date(value: Any) -> str:
    """'YYYYMMDD' -> 'YYYY-MM-DD'; ISO/date passes through as ISO string."""
    s = str(value)
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s[:10] if len(s) >= 10 else s


def _f(v: Any) -> float | None:
    """Coerce Decimals/None/NaN to plain float | None (JSON-safe)."""
    try:
        return float(v) if v is not None and pd.notna(v) else None
    except (TypeError, ValueError):
        return None


def _is_market_hours() -> bool:
    """Sen–Jum, jam bursa WIB (default 09:00–16:00; override via env)."""
    import os

    now = datetime.now(WIB)
    open_h, open_m = map(int, os.getenv("IDX_MARKET_OPEN", "09:00").split(":"))
    close_h, close_m = map(int, os.getenv("IDX_MARKET_CLOSE", "16:00").split(":"))
    t = now.hour * 60 + now.minute
    return (open_h * 60 + open_m) <= t < (close_h * 60 + close_m) and now.weekday() < 5


# --------------------------------------------------------------- overview


def _segment(shares: Any, value: Any, freq: Any) -> dict[str, Any]:
    """Baris agregat -> segment dict dengan volume dalam lembar DAN lot."""
    sh = _f(shares)
    return {
        "volume_shares": sh,
        "volume_lot": (sh / 100.0) if sh is not None else None,
        "value": _f(value),
        "frequency": _f(freq),
    }


def _share(part: Any, whole: Any) -> float | None:
    p, w = _f(part), _f(whole)
    if p is None or not w:
        return None
    return p / w


def get_market_trade_summary(date_str: str | None = None) -> dict[str, Any]:
    """Pasar reguler vs non-reguler (volume lembar/lot + value) satu sesi.

    Sumber: research.market_segment_daily, diisi dari GetStockSummary IDX
    (reguler + non-reguler tiap emiten). Tidak ada kalkulasi baru di sini —
    hanya baca baris yang sudah dihitung worker.
    """
    empty = {
        "date": None,
        "captured_at": None,
        "stock_count": 0,
        "regular": _segment(None, None, None),
        "non_regular": _segment(None, None, None),
        "total": _segment(None, None, None),
        "non_regular_share_volume": None,
        "non_regular_share_value": None,
    }
    try:
        with get_cursor() as cur:
            cur.execute(
                """select trade_date, captured_at, stock_count,
                          regular_volume, regular_value, regular_freq,
                          nonreg_volume, nonreg_value, nonreg_freq,
                          total_volume, total_value
                   from research.market_segment_daily
                   where trade_date = coalesce(%s::date,
                         (select max(trade_date) from research.market_segment_daily))""",
                (date_str,),
            )
            row = cur.fetchone()
    except Exception as e:
        # Tabel belum ada (worker belum pernah menulis) atau tanggal tak valid
        # -> sajikan kosong, bukan 500. Dicatat supaya tak jadi kegagalan senyap.
        logger.warning(f"[warn] market trade summary dilewati: {e}")
        return empty

    if not row or row["trade_date"] is None:
        return empty

    return {
        "date": row["trade_date"].isoformat(),
        "captured_at": row["captured_at"].isoformat() if row["captured_at"] else None,
        "stock_count": int(row["stock_count"] or 0),
        "regular": _segment(row["regular_volume"], row["regular_value"], row["regular_freq"]),
        "non_regular": _segment(row["nonreg_volume"], row["nonreg_value"], row["nonreg_freq"]),
        "total": _segment(row["total_volume"], row["total_value"], None),
        "non_regular_share_volume": _share(row["nonreg_volume"], row["total_volume"]),
        "non_regular_share_value": _share(row["nonreg_value"], row["total_value"]),
    }


# Midnight today in WIB, as timestamptz — bounds "ticks from this session".
# (The trailing ) closes the leading paren: ts column is timestamptz, so the
# naive-local result of date_trunc must be re-tagged to WIB.)
_WIB_DAY_START_SQL = (
    "(date_trunc('day', now() at time zone 'Asia/Jakarta') at time zone 'Asia/Jakarta')"
)

# A session's newest tick older than this means the live sweep is down; fall back
# to EOD rather than showing hours-old prices as if they were live.
_LIVE_MAX_AGE_SEC = 900

_LIVE_MOVERS_SQL = """
with snap as (
    select distinct on (code) code, last
    from research.intraday_ticks
    where ts >= {day_start}
    order by code, ts desc
),
live as (
    select s.code, s.last,
           (select p.prev_close from research.latest_pit p
             where p.code = s.code order by p.trade_date desc limit 1) as prev_close
    from snap s
    where s.last is not null and s.last > 0
),
calc as (
    select code, last,
           case when prev_close > 0
                then round((last - prev_close) / prev_close * 100, 4) end as percent
    from live
)
select code, last as close, percent
from calc
where percent is not null and percent {cmp} 0
order by percent {order}
limit %s
"""


def _live_movers(cur: Any, limit: int = 5) -> dict[str, Any] | None:
    """Top gainers/losers from this session's ticks, or None when unavailable.

    Ticks carry only the last price, so the reference is the prior close from the
    latest EOD session in latest_pit — which during a live session is exactly
    yesterday's close.
    """
    cur.execute(
        f"select max(ts) as mx from research.intraday_ticks where ts >= {_WIB_DAY_START_SQL}"
    )
    row = cur.fetchone()
    if not row or not row["mx"]:
        return None
    newest = row["mx"]
    if (datetime.now(WIB) - newest).total_seconds() > _LIVE_MAX_AGE_SEC:
        return None

    def _run(cmp_: str, order: str) -> list[Any]:
        cur.execute(
            _LIVE_MOVERS_SQL.format(day_start=_WIB_DAY_START_SQL, cmp=cmp_, order=order),
            (limit,),
        )
        return cur.fetchall()

    gainers = _run(">", "desc")
    losers = _run("<", "asc")
    if not gainers and not losers:
        return None
    return {
        "captured_at": newest.isoformat(),
        "top_gainers": gainers,
        "top_losers": losers,
    }


def get_market_overview() -> dict[str, Any]:
    with get_cursor() as cur:
        cur.execute(
            """select close, change, percent, current, captured_at
               from index_quotes where code = 'COMPOSITE'
               and close is not null and close > 0
               order by captured_at desc limit 1"""
        )
        idx = cur.fetchone()

        # Full-market EOD totals from the research superset (latest trade_date).
        cur.execute("select max(trade_date) from research.latest_pit")
        max_date = cur.fetchone()["max"]

        cur.execute(
            """select sum(volume) as total_volume,
                      sum(value)  as total_value,
                      count(distinct code) as stock_count
               from research.latest_pit
               where trade_date = %s""",
            (max_date,),
        )
        totals = cur.fetchone()

        # Live ticks beat the EOD superset while a session is running; the EOD
        # query below stays the fallback once the sweep is stale or the market shut.
        live = _live_movers(cur, 5) if _is_market_hours() else None

        if live:
            gainers, losers = live["top_gainers"], live["top_losers"]
            movers_at, movers_src = live["captured_at"], "intraday"
        else:
            cur.execute(
                """select code, close, percent
                   from research.latest_pit
                   where trade_date = %s
                     and percent is not null and volume > 0 and percent > 0
                   order by percent desc limit 5""",
                (max_date,),
            )
            gainers = cur.fetchall()

            cur.execute(
                """select code, close, percent
                   from research.latest_pit
                   where trade_date = %s
                     and percent is not null and volume > 0 and percent < 0
                   order by percent asc limit 5""",
                (max_date,),
            )
            losers = cur.fetchall()
            movers_at, movers_src = None, "eod"

    index_ov = None
    if idx:
        index_ov = {
            "code": "COMPOSITE",
            "close": _f(idx["close"]),
            "change": _f(idx["change"]),
            "percent": _f(idx["percent"]),
            "current": _f(idx["current"]),
            "captured_at": idx["captured_at"].isoformat() if idx["captured_at"] else None,
        }
    return {
        "index": index_ov,
        "totals": {
            "total_volume": _f(totals["total_volume"]) if totals else None,
            "total_value": _f(totals["total_value"]) if totals else None,
            "stock_count": int(totals["stock_count"]) if totals and totals["stock_count"] else 0,
        },
        "movers_source": movers_src,
        "movers_captured_at": movers_at,
        "top_gainers": [
            {"code": r["code"], "close": _f(r["close"]), "percent": _f(r["percent"])}
            for r in gainers
        ],
        "top_losers": [
            {"code": r["code"], "close": _f(r["close"]), "percent": _f(r["percent"])}
            for r in losers
        ],
    }


# --------------------------------------------------------------- session movers


def get_session_movers() -> dict[str, Any]:
    """Full-market top movers from the research EOD superset (per session close)."""
    with get_cursor() as cur:
        cur.execute("select max(trade_date) as d from research.latest_pit")
        latest = cur.fetchone()
        if not latest or not latest["d"]:
            return {"date": None, "captured_at": None, "top_gainers": [], "top_losers": []}
        date = latest["d"]

        cur.execute(
            "select max(knowledge_date) as c from research.latest_pit where trade_date = %s",
            (date,),
        )
        captured = cur.fetchone()["c"]

        cur.execute(
            """select code, close, percent from research.latest_pit
               where trade_date = %s and percent is not null and volume > 0
               order by percent desc limit 10""",
            (date,),
        )
        gainers = cur.fetchall()

        cur.execute(
            """select code, close, percent from research.latest_pit
               where trade_date = %s and percent is not null and volume > 0
               order by percent asc limit 10""",
            (date,),
        )
        losers = cur.fetchall()

    return {
        "date": _norm_date(date),
        "captured_at": captured.isoformat() if captured else None,
        "top_gainers": [
            {"code": r["code"], "close": _f(r["close"]), "percent": _f(r["percent"])}
            for r in gainers
        ],
        "top_losers": [
            {"code": r["code"], "close": _f(r["close"]), "percent": _f(r["percent"])}
            for r in losers
        ],
    }


# --------------------------------------------------------------- watchlist


def get_watchlist(codes: list[str]) -> list[dict[str, Any]]:
    if not codes:
        return []
    with get_cursor() as cur:
        cur.execute(
            """with latest as (
                   select distinct on (code) code, close, change, percent, volume, foreign_net
                   from research.latest_pit
                   order by code, trade_date desc
               )
               select l.code, l.close, l.change, l.percent,
                      l.volume, l.foreign_net,
                      (select count(*) from research.latest_pit s where s.code = l.code) as hist_days
               from latest l
               where l.code = any(%s)
               order by l.percent desc nulls last""",
            (codes,),
        )
        rows = cur.fetchall()
    return [
        {
            "code": r["code"],
            "close": _f(r["close"]),
            "change": _f(r["change"]),
            "percent": _f(r["percent"]),
            "volume": _f(r["volume"]),
            "foreign_net": _f(r["foreign_net"]),
            "hist_days": int(r["hist_days"]) if r["hist_days"] else 0,
        }
        for r in rows
    ]
