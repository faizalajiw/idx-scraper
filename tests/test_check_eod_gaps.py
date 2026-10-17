# tests/test_check_eod_gaps.py
"""Uji murni untuk keputusan ``check_eod_gaps`` — tanpa DB maupun jaringan.

Jalur ambil-data skrip itu memakai ``ambil_hari`` + ``klasifikasi`` dari
``scripts.backfill_raw_eod`` (satu-satunya jalur yang punya tes penutup di
``tests/test_backfill_raw_eod.py``). Yang diuji di sini adalah logika murni yang
tersisa: pemetaan ember per hari setelah penyisipan, potongan jendela retensi,
keputusan exit code, dan titik ulang untuk run berikutnya.
"""

from datetime import date, timedelta

from scripts.check_eod_gaps import dalam_jendela, kode_keluar, status_hari, titik_lanjut

# --- status_hari: klasifikasi hasil satu hari setelah penyisipan ---


def test_semua_baris_tercatat_berarti_terisi():
    # 3 baris diterima IDX: 2 masuk raw_eod + 1 dikarantina = 3 tertangani.
    assert status_hari([1, 2, 3], 200, tertangani=3) == "terisi"


def test_baris_tak_semua_tercatat_berarti_sebagian():
    """Interupsi di tengah sesi tidak boleh lolos sebagai sukses (Important 3)."""
    assert status_hari([1, 2, 3], 200, tertangani=2) == "sebagian"


def test_200_tanpa_baris_berarti_libur():
    assert status_hari([], 200, tertangani=0) == "libur"


def test_503_berarti_luar_jendela_bukan_lubang():
    """I1: 503 = di luar retensi IDX (±2 tahun) — bukan lubang yang bisa ditambal."""
    assert status_hari([], 503, tertangani=0) == "luar-jendela"


def test_403_dan_429_berarti_diblokir():
    """403/429 = Cloudflare menolak; jangan pernah dibaca libur."""
    assert status_hari([], 403, tertangani=0) == "diblokir"
    assert status_hari([], 429, tertangani=0) == "diblokir"


def test_transport_gagal_atau_status_asing_berarti_gagal():
    """Status selain 200/503/403/429 tak boleh jadi libur — cacat yang Task 1 tutup."""
    assert status_hari([], None, tertangani=0) == "gagal"
    assert status_hari([], "EXC:TimeoutError", tertangani=0) == "gagal"
    assert status_hari([], 500, tertangani=0) == "gagal"


# --- kode_keluar: exit code jujur untuk scheduler ---


def test_run_bersih_keluar_nol():
    assert kode_keluar(berhenti_menolak=False, gagal=0, sebagian=0) == 0


def test_libur_dan_luar_jendela_tidak_pernah_keluar_satu():
    """I1: hari tak bisa ditambal (libur / >720 hari) bukan kegagalan scheduler."""
    assert status_hari([], 200, tertangani=0) == "libur"
    assert status_hari([], 503, tertangani=0) == "luar-jendela"
    # Keduanya tidak masuk hitungan kegagalan -> run tetap dianggap bersih.
    assert kode_keluar(berhenti_menolak=False, gagal=0, sebagian=0) == 0


def test_berhenti_karena_403_429_beruntun_keluar_gagal():
    assert kode_keluar(berhenti_menolak=True, gagal=0, sebagian=0) == 1


def test_satu_sesi_gagal_sudah_cukup_gagal():
    assert kode_keluar(berhenti_menolak=False, gagal=1, sebagian=0) == 1


def test_sesi_tersimpan_sebagian_keluar_gagal():
    assert kode_keluar(berhenti_menolak=False, gagal=0, sebagian=2) == 1


# --- dalam_jendela: potongan jendela retensi IDX (±720 hari) ---

_HARI_INI = date(2026, 10, 17)


def test_tanggal_721_hari_lalu_di_luar_jendela():
    assert dalam_jendela(_HARI_INI - timedelta(days=721), _HARI_INI) is False


def test_tanggal_400_hari_lalu_masih_di_dalam_jendela():
    assert dalam_jendela(_HARI_INI - timedelta(days=400), _HARI_INI) is True


def test_batas_720_hari_masih_di_dalam_jendela():
    """Batas inklusif: 720 hari tepat masih boleh dicoba."""
    assert dalam_jendela(_HARI_INI - timedelta(days=720), _HARI_INI) is True


def test_hari_ini_di_dalam_jendela():
    assert dalam_jendela(_HARI_INI, _HARI_INI) is True


# --- titik_lanjut: tanggal ulang untuk run berikutnya ---


def test_titik_lanjut_ambil_tanggal_paling_awal():
    lubang = [(date(2026, 3, 19), 403), (date(2026, 5, 1), 503)]
    assert titik_lanjut(lubang, [date(2026, 6, 1)]) == date(2026, 3, 19)


def test_titik_lanjut_none_bila_semua_berhasil():
    assert titik_lanjut([], []) is None
