-- Buku klaim lapis 4 (P6 "Pengikat & Pengingat").
--
-- Satu baris = satu klaim mesin atas satu nama di satu sesi. Ditulis SEKALI oleh
-- job terjadwal SEBELUM hasilnya ada, dan tidak pernah ditimpa: kuncinya
-- (code, trade_date, rule) dengan `on conflict do nothing`. Kalau baris ini bisa
-- diedit sesudah hasilnya kelihatan, ia berhenti jadi bukti dan jadi cerita.
--
-- claim_refusal mencatat sesi yang tidak melahirkan klaim beserta sebabnya, supaya
-- "tidak ada klaim" selalu bisa dijawab: job gagal, atau sesi memang tanpa nama.
-- Barisnya catatan percobaan, bukan kesimpulan (satu sesi boleh punya penolakan
-- jam 17:00 lalu klaim jam 18:00).
--
-- Tabelnya ditulis juga oleh `research/journal_store.py` (DDL yang sama) supaya
-- perintah pertama sudah bisa jalan tanpa deploy; berkas ini untuk deploy.
--
-- Jalankan: psql -f sql/journal.sql

create table if not exists research.claim_journal (
    code             text          not null,
    trade_date       date          not null,
    rule             text          not null,
    direction        text          not null default 'BUY',
    grade            text,
    ref_raw          numeric(18,4) not null,
    stop_raw         numeric(18,4) not null,
    target_raw       numeric(18,4) not null,
    ref_adj          numeric(18,4),
    horizon_days     integer       not null,
    size_pct         numeric(9,4),
    size_lots        integer,
    size_rupiah      numeric(18,2),
    regime           text,
    vol_state        text,
    freshness_status text,
    status           text          not null default 'terbuka'
                     check (status in ('terbuka','kena_target','kena_stop','kedaluwarsa')),
    outcome_abn      numeric(12,8),
    touch_date       date,
    recorded_at      timestamptz   not null default now(),
    primary key (code, trade_date, rule)
);
create index if not exists idx_claim_journal_date
    on research.claim_journal (trade_date desc, status);

create table if not exists research.claim_refusal (
    trade_date date        not null,
    reason     text        not null,
    detail     text,
    created_at timestamptz not null default now(),
    primary key (trade_date, reason)
);

-- Cek hasil:
--   select count(*) from research.claim_journal;
--   select trade_date, reason, detail from research.claim_refusal order by trade_date desc limit 10;
