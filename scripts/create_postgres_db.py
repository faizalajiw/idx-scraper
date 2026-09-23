"""Create the target database if it doesn't exist, then run schema init.

Reads DATABASE_URL from the environment (loaded from .env.local by the caller).
Connects to the maintenance 'postgres' database to issue CREATE DATABASE,
then SupabaseStorage builds the schema on first connection to the target DB.
Safe to re-run: skips creation if the database already exists.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit

import psycopg


def _target_db(dsn: str) -> str:
    path = urlsplit(dsn).path.lstrip("/")
    if not path:
        raise SystemExit("DATABASE_URL has no database name in its path")
    return path


def _maintenance_dsn(dsn: str) -> str:
    parts = urlsplit(dsn)
    return urlunsplit((parts.scheme, parts.netloc, "/postgres", parts.query, parts.fragment))


def main() -> None:
    dsn = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL not set (put it in .env.local)")

    dbname = _target_db(dsn)
    with psycopg.connect(_maintenance_dsn(dsn), autocommit=True, connect_timeout=10) as conn:
        exists = conn.execute(
            "select 1 from pg_database where datname = %s", (dbname,)
        ).fetchone()
        if exists:
            print(f"database '{dbname}' already exists")
        else:
            # identifier can't be parameterized; dbname comes from local .env.local, not user input
            conn.execute(f'create database "{dbname}"')
            print(f"created database '{dbname}'")

    # Build schema by connecting to the target DB (triggers _init_schema)
    from idx_scraper.storage import SupabaseStorage

    storage = SupabaseStorage(dsn)
    storage.close()
    print(f"schema ready on '{dbname}'")


if __name__ == "__main__":
    main()
