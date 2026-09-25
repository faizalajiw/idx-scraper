-- ============================================================================
-- INTRADAY SCHEMA (Phase 3) — native monthly-partitioned tick store + 5m bars
-- ============================================================================
-- Runs alongside EOD research tables. Captures live snapshots of the top-N
-- liquid emiten during market hours, one IDX request per poll (GetStockSummary
-- returns the whole market at once). 5-minute OHLC bars are derived via VIEW.
--
-- Design:
--   * intraday_ticks is RANGE-partitioned by ts (monthly). PK includes ts.
--   * `last` is the running last-traded price; `volume`/`value` are cumulative
--     day-to-date at snapshot time (so bar volume = max - min within a bucket).
--   * Bars are a VIEW (derived), never stored — same raw-is-truth principle.
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS research;

-- ----------------------------------------------------------------------------
-- Partitioned tick store
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research.intraday_ticks (
    code    TEXT        NOT NULL,
    ts      TIMESTAMPTZ NOT NULL,
    last    NUMERIC(18,4),
    volume  BIGINT,          -- cumulative day volume at snapshot
    value   NUMERIC(24,4),   -- cumulative day value at snapshot
    PRIMARY KEY (code, ts)
) PARTITION BY RANGE (ts);

CREATE INDEX IF NOT EXISTS idx_intraday_ticks_ts
    ON research.intraday_ticks (ts);

-- ----------------------------------------------------------------------------
-- Helper: create the monthly partition covering a given date (idempotent)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION research.ensure_intraday_partition(p_day DATE)
RETURNS TEXT
LANGUAGE plpgsql AS $$
DECLARE
    m_start DATE := date_trunc('month', p_day)::date;
    m_end   DATE := (date_trunc('month', p_day) + interval '1 month')::date;
    part    TEXT := format('intraday_ticks_%s', to_char(m_start, 'YYYY_MM'));
BEGIN
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS research.%I PARTITION OF research.intraday_ticks '
        'FOR VALUES FROM (%L) TO (%L)',
        part, m_start, m_end
    );
    RETURN part;
END;
$$;

-- Seed current + next month so captures never hit a missing-partition error.
SELECT research.ensure_intraday_partition(current_date);
SELECT research.ensure_intraday_partition((date_trunc('month', current_date) + interval '1 month')::date);

-- ----------------------------------------------------------------------------
-- 5-minute OHLC bars, derived from ticks (WIB bucketing)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW research.intraday_bars_5m AS
SELECT
    code,
    to_timestamp(floor(extract(epoch FROM ts) / 300) * 300) AS bucket_start,
    (array_agg(last ORDER BY ts))[1]                        AS open,
    max(last)                                               AS high,
    min(last)                                               AS low,
    (array_agg(last ORDER BY ts DESC))[1]                   AS close,
    greatest(max(volume) - min(volume), 0)                  AS volume,
    count(*)                                                AS ticks
FROM research.intraday_ticks
WHERE last IS NOT NULL
GROUP BY code, bucket_start;
