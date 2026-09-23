"""FastAPI application entrypoint.

Run locally:
    uvicorn idx_scraper.api.app:app --reload --port 8000

This API is READ-ONLY. It does not ingest or mutate data — the APScheduler CLI
worker remains the sole writer. There is currently NO authentication on these
endpoints; they are intended for local/trusted-network use. Before exposing this
service publicly, add authentication/authorization and rate limiting.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from . import services
from .config import get_settings
from .database import close_pool, init_pool
from .schemas import (
    MarketOverview,
    SessionMovers,
    Signal,
    TechnicalChart,
    WatchlistRow,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_pool()
    yield
    close_pool()


app = FastAPI(
    title="IDX Scraper API",
    version="0.1.0",
    description="Read-only market data & analytics for the IDX dashboard.",
    lifespan=lifespan,
)

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _resolve_codes(codes: str | None) -> list[str]:
    if codes:
        return list(dict.fromkeys(c.strip().upper() for c in codes.split(",") if c.strip()))
    return settings.watchlist


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/market/overview", response_model=MarketOverview)
def market_overview() -> dict:
    return services.get_market_overview()


@app.get("/api/market/session-movers", response_model=SessionMovers)
def session_movers() -> dict:
    return services.get_session_movers()


@app.get("/api/watchlist", response_model=list[WatchlistRow])
def watchlist(codes: str | None = Query(default=None, description="Comma-separated tickers; defaults to IDX_WATCHLIST")) -> list[dict]:
    return services.get_watchlist(_resolve_codes(codes))


@app.get("/api/signals", response_model=list[Signal])
def signals(
    codes: str | None = Query(default=None, description="Comma-separated tickers; defaults to IDX_WATCHLIST"),
    min_days: int = Query(default=25, ge=1, le=500),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict]:
    resolved = _resolve_codes(codes)[:limit]
    return services.get_signals(resolved, min_days=min_days)


@app.get("/api/stocks/{code}/history")
def price_history(
    code: str,
    limit: int = Query(default=60, ge=1, le=1000),
) -> list[dict]:
    rows = services.get_price_history(code.upper(), limit=limit)
    if not rows:
        raise HTTPException(status_code=404, detail=f"No price history for {code.upper()}")
    return rows


@app.get("/api/stocks/{code}/technical", response_model=TechnicalChart)
def technical_chart(code: str) -> dict:
    result = services.get_technical_chart(code.upper())
    if not result["bars"]:
        raise HTTPException(status_code=404, detail=f"No chart data for {code.upper()}")
    return result
