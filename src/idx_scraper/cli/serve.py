"""cmd_serve: penjaga satu instance + registrasi jadwal APScheduler."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import dotenv

from .alerts import (
    _is_market_hours,
    _job_alerts,
    _job_health_eod,
    _job_indices,
    _job_news_refresh,
    _job_watchlist,
)
from .backfill import _BACKFILL_HARI, _eod_catchup, _job_eod_fallback
from .eod import _job_pipeline_refresh
from .fetch import WIB, _parse_watchlist, show_signals
from .jobs import _job_monthly_ic, _job_ownership, _next_weekday_at
from .journal import _job_journal_claims, _job_journal_outcomes, _job_journal_reminder

dotenv.load_dotenv()

from apscheduler.schedulers.blocking import BlockingScheduler

from .. import db
from ..client import (
    IDXClient,
)
from ..live_capture import LiveCapture
from ..logging_setup import say
from ..notify import (
    TelegramNotifier,
)
from ..source_state import (
    SOURCE_IDX,
    current_source,
)
from ..storage import get_storage_from_env

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
        say(
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
    #   16:30 eod_fallback  jaring kedua EOD; keluar ~1 detik kalau 16:10 berhasil;
    #                       slot 17:30 adalah yang pertama di belakang jendela
    #                       terbit IDX (ringkasan per saham terukur 17:13-17:16)
    #   16:50 daily_ic      evaluasi IC (DB murni)
    #   17:00 ownership     buka klien IDX sendiri (Senin) — 50 menit dari 16:10
    #   17:45 health_eod    alarm — SELALU setelah fallback DAN setelah tenggat 17:30
    #   17:55 journal       tulis klaim (diulang tiap jam), isi hasil 18:10,
    #                       pengingat 19:20
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
        _job_news_refresh,
        "interval",
        seconds=int(os.getenv("NEWS_INTERVAL", "900")),
        id="news_refresh",
    )

    # Pra-pembukaan Sen-Jum 08:50 - recompute + clear cache (belum ada EOD baru).
    # 08:50, bukan 08:45: task Windows (IDX-Serve/API/Web) menyalakan serve 08:45.
    # Jadwal yang lebih awal dari launcher akan terlewat di hari reboot - serve
    # lahir setelah jamnya lewat, jadi APScheduler menghitung jadwal berikutnya
    # (besok) dan recompute pra-buka hilang sehari tanpa alarm apa pun.
    scheduler.add_job(
        **_refresh("pra-buka Sen-Jum 08:50", do_eod=False),
        day_of_week="mon-fri",
        hour=8,
        minute=50,
        id="refresh_preopen",
    )
    # Close Sesi I: Sen-Kam 12:00, Jum 11:30 — tarik EOD + recompute.
    scheduler.add_job(
        **_refresh("close Sesi I Sen-Kam 12:00", do_eod=True),
        day_of_week="mon-thu",
        hour=12,
        minute=0,
        id="refresh_s1",
    )
    scheduler.add_job(
        **_refresh("close Sesi I Jum 11:30", do_eod=True),
        day_of_week="fri",
        hour=11,
        minute=30,
        id="refresh_s1_fri",
    )
    # Close Sesi II Sen-Jum 16:10 — offset 10 menit supaya Yahoo daily bar final.
    scheduler.add_job(
        **_refresh("close Sesi II Sen-Jum 16:10", do_eod=True),
        day_of_week="mon-fri",
        hour=16,
        minute=10,
        id="refresh_s2",
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
        _job_eod_fallback,
        "cron",
        day_of_week="mon-fri",
        hour="16-20",
        minute=30,
        args=[client, storage],
        id="eod_fallback",
        misfire_grace_time=7200,
        coalesce=True,
    )
    # Health-check EOD — Sen-Jum 17:45 WIB. SENGAJA setelah eod_fallback (17:30):
    # alarm harus menilai hasil jaring kedua, bukan hasil percobaan pertama.
    # Kalau ditaruh sebelum fallback, hari yang berhasil diselamatkan tetap
    # memicu Telegram "EOD TELAT" — alarm palsu yang lama-lama diabaikan.
    # 17:45, bukan lagi 16:45: ringkasan EOD per saham IDX terukur baru terbit
    # 17:13-17:16, jadi alarm 16:45 berbunyi "TELAT" 6 dari 6 hari bursa padahal
    # datanya belum ada di mana pun.
    # PENTING: alarm hanya berbunyi setelah tenggat 17:30
    # (`IDX_EOD_DEADLINE_HOUR`/`IDX_EOD_DEADLINE_MINUTE`). Jangan memindahkan job
    # ini ke jam sebelum itu — cek akan lolos tanpa suara.
    # `misfire_grace_time` 2 jam + `coalesce` seperti `eod_fallback`: laptop yang
    # tidur melewati 17:45 membuat alarm hari itu terlewat, dan tanpa grace job-nya
    # dibuang begitu saja — padahal ceknya masih relevan begitu mesin bangun
    # (mis. 18:30, sebelum jendela tutup).
    scheduler.add_job(
        _job_health_eod,
        "cron",
        day_of_week="mon-fri",
        hour=17,
        minute=45,
        id="health_eod",
        misfire_grace_time=7200,
        coalesce=True,
    )
    # Akhir pekan Sab-Min 08:45 & 16:00 — recompute + clear cache (pasar tutup).
    scheduler.add_job(
        **_refresh("akhir pekan 08:45", do_eod=False),
        day_of_week="sat,sun",
        hour=8,
        minute=45,
        id="refresh_weekend_am",
    )
    scheduler.add_job(
        **_refresh("akhir pekan 16:00", do_eod=False),
        day_of_week="sat,sun",
        hour=16,
        minute=0,
        id="refresh_weekend_pm",
    )
    # IC analysis HARIAN — 16:50 WIB, di ujung urutan sore. Sebelumnya bulanan
    # (tanggal 1); karena panel flow baru bertambah tiap hari dan gate IC tinggal
    # menunggu sampel memadai, evaluasi harian memastikan faktor aliran langsung
    # terpakai begitu lolos gate — bobot composite hold-check otomatis mengikuti.
    # Murni DB, tapi tetap ditaruh setelah rantai EOD supaya sore hari tidak ada
    # dua job yang hidup bersamaan sama sekali.
    scheduler.add_job(
        _job_monthly_ic,
        "cron",
        day_of_week="mon-fri",
        hour=16,
        minute=50,
        id="daily_ic",
    )
    # Buku klaim (P6) — ditulis SESUDAH sinyal/IC selesai dan SEBELUM hasilnya
    # diketahui; itu seluruh gunanya. Diulang tiap jam 17:55-21:55 seperti
    # `eod_fallback`: kalau data sesi belum masuk ia mencatat penolakannya dan
    # mencoba lagi jam berikutnya, dan kalau klaimnya sudah ada ia tidak menulis
    # apa-apa (kunci code+tanggal+aturan). Tidak ada jalur yang menimpa.
    # 17:55, bukan lagi 17:05: EOD sesi hari ini baru masuk lewat jaring kedua
    # 17:30, jadi tulisan 17:05 selalu menganggap sesi kemarin — lalu pengingat
    # 17:50 menemukan sesi hari ini bisu dan mengirim alarm palsu tiap hari bursa.
    scheduler.add_job(
        _job_journal_claims,
        "cron",
        day_of_week="mon-fri",
        hour="17-21",
        minute=55,
        id="journal_claims",
        misfire_grace_time=7200,
        coalesce=True,
    )
    # Isi hasil 18:10: mengukur klaim yang horizonnya sudah berjalan. Perpindahan
    # status tidak pernah dibatalkan, jadi job ini boleh jalan berkali-kali.
    # Menit :55, :10, dan :20 — tiap :30 ditempati `eod_fallback` dan :45 alarm
    # EOD; dua job cron di menit yang sama adalah pola yang dilarang di jadwal ini.
    scheduler.add_job(
        _job_journal_outcomes,
        "cron",
        day_of_week="mon-fri",
        hour=18,
        minute=10,
        id="journal_outcomes",
        misfire_grace_time=7200,
        coalesce=True,
    )
    # Pengingat 19:20: klaim yang levelnya tersentuh, ATAU alarm kalau sesi ini
    # tidak melahirkan klaim DAN tidak mencatat penolakan apa pun (job yang gagal
    # tanpa bersuara justru yang paling perlu terdengar). Setelah DUA kesempatan
    # tulis (17:55 dan 18:55), bukan setelah satu: sesi yang datanya baru masuk
    # sore tetap sempat direkam sebelum pengingat ini memvonisnya "bisu".
    scheduler.add_job(
        _job_journal_reminder,
        "cron",
        day_of_week="mon-fri",
        hour=19,
        minute=20,
        id="journal_reminder",
        misfire_grace_time=7200,
        coalesce=True,
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
        _job_ownership,
        "interval",
        weeks=1,
        start_date=_next_weekday_at(0, 17, 0),
        id="ownership_weekly",
        replace_existing=True,
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
        say(f"live capture — {live_top} liquid + 20 movers, {live_delay_ms}ms/req, rolling sweep")
    else:
        say("live capture OFF (IDX_LIVE_TOP=0)")

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
        say(f"telegram alerts (sinyal + aturan pantauan) every {alert_interval}s")
    else:
        say("telegram alerts OFF (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID belum diisi)")

    say(f"serving — index every {idx_interval}s, watchlist every {wl_interval}s")
    say(f"watchlist: {watchlist[:10]}...")
    say(
        f"live source: {current_source()} (auto-switch ke IDX saat probe 200; "
        f"probe tiap {int(os.getenv('IDX_PROBE_INTERVAL', '900'))}s)"
    )
    say(
        "refresh pipeline 8 menu - Sen-Jum 08:50, Sen-Kam 12:00 / Jum 11:30, "
        "Sen-Jum 16:10; akhir pekan 08:45 & 16:00 (recompute+clear cache)"
    )
    say(
        "jaring EOD sore — 16:30, diulang tiap jam sampai 20:30 (keluar cepat "
        "kalau EOD hari ini sudah lengkap); "
        "health EOD 17:45; IC 16:50; ownership Senin 17:00"
    )
    say(
        "buku klaim lapis 4 — tulis 17:55 lalu tiap jam sampai 21:55 (idempoten), "
        "isi hasil 18:10, pengingat 19:20; gerbang penahan punya sebab sendiri"
    )
    say(
        f"catchup EOD — hari ini (startup + jaring sore) dan {_BACKFILL_HARI} hari "
        "bursa terakhir yang bolong (IDX_BACKFILL_DAYS); close indeks lampau "
        "tidak bisa ditarik otomatis"
    )
    # --- Startup catchup: if today's EOD data is missing, run it now -------
    # Prevents missed refreshes when scheduler restarts after the cron window.
    # Logika yang sama dipakai job `eod_fallback` (16:30) lewat `_eod_catchup`,
    # jadi definisi "sudah lengkap" di sore hari hanya ada satu. Jalur startup
    # sekaligus melaporkan lubang close indeks hari lampau (tidak bisa ditarik
    # otomatis) — di jalur sore laporan itu sengaja dimatikan supaya log bersih.
    try:
        if db.tersedia():
            _eod_catchup(
                client,
                storage,
                label=f"startup {datetime.now(WIB):%Y-%m-%d}",
                lapor_lubang_index=True,
            )
    except Exception as e:
        say(f"[warn] catchup check gagal: {e}", err=True)

    try:
        scheduler.start()
    except KeyboardInterrupt:
        pass
    finally:
        scheduler.shutdown()
        live_capture.stop()
        client.close()
        storage.close()


def _tutup_pool_api() -> None:
    """Tutup pool koneksi API kalau jalur CLI ini sempat memakainya.

    Helper pembacaan di ``api/`` membuka pool sendiri saat pertama dipakai
    (``api/database.init_pool``). Di proses panjang (API, ``serve``) pool itu memang
    hidup terus; di proses CLI pendek ia masih hidup saat interpreter bubar, dan
    Python menutup job dengan ``PythonFinalizationError`` di log. Menutupnya di sini
    menghilangkan noise itu tanpa mengubah perilaku: pool berikutnya dibuat ulang
    saat dibutuhkan.
    """
    from ..api.database import close_pool

    close_pool()
