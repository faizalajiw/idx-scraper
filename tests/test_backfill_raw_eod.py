# tests/test_backfill_raw_eod.py
from datetime import date

from scripts.backfill_raw_eod import ambil_hari, klasifikasi, tanggal_perlu


def test_200_dengan_baris_berarti_data():
    assert klasifikasi([object()], 200) == "data"

def test_200_tanpa_baris_berarti_libur():
    assert klasifikasi([], 200) == "libur"

def test_503_berarti_luar_jendela_bukan_libur():
    assert klasifikasi([], 503) == "luar-jendela"

def test_403_berarti_diblokir():
    assert klasifikasi([], 403) == "diblokir"

def test_429_berarti_diblokir():
    assert klasifikasi([], 429) == "diblokir"

def test_status_tak_dikenal_atau_exception_berarti_gagal():
    assert klasifikasi([], None) == "gagal"
    assert klasifikasi([], "EXC:TimeoutError") == "gagal"

def test_tanggal_perlu_melewati_yang_sudah_ada():
    days = [date(2026, 10, 5), date(2026, 10, 6)]
    assert tanggal_perlu(days, {date(2026, 10, 5)}, ulang=False) == [date(2026, 10, 6)]

def test_tanggal_perlu_ulang_mengambil_semua():
    days = [date(2026, 10, 5), date(2026, 10, 6)]
    assert tanggal_perlu(days, {date(2026, 10, 5), date(2026, 10, 6)}, ulang=True) == days


class _Sesi:
    def __init__(self, status=None):
        self.last_status = status


class _Klien:
    """Stub IDXClient tanpa jaringan: tiap respons = (baris, status transport).

    Status hanya ditulis saat respons benar-benar diterima, meniru transport asli
    yang memperbarui ``last_status`` dari kode HTTP. Exception dilempar mentah
    (status lama tak tersentuh).
    """

    def __init__(self, respon, status_awal=None):
        self.session = _Sesi(status_awal)
        self._respon = list(respon)
        self.panggilan = 0

    def fetch_stock_summary(self, tanggal: str) -> list:
        self.panggilan += 1
        item = self._respon.pop(0)
        if isinstance(item, Exception):
            raise item
        baris, status = item
        self.session.last_status = status
        return baris


class _KlienBasi:
    """Meniru ``client._get_json``: exception transport ditelan → ``[]``.

    Kuncinya: ``last_status`` TIDAK diperbarui, jadi nilainya masih status
    permintaan sebelumnya (di sini sengaja disetel 200 seperti hari sukses lalu).
    """

    def __init__(self, status_basi=200):
        self.session = _Sesi(status_basi)
        self.panggilan = 0

    def fetch_stock_summary(self, tanggal: str) -> list:
        self.panggilan += 1
        return []


def test_transport_gagal_tidak_dibaca_libur():
    """Finding A: kegagalan transport tidak boleh lolos sebagai hari libur."""
    # (a) transport melempar — status basi 200 tak boleh menang.
    klien_lempar = _Klien([RuntimeError("connection reset")] * 3, status_awal=200)
    hasil = ambil_hari(klien_lempar, "20261012", tidur=lambda _s: None)
    assert klien_lempar.panggilan == 3
    assert klasifikasi(*hasil) == "gagal"

    # (b) akar Finding A: exception ditelan → [] tetapi last_status tetap 200 basi.
    klien_basi = _KlienBasi(status_basi=200)
    hasil = ambil_hari(klien_basi, "20261012", tidur=lambda _s: None)
    assert klien_basi.panggilan == 3
    assert klasifikasi(*hasil) == "gagal"


def test_libur_asli_berhenti_di_percobaan_pertama():
    klien = _Klien([([], 200)])
    assert ambil_hari(klien, "20261012", tidur=lambda _s: None) == ([], 200)
    assert klien.panggilan == 1


def test_503_sesaat_diulang_lalu_baris_terpakai():
    """Finding B: 503 sesaat di dalam jendela retensi harus diulang, bukan diputus."""
    baris = [object()]
    klien = _Klien([([], 503), (baris, 200)])
    assert ambil_hari(klien, "20261012", tidur=lambda _s: None) == (baris, 200)
    assert klien.panggilan == 2


def test_503_beruntun_habis_percobaan_berarti_luar_jendela():
    klien = _Klien([([], 503)] * 3)
    hasil = ambil_hari(klien, "20261012", tidur=lambda _s: None)
    assert klien.panggilan == 3
    assert klasifikasi(*hasil) == "luar-jendela"
