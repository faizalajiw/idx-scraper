"""Tes pemeriksa jumlah baris per sesi & lubang sesi (murni, tanpa DB).

Menutup dua bentuk kerusakan yang tidak terlihat oleh ``EodHealth`` (tanggal
terbaru) maupun ``FieldCoverage`` (kolom foreign): sesi yang barisnya cuma
separuh, dan hari bursa yang hilang di tengah rentang data.
"""

from datetime import date, timedelta

from idx_scraper.health import (
    count_sessions_between,
    evaluate_row_counts,
    evaluate_session_gaps,
    find_missing_sessions,
    format_no_data_message,
    format_row_count_message,
    format_session_gaps_message,
)


def _sesi(mulai: date, jumlah: int, baris: int = 500) -> list[tuple[date, int]]:
    """Rentang sesi harian (termasuk akhir pekan — fungsi ini tidak peduli kalender)."""
    return [(mulai + timedelta(days=i), baris) for i in range(jumlah)]


# --------------------------------------------------------------------------- #
# evaluate_row_counts
# --------------------------------------------------------------------------- #


def test_median_dan_sesi_normal_tidak_kena_alarm() -> None:
    rows = _sesi(date(2026, 10, 1), 10, baris=500)

    hasil = evaluate_row_counts(rows, sessions=30)

    assert hasil.ok
    assert hasil.median_rows == 500
    assert hasil.worst_pct == 100.0


def test_sesi_separuh_ditandai() -> None:
    rows = [*_sesi(date(2026, 10, 1), 9, baris=500), (date(2026, 10, 10), 120)]

    hasil = evaluate_row_counts(rows, sessions=30)

    assert not hasil.ok
    assert len(hasil.troublemakers) == 1
    assert hasil.troublemakers[0].startswith("2026-10-10 (120/500 baris, 24.0% median)")
    assert hasil.worst_pct == 24.0


def test_tepat_di_ambang_belum_dianggap_bermasalah() -> None:
    rows = [*_sesi(date(2026, 10, 1), 9, baris=500), (date(2026, 10, 10), 250)]

    hasil = evaluate_row_counts(rows, sessions=30, threshold_pct=50.0)

    assert hasil.ok  # 250 == 50% median: tepat di ambang, bukan di bawahnya


def test_sesi_lama_di_luar_jendela_tidak_ikut_menghidupkan_alarm() -> None:
    # Sesi pertama kecil (universe tumbuh), jendela hanya 3 sesi terakhir.
    rows = [(date(2026, 9, 1), 20), *_sesi(date(2026, 10, 1), 3, baris=500)]

    hasil = evaluate_row_counts(rows, sessions=3)

    assert hasil.ok
    assert hasil.checked == 3


def test_urutan_masukan_tidak_menentukan_sesi_yang_dinilai() -> None:
    # Masukan acak: jendela tetap 2 sesi TERAKHIR menurut tanggal.
    rows = [(date(2026, 10, 3), 500), (date(2026, 10, 1), 100), (date(2026, 10, 2), 500)]

    hasil = evaluate_row_counts(rows, sessions=2)

    assert hasil.checked == 2
    assert hasil.ok  # 2026-10-01 (kecil) di luar jendela, bukan dinilai


def test_daftar_kosong_aman() -> None:
    hasil = evaluate_row_counts([], sessions=30)

    assert hasil.checked == 0
    assert hasil.ok
    assert hasil.median_rows == 0


def test_sesi_yang_masih_ditulis_dilewati() -> None:
    # Sesi hari ini baru terisi 120 baris (ingest jalan) -> jangan alarm sekarang.
    rows = [*_sesi(date(2026, 10, 1), 9, baris=500), (date(2026, 10, 10), 120)]

    hasil = evaluate_row_counts(rows, sessions=30, in_progress=date(2026, 10, 10))

    assert hasil.ok
    assert hasil.checked == 9
    # Tanpa sesi itu, hari yang sama juga tidak muncul di daftar bermasalah —
    # jadi alarm baru berbunyi setelah sesi itu tak lagi "in progress".
    assert evaluate_row_counts(rows, sessions=30, in_progress=date(2026, 10, 11)).troublemakers


def test_median_nol_tidak_melempar() -> None:
    rows = [(date(2026, 10, 1), 0), (date(2026, 10, 2), 0)]

    hasil = evaluate_row_counts(rows, sessions=30)

    assert hasil.median_rows == 0
    assert hasil.ok  # 0 < 0*0.5 = 0 salah; tanpa data tak ada yang bisa dinilai


# --------------------------------------------------------------------------- #
# find_missing_sessions / evaluate_session_gaps
# --------------------------------------------------------------------------- #


def test_akhir_pekan_tidak_dihitung_lubang() -> None:
    # Rabu & Jumat (Kamis libur bursa, Sabtu/Minggu akhir pekan).
    dates = [date(2026, 10, 14), date(2026, 10, 16)]

    assert find_missing_sessions(dates, holidays={date(2026, 10, 15)}) == []


def test_hari_kerja_tanpa_sesi_dan_tanpa_libur_ditandai() -> None:
    dates = [date(2026, 10, 14), date(2026, 10, 16)]

    assert find_missing_sessions(dates, holidays=set()) == [date(2026, 10, 15)]


def test_lubang_berhari_hari_dan_libur_nasional() -> None:
    # 5 Okt (Sen) → 9 Okt (Jum): 6,7,8 Okt kerja, hilang.
    dates = [date(2026, 10, 5), date(2026, 10, 9)]

    assert find_missing_sessions(dates, holidays=set()) == [
        date(2026, 10, 6),
        date(2026, 10, 7),
        date(2026, 10, 8),
    ]
    # Kalau 7 Okt masuk daftar libur bursa, tinggal dua lubang.
    assert find_missing_sessions(dates, holidays={date(2026, 10, 7)}) == [
        date(2026, 10, 6),
        date(2026, 10, 8),
    ]


def test_tanggal_duplikat_dan_urut_tidak_berpengaruh() -> None:
    dates = [date(2026, 10, 16), date(2026, 10, 14), date(2026, 10, 16)]

    hasil = evaluate_session_gaps(dates, holidays=set())

    assert hasil.sessions == 2
    assert hasil.first == date(2026, 10, 14)
    assert hasil.last == date(2026, 10, 16)
    assert hasil.missing == [date(2026, 10, 15)]


def test_kurang_dari_dua_sesi_tidak_menghasilkan_lubang() -> None:
    hasil = evaluate_session_gaps([date(2026, 10, 16)], holidays=set())

    assert hasil.sessions == 1
    assert hasil.first == hasil.last == date(2026, 10, 16)
    assert hasil.ok


def test_sesi_yang_masih_ditulis_bukan_lubang() -> None:
    # IHSG sudah punya 7 Okt (bursa buka) tapi raw_eod belum (ingest jalan).
    eod = [date(2026, 10, 5), date(2026, 10, 9)]
    bursa = [date(2026, 10, 5), date(2026, 10, 7), date(2026, 10, 9)]

    assert evaluate_session_gaps(eod, bursa).missing == [date(2026, 10, 7)]
    hasil = evaluate_session_gaps(eod, bursa, in_progress=date(2026, 10, 7))

    assert hasil.ok
    assert hasil.missing == []
    assert hasil.sessions == 2  # sesi tetap dihitung; hanya keluhan yang ditahan


# --------------------------------------------------------------------------- #
# formatter pesan alarm (dipakai idx health + job health_eod)
# --------------------------------------------------------------------------- #


def test_pesan_sesi_separuh_sebut_angka_dan_cara_memperbaiki() -> None:
    c = evaluate_row_counts([*_sesi(date(2026, 10, 1), 9, baris=500), (date(2026, 10, 10), 120)])

    pesan = format_row_count_message(c)

    assert "Sesi EOD cuma separuh" in pesan
    assert "1 dari 10 sesi terakhir" in pesan
    assert "2026-10-10 (120/500 baris, 24.0% median)" in pesan
    assert "--start-date 2026-10-10 --end-date 2026-10-10 --ulang" in pesan
    assert "backfill_raw_eod" in pesan


def test_pesan_sesi_separuh_wajib_ulang() -> None:
    """Sesi separuh = tanggal yang SUDAH ada: tanpa `--ulang` backfill melewatinya.

    Perintah perbaikan yang tidak memperbaiki apa pun adalah alarm yang berbunyi
    lagi besok — persis mode "alarm diabaikan" yang mau dicegah.
    """
    c = evaluate_row_counts([*_sesi(date(2026, 10, 1), 9, baris=500), (date(2026, 10, 10), 120)])

    pesan = format_row_count_message(c)

    assert "--ulang" in pesan
    assert "melewati tanggal yang sudah ada" in pesan


def test_pesan_sesi_separuh_daftar_dipotong_dengan_sisa() -> None:
    # 6 sesi separuh: 5 terbaru ditampilkan, sisanya diringkas.
    rows = _sesi(date(2026, 10, 1), 10, baris=500) + _sesi(date(2026, 10, 11), 6, baris=10)

    pesan = format_row_count_message(evaluate_row_counts(rows))

    assert "• 2026-10-15" in pesan
    assert "• 2026-10-16" not in pesan  # sesi ke-6 dipotong
    assert "(+1 lagi)" in pesan


def test_pesan_lubang_sebut_rentang_dan_perintah_penambal() -> None:
    g = evaluate_session_gaps(
        [date(2026, 10, 5), date(2026, 10, 9)],
        [date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 9)],
    )

    pesan = format_session_gaps_message(g)

    assert "Ada hari bursa tanpa sesi" in pesan
    assert "2 hari tanpa baris" in pesan
    assert "2026-10-06" in pesan and "2026-10-07" in pesan
    assert "check_eod_gaps" in pesan
    assert "Catatan" not in pesan  # basis kalender-bursa: tak perlu catatan


def test_pesan_lubang_sebut_catatan_kalau_basis_lemah() -> None:
    pendek = evaluate_session_gaps([date(2026, 10, 5), date(2026, 10, 9)], [date(2026, 10, 9)])
    hari_kerja = evaluate_session_gaps(
        [date(2026, 10, 5), date(2026, 10, 9)], bursa_dates=[], holidays=set()
    )

    assert pendek.basis == "kalender-bursa-pendek"
    assert "cakupan acuan" in format_session_gaps_message(pendek)
    assert hari_kerja.basis == "hari-kerja"
    assert "hari kerja saja" in format_session_gaps_message(hari_kerja)


def test_daftar_kosong_aman_session_gaps() -> None:
    hasil = evaluate_session_gaps([])

    assert (hasil.first, hasil.last, hasil.sessions) == (None, None, 0)
    assert hasil.ok


def test_pesan_tak_ada_yang_diperiksa_sebut_penjaga_dan_penyebabnya() -> None:
    """`checked == 0` bukan hijau: penjaga itu harus bisa bilang kenapa ia bisu."""
    pesan = format_no_data_message("Jumlah baris per sesi")

    assert "tidak memeriksa apa pun" in pesan
    assert "Jumlah baris per sesi" in pesan
    assert "DATABASE_URL" in pesan
    assert "raw_eod" in pesan


# --------------------------------------------------------------------------- #
# dasar penilaian: kalender bursa dari data IHSG
# --------------------------------------------------------------------------- #


def test_kalender_bursa_mengecualikan_libur_tanpa_daftar_manual() -> None:
    # Sesi 5 & 9 Okt (Sen & Jum). IHSG juga hanya punya dua hari itu → 6-8 Okt
    # (kerja) bukan lubang: bursa memang tutup, tanpa perlu IDX_HOLIDAYS.
    eod = [date(2026, 10, 5), date(2026, 10, 9)]
    bursa = [date(2026, 10, 5), date(2026, 10, 9)]

    hasil = evaluate_session_gaps(eod, bursa)

    assert hasil.ok
    assert hasil.basis == "kalender-bursa"


def test_kalender_bursa_mendeteksi_lubang_asli() -> None:
    # IHSG punya 7 Okt (bursa buka), raw_eod tidak → itu lubang sungguhan.
    eod = [date(2026, 10, 5), date(2026, 10, 9)]
    bursa = [date(2026, 10, 5), date(2026, 10, 7), date(2026, 10, 9)]

    assert find_missing_sessions(eod, bursa) == [date(2026, 10, 7)]


def test_kalender_bursa_di_luar_rentang_eod_tidak_dihitung() -> None:
    # Acuan IHSG lebih tua dari sesi raw_eod pertama: jangan tuduh hari di luar
    # domain yang seharusnya dicakup raw_eod.
    eod = [date(2026, 10, 14), date(2026, 10, 16)]
    bursa = [date(2026, 8, 3), date(2026, 10, 14), date(2026, 10, 16)]

    assert find_missing_sessions(eod, bursa) == []


def test_baris_yahoo_yang_bocor_ke_akhir_pekan_tidak_menghidupkan_alarm() -> None:
    # Yahoo punya baris kolong (Sabtu 2026-10-10 di index_summary_daily nyata).
    # Bukan lubang: bursa tutup di akhir pekan, apa pun isi acuannya.
    eod = [date(2026, 10, 9), date(2026, 10, 12)]
    bursa = [*eod, date(2026, 10, 10)]

    assert find_missing_sessions(eod, bursa) == []


def test_tanpa_acuan_basis_jatuh_ke_hari_kerja() -> None:
    hasil = evaluate_session_gaps(
        [date(2026, 10, 14), date(2026, 10, 16)], bursa_dates=[], holidays=set()
    )

    assert hasil.basis == "hari-kerja"
    assert hasil.missing == [date(2026, 10, 15)]


def test_acuan_lebih_pendek_dari_rentang_ditandai_basis_pendek() -> None:
    # Acuan IHSG cuma menutup sebagian rentang raw_eod: lubang di luar cakupan
    # acuan tak terlihat, jadi basisnya jangan diklaim penuh.
    eod = [date(2024, 10, 15), date(2025, 6, 2), date(2026, 10, 16)]
    bursa = [date(2026, 1, 2), date(2026, 10, 16)]

    hasil = evaluate_session_gaps(eod, bursa)

    assert hasil.basis == "kalender-bursa-pendek"
    # Bagian yang masih terlihat acuan tetap dilaporkan: kalender bilang bursa
    # buka 2026-01-02, raw_eod tidak punya barisnya.
    assert hasil.missing == [date(2026, 1, 2)]


def test_acuan_menutup_rentang_tetap_basis_penuh() -> None:
    eod = [date(2026, 10, 14), date(2026, 10, 16)]
    bursa = [date(2026, 1, 2), date(2026, 10, 14), date(2026, 10, 16)]

    assert evaluate_session_gaps(eod, bursa).basis == "kalender-bursa"


# --------------------------------------------------------------------------- #
# count_sessions_between
# --------------------------------------------------------------------------- #


def test_hitung_sesi_tertinggal_dengan_kalender() -> None:
    bursa = [date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 16)]

    # Data terakhir 14 Okt, expected 16 Okt -> satu sesi tertinggal (15 Okt libur,
    # tidak ada di kalender; 16 Okt hari kerja tanpa baris).
    assert count_sessions_between(bursa, date(2026, 10, 14), date(2026, 10, 16)) == 1
    # Data terakhir 16 Okt = expected -> nol: cuma sesi hari ini yang belum masuk.
    assert count_sessions_between(bursa, date(2026, 10, 16), date(2026, 10, 16)) == 0
    # Belum ada data sama sekali -> seluruh hari bursa sampai expected.
    assert count_sessions_between(bursa, None, date(2026, 10, 16)) == 4


def test_hitung_sesi_tertinggal_tanpa_kalender_pakai_hari_kerja() -> None:
    # 15 Okt Kamis: dihitung hari kerja (daftar libur kosong), 17-18 Okt akhir pekan.
    assert count_sessions_between([], date(2026, 10, 14), date(2026, 10, 19), set()) == 3
    # Kalau 15 Okt & 16 Okt terdaftar libur, tinggal satu hari kerja.
    assert (
        count_sessions_between(
            [],
            date(2026, 10, 14),
            date(2026, 10, 19),
            {
                date(2026, 10, 15),
                date(2026, 10, 16),
            },
        )
        == 1
    )


def test_hitung_sesi_tanpa_kalender_dan_tanpa_titik_awal_mengembalikan_nol() -> None:
    # Tanpa acuan IHSG dan tanpa data terakhir, rentangnya tidak diketahui —
    # jangan mengarang angka.
    assert count_sessions_between([], None, date(2026, 10, 19), set()) == 0


# --------------------------------------------------------------------------- #
# wrapper DB: degradasi jujur tanpa DATABASE_URL (tidak menyentuh jaringan)
# --------------------------------------------------------------------------- #


def _tanpa_dsn(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)


def test_check_row_counts_tanpa_dsn_tidak_melempar(monkeypatch) -> None:
    from idx_scraper.health import check_row_counts

    _tanpa_dsn(monkeypatch)
    hasil = check_row_counts()

    assert hasil.checked == 0
    assert not hasil.troublemakers


def test_check_session_gaps_tanpa_dsn_tidak_melempar(monkeypatch) -> None:
    from idx_scraper.health import check_session_gaps

    _tanpa_dsn(monkeypatch)
    hasil = check_session_gaps()

    assert (hasil.first, hasil.last, hasil.sessions) == (None, None, 0)
