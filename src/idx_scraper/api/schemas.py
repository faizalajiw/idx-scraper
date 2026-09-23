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


class TechnicalChart(BaseModel):
    code: str
    signal: str
    bars: list[IndicatorBar]
