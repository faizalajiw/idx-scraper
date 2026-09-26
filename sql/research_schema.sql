-- ============================================================================
-- RESEARCH SCHEMA (Phase 0) — bitemporal, point-in-time, backtest-safe layer
-- ============================================================================
-- Runs ALONGSIDE the existing tables (index_quotes, stock_summary_daily, ...).
-- Nothing here touches or migrates the legacy tables. Dashboard keeps running
-- on the old tables until this layer is backfilled and validated.
--
-- Core principles:
--   1. raw_eod is APPEND-ONLY as-reported data. Never UPDATE, never overwrite.
--   2. Bitemporal: trade_date (when it happened) vs knowledge_date (when we knew)
--      -> zero look-ahead bias for backtests.
--   3. Adjusted prices are DERIVED via VIEW, never stored. raw = truth.
--   4. Quality gate rejects bad rows into quarantine before they hit raw_eod.
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS research;

-- ----------------------------------------------------------------------------
-- 1) RAW EOD — as-reported from IDX, append-only, NEVER updated
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research.raw_eod (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code         TEXT        NOT NULL,
    trade_date   DATE        NOT NULL,
    open         NUMERIC(18,4),
    high         NUMERIC(18,4),
    low          NUMERIC(18,4),
    close        NUMERIC(18,4),
    prev_close   NUMERIC(18,4),
    volume       BIGINT,
    value        NUMERIC(24,4),
    frequency    BIGINT,
    source       TEXT        NOT NULL DEFAULT 'IDX',
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now()   -- knowledge_date
);
CREATE INDEX IF NOT EXISTS idx_raw_eod_lookup
    ON research.raw_eod (code, trade_date, ingested_at DESC);

-- ----------------------------------------------------------------------------
-- 2) PRICES_PIT — bitemporal point-in-time snapshot
--    "price for trade_date, as known at knowledge_date"
--    Populated from raw_eod. A backtest queries "as of" a knowledge_date to
--    guarantee it only sees data that existed at simulation time.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research.prices_pit (
    code           TEXT        NOT NULL,
    trade_date     DATE        NOT NULL,
    knowledge_date TIMESTAMPTZ NOT NULL,
    open           NUMERIC(18,4),
    high           NUMERIC(18,4),
    low            NUMERIC(18,4),
    close          NUMERIC(18,4),
    volume         BIGINT,
    value          NUMERIC(24,4),
    PRIMARY KEY (code, trade_date, knowledge_date)
);
CREATE INDEX IF NOT EXISTS idx_prices_pit_asof
    ON research.prices_pit (code, trade_date, knowledge_date DESC);

-- ----------------------------------------------------------------------------
-- 3) CORPORATE ACTIONS — official IDX (split, reverse split, dividend, bonus, RI)
--    ratio semantics for splits:
--      forward split 1:2  -> ratio = 2.0  (share count x2, price /2)
--      reverse split 4:1  -> ratio = 0.25 (share count /4, price x4)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research.corporate_actions (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code        TEXT        NOT NULL,
    ex_date     DATE        NOT NULL,
    action_type TEXT        NOT NULL
                CHECK (action_type IN ('split','reverse_split','dividend','bonus','rights')),
    ratio       NUMERIC(18,8),   -- price adjustment factor (splits/bonus/rights)
    cash_amount NUMERIC(18,4),   -- cash dividend per share, if any
    source      TEXT        NOT NULL DEFAULT 'IDX',
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (code, ex_date, action_type)
);
CREATE INDEX IF NOT EXISTS idx_corp_actions_code_date
    ON research.corporate_actions (code, ex_date);

-- ----------------------------------------------------------------------------
-- 4) QUARANTINE — rows that failed the quality gate, kept for audit
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research.quarantine_eod (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code        TEXT,
    trade_date  DATE,
    payload     JSONB       NOT NULL,   -- full raw row, verbatim
    reason      TEXT        NOT NULL,   -- why it was rejected
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_quarantine_code_date
    ON research.quarantine_eod (code, trade_date);

-- ----------------------------------------------------------------------------
-- 5) QUALITY GATE — validates a candidate EOD row.
--    Returns NULL if the row is clean, or a text reason if it must be quarantined.
--    Called by the ingestion code before inserting into raw_eod.
--    The >35% jump check is left to the ingestion layer (needs corp-action lookup).
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION research.eod_reject_reason(
    p_open   NUMERIC,
    p_high   NUMERIC,
    p_low    NUMERIC,
    p_close  NUMERIC,
    p_volume BIGINT
) RETURNS TEXT
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    non_traded BOOLEAN;
BEGIN
    IF p_close IS NULL THEN
        RETURN 'close is null';
    END IF;
    IF p_close <= 0 THEN
        RETURN 'close <= 0';
    END IF;
    IF p_volume IS NOT NULL AND p_volume < 0 THEN
        RETURN 'volume < 0';
    END IF;

    -- Non-traded day: no volume and OHL are zero/absent. Close is a carried-forward
    -- reference price, so skip the range check (OHL are nulled on insert).
    non_traded := COALESCE(p_volume, 0) = 0
                  AND COALESCE(p_high, 0) = 0
                  AND COALESCE(p_low, 0) = 0
                  AND COALESCE(p_open, 0) = 0;
    IF non_traded THEN
        RETURN NULL;
    END IF;

    IF p_high IS NOT NULL AND p_low IS NOT NULL AND p_high < p_low THEN
        RETURN 'high < low';
    END IF;
    IF p_high IS NOT NULL AND p_low IS NOT NULL AND p_high > 0 AND p_low > 0 THEN
        IF p_close < p_low OR p_close > p_high THEN
            RETURN 'close outside [low, high]';
        END IF;
        IF p_open IS NOT NULL AND p_open > 0 AND (p_open < p_low OR p_open > p_high) THEN
            RETURN 'open outside [low, high]';
        END IF;
    END IF;
    RETURN NULL;  -- clean
END;
$$;

-- ----------------------------------------------------------------------------
-- 6) PRICES_ADJUSTED — DERIVED view, backward-adjusted close for corp actions.
--    A price on trade_date is divided by the cumulative split/bonus factor of
--    all actions with ex_date AFTER that trade_date, so the most recent bar is
--    unadjusted and history is scaled to be continuous.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW research.prices_adjusted AS
SELECT
    p.code,
    p.trade_date,
    p.knowledge_date,
    p.close                                   AS raw_close,
    p.close / COALESCE(f.cum_factor, 1)       AS adj_close,
    COALESCE(f.cum_factor, 1)                 AS cum_factor
FROM research.prices_pit p
LEFT JOIN LATERAL (
    SELECT exp(sum(ln(ca.ratio))) AS cum_factor
    FROM research.corporate_actions ca
    WHERE ca.code = p.code
      AND ca.action_type IN ('split','reverse_split','bonus','rights')
      AND ca.ratio IS NOT NULL
      AND ca.ratio > 0
      AND ca.ex_date > p.trade_date
) f ON true;

-- ----------------------------------------------------------------------------
-- 7) LATEST_PIT — convenience view: the newest knowledge_date per (code, trade_date).
--    This is the "current best knowledge" snapshot for normal (non-backtest) use.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW research.latest_pit AS
SELECT DISTINCT ON (code, trade_date)
    code, trade_date, knowledge_date, open, high, low, close, volume, value
FROM research.prices_pit
ORDER BY code, trade_date, knowledge_date DESC;

-- ----------------------------------------------------------------------------
-- 8) BROKER_DAILY — broker summary EOD (bandarmologi).
--    Agregat transaksi PER BROKER FIRM seluruh pasar dari
--    /primary/TradingSummary/GetBrokerSummary (IDX tidak menyediakan
--    breakdown per saham di endpoint publik; flow per emiten lewat faktor
--    order-book dari snapshot intraday). Upsert idempoten per tanggal.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research.broker_daily (
    trade_date  DATE        NOT NULL,
    broker_code TEXT        NOT NULL,
    broker_name TEXT,
    volume      BIGINT,
    value       NUMERIC(24,4),
    frequency   BIGINT,
    source      TEXT        NOT NULL DEFAULT 'IDX',
    captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (trade_date, broker_code)
);
CREATE INDEX IF NOT EXISTS idx_broker_daily_broker
    ON research.broker_daily (broker_code, trade_date);
