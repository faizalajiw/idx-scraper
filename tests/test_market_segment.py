"""Unit tests: agregat pasar reguler vs non-reguler (market_segment) — pure."""

from __future__ import annotations

from types import SimpleNamespace

from idx_scraper.market_segment import _int, _num, aggregate_day


def _row(code, vol, val, freq, nr_vol=None, nr_val=None, nr_freq=None):
    return SimpleNamespace(
        code=code,
        volume=vol,
        value=val,
        frequency=freq,
        non_regular_volume=nr_vol,
        non_regular_value=nr_val,
        non_regular_frequency=nr_freq,
    )


def test_totals_split_regular_and_non_regular():
    rows = [
        _row("AADI", 7_807_300, 91_998_137_500, 1000, 496, 5_283_950, 10),
        _row("ADRO", 34_216_500, 86_681_701_000, 900, 167, 422_860, 3),
        _row("AKKU", 52_162_700, 2_763_016_500, 800),  # tanpa non-reguler
    ]
    agg = aggregate_day(rows)
    assert agg["regular_volume"] == 94_186_500
    assert agg["regular_value"] == 181_442_855_000
    assert agg["regular_frequency"] == 2700
    assert agg["non_regular_volume"] == 663
    assert agg["non_regular_value"] == 5_706_810
    assert agg["non_regular_frequency"] == 13
    assert agg["total_volume"] == 94_187_163
    assert agg["total_value"] == 181_448_561_810
    assert agg["stock_count"] == 3


def test_missing_non_regular_stays_none_not_zero():
    rows = [_row("BBCA", 1_000, 50_000_000, 100)]
    agg = aggregate_day(rows)
    assert agg["non_regular_volume"] is None
    assert agg["non_regular_value"] is None
    assert agg["total_volume"] == 1_000
    assert agg["total_value"] == 50_000_000


def test_no_rows_at_all():
    agg = aggregate_day([])
    assert agg["regular_volume"] is None
    assert agg["total_volume"] is None
    assert agg["stock_count"] == 0


def test_stock_count_is_unique_codes():
    rows = [_row("BBCA", 1, 1, 1), _row("BBCA", 2, 2, 2), _row("BBRI", 3, 3, 3)]
    assert aggregate_day(rows)["stock_count"] == 2


def test_string_numbers_are_coerced():
    rows = [_row("X", "1,234,567", "9,876,543.21", 42, "100", "1,000.50", 2)]
    agg = aggregate_day(rows)
    assert agg["regular_volume"] == 1_234_567
    assert agg["regular_value"] == 9_876_543.21
    assert agg["non_regular_volume"] == 100
    assert agg["non_regular_value"] == 1_000.5
    assert agg["total_volume"] == 1_234_667


def test_num_int_helpers():
    assert _num(1.5) == 1.5
    assert _num("2,000") == 2000.0
    assert _num("") is None
    assert _num(None) is None
    assert _num("bukan angka") is None
    assert _int("3.7") == 3
    assert _int(None) is None
