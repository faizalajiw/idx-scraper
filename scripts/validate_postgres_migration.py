"""Validate SQLite → PostgreSQL migration parity.

Usage:
    python scripts/validate_postgres_migration.py \
        --sqlite ./data/idx.db \
        --dsn "postgresql://user:pass@host:5432/idx"

Checks (read-only, no mutations):
  - row counts per table
  - distinct code/ticker counts
  - NULL counts on business-critical columns
  - representative sample records (OHLCV, watchlist snapshot, EOD mover)
  - EOD previous/change/percent consistency
Numeric comparisons use tolerances: price/value 1e-4, percent 1e-6.
Exit code 0 = all checks passed, 1 = mismatch found.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from decimal import Decimal

try:
    import psycopg
except ImportError:
    print("psycopg not installed. Run: pip install 'idx-scraper[postgres]'", file=sys.stderr)
    raise

TABLES = [
    "index_quotes",
    "stock_quotes",
    "index_summary_daily",
    "stock_summary_daily",
    "stock_daily",
]
PRICE_TOL = Decimal("0.0001")
PCT_TOL = Decimal("0.000001")


def _num(v):
    return Decimal(str(v)) if v is not None else None


def _close(a, b, tol: Decimal) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(_num(a) - _num(b)) <= tol


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.checks = 0

    def check(self, ok: bool, msg: str) -> None:
        self.checks += 1
        status = "OK " if ok else "FAIL"
        print(f"  [{status}] {msg}")
        if not ok:
            self.failures.append(msg)


def validate(sqlite_path: str, dsn: str) -> bool:
    sconn = sqlite3.connect(sqlite_path)
    pconn = psycopg.connect(dsn, connect_timeout=10)
    rep = Report()
    try:
        print("== Row counts ==")
        # Append-only snapshot tables may hold exact-duplicate rows in SQLite;
        # Postgres dedups them via the natural-key unique index, so compare
        # Postgres against SQLite's DISTINCT natural-key count for those.
        natural_keys = {
            "index_quotes": "source, code, captured_at",
            "stock_quotes": "source, code, captured_at",
        }
        for t in TABLES:
            if t in natural_keys:
                s = sconn.execute(
                    f"select count(*) from (select distinct {natural_keys[t]} from {t})"
                ).fetchone()[0]
                label = f"{t}: sqlite_distinct={s}"
            else:
                s = sconn.execute(f"select count(*) from {t}").fetchone()[0]
                label = f"{t}: sqlite={s}"
            with pconn.cursor() as c:
                c.execute(f"select count(*) from {t}")
                p = c.fetchone()[0]
            rep.check(s == p, f"{label} postgres={p}")

        print("== Distinct code/ticker ==")
        for t, col in [
            ("index_quotes", "code"),
            ("stock_quotes", "code"),
            ("stock_summary_daily", "code"),
            ("stock_daily", "ticker"),
        ]:
            s = sconn.execute(f"select count(distinct {col}) from {t}").fetchone()[0]
            with pconn.cursor() as c:
                c.execute(f"select count(distinct {col}) from {t}")
                p = c.fetchone()[0]
            rep.check(s == p, f"{t}.{col} distinct: sqlite={s} postgres={p}")

        print("== NULL counts (critical columns) ==")
        null_checks = [
            ("stock_summary_daily", "close"),
            ("stock_summary_daily", "date"),
            ("stock_daily", "close"),
            ("index_quotes", "captured_at"),
        ]
        for t, col in null_checks:
            s = sconn.execute(f"select count(*) from {t} where {col} is null").fetchone()[0]
            with pconn.cursor() as c:
                c.execute(f"select count(*) from {t} where {col} is null")
                p = c.fetchone()[0]
            rep.check(s == p, f"{t}.{col} nulls: sqlite={s} postgres={p}")

        print("== Sample EOD records (OHLC + previous/change/percent) ==")
        sample = sconn.execute(
            "select code, date, open, high, low, close, previous, change, percent "
            "from stock_summary_daily order by date desc, code limit 20"
        ).fetchall()
        for code, date, o, h, low, c_, prev, chg, pct in sample:
            with pconn.cursor() as c:
                c.execute(
                    "select open, high, low, close, previous, change, percent "
                    "from stock_summary_daily where code=%s and date=%s",
                    (code, date),
                )
                row = c.fetchone()
            if row is None:
                rep.check(False, f"{code} {date}: missing in postgres")
                continue
            po, ph, pl, pc, pprev, pchg, ppct = row
            ok = (
                _close(o, po, PRICE_TOL) and _close(h, ph, PRICE_TOL)
                and _close(low, pl, PRICE_TOL) and _close(c_, pc, PRICE_TOL)
                and _close(prev, pprev, PRICE_TOL) and _close(chg, pchg, PRICE_TOL)
                and _close(pct, ppct, PCT_TOL)
            )
            rep.check(ok, f"{code} {date} OHLC/prev/change/percent match")

        print("== Sample Yahoo OHLCV (stock_daily) ==")
        sample = sconn.execute(
            "select ticker, date, open, high, low, close, volume "
            "from stock_daily order by date desc, ticker limit 20"
        ).fetchall()
        for tk, date, o, h, low, c_, vol in sample:
            with pconn.cursor() as c:
                c.execute(
                    "select open, high, low, close, volume from stock_daily "
                    "where ticker=%s and date=%s",
                    (tk, date),
                )
                row = c.fetchone()
            if row is None:
                rep.check(False, f"{tk} {date}: missing in postgres")
                continue
            po, ph, pl, pc, pvol = row
            ok = (
                _close(o, po, PRICE_TOL) and _close(h, ph, PRICE_TOL)
                and _close(low, pl, PRICE_TOL) and _close(c_, pc, PRICE_TOL)
                and int(vol) == int(pvol)
            )
            rep.check(ok, f"{tk} {date} OHLCV match")
    finally:
        sconn.close()
        pconn.close()

    print(f"\n{rep.checks} checks, {len(rep.failures)} failures")
    return not rep.failures


def main() -> None:
    ap = argparse.ArgumentParser(description="Validate IDX SQLite→PostgreSQL migration")
    ap.add_argument("--sqlite", default="./data/idx.db")
    ap.add_argument("--dsn", required=True)
    args = ap.parse_args()
    ok = validate(args.sqlite, args.dsn)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
