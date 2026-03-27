-- init_v4.sql
-- Multi-agent v4 architecture: new tables only
-- Existing tables (portfolio, positions, trades, agent_logs, etc.) are untouched.

-- ============================================================
-- 1. intelligence_buffer - 20-minute intelligence stream
-- ============================================================
CREATE TABLE IF NOT EXISTS intelligence_buffer (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_type TEXT NOT NULL,        -- 'GDELT', 'NEWSAPI', 'CLAWSTREET', 'FINNHUB'
    raw_content TEXT,
    micro_summary TEXT,
    sentiment_score DOUBLE PRECISION DEFAULT 0,  -- -1 to 1
    processed BOOLEAN DEFAULT false,
    run_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_ib_timestamp ON intelligence_buffer(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_ib_source ON intelligence_buffer(source_type);
CREATE INDEX IF NOT EXISTS idx_ib_processed ON intelligence_buffer(processed);

-- ============================================================
-- 2. daily_snapshots - 23:59 daily recaps
-- ============================================================
CREATE TABLE IF NOT EXISTS daily_snapshots (
    date DATE PRIMARY KEY,
    summary_text TEXT NOT NULL,
    key_events JSONB DEFAULT '[]',
    macro_bias TEXT DEFAULT 'NEUTRAL',  -- BULLISH/BEARISH/NEUTRAL
    hot_tickers TEXT[] DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- 3. weekly_matrix - Sunday recaps
-- ============================================================
CREATE TABLE IF NOT EXISTS weekly_matrix (
    week_id TEXT PRIMARY KEY,           -- format: '2026-W13'
    synthesis TEXT NOT NULL,
    long_term_risks TEXT,
    sector_rotation_signals JSONB DEFAULT '{}',
    macro_strategy TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- 4. trades_high_risk - High-risk trade log with chain of thought
-- ============================================================
CREATE TABLE IF NOT EXISTS trades_high_risk (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT now(),
    ticker TEXT NOT NULL,
    action TEXT NOT NULL,               -- BUY/SELL
    entry_price DOUBLE PRECISION NOT NULL,
    quantity INTEGER NOT NULL,
    logic_chain TEXT,                    -- Opus chain of thought
    stop_loss DOUBLE PRECISION,
    take_profit DOUBLE PRECISION,
    confidence_level DOUBLE PRECISION DEFAULT 0,
    run_id TEXT,
    status TEXT DEFAULT 'OPEN'          -- OPEN/CLOSED/STOPPED
);

CREATE INDEX IF NOT EXISTS idx_thr_ticker ON trades_high_risk(ticker);
CREATE INDEX IF NOT EXISTS idx_thr_status ON trades_high_risk(status);

-- ============================================================
-- 5. agent_checkpoints - For Render resilience
-- ============================================================
CREATE TABLE IF NOT EXISTS agent_checkpoints (
    run_id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    status TEXT DEFAULT 'RUNNING',      -- RUNNING/COMPLETED/FAILED/INTERRUPTED
    checkpoint_data JSONB DEFAULT '{}',
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_ac_status ON agent_checkpoints(status);

-- ============================================================
-- Enable RLS on all new tables with permissive policies
-- Uses DROP POLICY IF EXISTS + CREATE POLICY (no IF NOT EXISTS)
-- ============================================================
DO $$
DECLARE tbl TEXT;
BEGIN
    FOR tbl IN SELECT unnest(ARRAY[
        'intelligence_buffer','daily_snapshots','weekly_matrix',
        'trades_high_risk','agent_checkpoints'
    ])
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', tbl);
        EXECUTE format('DROP POLICY IF EXISTS "allow_all_%s" ON %I', tbl, tbl);
        EXECUTE format('CREATE POLICY "allow_all_%s" ON %I FOR ALL USING (true) WITH CHECK (true)', tbl, tbl);
    END LOOP;
END $$;

-- ============================================================
-- Extend existing agent_logs table with v4 columns
-- ============================================================
ALTER TABLE agent_logs ADD COLUMN IF NOT EXISTS agent_name TEXT DEFAULT 'legacy';
ALTER TABLE agent_logs ADD COLUMN IF NOT EXISTS step TEXT;
ALTER TABLE agent_logs ADD COLUMN IF NOT EXISTS thought_process TEXT;
