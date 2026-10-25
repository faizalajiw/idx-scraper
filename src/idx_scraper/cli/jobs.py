"""Job riset/sinyal (regime, IC, ownership, rekomendasi) + subcommand-nya."""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import dotenv

from .fetch import WIB, _parse_watchlist

dotenv.load_dotenv()


from .. import db
from ..client import (
    IDXClient,
)
from ..logging_setup import say


def _job_regime_daily() -> None:
    """Rekam klasifikasi regime hari bursa terakhir ke research.regime_daily."""
    dsn = db.dsn()
    if not dsn:
        return
    try:
        from ..research.regime import record_today

        row = record_today(dsn)
        if row:
            say(
                f"[{datetime.now(WIB).isoformat()}] regime {row['date']}: "
                f"{row['regime']} (ADX {row['adx']}, vol {row['vol_state']})"
            )
    except Exception as e:
        say(f"[err] regime harian: {e}", err=True)


def _job_monthly_ic() -> None:
    """IC analysis bulanan -> research.factor_ic_history (bobot composite)."""
    dsn = db.dsn()
    if not dsn:
        return
    try:
        from ..research.monthly_ic import run_and_store

        out = run_and_store(dsn)
        say(
            f"[{datetime.now(WIB).isoformat()}] IC bulanan tersimpan "
            f"run_date={out['run_date']} rows={out['rows']} bobot={out['weights']}"
        )
    except Exception as e:
        say(f"[err] IC bulanan: {e}", err=True)


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
        say(
            f"[{datetime.now(WIB).isoformat()}] ownership snapshot — "
            f"{ok}/{len(codes)} emiten, {total} baris"
        )
    except Exception as e:
        say(f"[err] ownership snapshot: {e}", err=True)


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
    dsn = db.dsn()
    if not dsn:
        return 0
    try:
        from ..jobs.backfill_broker_eod import backfill_broker_eod

        n, d = backfill_broker_eod(dsn)
        say(f"[{datetime.now(WIB).isoformat()}] broker summary {d}: {n} baris")
        return n
    except Exception as e:
        say(f"[err] broker summary job: {e}", err=True)
        return 0


def _job_signal_log() -> None:
    """Rekam sinyal BUY/SELL hari bursa terakhir ke research.signal_log.

    16:25 WIB — setelah close resmi IHSG + regime harian, supaya kolom regime
    ikut terisi. Idempoten per (code, trade_date); aman dijalankan ulang.
    """
    dsn = db.dsn()
    if not dsn:
        return
    try:
        from ..research.signal_log import load_log, record_recent

        # Window 1 bulan: cukup menutup koreksi data telat, jauh lebih murah
        # daripada menghitung ulang 18 bulan tiap hari.
        n = record_recent(dsn, months=1)
        log = load_log(dsn)
        last = str(log["date"].max().date()) if not log.empty else "-"
        say(
            f"[{datetime.now(WIB).isoformat()}] jejak sinyal tersimpan: "
            f"{n} baris (sinyal terakhir {last})"
        )
    except Exception as e:
        say(f"[err] jejak sinyal job: {e}", err=True)


def _job_recommendations() -> None:
    """Rekam kandidat beli hari bursa terakhir -> research.recommendation_log.

    Dijalankan setelah jejak sinyal: memakai input lapisan yang SAMA dengan API
    (bobot pola track record + penyesuaian faktor/broker bila tervalidasi),
    supaya baris yang diukur track record-nya persis yang tampil di dashboard.
    Idempoten per (code, trade_date); aman dijalankan ulang.
    """
    dsn = db.dsn()
    if not dsn:
        return
    try:
        from ..api import analytics as analytics_api
        from ..research.recommendation_log import record

        inputs = analytics_api.recommendation_record_inputs()
        n = record(dsn, months=1, **inputs)
        say(f"[{datetime.now(WIB).isoformat()}] rekomendasi tersimpan: {n} kandidat")
    except Exception as e:
        say(f"[err] rekomendasi job: {e}", err=True)


def cmd_recommendations(args) -> None:
    """Backfill + tampilkan track record kandidat beli (halaman Rekomendasi)."""
    dsn = db.dsn()
    if not dsn:
        say("[err] DATABASE_URL belum diisi", err=True)
        return
    from ..research.recommendation_log import evaluate, print_report, record

    # Input lapisan (bobot pola track record + faktor/broker bila tervalidasi)
    # diambil dari service supaya baris yang diukur = yang tampil di dashboard.
    # Best-effort: kalau API/DB belum siap, backfill tetap jalan tanpa lapisan itu.
    inputs: dict = {}
    try:
        from ..api import analytics as analytics_api

        inputs = analytics_api.recommendation_record_inputs()
    except Exception as e:
        say(f"[warn] input lapisan rekomendasi dilewati: {e}", err=True)

    written = record(dsn, months=args.months, **inputs)
    say(f"[ok] {written} baris kandidat ditulis")
    print_report(evaluate(dsn, horizons=tuple(args.k)))


def cmd_signal_log(args) -> None:
    """Backfill + tampilkan track record sinyal (jejak sinyal)."""
    dsn = db.dsn()
    if not dsn:
        say("[err] DATABASE_URL belum diisi", err=True)
        return
    from ..research.signal_log import backfill, evaluate, print_report

    written = backfill(dsn, months=args.months)
    say(f"[ok] {written} baris sinyal ditulis ke research.signal_log")
    print_report(evaluate(dsn, horizons=tuple(args.k)))


def cmd_ic(args) -> None:
    """Run IC analysis manual + simpan histori bobot faktor."""
    dsn = db.dsn()
    if not dsn:
        say("[err] DATABASE_URL belum diisi", err=True)
        return
    from ..research.monthly_ic import run_and_store

    out = run_and_store(dsn, horizons=args.k)
    say(f"run_date={out['run_date']} rows={out['rows']}")
    say(f"bobot composite terbaru: {out['weights']}")


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
    from ..ownership_store import snapshot_from_profile

    dsn = db.dsn()
    if not dsn:
        say("[err] DATABASE_URL belum diisi", err=True)
        return 0, 0
    if not codes:
        return 0, 0

    own_client = client is None
    client = client or IDXClient()
    ok = total = 0
    try:
        with db.connect() as conn, conn.cursor() as cur:
            for i, code in enumerate(codes, 1):
                try:
                    prof = client.fetch_company_profile(code)
                except Exception as e:
                    say(f"[warn] profil {code}: {e}", err=True)
                    prof = None
                n = snapshot_from_profile(cur, code, prof, snap)
                if n:
                    ok += 1
                total += n
                if i % 25 == 0:
                    say(f"  {i}/{len(codes)} — {total} baris")
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
                str(s.get("Code")).upper() for s in client.fetch_securities_stock() if s.get("Code")
            ]
        elif getattr(args, "codes", None):
            codes = [c.strip().upper() for c in args.codes.split(",") if c.strip()]
        else:
            codes = _parse_watchlist(os.getenv("IDX_WATCHLIST"))
        if not codes:
            say("tidak ada kode untuk di-snapshot")
            return
        say(f"snapshot kepemilikan {len(codes)} emiten...")
        ok, total = snapshot_ownership(codes, snap, client)
        say(f"ownership done — {ok}/{len(codes)} emiten, {total} baris")
    finally:
        client.close()
