"""Alert Telegram, health-check EOD, dan job indeks/watchlist/berita."""

from __future__ import annotations

import os
import sys
from datetime import datetime

import dotenv

from .fetch import (
    WIB,
    _parse_watchlist,
    fetch_and_store_indices,
    fetch_and_store_indices_yahoo,
    fetch_and_store_watchlist,
    fetch_and_store_watchlist_yahoo,
)

dotenv.load_dotenv()


from ..client import (
    IDXClient,
)
from ..logging_setup import say
from ..notify import (
    SignalState,
    TelegramNotifier,
    format_recommendation_message,
    format_rule_message,
    format_smart_money_message,
    run_alert_check,
)
from ..source_state import (
    SOURCE_IDX,
    current_source,
    maybe_probe_and_switch,
)
from ..storage import get_storage_from_env


def _lapor_bisu(penjaga: str, sudah: bool) -> bool:
    """Laporkan penjaga yang tidak memeriksa apa pun; Telegram cukup sekali per run.

    Empat jalur bisa sama-sama bisu dari satu akar masalah (``DATABASE_URL``
    hilang, DB kosong). Pesan pertama yang menyebut sebabnya sudah cukup —
    sisanya masuk log saja, supaya alarm tidak jadi empat pesan kembar.
    """
    from ..health import format_no_data_message

    stamp = datetime.now(WIB).isoformat()
    if sudah:
        say(f"[{stamp}] health {penjaga} — 0 sesi terbaca (sudah dilaporkan)")
    else:
        TelegramNotifier().send_message(format_no_data_message(penjaga))
        say(f"[{stamp}] health {penjaga} — 0 sesi terbaca (dilaporkan)")
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
    (``in_progress``) karena sesi yang sedang ditulis (jaring kedua baru menarik
    EOD sesi ini pukul 17:30) bukan kerusakan.

    "Tidak ada yang diperiksa" dihitung masalah, bukan diam: kalau
    ``research.raw_eod`` kosong atau ``DATABASE_URL`` tidak diset, cakupan kolom
    / baris / lubang semuanya lolos tanpa suara — mode bisu yang alarm ini ada
    untuk menutupnya (kartu Dashboard menandai keadaan itu `Waspada`). Telegram
    untuk keadaan itu dikirim sekali (``_lapor_bisu``), bukan sekali per jalur.
    """
    bisu = False
    try:
        from ..health import check_eod_health, format_health_message

        h = check_eod_health()
        if not h.ok:
            TelegramNotifier().send_message(format_health_message(h))
            say(f"[{datetime.now(WIB).isoformat()}] health EOD TELAT — {', '.join(h.missing)}")
        elif h.missing:
            # Mis. `DATABASE_URL` tidak diset: bukan "telat", tapi bukan OK juga.
            bisu = _lapor_bisu("Kesegaran EOD", bisu)
            say(f"[err] health EOD: {', '.join(h.missing)}", err=True)
        else:
            say(f"[{datetime.now(WIB).isoformat()}] health EOD OK — {h.expected}")
    except Exception as e:
        _cetak_aman(f"[err] health EOD: {e}", err=True)

    # Cek kedua: tanggal bisa ada tapi kolom foreign_* bolong (9 bulan
    # tanpa alarm pada 2025-09-24 → 2026-07-29). Telegram dikirim sebelum
    # log dicetak supaya alarm tidak hilang gara-gara encoding konsol.
    try:
        from ..health import check_field_coverage, format_field_coverage_message

        c = check_field_coverage()
        if c.checked == 0:
            bisu = _lapor_bisu("Cakupan kolom foreign_*", bisu)
        elif not c.ok:
            TelegramNotifier().send_message(format_field_coverage_message(c))
            say(
                f"[{datetime.now(WIB).isoformat()}] health cakupan kolom timpang — "
                f"{len(c.troublemakers)}/{c.checked} sesi"
            )
    except Exception as e:
        _cetak_aman(f"[err] health cakupan kolom: {e}", err=True)

    # Cek ketiga: tanggal ada, isinya cuma separuh sesi (insert tersela).
    try:
        from ..health import check_row_counts, format_row_count_message

        r = check_row_counts(in_progress=datetime.now(WIB).date())
        if r.checked == 0:
            bisu = _lapor_bisu("Jumlah baris per sesi", bisu)
        elif not r.ok:
            TelegramNotifier().send_message(format_row_count_message(r))
            say(
                f"[{datetime.now(WIB).isoformat()}] health sesi separuh — "
                f"{len(r.troublemakers)}/{r.checked} sesi di bawah median "
                f"{r.median_rows} baris"
            )
    except Exception as e:
        _cetak_aman(f"[err] health sesi separuh: {e}", err=True)

    # Cek keempat: hari bursa tanpa baris sama sekali di tengah rentang data.
    try:
        from ..health import check_session_gaps, format_session_gaps_message

        g = check_session_gaps(in_progress=datetime.now(WIB).date())
        if g.sessions == 0:
            bisu = _lapor_bisu("Lubang sesi", bisu)
        elif not g.ok:
            TelegramNotifier().send_message(format_session_gaps_message(g))
            say(
                f"[{datetime.now(WIB).isoformat()}] health lubang sesi — "
                f"{len(g.missing)} hari bursa tanpa baris"
            )
    except Exception as e:
        _cetak_aman(f"[err] health lubang sesi: {e}", err=True)


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
            say(
                f"alerts terkirim — sinyal: {signals}, aturan: {rules}, "
                f"smart money: {smart}, kandidat beli: {recs}"
            )
        else:
            say("alerts: tidak ada sinyal/aturan/smart money/kandidat yang berubah")
    finally:
        storage.close()


def _cetak_aman(pesan: str, file=None, err: bool = False) -> None:
    """Print yang tidak mematikan alarm di konsol yang tak bisa emoji.

    Konsol Windows (cp1252) melempar ``UnicodeEncodeError`` saat mencetak "⚠️";
    sisa pesannya tetap terbaca setelah emoji diganti, jadi informasi tidak hilang.
    Dipakai juga untuk pesan error di jalur alarm — pesan exception yang berisi
    non-ASCII tidak boleh menjatuhkan jalur penjaga berikutnya.
    """
    pengodean = (
        getattr(file, "encoding", None)
        if file is not None
        else getattr(sys.stderr, "encoding", None)
    )
    try:
        pesan.encode(pengodean or "utf-8")
    except (UnicodeEncodeError, LookupError):
        pesan = pesan.encode("ascii", "replace").decode("ascii")
    say(pesan, file=file, err=err)


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

    - **kesegaran**: lewat tenggat (default 17:30 WIB — ``IDX_EOD_DEADLINE_HOUR``
      dan ``IDX_EOD_DEADLINE_MINUTE``, di belakang jendela terbit IDX) dan data
      hari bursa belum masuk. Sesi hari ini yang belum terbit bukan kegagalan:
      labelnya "SESI BERJALAN" dan exit code-nya 0
    - **cakupan kolom** ``foreign_*`` di ``sessions`` sesi terakhir (timpang)
    - **baris per sesi**: sesi yang isinya di bawah 50% median ("sesi separuh")
    - **lubang sesi**: hari bursa tanpa baris di antara sesi pertama & terakhir

    Pemeriksa **baris per sesi** melewati sesi hari ini, sama seperti di job
    ``health_eod``: sesi yang sedang ditulis (jaring kedua baru menarik EOD
    sesi ini pukul 17:30) bukan kerusakan. (Pemeriksa lubang sesi tidak terpengaruh —
    lubang hanya dihitung di dalam rentang sesi yang sudah ada.)

    Pemeriksa yang **tidak memeriksa apa pun** (``checked == 0``, mis. DB kosong
    atau ``DATABASE_URL`` tidak diset) dihitung bermasalah, bukan hijau — sama
    seperti kartu "Kesehatan Data" di dashboard. Tiap pemeriksa punya penangkap
    sendiri, jadi satu yang melempar tidak menghapus laporan yang lain.
    """
    from ..health import (
        EOD_DEADLINE_HOUR,
        EOD_DEADLINE_MINUTE,
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
        say(
            f"hari bursa diharapkan: {h.expected} | saham terbaru: {h.stocks_latest} "
            f"| close IHSG terbaru: {h.index_latest}"
        )
    if c is not None:
        say(f"cakupan kolom foreign_*: {c.checked} sesi diperiksa, terburuk {c.worst_pct:.1f}%")
        if c.checked == 0:
            masalah.append("cakupan kolom: tidak ada sesi yang diperiksa")
        elif not c.ok:
            _cetak_aman(format_field_coverage_message(c))
            masalah.append(f"cakupan kolom: {len(c.troublemakers)}/{c.checked} sesi")
    if r is not None:
        say(
            f"baris per sesi: median {r.median_rows} baris, {len(r.troublemakers)} sesi "
            f"di bawah {r.threshold_pct:.0f}% median ({r.checked} sesi diperiksa)"
        )
        if r.checked == 0:
            masalah.append("baris per sesi: tidak ada sesi yang diperiksa")
        elif not r.ok:
            _cetak_aman(format_row_count_message(r))
            masalah.append(f"sesi separuh: {len(r.troublemakers)}/{r.checked} sesi")
    if g is not None:
        say(
            f"lubang sesi: {len(g.missing)} hari bursa tanpa baris "
            f"({g.sessions} sesi, basis {g.basis})"
        )
        if g.sessions == 0:
            masalah.append("lubang sesi: tidak ada sesi yang diperiksa")
        elif not g.ok:
            _cetak_aman(format_session_gaps_message(g))
            masalah.append(f"lubang sesi: {len(g.missing)} hari")

    # `EodHealth.missing` juga dipakai untuk masalah konfigurasi dan untuk sesi
    # hari ini yang memang belum terbit. Labelnya dipilih dari SEBABNYA, bukan
    # dari `h.ok`: dulu sesi tengah hari dicetak "CEK KONFIGURASI" lalu keluar 1,
    # jadi `idx health || kirim-alert` di cron berbunyi tiap siang tanpa ada yang
    # rusak. `h.in_window` = sesi hari ini belum lewat tenggat dan setiap sumber
    # paling jauh tertinggal satu sesi bursa — satu-satunya keadaan yang keluar 0
    # tanpa mencetak OK.
    gagal = bool(masalah) or (
        h is not None and (h.config_problem or (bool(h.missing) and not h.in_window))
    )
    if h is not None and h.missing:
        if h.config_problem:
            label = "CEK KONFIGURASI — "
        elif h.in_window:
            label = (
                "SESI BERJALAN — belum jatuh tenggat "
                f"{EOD_DEADLINE_HOUR:02d}:{EOD_DEADLINE_MINUTE:02d} WIB: "
            )
        elif h.overdue:
            label = "TELAT — belum masuk: "
        else:
            label = "TERTINGGAL — belum masuk: "
        say(label + ", ".join(h.missing))

    if h is not None and not h.missing and not masalah:
        say("OK — data EOD terbaru lengkap")
        return
    if masalah:
        say("TIMPANG — " + "; ".join(masalah))
    if gagal:
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

    from ..alert_rules import RuleState
    from ..api import alerts as alerts_api

    try:
        evaluations = alerts_api.evaluate_all()
    except Exception as e:  # DB down / config missing must not kill the scheduler
        say(f"[err] aturan pantauan dilewati: {e}", err=True)
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

    from ..api import analytics as analytics_api
    from ..recommendation_state import RecommendationState

    try:
        board = analytics_api.get_recommendations(limit=50, min_grade="A")
    except Exception as e:  # DB down / config missing must not kill the scheduler
        say(f"[err] alert rekomendasi dilewati: {e}", err=True)
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

    from ..api import analytics as analytics_api
    from ..smart_money_state import SmartMoneyState

    try:
        verdicts = analytics_api.get_smart_money_verdicts(codes)
    except Exception as e:  # DB down / config missing must not kill the scheduler
        say(f"[err] alert smart money dilewati: {e}", err=True)
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
        say(
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
    say(f"[{datetime.now(WIB).isoformat()}] index refreshed ({tag}): {n}")


def _job_watchlist(client: IDXClient, storage, codes: list[str]) -> None:
    if not _is_market_hours():
        return
    if current_source() == SOURCE_IDX:
        n = fetch_and_store_watchlist(client, storage, codes)
        tag = "IDX"
    else:
        n = fetch_and_store_watchlist_yahoo(storage, codes)
        tag = "YAHOO"
    say(f"[{datetime.now(WIB).isoformat()}] watchlist refreshed ({tag}): {n}")


def _job_news_refresh() -> None:
    """Tarik judul Stockbit Snips -> news_items. Error tidak menghentikan scheduler."""
    try:
        from ..news import run_once

        n = run_once()
        say(f"[{datetime.now(WIB).isoformat()}] news: {n} item baru")
    except Exception as e:
        say(f"[err] news job: {e}", err=True)
