"""Scheduler + CLI for IDX scraper.

Usage:
    idx snapshot          # one-off index + watchlist fetch → storage
    idx eod 20260919      # fetch EOD summary for a date → storage
    idx serve             # start polling loop (market hours only)
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import dotenv

dotenv.load_dotenv()

import pandas as pd
from apscheduler.schedulers.blocking import BlockingScheduler

from .analysis import calculate_indicators, generate_signal
from .client import IDXClient, fetch_stock_historical
from .notify import SignalState, TelegramNotifier, run_alert_check
from .storage import get_storage_from_env

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
            print(f"[err] insert index {q.code}: {e}", file=sys.stderr)
    return count


def fetch_and_store_watchlist(client: IDXClient, storage, codes: list[str]) -> int:
    count = 0
    for i, code in enumerate(codes):
        q = client.fetch_trading_daily(code)
        if q is None:
            # Kemungkinan kena throttle IDX: beri jeda lebih lama lalu 1x retry.
            time.sleep(3.0)
            q = client.fetch_trading_daily(code)
        if q is None:
            print(f"[warn] no data for {code} (throttle / kode tidak aktif?)", file=sys.stderr)
        else:
            try:
                storage.insert_stock_quote(q)
                count += 1
            except Exception as e:
                print(f"[err] insert stock {code}: {e}", file=sys.stderr)
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
            print(f"[err] insert eod {r.code}: {e}", file=sys.stderr)
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
                    print(f"[err] insert {ticker} {row.date}: {e}", file=sys.stderr)
        except Exception as e:
            print(f"[err] fetch historical for {ticker}: {e}", file=sys.stderr)
    print(f"seeded {count} historical rows for {len(tickers)} tickers")
    return count


def show_signals(storage, codes: list[str]) -> None:
    """Generate buy/sell signals from stored daily data (IDX EOD + Yahoo fallback)."""
    print("\n=== Trading Signals ===")
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
                print(f"[{signal}] {code}: {last['close']:.2f} (RSI: {rsi_str})")
                count += 1
        except Exception as e:
            print(f"[warn] signal {code}: {e}", file=sys.stderr)
            continue
    if count == 0:
        print("No signals (all HOLD)")
    print()

def _job_eod_full(client: IDXClient, storage) -> None:
    """Full market EOD snapshot at market close."""
    from datetime import datetime
    date_str = datetime.now(WIB).strftime("%Y%m%d")
    rows = client.fetch_stock_summary(date_str)
    count = 0
    for r in rows:
        try:
            storage.insert_eod_stock(r)
            count += 1
        except Exception:
            pass
    print(f"[{datetime.now(WIB).isoformat()}] EOD full market: {count} stocks")


def cmd_snapshot(args) -> None:
    client = IDXClient()
    storage = get_storage_from_env()
    try:
        n_idx = fetch_and_store_indices(client, storage)
        watchlist = _parse_watchlist(os.getenv("IDX_WATCHLIST"))
        n_stock = fetch_and_store_watchlist(client, storage, watchlist)
        print(f"snapshot done — index={n_idx}, stocks={n_stock}")
    finally:
        client.close()
        storage.close()


def cmd_eod(args) -> None:
    client = IDXClient()
    storage = get_storage_from_env()
    try:
        if args.date:
            rows = client.fetch_stock_summary(args.date)
        else:
            from datetime import datetime
            date_str = datetime.now(WIB).strftime("%Y%m%d")
            rows = client.fetch_stock_summary(date_str)
        count = 0
        for r in rows:
            try:
                storage.insert_eod_stock(r)
                count += 1
            except Exception:
                pass
        print(f"EOD fetch done — {count} stocks stored")
    finally:
        client.close()
        storage.close()


def cmd_seed(args) -> None:
    """Seed historical data for watchlist tickers."""
    storage = get_storage_from_env()
    tickers = _parse_watchlist(os.getenv("IDX_WATCHLIST"))
    days = args.days if hasattr(args, 'days') and args.days else 180
    try:
        seed_historical_data(storage, tickers, days)
    finally:
        storage.close()


def cmd_signals(args) -> None:
    storage = get_storage_from_env()
    tickers = _parse_watchlist(os.getenv("IDX_WATCHLIST"))
    try:
        show_signals(storage, tickers)
    finally:
        storage.close()


def cmd_alerts(args) -> None:
    """Scan sinyal dan kirim alert Telegram untuk sinyal yang berubah."""
    notifier = TelegramNotifier()
    if args.test:
        notifier.test_connection()
        return
    storage = get_storage_from_env()
    tickers = _parse_watchlist(os.getenv("IDX_WATCHLIST"))
    try:
        sent = run_alert_check(storage, tickers, notifier, SignalState())
        print(f"alerts: {sent} terkirim" if sent else "alerts: tidak ada sinyal baru")
    finally:
        storage.close()


def _job_alerts(storage, codes: list[str]) -> None:
    if not _is_market_hours():
        return
    notifier = TelegramNotifier()
    if not notifier.enabled:
        return
    sent = run_alert_check(storage, codes, notifier, SignalState())
    if sent:
        print(f"[{datetime.now(WIB).isoformat()}] telegram alerts: {sent}")


def _is_market_hours() -> bool:
    now = datetime.now(WIB)
    open_h, open_m = map(int, os.getenv("IDX_MARKET_OPEN", "09:00").split(":"))
    close_h, close_m = map(int, os.getenv("IDX_MARKET_CLOSE", "16:00").split(":"))
    t = now.hour * 60 + now.minute
    return (open_h * 60 + open_m) <= t < (close_h * 60 + close_m) and now.weekday() < 5


def _job_indices(client: IDXClient, storage) -> None:
    if not _is_market_hours():
        return
    n = fetch_and_store_indices(client, storage)
    print(f"[{datetime.now(WIB).isoformat()}] index refreshed: {n}")


def _job_watchlist(client: IDXClient, storage, codes: list[str]) -> None:
    if not _is_market_hours():
        return
    n = fetch_and_store_watchlist(client, storage, codes)
    print(f"[{datetime.now(WIB).isoformat()}] watchlist refreshed: {n}")


def cmd_serve(args) -> None:
    client = IDXClient()
    storage = get_storage_from_env()
    watchlist = _parse_watchlist(os.getenv("IDX_WATCHLIST"))
    idx_interval = int(os.getenv("IDX_INDEX_INTERVAL", "15"))
    wl_interval = int(os.getenv("IDX_WATCHLIST_INTERVAL", "30"))

    # Show initial signals
    show_signals(storage, watchlist)

    scheduler = BlockingScheduler(timezone=WIB)
    scheduler.add_job(
        _job_indices, "interval", seconds=idx_interval, args=[client, storage], id="idx"
    )
    if watchlist:
        scheduler.add_job(
            _job_watchlist,
            "interval",
            seconds=wl_interval,
            args=[client, storage, watchlist],
            id="wl",
        )
    # EOD full market snapshot at market close (16:00 WIB)
    scheduler.add_job(
        _job_eod_full,
        "cron",
        hour=16,
        minute=10,
        args=[client, storage],
        id="eod",
    )
    # Telegram alert loop (hanya kalau env Telegram diisi)
    alert_interval = int(os.getenv("IDX_ALERT_INTERVAL", "300"))
    if TelegramNotifier().enabled:
        scheduler.add_job(
            _job_alerts,
            "interval",
            seconds=alert_interval,
            args=[storage, watchlist],
            id="alerts",
        )
        print(f"telegram alerts every {alert_interval}s")
    else:
        print("telegram alerts OFF (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID belum diisi)")

    print(f"serving — index every {idx_interval}s, watchlist every {wl_interval}s")
    print(f"watchlist: {watchlist[:10]}...")
    print("EOD full market at 16:10 WIB")
    try:
        scheduler.start()
    except KeyboardInterrupt:
        pass
    finally:
        scheduler.shutdown()
        client.close()
        storage.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="idx")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("snapshot", help="one-off fetch index + watchlist")
    p_eod = sub.add_parser("eod", help="fetch entire market EOD data (optional: specify YYYYMMDD)")
    p_eod.add_argument("date", nargs="?", help="YYYYMMDD (optional, defaults to today)")
    p_seed = sub.add_parser("seed", help="seed historical data (yfinance) for watchlist tickers")
    p_seed.add_argument("--days", type=int, default=180, help="number of days of historical data (default: 180)")
    sub.add_parser("signals", help="show buy/sell signals from stored EOD data")
    p_alerts = sub.add_parser("alerts", help="kirim alert Telegram utk sinyal BUY/SELL yang berubah")
    p_alerts.add_argument("--test", action="store_true", help="test koneksi bot (getMe) lalu keluar")
    sub.add_parser("watchlist", help="fetch watchlist tickers (default: from .env)")
    sub.add_parser("serve", help="start continuous polling loop")

    args = parser.parse_args()
    if args.command == "snapshot":
        cmd_snapshot(args)
    elif args.command == "eod":
        cmd_eod(args)
    elif args.command == "seed":
        cmd_seed(args)
    elif args.command == "signals":
        cmd_signals(args)
    elif args.command == "alerts":
        cmd_alerts(args)
    elif args.command == "watchlist":
        client = IDXClient()
        storage = get_storage_from_env()
        try:
            watchlist = _parse_watchlist(os.getenv("IDX_WATCHLIST"))
            n = fetch_and_store_watchlist(client, storage, watchlist)
            print(f"watchlist done — {n} stocks stored")
        finally:
            client.close()
            storage.close()
    elif args.command == "serve":
        cmd_serve(args)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
