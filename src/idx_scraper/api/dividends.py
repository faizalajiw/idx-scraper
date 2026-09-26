"""Dividend & corporate-action service.

Read-only aggregation over ``research.corporate_actions`` (dividends, splits and
reverse splits ingested from Yahoo Finance) joined with ``research.latest_pit``
for the current price, so a stream of cash dividends can be turned into a
trailing yield.

Honest limits, surfaced in the API so the UI never over-promises:

* the source only carries corporate actions that have **already happened**, so
  there is no forward cum/ex-date calendar. ``recent`` is what was just paid
  out, not what is coming;
* ``cash_amount`` is per share, in IDR, as published by the issuer, and is not
  restated for splits that happened afterwards;
* a trailing yield built from a one-off special dividend looks spectacular and
  means nothing — which is why every row also carries its payment count.

The arithmetic lives in pure helpers (`yield_pct`, `summarize_dividends`) so it
can be tested without a database.
"""

from __future__ import annotations

import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .database import get_cursor

WIB = timezone(timedelta(hours=7))

TTM_DAYS = 365
RECENT_DAYS = 90
MAX_LIMIT = 500
BY_YEAR_LOOKBACK = 15  # years kept for the bar chart

DIVIDEND_DISCLAIMER = (
    "Dividen historis, bukan jadwal ke depan. Sumber gratis ini hanya memuat "
    "aksi korporasi yang sudah terjadi, jadi tidak ada tanggal cum/ex-date "
    "mendatang. Yield dihitung dari dividen tunai 12 bulan terakhir dibagi "
    "harga terakhir — bukan proyeksi dan bukan rekomendasi."
)


def _f(v: Any) -> float | None:
    """Decimal/str -> float, tolerating None.

    Note on units: `cash_amount` is a per-share figure, so sums are only
    meaningful *within one emiten* ("dividen per lembar sejak 2000"). Across
    emiten we report counts and yields instead of a fake money total.
    """
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _d(v: Any) -> str | None:
    """date/datetime -> ISO string, anything else -> str, None stays None."""
    if v is None:
        return None
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return str(v)


def yield_pct(cash: float | None, close: float | None) -> float | None:
    """Trailing dividend yield in percent, or None when the price is unusable."""
    if not cash or not close or close <= 0:
        return None
    return round(cash / close * 100, 2)


def summarize_dividends(
    payments: list[tuple[str, float]],
    as_of: str | None,
    ttm_days: int = TTM_DAYS,
) -> dict[str, Any]:
    """Pure: fold (ex_date, cash) payments into totals, annual buckets and growth.

    ``as_of`` anchors the trailing window, so the result is stable no matter
    when the query runs. Returns ISO dates so it can go straight into JSON.
    """
    ordered = sorted(((ex, cash) for ex, cash in payments if ex), key=lambda p: p[0], reverse=True)
    cutoff = date.fromisoformat(as_of) - timedelta(days=ttm_days) if as_of else None
    ttm_cash = (
        sum(cash for ex, cash in ordered if date.fromisoformat(ex) > cutoff)
        if cutoff
        else 0.0
    )

    by_year: dict[int, list[float]] = {}
    for ex, cash in ordered:
        by_year.setdefault(int(ex[:4]), []).append(cash)

    annual = [
        {"year": year, "cash": round(sum(by_year[year]), 4), "events": len(by_year[year])}
        for year in sorted(by_year, reverse=True)[:BY_YEAR_LOOKBACK]
    ]
    growth_pct = None
    if len(annual) >= 2 and annual[1]["cash"] > 0:
        growth_pct = round(
            (annual[0]["cash"] - annual[1]["cash"]) / annual[1]["cash"] * 100, 2
        )

    return {
        "ttm_cash": round(ttm_cash, 4),
        "ttm_events": sum(1 for ex, _ in ordered if cutoff and date.fromisoformat(ex) > cutoff),
        "total_cash": round(sum(cash for _, cash in ordered), 4),
        "total_events": len(ordered),
        "first_ex_date": ordered[-1][0] if ordered else None,
        "last_ex_date": ordered[0][0] if ordered else None,
        "annual": annual,
        "growth_pct": growth_pct,
    }


def _as_of(cur) -> date | None:
    cur.execute("select max(trade_date) as d from research.latest_pit")
    return cur.fetchone()["d"]


# Shared CTE: the newest known bar per emiten, i.e. the price to divide by.
_PX_CTE = """px as (
    select distinct on (code) code, name, close
    from research.latest_pit
    order by code, trade_date desc
)"""


def get_overview() -> dict[str, Any]:
    """Headline dividend picture: totals, history by year, recent payouts, top yields."""
    with get_cursor() as cur:
        as_of = _as_of(cur)
        if as_of is None:
            return {
                "as_of": None,
                "generated_at": None,
                "ttm_days": TTM_DAYS,
                "recent_days": RECENT_DAYS,
                "totals": {
                    "events": 0,
                    "codes": 0,
                    "splits": 0,
                    "first_ex_date": None,
                    "last_ex_date": None,
                    "ttm_events": 0,
                    "ttm_codes": 0,
                    "avg_ttm_yield": None,
                    "median_ttm_yield": None,
                    "max_ttm_yield": None,
                },
                "by_year": [],
                "recent": [],
                "top_yield": [],
                "disclaimer": DIVIDEND_DISCLAIMER,
            }

        cur.execute(
            """select
                 count(*)             as events,
                 count(distinct code) as codes,
                 min(ex_date)         as first_ex,
                 max(ex_date)         as last_ex
               from research.corporate_actions
               where action_type = 'dividend' and cash_amount > 0"""
        )
        total = cur.fetchone()

        cur.execute(
            """select count(*) as n
               from research.corporate_actions
               where action_type in ('split', 'reverse_split')"""
        )
        splits = cur.fetchone()["n"]

        cur.execute(
            """select
                 count(*)             as events,
                 count(distinct code) as codes
               from research.corporate_actions
               where action_type = 'dividend' and cash_amount > 0
                 and ex_date > %s::date - make_interval(days => %s)
                 and ex_date <= %s::date""",
            (as_of, TTM_DAYS, as_of),
        )
        ttm = cur.fetchone()

        cur.execute(
            """select extract(year from ex_date)::int as year,
                      count(*)                        as events,
                      count(distinct code)            as codes
               from research.corporate_actions
               where action_type = 'dividend' and cash_amount > 0
               group by 1 order by 1"""
        )
        by_year = [
            {"year": int(r["year"]), "events": r["events"], "codes": r["codes"]}
            for r in cur.fetchall()
            if int(r["year"]) >= as_of.year - BY_YEAR_LOOKBACK
        ]

        cur.execute(
            f"""with {_PX_CTE},
               ttm as (
                 select code, sum(cash_amount) as cash, count(*) as n
                 from research.corporate_actions
                 where action_type = 'dividend' and cash_amount > 0
                   and ex_date > %s::date - make_interval(days => %s)
                   and ex_date <= %s::date
                 group by code
               )
               select t.code, p.name, p.close, t.cash, t.n
               from ttm t left join px p on p.code = t.code""",
            (as_of, TTM_DAYS, as_of),
        )
        yielders = [
            {
                "code": r["code"],
                "name": r["name"],
                "close": _f(r["close"]),
                "ttm_cash": _f(r["cash"]) or 0.0,
                "ttm_events": r["n"],
                "yield_pct": yield_pct(_f(r["cash"]), _f(r["close"])),
            }
            for r in cur.fetchall()
        ]
        yielders.sort(key=lambda x: (x["yield_pct"] is None, -(x["yield_pct"] or 0.0)))

        cur.execute(
            f"""with {_PX_CTE}
               select ca.code, ca.ex_date, ca.cash_amount, p.name, p.close
               from research.corporate_actions ca
               left join px p on p.code = ca.code
               where ca.action_type = 'dividend' and ca.cash_amount > 0
                 and ca.ex_date > %s::date - make_interval(days => %s)
                 and ca.ex_date <= %s::date
               order by ca.ex_date desc, ca.code asc
               limit 60""",
            (as_of, RECENT_DAYS, as_of),
        )
        recent = [
            {
                "code": r["code"],
                "name": r["name"],
                "ex_date": _d(r["ex_date"]),
                "cash_amount": _f(r["cash_amount"]),
                "close": _f(r["close"]),
            }
            for r in cur.fetchall()
        ]

    yields = [y["yield_pct"] for y in yielders if y["yield_pct"] is not None]

    return {
        "as_of": _d(as_of),
        "generated_at": datetime.now(WIB).isoformat(timespec="seconds"),
        "ttm_days": TTM_DAYS,
        "recent_days": RECENT_DAYS,
        "totals": {
            "events": total["events"],
            "codes": total["codes"],
            "splits": splits,
            "first_ex_date": _d(total["first_ex"]),
            "last_ex_date": _d(total["last_ex"]),
            "ttm_events": ttm["events"],
            "ttm_codes": ttm["codes"],
            "avg_ttm_yield": round(statistics.fmean(yields), 2) if yields else None,
            "median_ttm_yield": round(statistics.median(yields), 2) if yields else None,
            "max_ttm_yield": max(yields) if yields else None,
        },
        "by_year": by_year,
        "recent": recent,
        "top_yield": yielders[:25],
        "disclaimer": DIVIDEND_DISCLAIMER,
    }


def get_stocks(
    min_yield: float | None = None,
    limit: int = 200,
    sort: str = "yield",
) -> list[dict[str, Any]]:
    """Every emiten that has ever paid cash, with its trailing-12-month yield.

    Sorted by trailing yield (default), total cash paid, or most recent payout.
    ``min_yield`` drops rows below a threshold; emiten with no usable price keep
    their row but sort last.
    """
    with get_cursor() as cur:
        as_of = _as_of(cur)
        if as_of is None:
            return []
        cur.execute(
            f"""with {_PX_CTE},
               hist as (
                 select code, count(*) as events, sum(cash_amount) as cash,
                        min(ex_date) as first_ex, max(ex_date) as last_ex
                 from research.corporate_actions
                 where action_type = 'dividend' and cash_amount > 0
                 group by code
               ),
               ttm as (
                 select code, sum(cash_amount) as cash, count(*) as n
                 from research.corporate_actions
                 where action_type = 'dividend' and cash_amount > 0
                   and ex_date > %s::date - make_interval(days => %s)
                   and ex_date <= %s::date
                 group by code
               )
               select h.code, h.events, h.cash, h.first_ex, h.last_ex,
                      p.name, p.close,
                      coalesce(t.cash, 0) as ttm_cash,
                      coalesce(t.n, 0)    as ttm_events
               from hist h
               left join ttm t on t.code = h.code
               left join px p on p.code = h.code""",
            (as_of, TTM_DAYS, as_of),
        )
        rows = cur.fetchall()

    items: list[dict[str, Any]] = []
    for r in rows:
        close = _f(r["close"])
        ttm_cash = _f(r["ttm_cash"]) or 0.0
        y = yield_pct(ttm_cash, close)
        if min_yield is not None and (y is None or y < min_yield):
            continue
        items.append(
            {
                "code": r["code"],
                "name": r["name"],
                "close": close,
                "ttm_cash": ttm_cash,
                "ttm_events": r["ttm_events"],
                "yield_pct": y,
                "total_events": r["events"],
                # Per-share accumulation, meaningful only within this emiten and
                # only as a raw sum (not split-adjusted).
                "total_cash_per_share": _f(r["cash"]) or 0.0,
                "first_ex_date": _d(r["first_ex"]),
                "last_ex_date": _d(r["last_ex"]),
            }
        )

    if sort == "yield":
        items.sort(key=lambda x: (x["yield_pct"] is None, -(x["yield_pct"] or 0.0), x["code"]))
    elif sort == "recent":
        items.sort(key=lambda x: (x["last_ex_date"] or "", x["code"]), reverse=True)
    else:  # "cash": most cash paid per share over the emiten's whole history
        items.sort(key=lambda x: (x["total_cash_per_share"], x["code"]), reverse=True)
    return items[: min(max(limit, 1), MAX_LIMIT)]


def get_stock(code: str) -> dict[str, Any] | None:
    """One emiten's cash-dividend history, trailing metrics and split history."""
    symbol = code.strip().upper()
    with get_cursor() as cur:
        as_of = _as_of(cur)
        cur.execute(
            """select name, close
               from research.latest_pit
               where code = %s
               order by trade_date desc
               limit 1""",
            (symbol,),
        )
        px = cur.fetchone()

        cur.execute(
            """select ex_date, cash_amount
               from research.corporate_actions
               where code = %s and action_type = 'dividend' and cash_amount > 0
               order by ex_date desc""",
            (symbol,),
        )
        payments = [(_d(r["ex_date"]) or "", _f(r["cash_amount"]) or 0.0) for r in cur.fetchall()]

        cur.execute(
            """select ex_date, action_type, ratio
               from research.corporate_actions
               where code = %s and action_type <> 'dividend'
               order by ex_date desc""",
            (symbol,),
        )
        splits = cur.fetchall()

        if px is None and not payments and not splits:
            return None

    close = _f(px["close"]) if px else None
    summary = summarize_dividends(payments, _d(as_of))

    return {
        "code": symbol,
        "name": px["name"] if px else None,
        "as_of": _d(as_of),
        "close": close,
        **summary,
        "yield_pct": yield_pct(summary["ttm_cash"], close),
        "history": [{"ex_date": ex, "cash_amount": cash} for ex, cash in sorted(payments)[::-1]],
        "splits": [
            {
                "ex_date": _d(r["ex_date"]),
                "action_type": r["action_type"],
                "ratio": _f(r["ratio"]),
            }
            for r in splits
        ],
        "disclaimer": DIVIDEND_DISCLAIMER,
    }


def get_corporate_actions(
    code: str | None = None,
    action_type: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Flat corporate-action ledger (newest first), optionally filtered."""
    clauses: list[str] = []
    params: list[Any] = []
    if code:
        clauses.append("ca.code = %s")
        params.append(code.strip().upper())
    if action_type:
        clauses.append("ca.action_type = %s")
        params.append(action_type.strip().lower())
    where = f"where {' and '.join(clauses)}" if clauses else ""
    params.append(min(max(limit, 1), MAX_LIMIT))

    with get_cursor() as cur:
        cur.execute(
            f"""with {_PX_CTE}
                select ca.code, p.name, ca.ex_date, ca.action_type,
                       ca.ratio, ca.cash_amount, ca.source
                from research.corporate_actions ca
                left join px p on p.code = ca.code
                {where}
                order by ca.ex_date desc, ca.code asc
                limit %s""",
            tuple(params),
        )
        rows = cur.fetchall()

    return [
        {
            "code": r["code"],
            "name": r["name"],
            "ex_date": _d(r["ex_date"]),
            "action_type": r["action_type"],
            "ratio": _f(r["ratio"]),
            "cash_amount": _f(r["cash_amount"]),
            "source": r["source"],
        }
        for r in rows
    ]
