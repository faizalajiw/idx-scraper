"""Service layer: all business/data calculations for the API.

Implements the aggregations and technical-analytics semantics in parameterized
Postgres queries; the frontend dashboard (idx-web, Recharts) only presents this
data and performs no calculations of its own.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from ..analysis import calculate_indicators, generate_signal, is_mechanical_sell
from .database import get_cursor

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
        print(f"[warn] market trade summary dilewati: {e}", file=sys.stderr)
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
            _LIVE_MOVERS_SQL.format(
                day_start=_WIB_DAY_START_SQL, cmp=cmp_, order=order
            ),
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
        max_date = cur.fetchone()[0]

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


# --------------------------------------------------------------- market leaders

# Metric -> (intraday tick column, EOD raw_eod column). Frequency has no
# intraday source (ticks only store volume/value), so it is always EOD.
_LEADER_METRICS: dict[str, tuple[str | None, str]] = {
    "volume": ("volume", "volume"),
    "value": ("value", "value"),
    "frequency": (None, "frequency"),
}

def _leaders_eod(cur: Any, metric: str, limit: int) -> dict[str, Any]:
    """Top-N emiten by metric from the latest EOD trade_date (raw_eod)."""
    col = _LEADER_METRICS[metric][1]
    cur.execute("select max(trade_date) as d from research.raw_eod")
    row = cur.fetchone()
    if not row or not row["d"]:
        return {"metric": metric, "source": "eod", "date": None, "captured_at": None, "rows": []}
    date = row["d"]
    # distinct on (code): raw_eod may hold multiple knowledge rows per trade_date.
    cur.execute(
        f"""with latest as (
                select distinct on (code)
                       code, name, close, prev_close, volume, value, frequency, ingested_at
                from research.raw_eod
                where trade_date = %s
                order by code, ingested_at desc
            )
            select code, name, close, prev_close, volume, value, frequency, ingested_at,
                   case when prev_close is not null and prev_close <> 0
                        then (close - prev_close) / prev_close * 100 end as percent
            from latest
            where {col} is not null and {col} > 0
            order by {col} desc
            limit %s""",
        (date, limit),
    )
    rows = cur.fetchall()
    captured = max((r["ingested_at"] for r in rows if r["ingested_at"]), default=None)
    return {
        "metric": metric,
        "source": "eod",
        "date": _norm_date(date),
        "captured_at": captured.isoformat() if captured else None,
        "rows": [
            {
                "code": r["code"],
                "name": r["name"],
                "close": _f(r["close"]),
                "percent": _f(r["percent"]),
                "volume": _f(r["volume"]),
                "value": _f(r["value"]),
                "frequency": _f(r["frequency"]),
            }
            for r in rows
        ],
    }

def _leaders_intraday(cur: Any, metric: str, limit: int) -> dict[str, Any] | None:
    """Top-N by cumulative day metric from the freshest intraday snapshot.

    Ticks store cumulative volume/value, so the newest ts per code already holds
    the day-to-date total. Restricted to this WIB session and to ticks that are
    still fresh, so a dead sweep falls back to EOD instead of serving old prices
    as if they were live. The reference close is the most recent EOD close, which
    during a session is the prior close today's move is measured against.
    """
    col = _LEADER_METRICS[metric][0]
    if col is None:
        return None
    cur.execute(
        f"select max(ts) as mx from research.intraday_ticks where ts >= {_WIB_DAY_START_SQL}"
    )
    row = cur.fetchone()
    if not row or not row["mx"]:
        return None
    latest_ts = row["mx"]
    if (datetime.now(WIB) - latest_ts).total_seconds() > _LIVE_MAX_AGE_SEC:
        return None
    cur.execute(
        f"""with snap as (
                select distinct on (code) code, last, volume, value
                from research.intraday_ticks
                where ts >= {_WIB_DAY_START_SQL}
                order by code, ts desc
            )
            select s.code, s.last, s.volume, s.value,
                   lp.name, lp.ref_close
            from snap s
            left join lateral (
                select name, close as ref_close
                from research.latest_pit p
                where p.code = s.code
                order by trade_date desc limit 1
            ) lp on true
            where s.{col} is not null and s.{col} > 0
            order by s.{col} desc
            limit %s""",
        (limit,),
    )
    rows = cur.fetchall()
    return {
        "metric": metric,
        "source": "intraday",
        "date": _today(),
        "captured_at": latest_ts.isoformat(),
        "rows": [
            {
                "code": r["code"],
                "name": r["name"],
                "close": _f(r["last"]),
                "percent": (
                    _f((float(r["last"]) - float(r["ref_close"])) / float(r["ref_close"]) * 100)
                    if r["last"] is not None and r["ref_close"] not in (None, 0)
                    else None
                ),
                "volume": _f(r["volume"]),
                "value": _f(r["value"]),
                "frequency": None,  # not captured intraday
            }
            for r in rows
        ],
    }

def get_market_leaders(metric: str, limit: int = 5) -> dict[str, Any]:
    """Top-N emiten by volume/value/frequency; realtime during market hours,
    else the latest EOD session. Frequency is EOD-only (no intraday source)."""
    metric = metric.lower()
    if metric not in _LEADER_METRICS:
        metric = "volume"
    limit = max(1, min(limit, 50))
    with get_cursor() as cur:
        if _is_market_hours():
            live = _leaders_intraday(cur, metric, limit)
            if live and live["rows"]:
                return live
        return _leaders_eod(cur, metric, limit)


# --------------------------------------------------------------- top brokers

def get_top_brokers(limit: int = 5) -> dict[str, Any]:
    """Top-N broker firms by traded value from the latest broker_daily session.

    broker_daily is EOD-only (IDX GetBrokerSummary). Returns an empty rows list
    (never raises) when the table/data is absent so the UI shows an empty state.
    """
    limit = max(1, min(limit, 50))
    with get_cursor() as cur:
        cur.execute("select to_regclass('research.broker_daily') as t")
        reg = cur.fetchone()
        if not reg or not reg["t"]:
            return {"date": None, "captured_at": None, "rows": []}
        cur.execute("select max(trade_date) as d from research.broker_daily")
        row = cur.fetchone()
        if not row or not row["d"]:
            return {"date": None, "captured_at": None, "rows": []}
        date = row["d"]
        cur.execute(
            """select broker_code, broker_name, volume, value, frequency, captured_at
               from research.broker_daily
               where trade_date = %s and value is not null
               order by value desc limit %s""",
            (date, limit),
        )
        rows = cur.fetchall()
    captured = max((r["captured_at"] for r in rows if r["captured_at"]), default=None)
    from ..broker_flow import classify_broker  # map kurasi kode -> kategori

    return {
        "date": _norm_date(date),
        "captured_at": captured.isoformat() if captured else None,
        "rows": [
            {
                "broker_code": r["broker_code"],
                "broker_name": r["broker_name"],
                "volume": _f(r["volume"]),
                "value": _f(r["value"]),
                "frequency": _f(r["frequency"]),
                "category": classify_broker(r["broker_code"]),
            }
            for r in rows
        ],
    }


# --------------------------------------------------------------- broker concentration

def get_broker_concentration(limit: int = 10) -> dict[str, Any]:
    """Konsentrasi broker pasar (CR1/CR3/CR5 + HHI) dari sesi broker_daily terakhir.

    CR/HHI dihitung dari SELURUH broker sesi itu, bukan hanya top-N — kalau
    penyebutnya cuma potongan teratas, angkanya selalu mendekati 100% dan tidak
    bermakna. broker_daily adalah agregat PER FIRMA untuk SELURUH pasar (bukan
    per saham; IDX tidak menyediakan breakdown per emiten di endpoint gratis),
    jadi metrik ini menggambarkan struktur pasar, bukan aliran satu saham.

    Tabel/data kosong -> struktur kosong (tidak pernah raise) supaya UI bisa
    menampilkan empty state.
    """
    limit = max(1, min(limit, 50))
    empty: dict[str, Any] = {
        "date": None,
        "captured_at": None,
        "n_brokers": 0,
        "total_value": None,
        "cr1": None,
        "cr3": None,
        "cr5": None,
        "hhi": None,
        "top": [],
    }
    with get_cursor() as cur:
        cur.execute("select to_regclass('research.broker_daily') as t")
        reg = cur.fetchone()
        if not reg or not reg["t"]:
            return empty
        cur.execute("select max(trade_date) as d from research.broker_daily")
        row = cur.fetchone()
        if not row or not row["d"]:
            return empty
        date = row["d"]
        cur.execute(
            """select broker_code, broker_name, volume, value, frequency, captured_at
               from research.broker_daily
               where trade_date = %s and value is not null
               order by value desc""",
            (date,),
        )
        broker_rows = cur.fetchall()

    from ..research.broker_activity import concentration

    captured = max(
        (r["captured_at"] for r in broker_rows if r["captured_at"]), default=None
    )
    conc = concentration(
        [
            {
                "broker_code": r["broker_code"],
                "broker_name": r["broker_name"],
                "volume": _f(r["volume"]),
                "value": _f(r["value"]),
                "frequency": _f(r["frequency"]),
            }
            for r in broker_rows
        ],
        top_n=limit,
    )
    return {
        "date": _norm_date(date),
        "captured_at": captured.isoformat() if captured else None,
        "n_brokers": conc["n_brokers"],
        "total_value": conc["total_value"],
        "cr1": conc["cr1"],
        "cr3": conc["cr3"],
        "cr5": conc["cr5"],
        "hhi": conc["hhi"],
        "top": [
            {
                "broker_code": t["broker_code"],
                "broker_name": t["broker_name"],
                "volume": t["volume"],
                "value": t["value"],
                "frequency": t["frequency"],
                "share": t["share"],
            }
            for t in conc["top"]
        ],
    }


# --------------------------------------------------------------- price history


def _load_ohlc_by_date(cur: Any, code: str) -> dict[str, tuple]:
    """EOD IDX rows first (research superset), fill gaps with Yahoo (stock_daily)."""
    by_date: dict[str, tuple] = {}
    cur.execute(
        """select trade_date, open, high, low, close, volume
           from research.latest_pit
           where code = %s order by trade_date""",
        (code,),
    )
    for r in cur.fetchall():
        by_date[_norm_date(r["trade_date"])] = (
            r["trade_date"], r["open"], r["high"], r["low"], r["close"], r["volume"]
        )
    cur.execute(
        """select date, open, high, low, close, volume from stock_daily
           where ticker = %s order by date""",
        (code,),
    )
    for r in cur.fetchall():
        by_date.setdefault(
            _norm_date(r["date"]),
            (r["date"], r["open"], r["high"], r["low"], r["close"], r["volume"]),
        )
    return by_date


def _live_bar(cur: Any, code: str) -> dict[str, Any] | None:
    """Latest snapshot for a code today (WIB); None if none."""
    cur.execute(
        """select distinct on (code) open, high, low, close, volume, captured_at
           from stock_quotes where code = %s
           order by code, captured_at desc""",
        (code,),
    )
    r = cur.fetchone()
    if not r or r["close"] is None:
        return None
    cap = r["captured_at"]
    cap_date = cap.strftime("%Y-%m-%d") if cap else ""
    if cap_date != _today():
        return None
    return {
        "date": _today(),
        "open": r["open"],
        "high": r["high"],
        "low": r["low"],
        "close": r["close"],
        "volume": r["volume"] or 0,
    }


def _merge_live_bar(df: pd.DataFrame, live: dict[str, Any] | None) -> pd.DataFrame:
    if not live:
        return df
    d = pd.to_datetime(live["date"])
    df = df.copy()
    if "date" in df.columns and len(df):
        df["date"] = pd.to_datetime(df["date"])
        df = df[df["date"] != d]
    row = pd.DataFrame([{
        "date": d,
        "open": live["open"],
        "high": live["high"],
        "low": live["low"],
        "close": live["close"],
        "volume": live["volume"],
    }])
    return (
        pd.concat([df, row], ignore_index=True)
        .sort_values("date")
        .reset_index(drop=True)
    )


def get_price_history(code: str, limit: int = 60) -> list[dict[str, Any]]:
    with get_cursor() as cur:
        by_date = _load_ohlc_by_date(cur, code)
    rows = [by_date[k] for k in sorted(by_date)][-limit:]
    return [
        {
            "date": _norm_date(r[0]),
            "open": _f(r[1]),
            "high": _f(r[2]),
            "low": _f(r[3]),
            "close": _f(r[4]),
            "volume": _f(r[5]),
        }
        for r in rows
    ]


# --------------------------------------------------------------- signals


def _build_frame(cur: Any, code: str) -> tuple[pd.DataFrame, bool]:
    by_date = _load_ohlc_by_date(cur, code)
    if not by_date:
        return pd.DataFrame(), False
    rows = sorted(by_date.values(), key=lambda r: _norm_date(r[0]))
    df = pd.DataFrame(
        [(_norm_date(r[0]), r[1], r[2], r[3], r[4], r[5]) for r in rows],
        columns=["date", "open", "high", "low", "close", "volume"],
    )
    # Numeric coercion (psycopg returns Decimal for numeric columns).
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    bar = _live_bar(cur, code)
    if bar is not None:
        bar = {k: (float(v) if v is not None else None)
               if k != "date" else v for k, v in bar.items()}
    is_live = bar is not None
    df = _merge_live_bar(df, bar)
    return df, is_live


def _zscore_context(df: pd.DataFrame) -> dict[str, Any]:
    """60-day mean-reversion context: z-score of last close vs 60d mean/std.

    Mirrors the definition used by analytics.get_valuation so both screens
    agree on what "stretched vs own band" means.
    """
    close = df["close"]
    window = close.tail(60)
    mean = window.mean()
    std = window.std()
    if not std or pd.isna(std) or mean <= 0:
        return {"z": None, "mean": None}
    z = float((close.iloc[-1] - mean) / std)
    return {"z": round(z, 2), "mean": round(float(mean), 2)}


def get_hold_check(codes: list[str], min_days: int = 40) -> list[dict[str, Any]]:
    """"Hold Check" verdicts: is each watched stock still worth holding?

    Combines the technical signal (SMA20/50 + RSI rule) with the mean-reversion
    valuation context from get_valuation into one 0-100 score and a verdict:

    - STRONG HOLD (>=75): technicals intact and valuation not stretched.
    - HOLD (55-74): mostly intact, some caution.
    - TRIM (35-54): warning signs — overextended above band, weakening trend,
      or momentum loss with stretched price.
    - EXIT (<35): technical breakdown (SELL signal) and/or deep weakness.

    This is a research aid, not a recommendation to buy/sell.
    """
    # Skor per emiten tidak berubah intraday (sumbernya EOD/IC/broker snapshot),
    # dan perhitungannya mahal (~4-5s untuk seluruh watchlist). Cache 10 menit
    # di kunci (codes, min_days); serve loop mengosongkannya tiap refresh via
    # POST /api/cache/clear.
    from .analytics import _research_cache_get, _research_cache_put

    cache_key = f"hold_check:{','.join(sorted(codes))}:{min_days}"
    cached = _research_cache_get(cache_key)
    if cached is not None:
        return cached

    # Lapisan aktivitas broker (proksi aliran, bobot dari IC). Dihitung SEBELUM
    # blok cursor: snapshot-nya membuka koneksi sendiri untuk query panel faktor
    # yang berat, jadi jangan tahan koneksi pool sambil menunggu. Gagal atau
    # belum tervalidasi -> lapisan tidak aktif (adj 0) dan hold-check berperilaku
    # persis seperti sebelum fitur ini ada.
    from ..research.broker_activity import apply_broker_layer

    try:
        from .analytics import broker_scores_by_code

        broker_by_code, broker_validated = broker_scores_by_code()
    except Exception as e:
        print(f"[warn] skor aktivitas broker dilewati: {e}", file=sys.stderr)
        broker_by_code, broker_validated = {}, False

    out: list[dict[str, Any]] = []
    with get_cursor() as cur:
        names: dict[str, str | None] = {}
        cur.execute(
            """select distinct on (code) code, name from research.latest_pit
               order by code, trade_date desc"""
        )
        for r in cur.fetchall():
            names[r["code"]] = r["name"]

        # Lapisan faktor (IC-derived): percentile cross-sectional vs SELURUH
        # pasar — bukan hanya watchlist — supaya ranking bermakna statistik.
        # Bobot dibaca dari research.factor_ic_history (run `idx ic` terbaru);
        # fallback konstanta bila tabel kosong. Cache 30 menit di composite.
        from ..research.composite import (
            apply_factor_layer,
            latest_weights,
            market_factor_ranks,
        )

        try:
            market_ranks = market_factor_ranks(cur)
            weights = latest_weights(cur)
        except Exception as e:
            # Faktor lapisan adalah enhancement — DB query gagal tidak boleh
            # mematikan hold-check teknikal yang sudah jalan.
            print(f"[warn] composite factor ranks dilewati: {e}", file=sys.stderr)
            market_ranks = {}
            weights = None

        for code in codes:
            df, _ = _build_frame(cur, code)
            if df.empty or len(df) < min_days:
                continue
            df = calculate_indicators(df)
            raw_signal = generate_signal(df)
            # Corp-action filter: SELL yang hanya terbentuk karena drop
            # mekanis ex-dividend dinetralkan jadi HOLD juga di hold-check,
            # supaya skor tidak kena penalti -25 untuk hal yang bukan
            # tekanan jual.
            signal, div_cash, div_mech = _apply_dividend_guard(cur, code, df, raw_signal)
            last = df.iloc[-1]
            close = float(last["close"])
            rsi = _f(last.get("RSI"))
            ma_s = _f(last.get("MA_Short"))
            ma_l = _f(last.get("MA_Long"))
            trend_up = bool(ma_s is not None and ma_l is not None and ma_s > ma_l)
            macd = _f(last.get("MACD"))
            macd_sig = _f(last.get("MACD_Signal"))
            macd_bullish = bool(macd is not None and macd_sig is not None and macd > macd_sig)

            bb_u = _f(last.get("BB_Upper"))
            bb_l = _f(last.get("BB_Lower"))
            if bb_u is not None and bb_l is not None and bb_u > bb_l:
                bb_pos = "above" if close > bb_u else "below" if close < bb_l else "inside"
            else:
                bb_pos = None

            zc = _zscore_context(df)
            z = zc["z"]
            mean = zc["mean"]
            below_target = (
                round((mean - close) / mean * 100, 1) if mean is not None and mean > 0 else None
            )

            reasons: list[str] = []
            score = 50.0

            # --- technical signal (dominant factor)
            if signal == "BUY":
                score += 20
                reasons.append("Sinyal teknikal BUY (SMA20 > SMA50, RSI sehat)")
            elif signal == "SELL":
                score -= 25
                reasons.append("Sinyal teknikal SELL (tren jangka pendek menembus ke bawah)")
            elif div_mech:
                reasons.append(
                    f"Sinyal SELL dibatalkan: turun mekanis ex-dividend "
                    f"(Rp {div_cash:.0f}/saham) — bukan tekanan jual"
                )
            else:
                reasons.append("Sinyal teknikal HOLD (tidak ada konfirmasi kuat)")

            if trend_up:
                score += 10
                reasons.append("Tren menengah masih naik (SMA20 di atas SMA50)")
            else:
                score -= 5
                reasons.append("Tren menengah melemah (SMA20 di bawah SMA50)")

            if macd_bullish:
                score += 5
                reasons.append("MACD masih bullish (di atas garis sinyal)")
            else:
                score -= 5
                reasons.append("MACD melemah (di bawah garis sinyal)")

            # --- RSI extremes
            if rsi is not None:
                if rsi >= 75:
                    score -= 10
                    reasons.append(f"RSI {rsi:.0f} overbought — rentan koreksi")
                elif rsi <= 25:
                    score -= 10
                    reasons.append(f"RSI {rsi:.0f} oversold — tekanan jual kuat")
                elif rsi >= 65:
                    score -= 3
                    reasons.append(f"RSI {rsi:.0f} mendekati overbought")

            # --- valuation context (z-score vs own 60d band)
            if z is not None:
                if z >= 2.0:
                    score -= 15
                    reasons.append(f"Harga jauh di atas band 60-hari (z {z:+.1f}) — overextended")
                elif z >= 1.5:
                    score -= 8
                    reasons.append(f"Harga di atas band 60-hari (z {z:+.1f}) — waspada")
                elif z <= -1.0:
                    # A dip only counts as healthy if the trend is still up.
                    if trend_up:
                        score += 5
                        reasons.append(f"Harga di bawah band 60-hari (z {z:+.1f}) tapi tren masih naik — kualitas dip")
                    else:
                        score -= 5
                        reasons.append(f"Harga di bawah band 60-hari (z {z:+.1f}) dengan tren melemah")
                else:
                    reasons.append("Harga masih dalam band wajar 60-hari")

            if bb_pos == "above":
                score -= 5
                reasons.append("Harga menembus Bollinger atas — ekstensi jangka pendek")
            elif bb_pos == "below":
                score -= 10
                reasons.append("Harga di bawah Bollinger bawah — tekanan jual")

            score = max(0.0, min(100.0, score))
            base_score = score
            if score >= 75:
                verdict = "STRONG HOLD"
            elif score >= 55:
                verdict = "HOLD"
            elif score >= 35:
                verdict = "TRIM"
            else:
                verdict = "EXIT"

            # --- lapisan faktor IC (vol/likuiditas/jarak 52w) ---

            base = market_ranks.get(code)
            (
                score,
                verdict,
                reasons,
                factor_adj,
            ) = apply_factor_layer(base_score, verdict, reasons, base, weights)

            # --- lapisan aktivitas broker (diterapkan setelah lapisan faktor) ---
            broker_row = broker_by_code.get(code)
            score, verdict, reasons, broker_adj = apply_broker_layer(
                score,
                verdict,
                reasons,
                broker_row["score"] if broker_row else None,
                broker_validated,
            )

            out.append({
                "code": code,
                "name": names.get(code),
                "signal": signal,
                "trend_up": trend_up,
                "rsi": rsi,
                "macd_bullish": macd_bullish,
                "bb_position": bb_pos,
                "z_score": z,
                "below_target_pct": below_target,
                "foreign_net": None,
                "score": round(score),  # type: ignore[arg-type]
                "base_score": round(base_score),  # type: ignore[arg-type]
                "factor_adj": round(factor_adj, 1),
                "factor_pct": base if base is not None else None,
                "broker_score": broker_row["score"] if broker_row else None,
                "broker_adj": round(broker_adj, 1),
                "verdict": verdict,
                "reasons": reasons,
            })

    # EOD foreign net for context (separate pass, keeps the main loop simple).
    with get_cursor() as cur:
        for row in out:
            cur.execute(
                """select foreign_net from research.latest_pit
                   where code = %s order by trade_date desc limit 1""",
                (row["code"],),
            )
            r = cur.fetchone()
            row["foreign_net"] = _f(r["foreign_net"]) if r else None

    out.sort(key=lambda r: r["score"], reverse=True)
    _research_cache_put(cache_key, out, ttl=600.0)
    return out


def _ex_div_cash(cur: Any, code: str, lookback_days: int = 7) -> float | None:
    """Cash dividend ex-date terakhir dalam `lookback_days` hari kalender.

    Window ketat: drop mekanis ex-dividend hanya relevan untuk sinyal yang
    terbentuk sekitar ex-date. None bila tidak ada.
    """
    cur.execute(
        """select cash_amount from research.corporate_actions
           where code = %s and action_type = 'dividend'
             and cash_amount is not null and cash_amount > 0
             and ex_date between current_date - %s::int and current_date
           order by ex_date desc limit 1""",
        (code, lookback_days),
    )
    row = cur.fetchone()
    return float(row["cash_amount"]) if row else None


def _apply_dividend_guard(
    cur: Any, code: str, df: pd.DataFrame, signal: str
) -> tuple[str, float | None, bool]:
    """Netralkan SELL palsu akibat ex-dividend (corp-action filter #4).

    Returns (signal_efektif, div_cash, was_mechanical). Hanya dipanggil bila
    sinyal mentah SELL — tidak menyentuh BUY/HOLD.
    """
    if signal != "SELL":
        return signal, None, False
    div = _ex_div_cash(cur, code)
    if div is None:
        return signal, None, False
    if is_mechanical_sell(df, div):
        return "HOLD", div, True
    return signal, div, False


def get_signals(codes: list[str], min_days: int = 25) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    generated_at = datetime.now(WIB).isoformat(timespec="seconds")
    with get_cursor() as cur:
        for code in codes:
            df, is_live = _build_frame(cur, code)
            if df.empty or len(df) < min_days:
                continue
            df = calculate_indicators(df)
            signal = generate_signal(df)
            last, prev = df.iloc[-1], df.iloc[-2]
            pct = None
            if prev["close"]:
                pct = (last["close"] - prev["close"]) / prev["close"] * 100
            # Corp-action filter: SELL yang hanya terbentuk karena drop
            # mekanis ex-dividend -> HOLD (bukan tekanan jual sesungguhnya).
            eff_signal, div_cash, mech = _apply_dividend_guard(cur, code, df, signal)
            if mech:
                pct = pct + (div_cash / prev["close"] * 100) if prev["close"] else pct
            last_date = last["date"]
            as_of = (
                last_date.strftime("%Y-%m-%d")
                if hasattr(last_date, "strftime")
                else _norm_date(last_date)
            )
            out.append({
                "code": code,
                "signal": eff_signal,
                "raw_signal": signal,
                "div_cash": div_cash,
                "div_adjusted": mech,
                "close": _f(last["close"]),
                "pct": _f(pct),
                "rsi": _f(last.get("RSI")),
                "sma20": _f(last.get("MA_Short")),
                "macd": _f(last.get("MACD")),
                "trend_up": bool((last.get("MA_Short") or 0) > (last.get("MA_Long") or 0)),
                "live": is_live,
                "as_of": as_of,
                "generated_at": generated_at,
            })
    return out


# --------------------------------------------------------------- technical chart


def get_technical_chart(code: str) -> dict[str, Any]:
    with get_cursor() as cur:
        df, _ = _build_frame(cur, code)
    if df.empty:
        return {"code": code, "signal": "HOLD", "bars": []}
    df = calculate_indicators(df)
    signal = generate_signal(df)
    bars: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        d = row["date"]
        bars.append({
            "date": d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else _norm_date(d),
            "open": _f(row.get("open")),
            "high": _f(row.get("high")),
            "low": _f(row.get("low")),
            "close": _f(row.get("close")),
            "volume": _f(row.get("volume")),
            "ma_short": _f(row.get("MA_Short")),
            "ma_long": _f(row.get("MA_Long")),
            "rsi": _f(row.get("RSI")),
            "bb_upper": _f(row.get("BB_Upper")),
            "bb_lower": _f(row.get("BB_Lower")),
            "macd": _f(row.get("MACD")),
            "macd_signal": _f(row.get("MACD_Signal")),
        })
    return {"code": code, "signal": signal, "bars": bars}
