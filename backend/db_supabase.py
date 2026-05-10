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
            proposed_trade TEXT,           -- JSON string con {ticker,action,quantity,...}
            executed_trade_id BIGINT,      -- FK a trades.id quando l'utente conferma l'esecuzione
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
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
    client = _get_client()
    # Prendi l'id piu' alto
    row = client.table("portfolio").select("id").order("id", desc=True).limit(1).execute()
    if row.data:
        client.table("portfolio").update({
            "cash_balance": cash_balance,
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


def upsert_position(ticker, quantity, avg_buy_price, current_price=0):
    client = _get_client()
    pnl = (current_price - avg_buy_price) * quantity if current_price > 0 else 0
    existing = get_position(ticker)
    if existing:
        client.table("positions").update({
            "quantity": quantity,
            "avg_buy_price": avg_buy_price,
            "current_price": current_price,
            "unrealized_pnl": pnl,
        }).eq("ticker", ticker).execute()
    else:
        client.table("positions").insert({
            "ticker": ticker,
            "quantity": quantity,
            "avg_buy_price": avg_buy_price,
            "current_price": current_price,
            "unrealized_pnl": pnl,
        }).execute()


def delete_position(ticker):
    client = _get_client()
    client.table("positions").delete().eq("ticker", ticker).execute()


def update_position_price(ticker, current_price):
    client = _get_client()
    pos = get_position(ticker)
    if pos:
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

def insert_trade(ticker, action, quantity, price, geo_reasoning, tech_reasoning, final_decision, confidence):
    """
    Inserisce un trade e ritorna l'ID della riga creata.
    """
    client = _get_client()
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
    result = client.table("trades").insert(payload).execute()
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
# Settings
# ============================================================

def get_setting(key, default=None):
    client = _get_client()
    result = client.table("settings").select("value").eq("key", key).limit(1).execute()
    return result.data[0]["value"] if result.data else default


def set_setting(key, value):
    client = _get_client()
    client.table("settings").upsert({
        "key": key,
        "value": str(value),
        "updated_at": _now_iso(),
    }).execute()


def get_all_settings():
    client = _get_client()
    result = client.table("settings").select("key, value").execute()
    return {r["key"]: r["value"] for r in (result.data or [])}


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
    client = _get_client()
    client.table("portfolio_snapshots").insert({
        "total_value": total_value,
        "cash_balance": cash_balance,
    }).execute()


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


def _chat_should_fallback(exc: Exception) -> bool:
    """Decide se l'errore indica che la tabella manca → switch a fallback mode."""
    s = str(exc).lower()
    return ("pgrst205" in s
            or "does not exist" in s
            or "no such table" in s
            or "could not find the table" in s
            or "schema cache" in s)


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


def _dec_chat_should_fallback(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(s in msg for s in [
        "does not exist", "no such table", "relation",
        "schema cache", "could not find", "pgrst205",
    ])


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


def insert_decision_chat_message(
    conversation_id: int, role: str, content: str,
    proposed_trade: dict | None = None,
) -> int | None:
    """
    Aggiunge un messaggio. proposed_trade e' un dict opzionale con i campi
    {ticker, action, quantity, reasoning, confidence}. Se la risposta
    dell'agente non propone un trade, lascia None.
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
            "created_at": _now_iso(),
        })
        # Cap a 200 messaggi per conversazione
        set_setting(key, json.dumps(msgs[-200:]))
        return msg_id
    except Exception as e:
        logger.warning("insert_decision_chat_message fallback fallita: %s", e)
        return None


def get_decision_chat_messages(conversation_id: int, limit: int = 100) -> list:
    """Ritorna i messaggi di una conversazione, ordine cronologico."""
    global _DECISION_CHAT_FALLBACK_MODE
    client = _get_client()
    if not _DECISION_CHAT_FALLBACK_MODE:
        try:
            result = (client.table("decision_chat_messages")
                      .select("*")
                      .eq("conversation_id", conversation_id)
                      .order("created_at", desc=False)
                      .limit(limit)
                      .execute())
            rows = result.data or []
            # Decode proposed_trade JSON string → dict
            for r in rows:
                pt = r.get("proposed_trade")
                if pt and isinstance(pt, str):
                    try:
                        r["proposed_trade"] = json.loads(pt)
                    except Exception:
                        r["proposed_trade"] = None
            return rows
        except Exception as e:
            if _dec_chat_should_fallback(e):
                _DECISION_CHAT_FALLBACK_MODE = True
            else:
                logger.warning("get_decision_chat_messages fallita: %s", e)
                return []

    # Fallback
    try:
        key = _DEC_CHAT_FALLBACK_KEY_MSGS.format(cid=conversation_id)
        msgs_str = get_setting(key, "[]")
        msgs = json.loads(msgs_str) if msgs_str else []
        return msgs[-limit:]
    except Exception:
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


def get_recent_user_directives(agent_type: str, hours: int = 48, limit: int = 5) -> list:
    """
    Ritorna gli ultimi N messaggi UTENTE (role='user') di una chat decision
    per un certo agent_type, dalle ultime `hours` ore. Usato dal Decision
    Agent autonomo per leggere le preferenze recenti dell'utente.

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
            # Calcola cutoff timestamp
            from datetime import datetime, timezone, timedelta
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
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


def get_decision_chat_message(message_id: int) -> dict | None:
    """Ritorna un singolo messaggio per ID (usato da execute-trade endpoint)."""
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
                row = r.data[0]
                pt = row.get("proposed_trade")
                if pt and isinstance(pt, str):
                    try:
                        row["proposed_trade"] = json.loads(pt)
                    except Exception:
                        row["proposed_trade"] = None
                return row
            return None
        except Exception as e:
            if _dec_chat_should_fallback(e):
                _DECISION_CHAT_FALLBACK_MODE = True
            else:
                logger.warning("get_decision_chat_message fallita: %s", e)
                return None
    # Fallback: scan settings (slow but rare)
    return None


# ============================================================================
# Agent Commitments — memoria persistente delle "promesse"/intenzioni dei
# Decision Agent. Iniettate nel contesto dei run successivi finche' non sono
# triggered/expired/cancelled. agent_type ∈ {"standard", "crypto"}.
# ============================================================================

_AGENT_COMMIT_FALLBACK_MODE = False


def _agent_commit_should_fallback(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(s in msg for s in [
        "does not exist", "no such table", "relation",
        "schema cache", "could not find", "pgrst205",
    ])


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


