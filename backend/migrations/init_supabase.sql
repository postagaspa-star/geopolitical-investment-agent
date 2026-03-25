-- =============================================================
-- Schema Supabase per GeoInvest AI
-- Esegui questo SQL nella Supabase SQL Editor (Dashboard > SQL)
-- =============================================================

-- Portfolio (singleton — una sola riga)
CREATE TABLE IF NOT EXISTS portfolio (
    id BIGSERIAL PRIMARY KEY,
    cash_balance DOUBLE PRECISION NOT NULL,
    total_value DOUBLE PRECISION NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Posizioni aperte
CREATE TABLE IF NOT EXISTS positions (
    id BIGSERIAL PRIMARY KEY,
    ticker TEXT NOT NULL UNIQUE,
    quantity INTEGER NOT NULL,
    avg_buy_price DOUBLE PRECISION NOT NULL,
    current_price DOUBLE PRECISION NOT NULL DEFAULT 0,
    unrealized_pnl DOUBLE PRECISION NOT NULL DEFAULT 0,
    opened_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Trade eseguiti
CREATE TABLE IF NOT EXISTS trades (
    id BIGSERIAL PRIMARY KEY,
    ticker TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('BUY', 'SELL')),
    quantity INTEGER NOT NULL,
    price DOUBLE PRECISION NOT NULL,
    total_value DOUBLE PRECISION NOT NULL,
    geopolitical_reasoning TEXT,
    technical_reasoning TEXT,
    final_decision TEXT,
    confidence_score DOUBLE PRECISION,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Log dell'agente
CREATE TABLE IF NOT EXISTS agent_logs (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    content TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agent_logs_run_id ON agent_logs(run_id);
CREATE INDEX IF NOT EXISTS idx_agent_logs_timestamp ON agent_logs(timestamp DESC);

-- Snapshot geopolitici
CREATE TABLE IF NOT EXISTS geopolitical_snapshots (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('GDELT', 'NEWSAPI')),
    raw_data TEXT,
    processed_summary TEXT,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Impostazioni chiave-valore
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Documenti tecnici caricati
CREATE TABLE IF NOT EXISTS technical_documents (
    id BIGSERIAL PRIMARY KEY,
    filename TEXT NOT NULL,
    content TEXT NOT NULL,
    file_size INTEGER DEFAULT 0,
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Intelligence weekend
CREATE TABLE IF NOT EXISTS weekend_intelligence (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL,
    content TEXT NOT NULL,
    key_events TEXT,
    market_implications TEXT,
    saved_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Briefing pre-market
CREATE TABLE IF NOT EXISTS pre_market_briefings (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL,
    market_session TEXT,
    content TEXT NOT NULL,
    priority_assets TEXT,
    saved_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Articoli gia' processati (deduplicazione)
CREATE TABLE IF NOT EXISTS processed_articles (
    url TEXT PRIMARY KEY,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Snapshot portafoglio (storico equity curve)
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id BIGSERIAL PRIMARY KEY,
    total_value DOUBLE PRECISION NOT NULL,
    cash_balance DOUBLE PRECISION NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_ts ON portfolio_snapshots(timestamp DESC);

-- Buffer intelligence (per il sistema multi-agent)
CREATE TABLE IF NOT EXISTS intelligence_buffer (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    summary TEXT,
    risk_score INTEGER,
    raw_data JSONB,
    processed_articles_count INTEGER DEFAULT 0
);

-- Disabilita RLS su tutte le tabelle (accesso server-side con service_role key)
ALTER TABLE portfolio ENABLE ROW LEVEL SECURITY;
ALTER TABLE positions ENABLE ROW LEVEL SECURITY;
ALTER TABLE trades ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE geopolitical_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE technical_documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE weekend_intelligence ENABLE ROW LEVEL SECURITY;
ALTER TABLE pre_market_briefings ENABLE ROW LEVEL SECURITY;
ALTER TABLE processed_articles ENABLE ROW LEVEL SECURITY;
ALTER TABLE portfolio_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE intelligence_buffer ENABLE ROW LEVEL SECURITY;

-- Policy: service_role ha accesso completo (bypass automatico)
-- Per la anon key, creiamo policy permissive (il backend usa service_role)
DO $$
DECLARE
    tbl TEXT;
BEGIN
    FOR tbl IN
        SELECT unnest(ARRAY[
            'portfolio','positions','trades','agent_logs',
            'geopolitical_snapshots','settings','technical_documents',
            'weekend_intelligence','pre_market_briefings',
            'processed_articles','portfolio_snapshots','intelligence_buffer'
        ])
    LOOP
        EXECUTE format('CREATE POLICY IF NOT EXISTS "allow_all_%s" ON %I FOR ALL USING (true) WITH CHECK (true)', tbl, tbl);
    END LOOP;
END $$;

-- Inserisci portafoglio iniziale se vuoto
INSERT INTO portfolio (cash_balance, total_value)
SELECT 100000, 100000
WHERE NOT EXISTS (SELECT 1 FROM portfolio LIMIT 1);
