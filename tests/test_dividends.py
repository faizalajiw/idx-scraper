"""Tests for the dividend arithmetic (pure helpers, no database)."""

from __future__ import annotations

from datetime import date, timedelta

from idx_scraper.api.dividends import (
    BY_YEAR_LOOKBACK,
    _d,
    _f,
    summarize_dividends,
    yield_pct,
)


def test_f_coerces_decimals_and_strings() -> None:
    assert _f("12.5") == 12.5
    assert _f(7) == 7.0
    assert _f(None) is None
    assert _f("nonsense") is None


def test_d_isoformats_dates_and_passes_through_none() -> None:
    assert _d(date(2026, 9, 24)) == "2026-09-24"
    assert _d(None) is None


def test_yield_is_cash_over_price_as_percent() -> None:
    assert yield_pct(150.0, 3000.0) == 5.0
    assert yield_pct(1.0, 3.0) == 33.33


def test_yield_needs_both_a_payout_and_a_usable_price() -> None:
    assert yield_pct(None, 3000.0) is None
    assert yield_pct(0.0, 3000.0) is None
    assert yield_pct(150.0, None) is None
    assert yield_pct(150.0, 0.0) is None
    assert yield_pct(150.0, -10.0) is None


def test_summarize_windows_the_trailing_year_from_as_of() -> None:
    payments = [
        ("2024-05-10", 100.0),  # inside the trailing 12 months
        ("2023-05-10", 80.0),  # just outside (cutoff is 2023-07-01)
        ("2022-05-10", 60.0),
    ]
    out = summarize_dividends(payments, "2024-06-30")

    assert out["ttm_cash"] == 100.0
    assert out["ttm_events"] == 1
    assert out["total_cash"] == 240.0
    assert out["total_events"] == 3


def test_summarize_reports_first_and_last_ex_date() -> None:
    payments = [("2024-01-05", 10.0), ("2020-03-01", 5.0), ("2022-07-15", 7.0)]
    out = summarize_dividends(payments, "2024-06-30")

    assert out["last_ex_date"] == "2024-01-05"
    assert out["first_ex_date"] == "2020-03-01"


def test_summarize_buckets_by_calendar_year_newest_first() -> None:
    payments = [("2023-06-01", 10.0), ("2023-01-01", 5.0), ("2024-06-01", 20.0)]
    out = summarize_dividends(payments, "2024-12-31")

    assert out["annual"] == [
        {"year": 2024, "cash": 20.0, "events": 1},
        {"year": 2023, "cash": 15.0, "events": 2},
    ]


def test_summarize_growth_compares_the_two_newest_years() -> None:
    payments = [("2024-05-01", 120.0), ("2023-05-01", 100.0), ("2022-05-01", 10.0)]
    out = summarize_dividends(payments, "2024-12-31")

    assert out["growth_pct"] == 20.0


def test_summarize_growth_is_none_with_a_single_year() -> None:
    out = summarize_dividends([("2024-05-01", 120.0)], "2024-12-31")
    assert out["growth_pct"] is None


def test_summarize_handles_no_history_at_all() -> None:
    out = summarize_dividends([], "2024-12-31")

    assert out["ttm_cash"] == 0.0
    assert out["ttm_events"] == 0
    assert out["total_events"] == 0
    assert out["first_ex_date"] is None
    assert out["last_ex_date"] is None
    assert out["annual"] == []


def test_summarize_without_as_of_has_no_trailing_window() -> None:
    payments = [("2024-05-01", 120.0), ("2023-05-01", 100.0)]
    out = summarize_dividends(payments, None)

    assert out["ttm_cash"] == 0.0
    assert out["ttm_events"] == 0
    assert out["total_cash"] == 220.0


def test_summarize_keeps_at_most_the_lookback_years() -> None:
    start = date(2024, 5, 1)
    payments = [
        ((start - timedelta(days=365 * i)).isoformat(), float(i + 1))
        for i in range(BY_YEAR_LOOKBACK + 5)
    ]
    out = summarize_dividends(payments, "2024-12-31")

    assert len(out["annual"]) == BY_YEAR_LOOKBACK
    assert out["total_events"] == BY_YEAR_LOOKBACK + 5
