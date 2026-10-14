"""Unit tests untuk aturan kesegaran dossier (fungsi murni, tanpa database)."""

from __future__ import annotations

from idx_scraper.api.dossier import mark_stale


def test_same_session_is_fresh():
    assert mark_stale("2026-10-09", "2026-10-09") is False


def test_older_session_is_stale():
    assert mark_stale("2026-10-08", "2026-10-09") is True


def test_missing_session_date_is_stale_when_market_has_data():
    assert mark_stale(None, "2026-10-09") is True


def test_no_market_reference_never_marks_stale():
    assert mark_stale("2026-10-08", None) is False
    assert mark_stale(None, None) is False
