-- Migration: cash_audit_log
-- Scopo: tracciare OGNI mutazione di cash_balance del portafoglio, cosi'
-- in caso di "cash che si aggiunge a caso" si vede subito quale path lo
-- ha fatto, quando, con quale delta e con quale motivo.
--
-- Va eseguita sul Supabase production via SQL editor. La funzione
-- insert_cash_audit_log nel backend e' fail-safe: se la tabella non
-- esiste il logging viene saltato e il trade procede comunque.
--
-- Idempotente: usa CREATE TABLE IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS cash_audit_log (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    delta DOUBLE PRECISION NOT NULL,
    old_cash DOUBLE PRECISION,
    new_cash DOUBLE PRECISION,
    reason TEXT NOT NULL,
    source TEXT,
    ticker TEXT,
    metadata TEXT
);

CREATE INDEX IF NOT EXISTS idx_cash_audit_timestamp
    ON cash_audit_log (timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_cash_audit_reason
    ON cash_audit_log (reason);

-- RLS: stessa policy delle altre tabelle interne. Tipicamente
-- service_role accede direttamente (nessun anon read).
-- Se enable_rls.sql e' gia' stato eseguito su questo Supabase,
-- aggiungi anche la riga seguente per coerenza:
--   ALTER TABLE cash_audit_log ENABLE ROW LEVEL SECURITY;
