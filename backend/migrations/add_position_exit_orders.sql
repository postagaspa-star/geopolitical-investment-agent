-- ============================================================================
-- position_exit_orders — ladder di uscite PARZIALI (SL/TP multi-livello)
-- ============================================================================
-- Permette piu' stop-loss / take-profit per la STESSA posizione, ognuno con la
-- propria quantita' (es. 2 BTC: TP 1 BTC @ 60k, TP 1 BTC @ 70k, SL 2 BTC @ 50k).
-- Quando il prezzo tocca un trigger, price_polling chiude SOLO la quantita' di
-- quell'ordine (chiusura parziale) e lo marca 'filled'.
--
-- Le colonne singole positions.stop_loss_price / take_profit_price restano per
-- il vecchio auto-exit single-level (off di default). Questo e' un sistema
-- ESPLICITO e dimensionato, con kill-switch dedicato settings.partial_exits_enabled.
--
-- L'app la crea da sola all'avvio (db_supabase auto-migration) se DATABASE_URL /
-- SUPABASE_DB_PASSWORD sono configurati. Questo file e' il fallback manuale:
-- ESEGUIRE UNA VOLTA nel SQL Editor del progetto Supabase.
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.position_exit_orders (
    id            BIGSERIAL PRIMARY KEY,
    ticker        TEXT NOT NULL,
    direction     TEXT NOT NULL DEFAULT 'LONG',
    kind          TEXT NOT NULL,                 -- 'SL' | 'TP'
    trigger_price DOUBLE PRECISION NOT NULL,
    quantity      NUMERIC(20,8) NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active', -- 'active' | 'filled' | 'cancelled'
    set_by        TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    filled_at     TIMESTAMPTZ,
    fill_price    DOUBLE PRECISION,
    filled_qty    NUMERIC(20,8)
);

CREATE INDEX IF NOT EXISTS idx_exit_orders_active
    ON public.position_exit_orders (ticker, status);
