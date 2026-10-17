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
import threading
import time
from datetime import date, datetime, timedelta, timezone
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
    format_recommendation_message,
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


def _job_broker_eod() -> int:
    """Broker summary EOD (bandarmologi) -> research.broker_daily.

    ~20 menit setelah close resmi supaya endpoint IDX sudah memuat data final
    hari itu. Idempoten per tanggal — aman dijalankan ulang.
    """
    dsn = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not dsn:
        return 0
    try:
        from scripts.backfill_broker_eod import backfill_broker_eod

        n, d = backfill_broker_eod(dsn)
        print(f"[{datetime.now(WIB).isoformat()}] broker summary {d}: {n} baris")
        return n
    except Exception as e:
        print(f"[err] broker summary job: {e}", file=sys.stderr)
        return 0


def _lapor_bisu(penjaga: str, sudah: bool) -> bool:
    """Laporkan penjaga yang tidak memeriksa apa pun; Telegram cukup sekali per run.

    Empat jalur bisa sama-sama bisu dari satu akar masalah (``DATABASE_URL``
    hilang, DB kosong). Pesan pertama yang menyebut sebabnya sudah cukup —
    sisanya masuk log saja, supaya alarm tidak jadi empat pesan kembar.
    """
    from .health import format_no_data_message

    stamp = datetime.now(WIB).isoformat()
    if sudah:
        print(f"[{stamp}] health {penjaga} — 0 sesi terbaca (sudah dilaporkan)")
    else:
        TelegramNotifier().send_message(format_no_data_message(penjaga))
        print(f"[{stamp}] health {penjaga} — 0 sesi terbaca (dilaporkan)")
    return True


def _job_health_eod() -> None:
    """Cek data EOD hari bursa sudah masuk; kirim Telegram kalau telat.

    Komplemen ``startup catchup``: catchup hanya jalan saat boot, sedangkan job
    ini menjaga kasus scheduler hidup tapi job EOD-nya gagal diam-diam (DB
    putus, Cloudflare). DB/Telegram error tidak boleh menghentikan scheduler.

    Dua jalur alarm di bawah sengaja punya ``try/except`` SENDIRI: satu
    pemeriksaan yang melempar (mis. ``check_field_coverage`` vs DB yang putus)
    tidak boleh menelan alarm jalur lain — penjaga cakupan kolom pernah
    mematikan alarm "Data EOD belum masuk" yang sudah ada lebih dulu.

    Empat jalur semuanya begini sekarang: kesegaran, cakupan kolom, jumlah baris
    per sesi, dan lubang sesi. Jalur baris melewati sesi hari ini
    (``in_progress``) karena pada 16:45 ingest bisa masih jalan dan sesi separuh
    yang sedang ditulis bukan kerusakan.

    "Tidak ada yang diperiksa" dihitung masalah, bukan diam: kalau
    ``research.raw_eod`` kosong atau ``DATABASE_URL`` tidak diset, cakupan kolom
    / baris / lubang semuanya lolos tanpa suara — mode bisu yang alarm ini ada
    untuk menutupnya (kartu Dashboard menandai keadaan itu `Waspada`). Telegram
    untuk keadaan itu dikirim sekali (``_lapor_bisu``), bukan sekali per jalur.
    """
    bisu = False
    try:
        from .health import check_eod_health, format_health_message

        h = check_eod_health()
        if not h.ok:
            TelegramNotifier().send_message(format_health_message(h))
            print(
                f"[{datetime.now(WIB).isoformat()}] health EOD TELAT — "
                f"{', '.join(h.missing)}"
            )
        elif h.missing:
            # Mis. `DATABASE_URL` tidak diset: bukan "telat", tapi bukan OK juga.
            bisu = _lapor_bisu("Kesegaran EOD", bisu)
            print(f"[err] health EOD: {', '.join(h.missing)}", file=sys.stderr)
        else:
            print(f"[{datetime.now(WIB).isoformat()}] health EOD OK — {h.expected}")
    except Exception as e:
        _cetak_aman(f"[err] health EOD: {e}", file=sys.stderr)

    # Cek kedua: tanggal bisa ada tapi kolom foreign_* bolong (9 bulan
    # tanpa alarm pada 2025-09-24 → 2026-07-29). Telegram dikirim sebelum
    # log dicetak supaya alarm tidak hilang gara-gara encoding konsol.
    try:
        from .health import check_field_coverage, format_field_coverage_message

        c = check_field_coverage()
        if c.checked == 0:
            bisu = _lapor_bisu("Cakupan kolom foreign_*", bisu)
        elif not c.ok:
            TelegramNotifier().send_message(format_field_coverage_message(c))
            print(
                f"[{datetime.now(WIB).isoformat()}] health cakupan kolom timpang — "
                f"{len(c.troublemakers)}/{c.checked} sesi"
            )
    except Exception as e:
        _cetak_aman(f"[err] health cakupan kolom: {e}", file=sys.stderr)

    # Cek ketiga: tanggal ada, isinya cuma separuh sesi (insert tersela).
    try:
        from .health import check_row_counts, format_row_count_message

        r = check_row_counts(in_progress=datetime.now(WIB).date())
        if r.checked == 0:
            bisu = _lapor_bisu("Jumlah baris per sesi", bisu)
        elif not r.ok:
            TelegramNotifier().send_message(format_row_count_message(r))
            print(
                f"[{datetime.now(WIB).isoformat()}] health sesi separuh — "
                f"{len(r.troublemakers)}/{r.checked} sesi di bawah median "
                f"{r.median_rows} baris"
            )
    except Exception as e:
        _cetak_aman(f"[err] health sesi separuh: {e}", file=sys.stderr)

    # Cek keempat: hari bursa tanpa baris sama sekali di tengah rentang data.
    try:
        from .health import check_session_gaps, format_session_gaps_message

        g = check_session_gaps(in_progress=datetime.now(WIB).date())
        if g.sessions == 0:
            bisu = _lapor_bisu("Lubang sesi", bisu)
        elif not g.ok:
            TelegramNotifier().send_message(format_session_gaps_message(g))
            print(
                f"[{datetime.now(WIB).isoformat()}] health lubang sesi — "
                f"{len(g.missing)} hari bursa tanpa baris"
            )
    except Exception as e:
        _cetak_aman(f"[err] health lubang sesi: {e}", file=sys.stderr)


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


def _job_recommendations() -> None:
    """Rekam kandidat beli hari bursa terakhir -> research.recommendation_log.

    Dijalankan setelah jejak sinyal: memakai input lapisan yang SAMA dengan API
    (bobot pola track record + penyesuaian faktor/broker bila tervalidasi),
    supaya baris yang diukur track record-nya persis yang tampil di dashboard.
    Idempoten per (code, trade_date); aman dijalankan ulang.
    """
    dsn = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not dsn:
        return
    try:
        from .api import analytics as analytics_api
        from .research.recommendation_log import record

        inputs = analytics_api.recommendation_record_inputs()
        n = record(dsn, months=1, **inputs)
        print(f"[{datetime.now(WIB).isoformat()}] rekomendasi tersimpan: {n} kandidat")
    except Exception as e:
        print(f"[err] rekomendasi job: {e}", file=sys.stderr)


def cmd_recommendations(args) -> None:
    """Backfill + tampilkan track record kandidat beli (halaman Rekomendasi)."""
    dsn = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not dsn:
        print("[err] DATABASE_URL/SUPABASE_DB_URL belum diisi", file=sys.stderr)
        return
    from .research.recommendation_log import evaluate, print_report, record

    # Input lapisan (bobot pola track record + faktor/broker bila tervalidasi)
    # diambil dari service supaya baris yang diukur = yang tampil di dashboard.
    # Best-effort: kalau API/DB belum siap, backfill tetap jalan tanpa lapisan itu.
    inputs: dict = {}
    try:
        from .api import analytics as analytics_api

        inputs = analytics_api.recommendation_record_inputs()
    except Exception as e:
        print(f"[warn] input lapisan rekomendasi dilewati: {e}", file=sys.stderr)

    written = record(dsn, months=args.months, **inputs)
    print(f"[ok] {written} baris kandidat ditulis")
    print_report(evaluate(dsn, horizons=tuple(args.k)))


def cmd_signal_log(args) -> None:
    """Backfill + tampilkan track record sinyal (jejak sinyal).
    """
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


def _boleh_tulis_close_index(now: datetime | None = None) -> bool:
    """True kalau baris "close resmi hari ini" sudah boleh ditulis (>= 16:00 WIB).

    Close resmi hanya ada setelah closing auction. Menulisnya lebih awal bukan
    tanpa akibat: ``backfill_index_eod_idx`` menstempel barisnya
    ``captured_at = hari ini 16:00`` — satu-satunya jam yang dihitung sebagai
    close oleh ``_eod_fresh_for`` dan ``check_eod_health``, dan baris berstempel
    paling baru di ``index_quotes``. Kalau job ini ikut jalan di pra-buka (08:50)
    atau close Sesi I (12:00) seperti dulu, nilai jam itu tersimpan berlabel
    16:00, sehingga:

    1. dua pembaca dashboard (``analytics._latest_index``, ``services.get_market_overview``)
       mengambilnya lewat ``order by captured_at desc limit 1`` — nilai pra-buka /
       tengah hari tampil sebagai IHSG hari itu, dan mengalahkan snapshot polling
       selama sisa sesi (polling berstempel jam berjalan, selalu lebih tua dari 16:00);
    2. ``_eod_fresh_for``/``check_eod_health`` mengklaim close hari ini sudah ada,
       jadi ``--skip-if-fresh`` dan alarm menilai hari itu "beres" tanpa dasar.

    Sebelum 16:00 biarkan snapshot polling yang mengisi dashboard: nilai terakhir
    memang bukan close, tapi setidaknya bukan angka yang salah label.
    """
    return (now or datetime.now(WIB)).hour >= 16


def _job_index_eod_close() -> None:
    """Tulis close resmi IHSG dari IDX live tiap hari bursa jam 16:10 WIB.

    Sumber = IDX GetIndexList (Current = close resmi pasca closing auction),
    bukan Yahoo — biar angka IHSG di dashboard sama persis dengan idx.co.id.
    Snapshot polling terakhir (mis. 15:59) ditimpa lewat baris idempotent
    captured_at 16:00 WIB + isi index_summary_daily (seri harian benchmark).
    """
    if not _boleh_tulis_close_index():
        return  # belum lewat close — lihat alasannya di `_boleh_tulis_close_index`
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


# IDX kadang menolak endpoint BERAT (GetStockSummary, GetBrokerSummary) hanya
# sesaat: 15 Okt 2026 pukul 16:10 dan 16:15 keduanya 403, sementara permintaan
# identik pukul 17:40 berhasil (963 baris dalam 0,3 s). Pipeline lama sekali
# jalan lalu menyerah, jadi EOD sehari hilang tanpa percobaan kedua. Jeda di
# bawah memberi kesempatan ulang cepat; kalau jendela penolakan panjang (seperti
# 15 Okt), yang menutupnya adalah job `eod_fallback` di dalam `serve` — dicek
# ulang tiap jam 16:30–20:30 sampai EOD hari itu benar-benar terisi, bukan lagi
# task Windows `IDX-EOD` yang sudah dihapus.
_EOD_RETRY_DELAYS: tuple[int, ...] = (60, 180)


def _retry_percobaan(now: datetime | None = None) -> int:
    """Jumlah percobaan untuk langkah EOD berat, menurut jam WIB.

    Sebelum jam close (16:00) EOD pasar memang belum terbit — ``refresh_s1``
    12:00 membuktikannya tiap hari dengan 0 baris. Di jendela itu percobaan
    ulang cuma membuang ~4 menit dan menambah beban IDX tanpa bisa berhasil,
    jadi dimatikan (1 percobaan). Sesudah close, retry penuh.
    """
    now = now or datetime.now(WIB)
    return len(_EOD_RETRY_DELAYS) + 1 if now.hour >= 16 else 1


def _retry_step(label: str, step, *, percobaan: int | None = None) -> int:
    """Jalankan ``step()`` sampai mengembalikan baris > 0.

    ``percobaan`` default 1 + ``len(_EOD_RETRY_DELAYS)`` ulangan; kirim
    ``percobaan=1`` untuk sekali jalan tanpa ulangan (lihat ``_retry_percobaan``).

    Hanya untuk langkah yang hasilnya pasti berisi pada hari bursa dan idempoten
    per tanggal (EOD saham selalu 963 baris, broker summary selalu 88 firma).
    Langkah yang "0 baris" bisa berarti wajar — mis. close indeks saat barisnya
    sudah ada — sengaja TIDAK dibungkus di sini.
    """
    if percobaan is None:
        percobaan = len(_EOD_RETRY_DELAYS) + 1
    baris = 0
    for i in range(percobaan):
        try:
            baris = step() or 0
        except Exception as e:
            baris = 0
            print(f"[err] {label}: {e}", file=sys.stderr)
        if baris > 0:
            return baris
        if i + 1 < percobaan:
            jeda = _EOD_RETRY_DELAYS[min(i, len(_EOD_RETRY_DELAYS) - 1)]
            print(
                f"[warn] {label}: 0 baris — percobaan {i + 2}/{percobaan} "
                f"dalam {jeda}s",
                file=sys.stderr,
            )
            time.sleep(jeda)
    if percobaan > 1:
        print(
            f"[err] {label}: tetap kosong setelah {percobaan} percobaan",
            file=sys.stderr,
        )
    return baris


def _simpan_eod_hari(client: IDXClient, storage, day: date) -> int:
    """Tarik + simpan EOD satu tanggal bursa (idempoten). Kembalikan jumlah baris.

    Dipakai tiga jalur yang butuh arti yang sama persis: close hari ini
    (``_job_eod_full``), permintaan manual (``idx eod [tanggal]``), dan backfill
    hari lampau (``_backfill_hari_lampau``). IDX melayani tanggal lampau di
    endpoint yang sama (``GetStockSummary?date=YYYYMMDD``), jadi jalur mundur
    tidak butuh sumber lain.

    Tiga tujuan tulis — sama seperti pipeline 16:10: tabel polos
    (``stock_summary_daily``), superset riset (``raw_eod`` + ``prices_pit``), dan
    agregat pasar reguler/non-reguler.
    """
    rows = client.fetch_stock_summary(day.strftime("%Y%m%d"))
    if not rows:
        return 0
    count = 0
    for r in rows:
        try:
            storage.insert_eod_stock(r)
            count += 1
        except Exception:
            pass
    _ingest_eod_research(rows)
    try:
        _upsert_segment_day(rows)
    except Exception as e:
        print(f"[err] agregat pasar reguler/non-reguler {day}: {e}", file=sys.stderr)
    return count


def _job_eod_full(client: IDXClient, storage) -> int:
    """Full market EOD snapshot at market close. Kembalikan jumlah baris."""
    count = _simpan_eod_hari(client, storage, datetime.now(WIB).date())
    print(f"[{datetime.now(WIB).isoformat()}] EOD full market: {count} stocks")
    return count

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

# Kunci lintas-job: dua pipeline bersamaan (mis. rantai 16:10 yang molor bertemu
# job ulang 16:30) berarti dua klien IDX berebut sumber yang sama — persis yang
# bikin EOD 0 baris. Yang kedua dilewati, bukan diantrikan: yang pertama sudah
# mengerjakan pekerjaan yang sama.
_PIPELINE_BUSY = threading.Lock()


def _job_pipeline_refresh(client: IDXClient, storage, *, label: str, do_eod: bool) -> None:
    """Satu siklus refresh — hanya satu pipeline boleh jalan pada satu waktu.

    Job yang datang saat pipeline lain masih hidup dilewati dengan pesan
    ``pipeline lain masih berjalan``; pekerjaannya sudah tercakup yang sedang
    jalan (semua langkah idempoten per tanggal).
    """
    if not _PIPELINE_BUSY.acquire(blocking=False):
        print(
            f"[warn] {label}: pipeline lain masih berjalan — dilewati",
            file=sys.stderr,
        )
        return
    try:
        _pipeline_refresh_body(client, storage, label=label, do_eod=do_eod)
    finally:
        _PIPELINE_BUSY.release()


def _pipeline_refresh_body(client: IDXClient, storage, *, label: str, do_eod: bool) -> None:
    """Inti satu siklus refresh (tanpa kunci lintas-job).

    ``do_eod`` True hanya di close sesi hari bursa (ada data EOD pasar hari ini);
    pra-buka & akhir pekan cukup recompute + koreksi IHSG dari Yahoo + clear
    cache. Semua sub-job idempoten & dibungkus try/except supaya satu gagal
    tidak menghentikan yang lain. Sengaja TIDAK dijaga _is_market_hours().

    Dua langkah yang bergantung pada endpoint berat IDX (EOD saham, broker
    summary) diberi percobaan ulang lewat ``_retry_step``; sisanya DB murni.
    Percobaan ulang dimatikan sebelum jam close — lihat ``_retry_percobaan``.
    """
    print(f"[{datetime.now(WIB).isoformat()}] pipeline refresh — {label}")
    percobaan = _retry_percobaan()
    if do_eod:
        _retry_step(
            "EOD saham",
            lambda: _job_eod_full(client, storage),
            percobaan=percobaan,
        )
    for name, fn in (
        ("index EOD close", _job_index_eod_close),
        ("regime harian", _job_regime_daily),
        ("jejak sinyal", _job_signal_log),
        ("rekomendasi", _job_recommendations),
    ):
        try:
            fn()
        except Exception as e:
            print(f"[err] refresh {name}: {e}", file=sys.stderr)
    _retry_step("broker summary", _job_broker_eod, percobaan=percobaan)
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
        try:
            n_rec = run_recommendation_alerts()
            if n_rec:
                print(f"[alerts] kandidat beli grade A: {n_rec} terkirim")
        except Exception as e:
            print(f"[err] refresh alert rekomendasi: {e}", file=sys.stderr)


def _ingest_eod_research(rows) -> None:
    """Append the same EOD rows into research.raw_eod (append-only) + refresh
    prices_pit. Keeps the research superset current with name + foreign flow.
    IDX omits PreviousPrice on EOD, so prev_close is recovered from close-change.
    """
    import json

    import psycopg

    from .market_segment import ensure_table

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        return
    inserted = quarantined = 0
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        # Kolom nr_* (pasar non-reguler) harus ada sebelum insert bawah; DDL
        # idempoten + no-op kalau sudah ada, jadi aman dijalankan tiap EOD.
        ensure_table(cur)
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
                    name, foreign_buy, foreign_sell, foreign_net,
                    nr_volume, nr_value, nr_freq)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (r.code, trade_date, _ohl(r.open), _ohl(r.high), _ohl(r.low),
                 r.close, prev_close, r.volume, r.value, r.frequency, r.source,
                 r.name, r.foreign_buy, r.foreign_sell, r.foreign_net,
                 r.non_regular_volume, r.non_regular_value, r.non_regular_frequency),
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


def _upsert_segment_day(rows) -> None:
    """Agregat reguler vs non-reguler per tanggal -> research.market_segment_daily.

    Dipanggil setelah ingest EOD supaya hero dashboard bisa menampilkan
    volume/value pasar non-reguler, bukan cuma pasar reguler. Gagal di sini
    tak boleh menjatuhkan pipeline EOD — dibungkus pemanggil.
    """
    import psycopg

    from .market_segment import aggregate_day, upsert_day

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        return
    by_date: dict[str, list] = {}
    for r in rows:
        try:
            trade_date = (
                datetime.strptime(r.date, "%Y%m%d").replace(tzinfo=WIB).date().isoformat()
            )
        except (TypeError, ValueError):
            continue
        by_date.setdefault(trade_date, []).append(r)

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        for trade_date, day_rows in by_date.items():
            upsert_day(cur, trade_date, aggregate_day(day_rows))


def cmd_trade_summary(args) -> None:
    """Tampilkan (dan opsional isi ulang) agregat pasar reguler/non-reguler."""
    import psycopg

    from .market_segment import _int, _num

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL belum di-set — tidak bisa baca/menulis agregat.")
        return

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        if getattr(args, "rebuild", False):
            from types import SimpleNamespace

            from .market_segment import aggregate_day, ensure_table, upsert_day

            ensure_table(cur)
            # Rebuild dari raw_eod yang SUDAH tersimpan: tanpa kontak IDX, dan
            # hanya tanggal yang memang punya baris. Baris lama sebelum kolom
            # nr_* ada akan ber agregat non-reguler NULL (jujur, bukan 0).
            cur.execute(
                """select trade_date, code, volume, value, frequency,
                          nr_volume, nr_value, nr_freq
                   from research.raw_eod
                   order by trade_date"""
            )
            by_date: dict[str, list] = {}
            for d, code, vol, val, freq, nrv, nrvl, nrf in cur.fetchall():
                by_date.setdefault(d.isoformat(), []).append(
                    SimpleNamespace(
                        code=code,
                        volume=vol,
                        value=val,
                        frequency=freq,
                        non_regular_volume=nrv,
                        non_regular_value=nrvl,
                        non_regular_frequency=nrf,
                    )
                )
            for trade_date, day_rows in by_date.items():
                upsert_day(cur, trade_date, aggregate_day(day_rows))
            print(f"[trade-summary] rebuild {len(by_date)} tanggal dari research.raw_eod")

        cur.execute(
            """select * from research.market_segment_daily
               where trade_date = coalesce(%s::date, (select max(trade_date) from research.market_segment_daily))""",
            (getattr(args, "date", None),),
        )
        row = cur.fetchone()

    if not row:
        print("Belum ada agregat. Jalankan `idx eod` dulu (atau `idx trade-summary --rebuild`).")
        return

    def _lot(v: int | None) -> str:
        return f"{(v or 0) / 100:,.0f}"

    print(f"Pasar {row['trade_date']}  (emiten: {row['stock_count']})")
    print(
        f"  Reguler     : volume {_lot(_int(row['regular_volume']))} lot "
        f"({_int(row['regular_volume']):,} lembar) · value Rp {_num(row['regular_value']):,.0f} · freq {_int(row['regular_freq']):,}"
    )
    print(
        f"  Non-reguler : volume {_lot(_int(row['nonreg_volume']))} lot "
        f"({_int(row['nonreg_volume']):,} lembar) · value Rp {_num(row['nonreg_value']):,.0f} · freq {_int(row['nonreg_freq']):,}"
    )
    print(
        f"  Total       : volume {_lot(_int(row['total_volume']))} lot · value Rp {_num(row['total_value']):,.0f}"
    )


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
            day = datetime.strptime(args.date, "%Y%m%d").replace(tzinfo=WIB).date()
        else:
            day = datetime.now(WIB).date()
        count = _simpan_eod_hari(client, storage, day)
        print(f"EOD fetch done — {count} stocks stored")
    finally:
        client.close()
        storage.close()


def _eod_fresh_for(day) -> tuple[bool, bool]:
    """Cek kesegaran EOD untuk ``day`` (tanggal WIB).

    Mengembalikan ``(ada EOD saham, ada close indeks resmi >= 16:00)``.
    Dipakai startup catchup ``serve`` dan jaring EOD sore ``_eod_catchup``: satu
    definisi "sudah lengkap" supaya keduanya tidak saling meniarakan pekerjaan.
    """
    import psycopg

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        return (False, False)
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "select 1 from research.latest_pit "
            "where trade_date = %s limit 1",
            (day,),
        )
        has_stocks = cur.fetchone() is not None
        cur.execute(
            "select 1 from index_quotes "
            "where code = 'COMPOSITE' "
            "and (captured_at at time zone 'Asia/Jakarta')::date = %s "
            "and extract(hour from captured_at at time zone 'Asia/Jakarta') >= 16 "
            "limit 1",
            (day,),
        )
        has_index = cur.fetchone() is not None
    return (has_stocks, has_index)


def _hari_bursa(day: date) -> bool:
    """True kalau ``day`` hari bursa: Sen–Jum dan bukan libur ``IDX_HOLIDAYS``.

    Tanpa cek libur, jaring EOD sore menjalankan pipeline penuh di hari libur
    nasional (data pasar memang tidak ada) — menit terbuang plus alarm palsu.
    """
    if day.weekday() >= 5:
        return False
    from .health import parse_holidays

    return day not in parse_holidays()


# Berapa hari bursa ke belakang yang dicek ulang `_backfill_hari_lampau`.
# 21 hari bursa ~ 4 minggu kalender: cukup melewati libur panjang (Lebaran
# 2026-03-18..24) tanpa menjelajah setahun tiap kali laptop baru dinyalakan.
# 5 hari bursa (= 1 minggu kalender) tidak cukup: `idx serve` yang mati 2–3
# minggu kehilangan sesinya tanpa satu pun alarm, karena cek berikutnya sudah
# melewati tanggal itu — dan karena retensi IDX hanya ±2 tahun, giliran
# berikutnya bisa permanen. Lubang lebih tua dari jendela ini ditarik manual:
# `python -m scripts.check_eod_gaps` (lubang tanggal) atau
# `python -m scripts.backfill_raw_eod --ulang` (tarik ulang semua kolom).
_BACKFILL_HARI_DEFAULT = 21
_BACKFILL_HARI = int(os.getenv("IDX_BACKFILL_DAYS") or _BACKFILL_HARI_DEFAULT)


def _hari_bursa_mundur(mulai: date, jumlah: int) -> list[date]:
    """``jumlah`` hari bursa terakhir yang ``<= mulai``, terbaru dulu (murni).

    Batas langkah sengaja dipasang: cukup melewati akhir pekan + libur panjang,
    tapi loop tidak bisa berputar tanpa akhir kalau ``IDX_HOLIDAYS`` diisi aneh.
    """
    out: list[date] = []
    for i in range(jumlah * 4 + 14):
        d = mulai - timedelta(days=i)
        if _hari_bursa(d):
            out.append(d)
            if len(out) == jumlah:
                break
    return out


def _backfill_hari_lampau(
    client: IDXClient, storage, *, label: str, lapor_lubang_index: bool = False
) -> int:
    """Tarik ulang EOD hari bursa lampau yang bolong. Kembalikan jumlah hari terisi.

    Kenapa perlu: ``_eod_catchup_hari_ini`` hanya melihat HARI INI. Kalau mesin
    tidur atau mati melewati seluruh jendela sore (16:10–22:30), hari itu tidak
    pernah kembali dengan sendirinya — dan ``idx health`` pun tidak menangkapnya,
    karena ia membandingkan tanggal TERBARU, bukan lubang di tengah. Fungsi ini
    menutupnya dari dua kesempatan yang sudah ada: startup ``serve`` dan jaring
    sore ``eod_fallback``.

    Cakupan jujur:

    - **Saham** ditarik di sini (``fetch_stock_summary`` melayani tanggal lampau,
      ``_simpan_eod_hari`` menulis ketiga tujuan tulisnya).
    - **Close resmi indeks** tidak: satu-satunya sumbernya endpoint IDX live
      (``GetIndexList``) yang selalu berarti "hari ini", jadi tanggal lampau
      hanya bisa ditambal manual — lihat catatan di README. ``lapor_lubang_index``
      menyalakan satu baris laporan untuk itu (dipakai jalur startup saja supaya
      log sore tidak berisik).
    - **Menu turunan** (regime, jejak sinyal, rekomendasi, broker) disegarkan
      sekali di akhir kalau ada yang ditarik; jendela langganan mereka 1 bulan,
      jadi tanggal yang baru masuk ikut terhitung.

    Biaya saat tidak ada lubang: satu query DB per tanggal (≈ nol). Tanggal yang
    IDX balas 0 baris tidak diingat-ingat — cek berikutnya boleh mencoba lagi,
    karena 0 baris bisa berarti libur tak terdaftar atau IDX sedang menolak.

    Jendelanya default 21 hari bursa (``IDX_BACKFILL_DAYS``). Lubang lebih tua
    dari itu tidak ditarik di sini — pakai skrip manual
    ``python -m scripts.check_eod_gaps`` (lihat runbook outage di README).
    """
    if _BACKFILL_HARI <= 0:
        return 0
    sekarang = datetime.now(WIB)
    kandidat = [
        d
        for d in _hari_bursa_mundur(sekarang.date(), _BACKFILL_HARI)
        if d != sekarang.date()  # hari ini sudah ditangani `_eod_catchup_hari_ini`
    ]
    if not kandidat:
        return 0

    kosong: list[date] = []
    tanpa_index: list[date] = []
    for day in kandidat:
        ada_saham, ada_index = _eod_fresh_for(day)
        if not ada_saham:
            kosong.append(day)
        if not ada_index:
            tanpa_index.append(day)

    if lapor_lubang_index and tanpa_index:
        print(
            f"[catchup] {label}: {len(kandidat)} hari bursa lampau dicek, "
            f"{len(tanpa_index)} di antaranya tanpa close indeks resmi "
            f"({', '.join(str(d) for d in tanpa_index)}) — close indeks hanya "
            "tersedia dari endpoint IDX live, jadi tanggal lampau tidak bisa "
            "ditarik ulang otomatis"
        )
    if not kosong:
        return 0

    terisi = 0
    for day in kosong:
        try:
            baris = _simpan_eod_hari(client, storage, day)
        except Exception as e:
            print(f"[err] backfill {day}: {e}", file=sys.stderr)
            continue
        if baris:
            terisi += 1
            print(f"[catchup] {label}: EOD {day} ditarik ulang — {baris} emiten")
        else:
            print(
                f"[warn] {label}: IDX membalas 0 baris untuk {day} — "
                "boleh dicoba lagi di cek berikutnya",
                file=sys.stderr,
            )
    if terisi:
        # Tanpa EOD: tidak ada sesi pasar baru, yang perlu jalan hanya recompute
        # menu turunan. Lock lintas-job tetap dipakai — kalau rantai 16:10 masih
        # berjalan, recompute rantai itu sudah mencakup pekerjaan ini.
        _job_pipeline_refresh(
            client, storage, label=f"backfill {label}", do_eod=False
        )
    return terisi


def _eod_catchup(
    client: IDXClient, storage, *, label: str, lapor_lubang_index: bool = False
) -> str:
    """Pastikan EOD lengkap; kerjakan yang kurang. Satu definisi.

    Dipakai dua pemanggil — job ``eod_fallback`` (tiap jam 16:30–20:30, di dalam
    ``serve``) dan startup catchup ``serve`` — supaya keduanya memakai definisi
    "sudah lengkap" yang sama (``_eod_fresh_for``) dan tidak saling meniarakan
    pekerjaan.

    Dua fase: hari ini dulu, lalu hari bursa lampau yang bolong
    (``_backfill_hari_lampau``) — lihat alasannya di docstring fungsi itu.

    Kembalikan hasil fase HARI INI: ``bukan-hari-bursa`` | ``pipeline`` |
    ``indeks`` | ``lengkap``.
    """
    hasil = _eod_catchup_hari_ini(client, storage, label=label)
    _backfill_hari_lampau(
        client, storage, label=label, lapor_lubang_index=lapor_lubang_index
    )
    return hasil


def _eod_catchup_hari_ini(client: IDXClient, storage, *, label: str) -> str:
    """Fase "hari ini" dari ``_eod_catchup`` (dipisah supaya fase lampau selalu ikut).

    Kembalikan ``bukan-hari-bursa`` | ``pipeline`` | ``indeks`` | ``lengkap``.
    """
    now_wib = datetime.now(WIB)
    if not _hari_bursa(now_wib.date()) or now_wib.hour < 16:
        print(f"[catchup] {label}: dilewati (pasar belum tutup atau bukan hari bursa)")
        return "bukan-hari-bursa"
    ada_saham, ada_indeks = _eod_fresh_for(now_wib.date())
    if not ada_saham:
        print(
            f"[catchup] {label}: EOD saham {now_wib:%Y-%m-%d} belum ada — "
            "menjalankan pipeline penuh..."
        )
        _job_pipeline_refresh(client, storage, label=f"catchup {label}", do_eod=True)
        return "pipeline"
    if not ada_indeks:
        print(
            f"[catchup] {label}: close resmi COMPOSITE {now_wib:%Y-%m-%d} belum ada — "
            "menjalankan index EOD close..."
        )
        _job_index_eod_close()
        _clear_api_cache()
        return "indeks"
    print(f"[catchup] {label}: EOD hari ini sudah lengkap (saham OK, index OK)")
    return "lengkap"


def _job_eod_fallback(client: IDXClient, storage) -> None:
    """Jaring kedua EOD — Sen–Jum, tiap jam 16:30–20:30 WIB, DI DALAM ``serve``.

    Menggantikan task Windows ``IDX-EOD`` (dihapus 15 Okt 2026): penjadwalan
    hanya di satu tempat, jadi tidak ada lagi dua pipeline yang bisa bertabrakan
    di sore hari. Kalau rantai 16:10 berhasil, job ini keluar dalam ~1 detik
    (satu query DB, tanpa request IDX). Kalau gagal, ia menjalankan pipeline
    penuh — menutup jendela penolakan IDX yang lebih panjang daripada retry
    60/180 detik di dalam rantai.

    Diulang tiap jam karena mesin bisa tidur melewati slot 16:10: proses ``serve``
    ikut beku (bukan mati), dan ``misfire_grace_time`` 2 jam membuat cek terakhir
    menyala begitu mesin bangun — sepanjang masih sebelum ~22:30.
    """
    _eod_catchup(client, storage, label="ulang EOD sore")


def cmd_refresh(args) -> None:
    """One-shot full refresh pipeline (EOD + recompute + clear cache).

    Inti yang sama dengan job terjadwal ``refresh_s2`` di ``serve``, tapi bisa
    dipanggil berdiri sendiri untuk mengisi slot yang terlewat secara manual.

    ``--skip-if-fresh`` membuat pemanggilan berulang tidak mengulang penuh bila
    EOD saham + close indeks hari ini sudah terisi.
    """
    if getattr(args, "skip_if_fresh", False):
        today = datetime.now(WIB).date()
        has_stocks, has_index = _eod_fresh_for(today)
        if has_stocks and has_index:
            print(
                f"[refresh] data EOD {today.isoformat()} sudah lengkap "
                "(saham + indeks) — dilewati (--skip-if-fresh)"
            )
            return
    client = IDXClient()
    storage = get_storage_from_env()
    try:
        _job_pipeline_refresh(
            client, storage, label="manual refresh (idx refresh)", do_eod=True
        )
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
    tiap Senin 16:30 WIB untuk emiten watchlist (lihat ``_job_ownership``).
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
        recs = run_recommendation_alerts()
        if signals or rules or smart or recs:
            print(
                f"alerts terkirim — sinyal: {signals}, aturan: {rules}, "
                f"smart money: {smart}, kandidat beli: {recs}"
            )
        else:
            print("alerts: tidak ada sinyal/aturan/smart money/kandidat yang berubah")
    finally:
        storage.close()


def _cetak_aman(pesan: str, file=None) -> None:
    """Print yang tidak mematikan alarm di konsol yang tak bisa emoji.

    Konsol Windows (cp1252) melempar ``UnicodeEncodeError`` saat mencetak "⚠️";
    sisa pesannya tetap terbaca setelah emoji diganti, jadi informasi tidak hilang.
    Dipakai juga untuk pesan error di jalur alarm — pesan exception yang berisi
    non-ASCII tidak boleh menjatuhkan jalur penjaga berikutnya.
    """
    try:
        print(pesan, file=file)
    except UnicodeEncodeError:
        print(pesan.encode("ascii", "replace").decode("ascii"), file=file)


def _periksa(nama: str, fungsi, masalah: list[str], **kwargs):
    """Jalankan satu pemeriksa; kegagalannya jadi temuan, bukan penghenti.

    ``idx health`` dipakai cron: satu penjaga yang melempar (DB putus,
    ``connect_timeout``) tidak boleh membatalkan laporan penjaga lain — kontrak
    yang sama dijaga di ``_job_health_eod``. Kembalikan ``None`` kalau gagal.
    """
    try:
        return fungsi(**kwargs)
    except Exception as e:
        masalah.append(f"{nama}: gagal diperiksa ({e})")
        return None


def cmd_health(args) -> None:
    """Cek data EOD hari bursa terakhir: kesegaran + 3 penjaga cakupan.

    Exit code 1 kalau ada yang tidak beres — biar bisa dipakai cron:
    ``idx health || kirim-alert``. Empat pemeriksa:

    - **kesegaran**: lewat deadline (``IDX_EOD_DEADLINE_HOUR``, default 16:00 WIB)
      dan data hari bursa belum masuk
    - **cakupan kolom** ``foreign_*`` di ``sessions`` sesi terakhir (timpang)
    - **baris per sesi**: sesi yang isinya di bawah 50% median ("sesi separuh")
    - **lubang sesi**: hari bursa tanpa baris di antara sesi pertama & terakhir

    Pemeriksa **baris per sesi** melewati sesi hari ini, sama seperti di job
    ``health_eod``: pukul 16:45 ingest bisa masih jalan, dan sesi separuh yang
    masih ditulis bukan kerusakan. (Pemeriksa lubang sesi tidak terpengaruh —
    lubang hanya dihitung di dalam rentang sesi yang sudah ada.)

    Pemeriksa yang **tidak memeriksa apa pun** (``checked == 0``, mis. DB kosong
    atau ``DATABASE_URL`` tidak diset) dihitung bermasalah, bukan hijau — sama
    seperti kartu "Kesehatan Data" di dashboard. Tiap pemeriksa punya penangkap
    sendiri, jadi satu yang melempar tidak menghapus laporan yang lain.
    """
    from .health import (
        check_eod_health,
        check_field_coverage,
        check_row_counts,
        check_session_gaps,
        format_field_coverage_message,
        format_row_count_message,
        format_session_gaps_message,
    )

    hari_ini = datetime.now(WIB).date()
    masalah: list[str] = []

    h = _periksa("kesegaran", check_eod_health, masalah)
    c = _periksa("cakupan kolom", check_field_coverage, masalah)
    r = _periksa("baris per sesi", check_row_counts, masalah, in_progress=hari_ini)
    g = _periksa("lubang sesi", check_session_gaps, masalah, in_progress=hari_ini)

    if h is not None:
        print(
            f"hari bursa diharapkan: {h.expected} | saham terbaru: {h.stocks_latest} "
            f"| close IHSG terbaru: {h.index_latest}"
        )
    if c is not None:
        print(f"cakupan kolom foreign_*: {c.checked} sesi diperiksa, terburuk {c.worst_pct:.1f}%")
        if c.checked == 0:
            masalah.append("cakupan kolom: tidak ada sesi yang diperiksa")
        elif not c.ok:
            _cetak_aman(format_field_coverage_message(c))
            masalah.append(f"cakupan kolom: {len(c.troublemakers)}/{c.checked} sesi")
    if r is not None:
        print(
            f"baris per sesi: median {r.median_rows} baris, {len(r.troublemakers)} sesi "
            f"di bawah {r.threshold_pct:.0f}% median ({r.checked} sesi diperiksa)"
        )
        if r.checked == 0:
            masalah.append("baris per sesi: tidak ada sesi yang diperiksa")
        elif not r.ok:
            _cetak_aman(format_row_count_message(r))
            masalah.append(f"sesi separuh: {len(r.troublemakers)}/{r.checked} sesi")
    if g is not None:
        print(
            f"lubang sesi: {len(g.missing)} hari bursa tanpa baris "
            f"({g.sessions} sesi, basis {g.basis})"
        )
        if g.sessions == 0:
            masalah.append("lubang sesi: tidak ada sesi yang diperiksa")
        elif not g.ok:
            _cetak_aman(format_session_gaps_message(g))
            masalah.append(f"lubang sesi: {len(g.missing)} hari")

    if h is not None and h.missing:
        # `EodHealth.missing` juga dipakai untuk masalah konfigurasi
        # ("DATABASE_URL tidak di-set"); itu bukan "telat", tapi tetap alasan
        # untuk tidak mengucapkan OK dan keluar 0.
        label = "TELAT — belum masuk: " if not h.ok else "CEK KONFIGURASI — "
        print(label + ", ".join(h.missing))

    if h is not None and h.ok and not h.missing and not masalah:
        print("OK — data EOD terbaru lengkap")
        return
    if masalah:
        print("TIMPANG — " + "; ".join(masalah))
    sys.exit(1)


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


def run_recommendation_alerts() -> int:
    """Kirim alert Telegram untuk emiten yang BARU masuk grade A di papan kandidat.

    Papan rekomendasi bersifat se-pasar (bukan watchlist), jadi tidak butuh
    daftar kode. Alurnya sama persis dengan alert lain: ambil papan lewat service
    read-only, saring transisi lewat :class:`RecommendationState`, kirim sekali.
    DB down / Telegram tak diatur tidak boleh menghentikan scheduler.
    """
    notifier = TelegramNotifier()
    if not notifier.enabled:
        return 0

    from .api import analytics as analytics_api
    from .recommendation_state import RecommendationState

    try:
        board = analytics_api.get_recommendations(limit=50, min_grade="A")
    except Exception as e:  # DB down / config missing must not kill the scheduler
        print(f"[err] alert rekomendasi dilewati: {e}", file=sys.stderr)
        return 0
    rows = board.get("rows") or []
    if not rows:
        return 0
    date = board.get("date")
    for r in rows:
        r.setdefault("date", date)

    events = RecommendationState().evaluate(rows)
    if not events:
        return 0
    return len(events) if notifier.send_message(format_recommendation_message(events)) else 0


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


def _job_news_refresh() -> None:
    """Tarik judul Stockbit Snips -> news_items. Error tidak menghentikan scheduler."""
    try:
        from .news import run_once

        n = run_once()
        print(f"[{datetime.now(WIB).isoformat()}] news: {n} item baru")
    except Exception as e:
        print(f"[err] news job: {e}", file=sys.stderr)

# --- penjaga satu instance ------------------------------------------------
_SERVE_LOCK_FD: int | None = None


def _serve_lock_path() -> Path:
    """Letak berkas kunci `serve` (bisa ditimpa lewat IDX_SERVE_LOCK)."""
    env = os.getenv("IDX_SERVE_LOCK")
    if env:
        return Path(env)
    base = os.getenv("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "idx-scraper" / "serve.lock"


def _take_single_instance_lock() -> bool:
    """Ambil kunci eksklusif `serve`; False bila instance lain sudah pegang.

    Dua `serve` = dua Chrome berebut profil yang sama -> browser saling
    menendang dan EOD masuk 0 baris. Kunci dilepas otomatis oleh OS saat
    proses mati, jadi tidak ada kunci basi yang perlu dibersihkan manual.
    """
    global _SERVE_LOCK_FD
    path = _serve_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        os.write(fd, f"{os.getpid()}\n".encode())
        os.lseek(fd, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    _SERVE_LOCK_FD = fd  # tahan buka agar kunci hidup selama proses hidup
    return True


def cmd_serve(args) -> None:
    if not _take_single_instance_lock():
        print(
            "[serve] instance lain sedang berjalan — keluar tanpa start.\n"
            "        (dua serve = dua Chrome berebut profil yang sama, EOD jadi 0 baris)\n"
            f"        kunci: {_serve_lock_path()}"
        )
        return
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
    #   - Sen-Jum 08:50  pra-pembukaan (waktu input) -> recompute + clear cache
    #   - Sen-Kam 12:00  close Sesi I  -> EOD + recompute
    #   - Jum     11:30  close Sesi I  -> EOD + recompute
    #   - Sen-Jum 16:10  close Sesi II -> EOD + recompute (offset utk Yahoo)
    #   - Sab-Min 08:45 & 16:00        -> recompute + clear cache (tanpa EOD)
    #
    # URUTAN SORE — satu arah, tidak ada dua job yang hidup bersamaan:
    #   16:10 refresh_s2    rantai EOD (terukur 4,6 menit; retry maks +4 menit)
    #   16:30 eod_fallback  jaring kedua EOD; keluar ~1 detik kalau 16:10 berhasil
    #   16:45 health_eod    alarm — SELALU setelah fallback, supaya tidak berbunyi
    #                       di hari yang sebenarnya terselamatkan (DB murni)
    #   16:50 daily_ic      evaluasi IC (DB murni)
    #   17:00 ownership     buka klien IDX sendiri (Senin) — 50 menit dari 16:10
    # Pipeline yang saling menimpa dilewati lewat kunci `_PIPELINE_BUSY`, jadi
    # rantai yang molor tidak bisa bertemu job berikutnya secara bersamaan.
    # ------------------------------------------------------------------
    def _refresh(label: str, do_eod: bool):
        return {
            "func": _job_pipeline_refresh,
            "trigger": "cron",
            "args": [client, storage],
            "kwargs": {"label": label, "do_eod": do_eod},
            # Job yang terlewat karena mesin tidur/shutdown TETAP dijalankan
            # setelah bangun, selama masih dalam jendela 2 jam. Tanpa ini
            # APScheduler memakai grace 1 detik: bangun jam 16:32 berarti
            # slot 16:10 dibuang tanpa jejak (penyebab EOD kosong 12 Okt).
            # coalesce=True mencegah tumpukan run bila beberapa slot lewat.
            "misfire_grace_time": 7200,
            "coalesce": True,
        }

    # Berita Snips 24/7 (tanpa gate jam bursa). Interval default 15 menit.
    scheduler.add_job(
        _job_news_refresh, "interval",
        seconds=int(os.getenv("NEWS_INTERVAL", "900")), id="news_refresh",
    )

    # Pra-pembukaan Sen-Jum 08:50 - recompute + clear cache (belum ada EOD baru).
    # 08:50, bukan 08:45: task Windows (IDX-Serve/API/Web) menyalakan serve 08:45.
    # Jadwal yang lebih awal dari launcher akan terlewat di hari reboot - serve
    # lahir setelah jamnya lewat, jadi APScheduler menghitung jadwal berikutnya
    # (besok) dan recompute pra-buka hilang sehari tanpa alarm apa pun.
    scheduler.add_job(
        **_refresh("pra-buka Sen-Jum 08:50", do_eod=False),
        day_of_week="mon-fri", hour=8, minute=50, id="refresh_preopen",
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
    # Close Sesi II Sen-Jum 16:10 — offset 10 menit supaya Yahoo daily bar final.
    scheduler.add_job(
        **_refresh("close Sesi II Sen-Jum 16:10", do_eod=True),
        day_of_week="mon-fri", hour=16, minute=10, id="refresh_s2",
    )
    # Jaring kedua EOD — Sen-Jum, tiap jam 16:30-20:30 WIB, DI DALAM serve.
    # 15 Okt 2026 task Windows `IDX-EOD` dihapus supaya penjadwalan sore hanya ada
    # di satu tempat (tidak ada lagi dua pipeline yang bisa bertabrakan).
    # Kalau rantai 16:10 berhasil: satu query DB lalu keluar (~1 detik, tanpa
    # request IDX). Kalau gagal, ia menjalankan pipeline penuh.
    # DIULANG tiap jam sampai 20:30 karena dua hal yang tidak ditutup retry:
    #   1. IDX menolak endpoint berat lebih lama daripada retry 60+180 detik
    #      (persis kasus 15 Okt);
    #   2. laptop tidur melewati slot 16:10 — proses `serve` ikut beku, dan
    #      `misfire_grace_time` 2 jam membuat cek ini menyala begitu mesin
    #      bangun, sepanjang masih sebelum ~22:30.
    # 20 menit setelah 16:10 = rantai terpanjang (~16:22 dengan retry) sudah
    # selesai; kalau toh belum, kunci `_PIPELINE_BUSY` mencegah tabrakan.
    scheduler.add_job(
        _job_eod_fallback, "cron", day_of_week="mon-fri", hour="16-20", minute=30,
        args=[client, storage], id="eod_fallback",
        misfire_grace_time=7200, coalesce=True,
    )
    # Health-check EOD — Sen-Jum 16:45 WIB. SENGAJA setelah eod_fallback (16:30):
    # alarm harus menilai hasil jaring kedua, bukan hasil percobaan pertama.
    # Kalau ditaruh sebelum fallback, hari yang berhasil diselamatkan tetap
    # memicu Telegram "EOD TELAT" — alarm palsu yang lama-lama diabaikan.
    # PENTING: alarm hanya berbunyi setelah `IDX_EOD_DEADLINE_HOUR` (default 16).
    # Jangan memindahkan job ini ke jam sebelum itu — cek akan lolos tanpa suara.
    # `misfire_grace_time` 2 jam + `coalesce` seperti `eod_fallback`: laptop yang
    # tidur melewati 16:45 membuat alarm hari itu terlewat, dan tanpa grace job-nya
    # dibuang begitu saja — padahal ceknya masih relevan begitu mesin bangun
    # (mis. 18:00, sebelum jendela tutup).
    scheduler.add_job(
        _job_health_eod, "cron", day_of_week="mon-fri", hour=16, minute=45,
        id="health_eod", misfire_grace_time=7200, coalesce=True,
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
    # IC analysis HARIAN — 16:50 WIB, di ujung urutan sore. Sebelumnya bulanan
    # (tanggal 1); karena panel flow baru bertambah tiap hari dan gate IC tinggal
    # menunggu sampel memadai, evaluasi harian memastikan faktor aliran langsung
    # terpakai begitu lolos gate — bobot composite hold-check otomatis mengikuti.
    # Murni DB, tapi tetap ditaruh setelah rantai EOD supaya sore hari tidak ada
    # dua job yang hidup bersamaan sama sekali.
    scheduler.add_job(
        _job_monthly_ic, "cron", day_of_week="mon-fri", hour=16, minute=50,
        id="daily_ic",
    )
    # Snapshot pemegang saham — Senin 17:00 WIB. Job ini membuka klien IDX
    # sendiri, jadi jaraknya ke refresh_s2 (16:10) dijaga lebar: kalau IDX
    # menolak jalur HTTP dan transport jatuh ke Chrome, dua klien bersamaan akan
    # berebut profil yang sama dan KEDUANYA gagal ("Opening in existing browser
    # session"). Rantai 16:10 selesai ~16:15 (terpanjang ~16:22 dengan retry),
    # jadi 17:00 menyisakan ~38 menit — bukan lagi batas tipis 15 menit.
    # Interval 1 minggu dengan start_date Senin berikutnya; dengan begitu tetap
    # ada satu run saat boot tanpa menunggu cron harian.
    scheduler.add_job(
        _job_ownership, "interval", weeks=1,
        start_date=_next_weekday_at(0, 17, 0),
        id="ownership_weekly", replace_existing=True,
    )
    # Live per-emiten capture — GetStockSummary whole-market is EOD-only, so the
    # live feed is polled one code at a time. Runs as a rolling background sweep
    # that self-gates on market hours and the IDX source.
    live_top = int(os.getenv("IDX_LIVE_TOP", "60"))
    live_delay_ms = int(os.getenv("IDX_LIVE_DELAY_MS", "2000"))
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
    print("refresh pipeline 8 menu - Sen-Jum 08:50, Sen-Kam 12:00 / Jum 11:30, "
          "Sen-Jum 16:10; akhir pekan 08:45 & 16:00 (recompute+clear cache)")
    print("jaring EOD sore — 16:30, diulang tiap jam sampai 20:30 (keluar cepat "
          "kalau EOD hari ini sudah lengkap); "
          "health EOD 16:45; IC 16:50; ownership Senin 17:00")
    print(f"catchup EOD — hari ini (startup + jaring sore) dan {_BACKFILL_HARI} hari "
          "bursa terakhir yang bolong (IDX_BACKFILL_DAYS); close indeks lampau "
          "tidak bisa ditarik otomatis");

    # --- Startup catchup: if today's EOD data is missing, run it now -------
    # Prevents missed refreshes when scheduler restarts after the cron window.
    # Logika yang sama dipakai job `eod_fallback` (16:30) lewat `_eod_catchup`,
    # jadi definisi "sudah lengkap" di sore hari hanya ada satu. Jalur startup
    # sekaligus melaporkan lubang close indeks hari lampau (tidak bisa ditarik
    # otomatis) — di jalur sore laporan itu sengaja dimatikan supaya log bersih.
    try:
        if os.getenv("DATABASE_URL"):
            _eod_catchup(
                client,
                storage,
                label=f"startup {datetime.now(WIB):%Y-%m-%d}",
                lapor_lubang_index=True,
            )
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
    p_rec = sub.add_parser("recommendations", help="backfill + tampilkan track record kandidat beli")
    p_rec.add_argument("--months", type=int, default=6, help="panjang histori (default: 6 bulan)")
    p_rec.add_argument("--k", type=int, nargs="+", default=[5, 10, 21], help="horizon hari bursa (default: 5 10 21)")
    sub.add_parser("watchlist", help="fetch watchlist tickers (default: from .env)")
    p_ts = sub.add_parser(
        "trade-summary",
        help="agregat pasar reguler vs non-reguler (volume lot + value) per tanggal",
    )
    p_ts.add_argument("--date", help="YYYY-MM-DD (default: tanggal terbaru)")
    p_ts.add_argument(
        "--rebuild",
        action="store_true",
        help="hitung ulang semua tanggal dari research.raw_eod yang tersimpan",
    )
    p_own = sub.add_parser("ownership", help="snapshot komposisi pemegang saham (aksi pemilik)")
    p_own.add_argument("--codes", help="daftar kode dipisah koma (default: watchlist .env)")
    p_own.add_argument("--all", action="store_true", help="semua emiten tercatat (berat, ±950 request)")
    p_own.add_argument("--date", help="tanggal snapshot ISO YYYY-MM-DD (default: hari ini)")
    sub.add_parser(
        "health",
        help="cek data EOD hari bursa terakhir: kesegaran, cakupan kolom, baris per "
        "sesi, lubang sesi (exit 1 kalau ada temuan)",
    )
    sub.add_parser("serve", help="start continuous polling loop")
    p_refresh = sub.add_parser(
        "refresh",
        help="one-shot full refresh pipeline (EOD + recompute + clear cache)",
    )
    p_refresh.add_argument(
        "--skip-if-fresh",
        action="store_true",
        help="lewati bila EOD saham + close indeks hari ini sudah ada",
    )
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
    elif args.command == "health":
        cmd_health(args)
    elif args.command == "ic":
        cmd_ic(args)
    elif args.command == "signal-log":
        cmd_signal_log(args)
    elif args.command == "recommendations":
        cmd_recommendations(args)
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
    elif args.command == "trade-summary":
        cmd_trade_summary(args)
    elif args.command == "serve":
        cmd_serve(args)
    elif args.command == "refresh":
        cmd_refresh(args)
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
