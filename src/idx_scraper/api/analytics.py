"""Analytics services for the IDX dashboard feature set.

All computations run on data already stored in Postgres (stock_summary_daily,
stock_quotes, index_quotes, stock_daily). Nothing here writes to the database.

Sectors: IDX-IC classification is not available from the free endpoints we
poll, so we map tickers to sectors with a curated dictionary of the most
liquid IDX names and fall back to "Lainnya" (other). The map lives in
sector_map.py and is intentionally data, not logic.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from ..analysis import (
    calculate_adx,
    calculate_indicators,
    classify_regime,
    generate_signal,
    signal_series,
)
from .database import get_cursor
from .sector_map import FALLBACK_SECTOR, sector_for

WIB = timezone(timedelta(hours=7))

# Cache in-process untuk event study & factors overview: query panel + compute
# butuh detik-an; data harian tidak berubah intraday. Key -> (monotonic_ts, val).
_RESEARCH_CACHE: dict[str, tuple[float, Any]] = {}
_RESEARCH_TTL = 3600.0  # 1 jam


def _research_cache_get(key: str) -> Any | None:
    hit = _RESEARCH_CACHE.get(key)
    if hit and (time.monotonic() - hit[0]) < hit[1]:
        return hit[2]
    return None


def _research_cache_put(key: str, val: Any, ttl: float | None = None) -> None:
    """Simpan ke cache; ``ttl`` khusus untuk hitungan yang mahal & harian."""
    _RESEARCH_CACHE[key] = (time.monotonic(), ttl if ttl is not None else _RESEARCH_TTL, val)

def clear_research_cache() -> int:
    """Kosongkan cache in-process supaya menu langsung menyajikan data terbaru.

    Dipanggil serve loop tiap pipeline refresh (pra-buka / close sesi) — tanpa
    ini Sentimen & Jejak Sinyal bisa menampilkan data basi hingga 1 jam.
    """
    n = len(_RESEARCH_CACHE)
    _RESEARCH_CACHE.clear()
    return n


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
            [(str(r["date"]), _f(r["close"]), _f(r["close"]), _f(r["close"]), _f(r["close"])) for r in rows],
            columns=["date", "open", "high", "low", "close"],
        )

    if df.empty:
        return {"regime": None, "source": None, "as_of": None,
                "generated_at": datetime.now(WIB).isoformat(timespec="seconds")}

    adx_df = calculate_adx(df)
    out = classify_regime(adx_df)
    out["source"] = source
    out["as_of"] = df["date"].iloc[-1][:10]
    out["generated_at"] = datetime.now(WIB).isoformat(timespec="seconds")
    # Nama regime diwarnai UI; arah tren penting untuk konteks sinyal.
    out["dir_hint"] = (
        "up"
        if out["regime"] == "TRENDING_UP"
        else "down" if out["regime"] == "TRENDING_DOWN" else None
    )
    return out


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
            "estimated": True,
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
            "estimated": True,
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


# ------------------------------------------------------- foreign flow per emiten


def get_stock_foreign_flow(code: str, days: int = 30, peer_days: int = 10, peer_limit: int = 10) -> dict[str, Any]:
    """Aliran asing satu emiten: tren harian, flip, dan pembanding sektor.

    Sumber ``research.latest_pit`` (``foreign_net`` dalam SAHAM — dikali close
    hari itu supaya rupiah). Agregasinya di modul pure
    ``idx_scraper.foreign_flow`` (dites tanpa DB), query di sini hanya
    menyiapkan baris ``{date, net_idr, buy_idr, sell_idr}``.

    Pembanding sektor memakai map kurasi ``sector_for`` (pola yang sama dengan
    peer di Aktivitas Broker): emiten di bucket fallback "Lainnya" tetap
    dibandingkan dengan sesama isinya, tapi ditandai ``comparable=False``
    supaya UI tidak mengklaim itu sektor sebenarnya.

    Returns ``code, sector, comparable, days, flow, flip_summary, peers,
    peer_rank``. Emiten tanpa data flow -> struktur dengan ``flow`` kosong
    (tidak pernah raise) supaya UI menampilkan empty state.
    """
    days = max(5, min(days, 120))
    peer_days = max(1, min(peer_days, 60))
    peer_limit = max(3, min(peer_limit, 30))
    code = code.upper()
    cache_key = f"stock_foreign_flow:{code}:{days}:{peer_days}:{peer_limit}"
    cached = _research_cache_get(cache_key)
    if cached is not None:
        return cached

    from ..foreign_flow import (
        daily_flow,
        rank_in_peers,
        sector_peer_flow,
    )
    from ..foreign_flow import (
        flip_summary as _flip_summary,
    )

    empty: dict[str, Any] = {
        "code": code,
        "sector": sector_for(code),
        "comparable": sector_for(code) != FALLBACK_SECTOR,
        "date": None,
        "flow": [],
        "flip_summary": {
            "last_flip": None,
            "days_since_flip": None,
            "current_streak": None,
            "current_side": "flat",
        },
        "peers": [],
        "peer_rank": {"rank": None, "count": 0, "median_sum": None},
    }

    with get_cursor() as cur:
        # Tren emiten: N hari terakhir yang punya baris (close NULL -> baris
        # dilewati; nilai flow NULL dibiarkan lewat agar hari terlihat kosong,
        # bukan dianggap net 0).
        cur.execute(
            """select trade_date as date,
                      foreign_net * close as net_idr,
                      foreign_buy * close as buy_idr,
                      foreign_sell * close as sell_idr
               from (
                   select * from research.latest_pit
                   where code = %s and close is not null
                   order by trade_date desc limit %s
               ) t
               order by trade_date""",
            (code, days),
        )
        rows = cur.fetchall()

        # Peer: emiten lain se-sektor, jendela SESI BURSA (N trade_date
        # terakhir), bukan hari kalender — supaya label "10 sesi" di UI
        # selalu persis 10 hari bursa untuk semua emiten.
        sector = sector_for(code)
        peer_days_map: dict[str, list[dict[str, Any]]] = {}
        if rows:
            cur.execute(
                """select code, trade_date as date, foreign_net * close as net_idr
                   from research.latest_pit
                   where trade_date in (
                       select distinct trade_date from research.latest_pit
                       order by 1 desc limit %s
                   )
                     and close is not null and foreign_net is not null
                   order by trade_date""",
                (peer_days,),
            )
            for r in cur.fetchall():
                c = str(r["code"]).upper()
                if c == code or sector_for(c) != sector:
                    continue
                peer_days_map.setdefault(c, []).append(
                    {"date": str(r["date"]), "net_idr": r["net_idr"]}
                )

    self_rows = [
        {"date": str(r["date"]), "net_idr": r["net_idr"], "buy_idr": r["buy_idr"], "sell_idr": r["sell_idr"]}
        for r in rows
    ]
    if not self_rows:
        return empty

    flow = daily_flow(self_rows)
    peers = sector_peer_flow(self_rows, peer_days_map, days=peer_days)

    # Batasi daftar yang ditampilkan, tapi emiten sendiri harus tetap terlihat.
    shown = [p for p in peers if not p["is_self"]][:peer_limit]
    if any(p["is_self"] for p in peers):
        shown = sorted([p for p in peers if p["is_self"]] + shown, key=lambda x: x["net_sum"], reverse=True)
    shown = [
        {**p, "code": code if p["is_self"] else p["code"]}
        for p in shown
    ]

    latest_date = flow[-1]["date"] if flow else None
    out: dict[str, Any] = {
        "code": code,
        "sector": sector,
        "comparable": sector != FALLBACK_SECTOR,
        "date": latest_date,
        "flow": flow,
        "flip_summary": _flip_summary(flow),
        "peers": shown,
        "peer_rank": rank_in_peers(peers),
    }
    _research_cache_put(cache_key, out)
    return out


# --------------------------------------------------------------- jejak smart money

#: Berapa sesi bursa ditarik untuk konteks streak (lebih panjang dari jendela
#: agregasi, supaya streak bisa menghitung mundur melewati jendela).
_SMART_MONEY_HISTORY = 30


def _smart_money_panel(history_days: int) -> dict[str, list[dict[str, Any]]]:
    """Panel baris harian per emiten untuk N sesi terakhir (satu query).

    Returns ``{code: {"name": ..., "rows": [...]}}`` — baris sudah dalam
    bentuk yang dibutuhkan ``smart_money`` (``date, net_idr, value, close,
    high, low, volume``), urut tanggal menaik per emiten. Baris dengan close
    NULL dilewati (tidak ada harga yang bisa dikalikan).
    """
    from ..smart_money import _f as _fnum

    with get_cursor() as cur:
        cur.execute(
            """select code, name, trade_date as date,
                      foreign_net * close as net_idr,
                      value, close, high, low, volume
               from research.latest_pit
               where trade_date in (
                   select distinct trade_date from research.latest_pit
                   order by 1 desc limit %s
               )
                 and close is not null
               order by code, trade_date""",
            (history_days,),
        )
        panel: dict[str, list[dict[str, Any]]] = {}
        names: dict[str, str] = {}
        for r in cur.fetchall():
            code = str(r["code"]).upper()
            names.setdefault(code, r["name"])
            panel.setdefault(code, []).append(
                {
                    "date": str(r["date"]),
                    "net_idr": _fnum(r["net_idr"]),
                    "value": _fnum(r["value"]),
                    "close": _fnum(r["close"]),
                    "high": _fnum(r["high"]),
                    "low": _fnum(r["low"]),
                    "volume": _fnum(r["volume"]),
                }
            )
    return {c: {"name": names.get(c), "rows": rows} for c, rows in panel.items()}


def get_smart_money_radar(days: int = 10) -> dict[str, Any]:
    """Radar smart money: emiten dengan jejak aliran terkuat di seluruh pasar.

    Sumber ``research.latest_pit``; logika peringkat di modul pure
    ``idx_scraper.smart_money.build_radar`` (dites tanpa DB). Output dibagi
    dua daftar — ``accumulation`` (net buy) dan ``distribution`` (net sell) —
    masing-masing top-``limit`` berdasar |net rupiah|. Cache 30 menit (data
    harian, tidak berubah intraday).
    """
    from ..smart_money import build_radar

    days = max(2, min(days, 60))
    cache_key = f"smart_money_radar:{days}"
    cached = _research_cache_get(cache_key)
    if cached is not None:
        return cached

    panel = _smart_money_panel(_SMART_MONEY_HISTORY)
    ranked = build_radar(panel, days=days)
    accumulation = [r for r in ranked if r["side"] == "akumulasi"][:_RADAR_TOP_N]
    distribution = [r for r in ranked if r["side"] == "distribusi"][:_RADAR_TOP_N]

    out: dict[str, Any] = {
        "days": days,
        "date": ranked[0]["date"] if ranked else None,
        "accumulation": accumulation,
        "distribution": distribution,
        "scanned": len(panel),
    }
    _research_cache_put(cache_key, out, ttl=1800.0)
    return out


_RADAR_TOP_N = 15


def get_stock_smart_money(code: str) -> dict[str, Any]:
    """Jejak smart money satu emiten: verdict + pola + level + narasi.

    Gabungan dari modul pure ``idx_scraper.smart_money``:
    - ``verdict`` (akumulasi/distribusi/netral + streak + ukuran)
    - ``detect_patterns`` (pola klasik yang terdeteksi)
    - ``consolidation_range`` (level pembatalan)
    - ``build_narrative`` (kalimat plain-language untuk banner)
    - ``sector`` konteks: berapa emiten se-sektor yang akumulasi vs distribusi

    Semua dari ``research.latest_pit`` (60 sesi terakhir). Cache 30 menit.
    Emiten tanpa data -> struktur kosong yang aman di-render (``has_data``
    False) supaya UI menampilkan empty state, bukan error.
    """
    from ..smart_money import (
        build_narrative,
        consolidation_range,
        detect_patterns,
    )
    from ..smart_money import (
        verdict as _verdict,
    )

    code = code.strip().upper()
    cache_key = f"smart_money_stock:{code}"
    cached = _research_cache_get(cache_key)
    if cached is not None:
        return cached

    empty: dict[str, Any] = {
        "code": code,
        "has_data": False,
        "verdict": None,
        "patterns": [],
        "range": None,
        "narrative": [],
        "sector": None,
        "date": None,
    }

    with get_cursor() as cur:
        cur.execute(
            """select code, name, trade_date as date,
                      foreign_net * close as net_idr,
                      value, close, high, low, volume
               from (
                   select * from research.latest_pit
                   where code = %s and close is not null
                   order by trade_date desc limit 60
               ) t order by trade_date""",
            (code,),
        )
        rows = cur.fetchall()

    if not rows:
        return empty

    panel_rows = [
        {
            "date": str(r["date"]),
            "net_idr": _f(r["net_idr"]),
            "value": _f(r["value"]),
            "close": _f(r["close"]),
            "high": _f(r["high"]),
            "low": _f(r["low"]),
            "volume": _f(r["volume"]),
        }
        for r in rows
    ]

    v = _verdict(panel_rows)
    pats = detect_patterns(panel_rows)
    rng = consolidation_range(panel_rows)
    narrative = build_narrative(code, v, pats, rng)

    # Konteks sektor: verifikasi apakah emiten ini "janggal" dibanding
    # se-sektor (berapa yang akumulasi vs distribusi di jendela sama).
    sector_ctx: dict[str, Any] | None = None
    sector = sector_for(code)
    try:
        if sector and sector != FALLBACK_SECTOR:
            sector_ctx = _sector_flow_context(code, sector)
    except Exception as e:
        print(f"[warn] konteks sektor dilewati di smart-money: {e}", file=sys.stderr)
        sector_ctx = None

    out: dict[str, Any] = {
        "code": code,
        "name": rows[-1].get("name"),
        "has_data": not v["insufficient"],
        "verdict": v,
        "patterns": pats,
        "range": rng,
        "narrative": narrative,
        "sector": {
            "sector": sector,
            "comparable": sector != FALLBACK_SECTOR,
            **sector_ctx,
        } if sector_ctx is not None else None,
        "date": v["date"],
    }
    _research_cache_put(cache_key, out, ttl=1800.0)
    return out


def _sector_flow_context(code: str, sector: str) -> dict[str, Any] | None:
    """Berapa emiten se-sektor yang akumulasi vs distribusi (jendela 10).

    Kode anggota sektor dari ``sector_map`` (kurasi + generated map); panel
    30 sesi terakhir hanya untuk kode itu (filter di SQL, bukan tarik semua
    pasar). Returns ``{sector_total, accumulating, distributing, position}``
    — ``position`` kalimat plain-language, mis. "1 dari 8 emiten perbankan
    yang akumulasi". None jika anggota ber-data < 3.
    """
    from ..smart_money import (
        VERDICT_ACCUMULATION,
        VERDICT_DISTRIBUTION,
        VERDICT_NEUTRAL,
        verdict,
    )
    from .sector_map import SECTOR_MAP, generated_map

    sector_codes = sorted(
        {c.upper() for c, s in SECTOR_MAP.items() if s == sector}
        | {c.upper() for c, s in generated_map().items() if s == sector}
    )
    if code.upper() not in sector_codes:
        # map tidak memetakan emiten ini ke sektor tsb (override manual bisa
        # terjadi di UI); sertakan supaya konteks tidak kehilangan subjeknya.
        sector_codes.append(code.upper())
    if not sector_codes:
        return None

    with get_cursor() as cur:
        cur.execute(
            """select code, trade_date as date,
                      net_idr,
                      value, close, high, low, volume
               from (
                   select code, trade_date, close, value, high, low,
                          volume, foreign_net * close as net_idr,
                          row_number() over (
                              partition by code order by trade_date desc
                          ) as rn
                     from research.latest_pit
                    where close is not null and code = any(%s)
               ) t
               where t.rn <= 30
               order by code, trade_date""",
            (sector_codes,),
        )
        all_rows = cur.fetchall()

    by_code: dict[str, list[dict[str, Any]]] = {}
    for r in all_rows:
        c = str(r["code"]).upper()
        by_code.setdefault(c, []).append(
            {
                "date": str(r["date"]),
                "net_idr": _f(r["net_idr"]),
                "value": _f(r["value"]),
                "close": _f(r["close"]),
                "high": _f(r["high"]),
                "low": _f(r["low"]),
                "volume": _f(r["volume"]),
            }
        )

    acc = dist = 0
    for c, rows in by_code.items():
        if not rows:
            continue
        v = verdict(rows)
        if v["insufficient"]:
            continue
        if v["side"] == VERDICT_ACCUMULATION:
            acc += 1
        elif v["side"] == VERDICT_DISTRIBUTION:
            dist += 1

    total = len(by_code)
    if total < 3:
        return None

    self_v = verdict(by_code.get(code.upper(), []))
    if self_v["insufficient"] or self_v["side"] == VERDICT_NEUTRAL:
        position = (
            f"Di sektor {sector}, {acc} emiten akumulasi & {dist} distribusi "
            f"(dari {total} ber-data)."
        )
    elif self_v["side"] == VERDICT_ACCUMULATION:
        position = (
            f"Satu dari {acc} emiten di sektor {sector} yang sedang akumulasi "
            f"(dari {total} ber-data)."
        )
    else:
        position = (
            f"Satu dari {dist} emiten di sektor {sector} yang sedang distribusi "
            f"(dari {total} ber-data)."
        )

    return {
        "sector_total": total,
        "accumulating": acc,
        "distributing": dist,
        "position": position,
    }


#: Sejarah untuk track record pola: cukup untuk 10-sesi jendela deteksi
#: + 21-sesi horizon forward terlama + margin, dengan cukup kejadian supaya
#: hit rate & alpha-nya bermakna. 120 sesi ≈ 6 bulan bursa.
_SMART_MONEY_TRACK_HISTORY = 120


def get_smart_money_track_record() -> dict[str, Any]:
    """Track record pola klasik jejak smart money — "pola ini terbukti?"

    Mengumpulkan kejadian pola dari 120 sesi terakhir seluruh pasar
    (:func:`collect_pattern_episodes`), memberi forward return T+1
    (disiplin ``research.signal_log``) + alpha vs pasar equal-weight, lalu
    agregasi via :func:`pattern_track_record`. Ini layer "apakah pola
    ini bisa dipercaya" — melengkapi banner/radar yang hanya memberi
    kondisi hari ini.

    Cache 1 jam (hitungan mahal & harian; tak berubah intraday).
    """
    from ..smart_money import collect_pattern_episodes, pattern_track_record

    cache_key = "smart_money_track_record"
    cached = _research_cache_get(cache_key)
    if cached is not None:
        return cached

    panel = _smart_money_panel(_SMART_MONEY_TRACK_HISTORY)

    # Indeks (code, date) -> index baris, supaya lookup T (hari kejadian)
    # dan forward return O(1) per episode, bukan scan ulang per emiten.
    # Forward return tetap dihitung di hari *bursa* (urut baris per emiten),
    # bukan hari kalender.
    idx: dict[tuple[str, str], int] = {}
    for code, entry in panel.items():
        rows = entry["rows"]
        for j, r in enumerate(rows):
            idx[(code, r["date"])] = j

    closes: dict[str, list[dict[str, Any]]] = {
        code: entry["rows"] for code, entry in panel.items()
    }

    episodes = collect_pattern_episodes(panel)

    # Return pasar equal-weight per (tanggal kejadian, horizon) — dihitung
    # sekali lalu dipakai semua episode di tanggal/horizon yang sama.
    market: dict[tuple[str, int], float | None] = {}

    for ep in episodes:
        code, date = ep["code"], ep["date"]
        i = idx.get((code, date))
        rows = closes.get(code)
        if i is None or not rows:
            continue
        # entry = close di T+1 (hari bursa berikutnya).
        if i + 1 >= len(rows):
            continue
        entry = rows[i + 1].get("close")
        if entry is None or entry <= 0:
            continue
        for h in (5, 10, 21):
            j = i + 1 + h
            exit_ = rows[j].get("close") if j < len(rows) else None
            if exit_ is None:
                ep[f"fwd{h}"] = None
                ep[f"abn{h}"] = None
                continue
            fwd = (exit_ / entry - 1.0) * 100.0
            ep[f"fwd{h}"] = fwd
            mkey = (date, h)
            if mkey not in market:
                market[mkey] = _market_return(closes, idx, date, h)
            mkt = market[mkey]
            ep[f"abn{h}"] = (fwd - mkt) if mkt is not None else None

    out = pattern_track_record(episodes)
    result = {
        "patterns": out,
        "n_episodes": len(episodes),
        "history_sessions": _SMART_MONEY_TRACK_HISTORY,
        "horizon_note": "Forward return entry T+1 (hari bursa), exit T+1+horizon.",
    }
    _research_cache_put(cache_key, result, ttl=3600.0)
    return result


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


# --------------------------------------------------------------- ruang keputusan


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

    from .services import get_hold_check

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
        print(f"[warn] sentimen dilewati di decision: {e}", file=sys.stderr)

    # --- 4) jejak asing 10 sesi (dari modul pure foreign_flow) -------------
    foreign_10: list[dict[str, Any]] = []
    flip: dict[str, Any] | None = None
    try:
        ff = get_stock_foreign_flow(code, days=10, peer_days=10, peer_limit=3)
        foreign_10 = [
            {"date": p["date"], "net": p["net"], "flip": p["flip"]}
            for p in ff.get("flow", [])
        ]
        flip = ff.get("flip_summary", {}).get("last_flip")
    except Exception as e:
        print(f"[warn] foreign flow dilewati di decision: {e}", file=sys.stderr)

    # --- 5) base rate event study emiten -----------------------------------
    events: list[dict[str, Any]] = []
    try:
        ev = get_stock_events(code)
        if ev:
            events = ev.get("events", [])
    except Exception as e:
        print(f"[warn] event study dilewati di decision: {e}", file=sys.stderr)

    # --- 6) level invalidasi teknikal (pure dari frame emiten) -------------
    invalidation: dict[str, Any] | None = None
    try:
        with get_cursor() as cur:
            df, _ = _load_frame_for_decision(cur, code)
        if not df.empty:
            from ..analysis import calculate_indicators

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
        print(f"[warn] level invalidasi dilewati di decision: {e}", file=sys.stderr)

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
    from .services import _build_frame

    return _build_frame(cur, code)


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
        sector = sector_for(r["code"])
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
    min_broker_score: float | None = None,
    min_days: int = 30,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """    Rule-based multi-factor screener ("AI screening" v1: transparent rules).

    Factors per stock: technical signal (SMA20/50+RSI rule), 20-day momentum,
    RSI, volume ratio (today vs 20d avg — detects unusual activity), foreign
    net, liquidity (traded value), order-book flow (ob_imbalance / ob_absorption
    dari snapshot intraday), plus skor aktivitas broker (proksi aliran, bobot
    dari IC bulanan). All filters are optional; combining them is the "strategy".

    ``min_broker_score`` hanya bermakna kalau skor broker sudah tervalidasi. Kalau
    belum, tidak ada emiten yang bisa dinyatakan lolos kriteria yang tidak bisa
    dihitung -> hasilnya kosong, bukan filter yang diam-diam diabaikan.
    """
    ob = _latest_orderbook_factors()
    # Skor broker diambil di luar blok cursor: snapshot-nya membuka koneksi
    # sendiri untuk query panel faktor, jadi jangan tahan koneksi pool menunggu.
    broker_by_code, _broker_validated = broker_scores_by_code()
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

            # --- metrik riset tambahan (ATR%, 52w high, hari sejak sinyal) ---
            # ATR(14) Wilder-style rolling, % dari close (volatilitas komparabel
            # antar emiten). High/low NULL (hari non-trading) -> diabaikan.
            prev_close = close.shift(1)
            tr = pd.concat(
                [
                    df["high"] - df["low"],
                    (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs(),
                ],
                axis=1,
            ).max(axis=1)
            atr = tr.rolling(14).mean()
            atr_pct = (
                float(atr.iloc[-1] / close.iloc[-1] * 100)
                if pd.notna(atr.iloc[-1]) and close.iloc[-1] > 0
                else None
            )
            # Jarak dari puncak 52 minggu (<= 0; min_periods 63 agar emiten baru
            # tidak selalu -100%).
            hi_252 = close.rolling(252, min_periods=63).max()
            dist_52w = (
                float(close.iloc[-1] / hi_252.iloc[-1] - 1.0)
                if pd.notna(hi_252.iloc[-1])
                else None
            )
            # Hari sejak sinyal BUY/SELL terakhir (rule = signal_series, sama
            # dengan generate_signal). None = belum pernah bersinyal non-HOLD.
            sigs = signal_series(df)
            non_hold = sigs[sigs != "HOLD"]
            days_since_signal = int(len(sigs) - 1 - non_hold.index[-1]) if len(non_hold) else None

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
                "atr_pct": round(atr_pct, 2) if atr_pct is not None else None,
                "dist_52w": round(dist_52w * 100, 2) if dist_52w is not None else None,
                "days_since_signal": days_since_signal,
                "ob_imbalance": ob.get(code, {}).get("ob_imbalance"),
                "ob_absorption": ob.get(code, {}).get("ob_absorption"),
                "broker_score": (broker_by_code.get(code) or {}).get("score"),
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
            if min_broker_score is not None:
                bs = (broker_by_code.get(code) or {}).get("score")
                if bs is None or bs < min_broker_score:
                    continue
            out.append(row)

    out.sort(key=lambda r: r["momentum_20d"] if r["momentum_20d"] is not None else -999, reverse=True)
    return out[:limit]


# --------------------------------------------------------------- order-book helper


def _latest_orderbook_factors() -> dict[str, dict[str, float | None]]:
    """Faktor order-book terakhir per emiten (cache 30 menit).

    Dihitung dari snapshot intraday via research.orderbook; None bila emiten
    tidak tercakup capture / snapshot terlalu tipis / sumber bermasalah —
    screener tetap jalan tanpa kolom ini.
    """
    cached = _research_cache_get("ob_latest")
    if cached is not None:
        return cached
    out: dict[str, dict[str, float | None]] = {}
    dsn = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if dsn:
        try:
            from ..research.orderbook import compute_daily, load_snapshots

            ob_daily = compute_daily(load_snapshots(dsn))
            for r in ob_daily.itertuples():
                # groupby mengurutkan (code, date) asc -> okrurrence terakhir
                # per kode = tanggal terbaru.
                out[r.code] = {
                    "ob_imbalance": None if pd.isna(r.ob_imbalance) else round(float(r.ob_imbalance), 4),
                    "ob_absorption": None if pd.isna(r.ob_absorption) else round(float(r.ob_absorption), 4),
                }
        except Exception as e:  # graceful degradation — screener tetap hidup
            print(f"[warn] faktor order-book dilewati: {e}", file=sys.stderr)
    _research_cache_put("ob_latest", out)
    return out


# --------------------------------------------------------------- event study UI


def _event_study_bundle() -> dict[str, Any]:
    """Jalankan event study semua preset sekali, cache 1 jam, share antar kode."""
    cached = _research_cache_get("event_bundle")
    if cached is not None:
        return cached

    from ..research import events as ev

    dsn = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL/SUPABASE_DB_URL is not set")

    # Panel adjusted utk return emiten; market_daily (raw close rata-rata
    # pasar) utk konteks kurva event-time.
    panel = ev.load_panel_from_db(dsn)
    with get_cursor() as cur:
        cur.execute(
            """select trade_date as date, avg(close) as close
               from research.latest_pit group by trade_date order by trade_date"""
        )
        md = pd.Series(
            {pd.Timestamp(r["date"]): _f(r["close"]) for r in cur.fetchall()},
            dtype=float,
        ).dropna()
    ev.set_market_daily(md)

    bundle: dict[str, Any] = {"events": {}, "panel": panel}
    for name in sorted(ev.EVENT_PRESETS):
        bundle["events"][name] = ev.run_event_study(panel, name, horizon=21, min_gap=10)
    # Hitungannya mahal (semua preset × seluruh panel, ~2 menit saat dingin)
    # dan datanya harian — TTL 6 jam, bukan 1 jam default.
    _research_cache_put("event_bundle", bundle, ttl=6 * 3600.0)
    return bundle


def get_stock_events(code: str) -> dict[str, Any] | None:
    """Statistik event study utk satu emiten: riwayat + agregat pasar.

    Returns None bila emiten tidak ditemukan di panel (kode salah / terlalu
    pendek histori).
    """
    from ..research import events as ev  # ev dipakai utk PRESETS di bawah

    code = code.strip().upper()
    bundle = _event_study_bundle()
    panel: pd.DataFrame = bundle["panel"]
    if panel.empty or code not in set(panel["code"]):
        return None

    out_events: list[dict[str, Any]] = []
    for name, res in bundle["events"].items():
        ev_rows = res.events
        mine = (
            ev_rows[ev_rows["code"] == code] if not ev_rows.empty else pd.DataFrame()
        )
        out_events.append(
            {
                "event": name,
                "description": ev.EVENT_PRESETS[name]["description"],
                "my_count": len(mine),
                "my_last_date": (
                    str(pd.Timestamp(mine["date"].max()).date())
                    if not mine.empty
                    else None
                ),
                "my_median_fwd": (
                    float(mine["fwd"].median()) if not mine.empty else None
                ),
                "my_median_abnormal": (
                    float(mine["abnormal"].median()) if not mine.empty else None
                ),
                # baseline pasar (semua emiten, semua event)
                "market_count": int(res.n_events),
                "market_hit_rate": res.hit_rate,
                "market_median_fwd": res.median_fwd,
                "market_mean_abnormal": res.mean_abnormal,
            }
        )
    last_date = str(pd.Timestamp(panel["date"].max()).date())
    return {"code": code, "as_of": last_date, "events": out_events}


# --------------------------------------------------------------- factors/IC UI


def get_factors_overview() -> dict[str, Any]:
    """Ringkasan kalibrasi faktor: run terbaru + bobot aktif + histori panjang."""
    cached = _research_cache_get("factors_overview")
    if cached is not None:
        return cached

    with get_cursor() as cur:
        cur.execute("select max(run_date) as d from research.factor_ic_history")
        latest = cur.fetchone()["d"]
        if not latest:
            from ..research.factors import FACTOR_DEFINITIONS

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
    weights = {
        f["factor"]: f["weight"] for f in factors if f["horizon"] == 10 and f["weight"]
    }
    if not weights:
        from ..research.composite import FACTOR_WEIGHTS

        weights = {"vol_21d": FACTOR_WEIGHTS["vol"], "turnover_21d": FACTOR_WEIGHTS["turnover"], "dist_52w_high": FACTOR_WEIGHTS["dist_52w"]}

    from ..research.factors import FACTOR_DEFINITIONS

    out = {
        "latest_run": str(latest),
        "weights": weights,
        "factors": factors,
        "history": history,
        "definitions": dict(FACTOR_DEFINITIONS),
    }
    _research_cache_put("factors_overview", out)
    return out


# --------------------------------------------------------------- regime history


def get_regime_history(days: int = 90) -> dict[str, Any]:
    """Histori regime harian + agregat jangka panjang dari research.regime_daily."""
    import psycopg

    dsn = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL/SUPABASE_DB_URL is not set")

    from ..research.regime import backfill, summary

    # Idempoten & murah: backfill menghitung ulang dari index_summary_daily
    # (SQL window, sekilas saja) sehingga histori selalu terkini tanpa job.
    try:
        backfill(dsn)
    except Exception:
        pass  # tabel belum siap -> tetap sajikan yang ada

    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn, conn.cursor() as cur:
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


# --------------------------------------------------------------- sentimen (flow & buku)


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

    from ..research import sentiment

    dsn = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL/SUPABASE_DB_URL is not set")

    with get_cursor() as cur:
        idx = _latest_index(cur)
    index_pct = idx["percent"] if idx else None

    # Order-book adalah enhancement: capture bisa tidak tersedia / tipis.
    try:
        ob = _latest_orderbook_factors()
    except Exception as e:
        print(f"[warn] sentimen tanpa faktor order-book: {e}", file=sys.stderr)
        ob = {}

    out = sentiment.compute(dsn, index_pct=index_pct, ob_by_code=ob, limit=limit)
    _research_cache_put(cache_key, out)
    return out


# --------------------------------------------------------------- signal track record


def get_signal_track() -> dict[str, Any]:
    """Track record sinyal BUY/SELL dari research.signal_log (cache 1 jam).

    Kalau log masih kosong, backfill dijalankan sekali (idempoten) supaya
    halaman punya isi tanpa harus menunggu job harian. Sinyal dinilai di close
    T+1 dan dibandingkan dengan pasar equal-weight pada window yang sama.
    """
    cached = _research_cache_get("signal_track")
    if cached is not None:
        return cached

    from ..research import signal_log as sl

    dsn = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL/SUPABASE_DB_URL is not set")

    try:
        if sl.load_log(dsn).empty:
            sl.backfill(dsn)
    except Exception as e:  # DB belum siap -> tetap sajikan apa adanya
        print(f"[warn] backfill jejak sinyal dilewati: {e}", file=sys.stderr)

    out = sl.evaluate(dsn, horizons=sl.DEFAULT_HORIZONS)
    out["generated_at"] = datetime.now(WIB).isoformat(timespec="seconds")
    _research_cache_put("signal_track", out)
    return out


# --------------------------------------------------------------- broker flow (kategori)


def get_broker_flow(days: int = 20, top_n: int = 5) -> dict[str, Any]:
    """Komposisi nilai transaksi per kategori broker: asing / lokal / BUMN.

    Sumber: ``research.broker_daily`` (EOD per firma, seluruh pasar). Kategori
    dari map kurasi kode broker (``idx_scraper.broker_flow``) — kode asing
    (UBS, CGS, JPM, ...) dipisah dari lokal, dan sekuritas BUMN (Mandiri, BNI,
    BRI Danareksa, Bahana) dipisah sendiri.

    Yang diukur adalah KOMPOSISI (turnover share), bukan net buy/sell: IDX
    tidak mempublikasikan split beli/jual per firma di endpoint publik.

    Returns ``date, captured_at, n_brokers, total_value, categories,
    top, history, classification``. Tabel kosong -> struktur kosong (tidak
    pernah raise) supaya UI menampilkan empty state.
    """
    days = max(1, min(days, 120))
    top_n = max(1, min(top_n, 20))
    cache_key = f"broker_flow:{days}:{top_n}"
    cached = _research_cache_get(cache_key)
    if cached is not None:
        return cached

    from ..broker_flow import (
        CATEGORIES,
        classify_broker,
        composition,
        composition_series,
        top_brokers_by_category,
    )

    empty: dict[str, Any] = {
        "date": None,
        "captured_at": None,
        "n_brokers": 0,
        "total_value": None,
        "categories": {c: {"value": None, "share": None, "n_brokers": 0} for c in CATEGORIES},
        "top": {c: [] for c in CATEGORIES},
        "history": [],
        "classification": [],
    }
    with get_cursor() as cur:
        cur.execute("select to_regclass('research.broker_daily') as t")
        reg = cur.fetchone()
        if not reg or not reg["t"]:
            return empty
        cur.execute(
            """select trade_date as d, broker_code, broker_name, value, captured_at
               from research.broker_daily
               where trade_date >= (select max(trade_date) from research.broker_daily) - %s::int
                 and broker_code is not null
               order by trade_date""",
            (days,),
        )
        rows = cur.fetchall()

    if not rows:
        return empty

    by_date: dict[str, list[dict[str, Any]]] = {}
    captured: dict[str, Any] = {}
    for r in rows:
        d = str(r["d"])
        by_date.setdefault(d, []).append(r)
        if r.get("captured_at") and (captured.get(d) is None or r["captured_at"] > captured[d]):
            captured[d] = r["captured_at"]

    latest = max(by_date)
    comp = composition(by_date[latest])
    tops = top_brokers_by_category(by_date[latest], top_n)

    out: dict[str, Any] = {
        "date": latest,
        "captured_at": captured.get(latest).isoformat() if captured.get(latest) else None,
        "n_brokers": comp["n_brokers"],
        "total_value": comp["total_value"],
        "categories": comp["categories"],
        "top": tops,
        "history": composition_series(by_date),
        "classification": [
            {
                "broker_code": str(r["broker_code"]),
                "broker_name": r.get("broker_name"),
                "category": classify_broker(str(r["broker_code"])),
            }
            for r in sorted(by_date[latest], key=lambda x: x.get("value") or 0, reverse=True)
        ],
    }
    _research_cache_put(cache_key, out)
    return out


# --------------------------------------------------------------- broker activity

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

    from ..research import broker_activity as ba
    from ..research.ic_history import WEIGHT_HORIZON

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

    dsn = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not dsn:
        snap["reason"] = "DATABASE_URL/SUPABASE_DB_URL belum diisi"
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
    from ..research.ic import build_factor_panel

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

    from .services import get_broker_concentration

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
                "close": m.get("close")
                if m.get("close") is not None
                else _f(r["close"]),
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


def _sector_peers(
    code: str,
    scores: pd.DataFrame,
    as_of: Any,
    limit: int = 15,
) -> dict[str, Any] | None:
    """Skor emiten lain di sektor yang sama, dari cross-section terakhir.

    Pembanding diambil dari emiten yang SUDAH punya skor. Emiten sektor ini yang
    skornya belum terbentuk tidak muncul — bukan dianggap 0, karena "tidak ada
    data" dan "skornya jelek" adalah dua hal berbeda.

    Emiten di bucket fallback ``Lainnya`` ditandai ``comparable=False``:
    "Lainnya" bukan sektor, jadi membandingkan skor di dalamnya tidak berarti.
    Returns None kalau emiten tidak punya skor sama sekali.
    """
    if scores.empty or "code" not in scores.columns:
        return None
    if not (scores["code"] == code).any():
        return None

    sector = sector_for(code)
    if sector == FALLBACK_SECTOR:
        return {
            "name": sector,
            "comparable": False,
            "peer_count": 0,
            "my_rank": None,
            "median_score": None,
            "peers": [],
        }

    members = scores[scores["code"].map(lambda c: sector_for(str(c)) == sector)]
    if members.empty:
        return None

    ordered = members.sort_values("score", ascending=False).reset_index(drop=True)
    # Peringkat dihitung sebelum pemotongan daftar: emiten peringkat 18 harus
    # tetap dilaporkan 18 walau hanya 15 baris yang ditampilkan.
    pos = ordered.index[ordered["code"] == code]
    shown = ordered.head(limit)

    names = _broker_activity_meta([str(c) for c in shown["code"]], as_of)
    return {
        "name": sector,
        "comparable": True,
        "peer_count": len(members),
        "my_rank": int(pos[0]) + 1 if len(pos) else None,
        "median_score": round(float(members["score"].median()), 1),
        "peers": [
            {
                "code": str(r["code"]),
                "name": names.get(str(r["code"]), {}).get("name"),
                "score": round(float(r["score"]), 1),
                "coverage": round(float(r["coverage"]), 2),
                "is_self": str(r["code"]) == code,
            }
            for _, r in shown.iterrows()
        ],
    }


def _rotation_delta(history: pd.DataFrame, sessions: int) -> float | None:
    """Perubahan median skor sektor vs ``sessions`` sesi sebelumnya (None bila kurang)."""
    if len(history) <= sessions:
        return None
    return round(
        float(history["median_score"].iloc[-1] - history["median_score"].iloc[-1 - sessions]),
        1,
    )


def get_sector_rotation(lookback: int = 60, min_names: int = 3) -> dict[str, Any]:
    """Rotasi sektor dari skor aktivitas broker (proksi aliran dana).

    Beda dari RRG di halaman Sektor (rotasi berbasis HARGA relatif), ini rotasi
    berbasis JEJAK ALIRAN: sektor mana yang skor akumulasinya sedang naik.

    Tiap tanggal diagregasi per sektor (median skor + breadth), lalu rotasi
    diukur dari perubahan median — bukan levelnya. Sektor dengan kurang dari
    ``min_names`` emiten berskor dibuang, dan emiten di bucket fallback
    "Lainnya" tidak diikutkan karena itu bukan sektor (jumlahnya dilaporkan
    terpisah sebagai ``unmapped_names`` supaya tetap jujur berapa yang tercakup).
    """
    lookback = max(5, min(lookback, 250))
    min_names = max(2, min(min_names, 50))
    cache_key = f"sector_rotation:{lookback}:{min_names}"
    cached = _research_cache_get(cache_key)
    if cached is not None:
        return cached

    from ..research import broker_activity as ba

    snap = broker_activity_snapshot()
    as_of = snap["as_of"]
    out: dict[str, Any] = {
        "as_of": str(as_of.date()) if as_of is not None else None,
        "validated": bool(snap["validated"]),
        "reason": snap["reason"],
        "horizon": snap["horizon"],
        "ic_run_date": snap["ic_run_date"],
        "lookback": lookback,
        "min_names": min_names,
        "unmapped_names": 0,
        "sectors": [],
    }

    if not snap["validated"]:
        _research_cache_put(cache_key, out)
        return out

    panel: pd.DataFrame = snap["panel"]
    scores: pd.DataFrame = snap["scores"]
    if panel.empty or scores.empty:
        out["reason"] = (
            "Skor sudah tervalidasi, tapi panel faktor belum cukup panjang untuk "
            "menghitung rotasi sektor."
        )
        _research_cache_put(cache_key, out)
        return out

    codes = sorted({str(c).upper() for c in panel["code"]})
    sector_of = {c: sector_for(c) for c in codes}
    # Berapa emiten berskor yang belum punya sektor sebenarnya — dilaporkan,
    # tidak disembunyikan, supaya cakupan rotasi bisa dinilai apa adanya.
    out["unmapped_names"] = sum(
        1 for c in (str(x).upper() for x in scores["code"]) if sector_for(c) == FALLBACK_SECTOR
    )

    hist = ba.sector_score_history(
        panel,
        snap["factors"],
        sector_of,
        lookback=lookback,
        min_names=min_names,
        exclude_sectors={FALLBACK_SECTOR},
    )
    if hist.empty:
        out["reason"] = (
            "Belum ada sektor dengan cukup emiten berskor untuk dihitung "
            f"(minimal {min_names} emiten per sektor)."
        )
        _research_cache_put(cache_key, out)
        return out

    sectors: list[dict[str, Any]] = []
    for sector, group in hist.groupby("sector"):
        history = group.sort_values("date")
        latest = history.iloc[-1]
        level = round(float(latest["median_score"]), 1)
        delta_5d = _rotation_delta(history, 5)
        sectors.append(
            {
                "sector": str(sector),
                "n_names": int(latest["n_names"]),
                "median_score": level,
                "breadth": round(float(latest["breadth"]), 2),
                "delta_5d": delta_5d,
                "delta_21d": _rotation_delta(history, 21),
                # Fase ditentukan dari perubahan 5 sesi: pertanyaan "sedang ke mana"
                # lebih relevan untuk rotasi daripada "sedang di mana".
                "phase": ba.rotation_phase(level, delta_5d),
                "history": [round(float(v), 1) for v in history["median_score"].tail(30)],
            }
        )

    # Urut dari yang paling sedang menguat (rotasi masuk), bukan dari level.
    sectors.sort(key=lambda s: s["delta_5d"] if s["delta_5d"] is not None else -999.0, reverse=True)
    out["sectors"] = sectors
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

    from ..research import broker_activity as ba

    snap = broker_activity_snapshot()
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

    history = ba.composite_score_history(
        snap["panel"], snap["factors"], code, lookback=lookback
    )
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
