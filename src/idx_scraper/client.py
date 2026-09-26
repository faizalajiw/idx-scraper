"""IDX client — session management + endpoint wrappers.

Uses curl_cffi to bypass Cloudflare TLS fingerprinting on idx.co.id.
All endpoints are unofficial/internal IDX APIs used for personal research.
"""

from __future__ import annotations

import math
import sys
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import yfinance as yf
from curl_cffi import requests as cf_requests
from pydantic import BaseModel

from .cf_transport import BrowserTransport
from .models import EodStockRow, IndexQuote, StockQuote

WIB = timezone(timedelta(hours=7))
IDX_BASE = "https://www.idx.co.id"


class IDXClient:
    def __init__(self) -> None:
        # Transport is created lazily — in Yahoo-only / cooldown mode we never
        # want to launch Chrome or touch IDX at all.
        self.session: BrowserTransport | None = None
        self._warmed = False

    def _ensure_session(self) -> BrowserTransport:
        if self.session is None:
            self.session = BrowserTransport()
        return self.session

    def warmup(self) -> None:
        if self._warmed:
            return
        self._ensure_session().ensure_open()
        self._warmed = True

    def _get_json(self, path: str) -> Any | None:
        self.warmup()
        try:
            r = self._ensure_session().get(path)
            if r is None:
                return None
            return r
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
        if self.session is not None:
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


# --- Yahoo Finance live quotes (fallback while IDX is behind Cloudflare) ---

# IDX index code -> Yahoo ticker. COMPOSITE (IHSG) is the one the dashboard reads.
YAHOO_INDEX_MAP = {
    "COMPOSITE": "^JKSE",
}


def _to_jk(code: str) -> str:
    code = code.strip().upper()
    return code if code.endswith(".JK") else f"{code}.JK"


def fetch_yahoo_quotes(codes: list[str]) -> list[StockQuote]:
    """Live-ish StockQuote per emiten via Yahoo Finance (delayed ~15m).

    Uses one batched 1m intraday download for the last price and one batched
    daily download for previous close / OHLC / volume. No IDX contact.
    """
    codes = [c.strip().upper() for c in codes if c.strip()]
    if not codes:
        return []
    tickers = [_to_jk(c) for c in codes]
    now = datetime.now(WIB)

    intr = None
    try:
        intr = yf.download(
            tickers=" ".join(tickers), period="1d", interval="1m",
            group_by="ticker", progress=False, threads=True, auto_adjust=False,
        )
    except Exception as e:
        print(f"[warn] yahoo intraday gagal: {e}", file=sys.stderr)
    try:
        day = yf.download(
            tickers=" ".join(tickers), period="5d", interval="1d",
            group_by="ticker", progress=False, threads=True, auto_adjust=False,
        )
    except Exception as e:
        print(f"[warn] yahoo daily gagal: {e}", file=sys.stderr)
        return []

    single = len(tickers) == 1

    def _frame(src, tk):
        if src is None:
            return None
        try:
            return src if single else src[tk]
        except Exception:
            return None

    results: list[StockQuote] = []
    for code, tk in zip(codes, tickers):
        d = _frame(day, tk)
        if d is None:
            continue
        d = d.dropna(how="all")
        if len(d) == 0:
            continue
        today = d.iloc[-1]
        prev = float(d.iloc[-2]["Close"]) if len(d) >= 2 else None

        # last price: prefer intraday 1m close, else today's daily close
        last = None
        i = _frame(intr, tk)
        if i is not None:
            i = i.dropna(how="all")
            if len(i):
                try:
                    last = float(i.iloc[-1]["Close"])
                except (KeyError, ValueError, TypeError):
                    last = None
        if last is None:
            try:
                last = float(today["Close"])
            except (KeyError, ValueError, TypeError):
                last = None
        if last is None:
            continue

        def _f(key, today=today):  # bind this row: closure is called per-loop (B023)
            try:
                v = float(today[key])
                return None if math.isnan(v) else v  # NaN guard
            except (KeyError, ValueError, TypeError):
                return None

        def _i(key):
            v = _f(key)
            return int(v) if v is not None else None

        change = (last - prev) if prev is not None else None
        results.append(StockQuote(
            source="Yahoo",
            code=code,
            board=None,
            previous=prev,
            open=_f("Open"),
            high=_f("High"),
            low=_f("Low"),
            close=last,
            change=change,
            volume=_i("Volume"),
            value=None,
            frequency=None,
            captured_at=now,
            metadata={"src": "yahoo", "ticker": tk},
        ))
    return results


def fetch_yahoo_indices(index_codes: list[str] | None = None) -> list[IndexQuote]:
    """Live-ish IndexQuote via Yahoo. Defaults to COMPOSITE (^JKSE / IHSG)."""
    codes = index_codes or list(YAHOO_INDEX_MAP.keys())
    now = datetime.now(WIB)
    results: list[IndexQuote] = []
    for code in codes:
        yt = YAHOO_INDEX_MAP.get(code.upper())
        if not yt:
            continue
        try:
            df = yf.Ticker(yt).history(period="5d")
        except Exception as e:
            print(f"[warn] yahoo index {code} gagal: {e}", file=sys.stderr)
            continue
        df = df.dropna(how="all")
        if len(df) == 0:
            continue
        last = float(df.iloc[-1]["Close"])
        prev = float(df.iloc[-2]["Close"]) if len(df) >= 2 else None
        change = (last - prev) if prev is not None else None
        percent = (change / prev * 100) if (prev and change is not None) else None
        results.append(IndexQuote(
            source="Yahoo",
            code=code.upper(),
            close=last,
            change=change,
            percent=percent,
            current=last,
            captured_at=now,
            metadata={"src": "yahoo", "ticker": yt},
        ))
    return results


def idx_reachable(timeout: float = 12.0) -> bool:
    """Cheap, throttled probe: is IDX reachable again (Cloudflare cleared)?

    Uses a single plain curl_cffi request to a lightweight IDX JSON endpoint.
    Returns True only on HTTP 200 with a JSON body (list/dict). Never launches
    a browser — this is the auto-switch signal for going Yahoo -> IDX.
    """
    url = f"{IDX_BASE}/primary/home/GetIndexList"
    try:
        r = cf_requests.get(
            url,
            impersonate="chrome124",
            timeout=timeout,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": f"{IDX_BASE}/",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
    except Exception:
        return False
    if r.status_code != 200:
        return False
    try:
        body = r.json()
    except Exception:
        return False
    return isinstance(body, (list, dict)) and bool(body)
