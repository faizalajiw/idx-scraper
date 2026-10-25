"""Helper fetch & seed data (IDX + Yahoo, sinyal ringkas)."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import dotenv

dotenv.load_dotenv()

import pandas as pd

from ..analysis import calculate_indicators, generate_signal
from ..client import (
    IDXClient,
    fetch_stock_historical,
    fetch_yahoo_indices,
    fetch_yahoo_quotes,
)
from ..logging_setup import say

WIB = timezone(timedelta(hours=7))


def _parse_watchlist(raw: str | None) -> list[str]:
    if not raw:
        return []
    codes = [c.strip().upper() for c in raw.split(",") if c.strip()]
    # de-duplicate while preserving order (env watchlist often has repeats)
    return list(dict.fromkeys(codes))


def fetch_and_store_indices(client: IDXClient, storage) -> int:
    quotes = client.fetch_index_list()
    count = 0
    for q in quotes:
        try:
            storage.insert_index_quote(q)
            count += 1
        except Exception as e:
            say(f"[err] insert index {q.code}: {e}", err=True)
    return count


def fetch_and_store_watchlist(client: IDXClient, storage, codes: list[str]) -> int:
    count = 0
    for _i, code in enumerate(codes):
        q = client.fetch_trading_daily(code)
        if q is None:
            # Kemungkinan kena throttle IDX: beri jeda lebih lama lalu 1x retry.
            time.sleep(3.0)
            q = client.fetch_trading_daily(code)
        if q is None:
            say(f"[warn] no data for {code} (throttle / kode tidak aktif?)", err=True)
        else:
            try:
                storage.insert_stock_quote(q)
                count += 1
            except Exception as e:
                say(f"[err] insert stock {code}: {e}", err=True)
        time.sleep(1.2)  # sopan ke server IDX, hindari throttling
    return count


def fetch_and_store_eod(client: IDXClient, storage, date_str: str) -> int:
    rows = client.fetch_stock_summary(date_str)
    count = 0
    for r in rows:
        try:
            storage.insert_eod_stock(r)
            count += 1
        except Exception as e:
            say(f"[err] insert eod {r.code}: {e}", err=True)
    return count


def fetch_and_store_indices_yahoo(storage) -> int:
    """Store live-ish index quotes from Yahoo (COMPOSITE/IHSG)."""
    quotes = fetch_yahoo_indices()
    count = 0
    for q in quotes:
        try:
            storage.insert_index_quote(q)
            count += 1
        except Exception as e:
            say(f"[err] insert yahoo index {q.code}: {e}", err=True)
    return count


def fetch_and_store_watchlist_yahoo(storage, codes: list[str]) -> int:
    """Store live-ish watchlist quotes from Yahoo (batched, no IDX contact)."""
    quotes = fetch_yahoo_quotes(codes)
    count = 0
    for q in quotes:
        try:
            storage.insert_stock_quote(q)
            count += 1
        except Exception as e:
            say(f"[err] insert yahoo stock {q.code}: {e}", err=True)
    return count


def seed_historical_data(storage, tickers: list[str], days: int = 180) -> int:
    """Fetch historical OHLCV data from yfinance and store in stock_daily table."""
    count = 0
    end = datetime.now(WIB)
    start = end - timedelta(days=days)
    for ticker in tickers:
        try:
            rows = fetch_stock_historical(
                ticker,
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d"),
            )
            for row in rows:
                try:
                    storage.insert_stock_daily(row)
                    count += 1
                except Exception as e:
                    say(f"[err] insert {ticker} {row.date}: {e}", err=True)
        except Exception as e:
            say(f"[err] fetch historical for {ticker}: {e}", err=True)
    say(f"seeded {count} historical rows for {len(tickers)} tickers")
    return count


def show_signals(storage, codes: list[str]) -> None:
    """Generate buy/sell signals from stored daily data (IDX EOD + Yahoo fallback)."""
    say("\n=== Trading Signals ===")
    count = 0
    for code in codes[:20]:
        try:
            rows = storage.load_price_history(code, limit=60)
            if len(rows) < 25:
                continue
            df = pd.DataFrame(rows)
            df = calculate_indicators(df)
            signal = generate_signal(df)
            if signal != "HOLD":
                last = df.iloc[-1]
                rsi = last.get("RSI")
                rsi_str = f"{float(rsi):.1f}" if pd.notna(rsi) else "-"
                say(f"[{signal}] {code}: {last['close']:.2f} (RSI: {rsi_str})")
                count += 1
        except Exception as e:
            say(f"[warn] signal {code}: {e}", err=True)
            continue
    if count == 0:
        say("No signals (all HOLD)")
    say()


def liquid_codes(client: IDXClient, top_n: int) -> list[str]:
    """Return the top-N most liquid tickers ranked by daily trading value."""
    raw = client._get_json("/primary/TradingSummary/GetStockSummary?length=9999&start=0")
    if not raw or not isinstance(raw.get("data"), list):
        return []

    def _value(d: dict) -> float:
        try:
            return float(d.get("Value") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    ranked = sorted(raw["data"], key=_value, reverse=True)
    codes = [d["StockCode"] for d in ranked if d.get("StockCode")]
    return codes[:top_n]
