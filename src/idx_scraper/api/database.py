"""Postgres connection pool for the read-only API.

Uses ``psycopg_pool.ConnectionPool`` so concurrent requests reuse connections.
All queries run through short-lived connections checked out from the pool and
returned automatically. Rows come back as dicts via ``dict_row``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import get_settings

_pool: ConnectionPool | None = None


def init_pool() -> ConnectionPool:
    """Create the connection pool (idempotent)."""
    global _pool
    if _pool is not None:
        return _pool
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError(
            "DATABASE_URL/SUPABASE_DB_URL is not set; the API requires Postgres."
        )
    def _configure(conn: Any) -> None:
        # Read-only API: autocommit avoids leaving pooled connections INTRANS.
        conn.autocommit = True
        conn.execute("set time zone 'Asia/Jakarta'")  # keep dates/times in WIB

    _pool = ConnectionPool(
        conninfo=settings.database_url,
        min_size=settings.pool_min,
        max_size=settings.pool_max,
        open=True,
        configure=_configure,
    )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def get_cursor() -> Iterator[Any]:
    """Yield a dict-row cursor from a pooled connection (read-only usage)."""
    if _pool is None:
        init_pool()
    assert _pool is not None
    with _pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        yield cur
