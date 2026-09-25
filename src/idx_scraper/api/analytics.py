"""Analytics services for the IDX dashboard feature set.

All computations run on data already stored in Postgres (stock_summary_daily,
stock_quotes, index_quotes, stock_daily). Nothing here writes to the database.

Sectors: IDX-IC classification is not available from the free endpoints we
poll, so we map tickers to sectors with a curated dictionary of the most
liquid IDX names and fall back to "Lainnya" (other). The map lives in
sector_map.py and is intentionally data, not logic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from ..analysis import calculate_indicators, generate_signal
from .database import get_cursor
from .sector_map import SECTOR_MAP

WIB = timezone(timedelta(hours=7))


def _f(v: Any) -> float | None:
    try:
        return float(v) if v is not None and pd.notna(v) else None
    except (TypeError, ValueError):
        return None


def _today() -> str:
    return datetime.now(WIB).strftime("%Y-%m-%d")


def _latest_eod_date(cur: Any) -> str | None:
    cur.execute("select max(trade_date) as d from research.latest_pit")
    row = cur.fetchone()
    return str(row["d"]) if row and row["d"] else None


def _norm_date(value: Any) -> str:
    """'YYYYMMDD' -> 'YYYY-MM-DD'; ISO/date passes through as ISO string."""
    s = str(value)
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s[:10] if len(s) >= 10 else s


def _latest_index(cur: Any, code: str = "COMPOSITE") -> dict[str, Any] | None:
    cur.execute(
        """select close, change, percent, current, captured_at from index_quotes
           where code = %s order by captured_at desc limit 1""",
        (code,),
    )
    r = cur.fetchone()
    if not r:
        return None
    return {
        "close": _f(r["close"]),
        "change": _f(r["change"]),
        "percent": _f(r["percent"]),
        "current": _f(r["current"]),
        "captured_at": r["captured_at"].isoformat() if r["captured_at"] else None,
    }


# --------------------------------------------------------------- broker summary


def get_broker_summary(code: str, date: str | None = None) -> dict[str, Any]:
    """Top brokers by buy/sell value for one stock on one trading day.

    Broker-level data is not published through the free IDX endpoints we poll,
    so this reconstructs a *proxy* from the best available granular signals:
    foreign buy/sell (broker-dealer aggregated) plus bid/offer imbalance from
    the intraday snapshots of that day. The API marks rows as estimates via
    "estimated": true so the UI can disclose it honestly.
    """
    with get_cursor() as cur:
        d = date or _latest_eod_date(cur)
        if not d:
            return {"code": code, "name": None, "date": None, "top_buyers": [], "top_sellers": []}

        cur.execute(
            """select name, foreign_buy, foreign_sell, close, volume, value
               from research.latest_pit where code = %s and trade_date = %s limit 1""",
            (code, d),
        )
        eod = cur.fetchone()

        cur.execute(
            """select bid, bid_volume, offer, offer_volume, foreign_net, captured_at
               from stock_quotes
               where code = %s and captured_at::date = %s
               order by captured_at asc""",
            (code, d),
        )
        snaps = cur.fetchall()

    name = eod["name"] if eod else None
    fbuy = _f(eod["foreign_buy"]) if eod else None
    fsell = _f(eod["foreign_sell"]) if eod else None

    # Bid/offer imbalance averaged over the day as a domestic-flow proxy.
    bid_v = [_f(s["bid_volume"]) for s in snaps if _f(s["bid_volume"])]
    off_v = [_f(s["offer_volume"]) for s in snaps if _f(s["offer_volume"])]
    avg_bid = sum(bid_v) / len(bid_v) if bid_v else None
    avg_offer = sum(off_v) / len(off_v) if off_v else None

    rows: list[dict[str, Any]] = []
    if fbuy is not None:
        rows.append({
            "broker": "FOREIGN (aggregate)",
            "code": None,
            "buy_value": fbuy,
            "sell_value": fsell or 0.0,
            "net": fbuy - (fsell or 0.0),
            "buy_rank": 1,
            "sell_rank": 1 if fsell is not None else None,
        })
    if avg_bid is not None or avg_offer is not None:
        # Rough IDR estimate using the day's close as reference price.
        ref = _f(eod["close"]) if eod else None
        b = avg_bid * ref if (avg_bid is not None and ref) else None
        o = avg_offer * ref if (avg_offer is not None and ref) else None
        rows.append({
            "broker": "DOMESTIC (bid/offer proxy)",
            "code": None,
            "buy_value": b or 0.0,
            "sell_value": o or 0.0,
            "net": (b or 0.0) - (o or 0.0),
            "buy_rank": 2,
            "sell_rank": 2,
        })

    buyers = sorted(rows, key=lambda r: r["buy_value"], reverse=True)
    sellers = sorted(rows, key=lambda r: r["sell_value"], reverse=True)
    return {
        "code": code,
        "name": name,
        "date": d,
        "top_buyers": buyers,
        "top_sellers": sellers,
    }


# --------------------------------------------------------------- foreign flow


def get_foreign_flow(days: int = 20) -> dict[str, Any]:
    """Aggregate foreign buy/sell/net per day from EOD foreign_* columns.

    IDX publishes foreign volumes in SHARES; convert to IDR notional by
    multiplying with the day's close so cross-stock sums are meaningful.
    """
    with get_cursor() as cur:
        cur.execute(
            """select trade_date as date,
                      sum(foreign_buy * close) as buy,
                      sum(foreign_sell * close) as sell,
                      sum(foreign_net * close) as net
               from research.latest_pit
               where trade_date >= (select max(trade_date) from research.latest_pit) - %s::int
                 and foreign_net is not null and close is not null
               group by trade_date order by trade_date""",
            (days,),
        )
        by_day = cur.fetchall()

        cur.execute(
            """select code, name, foreign_net * close as net_idr,
                      percent
               from research.latest_pit
               where trade_date = (select max(trade_date) from research.latest_pit
                                    where foreign_net is not null)
                 and foreign_net is not null and foreign_net <> 0 and close is not null
               order by net_idr desc limit 10"""
        )
        inn = cur.fetchall()

        cur.execute(
            """select code, name, foreign_net * close as net_idr,
                      percent
               from research.latest_pit
               where trade_date = (select max(trade_date) from research.latest_pit
                                    where foreign_net is not null)
                 and foreign_net is not null and foreign_net <> 0 and close is not null
               order by net_idr asc limit 10"""
        )
        out = cur.fetchall()

    days_list = [
        {"date": str(r["date"]), "buy": _f(r["buy"]), "sell": _f(r["sell"]), "net": _f(r["net"])}
        for r in by_day
    ]
    latest = days_list[-1] if days_list else None

    def _mover(r: Any) -> dict[str, Any]:
        return {"code": r["code"], "name": r["name"], "net": _f(r["net_idr"]), "percent": _f(r["percent"])}

    return {
        "date": latest["date"] if latest else None,
        "total_buy": latest["buy"] if latest else None,
        "total_sell": latest["sell"] if latest else None,
        "total_net": latest["net"] if latest else None,
        "days": days_list,
        "top_net_in": [_mover(r) for r in inn],
        "top_net_out": [_mover(r) for r in out],
    }


# --------------------------------------------------------------- sector analysis


# --------------------------------------------------------------- sector RRG


def get_sector_rrg(
    benchmark: str = "COMPOSITE",
    window: int = 21,
    tail_weeks: int = 8,
) -> dict[str, Any]:
    """Relative Rotation Graph (RRG) points per sector, benchmarked to an index.

    RRG semantics (relative to the benchmark index):
      - RS-Ratio  = 100 + (log(relative strength) - mean) / std * scale
        (indexed so 100 = in line with benchmark; >100 outperforming).
      - RS-Momentum = 100 + rate of change of the RS-Ratio, normalized the same
        way (>100 improving, <100 weakening).

    The relative-strength series per sector is built from the equal-weighted
    average daily return of its constituents in stock_summary_daily, so no new
    data source is needed. Benchmark series comes from index_quotes (IHSG).

    tail_weeks controls how many past weekly points are returned per sector so
    the UI can draw the rotation trail; the last point is "now".
    """
    import numpy as np

    with get_cursor() as cur:
        cur.execute("select max(trade_date) as d from research.latest_pit")
        d = cur.fetchone()["d"]
        if not d:
            return {"benchmark": benchmark, "window": window, "date": None, "points": []}

        cur.execute(
            """select code, trade_date as date, percent from research.latest_pit
               where trade_date >= %s - (%s * 3)::int and percent is not null
               order by trade_date""",
            (d, window + tail_weeks * 5),
        )
        rows = cur.fetchall()

        cur.execute(
            """select distinct on (captured_at::date) captured_at::date as date, close
               from index_quotes
               where code = %s and close is not null
               order by captured_at::date, captured_at desc""",
            (benchmark,),
        )
        idx_rows = cur.fetchall()

    # index_quotes only carries a few days of live captures; RRG needs weeks,
    # so fall back to a synthetic composite whenever the real benchmark is short.
    if len(idx_rows) < window + 5:
        with get_cursor() as cur:
            cur.execute(
                """select trade_date as date, avg(percent) as pct from research.latest_pit
                   where percent is not null group by trade_date order by trade_date"""
            )
            idx_rows = [
                {"date": r["date"], "close": None, "pct": _f(r["pct"])}
                for r in cur.fetchall()
            ]
            pct_mode = True
    else:
        pct_mode = False

    # ---- benchmark daily return series (indexed by ISO date) ----
    if pct_mode:
        bench_ret: dict[str, float] = {}
        for r in idx_rows:
            p = _f(r["pct"])
            if p is not None:
                bench_ret[_norm_date(r["date"])] = p / 100.0
    else:
        closes: dict[str, float] = {}
        for r in idx_rows:
            c = _f(r["close"])
            if c:
                closes[_norm_date(r["date"])] = c
        dates_sorted = sorted(closes)
        bench_ret = {
            dates_sorted[i]: closes[dates_sorted[i]] / closes[dates_sorted[i - 1]] - 1.0
            for i in range(1, len(dates_sorted))
        }

    if len(bench_ret) < window + 5:
        return {"benchmark": benchmark, "window": window, "date": str(d), "points": []}

    # ---- sector daily returns: equal-weighted mean of constituents per date ----
    sums: dict[str, dict[str, float]] = {}
    counts: dict[str, dict[str, int]] = {}
    for r in rows:
        sector = SECTOR_MAP.get(r["code"], "Lainnya")
        p = _f(r["percent"])
        if p is None:
            continue
        dt = _norm_date(r["date"])
        sums.setdefault(sector, {}).setdefault(dt, 0.0)
        counts.setdefault(sector, {}).setdefault(dt, 0)
        sums[sector][dt] += p / 100.0
        counts[sector][dt] += 1

    # Keep only sectors with enough history; drop "Lainnya" (unclassified mix).
    sector_daily: dict[str, pd.Series] = {}
    for sector, by_date in sums.items():
        if sector == "Lainnya":
            continue
        dates = sorted(by_date)
        if len(dates) < window + 5:
            continue
        vals = [by_date[dt] / counts[sector][dt] for dt in dates]
        sector_daily[sector] = pd.Series(vals, index=pd.Index(dates), dtype=float)

    if not sector_daily:
        return {"benchmark": benchmark, "window": window, "date": str(d), "points": []}

    # ---- weekly resample + relative strength ----
    bench = pd.Series(bench_ret, dtype=float).sort_index()
    bench_df = pd.DataFrame({"ret": bench.values}, index=pd.to_datetime(bench.index))
    bench_w = bench_df["ret"].resample("W-FRI").prod(min_count=1).dropna()

    points: list[dict[str, Any]] = []
    for sector, s in sector_daily.items():
        sdf = pd.DataFrame({"ret": s.values}, index=pd.to_datetime(s.index))
        sw = sdf["ret"].resample("W-FRI").prod(min_count=1).dropna()
        common = sw.index.intersection(bench_w.index)
        if len(common) < window + 2:
            continue
        rel = (1 + sw.loc[common]) / (1 + bench_w.loc[common])
        rs = rel.cumprod()
        if len(rs) < window + 2:
            continue
        lr = np.log(rs)

        # Each point is normalized against ITS OWN trailing window (rolling),
        # like a real RRG: the trail shows where the sector sat relative to its
        # trailing distribution at each week, not positions inside one window.
        roll = lr.rolling(window)
        ratio = 100 + (lr - roll.mean()) / roll.std() * 2

        mdiff = lr.diff()
        rollm = mdiff.rolling(window)
        mom = 100 + (mdiff - rollm.mean()) / rollm.std() * 2

        both = pd.DataFrame({"ratio": ratio, "mom": mom}).dropna()
        if both.empty:
            continue
        # Dates are week-ending (Friday) labels; the last one may be the Friday
        # of the current, still-open week.
        for dt, rowv in both.tail(tail_weeks).iterrows():
            points.append({
                "sector": sector,
                "date": dt.strftime("%Y-%m-%d"),
                "rs_ratio": round(float(rowv["ratio"]), 2),
                "rs_momentum": round(float(rowv["mom"]), 2),
            })

    return {
        "benchmark": benchmark,
        "window": window,
        "date": str(d),
        "points": points,
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
        sector = SECTOR_MAP.get(r["code"], "Lainnya")
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
        sectors.append({
            "sector": sector,
            "stock_count": len(items),
            "avg_percent": round(sum(pcts) / len(pcts), 2) if pcts else None,
            "total_value": sum(values) if values else None,
            "total_foreign_net": sum(fnets) if fnets else None,
            "gainers": sum(1 for p in pcts if p > 0),
            "losers": sum(1 for p in pcts if p < 0),
            "top_stock": {"code": top["code"], "percent": _f(top["percent"])} if top else None,
        })

    sectors.sort(key=lambda s: s["avg_percent"] if s["avg_percent"] is not None else -999, reverse=True)
    return {"date": d, "sectors": sectors}


# --------------------------------------------------------------- market narration


def _fmt_rp(v: float | None) -> str:
    if v is None:
        return "-"
    a = abs(v)
    if a >= 1e12:
        return f"Rp {v / 1e12:.2f} T"
    if a >= 1e9:
        return f"Rp {v / 1e9:.1f} M"
    return f"Rp {v / 1e6:.1f} Jt"


def get_market_narration() -> dict[str, Any]:
    """Plain-language summary of the session, assembled from stored aggregates."""
    sections: list[dict[str, Any]] = []
    with get_cursor() as cur:
        d = _latest_eod_date(cur)
        idx = _latest_index(cur)

        # Breadth: how many stocks up/down/flat today.
        cur.execute(
            """select
                   count(*) filter (where percent > 0) as up,
                   count(*) filter (where percent < 0) as down,
                   count(*) filter (where percent = 0) as flat,
                   count(*) as total
               from research.latest_pit where trade_date = %s""",
            (d,),
        )
        breadth = cur.fetchone()

        # Total market value of the day.
        cur.execute(
            "select sum(value) as v, sum(volume) as vol from research.latest_pit where trade_date = %s",
            (d,),
        )
        totals = cur.fetchone()

        # Foreign flow of the day.
        cur.execute(
            "select sum(foreign_net) as net from research.latest_pit where trade_date = %s",
            (d,),
        )
        fnet = cur.fetchone()

        # Sector leader & laggard (only sectors with >= 5 stocks to avoid noise).
        cur.execute(
            """select code, name, percent from research.latest_pit
               where trade_date = %s and percent is not null and value > 1e9
               order by percent desc limit 3""",
            (d,),
        )
        gainers = cur.fetchall()
        cur.execute(
            """select code, name, percent from research.latest_pit
               where trade_date = %s and percent is not null and value > 1e9
               order by percent asc limit 3""",
            (d,),
        )
        losers = cur.fetchall()

    # --- index section
    if idx:
        pct = idx["percent"] or (idx["change"] or 0)
        tone = "up" if (idx["change"] or 0) > 0 else "down" if (idx["change"] or 0) < 0 else "neutral"
        arah = "menguat" if tone == "up" else "melemah" if tone == "down" else " stagnant"
        sections.append({
            "title": "IHSG",
            "icon": "📊",
            "tone": tone,
            "text": (
                f"IHSG {arah} ke level {idx['current'] or idx['close']:,.2f} "
                f"({'+' if (idx['change'] or 0) > 0 else ''}{idx['change'] or 0:,.2f}; "
                f"{pct:+.2f}%)."
            ),
        })

    # --- breadth section
    if breadth and breadth["total"]:
        up, down, flat = breadth["up"], breadth["down"], breadth["flat"]
        total = breadth["total"]
        tone = "up" if up > down else "down" if down > up else "neutral"
        if up + down > 0:
            ratio = up / max(down, 1)
            mood = "optimisme mendominasi" if ratio > 1.5 else "tekanan jual lebih kuat" if ratio < 0.67 else "sentimen berimbang"
        else:
            mood = "aktivitas tipis"
        sections.append({
            "title": "Breath Pasar",
            "icon": "⚖️",
            "tone": tone,
            "text": (
                f"Dari {total} emiten aktif, {up} naik, {down} turun, {flat} stagnan — {mood}."
            ),
        })

    # --- value/liquidity section
    if totals and totals["v"]:
        sections.append({
            "title": "Nilai Transaksi",
            "icon": "💰",
            "tone": "neutral",
            "text": (
                f"Total nilai transaksi {_fmt_rp(_f(totals['v']))} "
                f"dengan volume {(_f(totals['vol']) or 0):,.0f} lembar."
            ),
        })

    # --- foreign flow section
    if fnet and fnet["net"] is not None:
        net = _f(fnet["net"]) or 0.0
        tone = "up" if net > 0 else "down" if net < 0 else "neutral"
        arah = "inflow" if net > 0 else "outflow" if net < 0 else "netral"
        sections.append({
            "title": "Arus Dana Asing",
            "icon": "🌍",
            "tone": tone,
            "text": f"Asing mencatat {arah} bersih {_fmt_rp(abs(net))} di pasar reguler.",
        })

    # --- movers section
    if gainers:
        g = ", ".join(f"{r['code']} ({r['percent']:+.1f}%)" for r in gainers[:3])
        sections.append({
            "title": "Unggul",
            "icon": "🚀",
            "tone": "up",
            "text": f"Penguat teratas: {g}.",
        })
    if losers:
        l = ", ".join(f"{r['code']} ({r['percent']:+.1f}%)" for r in losers[:3])
        sections.append({
            "title": "Tertekan",
            "icon": "🩸",
            "tone": "down",
            "text": f"Pelemah terdalam: {l}.",
        })

    return {
        "date": d,
        "generated_at": datetime.now(WIB).isoformat(),
        "sections": sections,
    }


# --------------------------------------------------------------- valuation


def _valuation_frame(cur: Any, code: str) -> pd.DataFrame | None:
    cur.execute(
        """select trade_date as date, close, volume from research.latest_pit
           where code = %s order by trade_date""",
        (code,),
    )
    rows = cur.fetchall()
    if len(rows) < 30:
        return None
    df = pd.DataFrame(
        [(str(r["date"]), _f(r["close"]), _f(r["volume"])) for r in rows],
        columns=["date", "close", "volume"],
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
            dfi = calculate_indicators(df.rename(columns={}).assign(open=df["close"], high=df["close"], low=df["close"]))
            rsi = _f(dfi["RSI"].iloc[-1])
            trend_up = bool(dfi["MA_Short"].iloc[-1] > dfi["MA_Long"].iloc[-1]) if len(dfi) > 50 else None

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
        cur.execute(
            """select distinct on (code) code, name from research.latest_pit
               where trade_date = (select max(trade_date) from research.latest_pit)
               order by code, knowledge_date desc"""
        )
        names = {r["code"]: r["name"] for r in cur.fetchall()}
    for lst in (undervalued, overvalued):
        for r in lst:
            r["name"] = names.get(r["code"])

    return {"undervalued": undervalued, "overvalued": overvalued}


# --------------------------------------------------------------- AI screener


def get_screener(
    signal: str | None = None,
    rsi_min: float | None = None,
    rsi_max: float | None = None,
    min_momentum: float | None = None,
    max_momentum: float | None = None,
    min_value: float | None = None,
    foreign_in_only: bool = False,
    min_vol_ratio: float | None = None,
    min_days: int = 30,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Rule-based multi-factor screener ("AI screening" v1: transparent rules).

    Factors per stock: technical signal (SMA20/50+RSI rule), 20-day momentum,
    RSI, volume ratio (today vs 20d avg — detects unusual activity), foreign
    net, and liquidity (traded value). All filters are optional; combining
    them is the "strategy".
    """
    with get_cursor() as cur:
        cur.execute("select distinct code from research.latest_pit order by code")
        codes = [r["code"] for r in cur.fetchall()]
        names: dict[str, str | None] = {}
        cur.execute(
            """select distinct on (code) code, name from research.latest_pit
               order by code, trade_date desc"""
        )
        for r in cur.fetchall():
            names[r["code"]] = r["name"]

    out: list[dict[str, Any]] = []
    with get_cursor() as cur:
        for code in codes:
            df = _valuation_frame(cur, code)
            if df is None or len(df) < min_days:
                continue
            df = calculate_indicators(
                df.assign(open=df["close"], high=df["close"], low=df["close"])
            )
            sig = generate_signal(df)
            close = df["close"]
            momo = (close.iloc[-1] / close.iloc[-21] - 1) * 100 if len(close) > 21 else None
            rsi = _f(df["RSI"].iloc[-1])
            vol = df["volume"].fillna(0)
            vol_ratio = float(vol.iloc[-1] / vol.tail(20).mean()) if vol.tail(20).mean() > 0 else None

            # Latest day liquidity + foreign net from EOD table
            cur.execute(
                """select value, foreign_net, close from research.latest_pit
                   where code = %s order by trade_date desc limit 1""",
                (code,),
            )
            eod = cur.fetchone()
            value = _f(eod["value"]) if eod else None
            fnet = _f(eod["foreign_net"]) if eod else None
            last_close = _f(eod["close"]) if eod else _f(close.iloc[-1])

            row = {
                "code": code,
                "name": names.get(code),
                "close": last_close,
                "percent": None,
                "rsi": rsi,
                "signal": sig,
                "trend_up": bool((df["MA_Short"].iloc[-1] or 0) > (df["MA_Long"].iloc[-1] or 0)),
                "momentum_20d": round(momo, 2) if momo is not None else None,
                "vol_ratio": round(vol_ratio, 2) if vol_ratio is not None else None,
                "foreign_net": fnet,
                "value": value,
                "hist_days": len(df),
            }

            # ---- apply filters
            if signal and sig != signal:
                continue
            if rsi_min is not None and (rsi is None or rsi < rsi_min):
                continue
            if rsi_max is not None and (rsi is None or rsi > rsi_max):
                continue
            if min_momentum is not None and (momo is None or momo < min_momentum):
                continue
            if max_momentum is not None and (momo is None or momo > max_momentum):
                continue
            if min_value is not None and (value is None or value < min_value):
                continue
            if foreign_in_only and (fnet is None or fnet <= 0):
                continue
            if min_vol_ratio is not None and (vol_ratio is None or vol_ratio < min_vol_ratio):
                continue
            out.append(row)

    out.sort(key=lambda r: r["momentum_20d"] if r["momentum_20d"] is not None else -999, reverse=True)
    return out[:limit]
