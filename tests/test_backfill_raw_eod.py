# tests/test_backfill_raw_eod.py
from datetime import date

from scripts.backfill_raw_eod import klasifikasi, tanggal_perlu


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
