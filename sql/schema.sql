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

-- Table: stock_summary_daily (EOD data per stock)
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
  captured_at timestamp with time zone not null default now()
);
create index if not exists idx_stock_summary_daily_date on stock_summary_daily(date desc);
create index if not exists idx_stock_summary_daily_code_date on stock_summary_daily(code, date desc);

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
