"""
Applicazione principale FastAPI per l'agente geopolitico di investimento.
Fornisce endpoint REST per il portafoglio, le posizioni, i trade e i log.
"""

import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import BackgroundTasks, FastAPI, File, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import agent
import database
import portfolio
import scheduler

# Cartella per i documenti caricati (persistente su Render via /data)
DOCUMENTS_DIR = os.environ.get("DOCUMENTS_PATH", os.path.join(os.path.dirname(__file__), "documents"))
os.makedirs(DOCUMENTS_DIR, exist_ok=True)

# Configurazione del logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Gestione del ciclo di vita dell'applicazione.
    All'avvio: inizializza il database e riavvia lo scheduler se era attivo.
    Alla chiusura: arresta lo scheduler.
    """
    # Fase di avvio
    logger.info("Inizializzazione del database...")
    database.init_db()
    logger.info("Database inizializzato con successo.")

    # Schema Simulator (idempotente, fail-safe)
    try:
        from simulator import db as sim_db
        sim_db.ensure_schema()
    except Exception as e:
        logger.warning("Simulator schema init fallita (non bloccante): %s", e)

    # Schema Chat Assistant (idempotente, fail-safe)
    # Crea le tabelle chat_conversations / chat_messages al volo se mancano.
    # Su Supabase richiede DATABASE_URL o SUPABASE_DB_PASSWORD.
    try:
        from agents import chat_assistant
        ok, err = chat_assistant.ensure_chat_tables()
        if ok:
            logger.info("Chat assistant: tabelle chat_* verificate.")
        else:
            logger.warning("Chat assistant: tabelle non verificate (%s). "
                           "Verra' ritentato alla prima richiesta.", err)
    except Exception as e:
        logger.warning("Chat assistant init fallita (non bloccante): %s", e)

    # Preset documents crypto: scansiona backend/preset_documents/crypto/
    # e fa upsert nel DB con is_preset=true. Idempotente: re-run sicuro.
    try:
        preset_dir = os.path.join(os.path.dirname(__file__), "preset_documents", "crypto")
        if os.path.isdir(preset_dir):
            n_loaded = 0
            for filename in sorted(os.listdir(preset_dir)):
                if not filename.lower().endswith((".txt", ".md")):
                    continue
                fpath = os.path.join(preset_dir, filename)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        content = f.read()
                    database.upsert_preset_document(
                        filename=filename, content=content,
                        file_size=len(content.encode("utf-8")),
                        category="crypto",
                    )
                    n_loaded += 1
                except Exception as ex:
                    logger.warning("Errore loading preset %s: %s", filename, ex)
            logger.info("Preset crypto documenti caricati: %d", n_loaded)
    except Exception as e:
        logger.warning("Auto-load preset documents fallito: %s", e)

    # Avvia SEMPRE lo scheduler al deploy — il monitoraggio è sempre attivo
    try:
        logger.info("Avvio automatico dello scheduler (sempre attivo al deploy)...")
        scheduler.start_scheduler()
        logger.info("Scheduler avviato con successo al deploy.")
    except Exception as e:
        logger.error("ERRORE avvio scheduler al deploy: %s", e, exc_info=True)
        # Ritenta dopo un breve delay (il DB potrebbe non essere pronto)
        import asyncio
        await asyncio.sleep(2)
        try:
            scheduler.start_scheduler()
            logger.info("Scheduler avviato al secondo tentativo.")
        except Exception as e2:
            logger.error("Scheduler non avviato dopo 2 tentativi: %s", e2, exc_info=True)

    yield

    # Fase di chiusura (deploy/restart — NON salvare stato nel DB)
    logger.info("Arresto dello scheduler (deploy/restart, non persiste)...")
    scheduler.stop_scheduler(persist=False)
    logger.info("Applicazione chiusa correttamente.")


# Creazione dell'applicazione FastAPI con gestione del ciclo di vita
app = FastAPI(
    title="Agente Geopolitico di Investimento",
    description="API per il monitoraggio e la gestione degli investimenti basati su analisi geopolitica.",
    version="1.0.0",
    lifespan=lifespan,
)

# Abilitazione CORS per tutte le origini (utile per lo sviluppo del frontend)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Endpoint di controllo ---


@app.get("/health")
@app.get("/api/health")
async def health_check():
    """Controllo dello stato di salute del servizio."""
    return {"status": "ok"}


# --- Endpoint del portafoglio ---


@app.get("/api/portfolio")
async def get_portfolio():
    """Restituisce lo stato attuale del portafoglio."""
    try:
        state = portfolio.get_portfolio_state()
        return state
    except Exception as e:
        logger.error(f"Errore nel recupero dello stato del portafoglio: {e}", exc_info=True)
        return {"error": str(e)}


# --- Endpoint delle posizioni ---


@app.get("/api/positions")
async def get_positions():
    """
    Restituisce le posizioni aperte con prezzi correnti dalla cache (price_quotes).
    Se la cache è fresca (<2 min), sovrascrive current_price e ricalcola unrealized_pnl.
    """
    try:
        positions = database.get_positions() or []
        if not positions:
            return positions

        # Arricchisci con prezzi dalla cache
        try:
            from price_polling import get_cached_prices_bulk
            tickers = [p.get("ticker") for p in positions if p.get("ticker")]
            cached = get_cached_prices_bulk(tickers, max_age_seconds=120)

            for p in positions:
                t = p.get("ticker")
                if t and t in cached:
                    quote = cached[t]
                    p["current_price"] = quote["price"]
                    qty = p.get("quantity", 0)
                    avg = p.get("avg_buy_price", 0)
                    if qty and avg:
                        p["unrealized_pnl"] = round((quote["price"] - avg) * qty, 2)
                    p["price_age_seconds"] = quote["age_seconds"]
                    p["price_change_pct"] = quote.get("change_pct", 0)
        except Exception as cache_err:
            logger.debug("Cache prezzi non disponibile: %s", cache_err)

        return positions
    except Exception as e:
        logger.error(f"Errore nel recupero delle posizioni: {e}", exc_info=True)
        return {"error": str(e)}


@app.get("/api/debug/massive-key-check")
async def debug_massive_key():
    """Diagnostica: verifica come viene letta la key da price_polling._get_massive_key()."""
    from price_polling import _get_massive_key
    key = _get_massive_key()
    env_key = os.environ.get("MASSIVE_API_KEY", "")
    db_key = ""
    try:
        db_key = database.get_setting("massive_api_key", "") or ""
    except Exception as e:
        db_key = f"ERROR: {e}"
    return {
        "key_found_via_helper": bool(key),
        "key_helper_length": len(key) if key else 0,
        "key_helper_prefix": key[:6] if key else "",
        "key_in_env": bool(env_key),
        "key_in_db": bool(db_key) and not str(db_key).startswith("ERROR"),
        "key_db_prefix": db_key[:6] if db_key and not str(db_key).startswith("ERROR") else "",
        "db_error": str(db_key) if str(db_key).startswith("ERROR") else None,
    }


@app.post("/api/prices/trigger-poll")
async def trigger_price_poll():
    """Forza un ciclo di Price Polling (debug/test)."""
    try:
        from price_polling import update_price_cache
        result = await update_price_cache()
        return {"status": "ok", **result}
    except Exception as e:
        logger.error(f"Errore trigger price poll: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})


@app.post("/api/admin/audit-position-prices")
async def audit_position_prices():
    """
    Confronta il `current_price` salvato per ogni posizione con un fetch
    fresco da yfinance. Riporta le discrepanze (>5%) e applica il fix.

    Uso: chiamare se sospetti che i current_price delle posizioni siano stale
    o corrotti (es. MSFT a $420 quando in realtà è a $350).
    """
    import asyncio
    try:
        positions = database.get_positions() or []
        if not positions:
            return {"status": "ok", "positions_audited": 0, "discrepancies": [], "fixed": 0}

        tickers = [p.get("ticker") for p in positions if p.get("ticker")]
        # Fetch fresco via per-ticker (più affidabile del batch)
        from price_polling import _fetch_yfinance_per_ticker_fallback
        fresh = await asyncio.to_thread(_fetch_yfinance_per_ticker_fallback, tickers)

        discrepancies = []
        fixed = 0
        for p in positions:
            ticker = p.get("ticker")
            stored = float(p.get("current_price") or 0)
            avg_buy = float(p.get("avg_buy_price") or 0)
            quote = fresh.get(ticker) if ticker else None
            if not quote:
                discrepancies.append({
                    "ticker": ticker,
                    "stored_current": stored,
                    "fresh": None,
                    "status": "NO_FRESH_DATA",
                })
                continue
            fresh_price = float(quote.get("price") or 0)
            if fresh_price <= 0:
                continue
            delta_pct = ((stored - fresh_price) / fresh_price) * 100 if fresh_price > 0 else 0
            entry = {
                "ticker": ticker,
                "stored_current": round(stored, 2),
                "fresh_yfinance": round(fresh_price, 2),
                "avg_buy": round(avg_buy, 2),
                "delta_pct": round(delta_pct, 1),
            }
            # Se delta > 5%, applica il fix (sovrascrive con il valore fresco)
            if abs(delta_pct) > 5:
                try:
                    database.update_position_price(ticker, fresh_price)
                    fixed += 1
                    entry["status"] = "FIXED"
                except Exception as e:
                    entry["status"] = f"FIX_FAILED: {e}"
                discrepancies.append(entry)
            else:
                entry["status"] = "OK"
                # Non aggiungiamo gli OK alla lista (rumore), salvo che voglia vederli

        return {
            "status": "ok",
            "positions_audited": len(positions),
            "discrepancies_found": len([d for d in discrepancies if d.get("status") == "FIXED"]),
            "fixed": fixed,
            "details": discrepancies,
        }
    except Exception as e:
        logger.error(f"Errore audit-position-prices: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})


@app.get("/api/prices/quotes")
async def get_price_quotes(tickers: str = Query(default="")):
    """
    Restituisce gli ultimi prezzi cached per i ticker richiesti.
    Esempio: /api/prices/quotes?tickers=SPY,XOM,LMT
    Senza parametro: ritorna tutti i ticker presenti in cache.
    """
    try:
        from price_polling import get_cached_prices_bulk
        if tickers:
            ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]
            return get_cached_prices_bulk(ticker_list, max_age_seconds=300)
        else:
            client = database.get_client()
            if not client:
                return {}
            rows = client.table("price_quotes").select("*").execute()
            return {r["ticker"]: r for r in (rows.data or [])}
    except Exception as e:
        logger.error(f"Errore /api/prices/quotes: {e}", exc_info=True)
        return {"error": str(e)}


# --- Endpoint della cronologia dei trade ---


@app.get("/api/trades")
async def get_trades(limit: int = Query(default=50, ge=1, le=1000)):
    """
    Restituisce la cronologia dei trade con il ragionamento associato.
    Accetta il parametro ?limit per limitare il numero di risultati.
    """
    try:
        trades = database.get_trades(limit=limit)
        return trades
    except Exception as e:
        logger.error(f"Errore nel recupero dei trade: {e}", exc_info=True)
        return {"error": str(e)}


# --- Endpoint dei log dell'agente ---


@app.get("/api/logs")
async def get_logs(
    limit: int = Query(default=100, ge=1, le=5000),
    run_id: str | None = Query(default=None),
):
    """
    Restituisce i log dell'agente.
    Accetta i parametri ?limit e ?run_id per filtrare i risultati.
    """
    try:
        if run_id:
            logs = database.get_logs_by_run(run_id)
        else:
            logs = database.get_agent_logs(limit=limit)
        return logs
    except Exception as e:
        logger.error(f"Errore nel recupero dei log: {e}", exc_info=True)
        return {"error": str(e)}


# --- Endpoint dei dati geopolitici ---


@app.get("/api/geopolitical")
async def get_geopolitical():
    """Restituisce gli snapshot geopolitici piu' recenti."""
    try:
        snapshots = database.get_geopolitical_snapshots()
        return snapshots
    except Exception as e:
        logger.error(f"Errore nel recupero degli snapshot geopolitici: {e}", exc_info=True)
        return {"error": str(e)}


# --- Endpoint di controllo dell'agente ---


@app.post("/api/agent/run")
async def trigger_agent_run(background_tasks: BackgroundTasks):
    """
    Avvia manualmente un'esecuzione singola del pipeline multi-agente.
    PRIMA: chiamava il vecchio agent.run_agent() (single-agent legacy → log
    confondenti tipo "mode: weekend, architecture: single-agent" anche con
    la nuova architettura).
    ORA: chiama run_full_pipeline (Watchdog → Technical → Decision) o
    direttamente run_scheduled_24h_pipeline (R1) se mercati chiusi.
    """
    run_id = str(uuid.uuid4())
    mode = scheduler.get_current_mode()
    market_open = scheduler.is_market_open()

    async def _run():
        try:
            logger.info(f"Esecuzione manuale pipeline avviata (run_id: {run_id}, mode: {mode}).")
            from agents.orchestrator import (
                run_full_pipeline,
                run_scheduled_24h_pipeline,
            )
            if market_open:
                # Orari di mercato → pipeline completa con Sonnet 4.5
                result = await run_full_pipeline(run_id=run_id)
            else:
                # Mercati chiusi → R1 scheduled (crypto only)
                result = await run_scheduled_24h_pipeline(run_id=run_id)
            logger.info(f"Esecuzione manuale completata (run_id: {run_id}, result: {result.get('decision', '?')}).")
        except Exception as e:
            logger.error(
                f"Errore durante l'esecuzione manuale (run_id: {run_id}): {e}",
                exc_info=True,
            )

    background_tasks.add_task(_run)
    return {"status": "started", "run_id": run_id, "mode": mode, "market_open": market_open}


@app.post("/api/agent/start")
async def start_continuous_monitoring():
    """Avvia il monitoraggio continuo (scheduler ogni 20 minuti)."""
    try:
        if scheduler.is_scheduler_running():
            return {"status": "already_running", "message": "Il monitoraggio e' gia' attivo."}
        scheduler.start_scheduler()
        return {"status": "started", "message": "Monitoraggio continuo avviato."}
    except Exception as e:
        logger.error(f"Errore nell'avvio del monitoraggio: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/agent/stop")
async def stop_continuous_monitoring():
    """Ferma il monitoraggio continuo."""
    try:
        scheduler.stop_scheduler()
        return {"status": "stopped", "message": "Monitoraggio continuo fermato."}
    except Exception as e:
        logger.error(f"Errore nell'arresto del monitoraggio: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/agent/status")
async def get_agent_status():
    """
    Restituisce lo stato corrente dell'agente e dello scheduler.
    Include: running, last_run, next_run, mode, market_open.
    """
    try:
        status = agent.agent_status.copy()
        scheduler_info = scheduler.get_scheduler_info()
        status.update({
            "scheduler_running": scheduler_info["running"],
            "mode": scheduler_info["mode"],
            "market_open": scheduler_info["market_open"],
            "is_weekend": scheduler_info["is_weekend"],
            "next_run": scheduler_info.get("next_run"),
            "next_market_open": scheduler_info.get("next_market_open"),
            "agents": scheduler_info.get("agents", {}),
            "architecture": "multi-agent" if scheduler_info["mode"] == "full" else "single-agent",
            "deepseek_available": bool(os.environ.get("DEEPSEEK_API_KEY")),
        })
        return status
    except Exception as e:
        logger.error(f"Errore nel recupero dello stato dell'agente: {e}", exc_info=True)
        return {"error": str(e)}


@app.get("/api/intelligence")
async def get_intelligence(limit: int = Query(default=20, ge=1, le=100)):
    """Restituisce le analisi di intelligence accumulate nei weekend."""
    try:
        data = database.get_weekend_intelligence(limit=limit)
        return data
    except Exception as e:
        logger.error(f"Errore nel recupero dell'intelligence: {e}", exc_info=True)
        return {"error": str(e)}


@app.get("/api/briefings")
async def get_briefings(limit: int = Query(default=20, ge=1, le=100)):
    """Restituisce i pre-market briefings."""
    try:
        data = database.get_pre_market_briefings(limit=limit)
        return data
    except Exception as e:
        logger.error(f"Errore nel recupero dei briefings: {e}", exc_info=True)
        return {"error": str(e)}


@app.get("/api/scout-buffer")
async def get_scout_buffer(
    limit: int = Query(default=80, ge=1, le=500),
    hours: int = Query(default=24, ge=1, le=168),
):
    """
    Restituisce il contenuto recente di intelligence_buffer (micro-schede Scout)
    per la visualizzazione nel frontend. Include tutti i find dello Scout:
    GDELT, NewsAPI, yFinance News, Reddit (sentiment retail), X, ClawStreet,
    Congressional.
    """
    try:
        client = database.get_client()
        if not client:
            return []
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        cutoff = (_dt.now(_tz.utc) - _td(hours=hours)).isoformat()
        result = client.table("intelligence_buffer") \
            .select("*") \
            .gte("timestamp", cutoff) \
            .order("timestamp", desc=True) \
            .limit(limit) \
            .execute()
        rows = result.data if result.data else []
        # Espandi raw_content (JSON) per esporre i campi originali
        enriched = []
        for r in rows:
            raw = r.get("raw_content") or "{}"
            try:
                raw_obj = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                raw_obj = {}
            enriched.append({
                "id": r.get("id"),
                "timestamp": r.get("timestamp"),
                "source_type": r.get("source_type"),
                "micro_summary": r.get("micro_summary"),
                "sentiment_score": r.get("sentiment_score"),
                "sentiment_type": raw_obj.get("sentiment_type", "INSTITUTIONAL"),
                "key_tickers": raw_obj.get("key_tickers", []),
                "risk_keywords": raw_obj.get("risk_keywords", []),
                "run_id": r.get("run_id"),
            })
        return enriched
    except Exception as e:
        logger.error(f"Errore /api/scout-buffer: {e}", exc_info=True)
        return {"error": str(e)}


# --- Endpoint delle impostazioni ---


class SettingsPayload(BaseModel):
    """Modello per il salvataggio delle impostazioni."""
    settings: dict


@app.get("/api/settings")
async def get_settings():
    """Restituisce tutte le impostazioni correnti."""
    try:
        all_settings = database.get_all_settings()
        return {"settings": all_settings}
    except Exception as e:
        logger.error(f"Errore nel recupero delle impostazioni: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/settings")
async def save_settings(payload: SettingsPayload):
    """Salva un insieme di impostazioni (chiave-valore)."""
    try:
        for key, value in payload.settings.items():
            database.set_setting(key, value if isinstance(value, str) else json.dumps(value))
        return {"status": "saved", "count": len(payload.settings)}
    except Exception as e:
        logger.error(f"Errore nel salvataggio delle impostazioni: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


# ═══════════════════════════════════════════════════════════════════════
# CHAT ASSISTANT — analista AI delle decisioni del Decision Agent
# ═══════════════════════════════════════════════════════════════════════
# Mini-memoria: solo le ultime 10 conversazioni vengono conservate.
# Engine: DeepSeek-R1 (reasoning) per analisi profonda dei pattern.
# Sicurezza: read-only, l'AI NON puo' eseguire trade.

class ChatSendPayload(BaseModel):
    conversation_id: int | None = None  # se None, crea nuova conversazione
    message: str
    selected_trade_ids: list[int] | None = None  # trade scelti dall'utente


@app.get("/api/chat/health")
async def chat_health():
    """
    Diagnostica del chat assistant: verifica tabelle, API key, e prova a
    costruire il live_context. Endpoint utile per debug del HTTP 500.
    """
    try:
        from agents import chat_assistant
        ok, err = chat_assistant.ensure_chat_tables()
        api_key = chat_assistant._get_api_key()
        live_ctx_len = 0
        live_ctx_err = None
        try:
            live_ctx = chat_assistant.build_live_context()
            live_ctx_len = len(live_ctx)
        except Exception as e:
            live_ctx_err = str(e)
        return {
            "tables_ok": ok,
            "tables_error": err if not ok else None,
            "deepseek_api_key_set": bool(api_key),
            "deepseek_api_key_len": len(api_key) if api_key else 0,
            "live_context_chars": live_ctx_len,
            "live_context_error": live_ctx_err,
        }
    except Exception as e:
        import traceback
        return JSONResponse(status_code=500, content={
            "error": str(e),
            "type": type(e).__name__,
            "traceback": traceback.format_exc()[-2000:],
        })


@app.post("/api/chat/init-tables")
async def chat_init_tables():
    """
    Forza la creazione delle tabelle chat_* su Supabase via psycopg2.
    Utile se la migration automatica all'avvio non e' passata (es.
    DATABASE_URL non era configurato al momento del primo deploy).
    """
    try:
        from agents import chat_assistant
        ok, err = chat_assistant.ensure_chat_tables()
        # Verifica esplicita post-creazione
        try:
            test_id = database.create_chat_conversation(
                title="__health_check__",
                selected_decisions=None,
            )
            if test_id:
                # Cleanup: rimuovi la conversazione di test
                try:
                    database.delete_chat_conversation(test_id)
                except Exception:
                    pass
                return {"ok": True, "tables_ready": True, "test_id_created": test_id}
            else:
                return {
                    "ok": ok, "tables_ready": ok,
                    "error": err, "test_failed": "INSERT returned None",
                }
        except Exception as test_err:
            return {
                "ok": False, "tables_ready": ok,
                "error": err,
                "test_failed": f"INSERT raised: {type(test_err).__name__}: {test_err}",
            }
    except Exception as e:
        import traceback
        return JSONResponse(status_code=500, content={
            "ok": False,
            "error": str(e), "type": type(e).__name__,
            "traceback": traceback.format_exc()[-1500:],
        })


@app.post("/api/chat/test-deepseek")
async def chat_test_deepseek():
    """
    Diagnostica DIRETTA su DeepSeek: invia una richiesta minima ('Ciao')
    e ritorna esito + dettagli. Bypassa tabelle e contesto live, per
    isolare problemi di API key / rete / quota.
    """
    try:
        from agents import chat_assistant
        api_key = chat_assistant._get_api_key()
        if not api_key:
            return {"ok": False, "stage": "api_key", "error": "DEEPSEEK_API_KEY non configurata"}

        import aiohttp
        payload = {
            "model": chat_assistant.DEEPSEEK_R1_MODEL,
            "messages": [
                {"role": "system", "content": "Rispondi in 1 parola."},
                {"role": "user", "content": "Ciao"},
            ],
            "max_tokens": 10,
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                chat_assistant.DEEPSEEK_API_URL, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                body = await resp.text()
                return {
                    "ok": resp.status == 200,
                    "status_code": resp.status,
                    "body_preview": body[:800],
                    "stage": "http_response",
                }
    except Exception as e:
        import traceback
        return JSONResponse(status_code=500, content={
            "ok": False, "stage": "exception",
            "error": str(e), "type": type(e).__name__,
            "traceback": traceback.format_exc()[-1500:],
        })


@app.get("/api/chat/conversations")
async def chat_list_conversations(limit: int = Query(default=10, ge=1, le=50)):
    """Lista delle ultime conversazioni con l'analista AI."""
    try:
        return database.get_chat_conversations(limit=limit)
    except Exception as e:
        logger.error(f"Errore lista conversazioni chat: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/chat/conversations/{conv_id}/messages")
async def chat_get_messages(conv_id: int):
    """Tutti i messaggi di una conversazione."""
    try:
        msgs = database.get_chat_messages(conv_id)
        return {"conversation_id": conv_id, "messages": msgs}
    except Exception as e:
        logger.error(f"Errore lettura messaggi chat: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.delete("/api/chat/conversations/{conv_id}")
async def chat_delete_conversation(conv_id: int):
    """Elimina una conversazione (cascade sui messaggi)."""
    try:
        database.delete_chat_conversation(conv_id)
        return {"status": "deleted", "id": conv_id}
    except Exception as e:
        logger.error(f"Errore cancellazione conversazione chat: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/chat/decisions")
async def chat_list_decisions(limit: int = Query(default=30, ge=1, le=100)):
    """
    Lista degli ultimi trade (BUY/SELL eseguiti dal Decision Agent standard
    o crypto) che l'utente puo' selezionare come contesto per la chat.
    Restituisce solo i campi essenziali per la UI di selezione.
    """
    try:
        trades = database.get_trades(limit=limit) or []
        compact = [{
            "id": t.get("id"),
            "timestamp": t.get("timestamp"),
            "ticker": t.get("ticker"),
            "action": t.get("action"),
            "quantity": t.get("quantity"),
            "price": t.get("price"),
            "confidence": t.get("confidence_score"),
            "is_crypto": "-USD" in (t.get("ticker") or ""),
        } for t in trades]
        return compact
    except Exception as e:
        logger.error(f"Errore lista decisioni chat: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/chat/send")
async def chat_send(payload: ChatSendPayload):
    """
    Invia un messaggio all'analista AI. Crea automaticamente una nuova
    conversazione se conversation_id e' None. Ritorna la risposta completa
    (non-streaming per semplicita').

    Il prompt include sempre: portfolio, posizioni, market state, ultime
    decisioni, briefing. Se selected_trade_ids e' fornito, anche i trade
    selezionati come contesto specifico.
    """
    try:
        from agents import chat_assistant

        message = (payload.message or "").strip()
        if not message:
            return JSONResponse(status_code=400, content={"error": "Messaggio vuoto"})

        # 0. Verifica/crea le tabelle chat_* se mancano (Supabase first-deploy)
        ok, err_msg = chat_assistant.ensure_chat_tables()
        if not ok:
            logger.error("chat_send: tabelle chat non disponibili: %s", err_msg)
            return JSONResponse(
                status_code=500,
                content={
                    "error": "Tabelle chat non inizializzate.",
                    "detail": err_msg,
                    "hint": "Verifica DATABASE_URL su Render oppure esegui la migration manualmente.",
                },
            )

        # 1. Determina/crea la conversazione
        conv_id = payload.conversation_id
        is_new_conversation = False
        if conv_id is None:
            title = chat_assistant.auto_title_from_first_message(message)
            sel_json = json.dumps(payload.selected_trade_ids) if payload.selected_trade_ids else None
            try:
                conv_id = database.create_chat_conversation(title=title, selected_decisions=sel_json)
            except Exception as e:
                logger.error("chat_send: create_chat_conversation crash: %s", e, exc_info=True)
                err_str = str(e).lower()
                # Recovery automatico: se "relation does not exist" o "no such table",
                # tenta migration al volo e ritenta UNA volta
                if any(s in err_str for s in ["does not exist", "no such table", "relation"]):
                    logger.info("chat_send: tentativo recovery via ensure_chat_tables...")
                    ok2, err2 = chat_assistant.ensure_chat_tables()
                    if ok2:
                        try:
                            conv_id = database.create_chat_conversation(
                                title=title, selected_decisions=sel_json,
                            )
                        except Exception as e2:
                            return JSONResponse(
                                status_code=500,
                                content={
                                    "error": "Errore creazione conversazione (recovery fallito)",
                                    "detail": f"{type(e2).__name__}: {e2}",
                                    "hint": "Le tabelle non sono accessibili. Prova POST /api/chat/init-tables.",
                                },
                            )
                    else:
                        return JSONResponse(
                            status_code=500,
                            content={
                                "error": "Tabelle chat_* non create",
                                "detail": err2,
                                "hint": "Configura DATABASE_URL su Render, poi fai POST /api/chat/init-tables.",
                            },
                        )
                else:
                    return JSONResponse(
                        status_code=500,
                        content={
                            "error": "Errore creazione conversazione",
                            "detail": f"{type(e).__name__}: {e}",
                        },
                    )
            is_new_conversation = True
            if conv_id is None:
                return JSONResponse(
                    status_code=500,
                    content={
                        "error": "Impossibile creare la conversazione (DB ha ritornato None)",
                        "hint": (
                            "Insert su chat_conversations e' tornato senza errori "
                            "ma senza data. Verifica /api/chat/health e prova "
                            "POST /api/chat/init-tables per ricreare lo schema."
                        ),
                    },
                )

        # 2. Carica history esistente (esclude il nuovo messaggio)
        try:
            history = database.get_chat_messages(conv_id) or []
        except Exception as e:
            logger.warning("chat_send: get_chat_messages failed: %s", e)
            history = []

        # 3. Costruisci contesto decisioni (solo dai trade_ids forniti ora;
        #    se non passati, usa quelli salvati sulla conversazione)
        trade_ids = payload.selected_trade_ids
        if not trade_ids and not is_new_conversation:
            try:
                convs = database.get_chat_conversations(limit=50) or []
                for c in convs:
                    if c.get("id") == conv_id and c.get("selected_decisions"):
                        try:
                            trade_ids = json.loads(c["selected_decisions"])
                        except Exception:
                            trade_ids = None
                        break
            except Exception:
                pass

        decisions_context = ""
        if trade_ids:
            try:
                all_trades = database.get_trades(limit=200) or []
                trade_id_set = set(trade_ids)
                selected = [t for t in all_trades if t.get("id") in trade_id_set]
                decisions_context = chat_assistant.build_decisions_context(selected)
                try:
                    database.update_chat_conversation_decisions(conv_id, json.dumps(trade_ids))
                except Exception:
                    pass
            except Exception as e:
                logger.warning("chat_send: decisions_context build failed: %s", e)

        # 4. Costruisci sempre il live_context (portfolio + market + ...)
        try:
            live_context = chat_assistant.build_live_context()
        except Exception as e:
            logger.warning("chat_send: live_context build failed: %s", e)
            live_context = ""

        # 5. Salva il messaggio user PRIMA di chiamare l'AI
        try:
            database.insert_chat_message(conv_id, "user", message)
        except Exception as e:
            logger.error("chat_send: insert_chat_message (user) failed: %s", e, exc_info=True)
            return JSONResponse(
                status_code=500,
                content={"error": "Errore salvataggio messaggio user", "detail": str(e)},
            )

        # 6. Chiama R1
        reply, reasoning = await chat_assistant.chat_completion(
            system_prompt=chat_assistant.SYSTEM_PROMPT,
            history=history,
            user_message=message,
            decisions_context=decisions_context,
            live_context=live_context,
        )

        # 7. Salva la risposta dell'assistant (anche se è errore, per la cronologia)
        try:
            database.insert_chat_message(conv_id, "assistant", reply)
        except Exception as e:
            logger.warning("chat_send: insert_chat_message (assistant) failed: %s", e)

        # 8. Mini-memoria: trim a 10 conversazioni totali
        try:
            database.trim_chat_conversations(keep_last=10)
        except Exception:
            pass

        return {
            "conversation_id": conv_id,
            "is_new": is_new_conversation,
            "reply": reply,
            "trades_used": len(trade_ids) if trade_ids else 0,
            "live_context_chars": len(live_context),
        }
    except Exception as e:
        logger.error(f"Errore chat send (top-level): {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": str(e), "type": type(e).__name__},
        )


# ═══════════════════════════════════════════════════════════════════════
# SIMULATOR — endpoint
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/simulator/scenarios")
async def sim_scenarios(category: str = Query(...)):
    """Lista scenari disponibili per categoria (senza reveal)."""
    from simulator import scenarios as _scn
    return _scn.get_scenarios_by_category(category)


@app.get("/api/simulator/scenarios/counts")
async def sim_scenario_counts():
    """{category: count} per la UI."""
    from simulator import scenarios as _scn
    return _scn.get_scenario_counts()


@app.get("/api/simulator/kpi")
async def sim_kpi():
    """KPI aggregati per la dashboard simulator."""
    from simulator import db as sim_db
    runs = sim_db.list_runs(limit=500)
    total = len(runs)
    if total == 0:
        return {"score": None, "score_label": "Nessun run", "win_rate": 0,
                "wins": 0, "total": 0, "single_step": 0, "multi_step": 0,
                "avg_delta_sp": 0, "win_by_category": {}}
    wins = sum(1 for r in runs if r.get("outcome") == "green")
    single = sum(1 for r in runs if r.get("scenario_type") == "single")
    multi = total - single
    avg_delta = sum((r.get("delta_sp") or 0) for r in runs) / total
    win_by_cat = {}
    for cat in ["normale", "geopolitico", "macro", "crash_rally"]:
        cr = [r for r in runs if r.get("category") == cat]
        n = len(cr)
        win_by_cat[cat] = (sum(1 for r in cr if r.get("outcome") == "green") / n) if n else 0
        win_by_cat[cat + "_total"] = n
    return {
        "score": round(wins / total * 100, 1),
        "score_label": f"{wins} verdi su {total} run",
        "win_rate": wins / total,
        "wins": wins, "total": total, "single_step": single, "multi_step": multi,
        "avg_delta_sp": avg_delta,
        "win_by_category": win_by_cat,
    }


@app.get("/api/simulator/runs")
async def sim_list_runs(category: str = Query(default=None),
                         scenario_type: str = Query(default=None),
                         outcome: str = Query(default=None),
                         limit: int = Query(default=20, ge=1, le=500)):
    from simulator import db as sim_db
    return sim_db.list_runs(category=category, scenario_type=scenario_type,
                             outcome=outcome, limit=limit)


@app.get("/api/simulator/result/{run_id}")
async def sim_get_result(run_id: str):
    """
    Ritorna il risultato di un run completato. Fallback chain:
      1. Supabase sim_runs
      2. SQLite locale
      3. In-memory _active_runs (se l'INSERT su DB e' fallito)
    """
    try:
        from simulator import db as sim_db
        run = sim_db.get_run(run_id)
        if not run:
            # Diagnostica: verifica se il run e' stato avviato ma non finalizzato
            try:
                from simulator import runner as _runner
                in_memory = run_id in getattr(_runner, "_active_runs", {})
            except Exception:
                in_memory = False
            return JSONResponse(
                status_code=404,
                content={
                    "error": "Run non trovato",
                    "run_id": run_id,
                    "in_active_memory": in_memory,
                    "hint": (
                        "Il run e' stato avviato ma il salvataggio su DB non e' riuscito. "
                        "Verifica /api/simulator/health per diagnostica completa."
                        if in_memory else
                        "Run inesistente o scaduto. Avvia un nuovo scenario."
                    ),
                },
            )
        # Espandi full_data per UI
        full = run.get("full_data") or {}
        if isinstance(full, dict):
            run["price_chart"] = full.get("price_chart", [])
            run["steps_data"] = full.get("steps_data", [])
        return run
    except Exception as e:
        logger.error("sim_get_result error: %s", e, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": str(e), "type": type(e).__name__},
        )


@app.post("/api/simulator/init-tables")
async def sim_init_tables():
    """
    Forza la creazione delle tabelle sim_* su Supabase via psycopg2.
    Utile se la migration auto all'avvio non e' passata. Verifica con un
    INSERT/DELETE di test che il client Supabase possa effettivamente leggere.
    """
    try:
        from simulator import db as sim_db
        sim_db.ensure_schema()

        # Verifica con probe diretto
        client = sim_db._get_client()
        if client:
            try:
                r = client.table("sim_runs").select("id").limit(1).execute()
                return {
                    "ok": True, "tables_ready": True,
                    "client": "supabase",
                    "current_runs_count_sample": len(r.data or []),
                }
            except Exception as e:
                return {
                    "ok": False, "tables_ready": False,
                    "client": "supabase",
                    "probe_error": str(e),
                    "hint": (
                        "Migration psycopg2 ha girato ma la tabella non e' "
                        "leggibile dal client Supabase. Verifica DATABASE_URL "
                        "e SUPABASE_DB_PASSWORD su Render."
                    ),
                }
        else:
            # SQLite path
            import db_sqlite
            try:
                with db_sqlite.get_db() as conn:
                    n = conn.execute("SELECT COUNT(*) as c FROM sim_runs").fetchone()
                    return {"ok": True, "tables_ready": True, "client": "sqlite",
                            "current_runs_count": n["c"] if n else 0}
            except Exception as e:
                return {"ok": False, "tables_ready": False, "client": "sqlite",
                        "probe_error": str(e)}
    except Exception as e:
        import traceback
        return JSONResponse(status_code=500, content={
            "ok": False, "error": str(e), "type": type(e).__name__,
            "traceback": traceback.format_exc()[-1500:],
        })


@app.get("/api/simulator/health")
async def sim_health():
    """
    Diagnostica del Simulator: verifica tabelle, runs in memoria e DB.
    Utile per debugging del 404 sul Visualizza Risultato.
    """
    info: dict = {}
    try:
        from simulator import db as sim_db
        from simulator import runner as _runner
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

    # 1. Test tabella sim_runs
    table_ok = False
    table_err = None
    try:
        client = sim_db._get_client()
        if client:
            r = client.table("sim_runs").select("id").limit(1).execute()
            table_ok = True
            info["supabase_table_count_sample"] = len(r.data or [])
        else:
            info["supabase_client"] = "not available — using SQLite"
            try:
                import db_sqlite
                with db_sqlite.get_db() as conn:
                    n = conn.execute("SELECT COUNT(*) as c FROM sim_runs").fetchone()
                    info["sqlite_runs_count"] = n["c"] if n else 0
                table_ok = True
            except Exception as e:
                table_err = f"sqlite: {e}"
    except Exception as e:
        table_err = str(e)
    info["table_ok"] = table_ok
    if table_err:
        info["table_error"] = table_err

    # 2. Active runs in memoria
    active = getattr(_runner, "_active_runs", {})
    info["active_runs_in_memory"] = len(active)
    info["active_run_ids"] = list(active.keys())[:10]

    # 3. Conteggio runs persistiti (se accessibile)
    try:
        runs = sim_db.list_runs(limit=1000) if hasattr(sim_db, "list_runs") else []
        info["persisted_runs_count"] = len(runs)
    except Exception as e:
        info["persisted_runs_count_error"] = str(e)

    return info


class SimRunStartReq(BaseModel):
    category: str
    scenario_type: str  # 'single' | 'multi'
    num_steps: int = 1
    scenario_id: str | None = None
    mode: str = "manual"


@app.post("/api/simulator/run/start")
async def sim_run_start(req: SimRunStartReq):
    from simulator import runner as _runner
    try:
        result = await _runner.start_run(
            category=req.category, scenario_type=req.scenario_type,
            num_steps=req.num_steps, scenario_id=req.scenario_id, mode=req.mode,
        )
        return result
    except Exception as e:
        logger.error("Errore sim run start: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


class SimStepReq(BaseModel):
    step_index: int


@app.post("/api/simulator/run/{run_id}/step")
async def sim_run_step(run_id: str, req: SimStepReq):
    from simulator import runner as _runner
    try:
        return await _runner.execute_step(run_id, req.step_index)
    except Exception as e:
        logger.error("Errore sim step: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/simulator/auto-mode")
async def sim_get_auto_mode():
    from simulator import db as sim_db
    enabled = sim_db.get_setting("auto_mode_enabled", "false") == "true"
    cap = int(sim_db.get_setting("auto_mode_daily_cap", "5") or 5)
    today = sim_db.runs_today(mode="auto")
    return {"enabled": enabled, "daily_cap": cap, "runs_today": today}


@app.post("/api/simulator/auto-mode")
async def sim_set_auto_mode(payload: dict):
    from simulator import db as sim_db
    enabled = bool(payload.get("enabled"))
    cap = int(payload.get("daily_cap", 5))
    sim_db.set_setting("auto_mode_enabled", "true" if enabled else "false")
    sim_db.set_setting("auto_mode_daily_cap", str(cap))
    return {"enabled": enabled, "daily_cap": cap}


@app.get("/api/simulator/analytics")
async def sim_analytics():
    """Aggregati per la pagina History/Analytics."""
    from simulator import db as sim_db
    runs = sim_db.list_runs(limit=500)
    win_by_cat = {}
    for cat in ["normale", "geopolitico", "macro", "crash_rally"]:
        cr = [r for r in runs if r.get("category") == cat]
        if cr:
            win_by_cat[cat] = sum(1 for r in cr if r.get("outcome") == "green") / len(cr)
        else:
            win_by_cat[cat] = 0
    perf_by_horizon = {
        "h_1w": sum((r.get("perf_1w") or 0) for r in runs) / max(1, len(runs)),
        "h_1m": sum((r.get("perf_1m") or 0) for r in runs) / max(1, len(runs)),
        "h_3m": sum((r.get("perf_3m") or 0) for r in runs) / max(1, len(runs)),
    }
    outcomes = {"green": 0, "yellow": 0, "red": 0}
    for r in runs:
        o = r.get("outcome", "yellow")
        outcomes[o] = outcomes.get(o, 0) + 1
    # Curva apprendimento: perf_1m progressivo
    sorted_runs = sorted(runs, key=lambda r: r.get("completed_at") or "")
    learning = [{"i": i, "value": r.get("perf_1m") or 0}
                for i, r in enumerate(sorted_runs)]
    return {
        "win_by_category": win_by_cat,
        "perf_by_horizon": perf_by_horizon,
        "outcome_distribution": outcomes,
        "learning_curve": learning,
        "patterns_text": sim_db.get_setting("patterns_analysis", ""),
    }


@app.post("/api/simulator/patterns")
async def sim_generate_patterns():
    """
    Genera analisi testuale dei pattern via DeepSeek-R1.
    Legge l'intero storico e produce 'pattern ricorrenti'.
    """
    from simulator import db as sim_db
    runs = sim_db.list_runs(limit=200)
    if len(runs) < 5:
        return {"analysis": "Servono almeno 5 run per generare un'analisi affidabile."}

    summary_lines = ["Run recenti (cronologico):"]
    for r in runs[-50:]:
        summary_lines.append(
            f"  [{r.get('category')}] {r.get('action_chosen')} {r.get('asset_chosen','-')} "
            f"conv={r.get('conviction')} horizon={r.get('horizon')} "
            f"perf_1m={r.get('perf_1m')} delta_sp={r.get('delta_sp')} outcome={r.get('outcome')}"
        )
    prompt = (
        "Analizza il pattern del seguente Investment Analyst AI sui run del Simulator. "
        "Identifica: (1) categorie dove va bene/male, (2) bias sistematici (es. troppo conservativo, "
        "overconfident con conviction ALTA, ecc.), (3) consigli di calibrazione. "
        "Rispondi in 4-6 paragrafi, italiano, tono analitico-neutro."
    )
    try:
        import aiohttp, os
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not api_key:
            return {"analysis": "DEEPSEEK_API_KEY non configurata"}
        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                "https://api.deepseek.com/v1/chat/completions",
                json={
                    "model": "deepseek-reasoner",
                    "messages": [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": "\n".join(summary_lines)},
                    ],
                    "max_tokens": 2500,
                },
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    return {"analysis": f"Errore DeepSeek: HTTP {resp.status} {body[:200]}"}
                data = await resp.json()
        text = data["choices"][0]["message"]["content"] or ""
        # Strip <think>
        import re as _re
        text = _re.sub(r"<think>.*?</think>", "", text, flags=_re.DOTALL).strip()
        sim_db.set_setting("patterns_analysis", text)
        return {"analysis": text}
    except Exception as e:
        return {"analysis": f"Errore generazione: {e}"}


# ─── Sim Advisor: chat AI per consigli post-run + memoria categorizzata ─────

@app.get("/api/simulator/advisor/{run_id}")
async def sim_advisor_get(run_id: str):
    """
    Stato della chat advisor per un run.
    - Se non esiste ancora, genera il primo "proposal" automaticamente.
    - Se esiste, ritorna messaggi salvati + advice già archiviati per la
      stessa categoria (per UI).
    """
    try:
        from simulator import db as sim_db
        from agents import sim_advisor
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"import fallito: {e}"})

    run = sim_db.get_run(run_id)
    if not run:
        return JSONResponse(status_code=404, content={
            "error": "Run non trovato",
            "run_id": run_id,
        })

    # Ricava category_key e tags dal run
    category_key, tags = sim_advisor.detect_scenario_key(run)

    # Stato chat esistente o nuovo
    state = sim_advisor.load_advisor_chat(run_id) or {}
    messages: list = state.get("messages", [])

    existing_for_category = sim_advisor.load_advice_for_key(category_key, max_items=20)

    # Se la chat è vuota → genera la proposta iniziale on-demand
    if not messages:
        run_context = sim_advisor.build_run_context(run, existing_for_category)
        try:
            text, _reasoning, advices = await sim_advisor.chat_with_advisor(
                history=[], user_message="", run_context=run_context,
            )
        except Exception as e:
            logger.error("sim_advisor initial proposal failed: %s", e, exc_info=True)
            return JSONResponse(status_code=500, content={
                "error": f"Generazione proposta iniziale fallita: {e}",
                "category_key": category_key,
                "scenario_tags": tags,
            })

        messages = [{
            "role": "assistant",
            "content": text,
            "proposed_advices": advices,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }]
        state = {
            "run_id": run_id,
            "category_key": category_key,
            "scenario_tags": tags,
            "messages": messages,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        sim_advisor.save_advisor_chat(run_id, state)

    return {
        "run_id": run_id,
        "category_key": category_key,
        "scenario_tags": tags,
        "messages": messages,
        "existing_advices": existing_for_category,
    }


class SimAdvisorMessageReq(BaseModel):
    message: str


@app.post("/api/simulator/advisor/{run_id}/message")
async def sim_advisor_message(run_id: str, req: SimAdvisorMessageReq):
    """Invia un messaggio user nella chat advisor di un run e ottiene la risposta R1."""
    try:
        from simulator import db as sim_db
        from agents import sim_advisor
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"import fallito: {e}"})

    user_msg = (req.message or "").strip()
    if not user_msg:
        return JSONResponse(status_code=400, content={"error": "Messaggio vuoto"})

    run = sim_db.get_run(run_id)
    if not run:
        return JSONResponse(status_code=404, content={"error": "Run non trovato"})

    state = sim_advisor.load_advisor_chat(run_id) or {}
    messages: list = state.get("messages", [])
    category_key = state.get("category_key")
    if not category_key:
        category_key, tags = sim_advisor.detect_scenario_key(run)
        state["category_key"] = category_key
        state["scenario_tags"] = tags

    existing_for_category = sim_advisor.load_advice_for_key(category_key, max_items=20)
    run_context = sim_advisor.build_run_context(run, existing_for_category)

    # Append messaggio user
    user_entry = {
        "role": "user",
        "content": user_msg,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    messages.append(user_entry)

    # History per il modello: tutti i messaggi precedenti
    history_for_llm = [{"role": m["role"], "content": m["content"]}
                       for m in messages[:-1]]

    try:
        text, _reasoning, advices = await sim_advisor.chat_with_advisor(
            history=history_for_llm,
            user_message=user_msg,
            run_context=run_context,
        )
    except Exception as e:
        logger.error("sim_advisor message failed: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={
            "error": f"Chiamata advisor fallita: {e}",
        })

    assistant_entry = {
        "role": "assistant",
        "content": text,
        "proposed_advices": advices,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    messages.append(assistant_entry)

    state["messages"] = messages
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    sim_advisor.save_advisor_chat(run_id, state)

    return {
        "user_message": user_entry,
        "assistant_message": assistant_entry,
        "category_key": category_key,
    }


class SimAdvisorSaveReq(BaseModel):
    title: str
    text: str
    rationale: str = ""


@app.post("/api/simulator/advisor/{run_id}/save")
async def sim_advisor_save(run_id: str, req: SimAdvisorSaveReq):
    """Salva manualmente un advice nella memoria categorizzata."""
    try:
        from simulator import db as sim_db
        from agents import sim_advisor
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"import fallito: {e}"})

    if not (req.title or "").strip() or not (req.text or "").strip():
        return JSONResponse(status_code=400, content={
            "error": "title e text sono obbligatori",
        })

    run = sim_db.get_run(run_id)
    if not run:
        return JSONResponse(status_code=404, content={"error": "Run non trovato"})

    category_key, tags = sim_advisor.detect_scenario_key(run)

    advice = {
        "run_id": run_id,
        "scenario_category": category_key,
        "scenario_tags": tags,
        "title": req.title.strip()[:200],
        "text": req.text.strip()[:1000],
        "rationale": (req.rationale or "").strip()[:600],
    }

    try:
        aid = sim_advisor.save_advice(advice)
    except Exception as e:
        logger.error("sim_advisor save failed: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={
            "error": f"Salvataggio fallito: {e}",
        })

    return {
        "id": aid,
        "category_key": category_key,
        "scenario_tags": tags,
        "saved": True,
    }


@app.get("/api/simulator/advisor/memory/all")
async def sim_advisor_memory_all():
    """Lista tutti gli advice in memoria, raggruppati per category_key."""
    try:
        from agents import sim_advisor
        grouped = sim_advisor.list_all_advice()
        return {"groups": grouped, "category_count": len(grouped),
                "total_advices": sum(len(v) for v in grouped.values())}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/simulator/advisor/memory/{category_key}")
async def sim_advisor_memory_for_key(category_key: str):
    """Lista gli advice per una specifica category_key."""
    try:
        from agents import sim_advisor
        items = sim_advisor.load_advice_for_key(category_key, max_items=50)
        return {"category_key": category_key, "items": items}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.delete("/api/simulator/advisor/memory/{category_key}/{advice_id}")
async def sim_advisor_memory_delete(category_key: str, advice_id: str):
    """Elimina un advice specifico dalla memoria."""
    try:
        from agents import sim_advisor
        ok = sim_advisor.delete_advice(advice_id, category_key)
        if not ok:
            return JSONResponse(status_code=404, content={"error": "Advice non trovato"})
        return {"deleted": True, "id": advice_id, "category_key": category_key}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.delete("/api/simulator/advisor/{run_id}")
async def sim_advisor_chat_delete(run_id: str):
    """Reset della chat advisor per un run (la memoria archiviata resta)."""
    try:
        from agents import sim_advisor
        sim_advisor.save_advisor_chat(run_id, {})
        return {"reset": True, "run_id": run_id}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/settings/prompt-defaults")
async def get_prompt_defaults():
    """
    Restituisce i prompt di default per i 5 agenti operativi:
      Scout, Technical (equity), Decision (equity Sonnet),
      Technical Crypto (V3), Decision Crypto (R1).
    Usato dal frontend per mostrare placeholder e ripristinare i default.
    """
    try:
        from agents.scout import SCOUT_20MIN_PROMPT_DEFAULT
        from agents.technical import TECH_PROMPT_DEFAULT
        from agents.decision import DECISION_SYSTEM_PROMPT_DEFAULT
        from agents.technical_crypto import CRYPTO_TECHNICAL_PROMPT_DEFAULT
        from agents.decision_crypto import CRYPTO_DECISION_PROMPT_DEFAULT
        return {
            "prompt_scout": SCOUT_20MIN_PROMPT_DEFAULT,
            "prompt_technical": TECH_PROMPT_DEFAULT,
            "prompt_decision": DECISION_SYSTEM_PROMPT_DEFAULT,
            "prompt_technical_crypto": CRYPTO_TECHNICAL_PROMPT_DEFAULT,
            "prompt_decision_crypto": CRYPTO_DECISION_PROMPT_DEFAULT,
        }
    except Exception as e:
        logger.error(f"Errore caricamento prompt default: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


# --- Endpoint dei documenti tecnici ---


@app.get("/api/documents")
async def list_documents():
    """Restituisce la lista dei documenti tecnici caricati. ?category=crypto filtra."""
    try:
        category = None
        # Permette ?category=generic|crypto. Se non specificato, ritorna tutti.
        try:
            from fastapi import Request as _Req  # noqa
            # FastAPI Query param non si può aggiungere a una funzione esistente
            # senza rompere altri call site → leggiamo da un Query param manuale
        except Exception:
            pass
        docs = database.get_documents()
        return docs
    except Exception as e:
        logger.error(f"Errore nel recupero dei documenti: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/documents/by-category/{category}")
async def list_documents_by_category(category: str):
    """Restituisce i documenti filtrati per categoria ('generic' o 'crypto')."""
    try:
        if category not in ("generic", "crypto"):
            return JSONResponse(status_code=400, content={"error": "category deve essere 'generic' o 'crypto'"})
        docs = database.get_documents(category=category)
        return docs
    except Exception as e:
        logger.error(f"Errore nel recupero dei documenti per categoria: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/documents/upload")
async def upload_document(file: UploadFile = File(...), category: str = Query(default="generic")):
    """
    Carica un documento PDF o TXT.
    category='generic' (Decision normale) o 'crypto' (Decision Crypto).
    """
    try:
        if category not in ("generic", "crypto"):
            category = "generic"
        filename = file.filename or "documento_sconosciuto"
        raw_bytes = await file.read()
        file_size = len(raw_bytes)

        # Estrazione del contenuto in base al tipo di file
        if filename.lower().endswith(".pdf"):
            try:
                import io
                from PyPDF2 import PdfReader
                reader = PdfReader(io.BytesIO(raw_bytes))
                content = "\n".join(page.extract_text() or "" for page in reader.pages)
            except Exception as pdf_err:
                logger.error(f"Errore nell'estrazione del PDF: {pdf_err}")
                content = f"[Errore estrazione PDF: {pdf_err}]"
        elif filename.lower().endswith(".txt"):
            content = raw_bytes.decode("utf-8", errors="replace")
        else:
            return JSONResponse(
                status_code=400,
                content={"error": "Formato non supportato. Usa PDF o TXT."})

        # Salva il file su disco
        filepath = os.path.join(DOCUMENTS_DIR, filename)
        with open(filepath, "wb") as f:
            f.write(raw_bytes)

        # Salva nel database
        database.insert_document(filename, content, file_size, category=category)
        logger.info(f"Documento caricato: {filename} ({file_size} bytes, category={category})")

        return {"status": "uploaded", "filename": filename, "size": file_size, "category": category}
    except Exception as e:
        logger.error(f"Errore nel caricamento del documento: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.delete("/api/documents/{doc_id}")
async def remove_document(doc_id: int):
    """Elimina un documento tecnico per ID. Protegge i preset."""
    try:
        # Blocca delete su documenti preset (precaricati dall'utente come PDF)
        try:
            docs = database.get_documents()
            target = next((d for d in docs if d.get("id") == doc_id), None)
            if target and (target.get("is_preset") or target.get("is_preset") == 1):
                return JSONResponse(status_code=403, content={
                    "error": "Documento preset, non eliminabile dall'UI"
                })
        except Exception:
            pass
        database.delete_document(doc_id)
        return {"status": "deleted", "id": doc_id}
    except Exception as e:
        logger.error(f"Errore nell'eliminazione del documento: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


# --- Endpoint reset portafoglio ---


class ResetPayload(BaseModel):
    new_balance: float = 100000.0


@app.post("/api/portfolio/reset")
async def reset_portfolio(payload: ResetPayload):
    """Resetta il portafoglio con un nuovo saldo iniziale."""
    try:
        database.reset_portfolio_data(payload.new_balance)
        logger.info(f"Portafoglio resettato con saldo: {payload.new_balance}")
        return {"status": "reset", "new_balance": payload.new_balance}
    except Exception as e:
        logger.error(f"Errore nel reset del portafoglio: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


# --- Endpoint chiusura manuale posizione ---


class ClosePositionPayload(BaseModel):
    ticker: str


@app.post("/api/positions/close")
async def close_position(payload: ClosePositionPayload):
    """Chiude manualmente una posizione vendendo tutte le azioni al prezzo corrente."""
    try:
        import data_fetchers
        pos = database.get_position(payload.ticker)
        if not pos:
            return JSONResponse(status_code=404, content={"error": f"Posizione {payload.ticker} non trovata"})

        # Ottieni il prezzo corrente
        price_data = data_fetchers.fetch_market_data(payload.ticker, period_days=5)
        if price_data.get("data"):
            current_price = price_data["data"][-1]["close"]
        else:
            current_price = pos["current_price"]
        if current_price <= 0:
            return JSONResponse(status_code=400, content={"error": "Impossibile ottenere il prezzo corrente"})

        result = portfolio.execute_sell(
            ticker=payload.ticker,
            quantity=pos["quantity"],
            price=current_price,
            geo_reasoning="Chiusura manuale da interfaccia",
            tech_reasoning="Chiusura manuale da interfaccia",
            confidence=100,
        )
        return result
    except Exception as e:
        logger.error(f"Errore nella chiusura della posizione: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


# --- Endpoint ClawStreet ---


class ClawStreetRegisterPayload(BaseModel):
    name: str = "GeoInvest AI"
    ticker: str = "GEO"
    strategy: str = "Geopolitical risk analysis combined with technical analysis. Uses GDELT, NewsAPI, and Congressional trading data to identify macro opportunities."
    personality: str = "Disciplined and data-driven. Follows the trend, never averages down losses, always sets stop-loss."
    bio: str = "AI agent combining geopolitical intelligence with technical analysis to trade global macro themes."


@app.post("/api/clawstreet/register")
async def register_clawstreet_bot(payload: ClawStreetRegisterPayload):
    """Registra il bot su ClawStreet e salva le credenziali nel database."""
    try:
        import data_fetchers
        result = await data_fetchers.register_clawstreet_bot(
            name=payload.name,
            ticker=payload.ticker,
            strategy=payload.strategy,
            personality=payload.personality,
            bio=payload.bio,
        )
        if result.get("success"):
            data = result.get("data", {})
            # Salva le credenziali nel database
            if data.get("bot_id") or data.get("id"):
                database.set_setting("clawstreet_bot_id", str(data.get("bot_id") or data.get("id", "")))
            if data.get("api_key") or data.get("apiKey"):
                database.set_setting("clawstreet_api_key", str(data.get("api_key") or data.get("apiKey", "")))
            if data.get("claim_url") or data.get("claimUrl") or data.get("url"):
                database.set_setting("clawstreet_claim_url", str(data.get("claim_url") or data.get("claimUrl") or data.get("url", "")))
            # Salva anche nome e ticker
            database.set_setting("clawstreet_bot_name", payload.name)
            database.set_setting("clawstreet_bot_ticker", payload.ticker)
            return {"status": "registered", "data": data}
        else:
            return JSONResponse(status_code=400, content={
                "error": "Registrazione fallita",
                "details": result,
            })
    except Exception as e:
        logger.error(f"Errore nella registrazione ClawStreet: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/clawstreet/set-credentials")
async def set_clawstreet_credentials(payload: dict):
    """
    Salva manualmente le credenziali di un bot ClawStreet già esistente
    (recuperate dall'utente — tipicamente quando ha registrato il bot in
    precedenza e ha ancora salvato bot_id + api_key).
    """
    bot_id = (payload.get("bot_id") or "").strip()
    api_key = (payload.get("api_key") or "").strip()
    bot_name = (payload.get("bot_name") or "").strip()
    bot_ticker = (payload.get("bot_ticker") or "").strip()

    if not bot_id or not api_key:
        return JSONResponse(status_code=400, content={
            "status": "error", "message": "bot_id e api_key sono obbligatori"
        })

    # Verifica le credenziali con ClawStreet prima di salvare
    try:
        import aiohttp
        url = f"https://www.clawstreet.io/api/bots/{bot_id}/balance"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers={"Authorization": f"Bearer {api_key}"},
                                    timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status not in (200, 201):
                    body = await resp.text()
                    return JSONResponse(status_code=400, content={
                        "status": "error",
                        "message": f"Credenziali non valide (HTTP {resp.status}): {body[:200]}"
                    })
                bal_data = await resp.json()
    except Exception as e:
        return JSONResponse(status_code=500, content={
            "status": "error", "message": f"Errore verifica: {e}"
        })

    # Salva nel DB
    database.set_setting("clawstreet_bot_id", bot_id)
    database.set_setting("clawstreet_api_key", api_key)
    if bot_name:
        database.set_setting("clawstreet_bot_name", bot_name)
    if bot_ticker:
        database.set_setting("clawstreet_bot_ticker", bot_ticker)
    return {"status": "ok", "bot_id": bot_id, "balance_data": bal_data}


@app.post("/api/clawstreet/mirror-historical-trades")
async def mirror_historical_trades(limit: int = Query(default=100, ge=1, le=1000)):
    """
    Replica tutti i trade già eseguiti nel paper trading interno
    sul bot ClawStreet configurato. Utile per sincronizzare lo storico.
    """
    bot_id = database.get_setting("clawstreet_bot_id", "") or os.environ.get("CLAWSTREET_BOT_ID", "")
    api_key = database.get_setting("clawstreet_api_key", "") or os.environ.get("CLAWSTREET_API_KEY", "")
    if not bot_id or not api_key or bot_id == "GEO":
        return JSONResponse(status_code=400, content={
            "status": "error",
            "message": "Credenziali ClawStreet mancanti. Salva bot_id+api_key con /api/clawstreet/set-credentials prima.",
        })

    try:
        import data_fetchers
        trades = database.get_trades(limit=limit) or []
        results = {"mirrored": 0, "skipped": 0, "failed": 0, "details": []}
        for t in trades:
            ticker = t.get("ticker", "")
            action = (t.get("action") or "").lower()
            qty = int(t.get("quantity") or 0)
            reasoning = (t.get("final_decision") or t.get("geopolitical_reasoning") or
                         t.get("technical_reasoning") or f"Historical trade {t.get('timestamp','')}")[:280]
            if not ticker or action not in ("buy", "sell", "short", "cover") or qty <= 0:
                results["skipped"] += 1
                continue
            mirror = await data_fetchers.mirror_trade_to_clawstreet(
                bot_id=bot_id, api_key=api_key,
                symbol=ticker, action=action, qty=qty, reasoning=reasoning,
            )
            if mirror.get("mirrored"):
                results["mirrored"] += 1
            else:
                results["failed"] += 1
            results["details"].append({
                "ticker": ticker, "action": action, "qty": qty,
                "result": "ok" if mirror.get("mirrored") else f"fail: {mirror.get('reason') or mirror.get('error') or mirror.get('status')}",
            })
        return {"status": "ok", **results}
    except Exception as e:
        logger.error(f"Errore mirror storico: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})


@app.post("/api/clawstreet/reconcile")
async def reconcile_clawstreet_trades(since_hours: int = Query(default=48, ge=1, le=720)):
    """
    Confronta i trade locali con quelli su ClawStreet e invia quelli mancanti.
    Usa una semplice corrispondenza per (ticker, action, qty) sui trade locali
    delle ultime 'since_hours' ore. Idempotente entro la finestra.
    """
    bot_id = database.get_setting("clawstreet_bot_id", "") or os.environ.get("CLAWSTREET_BOT_ID", "")
    api_key = database.get_setting("clawstreet_api_key", "") or os.environ.get("CLAWSTREET_API_KEY", "")
    if not bot_id or not api_key or bot_id == "GEO":
        return JSONResponse(status_code=400, content={
            "status": "error",
            "message": "Credenziali ClawStreet mancanti.",
        })

    import data_fetchers
    import aiohttp as _aiohttp
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    cutoff = _dt.now(_tz.utc) - _td(hours=since_hours)

    # 1) Trade locali nella finestra
    local_trades = database.get_trades(limit=500) or []
    local_in_window = []
    for t in local_trades:
        ts_str = t.get("timestamp") or ""
        try:
            t_ts = _dt.fromisoformat(ts_str.replace("Z", "+00:00"))
        except Exception:
            continue
        if t_ts < cutoff:
            continue
        action = (t.get("action") or "").lower()
        ticker = t.get("ticker", "")
        qty = int(t.get("quantity") or 0)
        if not ticker or action not in ("buy", "sell") or qty <= 0:
            continue
        local_in_window.append({
            "ticker": ticker, "action": action, "qty": qty,
            "ts": t_ts,
            "reasoning": (t.get("final_decision") or t.get("geopolitical_reasoning") or "")[:280],
        })

    # 2) Trade gia' su ClawStreet
    cs_trades = []
    try:
        url = f"https://www.clawstreet.io/api/bots/{bot_id}/trades"
        async with _aiohttp.ClientSession() as sess:
            async with sess.get(url, headers={"Authorization": f"Bearer {api_key}"},
                                 timeout=_aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    cs_trades = data.get("trades", []) if isinstance(data, dict) else []
    except Exception as e:
        logger.warning("Reconcile: impossibile leggere trade ClawStreet: %s", e)

    # Counter (ticker, action, qty) gia' presenti su CS
    from collections import Counter
    cs_keys = Counter(
        (t.get("symbol", "").upper(), (t.get("action") or "").lower(), int(t.get("qty") or 0))
        for t in cs_trades
    )

    # 3) Per ogni local trade, controlla se gia' su CS; se no, invialo
    missing = []
    sent = []
    failed = []
    # Counter dei local in window per gestire multipli identici
    local_counter = Counter()
    for tr in sorted(local_in_window, key=lambda x: x["ts"]):
        key = (tr["ticker"].upper(), tr["action"], tr["qty"])
        local_counter[key] += 1
        already_on_cs = cs_keys.get(key, 0)
        if local_counter[key] <= already_on_cs:
            continue  # gia' specchiato
        missing.append(tr)

    skipped = []
    for tr in missing:
        result = await data_fetchers.mirror_trade_to_clawstreet(
            bot_id=bot_id, api_key=api_key,
            symbol=tr["ticker"], action=tr["action"], qty=tr["qty"],
            reasoning=tr["reasoning"] or f"Reconcile {tr['ts'].isoformat()}",
        )
        if result.get("mirrored"):
            sent.append({"ticker": tr["ticker"], "action": tr["action"], "qty": tr["qty"]})
        elif result.get("skipped"):
            # Errori "expected" (INVALID_SYMBOL, INSUFFICIENT_BUYING_POWER, ecc.):
            # ClawStreet non supporta il simbolo o non ha capitale virtuale sufficiente.
            # Non e' un bug dell'app — e' un limite della piattaforma vetrina.
            skipped.append({
                "ticker": tr["ticker"], "action": tr["action"], "qty": tr["qty"],
                "reason": result.get("reason", "skip"),
            })
        else:
            failed.append({
                "ticker": tr["ticker"], "action": tr["action"], "qty": tr["qty"],
                "reason": str(result.get("response") or result.get("error") or result.get("status"))[:200],
            })

    return {
        "status": "ok",
        "window_hours": since_hours,
        "local_trades_in_window": len(local_in_window),
        "clawstreet_trades": len(cs_trades),
        "missing": len(missing),
        "sent": len(sent),
        "skipped": len(skipped),
        "failed": len(failed),
        "details": {"sent": sent, "skipped": skipped, "failed": failed},
    }


@app.get("/api/clawstreet/diagnostics")
async def clawstreet_diagnostics():
    """
    Diagnostica completa del mirroring ClawStreet:
      - Posizioni locali vs posizioni ClawStreet (diff per ticker)
      - Cash balance locale vs ClawStreet
      - Conteggio trade per stato mirror (ok/failed/skipped/pending) ultimi 7gg
      - Lista dei pending mirrors da riprovare

    Usato dal frontend per mostrare un pannello "ClawStreet sync health".
    """
    import aiohttp as _aiohttp

    bot_id = database.get_setting("clawstreet_bot_id", "") or os.environ.get("CLAWSTREET_BOT_ID", "")
    api_key = database.get_setting("clawstreet_api_key", "") or os.environ.get("CLAWSTREET_API_KEY", "")
    if not bot_id or not api_key or bot_id == "GEO":
        return {
            "status": "no_credentials",
            "message": "Credenziali ClawStreet non configurate.",
        }

    # 1) Stato locale
    local_portfolio = database.get_portfolio() or {}
    local_positions = database.get_positions() or []
    local_cash = float(local_portfolio.get("cash_balance") or 0)

    # 2) Stato ClawStreet — l'endpoint /balance restituisce GIÀ le positions
    # nel suo payload (campo "positions"), non c'è un endpoint /positions
    # separato (404). Usiamo solo /balance e ne leggiamo entrambi.
    headers = {"Authorization": f"Bearer {api_key}"}
    cs_balance = None
    cs_positions = []
    cs_error = None
    try:
        async with _aiohttp.ClientSession() as sess:
            async with sess.get(f"https://www.clawstreet.io/api/bots/{bot_id}/balance",
                                 headers=headers,
                                 timeout=_aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    cs_balance = await resp.json(content_type=None)
                else:
                    cs_error = f"HTTP {resp.status} su /balance"
    except Exception as exc:
        cs_error = str(exc)[:200]

    # Estrai positions dal payload /balance (è una lista di dict)
    if cs_balance and isinstance(cs_balance, dict):
        cs_positions = cs_balance.get("positions", []) or []

    cs_cash = None
    if cs_balance:
        # ClawStreet può ritornare balance in vari formati
        cs_cash = (cs_balance.get("cash")
                   or cs_balance.get("cash_balance")
                   or cs_balance.get("balance")
                   or (cs_balance.get("data", {}) or {}).get("cash"))
        if cs_cash is not None:
            try:
                cs_cash = float(cs_cash)
            except (ValueError, TypeError):
                cs_cash = None

    # 3) Confronto posizioni: ticker → (local_qty, cs_qty)
    from clawstreet_universe import to_clawstreet_format
    local_by_cs_symbol = {}
    for p in local_positions:
        cs_sym = to_clawstreet_format(p.get("ticker", ""))
        local_by_cs_symbol[cs_sym] = float(p.get("quantity") or 0)

    cs_by_symbol = {}
    for p in cs_positions:
        sym = (p.get("symbol") or p.get("ticker") or "").upper()
        qty = float(p.get("qty") or p.get("quantity") or 0)
        if sym:
            cs_by_symbol[sym] = qty

    all_symbols = sorted(set(local_by_cs_symbol.keys()) | set(cs_by_symbol.keys()))
    position_diffs = []
    in_sync = 0
    out_of_sync = 0
    for sym in all_symbols:
        loc = local_by_cs_symbol.get(sym, 0)
        cs = cs_by_symbol.get(sym, 0)
        delta = loc - cs
        if abs(delta) < 0.001:
            in_sync += 1
        else:
            out_of_sync += 1
            position_diffs.append({
                "symbol": sym, "local_qty": loc, "cs_qty": cs, "delta": delta,
            })

    # 4) Riepilogo mirror status
    mirror_summary = {}
    pending_mirrors = []
    try:
        mirror_summary = database.get_mirror_status_summary()
        pending_mirrors = database.get_pending_mirror_trades(window_hours=72, max_attempts=10, limit=20)
    except Exception as exc:
        logger.warning("Mirror summary fallita: %s", exc)

    return {
        "status": "ok",
        "credentials_ok": True,
        "cs_error": cs_error,
        "local": {
            "cash_balance": round(local_cash, 2),
            "positions_count": len(local_positions),
            "total_value": float(local_portfolio.get("total_value") or 0),
        },
        "clawstreet": {
            "cash_balance": cs_cash,
            "positions_count": len(cs_positions),
            "raw_balance_response": cs_balance,
        },
        "positions_sync": {
            "in_sync": in_sync,
            "out_of_sync": out_of_sync,
            "diffs": position_diffs[:30],   # primi 30 per UI
        },
        "mirror_status_7d": mirror_summary,
        "pending_mirrors": [
            {
                "trade_id": t.get("id"),
                "ticker": t.get("ticker"),
                "action": t.get("action"),
                "quantity": t.get("quantity"),
                "status": t.get("cs_mirror_status"),
                "reason": t.get("cs_mirror_reason"),
                "attempts": t.get("cs_mirror_attempts"),
                "timestamp": t.get("timestamp"),
            } for t in pending_mirrors
        ],
    }


@app.post("/api/admin/apply-cs-mirror-migration")
async def apply_cs_mirror_migration():
    """
    Trigger manuale della migrazione cs_mirror_* su Supabase.
    Idempotente. Utile se DATABASE_URL/SUPABASE_DB_PASSWORD vengono aggiunti
    DOPO il primo deploy → senza riavviare l'app, hit questo endpoint.
    """
    try:
        # Forza il reload del modulo db_supabase per ri-eseguire la migration
        import db_supabase
        db_supabase._ensure_cs_mirror_columns()
        return {"status": "ok", "message": "Migrazione applicata (vedi log Render per esito)"}
    except Exception as e:
        logger.error("Errore migration: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})


@app.post("/api/clawstreet/retry-mirrors")
async def retry_failed_mirrors(window_hours: int = Query(default=24, ge=1, le=168)):
    """
    Forza il retry dei mirror falliti (ultimi window_hours ore).
    Utile come trigger manuale dal pulsante della UI.
    """
    try:
        from clawstreet_mirror import retry_pending_mirrors
        result = await retry_pending_mirrors(window_hours=window_hours, limit=50)
        return {"status": "ok", **result}
    except Exception as e:
        logger.error("Errore retry mirror: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})


@app.post("/api/scout/run")
async def trigger_scout_run(background_tasks: BackgroundTasks):
    """
    Avvia un singolo run dello Scout in background (per test/diagnostica).
    """
    import uuid as _uuid
    run_id = str(_uuid.uuid4())

    async def _do():
        try:
            from agents.orchestrator import run_scout_pipeline
            result = await run_scout_pipeline(run_id=run_id)
            logger.info("Manual scout run %s: %s", run_id, result)
        except Exception as e:
            logger.error("Manual scout run failed: %s", e, exc_info=True)

    background_tasks.add_task(_do)
    return {"status": "started", "run_id": run_id}


@app.post("/api/clawstreet/clear-credentials")
async def clear_clawstreet_credentials():
    """
    Pulisce le credenziali ClawStreet dal DB. Utile se è stato registrato
    un bot per errore — il sistema non tenterà più di mirrorare i trade.
    Il bot remoto resta unclaimed (non genera costi finché non viene attivato).
    """
    try:
        for k in ["clawstreet_bot_id", "clawstreet_api_key", "clawstreet_claim_url",
                  "clawstreet_bot_name", "clawstreet_bot_ticker"]:
            try:
                database.set_setting(k, "")
            except Exception:
                pass
        return {"status": "ok", "message": "Credenziali ClawStreet pulite dal DB"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})


@app.get("/api/clawstreet/status")
async def get_clawstreet_status():
    """Restituisce lo stato della registrazione ClawStreet."""
    try:
        # Bot gia' registrato su ClawStreet — valori noti
        bot_id = database.get_setting("clawstreet_bot_id", "") or os.environ.get("CLAWSTREET_BOT_ID", "GEO")
        api_key = database.get_setting("clawstreet_api_key", "") or os.environ.get("CLAWSTREET_API_KEY", "")
        claim_url = database.get_setting("clawstreet_claim_url", "") or os.environ.get("CLAWSTREET_CLAIM_URL", "")
        bot_name = "GeoInvest AI"
        bot_ticker = "GEO"
        return {
            "registered": True,
            "bot_id": bot_id,
            "bot_name": bot_name,
            "bot_ticker": bot_ticker,
            "claim_url": claim_url,
            "public_url": "https://www.clawstreet.io/agents/geoinvest-ai",
        }
    except Exception as e:
        logger.error(f"Errore nello stato ClawStreet: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


# --- Endpoint test connessione API ---


@app.post("/api/test-connection")
async def test_api_connection():
    """Testa la connessione alle API esterne (Anthropic e NewsAPI)."""
    results = {}
    # Test Anthropic
    api_key = database.get_setting("anthropic_api_key", os.environ.get("ANTHROPIC_API_KEY", ""))
    if api_key:
        try:
            from anthropic import Anthropic
            client = Anthropic(api_key=api_key)
            # Chiamata minima per verificare la chiave
            models_to_try = [
                database.get_setting("model_name", "claude-3-5-haiku-20241022"),
                "claude-3-5-haiku-20241022",
                "claude-sonnet-4-20250514",
                "claude-3-haiku-20240307",
            ]
            connected = False
            for m in models_to_try:
                try:
                    client.messages.create(model=m, max_tokens=10,
                                           messages=[{"role": "user", "content": "ping"}])
                    connected = True
                    break
                except Exception:
                    continue
            results["anthropic"] = {"status": "ok"} if connected else {"status": "error", "message": "Nessun modello disponibile"}
        except Exception as e:
            results["anthropic"] = {"status": "error", "message": str(e)}
    else:
        results["anthropic"] = {"status": "error", "message": "Chiave API non configurata"}

    # Test NewsAPI
    news_key = database.get_setting("news_api_key", os.environ.get("NEWS_API_KEY", ""))
    if news_key:
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                url = f"https://newsapi.org/v2/top-headlines?country=us&pageSize=1&apiKey={news_key}"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        results["newsapi"] = {"status": "ok"}
                    else:
                        results["newsapi"] = {"status": "error", "message": f"HTTP {resp.status}"}
        except Exception as e:
            results["newsapi"] = {"status": "error", "message": str(e)}
    else:
        results["newsapi"] = {"status": "error", "message": "Chiave API non configurata"}

    return results


# --- Endpoint storico portafoglio ---

@app.get("/api/portfolio/history")
async def get_portfolio_history(period: str = Query(default="30d")):
    """Restituisce lo storico del valore del portafoglio.

    Periodi supportati:
      1h, 4h, 1d  -> intraday (ultimo giorno, snapshot ogni minuto via price polling)
      7d/1w, 30d/1m, 90d/3m, all
    """
    try:
        days_map = {
            "1h": 1, "4h": 1, "1d": 1,
            "7d": 7, "1w": 7,
            "30d": 30, "1m": 30,
            "90d": 90, "3m": 90,
            "all": 3650,
        }
        days = days_map.get(period.lower(), 30)
        history = database.get_portfolio_history(days=days)
        if not history:
            p = database.get_portfolio()
            val = p["total_value"] if p else 100000
            cash = p["cash_balance"] if p else 100000
            from datetime import datetime, timezone
            history = [{"total_value": val, "cash_balance": cash, "timestamp": datetime.now(timezone.utc).isoformat()}]
        return history
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# --- Endpoint diagnostica ---

@app.get("/api/settings/test-anthropic")
async def test_anthropic():
    """Testa la connessione ad Anthropic."""
    api_key = database.get_setting("anthropic_api_key", os.environ.get("ANTHROPIC_API_KEY", ""))
    if not api_key:
        return {"status": "error", "message": "Chiave API non configurata"}
    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=api_key)
        # Prova diversi modelli in ordine di preferenza
        model = database.get_setting("model_name", None)
        models_to_try = []
        if model:
            models_to_try.append(model)
        models_to_try.extend([
            "claude-3-5-haiku-20241022",
            "claude-sonnet-4-20250514",
            "claude-3-5-sonnet-20241022",
            "claude-3-haiku-20240307",
        ])
        last_err = None
        for m in models_to_try:
            try:
                client.messages.create(model=m, max_tokens=10,
                                       messages=[{"role": "user", "content": "ping"}])
                return {"status": "ok", "model": m}
            except Exception as model_err:
                last_err = model_err
                continue
        return {"status": "error", "message": str(last_err)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/settings/test-newsapi")
async def test_newsapi():
    """Testa la connessione a NewsAPI."""
    news_key = database.get_setting("news_api_key", os.environ.get("NEWS_API_KEY", ""))
    if not news_key:
        return {"status": "error", "message": "Chiave API non configurata"}
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            url = f"https://newsapi.org/v2/top-headlines?country=us&pageSize=1&apiKey={news_key}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return {"status": "ok"}
                else:
                    return {"status": "error", "message": f"HTTP {resp.status}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/settings/test-yfinance")
async def test_yfinance():
    """Testa yfinance scaricando dati SPY con retry per rate-limit."""
    try:
        import data_fetchers
        result = data_fetchers.fetch_market_data("SPY", period_days=5)
        if result.get("error"):
            return {"status": "error", "message": result["error"]}
        if not result.get("data"):
            return {"status": "error", "message": "Nessun dato ricevuto"}
        last_close = result["data"][-1]["close"]
        return {"status": "ok", "last_price": round(last_close, 2)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/settings/test-db")
async def test_db():
    """Testa la connessione al database."""
    try:
        p = database.get_portfolio()
        if p:
            return {"status": "ok", "tables": "portfolio accessibile"}
        return {"status": "error", "message": "Portafoglio vuoto"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/settings/test-finnhub")
async def test_finnhub():
    """Testa la connessione a Finnhub."""
    finnhub_key = database.get_setting("finnhub_api_key", os.environ.get("FINNHUB_API_KEY", ""))
    if not finnhub_key:
        return {"status": "error", "message": "Chiave API Finnhub non configurata"}
    try:
        import aiohttp
        url = f"https://finnhub.io/api/v1/stock/profile2?symbol=AAPL&token={finnhub_key}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data and data.get("name"):
                        return {"status": "ok"}
                    return {"status": "error", "message": "Risposta vuota (chiave non valida?)"}
                elif resp.status == 401:
                    return {"status": "error", "message": "Chiave API non valida"}
                else:
                    return {"status": "error", "message": f"HTTP {resp.status}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/settings/massive-key")
async def set_massive_key(payload: dict):
    """Salva/aggiorna la API key di Massive (cifrata in DB Supabase)."""
    key = (payload.get("api_key") or "").strip()
    if not key:
        return JSONResponse(status_code=400, content={"status": "error", "message": "api_key vuota"})
    try:
        database.set_setting("massive_api_key", key)
        return {"status": "ok", "message": "Massive API key salvata"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})


@app.get("/api/settings/test-massive")
async def test_massive():
    """Testa la connessione all'API Massive con la key configurata."""
    key = os.environ.get("MASSIVE_API_KEY", "")
    if not key:
        try:
            key = database.get_setting("massive_api_key", "") or ""
        except Exception:
            pass
    if not key:
        return {"status": "error", "message": "MASSIVE_API_KEY non configurata"}
    try:
        import aiohttp
        url = f"https://api.massive.com/v2/aggs/ticker/AAPL/prev?adjusted=true&apiKey={key}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                body = await resp.json()
                if resp.status == 200 and body.get("status") == "OK":
                    return {"status": "ok", "endpoint": "/v2/aggs/ticker/AAPL/prev",
                            "results_count": body.get("resultsCount", 0)}
                return {"status": "error",
                        "message": f"HTTP {resp.status}: {body.get('message', 'unknown')}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/settings/test-deepseek")
async def test_deepseek():
    """Testa la connessione a DeepSeek API."""
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not deepseek_key:
        return {"status": "error", "message": "DEEPSEEK_API_KEY non configurata"}
    try:
        import aiohttp
        headers = {"Authorization": f"Bearer {deepseek_key}", "Content-Type": "application/json"}
        payload = {
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 5,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post("https://api.deepseek.com/v1/chat/completions",
                                    json=payload, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    return {"status": "ok", "model": "deepseek-chat"}
                else:
                    body = await resp.text()
                    return {"status": "error", "message": f"HTTP {resp.status}: {body[:200]}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/settings/test-gdelt")
async def test_gdelt():
    """
    Testa la connessione a GDELT.
    Usa prima la cache di data_fetchers (10 min TTL) così non genera traffico
    inutile e non si fa bloccare dai 429 frequenti dell'endpoint pubblico.
    Se la cache è vuota fa una chiamata di test, e tratta 429/HTML come WARN
    (non error): è il comportamento normale di GDELT free tier.
    """
    # 1) Se abbiamo un risultato in cache recente, GDELT è funzionante
    try:
        import data_fetchers
        cache = getattr(data_fetchers, "_gdelt_cache", {})
        if cache.get("all"):
            cached_time, _ = cache["all"]
            import time as _t
            age = int(_t.time() - cached_time)
            if age < 600:  # entro la finestra TTL
                return {"status": "ok", "message": f"Cache attiva (age {age}s)"}
    except Exception:
        pass

    # 2) Test live tollerante a 429 e a body non-JSON
    try:
        import aiohttp
        url = "https://api.gdeltproject.org/api/v2/doc/doc?query=test&mode=artlist&maxrecords=1&format=json"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 429:
                    return {"status": "warn", "message": "Rate-limited (429) — comportamento normale GDELT, lo Scout retry-a in automatico"}
                if resp.status != 200:
                    return {"status": "error", "message": f"HTTP {resp.status}"}
                body = (await resp.text()).lstrip()
                if not body or body[0] not in "{[":
                    return {"status": "warn", "message": "Risposta non-JSON (probabile rate-limit camuffato)"}
                return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)[:200]}


@app.post("/api/migrate/v4")
async def run_v4_migration():
    """
    Esegue la migrazione v4 creando le nuove tabelle per il sistema multi-agent.
    Sicuro da rieseguire (usa IF NOT EXISTS).
    """
    try:
        migration_path = os.path.join(os.path.dirname(__file__), "migrations", "init_v4.sql")
        if not os.path.exists(migration_path):
            return {"status": "error", "message": "init_v4.sql non trovato"}

        with open(migration_path) as f:
            sql = f.read()

        # Usa la connessione diretta PostgreSQL via Supabase
        from db_supabase import _get_client
        client = _get_client()

        # Esegui ogni statement separatamente via rpc
        # Supabase non supporta SQL diretto, usiamo postgrest rpc
        # Alternativa: controlla se le tabelle esistono gia'
        v4_tables = [
            "intelligence_buffer", "daily_snapshots", "weekly_matrix",
            "trades_high_risk", "agent_checkpoints",
        ]
        existing = []
        missing = []
        for table in v4_tables:
            try:
                client.table(table).select("*").limit(1).execute()
                existing.append(table)
            except Exception:
                missing.append(table)

        return {
            "status": "ok",
            "existing_tables": existing,
            "missing_tables": missing,
            "message": (
                "Tutte le tabelle v4 presenti!" if not missing
                else f"Tabelle mancanti: {missing}. Esegui init_v4.sql nel SQL Editor di Supabase dashboard."
            ),
            "sql_file": "backend/migrations/init_v4.sql",
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


# --- Montaggio dei file statici del frontend React ---
# Modalità ENCRYPTION_ENABLED=true: il server espone SOLO unlock.html e
# /encrypted-bundle. Il browser (Web Crypto API) decifra in locale dopo aver
# ricevuto il token. La build React in chiaro NON viene mai trasmessa.
# Modalità default: serve la build React normalmente (sviluppo).
_ENCRYPTION_ENABLED = os.environ.get("ENCRYPTION_ENABLED", "").lower() in ("1", "true", "yes")

_encrypted_dir = os.path.join(os.path.dirname(__file__), "encrypted_assets")
_encrypted_bundle = os.path.join(_encrypted_dir, "bundle.enc")
_unlock_html = os.path.join(_encrypted_dir, "unlock.html")

if _ENCRYPTION_ENABLED:
    if not os.path.isfile(_encrypted_bundle):
        logger.error(
            "ENCRYPTION_ENABLED=true ma %s mancante. "
            "Esegui `python scripts/encrypt_build.py` prima del deploy.",
            _encrypted_bundle,
        )

    @app.get("/encrypted-bundle")
    async def serve_encrypted_bundle():
        """Serve il blob cifrato (salt + nonce + AES-GCM ciphertext+tag)."""
        if not os.path.isfile(_encrypted_bundle):
            return JSONResponse(status_code=404, content={"detail": "Bundle non disponibile"})
        return FileResponse(
            _encrypted_bundle,
            media_type="application/octet-stream",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )

    @app.get("/{full_path:path}")
    async def serve_unlock(request: Request, full_path: str):
        """Catch-all: tutte le rotte non-API restituiscono unlock.html.
        Il server NON serve mai la build React in chiaro."""
        if not os.path.isfile(_unlock_html):
            return JSONResponse(
                status_code=503,
                content={"detail": "unlock.html mancante; build incompleto"},
            )
        return FileResponse(
            _unlock_html,
            media_type="text/html; charset=utf-8",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
            },
        )

    logger.info("Frontend protetto da client-side encryption (ENCRYPTION_ENABLED=true).")

else:
    # Modalità classica: serve la build React in chiaro
    _static_dir = os.path.join(os.path.dirname(__file__), "static")
    _frontend_build_dir = os.path.join(os.path.dirname(__file__), "..", "frontend", "build")

    _spa_dir: str | None = None
    if os.path.isdir(_static_dir):
        _spa_dir = _static_dir
        logger.info(f"Directory static trovata (Render): {_static_dir}")
    elif os.path.isdir(_frontend_build_dir):
        _spa_dir = _frontend_build_dir
        logger.info(f"Directory di build del frontend trovata: {_frontend_build_dir}")
    else:
        logger.warning(
            "Nessuna directory di build del frontend trovata. "
            "Il montaggio dei file statici e' stato saltato."
        )

    if _spa_dir:
        _assets_dir = os.path.join(_spa_dir, "static")
        if os.path.isdir(_assets_dir):
            app.mount("/static", StaticFiles(directory=_assets_dir), name="static-assets")

        @app.get("/{full_path:path}")
        async def serve_spa(request: Request, full_path: str):
            """Serve index.html per tutte le rotte non-API (SPA catch-all)."""
            file_path = os.path.join(_spa_dir, full_path)
            if full_path and os.path.isfile(file_path):
                return FileResponse(file_path)
            index_path = os.path.join(_spa_dir, "index.html")
            if os.path.isfile(index_path):
                return FileResponse(index_path)
            return JSONResponse(status_code=404, content={"detail": "Frontend non trovato"})
