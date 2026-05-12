"""
Active Runs Registry — tracking real-time dei run del Simulator.

Storage: sim_settings (chiave-valore JSON), pattern stesso del fallback runs:
  _sim_run_progress::index            JSON list di run_id attivi
  _sim_run_progress::{run_id}         JSON con progress dettagliato

Schema progress record:
  {
    "run_id": str,
    "engine": "v2_equity" | "v2_crypto" | "v1_legacy",
    "mode": "manual" | "auto",
    "category": str,
    "scenario_id": str,
    "scenario_title": str,
    "current_step": int,                   # 0-indexed; -1 = pre-start
    "total_steps": int,
    "status": "starting" | "stepping" | "calling_ai" |
              "applying_trades" | "finalizing" | "completed" | "error",
    "started_at": iso8601,
    "last_update_at": iso8601,
    "elapsed_seconds": int,                # auto-derived in list_active
    "outcome": "green"|"yellow"|"red"|null,    # solo se completed
    "pnl_pct": float|null,                 # solo se completed
    "error_msg": str|null,                 # solo se error
  }

TTL:
  - completed/error: rimossi dall'index dopo 60s (cosi' il client li vede
    "appena finito" per un po', poi spariscono)
  - stepping/starting: rimossi dopo 15min (stale - probabile crash)

Cleanup: ad ogni list_active() facciamo cleanup soft inline.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ── Storage prefix (NON collide con `_sim_active_run::` di runner.py V1
# legacy che invece tiene l'intero state del run per la shadow persistence) ──
INDEX_KEY = "_sim_run_progress::index"
RECORD_KEY_PREFIX = "_sim_run_progress::"


# TTL: dopo quanto tempo un run "completed" sparisce dalla lista
TTL_COMPLETED_SECONDS = 60        # 1 min: l'utente vede ancora il risultato
TTL_ERROR_SECONDS = 120           # 2 min: piu' tempo per leggere l'errore
TTL_STALE_RUNNING_SECONDS = 900   # 15 min: probabile crash, cleanup forzato


def _settings_get(key: str, default: str = "") -> str:
    try:
        from simulator import db as sim_db
        return sim_db.get_setting(key, default) or default
    except Exception:
        try:
            import database
            return database.get_setting(key, default) or default
        except Exception:
            return default


def _settings_set(key: str, value: str) -> None:
    try:
        from simulator import db as sim_db
        sim_db.set_setting(key, value)
    except Exception as e:
        logger.debug("active_runs settings set %s fallita: %s", key, e)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_index() -> list[str]:
    raw = _settings_get(INDEX_KEY, "[]")
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _save_index(idx: list[str]) -> None:
    # Dedup + cap a 50 (cap di sicurezza, nessuno dovrebbe avere 50 run paralleli)
    deduped = list(dict.fromkeys(idx))[:50]
    _settings_set(INDEX_KEY, json.dumps(deduped))


def _load_record(run_id: str) -> Optional[dict]:
    raw = _settings_get(f"{RECORD_KEY_PREFIX}{run_id}", "")
    if not raw:
        return None
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
        return v if isinstance(v, dict) else None
    except Exception:
        return None


def _save_record(run_id: str, record: dict) -> None:
    _settings_set(f"{RECORD_KEY_PREFIX}{run_id}",
                   json.dumps(record, default=str))


def _delete_record(run_id: str) -> None:
    """Soft delete: scrive valore vuoto (sim_settings non ha DELETE)."""
    _settings_set(f"{RECORD_KEY_PREFIX}{run_id}", "")


def _seconds_since(iso_ts: str) -> float:
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return 0.0


# ─── Public API ───────────────────────────────────────────────────────────

def register(
    run_id: str, engine: str, mode: str, category: str,
    scenario_id: str, scenario_title: str, total_steps: int,
) -> None:
    """Registra un run appena avviato. Idempotente (ri-registra OK)."""
    record = {
        "run_id": run_id,
        "engine": engine,
        "mode": mode,
        "category": category or "?",
        "scenario_id": scenario_id or "?",
        "scenario_title": (scenario_title or "")[:120],
        "current_step": -1,
        "total_steps": int(total_steps or 1),
        "status": "starting",
        "started_at": _now_iso(),
        "last_update_at": _now_iso(),
        "outcome": None,
        "pnl_pct": None,
        "error_msg": None,
    }
    _save_record(run_id, record)
    idx = _load_index()
    if run_id not in idx:
        idx.append(run_id)
        _save_index(idx)
    logger.debug("[active_runs] register %s engine=%s mode=%s steps=%d",
                 run_id, engine, mode, total_steps)


def update_step(run_id: str, step_index: int,
                status: str = "stepping") -> None:
    """Aggiorna il record allo step corrente."""
    rec = _load_record(run_id)
    if not rec:
        # Run non registrato: skip silenzioso (potrebbe essere stato cleanup-ato)
        return
    rec["current_step"] = int(step_index)
    rec["status"] = status
    rec["last_update_at"] = _now_iso()
    _save_record(run_id, rec)


def update_status(run_id: str, status: str) -> None:
    """Aggiorna solo lo status (es. 'calling_ai', 'applying_trades')."""
    rec = _load_record(run_id)
    if not rec:
        return
    rec["status"] = status
    rec["last_update_at"] = _now_iso()
    _save_record(run_id, rec)


def mark_completed(run_id: str, outcome: Optional[str] = None,
                    pnl_pct: Optional[float] = None,
                    persisted_run_id: Optional[str] = None) -> None:
    """Marca il run come completato con outcome + P&L."""
    rec = _load_record(run_id)
    if not rec:
        return
    rec["status"] = "completed"
    rec["outcome"] = outcome
    rec["pnl_pct"] = pnl_pct
    rec["persisted_run_id"] = persisted_run_id
    rec["current_step"] = rec.get("total_steps", 1)
    rec["last_update_at"] = _now_iso()
    _save_record(run_id, rec)


def mark_error(run_id: str, error_msg: str) -> None:
    """Marca il run come fallito con messaggio."""
    rec = _load_record(run_id)
    if not rec:
        return
    rec["status"] = "error"
    rec["error_msg"] = (error_msg or "")[:300]
    rec["last_update_at"] = _now_iso()
    _save_record(run_id, rec)


def list_active(include_recent: bool = True) -> list[dict]:
    """
    Ritorna i run attivi (running) e quelli appena completati/falliti.

    include_recent=True: mostra anche completed/error per TTL_COMPLETED_SECONDS
    cosi' il client vede il "flash" di fine partita.

    Cleanup inline: rimuove dall'index i record:
      - status='completed' e last_update > TTL_COMPLETED
      - status='error' e last_update > TTL_ERROR
      - status='stepping/starting' e last_update > TTL_STALE_RUNNING (crash)
    """
    idx = _load_index()
    if not idx:
        return []

    out: list[dict] = []
    keep_idx: list[str] = []

    for run_id in idx:
        rec = _load_record(run_id)
        if not rec:
            # Record orfano nell'index: pulisci
            continue

        status = rec.get("status", "")
        last_update = rec.get("last_update_at", rec.get("started_at", ""))
        elapsed_since_update = _seconds_since(last_update)

        # Cleanup decisione
        keep = True
        if status == "completed" and elapsed_since_update > TTL_COMPLETED_SECONDS:
            keep = False
        elif status == "error" and elapsed_since_update > TTL_ERROR_SECONDS:
            keep = False
        elif status in ("starting", "stepping", "calling_ai", "applying_trades",
                         "finalizing") and elapsed_since_update > TTL_STALE_RUNNING_SECONDS:
            # Probabile crash o pod restart: marca come error e tieni 1 round.
            # BUG FIX: NON aggiorniamo last_update_at qui — manteniamo
            # l'originale cosi' il TTL_ERROR si attiva correttamente al
            # prossimo poll. Prima il last_update_at veniva resettato a
            # _now_iso() e il record restava "fresco" all'infinito,
            # facendo apparire run stuck per ore nella UI.
            # Inoltre se l'errore esisteva gia' (es. error_msg gia' presente),
            # non lo sovrascrive — preserva il primo errore osservato.
            if not rec.get("error_msg"):
                rec["error_msg"] = (
                    f"Stale: nessun aggiornamento da "
                    f"{int(elapsed_since_update / 60)}min"
                )
            rec["status"] = "error"
            # Memorizza il "first_stale_marker_at" per countdown TTL preciso
            if not rec.get("first_stale_marker_at"):
                rec["first_stale_marker_at"] = _now_iso()
            _save_record(run_id, rec)
            # keep=True: viene mostrato come 'error' al prossimo poll, e
            # poi sara' rimosso quando first_stale_marker_at + TTL_ERROR e' passato.

        # Secondo controllo TTL_ERROR usando first_stale_marker_at se presente
        if status in ("starting", "stepping", "calling_ai", "applying_trades",
                       "finalizing") and rec.get("first_stale_marker_at"):
            # Run stuck originariamente, ora marcato error
            elapsed_since_marker = _seconds_since(rec["first_stale_marker_at"])
            if elapsed_since_marker > TTL_ERROR_SECONDS:
                keep = False

        if not keep:
            _delete_record(run_id)
            continue

        keep_idx.append(run_id)

        # Filtra per include_recent
        if not include_recent and status in ("completed", "error"):
            continue

        # Arricchisci con elapsed_seconds totale
        rec_out = dict(rec)
        rec_out["elapsed_seconds"] = int(_seconds_since(rec.get("started_at", "")))
        rec_out["seconds_since_update"] = int(elapsed_since_update)
        out.append(rec_out)

    # Aggiorna index se sono stati puliti elementi
    if len(keep_idx) != len(idx):
        _save_index(keep_idx)

    # Ordina: running prima (per started_at desc), poi completed/error
    def _sort_key(r):
        st = r.get("status", "")
        is_done = st in ("completed", "error")
        return (1 if is_done else 0, -_seconds_since(r.get("started_at", "")))
    out.sort(key=_sort_key)
    return out


def get(run_id: str) -> Optional[dict]:
    """Ritorna un singolo record (per debug/inspection puntuale)."""
    rec = _load_record(run_id)
    if not rec:
        return None
    rec["elapsed_seconds"] = int(_seconds_since(rec.get("started_at", "")))
    return rec


def dismiss(run_id: str) -> dict:
    """
    Rimuove FORZATAMENTE un run dal registro active_runs. Da usare per:
      - run stuck (status=error o running) che non rispondono al TTL
      - cleanup manuale post-incidente

    Rimuove sia dall'index sia il record. Non tocca la tabella sim_runs
    (se il run e' stato gia' persistito in DB resta li' come dato storico).

    Ritorna {removed: bool, was_present: bool, prev_status: str | None}.
    """
    idx = _load_index()
    was_present = run_id in idx
    prev_rec = _load_record(run_id)
    prev_status = (prev_rec or {}).get("status")

    # Rimuovi dall'index
    if was_present:
        new_idx = [r for r in idx if r != run_id]
        _save_index(new_idx)

    # Rimuovi il record
    _delete_record(run_id)

    return {
        "removed": True,
        "was_present": was_present,
        "prev_status": prev_status,
        "run_id": run_id,
    }


def dismiss_all_stuck() -> dict:
    """
    Rimuove TUTTI i run con status='error' o stale (running ma senza
    update da > TTL_STALE_RUNNING_SECONDS). Utile per cleanup massivo.

    Ritorna {removed: int, run_ids: [str]}.
    """
    idx = _load_index()
    removed_ids: list[str] = []
    for run_id in list(idx):
        rec = _load_record(run_id)
        if not rec:
            # Orfani: pulisci sempre
            removed_ids.append(run_id)
            continue
        status = rec.get("status", "")
        last_update = rec.get("last_update_at", rec.get("started_at", ""))
        elapsed = _seconds_since(last_update)
        is_stuck = (
            status == "error"
            or (status in ("starting", "stepping", "calling_ai",
                            "applying_trades", "finalizing")
                and elapsed > TTL_STALE_RUNNING_SECONDS)
        )
        if is_stuck:
            _delete_record(run_id)
            removed_ids.append(run_id)
    keep = [r for r in idx if r not in removed_ids]
    if len(keep) != len(idx):
        _save_index(keep)
    return {"removed": len(removed_ids), "run_ids": removed_ids}
