-- ============================================================================
-- create_sim_runs.sql — CREA LA TABELLA sim_runs SU SUPABASE
-- ============================================================================
-- PERCHÉ SERVE:
--   Su questo Supabase la tabella `sim_runs` NON è mai stata creata:
--   l'auto-migration del backend richiede DATABASE_URL / SUPABASE_DB_PASSWORD
--   che su Render non sono configurate, quindi ogni run del Simulator è
--   sempre finita nel fallback key-value dentro `sim_settings`
--   (chiavi `_sim_run_fallback::*`). Quel fallback:
--     - ha un cap (i run più vecchi vengono cancellati al superamento) →
--       è la "soglia oltre cui il database si svuota";
--     - è stato azzerato dal purge di cleanup_settings_bloat.sql (lo storico
--       pre-giugno è andato perso così).
--   Con la tabella vera: persistenza illimitata, query per categoria/data,
--   nessun purge che la tocca.
--
-- DOPO AVERLA CREATA: riavvia il servizio Render (Manual Deploy → "Restart
--   service" oppure il prossimo deploy). Al boot il backend migra da solo
--   dentro sim_runs tutti i run trovati nel fallback — scansiona le chiavi
--   `_sim_run_fallback::run::*` sia in sim_settings sia nella settings Live
--   (recupera anche eventuali orfani pre-migrazione). Non serve fare altro.
--
-- COME ESEGUIRE: Supabase Dashboard → SQL Editor → incolla tutto → Run.
--   Idempotente: ri-eseguibile senza danni.
-- ============================================================================

CREATE TABLE IF NOT EXISTS sim_runs (
    id TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    mode TEXT DEFAULT 'manual',
    category TEXT NOT NULL,
    scenario_type TEXT NOT NULL,
    steps INTEGER DEFAULT 1,
    scenario_id TEXT NOT NULL,
    historical_period TEXT,
    asset_chosen TEXT,
    action_chosen TEXT,
    conviction TEXT,
    horizon TEXT,
    perf_1w DOUBLE PRECISION,
    perf_1m DOUBLE PRECISION,
    perf_3m DOUBLE PRECISION,
    perf_sp_1m DOUBLE PRECISION,
    perf_sector_1m DOUBLE PRECISION,
    perf_monkey_1m DOUBLE PRECISION,
    delta_sp DOUBLE PRECISION,
    delta_sector DOUBLE PRECISION,
    delta_monkey DOUBLE PRECISION,
    outcome TEXT,
    original_thesis TEXT,
    what_happened TEXT,
    thesis_evaluation TEXT,
    full_data JSONB
);

CREATE INDEX IF NOT EXISTS idx_sim_runs_category  ON sim_runs(category);
CREATE INDEX IF NOT EXISTS idx_sim_runs_completed ON sim_runs(completed_at DESC);
CREATE INDEX IF NOT EXISTS idx_sim_runs_mode_created ON sim_runs(mode, created_at DESC);

-- RLS coerente col resto dello schema (il backend usa la service key → bypass)
ALTER TABLE IF EXISTS sim_runs ENABLE ROW LEVEL SECURITY;

-- VERIFICA (opzionale): dopo il Run, questa deve restituire la tabella vuota
-- senza errori:
--   SELECT count(*) FROM sim_runs;
