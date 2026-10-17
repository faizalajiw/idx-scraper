"""Unit test jalur alarm job ``_job_health_eod`` — tanpa DB/Telegram nyata.

Kenapa ada: penjaga cakupan kolom baru (``check_field_coverage``) dijalankan
sebelum alarm lama "Data EOD belum masuk". Kalau keduanya berada dalam satu
``try``, exception dari penjaga baru (DB putus, ``connect_timeout``, tabel
kosong) ditelan ``except`` dan alarm lama — satu-satunya peringatan telat yang
sudah ada sebelum penjaga baru — tidak pernah terkirim. Tes ini memaksa penjaga
baru melempar dan menuntut alarm lama tetap dikirim.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from idx_scraper import cli, health

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


def test_penjaga_cakupan_melempar_alarm_eod_lama_tetap_terkirim(monkeypatch):
    """I2: exception dari penjaga cakupan tidak boleh mematikan alarm lama."""
    notifier = _Notifier()
    monkeypatch.setattr(cli, "TelegramNotifier", lambda: notifier)
    monkeypatch.setattr(health, "check_eod_health", _eod_telat)

    def _meledak() -> health.FieldCoverage:
        raise RuntimeError("connection timeout")

    monkeypatch.setattr(health, "check_field_coverage", _meledak)

    cli._job_health_eod()

    assert notifier.pesan == [health.format_health_message(_eod_telat())]


def test_alarm_cakupan_kolom_tetap_terkirim(monkeypatch):
    """Regression pin: jalur penjaga baru tetap berbunyi (jangan mati saat memisah try)."""
    notifier = _Notifier()
    monkeypatch.setattr(cli, "TelegramNotifier", lambda: notifier)
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
