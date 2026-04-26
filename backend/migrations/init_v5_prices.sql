-- init_v5_prices.sql
-- Real-time price cache for v5 architecture.
-- Updated every 60 seconds during market hours by price_polling.py

-- ============================================================
-- price_quotes — current price snapshot per ticker
-- ============================================================
CREATE TABLE IF NOT EXISTS price_quotes (
    ticker TEXT PRIMARY KEY,
    price DOUBLE PRECISION NOT NULL,
    prev_close DOUBLE PRECISION,
    change_pct DOUBLE PRECISION DEFAULT 0,
    volume BIGINT DEFAULT 0,
    day_high DOUBLE PRECISION,
    day_low DOUBLE PRECISION,
    market_state TEXT DEFAULT 'REGULAR',  -- REGULAR/PRE/POST/CLOSED
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    source TEXT DEFAULT 'yfinance'
);

CREATE INDEX IF NOT EXISTS idx_pq_updated ON price_quotes(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_pq_market_state ON price_quotes(market_state);

-- ============================================================
-- price_history — minute-by-minute history (last 24h)
-- For backtest / chart display / volatility analysis
-- ============================================================
CREATE TABLE IF NOT EXISTS price_history (
    id BIGSERIAL PRIMARY KEY,
    ticker TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    price DOUBLE PRECISION NOT NULL,
    volume BIGINT DEFAULT 0,
    UNIQUE (ticker, timestamp)
);

CREATE INDEX IF NOT EXISTS idx_ph_ticker_ts ON price_history(ticker, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_ph_timestamp ON price_history(timestamp DESC);

-- Auto-cleanup history older than 24h (executed periodically by scheduler)
-- Using CREATE FUNCTION with DROP first for idempotency
DROP FUNCTION IF EXISTS cleanup_old_price_history();
CREATE FUNCTION cleanup_old_price_history() RETURNS void AS $$
BEGIN
    DELETE FROM price_history WHERE timestamp < now() - INTERVAL '24 hours';
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- RLS policies
-- ============================================================
DO $$
DECLARE tbl TEXT;
BEGIN
    FOR tbl IN SELECT unnest(ARRAY['price_quotes', 'price_history'])
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', tbl);
        EXECUTE format('DROP POLICY IF EXISTS "allow_all_%s" ON %I', tbl, tbl);
        EXECUTE format('CREATE POLICY "allow_all_%s" ON %I FOR ALL USING (true) WITH CHECK (true)', tbl, tbl);
    END LOOP;
END $$;
