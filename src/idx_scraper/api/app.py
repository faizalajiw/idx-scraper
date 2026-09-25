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

from idx_scraper import watchlist_store

from . import services
from .config import get_settings
from .database import close_pool, get_cursor, init_pool
from .schemas import (
    ForeignFlow,
    HoldCheckResponse,
    MarketNarration,
    MarketOverview,
    ScreenerRow,
    SectorAnalysis,
    SectorRRG,
    SessionMovers,
    Signal,
    StockBrokerSummary,
    TechnicalChart,
    ValuationResponse,
    WatchlistRow,
    WatchlistUpdate,
    QualityOverview,
    QuarantineRow,
    QuarantineReason,
    CoverageGaps,
    ThinDay,
    CorpActionSummary,
)
from . import analytics
from . import quality


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
    allow_methods=["GET", "POST", "PUT", "DELETE"],
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


@app.post("/api/watchlist", response_model=list[WatchlistRow])
def add_to_watchlist(payload: WatchlistUpdate) -> list[dict]:
    """Add tickers to the .env-backed watchlist and return the updated rows."""
    try:
        codes = watchlist_store.update_watchlist(add=payload.codes, remove=[])
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Cannot persist watchlist: {e}")
    settings.watchlist = codes  # keep the cached settings in sync
    return services.get_watchlist(codes)


@app.put("/api/watchlist", response_model=list[WatchlistRow])
def replace_watchlist(payload: WatchlistUpdate) -> list[dict]:
    """Replace the whole watchlist (persisted to IDX_WATCHLIST in .env)."""
    codes = list(dict.fromkeys(c.strip().upper() for c in payload.codes if c.strip()))
    if not codes:
        raise HTTPException(status_code=422, detail="codes must not be empty")
    try:
        codes = watchlist_store.write_watchlist(codes)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Cannot persist watchlist: {e}")
    settings.watchlist = codes
    return services.get_watchlist(codes)


@app.delete("/api/watchlist/{code}", response_model=list[WatchlistRow])
def remove_from_watchlist(code: str) -> list[dict]:
    """Remove one ticker from the .env-backed watchlist."""
    symbol = code.strip().upper()
    if symbol not in watchlist_store.read_watchlist():
        raise HTTPException(status_code=404, detail=f"{symbol} is not in the watchlist")
    try:
        codes = watchlist_store.update_watchlist(add=[], remove=[symbol])
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Cannot persist watchlist: {e}")
    settings.watchlist = codes
    return services.get_watchlist(codes)


@app.get("/api/hold-check", response_model=HoldCheckResponse)
def hold_check(
    codes: str | None = Query(default=None, description="Comma-separated tickers; defaults to IDX_WATCHLIST"),
    min_days: int = Query(default=40, ge=1, le=500),
) -> dict:
    """Combined technical + valuation "still worth holding" verdict per ticker."""
    resolved = _resolve_codes(codes)
    items = services.get_hold_check(resolved, min_days=min_days)
    from . import analytics

    with get_cursor() as cur:
        date = analytics._latest_eod_date(cur)
    return {"date": date, "items": items}


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


# --------------------------------------------------------------- analytics v2


@app.get("/api/stocks/{code}/brokers", response_model=StockBrokerSummary)
def stock_brokers(code: str, date: str | None = Query(default=None)) -> dict:
    return analytics.get_broker_summary(code.upper(), date=date)


@app.get("/api/foreign-flow", response_model=ForeignFlow)
def foreign_flow(days: int = Query(default=20, ge=1, le=120)) -> dict:
    return analytics.get_foreign_flow(days=days)


@app.get("/api/sectors/rrg", response_model=SectorRRG)
def sectors_rrg(
    benchmark: str = Query(default="COMPOSITE", description="Benchmark index code from index_quotes"),
    window: int = Query(default=21, ge=10, le=120, description="Trailing weekly window for normalization"),
    tail_weeks: int = Query(default=8, ge=1, le=52, description="Weekly trail points per sector"),
) -> dict:
    """RRG (Relative Rotation Graph) positions per sector vs the benchmark."""
    return analytics.get_sector_rrg(benchmark=benchmark, window=window, tail_weeks=tail_weeks)


@app.get("/api/sectors", response_model=SectorAnalysis)
def sectors(date: str | None = Query(default=None)) -> dict:
    return analytics.get_sector_analysis(date=date)


@app.get("/api/market/narration", response_model=MarketNarration)
def market_narration() -> dict:
    return analytics.get_market_narration()


@app.get("/api/valuation", response_model=ValuationResponse)
def valuation() -> dict:
    return analytics.get_valuation()


@app.get("/api/screener", response_model=list[ScreenerRow])
def screener(
    signal: str | None = Query(default=None, pattern="^(BUY|SELL|HOLD)$"),
    rsi_min: float | None = Query(default=None, ge=0, le=100),
    rsi_max: float | None = Query(default=None, ge=0, le=100),
    min_momentum: float | None = Query(default=None),
    max_momentum: float | None = Query(default=None),
    min_value: float | None = Query(default=None, ge=0),
    foreign_in_only: bool = Query(default=False),
    min_vol_ratio: float | None = Query(default=None, ge=0),
    min_days: int = Query(default=30, ge=1, le=500),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict]:
    return analytics.get_screener(
        signal=signal,
        rsi_min=rsi_min,
        rsi_max=rsi_max,
        min_momentum=min_momentum,
        max_momentum=max_momentum,
        min_value=min_value,
        foreign_in_only=foreign_in_only,
        min_vol_ratio=min_vol_ratio,
        min_days=min_days,
        limit=limit,
    )

# --------------------------------------------------------------- data quality

@app.get("/api/quality/overview", response_model=QualityOverview)
def quality_overview() -> dict:
    """Headline health of the research.* layer: coverage, freshness, counts."""
    return quality.get_quality_overview()

@app.get("/api/quality/quarantine", response_model=list[QuarantineRow])
def quality_quarantine(limit: int = Query(default=100, ge=1, le=1000)) -> list[dict]:
    """Most recent rows rejected by the quality gate, with reason + raw payload."""
    return quality.get_quarantine(limit=limit)

@app.get("/api/quality/quarantine-reasons", response_model=list[QuarantineReason])
def quality_quarantine_reasons() -> list[dict]:
    """Quarantine counts grouped by rejection reason."""
    return quality.get_quarantine_reasons()

@app.get("/api/quality/coverage-gaps", response_model=CoverageGaps)
def quality_coverage_gaps() -> dict:
    """Weekdays in the covered window with no EOD rows (holidays or scrape misses)."""
    return quality.get_coverage_gaps()

@app.get("/api/quality/thin-days", response_model=list[ThinDay])
def quality_thin_days(
    min_codes: int = Query(default=100, ge=1, le=2000),
    limit: int = Query(default=30, ge=1, le=200),
) -> list[dict]:
    """Trading days with unusually few emiten reported (possible partial scrape)."""
    return quality.get_thin_days(min_codes=min_codes, limit=limit)

@app.get("/api/quality/corp-actions", response_model=CorpActionSummary)
def quality_corp_actions() -> dict:
    """Corporate-action counts broken down by type and source."""
    return quality.get_corp_action_summary()

@app.get("/api/quality/duplicates")
def quality_duplicates() -> dict:
    """Count of (code, trade_date) bars with more than one knowledge_date."""
    return quality.get_duplicate_pit()
