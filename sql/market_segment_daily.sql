-- Agregat pasar REGULER vs NON-REGULER (tunai + negosiasi) per hari bursa.
-- Sumber: /primary/TradingSummary/GetStockSummary — IDX memisahkan Volume/Value/
-- Frequency (reguler) dari NonRegularVolume/Value/Frequency (tunai+negosiasi)
-- per emiten. Jumlah keduanya = total pasar (terverifikasi silang dengan
-- /primary/Home/GetTradeSummary baris "Saham").
--
-- DDL dijalankan otomatis oleh idx_scraper.market_segment.ensure_table() (dipakai
-- job EOD `idx serve` maupun scripts.backfill_market_segment). File ini mirror
-- untuk dijalankan manual di SQL editor (Supabase) kalau perlu.
--
-- Upsert idempoten per trade_date, jadi aman dijalankan ulang.

create table if not exists research.market_segment_daily (
    trade_date      date        not null,
    regular_volume  bigint,
    regular_value   numeric(24,4),
    regular_freq    bigint,
    nonreg_volume   bigint,
    nonreg_value    numeric(24,4),
    nonreg_freq     bigint,
    total_volume    bigint,
    total_value     numeric(24,4),
    stock_count     integer,
    source          text        not null default 'IDX',
    captured_at     timestamptz not null default now(),
    primary key (trade_date)
);
create index if not exists idx_market_segment_daily_date
    on research.market_segment_daily (trade_date desc);

-- Kolom non-reguler per emiten di raw_eod (hanya tambah; baris lama yang sudah
-- ada tetap utuh dan ber-Nilai NULL = "tidak terdata", bukan "nol").
alter table research.raw_eod add column if not exists nr_volume bigint;
alter table research.raw_eod add column if not exists nr_value  numeric(24,4);
alter table research.raw_eod add column if not exists nr_freq   bigint;
