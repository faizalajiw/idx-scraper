-- ============================================================================
-- POINT-IN-TIME QUERY FUNCTIONS (Phase 4) — as-of, look-ahead-free access
-- ============================================================================
-- These are the read primitives a backtest uses. The golden rule: a simulation
-- standing at knowledge_date T may only see data that was known at T.
--
--   * prices_asof(T)      -> latest EOD per (code, trade_date) with
--                            knowledge_date <= T   (bitemporal cut)
--   * prices_asof_adj(T)  -> same, plus backward split/bonus adjustment using
--                            only corp actions known at T (ingested_at <= T)
--
-- raw_eod stays the truth; everything here is derived on the fly.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- prices_asof: bitemporal cut. For each (code, trade_date) return the row with
-- the newest knowledge_date that is still <= the as-of instant. Rows we hadn't
-- learned yet at T are invisible -> no look-ahead.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION research.prices_asof(p_knowledge TIMESTAMPTZ)
RETURNS TABLE (
    code           TEXT,
    trade_date     DATE,
    knowledge_date TIMESTAMPTZ,
    open           NUMERIC(18,4),
    high           NUMERIC(18,4),
    low            NUMERIC(18,4),
    close          NUMERIC(18,4),
    volume         BIGINT,
    value          NUMERIC(24,4)
)
LANGUAGE sql STABLE AS $$
    SELECT DISTINCT ON (p.code, p.trade_date)
        p.code, p.trade_date, p.knowledge_date,
        p.open, p.high, p.low, p.close, p.volume, p.value
    FROM research.prices_pit p
    WHERE p.knowledge_date <= p_knowledge
    ORDER BY p.code, p.trade_date, p.knowledge_date DESC;
$$;

-- ----------------------------------------------------------------------------
-- prices_asof_adj: as-of prices with backward corporate-action adjustment,
-- honoring knowledge on the corp actions too (ingested_at <= T). A split we
-- hadn't recorded yet at T does not retroactively adjust the past.
--   adj_close = raw_close / (product of ratios for ex_date > trade_date)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION research.prices_asof_adj(p_knowledge TIMESTAMPTZ)
RETURNS TABLE (
    code           TEXT,
    trade_date     DATE,
    knowledge_date TIMESTAMPTZ,
    raw_close      NUMERIC(18,4),
    adj_close      NUMERIC(18,4),
    cum_factor     NUMERIC(18,8),
    volume         BIGINT
)
LANGUAGE sql STABLE AS $$
    SELECT
        b.code, b.trade_date, b.knowledge_date,
        b.close                              AS raw_close,
        b.close / COALESCE(f.cum_factor, 1)  AS adj_close,
        COALESCE(f.cum_factor, 1)            AS cum_factor,
        b.volume
    FROM research.prices_asof(p_knowledge) b
    LEFT JOIN LATERAL (
        SELECT exp(sum(ln(ca.ratio))) AS cum_factor
        FROM research.corporate_actions ca
        WHERE ca.code = b.code
          AND ca.action_type IN ('split','reverse_split','bonus','rights')
          AND ca.ratio IS NOT NULL AND ca.ratio > 0
          AND ca.ex_date > b.trade_date
          AND ca.ingested_at <= p_knowledge
    ) f ON true;
$$;
