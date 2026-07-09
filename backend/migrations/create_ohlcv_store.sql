-- ============================================================================
-- create_ohlcv_store.sql — magazzino OHLCV pre-caricato (Approach 1)
-- ============================================================================
-- PERCHÉ: il technical scarica OHLCV di molti ticker in parallelo durante il
--   run del Decision → i provider equity vanno in rate-limit silenzioso e
--   restituiscono barre corrotte (identiche) → i ticker vengono scartati e il
--   campione investibile crolla. Con questo magazzino, un job in background
--   tiene i dati freschi (un ticker alla volta, con pause) e il Decision
--   legge da qui: niente raffica, niente corruzione.
--
-- COME ESEGUIRE: Supabase Dashboard → SQL Editor → incolla ed esegui.
--   Idempotente. Dopo: imposta su Render la env var OHLCV_STORE_ENABLED=true
--   per attivare (di default è spento e non cambia nulla).
-- ============================================================================

CREATE TABLE IF NOT EXISTS ohlcv_daily (
    ticker     TEXT NOT NULL,
    date       TEXT NOT NULL,                 -- YYYY-MM-DD (EOD daily bar)
    open       DOUBLE PRECISION,
    high       DOUBLE PRECISION,
    low        DOUBLE PRECISION,
    close      DOUBLE PRECISION,
    volume     BIGINT,
    source     TEXT,                          -- provider che ha fornito la barra
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ticker, date)
);

-- Lettura tipica: barre di UN ticker, più recenti prima.
CREATE INDEX IF NOT EXISTS idx_ohlcv_ticker_date
    ON ohlcv_daily (ticker, date DESC);
-- Diagnostica freschezza (quali ticker aggiornati di recente).
CREATE INDEX IF NOT EXISTS idx_ohlcv_updated
    ON ohlcv_daily (updated_at DESC);

ALTER TABLE IF EXISTS ohlcv_daily ENABLE ROW LEVEL SECURITY;

-- VERIFICA (opzionale): dopo il Run deve restituire 0 senza errori.
--   SELECT count(*) FROM ohlcv_daily;
