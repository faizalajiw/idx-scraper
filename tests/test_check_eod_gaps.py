# tests/test_check_eod_gaps.py
"""Uji murni untuk keputusan ``check_eod_gaps`` — tanpa DB maupun jaringan.

Jalur ambil-data skrip itu memakai ``ambil_hari`` dari ``scripts.backfill_raw_eod``
(satu-satunya jalur yang punya tes penutup di ``tests/test_backfill_raw_eod.py``).
Yang diuji di sini adalah logika murni yang tersisa: klasifikasi hasil satu hari
setelah penyisipan dan keputusan exit code.
"""

from datetime import date

from scripts.check_eod_gaps import kode_keluar, status_hari, titik_lanjut

# --- status_hari: klasifikasi hasil satu hari setelah penyisipan ---


def test_semua_baris_tercatat_berarti_terisi():
    # 3 baris diterima IDX: 2 masuk raw_eod + 1 dikarantina = 3 tertangani.
    assert status_hari([1, 2, 3], 200, tertangani=3) == "terisi"


def test_baris_tak_semua_tercatat_berarti_sebagian():
    """Interupsi di tengah sesi tidak boleh lolos sebagai sukses (Important 3)."""
    assert status_hari([1, 2, 3], 200, tertangani=2) == "sebagian"


def test_200_tanpa_baris_berarti_libur():
    assert status_hari([], 200, tertangani=0) == "libur"


def test_transport_gagal_tidak_dibaca_libur():
    """Status selain 200 tak boleh jadi libur — cacat yang Task 1 tutup (Critical)."""
    assert status_hari([], None, tertangani=0) == "lubang"
    assert status_hari([], "EXC:TimeoutError", tertangani=0) == "lubang"
    assert status_hari([], 500, tertangani=0) == "lubang"


# --- kode_keluar: exit code jujur untuk scheduler ---


def test_run_bersih_keluar_nol():
    assert kode_keluar(berhenti_menolak=False, lubang=0, sebagian=0) == 0


def test_libur_bursa_saja_bukan_kegagalan():
    """48 hari kerja tanpa sesi memang libur bursa; exit tetap 0."""
    assert kode_keluar(berhenti_menolak=False, lubang=0, sebagian=0) == 0


def test_berhenti_karena_403_429_beruntun_keluar_gagal():
    assert kode_keluar(berhenti_menolak=True, lubang=1, sebagian=0) == 1


def test_satu_hari_lubang_sudah_cukup_gagal():
    assert kode_keluar(berhenti_menolak=False, lubang=1, sebagian=0) == 1


def test_sesi_tersimpan_sebagian_keluar_gagal():
    assert kode_keluar(berhenti_menolak=False, lubang=0, sebagian=2) == 1


# --- titik_lanjut: tanggal ulang untuk run berikutnya ---


def test_titik_lanjut_ambil_tanggal_paling_awal():
    lubang = [(date(2026, 3, 19), 403), (date(2026, 5, 1), 503)]
    assert titik_lanjut(lubang, [date(2026, 6, 1)]) == date(2026, 3, 19)


def test_titik_lanjut_none_bila_semua_berhasil():
    assert titik_lanjut([], []) is None
