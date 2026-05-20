-- ============================================================================
-- enable_rls.sql — chiude l'alert Supabase "rls_disabled_in_public"
-- ============================================================================
--
-- COSA FA:
--   Abilita Row Level Security su TUTTE le tabelle pubbliche di GeoInvest.
--   Nessuna policy → default deny per anon/authenticated. Solo la
--   service_role key può leggere/scrivere (bypassa sempre RLS by design).
--
-- PERCHÉ E' SICURO PER QUESTA APP:
--   Il backend FastAPI usa SUPABASE_KEY = service_role (segreta, vive solo
--   su Render). Il frontend React parla SOLO con FastAPI, non direttamente
--   con Supabase: quindi l'anon key NON è esposta in nessun bundle pubblico.
--   Con questa migration:
--     - Backend (service_role): continua a funzionare normalmente.
--     - Anon key (anche se esposta): non legge/scrive piu' nulla.
--
-- PRECONDIZIONE — VERIFICA PRIMA DI ESEGUIRE:
--   Su Render → Environment → SUPABASE_KEY deve essere la service_role
--   key (non l'anon). Decodifica su jwt.io: claim "role" deve essere
--   "service_role". Se è "anon", PRIMA sostituiscila con la service_role
--   (Supabase Dashboard → Project Settings → API → service_role secret),
--   POI esegui questa migration.
--
-- COME ESEGUIRE:
--   Supabase Dashboard → SQL Editor → New query → incolla questo file
--   intero → Run. Idempotente: rieseguibile senza danni.
--
-- ROLLBACK:
--   Per ogni tabella: ALTER TABLE public.<nome> DISABLE ROW LEVEL SECURITY;
-- ============================================================================

-- Portfolio & trading state
ALTER TABLE IF EXISTS public.portfolio              ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.portfolio_snapshots    ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.positions              ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.trades                 ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.trades_high_risk       ENABLE ROW LEVEL SECURITY;

-- Agent execution & memory
ALTER TABLE IF EXISTS public.agent_logs             ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.agent_checkpoints      ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.agent_commitments      ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.geopolitical_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.processed_articles     ENABLE ROW LEVEL SECURITY;

-- Intelligence cascade (L0/8H/4D)
ALTER TABLE IF EXISTS public.intelligence_buffer    ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.daily_snapshots        ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.weekly_matrix          ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.weekend_intelligence   ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.pre_market_briefings   ENABLE ROW LEVEL SECURITY;

-- Chat (analista DeepSeek-R1 + decision-chat)
ALTER TABLE IF EXISTS public.chat_conversations           ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.chat_messages                ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.decision_chat_conversations  ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.decision_chat_messages       ENABLE ROW LEVEL SECURITY;

-- Documenti e impostazioni
ALTER TABLE IF EXISTS public.technical_documents    ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.settings               ENABLE ROW LEVEL SECURITY;

-- Price polling (live quotes + storico minutale)
ALTER TABLE IF EXISTS public.price_quotes           ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.price_history          ENABLE ROW LEVEL SECURITY;

-- Simulator
ALTER TABLE IF EXISTS public.sim_runs               ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.sim_settings           ENABLE ROW LEVEL SECURITY;

-- ============================================================================
-- VERIFICA POST-MIGRATION (incolla nella stessa SQL Editor dopo il Run):
-- ============================================================================
-- SELECT schemaname, tablename, rowsecurity
-- FROM pg_tables
-- WHERE schemaname = 'public'
-- ORDER BY tablename;
--
-- Tutte le righe devono mostrare rowsecurity = true.
-- ============================================================================
