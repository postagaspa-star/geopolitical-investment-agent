"""
DB helper per il Simulator. Usa la stessa istanza Supabase / SQLite del Live
ma con tabelle separate (prefix sim_*).

Tabelle:
  - sim_runs: un record per ogni scenario completato (memoria principale)
  - sim_steps: dettaglio step-by-step (multi-step scenarios)
  - sim_settings: stato auto-mode + cap giornaliero
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)


# ─── Migration auto su startup (chiamata da database.init_db) ───────────────

SIM_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS sim_runs (
    id TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    mode TEXT DEFAULT 'manual',
    category TEXT NOT NULL,
    scenario_type TEXT NOT NULL,
    steps INTEGER DEFAULT 1,
    scenario_id TEXT NOT NULL,
    historical_period TEXT,
    asset_chosen TEXT,
    action_chosen TEXT,
    conviction TEXT,
    horizon TEXT,
    perf_1w DOUBLE PRECISION,
    perf_1m DOUBLE PRECISION,
    perf_3m DOUBLE PRECISION,
    perf_sp_1m DOUBLE PRECISION,
    perf_sector_1m DOUBLE PRECISION,
    perf_monkey_1m DOUBLE PRECISION,
    delta_sp DOUBLE PRECISION,
    delta_sector DOUBLE PRECISION,
    delta_monkey DOUBLE PRECISION,
    outcome TEXT,
    original_thesis TEXT,
    what_happened TEXT,
    thesis_evaluation TEXT,
    full_data JSONB
);
CREATE INDEX IF NOT EXISTS idx_sim_runs_category ON sim_runs(category);
CREATE INDEX IF NOT EXISTS idx_sim_runs_completed ON sim_runs(completed_at DESC);

CREATE TABLE IF NOT EXISTS sim_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

SIM_MIGRATION_SQLITE = """
CREATE TABLE IF NOT EXISTS sim_runs (
    id TEXT PRIMARY KEY,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    mode TEXT DEFAULT 'manual',
    category TEXT NOT NULL,
    scenario_type TEXT NOT NULL,
    steps INTEGER DEFAULT 1,
    scenario_id TEXT NOT NULL,
    historical_period TEXT,
    asset_chosen TEXT,
    action_chosen TEXT,
    conviction TEXT,
    horizon TEXT,
    perf_1w REAL,
    perf_1m REAL,
    perf_3m REAL,
    perf_sp_1m REAL,
    perf_sector_1m REAL,
    perf_monkey_1m REAL,
    delta_sp REAL,
    delta_sector REAL,
    delta_monkey REAL,
    outcome TEXT,
    original_thesis TEXT,
    what_happened TEXT,
    thesis_evaluation TEXT,
    full_data TEXT
);
CREATE INDEX IF NOT EXISTS idx_sim_runs_category ON sim_runs(category);
CREATE INDEX IF NOT EXISTS idx_sim_runs_completed ON sim_runs(completed_at DESC);

CREATE TABLE IF NOT EXISTS sim_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def ensure_schema():
    """Applica la migration. Chiamata all'avvio dell'app."""
    import database

    # Tenta SQLite prima (più semplice, idempotente)
    try:
        if hasattr(database, "_sqlite_db") or "sqlite" in repr(type(database)).lower():
            import db_sqlite
            with db_sqlite.get_db() as conn:
                for stmt in SIM_MIGRATION_SQLITE.split(";"):
                    if stmt.strip():
                        try:
                            conn.execute(stmt)
                        except Exception:
                            pass
            logger.info("[SIM] Schema SQLite ok.")
            return
    except Exception:
        pass

    # Supabase: usa l'auto-migration via psycopg2 (stesso pattern di cs_mirror)
    import os
    db_url = os.environ.get("DATABASE_URL", "").strip()
    if not db_url:
        db_pass = os.environ.get("SUPABASE_DB_PASSWORD", "").strip()
        sup_url = os.environ.get("SUPABASE_URL", "")
        if db_pass and sup_url:
            try:
                ref = sup_url.split("//")[1].split(".")[0]
                # NOTA: usiamo eu-central-1 per coerenza con db_supabase.py
                # (era eu-west-3 → mismatch silenzioso che non creava le tabelle)
                db_url = f"postgresql://postgres.{ref}:{db_pass}@aws-0-eu-central-1.pooler.supabase.com:6543/postgres"
            except Exception:
                pass
    if not db_url:
        logger.warning("[SIM] DATABASE_URL non disponibile — schema sim_* non applicato. "
                       "Esegui SIM_MIGRATION_SQL su Supabase Dashboard manualmente.")
        return
    try:
        import psycopg2
        with psycopg2.connect(db_url, connect_timeout=15) as conn:
            with conn.cursor() as cur:
                cur.execute(SIM_MIGRATION_SQL)
            conn.commit()
        logger.info("[SIM] Schema Supabase applicato.")
    except Exception as exc:
        logger.warning("[SIM] Auto-migration sim_* fallita: %s", exc)


# ─── CRUD via Supabase client (preferito) o SQLite fallback ─────────────────

def _get_client():
    try:
        import database
        return database.get_client()
    except Exception:
        return None


def insert_run(run_data: dict) -> str:
    """
    Inserisce un run completo. Ritorna l'id.

    Cerca di salvare prima su Supabase. Se fallisce (es. tabella sim_runs
    non esiste perche' la migration psycopg2 non e' passata) tenta di
    crearla al volo via psycopg2 e poi riprova l'INSERT. Solo se anche
    quello fallisce, prova SQLite come fallback (locale).
    """
    client = _get_client()
    if client:
        payload = dict(run_data)
        if isinstance(payload.get("full_data"), dict):
            payload["full_data"] = json.dumps(payload["full_data"], default=str)
        try:
            client.table("sim_runs").insert(payload).execute()
            logger.info("[SIM] insert_run Supabase ok: id=%s", payload["id"])
            return payload["id"]
        except Exception as exc:
            err_str = str(exc).lower()
            logger.warning(
                "[SIM] insert_run Supabase failed: %s | tabella esiste? %s",
                exc, "no" if "does not exist" in err_str or "relation" in err_str else "boh",
            )
            # Tentativo recovery: applica la migration al volo e ritenta
            if "does not exist" in err_str or "relation" in err_str or "schema" in err_str:
                try:
                    logger.info("[SIM] tentativo applicazione migration al volo...")
                    ensure_schema()
                    client.table("sim_runs").insert(payload).execute()
                    logger.info("[SIM] insert_run Supabase ok dopo recovery: id=%s", payload["id"])
                    return payload["id"]
                except Exception as exc2:
                    logger.error("[SIM] recovery migration fallito: %s", exc2)

    # SQLite fallback (solo se Supabase non e' disponibile)
    try:
        import db_sqlite
        # Assicura che la tabella esista anche in SQLite
        try:
            with db_sqlite.get_db() as conn:
                for stmt in SIM_MIGRATION_SQLITE.split(";"):
                    if stmt.strip():
                        try:
                            conn.execute(stmt)
                        except Exception:
                            pass
        except Exception:
            pass
        with db_sqlite.get_db() as conn:
            keys = list(run_data.keys())
            placeholders = ",".join("?" for _ in keys)
            cols = ",".join(keys)
            vals = []
            for k in keys:
                v = run_data[k]
                if isinstance(v, (dict, list)):
                    v = json.dumps(v, default=str)
                vals.append(v)
            conn.execute(f"INSERT INTO sim_runs ({cols}) VALUES ({placeholders})", vals)
        logger.info("[SIM] insert_run SQLite ok: id=%s", run_data["id"])
        return run_data["id"]
    except Exception as exc:
        logger.error("[SIM] insert_run SQLite failed: %s", exc, exc_info=True)
        raise


def get_run(run_id: str) -> dict | None:
    """
    Cerca un run in 3 sorgenti in ordine:
      1. Supabase (sim_runs)
      2. SQLite locale (fallback dev)
      3. In-memory _active_runs del runner (fallback ultimo: se l'INSERT
         su Supabase e' fallito ma il run e' stato comunque eseguito in
         memoria, lo ricostruiamo on-the-fly cosi' l'utente vede il
         risultato invece di un 404)
    """
    client = _get_client()
    if client:
        try:
            r = client.table("sim_runs").select("*").eq("id", run_id).limit(1).execute()
            if r.data:
                row = r.data[0]
                if isinstance(row.get("full_data"), str):
                    try:
                        row["full_data"] = json.loads(row["full_data"])
                    except Exception:
                        pass
                return row
        except Exception as exc:
            logger.warning("[SIM] get_run Supabase failed: %s", exc)

    try:
        import db_sqlite
        with db_sqlite.get_db() as conn:
            row = conn.execute("SELECT * FROM sim_runs WHERE id=?", (run_id,)).fetchone()
            if row:
                d = dict(row)
                if isinstance(d.get("full_data"), str):
                    try:
                        d["full_data"] = json.loads(d["full_data"])
                    except Exception:
                        pass
                return d
    except Exception:
        pass

    # Fallback in-memory: il run e' stato eseguito ma _finalize_run ha fallito
    # il salvataggio su DB. Ricostruiamo i dati dallo state attivo del runner.
    try:
        from simulator import runner as _runner
        rebuilt = _runner.rebuild_run_from_memory(run_id)
        if rebuilt:
            logger.warning(
                "[SIM] get_run %s servito da _active_runs (DB non disponibile)",
                run_id,
            )
            return rebuilt
    except Exception as exc:
        logger.warning("[SIM] in-memory rebuild failed: %s", exc)

    return None


def list_runs(category: str | None = None, scenario_type: str | None = None,
              outcome: str | None = None, limit: int = 50) -> list[dict]:
    client = _get_client()
    if client:
        try:
            q = client.table("sim_runs").select("*")\
                .order("completed_at", desc=True).limit(limit)
            if category:
                q = q.eq("category", category)
            if scenario_type:
                q = q.eq("scenario_type", scenario_type)
            if outcome:
                q = q.eq("outcome", outcome)
            r = q.execute()
            return r.data or []
        except Exception:
            pass
    try:
        import db_sqlite
        clauses, vals = [], []
        if category: clauses.append("category=?"); vals.append(category)
        if scenario_type: clauses.append("scenario_type=?"); vals.append(scenario_type)
        if outcome: clauses.append("outcome=?"); vals.append(outcome)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with db_sqlite.get_db() as conn:
            rows = conn.execute(
                f"SELECT * FROM sim_runs {where} ORDER BY completed_at DESC LIMIT ?",
                vals + [limit],
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def get_setting(key: str, default: str = "") -> str:
    client = _get_client()
    if client:
        try:
            r = client.table("sim_settings").select("value").eq("key", key).limit(1).execute()
            if r.data:
                return r.data[0].get("value") or default
        except Exception:
            pass
    return default


def set_setting(key: str, value: str):
    client = _get_client()
    if client:
        try:
            client.table("sim_settings").upsert({"key": key, "value": value}).execute()
            return
        except Exception:
            pass


def runs_today(mode: str = "auto") -> int:
    """Conta run eseguiti oggi (UTC) in modalità data."""
    cutoff = (datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)).isoformat()
    client = _get_client()
    if client:
        try:
            r = client.table("sim_runs").select("id", count="exact")\
                .eq("mode", mode).gte("created_at", cutoff).execute()
            return r.count or 0
        except Exception:
            pass
    return 0
