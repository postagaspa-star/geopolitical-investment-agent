"""
DatabaseManager per Supabase (PostgreSQL).
Drop-in replacement per il modulo database.py basato su SQLite.
Usa la libreria supabase-py con la service_role key per accesso completo.
"""
import json
import logging
import os
from datetime import datetime, timezone, timedelta

from supabase import create_client, Client

logger = logging.getLogger(__name__)

# --- Connessione Supabase ---
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")  # service_role key

_client: Client | None = None


def _get_client() -> Client:
    """Restituisce il client Supabase singleton."""
    global _client
    if _client is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError(
                "SUPABASE_URL e SUPABASE_KEY devono essere configurati come variabili d'ambiente"
            )
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _client


def get_client() -> Client:
    """Public alias for _get_client — used by agents/ modules via `database.get_client()`."""
    return _get_client()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ============================================================
# Init (compatibilita' con il vecchio database.init_db)
# ============================================================

def init_db():
    """Verifica la connessione a Supabase e inizializza il portafoglio se vuoto."""
    try:
        client = _get_client()
        # Verifica connessione leggendo il portfolio
        result = client.table("portfolio").select("id").limit(1).execute()
        if not result.data:
            # Inserisci portafoglio iniziale
            bal = float(os.environ.get("INITIAL_PORTFOLIO_BALANCE", 100000))
            client.table("portfolio").insert({
                "cash_balance": bal,
                "total_value": bal,
            }).execute()
            logger.info("Portafoglio iniziale creato su Supabase con saldo %.2f", bal)
        logger.info("Supabase connesso e operativo.")

        # Auto-migrate v4 tables (safe: checks existence first)
        _ensure_v4_tables(client)

        # Auto-migrate schema (idempotente, gira ad ogni avvio: ALTER TABLE IF NOT EXISTS).
        # Include v10 SL/TP, v11 quantity NUMERIC, v9 chat tables, etc.
        _ensure_schema_migrations()

    except Exception as e:
        logger.error("Errore connessione Supabase: %s", e, exc_info=True)
        raise


def _ensure_schema_migrations():
    """
    Auto-migrazione schema su Supabase Postgres. Idempotente — usa
    IF NOT EXISTS / DO $$ BEGIN ... END$$, gira ad ogni avvio.

    Migrations applicate:
      - v9: chat_conversations, chat_messages
      - v10: positions.stop_loss_price, take_profit_price, auto_exit_*
      - v11: trades.quantity / positions.quantity da INTEGER → NUMERIC(20,8)
        (CRITICO: senza questo, sell di 99.5 azioni o 0.05 BTC falliscono
        con "invalid input syntax for type integer")
      - v12: technical_documents.category, is_preset

    Richiede uno tra:
      - DATABASE_URL: postgres://...:...@.../postgres (raccomandato)
      - SUPABASE_DB_PASSWORD: la password del DB (postgres user)
        + SUPABASE_URL per derivare l'host
    Se nessuno è configurato, logga warning e continua (degrade graceful).
    """
    db_url = os.environ.get("DATABASE_URL", "").strip()
    if not db_url:
        # Prova a derivarla da SUPABASE_URL + SUPABASE_DB_PASSWORD
        db_pass = os.environ.get("SUPABASE_DB_PASSWORD", "").strip()
        if db_pass and SUPABASE_URL:
            # SUPABASE_URL = https://<ref>.supabase.co
            try:
                ref = SUPABASE_URL.split("//")[1].split(".")[0]
                # Pooler region-agnostic: prova prima il pooler standard
                db_url = f"postgresql://postgres.{ref}:{db_pass}@aws-0-eu-central-1.pooler.supabase.com:6543/postgres"
            except Exception:
                pass

    if not db_url:
        logger.warning(
            "Auto-migrazione schema SKIPPED: configurare DATABASE_URL "
            "(consigliato) oppure SUPABASE_DB_PASSWORD su Render."
        )
        return

    try:
        import psycopg2
    except ImportError:
        logger.warning("psycopg2 non installato — auto-migrazione schema skipped.")
        return

    migration_sql = """
        -- v7: documenti separati per Decision normale vs Decision Crypto
        ALTER TABLE technical_documents ADD COLUMN IF NOT EXISTS category TEXT DEFAULT 'generic';
        -- backfill: i documenti caricati prima della migration hanno category=NULL.
        -- Senza questo UPDATE il filtro WHERE category='generic' li escluderebbe
        -- → Decision normale perderebbe tutti i documenti già caricati.
        UPDATE technical_documents SET category = 'generic' WHERE category IS NULL;
        -- v8: flag is_preset per i documenti precaricati (PDF crypto forniti
        -- nei preset_documents/). Non eliminabili dall'UI.
        ALTER TABLE technical_documents ADD COLUMN IF NOT EXISTS is_preset BOOLEAN DEFAULT FALSE;
        -- v10: SL/TP automatici impostati dall'agente sulle posizioni.
        -- Quando il prezzo corrente raggiunge questi livelli, price_polling
        -- esegue la chiusura automatica. 0 (default) = non impostato.
        ALTER TABLE positions ADD COLUMN IF NOT EXISTS stop_loss_price DOUBLE PRECISION DEFAULT 0;
        ALTER TABLE positions ADD COLUMN IF NOT EXISTS take_profit_price DOUBLE PRECISION DEFAULT 0;
        ALTER TABLE positions ADD COLUMN IF NOT EXISTS auto_exit_set_at TIMESTAMPTZ;
        ALTER TABLE positions ADD COLUMN IF NOT EXISTS auto_exit_set_by TEXT;

        -- v11: quantity NUMERIC invece di INTEGER.
        -- Bug: 50% di 199 NVDA shares = 99.5 → INTEGER rifiuta con
        -- "invalid input syntax for type integer". Inoltre crypto frazionali
        -- (0.05 BTC) venivano truncati a 0. NUMERIC(20,8) supporta entrambi.
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='trades' AND column_name='quantity'
                  AND data_type='integer'
            ) THEN
                ALTER TABLE trades ALTER COLUMN quantity TYPE NUMERIC(20,8)
                    USING quantity::NUMERIC(20,8);
            END IF;
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='positions' AND column_name='quantity'
                  AND data_type='integer'
            ) THEN
                ALTER TABLE positions ALTER COLUMN quantity TYPE NUMERIC(20,8)
                    USING quantity::NUMERIC(20,8);
            END IF;
        END$$;

        -- v9: chat assistant (conversazioni con AI DeepSeek-R1).
        -- Mini-memoria delle ultime 10 conversazioni dell'utente.
        CREATE TABLE IF NOT EXISTS chat_conversations (
            id BIGSERIAL PRIMARY KEY,
            title TEXT NOT NULL DEFAULT 'Nuova conversazione',
            selected_decisions TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS chat_messages (
            id BIGSERIAL PRIMARY KEY,
            conversation_id BIGINT NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
            content TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_chat_messages_conv
            ON chat_messages (conversation_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_chat_conversations_updated
            ON chat_conversations (updated_at DESC);

        -- v12: decision chat (chat con i Decision Agent, separata dal Coach analyst).
        -- Supporta proposta + esecuzione trade con conferma utente.
        CREATE TABLE IF NOT EXISTS decision_chat_conversations (
            id BIGSERIAL PRIMARY KEY,
            agent_type TEXT NOT NULL DEFAULT 'standard'
                CHECK (agent_type IN ('standard','crypto')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS decision_chat_messages (
            id BIGSERIAL PRIMARY KEY,
            conversation_id BIGINT NOT NULL
                REFERENCES decision_chat_conversations(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
            content TEXT NOT NULL,
            proposed_trade TEXT,           -- JSON string con {ticker,action,quantity,...} (legacy/compat)
            executed_trade_id BIGINT,      -- FK a trades.id quando l'utente conferma l'esecuzione
            proposed_actions TEXT,         -- v14: JSON array di azioni proposte (trade/stop_loss/take_profit/directive)
            executed_action_results TEXT,  -- v14: JSON dict {action_index_str: result_dict}
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        -- v14: migrazione idempotente per DB pre-esistenti senza queste colonne
        ALTER TABLE decision_chat_messages
            ADD COLUMN IF NOT EXISTS proposed_actions TEXT;
        ALTER TABLE decision_chat_messages
            ADD COLUMN IF NOT EXISTS executed_action_results TEXT;
        CREATE INDEX IF NOT EXISTS idx_dec_chat_messages_conv
            ON decision_chat_messages (conversation_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_dec_chat_conv_agent
            ON decision_chat_conversations (agent_type, updated_at DESC);

        -- v13: agent_commitments — memoria persistente delle "promesse"
        -- che il Decision Agent prende durante un run. Vengono iniettate
        -- nel contesto dei run successivi finche' non sono triggered/expired/cancelled.
        --   commitment_type:
        --     monitor          → "tieni d'occhio X finche' Y"
        --     conditional_buy  → "compra X se Y entro Z"
        --     conditional_sell → "vendi X se Y entro Z"
        --     watch_event      → "monitora evento (es. FOMC) e fai Y dopo"
        --     reminder         → "ricordati di fare Y al prossimo run"
        --   status: active → triggered/expired/cancelled
        --   trigger_action: testo libero descrittivo (es. "BUY ETH 0.5%")
        --   expires_at: scadenza assoluta. Se NOW() > expires_at, il get_active
        --     fa lo sweep automatico settando status='expired'.
        CREATE TABLE IF NOT EXISTS agent_commitments (
            id BIGSERIAL PRIMARY KEY,
            agent_type TEXT NOT NULL
                CHECK (agent_type IN ('standard','crypto')),
            ticker TEXT,
            commitment_type TEXT NOT NULL
                CHECK (commitment_type IN
                    ('monitor','conditional_buy','conditional_sell','watch_event','reminder')),
            condition_text TEXT NOT NULL,
            trigger_action TEXT,
            expires_at TIMESTAMPTZ,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','triggered','expired','cancelled')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            resolved_at TIMESTAMPTZ,
            resolved_reason TEXT,
            source_run_id TEXT,
            notes TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_agent_commit_active
            ON agent_commitments (agent_type, status, expires_at);
        CREATE INDEX IF NOT EXISTS idx_agent_commit_recent
            ON agent_commitments (agent_type, created_at DESC);
    """

    try:
        with psycopg2.connect(db_url, connect_timeout=15) as conn:
            with conn.cursor() as cur:
                cur.execute(migration_sql)
            conn.commit()
        logger.info("Auto-migrazione schema applicata su Supabase con successo.")
    except Exception as exc:
        # Non bloccare l'avvio del bot — degrade graceful
        logger.warning(
            "Auto-migrazione schema fallita (%s). Esegui manualmente "
            "le migration SQL su Supabase Dashboard.",
            exc,
        )


def _ensure_v4_tables(client: Client):
    """
    Ensure v4 multi-agent tables exist by attempting to read from them.
    If a table doesn't exist, log a warning. Tables must be created via
    the SQL migration (init_v4.sql) in the Supabase dashboard.
    """
    v4_tables = [
        "intelligence_buffer", "daily_snapshots", "weekly_matrix",
        "trades_high_risk", "agent_checkpoints",
    ]
    missing = []
    for table in v4_tables:
        try:
            client.table(table).select("*").limit(1).execute()
        except Exception:
            missing.append(table)

    if missing:
        logger.warning(
            "V4 tables mancanti: %s. Esegui init_v4.sql nel SQL Editor di Supabase.",
            missing,
        )
    else:
        logger.info("Tutte le tabelle v4 presenti.")


# ============================================================
# Portfolio
# ============================================================

def get_portfolio():
    client = _get_client()
    result = client.table("portfolio").select("*").order("id", desc=True).limit(1).execute()
    return result.data[0] if result.data else None


def update_portfolio(cash_balance, total_value):
    """Aggiorna cash_balance + total_value sull'unica riga del portafoglio.

    NOTA egress: questa funzione viene chiamata da OGNI tick di polling
    e da ogni trade. Per evitare di consumare la quota Supabase fa il
    minimo indispensabile: una SELECT id (necessaria per identificare
    la riga) + una UPDATE. Nessun audit log automatico — i callers che
    vogliono auditare (es. adjust_cash, restore-cash) chiamano
    `insert_cash_audit_log` direttamente, dato che hanno old_cash in
    scope senza doverlo rileggere dal DB.
    """
    client = _get_client()
    row = client.table("portfolio").select("id").order("id", desc=True).limit(1).execute()
    if row.data:
        client.table("portfolio").update({
            "cash_balance": cash_balance,
            "total_value": total_value,
            "updated_at": _now_iso(),
        }).eq("id", row.data[0]["id"]).execute()


def update_portfolio_total_value(total_value):
    """Aggiorna SOLO total_value (NON il cash).

    Usato da calculate_total_value, che e' una VALUTAZIONE e non deve mai
    riscrivere il cash: prima faceva update_portfolio(cash, total) col cash
    letto all'inizio della funzione → se un trade modificava il cash nel
    frattempo, lo scriveva STALE sovrascrivendo la modifica (perdita
    silenziosa di denaro). Toccando solo total_value la race sparisce.
    """
    client = _get_client()
    row = client.table("portfolio").select("id").order("id", desc=True).limit(1).execute()
    if row.data:
        client.table("portfolio").update({
            "total_value": total_value,
            "updated_at": _now_iso(),
        }).eq("id", row.data[0]["id"]).execute()


# ============================================================
# Positions
# ============================================================

def get_positions():
    client = _get_client()
    result = client.table("positions").select("*").order("opened_at", desc=True).execute()
    return result.data or []


def get_position(ticker):
    client = _get_client()
    result = client.table("positions").select("*").eq("ticker", ticker).limit(1).execute()
    return result.data[0] if result.data else None


def upsert_position(ticker, quantity, avg_buy_price, current_price=0,
                    direction=None):
    client = _get_client()
    existing = get_position(ticker)
    # direction: se NON passata, PRESERVA quella esistente — un update di
    # prezzo/ribilanciamento che non specifica direction NON deve
    # ribaltare uno SHORT a LONG. Nuova posizione senza direction → LONG.
    if direction is None:
        direction = (existing.get("direction") if existing else None) or "LONG"
    direction = "SHORT" if str(direction).upper() == "SHORT" else "LONG"
    # P&L non realizzato: LONG guadagna se il prezzo SALE, SHORT se SCENDE.
    if current_price > 0:
        pnl = ((avg_buy_price - current_price) if direction == "SHORT"
               else (current_price - avg_buy_price)) * quantity
    else:
        pnl = 0
    base = {
        "quantity": quantity,
        "avg_buy_price": avg_buy_price,
        "current_price": current_price,
        "unrealized_pnl": pnl,
    }

    def _write(payload):
        if existing:
            client.table("positions").update(payload).eq("ticker", ticker).execute()
        else:
            client.table("positions").insert({**payload, "ticker": ticker}).execute()

    try:
        _write({**base, "direction": direction})
    except Exception as exc:
        # Resilienza finestra di deploy: se la colonna 'direction' non
        # esiste ancora su Supabase (migration add_short_direction.sql
        # non applicata), NON rompere le posizioni long — riscrivi senza
        # il tag. Va comunque applicata la migration prima dello Stadio 2.
        if "direction" in str(exc).lower():
            logger.warning("upsert_position: colonna 'direction' assente su "
                           "Supabase — scrivo senza tag (applica la migration "
                           "add_short_direction.sql)")
            _write(base)
        else:
            raise


def delete_position(ticker):
    client = _get_client()
    client.table("positions").delete().eq("ticker", ticker).execute()


def update_position_price(ticker, current_price):
    client = _get_client()
    pos = get_position(ticker)
    if pos:
        # P&L non realizzato DIRECTION-AWARE: una posizione SHORT guadagna
        # quando il prezzo SCENDE. La formula long avrebbe invertito il
        # segno sugli short ad ogni aggiornamento prezzo del polling.
        _dir = str(pos.get("direction") or "LONG").upper()
        if _dir == "SHORT":
            pnl = (pos["avg_buy_price"] - current_price) * pos["quantity"]
        else:
            pnl = (current_price - pos["avg_buy_price"]) * pos["quantity"]
        client.table("positions").update({
            "current_price": current_price,
            "unrealized_pnl": pnl,
        }).eq("ticker", ticker).execute()


def update_position_auto_exit(ticker, stop_loss_price=None, take_profit_price=None, set_by=""):
    """Aggiorna i livelli SL/TP di una posizione (Supabase)."""
    client = _get_client()
    existing = get_position(ticker)
    if not existing:
        return False
    update_fields = {"auto_exit_set_at": _now_iso(), "auto_exit_set_by": set_by or ""}
    if stop_loss_price is not None:
        update_fields["stop_loss_price"] = float(stop_loss_price)
    if take_profit_price is not None:
        update_fields["take_profit_price"] = float(take_profit_price)
    try:
        client.table("positions").update(update_fields).eq("ticker", ticker).execute()
        return True
    except Exception as e:
        logger.warning("update_position_auto_exit fallita: %s", e)
        return False


def get_positions_with_auto_exits():
    """Posizioni con SL o TP > 0 impostati."""
    client = _get_client()
    try:
        # Supabase non ha OR composto facilmente: facciamo due query e mergiamo
        sl_rows = client.table("positions").select("*").gt("stop_loss_price", 0).execute()
        tp_rows = client.table("positions").select("*").gt("take_profit_price", 0).execute()
        merged = {}
        for r in (sl_rows.data or []) + (tp_rows.data or []):
            merged[r["id"]] = r
        return list(merged.values())
    except Exception as e:
        logger.warning("get_positions_with_auto_exits fallita: %s", e)
        return []


def count_positions():
    client = _get_client()
    result = client.table("positions").select("id", count="exact").execute()
    return result.count or 0


# ============================================================
# Trades
# ============================================================

def insert_trade(ticker, action, quantity, price, geo_reasoning, tech_reasoning,
                 final_decision, confidence, direction="LONG"):
    """
    Inserisce un trade e ritorna l'ID della riga creata.

    direction = 'LONG' | 'SHORT' — tag che distingue le operazioni sul
    book long da quelle short (aprire short = SELL+SHORT, coprire =
    BUY+SHORT). action resta il verbo di mercato 'BUY'/'SELL'.
    """
    client = _get_client()
    direction = "SHORT" if str(direction).upper() == "SHORT" else "LONG"
    payload = {
        "ticker": ticker,
        "action": action,
        "quantity": quantity,
        "price": price,
        "total_value": price * quantity,
        "geopolitical_reasoning": geo_reasoning,
        "technical_reasoning": tech_reasoning,
        "final_decision": final_decision,
        "confidence_score": confidence,
    }
    try:
        result = client.table("trades").insert({**payload, "direction": direction}).execute()
    except Exception as exc:
        # Resilienza finestra di deploy: colonna 'direction' non ancora
        # presente → inserisci senza tag invece di perdere il trade.
        if "direction" in str(exc).lower():
            logger.warning("insert_trade: colonna 'direction' assente su "
                           "Supabase — inserisco senza tag (applica la "
                           "migration add_short_direction.sql)")
            result = client.table("trades").insert(payload).execute()
        else:
            raise
    if result.data and len(result.data) > 0:
        return result.data[0].get("id")
    return None


def get_trades(limit=50):
    client = _get_client()
    result = client.table("trades").select("*").order("timestamp", desc=True).limit(limit).execute()
    return result.data or []


def update_trade_mirror_status(trade_id, status, reason=None, increment_attempts=True):
    """No-op stub (mantenuto per compat: nessun mirror esterno attivo)."""
    return


def get_pending_mirror_trades(window_hours=24, max_attempts=5, limit=50):
    """No-op stub (mantenuto per compat: nessun mirror esterno attivo)."""
    return []


def get_mirror_status_summary():
    """No-op stub (mantenuto per compat: nessun mirror esterno attivo)."""
    try:
        return {}
    except Exception:
        return {}


# ============================================================
# Agent Logs
# ============================================================

def insert_agent_log(run_id, phase, content):
    client = _get_client()
    client.table("agent_logs").insert({
        "run_id": run_id,
        "phase": phase,
        "content": content,
    }).execute()


def get_agent_logs(limit=100):
    client = _get_client()
    result = client.table("agent_logs").select("*").order("timestamp", desc=True).limit(limit).execute()
    return result.data or []


def get_logs_by_run(run_id):
    client = _get_client()
    result = client.table("agent_logs").select("*").eq("run_id", run_id).order("timestamp").execute()
    return result.data or []


def get_recent_decisions(limit=15, agent_type=None):
    """
    Recupera gli ultimi N "consigli finali" del Decision Agent (Live).
    Usata per iniettare memoria operativa nel system prompt del run successivo,
    cosi' l'agente vede pattern, errori ricorrenti e tesi recenti dei propri
    cicli passati invece di partire from-scratch ogni volta.

    Phases incluse (un record per run, il "consiglio finale" sintetico):
      - DECISION_REASONING       (Decision standard, Claude o R1)
      - DECISION_CRYPTO_COMPLETE (Decision crypto, R1)

    @param limit       max numero di decisioni
    @param agent_type  None | "standard" | "crypto" — filtra per dominio
    """
    if agent_type == "standard":
        phases = ["DECISION_REASONING"]
    elif agent_type == "crypto":
        phases = ["DECISION_CRYPTO_COMPLETE"]
    else:
        phases = ["DECISION_REASONING", "DECISION_CRYPTO_COMPLETE"]

    client = _get_client()
    result = (
        client.table("agent_logs")
        .select("*")
        .in_("phase", phases)
        .order("timestamp", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data or []


# ============================================================
# Geopolitical Snapshots
# ============================================================

def insert_geopolitical_snapshot(run_id, source, raw_data, processed_summary):
    client = _get_client()
    client.table("geopolitical_snapshots").insert({
        "run_id": run_id,
        "source": source,
        "raw_data": raw_data,
        "processed_summary": processed_summary,
    }).execute()


def get_geopolitical_snapshots(limit=20):
    client = _get_client()
    result = client.table("geopolitical_snapshots").select("*").order("timestamp", desc=True).limit(limit).execute()
    return result.data or []


# ============================================================
# Settings (con cache in-memory TTL per ridurre l'egress)
# ============================================================
# Le settings vengono lette da decine di code path ad ogni ciclo
# (agenti, polling, decision, ecc.). Senza cache, ogni get_setting
# è una chiamata API a Supabase → fettissimo del nostro egress era
# proprio questo. La cache TTL 30s riduce di ~100x il traffico.
#
# set_setting() invalida la chiave per non servire valori stantii
# dopo una scrittura.

import time as _time
from threading import Lock as _Lock
_settings_cache: dict = {}
_settings_cache_lock = _Lock()
_SETTINGS_TTL = 30  # secondi


def _cache_get(key):
    """Ritorna (hit, value). hit=False se mancante o scaduto."""
    with _settings_cache_lock:
        entry = _settings_cache.get(key)
        if entry and entry[0] > _time.monotonic():
            return True, entry[1]
    return False, None


def _cache_put(key, value):
    with _settings_cache_lock:
        _settings_cache[key] = (_time.monotonic() + _SETTINGS_TTL, value)


def _cache_invalidate(key=None):
    with _settings_cache_lock:
        if key is None:
            _settings_cache.clear()
        else:
            _settings_cache.pop(key, None)
            _settings_cache.pop("__all__", None)
            _settings_cache.pop("__config__", None)


def get_setting(key, default=None):
    hit, value = _cache_get(key)
    if hit:
        return value if value is not None else default
    client = _get_client()
    result = client.table("settings").select("value").eq("key", key).limit(1).execute()
    value = result.data[0]["value"] if result.data else None
    _cache_put(key, value)
    return value if value is not None else default


def set_setting(key, value):
    client = _get_client()
    client.table("settings").upsert({
        "key": key,
        "value": str(value),
        "updated_at": _now_iso(),
    }).execute()
    # Invalida cache per non servire il valore vecchio al prossimo read
    _cache_invalidate(key)


# Prefisso convenzionale dei valori NON-config serializzati nella tabella
# `settings`: chat-fallback, sim-progress, sim-scenari, ecc. Tutti contengono
# "::" e iniziano con "_". NESSUNA chiave di configurazione reale lo usa
# (verificato: risk_*, prompt_*, *_api_key, user_risk_profile, last_decision_*,
# crypto_decision_engine, ecc. sono tutti nomi "piatti").
_TRANSIENT_KEY_MARKER = "::"


def _is_config_key(key: str) -> bool:
    """True se la chiave e' una vera impostazione di config (non stato transitorio).

    Discriminatore robusto: lo stato transitorio (chat-fallback, sim-progress)
    usa sempre chiavi namespaced con "::". Le config reali non lo fanno mai.
    """
    return _TRANSIENT_KEY_MARKER not in (key or "")


def get_all_settings():
    """TUTTE le righe della tabella settings, incluse le chiavi transitorie
    (_chat_fallback::*, _sim_*::*). Paginerebbe oltre il cap PostgREST di 1000
    righe. Usato da diagnostica/chat-fallback che DEVONO vedere tutto.

    Per l'endpoint /api/settings e la UI usare get_config_settings()."""
    hit, value = _cache_get("__all__")
    if hit:
        return value
    client = _get_client()
    # Paginazione esplicita: PostgREST tronca silenziosamente a 1000 righe.
    # La tabella settings, col fallback chat/sim serializzato, supera le 1000
    # → senza .range() chiavi legittime diventavano invisibili (incluso il
    # flag crypto_decision_engine appena scritto). Ora leggiamo a finestre.
    data: dict = {}
    page = 0
    page_size = 1000
    while True:
        lo = page * page_size
        hi = lo + page_size - 1
        result = client.table("settings").select("key, value").range(lo, hi).execute()
        rows = result.data or []
        for r in rows:
            data[r["key"]] = r["value"]
        if len(rows) < page_size:
            break
        page += 1
        if page > 100:   # hard stop di sicurezza (max ~100k righe)
            logger.warning("get_all_settings: >100k righe, interrompo paginazione")
            break
    _cache_put("__all__", data)
    return data


def get_config_settings():
    """SOLO le impostazioni di configurazione reali (esclude lo stato
    transitorio namespaced con "::"). Non puo' mai essere troncato dal cap
    PostgREST perche' le chiavi di config sono poche decine.

    E' la fonte per l'endpoint GET /api/settings e per la pagina Settings.
    """
    hit, value = _cache_get("__config__")
    if hit:
        return value
    all_settings = get_all_settings()
    config = {k: v for k, v in all_settings.items() if _is_config_key(k)}
    _cache_put("__config__", config)
    return config


def purge_transient_settings(prefixes=("_sim_run_progress::", "_sim_run_fallback::"),
                              dry_run: bool = False) -> dict:
    """Elimina dalla tabella settings le chiavi transitorie con i prefissi dati.

    DEFAULT SICURO: tocca solo lo stato del Simulator (progress/fallback run),
    che e' rigenerabile e ha gia' un TTL logico. NON include `_chat_fallback::*`
    (storia chat dell'utente quando le tabelle chat mancano) ne' `_sim_scenario::*`
    (scenari dinamici salvati) — vanno purgati solo con prefisso esplicito e
    consapevole.

    dry_run=True → conta soltanto, non cancella. Ritorna {prefix: count}.
    """
    client = _get_client()
    report: dict = {}
    for pref in prefixes:
        try:
            # PostgREST: like con wildcard. Conta prima.
            sel = client.table("settings").select("key").like("key", f"{pref}%").execute()
            keys = [r["key"] for r in (sel.data or [])]
            report[pref] = len(keys)
            if not dry_run and keys:
                client.table("settings").delete().like("key", f"{pref}%").execute()
        except Exception as e:
            logger.warning("purge_transient_settings(%s) fallita: %s", pref, e)
            report[pref] = -1
    if not dry_run:
        _cache_invalidate()
    return report


def invalidate_settings_cache():
    """Esportata per test e per /api/admin endpoints che modificano i settings
    senza passare da set_setting (es. SQL diretto via psycopg2)."""
    _cache_invalidate()


# ============================================================
# Technical Documents
# ============================================================

def insert_document(filename, content, file_size=0, category="generic", is_preset=False):
    """
    Inserisce un documento nella knowledge base. Cascade defensiva per gestire
    schemi minori che potrebbero mancare di colonne (`is_preset`, `category`).

    Bug precedente: se l'INSERT con is_preset+category falliva (anche per
    motivi non legati alla colonna), il primo fallback rimuoveva is_preset
    ma non distingueva. Il secondo fallback rimuoveva ANCHE category. Risultato:
    documenti salvati con category=None invece di quella richiesta.

    Fix: cascade più mirata, log esplicito quando si rimuove category.
    """
    client = _get_client()
    payload = {"filename": filename, "content": content, "file_size": file_size}

    # Tentativo 1: schema completo (is_preset + category)
    try:
        client.table("technical_documents").insert(
            {**payload, "category": category, "is_preset": is_preset}
        ).execute()
        return
    except Exception as exc1:
        # Solo se l'errore è specifico a is_preset → ritenta senza
        msg1 = str(exc1).lower()
        is_preset_missing = "is_preset" in msg1 or "column" in msg1 and "is_preset" in msg1

    # Tentativo 2: senza is_preset, mantieni category (campo importante!)
    try:
        client.table("technical_documents").insert(
            {**payload, "category": category}
        ).execute()
        return
    except Exception as exc2:
        msg2 = str(exc2).lower()
        category_missing = "category" in msg2 or "column" in msg2 and "category" in msg2
        logger.warning("[insert_document] retry-no-category necessario: %s", str(exc2)[:200])

    # Tentativo 3 (last resort): solo campi base
    client.table("technical_documents").insert(payload).execute()


def upsert_preset_document(filename, content, file_size=0, category="crypto"):
    """Inserisce o aggiorna un preset crypto (idempotente)."""
    client = _get_client()
    try:
        existing = (client.table("technical_documents").select("id")
                    .eq("filename", filename).eq("is_preset", True).limit(1).execute())
        if existing.data:
            client.table("technical_documents").update({
                "content": content, "file_size": file_size, "category": category,
            }).eq("id", existing.data[0]["id"]).execute()
        else:
            client.table("technical_documents").insert({
                "filename": filename, "content": content, "file_size": file_size,
                "category": category, "is_preset": True,
            }).execute()
    except Exception as e:
        import logging as _log
        _log.getLogger(__name__).warning("upsert_preset_document fallita: %s", e)


def get_documents(category=None):
    """Lista documenti. Se category specificata ('generic' o 'crypto'), filtra."""
    client = _get_client()
    try:
        q = client.table("technical_documents").select(
            "id, filename, file_size, uploaded_at, category, is_preset"
        ).order("uploaded_at", desc=True)
        if category is not None:
            q = q.eq("category", category)
        result = q.execute()
        return result.data or []
    except Exception:
        try:
            q2 = client.table("technical_documents").select(
                "id, filename, file_size, uploaded_at, category"
            ).order("uploaded_at", desc=True)
            if category is not None:
                q2 = q2.eq("category", category)
            return q2.execute().data or []
        except Exception:
            result = client.table("technical_documents").select(
                "id, filename, file_size, uploaded_at"
            ).order("uploaded_at", desc=True).execute()
            return result.data or []


def get_document_contents(category=None):
    """Contenuti documenti per iniezione nel prompt. Filtra per category se specificata."""
    client = _get_client()
    try:
        q = client.table("technical_documents").select(
            "id, filename, content, category"
        ).order("uploaded_at")
        if category is not None:
            q = q.eq("category", category)
        result = q.execute()
        return result.data or []
    except Exception:
        result = client.table("technical_documents").select(
            "id, filename, content"
        ).order("uploaded_at").execute()
        return result.data or []


def delete_document(doc_id):
    client = _get_client()
    client.table("technical_documents").delete().eq("id", doc_id).execute()


# ============================================================
# Portfolio Reset
# ============================================================

def reset_portfolio_data(new_balance):
    client = _get_client()
    client.table("positions").delete().neq("id", 0).execute()
    client.table("trades").delete().neq("id", 0).execute()
    client.table("portfolio_snapshots").delete().neq("id", 0).execute()
    client.table("agent_logs").delete().neq("id", 0).execute()
    # Aggiorna il portafoglio
    row = client.table("portfolio").select("id").order("id", desc=True).limit(1).execute()
    if row.data:
        client.table("portfolio").update({
            "cash_balance": new_balance,
            "total_value": new_balance,
            "updated_at": _now_iso(),
        }).eq("id", row.data[0]["id"]).execute()
    set_setting("initial_balance", str(new_balance))


# ============================================================
# Weekend Intelligence
# ============================================================

def insert_weekend_intelligence(run_id, content, key_events, market_implications):
    client = _get_client()
    client.table("weekend_intelligence").insert({
        "run_id": run_id,
        "content": content,
        "key_events": key_events,
        "market_implications": market_implications,
    }).execute()


def get_weekend_intelligence(limit=20):
    client = _get_client()
    result = client.table("weekend_intelligence").select("*").order("saved_at", desc=True).limit(limit).execute()
    return result.data or []


def get_latest_weekend_intelligence():
    client = _get_client()
    result = client.table("weekend_intelligence").select("*").order("saved_at", desc=True).limit(1).execute()
    return result.data[0] if result.data else None


# ============================================================
# Pre-Market Briefings
# ============================================================

def insert_pre_market_briefing(run_id, market_session, content, priority_assets):
    client = _get_client()
    client.table("pre_market_briefings").insert({
        "run_id": run_id,
        "market_session": market_session,
        "content": content,
        "priority_assets": priority_assets,
    }).execute()


def get_pre_market_briefings(limit=20):
    client = _get_client()
    result = client.table("pre_market_briefings").select("*").order("saved_at", desc=True).limit(limit).execute()
    return result.data or []


def get_latest_pre_market_briefing():
    client = _get_client()
    result = client.table("pre_market_briefings").select("*").order("saved_at", desc=True).limit(1).execute()
    return result.data[0] if result.data else None


# ============================================================
# Processed Articles (deduplicazione)
# ============================================================

def is_article_processed(url):
    client = _get_client()
    result = client.table("processed_articles").select("url").eq("url", url).limit(1).execute()
    return bool(result.data)


def mark_article_processed(url):
    client = _get_client()
    client.table("processed_articles").upsert({
        "url": url,
        "processed_at": _now_iso(),
    }).execute()


def count_new_articles_since(hours=2):
    client = _get_client()
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    result = client.table("processed_articles").select("url", count="exact").gte("processed_at", since).execute()
    return result.count or 0


def cleanup_old_processed_articles(days=7):
    client = _get_client()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    client.table("processed_articles").delete().lt("processed_at", cutoff).execute()


# ============================================================
# Portfolio Snapshots (equity curve)
# ============================================================

def insert_portfolio_snapshot(total_value, cash_balance):
    """Salva uno snapshot del valore del portafoglio.

    HARD SANITY CAP: rifiuta snapshot con drift >3x (o <1/3x) rispetto
    all'ultimo snapshot. Movimenti reali non causano mai salti di 3x in
    pochi minuti; sono sintomo di bug nel calcolo del NAV (es. doppio
    conteggio di posizioni short prima del fix direction-aware). Le
    guard piu' fini (IQR median, drift guard del polling) restano i
    livelli primari di difesa; questo è il backstop al livello DB.
    """
    try:
        tv = float(total_value)
    except (TypeError, ValueError):
        logger.warning("insert_portfolio_snapshot: total_value non numerico (%r), skip", total_value)
        return
    if tv <= 0:
        logger.warning("insert_portfolio_snapshot: total_value=%.2f non positivo, skip", tv)
        return

    client = _get_client()
    # Sanity cap: leggi l'ultimo snapshot e confronta. Se il check fallisce
    # per errore di rete/DB, fail-open (insert normale) per non degradare
    # la equity curve in caso di hiccup transitorio.
    try:
        last = (client.table("portfolio_snapshots")
                .select("total_value")
                .order("timestamp", desc=True)
                .limit(1).execute())
        if last.data and last.data[0].get("total_value"):
            last_val = float(last.data[0]["total_value"])
            if last_val > 0:
                ratio = tv / last_val
                if ratio > 3.0 or ratio < 1 / 3.0:
                    logger.error(
                        "insert_portfolio_snapshot REJECT: total_value=%.2f "
                        "vs last=%.2f (ratio %.2fx). Sintomo di bug nel "
                        "calcolo NAV — snapshot NON inserito.",
                        tv, last_val, ratio,
                    )
                    return
    except Exception as exc:
        logger.debug("insert_portfolio_snapshot sanity check fallita (fail-open): %s", exc)

    client.table("portfolio_snapshots").insert({
        "total_value": tv,
        "cash_balance": cash_balance,
    }).execute()


# ============================================================
# Cash Audit Log — traccia ogni mutazione di cash_balance
# ============================================================

def insert_cash_audit_log(delta, old_cash, new_cash, reason,
                          source=None, ticker=None, metadata=None):
    """Log di una mutazione di cash. Best-effort: se la tabella non esiste
    (migration add_cash_audit_log.sql non applicata) la chiamata fa no-op,
    non rompe il trade.
    """
    try:
        d = float(delta)
    except (TypeError, ValueError):
        return
    if abs(d) < 0.005:
        return
    try:
        client = _get_client()
        client.table("cash_audit_log").insert({
            "delta": d,
            "old_cash": old_cash,
            "new_cash": new_cash,
            "reason": reason,
            "source": source,
            "ticker": ticker,
            "metadata": metadata,
        }).execute()
    except Exception as exc:
        logger.debug("cash_audit_log insert skipped (table missing?): %s", exc)


def get_cash_audit_log(limit=100, since_iso=None, reason=None):
    """Ritorna le ultime righe del cash_audit_log, piu' recenti per prime.
    Se la tabella non esiste ritorna lista vuota.
    """
    try:
        client = _get_client()
        q = (client.table("cash_audit_log").select("*")
             .order("timestamp", desc=True)
             .limit(min(int(limit or 100), 1000)))
        if since_iso:
            q = q.gte("timestamp", since_iso)
        if reason:
            q = q.eq("reason", reason)
        return q.execute().data or []
    except Exception as exc:
        logger.debug("cash_audit_log read skipped: %s", exc)
        return []


def get_portfolio_history(days=30):
    """
    Ritorna gli snapshot di portafoglio degli ultimi N giorni.

    Bug precedente: Supabase impone un limite default di 1000 righe per query
    e l'order era ASC senza limit esplicito, quindi venivano restituite le
    PRIME 1000 righe del periodo (le più vecchie). I grafici Live Analytics
    mostravano dati di settimane fa invece dei più recenti.

    Fix: ordine DESC + limit esplicito → prendiamo le 1000 più recenti, poi
    invertiamo lato Python per restituirle ASC (compatibile col frontend).
    """
    client = _get_client()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    result = (client.table("portfolio_snapshots")
              .select("total_value, cash_balance, timestamp")
              .gte("timestamp", since)
              .order("timestamp", desc=True)
              .limit(1000)
              .execute())
    rows = result.data or []
    rows.reverse()   # da DESC a ASC per il chart (timeline cronologica)
    return rows


def get_first_portfolio_snapshot():
    """
    Ritorna il PRIMISSIMO snapshot mai registrato (inizio vita reale del
    portafoglio), indipendentemente dal limite di 1000 righe.

    Perche' serve: get_portfolio_history() ha .limit(1000); con snapshot
    inseriti ogni minuto dal price_polling, 1000 righe coprono solo ~10
    giorni. Quindi return e alpha vs S&P venivano calcolati su una finestra
    parziale (es. solo da meta' maggio) invece che sulla vita completa del
    portafoglio (~2 mesi). Questa query prende il punto di partenza vero.
    """
    client = _get_client()
    try:
        result = (client.table("portfolio_snapshots")
                  .select("total_value, cash_balance, timestamp")
                  .order("timestamp", desc=False)
                  .limit(1)
                  .execute())
        rows = result.data or []
        return rows[0] if rows else None
    except Exception as exc:
        logger.warning("get_first_portfolio_snapshot fallita: %s", exc)
        return None


def get_first_snapshot_since(ts_iso):
    """
    Primo snapshot con timestamp >= ts_iso (ISO string). Usato per
    ancorare return/alpha all'inizio UFFICIALE (primo movimento dopo la
    chiusura di CRWD+LMT), non al primissimo snapshot del periodo
    inattivo. .limit(1) → nessun problema col cap di 1000 righe.
    """
    client = _get_client()
    try:
        result = (client.table("portfolio_snapshots")
                  .select("total_value, cash_balance, timestamp")
                  .gte("timestamp", ts_iso)
                  .order("timestamp", desc=False)
                  .limit(1)
                  .execute())
        rows = result.data or []
        return rows[0] if rows else None
    except Exception as exc:
        logger.warning("get_first_snapshot_since fallita: %s", exc)
        return None


# ============================================================
# Chat Assistant (conversazioni con l'analista AI DeepSeek-R1)
# ============================================================

# ─── Chat storage: dual mode (table preferito, settings fallback) ──────────
# Se le tabelle chat_conversations/chat_messages non esistono su Supabase
# (DATABASE_URL non configurato → migration psycopg2 saltata), il sistema
# automaticamente cade su una serializzazione JSON nella tabella `settings`
# che esiste sempre. Cosi' la chat funziona anche senza migrations DDL.

_CHAT_FALLBACK_MODE = False  # True dopo il primo errore di tabella mancante
_CHAT_FALLBACK_KEY_LIST = "_chat_fallback::conv_list"   # JSON list di id ordinati
_CHAT_FALLBACK_KEY_CONV = "_chat_fallback::conv::{id}"  # JSON per ogni conv
_CHAT_FALLBACK_KEY_MSGS = "_chat_fallback::msgs::{id}"  # JSON list di messaggi


def _is_missing_table_err(exc: Exception) -> bool:
    """
    True SOLO per "tabella inesistente" (DDL non applicato). Classificatore
    PRECISO e condiviso: i precedenti erano troppo larghi e LATCHAVANO un
    fallback permanente su errori TRANSITORI ('schema cache' durante un
    reload PostgREST, 'relation' dentro 'permission denied for relation'
    o 'deadlock ... relation', un nudo 'does not exist' che matcha anche
    'column ... does not exist'). Un singolo blip nascondeva cosi' tutta
    la chat / decision-chat / commitments fino al riavvio del processo.
    """
    s = str(exc).lower()
    if "pgrst205" in s:
        return True
    if "could not find the table" in s:
        return True
    if "no such table" in s:                       # SQLite
        return True
    if "relation" in s and "does not exist" in s:  # Postgres canonico
        return True
    return False


def _chat_should_fallback(exc: Exception) -> bool:
    """Tabella chat assente → fallback. Usa il classificatore preciso."""
    return _is_missing_table_err(exc)


def _chat_fallback_load_list() -> list:
    """Carica la lista di conversazioni dal fallback (tabella settings)."""
    raw = get_setting(_CHAT_FALLBACK_KEY_LIST, "[]") or "[]"
    try:
        return json.loads(raw) if isinstance(raw, str) else (raw or [])
    except Exception:
        return []


def _chat_fallback_save_list(lst: list):
    set_setting(_CHAT_FALLBACK_KEY_LIST, json.dumps(lst, default=str))


def _chat_fallback_load_conv(conv_id) -> dict | None:
    raw = get_setting(_CHAT_FALLBACK_KEY_CONV.format(id=conv_id), "")
    if not raw:
        return None
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return None


def _chat_fallback_save_conv(conv_id, conv: dict):
    set_setting(_CHAT_FALLBACK_KEY_CONV.format(id=conv_id),
                json.dumps(conv, default=str))


def _chat_fallback_load_msgs(conv_id) -> list:
    raw = get_setting(_CHAT_FALLBACK_KEY_MSGS.format(id=conv_id), "[]") or "[]"
    try:
        return json.loads(raw) if isinstance(raw, str) else (raw or [])
    except Exception:
        return []


def _chat_fallback_save_msgs(conv_id, msgs: list):
    set_setting(_CHAT_FALLBACK_KEY_MSGS.format(id=conv_id),
                json.dumps(msgs, default=str))


def create_chat_conversation(title: str = "Nuova conversazione",
                             selected_decisions: str | None = None) -> int | None:
    """
    Crea una nuova conversazione su tabella dedicata. Se manca, cade
    automaticamente su settings come storage chiave-valore.
    """
    global _CHAT_FALLBACK_MODE
    client = _get_client()
    payload = {"title": title[:120], "selected_decisions": selected_decisions}

    if not _CHAT_FALLBACK_MODE:
        try:
            result = client.table("chat_conversations").insert(payload).execute()
            if result.data and len(result.data) > 0:
                return result.data[0].get("id")
            logger.error("create_chat_conversation: insert ok ma no data")
            return None
        except Exception as e:
            if _chat_should_fallback(e):
                logger.warning("create_chat_conversation: tabella mancante, switch a settings fallback (%s)", e)
                _CHAT_FALLBACK_MODE = True
            else:
                raise

    # Fallback mode: usa settings table
    import time as _t
    new_id = int(_t.time() * 1000)  # millisecond timestamp come id univoco
    now_iso = _now_iso()
    conv = {
        "id": new_id, "title": title[:120],
        "selected_decisions": selected_decisions,
        "created_at": now_iso, "updated_at": now_iso,
    }
    _chat_fallback_save_conv(new_id, conv)
    lst = _chat_fallback_load_list()
    lst.insert(0, new_id)  # piu' recente in cima
    _chat_fallback_save_list(lst[:50])  # cap a 50 totali
    return new_id


def get_chat_conversations(limit: int = 10) -> list:
    """Lista le ultime N conversazioni (tabella o fallback settings)."""
    global _CHAT_FALLBACK_MODE
    client = _get_client()
    if not _CHAT_FALLBACK_MODE:
        try:
            result = (client.table("chat_conversations")
                      .select("id, title, selected_decisions, created_at, updated_at")
                      .order("updated_at", desc=True)
                      .limit(limit)
                      .execute())
            return result.data or []
        except Exception as e:
            if _chat_should_fallback(e):
                logger.warning("get_chat_conversations: switch a settings fallback")
                _CHAT_FALLBACK_MODE = True
            else:
                logger.warning("get_chat_conversations fallita: %s", e)
                return []

    # Fallback
    try:
        ids = _chat_fallback_load_list()[:limit]
        out = []
        for cid in ids:
            conv = _chat_fallback_load_conv(cid)
            if conv:
                out.append(conv)
        return out
    except Exception as e:
        logger.warning("get_chat_conversations fallback fallita: %s", e)
        return []


def get_chat_messages(conversation_id: int) -> list:
    """Messaggi di una conversazione, in ordine cronologico."""
    global _CHAT_FALLBACK_MODE
    client = _get_client()
    if not _CHAT_FALLBACK_MODE:
        try:
            result = (client.table("chat_messages")
                      .select("id, role, content, created_at")
                      .eq("conversation_id", conversation_id)
                      .order("created_at")
                      .execute())
            return result.data or []
        except Exception as e:
            if _chat_should_fallback(e):
                logger.warning("get_chat_messages: switch a settings fallback")
                _CHAT_FALLBACK_MODE = True
            else:
                logger.warning("get_chat_messages fallita: %s", e)
                return []

    # Fallback
    try:
        return _chat_fallback_load_msgs(conversation_id)
    except Exception as e:
        logger.warning("get_chat_messages fallback fallita: %s", e)
        return []


def insert_chat_message(conversation_id: int, role: str, content: str) -> int | None:
    """Aggiunge un messaggio e aggiorna updated_at della conversazione."""
    global _CHAT_FALLBACK_MODE
    client = _get_client()
    if not _CHAT_FALLBACK_MODE:
        try:
            result = client.table("chat_messages").insert({
                "conversation_id": conversation_id,
                "role": role,
                "content": content,
            }).execute()
            client.table("chat_conversations").update({
                "updated_at": _now_iso(),
            }).eq("id", conversation_id).execute()
            if result.data and len(result.data) > 0:
                return result.data[0].get("id")
        except Exception as e:
            if _chat_should_fallback(e):
                logger.warning("insert_chat_message: switch a settings fallback")
                _CHAT_FALLBACK_MODE = True
            else:
                logger.warning("insert_chat_message fallita: %s", e)
                return None

    # Fallback
    try:
        import time as _t
        msg_id = int(_t.time() * 1000)
        msgs = _chat_fallback_load_msgs(conversation_id)
        msgs.append({
            "id": msg_id, "role": role, "content": content,
            "created_at": _now_iso(),
        })
        _chat_fallback_save_msgs(conversation_id, msgs[-200:])  # cap 200 msg per conv

        # Aggiorna updated_at della conversazione
        conv = _chat_fallback_load_conv(conversation_id)
        if conv:
            conv["updated_at"] = _now_iso()
            _chat_fallback_save_conv(conversation_id, conv)
            # Sposta in cima alla lista
            lst = _chat_fallback_load_list()
            if conversation_id in lst:
                lst.remove(conversation_id)
            lst.insert(0, conversation_id)
            _chat_fallback_save_list(lst[:50])
        return msg_id
    except Exception as e:
        logger.warning("insert_chat_message fallback fallita: %s", e)
        return None


def update_chat_conversation_title(conversation_id: int, title: str):
    global _CHAT_FALLBACK_MODE
    client = _get_client()
    if not _CHAT_FALLBACK_MODE:
        try:
            client.table("chat_conversations").update({
                "title": title[:120], "updated_at": _now_iso(),
            }).eq("id", conversation_id).execute()
            return
        except Exception as e:
            if _chat_should_fallback(e):
                _CHAT_FALLBACK_MODE = True
            else:
                logger.warning("update_chat_conversation_title fallita: %s", e)
                return
    try:
        conv = _chat_fallback_load_conv(conversation_id)
        if conv:
            conv["title"] = title[:120]
            conv["updated_at"] = _now_iso()
            _chat_fallback_save_conv(conversation_id, conv)
    except Exception as e:
        logger.warning("update_title fallback fallita: %s", e)


def update_chat_conversation_decisions(conversation_id: int, selected_decisions: str):
    global _CHAT_FALLBACK_MODE
    client = _get_client()
    if not _CHAT_FALLBACK_MODE:
        try:
            client.table("chat_conversations").update({
                "selected_decisions": selected_decisions,
                "updated_at": _now_iso(),
            }).eq("id", conversation_id).execute()
            return
        except Exception as e:
            if _chat_should_fallback(e):
                _CHAT_FALLBACK_MODE = True
            else:
                logger.warning("update_chat_conversation_decisions fallita: %s", e)
                return
    try:
        conv = _chat_fallback_load_conv(conversation_id)
        if conv:
            conv["selected_decisions"] = selected_decisions
            conv["updated_at"] = _now_iso()
            _chat_fallback_save_conv(conversation_id, conv)
    except Exception as e:
        logger.warning("update_decisions fallback fallita: %s", e)


def delete_chat_conversation(conversation_id: int):
    """Elimina una conversazione (cascade sui messaggi via FK)."""
    global _CHAT_FALLBACK_MODE
    client = _get_client()
    if not _CHAT_FALLBACK_MODE:
        try:
            client.table("chat_messages").delete().eq("conversation_id", conversation_id).execute()
            client.table("chat_conversations").delete().eq("id", conversation_id).execute()
            return
        except Exception as e:
            if _chat_should_fallback(e):
                _CHAT_FALLBACK_MODE = True
            else:
                logger.warning("delete_chat_conversation fallita: %s", e)
                return
    try:
        # Rimuovi da lista, poi cancella conv + msgs
        lst = _chat_fallback_load_list()
        if conversation_id in lst:
            lst.remove(conversation_id)
            _chat_fallback_save_list(lst)
        # Settings non ha delete diretto: sovrascriviamo con stringa vuota
        # (alla rilettura ritorna None/[])
        try:
            set_setting(_CHAT_FALLBACK_KEY_CONV.format(id=conversation_id), "")
            set_setting(_CHAT_FALLBACK_KEY_MSGS.format(id=conversation_id), "[]")
        except Exception:
            pass
    except Exception as e:
        logger.warning("delete fallback fallita: %s", e)


def trim_chat_conversations(keep_last: int = 10):
    """Mantiene solo le ultime N conversazioni; elimina le altre."""
    global _CHAT_FALLBACK_MODE
    client = _get_client()
    if not _CHAT_FALLBACK_MODE:
        try:
            result = (client.table("chat_conversations")
                      .select("id")
                      .order("updated_at", desc=True)
                      .execute())
            rows = result.data or []
            if len(rows) <= keep_last:
                return
            to_delete = [r["id"] for r in rows[keep_last:]]
            for cid in to_delete:
                client.table("chat_messages").delete().eq("conversation_id", cid).execute()
                client.table("chat_conversations").delete().eq("id", cid).execute()
            return
        except Exception as e:
            if _chat_should_fallback(e):
                _CHAT_FALLBACK_MODE = True
            else:
                logger.warning("trim_chat_conversations fallita: %s", e)
                return
    try:
        lst = _chat_fallback_load_list()
        if len(lst) <= keep_last:
            return
        to_delete = lst[keep_last:]
        for cid in to_delete:
            try:
                set_setting(_CHAT_FALLBACK_KEY_CONV.format(id=cid), "")
                set_setting(_CHAT_FALLBACK_KEY_MSGS.format(id=cid), "[]")
            except Exception:
                pass
        _chat_fallback_save_list(lst[:keep_last])
    except Exception as e:
        logger.warning("trim fallback fallita: %s", e)


# ============================================================================
# Decision Chat — chat con i Decision Agent (Standard + Crypto), con
# capacita' di proporre ed eseguire trade. Tabelle SEPARATE da
# chat_conversations/chat_messages (che restano per Coach Cards / analyst).
#
# Schema:
#   decision_chat_conversations(id, agent_type TEXT, created_at, updated_at)
#   decision_chat_messages(id, conversation_id, role, content, proposed_trade JSONB,
#                          executed_trade_id BIGINT NULL, created_at)
#
# agent_type ∈ {"standard", "crypto"}. Una conversazione "attiva" per agent_type
# (la piu' recente). Il frontend chiede "dammi la conversation per agent_type X"
# e questo crea/recupera quella corrente.
# ============================================================================

# Flag globale fallback per le tabelle decision_chat_*
_DECISION_CHAT_FALLBACK_MODE = False
_DEC_CHAT_FALLBACK_KEY_LIST = "decision_chat_list_fallback"
_DEC_CHAT_FALLBACK_KEY_CONV = "dec_chat_conv_{aid}"   # per agent_type
_DEC_CHAT_FALLBACK_KEY_MSGS = "dec_chat_msgs_{cid}"


def _is_missing_column_err(exc: Exception) -> bool:
    """True per 'colonna inesistente'. A differenza degli errori transitori
    (schema cache reload), una colonna mancante e' PERSISTENTE: la tabella
    decision_chat_messages senza le colonne proposed_actions/
    executed_action_results (aggiunte nel codice ma non migrate su Supabase
    quando manca DATABASE_URL) fa fallire l'insert con questo errore. Per la
    decision-chat e' corretto ripiegare sul KV store schema-less, che salva
    l'intero messaggio (azioni/direttive incluse) senza vincoli di colonna.
    """
    s = str(exc).lower()
    if "pgrst204" in s:                              # PostgREST: column not found
        return True
    if "column" in s and "does not exist" in s:      # Postgres canonico
        return True
    if "could not find the" in s and "column" in s:  # PostgREST schema cache
        return True
    return False


def _dec_chat_should_fallback(exc: Exception) -> bool:
    # Tabella mancante OPPURE colonna mancante → schema proper inutilizzabile,
    # si ripiega sul KV store (preserva proposed_actions/ordini/direttive).
    # Entrambe le condizioni sono persistenti: il latch su KV e' corretto qui.
    return _is_missing_table_err(exc) or _is_missing_column_err(exc)


def get_or_create_decision_chat_conversation(agent_type: str) -> int | None:
    """
    Ritorna l'ID della conversazione attiva per agent_type. Se non esiste,
    ne crea una nuova. agent_type ∈ {"standard", "crypto"}.
    """
    global _DECISION_CHAT_FALLBACK_MODE
    agent_type = (agent_type or "standard").lower()
    if agent_type not in ("standard", "crypto"):
        agent_type = "standard"

    client = _get_client()
    if not _DECISION_CHAT_FALLBACK_MODE:
        try:
            # Cerca la piu' recente per agent_type
            result = (client.table("decision_chat_conversations")
                      .select("id")
                      .eq("agent_type", agent_type)
                      .order("updated_at", desc=True)
                      .limit(1)
                      .execute())
            if result.data and len(result.data) > 0:
                return result.data[0]["id"]
            # Non esiste: crea
            payload = {"agent_type": agent_type}
            result = client.table("decision_chat_conversations").insert(payload).execute()
            if result.data and len(result.data) > 0:
                return result.data[0].get("id")
            return None
        except Exception as e:
            if _dec_chat_should_fallback(e):
                logger.warning("get_or_create_decision_chat: switch a fallback (%s)", e)
                _DECISION_CHAT_FALLBACK_MODE = True
            else:
                logger.warning("get_or_create_decision_chat fallita: %s", e)
                return None

    # Fallback su settings
    try:
        import time as _t
        key = _DEC_CHAT_FALLBACK_KEY_CONV.format(aid=agent_type)
        existing = get_setting(key, "")
        if existing:
            try:
                return int(existing)
            except Exception:
                pass
        new_id = int(_t.time() * 1000)
        set_setting(key, str(new_id))
        return new_id
    except Exception as e:
        logger.warning("get_or_create_decision_chat fallback fallita: %s", e)
        return None


def _dec_chat_decode_row(r: dict) -> dict:
    """Decodifica i campi JSON-string di una riga decision_chat_messages."""
    for key in ("proposed_trade", "proposed_actions", "executed_action_results"):
        v = r.get(key)
        if v and isinstance(v, str):
            try:
                r[key] = json.loads(v)
            except Exception:
                r[key] = None
    return r


def insert_decision_chat_message(
    conversation_id: int, role: str, content: str,
    proposed_trade: dict | None = None,
    proposed_actions: list | None = None,
) -> int | None:
    """
    Aggiunge un messaggio. proposed_trade e' legacy (dict opzionale).
    proposed_actions e' la nuova interfaccia: lista di azioni dell'agente
    (trade / stop_loss / take_profit / directive) che l'utente puo' eseguire
    singolarmente.
    """
    global _DECISION_CHAT_FALLBACK_MODE
    client = _get_client()
    if not _DECISION_CHAT_FALLBACK_MODE:
        try:
            payload = {
                "conversation_id": conversation_id,
                "role": role,
                "content": content,
            }
            if proposed_trade:
                payload["proposed_trade"] = json.dumps(proposed_trade)
            if proposed_actions:
                payload["proposed_actions"] = json.dumps(proposed_actions)
            result = client.table("decision_chat_messages").insert(payload).execute()
            client.table("decision_chat_conversations").update({
                "updated_at": _now_iso(),
            }).eq("id", conversation_id).execute()
            if result.data and len(result.data) > 0:
                return result.data[0].get("id")
        except Exception as e:
            if _dec_chat_should_fallback(e):
                logger.warning("insert_decision_chat_message: switch a fallback")
                _DECISION_CHAT_FALLBACK_MODE = True
            else:
                logger.warning("insert_decision_chat_message fallita: %s", e)
                return None

    # Fallback su settings
    try:
        import time as _t
        msg_id = int(_t.time() * 1000)
        key = _DEC_CHAT_FALLBACK_KEY_MSGS.format(cid=conversation_id)
        msgs_str = get_setting(key, "[]")
        try:
            msgs = json.loads(msgs_str) if msgs_str else []
        except Exception:
            msgs = []
        msgs.append({
            "id": msg_id, "role": role, "content": content,
            "proposed_trade": proposed_trade,
            "proposed_actions": proposed_actions,
            "created_at": _now_iso(),
        })
        # Cap a 200 messaggi per conversazione
        set_setting(key, json.dumps(msgs[-200:]))
        return msg_id
    except Exception as e:
        logger.warning("insert_decision_chat_message fallback fallita: %s", e)
        return None


def _dec_chat_read_fallback_msgs(conversation_id: int, limit: int = 100) -> list:
    """
    Lettura unificata dei messaggi dal fallback settings store.
    Decodifica i campi JSON-string in dict/list per il frontend.
    """
    try:
        key = _DEC_CHAT_FALLBACK_KEY_MSGS.format(cid=conversation_id)
        msgs_str = get_setting(key, "[]")
        msgs = json.loads(msgs_str) if msgs_str else []
        if not isinstance(msgs, list):
            return []
        # Decodifica eventuali campi JSON-string (in caso di edge legacy)
        for m in msgs:
            if not isinstance(m, dict):
                continue
            for key_name in ("proposed_trade", "proposed_actions",
                              "executed_action_results"):
                v = m.get(key_name)
                if v and isinstance(v, str):
                    try:
                        m[key_name] = json.loads(v)
                    except Exception:
                        pass
        return msgs[-limit:]
    except Exception:
        return []


def get_decision_chat_messages(conversation_id: int, limit: int = 100) -> list:
    """
    Ritorna i messaggi di una conversazione, ordine cronologico.

    BUG FIX: se la tabella DB ritorna lista vuota MA il fallback store ha
    messaggi per questa conversation, ritorna i fallback. Questo evita
    che la chat appaia "vuota" quando i messaggi vivono in fallback store
    (es. l'INSERT su decision_chat_messages e' fallito ma il SELECT
    sulla tabella ritorna ok=[] perche' la conversation esiste ma e' vuota).

    Inoltre se ENTRAMBI hanno messaggi (caso edge post-recovery), unisce
    e ordina per timestamp — evitando perdita di history.
    """
    global _DECISION_CHAT_FALLBACK_MODE
    client = _get_client()
    db_rows: list = []
    db_ok = False
    if not _DECISION_CHAT_FALLBACK_MODE:
        try:
            result = (client.table("decision_chat_messages")
                      .select("*")
                      .eq("conversation_id", conversation_id)
                      .order("created_at", desc=True)
                      .order("id", desc=True)
                      .limit(limit)
                      .execute())
            # Prende i piu' RECENTI N (DESC+LIMIT) poi li rimette in ordine
            # cronologico. Prima ASC+LIMIT restituiva i piu' VECCHI N.
            db_rows = result.data or []
            db_rows.reverse()
            for r in db_rows:
                _dec_chat_decode_row(r)
            db_ok = True
        except Exception as e:
            if _dec_chat_should_fallback(e):
                _DECISION_CHAT_FALLBACK_MODE = True
            else:
                logger.warning("get_decision_chat_messages fallita: %s", e)

    # Leggi anche il fallback store: se ha messaggi che il DB NON ha
    # (perche' insert e' fallito), li includiamo.
    fallback_msgs = _dec_chat_read_fallback_msgs(conversation_id, limit)

    if db_ok and not fallback_msgs:
        return db_rows
    if not db_ok and fallback_msgs:
        return fallback_msgs
    if db_ok and fallback_msgs:
        # Merge: unisci ed ordina per (created_at | id), evitando dup
        seen_ids: set = set()
        merged: list = []
        for m in db_rows + fallback_msgs:
            if not isinstance(m, dict):
                continue
            mid = m.get("id")
            if mid in seen_ids:
                continue
            seen_ids.add(mid)
            merged.append(m)
        # Sort by created_at se disponibile, altrimenti per id
        def _sortkey(x):
            return (str(x.get("created_at") or ""), x.get("id") or 0)
        merged.sort(key=_sortkey)
        return merged[-limit:]
    # Entrambi vuoti
    return []


def clear_decision_chat(agent_type: str) -> bool:
    """Cancella la conversazione attiva per agent_type (e tutti i messaggi)."""
    global _DECISION_CHAT_FALLBACK_MODE
    agent_type = (agent_type or "standard").lower()
    client = _get_client()
    if not _DECISION_CHAT_FALLBACK_MODE:
        try:
            # Trova conv attiva
            r = (client.table("decision_chat_conversations")
                 .select("id")
                 .eq("agent_type", agent_type)
                 .execute())
            for row in (r.data or []):
                cid = row["id"]
                client.table("decision_chat_messages").delete().eq("conversation_id", cid).execute()
                client.table("decision_chat_conversations").delete().eq("id", cid).execute()
            return True
        except Exception as e:
            if _dec_chat_should_fallback(e):
                _DECISION_CHAT_FALLBACK_MODE = True
            else:
                logger.warning("clear_decision_chat fallita: %s", e)
                return False

    # Fallback
    try:
        key_conv = _DEC_CHAT_FALLBACK_KEY_CONV.format(aid=agent_type)
        existing = get_setting(key_conv, "")
        if existing:
            try:
                cid = int(existing)
                set_setting(_DEC_CHAT_FALLBACK_KEY_MSGS.format(cid=cid), "[]")
            except Exception:
                pass
        set_setting(key_conv, "")
        return True
    except Exception:
        return False


def get_recent_user_directives(agent_type: str, hours: int = 48, limit: int = 5,
                               since_iso: str | None = None) -> list:
    """
    Ritorna gli ultimi N messaggi UTENTE (role='user') di una chat decision
    per un certo agent_type.

    since_iso (one-shot): se passato, ritorna SOLO i messaggi creati DOPO
    quel timestamp (tipicamente l'ultimo run del Decision Agent). Cosi' una
    direttiva viene letta da UN solo run e non riproposta a ogni run per
    `hours`. Il cutoff effettivo e' il PIU' RECENTE tra (now - hours) e
    since_iso, cosi' anche con since_iso vecchio non si pescano direttive
    piu' vecchie della finestra di sicurezza.

    Returns: lista di dict con keys {content, created_at}.
    """
    global _DECISION_CHAT_FALLBACK_MODE
    agent_type = (agent_type or "standard").lower()
    client = _get_client()

    if not _DECISION_CHAT_FALLBACK_MODE:
        try:
            # Trova la conversazione attiva
            r = (client.table("decision_chat_conversations")
                 .select("id")
                 .eq("agent_type", agent_type)
                 .order("updated_at", desc=True)
                 .limit(1)
                 .execute())
            if not r.data:
                return []
            cid = r.data[0]["id"]
            # Calcola cutoff timestamp = max(now - hours, since_iso)
            from datetime import datetime, timezone, timedelta
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            if since_iso and since_iso > cutoff:
                cutoff = since_iso
            r2 = (client.table("decision_chat_messages")
                  .select("content, created_at")
                  .eq("conversation_id", cid)
                  .eq("role", "user")
                  .gte("created_at", cutoff)
                  .order("created_at", desc=True)
                  .limit(limit)
                  .execute())
            return r2.data or []
        except Exception as e:
            if _dec_chat_should_fallback(e):
                _DECISION_CHAT_FALLBACK_MODE = True
            else:
                logger.warning("get_recent_user_directives fallita: %s", e)
                return []

    # Fallback
    try:
        from datetime import datetime, timezone, timedelta
        key_conv = _DEC_CHAT_FALLBACK_KEY_CONV.format(aid=agent_type)
        existing = get_setting(key_conv, "")
        if not existing:
            return []
        cid = int(existing)
        msgs_str = get_setting(_DEC_CHAT_FALLBACK_KEY_MSGS.format(cid=cid), "[]")
        msgs = json.loads(msgs_str) if msgs_str else []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        if since_iso:
            try:
                since_dt = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
                if since_dt.tzinfo is None:
                    since_dt = since_dt.replace(tzinfo=timezone.utc)
                if since_dt > cutoff:
                    cutoff = since_dt
            except Exception:
                pass
        user_msgs = [m for m in msgs if m.get("role") == "user"]
        # Filter by time
        filtered = []
        for m in user_msgs:
            try:
                ts = datetime.fromisoformat(str(m.get("created_at", "")).replace("Z", "+00:00"))
                if ts >= cutoff:
                    filtered.append(m)
            except Exception:
                continue
        return filtered[-limit:]
    except Exception:
        return []


def mark_decision_chat_trade_executed(message_id: int, trade_id: int) -> bool:
    """
    Marca un messaggio chat come "trade eseguito" salvando il trade_id reale
    nel campo executed_trade_id. Cosi' il frontend puo' mostrare
    "✓ Eseguito" sulla card invece del bottone "Esegui".
    """
    global _DECISION_CHAT_FALLBACK_MODE
    client = _get_client()
    if not _DECISION_CHAT_FALLBACK_MODE:
        try:
            client.table("decision_chat_messages").update({
                "executed_trade_id": trade_id,
            }).eq("id", message_id).execute()
            return True
        except Exception as e:
            if _dec_chat_should_fallback(e):
                _DECISION_CHAT_FALLBACK_MODE = True
            else:
                logger.warning("mark_decision_chat_trade_executed fallita: %s", e)
                return False
    # Fallback: noop (i messaggi fallback non hanno executed_trade_id tracciato)
    return True


def _dec_chat_fallback_find_message(message_id: int) -> tuple[dict | None, int, list]:
    """
    Cerca un messaggio nel fallback settings store. Ritorna (msg, conv_id, msgs_list).
    Usato quando la tabella decision_chat_messages non e' disponibile e i
    messaggi vivono in settings (key "dec_chat_msgs_{cid}").

    Strategia: il fallback conv_id e' un timestamp-based (int(time.time()*1000)),
    NON in range piccolo. Devo recuperarlo dai settings "dec_chat_conv_{aid}"
    per ogni agent_type noto (standard, crypto).

    Anche scansiona la lista di conv_id stored in
    _DEC_CHAT_FALLBACK_KEY_LIST se presente.
    """
    candidate_cids: list[int] = []
    seen: set[int] = set()

    # Strategia 1: leggi i conv_id correnti per ogni agent_type known
    for agent_type in ("standard", "crypto"):
        try:
            key = _DEC_CHAT_FALLBACK_KEY_CONV.format(aid=agent_type)
            v = get_setting(key, "") or ""
            if v:
                try:
                    cid = int(v)
                    if cid not in seen:
                        candidate_cids.append(cid)
                        seen.add(cid)
                except Exception:
                    pass
        except Exception:
            pass

    # Strategia 2: lista esplicita dei conv_id (se presente)
    try:
        list_raw = get_setting(_DEC_CHAT_FALLBACK_KEY_LIST, "[]") or "[]"
        try:
            for cid in json.loads(list_raw) or []:
                try:
                    cid_int = int(cid)
                    if cid_int not in seen:
                        candidate_cids.append(cid_int)
                        seen.add(cid_int)
                except Exception:
                    continue
        except Exception:
            pass
    except Exception:
        pass

    # Strategia 3: fallback su range piccolo (compat con conv_id sequenziali
    # da prima del fallback timestamp-based)
    for cid in range(1, 50):
        if cid not in seen:
            candidate_cids.append(cid)
            seen.add(cid)

    # Cerca il messaggio nelle conversation in ordine di probabilita'
    for cid in candidate_cids:
        try:
            key = _DEC_CHAT_FALLBACK_KEY_MSGS.format(cid=cid)
            msgs_str = get_setting(key, "[]") or "[]"
            msgs = json.loads(msgs_str) if msgs_str else []
            if not isinstance(msgs, list):
                continue
            for m in msgs:
                if isinstance(m, dict) and m.get("id") == message_id:
                    return m, cid, msgs
        except Exception:
            continue
    return None, 0, []


def mark_decision_chat_action_executed(message_id: int, action_index: int,
                                       result: dict) -> bool:
    """
    Salva il risultato dell'esecuzione di una proposed_action della chat
    (stop-loss, take-profit, direttiva). I risultati sono indicizzati per
    posizione nell'array proposed_actions (key = action_index string).

    Supporta fallback mode: se il message_id non e' un id reale del DB
    (es. timestamp-based perche' la tabella ha rifiutato l'INSERT), cerca
    nei settings store e aggiorna in place.
    """
    global _DECISION_CHAT_FALLBACK_MODE
    client = _get_client()
    if not _DECISION_CHAT_FALLBACK_MODE:
        try:
            r = (client.table("decision_chat_messages")
                 .select("executed_action_results")
                 .eq("id", message_id)
                 .limit(1)
                 .execute())
            if r.data:
                current = {}
                ear = r.data[0].get("executed_action_results")
                if ear and isinstance(ear, str):
                    try:
                        current = json.loads(ear) or {}
                    except Exception:
                        current = {}
                elif isinstance(ear, dict):
                    current = ear
                current[str(action_index)] = result
                client.table("decision_chat_messages").update({
                    "executed_action_results": json.dumps(current),
                }).eq("id", message_id).execute()
                return True
            # Message NON trovato in tabella → potrebbe essere in fallback
            # (message_id timestamp-based). Cadiamo sotto.
        except Exception as e:
            if _dec_chat_should_fallback(e):
                _DECISION_CHAT_FALLBACK_MODE = True
            else:
                logger.warning("mark_decision_chat_action_executed fallita: %s", e)
                # NON return False: prova comunque il fallback

    # Fallback: cerca il messaggio nei settings store e aggiorna in place.
    msg, cid, msgs = _dec_chat_fallback_find_message(message_id)
    if msg is None or not msgs:
        logger.warning("mark_decision_chat_action_executed: msg %s non trovato "
                       "ne' in tabella ne' in fallback", message_id)
        return False
    try:
        current = msg.get("executed_action_results") or {}
        if isinstance(current, str):
            try:
                current = json.loads(current) or {}
            except Exception:
                current = {}
        if not isinstance(current, dict):
            current = {}
        current[str(action_index)] = result
        # Aggiorna l'entry e ri-scrive l'intero array nella setting
        for m in msgs:
            if isinstance(m, dict) and m.get("id") == message_id:
                m["executed_action_results"] = current
                break
        key = _DEC_CHAT_FALLBACK_KEY_MSGS.format(cid=cid)
        set_setting(key, json.dumps(msgs[-200:]))
        return True
    except Exception as e:
        logger.warning("mark_decision_chat_action_executed fallback err: %s", e)
        return False


def get_decision_chat_message(message_id: int) -> dict | None:
    """
    Ritorna un singolo messaggio per ID. Supporta sia la tabella DB sia
    il fallback settings store: se il message_id e' un timestamp-based
    (assistant_message_id quando l'insert table e' fallito), lo trova
    cercando in tutte le conversation fallback dei settings.
    """
    global _DECISION_CHAT_FALLBACK_MODE
    client = _get_client()
    if not _DECISION_CHAT_FALLBACK_MODE:
        try:
            r = (client.table("decision_chat_messages")
                 .select("*")
                 .eq("id", message_id)
                 .limit(1)
                 .execute())
            if r.data:
                return _dec_chat_decode_row(r.data[0])
            # NON found in table: cadiamo nel fallback search sotto
        except Exception as e:
            if _dec_chat_should_fallback(e):
                _DECISION_CHAT_FALLBACK_MODE = True
            else:
                logger.warning("get_decision_chat_message fallita: %s", e)

    # Fallback: cerca nei settings store (anche se non in fallback mode, per
    # gestire il caso "tabella ha rifiutato l'INSERT ma non sa di esserlo")
    msg, _cid, _msgs = _dec_chat_fallback_find_message(message_id)
    if msg is not None:
        # Decodifica eventuali campi JSON-string se presenti
        if isinstance(msg.get("proposed_actions"), str):
            try:
                msg["proposed_actions"] = json.loads(msg["proposed_actions"])
            except Exception:
                pass
        if isinstance(msg.get("executed_action_results"), str):
            try:
                msg["executed_action_results"] = json.loads(msg["executed_action_results"])
            except Exception:
                pass
        return msg
    return None


# ============================================================================
# Agent Commitments — memoria persistente delle "promesse"/intenzioni dei
# Decision Agent. Iniettate nel contesto dei run successivi finche' non sono
# triggered/expired/cancelled. agent_type ∈ {"standard", "crypto"}.
# ============================================================================

_AGENT_COMMIT_FALLBACK_MODE = False


def _agent_commit_should_fallback(exc: Exception) -> bool:
    return _is_missing_table_err(exc)


def _expire_old_commitments(client, agent_type: str) -> None:
    """Sweep one-shot: marca status='expired' tutte le righe attive con expires_at < now."""
    try:
        client.table("agent_commitments").update({
            "status": "expired",
            "resolved_at": _now_iso(),
            "resolved_reason": "auto-expired (deadline passed)",
        }).eq("agent_type", agent_type).eq("status", "active").lt(
            "expires_at", _now_iso()
        ).execute()
    except Exception as e:
        logger.debug("expire sweep noop: %s", e)


def add_agent_commitment(
    agent_type: str,
    commitment_type: str,
    condition_text: str,
    trigger_action: str | None = None,
    expires_in_hours: float | None = None,
    ticker: str | None = None,
    source_run_id: str | None = None,
    notes: str | None = None,
) -> int | None:
    """Inserisce un nuovo impegno e ritorna il suo id."""
    global _AGENT_COMMIT_FALLBACK_MODE
    agent_type = (agent_type or "standard").lower()
    if agent_type not in ("standard", "crypto"):
        agent_type = "standard"

    expires_at_iso = None
    if expires_in_hours and expires_in_hours > 0:
        expires_at_iso = (
            datetime.now(timezone.utc) + timedelta(hours=float(expires_in_hours))
        ).isoformat()

    client = _get_client()
    if not _AGENT_COMMIT_FALLBACK_MODE:
        try:
            payload = {
                "agent_type": agent_type,
                "commitment_type": commitment_type,
                "condition_text": (condition_text or "")[:2000],
                "trigger_action": (trigger_action or "")[:1000] if trigger_action else None,
                "expires_at": expires_at_iso,
                "status": "active",
                "ticker": (ticker or "").upper()[:32] if ticker else None,
                "source_run_id": source_run_id,
                "notes": (notes or "")[:1000] if notes else None,
            }
            r = client.table("agent_commitments").insert(payload).execute()
            if r.data:
                return r.data[0].get("id")
            return None
        except Exception as e:
            if _agent_commit_should_fallback(e):
                _AGENT_COMMIT_FALLBACK_MODE = True
                logger.warning("agent_commitments: switch a fallback (%s)", e)
            else:
                logger.warning("add_agent_commitment fallita: %s", e)
                return None

    # Fallback: salva su settings come lista JSON per agent_type
    try:
        import time as _t
        key = f"agent_commitments_fallback_{agent_type}"
        existing = get_setting(key, "[]")
        items = json.loads(existing) if existing else []
        new_id = int(_t.time() * 1000)
        items.append({
            "id": new_id,
            "agent_type": agent_type,
            "commitment_type": commitment_type,
            "condition_text": condition_text,
            "trigger_action": trigger_action,
            "expires_at": expires_at_iso,
            "status": "active",
            "ticker": ticker,
            "source_run_id": source_run_id,
            "notes": notes,
            "created_at": _now_iso(),
        })
        set_setting(key, json.dumps(items[-50:]))  # keep last 50
        return new_id
    except Exception as e:
        logger.warning("add_agent_commitment fallback fallita: %s", e)
        return None


def get_active_agent_commitments(agent_type: str, limit: int = 20) -> list:
    """Ritorna gli impegni ATTIVI per agent_type. Sweep automatico degli expired."""
    global _AGENT_COMMIT_FALLBACK_MODE
    agent_type = (agent_type or "standard").lower()
    client = _get_client()

    if not _AGENT_COMMIT_FALLBACK_MODE:
        try:
            _expire_old_commitments(client, agent_type)
            r = (client.table("agent_commitments")
                 .select("*")
                 .eq("agent_type", agent_type)
                 .eq("status", "active")
                 .order("created_at", desc=True)
                 .limit(limit)
                 .execute())
            return r.data or []
        except Exception as e:
            if _agent_commit_should_fallback(e):
                _AGENT_COMMIT_FALLBACK_MODE = True
            else:
                logger.warning("get_active_agent_commitments fallita: %s", e)
                return []

    # Fallback
    try:
        key = f"agent_commitments_fallback_{agent_type}"
        items = json.loads(get_setting(key, "[]") or "[]")
        now_iso = _now_iso()
        out = []
        for it in items:
            if it.get("status") != "active":
                continue
            exp = it.get("expires_at")
            if exp and exp < now_iso:
                it["status"] = "expired"
                continue
            out.append(it)
        # Persist sweep
        try:
            set_setting(key, json.dumps(items[-50:]))
        except Exception:
            pass
        out.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return out[:limit]
    except Exception:
        return []


def get_recent_agent_commitments(agent_type: str, limit: int = 30) -> list:
    """Ritorna gli ultimi N impegni (qualsiasi status) per la history view."""
    global _AGENT_COMMIT_FALLBACK_MODE
    agent_type = (agent_type or "standard").lower()
    client = _get_client()

    if not _AGENT_COMMIT_FALLBACK_MODE:
        try:
            r = (client.table("agent_commitments")
                 .select("*")
                 .eq("agent_type", agent_type)
                 .order("created_at", desc=True)
                 .limit(limit)
                 .execute())
            return r.data or []
        except Exception as e:
            if _agent_commit_should_fallback(e):
                _AGENT_COMMIT_FALLBACK_MODE = True
            else:
                logger.warning("get_recent_agent_commitments fallita: %s", e)
                return []
    try:
        key = f"agent_commitments_fallback_{agent_type}"
        items = json.loads(get_setting(key, "[]") or "[]")
        items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return items[:limit]
    except Exception:
        return []


def resolve_agent_commitment(
    commitment_id: int,
    status: str = "triggered",
    resolved_reason: str | None = None,
) -> bool:
    """Marca un impegno come triggered/cancelled/expired."""
    global _AGENT_COMMIT_FALLBACK_MODE
    if status not in ("triggered", "cancelled", "expired"):
        status = "cancelled"
    client = _get_client()

    if not _AGENT_COMMIT_FALLBACK_MODE:
        try:
            client.table("agent_commitments").update({
                "status": status,
                "resolved_at": _now_iso(),
                "resolved_reason": (resolved_reason or "")[:1000],
            }).eq("id", commitment_id).execute()
            return True
        except Exception as e:
            if _agent_commit_should_fallback(e):
                _AGENT_COMMIT_FALLBACK_MODE = True
            else:
                logger.warning("resolve_agent_commitment fallita: %s", e)
                return False

    # Fallback: cerca in entrambi i fallback (standard/crypto)
    try:
        for at in ("standard", "crypto"):
            key = f"agent_commitments_fallback_{at}"
            items = json.loads(get_setting(key, "[]") or "[]")
            changed = False
            for it in items:
                if int(it.get("id", 0)) == int(commitment_id):
                    it["status"] = status
                    it["resolved_at"] = _now_iso()
                    it["resolved_reason"] = resolved_reason or ""
                    changed = True
            if changed:
                set_setting(key, json.dumps(items[-50:]))
                return True
        return False
    except Exception as e:
        logger.warning("resolve fallback fallita: %s", e)
        return False


