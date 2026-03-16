"""
Database module - Setup e queries SQLite per il sistema di investimento simulato.
"""
import sqlite3, os
from contextlib import contextmanager

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
                phase TEXT NOT NULL CHECK(phase IN ('GEOPOLITICAL','TECHNICAL','DECISION','ERROR','INFO')),
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
        row = conn.execute("SELECT COUNT(*) as cnt FROM portfolio").fetchone()
        if row["cnt"] == 0:
            bal = float(os.environ.get("INITIAL_PORTFOLIO_BALANCE", 100000))
            conn.execute("INSERT INTO portfolio (cash_balance, total_value) VALUES (?,?)", (bal, bal))

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
    with get_db() as conn:
        conn.execute("INSERT INTO trades (ticker,action,quantity,price,total_value,geopolitical_reasoning,technical_reasoning,final_decision,confidence_score) VALUES (?,?,?,?,?,?,?,?,?)",
                     (ticker, action, quantity, price, price*quantity, geo_reasoning, tech_reasoning, final_decision, confidence))

def get_trades(limit=50):
    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()]

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
    """Restituisce tutte le impostazioni come dizionario."""
    with get_db() as conn:
        rows = conn.execute("SELECT key, value, updated_at FROM settings").fetchall()
        return {r["key"]: {"value": r["value"], "updated_at": r["updated_at"]} for r in rows}


# --- Funzioni per i documenti tecnici ---

def insert_document(filename, content, file_size=0):
    """Inserisce un nuovo documento tecnico."""
    with get_db() as conn:
        conn.execute("INSERT INTO technical_documents (filename, content, file_size) VALUES (?,?,?)",
                     (filename, content, file_size))

def get_documents():
    """Restituisce tutti i documenti tecnici (senza contenuto completo per la lista)."""
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, filename, file_size, uploaded_at FROM technical_documents ORDER BY uploaded_at DESC").fetchall()]

def get_document_contents():
    """Restituisce il contenuto di tutti i documenti tecnici (per iniezione nel prompt)."""
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, filename, content FROM technical_documents ORDER BY uploaded_at ASC").fetchall()]

def delete_document(doc_id):
    """Elimina un documento tecnico per ID."""
    with get_db() as conn:
        conn.execute("DELETE FROM technical_documents WHERE id=?", (doc_id,))

def reset_portfolio_data(new_balance):
    """Resetta il portafoglio: elimina posizioni, trade, e reinizializza il saldo."""
    with get_db() as conn:
        conn.execute("DELETE FROM positions")
        conn.execute("DELETE FROM trades")
        conn.execute("UPDATE portfolio SET cash_balance=?, total_value=?, updated_at=datetime('now') WHERE id=(SELECT MAX(id) FROM portfolio)",
                     (new_balance, new_balance))


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
