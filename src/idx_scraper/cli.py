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
from pathlib import Path

import dotenv

dotenv.load_dotenv()

# scripts/ lives at the repo root, outside the installable src/ package. Put the
# repo root on sys.path so the scheduler's `from scripts.X import ...` resolves
# regardless of the caller's working directory or PYTHONPATH.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pandas as pd
from apscheduler.schedulers.blocking import BlockingScheduler

from .analysis import calculate_indicators, generate_signal
from .client import (
    IDXClient,
    fetch_stock_historical,
    fetch_yahoo_indices,
    fetch_yahoo_quotes,
)
from .live_capture import LiveCapture
from .notify import (
    SignalState,
    TelegramNotifier,
    format_rule_message,
    format_smart_money_message,
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


def _job_ownership() -> None:
    """Snapshot pemegang saham watchlist -> research.ownership (mingguan).

    IDX hanya memberi komposisi terkini, jadi "aksi pemilik" (siapa menambah /
    mengurangi) baru bisa dihitung setelah ada dua snapshot di tanggal berbeda.
    Job ini menjaga pembanding itu tetap ada tanpa perlu diingat manual.
    """
    codes = _parse_watchlist(os.getenv("IDX_WATCHLIST"))
    if not codes:
        return
    try:
        client = IDXClient()
        try:
            ok, total = snapshot_ownership(codes, None, client)
        finally:
            client.close()
        print(
            f"[{datetime.now(WIB).isoformat()}] ownership snapshot — "
            f"{ok}/{len(codes)} emiten, {total} baris"
        )
    except Exception as e:
        print(f"[err] ownership snapshot: {e}", file=sys.stderr)


def _next_weekday_at(dow: int, hour: int, minute: int) -> datetime:
    """Datetime WIB berikutnya yang jatuh pada ``dow`` (0=Senin) pukul hh:mm.

    Dipakai agar job ber-interval panjang (mingguan/bulanan) tetap berjalan
    sekali saat boot, tanpa menyimpan state terakhir jalan.
    """
    now = datetime.now(WIB)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    days_ahead = (dow - now.weekday()) % 7
    if days_ahead == 0 and target <= now:
        days_ahead = 7
    return target + timedelta(days=days_ahead)


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


def _job_signal_log() -> None:
    """Rekam sinyal BUY/SELL hari bursa terakhir ke research.signal_log.

    16:25 WIB — setelah close resmi IHSG + regime harian, supaya kolom regime
    ikut terisi. Idempoten per (code, trade_date); aman dijalankan ulang.
    """
    dsn = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not dsn:
        return
    try:
        from .research.signal_log import load_log, record_recent

        # Window 1 bulan: cukup menutup koreksi data telat, jauh lebih murah
        # daripada menghitung ulang 18 bulan tiap hari.
        n = record_recent(dsn, months=1)
        log = load_log(dsn)
        last = str(log["date"].max().date()) if not log.empty else "-"
        print(
            f"[{datetime.now(WIB).isoformat()}] jejak sinyal tersimpan: "
            f"{n} baris (sinyal terakhir {last})"
        )
    except Exception as e:
        print(f"[err] jejak sinyal job: {e}", file=sys.stderr)


def cmd_signal_log(args) -> None:
    """Backfill + tampilkan track record sinyal (jejak sinyal)."""
    dsn = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not dsn:
        print("[err] DATABASE_URL/SUPABASE_DB_URL belum diisi", file=sys.stderr)
        return
    from .research.signal_log import backfill, evaluate, print_report

    written = backfill(dsn, months=args.months)
    print(f"[ok] {written} baris sinyal ditulis ke research.signal_log")
    print_report(evaluate(dsn, horizons=tuple(args.k)))


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
    """Tulis close resmi IHSG dari IDX live tiap hari bursa jam 16:10 WIB.

    Sumber = IDX GetIndexList (Current = close resmi pasca closing auction),
    bukan Yahoo — biar angka IHSG di dashboard sama persis dengan idx.co.id.
    Snapshot polling terakhir (mis. 15:59) ditimpa lewat baris idempotent
    captured_at 16:00 WIB + isi index_summary_daily (seri harian benchmark).
    """
    if not _is_market_hours() and datetime.now(WIB).weekday() >= 5:
        return  # weekend guard (cron sudah bursa-only, ini double safety)
    dsn = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not dsn:
        return
    try:
        from scripts.backfill_index_eod_idx import backfill_index_eod_idx

        upserted, snapshots = backfill_index_eod_idx(dsn)
        print(
            f"[{datetime.now(WIB).isoformat()}] index EOD close (IDX live) — "
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

def _clear_api_cache() -> None:
    """Flush the API in-process analytic cache so menus show fresh data at once.

    The API is a separate process; cache lives there. Best-effort — kalau API
    sedang mati, refresh berikutnya tetap jalan (TTL 1 jam sebagai jaring).
    """
    base = os.getenv("IDX_API_BASE", "http://127.0.0.1:8000").rstrip("/")
    try:
        import urllib.request

        req = urllib.request.Request(f"{base}/api/cache/clear", method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
        print(f"[{datetime.now(WIB).isoformat()}] cache analitik dikosongkan")
    except Exception as e:
        print(f"[warn] gagal clear cache API: {e}", file=sys.stderr)

def _job_pipeline_refresh(client: IDXClient, storage, *, label: str, do_eod: bool) -> None:
    """Satu siklus refresh pipeline untuk 8 menu (Narasi/Screener/Valuasi/Hold
    Check/Jejak Sinyal/Foreign Flow/Sentimen/Sektor).

    ``do_eod`` True hanya di close sesi hari bursa (ada data EOD pasar hari ini);
    pra-buka & akhir pekan cukup recompute + koreksi IHSG dari Yahoo + clear
    cache. Semua sub-job idempoten & dibungkus try/except supaya satu gagal
    tidak menghentikan yang lain. Sengaja TIDAK dijaga _is_market_hours().
    """
    print(f"[{datetime.now(WIB).isoformat()}] pipeline refresh — {label}")
    if do_eod:
        try:
            _job_eod_full(client, storage)
        except Exception as e:
            print(f"[err] refresh EOD: {e}", file=sys.stderr)
    for name, fn in (
        ("index EOD close", _job_index_eod_close),
        ("regime harian", _job_regime_daily),
        ("jejak sinyal", _job_signal_log),
        ("broker summary", _job_broker_eod),
    ):
        try:
            fn()
        except Exception as e:
            print(f"[err] refresh {name}: {e}", file=sys.stderr)
    _clear_api_cache()
    # Alert jejak smart money: verdict jendela 10 sesi baru berubah sekarang
    # (ada data EOD baru), jadi ini titik yang tepat untuk cek transisi +
    # kirim Telegram. Dibungkus try/except — gagal tak menghentikan refresh.
    if do_eod:
        try:
            n_sm = run_smart_money_alerts(_parse_watchlist(os.getenv("IDX_WATCHLIST")))
            if n_sm:
                print(f"[alerts] jejak smart money: {n_sm} transisi terkirim")
        except Exception as e:
            print(f"[err] refresh alert smart money: {e}", file=sys.stderr)


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
               order by code, trade_date, prev_close is null, ingested_at desc
               on conflict (code, trade_date, knowledge_date) do nothing"""
        )
        # Backfill any remaining NULL prev_close from the previous day's close.
        conn.execute(
            """update research.prices_pit p
               set prev_close = prev.close
               from research.prices_pit prev
               where p.trade_date > prev.trade_date
                 and p.code = prev.code
                 and p.prev_close is null
                 and prev.close is not null
                 and prev.trade_date = p.trade_date - 1"""
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


def snapshot_ownership(
    codes: list[str], snap: str | None = None, client: IDXClient | None = None
) -> tuple[int, int]:
    """Snapshot komposisi pemegang saham ``codes`` -> ``research.ownership``.

    Inti bersama CLI (``idx ownership``) dan scheduler (``_job_ownership``):
    satu snapshot per ``snapshot_date`` (default hari ini WIB). Returns
    ``(emiten berdata, total baris)``. Idempoten — upsert per (snapshot_date,
    code, holder_name, category), jadi menjalankan ulang di hari yang sama aman
    dan emiten tanpa data tidak menimpa snapshot lama.
    """
    import psycopg

    from .ownership_store import snapshot_from_profile

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("[err] DATABASE_URL belum diisi", file=sys.stderr)
        return 0, 0
    if not codes:
        return 0, 0

    own_client = client is None
    client = client or IDXClient()
    ok = total = 0
    try:
        with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
            for i, code in enumerate(codes, 1):
                try:
                    prof = client.fetch_company_profile(code)
                except Exception as e:
                    print(f"[warn] profil {code}: {e}", file=sys.stderr)
                    prof = None
                n = snapshot_from_profile(cur, code, prof, snap)
                if n:
                    ok += 1
                total += n
                if i % 25 == 0:
                    print(f"  {i}/{len(codes)} — {total} baris")
    finally:
        if own_client:
            client.close()
    return ok, total


def cmd_ownership(args) -> None:
    """Snapshot komposisi pemegang saham -> research.ownership.

    Sumber gratis per-emiten (``GetCompanyProfilesDetail``). Jalankan berkala
    (mingguan/bulanan) supaya aksi pemilik — siapa menambah/mengurangi — bisa
    dihitung dari selisih dua snapshot. Scheduler juga menjalankannya otomatis
    tiap Senin 06:45 WIB untuk emiten watchlist (lihat ``_job_ownership``).
    """
    client = IDXClient()
    snap = getattr(args, "date", None)
    try:
        if getattr(args, "all", False):
            codes = [
                str(s.get("Code")).upper()
                for s in client.fetch_securities_stock()
                if s.get("Code")
            ]
        elif getattr(args, "codes", None):
            codes = [c.strip().upper() for c in args.codes.split(",") if c.strip()]
        else:
            codes = _parse_watchlist(os.getenv("IDX_WATCHLIST"))
        if not codes:
            print("tidak ada kode untuk di-snapshot")
            return
        print(f"snapshot kepemilikan {len(codes)} emiten...")
        ok, total = snapshot_ownership(codes, snap, client)
        print(f"ownership done — {ok}/{len(codes)} emiten, {total} baris")
    finally:
        client.close()


def cmd_alerts(args) -> None:
    """Scan sinyal dan kirim alert Telegram untuk sinyal yang berubah.

    Juga mengevaluasi aturan pantauan (harga/RSI/volume) yang disimpan lewat
    halaman Pantau di dashboard, dan transisi verdict jejak smart money
    (pemain besar masuk/keluar) untuk emiten watchlist.
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
        smart = run_smart_money_alerts(tickers)
        if signals or rules or smart:
            print(
                f"alerts terkirim — sinyal: {signals}, aturan: {rules}, "
                f"smart money: {smart}"
            )
        else:
            print("alerts: tidak ada sinyal/aturan/smart money yang berubah")
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


def run_smart_money_alerts(codes: list[str]) -> int:
    """Kirim alert Telegram untuk emiten yang verdict jejak smart money berubah.

    Di panggil setelah data EOD baru masuk (verdict jendela 10 sesi hanya
    berubah di titik itu). Alur: ambil verdict per emiten watchlist lewat
    service read-only ``analytics.get_smart_money_verdicts``, lalu
    :class:`SmartMoneyState` menyaring yang transisinya bermakna (baru masuk /
    baru keluar / balik arah) dan mengirimkannya sekali — anti-spam sama
    persis dengan aturan pantauan. DB down / Telegram tak diatur tidak boleh
    menghentikan scheduler, jadi seluruh badan dibungkus try/except.
    """
    if not codes:
        return 0
    notifier = TelegramNotifier()
    if not notifier.enabled:
        return 0

    from .api import analytics as analytics_api
    from .smart_money_state import SmartMoneyState

    try:
        verdicts = analytics_api.get_smart_money_verdicts(codes)
    except Exception as e:  # DB down / config missing must not kill the scheduler
        print(f"[err] alert smart money dilewati: {e}", file=sys.stderr)
        return 0
    if not verdicts:
        return 0

    events = SmartMoneyState().evaluate(verdicts)
    if not events:
        return 0
    return len(events) if notifier.send_message(format_smart_money_message(events)) else 0


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
    # Auto-switch check runs on the index tick (throttled internally). Probe
    # through the same browser transport that fetches data — the plain curl
    # probe can't clear a Cloudflare challenge, so it would never flip to IDX.
    probe_interval = int(os.getenv("IDX_PROBE_INTERVAL", "900"))  # 15 min default
    source = maybe_probe_and_switch(client.reachable, probe_interval)
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
    # ------------------------------------------------------------------
    # Refresh pipeline 8 menu (Narasi/Screener/Valuasi/Hold Check/Jejak
    # Sinyal/Foreign Flow/Sentimen/Sektor) pada jadwal WIB yang diminta.
    # Semua job ini SENGAJA bypass _is_market_hours() dan clear cache API.
    #   - Sen-Jum 08:45  pra-pembukaan (waktu input) -> recompute + clear cache
    #   - Sen-Kam 12:00  close Sesi I  -> EOD + recompute
    #   - Jum     11:30  close Sesi I  -> EOD + recompute
    #   - Sen-Jum 16:00  close Sesi II -> EOD + recompute (offset kecil utk Yahoo)
    #   - Sab-Min 08:45 & 16:00        -> recompute + clear cache (tanpa EOD)
    # ------------------------------------------------------------------
    def _refresh(label: str, do_eod: bool):
        return {
            "func": _job_pipeline_refresh,
            "trigger": "cron",
            "args": [client, storage],
            "kwargs": {"label": label, "do_eod": do_eod},
        }

    # Pra-pembukaan Sen-Jum 08:45 — recompute + clear cache (belum ada EOD baru).
    scheduler.add_job(
        **_refresh("pra-buka Sen-Jum 08:45", do_eod=False),
        day_of_week="mon-fri", hour=8, minute=45, id="refresh_preopen",
    )
    # Close Sesi I: Sen-Kam 12:00, Jum 11:30 — tarik EOD + recompute.
    scheduler.add_job(
        **_refresh("close Sesi I Sen-Kam 12:00", do_eod=True),
        day_of_week="mon-thu", hour=12, minute=0, id="refresh_s1",
    )
    scheduler.add_job(
        **_refresh("close Sesi I Jum 11:30", do_eod=True),
        day_of_week="fri", hour=11, minute=30, id="refresh_s1_fri",
    )
    # Close Sesi II Sen-Jum 16:00 — offset 5 menit supaya Yahoo daily bar final.
    scheduler.add_job(
        **_refresh("close Sesi II Sen-Jum 16:00", do_eod=True),
        day_of_week="mon-fri", hour=16, minute=5, id="refresh_s2",
    )
    # Susulan +15 menit — jaring bar harian yang telat final dari Yahoo.
    scheduler.add_job(
        **_refresh("close Sesi II susulan Sen-Jum 16:20", do_eod=True),
        day_of_week="mon-fri", hour=16, minute=20, id="refresh_s2_late",
    )
    # Akhir pekan Sab-Min 08:45 & 16:00 — recompute + clear cache (pasar tutup).
    scheduler.add_job(
        **_refresh("akhir pekan 08:45", do_eod=False),
        day_of_week="sat,sun", hour=8, minute=45, id="refresh_weekend_am",
    )
    scheduler.add_job(
        **_refresh("akhir pekan 16:00", do_eod=False),
        day_of_week="sat,sun", hour=16, minute=0, id="refresh_weekend_pm",
    )
    # IC analysis HARIAN — 06:30 WIB (pra-buka, sebelum refresh 08:45).
    # Sebelumnya bulanan (tanggal 1); karena panel flow baru bertambah tiap
    # hari dan gate IC tinggal menunggu sampel memadai, evaluasi harian
    # memastikan faktor aliran langsung terpakai begitu lolos gate — bobot
    # composite hold-check otomatis mengikuti hasil terbaru.
    scheduler.add_job(
        _job_monthly_ic, "cron", day_of_week="mon-fri", hour=6, minute=30,
        id="daily_ic",
    )
    # Snapshot pemegang saham — Senin 06:45 WIB (di luar jam bursa, supaya tidak
    # berebut profil Cloudflare dengan EOD fetch). Interval 1 minggu dengan
    # start_date Senin berikutnya; dengan begitu tetap ada satu run saat boot
    # tanpa menunggu cron harian.
    scheduler.add_job(
        _job_ownership, "interval", weeks=1,
        start_date=_next_weekday_at(0, 6, 45),
        id="ownership_weekly", replace_existing=True,
    )
    # Live per-emiten capture — GetStockSummary whole-market is EOD-only, so the
    # live feed is polled one code at a time. Runs as a rolling background sweep
    # that self-gates on market hours and the IDX source.
    live_top = int(os.getenv("IDX_LIVE_TOP", "60"))
    live_delay_ms = int(os.getenv("IDX_LIVE_DELAY_MS", "1000"))
    live_capture = LiveCapture(
        client,
        top_n=live_top,
        delay_sec=live_delay_ms / 1000,
        should_run=lambda: _is_market_hours() and current_source() == SOURCE_IDX,
    )
    if live_top > 0:
        live_capture.start()
        print(
            f"live capture — {live_top} liquid + 20 movers, {live_delay_ms}ms/req, rolling sweep"
        )
    else:
        print("live capture OFF (IDX_LIVE_TOP=0)")

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
    print("refresh pipeline 8 menu — Sen-Jum 08:45, Sen-Kam 12:00 / Jum 11:30, "
          "Sen-Jum 16:05 & 16:20; akhir pekan 08:45 & 16:00 (recompute+clear cache)")
    print("ownership snapshot — Senin 06:45 WIB (watchlist)")

    # --- Startup catchup: if today's EOD data is missing, run it now -------
    # Prevents missed refreshes when scheduler restarts after the cron window.
    # Checks BOTH prices_pit (stock EOD) AND index_quotes (COMPOSITE official close).
    try:
        import psycopg as _pg

        _dsn = os.getenv("DATABASE_URL")
        if _dsn:
            _today = datetime.now(WIB).date()
            with _pg.connect(_dsn) as _conn, _conn.cursor() as _cur:
                _cur.execute(
                    "select 1 from research.latest_pit "
                    "where trade_date = %s limit 1",
                    (_today,),
                )
                _has_today_stocks = _cur.fetchone() is not None
                # Also check if COMPOSITE has an official 16:00 EOD close for today
                _cur.execute(
                    "select 1 from index_quotes "
                    "where code = 'COMPOSITE' "
                    "and (captured_at at time zone 'Asia/Jakarta')::date = %s "
                    "and extract(hour from captured_at at time zone 'Asia/Jakarta') >= 16 "
                    "limit 1",
                    (_today,),
                )
                _has_today_index = _cur.fetchone() is not None
            now_wib = datetime.now(WIB)
            wd = now_wib.weekday()  # 0=Mon..6=Sun
            past_close = now_wib.hour >= 16
            is_trading_day = wd < 5

            if is_trading_day and past_close:
                if not _has_today_stocks:
                    print(f"[catchup] data EOD saham {now_wib.strftime('%Y-%m-%d')} belum ada — "
                          "menjalankan full refresh pipeline...")
                    _job_pipeline_refresh(
                        client, storage,
                        label=f"startup catchup {now_wib.strftime('%Y-%m-%d')}",
                        do_eod=True,
                    )
                elif not _has_today_index:
                    print(f"[catchup] close resmi COMPOSITE {now_wib.strftime('%Y-%m-%d')} belum ada — "
                          "menjalankan index EOD close...")
                    _job_index_eod_close()
                    _clear_api_cache()
                else:
                    print("[catchup] data EOD hari ini sudah lengkap (saham OK, index OK)")
            else:
                print(f"[catchup] check EOD selesai "
                      f"(pasar {'sudah tutup' if past_close else 'belum tutup'}, "
                      f"{'hari kerja' if is_trading_day else 'akhir pekan'})")
    except Exception as e:
        print(f"[warn] catchup check gagal: {e}", file=sys.stderr)

    try:
        scheduler.start()
    except KeyboardInterrupt:
        pass
    finally:
        scheduler.shutdown()
        live_capture.stop()
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
    p_slog = sub.add_parser("signal-log", help="backfill + tampilkan track record sinyal BUY/SELL")
    p_slog.add_argument("--months", type=int, default=18, help="panjang histori (default: 18 bulan)")
    p_slog.add_argument("--k", type=int, nargs="+", default=[5, 10, 21], help="horizon hari bursa (default: 5 10 21)")
    sub.add_parser("watchlist", help="fetch watchlist tickers (default: from .env)")
    p_own = sub.add_parser("ownership", help="snapshot komposisi pemegang saham (aksi pemilik)")
    p_own.add_argument("--codes", help="daftar kode dipisah koma (default: watchlist .env)")
    p_own.add_argument("--all", action="store_true", help="semua emiten tercatat (berat, ±950 request)")
    p_own.add_argument("--date", help="tanggal snapshot ISO YYYY-MM-DD (default: hari ini)")
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
    elif args.command == "signal-log":
        cmd_signal_log(args)
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
    elif args.command == "ownership":
        cmd_ownership(args)
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
