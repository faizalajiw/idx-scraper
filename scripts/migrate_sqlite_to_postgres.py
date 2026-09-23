"""Migrate IDX data from SQLite → PostgreSQL (idempotent, batched, parameterized).

Usage:
    python scripts/migrate_sqlite_to_postgres.py \
        --sqlite ./data/idx.db \
        --dsn "postgresql://user:pass@host:5432/idx"

The target schema is created by SupabaseStorage._init_schema (same DDL used at runtime),
so we reuse it here to guarantee parity. All inserts are ON CONFLICT-safe so the script
can be re-run without duplicating rows. SQLite is never modified.
"""

from __future__ import annotations

import argparse
import ast
import json
import sqlite3
import sys
from typing import Any

try:
    import psycopg
except ImportError:
    print("psycopg not installed. Run: pip install 'idx-scraper[postgres]'", file=sys.stderr)
    raise

from idx_scraper.storage import SupabaseStorage

BATCH = 1000


def _metadata_to_json(raw: Any) -> str:
    """Legacy SQLite stored metadata as Python dict repr; convert to JSON text."""
    if raw is None:
        return "{}"
    s = str(raw)
    try:
        return json.dumps(json.loads(s))
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        return json.dumps(ast.literal_eval(s))
    except (ValueError, SyntaxError):
        return "{}"


def _rows(cur: sqlite3.Cursor, table: str, cols: list[str]):
    col_sql = ", ".join(cols)
    cur.execute(f"select {col_sql} from {table}")
    while True:
        batch = cur.fetchmany(BATCH)
        if not batch:
            break
        yield batch


def _copy_table(
    sconn: sqlite3.Connection,
    pconn: psycopg.Connection,
    table: str,
    cols: list[str],
    conflict: str,
    transforms: dict[str, Any] | None = None,
) -> int:
    transforms = transforms or {}
    placeholders = ", ".join(["%s"] * len(cols))
    col_sql = ", ".join(cols)
    insert = f"insert into {table} ({col_sql}) values ({placeholders}) {conflict}"
    total = 0
    scur = sconn.cursor()
    with pconn.cursor() as pcur:
        for batch in _rows(scur, table, cols):
            params = []
            for row in batch:
                vals = []
                for c, v in zip(cols, row):
                    vals.append(transforms[c](v) if c in transforms else v)
                params.append(tuple(vals))
            pcur.executemany(insert, params)
            total += len(params)
    return total


def migrate(sqlite_path: str, dsn: str) -> dict[str, int]:
    sconn = sqlite3.connect(sqlite_path)
    storage = SupabaseStorage(dsn)  # creates schema if missing
    pconn = storage._conn
    counts: dict[str, int] = {}
    try:
        counts["index_quotes"] = _copy_table(
            sconn, pconn, "index_quotes",
            ["source", "code", "close", "change", "percent", "current", "captured_at", "metadata"],
            "on conflict do nothing",
            {"metadata": _metadata_to_json},
        )
        counts["stock_quotes"] = _copy_table(
            sconn, pconn, "stock_quotes",
            ["source", "code", "board", "previous", "open", "high", "low", "close", "change",
             "volume", "value", "frequency", "bid", "bid_volume", "offer", "offer_volume",
             "foreign_net", "captured_at", "metadata"],
            "on conflict do nothing",
            {"metadata": _metadata_to_json},
        )
        counts["index_summary_daily"] = _copy_table(
            sconn, pconn, "index_summary_daily",
            ["code", "close", "open", "high", "low", "volume", "value", "date", "captured_at"],
            "on conflict do nothing",
        )
        counts["stock_summary_daily"] = _copy_table(
            sconn, pconn, "stock_summary_daily",
            ["code", "close", "open", "high", "low", "previous", "change", "percent",
             "volume", "value", "date", "captured_at"],
            "on conflict (code, date) do nothing",
        )
        counts["stock_daily"] = _copy_table(
            sconn, pconn, "stock_daily",
            ["ticker", "date", "open", "high", "low", "close", "volume", "adjusted_close"],
            "on conflict (ticker, date) do nothing",
        )
    finally:
        sconn.close()
        storage.close()
    return counts


def main() -> None:
    ap = argparse.ArgumentParser(description="Migrate IDX SQLite data to PostgreSQL")
    ap.add_argument("--sqlite", default="./data/idx.db", help="path to SQLite db")
    ap.add_argument("--dsn", required=True, help="PostgreSQL DSN")
    args = ap.parse_args()
    counts = migrate(args.sqlite, args.dsn)
    print("Migration complete (rows inserted, excluding conflicts):")
    for t, n in counts.items():
        print(f"  {t:22} {n}")


if __name__ == "__main__":
    main()
