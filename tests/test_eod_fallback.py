"""Kunci jaring EOD sore (``cli._eod_catchup`` + job ``eod_fallback``).

Kenapa ada: 15 Okt 2026 EOD hilang karena IDX menolak ``GetStockSummary`` pada
16:10 (rantai ``serve``) dan 16:15 (task Windows ``IDX-EOD``), sementara
permintaan identik 17:40 berhasil 963 baris dalam 0,3 detik. Retry 60/180 detik
di dalam rantai hanya menutup gangguan pendek, jadi jaring kedua harus ada —
dan sejak 15 Okt 2026 letaknya DI DALAM ``serve`` (16:30), bukan lagi sebagai
task Windows yang bisa bertabrakan dengan rantai 16:10.

Tes ini mengunci janji itu: keluar cepat kalau hari ini sudah lengkap, kerja
hanya yang kurang, dan tidak menyentuh DB sebelum pasar tutup.

Tidak menyentuh jaringan, DB, maupun Chrome — semua efek samping distub.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from idx_scraper import cli
from idx_scraper.cli import backfill


class _Jam(datetime):
    """``datetime`` dengan ``now()`` yang dipatok lewat ``_MOMEN``.

    ``_eod_catchup`` membaca jam sendiri, jadi tes harus bisa membekukannya.
    Subclass (bukan mock) supaya ``strptime``/``isoformat`` tetap asli.
    """

    @classmethod
    def now(cls, tz=None):
        return _MOMEN["nilai"]


_MOMEN: dict[str, datetime] = {}


@pytest.fixture()
def beku(monkeypatch):
    """Patok ``cli.datetime.now()`` pada satu momen WIB."""

    def _pasang(momen: datetime) -> datetime:
        _MOMEN["nilai"] = momen
        monkeypatch.setattr(backfill, "datetime", _Jam)
        return momen

    return _pasang


def _wib(hari: int, jam: int, menit: int = 0) -> datetime:
    """Oktober 2026: 16 = Jumat, 17 = Sabtu."""
    return datetime(2026, 10, hari, jam, menit, tzinfo=cli.WIB)


def _stub_jalur(monkeypatch, dicatat: dict) -> dict:
    """Ganti tiap langkah yang bisa dijalankan ``_eod_catchup`` dengan pencatat."""

    def pipeline(client, storage, *, label, do_eod) -> None:
        dicatat.setdefault("pipeline", []).append((label, do_eod))

    def index() -> None:
        dicatat.setdefault("index", []).append(1)

    def cache() -> None:
        dicatat.setdefault("cache", []).append(1)

    monkeypatch.setattr(backfill, "_job_pipeline_refresh", pipeline)
    monkeypatch.setattr(backfill, "_job_index_eod_close", index)
    monkeypatch.setattr(backfill, "_clear_api_cache", cache)
    return dicatat


def _fresh(monkeypatch, saham: bool, indeks: bool) -> None:
    monkeypatch.setattr(backfill, "_eod_fresh_for", lambda day: (saham, indeks))


def _jangan_sentuh_db(monkeypatch, hari_ini: date) -> None:
    """Tolak query kesegaran untuk HARI INI — hari lampau tetap boleh dicek.

    Fase "hari ini" tidak boleh menyentuh DB sebelum pasar tutup; fase lampau
    (``_backfill_hari_lampau``) justru selalu ikut jalan dan memeriksa tanggal
    yang lebih tua, jadi yang dilarang hanya tanggal ``hari_ini``.
    """

    def _panggil(day):
        if day == hari_ini:
            raise AssertionError("_eod_fresh_for tidak boleh dipanggil untuk hari ini")
        return (True, True)  # tidak ada lubang: backfill tidak menarik apa pun

    monkeypatch.setattr(backfill, "_eod_fresh_for", _panggil)


def test_lewat_sebelum_pasar_tutup(monkeypatch, beku):
    """Sebelum 16:00 jangan menyentuh DB: EOD pasar memang belum terbit."""
    beku(_wib(16, 10))  # Jumat 10:00
    monkeypatch.setenv("IDX_HOLIDAYS", "")
    _jangan_sentuh_db(monkeypatch, date(2026, 10, 16))

    assert cli._eod_catchup(object(), object(), label="tes") == "bukan-hari-bursa"


def test_lewat_akhir_pekan(monkeypatch, beku):
    beku(_wib(17, 16, 30))  # Sabtu
    monkeypatch.setenv("IDX_HOLIDAYS", "")
    _jangan_sentuh_db(monkeypatch, date(2026, 10, 17))

    assert cli._eod_catchup(object(), object(), label="tes") == "bukan-hari-bursa"


def test_lewat_hari_libur_bursa(monkeypatch, beku):
    """Hari libur nasional: tidak ada data pasar -> jangan jalankan apa-apa."""
    beku(_wib(16, 16, 30))  # Jumat, tapi ada di IDX_HOLIDAYS
    monkeypatch.setenv("IDX_HOLIDAYS", "2026-10-16")
    _jangan_sentuh_db(monkeypatch, date(2026, 10, 16))

    assert cli._eod_catchup(object(), object(), label="tes") == "bukan-hari-bursa"


def test_sudah_lengkap_keluar_tanpa_kerja(monkeypatch, beku):
    """Ini jalur normal tiap hari: rantai 16:10 berhasil -> ~1 detik lalu keluar."""
    beku(_wib(16, 16, 30))
    monkeypatch.setenv("IDX_HOLIDAYS", "")
    _fresh(monkeypatch, saham=True, indeks=True)
    dicatat = _stub_jalur(monkeypatch, {})

    assert cli._eod_catchup(object(), object(), label="tes") == "lengkap"
    assert dicatat == {}


def test_saham_belum_ada_jalankan_pipeline(monkeypatch, beku, capsys):
    """Kasus 15 Okt: EOD saham kosong -> pipeline penuh, bukan sekadar indeks."""
    beku(_wib(16, 16, 30))
    monkeypatch.setenv("IDX_HOLIDAYS", "")
    _fresh(monkeypatch, saham=False, indeks=False)
    dicatat = _stub_jalur(monkeypatch, {})

    assert cli._eod_catchup(object(), object(), label="tes") == "pipeline"
    assert len(dicatat["pipeline"]) == 1
    _, do_eod = dicatat["pipeline"][0]
    assert do_eod is True
    assert "index" not in dicatat
    assert "menjalankan pipeline penuh" in capsys.readouterr().out


def test_hanya_indeks_belum_ada(monkeypatch, beku):
    """Saham sudah lengkap tapi close resmi COMPOSITE belum -> cukup indeks."""
    beku(_wib(16, 16, 30))
    monkeypatch.setenv("IDX_HOLIDAYS", "")
    _fresh(monkeypatch, saham=True, indeks=False)
    dicatat = _stub_jalur(monkeypatch, {})

    assert cli._eod_catchup(object(), object(), label="tes") == "indeks"
    assert "pipeline" not in dicatat
    assert len(dicatat["index"]) == 1
    assert len(dicatat["cache"]) == 1


def test_job_eod_fallback_memakai_catchup(monkeypatch):
    """Job terjadwal hanyalah pembungkus tipis ``_eod_catchup`` — satu definisi."""
    dipanggil: dict = {}

    def catchup(client, storage, *, label):
        dipanggil["label"] = label
        return "lengkap"

    monkeypatch.setattr(backfill, "_eod_catchup", catchup)

    assert cli._job_eod_fallback(object(), object()) is None
    assert dipanggil["label"] == "ulang EOD sore"


@pytest.mark.parametrize(
    "hari,libur,harapan",
    [
        (16, "", True),  # Jumat biasa
        (17, "", False),  # Sabtu
        (16, "2026-10-16", False),  # libur nasional di hari kerja
    ],
)
def test_hari_bursa(monkeypatch, hari, libur, harapan):
    monkeypatch.setenv("IDX_HOLIDAYS", libur)
    assert cli._hari_bursa(date(2026, 10, hari)) is harapan
