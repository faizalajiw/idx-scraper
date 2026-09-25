-- ============================================================================
-- RESEARCH EOD META (Opsi A) — make research.* a lossless superset of the old
-- public.stock_summary_daily so the dashboard can be rewired without losing
-- stock names or foreign-flow data.
-- ============================================================================
-- Adds the 4 columns the research layer was missing (name + foreign buy/sell/net)
-- to raw_eod (as-reported truth) and prices_pit (bitemporal snapshot), plus
-- prev_close on prices_pit so change/percent can be derived in latest_pit.
-- change/percent are DERIVED in the view (never stored) from prev_close.
-- Idempotent: safe to re-run.
-- ============================================================================

-- 1) raw_eod: as-reported name + foreign flow (shares, as IDX publishes them)
ALTER TABLE research.raw_eod
    ADD COLUMN IF NOT EXISTS name         TEXT,
    ADD COLUMN IF NOT EXISTS foreign_buy  NUMERIC(24,4),
    ADD COLUMN IF NOT EXISTS foreign_sell NUMERIC(24,4),
    ADD COLUMN IF NOT EXISTS foreign_net  NUMERIC(24,4);

-- 2) prices_pit: carry name, prev_close, foreign flow into the PIT snapshot
ALTER TABLE research.prices_pit
    ADD COLUMN IF NOT EXISTS name         TEXT,
    ADD COLUMN IF NOT EXISTS prev_close   NUMERIC(18,4),
    ADD COLUMN IF NOT EXISTS foreign_buy  NUMERIC(24,4),
    ADD COLUMN IF NOT EXISTS foreign_sell NUMERIC(24,4),
    ADD COLUMN IF NOT EXISTS foreign_net  NUMERIC(24,4);

-- 3) latest_pit: rebuild to expose name, prev_close, derived change/percent,
--    and foreign flow. DROP first because column set changes shape.
DROP VIEW IF EXISTS research.latest_pit;
CREATE VIEW research.latest_pit AS
SELECT DISTINCT ON (code, trade_date)
    code,
    trade_date,
    knowledge_date,
    name,
    open, high, low, close, prev_close,
    (close - prev_close)                                            AS change,
    CASE WHEN prev_close > 0
         THEN round((close - prev_close) / prev_close * 100, 4)
         END                                                        AS percent,
    volume, value,
    foreign_buy, foreign_sell, foreign_net
FROM research.prices_pit
ORDER BY code, trade_date, knowledge_date DESC;
