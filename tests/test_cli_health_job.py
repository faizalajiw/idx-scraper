"""Unit test jalur alarm `_job_health_eod` & `idx health` — tanpa DB/Telegram nyata.

Kenapa ada: penjaga cakupan kolom baru (``check_field_coverage``) dijalankan
sebelum alarm lama "Data EOD belum masuk". Kalau keduanya berada dalam satu
``try``, exception dari penjaga baru (DB putus, ``connect_timeout``, tabel
kosong) ditelan ``except`` dan alarm lama — satu-satunya peringatan telat yang
sudah ada sebelum penjaga baru — tidak pernah terkirim.

Sejak penjaga "sesi separuh" (``check_row_counts``) dan "lubang sesi"
(``check_session_gaps``) ikut disambungkan ke jalur alarm, ketentuan itu berlaku
untuk empat jalur sekaligus: kesegaran, cakupan kolom, baris per sesi, lubang
sesi. Tes di sini memaksa satu penjaga melempar dan menuntut alarm jalur lain
tetap terkirim.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from idx_scraper import cli, health
from idx_scraper.cli import alerts

WIB = timezone(timedelta(hours=7))


class _Notifier:
    """Menangkap pesan Telegram tanpa jaringan."""

    enabled = True

    def __init__(self) -> None:
        self.pesan: list[str] = []

    def send_message(self, pesan: str, *args, **kwargs) -> None:
        self.pesan.append(pesan)


def _eod_telat() -> health.EodHealth:
    return health.EodHealth(
        expected=date(2026, 10, 16),
        stocks_latest=date(2026, 10, 15),
        index_latest=date(2026, 10, 15),
        checked_at=datetime(2026, 10, 16, 16, 45, tzinfo=WIB),
        missing=["saham EOD (terbaru 2026-10-15)"],
        overdue=True,
    )


def _eod_sesi_berjalan() -> health.EodHealth:
    """Senin 15:16 di `evaluate_health` sungguhan: sesi hari ini belum terbit.

    Keadaan ini normal (pasar belum tutup), bukan kerusakan - tapi sebelum
    perbaikan, `idx health` mencetak "CEK KONFIGURASI" dan keluar 1.
    """
    return health.evaluate_health(
        expected=date(2026, 10, 19),
        stocks_latest=date(2026, 10, 16),
        index_latest=date(2026, 10, 16),
        now=datetime(2026, 10, 19, 15, 16, tzinfo=WIB),
    )


def _eod_tertinggal() -> health.EodHealth:
    """Sabtu: sesi Jumat belum masuk dan memang sudah lewat - tertinggal, bukan menunggu."""
    return health.evaluate_health(
        expected=date(2026, 10, 9),
        stocks_latest=date(2026, 10, 8),
        index_latest=date(2026, 10, 8),
        now=datetime(2026, 10, 10, 10, 0, tzinfo=WIB),
    )


def test_penjaga_cakupan_melempar_alarm_eod_lama_tetap_terkirim(monkeypatch):
    """I2: exception dari penjaga cakupan tidak boleh mematikan alarm lama."""
    notifier = _Notifier()
    monkeypatch.setattr(alerts, "TelegramNotifier", lambda: notifier)
    monkeypatch.setattr(health, "check_eod_health", _eod_telat)

    def _meledak() -> health.FieldCoverage:
        raise RuntimeError("connection timeout")

    monkeypatch.setattr(health, "check_field_coverage", _meledak)

    cli._job_health_eod()

    assert notifier.pesan == [health.format_health_message(_eod_telat())]


def test_alarm_cakupan_kolom_tetap_terkirim(monkeypatch):
    """Regression pin: jalur penjaga baru tetap berbunyi (jangan mati saat memisah try)."""
    notifier = _Notifier()
    monkeypatch.setattr(alerts, "TelegramNotifier", lambda: notifier)
    monkeypatch.setattr(health, "check_eod_health", _eod_telat)
    timpang = health.FieldCoverage(
        checked=30, troublemakers=["2026-10-15 (900/1000, 90.0%)"], worst_pct=90.0
    )
    monkeypatch.setattr(health, "check_field_coverage", lambda: timpang)

    cli._job_health_eod()

    assert notifier.pesan == [
        health.format_health_message(_eod_telat()),
        health.format_field_coverage_message(timpang),
    ]


# --------------------------------------------------------------------------- #
# Penjaga sesi separuh & lubang sesi (sejak disambungkan ke jalur alarm)
# --------------------------------------------------------------------------- #


def _eod_ok() -> health.EodHealth:
    return health.EodHealth(
        expected=date(2026, 10, 16),
        stocks_latest=date(2026, 10, 16),
        index_latest=date(2026, 10, 16),
        checked_at=datetime(2026, 10, 16, 16, 45, tzinfo=WIB),
    )


def _cov_ok() -> health.FieldCoverage:
    return health.FieldCoverage(checked=30, worst_pct=100.0)


def _rows_ok() -> health.RowCountCoverage:
    return health.RowCountCoverage(checked=30, median_rows=963)


def _rows_separuh() -> health.RowCountCoverage:
    return health.RowCountCoverage(
        checked=30,
        median_rows=963,
        troublemakers=["2026-10-15 (120/963 baris, 12.5% median)"],
        worst_pct=12.5,
    )


def _gaps_ok() -> health.SessionGaps:
    return health.SessionGaps(first=date(2024, 10, 15), last=date(2026, 10, 16), sessions=476)


def _gaps_lubang() -> health.SessionGaps:
    return health.SessionGaps(
        first=date(2024, 10, 15),
        last=date(2026, 10, 16),
        sessions=476,
        missing=[date(2026, 10, 7)],
    )


def _diamkan_jalur_lama(monkeypatch) -> None:
    """Jalur kesegaran & cakupan kolom dibuat bersih supaya yang diuji jelas."""
    monkeypatch.setattr(health, "check_eod_health", _eod_ok)
    monkeypatch.setattr(health, "check_field_coverage", _cov_ok)


def test_alarm_sesi_separuh_terkirim(monkeypatch):
    notifier = _Notifier()
    monkeypatch.setattr(alerts, "TelegramNotifier", lambda: notifier)
    _diamkan_jalur_lama(monkeypatch)
    monkeypatch.setattr(health, "check_row_counts", lambda **kw: _rows_separuh())
    monkeypatch.setattr(health, "check_session_gaps", lambda **kw: _gaps_ok())

    cli._job_health_eod()

    assert notifier.pesan == [health.format_row_count_message(_rows_separuh())]


def test_alarm_lubang_sesi_terkirim(monkeypatch):
    notifier = _Notifier()
    monkeypatch.setattr(alerts, "TelegramNotifier", lambda: notifier)
    _diamkan_jalur_lama(monkeypatch)
    monkeypatch.setattr(health, "check_row_counts", lambda **kw: _rows_ok())
    monkeypatch.setattr(health, "check_session_gaps", lambda **kw: _gaps_lubang())

    cli._job_health_eod()

    assert notifier.pesan == [health.format_session_gaps_message(_gaps_lubang())]


def test_penjaga_sesi_separuh_melempar_tiga_alarm_lain_tetap_terkirim(monkeypatch):
    """Exception di satu penjaga tak boleh menelan alarm penjaga lain."""
    notifier = _Notifier()
    monkeypatch.setattr(alerts, "TelegramNotifier", lambda: notifier)
    monkeypatch.setattr(health, "check_eod_health", _eod_telat)
    monkeypatch.setattr(health, "check_field_coverage", _cov_ok)

    def _meledak(**kwargs):
        raise RuntimeError("connection timeout")

    monkeypatch.setattr(health, "check_row_counts", _meledak)
    monkeypatch.setattr(health, "check_session_gaps", lambda **kw: _gaps_lubang())

    cli._job_health_eod()

    assert notifier.pesan == [
        health.format_health_message(_eod_telat()),
        health.format_session_gaps_message(_gaps_lubang()),
    ]


def test_penjaga_lubang_melempar_alarm_separuh_tetap_terkirim(monkeypatch):
    notifier = _Notifier()
    monkeypatch.setattr(alerts, "TelegramNotifier", lambda: notifier)
    _diamkan_jalur_lama(monkeypatch)
    monkeypatch.setattr(health, "check_row_counts", lambda **kw: _rows_separuh())

    def _meledak(**kwargs):
        raise RuntimeError("connection timeout")

    monkeypatch.setattr(health, "check_session_gaps", _meledak)

    cli._job_health_eod()

    assert notifier.pesan == [health.format_row_count_message(_rows_separuh())]


def test_job_melewati_sesi_hari_ini(monkeypatch):
    """Job sore jam 16:45: sesi yang mungkin masih ditulis tidak dinilai."""
    notifier = _Notifier()
    monkeypatch.setattr(alerts, "TelegramNotifier", lambda: notifier)
    _diamkan_jalur_lama(monkeypatch)

    diterima: dict[str, object] = {}

    def _rekap(**kwargs):
        diterima.update(kwargs)
        return _rows_ok()

    monkeypatch.setattr(health, "check_row_counts", _rekap)
    monkeypatch.setattr(health, "check_session_gaps", lambda **kw: _gaps_ok())

    cli._job_health_eod()

    assert diterima["in_progress"] == datetime.now(WIB).date()


# --------------------------------------------------------------------------- #
# "Tidak ada yang diperiksa" bukan hijau — jalur mana pun
# --------------------------------------------------------------------------- #


def _rows_kosong() -> health.RowCountCoverage:
    return health.RowCountCoverage(checked=0)


def _cov_kosong() -> health.FieldCoverage:
    return health.FieldCoverage(checked=0)


def _gaps_kosong() -> health.SessionGaps:
    return health.SessionGaps(first=None, last=None, sessions=0)


def test_alarm_job_berbunyi_kalau_tak_ada_yang_diperiksa(monkeypatch):
    """DB kosong / `DATABASE_URL` hilang: penjaga bisu itu masalah, bukan diam.

    Empat penjaga bisa bisu dari satu akar masalah — Telegram cukup sekali.
    """
    notifier = _Notifier()
    monkeypatch.setattr(alerts, "TelegramNotifier", lambda: notifier)
    _diamkan_jalur_lama(monkeypatch)
    monkeypatch.setattr(health, "check_field_coverage", _cov_kosong)
    monkeypatch.setattr(health, "check_row_counts", lambda **kw: _rows_kosong())
    monkeypatch.setattr(health, "check_session_gaps", lambda **kw: _gaps_kosong())

    cli._job_health_eod()

    assert len(notifier.pesan) == 1
    assert "tidak memeriksa apa pun" in notifier.pesan[0]
    assert "Cakupan kolom" in notifier.pesan[0]  # penjaga pertama yang bisu


def test_alarm_job_satu_pesan_bisu_dari_kesegaran(monkeypatch, capsys):
    """Kalau kesegaran yang lebih dulu bisu, pesannya dari jalur itu — bukan dobel."""
    notifier = _Notifier()
    monkeypatch.setattr(alerts, "TelegramNotifier", lambda: notifier)
    _diamkan_jalur_lama(monkeypatch)
    monkeypatch.setattr(health, "check_row_counts", lambda **kw: _rows_kosong())
    monkeypatch.setattr(health, "check_session_gaps", lambda **kw: _gaps_kosong())
    monkeypatch.setattr(
        health,
        "check_eod_health",
        lambda: health.EodHealth(
            expected=date(2026, 10, 16),
            stocks_latest=None,
            index_latest=None,
            checked_at=datetime(2026, 10, 16, 16, 45, tzinfo=WIB),
            missing=["DATABASE_URL tidak di-set"],
        ),
    )

    cli._job_health_eod()

    assert len(notifier.pesan) == 1
    assert "Kesegaran EOD" in notifier.pesan[0]
    assert "DATABASE_URL" in notifier.pesan[0]
    assert "sudah dilaporkan" in capsys.readouterr().out


def test_job_penjaga_melempar_pesan_error_tak_menjatuhkan_jalur_lain(monkeypatch):
    """Handler `except` pun harus tahan konsol non-ASCII (pesan exception bebas)."""
    notifier = _Notifier()
    monkeypatch.setattr(alerts, "TelegramNotifier", lambda: notifier)
    _diamkan_jalur_lama(monkeypatch)

    def _meledak(**kwargs):
        raise RuntimeError("koneksi putus — é")

    monkeypatch.setattr(health, "check_row_counts", _meledak)
    monkeypatch.setattr(health, "check_session_gaps", lambda **kw: _gaps_lubang())

    cli._job_health_eod()

    assert notifier.pesan == [health.format_session_gaps_message(_gaps_lubang())]


# --------------------------------------------------------------------------- #
# `idx health` — exit code & keluaran (kontrak yang dipakai cron)
# --------------------------------------------------------------------------- #


def _semua_bersih(monkeypatch) -> None:
    _diamkan_jalur_lama(monkeypatch)
    monkeypatch.setattr(health, "check_row_counts", lambda **kw: _rows_ok())
    monkeypatch.setattr(health, "check_session_gaps", lambda **kw: _gaps_ok())


def test_cmd_health_bersih_exit_0(monkeypatch, capsys):
    _semua_bersih(monkeypatch)

    cli.cmd_health(None)

    keluaran = capsys.readouterr().out
    assert "OK — data EOD terbaru lengkap" in keluaran
    assert "baris per sesi: median 963 baris" in keluaran
    assert "lubang sesi: 0 hari bursa tanpa baris" in keluaran


def test_cmd_health_sesi_separuh_exit_1(monkeypatch, capsys):
    _diamkan_jalur_lama(monkeypatch)
    monkeypatch.setattr(health, "check_row_counts", lambda **kw: _rows_separuh())
    monkeypatch.setattr(health, "check_session_gaps", lambda **kw: _gaps_ok())

    with pytest.raises(SystemExit) as keluar:
        cli.cmd_health(None)

    assert keluar.value.code == 1
    keluaran = capsys.readouterr().out
    assert "Sesi EOD cuma separuh" in keluaran
    assert "TIMPANG — sesi separuh: 1/30 sesi" in keluaran


def test_cmd_health_lubang_sesi_exit_1(monkeypatch, capsys):
    _diamkan_jalur_lama(monkeypatch)
    monkeypatch.setattr(health, "check_row_counts", lambda **kw: _rows_ok())
    monkeypatch.setattr(health, "check_session_gaps", lambda **kw: _gaps_lubang())

    with pytest.raises(SystemExit) as keluar:
        cli.cmd_health(None)

    assert keluar.value.code == 1
    keluaran = capsys.readouterr().out
    assert "Ada hari bursa tanpa sesi" in keluaran
    assert "TIMPANG — lubang sesi: 1 hari" in keluaran


def test_cmd_health_telat_dan_penjaga_lain_sama_sama_dilaporkan(monkeypatch, capsys):
    monkeypatch.setattr(health, "check_eod_health", _eod_telat)
    monkeypatch.setattr(health, "check_field_coverage", _cov_ok)
    monkeypatch.setattr(health, "check_row_counts", lambda **kw: _rows_separuh())
    monkeypatch.setattr(health, "check_session_gaps", lambda **kw: _gaps_lubang())

    with pytest.raises(SystemExit) as keluar:
        cli.cmd_health(None)

    assert keluar.value.code == 1
    keluaran = capsys.readouterr().out
    assert "TELAT — belum masuk: saham EOD (terbaru 2026-10-15)" in keluaran
    assert "TIMPANG — sesi separuh: 1/30 sesi; lubang sesi: 1 hari" in keluaran


def test_cmd_health_melewati_sesi_hari_ini(monkeypatch):
    _semua_bersih(monkeypatch)

    diterima: dict[str, object] = {}

    def _rekap(**kwargs):
        diterima.update(kwargs)
        return _rows_ok()

    monkeypatch.setattr(health, "check_row_counts", _rekap)

    cli.cmd_health(None)

    assert diterima["in_progress"] == datetime.now(WIB).date()


def test_cmd_health_tak_ada_yang_diperiksa_exit_1(monkeypatch, capsys):
    """`checked == 0` bukan hijau: cron tak boleh diyakinkan oleh penjaga bisu."""
    _diamkan_jalur_lama(monkeypatch)
    monkeypatch.setattr(health, "check_field_coverage", _cov_kosong)
    monkeypatch.setattr(health, "check_row_counts", lambda **kw: _rows_kosong())
    monkeypatch.setattr(health, "check_session_gaps", lambda **kw: _gaps_kosong())

    with pytest.raises(SystemExit) as keluar:
        cli.cmd_health(None)

    assert keluar.value.code == 1
    keluaran = capsys.readouterr().out
    assert "cakupan kolom: tidak ada sesi yang diperiksa" in keluaran
    assert "baris per sesi: tidak ada sesi yang diperiksa" in keluaran
    assert "lubang sesi: tidak ada sesi yang diperiksa" in keluaran
    assert "OK — data EOD terbaru lengkap" not in keluaran


def test_cmd_health_penjaga_melempar_tetap_lapor_sisa_pemeriksa(monkeypatch, capsys):
    """Satu penjaga yang melempar tidak boleh membatalkan laporan penjaga lain."""
    _semua_bersih(monkeypatch)

    def _meledak():
        raise RuntimeError("connection timeout")

    monkeypatch.setattr(health, "check_field_coverage", _meledak)

    with pytest.raises(SystemExit) as keluar:
        cli.cmd_health(None)

    assert keluar.value.code == 1
    keluaran = capsys.readouterr().out
    assert "cakupan kolom: gagal diperiksa (connection timeout)" in keluaran
    assert "baris per sesi: median 963 baris" in keluaran  # penjaga lain tetap lapor
    assert "lubang sesi: 0 hari bursa tanpa baris" in keluaran


def test_cmd_health_dsn_hilang_bukan_ok(monkeypatch, capsys):
    """`EodHealth.missing` untuk masalah konfigurasi: bukan "telat", tetap bukan OK."""
    _semua_bersih(monkeypatch)
    monkeypatch.setattr(
        health,
        "check_eod_health",
        lambda: health.EodHealth(
            expected=date(2026, 10, 16),
            stocks_latest=None,
            index_latest=None,
            checked_at=datetime(2026, 10, 16, 16, 45, tzinfo=WIB),
            missing=["DATABASE_URL tidak di-set"],
        ),
    )

    with pytest.raises(SystemExit) as keluar:
        cli.cmd_health(None)

    assert keluar.value.code == 1
    keluaran = capsys.readouterr().out
    assert "CEK KONFIGURASI — DATABASE_URL tidak di-set" in keluaran
    assert "OK — data EOD terbaru lengkap" not in keluaran


def test_cmd_health_sesi_berjalan_exit_0(monkeypatch, capsys):
    """Sesi hari ini yang belum terbit bukan kegagalan: keluar 0, label "sedang berjalan".

    Sebelum ini keadaan tengah hari dicetak "CEK KONFIGURASI" dengan exit code 1
    - `idx health || kirim-alert` di cron berbunyi tiap siang tanpa ada yang
    rusak, dan labelnya menuduh konfigurasi yang sehat.
    """
    _semua_bersih(monkeypatch)
    monkeypatch.setattr(health, "check_eod_health", _eod_sesi_berjalan)

    cli.cmd_health(None)

    keluaran = capsys.readouterr().out
    assert "SESI BERJALAN" in keluaran
    assert "17:30" in keluaran  # tenggat disebut supaya jelas kapan harus peduli
    assert "CEK KONFIGURASI" not in keluaran
    assert "TELAT" not in keluaran


def test_cmd_health_tertinggal_sebelum_tenggat_exit_1(monkeypatch, capsys):
    """Sesi Jumat yang hilang terbaca "tertinggal" walau belum lewat tenggat hari ini."""
    _semua_bersih(monkeypatch)
    monkeypatch.setattr(health, "check_eod_health", _eod_tertinggal)

    with pytest.raises(SystemExit) as keluar:
        cli.cmd_health(None)

    assert keluar.value.code == 1
    keluaran = capsys.readouterr().out
    assert "TERTINGGAL — belum masuk: saham EOD (terbaru 2026-10-08)" in keluaran
