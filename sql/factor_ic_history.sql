-- ============================================================================
-- FACTOR IC HISTORY — histori hasil IC analysis bulanan + bobot composite
-- ============================================================================
-- Ditulis oleh `idx ic` (cli.cmd_ic) / job bulanan di cmd_serve. Dipakai
-- research.composite untuk membaca bobot faktor TERBARU ke hold-check
-- (fallback: konstanta FACTOR_WEIGHTS di composite.py).
--
-- Idempotent: primary key (run_date, factor, horizon) — analysis ulang untuk
-- tanggal yang sama menimpa baris lama, tidak menduplikasi.
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS research;

CREATE TABLE IF NOT EXISTS research.factor_ic_history (
    run_date      DATE PRIMARY KEY
                  DEFAULT (current_date),
    factor        TEXT        NOT NULL,
    horizon       INTEGER     NOT NULL,
    mean_ic       NUMERIC(12,6),
    icir          NUMERIC(12,6),
    t_stat        NUMERIC(12,6),
    hit_rate      NUMERIC(8,6),
    n_days        INTEGER,
    eligible      BOOLEAN     NOT NULL DEFAULT false,
    weight        NUMERIC(8,6) NOT NULL DEFAULT 0,
    generated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_date, factor, horizon)
);

CREATE INDEX IF NOT EXISTS idx_factor_ic_history_recent
    ON research.factor_ic_history (run_date DESC);

COMMENT ON TABLE research.factor_ic_history IS
    'Histori IC analysis faktor (bulanan). Bobot composite hold-check dibaca dari run terbaru; eligible = |IC|>=0.05 & |ICIR|>=0.5.';
