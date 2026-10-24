"""broker_flow: pecahan api/analytics.py (FAI-19)."""

from __future__ import annotations

from typing import Any

from ..database import get_cursor
from ..sector_map import FALLBACK_SECTOR, sector_for
from ._common import _f, _latest_eod_date, _research_cache_get, _research_cache_put


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
        rows.append(
            {
                "broker": "FOREIGN (aggregate)",
                "code": None,
                "buy_value": fbuy,
                "sell_value": fsell or 0.0,
                "net": fbuy - (fsell or 0.0),
                "buy_rank": 1,
                "sell_rank": 1 if fsell is not None else None,
                "estimated": True,
            }
        )
    if avg_bid is not None or avg_offer is not None:
        # Rough IDR estimate using the day's close as reference price.
        ref = _f(eod["close"]) if eod else None
        b = avg_bid * ref if (avg_bid is not None and ref) else None
        o = avg_offer * ref if (avg_offer is not None and ref) else None
        rows.append(
            {
                "broker": "DOMESTIC (bid/offer proxy)",
                "code": None,
                "buy_value": b or 0.0,
                "sell_value": o or 0.0,
                "net": (b or 0.0) - (o or 0.0),
                "buy_rank": 2,
                "sell_rank": 2,
                "estimated": True,
            }
        )

    buyers = sorted(rows, key=lambda r: r["buy_value"], reverse=True)
    sellers = sorted(rows, key=lambda r: r["sell_value"], reverse=True)
    return {
        "code": code,
        "name": name,
        "date": d,
        "top_buyers": buyers,
        "top_sellers": sellers,
    }


def get_foreign_flow(days: int = 20) -> dict[str, Any]:
    """Aggregate foreign buy/sell/net per day from EOD foreign_* columns.

    IDX publishes foreign volumes in SHARES; convert to IDR notional by
    multiplying with the day's close so cross-stock sums are meaningful.
    """
    with get_cursor() as cur:
        cur.execute("select max(trade_date) from research.latest_pit where foreign_net is not null")
        max_date = cur.fetchone()["max"]

        cur.execute(
            """select trade_date as date,
                      sum(foreign_buy * close) as buy,
                      sum(foreign_sell * close) as sell,
                      sum(foreign_net * close) as net
               from research.latest_pit
               where trade_date >= %s::date - %s::int
                 and foreign_net is not null and close is not null
               group by trade_date order by trade_date""",
            (max_date, days),
        )
        by_day = cur.fetchall()

        cur.execute(
            """select code, name, foreign_net * close as net_idr,
                      percent
               from research.latest_pit
               where trade_date = %s
                 and foreign_net is not null and foreign_net <> 0 and close is not null
               order by net_idr desc limit 10""",
            (max_date,),
        )
        inn = cur.fetchall()

        cur.execute(
            """select code, name, foreign_net * close as net_idr,
                      percent
               from research.latest_pit
               where trade_date = %s
                 and foreign_net is not null and foreign_net <> 0 and close is not null
               order by net_idr asc limit 10""",
            (max_date,),
        )
        out = cur.fetchall()

    days_list = [
        {"date": str(r["date"]), "buy": _f(r["buy"]), "sell": _f(r["sell"]), "net": _f(r["net"])}
        for r in by_day
    ]
    latest = days_list[-1] if days_list else None

    def _mover(r: Any) -> dict[str, Any]:
        return {
            "code": r["code"],
            "name": r["name"],
            "net": _f(r["net_idr"]),
            "percent": _f(r["percent"]),
        }

    return {
        "date": latest["date"] if latest else None,
        "total_buy": latest["buy"] if latest else None,
        "total_sell": latest["sell"] if latest else None,
        "total_net": latest["net"] if latest else None,
        "days": days_list,
        "top_net_in": [_mover(r) for r in inn],
        "top_net_out": [_mover(r) for r in out],
    }


def get_stock_foreign_flow(
    code: str, days: int = 30, peer_days: int = 10, peer_limit: int = 10
) -> dict[str, Any]:
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

    from ...foreign_flow import (
        daily_flow,
        rank_in_peers,
        sector_peer_flow,
    )
    from ...foreign_flow import (
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
        {
            "date": str(r["date"]),
            "net_idr": r["net_idr"],
            "buy_idr": r["buy_idr"],
            "sell_idr": r["sell_idr"],
        }
        for r in rows
    ]
    if not self_rows:
        return empty

    flow = daily_flow(self_rows)
    peers = sector_peer_flow(self_rows, peer_days_map, days=peer_days)

    # Batasi daftar yang ditampilkan, tapi emiten sendiri harus tetap terlihat.
    shown = [p for p in peers if not p["is_self"]][:peer_limit]
    if any(p["is_self"] for p in peers):
        shown = sorted(
            [p for p in peers if p["is_self"]] + shown, key=lambda x: x["net_sum"], reverse=True
        )
    shown = [{**p, "code": code if p["is_self"] else p["code"]} for p in shown]

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
