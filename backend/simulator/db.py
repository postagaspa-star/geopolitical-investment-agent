"""
DB helper per il Simulator. Usa la stessa istanza Supabase / SQLite del Live
ma con tabelle separate (prefix sim_*).

Tabelle:
  - sim_runs: un record per ogni scenario completato (memoria principale)
  - sim_steps: dettaglio step-by-step (multi-step scenarios)
  - sim_settings: stato auto-mode + cap giornaliero

Persistenza ridondante: oltre a Supabase (primaria) e SQLite (fallback dev),
i run vengono salvati anche in un file JSON locale (`/tmp/sim_runs_cache.json`)
come backup di emergenza. Cosi' se Supabase fallisce E SQLite non e' migrato,
il fallback `get_run` puo' recuperare i dati anche dopo un restart del pod
Render (almeno fino al prossimo cleanup del filesystem ephemeral).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

# Cache file locale per persistenza di emergenza (super-fallback dopo Supabase
# e SQLite). Su Render /tmp e' ephemeral ma sopravvive ai restart del pod
# senza redeploy. Se cambi codice e fai push, viene resettato.
_LOCAL_CACHE_PATH = os.environ.get(
    "SIM_LOCAL_CACHE_PATH",
    os.path.join(os.path.dirname(__file__), "..", "sim_runs_cache.json"),
)


def _load_local_cache() -> dict:
    try:
        with open(_LOCAL_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_local_cache(cache: dict):
    try:
        os.makedirs(os.path.dirname(_LOCAL_CACHE_PATH), exist_ok=True)
        with open(_LOCAL_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, default=str, ensure_ascii=False)
    except Exception as exc:
        logger.warning("[SIM] save local cache fallito: %s", exc)


def _put_in_local_cache(run_id: str, run_data: dict):
    """Aggiunge un run alla cache locale (cap 200 run per evitare crescita illimitata)."""
    cache = _load_local_cache()
    cache[run_id] = run_data
    if len(cache) > 200:
        # Mantieni solo gli ultimi 200 per data
        sorted_items = sorted(
            cache.items(),
            key=lambda kv: str(kv[1].get("completed_at", "")),
            reverse=True,
        )
        cache = dict(sorted_items[:200])
    _save_local_cache(cache)


def _get_from_local_cache(run_id: str) -> dict | None:
    cache = _load_local_cache()
    return cache.get(run_id)


# ═══════════════════════════════════════════════════════════════════════
# DUAL-MODE FALLBACK: storage dei run su `sim_settings` quando la tabella
# `sim_runs` non e' accessibile (DDL fallita, DATABASE_URL non configurato).
# Stesso pattern di db_supabase._chat_fallback_*.
#
# La tabella `sim_settings` esiste sempre (e' creata dal pattern key-value
# generico) ed e' accessibile via REST API senza DDL.
#
# Schema chiavi:
#   _sim_run_fallback::list           JSON list di run_id ordinati per recency
#   _sim_run_fallback::run::{id}      JSON dell'intero run_data
# ═══════════════════════════════════════════════════════════════════════

_SIM_RUN_FALLBACK_MODE = False  # True dopo il primo errore di tabella mancante
_RUN_FALLBACK_LIST_KEY = "_sim_run_fallback::list"
_RUN_FALLBACK_RUN_KEY = "_sim_run_fallback::run::{id}"
_RUN_FALLBACK_MAX_LIST = 300   # cap totale run preservati nel fallback


def _is_table_missing_error(exc: Exception) -> bool:
    """Riconosce errori "tabella non trovata" su Supabase / Postgres."""
    s = str(exc).lower()
    return ("pgrst205" in s
            or "does not exist" in s
            or "no such table" in s
            or "could not find the table" in s
            or "schema cache" in s
            or "relation" in s and "does not exist" in s)


def _settings_get(key: str, default: str = "") -> str:
    """Wrapper su sim_settings get_setting."""
    try:
        return get_setting(key, default) or default
    except Exception:
        return default


def _settings_set(key: str, value: str) -> None:
    try:
        set_setting(key, value)
    except Exception as exc:
        logger.warning("[SIM] settings set fallita per %s: %s", key, exc)


def _run_fallback_load_list() -> list:
    raw = _settings_get(_RUN_FALLBACK_LIST_KEY, "[]") or "[]"
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _run_fallback_save_list(lst: list) -> None:
    _settings_set(_RUN_FALLBACK_LIST_KEY, json.dumps(lst, default=str))


def _run_fallback_load_run(run_id: str) -> dict | None:
    raw = _settings_get(_RUN_FALLBACK_RUN_KEY.format(id=run_id), "")
    if not raw:
        return None
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return None


def _run_fallback_save_run(run_data: dict) -> bool:
    """Salva un run nel fallback settings + aggiorna l'indice."""
    rid = run_data.get("id")
    if not rid:
        return False
    try:
        _settings_set(_RUN_FALLBACK_RUN_KEY.format(id=rid),
                      json.dumps(run_data, default=str))
        lst = _run_fallback_load_list()
        # Sposta in cima (= piu' recente)
        if rid in lst:
            lst.remove(rid)
        lst.insert(0, rid)
        # Cap totale: rimuove vecchi run anche dalle key dedicate
        if len(lst) > _RUN_FALLBACK_MAX_LIST:
            to_drop = lst[_RUN_FALLBACK_MAX_LIST:]
            lst = lst[:_RUN_FALLBACK_MAX_LIST]
            for old_id in to_drop:
                try:
                    _settings_set(_RUN_FALLBACK_RUN_KEY.format(id=old_id), "")
                except Exception:
                    pass
        _run_fallback_save_list(lst)
        return True
    except Exception as exc:
        logger.warning("[SIM] fallback save run %s fallita: %s", rid, exc)
        return False


def _run_fallback_list_runs(limit: int = 50, category: str | None = None,
                             scenario_type: str | None = None,
                             outcome: str | None = None) -> list[dict]:
    """Lista run dal fallback applicando filtri. Caricamento on-demand."""
    out: list[dict] = []
    ids = _run_fallback_load_list()
    for rid in ids[:limit * 4]:  # buffer per filtri
        run = _run_fallback_load_run(rid)
        if not run:
            continue
        if category and run.get("category") != category:
            continue
        if scenario_type and run.get("scenario_type") != scenario_type:
            continue
        if outcome and run.get("outcome") != outcome:
            continue
        out.append(run)
        if len(out) >= limit:
            break
    return out


def get_storage_mode() -> dict:
    """
    Ritorna lo stato corrente del backend di storage per i sim_runs.
    Usato dall'endpoint /api/simulator/health per diagnostica visibile.
    """
    info = {"fallback_mode": _SIM_RUN_FALLBACK_MODE}
    client = _get_client()
    if client is None:
        info["primary_backend"] = "sqlite"
    else:
        # Probe sim_runs accessibilita'
        try:
            client.table("sim_runs").select("id").limit(1).execute()
            info["primary_backend"] = "supabase_sim_runs"
            info["sim_runs_accessible"] = True
        except Exception as exc:
            info["primary_backend"] = "supabase_settings_fallback"
            info["sim_runs_accessible"] = False
            info["sim_runs_error"] = str(exc)[:200]
    # Conta nel fallback (anche se primary funziona, mostra count)
    try:
        info["fallback_count"] = len(_run_fallback_load_list())
    except Exception:
        info["fallback_count"] = -1
    return info


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
    """
    Applica la migration. Chiamata all'avvio dell'app.

    Determina il backend via database.get_client():
      - None  → SQLite (crea tabelle direttamente)
      - non-None → Supabase (crea via psycopg2 + DATABASE_URL)

    NOTA: hasattr(database, "_sqlite_db") NON funziona perche' Python non
    esporta i simboli con underscore via `from module import *`.
    """
    import database

    # Determina il backend
    try:
        client = database.get_client()
    except Exception:
        client = None

    is_sqlite = client is None

    if is_sqlite:
        # SQLite: crea le tabelle inline (fail-safe, idempotente)
        try:
            import db_sqlite
            with db_sqlite.get_db() as conn:
                for stmt in SIM_MIGRATION_SQLITE.split(";"):
                    if stmt.strip():
                        try:
                            conn.execute(stmt)
                        except Exception as e:
                            logger.debug("[SIM] SQLite stmt skip: %s", e)
            logger.info("[SIM] Schema SQLite ok.")
            return
        except Exception as exc:
            logger.error("[SIM] Errore creazione schema SQLite: %s", exc, exc_info=True)
            return

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

    Strategia di persistenza a 4 tier (in ordine di tentativo):
      1. Supabase tabella `sim_runs` (primario, migration via psycopg2)
      2. Supabase tabella `sim_settings` come key-value JSON (DUAL-MODE
         FALLBACK: stesso pattern della chat — non richiede DDL, sempre
         accessibile via REST API). Garantisce persistenza CROSS-DEPLOY.
      3. SQLite locale (dev / containers con disk persistente)
      4. Cache file locale (`/tmp` ephemeral, solo intra-pod)

    La novita' chiave e' il Tier 2: prima si perdevano i run quando
    sim_runs non era accessibile (DATABASE_URL mancante → migration
    saltata). Ora cadiamo automaticamente su sim_settings che e' SEMPRE
    accessibile e persiste tra deploy.
    """
    global _SIM_RUN_FALLBACK_MODE
    rid = run_data["id"]

    # Tier "background": cache file locale — SEMPRE (no-op se non scrivibile)
    try:
        _put_in_local_cache(rid, run_data)
    except Exception as exc:
        logger.warning("[SIM] insert_run: local cache fallita: %s", exc)

    client = _get_client()

    # Tier 1: Supabase sim_runs (se non gia' in fallback mode)
    if client and not _SIM_RUN_FALLBACK_MODE:
        payload = dict(run_data)
        if isinstance(payload.get("full_data"), dict):
            payload["full_data"] = json.dumps(payload["full_data"], default=str)
        try:
            client.table("sim_runs").insert(payload).execute()
            logger.info("[SIM] insert_run Supabase sim_runs ok: id=%s", rid)
            # SCRIVI ANCHE in fallback come backup di sicurezza, cosi' se la
            # tabella poi diventa inaccessibile per qualunque motivo, il run
            # resta consultabile via fallback.
            try:
                _run_fallback_save_run(run_data)
            except Exception:
                pass
            return rid
        except Exception as exc:
            if _is_table_missing_error(exc):
                logger.warning("[SIM] insert_run: sim_runs MANCANTE, switch a "
                               "fallback sim_settings (persistente). Errore: %s",
                               str(exc)[:150])
                _SIM_RUN_FALLBACK_MODE = True
                # Tentativo recovery one-shot: applica migration e ritenta
                try:
                    ensure_schema()
                    client.table("sim_runs").insert(payload).execute()
                    logger.info("[SIM] insert_run sim_runs ok DOPO recovery: id=%s", rid)
                    _SIM_RUN_FALLBACK_MODE = False
                    return rid
                except Exception as exc2:
                    logger.warning("[SIM] recovery migration fallita: %s", str(exc2)[:120])
            else:
                logger.warning("[SIM] insert_run sim_runs error (non-DDL): %s",
                               str(exc)[:200])

    # Tier 2: Supabase sim_settings fallback (DUAL-MODE)
    if client:
        if _run_fallback_save_run(run_data):
            logger.info("[SIM] insert_run salvato in fallback sim_settings: id=%s", rid)
            return rid
        logger.warning("[SIM] fallback sim_settings save fallita per id=%s", rid)

    # Tier 3: SQLite (solo se Supabase non e' configurato)
    if not client:
        try:
            import db_sqlite
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
            logger.info("[SIM] insert_run SQLite ok: id=%s", rid)
            return rid
        except Exception as exc:
            logger.error("[SIM] insert_run SQLite failed: %s", exc, exc_info=True)

    # Se siamo qui, abbiamo Supabase ma nemmeno il fallback ha funzionato.
    # Non solleviamo: la cache file ha salvato comunque (best-effort).
    logger.error("[SIM] insert_run: TUTTI i tier falliti per id=%s, solo cache file", rid)
    return rid


def get_run(run_id: str) -> dict | None:
    """
    Cerca un run in 5 sorgenti in ordine:
      1. Supabase tabella sim_runs (primario)
      2. Supabase tabella sim_settings (DUAL-MODE FALLBACK persistente)
      3. SQLite locale (fallback dev)
      4. Local file cache (ephemeral, intra-pod restart only)
      5. In-memory _active_runs del runner (last resort)
    """
    global _SIM_RUN_FALLBACK_MODE
    client = _get_client()

    # Tier 1: Supabase sim_runs
    if client and not _SIM_RUN_FALLBACK_MODE:
        try:
            r = client.table("sim_runs").select("*").eq("id", run_id).limit(1).execute()
            if r.data:
                row = r.data[0]
                if isinstance(row.get("full_data"), str):
                    try:
                        row["full_data"] = json.loads(row["full_data"])
                    except Exception:
                        pass
                row["_source"] = "supabase"
                return row
        except Exception as exc:
            if _is_table_missing_error(exc):
                _SIM_RUN_FALLBACK_MODE = True
                logger.warning("[SIM] get_run: sim_runs mancante, switch a fallback")
            else:
                logger.warning("[SIM] get_run Supabase failed: %s", exc)

    # Tier 2: Supabase sim_settings DUAL-MODE FALLBACK
    if client:
        run = _run_fallback_load_run(run_id)
        if run:
            run["_source"] = "supabase_settings_fallback"
            return run

    # Tier 3: SQLite
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
                d["_source"] = "sqlite"
                return d
    except Exception:
        pass

    # Tier 3: cache file locale (sopravvive ai pod restart su Render)
    try:
        cached = _get_from_local_cache(run_id)
        if cached:
            logger.warning("[SIM] get_run %s servito da local file cache", run_id)
            cached["_source"] = "local_file_cache"
            return cached
    except Exception as exc:
        logger.warning("[SIM] local cache lookup failed: %s", exc)

    # Tier 4: in-memory rebuild (richiede che il pod NON si sia riavviato
    # tra _finalize_run e il click "Visualizza Risultato")
    try:
        from simulator import runner as _runner
        rebuilt = _runner.rebuild_run_from_memory(run_id)
        if rebuilt:
            logger.warning(
                "[SIM] get_run %s servito da _active_runs (DB non disponibile)",
                run_id,
            )
            # Salva ANCHE in local cache cosi' la prossima volta funziona
            # senza dipendere da _active_runs (es. dopo un restart)
            try:
                _put_in_local_cache(run_id, rebuilt)
            except Exception:
                pass
            return rebuilt
    except Exception as exc:
        logger.warning("[SIM] in-memory rebuild failed: %s", exc)

    return None


def list_runs(category: str | None = None, scenario_type: str | None = None,
              outcome: str | None = None, limit: int = 50) -> list[dict]:
    """
    Lista i run con merge di tutte le sorgenti disponibili:
      1. Supabase sim_runs (primario)
      2. Supabase sim_settings (fallback DUAL-MODE)
      3. SQLite locale (dev)

    Quando il fallback e' attivo, ritorniamo dal fallback. Altrimenti
    proviamo sim_runs come prima — ma se va a vuoto e c'e' un fallback
    popolato, mergiamo entrambi (dedup per id, ordine per recency).
    """
    global _SIM_RUN_FALLBACK_MODE
    client = _get_client()
    primary: list[dict] = []
    used_fallback = False

    # Tier 1: Supabase sim_runs
    if client and not _SIM_RUN_FALLBACK_MODE:
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
            primary = r.data or []
        except Exception as exc:
            if _is_table_missing_error(exc):
                _SIM_RUN_FALLBACK_MODE = True
                logger.warning("[SIM] list_runs: sim_runs mancante, switch a fallback")
            else:
                logger.warning("[SIM] list_runs sim_runs error: %s", str(exc)[:200])

    # Tier 2: Supabase sim_settings (sempre interrogato come merge,
    # cosi' non perdiamo run salvati nel fallback prima della migration)
    fb_runs: list[dict] = []
    if client:
        try:
            fb_runs = _run_fallback_list_runs(
                limit=limit, category=category,
                scenario_type=scenario_type, outcome=outcome,
            )
            used_fallback = bool(fb_runs)
        except Exception as exc:
            logger.debug("[SIM] list_runs fallback fail: %s", exc)

    # Merge primary + fallback con dedup per id, ordina per completed_at desc
    merged: dict = {}
    for r in primary:
        if r.get("id"):
            merged[r["id"]] = r
    for r in fb_runs:
        if r.get("id") and r["id"] not in merged:
            merged[r["id"]] = r

    if merged:
        out = sorted(
            merged.values(),
            key=lambda r: str(r.get("completed_at") or ""),
            reverse=True,
        )[:limit]
        if used_fallback and not primary:
            logger.info("[SIM] list_runs: %d run serviti da fallback sim_settings",
                        len(out))
        return out

    # Tier 3: SQLite (no Supabase configurato)
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


# Tracking per evitare di hammerare Supabase con la stessa table-missing query.
# Una volta scoperto che sim_settings non esiste, fallback IMMEDIATO a settings.
_SIM_SETTINGS_TABLE_MISSING = False


def get_setting(key: str, default: str = "") -> str:
    """
    Legge una chiave da sim_settings. Se la tabella non esiste su Supabase
    (PGRST205), fallback automatico alla tabella standard `settings` del Live.

    Bug precedente: get_setting silently ritornava default quando sim_settings
    mancava. Tutti i save (advice, sim_run fallback) erano "successi" ma le
    letture successive non trovavano nulla → memoria advice sempre vuota.
    """
    global _SIM_SETTINGS_TABLE_MISSING
    client = _get_client()

    if client and not _SIM_SETTINGS_TABLE_MISSING:
        try:
            r = client.table("sim_settings").select("value").eq("key", key).limit(1).execute()
            if r.data:
                return r.data[0].get("value") or default
            return default   # tabella OK ma chiave non presente
        except Exception as exc:
            if _is_table_missing_error(exc):
                _SIM_SETTINGS_TABLE_MISSING = True
                logger.warning("[SIM] sim_settings missing → fallback a tabella settings")
            else:
                logger.debug("[SIM] get_setting %s error (non-DDL): %s", key, exc)

    # Fallback: tabella `settings` del Live (sempre presente)
    try:
        import database
        return database.get_setting(key, default) or default
    except Exception:
        return default


def set_setting(key: str, value: str):
    """
    Scrive una chiave su sim_settings. Se la tabella non esiste, fallback
    automatico a database.set_setting (tabella `settings` del Live).

    Bug precedente: la upsert su sim_settings falliva silenziosamente
    (try/except senza fallback). Ora rilancia su `settings` se sim_settings
    è inaccessibile, garantendo persistenza cross-deploy.
    """
    global _SIM_SETTINGS_TABLE_MISSING
    client = _get_client()

    if client and not _SIM_SETTINGS_TABLE_MISSING:
        try:
            client.table("sim_settings").upsert({"key": key, "value": value}).execute()
            return
        except Exception as exc:
            if _is_table_missing_error(exc):
                _SIM_SETTINGS_TABLE_MISSING = True
                logger.warning("[SIM] sim_settings missing → fallback a tabella settings")
            else:
                logger.debug("[SIM] set_setting %s error (non-DDL): %s", key, exc)

    # Fallback: tabella `settings` del Live
    try:
        import database
        database.set_setting(key, value)
    except Exception as exc:
        logger.warning("[SIM] set_setting fallback su `settings` fallito: %s", exc)


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
