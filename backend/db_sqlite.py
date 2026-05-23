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
                direction TEXT NOT NULL DEFAULT 'LONG',
                opened_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('BUY','SELL')),
                quantity INTEGER NOT NULL, price REAL NOT NULL, total_value REAL NOT NULL,
                geopolitical_reasoning TEXT, technical_reasoning TEXT,
                final_decision TEXT, confidence_score REAL,
                direction TEXT NOT NULL DEFAULT 'LONG',
                timestamp TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS agent_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                phase TEXT NOT NULL CHECK(phase IN ('GEOPOLITICAL','TECHNICAL','DECISION','ERROR','INFO','MARKET_INTELLIGENCE')),
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
            -- Chat assistant: conversazioni con l'analista AI (DeepSeek-R1).
            -- Ogni conversazione ha un titolo (auto-generato dal primo
            -- messaggio) e una lista di messaggi con ruolo user/assistant.
            -- selected_decisions e' un JSON array di trade_id usati come
            -- contesto per quella specifica conversazione.
            CREATE TABLE IF NOT EXISTS chat_conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL DEFAULT 'Nuova conversazione',
                selected_decisions TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (conversation_id) REFERENCES chat_conversations(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_chat_messages_conv
                ON chat_messages (conversation_id, created_at);

            -- v12: decision chat (chat con i Decision Agent, separata)
            CREATE TABLE IF NOT EXISTS decision_chat_conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_type TEXT NOT NULL DEFAULT 'standard'
                    CHECK(agent_type IN ('standard','crypto')),
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS decision_chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
                content TEXT NOT NULL,
                proposed_trade TEXT,
                executed_trade_id INTEGER,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (conversation_id) REFERENCES decision_chat_conversations(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_dec_chat_messages_conv
                ON decision_chat_messages (conversation_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_dec_chat_conv_agent
                ON decision_chat_conversations (agent_type, updated_at DESC);
            -- v13: agent_commitments — memoria persistente delle "promesse" del Decision Agent
            CREATE TABLE IF NOT EXISTS agent_commitments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_type TEXT NOT NULL CHECK(agent_type IN ('standard','crypto')),
                ticker TEXT,
                commitment_type TEXT NOT NULL CHECK(commitment_type IN
                    ('monitor','conditional_buy','conditional_sell','watch_event','reminder')),
                condition_text TEXT NOT NULL,
                trigger_action TEXT,
                expires_at TEXT,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('active','triggered','expired','cancelled')),
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                resolved_at TEXT,
                resolved_reason TEXT,
                source_run_id TEXT,
                notes TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_agent_commit_active
                ON agent_commitments (agent_type, status, expires_at);
            CREATE INDEX IF NOT EXISTS idx_agent_commit_recent
                ON agent_commitments (agent_type, created_at DESC);
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
                    phase TEXT NOT NULL CHECK(phase IN ('GEOPOLITICAL','TECHNICAL','DECISION','ERROR','INFO','MARKET_INTELLIGENCE')),
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

        # ── Migrazione tabella positions: SL/TP automatici impostati dall'agente ──
        # Quando il prezzo corrente raggiunge questi livelli, price_polling
        # esegue la chiusura automatica via auto_exit job. 0 = non impostato.
        for col_def in (
            "stop_loss_price REAL DEFAULT 0",
            "take_profit_price REAL DEFAULT 0",
            "auto_exit_set_at TEXT",
            "auto_exit_set_by TEXT",
        ):
            try:
                conn.execute(f"ALTER TABLE positions ADD COLUMN {col_def}")
            except Exception:
                pass

        # ── Short selling: tag direction LONG/SHORT su positions e trades ──
        # 'LONG' di default → tutte le righe esistenti restano corrette
        # (erano tutte posizioni/trade long).
        for _tbl in ("positions", "trades"):
            try:
                conn.execute(
                    f"ALTER TABLE {_tbl} ADD COLUMN direction TEXT "
                    f"NOT NULL DEFAULT 'LONG'")
            except Exception:
                pass

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

        # ── v14: actionable chat — la chat decisionale puo' proporre azioni
        #    diverse dal solo trade (stop-loss, take-profit, direttive utente).
        #    Salviamo l'array JSON in proposed_actions; executed_action_results
        #    e' un dict {action_index_str: result_dict} con esiti per azione.
        for col_def in (
            "proposed_actions TEXT",
            "executed_action_results TEXT",
        ):
            try:
                conn.execute(f"ALTER TABLE decision_chat_messages ADD COLUMN {col_def}")
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

def upsert_position(ticker, quantity, avg_buy_price, current_price=0,
                    direction=None):
    with get_db() as conn:
        existing = conn.execute("SELECT * FROM positions WHERE ticker=?", (ticker,)).fetchone()
        # direction: se NON passata, PRESERVA quella della posizione
        # esistente — un update di prezzo / ribilanciamento che non
        # specifica direction NON deve ribaltare uno SHORT a LONG.
        # Nuova posizione senza direction → LONG.
        if direction is None:
            try:
                direction = existing["direction"] if existing else "LONG"
            except (KeyError, IndexError):
                direction = "LONG"
        direction = "SHORT" if str(direction).upper() == "SHORT" else "LONG"
        # P&L non realizzato: LONG guadagna se il prezzo SALE, SHORT se SCENDE.
        if current_price > 0:
            if direction == "SHORT":
                pnl = (avg_buy_price - current_price) * quantity
            else:
                pnl = (current_price - avg_buy_price) * quantity
        else:
            pnl = 0
        if existing:
            conn.execute("UPDATE positions SET quantity=?, avg_buy_price=?, current_price=?, unrealized_pnl=?, direction=? WHERE ticker=?",
                         (quantity, avg_buy_price, current_price, pnl, direction, ticker))
        else:
            conn.execute("INSERT INTO positions (ticker,quantity,avg_buy_price,current_price,unrealized_pnl,direction) VALUES (?,?,?,?,?,?)",
                         (ticker, quantity, avg_buy_price, current_price, pnl, direction))


def update_position_auto_exit(ticker, stop_loss_price=None, take_profit_price=None, set_by=""):
    """
    Aggiorna i livelli SL/TP automatici di una posizione esistente.
    Se un parametro e' None, NON viene modificato; per rimuovere passa 0.
    Ritorna True se la posizione esiste ed e' stata aggiornata.
    """
    with get_db() as conn:
        existing = conn.execute("SELECT id FROM positions WHERE ticker=?", (ticker,)).fetchone()
        if not existing:
            return False
        sets = []
        params = []
        if stop_loss_price is not None:
            sets.append("stop_loss_price=?")
            params.append(float(stop_loss_price))
        if take_profit_price is not None:
            sets.append("take_profit_price=?")
            params.append(float(take_profit_price))
        if not sets:
            return True  # nothing to update
        sets.append("auto_exit_set_at=datetime('now')")
        sets.append("auto_exit_set_by=?")
        params.append(set_by or "")
        params.append(ticker)
        conn.execute(f"UPDATE positions SET {', '.join(sets)} WHERE ticker=?", params)
        return True


def get_positions_with_auto_exits():
    """
    Ritorna le posizioni che hanno almeno uno tra stop_loss_price o
    take_profit_price > 0 (cioe' impostati dall'agente).
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM positions "
            "WHERE COALESCE(stop_loss_price, 0) > 0 OR COALESCE(take_profit_price, 0) > 0"
        ).fetchall()
        return [dict(r) for r in rows]

def delete_position(ticker):
    with get_db() as conn:
        conn.execute("DELETE FROM positions WHERE ticker=?", (ticker,))

def update_position_price(ticker, current_price):
    with get_db() as conn:
        pos = conn.execute("SELECT * FROM positions WHERE ticker=?", (ticker,)).fetchone()
        if pos:
            # P&L non realizzato DIRECTION-AWARE: una posizione SHORT
            # guadagna quando il prezzo SCENDE. La formula long avrebbe
            # mostrato il segno invertito sugli short ad ogni price poll.
            try:
                _dir = (pos["direction"] or "LONG").upper()
            except (KeyError, IndexError):
                _dir = "LONG"
            if _dir == "SHORT":
                pnl = (pos["avg_buy_price"] - current_price) * pos["quantity"]
            else:
                pnl = (current_price - pos["avg_buy_price"]) * pos["quantity"]
            conn.execute("UPDATE positions SET current_price=?, unrealized_pnl=? WHERE ticker=?", (current_price, pnl, ticker))

def count_positions():
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) as cnt FROM positions").fetchone()["cnt"]

def insert_trade(ticker, action, quantity, price, geo_reasoning, tech_reasoning,
                 final_decision, confidence, direction="LONG"):
    """
    Inserisce un trade e ritorna l'ID della riga creata.

    direction = 'LONG' | 'SHORT' — tag che distingue le operazioni sul
    book long da quelle sul book short. action resta 'BUY'/'SELL' (il
    verbo di mercato): aprire uno short e' un SELL+SHORT, coprirlo e'
    un BUY+SHORT.
    """
    direction = "SHORT" if str(direction).upper() == "SHORT" else "LONG"
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO trades (ticker,action,quantity,price,total_value,geopolitical_reasoning,technical_reasoning,final_decision,confidence_score,direction) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ticker, action, quantity, price, price*quantity, geo_reasoning, tech_reasoning, final_decision, confidence, direction),
        )
        return cur.lastrowid

def get_trades(limit=50):
    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()]


def update_trade_mirror_status(trade_id, status, reason=None, increment_attempts=True):
    """No-op stub (mantenuto per compat: nessun mirror esterno attivo)."""
    return


def get_pending_mirror_trades(window_hours=24, max_attempts=5, limit=50):
    """No-op stub (mantenuto per compat: nessun mirror esterno attivo)."""
    return []


def get_mirror_status_summary():
    """No-op stub (mantenuto per compat: nessun mirror esterno attivo)."""
    return {}


def insert_agent_log(run_id, phase, content):
    with get_db() as conn:
        conn.execute("INSERT INTO agent_logs (run_id,phase,content) VALUES (?,?,?)", (run_id, phase, content))

def get_agent_logs(limit=100):
    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM agent_logs ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()]

def get_logs_by_run(run_id):
    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM agent_logs WHERE run_id=? ORDER BY timestamp ASC", (run_id,)).fetchall()]


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
        phases = ("DECISION_REASONING",)
    elif agent_type == "crypto":
        phases = ("DECISION_CRYPTO_COMPLETE",)
    else:
        phases = ("DECISION_REASONING", "DECISION_CRYPTO_COMPLETE")

    placeholders = ",".join("?" * len(phases))
    sql = (
        f"SELECT * FROM agent_logs "
        f"WHERE phase IN ({placeholders}) "
        f"ORDER BY timestamp DESC LIMIT ?"
    )
    with get_db() as conn:
        rows = conn.execute(sql, (*phases, limit)).fetchall()
        return [dict(r) for r in rows]

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
    """Salva uno snapshot del valore del portafoglio.

    HARD SANITY CAP: rifiuta snapshot con drift >3x (o <1/3x) rispetto
    all'ultimo snapshot — sintomo di bug nel calcolo NAV. Vedi il
    commento equivalente in db_supabase.insert_portfolio_snapshot.
    """
    try:
        tv = float(total_value)
    except (TypeError, ValueError):
        return
    if tv <= 0:
        return

    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT total_value FROM portfolio_snapshots "
                "ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            if row and row[0]:
                last_val = float(row[0])
                if last_val > 0:
                    ratio = tv / last_val
                    if ratio > 3.0 or ratio < 1 / 3.0:
                        # Snapshot impossibile per movimenti reali — reject
                        return
            conn.execute(
                "INSERT INTO portfolio_snapshots (total_value, cash_balance) VALUES (?,?)",
                (tv, cash_balance))
    except Exception:
        # Fail-open: preserva comportamento storico se il check si rompe
        try:
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO portfolio_snapshots (total_value, cash_balance) VALUES (?,?)",
                    (tv, cash_balance))
        except Exception:
            pass


def delete_portfolio_snapshot_by_ts(timestamp_iso):
    """Cancella uno snapshot specifico per timestamp ISO.

    Usato dall'endpoint admin /api/admin/portfolio-snapshots/outliers
    per ripulire picchi anomali storici nella equity curve.
    """
    if not timestamp_iso:
        return False
    try:
        with get_db() as conn:
            cur = conn.execute(
                "DELETE FROM portfolio_snapshots WHERE timestamp = ?",
                (timestamp_iso,))
            return cur.rowcount > 0
    except Exception:
        return False

def get_portfolio_history(days=30):
    """Restituisce lo storico del valore del portafoglio."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT total_value, cash_balance, timestamp FROM portfolio_snapshots "
            "WHERE timestamp >= datetime('now', ?) ORDER BY timestamp ASC",
            (f'-{days} days',)).fetchall()
        return [dict(r) for r in rows]

def get_first_portfolio_snapshot():
    """Ritorna il primissimo snapshot mai registrato (inizio vita reale).
    Parallelo alla versione Supabase: serve come punto di partenza vero per
    return/alpha quando lo storico recente e' troncato."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT total_value, cash_balance, timestamp FROM portfolio_snapshots "
            "ORDER BY timestamp ASC LIMIT 1").fetchone()
        return dict(row) if row else None

def get_first_snapshot_since(ts_iso):
    """Primo snapshot con timestamp >= ts_iso. Parallelo a Supabase:
    ancora return/alpha all'inizio ufficiale (post chiusura CRWD+LMT)."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT total_value, cash_balance, timestamp FROM portfolio_snapshots "
            "WHERE timestamp >= ? ORDER BY timestamp ASC LIMIT 1",
            (ts_iso,)).fetchone()
        return dict(row) if row else None


def get_client():
    """SQLite stub — v4 tables not supported locally. Returns None."""
    return None


# ============================================================
# Chat Assistant (conversazioni con l'analista AI DeepSeek-R1)
# ============================================================

def create_chat_conversation(title: str = "Nuova conversazione",
                             selected_decisions: str | None = None) -> int:
    """Crea una nuova conversazione e ritorna l'id."""
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO chat_conversations (title, selected_decisions) VALUES (?,?)",
            (title[:120], selected_decisions),
        )
        return cur.lastrowid


def get_chat_conversations(limit: int = 10) -> list:
    """Lista le ultime N conversazioni (default 10) per la mini-memoria."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, title, selected_decisions, created_at, updated_at "
            "FROM chat_conversations ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_chat_messages(conversation_id: int) -> list:
    """Tutti i messaggi di una conversazione, in ordine cronologico."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, role, content, created_at FROM chat_messages "
            "WHERE conversation_id=? ORDER BY created_at ASC, id ASC",
            (conversation_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def insert_chat_message(conversation_id: int, role: str, content: str) -> int:
    """Aggiunge un messaggio e aggiorna updated_at della conversazione."""
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO chat_messages (conversation_id, role, content) VALUES (?,?,?)",
            (conversation_id, role, content),
        )
        conn.execute(
            "UPDATE chat_conversations SET updated_at=datetime('now') WHERE id=?",
            (conversation_id,),
        )
        return cur.lastrowid


def update_chat_conversation_title(conversation_id: int, title: str):
    """Aggiorna il titolo di una conversazione."""
    with get_db() as conn:
        conn.execute(
            "UPDATE chat_conversations SET title=?, updated_at=datetime('now') WHERE id=?",
            (title[:120], conversation_id),
        )


def update_chat_conversation_decisions(conversation_id: int, selected_decisions: str):
    """Aggiorna l'elenco dei trade_id selezionati per questa conversazione."""
    with get_db() as conn:
        conn.execute(
            "UPDATE chat_conversations SET selected_decisions=?, updated_at=datetime('now') WHERE id=?",
            (selected_decisions, conversation_id),
        )


def delete_chat_conversation(conversation_id: int):
    """Elimina una conversazione (cascade sui messaggi via FK)."""
    with get_db() as conn:
        conn.execute("DELETE FROM chat_messages WHERE conversation_id=?", (conversation_id,))
        conn.execute("DELETE FROM chat_conversations WHERE id=?", (conversation_id,))


def trim_chat_conversations(keep_last: int = 10):
    """
    Mantiene solo le ultime N conversazioni (per ordine updated_at) ed elimina
    le altre. Chiamato dopo ogni nuova conversazione per la mini-memoria.
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id FROM chat_conversations ORDER BY updated_at DESC LIMIT -1 OFFSET ?",
            (keep_last,),
        ).fetchall()
        for r in rows:
            conn.execute("DELETE FROM chat_messages WHERE conversation_id=?", (r["id"],))
            conn.execute("DELETE FROM chat_conversations WHERE id=?", (r["id"],))


# ============================================================================
# Decision Chat (chat con Decision Agent — Standard / Crypto, con esecuzione trade)
# ============================================================================
import json as _json


def get_or_create_decision_chat_conversation(agent_type: str) -> int | None:
    agent_type = (agent_type or "standard").lower()
    if agent_type not in ("standard", "crypto"):
        agent_type = "standard"
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM decision_chat_conversations WHERE agent_type=? "
            "ORDER BY updated_at DESC LIMIT 1",
            (agent_type,),
        ).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO decision_chat_conversations (agent_type) VALUES (?)",
            (agent_type,),
        )
        return cur.lastrowid


def _deserialize_decision_chat_row(d: dict) -> dict:
    """Helper: decodifica i campi JSON delle righe decision_chat_messages."""
    pt = d.get("proposed_trade")
    if pt:
        try:
            d["proposed_trade"] = _json.loads(pt)
        except Exception:
            d["proposed_trade"] = None
    pa = d.get("proposed_actions")
    if pa:
        try:
            d["proposed_actions"] = _json.loads(pa)
        except Exception:
            d["proposed_actions"] = None
    ear = d.get("executed_action_results")
    if ear:
        try:
            d["executed_action_results"] = _json.loads(ear)
        except Exception:
            d["executed_action_results"] = None
    return d


def insert_decision_chat_message(conversation_id: int, role: str, content: str,
                                 proposed_trade: dict | None = None,
                                 proposed_actions: list | None = None) -> int | None:
    pt_json = _json.dumps(proposed_trade) if proposed_trade else None
    pa_json = _json.dumps(proposed_actions) if proposed_actions else None
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO decision_chat_messages "
            "(conversation_id, role, content, proposed_trade, proposed_actions) "
            "VALUES (?,?,?,?,?)",
            (conversation_id, role, content, pt_json, pa_json),
        )
        conn.execute(
            "UPDATE decision_chat_conversations SET updated_at=datetime('now') WHERE id=?",
            (conversation_id,),
        )
        return cur.lastrowid


def get_decision_chat_messages(conversation_id: int, limit: int = 100) -> list:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, conversation_id, role, content, proposed_trade, "
            "proposed_actions, executed_trade_id, executed_action_results, "
            "created_at "
            "FROM decision_chat_messages WHERE conversation_id=? "
            "ORDER BY created_at ASC LIMIT ?",
            (conversation_id, limit),
        ).fetchall()
        return [_deserialize_decision_chat_row(dict(r)) for r in rows]


def get_decision_chat_message(message_id: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM decision_chat_messages WHERE id=?",
            (message_id,),
        ).fetchone()
        if not row:
            return None
        return _deserialize_decision_chat_row(dict(row))


def clear_decision_chat(agent_type: str) -> bool:
    agent_type = (agent_type or "standard").lower()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id FROM decision_chat_conversations WHERE agent_type=?",
            (agent_type,),
        ).fetchall()
        for r in rows:
            conn.execute("DELETE FROM decision_chat_messages WHERE conversation_id=?", (r["id"],))
            conn.execute("DELETE FROM decision_chat_conversations WHERE id=?", (r["id"],))
    return True


def get_recent_user_directives(agent_type: str, hours: int = 48, limit: int = 5) -> list:
    """Ultimi N messaggi UTENTE delle ultime `hours` ore per agent_type."""
    agent_type = (agent_type or "standard").lower()
    with get_db() as conn:
        conv = conn.execute(
            "SELECT id FROM decision_chat_conversations WHERE agent_type=? "
            "ORDER BY updated_at DESC LIMIT 1",
            (agent_type,),
        ).fetchone()
        if not conv:
            return []
        rows = conn.execute(
            "SELECT content, created_at FROM decision_chat_messages "
            "WHERE conversation_id=? AND role='user' "
            "AND created_at >= datetime('now', ?) "
            "ORDER BY created_at DESC LIMIT ?",
            (conv["id"], f"-{hours} hours", limit),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_decision_chat_trade_executed(message_id: int, trade_id: int) -> bool:
    with get_db() as conn:
        conn.execute(
            "UPDATE decision_chat_messages SET executed_trade_id=? WHERE id=?",
            (trade_id, message_id),
        )
    return True


def mark_decision_chat_action_executed(message_id: int, action_index: int,
                                       result: dict) -> bool:
    """
    Salva il risultato dell'esecuzione di una proposed_action (stop-loss,
    take-profit, direttiva). I risultati sono indicizzati per
    `action_index` (posizione nell'array proposed_actions del messaggio)
    cosi' il frontend puo' mostrare lo stato per ogni singola azione.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT executed_action_results FROM decision_chat_messages WHERE id=?",
            (message_id,),
        ).fetchone()
        if not row:
            return False
        current = {}
        if row["executed_action_results"]:
            try:
                current = _json.loads(row["executed_action_results"])
                if not isinstance(current, dict):
                    current = {}
            except Exception:
                current = {}
        current[str(action_index)] = result
        conn.execute(
            "UPDATE decision_chat_messages SET executed_action_results=? WHERE id=?",
            (_json.dumps(current), message_id),
        )
    return True


# ============================================================================
# Agent Commitments — memoria persistente delle "promesse" del Decision Agent
# Mirror SQLite di db_supabase.add_agent_commitment / get_active / resolve.
# ============================================================================

def _expire_old_commitments_sqlite(conn, agent_type: str) -> None:
    """Sweep one-shot: marca expired le righe attive con expires_at < now."""
    try:
        conn.execute(
            "UPDATE agent_commitments "
            "SET status='expired', resolved_at=datetime('now'), "
            "    resolved_reason='auto-expired (deadline passed)' "
            "WHERE agent_type=? AND status='active' "
            "AND expires_at IS NOT NULL AND expires_at < datetime('now')",
            (agent_type,),
        )
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
    agent_type = (agent_type or "standard").lower()
    if agent_type not in ("standard", "crypto"):
        agent_type = "standard"

    expires_at_str = None
    if expires_in_hours and expires_in_hours > 0:
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        expires_at_str = (_dt.now(_tz.utc) + _td(hours=float(expires_in_hours))).isoformat()

    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO agent_commitments "
            "(agent_type, ticker, commitment_type, condition_text, trigger_action, "
            " expires_at, status, source_run_id, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)",
            (
                agent_type,
                (ticker or "").upper()[:32] if ticker else None,
                commitment_type,
                (condition_text or "")[:2000],
                (trigger_action or "")[:1000] if trigger_action else None,
                expires_at_str,
                source_run_id,
                (notes or "")[:1000] if notes else None,
            ),
        )
        return cur.lastrowid


def get_active_agent_commitments(agent_type: str, limit: int = 20) -> list:
    agent_type = (agent_type or "standard").lower()
    with get_db() as conn:
        _expire_old_commitments_sqlite(conn, agent_type)
        rows = conn.execute(
            "SELECT * FROM agent_commitments "
            "WHERE agent_type=? AND status='active' "
            "ORDER BY created_at DESC LIMIT ?",
            (agent_type, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_recent_agent_commitments(agent_type: str, limit: int = 30) -> list:
    agent_type = (agent_type or "standard").lower()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_commitments "
            "WHERE agent_type=? "
            "ORDER BY created_at DESC LIMIT ?",
            (agent_type, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def resolve_agent_commitment(
    commitment_id: int,
    status: str = "triggered",
    resolved_reason: str | None = None,
) -> bool:
    if status not in ("triggered", "cancelled", "expired"):
        status = "cancelled"
    with get_db() as conn:
        conn.execute(
            "UPDATE agent_commitments "
            "SET status=?, resolved_at=datetime('now'), resolved_reason=? "
            "WHERE id=?",
            (status, (resolved_reason or "")[:1000], commitment_id),
        )
    return True

