"""Kontrak logging terpusat: satu pintu `say`, berkas harian, redaksi rahasia."""

from __future__ import annotations

import logging
import sys

import pytest

from idx_scraper import logging_setup as ls


@pytest.fixture
def log_dir(monkeypatch, tmp_path):
    """Aktifkan logging berkas di tmp; pulihkan handler root sesudah tes."""
    akar = logging.getLogger()
    lama = list(akar.handlers)
    monkeypatch.setenv("IDX_LOG_DIR", str(tmp_path / "log"))
    monkeypatch.setenv("IDX_LOG_LEVEL", "DEBUG")
    monkeypatch.setattr(ls, "_aktif", False)
    yield tmp_path / "log"
    akar.handlers[:] = lama
    monkeypatch.setattr(ls, "_aktif", False)


def satu_berkas(log_dir) -> str:
    berkas = list(log_dir.iterdir())
    assert len(berkas) == 1
    return berkas[0].read_text(encoding="utf-8")


def test_say_sebelum_setup_cetak_langsung_ke_stream(capsys, monkeypatch) -> None:
    monkeypatch.setattr(ls, "_aktif", False)
    ls.say("halo")
    ls.say("keluaran salah", err=True)
    tertangkap = capsys.readouterr()
    assert tertangkap.out.strip() == "halo"
    assert tertangkap.err.strip() == "keluaran salah"


def test_setup_idempoten_tidak_menggandakan_handler(log_dir) -> None:
    akar = logging.getLogger()
    sebelum = len(akar.handlers)
    ls.setup(job="uji")
    sesudah = len(akar.handlers)
    assert sesudah > sebelum  # stderr + berkas
    ls.setup(job="eod")
    assert len(akar.handlers) == sesudah


def test_say_setelah_setup_masuk_berkas_dengan_job_dan_level(log_dir, capsys) -> None:
    ls.setup(job="eod")
    ls.say("job berjalan")
    ls.say("gagal sambung", err=True)

    isi = satu_berkas(log_dir)
    assert "[eod] idx_scraper.logging_setup: job berjalan" in isi
    assert "ERROR" in isi and "gagal sambung" in isi
    # stempel waktu ikut: bukan sekadar pesan mentah
    assert isi.split(" ")[0].count("-") == 2
    assert capsys.readouterr().out == ""  # tidak menulis langsung ke stdout


def test_redaksi_menyembunyikan_dsn_dan_token(log_dir) -> None:
    ls.setup(job="redaksi")
    ls.say("konek ke postgresql://postgres:rahasia@localhost:5432/idx_scraper")
    ls.say("TELEGRAM_BOT_TOKEN=abc123def")
    isi = satu_berkas(log_dir)
    assert "rahasia" not in isi
    assert "postgres:***@localhost:5432/idx_scraper" in isi
    assert "abc123def" not in isi


def test_level_env_menyaring_record_di_bawah_ambang(log_dir, monkeypatch) -> None:
    monkeypatch.setenv("IDX_LOG_LEVEL", "WARNING")
    ls.setup(job="uji")
    ls.say("detil biasa")  # INFO
    assert "detil biasa" not in satu_berkas(log_dir)
    ls.say("peringatan", err=True)  # ERROR
    assert "peringatan" in satu_berkas(log_dir)


def test_say_tanpa_setup_tetap_aman_untuk_kwarg_print(capsys, monkeypatch) -> None:
    monkeypatch.setattr(ls, "_aktif", False)
    ls.say("emoji", "aman", flush=True, file=sys.stdout)
    assert capsys.readouterr().out.strip() == "emoji aman"
