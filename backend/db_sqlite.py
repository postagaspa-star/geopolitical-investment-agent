"""
Database module - Setup e queries SQLite per il sistema di investimento simulato.
"""
import logging
import sqlite3, os
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# Path del database: usa /data/portfolio.db su Render (disco persistente),
# altrimenti fallback locale
_default_db = os.path.join(os.path.dirname(__file__), "investment_agent.db")
DB_PATH = os.environ.get("DB_PATH", _default_db)

# Crea la directory del DB se non esiste (necessario per /data su Render)
_db_dir = os.path.dirname(DB_PATH)
if _db_dir and not os.path.exists(_db_dir):
    os.makedirs(_db_dir, exist_ok=True)

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    """Inizializza il database creando tutte le tabelle necessarie."""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS portfolio (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cash_balance REAL NOT NULL, total_value REAL NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL UNIQUE, quantity INTEGER NOT NULL,
                avg_buy_price REAL NOT NULL, current_price REAL NOT NULL DEFAULT 0,
                unrealized_pnl REAL NOT NULL DEFAULT 0,
                opened_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('BUY','SELL')),
                quantity INTEGER NOT NULL, price REAL NOT NULL, total_value REAL NOT NULL,
                geopolitical_reasoning TEXT, technical_reasoning TEXT,
                final_decision TEXT, confidence_score REAL,
                timestamp TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS agent_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                phase TEXT NOT NULL CHECK(phase IN ('GEOPOLITICAL','TECHNICAL','DECISION','ERROR','INFO','MARKET_INTELLIGENCE','CLAWSTREET_MIRROR')),
                content TEXT NOT NULL,
                timestamp TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS geopolitical_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                source TEXT NOT NULL CHECK(source IN ('GDELT','NEWSAPI')),
                raw_data TEXT, processed_summary TEXT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS technical_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                content TEXT NOT NULL,
                file_size INTEGER DEFAULT 0,
                uploaded_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS weekend_intelligence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                content TEXT NOT NULL,
                key_events TEXT,
                market_implications TEXT,
                saved_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS pre_market_briefings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                market_session TEXT,
                content TEXT NOT NULL,
                priority_assets TEXT,
                saved_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS processed_articles (
                url TEXT PRIMARY KEY,
                processed_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                total_value REAL NOT NULL,
                cash_balance REAL NOT NULL,
                timestamp TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)
        # Migrazione: ricreare agent_logs se ha il vecchio constraint
        # (SQLite non supporta ALTER TABLE per modificare CHECK constraint)
        try:
            conn.execute(
                "INSERT INTO agent_logs (run_id, phase, content) VALUES ('_migration_test', 'MARKET_INTELLIGENCE', 'test')"
            )
            conn.execute("DELETE FROM agent_logs WHERE run_id = '_migration_test'")
        except Exception:
            # Il vecchio constraint non accetta le nuove fasi: ricrea la tabella
            logger.info("Migrazione agent_logs: aggiornamento constraint phase...")
            rows = conn.execute("SELECT run_id, phase, content, timestamp FROM agent_logs").fetchall()
            conn.execute("DROP TABLE agent_logs")
            conn.execute("""
                CREATE TABLE agent_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    phase TEXT NOT NULL CHECK(phase IN ('GEOPOLITICAL','TECHNICAL','DECISION','ERROR','INFO','MARKET_INTELLIGENCE','CLAWSTREET_MIRROR')),
                    content TEXT NOT NULL,
                    timestamp TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            for r in rows:
                try:
                    conn.execute(
                        "INSERT INTO agent_logs (run_id, phase, content, timestamp) VALUES (?,?,?,?)",
                        (r["run_id"], r["phase"], r["content"], r["timestamp"]),
                    )
                except Exception:
                    pass
            logger.info("Migrazione agent_logs completata.")

        row = conn.execute("SELECT COUNT(*) as cnt FROM portfolio").fetchone()
        if row["cnt"] == 0:
            bal = float(os.environ.get("INITIAL_PORTFOLIO_BALANCE", 100000))
            conn.execute("INSERT INTO portfolio (cash_balance, total_value) VALUES (?,?)", (bal, bal))

        # ── Migrazione tabella trades: aggiunta colonne ClawStreet mirror ──
        # Permette di tracciare per ogni trade se è stato specchiato con
        # successo su ClawStreet e di riprovare le mirror fallite.
        # SQLite non supporta IF NOT EXISTS su ALTER TABLE → catch errore.
        for col_def in (
            "cs_mirror_status TEXT DEFAULT 'pending'",
            "cs_mirror_reason TEXT",
            "cs_mirror_attempts INTEGER DEFAULT 0",
            "cs_mirror_last_attempt_at TEXT",
        ):
            try:
                conn.execute(f"ALTER TABLE trades ADD COLUMN {col_def}")
            except Exception:
                pass  # colonna già esistente

        # ── Migrazione documenti: aggiunta colonna category ──
        # Permette di separare documenti generici (per Decision normale)
        # da documenti crypto-specific (per Decision Crypto).
        try:
            conn.execute("ALTER TABLE technical_documents ADD COLUMN category TEXT DEFAULT 'generic'")
        except Exception:
            pass
        # Backfill: documenti pre-existenti possono avere category=NULL anche
        # se è stata aggiunta la colonna con default — mettiamo 'generic'.
        try:
            conn.execute(
                "UPDATE technical_documents SET category='generic' WHERE category IS NULL"
            )
        except Exception:
            pass
        # Colonna is_preset per distinguere documenti precaricati (PDF crypto
        # forniti dall'utente) da quelli upload manuali. I preset non sono
        # eliminabili dall'UI per evitare cancellazioni accidentali.
        try:
            conn.execute("ALTER TABLE technical_documents ADD COLUMN is_preset INTEGER DEFAULT 0")
        except Exception:
            pass

def get_portfolio():
    with get_db() as conn:
        row = conn.execute("SELECT * FROM portfolio ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

def update_portfolio(cash_balance, total_value):
    with get_db() as conn:
        conn.execute("UPDATE portfolio SET cash_balance=?, total_value=?, updated_at=datetime('now') WHERE id=(SELECT MAX(id) FROM portfolio)", (cash_balance, total_value))

def get_positions():
    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM positions ORDER BY opened_at DESC").fetchall()]

def get_position(ticker):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM positions WHERE ticker=?", (ticker,)).fetchone()
        return dict(row) if row else None

def upsert_position(ticker, quantity, avg_buy_price, current_price=0):
    with get_db() as conn:
        existing = conn.execute("SELECT * FROM positions WHERE ticker=?", (ticker,)).fetchone()
        pnl = (current_price - avg_buy_price) * quantity if current_price > 0 else 0
        if existing:
            conn.execute("UPDATE positions SET quantity=?, avg_buy_price=?, current_price=?, unrealized_pnl=? WHERE ticker=?",
                         (quantity, avg_buy_price, current_price, pnl, ticker))
        else:
            conn.execute("INSERT INTO positions (ticker,quantity,avg_buy_price,current_price,unrealized_pnl) VALUES (?,?,?,?,?)",
                         (ticker, quantity, avg_buy_price, current_price, pnl))

def delete_position(ticker):
    with get_db() as conn:
        conn.execute("DELETE FROM positions WHERE ticker=?", (ticker,))

def update_position_price(ticker, current_price):
    with get_db() as conn:
        pos = conn.execute("SELECT * FROM positions WHERE ticker=?", (ticker,)).fetchone()
        if pos:
            pnl = (current_price - pos["avg_buy_price"]) * pos["quantity"]
            conn.execute("UPDATE positions SET current_price=?, unrealized_pnl=? WHERE ticker=?", (current_price, pnl, ticker))

def count_positions():
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) as cnt FROM positions").fetchone()["cnt"]

def insert_trade(ticker, action, quantity, price, geo_reasoning, tech_reasoning, final_decision, confidence):
    """
    Inserisce un trade e ritorna l'ID della riga creata.
    L'ID è necessario per linkare il trade allo stato del mirror ClawStreet.
    """
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO trades (ticker,action,quantity,price,total_value,geopolitical_reasoning,technical_reasoning,final_decision,confidence_score) VALUES (?,?,?,?,?,?,?,?,?)",
            (ticker, action, quantity, price, price*quantity, geo_reasoning, tech_reasoning, final_decision, confidence),
        )
        return cur.lastrowid

def get_trades(limit=50):
    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()]


def update_trade_mirror_status(trade_id, status, reason=None, increment_attempts=True):
    """
    Aggiorna lo stato del mirror ClawStreet per un trade.

    Args:
        trade_id: ID del trade nella tabella trades
        status: 'ok' | 'failed' | 'skipped' | 'pending' | 'no_creds' | 'unsupported'
        reason: dettaglio errore (es. HTTP 500, INVALID_SYMBOL, ...)
        increment_attempts: True per incrementare il contatore tentativi
    """
    with get_db() as conn:
        if increment_attempts:
            conn.execute(
                "UPDATE trades SET cs_mirror_status=?, cs_mirror_reason=?, "
                "cs_mirror_attempts=COALESCE(cs_mirror_attempts,0)+1, "
                "cs_mirror_last_attempt_at=datetime('now') WHERE id=?",
                (status, (reason or "")[:500], trade_id),
            )
        else:
            conn.execute(
                "UPDATE trades SET cs_mirror_status=?, cs_mirror_reason=?, "
                "cs_mirror_last_attempt_at=datetime('now') WHERE id=?",
                (status, (reason or "")[:500], trade_id),
            )


def get_pending_mirror_trades(window_hours=24, max_attempts=5, limit=50):
    """
    Ritorna i trade non ancora specchiati su ClawStreet (status in 'pending'/'failed')
    nelle ultime `window_hours` ore, esclusi quelli che hanno già fallito >max_attempts volte.
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM trades "
            "WHERE (cs_mirror_status IS NULL "
            "       OR cs_mirror_status IN ('pending','failed')) "
            "  AND COALESCE(cs_mirror_attempts,0) < ? "
            "  AND timestamp >= datetime('now', ?) "
            "ORDER BY timestamp ASC LIMIT ?",
            (max_attempts, f"-{window_hours} hours", limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_mirror_status_summary():
    """Conteggio trade per stato mirror (ultimi 7 giorni). Utile per diagnostica."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT COALESCE(cs_mirror_status,'pending') as status, COUNT(*) as cnt "
            "FROM trades WHERE timestamp >= datetime('now','-7 days') "
            "GROUP BY cs_mirror_status"
        ).fetchall()
        return {r["status"]: r["cnt"] for r in rows}

def insert_agent_log(run_id, phase, content):
    with get_db() as conn:
        conn.execute("INSERT INTO agent_logs (run_id,phase,content) VALUES (?,?,?)", (run_id, phase, content))

def get_agent_logs(limit=100):
    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM agent_logs ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()]

def get_logs_by_run(run_id):
    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM agent_logs WHERE run_id=? ORDER BY timestamp ASC", (run_id,)).fetchall()]

def insert_geopolitical_snapshot(run_id, source, raw_data, processed_summary):
    with get_db() as conn:
        conn.execute("INSERT INTO geopolitical_snapshots (run_id,source,raw_data,processed_summary) VALUES (?,?,?,?)",
                     (run_id, source, raw_data, processed_summary))

def get_geopolitical_snapshots(limit=20):
    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM geopolitical_snapshots ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()]


# --- Funzioni per le impostazioni ---

def get_setting(key, default=None):
    """Restituisce il valore di una impostazione, o il default se non esiste."""
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

def set_setting(key, value):
    """Inserisce o aggiorna una impostazione."""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
            (key, value))

def get_all_settings():
    """Restituisce tutte le impostazioni come dizionario piatto {chiave: valore}."""
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}


# --- Funzioni per i documenti tecnici ---

def insert_document(filename, content, file_size=0, category="generic", is_preset=False):
    """Inserisce un nuovo documento tecnico. category: 'generic' o 'crypto'."""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO technical_documents (filename, content, file_size, category, is_preset) VALUES (?,?,?,?,?)",
            (filename, content, file_size, category, 1 if is_preset else 0),
        )

def upsert_preset_document(filename, content, file_size=0, category="crypto"):
    """
    Inserisce o aggiorna un preset (filename = key univoca).
    Idempotente — chiamato all'avvio per i documenti in preset_documents/.
    """
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM technical_documents WHERE filename=? AND COALESCE(is_preset,0)=1",
            (filename,),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE technical_documents SET content=?, file_size=?, category=? WHERE id=?",
                (content, file_size, category, existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO technical_documents (filename, content, file_size, category, is_preset) "
                "VALUES (?,?,?,?,1)",
                (filename, content, file_size, category),
            )

def get_documents(category=None):
    """
    Restituisce tutti i documenti tecnici (lista, senza contenuto completo).
    Se category è specificato, filtra (es. 'generic' o 'crypto').
    """
    with get_db() as conn:
        cols = ("id, filename, file_size, uploaded_at, "
                "COALESCE(category,'generic') as category, "
                "COALESCE(is_preset,0) as is_preset")
        if category is not None:
            rows = conn.execute(
                f"SELECT {cols} FROM technical_documents "
                f"WHERE COALESCE(category,'generic')=? ORDER BY uploaded_at DESC",
                (category,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {cols} FROM technical_documents ORDER BY uploaded_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

def get_document_contents(category=None):
    """
    Restituisce il contenuto dei documenti per iniezione nel prompt.
    Se category è specificato, filtra (es. 'crypto' per Decision Crypto).
    """
    with get_db() as conn:
        if category is not None:
            rows = conn.execute(
                "SELECT id, filename, content, COALESCE(category,'generic') as category "
                "FROM technical_documents WHERE COALESCE(category,'generic')=? "
                "ORDER BY uploaded_at ASC",
                (category,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, filename, content, COALESCE(category,'generic') as category "
                "FROM technical_documents ORDER BY uploaded_at ASC"
            ).fetchall()
        return [dict(r) for r in rows]

def delete_document(doc_id):
    """Elimina un documento tecnico per ID."""
    with get_db() as conn:
        conn.execute("DELETE FROM technical_documents WHERE id=?", (doc_id,))

def reset_portfolio_data(new_balance):
    """Resetta il portafoglio: elimina posizioni, trade, snapshots e reinizializza il saldo."""
    with get_db() as conn:
        conn.execute("DELETE FROM positions")
        conn.execute("DELETE FROM trades")
        conn.execute("DELETE FROM portfolio_snapshots")
        conn.execute("DELETE FROM agent_logs")
        conn.execute("UPDATE portfolio SET cash_balance=?, total_value=?, updated_at=datetime('now') WHERE id=(SELECT MAX(id) FROM portfolio)",
                     (new_balance, new_balance))
    # Aggiorna anche la impostazione initial_balance
    set_setting("initial_balance", str(new_balance))


# --- Funzioni per weekend intelligence ---

def insert_weekend_intelligence(run_id, content, key_events, market_implications):
    """Inserisce un'analisi geopolitica del weekend."""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO weekend_intelligence (run_id, content, key_events, market_implications) VALUES (?,?,?,?)",
            (run_id, content, key_events, market_implications))

def get_weekend_intelligence(limit=20):
    """Restituisce le ultime analisi weekend."""
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM weekend_intelligence ORDER BY saved_at DESC LIMIT ?", (limit,)).fetchall()]

def get_latest_weekend_intelligence():
    """Restituisce l'ultima analisi weekend disponibile."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM weekend_intelligence ORDER BY saved_at DESC LIMIT 1").fetchone()
        return dict(row) if row else None


# --- Funzioni per pre-market briefings ---

def insert_pre_market_briefing(run_id, market_session, content, priority_assets):
    """Inserisce un briefing pre-market."""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO pre_market_briefings (run_id, market_session, content, priority_assets) VALUES (?,?,?,?)",
            (run_id, market_session, content, priority_assets))

def get_pre_market_briefings(limit=20):
    """Restituisce gli ultimi briefing pre-market."""
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM pre_market_briefings ORDER BY saved_at DESC LIMIT ?", (limit,)).fetchall()]

def get_latest_pre_market_briefing():
    """Restituisce l'ultimo briefing pre-market disponibile."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM pre_market_briefings ORDER BY saved_at DESC LIMIT 1").fetchone()
        return dict(row) if row else None


# --- Funzioni per la deduplicazione articoli ---

def is_article_processed(url):
    """Verifica se un articolo e' gia' stato processato."""
    with get_db() as conn:
        row = conn.execute("SELECT url FROM processed_articles WHERE url=?", (url,)).fetchone()
        return row is not None

def mark_article_processed(url):
    """Segna un articolo come processato."""
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO processed_articles (url, processed_at) VALUES (?, datetime('now'))",
            (url,))

def count_new_articles_since(hours=2):
    """Conta gli articoli nuovi nelle ultime N ore."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM processed_articles WHERE processed_at >= datetime('now', ?)",
            (f'-{hours} hours',)).fetchone()
        return row["cnt"] if row else 0

def cleanup_old_processed_articles(days=7):
    """Rimuove articoli processati piu' vecchi di N giorni."""
    with get_db() as conn:
        conn.execute(
            "DELETE FROM processed_articles WHERE processed_at < datetime('now', ?)",
            (f'-{days} days',))

def insert_portfolio_snapshot(total_value, cash_balance):
    """Salva uno snapshot del valore del portafoglio."""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO portfolio_snapshots (total_value, cash_balance) VALUES (?,?)",
            (total_value, cash_balance))

def get_portfolio_history(days=30):
    """Restituisce lo storico del valore del portafoglio."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT total_value, cash_balance, timestamp FROM portfolio_snapshots "
            "WHERE timestamp >= datetime('now', ?) ORDER BY timestamp ASC",
            (f'-{days} days',)).fetchall()
        return [dict(r) for r in rows]


def get_client():
    """SQLite stub — v4 tables not supported locally. Returns None."""
    return None
