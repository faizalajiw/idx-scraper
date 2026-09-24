"""IDX client — session management + endpoint wrappers.

Uses curl_cffi to bypass Cloudflare TLS fingerprinting on idx.co.id.
All endpoints are unofficial/internal IDX APIs used for personal research.
"""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import yfinance as yf
from curl_cffi import requests as cf_requests
from pydantic import BaseModel

from .models import EodStockRow, IndexQuote, StockQuote

WIB = timezone(timedelta(hours=7))
IDX_BASE = "https://www.idx.co.id"


class IDXClient:
    def __init__(self) -> None:
        self.session = cf_requests.Session(impersonate="chrome", timeout=25.0)
        self._warmed = False

    def warmup(self) -> None:
        if self._warmed:
            return
        self.session.get(f"{IDX_BASE}/id")
        time.sleep(1.0)
        self.session.get(f"{IDX_BASE}/primary/home/GetIndexList")
        self._warmed = True

    def _get_json(self, path: str) -> Any | None:
        self.warmup()
        try:
            r = self.session.get(f"{IDX_BASE}{path}")
            if r.status_code != 200:
                print(f"[warn] IDX {path} status {r.status_code}", file=sys.stderr)
                return None
            return r.json()
        except Exception as e:
            print(f"[warn] IDX {path} gagal: {e}", file=sys.stderr)
            return None

    # --- Index / IHSG ---

    def fetch_index_list(self) -> list[IndexQuote]:
        """Fetch all indices (COMPOSITE, LQ45, etc.) near-real-time."""
        raw = self._get_json("/primary/home/GetIndexList")
        if not isinstance(raw, list):
            return []
        now = datetime.now(WIB)
        results: list[IndexQuote] = []
        for item in raw:
            code = item.get("IndexCode", "")
            if not code:
                continue
            results.append(IndexQuote(
                source="IDX",
                code=code,
                close=_parse_float(item.get("Closing")),
                change=_parse_float(item.get("Change")),
                percent=_parse_pct(item.get("Percent")),
                current=_parse_float(item.get("Current")),
                captured_at=now,
                metadata={"time": item.get("Time")},
            ))
        return results

    def fetch_index_chart(self, index_code: str = "COMPOSITE", period: str = "1D") -> dict[str, Any] | None:
        """Fetch intraday chart data for an index."""
        return self._get_json(f"/primary/helper/GetIndexChart?indexCode={index_code}&period={period}")

    # --- Per-emiten snapshot ---

    def fetch_trading_daily(self, code: str) -> StockQuote | None:
        """Near-real-time price snapshot for a single ticker."""
        raw = self._get_json(f"/primary/ListedCompany/GetTradingInfoDaily?code={code}")
        if not raw or not raw.get("SecurityCode"):
            return None
        now = datetime.now(WIB)
        return StockQuote(
            source="IDX",
            code=raw["SecurityCode"],
            board=raw.get("BoardCode"),
            previous=_pf(raw.get("PreviousPrice")),
            open=_pf(raw.get("OpeningPrice")),
            high=_pf(raw.get("HighestPrice")),
            low=_pf(raw.get("LowestPrice")),
            close=_pf(raw.get("ClosingPrice")),
            change=_pf(raw.get("Change")),
            volume=_pi(raw.get("TradedVolume")),
            value=_pf(raw.get("TradedValue")),
            frequency=_pi(raw.get("TradedFrequency")),
            bid=_pf(raw.get("BestBidPrice")),
            bid_volume=_pi(raw.get("BestBidVolume")),
            offer=_pf(raw.get("BestOfferPrice")),
            offer_volume=_pi(raw.get("BestOfferVolume")),
            foreign_net=_pf(raw.get("NumberForeigner")),
            captured_at=now,
        )

    # --- EOD stock summary ---

    def fetch_stock_summary(self, date: str) -> list[EodStockRow]:
        """EOD OHLC for all tickers on a given date (YYYYMMDD)."""
        raw = self._get_json(f"/primary/TradingSummary/GetStockSummary?date={date}")
        if not raw or not isinstance(raw.get("data"), list):
            return []
        now = datetime.now(WIB)
        results: list[EodStockRow] = []
        for item in raw["data"]:
            # IDX ForeignBuy/ForeignSell are in SHARES (lembar), not IDR; there
            # is no ForeignNet field, so derive it in the same unit.
            fbuy = _pf(item.get("ForeignBuy"))
            fsell = _pf(item.get("ForeignSell"))
            fnet = (fbuy - fsell) if (fbuy is not None and fsell is not None) else (fbuy if fsell is None else (None if fbuy is None else -fsell))
            results.append(EodStockRow(
                source="IDX",
                date=date,
                code=item.get("StockCode", ""),
                name=item.get("StockName"),
                board=item.get("BoardCode"),
                previous=_pf(item.get("PreviousPrice")),
                open=_pf(item.get("OpenPrice")),
                high=_pf(item.get("High")),
                low=_pf(item.get("Low")),
                close=_pf(item.get("Close")),
                change=_pf(item.get("Change")),
                volume=_pi(item.get("Volume")),
                value=_pf(item.get("Value")),
                frequency=_pi(item.get("Frequency")),
                bid=_pf(item.get("BestBidPrice")),
                bid_volume=_pi(item.get("BestBidVolume")),
                offer=_pf(item.get("BestOfferPrice")),
                offer_volume=_pi(item.get("BestOfferVolume")),
                foreign_buy=fbuy,
                foreign_sell=fsell,
                foreign_net=fnet,
                individual_index=_pf(item.get("IndividualIndex")),
                captured_at=now,
            ))
        return results

    def fetch_full_market_today(self) -> list[EodStockRow]:
        """Shortcut to fetch EOD summary for TODAY (market close data)."""
        today = datetime.now(WIB).strftime("%Y%m%d")
        return self.fetch_stock_summary(today)

    # --- Master data ---

    def fetch_securities_stock(self, start: int = 0, length: int = 9999) -> list[dict[str, Any]]:
        raw = self._get_json(f"/primary/StockData/GetSecuritiesStock?start={start}&length={length}")
        if not raw or not isinstance(raw.get("data"), list):
            return []
        return raw["data"]

    def close(self) -> None:
        self.session.close()


def _parse_float(val: Any) -> float | None:
    if val is None:
        return None
    # JSON numbers arrive as int/float already — never strip their decimal point.
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s:
        return None
    # Indonesian locale string ("6.384,726"): '.' = ribuan, ',' = desimal.
    # Plain string ("6200.0"): '.' is already the decimal separator.
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _parse_pct(val: Any) -> float | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).replace("%", "").strip()
    if not s:
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


_pf = _parse_float


def _pi(val: Any) -> int | None:
    f = _parse_float(val)
    return int(f) if f is not None else None


class StockHistorical(BaseModel):
    """Daily OHLCV bar from Yahoo Finance."""

    ticker: str
    date: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    adjusted_close: float | None = None


def fetch_stock_historical(
    ticker: str,
    start: str | None = None,
    end: str | None = None,
    days: int = 60,
) -> list[StockHistorical]:
    """Fetch daily historical OHLCV data using yfinance."""
    if not ticker.endswith(".JK"):
        ticker = ticker + ".JK"

    if start is None:
        start = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
    if end is None:
        end = datetime.now(UTC).strftime("%Y-%m-%d")

    tick = yf.Ticker(ticker)
    df = tick.history(start=start, end=end)
    if df.empty:
        return []
    rows = []
    for date, row in df.iterrows():
        try:
            rows.append(
                StockHistorical(
                    ticker=ticker.replace(".JK", ""),  # store original ticker without suffix
                    date=date.to_pydatetime().astimezone(UTC),
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=int(row["Volume"]),
                    adjusted_close=None,
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    return rows
