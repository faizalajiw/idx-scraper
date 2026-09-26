"""Data-quality service — surfaces the health of the research.* layer.

Read-only aggregations over research.raw_eod / prices_pit / quarantine_eod /
corporate_actions. Powers the data-quality panel: coverage, freshness,
quarantine audit, calendar gaps, and duplicate-knowledge checks.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .database import get_cursor

WIB = timezone(timedelta(hours=7))


def _f(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def get_quality_overview() -> dict[str, Any]:
    """Headline counts + freshness for the research layer."""
    with get_cursor() as cur:
        cur.execute(
            """select
                 (select count(*) from research.raw_eod)                        as raw_rows,
                 (select count(distinct code) from research.raw_eod)             as raw_codes,
                 (select count(distinct trade_date) from research.raw_eod)       as trading_days,
                 (select min(trade_date) from research.raw_eod)                  as first_day,
                 (select max(trade_date) from research.raw_eod)                  as last_day,
                 (select count(*) from research.prices_pit)                      as pit_rows,
                 (select count(*) from research.quarantine_eod)                  as quarantine_rows,
                 (select count(*) from research.corporate_actions)               as corp_actions,
                 (select max(ingested_at) from research.raw_eod)                 as last_ingest"""
        )
        row = cur.fetchone()

    last_ingest = row["last_ingest"]
    staleness_hours = None
    if last_ingest is not None:
        staleness_hours = round(
            (datetime.now(WIB) - last_ingest).total_seconds() / 3600, 1
        )

    return {
        "raw_rows": row["raw_rows"],
        "raw_codes": row["raw_codes"],
        "trading_days": row["trading_days"],
        "first_day": str(row["first_day"]) if row["first_day"] else None,
        "last_day": str(row["last_day"]) if row["last_day"] else None,
        "pit_rows": row["pit_rows"],
        "quarantine_rows": row["quarantine_rows"],
        "corp_actions": row["corp_actions"],
        "last_ingest": last_ingest.isoformat() if last_ingest else None,
        "staleness_hours": staleness_hours,
    }


def get_quarantine(limit: int = 100) -> list[dict[str, Any]]:
    """Most recent quarantined rows with their rejection reason."""
    with get_cursor() as cur:
        cur.execute(
            """select code, trade_date, reason, payload, ingested_at
               from research.quarantine_eod
               order by ingested_at desc, id desc
               limit %s""",
            (limit,),
        )
        rows = cur.fetchall()
    return [
        {
            "code": r["code"],
            "trade_date": str(r["trade_date"]) if r["trade_date"] else None,
            "reason": r["reason"],
            "payload": r["payload"],
            "ingested_at": r["ingested_at"].isoformat() if r["ingested_at"] else None,
        }
        for r in rows
    ]


def get_quarantine_reasons() -> list[dict[str, Any]]:
    """Quarantine counts grouped by rejection reason (for a bar/pie)."""
    with get_cursor() as cur:
        cur.execute(
            """select reason, count(*) as n
               from research.quarantine_eod
               group by reason order by n desc"""
        )
        rows = cur.fetchall()
    return [{"reason": r["reason"], "count": r["n"]} for r in rows]


def get_coverage_gaps() -> dict[str, Any]:
    """Calendar gaps: weekdays in the covered window with no EOD rows at all.

    A missing weekday is likely a holiday (expected) or a scrape miss (bad).
    We can't distinguish without a holiday calendar, so we just surface them.
    """
    with get_cursor() as cur:
        cur.execute("select min(trade_date) as lo, max(trade_date) as hi from research.raw_eod")
        b = cur.fetchone()
        lo, hi = b["lo"], b["hi"]
        if lo is None or hi is None:
            return {"window": None, "missing_weekdays": [], "covered_days": 0}

        cur.execute("select distinct trade_date from research.raw_eod")
        covered = {r["trade_date"] for r in cur.fetchall()}

    missing: list[str] = []
    d = lo
    while d <= hi:
        if d.weekday() < 5 and d not in covered:
            missing.append(str(d))
        d += timedelta(days=1)

    return {
        "window": {"first": str(lo), "last": str(hi)},
        "covered_days": len(covered),
        "missing_weekdays": missing,
    }


def get_thin_days(min_codes: int = 100, limit: int = 30) -> list[dict[str, Any]]:
    """Trading days with unusually few emiten reported (possible partial scrape)."""
    with get_cursor() as cur:
        cur.execute(
            """select trade_date, count(distinct code) as codes
               from research.raw_eod
               group by trade_date
               having count(distinct code) < %s
               order by codes asc, trade_date desc
               limit %s""",
            (min_codes, limit),
        )
        rows = cur.fetchall()
    return [{"trade_date": str(r["trade_date"]), "codes": r["codes"]} for r in rows]


def get_duplicate_pit() -> dict[str, Any]:
    """Sanity: count (code, trade_date) with more than one knowledge_date.

    >1 is legitimate (restatements) but a spike can flag a re-ingest bug.
    """
    with get_cursor() as cur:
        cur.execute(
            """select count(*) as multi_versioned from (
                 select code, trade_date
                 from research.prices_pit
                 group by code, trade_date
                 having count(*) > 1
               ) t"""
        )
        row = cur.fetchone()
    return {"multi_versioned_bars": row["multi_versioned"]}


def get_corp_action_summary() -> dict[str, Any]:
    """Breakdown of corporate actions by type + source."""
    with get_cursor() as cur:
        cur.execute(
            """select action_type, source, count(*) as n
               from research.corporate_actions
               group by action_type, source
               order by action_type, source"""
        )
        rows = cur.fetchall()
    by_type: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for r in rows:
        by_type[r["action_type"]] = by_type.get(r["action_type"], 0) + r["n"]
        by_source[r["source"]] = by_source.get(r["source"], 0) + r["n"]
    return {"by_type": by_type, "by_source": by_source}
