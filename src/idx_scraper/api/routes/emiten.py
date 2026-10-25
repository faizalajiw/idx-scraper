"""Rute per emiten: pencarian, watchlist, hold-check, sinyal, harga."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Query

from idx_scraper import watchlist_store

from .. import analytics, services
from ..database import get_cursor
from ..schemas import (
    HoldCheckResponse,
    Signal,
    StockSearchResult,
    TechnicalChart,
    WatchlistRow,
    WatchlistUpdate,
)
from ._shared import Router, _resolve_codes, settings

route = Router()


@route.get("/api/search", response_model=list[StockSearchResult])
def search_stocks(
    q: str = Query(default="", max_length=50, description="Kode atau nama emiten"),
    limit: int = Query(default=10, ge=1, le=25),
) -> list[dict]:
    """Cari emiten by kode/nama — untuk command palette frontend.

    Query nama langsung dari research.latest_pit; murah karena tidak menghitung
    indikator. Q kosong = hasil kosong (bukan daftar penuh)."""
    return analytics.search_stocks(q, limit)


@route.get("/api/watchlist", response_model=list[WatchlistRow])
def watchlist(
    codes: str | None = Query(
        default=None, description="Comma-separated tickers; defaults to IDX_WATCHLIST"
    ),
) -> list[dict]:
    return services.get_watchlist(_resolve_codes(codes))


@route.post("/api/watchlist", response_model=list[WatchlistRow])
def add_to_watchlist(payload: WatchlistUpdate) -> list[dict]:
    """Add tickers to the .env-backed watchlist and return the updated rows."""
    try:
        codes = watchlist_store.update_watchlist(add=payload.codes, remove=[])
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Cannot persist watchlist: {e}") from e
    settings.watchlist = codes  # keep the cached settings in sync
    return services.get_watchlist(codes)


@route.put("/api/watchlist", response_model=list[WatchlistRow])
def replace_watchlist(payload: WatchlistUpdate) -> list[dict]:
    """Replace the whole watchlist (persisted to IDX_WATCHLIST in .env)."""
    codes = list(dict.fromkeys(c.strip().upper() for c in payload.codes if c.strip()))
    if not codes:
        raise HTTPException(status_code=422, detail="codes must not be empty")
    try:
        codes = watchlist_store.write_watchlist(codes)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Cannot persist watchlist: {e}") from e
    settings.watchlist = codes
    return services.get_watchlist(codes)


@route.delete("/api/watchlist/{code}", response_model=list[WatchlistRow])
def remove_from_watchlist(code: str) -> list[dict]:
    """Remove one ticker from the .env-backed watchlist."""
    symbol = code.strip().upper()
    if symbol not in watchlist_store.read_watchlist():
        raise HTTPException(status_code=404, detail=f"{symbol} is not in the watchlist")
    try:
        codes = watchlist_store.update_watchlist(add=[], remove=[symbol])
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Cannot persist watchlist: {e}") from e
    settings.watchlist = codes
    return services.get_watchlist(codes)


@route.get("/api/hold-check", response_model=HoldCheckResponse)
def hold_check(
    codes: str | None = Query(
        default=None, description="Comma-separated tickers; defaults to IDX_WATCHLIST"
    ),
    min_days: int = Query(default=40, ge=1, le=500),
) -> dict:
    """Combined technical + valuation "still worth holding" verdict per ticker."""
    resolved = _resolve_codes(codes)
    items = services.get_hold_check(resolved, min_days=min_days)
    from . import analytics

    with get_cursor() as cur:
        date = analytics._latest_eod_date(cur)
    return {
        "date": date,
        "items": items,
        "generated_at": datetime.now(timezone(timedelta(hours=7))).isoformat(timespec="seconds"),
    }


@route.get("/api/signals", response_model=list[Signal])
def signals(
    codes: str | None = Query(
        default=None, description="Comma-separated tickers; defaults to IDX_WATCHLIST"
    ),
    min_days: int = Query(default=25, ge=1, le=500),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict]:
    resolved = _resolve_codes(codes)[:limit]
    return services.get_signals(resolved, min_days=min_days)


@route.get("/api/stocks/{code}/history")
def price_history(
    code: str,
    limit: int = Query(default=60, ge=1, le=1000),
) -> list[dict]:
    rows = services.get_price_history(code.upper(), limit=limit)
    if not rows:
        raise HTTPException(status_code=404, detail=f"No price history for {code.upper()}")
    return rows


@route.get("/api/stocks/{code}/technical", response_model=TechnicalChart)
def technical_chart(code: str) -> dict:
    result = services.get_technical_chart(code.upper())
    if not result["bars"]:
        raise HTTPException(status_code=404, detail=f"No chart data for {code.upper()}")
    return result
