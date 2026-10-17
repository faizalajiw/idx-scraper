-- IDX Scraper Schema for Supabase / PostgreSQL
-- Run in Supabase SQL Editor

-- Enable UUID extension (optional, but good practice)
create extension if not exists "uuid-ossp";

-- Table: index_quotes (IHSG & other indices)
create table if not exists index_quotes (
  id uuid primary key default uuid_generate_v4(),
  source text not null check (source in ('IDX', 'Yahoo')),
  code text not null,              -- COMPOSITE, LQ45, etc.
  close numeric(15,3),
  change numeric(15,3),
  percent numeric(10,4),
  current numeric(15,3),
  captured_at timestamp with time zone not null default now(),
  metadata jsonb default '{}'::jsonb
);
create index if not exists idx_index_quotes_code_capture on index_quotes(code, captured_at desc);
create index if not exists idx_index_quotes_capture on index_quotes(captured_at desc);
-- Natural key untuk upsert: `on conflict (source, code, captured_at)` di
-- scripts/backfill_index_eod_idx.py. Hanya ditulis runtime oleh storage.py kalau
-- baris ini hilang, jadi deploy yang cuma menjalankan sql/ akan gagal saat upsert.
create unique index if not exists ux_index_quotes_natural on index_quotes(source, code, captured_at);

-- Table: stock_quotes (per-emitenn snapshot)
create table if not exists stock_quotes (
  id uuid primary key default uuid_generate_v4(),
  source text not null check (source in ('IDX', 'Yahoo')),
  code text not null,              -- BBCA, BBRI, etc.
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
  captured_at timestamp with time zone not null default now(),
  metadata jsonb default '{}'::jsonb
);
create index if not exists idx_stock_quotes_code_capture on stock_quotes(code, captured_at desc);
create index if not exists idx_stock_quotes_capture on stock_quotes(captured_at desc);

-- Table: index_summary_daily (EOD data)
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
  captured_at timestamp with time zone not null default now()
);
create index if not exists idx_index_summary_daily_date on index_summary_daily(date desc);
-- Target `on conflict (code, date)` di index_summary_daily untuk tiap skrip
-- backfill indeks: scripts/backfill_index_eod.py, backfill_index_eod_idx.py, dan
-- backfill_index_history_yahoo.py. Ketiganya juga membuat index ini sendiri saat
-- jalan, jadi tidak ada jalur runtime yang bergantung pada pernyataan di bawah —
-- fungsinya paritas file skema ↔ DB hidup, supaya `psql -f sql/schema.sql`
-- menghasilkan bentuk yang sama.
create unique index if not exists ux_index_summary_daily_code_date on index_summary_daily(code, date);

-- Table: stock_summary_daily (EOD data per stock)
create table if not exists stock_summary_daily (
  id uuid primary key default uuid_generate_v4(),
  code text not null,
  name text,
  close numeric(15,2),
  open numeric(15,2),
  high numeric(15,2),
  low numeric(15,2),
  previous numeric(15,2),
  change numeric(15,2),
  percent numeric(10,4),
  volume bigint,
  value numeric(15,2),
  frequency bigint,
  foreign_buy numeric(18,2),
  foreign_sell numeric(18,2),
  foreign_net numeric(18,2),
  date date not null,
  captured_at timestamp with time zone not null default now()
);
create index if not exists idx_stock_summary_daily_date on stock_summary_daily(date desc);
create index if not exists idx_stock_summary_daily_code_date on stock_summary_daily(code, date desc);
-- Satu baris per (code, date): dibutuhkan `on conflict (code, date)` di
-- src/idx_scraper/storage.py dan scripts/migrate_sqlite_to_postgres.py.
-- storage.py juga membuatnya runtime (setelah membuang duplikat lama).
create unique index if not exists ux_stock_summary_daily_code_date on stock_summary_daily(code, date);

-- Table: stock_daily (riwayat harian per emiten; sumber Yahoo & IDX)
-- Mirror dari DDL di src/idx_scraper/storage.py; `unique (ticker, date)` adalah
-- target `on conflict (ticker, date)` di storage.py dan
-- scripts/migrate_sqlite_to_postgres.py.
create table if not exists stock_daily (
  id uuid primary key default uuid_generate_v4(),
  ticker text not null,
  date date not null,
  open numeric(18,4) not null,
  high numeric(18,4) not null,
  low numeric(18,4) not null,
  close numeric(18,4) not null,
  volume bigint not null,
  adjusted_close numeric(18,4),
  unique(ticker, date)
);
create index if not exists idx_stock_daily_ticker_date on stock_daily(ticker, date desc);

-- Table: news_items (berita Snips) — mirror dari DDL di src/idx_scraper/news.py.
-- `url` sebagai primary key adalah target `on conflict (url) do nothing`.
create table if not exists news_items (
  url text primary key,
  title text not null,
  source text not null,
  published_at timestamptz not null,
  fetched_at timestamptz not null default now()
);
create index if not exists idx_news_items_published on news_items (published_at desc);

-- Functions (optional helpers)
create or replace function last_price(p_code text, p_source text default 'Yahoo')
returns numeric
language sql stable
as $$
  select price from stock_quotes
  where code = p_code and source = p_source
  order by captured_at desc
  limit 1
$$;

create or replace function last_index(p_code text, p_source text default 'IDX')
returns numeric
language sql stable
as $$
  select current from index_quotes
  where code = p_code and source = p_source
  order by captured_at desc
  limit 1
$$;
