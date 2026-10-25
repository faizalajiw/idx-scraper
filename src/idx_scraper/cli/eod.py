"""Pipeline EOD: close indeks, retry, ingest, refresh, seed, snapshot."""

from __future__ import annotations

import os
import threading
import time
from datetime import date, datetime

import dotenv

from .alerts import _is_market_hours, run_recommendation_alerts, run_smart_money_alerts
from .fetch import (
    WIB,
    _parse_watchlist,
    fetch_and_store_indices,
    fetch_and_store_watchlist,
    liquid_codes,
    seed_historical_data,
    show_signals,
)
from .jobs import _job_broker_eod, _job_recommendations, _job_regime_daily, _job_signal_log

dotenv.load_dotenv()


from .. import db
from ..client import (
    IDXClient,
)
from ..logging_setup import say
from ..storage import get_storage_from_env


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
    dsn = db.dsn()
    if not dsn:
        return
    try:
        from ..jobs.backfill_index_eod_idx import backfill_index_eod_idx

        upserted, snapshots = backfill_index_eod_idx(dsn)
        say(
            f"[{datetime.now(WIB).isoformat()}] index EOD close (IDX live) — "
            f"upsert={upserted}, snapshot baru={snapshots}"
        )
    except Exception as e:
        say(f"[err] index EOD close job: {e}", err=True)


_EOD_RETRY_DELAYS: tuple[int, ...] = (60, 180)


def _retry_percobaan(now: datetime | None = None) -> int:
    """Jumlah percobaan untuk langkah EOD berat, menurut jam WIB.

    Sebelum tenggat EOD (17:30) ringkasan pasar memang belum terbit: di
    ``_serve.log`` EOD penuh hanya pernah berhasil 17:13:38 (16 Okt) dan
    17:16:16 (19 Okt), sementara ``refresh_s1`` 12:00 dan rantai 16:10 tiap hari
    dibalas 0 baris. Percobaan ulang di jendela itu cuma membuang ~4 menit plus
    tiga baris log dan menambah beban IDX tanpa bisa berhasil, jadi dimatikan
    (1 percobaan). Sesudah tenggat retry penuh — di situlah jaring kedua 17:30
    bekerja, dan ia memang berjalan setelah datanya ada.
    """
    from ..health import EOD_DEADLINE_HOUR, EOD_DEADLINE_MINUTE

    now = now or datetime.now(WIB)
    if (now.hour, now.minute) >= (EOD_DEADLINE_HOUR, EOD_DEADLINE_MINUTE):
        return len(_EOD_RETRY_DELAYS) + 1
    return 1


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
            say(f"[err] {label}: {e}", err=True)
        if baris > 0:
            return baris
        if i + 1 < percobaan:
            jeda = _EOD_RETRY_DELAYS[min(i, len(_EOD_RETRY_DELAYS) - 1)]
            say(
                f"[warn] {label}: 0 baris — percobaan {i + 2}/{percobaan} dalam {jeda}s",
                err=True,
            )
            time.sleep(jeda)
    if percobaan > 1:
        say(
            f"[err] {label}: tetap kosong setelah {percobaan} percobaan",
            err=True,
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
        say(f"[err] agregat pasar reguler/non-reguler {day}: {e}", err=True)
    return count


def _job_eod_full(client: IDXClient, storage) -> int:
    """Full market EOD snapshot at market close. Kembalikan jumlah baris."""
    count = _simpan_eod_hari(client, storage, datetime.now(WIB).date())
    say(f"[{datetime.now(WIB).isoformat()}] EOD full market: {count} stocks")
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
        say(f"[{datetime.now(WIB).isoformat()}] cache analitik dikosongkan")
    except Exception as e:
        say(f"[warn] gagal clear cache API: {e}", err=True)


_PIPELINE_BUSY = threading.Lock()


def _job_pipeline_refresh(client: IDXClient, storage, *, label: str, do_eod: bool) -> None:
    """Satu siklus refresh — hanya satu pipeline boleh jalan pada satu waktu.

    Job yang datang saat pipeline lain masih hidup dilewati dengan pesan
    ``pipeline lain masih berjalan``; pekerjaannya sudah tercakup yang sedang
    jalan (semua langkah idempoten per tanggal).
    """
    if not _PIPELINE_BUSY.acquire(blocking=False):
        say(
            f"[warn] {label}: pipeline lain masih berjalan — dilewati",
            err=True,
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
    Percobaan ulang dimatikan sebelum tenggat EOD - lihat ``_retry_percobaan``.
    """
    say(f"[{datetime.now(WIB).isoformat()}] pipeline refresh — {label}")
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
            say(f"[err] refresh {name}: {e}", err=True)
    _retry_step("broker summary", _job_broker_eod, percobaan=percobaan)
    _clear_api_cache()
    # Alert jejak smart money: verdict jendela 10 sesi baru berubah sekarang
    # (ada data EOD baru), jadi ini titik yang tepat untuk cek transisi +
    # kirim Telegram. Dibungkus try/except — gagal tak menghentikan refresh.
    if do_eod:
        try:
            n_sm = run_smart_money_alerts(_parse_watchlist(os.getenv("IDX_WATCHLIST")))
            if n_sm:
                say(f"[alerts] jejak smart money: {n_sm} transisi terkirim")
        except Exception as e:
            say(f"[err] refresh alert smart money: {e}", err=True)
        try:
            n_rec = run_recommendation_alerts()
            if n_rec:
                say(f"[alerts] kandidat beli grade A: {n_rec} terkirim")
        except Exception as e:
            say(f"[err] refresh alert rekomendasi: {e}", err=True)


def _ingest_eod_research(rows) -> None:
    """Append the same EOD rows into research.raw_eod (append-only) + refresh
    prices_pit. Keeps the research superset current with name + foreign flow.
    IDX omits PreviousPrice on EOD, so prev_close is recovered from close-change.
    """
    import json

    from ..market_segment import ensure_table

    dsn = db.dsn()
    if not dsn:
        return
    inserted = quarantined = 0
    with db.connect() as conn, conn.cursor() as cur:
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
                (
                    r.code,
                    trade_date,
                    _ohl(r.open),
                    _ohl(r.high),
                    _ohl(r.low),
                    r.close,
                    prev_close,
                    r.volume,
                    r.value,
                    r.frequency,
                    r.source,
                    r.name,
                    r.foreign_buy,
                    r.foreign_sell,
                    r.foreign_net,
                    r.non_regular_volume,
                    r.non_regular_value,
                    r.non_regular_frequency,
                ),
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
    say(f"[research] raw_eod +{inserted}, quarantine +{quarantined}, prices_pit refreshed")


def _upsert_segment_day(rows) -> None:
    """Agregat reguler vs non-reguler per tanggal -> research.market_segment_daily.

    Dipanggil setelah ingest EOD supaya hero dashboard bisa menampilkan
    volume/value pasar non-reguler, bukan cuma pasar reguler. Gagal di sini
    tak boleh menjatuhkan pipeline EOD — dibungkus pemanggil.
    """
    from ..market_segment import aggregate_day, upsert_day

    dsn = db.dsn()
    if not dsn:
        return
    by_date: dict[str, list] = {}
    for r in rows:
        try:
            trade_date = datetime.strptime(r.date, "%Y%m%d").replace(tzinfo=WIB).date().isoformat()
        except (TypeError, ValueError):
            continue
        by_date.setdefault(trade_date, []).append(r)

    with db.connect() as conn, conn.cursor() as cur:
        for trade_date, day_rows in by_date.items():
            upsert_day(cur, trade_date, aggregate_day(day_rows))


def cmd_trade_summary(args) -> None:
    """Tampilkan (dan opsional isi ulang) agregat pasar reguler/non-reguler."""
    from ..market_segment import _int, _num

    dsn = db.dsn()
    if not dsn:
        say("DATABASE_URL belum di-set — tidak bisa baca/menulis agregat.")
        return

    with db.connect() as conn, conn.cursor() as cur:
        if getattr(args, "rebuild", False):
            from types import SimpleNamespace

            from ..market_segment import aggregate_day, ensure_table, upsert_day

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
            say(f"[trade-summary] rebuild {len(by_date)} tanggal dari research.raw_eod")

        cur.execute(
            """select * from research.market_segment_daily
               where trade_date = coalesce(%s::date, (select max(trade_date) from research.market_segment_daily))""",
            (getattr(args, "date", None),),
        )
        row = cur.fetchone()

    if not row:
        say("Belum ada agregat. Jalankan `idx eod` dulu (atau `idx trade-summary --rebuild`).")
        return

    def _lot(v: int | None) -> str:
        return f"{(v or 0) / 100:,.0f}"

    say(f"Pasar {row['trade_date']}  (emiten: {row['stock_count']})")
    say(
        f"  Reguler     : volume {_lot(_int(row['regular_volume']))} lot "
        f"({_int(row['regular_volume']):,} lembar) · value Rp {_num(row['regular_value']):,.0f} · freq {_int(row['regular_freq']):,}"
    )
    say(
        f"  Non-reguler : volume {_lot(_int(row['nonreg_volume']))} lot "
        f"({_int(row['nonreg_volume']):,} lembar) · value Rp {_num(row['nonreg_value']):,.0f} · freq {_int(row['nonreg_freq']):,}"
    )
    say(
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
            say(f"scanning {len(codes)} most liquid tickers...")
        else:
            codes = _parse_watchlist(os.getenv("IDX_WATCHLIST"))
        n_stock = fetch_and_store_watchlist(client, storage, codes)
        say(f"snapshot done — index={n_idx}, stocks={n_stock}")
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
        say(f"EOD fetch done — {count} stocks stored")
    finally:
        client.close()
        storage.close()


def _eod_fresh_for(day) -> tuple[bool, bool]:
    """Cek kesegaran EOD untuk ``day`` (tanggal WIB).

    Mengembalikan ``(ada EOD saham, ada close indeks resmi)``. Close dianggap
    resmi kalau ada snapshot ``index_quotes`` pukul >= 16:00 **atau** baris close
    harian di ``index_summary_daily``: sesi yang snapshot-nya terlewat (mesin
    mati) tetap lengkap asal close hariannya ada, dan justru itu yang dibaca
    ``regime_daily``. Dipakai startup catchup ``serve`` dan jaring EOD sore
    ``_eod_catchup``: satu definisi "sudah lengkap" supaya keduanya tidak saling
    meniarakan pekerjaan.
    """
    dsn = db.dsn()
    if not dsn:
        return (False, False)
    with db.connect(autocommit=False) as conn, conn.cursor() as cur:
        cur.execute(
            "select 1 from research.latest_pit where trade_date = %s limit 1",
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
        if not has_index:
            cur.execute(
                "select 1 from index_summary_daily where code = 'COMPOSITE' and date = %s limit 1",
                (day,),
            )
            has_index = cur.fetchone() is not None
    return (has_stocks, has_index)


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
            say(
                f"[refresh] data EOD {today.isoformat()} sudah lengkap "
                "(saham + indeks) — dilewati (--skip-if-fresh)"
            )
            return
    client = IDXClient()
    storage = get_storage_from_env()
    try:
        _job_pipeline_refresh(client, storage, label="manual refresh (idx refresh)", do_eod=True)
    finally:
        client.close()
        storage.close()


def cmd_seed(args) -> None:
    """Seed historical data for watchlist tickers."""
    storage = get_storage_from_env()
    tickers = _parse_watchlist(os.getenv("IDX_WATCHLIST"))
    days = args.days if hasattr(args, "days") and args.days else 180
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
