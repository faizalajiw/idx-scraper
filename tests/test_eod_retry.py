"""Kunci perilaku percobaan ulang langkah EOD (``cli._retry_step``).

Kenapa ada: 15 Okt 2026 IDX menolak ``GetStockSummary`` dengan 403 pada 16:10
(``serve``) dan 16:15 (task ``IDX-EOD``), sementara permintaan identik pada
17:40 berhasil 963 baris dalam 0,3 detik. Pipeline saat itu sekali jalan lalu
menyerah, jadi EOD sehari hilang tanpa percobaan kedua. Tes ini menjaga agar
percobaan ulang itu tidak hilang diam-diam dan jedanya tetap terbatas.

Tidak menyentuh jaringan, DB, maupun Chrome — seluruh langkah diganti stub.
"""

from __future__ import annotations

import pytest

from idx_scraper import cli


@pytest.fixture()
def sleeps(monkeypatch) -> list[int]:
    """Rekam jeda alih-alih benar-benar tidur."""
    direkam: list[int] = []
    monkeypatch.setattr(cli.time, "sleep", lambda detik: direkam.append(detik))
    return direkam


def _beruntun(hasil: list) -> object:
    """Bikin ``step()`` yang mengembalikan ``hasil`` berurutan; Exception dilempar."""
    sisa = list(hasil)
    panggilan: list[int] = []

    def step() -> object:
        panggilan.append(1)
        nilai = sisa.pop(0)
        if isinstance(nilai, Exception):
            raise nilai
        return nilai

    step.panggilan = panggilan  # type: ignore[attr-defined]
    return step


def test_berhenti_setelah_sukses(sleeps):
    step = _beruntun([963])
    assert cli._retry_step("EOD saham", step) == 963
    assert len(step.panggilan) == 1
    assert sleeps == []


def test_mengulang_dengan_jeda_berjenjang(sleeps):
    step = _beruntun([0, 0, 7])
    assert cli._retry_step("EOD saham", step) == 7
    assert len(step.panggilan) == 3
    assert sleeps == list(cli._EOD_RETRY_DELAYS)


def test_menyerah_dan_lapor_setelah_habis(sleeps, capsys):
    step = _beruntun([0, 0, 0])
    assert cli._retry_step("EOD saham", step) == 0
    assert len(step.panggilan) == len(cli._EOD_RETRY_DELAYS) + 1
    assert "tetap kosong" in capsys.readouterr().err


def test_exception_dihitung_sebagai_gagal(sleeps):
    step = _beruntun([RuntimeError("403 dari IDX"), 5])
    assert cli._retry_step("EOD saham", step) == 5
    assert len(step.panggilan) == 2
    assert sleeps == [cli._EOD_RETRY_DELAYS[0]]


def test_total_jeda_tetap_pendek():
    """Jendela penolakan panjang bukan tugas retry ini.

    Retry hanya menutup gangguan singkat; penolakan berjam-jam ditutup job
    ``eod_fallback`` (16:30, di dalam ``serve``). Batas 4 menit menjaga rantai
    16:10 tidak melewati job sore berikutnya tanpa disadari.
    """
    assert sum(cli._EOD_RETRY_DELAYS) <= 240
    assert len(cli._EOD_RETRY_DELAYS) <= 3


def test_job_eod_full_mengembalikan_jumlah(monkeypatch):
    class _Storage:
        def insert_eod_stock(self, row) -> None:
            pass

    class _Client:
        def fetch_stock_summary(self, tanggal: str) -> list:
            return [object(), object(), object()]

    monkeypatch.setattr(cli, "_ingest_eod_research", lambda rows: None)
    monkeypatch.setattr(cli, "_upsert_segment_day", lambda rows: None)

    assert cli._job_eod_full(_Client(), _Storage()) == 3


def _stub_pipeline(monkeypatch, panggilan: dict, hasil_eod: int, hasil_broker: int) -> None:
    """Ganti seluruh efek samping ``_job_pipeline_refresh`` dengan pencatat."""

    def eod(client, storage) -> int:
        panggilan["eod"] = panggilan.get("eod", 0) + 1
        return hasil_eod

    def broker() -> int:
        panggilan["broker"] = panggilan.get("broker", 0) + 1
        return hasil_broker

    monkeypatch.setattr(cli, "_job_eod_full", eod)
    monkeypatch.setattr(cli, "_job_broker_eod", broker)
    for nama in (
        "_job_index_eod_close",
        "_job_regime_daily",
        "_job_signal_log",
        "_job_recommendations",
        "_clear_api_cache",
        "run_smart_money_alerts",
        "run_recommendation_alerts",
    ):
        monkeypatch.setattr(cli, nama, lambda *a, **k: 0, raising=False)


def test_pipeline_mengulang_kedua_langkah_idx(monkeypatch, sleeps):
    """Dua langkah yang gagal 15 Okt harus lewat jalur percobaan ulang."""
    panggilan: dict = {}
    _stub_pipeline(monkeypatch, panggilan, hasil_eod=0, hasil_broker=0)
    # Retry penuh hanya berlaku sesudah close, sedangkan suite bisa dijalankan
    # jam berapa saja -> percobaan dipatok supaya tes tidak tergantung jam.
    monkeypatch.setattr(
        cli, "_retry_percobaan", lambda: len(cli._EOD_RETRY_DELAYS) + 1
    )

    cli._job_pipeline_refresh(object(), object(), label="tes", do_eod=True)

    percobaan = len(cli._EOD_RETRY_DELAYS) + 1
    assert panggilan["eod"] == percobaan
    assert panggilan["broker"] == percobaan


def test_pipeline_sebelum_close_tidak_mengulang(monkeypatch, sleeps):
    """Sebelum close EOD pasar belum terbit -> percobaan ulang dimatikan.

    ``refresh_s1`` 12:00 mencoba EOD pasar tiap hari dan selalu 0 baris (data
    belum ada). Kalau retry ikut jalan di sana, tiap hari dua langkah x tiga
    percobaan = menit terbuang plus beban IDX tanpa satu pun hasil.
    """
    panggilan: dict = {}
    _stub_pipeline(monkeypatch, panggilan, hasil_eod=0, hasil_broker=0)
    monkeypatch.setattr(cli, "_retry_percobaan", lambda: 1)

    cli._job_pipeline_refresh(object(), object(), label="tes", do_eod=True)

    assert panggilan["eod"] == 1
    assert panggilan["broker"] == 1
    assert sleeps == []


def test_retry_percobaan_mengikuti_jam_close():
    """1 percobaan sebelum 16:00 WIB, penuh sesudahnya."""
    from datetime import datetime

    penuh = len(cli._EOD_RETRY_DELAYS) + 1
    assert cli._retry_percobaan(datetime(2026, 10, 15, 12, 0, tzinfo=cli.WIB)) == 1
    assert cli._retry_percobaan(datetime(2026, 10, 15, 15, 59, tzinfo=cli.WIB)) == 1
    assert cli._retry_percobaan(datetime(2026, 10, 15, 16, 0, tzinfo=cli.WIB)) == penuh
    assert cli._retry_percobaan(datetime(2026, 10, 15, 17, 40, tzinfo=cli.WIB)) == penuh


def test_retry_step_sekali_jalan_diam(sleeps, capsys):
    """``percobaan=1`` = sekali jalan, tanpa jeda dan tanpa keluhan "tetap kosong"."""
    step = _beruntun([0])
    assert cli._retry_step("EOD saham", step, percobaan=1) == 0
    assert len(step.panggilan) == 1
    assert sleeps == []
    assert "tetap kosong" not in capsys.readouterr().err


def test_pipeline_kedua_dilewati_saat_masih_jalan(monkeypatch, capsys):
    """Dua pipeline tidak boleh jalan bersamaan (rantai molor vs jaring sore).

    Yang kedua dilewati, bukan diantrikan: pipeline yang sedang jalan sudah
    mengerjakan langkah yang sama (semuanya idempoten per tanggal).
    """
    panggilan: dict = {}

    def body(client, storage, *, label, do_eod) -> None:
        panggilan[label] = panggilan.get(label, 0) + 1
        # Simulasi job lain datang saat pipeline ini masih hidup.
        cli._job_pipeline_refresh(client, storage, label="kedua", do_eod=False)

    monkeypatch.setattr(cli, "_pipeline_refresh_body", body)

    cli._job_pipeline_refresh(object(), object(), label="pertama", do_eod=True)

    assert panggilan == {"pertama": 1}
    err = capsys.readouterr().err
    assert "kedua" in err and "masih berjalan" in err


def test_kunci_dilepas_setelah_pipeline(monkeypatch):
    """Kunci harus bebas lagi setelah pipeline selesai maupun setelah gagal."""

    def body_ok(client, storage, *, label, do_eod) -> None:
        return None

    def body_gagal(client, storage, *, label, do_eod) -> None:
        raise RuntimeError("DB putus")

    monkeypatch.setattr(cli, "_pipeline_refresh_body", body_ok)
    cli._job_pipeline_refresh(object(), object(), label="satu", do_eod=False)

    monkeypatch.setattr(cli, "_pipeline_refresh_body", body_gagal)
    with pytest.raises(RuntimeError):
        cli._job_pipeline_refresh(object(), object(), label="dua", do_eod=False)

    monkeypatch.setattr(cli, "_pipeline_refresh_body", body_ok)
    cli._job_pipeline_refresh(object(), object(), label="tiga", do_eod=False)

    assert cli._PIPELINE_BUSY.locked() is False


def test_pipeline_tanpa_eod_tidak_menyentuh_idx(monkeypatch, sleeps):
    """Pra-buka & akhir pekan (``do_eod=False``) tidak boleh memicu EOD."""
    panggilan: dict = {}
    _stub_pipeline(monkeypatch, panggilan, hasil_eod=963, hasil_broker=88)
    monkeypatch.setattr(cli, "_retry_percobaan", lambda: 1)

    cli._job_pipeline_refresh(object(), object(), label="tes", do_eod=False)

    assert "eod" not in panggilan
