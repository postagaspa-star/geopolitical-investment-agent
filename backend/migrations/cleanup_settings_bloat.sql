-- ============================================================================
-- cleanup_settings_bloat.sql
-- ============================================================================
-- PROBLEMA:
--   La tabella `settings` (key-value) e' cresciuta oltre 1000 righe perche'
--   stato TRANSITORIO vi e' stato serializzato come fallback quando le tabelle
--   dedicate mancavano su questo Supabase:
--     - _chat_fallback::*        → storia chat (manca chat_conversations/messages)
--     - _sim_run_progress::*     → progress run simulatore (effimero, TTL 1-15min)
--     - _sim_run_fallback::*     → run simulatore (manca sim_runs)
--     - _sim_scenario::*         → scenari dinamici salvati
--   Oltre le 1000 righe, PostgREST TRONCA silenziosamente le select non
--   paginate: GET /api/settings restituiva solo le prime 1000 chiavi, rendendo
--   invisibili impostazioni legittime appena salvate (es. crypto_decision_engine).
--
-- QUESTA MIGRATION FA DUE COSE:
--   (A) ROOT CAUSE — crea le tabelle mancanti, cosi' il fallback su `settings`
--       smette di attivarsi e il bloat non si rigenera.
--   (B) PURGE — elimina lo stato transitorio EFFIMERO gia' accumulato.
--
-- SICUREZZA:
--   - Idempotente (CREATE TABLE IF NOT EXISTS).
--   - Il PURGE di default tocca SOLO _sim_run_progress::* e _sim_run_fallback::*
--     (rigenerabili, gia' a TTL). NON tocca _chat_fallback::* di default:
--     potrebbe essere la TUA storia chat. Vedi blocco OPZIONALE in fondo.
--   - Dopo aver creato le tabelle (A), la chat/sim NUOVA andra' nelle tabelle
--     giuste; quella vecchia nel fallback resta leggibile finche' non la migri
--     o la elimini consapevolmente.
--
-- COME ESEGUIRE: Supabase Dashboard → SQL Editor → incolla tutto → Run.
-- ============================================================================

-- ─────────────────────────────────────────────────────────────────────────
-- (A) ROOT CAUSE: tabelle dedicate mancanti
-- ─────────────────────────────────────────────────────────────────────────

-- Chat analista (DeepSeek-R1)
CREATE TABLE IF NOT EXISTS chat_conversations (
    id BIGSERIAL PRIMARY KEY,
    title TEXT NOT NULL DEFAULT 'Nuova conversazione',
    selected_decisions TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id BIGSERIAL PRIMARY KEY,
    conversation_id BIGINT NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_conv
    ON chat_messages (conversation_id, created_at);

-- Decision chat (chat con i Decision Agent)
CREATE TABLE IF NOT EXISTS decision_chat_conversations (
    id BIGSERIAL PRIMARY KEY,
    agent_type TEXT NOT NULL DEFAULT 'standard' CHECK (agent_type IN ('standard','crypto')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS decision_chat_messages (
    id BIGSERIAL PRIMARY KEY,
    conversation_id BIGINT NOT NULL REFERENCES decision_chat_conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
    content TEXT NOT NULL,
    proposed_trade TEXT,
    executed_trade_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_dec_chat_messages_conv
    ON decision_chat_messages (conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_dec_chat_conv_agent
    ON decision_chat_conversations (agent_type, updated_at DESC);

-- Simulator: tabella key-value dedicata (separa lo stato sim dal settings Live)
CREATE TABLE IF NOT EXISTS sim_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- RLS coerente con enable_rls.sql (il backend usa service_role → bypass)
ALTER TABLE IF EXISTS chat_conversations          ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS chat_messages               ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS decision_chat_conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS decision_chat_messages      ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS sim_settings                ENABLE ROW LEVEL SECURITY;

-- ─────────────────────────────────────────────────────────────────────────
-- (B) PURGE dello stato transitorio EFFIMERO gia' accumulato in `settings`
--     (default sicuro: solo progress/fallback run del simulatore)
-- ─────────────────────────────────────────────────────────────────────────

-- Conteggio PRIMA (diagnostica — guarda l'output prima/dopo)
SELECT 'before' AS phase,
       count(*) FILTER (WHERE key LIKE '_sim_run_progress::%')  AS sim_progress,
       count(*) FILTER (WHERE key LIKE '_sim_run_fallback::%')  AS sim_fallback,
       count(*) FILTER (WHERE key LIKE '_chat_fallback::%')     AS chat_fallback,
       count(*) FILTER (WHERE key LIKE '_sim_scenario::%')      AS sim_scenario,
       count(*) AS total_rows
FROM settings;

DELETE FROM settings WHERE key LIKE '_sim_run_progress::%';
DELETE FROM settings WHERE key LIKE '_sim_run_fallback::%';

-- Conteggio DOPO
SELECT 'after' AS phase, count(*) AS total_rows FROM settings;

-- ============================================================================
-- (B-bis) OPZIONALE — DISTRUTTIVO. Scommenta SOLO se vuoi eliminare anche la
--   vecchia storia chat e gli scenari dinamici serializzati nel fallback.
--   ATTENZIONE: _chat_fallback::* e' la TUA storia chat (se le tabelle chat
--   non esistevano). Dopo aver eseguito (A), valuta se prima vuoi migrarla.
-- ----------------------------------------------------------------------------
-- DELETE FROM settings WHERE key LIKE '_chat_fallback::%';
-- DELETE FROM settings WHERE key LIKE '_dec_chat_fallback::%';
-- DELETE FROM settings WHERE key LIKE '_sim_scenario::%';
-- ============================================================================

-- VERIFICA: nessuna chiave di config reale contiene "::", quindi questa query
-- deve restituire SOLO eventuali residui transitori (idealmente 0 dopo il purge):
-- SELECT key FROM settings WHERE key LIKE '%::%' ORDER BY key;
