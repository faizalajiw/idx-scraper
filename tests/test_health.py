"""Unit tests: health-check data EOD (expected trading date + evaluasi keterlambatan)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from idx_scraper.health import (
    EodHealth,
    evaluate_health,
    expected_trading_date,
    format_health_message,
    parse_holidays,
)

WIB = timezone(timedelta(hours=7))


def _wib(y, m, d, hh=0, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=WIB)


# --------------------------------------------------------------------------- #
# expected_trading_date
# --------------------------------------------------------------------------- #


def test_weekday_is_itself():
    mon = _wib(2026, 10, 5)  # Senin
    assert mon.weekday() == 0
    assert expected_trading_date(mon, holidays=set()) == date(2026, 10, 5)


def test_saturday_falls_back_to_friday():
    sat = _wib(2026, 10, 10)
    assert sat.weekday() == 5
    assert expected_trading_date(sat, holidays=set()) == date(2026, 10, 9)


def test_sunday_falls_back_to_friday():
    sun = _wib(2026, 10, 11)
    assert sun.weekday() == 6
    assert expected_trading_date(sun, holidays=set()) == date(2026, 10, 9)


def test_holiday_skips_to_previous_weekday():
    # Senin libur -> hari bursa terakhir = Jumat sebelumnya.
    mon = _wib(2026, 10, 5)
    assert expected_trading_date(mon, holidays={date(2026, 10, 5)}) == date(2026, 10, 2)


def test_holiday_chain_skips_weekend():
    # Kamis & Jumat libur -> mundur ke Rabu.
    fri = _wib(2026, 10, 9, 9, 0)
    holidays = {date(2026, 10, 8), date(2026, 10, 9)}
    assert expected_trading_date(fri, holidays=holidays) == date(2026, 10, 7)


# --------------------------------------------------------------------------- #
# parse_holidays
# --------------------------------------------------------------------------- #


def test_parse_holidays_ignores_garbage():
    out = parse_holidays("2026-12-25, bukan-tanggal ,2027-01-01,,")
    assert out == {date(2026, 12, 25), date(2027, 1, 1)}


def test_parse_holidays_empty():
    assert parse_holidays("") == set()


# --------------------------------------------------------------------------- #
# evaluate_health
# --------------------------------------------------------------------------- #


def test_complete_data_is_ok():
    h = evaluate_health(
        expected=date(2026, 10, 5),
        stocks_latest=date(2026, 10, 5),
        index_latest=date(2026, 10, 5),
        now=_wib(2026, 10, 5, 17, 5),
    )
    assert h.ok and not h.overdue
    assert h.missing == []


def test_missing_after_deadline_is_overdue():
    h = evaluate_health(
        expected=date(2026, 10, 5),
        stocks_latest=date(2026, 10, 2),
        index_latest=date(2026, 10, 5),
        now=_wib(2026, 10, 5, 17, 0),
    )
    assert h.overdue and not h.ok
    assert len(h.missing) == 1 and "saham" in h.missing[0]


def test_missing_before_deadline_is_not_overdue():
    h = evaluate_health(
        expected=date(2026, 10, 5),
        stocks_latest=date(2026, 10, 2),
        index_latest=date(2026, 10, 2),
        now=_wib(2026, 10, 5, 9, 30),
    )
    assert not h.overdue and h.ok
    assert len(h.missing) == 2  # tetap dilaporkan, cuma belum "telat"


def test_missing_on_weekend_not_overdue():
    h = evaluate_health(
        expected=date(2026, 10, 9),
        stocks_latest=date(2026, 10, 8),
        index_latest=date(2026, 10, 9),
        now=_wib(2026, 10, 10, 20, 0),  # Sabtu malam
    )
    assert not h.overdue


def test_index_only_missing_is_overdue():
    h = evaluate_health(
        expected=date(2026, 10, 5),
        stocks_latest=date(2026, 10, 5),
        index_latest=None,
        now=_wib(2026, 10, 5, 18, 0),
    )
    assert h.overdue
    assert any("IHSG" in m for m in h.missing)


def test_custom_deadline_hour():
    h = evaluate_health(
        expected=date(2026, 10, 5),
        stocks_latest=date(2026, 10, 2),
        index_latest=date(2026, 10, 2),
        now=_wib(2026, 10, 5, 16, 30),
        deadline_hour=16,
    )
    assert h.overdue


# --------------------------------------------------------------------------- #
# format_health_message
# --------------------------------------------------------------------------- #


def test_message_mentions_expected_date_and_fix():
    h = EodHealth(
        expected=date(2026, 10, 5),
        stocks_latest=date(2026, 10, 2),
        index_latest=date(2026, 10, 2),
        checked_at=_wib(2026, 10, 5, 17, 0),
        missing=["saham EOD (terbaru 2026-10-02)"],
        overdue=True,
    )
    msg = format_health_message(h)
    assert "2026-10-05" in msg
    assert "idx eod" in msg
    assert "idx serve" in msg
