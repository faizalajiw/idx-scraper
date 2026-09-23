"""Service layer: all business/data calculations for the API.

Implements the aggregations and technical-analytics semantics in parameterized
Postgres queries; the frontend dashboard (idx-web, Recharts) only presents this
data and performs no calculations of its own.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from ..analysis import calculate_indicators, generate_signal
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


# --------------------------------------------------------------- overview


def get_market_overview() -> dict[str, Any]:
    with get_cursor() as cur:
        cur.execute(
            """select close, change, percent, current, captured_at
               from index_quotes where code = 'COMPOSITE'
               order by captured_at desc limit 1"""
        )
        idx = cur.fetchone()

        # Latest snapshot per code (anti double-count across snapshots).
        cur.execute(
            """with latest as (
                   select distinct on (code) *
                   from stock_quotes
                   order by code, captured_at desc
               )
               select sum(volume) as total_volume,
                      sum(value)  as total_value,
                      count(distinct code) as stock_count
               from latest"""
        )
        totals = cur.fetchone()

        cur.execute(
            """with latest as (
                   select distinct on (code) code, close, change, previous
                   from stock_quotes
                   order by code, captured_at desc
               )
               select code, close,
                      round(change * 100.0 / nullif(previous, 0), 2) as percent
               from latest
               where change is not null and previous > 0 and change > 0
               order by percent desc limit 5"""
        )
        gainers = cur.fetchall()

        cur.execute(
            """with latest as (
                   select distinct on (code) code, close, change, previous
                   from stock_quotes
                   order by code, captured_at desc
               )
               select code, close,
                      round(change * 100.0 / nullif(previous, 0), 2) as percent
               from latest
               where change is not null and previous > 0 and change < 0
               order by percent asc limit 5"""
        )
        losers = cur.fetchall()

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
    """Full-market top movers from EOD summary (per session close)."""
    with get_cursor() as cur:
        cur.execute("select max(date) as d from stock_summary_daily")
        latest = cur.fetchone()
        if not latest or not latest["d"]:
            return {"date": None, "captured_at": None, "top_gainers": [], "top_losers": []}
        date = latest["d"]

        cur.execute(
            "select max(captured_at) as c from stock_summary_daily where date = %s",
            (date,),
        )
        captured = cur.fetchone()["c"]

        cur.execute(
            """select code, close, percent from stock_summary_daily
               where date = %s and percent is not null and volume > 0
               order by percent desc limit 10""",
            (date,),
        )
        gainers = cur.fetchall()

        cur.execute(
            """select code, close, percent from stock_summary_daily
               where date = %s and percent is not null and volume > 0
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
                   select distinct on (code) code, close, change, previous, volume, foreign_net
                   from stock_quotes
                   order by code, captured_at desc
               )
               select l.code, l.close, l.change,
                      round(l.change * 100.0 / nullif(l.previous, 0), 2) as percent,
                      l.volume, l.foreign_net,
                      (select count(*) from stock_summary_daily s where s.code = l.code) as hist_days
               from latest l
               where l.code = any(%s)
               order by percent desc nulls last""",
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


# --------------------------------------------------------------- price history


def _load_ohlc_by_date(cur: Any, code: str) -> dict[str, tuple]:
    """EOD IDX rows first, fill gaps with Yahoo (stock_daily). Keyed by ISO date."""
    by_date: dict[str, tuple] = {}
    cur.execute(
        """select date, open, high, low, close, volume from stock_summary_daily
           where code = %s order by date""",
        (code,),
    )
    for r in cur.fetchall():
        by_date[_norm_date(r["date"])] = (
            r["date"], r["open"], r["high"], r["low"], r["close"], r["volume"]
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


def get_signals(codes: list[str], min_days: int = 25) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
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
            out.append({
                "code": code,
                "signal": signal,
                "close": _f(last["close"]),
                "pct": _f(pct),
                "rsi": _f(last.get("RSI")),
                "sma20": _f(last.get("MA_Short")),
                "macd": _f(last.get("MACD")),
                "trend_up": bool((last.get("MA_Short") or 0) > (last.get("MA_Long") or 0)),
                "live": is_live,
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
