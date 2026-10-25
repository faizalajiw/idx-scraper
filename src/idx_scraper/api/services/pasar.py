"""Service layer — leaders, broker teratas, konsentrasi broker, dan riwayat
harga.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from ..database import get_cursor
from .inti import (
    _LIVE_MAX_AGE_SEC,
    _WIB_DAY_START_SQL,
    WIB,
    _f,
    _is_market_hours,
    _norm_date,
    _today,
)

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
    from ...broker_flow import classify_broker  # map kurasi kode -> kategori

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

    from ...research.broker_activity import concentration

    captured = max((r["captured_at"] for r in broker_rows if r["captured_at"]), default=None)
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
            r["trade_date"],
            r["open"],
            r["high"],
            r["low"],
            r["close"],
            r["volume"],
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
    row = pd.DataFrame(
        [
            {
                "date": d,
                "open": live["open"],
                "high": live["high"],
                "low": live["low"],
                "close": live["close"],
                "volume": live["volume"],
            }
        ]
    )
    return pd.concat([df, row], ignore_index=True).sort_values("date").reset_index(drop=True)


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
