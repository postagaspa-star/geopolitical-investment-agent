-- Migrazione v6: tracking ClawStreet mirror status su trades
--
-- Esegui UNA VOLTA su Supabase (SQL Editor):
--   1. Apri https://supabase.com/dashboard/project/<your-project>/sql
--   2. Incolla tutto il contenuto sotto e clicca "Run"
--
-- Cosa fa: aggiunge 4 colonne alla tabella `trades` per tracciare se ogni
-- trade è stato specchiato con successo su ClawStreet. Il bot usa queste
-- colonne per:
--   - Sapere quali trade sono già su CS (cs_mirror_status='ok')
--   - Riprovare automaticamente quelli falliti (cs_mirror_status='failed')
--   - Identificare trade non specchiabili (cs_mirror_status='skipped')
--
-- Senza questa migrazione, il sistema cade in degrade graceful (try/except
-- nel db_supabase.py) ma non può fare retry automatici.

ALTER TABLE trades ADD COLUMN IF NOT EXISTS cs_mirror_status TEXT DEFAULT 'pending';
ALTER TABLE trades ADD COLUMN IF NOT EXISTS cs_mirror_reason TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS cs_mirror_attempts INTEGER DEFAULT 0;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS cs_mirror_last_attempt_at TIMESTAMPTZ;

-- Indice per query rapida dei pending (usato da get_pending_mirror_trades)
CREATE INDEX IF NOT EXISTS idx_trades_cs_mirror_status
  ON trades (cs_mirror_status, timestamp)
  WHERE cs_mirror_status IN ('pending', 'failed');

-- Backfill: i trade pre-esistenti hanno status NULL → li marchiamo 'unknown'
-- per non confonderli con i nuovi 'pending'. Il retry job li ignorerà.
UPDATE trades
   SET cs_mirror_status = 'legacy_unknown'
 WHERE cs_mirror_status IS NULL;
