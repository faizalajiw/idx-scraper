"""Unit tests for idx_scraper.storage (SQLite backend, in tmp_path)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from idx_scraper.models import EodStockRow
from idx_scraper.storage import SQLiteStorage, StockDaily

WIB = timezone(timedelta(hours=7))
NOW = datetime(2026, 9, 21, 10, 0, 0, tzinfo=WIB)


def make_row(code="BBCA", date="20260921", close=9000.0) -> EodStockRow:
    return EodStockRow(
        source="IDX",
        date=date,
        code=code,
        previous=close - 100,
        open=close - 50,
        high=close + 50,
        low=close - 60,
        close=close,
        change=100.0,
        volume=1_000_000,
        value=9_000_000_000,
        captured_at=NOW,
    )


def make_storage(tmp_path) -> SQLiteStorage:
    return SQLiteStorage(tmp_path / "test_idx.db")


def test_insert_and_load_price_history(tmp_path):
    storage = make_storage(tmp_path)
    try:
        storage.insert_eod_stock(make_row(date="20260915", close=8800))
        storage.insert_eod_stock(make_row(date="20260916", close=8900))
        storage.insert_eod_stock(make_row(date="20260917", close=9000))
        rows = storage.load_price_history("BBCA")
        assert len(rows) == 3
        assert [r["date"] for r in rows] == ["2026-09-15", "2026-09-16", "2026-09-17"]
        assert rows[-1]["close"] == 9000
    finally:
        storage.close()


def test_eod_duplicate_insert_upserts_latest(tmp_path):
    storage = make_storage(tmp_path)
    try:
        storage.insert_eod_stock(make_row(date="20260915", close=8800))
        storage.insert_eod_stock(make_row(date="20260915", close=9999))
        rows = storage.load_price_history("BBCA")
        assert len(rows) == 1
        assert rows[0]["close"] == 9999  # latest fetch wins (EOD runs twice/day)
    finally:
        storage.close()


def test_eod_derives_previous_and_percent_when_missing(tmp_path):
    """IDX EOD summary omits PreviousPrice; storage derives it from close - change."""
    storage = make_storage(tmp_path)
    try:
        row = EodStockRow(
            source="IDX",
            date="20260921",
            code="BBRI",
            previous=None,  # missing from IDX summary
            open=4900.0,
            high=5100.0,
            low=4850.0,
            close=5000.0,
            change=200.0,
            volume=2_000_000,
            value=10_000_000_000,
            captured_at=NOW,
        )
        storage.insert_eod_stock(row)
        got = storage._conn.execute(
            "select previous, percent from stock_summary_daily where code=? and date=?",
            ("BBRI", "2026-09-21"),
        ).fetchone()
        assert got is not None
        previous, percent = got
        assert previous == 4800.0  # 5000 - 200
        assert abs(percent - (200.0 / 4800.0 * 100)) < 1e-6
    finally:
        storage.close()

def test_eod_legacy_yyyymmdd_dates_migrated(tmp_path):
    storage = make_storage(tmp_path)
    try:
        storage.insert_eod_stock(make_row(date="20260910", close=8800))
        # Legacy row written directly in old format
        storage._conn.execute(
            "insert into stock_summary_daily (code, close, date, captured_at) values (?, ?, ?, ?)",
            ("BBCA", 8900.0, "20260911", NOW.isoformat()),
        )
        storage._conn.commit()
        # Re-opening the same DB triggers the migration
        storage2 = SQLiteStorage(storage.path)
        try:
            rows = storage2.load_price_history("BBCA")
            assert [r["date"] for r in rows] == ["2026-09-10", "2026-09-11"]
        finally:
            storage2.close()
    finally:
        storage.close()


def test_history_prefers_idx_eod_over_yahoo(tmp_path):
    storage = make_storage(tmp_path)
    try:
        storage.insert_eod_stock(make_row(date="20260915", close=8800))
        storage.insert_stock_daily(
            StockDaily(
                ticker="BBCA",
                date=datetime(2026, 9, 15, tzinfo=WIB),
                open=8700,
                high=8850,
                low=8650,
                close=8799,  # Yahoo value should NOT override IDX EOD
                volume=2_000_000,
            )
        )
        rows = storage.load_price_history("BBCA")
        assert len(rows) == 1
        assert rows[0]["close"] == 8800
    finally:
        storage.close()


def test_history_fills_gaps_with_yahoo(tmp_path):
    storage = make_storage(tmp_path)
    try:
        storage.insert_eod_stock(make_row(date="20260915", close=8800))
        storage.insert_stock_daily(
            StockDaily(
                ticker="BBCA",
                date=datetime(2026, 9, 12, tzinfo=WIB),
                open=8600,
                high=8700,
                low=8550,
                close=8650,
                volume=1_500_000,
            )
        )
        rows = storage.load_price_history("BBCA")
        assert [r["date"] for r in rows] == ["2026-09-12", "2026-09-15"]
    finally:
        storage.close()


def test_stock_daily_insert_is_idempotent(tmp_path):
    storage = make_storage(tmp_path)
    try:
        bar = StockDaily(
            ticker="TLKM",
            date=datetime(2026, 9, 12, tzinfo=WIB),
            open=3000,
            high=3100,
            low=2950,
            close=3050,
            volume=5_000_000,
        )
        storage.insert_stock_daily(bar)
        storage.insert_stock_daily(bar)
        rows = storage.load_price_history("TLKM")
        assert len(rows) == 1
    finally:
        storage.close()
