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

        # Auto-migrate v6: colonne ClawStreet mirror tracking
        # (idempotente, gira ad ogni avvio: ALTER TABLE IF NOT EXISTS)
        _ensure_cs_mirror_columns()

    except Exception as e:
        logger.error("Errore connessione Supabase: %s", e, exc_info=True)
        raise


def _ensure_cs_mirror_columns():
    """
    Auto-migrazione v6: aggiunge colonne cs_mirror_* alla tabella trades su
    Supabase Postgres. Idempotente — usa IF NOT EXISTS, gira ad ogni avvio.

    Richiede uno tra:
      - DATABASE_URL: postgres://...:...@.../postgres (raccomandato)
      - SUPABASE_DB_PASSWORD: la password del DB (postgres user)
        + SUPABASE_URL per derivare l'host
    Se nessuno è configurato, logga warning e continua (degrade graceful:
    il sistema usa try/except su insert_trade e simili, quindi funziona
    senza tracking ma il retry job non recupera i pending).
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
            "Auto-migrazione cs_mirror_* SKIPPED: configurare DATABASE_URL "
            "(consigliato) oppure SUPABASE_DB_PASSWORD su Render. "
            "In alternativa eseguire manualmente backend/migrations/init_v6_cs_mirror.sql "
            "su Supabase Dashboard → SQL Editor."
        )
        return

    try:
        import psycopg2
    except ImportError:
        logger.warning("psycopg2 non installato — auto-migrazione cs_mirror_* skipped.")
        return

    migration_sql = """
        ALTER TABLE trades ADD COLUMN IF NOT EXISTS cs_mirror_status TEXT DEFAULT 'pending';
        ALTER TABLE trades ADD COLUMN IF NOT EXISTS cs_mirror_reason TEXT;
        ALTER TABLE trades ADD COLUMN IF NOT EXISTS cs_mirror_attempts INTEGER DEFAULT 0;
        ALTER TABLE trades ADD COLUMN IF NOT EXISTS cs_mirror_last_attempt_at TIMESTAMPTZ;
        CREATE INDEX IF NOT EXISTS idx_trades_cs_mirror_status
            ON trades (cs_mirror_status, timestamp)
            WHERE cs_mirror_status IN ('pending', 'failed');
        UPDATE trades SET cs_mirror_status = 'legacy_unknown'
            WHERE cs_mirror_status IS NULL;
        -- v7: documenti separati per Decision normale vs Decision Crypto
        ALTER TABLE technical_documents ADD COLUMN IF NOT EXISTS category TEXT DEFAULT 'generic';
        -- backfill: i documenti caricati prima della migration hanno category=NULL.
        -- Senza questo UPDATE il filtro WHERE category='generic' li escluderebbe
        -- → Decision normale perderebbe tutti i documenti già caricati.
        UPDATE technical_documents SET category = 'generic' WHERE category IS NULL;
    """

    try:
        with psycopg2.connect(db_url, connect_timeout=15) as conn:
            with conn.cursor() as cur:
                cur.execute(migration_sql)
            conn.commit()
        logger.info("Auto-migrazione cs_mirror_* applicata su Supabase con successo.")
    except Exception as exc:
        # Non bloccare l'avvio del bot — degrade graceful
        logger.warning(
            "Auto-migrazione cs_mirror_* fallita (%s). Esegui manualmente "
            "backend/migrations/init_v6_cs_mirror.sql su Supabase Dashboard.",
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


def count_positions():
    client = _get_client()
    result = client.table("positions").select("id", count="exact").execute()
    return result.count or 0


# ============================================================
# Trades
# ============================================================

def insert_trade(ticker, action, quantity, price, geo_reasoning, tech_reasoning, final_decision, confidence):
    """
    Inserisce un trade e ritorna l'ID della riga creata (necessario per
    linkare il trade allo stato del mirror ClawStreet).
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
    # Aggiungi i campi mirror solo se la migration è stata applicata
    # (gestione degrade graceful: se le colonne non esistono, ritenta senza)
    payload_with_mirror = {**payload, "cs_mirror_status": "pending", "cs_mirror_attempts": 0}
    try:
        result = client.table("trades").insert(payload_with_mirror).execute()
    except Exception:
        result = client.table("trades").insert(payload).execute()
    if result.data and len(result.data) > 0:
        return result.data[0].get("id")
    return None


def get_trades(limit=50):
    client = _get_client()
    result = client.table("trades").select("*").order("timestamp", desc=True).limit(limit).execute()
    return result.data or []


def update_trade_mirror_status(trade_id, status, reason=None, increment_attempts=True):
    """Aggiorna stato del mirror ClawStreet per un trade (Supabase)."""
    if not trade_id:
        return
    from datetime import datetime as _dt, timezone as _tz
    client = _get_client()
    update_fields = {
        "cs_mirror_status": status,
        "cs_mirror_reason": (reason or "")[:500],
        "cs_mirror_last_attempt_at": _dt.now(_tz.utc).isoformat(),
    }
    try:
        if increment_attempts:
            current = client.table("trades").select("cs_mirror_attempts").eq("id", trade_id).execute()
            cur_n = (current.data[0].get("cs_mirror_attempts") if current.data else 0) or 0
            update_fields["cs_mirror_attempts"] = cur_n + 1
        client.table("trades").update(update_fields).eq("id", trade_id).execute()
    except Exception as exc:
        import logging as _log
        _log.getLogger(__name__).warning("update_trade_mirror_status fallita: %s", exc)


def get_pending_mirror_trades(window_hours=24, max_attempts=5, limit=50):
    """Trade non ancora specchiati su ClawStreet (Supabase)."""
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    cutoff = (_dt.now(_tz.utc) - _td(hours=window_hours)).isoformat()
    client = _get_client()
    try:
        result = (client.table("trades")
                  .select("*")
                  .gte("timestamp", cutoff)
                  .lt("cs_mirror_attempts", max_attempts)
                  .in_("cs_mirror_status", ["pending", "failed"])
                  .order("timestamp", desc=False)
                  .limit(limit)
                  .execute())
        return result.data or []
    except Exception as exc:
        import logging as _log
        _log.getLogger(__name__).warning("get_pending_mirror_trades fallita: %s", exc)
        return []


def get_mirror_status_summary():
    """Conteggio trade per stato mirror (ultimi 7 giorni)."""
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    cutoff = (_dt.now(_tz.utc) - _td(days=7)).isoformat()
    client = _get_client()
    try:
        result = client.table("trades").select("cs_mirror_status").gte("timestamp", cutoff).execute()
        rows = result.data or []
        summary = {}
        for r in rows:
            key = r.get("cs_mirror_status") or "pending"
            summary[key] = summary.get(key, 0) + 1
        return summary
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

def insert_document(filename, content, file_size=0, category="generic"):
    client = _get_client()
    payload = {
        "filename": filename,
        "content": content,
        "file_size": file_size,
    }
    # Aggiungi category solo se la migration è stata applicata. Tentiamo
    # con category, fallback senza in caso di colonna mancante.
    payload_cat = {**payload, "category": category}
    try:
        client.table("technical_documents").insert(payload_cat).execute()
    except Exception:
        client.table("technical_documents").insert(payload).execute()


def get_documents(category=None):
    """Lista documenti. Se category specificata ('generic' o 'crypto'), filtra."""
    client = _get_client()
    try:
        q = client.table("technical_documents").select(
            "id, filename, file_size, uploaded_at, category"
        ).order("uploaded_at", desc=True)
        if category is not None:
            q = q.eq("category", category)
        result = q.execute()
        return result.data or []
    except Exception:
        # Fallback se colonna category non esiste
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
    client = _get_client()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    result = client.table("portfolio_snapshots").select("total_value, cash_balance, timestamp").gte("timestamp", since).order("timestamp").execute()
    return result.data or []
