-- ============================================================================
-- SIGNAL LOG — jejak sinyal BUY/SELL harian + basis track record
-- ============================================================================
-- Ditulis oleh `idx signal-log` (cli.cmd_signal_log) / job harian 16:25 WIB.
-- Sinyal dihitung point-in-time dari layer research.prices_asof_adj memakai
-- rule yang sama dengan dashboard (SMA20/50 + RSI, analysis.signal_series),
-- dengan entry dinilai di close T+1.
--
-- Sengaja HANYA menyimpan sinyal non-HOLD: "hari sinyal" adalah definisinya,
-- dan tabel tetap kecil. SELL palsu ex-dividend sudah dibuang saat penulisan
-- (analysis.is_mechanical_sell), jadi track record tidak menghitung sinyal
-- yang bukan tekanan jual sesungguhnya.
--
-- Idempotent: primary key (code, trade_date) — run ulang menimpa baris lama.
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS research;

CREATE TABLE IF NOT EXISTS research.signal_log (
    code         TEXT        NOT NULL,
    trade_date   DATE        NOT NULL,
    signal       TEXT        NOT NULL CHECK (signal IN ('BUY','SELL')),
    close        NUMERIC(18,4),
    rsi          NUMERIC(8,2),
    sma_short    NUMERIC(18,4),
    sma_long     NUMERIC(18,4),
    trend_up     BOOLEAN,
    regime       TEXT,
    source       TEXT        NOT NULL DEFAULT 'PIT',
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (code, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_signal_log_date
    ON research.signal_log (trade_date DESC);

CREATE INDEX IF NOT EXISTS idx_signal_log_signal
    ON research.signal_log (signal, trade_date DESC);

COMMENT ON TABLE research.signal_log IS
    'Jejak sinyal BUY/SELL harian (PIT). Kinerja ke depan dihitung on-the-fly dari prices_asof_adj; regime = regime IHSG saat sinyal.';
