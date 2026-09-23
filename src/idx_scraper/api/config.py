"""Runtime configuration for the FastAPI backend."""

from __future__ import annotations

import os
from functools import lru_cache


def _parse_watchlist(raw: str | None) -> list[str]:
    if not raw:
        return []
    codes = [c.strip().upper() for c in raw.split(",") if c.strip()]
    return list(dict.fromkeys(codes))  # de-dup, preserve order


class Settings:
    """Environment-driven settings.

    The API is Postgres-only: it reads from the same DSN the scraper writes to.
    """

    def __init__(self) -> None:
        self.database_url: str | None = (
            os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
        )
        self.watchlist: list[str] = _parse_watchlist(os.getenv("IDX_WATCHLIST"))
        # Comma-separated list of allowed CORS origins for the Next.js frontend.
        self.cors_origins: list[str] = [
            o.strip()
            for o in os.getenv(
                "IDX_API_CORS_ORIGINS", "http://localhost:3000,http://localhost:3100"
            ).split(",")
            if o.strip()
        ]
        self.pool_min: int = int(os.getenv("IDX_API_POOL_MIN", "1"))
        self.pool_max: int = int(os.getenv("IDX_API_POOL_MAX", "10"))


@lru_cache
def get_settings() -> Settings:
    return Settings()
