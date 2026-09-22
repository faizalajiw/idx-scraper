"""Pydantic models for normalized market data."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class IndexQuote(BaseModel):
    source: str = "IDX"
    code: str
    close: float | None = None
    change: float | None = None
    percent: float | None = None
    current: float | None = None
    captured_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class StockQuote(BaseModel):
    source: str = "IDX"
    code: str
    board: str | None = None
    previous: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    change: float | None = None
    volume: int | None = None
    value: float | None = None
    frequency: int | None = None
    bid: float | None = None
    bid_volume: int | None = None
    offer: float | None = None
    offer_volume: int | None = None
    foreign_net: float | None = None
    captured_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class EodStockRow(BaseModel):
    """One row from GetStockSummary (EOD OHLC for a single ticker)."""

    source: str = "IDX"
    date: str  # YYYYMMDD
    code: str
    name: str | None = None
    board: str | None = None
    previous: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    change: float | None = None
    volume: int | None = None
    value: float | None = None
    frequency: int | None = None
    bid: float | None = None
    bid_volume: int | None = None
    offer: float | None = None
    offer_volume: int | None = None
    foreign_buy: float | None = None
    foreign_sell: float | None = None
    foreign_net: float | None = None
    individual_index: float | None = None
    captured_at: datetime