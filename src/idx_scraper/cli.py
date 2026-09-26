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
from .client import (
    IDXClient,
    fetch_stock_historical,
    fetch_yahoo_indices,
    fetch_yahoo_quotes,
    idx_reachable,
)
from .notify import (
    SignalState,
    TelegramNotifier,
    format_rule_message,
    run_alert_check,
)
from .source_state import (
    SOURCE_IDX,
    current_source,
    maybe_probe_and_switch,
)
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


def fetch_and_store_indices_yahoo(storage) -> int:
    """Store live-ish index quotes from Yahoo (COMPOSITE/IHSG)."""
    quotes = fetch_yahoo_indices()
    count = 0
    for q in quotes:
        try:
            storage.insert_index_quote(q)
            count += 1
        except Exception as e:
            print(f"[err] insert yahoo index {q.code}: {e}", file=sys.stderr)
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
            print(f"[err] insert yahoo stock {q.code}: {e}", file=sys.stderr)
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

def liquid_codes(client: IDXClient, top_n: int) -> list[str]:
    """Return the top-N most liquid tickers ranked by daily trading value."""
    raw = client._get_json(
        "/primary/TradingSummary/GetStockSummary?length=9999&start=0"
    )
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


def _job_regime_daily() -> None:
    """Rekam klasifikasi regime hari bursa terakhir ke research.regime_daily."""
    dsn = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not dsn:
        return
    try:
        from .research.regime import record_today

        row = record_today(dsn)
        if row:
            print(
                f"[{datetime.now(WIB).isoformat()}] regime {row['date']}: "
                f"{row['regime']} (ADX {row['adx']}, vol {row['vol_state']})"
            )
    except Exception as e:
        print(f"[err] regime harian: {e}", file=sys.stderr)


def _job_monthly_ic() -> None:
    """IC analysis bulanan -> research.factor_ic_history (bobot composite)."""
    dsn = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not dsn:
        return
    try:
        from .research.monthly_ic import run_and_store

        out = run_and_store(dsn)
        print(
            f"[{datetime.now(WIB).isoformat()}] IC bulanan tersimpan "
            f"run_date={out['run_date']} rows={out['rows']} bobot={out['weights']}"
        )
    except Exception as e:
        print(f"[err] IC bulanan: {e}", file=sys.stderr)


def _job_broker_eod() -> None:
    """Broker summary EOD (bandarmologi) -> research.broker_daily, 16:30 WIB.

    20 menit setelah close resmi supaya endpoint IDX sudah memuat data final
    hari itu. Idempoten per tanggal — aman dijalankan ulang.
    """
    dsn = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not dsn:
        return
    try:
        from scripts.backfill_broker_eod import backfill_broker_eod

        n, d = backfill_broker_eod(dsn)
        print(f"[{datetime.now(WIB).isoformat()}] broker summary {d}: {n} baris")
    except Exception as e:
        print(f"[err] broker summary job: {e}", file=sys.stderr)


def cmd_ic(args) -> None:
    """Run IC analysis manual + simpan histori bobot faktor."""
    dsn = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not dsn:
        print("[err] DATABASE_URL/SUPABASE_DB_URL belum diisi", file=sys.stderr)
        return
    from .research.monthly_ic import run_and_store

    out = run_and_store(dsn, horizons=args.k)
    print(f"run_date={out['run_date']} rows={out['rows']}")
    print(f"bobot composite terbaru: {out['weights']}")


def _job_index_eod_close() -> None:
    """Tulis close resmi IHSG (Yahoo ^JKSE daily) tiap hari bursa jam 16:10 WIB.

    Yahoo daily close baru final beberapa menit setelah closing auction 16:00;
    snapshot polling terakhir (mis. 15:59) biasanya selisih beberapa poin dari
    close resmi. Job ini menimpanya lewat snapshot EOD idempotent + mengisi
    index_summary_daily (seri harian untuk RRG/benchmark).
    """
    if not _is_market_hours() and datetime.now(WIB).weekday() >= 5:
        return  # weekend guard (cron sudah bursa-only, ini double safety)
    dsn = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not dsn:
        return
    try:
        from scripts.backfill_index_eod import backfill_index_eod

        upserted, snapshots = backfill_index_eod(dsn, days=10)
        print(
            f"[{datetime.now(WIB).isoformat()}] index EOD close — "
            f"upsert={upserted}, snapshot baru={snapshots}"
        )
    except Exception as e:
        print(f"[err] index EOD close job: {e}", file=sys.stderr)


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
    _ingest_eod_research(rows)


def _ingest_eod_research(rows) -> None:
    """Append the same EOD rows into research.raw_eod (append-only) + refresh
    prices_pit. Keeps the research superset current with name + foreign flow.
    IDX omits PreviousPrice on EOD, so prev_close is recovered from close-change.
    """
    import json

    import psycopg

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        return
    inserted = quarantined = 0
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        for r in rows:
            trade_date = datetime.strptime(r.date, "%Y%m%d").replace(tzinfo=WIB).date()
            prev_close = r.previous
            if prev_close is None and r.close is not None and r.change is not None:
                prev_close = r.close - r.change

            reason = cur.execute(
                "select research.eod_reject_reason("
                "%s::numeric,%s::numeric,%s::numeric,%s::numeric,%s::bigint)",
                (r.open, r.high, r.low, r.close, r.volume),
            ).fetchone()[0]
            if reason is not None:
                cur.execute(
                    """insert into research.quarantine_eod (code, trade_date, payload, reason)
                       values (%s, %s, %s, %s)""",
                    (r.code, trade_date, json.dumps(r.model_dump(mode="json")), reason),
                )
                quarantined += 1
                continue

            def _ohl(v):  # null zeroed OHL from non-traded days
                return v if v else None
            cur.execute(
                """insert into research.raw_eod
                   (code, trade_date, open, high, low, close, prev_close,
                    volume, value, frequency, source,
                    name, foreign_buy, foreign_sell, foreign_net)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (r.code, trade_date, _ohl(r.open), _ohl(r.high), _ohl(r.low),
                 r.close, prev_close, r.volume, r.value, r.frequency, r.source,
                 r.name, r.foreign_buy, r.foreign_sell, r.foreign_net),
            )
            inserted += 1

        conn.execute(
            """insert into research.prices_pit
                   (code, trade_date, knowledge_date, open, high, low, close, volume, value,
                    name, prev_close, foreign_buy, foreign_sell, foreign_net)
               select distinct on (code, trade_date)
                   code, trade_date, ingested_at, open, high, low, close, volume, value,
                   name, prev_close, foreign_buy, foreign_sell, foreign_net
               from research.raw_eod
               order by code, trade_date, ingested_at desc
               on conflict (code, trade_date, knowledge_date) do nothing"""
        )
    print(f"[research] raw_eod +{inserted}, quarantine +{quarantined}, prices_pit refreshed")


def cmd_snapshot(args) -> None:
    client = IDXClient()
    storage = get_storage_from_env()
    try:
        n_idx = fetch_and_store_indices(client, storage)
        top_n = getattr(args, "top", None)
        if top_n:
            codes = liquid_codes(client, top_n)
            print(f"scanning {len(codes)} most liquid tickers...")
        else:
            codes = _parse_watchlist(os.getenv("IDX_WATCHLIST"))
        n_stock = fetch_and_store_watchlist(client, storage, codes)
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
        _ingest_eod_research(rows)
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
    """Scan sinyal dan kirim alert Telegram untuk sinyal yang berubah.

    Juga mengevaluasi aturan pantauan (harga/RSI/volume) yang disimpan lewat
    halaman Pantau di dashboard.
    """
    notifier = TelegramNotifier()
    if args.test:
        notifier.test_connection()
        return
    storage = get_storage_from_env()
    tickers = _parse_watchlist(os.getenv("IDX_WATCHLIST"))
    try:
        signals = run_alert_check(storage, tickers, notifier, SignalState())
        rules = run_rule_alerts()
        if signals or rules:
            print(f"alerts terkirim — sinyal: {signals}, aturan: {rules}")
        else:
            print("alerts: tidak ada sinyal atau aturan yang berubah")
    finally:
        storage.close()


def run_rule_alerts() -> int:
    """Evaluate saved watch rules and push newly-triggered ones to Telegram.

    Rules are evaluated through the API service layer on purpose: the dashboard
    and the notifier must agree on what "triggered" means, and that layer already
    reads the Postgres research data the UI shows.
    """
    notifier = TelegramNotifier()
    if not notifier.enabled:
        return 0

    from .alert_rules import RuleState
    from .api import alerts as alerts_api

    try:
        evaluations = alerts_api.evaluate_all()
    except Exception as e:  # DB down / config missing must not kill the scheduler
        print(f"[err] aturan pantauan dilewati: {e}", file=sys.stderr)
        return 0
    if not evaluations:
        return 0

    fresh = RuleState().newly_triggered(evaluations)
    if not fresh:
        return 0
    return len(fresh) if notifier.send_message(format_rule_message(fresh)) else 0


def _job_alerts(storage, codes: list[str]) -> None:
    if not _is_market_hours():
        return
    notifier = TelegramNotifier()
    if not notifier.enabled:
        return
    signals = run_alert_check(storage, codes, notifier, SignalState())
    rules = run_rule_alerts()
    if signals or rules:
        print(
            f"[{datetime.now(WIB).isoformat()}] telegram alerts — "
            f"sinyal: {signals}, aturan: {rules}"
        )


def _is_market_hours() -> bool:
    now = datetime.now(WIB)
    open_h, open_m = map(int, os.getenv("IDX_MARKET_OPEN", "09:00").split(":"))
    close_h, close_m = map(int, os.getenv("IDX_MARKET_CLOSE", "16:00").split(":"))
    t = now.hour * 60 + now.minute
    return (open_h * 60 + open_m) <= t < (close_h * 60 + close_m) and now.weekday() < 5


def _job_indices(client: IDXClient, storage) -> None:
    if not _is_market_hours():
        return
    # Auto-switch check runs on the index tick (throttled internally).
    probe_interval = int(os.getenv("IDX_PROBE_INTERVAL", "900"))  # 15 min default
    source = maybe_probe_and_switch(idx_reachable, probe_interval)
    if source == SOURCE_IDX:
        n = fetch_and_store_indices(client, storage)
        tag = "IDX"
    else:
        n = fetch_and_store_indices_yahoo(storage)
        tag = "YAHOO"
    print(f"[{datetime.now(WIB).isoformat()}] index refreshed ({tag}): {n}")


def _job_watchlist(client: IDXClient, storage, codes: list[str]) -> None:
    if not _is_market_hours():
        return
    if current_source() == SOURCE_IDX:
        n = fetch_and_store_watchlist(client, storage, codes)
        tag = "IDX"
    else:
        n = fetch_and_store_watchlist_yahoo(storage, codes)
        tag = "YAHOO"
    print(f"[{datetime.now(WIB).isoformat()}] watchlist refreshed ({tag}): {n}")


def _job_intraday(client: IDXClient, top_n: int) -> None:
    """Snapshot top-N liquid emiten into research.intraday_ticks (market hours only).

    Skipped entirely while on the Yahoo source — this is an IDX-only feature and
    we must not touch IDX during cooldown.
    """
    if not _is_market_hours():
        return
    if current_source() != SOURCE_IDX:
        return
    import psycopg

    from scripts.intraday_capture import capture_once
    try:
        with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
            n = capture_once(conn, client, top_n)
        print(f"[{datetime.now(WIB).isoformat()}] intraday captured: {n}")
    except Exception as e:
        print(f"[err] intraday capture: {e}", file=sys.stderr)


def cmd_serve(args) -> None:
    client = IDXClient()
    storage = get_storage_from_env()
    watchlist = _parse_watchlist(os.getenv("IDX_WATCHLIST"))
    # Yahoo data is ~15m delayed, so gentle default intervals; overridable via env.
    idx_interval = int(os.getenv("IDX_INDEX_INTERVAL", "60"))
    wl_interval = int(os.getenv("IDX_WATCHLIST_INTERVAL", "60"))

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
    # EOD full market snapshot, 5 menit setelah tiap penutupan sesi (WIB, hari bursa)
    # Sesi I close: Sen-Kam 12:00 -> fetch 12:05 ; Jum 11:30 -> fetch 11:35
    scheduler.add_job(
        _job_eod_full, "cron", day_of_week="mon-thu", hour=12, minute=5,
        args=[client, storage], id="eod_s1",
    )
    scheduler.add_job(
        _job_eod_full, "cron", day_of_week="fri", hour=11, minute=35,
        args=[client, storage], id="eod_s1_fri",
    )
    # Sesi II close: 15:49:59 semua hari -> fetch 15:55
    scheduler.add_job(
        _job_eod_full, "cron", day_of_week="mon-fri", hour=15, minute=55,
        args=[client, storage], id="eod_s2",
    )
    # Close resmi IHSG dari Yahoo daily — jam 16:10 WIB (closing auction 16:00,
    # Yahoo butuh beberapa menit supaya bar daily-nya final).
    scheduler.add_job(
        _job_index_eod_close, "cron", day_of_week="mon-fri", hour=16, minute=10,
        id="index_eod_close",
    )
    # IC analysis bulanan — hari pertama kerja tiap bulan, 06:30 WIB (pra-buka).
    # Bobot composite hold-check otomatis mengikuti hasil terbaru.
    scheduler.add_job(
        _job_monthly_ic, "cron", day="1", hour=6, minute=30,
        id="monthly_ic",
    )
    # Histori regime — tiap hari bursa 16:20 WIB (setelah close resmi IHSG 16:10).
    scheduler.add_job(
        _job_regime_daily, "cron", day_of_week="mon-fri", hour=16, minute=20,
        id="regime_daily",
    )
    # Broker summary EOD (bandarmologi) — 16:30 WIB, idempoten per tanggal.
    scheduler.add_job(
        _job_broker_eod, "cron", day_of_week="mon-fri", hour=16, minute=30,
        id="broker_eod",
    )
    # Intraday tick capture — top-N liquid emiten, satu request seluruh pasar.
    # IDX-only feature; the job self-skips while on the Yahoo source (cooldown).
    intraday_top = int(os.getenv("IDX_INTRADAY_TOP", "400"))
    intraday_interval = int(os.getenv("IDX_INTRADAY_INTERVAL", "60"))
    if intraday_top > 0:
        scheduler.add_job(
            _job_intraday, "interval", seconds=intraday_interval,
            args=[client, intraday_top], id="intraday",
        )
        print(f"intraday capture — top {intraday_top} liquid every {intraday_interval}s")

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
        print(f"telegram alerts (sinyal + aturan pantauan) every {alert_interval}s")
    else:
        print("telegram alerts OFF (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID belum diisi)")

    print(f"serving — index every {idx_interval}s, watchlist every {wl_interval}s")
    print(f"watchlist: {watchlist[:10]}...")
    print(f"live source: {current_source()} (auto-switch ke IDX saat probe 200; "
          f"probe tiap {int(os.getenv('IDX_PROBE_INTERVAL', '900'))}s)")
    print("EOD full market — Sesi I: Sen-Kam 12:05 / Jum 11:35 WIB, Sesi II: 15:55 WIB")
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

    p_snap = sub.add_parser("snapshot", help="one-off fetch index + watchlist")
    p_snap.add_argument("--top", type=int, metavar="N",
                        help="scan N most liquid tickers instead of .env watchlist (e.g. --top 200)")
    p_eod = sub.add_parser("eod", help="fetch entire market EOD data (optional: specify YYYYMMDD)")
    p_eod.add_argument("date", nargs="?", help="YYYYMMDD (optional, defaults to today)")
    p_seed = sub.add_parser("seed", help="seed historical data (yfinance) for watchlist tickers")
    p_seed.add_argument("--days", type=int, default=180, help="number of days of historical data (default: 180)")
    sub.add_parser("signals", help="show buy/sell signals from stored EOD data")
    p_alerts = sub.add_parser("alerts", help="kirim alert Telegram utk sinyal BUY/SELL yang berubah")
    p_alerts.add_argument("--test", action="store_true", help="test koneksi bot (getMe) lalu keluar")
    p_ic = sub.add_parser("ic", help="run IC analysis + simpan bobot faktor ke research.factor_ic_history")
    p_ic.add_argument("--k", type=int, nargs="+", default=[5, 10], help="horizon hari bursa (default: 5 10)")
    sub.add_parser("watchlist", help="fetch watchlist tickers (default: from .env)")
    sub.add_parser("serve", help="start continuous polling loop")
    p_source = sub.add_parser("source", help="show / set live data source (YAHOO|IDX)")
    p_source.add_argument("set", nargs="?", choices=["YAHOO", "IDX", "yahoo", "idx"],
                          help="force source (optional; omit to just show state)")

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
    elif args.command == "ic":
        cmd_ic(args)
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
    elif args.command == "source":
        from .source_state import force_source, get_state
        if getattr(args, "set", None):
            force_source(args.set)
            print(f"source di-set ke: {args.set.upper()}")
        st = get_state()
        print(f"source={st.source} probe_count={st.probe_count} idx_ok_count={st.idx_ok_count}")
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
