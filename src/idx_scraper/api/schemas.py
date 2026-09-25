"""Pydantic response schemas for the API (JSON-serializable, Recharts-ready)."""

from __future__ import annotations

from pydantic import BaseModel


class IndexOverview(BaseModel):
    code: str
    close: float | None = None
    change: float | None = None
    percent: float | None = None
    current: float | None = None
    captured_at: str | None = None


class MarketTotals(BaseModel):
    total_volume: float | None = None
    total_value: float | None = None
    stock_count: int = 0


class Mover(BaseModel):
    code: str
    close: float | None = None
    percent: float | None = None


class MarketOverview(BaseModel):
    index: IndexOverview | None = None
    totals: MarketTotals
    top_gainers: list[Mover]
    top_losers: list[Mover]


class SessionMovers(BaseModel):
    date: str | None = None
    captured_at: str | None = None
    top_gainers: list[Mover]
    top_losers: list[Mover]


class WatchlistRow(BaseModel):
    code: str
    close: float | None = None
    change: float | None = None
    percent: float | None = None
    volume: float | None = None
    foreign_net: float | None = None
    hist_days: int = 0


class WatchlistUpdate(BaseModel):
    """Full replacement list for the .env-backed watchlist."""

    codes: list[str]


class Signal(BaseModel):
    code: str
    signal: str
    close: float | None = None
    pct: float | None = None
    rsi: float | None = None
    sma20: float | None = None
    macd: float | None = None
    trend_up: bool
    live: bool


class PriceBar(BaseModel):
    date: str
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None


class IndicatorBar(PriceBar):
    ma_short: float | None = None
    ma_long: float | None = None
    rsi: float | None = None
    bb_upper: float | None = None
    bb_lower: float | None = None
    macd: float | None = None
    macd_signal: float | None = None


class BrokerRow(BaseModel):
    broker: str
    code: str | None = None
    buy_value: float = 0.0
    sell_value: float = 0.0
    net: float = 0.0
    buy_rank: int | None = None
    sell_rank: int | None = None


class StockBrokerSummary(BaseModel):
    code: str
    name: str | None = None
    date: str | None = None
    top_buyers: list[BrokerRow]
    top_sellers: list[BrokerRow]


class ForeignFlowDay(BaseModel):
    date: str
    buy: float | None = None
    sell: float | None = None
    net: float | None = None


class ForeignMover(BaseModel):
    code: str
    name: str | None = None
    net: float | None = None
    percent: float | None = None


class ForeignFlow(BaseModel):
    date: str | None = None
    total_buy: float | None = None
    total_sell: float | None = None
    total_net: float | None = None
    days: list[ForeignFlowDay]
    top_net_in: list[ForeignMover]
    top_net_out: list[ForeignMover]


class SectorTopStock(BaseModel):
    code: str
    percent: float | None = None


class SectorRow(BaseModel):
    sector: str
    stock_count: int
    avg_percent: float | None = None
    total_value: float | None = None
    total_foreign_net: float | None = None
    gainers: int
    losers: int
    top_stock: SectorTopStock | None = None


class SectorAnalysis(BaseModel):
    date: str | None = None
    sectors: list[SectorRow]


class RRGPoint(BaseModel):
    """One weekly RRG position of a sector vs the benchmark index."""

    sector: str
    date: str
    rs_ratio: float
    rs_momentum: float


class SectorRRG(BaseModel):
    benchmark: str
    window: int
    date: str | None = None
    points: list[RRGPoint]


class NarrationSection(BaseModel):
    title: str
    icon: str
    tone: str
    text: str


class MarketNarration(BaseModel):
    date: str | None = None
    generated_at: str | None = None
    sections: list[NarrationSection]


class ValuationRow(BaseModel):
    code: str
    name: str | None = None
    close: float | None = None
    z_score: float | None = None
    momentum_pct: float | None = None
    rsi: float | None = None
    trend_up: bool = False
    target_price: float | None = None


class ValuationResponse(BaseModel):
    undervalued: list[ValuationRow]
    overvalued: list[ValuationRow]


class ScreenerRow(BaseModel):
    code: str
    name: str | None = None
    close: float | None = None
    percent: float | None = None
    rsi: float | None = None
    signal: str
    trend_up: bool
    momentum_20d: float | None = None
    vol_ratio: float | None = None
    foreign_net: float | None = None
    value: float | None = None
    hist_days: int


class TechnicalChart(BaseModel):
    code: str
    signal: str
    bars: list[IndicatorBar]


class HoldCheckItem(BaseModel):
    """One ticker's combined technical + valuation "still worth holding" verdict.

    score is 0-100 (higher = healthier hold); verdict is one of
    STRONG HOLD / HOLD / TRIM / EXIT. reasons carries the human-readable
    bullets shown in the UI.
    """

    code: str
    name: str | None = None
    signal: str
    trend_up: bool
    rsi: float | None = None
    macd_bullish: bool = False
    bb_position: str | None = None  # "above" | "inside" | "below"
    z_score: float | None = None
    below_target_pct: float | None = None
    foreign_net: float | None = None
    score: int
    verdict: str
    reasons: list[str]


class HoldCheckResponse(BaseModel):
    date: str | None = None
    items: list[HoldCheckItem]


# --------------------------------------------------------------- data quality

class QualityOverview(BaseModel):
    raw_rows: int
    raw_codes: int
    trading_days: int
    first_day: str | None = None
    last_day: str | None = None
    pit_rows: int
    quarantine_rows: int
    corp_actions: int
    last_ingest: str | None = None
    staleness_hours: float | None = None


class QuarantineRow(BaseModel):
    code: str | None = None
    trade_date: str | None = None
    reason: str
    payload: dict | None = None
    ingested_at: str | None = None


class QuarantineReason(BaseModel):
    reason: str
    count: int


class CoverageWindow(BaseModel):
    first: str
    last: str


class CoverageGaps(BaseModel):
    window: CoverageWindow | None = None
    covered_days: int
    missing_weekdays: list[str]


class ThinDay(BaseModel):
    trade_date: str
    codes: int


class CorpActionSummary(BaseModel):
    by_type: dict[str, int]
    by_source: dict[str, int]
