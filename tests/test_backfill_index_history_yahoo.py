# tests/test_backfill_index_history_yahoo.py
"""Uji murni penyaringan bar Yahoo — tanpa jaringan maupun DB.

``_baris_valid`` menerima iterable ``(tanggal, mapping_ohlc)`` sehingga bisa
diuji dengan data in-memory; skripnya sendiri menyuapnya dari DataFrame Yahoo.
"""

from datetime import date, datetime, timedelta, timezone

from scripts.backfill_index_history_yahoo import _baris_valid

WIB = timezone(timedelta(hours=7))


def _bar(o, h, lo, c):
    return {"Open": o, "High": h, "Low": lo, "Close": c}


def test_bar_lengkap_diterima():
    bars, dilewati = _baris_valid([(date(2024, 11, 1), _bar(7000.0, 7100.0, 6950.0, 7050.0))])
    assert list(bars) == [date(2024, 11, 1)]
    assert bars[date(2024, 11, 1)] == {
        "open": 7000.0,
        "high": 7100.0,
        "low": 6950.0,
        "close": 7050.0,
    }
    assert dilewati == 0


def test_nan_pada_open_membuat_baris_dilewati():
    """Important 4(c): NaN pada OHL tak boleh lolos ke kolom numeric."""
    bars, dilewati = _baris_valid(
        [
            (date(2024, 11, 1), _bar(float("nan"), 7100.0, 6950.0, 7050.0)),
            (date(2024, 11, 4), _bar(7000.0, 7100.0, 6950.0, 7050.0)),
        ]
    )
    assert list(bars) == [date(2024, 11, 4)]
    assert dilewati == 1


def test_nan_pada_low_dihitung_sebagai_dilewati():
    bars, dilewati = _baris_valid([(date(2024, 11, 1), _bar(7000.0, 7100.0, float("nan"), 7050.0))])
    assert bars == {}
    assert dilewati == 1


def test_kolom_ohl_yang_hilang_membuat_baris_dilewati():
    bars, dilewati = _baris_valid([(date(2024, 11, 1), {"Open": 7000.0, "Close": 7050.0})])
    assert bars == {}
    assert dilewati == 1


def test_nilai_kosong_atau_bukan_angka_dilewati():
    bars, dilewati = _baris_valid([(date(2024, 11, 1), _bar(None, "x", None, None))])
    assert bars == {}
    assert dilewati == 1


def test_close_nol_atau_negatif_dilewati():
    bars, dilewati = _baris_valid(
        [
            (date(2024, 11, 1), _bar(7000.0, 7100.0, 6950.0, 0.0)),
            (date(2024, 11, 4), _bar(7000.0, 7100.0, 6950.0, -1.0)),
        ]
    )
    assert bars == {}
    assert dilewati == 2


def test_indeks_datetime_dipetakan_ke_tanggal():
    """Kalau index bukan Timestamp, tetap harus jadi ``date``."""
    bars, _ = _baris_valid(
        [(datetime(2024, 11, 1, 16, 0, tzinfo=WIB), _bar(7000.0, 7100.0, 6950.0, 7050.0))]
    )
    assert date(2024, 11, 1) in bars
