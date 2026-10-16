"""Kunci backfill hari bursa lampau (``cli._backfill_hari_lampau``).

Kenapa ada: ``eod_catchup`` hanya melihat HARI INI. Mesin yang tidur/off melewati
seluruh jendela sore (16:10–22:30) kehilangan satu hari bursa untuk selamanya —
``health_eod`` pun tidak menangkapnya karena ia membandingkan tanggal TERBARU,
bukan lubang di tengah. Fase kedua ``_eod_catchup`` menutupnya dari dua
kesempatan yang sudah ada: startup ``serve`` dan jaring sore ``eod_fallback``.

Ikut dikunci di sini: ``_job_index_eod_close`` tidak boleh menulis baris berlabel
16:00 sebelum jam close. Dulu pra-buka 08:45 & Sesi I 12:00 ikut memanggilnya,
sehingga nilai jam itu tersimpan sebagai "close resmi hari ini" — menang
``order by captured_at desc`` di dashboard dan membuat ``_eod_fresh_for``
mengklaim close sudah ada.

Tidak menyentuh jaringan, DB, maupun Chrome — semua efek samping distub.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from idx_scraper import cli


class _Jam(datetime):
    """``datetime`` dengan ``now()`` yang dipatok lewat ``_MOMEN``."""

    @classmethod
    def now(cls, tz=None):
        return _MOMEN["nilai"]


_MOMEN: dict[str, datetime] = {}


@pytest.fixture()
def beku(monkeypatch):
    """Patok ``cli.datetime.now()`` pada satu momen WIB."""

    def _pasang(momen: datetime) -> datetime:
        _MOMEN["nilai"] = momen
        monkeypatch.setattr(cli, "datetime", _Jam)
        return momen

    return _pasang


def _wib(hari: int, jam: int, menit: int = 0) -> datetime:
    """Oktober 2026: 12=Sen, 13=Sel, 14=Rab, 15=Kam, 16=Jum, 17=Sab, 18=Min."""
    return datetime(2026, 10, hari, jam, menit, tzinfo=cli.WIB)


class _Client:
    """IDX palsu: catat tanggal yang diminta, balas ``jumlah`` baris."""

    def __init__(self, jumlah: int = 963) -> None:
        self.panggilan: list[str] = []
        self._jumlah = jumlah

    def fetch_stock_summary(self, tanggal: str) -> list:
        self.panggilan.append(tanggal)
        return [object() for _ in range(self._jumlah)]


class _Storage:
    def __init__(self) -> None:
        self.baris = 0

    def insert_eod_stock(self, row) -> None:
        self.baris += 1


@pytest.fixture()
def jalur(monkeypatch) -> dict:
    """Ganti tiga tujuan tulis + recompute dengan pencatat."""
    dicatat: dict = {"ingest": [], "segment": [], "pipeline": [], "index": []}
    monkeypatch.setattr(
        cli, "_ingest_eod_research", lambda rows: dicatat["ingest"].append(len(rows))
    )
    monkeypatch.setattr(
        cli, "_upsert_segment_day", lambda rows: dicatat["segment"].append(len(rows))
    )
    monkeypatch.setattr(
        cli,
        "_job_pipeline_refresh",
        lambda c, s, *, label, do_eod: dicatat["pipeline"].append((label, do_eod)),
    )
    monkeypatch.setattr(cli, "_job_index_eod_close", lambda: dicatat["index"].append(1))
    return dicatat


def _fresh(monkeypatch, kosong: set[date]) -> None:
    """Segar semua kecuali tanggal di ``kosong`` (saham & close indeks hilang)."""

    def _cek(day):
        return (day not in kosong, day not in kosong)

    monkeypatch.setattr(cli, "_eod_fresh_for", _cek)


# ------------------------------------------------------------------ penanggalan


def test_hari_bursa_mundur_melewati_akhir_pekan(monkeypatch):
    monkeypatch.setenv("IDX_HOLIDAYS", "")
    assert cli._hari_bursa_mundur(date(2026, 10, 19), 3) == [
        date(2026, 10, 19),  # Senin
        date(2026, 10, 16),  # Jumat (Sabtu & Minggu dilewati)
        date(2026, 10, 15),
    ]


def test_hari_bursa_mundur_melewati_libur(monkeypatch):
    monkeypatch.setenv("IDX_HOLIDAYS", "2026-10-15")
    assert cli._hari_bursa_mundur(date(2026, 10, 16), 3) == [
        date(2026, 10, 16),
        date(2026, 10, 14),  # 15 libur
        date(2026, 10, 13),
    ]


def test_hari_bursa_mundur_urut_dan_terbatas(monkeypatch):
    monkeypatch.setenv("IDX_HOLIDAYS", "")
    hasil = cli._hari_bursa_mundur(date(2026, 10, 16), 5)
    assert len(hasil) == 5
    assert hasil == sorted(hasil, reverse=True)


def test_hari_bursa_mundur_tidak_berputar_tanpa_akhir(monkeypatch):
    """Semua libur: loop tetap berhenti, tidak berputar mencari hari bursa."""

    def _hari(i: int) -> str:
        return (date(2026, 10, 16) - timedelta(days=i)).isoformat()

    monkeypatch.setenv("IDX_HOLIDAYS", ",".join(_hari(i) for i in range(60)))
    assert cli._hari_bursa_mundur(date(2026, 10, 16), 5) == []


# ---------------------------------------------------------------------- backfill


def test_tanpa_lubang_tidak_menyentuh_idx(monkeypatch, beku, jalur):
    """Jalur normal: satu query DB per tanggal, nol request IDX."""
    beku(_wib(16, 16, 30))
    monkeypatch.setenv("IDX_HOLIDAYS", "")
    _fresh(monkeypatch, kosong=set())
    client = _Client()

    assert cli._backfill_hari_lampau(client, _Storage(), label="tes") == 0
    assert client.panggilan == []
    assert jalur["pipeline"] == []


def test_menarik_hari_lampau_yang_bolong(monkeypatch, beku, jalur, capsys):
    beku(_wib(16, 16, 30))
    monkeypatch.setenv("IDX_HOLIDAYS", "")
    _fresh(monkeypatch, kosong={date(2026, 10, 14)})
    client = _Client()
    storage = _Storage()

    assert cli._backfill_hari_lampau(client, storage, label="tes") == 1
    assert client.panggilan == ["20261014"]
    assert storage.baris == 963
    assert jalur["ingest"] == [963]  # raw_eod + prices_pit
    assert jalur["segment"] == [963]  # agregat pasar reguler/non-reguler
    assert jalur["pipeline"] == [("backfill tes", False)]  # recompute, tanpa EOD
    assert "ditarik ulang" in capsys.readouterr().out


def test_hari_ini_bukan_urusan_backfill(monkeypatch, beku, jalur):
    """Fase lampau tidak boleh menyerobot pekerjaan fase hari ini."""
    beku(_wib(16, 16, 30))
    monkeypatch.setenv("IDX_HOLIDAYS", "")
    _fresh(monkeypatch, kosong={date(2026, 10, 16)})
    client = _Client()

    assert cli._backfill_hari_lampau(client, _Storage(), label="tes") == 0
    assert client.panggilan == []


def test_jalan_jam_berapa_pun(monkeypatch, beku, jalur):
    """Restart pagi tetap menutup lubang kemarin — fase hari ini tidak."""
    beku(_wib(16, 8, 0))
    monkeypatch.setenv("IDX_HOLIDAYS", "")
    _fresh(monkeypatch, kosong={date(2026, 10, 15)})
    client = _Client()

    assert cli._backfill_hari_lampau(client, _Storage(), label="tes") == 1
    assert client.panggilan == ["20261015"]


def test_bisa_dimatikan_dari_env(monkeypatch, beku, jalur):
    beku(_wib(16, 16, 30))
    monkeypatch.setenv("IDX_HOLIDAYS", "")
    monkeypatch.setattr(cli, "_BACKFILL_HARI", 0)
    _fresh(monkeypatch, kosong={date(2026, 10, 15)})
    client = _Client()

    assert cli._backfill_hari_lampau(client, _Storage(), label="tes") == 0
    assert client.panggilan == []


def test_0_baris_dilaporkan_dan_boleh_dicoba_lagi(monkeypatch, beku, jalur, capsys):
    """0 baris tidak diingat-ingat: 403 sesaat vs libur tak terdaftar tak terbedakan."""
    beku(_wib(16, 16, 30))
    monkeypatch.setenv("IDX_HOLIDAYS", "")
    _fresh(monkeypatch, kosong={date(2026, 10, 15)})
    client = _Client(jumlah=0)

    assert cli._backfill_hari_lampau(client, _Storage(), label="tes") == 0
    assert len(client.panggilan) == 1
    assert jalur["pipeline"] == []  # tidak ada isi -> tidak ada recompute
    assert "0 baris" in capsys.readouterr().err

    cli._backfill_hari_lampau(client, _Storage(), label="tes")
    assert len(client.panggilan) == 2


def test_laporan_lubang_index_hanya_kalau_diminta(monkeypatch, beku, jalur, capsys):
    """Close indeks lampau tidak bisa ditarik — startup melaporkannya sekali."""
    beku(_wib(16, 16, 30))
    monkeypatch.setenv("IDX_HOLIDAYS", "")
    monkeypatch.setattr(cli, "_eod_fresh_for", lambda day: (True, False))

    cli._backfill_hari_lampau(
        object(), object(), label="startup", lapor_lubang_index=True
    )
    out = capsys.readouterr().out
    assert "tanpa close indeks resmi" in out
    assert "2026-10-15" in out

    cli._backfill_hari_lampau(object(), object(), label="ulang EOD sore")
    assert "tanpa close indeks resmi" not in capsys.readouterr().out


# --------------------------------------------------- sambungan ke _eod_catchup


def test_jaring_sore_ikut_menutup_lubang_kemarin(monkeypatch, beku, jalur):
    """``serve`` yang TIDAK pernah di-restart pun menyembuhkan lubang.

    Tick sore berikutnya melihat ke belakang — inilah yang membuat backfill ini
    berguna tanpa menunggu start ulang proses.
    """
    beku(_wib(16, 16, 30))
    monkeypatch.setenv("IDX_HOLIDAYS", "")
    _fresh(monkeypatch, kosong={date(2026, 10, 15)})
    client = _Client()

    assert cli._eod_catchup(client, _Storage(), label="ulang EOD sore") == "lengkap"
    assert client.panggilan == ["20261015"]


def test_startup_menyalakan_laporan(monkeypatch):
    """Jalur startup meminta laporan lubang indeks, jalur sore tidak."""
    dipanggil: dict = {}
    monkeypatch.setattr(
        cli, "_eod_catchup_hari_ini", lambda c, s, *, label: "lengkap"
    )

    def backfill(client, storage, *, label, lapor_lubang_index=False):
        dipanggil["lapor"] = lapor_lubang_index
        return 0

    monkeypatch.setattr(cli, "_backfill_hari_lampau", backfill)

    assert cli._eod_catchup(object(), object(), label="startup") == "lengkap"
    assert dipanggil["lapor"] is False
    cli._eod_catchup(object(), object(), label="startup", lapor_lubang_index=True)
    assert dipanggil["lapor"] is True


def test_cmd_eod_dan_job_full_memakai_jalur_yang_sama(monkeypatch):
    """Manual, close harian, dan backfill menulis lewat ``_simpan_eod_hari``."""
    dicatat: list = []
    monkeypatch.setattr(
        cli, "_simpan_eod_hari", lambda c, s, day: dicatat.append(day) or 963
    )
    monkeypatch.setattr(
        cli, "IDXClient", lambda: type("C", (), {"close": lambda self: None})()
    )
    monkeypatch.setattr(
        cli,
        "get_storage_from_env",
        lambda: type("S", (), {"close": lambda self: None})(),
    )

    class _Args:
        date = "20261014"

    cli.cmd_eod(_Args())
    assert dicatat == [date(2026, 10, 14)]

    dicatat.clear()
    cli._job_eod_full(object(), object())
    assert dicatat == [datetime.now(cli.WIB).date()]


# ------------------------------------------------- close indeks tidak sebelum 16:00


def test_boleh_tulis_close_index_mengikuti_jam():
    assert cli._boleh_tulis_close_index(_wib(16, 8, 45)) is False  # pra-buka
    assert cli._boleh_tulis_close_index(_wib(16, 12, 0)) is False  # Sesi I
    assert cli._boleh_tulis_close_index(_wib(16, 15, 59)) is False
    assert cli._boleh_tulis_close_index(_wib(16, 16, 0)) is True
    assert cli._boleh_tulis_close_index(_wib(16, 17, 40)) is True


def test_job_index_eod_close_menghormati_jam(monkeypatch):
    import scripts.backfill_index_eod_idx as idx_eod

    panggilan: list = []
    monkeypatch.setattr(
        idx_eod,
        "backfill_index_eod_idx",
        lambda dsn: panggilan.append(dsn) or (1, 1),
    )
    monkeypatch.setenv("DATABASE_URL", "postgres://palsu")
    monkeypatch.setattr(cli, "_is_market_hours", lambda: True)

    monkeypatch.setattr(cli, "_boleh_tulis_close_index", lambda: False)
    cli._job_index_eod_close()
    assert panggilan == []

    monkeypatch.setattr(cli, "_boleh_tulis_close_index", lambda: True)
    cli._job_index_eod_close()
    assert len(panggilan) == 1
