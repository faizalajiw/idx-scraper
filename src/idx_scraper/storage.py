"""
Storage abstraction for IDX scraper.

Supports:
- SQLite (default, zero-config, file-based) — great for local dev / offline
- Supabase / Postgres — production, shared, scalable, API-ready
"""

from __future__ import annotations

import os
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# Optional imports (only loaded if backend selected)
try:
    import psycopg
except ImportError:
    psycopg = None

from .models import EodStockRow, IndexQuote, StockQuote


@dataclass
class IndexSummaryDaily:
    code: str
    close: float | None
    open_: float | None
    high: float | None
    low: float | None
    volume: int | None
    value: float | None
    date: datetime
    captured_at: datetime


@dataclass
class StockSummaryDaily:
    code: str
    close: float | None
    open_: float | None
    high: float | None
    low: float | None
    previous: float | None
    change: float | None
    percent: float | None
    volume: int | None
    value: float | None
    date: datetime
    captured_at: datetime


# NOTE: IndexQuote & StockQuote models live in .models (pydantic) —
# the duplicate dataclasses that shadowed them here were removed.


@dataclass
class StockDaily:
    """OHLCV daily bar from Yahoo Finance or similar."""
    ticker: str
    date: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    adjusted_close: float | None = None


def _norm_date(value: str) -> str:
    """Normalize 'YYYYMMDD' or ISO date strings to ISO 'YYYY-MM-DD'."""
    s = str(value)
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


class Storage(ABC):
    """Abstract storage backend."""

    @abstractmethod
    def insert_index_quote(self, quote: IndexQuote) -> None: ...
    @abstractmethod
    def insert_stock_quote(self, quote: StockQuote) -> None: ...
    @abstractmethod
    def insert_index_summary(self, summary: IndexSummaryDaily) -> None: ...
    @abstractmethod
    def insert_stock_summary(self, summary: StockSummaryDaily) -> None: ...
    @abstractmethod
    def insert_eod_stock(self, row: EodStockRow) -> None: ...
    @abstractmethod
    def insert_stock_daily(self, daily: StockDaily) -> None: ...
    @abstractmethod
    def load_price_history(self, code: str, limit: int = 60) -> list[dict[str, Any]]: ...
    @abstractmethod
    def close(self) -> None: ...


class SQLiteStorage(Storage):
    """SQLite file-based storage. Zero config, great for local dev."""

    def __init__(self, path: str | Path = "./data/idx.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._init_schema()

    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.executescript("""
        create table if not exists index_quotes (
          id integer primary key autoincrement,
          source text not null check (source in ('IDX','Yahoo')),
          code text not null,
          close real, change real, percent real, current real,
          captured_at text not null,
          metadata text default '{}'
        );
        create index if not exists idx_index_quotes_code_capture
          on index_quotes(code, captured_at desc);

        create table if not exists stock_quotes (
          id integer primary key autoincrement,
          source text not null check (source in ('IDX','Yahoo')),
          code text not null,
          board text,
          previous real, open real, high real, low real, close real, change real,
          volume integer, value real, frequency integer,
          bid real, bid_volume integer, offer real, offer_volume integer, foreign_net real,
          captured_at text not null,
          metadata text default '{}'
        );
        create index if not exists idx_stock_quotes_code_capture
          on stock_quotes(code, captured_at desc);

        create table if not exists index_summary_daily (
          id integer primary key autoincrement,
          code text not null,
          close real, open real, high real, low real,
          volume integer, value real,
          date text not null,
          captured_at text not null
        );
        create index if not exists idx_index_summary_daily_date
          on index_summary_daily(date desc);

        create table if not exists stock_summary_daily (
          id integer primary key autoincrement,
          code text not null,
          close real, open real, high real, low real,
          previous real, change real, percent real,
          volume integer, value real,
          date text not null,
          captured_at text not null
        );
        create index if not exists idx_stock_summary_daily_date
          on stock_summary_daily(date desc);
        create index if not exists idx_stock_summary_daily_code_date
          on stock_summary_daily(code, date desc);
        create table if not exists stock_daily (
          id integer primary key autoincrement,
          ticker text not null,
          date text not null,
          open real not null,
          high real not null,
          low real not null,
          close real not null,
          volume integer not null,
          adjusted_close real,
          unique(ticker, date)
        );
        create index if not exists idx_stock_daily_ticker_date on stock_daily(ticker, date desc);
        """)
        # Normalize legacy 'YYYYMMDD' EOD dates to ISO so (code, date) dedup works.
        cur.execute(
            "update stock_summary_daily set date = substr(date,1,4)||'-'||substr(date,5,2)||'-'||substr(date,7,2) "
            "where length(date)=8 and date not like '%-%'"
        )
        # One EOD row per (code, date): de-duplicate legacy rows, then enforce a unique index.
        has_ux = cur.execute(
            "select 1 from sqlite_master where type='index' and name='ux_stock_summary_daily_code_date'"
        ).fetchone()
        if not has_ux:
            cur.execute(
                "delete from stock_summary_daily where id not in "
                "(select min(id) from stock_summary_daily group by code, date)"
            )
            cur.execute(
                "create unique index ux_stock_summary_daily_code_date on stock_summary_daily(code, date)"
            )
        self._conn.commit()

    def _ts(self, dt: datetime) -> str:
        return dt.isoformat()

    def insert_stock_daily(self, daily: StockDaily) -> None:
        self._conn.execute(
            """insert or ignore into stock_daily (ticker, date, open, high, low, close, volume, adjusted_close)
               values (?, ?, ?, ?, ?, ?, ?, ?)""",
            (daily.ticker, daily.date.date().isoformat(), daily.open, daily.high, daily.low, daily.close, daily.volume, daily.adjusted_close),
        )
        self._conn.commit()

    def insert_index_quote(self, q: IndexQuote) -> None:
        self._conn.execute(
            """insert into index_quotes (source, code, close, change, percent, current, captured_at, metadata)
               values (?, ?, ?, ?, ?, ?, ?, ?)""",
            (q.source, q.code, q.close, q.change, q.percent, q.current,
             self._ts(q.captured_at), str(q.metadata or {})),
        )
        self._conn.commit()

    def insert_stock_quote(self, q: StockQuote) -> None:
        self._conn.execute(
            """insert into stock_quotes (source, code, board, previous, open, high, low, close, change,
               volume, value, frequency, bid, bid_volume, offer, offer_volume, foreign_net, captured_at, metadata)
               values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (q.source, q.code, q.board, q.previous, q.open, q.high, q.low, q.close, q.change,
             q.volume, q.value, q.frequency, q.bid, q.bid_volume, q.offer, q.offer_volume, q.foreign_net,
             self._ts(q.captured_at), str(q.metadata or {})),
        )
        self._conn.commit()

    def insert_eod_stock(self, row: EodStockRow) -> None:
        percent = (row.change / row.previous * 100) if (row.previous and row.change is not None) else None
        self._conn.execute(
            """insert or ignore into stock_summary_daily (code, close, open, high, low, previous, change, percent,
               volume, value, date, captured_at)
               values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (row.code, row.close, row.open, row.high, row.low, row.previous, row.change,
             percent, row.volume, row.value, _norm_date(row.date), self._ts(row.captured_at)),
        )
        self._conn.commit()

    def insert_index_summary(self, s: IndexSummaryDaily) -> None:
        self._conn.execute(
            """insert into index_summary_daily (code, close, open, high, low, volume, value, date, captured_at)
               values (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (s.code, s.close, s.open_, s.high, s.low, s.volume, s.value,
             s.date.date().isoformat(), self._ts(s.captured_at)),
        )
        self._conn.commit()

    def insert_stock_summary(self, s: StockSummaryDaily) -> None:
        self._conn.execute(
            """insert into stock_summary_daily (code, close, open, high, low, previous, change, percent,
               volume, value, date, captured_at)
               values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (s.code, s.close, s.open_, s.high, s.low, s.previous, s.change, s.percent,
             s.volume, s.value, s.date.date().isoformat(), self._ts(s.captured_at)),
        )
        self._conn.commit()

    def load_price_history(self, code: str, limit: int = 60) -> list[dict[str, Any]]:
        """Daily OHLCV history: prefer IDX EOD rows, fill gaps with Yahoo (stock_daily)."""
        rows: dict[str, dict[str, Any]] = {}
        # No SQL limit: legacy tables may mix date formats, so sort/slice in Python.
        cur = self._conn.execute(
            """select date, open, high, low, close, volume from stock_summary_daily
               where code = ?""",
            (code,),
        )
        for date, o, h, low, c, v in cur.fetchall():
            iso = _norm_date(date)
            rows[iso] = {"date": iso, "open": o, "high": h, "low": low, "close": c, "volume": v}
        cur = self._conn.execute(
            """select date, open, high, low, close, volume from stock_daily
               where ticker = ?""",
            (code,),
        )
        for date, o, h, low, c, v in cur.fetchall():
            iso = _norm_date(date)
            rows.setdefault(iso, {"date": iso, "open": o, "high": h, "low": low, "close": c, "volume": v})
        return [rows[k] for k in sorted(rows)][-limit:]

    def close(self) -> None:
        self._conn.close()


class SupabaseStorage(Storage):
    """Supabase / Postgres storage via psycopg. Uses connection pooler for polling."""

    def __init__(self, dsn: str) -> None:
        if psycopg is None:
            raise RuntimeError("psycopg[binary] not installed. Run: pip install 'idx-scraper[supabase]'")
        # Use autocommit for simple inserts; connection pooling handled by Supabase pooler
        self._conn = psycopg.connect(dsn, autocommit=True, connect_timeout=10)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute('create extension if not exists "uuid-ossp";')
            cur.execute("""
            create table if not exists index_quotes (
              id uuid primary key default uuid_generate_v4(),
              source text not null check (source in ('IDX','Yahoo')),
              code text not null,
              close numeric(15,3),
              change numeric(15,3),
              percent numeric(10,4),
              current numeric(15,3),
              captured_at timestamptz not null default now(),
              metadata jsonb default '{}'::jsonb
            );
            create index if not exists idx_index_quotes_code_capture on index_quotes(code, captured_at desc);
            """)
            cur.execute("""
            create table if not exists stock_quotes (
              id uuid primary key default uuid_generate_v4(),
              source text not null check (source in ('IDX','Yahoo')),
              code text not null,
              price numeric(15,2),
              previous_price numeric(15,2),
              change numeric(15,2),
              percent numeric(10,4),
              volume bigint,
              value numeric(15,2),
              bid numeric(15,2),
              bid_volume bigint,
              offer numeric(15,2),
              offer_volume bigint,
              captured_at timestamptz not null default now(),
              metadata jsonb default '{}'::jsonb
            );
            create index if not exists idx_stock_quotes_code_capture on stock_quotes(code, captured_at desc);
            """)
            cur.execute("""
            create table if not exists index_summary_daily (
              id uuid primary key default uuid_generate_v4(),
              code text not null,
              close numeric(15,3),
              open numeric(15,3),
              high numeric(15,3),
              low numeric(15,3),
              volume bigint,
              value numeric(15,2),
              date date not null,
              captured_at timestamptz not null default now()
            );
            create index if not exists idx_index_summary_daily_date on index_summary_daily(date desc);
            """)
            cur.execute("""
            create table if not exists stock_summary_daily (
              id uuid primary key default uuid_generate_v4(),
              code text not null,
              close numeric(15,2),
              open numeric(15,2),
              high numeric(15,2),
              low numeric(15,2),
              previous numeric(15,2),
              change numeric(15,2),
              percent numeric(10,4),
              volume bigint,
              value numeric(15,2),
              date date not null,
              captured_at timestamptz not null default now()
            );
            create index if not exists idx_stock_summary_daily_date on stock_summary_daily(date desc);
            create index if not exists idx_stock_summary_daily_code_date on stock_summary_daily(code, date desc);
            """)
            cur.execute("""
            create table if not exists stock_daily (
              id uuid primary key default uuid_generate_v4(),
              ticker text not null,
              date date not null,
              open numeric(15,2) not null,
              high numeric(15,2) not null,
              low numeric(15,2) not null,
              close numeric(15,2) not null,
              volume bigint not null,
              adjusted_close numeric(15,2),
              unique(ticker, date)
            );
            create index if not exists idx_stock_daily_ticker_date on stock_daily(ticker, date desc);
            """)
            # One EOD row per (code, date): de-duplicate then enforce uniqueness for ON CONFLICT.
            cur.execute(
                """delete from stock_summary_daily s
                   using stock_summary_daily s2
                   where s.code = s2.code and s.date = s2.date and s.id > s2.id"""
            )
            cur.execute(
                """create unique index if not exists ux_stock_summary_daily_code_date
                   on stock_summary_daily(code, date)"""
            )

    def insert_stock_daily(self, daily: StockDaily) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """insert into stock_daily (ticker, date, open, high, low, close, volume, adjusted_close)
                   values (%s, %s, %s, %s, %s, %s, %s, %s)
                   on conflict (ticker, date) do nothing""",
                (daily.ticker, daily.date.date(), daily.open, daily.high, daily.low, daily.close, daily.volume, daily.adjusted_close),
            )

    def insert_index_quote(self, q: IndexQuote) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """insert into index_quotes (source, code, close, change, percent, current, captured_at, metadata)
                   values (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (q.source, q.code, q.close, q.change, q.percent, q.current, self._ts(q.captured_at), str(q.metadata or {})),
            )

    def _ts(self, dt: datetime) -> str:
        return dt.isoformat()

    def insert_stock_quote(self, q: StockQuote) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """insert into stock_quotes (source, code, board, previous, open, high, low, close, change,
                   volume, value, frequency, bid, bid_volume, offer, offer_volume, foreign_net, captured_at, metadata)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (q.source, q.code, q.board, q.previous, q.open, q.high, q.low, q.close, q.change,
                 q.volume, q.value, q.frequency, q.bid, q.bid_volume, q.offer, q.offer_volume, q.foreign_net,
                 self._ts(q.captured_at), str(q.metadata or {})),
            )

    def insert_index_summary(self, s: IndexSummaryDaily) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """insert into index_summary_daily (code, close, open, high, low, volume, value, date, captured_at)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (s.code, s.close, s.open_, s.high, s.low, s.volume, s.value,
                 s.date.date().isoformat(), self._ts(s.captured_at)),
            )

    def insert_stock_summary(self, s: StockSummaryDaily) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """insert into stock_summary_daily (code, close, open, high, low, previous, change, percent,
                   volume, value, date, captured_at)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (s.code, s.close, s.open_, s.high, s.low, s.previous, s.change, s.percent,
                 s.volume, s.value, s.date.date().isoformat(), self._ts(s.captured_at)),
            )

    def insert_eod_stock(self, row: EodStockRow) -> None:
        percent = (row.change / row.previous * 100) if (row.previous and row.change is not None) else None
        with self._conn.cursor() as cur:
            cur.execute(
                """insert into stock_summary_daily (code, close, open, high, low, previous, change, percent,
                   volume, value, date, captured_at)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   on conflict (code, date) do nothing""",
                (row.code, row.close, row.open, row.high, row.low, row.previous, row.change,
                 percent, row.volume, row.value, _norm_date(row.date), self._ts(row.captured_at)),
            )

    def load_price_history(self, code: str, limit: int = 60) -> list[dict[str, Any]]:
        """Daily OHLCV history: prefer IDX EOD rows, fill gaps with Yahoo (stock_daily)."""
        rows: dict[str, dict[str, Any]] = {}
        with self._conn.cursor() as cur:
            cur.execute(
                """select date::text, open, high, low, close, volume from stock_summary_daily
                   where code = %s order by date desc limit %s""",
                (code, limit),
            )
            for date, o, h, low, c, v in cur.fetchall():
                iso = _norm_date(date)
                rows[iso] = {"date": iso, "open": o, "high": h, "low": low, "close": c, "volume": v}
            cur.execute(
                """select date::text, open, high, low, close, volume from stock_daily
                   where ticker = %s order by date desc limit %s""",
                (code, limit),
            )
            for date, o, h, low, c, v in cur.fetchall():
                iso = _norm_date(date)
                rows.setdefault(iso, {"date": iso, "open": o, "high": h, "low": low, "close": c, "volume": v})
        return [rows[k] for k in sorted(rows)][-limit:]

    def close(self) -> None:
        self._conn.close()


def get_storage_from_env() -> Storage:
    """Factory: pick backend from env vars."""
    backend = os.getenv("IDX_STORAGE", "sqlite").lower()
    if backend == "supabase":
        dsn = os.getenv("SUPABASE_DB_URL")
        if not dsn:
            raise RuntimeError("IDX_STORAGE=supabase but SUPABASE_DB_URL not set")
        return SupabaseStorage(dsn)
    # default sqlite
    sqlite_path = os.getenv("IDX_SQLITE_PATH", "./data/idx.db")
    return SQLiteStorage(sqlite_path)