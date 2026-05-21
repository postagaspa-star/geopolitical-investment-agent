-- ============================================================================
-- add_short_direction.sql — abilita le posizioni SHORT (vendite allo scoperto)
-- ============================================================================
--
-- COSA FA:
--   Aggiunge la colonna `direction` ('LONG' | 'SHORT') alle tabelle
--   `positions` e `trades`. È il TAG che distingue le posizioni/operazioni
--   long da quelle short.
--
--   - direction = 'LONG'  → posizione/operazione classica (default).
--   - direction = 'SHORT' → vendita allo scoperto: si guadagna se il
--     prezzo SCENDE.
--
--   Tutte le righe esistenti ricevono 'LONG' (corretto: finora il bot ha
--   operato solo long).
--
-- QUANDO ESEGUIRLA:
--   PRIMA che parta lo Stadio 2 dello short selling (quando il bot inizia
--   davvero ad aprire short). Per lo Stadio 1 il codice è resiliente: se
--   la colonna manca, scrive comunque i trade long senza il tag — ma il
--   tag serve, quindi esegui questa migration appena possibile.
--
-- COME ESEGUIRE:
--   Supabase Dashboard → SQL Editor → New query → incolla → Run.
--   Idempotente (ADD COLUMN IF NOT EXISTS): rieseguibile senza danni.
--
-- ROLLBACK:
--   ALTER TABLE positions DROP COLUMN IF EXISTS direction;
--   ALTER TABLE trades    DROP COLUMN IF EXISTS direction;
-- ============================================================================

ALTER TABLE positions ADD COLUMN IF NOT EXISTS direction TEXT NOT NULL DEFAULT 'LONG';
ALTER TABLE trades    ADD COLUMN IF NOT EXISTS direction TEXT NOT NULL DEFAULT 'LONG';

-- Backfill di sicurezza (in caso la colonna fosse stata creata NULLable):
UPDATE positions SET direction = 'LONG' WHERE direction IS NULL;
UPDATE trades    SET direction = 'LONG' WHERE direction IS NULL;

-- Indice per filtrare velocemente le posizioni short.
CREATE INDEX IF NOT EXISTS idx_positions_direction ON positions(direction);
CREATE INDEX IF NOT EXISTS idx_trades_direction    ON trades(direction);

-- ============================================================================
-- VERIFICA (incolla dopo il Run):
--   SELECT direction, COUNT(*) FROM positions GROUP BY direction;
--   SELECT direction, COUNT(*) FROM trades    GROUP BY direction;
-- ============================================================================
