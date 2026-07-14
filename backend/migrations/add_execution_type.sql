-- ============================================================================
-- add_execution_type.sql — origine dei trade + igiene confidence
-- (Step 6 analisi 13/07)
-- ============================================================================
--
-- COSA FA:
--   1. Aggiunge la colonna `execution_type` alla tabella `trades`: chi ha
--      originato il trade.
--        'ai'            → decisione LLM (Decision Standard/Crypto)
--        'scalper' | 'orchestrator' | 'airbag' → agenti dedicati
--        'partial_sl' | 'partial_tp' | 'stop_enforced' | 'auto_exit'
--                        → esecuzioni meccaniche (stop/take profit)
--        'fail_closed'   → chiusura di sicurezza (SL non settabile)
--        'liquidation'   → circuit breaker
--      Prima erano distinguibili solo dai tag testuali infilati in
--      geopolitical_reasoning, e i marcatori 0/100 inquinavano ogni
--      analisi della confidence.
--   2. Backfill dello storico con l'euristica sui tag testuali.
--   3. Ripara le confidence salvate in scala frazionaria (es. 0.72
--      invece di 72).
--
-- QUANDO ESEGUIRLA:
--   PRIMA del deploy del branch feat/sim-live-hardening. Il codice è
--   comunque resiliente: se la colonna manca, insert_trade riprova
--   senza (i trade non si perdono, restano solo non tipizzati).
--
-- COME ESEGUIRE:
--   Supabase Dashboard → SQL Editor → New query → incolla → Run.
--   Idempotente: rieseguibile senza danni (il backfill tocca solo
--   righe con execution_type NULL).
--
-- ROLLBACK:
--   ALTER TABLE trades DROP COLUMN IF EXISTS execution_type;
--   (la normalizzazione confidence non è reversibile, ma corregge
--    solo valori palesemente errati: frazioni 0-1 su scala 0-100)
-- ============================================================================

ALTER TABLE trades ADD COLUMN IF NOT EXISTS execution_type TEXT;

-- Backfill storico dai tag testuali (solo righe non ancora tipizzate):
UPDATE trades SET execution_type = CASE
  WHEN geopolitical_reasoning LIKE 'FAIL-CLOSED%'               THEN 'fail_closed'
  WHEN geopolitical_reasoning = 'stop_enforced'                 THEN 'stop_enforced'
  WHEN geopolitical_reasoning = 'partial_sl'                    THEN 'partial_sl'
  WHEN geopolitical_reasoning = 'partial_tp'                    THEN 'partial_tp'
  WHEN geopolitical_reasoning LIKE 'auto_stop_loss%'
    OR geopolitical_reasoning LIKE 'auto_take_profit%'          THEN 'auto_exit'
  WHEN geopolitical_reasoning LIKE 'CIRCUIT_BREAKER_LIQUIDATE%' THEN 'liquidation'
  WHEN geopolitical_reasoning = '(scalper)'                     THEN 'scalper'
  WHEN geopolitical_reasoning LIKE '[ORCHESTRATOR]%'            THEN 'orchestrator'
  WHEN geopolitical_reasoning = '(airbag deterministico)'       THEN 'airbag'
  ELSE 'ai' END
WHERE execution_type IS NULL;

-- Igiene confidence: frazioni 0-1 salvate per errore → scala 0-100
-- (caso reale: COVER NEAR-USD dell'08/07 con confidence 0.72)
UPDATE trades SET confidence_score = ROUND(confidence_score * 100)
WHERE confidence_score > 0 AND confidence_score <= 1;

CREATE INDEX IF NOT EXISTS idx_trades_execution_type ON trades(execution_type);

-- ============================================================================
-- VERIFICA (incolla dopo il Run):
--   SELECT execution_type, COUNT(*) FROM trades GROUP BY execution_type;
--   SELECT COUNT(*) FROM trades WHERE confidence_score > 0 AND confidence_score <= 1;
--   -- atteso: 0
-- ============================================================================
