"""Integration tests for dashboard/app.py using Streamlit AppTest."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

STREAMLIT_DIR = Path(__file__).resolve().parents[1] / "dashboard"


@pytest.fixture()
def seed_db(tmp_path, monkeypatch):
    """Create a temp SQLite DB with minimal index/stock/EOD data and point app at it."""
    db_path = tmp_path / "idx.db"
    db = sqlite3.connect(db_path)
    db.executescript(
        """
        create table index_quotes (
          id integer primary key autoincrement,
          source text, code text, close real, change real, percent real,
          current real, captured_at text, metadata text
        );
        create table stock_quotes (
          id integer primary key autoincrement,
          source text, code text, board text,
          previous real, open real, high real, low real, close real, change real,
          volume integer, value real, frequency integer,
          bid real, bid_volume integer, offer real, offer_volume integer,
          foreign_net real, captured_at text, metadata text
        );
        create table stock_summary_daily (
          id integer primary key autoincrement,
          code text, close real, open real, high real, low real,
          previous real, change real, percent real,
          volume integer, value real, date text, captured_at text
        );
        """
    )
    ts = "2026-09-21T10:00:00+07:00"
    db.execute(
        "insert into index_quotes (source, code, close, change, percent, current, captured_at, metadata) values (?,?,?,?,?,?,?,?)",
        ("IDX", "COMPOSITE", 7800.5, 12.3, 0.16, 7800.5, ts, "{}"),
    )
    db.execute(
        """insert into stock_quotes (source, code, previous, open, high, low, close, change,
           volume, value, captured_at, metadata) values (?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("IDX", "BBCA", 9800, 9850, 9950, 9800, 9900, 100, 1_000_000, 9.9e9, ts, "{}"),
    )
    # 40 days of synthetic EOD history for BBCA
    start = datetime(2026, 8, 1, tzinfo=timezone(timedelta(hours=7)))
    for i in range(40):
        d = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        close = 9000 + i * 10
        db.execute(
            """insert into stock_summary_daily (code, close, open, high, low, previous, change,
               volume, value, date, captured_at) values (?,?,?,?,?,?,?,?,?,?,?)""",
            ("BBCA", close, close - 20, close + 30, close - 40, close - 10, 10, 1_000_000, 9e9, d, ts),
        )
    db.commit()
    db.close()

    monkeypatch.chdir(STREAMLIT_DIR)
    monkeypatch.setenv("IDX_SQLITE_PATH", str(db_path))
    return db_path


def _run_apptest():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(STREAMLIT_DIR / "app.py"), default_timeout=60)
    at.run()
    return at


def test_dashboard_renders_with_data(seed_db):
    at = _run_apptest()
    assert not at.exception, f"Dashboard raised: {at.exception}"
    # Market overview IHSG metric rendered
    assert any("7,800" in m.value for m in at.metric)


def test_dashboard_watchlist_and_signals(seed_db):
    at = _run_apptest()
    assert not at.exception
    # Default watchlist contains BBCA -> watchlist table renders
    assert any("BBCA" in str(df.value) for df in at.dataframe)


def test_dashboard_rerun_safe(seed_db):
    """Second run must also succeed (no stale module state / rerun loop)."""
    _run_apptest()
    at2 = _run_apptest()
    assert not at2.exception


def test_dashboard_selectbox_changes_ticker(seed_db):
    at = _run_apptest()
    assert not at.exception
    sb = at.selectbox[0]
    sb.set_value("BBCA").run()
    assert not at.exception
