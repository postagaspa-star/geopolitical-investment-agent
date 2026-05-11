"""
Applicazione principale FastAPI per l'agente geopolitico di investimento.
Fornisce endpoint REST per il portafoglio, le posizioni, i trade e i log.
"""

import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta

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
            cached = get_cached_prices_bulk(tickers, max_age_seconds=700)

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
            return get_cached_prices_bulk(ticker_list, max_age_seconds=700)
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
    Mercati aperti → run_full_pipeline (Watchdog → Technical → Decision Sonnet).
    Mercati chiusi → run_crypto_pipeline (Technical Crypto → Decision R1).
    """
    run_id = str(uuid.uuid4())
    mode = scheduler.get_current_mode()
    market_open = scheduler.is_market_open()

    async def _run():
        try:
            logger.info(f"Esecuzione manuale pipeline avviata (run_id: {run_id}, mode: {mode}).")
            from agents.orchestrator import run_full_pipeline, run_crypto_pipeline
            if market_open:
                result = await run_full_pipeline(run_id=run_id)
            else:
                result = await run_crypto_pipeline(run_id=run_id)
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
    GDELT, NewsAPI, yFinance News, Reddit (sentiment retail), X,
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
# CHAT DECISION — chat conversazionale con i Decision Agent (Standard / Crypto)
# Diverso dal /api/chat/* esistente (Coach Cards / analyst read-only): qui
# l'agente puo' PROPORRE trade e l'utente li conferma per esecuzione.
# ═══════════════════════════════════════════════════════════════════════

class ChatDecisionSendPayload(BaseModel):
    agent_type: str  # "standard" | "crypto"
    message: str


class ChatDecisionExecutePayload(BaseModel):
    message_id: int


class ChatDecisionExecuteActionPayload(BaseModel):
    """Esegue una specifica proposed_action di un messaggio chat decision."""
    message_id: int
    action_index: int


@app.post("/api/chat-decision/send")
async def chat_decision_send(payload: ChatDecisionSendPayload):
    """
    Invia un messaggio al Decision Agent (Standard o Crypto). Ritorna la
    risposta + un eventuale proposed_trade che l'utente puo' confermare.
    """
    try:
        from agents import chat_decision

        agent_type = (payload.agent_type or "standard").lower()
        if agent_type not in ("standard", "crypto"):
            return JSONResponse(status_code=400, content={"error": "agent_type non valido"})

        message = (payload.message or "").strip()
        if not message:
            return JSONResponse(status_code=400, content={"error": "messaggio vuoto"})

        result = await chat_decision.chat_with_decision_agent(agent_type, message)
        return result
    except Exception as e:
        logger.error("chat_decision_send: %s", e, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": str(e), "type": type(e).__name__},
        )


@app.get("/api/chat-decision/history/{agent_type}")
async def chat_decision_history(agent_type: str, limit: int = 100):
    """Ritorna i messaggi della conversazione attiva per agent_type."""
    try:
        agent_type = (agent_type or "standard").lower()
        if agent_type not in ("standard", "crypto"):
            return JSONResponse(status_code=400, content={"error": "agent_type non valido"})

        conv_id = database.get_or_create_decision_chat_conversation(agent_type)
        if not conv_id:
            return {"conversation_id": None, "messages": []}

        messages = database.get_decision_chat_messages(conv_id, limit=limit)
        return {"conversation_id": conv_id, "agent_type": agent_type, "messages": messages}
    except Exception as e:
        logger.error("chat_decision_history: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.delete("/api/chat-decision/clear/{agent_type}")
async def chat_decision_clear(agent_type: str):
    """Cancella la conversazione attiva per agent_type (cascade sui msg)."""
    try:
        agent_type = (agent_type or "standard").lower()
        if agent_type not in ("standard", "crypto"):
            return JSONResponse(status_code=400, content={"error": "agent_type non valido"})

        ok = database.clear_decision_chat(agent_type)
        return {"cleared": ok, "agent_type": agent_type}
    except Exception as e:
        logger.error("chat_decision_clear: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/chat-decision/execute-trade")
async def chat_decision_execute_trade(payload: ChatDecisionExecutePayload):
    """
    Esegue il proposed_trade contenuto in un messaggio chat. Richiede
    conferma esplicita dell'utente (chiamata triggerata dal click sul
    bottone "Esegui" nella card del frontend).

    Recupera il messaggio dal DB, valida il trade, esegue via portfolio.execute_*
    e marca il messaggio con executed_trade_id.
    """
    try:
        msg = database.get_decision_chat_message(payload.message_id)
        if not msg:
            return JSONResponse(status_code=404, content={"error": "messaggio non trovato"})

        if msg.get("executed_trade_id"):
            return JSONResponse(
                status_code=409,
                content={"error": "trade gia' eseguito",
                         "trade_id": msg.get("executed_trade_id")},
            )

        proposed = msg.get("proposed_trade")
        if not proposed or not isinstance(proposed, dict):
            return JSONResponse(
                status_code=400,
                content={"error": "il messaggio non contiene un proposed_trade valido"},
            )

        ticker = (proposed.get("ticker") or "").upper().strip()
        action = (proposed.get("action") or "").upper().strip()
        try:
            quantity = float(proposed.get("quantity", 0))
        except (TypeError, ValueError):
            quantity = 0
        reasoning = proposed.get("reasoning") or "Trade proposto via chat decision agent"

        if not ticker or action not in ("BUY", "SELL") or quantity <= 0:
            return JSONResponse(
                status_code=400,
                content={"error": "proposed_trade malformato",
                         "details": {"ticker": ticker, "action": action, "quantity": quantity}},
            )

        # Recupera prezzo corrente: ultima candela daily (fetch_market_data
        # ha cache 5 min in-memory, quindi il fetch e' praticamente gratis).
        current_price = None
        try:
            import data_fetchers as _df
            md = _df.fetch_market_data(ticker, period_days=2)
            if md and md.get("data"):
                current_price = md["data"][-1].get("close")
        except Exception as e:
            logger.warning("fetch price for chat trade %s: %s", ticker, e)

        if not current_price or current_price <= 0:
            return JSONResponse(
                status_code=502,
                content={"error": f"prezzo non disponibile per {ticker}"},
            )

        # Esegui il trade
        import portfolio as _portfolio
        geo_reasoning = f"[CHAT-USER-CONFIRMED] {reasoning}"
        tech_reasoning = "(eseguito via chat decision agent)"

        try:
            if action == "BUY":
                result = _portfolio.execute_buy(
                    ticker, quantity, current_price,
                    geo_reasoning, tech_reasoning, 80,
                )
            else:
                result = _portfolio.execute_sell(
                    ticker, quantity, current_price,
                    geo_reasoning, tech_reasoning, 80,
                )
        except Exception as e:
            logger.error("chat_decision execute_trade failed: %s", e, exc_info=True)
            return JSONResponse(
                status_code=500,
                content={"error": f"esecuzione fallita: {e}"},
            )

        if not result or not result.get("success"):
            return JSONResponse(
                status_code=400,
                content={"error": "trade non eseguito",
                         "reason": (result or {}).get("reason", "unknown"),
                         "details": result if isinstance(result, dict) else {}},
            )

        trade_id = result.get("trade_id") or 0
        if trade_id:
            database.mark_decision_chat_trade_executed(payload.message_id, trade_id)

        return {
            "executed": True,
            "trade_id": trade_id,
            "ticker": ticker,
            "action": action,
            "quantity": quantity,
            "price": current_price,
            "result": result,
        }

    except Exception as e:
        logger.error("chat_decision_execute_trade top-level: %s", e, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": str(e), "type": type(e).__name__},
        )


# ──────────────────────────────────────────────────────────────────────────
# ACTIONABLE CHAT — esecuzione generica di proposed_actions[]
# ──────────────────────────────────────────────────────────────────────────

def _exec_action_set_stop_loss(action: dict) -> dict:
    """Imposta stop-loss su una posizione esistente. Calcola il prezzo
    assoluto da pct se necessario (rispetto all'avg_entry_price)."""
    ticker = (action.get("ticker") or "").upper().strip()
    if not ticker:
        return {"ok": False, "error": "ticker mancante"}

    # Recupera la posizione per validare e per calcolare stop_loss_price da pct
    positions = database.get_positions() or []
    pos = next((p for p in positions if (p.get("ticker") or "").upper() == ticker), None)
    if not pos:
        return {"ok": False, "error": f"nessuna posizione aperta su {ticker}"}

    if action.get("stop_loss_price") is not None:
        sl_price = float(action["stop_loss_price"])
    else:
        pct = float(action.get("stop_loss_pct", 0))
        # FIX: la colonna effettiva nelle posizioni e' `avg_buy_price`
        # (vedi db_sqlite/db_supabase schema). I fallback su avg_entry_price /
        # entry_price restano per compat con eventuali payload diversi.
        entry = float(
            pos.get("avg_buy_price")
            or pos.get("avg_entry_price")
            or pos.get("entry_price")
            or 0
        )
        if entry <= 0:
            return {"ok": False, "error": "prezzo medio di carico non disponibile"}
        # pct negativo per BUY (long), positivo per SHORT — qui assumiamo long
        sl_price = round(entry * (1 + pct / 100.0), 4)

    if sl_price <= 0:
        return {"ok": False, "error": "stop_loss_price calcolato non valido"}

    try:
        database.update_position_auto_exit(
            ticker, stop_loss_price=sl_price, set_by="chat_decision_user",
        )
    except Exception as e:
        return {"ok": False, "error": f"update DB fallito: {e}"}

    return {"ok": True, "ticker": ticker, "stop_loss_price": sl_price}


def _exec_action_set_take_profit(action: dict) -> dict:
    """Imposta take-profit su una posizione esistente."""
    ticker = (action.get("ticker") or "").upper().strip()
    if not ticker:
        return {"ok": False, "error": "ticker mancante"}

    positions = database.get_positions() or []
    pos = next((p for p in positions if (p.get("ticker") or "").upper() == ticker), None)
    if not pos:
        return {"ok": False, "error": f"nessuna posizione aperta su {ticker}"}

    if action.get("take_profit_price") is not None:
        tp_price = float(action["take_profit_price"])
    else:
        pct = float(action.get("take_profit_pct", 0))
        # FIX: usa avg_buy_price (campo reale schema), fallback su alias
        entry = float(
            pos.get("avg_buy_price")
            or pos.get("avg_entry_price")
            or pos.get("entry_price")
            or 0
        )
        if entry <= 0:
            return {"ok": False, "error": "prezzo medio di carico non disponibile"}
        tp_price = round(entry * (1 + pct / 100.0), 4)

    if tp_price <= 0:
        return {"ok": False, "error": "take_profit_price calcolato non valido"}

    try:
        database.update_position_auto_exit(
            ticker, take_profit_price=tp_price, set_by="chat_decision_user",
        )
    except Exception as e:
        return {"ok": False, "error": f"update DB fallito: {e}"}

    return {"ok": True, "ticker": ticker, "take_profit_price": tp_price}


def _exec_action_add_directive(action: dict) -> dict:
    """Appende una direttiva utente al testo esistente in settings."""
    new_directive = (action.get("text") or "").strip()
    if not new_directive or len(new_directive) < 3:
        return {"ok": False, "error": "testo direttiva mancante"}

    try:
        current = database.get_setting("user_directives_text", "") or ""
        # Append come bullet su nuova riga
        prefix = f"- [{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC, da chat] "
        line = prefix + new_directive
        if current.strip():
            updated = current.rstrip() + "\n" + line
        else:
            updated = line
        database.set_setting("user_directives_text", updated[:8000])
    except Exception as e:
        return {"ok": False, "error": f"set_setting fallito: {e}"}

    return {"ok": True, "directive_text": new_directive}


async def _exec_action_execute_trade(action: dict) -> dict:
    """Esegue un trade equivalentemente all'endpoint execute-trade legacy."""
    ticker = (action.get("ticker") or "").upper().strip()
    act = (action.get("action") or "").upper().strip()
    try:
        qty = float(action.get("quantity", 0))
    except (TypeError, ValueError):
        return {"ok": False, "error": "quantity non valida"}
    if not ticker or act not in ("BUY", "SELL") or qty <= 0:
        return {"ok": False, "error": "parametri trade invalidi"}

    # Prezzo corrente
    current_price = None
    try:
        import data_fetchers as _df
        md = _df.fetch_market_data(ticker, period_days=2)
        if md and md.get("data"):
            current_price = md["data"][-1].get("close")
    except Exception as e:
        logger.warning("fetch price for chat action trade %s: %s", ticker, e)
    if not current_price or current_price <= 0:
        return {"ok": False, "error": f"prezzo non disponibile per {ticker}"}

    import portfolio as _portfolio
    geo_reasoning = f"[CHAT-USER-CONFIRMED] {action.get('reasoning') or 'trade via chat'}"
    tech_reasoning = "(eseguito via chat decision agent — proposed_actions)"
    try:
        if act == "BUY":
            result = _portfolio.execute_buy(
                ticker, qty, current_price, geo_reasoning, tech_reasoning, 80,
            )
        else:
            result = _portfolio.execute_sell(
                ticker, qty, current_price, geo_reasoning, tech_reasoning, 80,
            )
    except Exception as e:
        return {"ok": False, "error": f"esecuzione fallita: {e}"}

    if not result or not result.get("success"):
        return {"ok": False,
                "error": "trade non eseguito",
                "reason": (result or {}).get("reason", "unknown")}

    return {"ok": True, "trade_id": result.get("trade_id"),
            "ticker": ticker, "action": act,
            "quantity": qty, "price": current_price}


@app.post("/api/chat-decision/execute-action")
async def chat_decision_execute_action(payload: ChatDecisionExecuteActionPayload):
    """
    Esegue una specifica proposed_action (per indice) di un messaggio chat
    della pagina /live/chat. Tipi supportati:
      - execute_trade
      - set_stop_loss
      - set_take_profit
      - add_directive

    Richiede sempre conferma esplicita dell'utente (chiamata triggerata dal
    bottone "Conferma" della card frontend).
    """
    try:
        msg = database.get_decision_chat_message(payload.message_id)
        if not msg:
            return JSONResponse(status_code=404, content={"error": "messaggio non trovato"})

        actions = msg.get("proposed_actions") or []
        if not isinstance(actions, list) or not actions:
            return JSONResponse(
                status_code=400,
                content={"error": "il messaggio non contiene proposed_actions"},
            )

        if payload.action_index < 0 or payload.action_index >= len(actions):
            return JSONResponse(
                status_code=400,
                content={"error": f"action_index {payload.action_index} fuori range (0..{len(actions)-1})"},
            )

        # Già eseguita?
        prev = msg.get("executed_action_results") or {}
        if isinstance(prev, dict) and str(payload.action_index) in prev:
            return JSONResponse(
                status_code=409,
                content={"error": "azione gia' eseguita",
                         "previous_result": prev[str(payload.action_index)]},
            )

        action = actions[payload.action_index]
        atype = (action.get("type") or "").lower()

        if atype == "execute_trade":
            result = await _exec_action_execute_trade(action)
            # Se TRADE OK, marca anche executed_trade_id per compat UI legacy
            if result.get("ok") and result.get("trade_id"):
                try:
                    database.mark_decision_chat_trade_executed(
                        payload.message_id, result["trade_id"],
                    )
                except Exception:
                    pass
        elif atype == "set_stop_loss":
            result = _exec_action_set_stop_loss(action)
        elif atype == "set_take_profit":
            result = _exec_action_set_take_profit(action)
        elif atype == "add_directive":
            result = _exec_action_add_directive(action)
        else:
            return JSONResponse(
                status_code=400,
                content={"error": f"tipo azione non supportato: {atype}"},
            )

        # Persisti il risultato per indice
        try:
            database.mark_decision_chat_action_executed(
                payload.message_id, payload.action_index, result,
            )
        except Exception as e:
            logger.warning("mark_decision_chat_action_executed fallita: %s", e)

        status_code = 200 if result.get("ok") else 400
        return JSONResponse(status_code=status_code, content={
            "executed": bool(result.get("ok")),
            "action_type": atype,
            "result": result,
        })

    except Exception as e:
        logger.error("chat_decision_execute_action top-level: %s", e, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": str(e), "type": type(e).__name__},
        )


# ═══════════════════════════════════════════════════════════════════════
# AGENT COMMITMENTS — memoria persistente delle "promesse" del Decision Agent
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/commitments/{agent_type}")
async def list_commitments(agent_type: str, status: str = Query("active")):
    """
    Ritorna gli impegni del Decision Agent per agent_type ∈ {standard,crypto}.
    status='active' (default) = solo attivi. status='all' = tutti recenti.
    """
    if agent_type not in ("standard", "crypto"):
        return JSONResponse(status_code=400, content={"error": "agent_type non valido"})
    try:
        if status == "all":
            items = database.get_recent_agent_commitments(agent_type, limit=30)
        else:
            items = database.get_active_agent_commitments(agent_type, limit=30)
        return {"agent_type": agent_type, "status": status, "items": items, "count": len(items)}
    except Exception as e:
        logger.error("list_commitments error: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


class CommitmentCancelPayload(BaseModel):
    reason: str | None = None


@app.post("/api/commitments/{commitment_id}/cancel")
async def cancel_commitment(commitment_id: int, payload: CommitmentCancelPayload):
    """L'utente cancella manualmente un impegno attivo."""
    try:
        ok = database.resolve_agent_commitment(
            commitment_id,
            status="cancelled",
            resolved_reason=f"[USER] {payload.reason or 'cancellato manualmente'}",
        )
        return {"success": bool(ok), "commitment_id": commitment_id}
    except Exception as e:
        logger.error("cancel_commitment error: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


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

    # 4. Storage mode: rivela in tempo reale quale tier sta servendo i run
    # (sim_runs primario vs sim_settings fallback dual-mode vs SQLite vs cache).
    # Utile per diagnosticare se i run di oggi sopravviveranno al prossimo deploy.
    try:
        if hasattr(sim_db, "get_storage_mode"):
            info["storage_mode"] = sim_db.get_storage_mode()
    except Exception as e:
        info["storage_mode_error"] = str(e)

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
    except ValueError as e:
        # ValueError = errore noto/recuperabile (rate limit, parsing, ecc.)
        # Ritorniamo 503 con messaggio chiaro invece di 500 generico.
        logger.error("[SIM] step %s/%d ValueError: %s", run_id, req.step_index, e)
        msg = str(e)
        is_transient = ("429" in msg or "timeout" in msg.lower()
                        or "503" in msg or "504" in msg or "502" in msg)
        return JSONResponse(
            status_code=503,
            content={
                "error": msg,
                "type": "transient" if is_transient else "fatal",
                "hint": ("Riprova tra qualche secondo: il modello e' temporaneamente"
                         " sovraccarico o non risponde."
                         if is_transient
                         else "Errore inaspettato sul step. Controlla i log."),
            },
        )
    except Exception as e:
        logger.error("[SIM] step %s/%d crash: %s", run_id, req.step_index, e,
                     exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": str(e), "type": type(e).__name__,
                     "hint": "Errore inaspettato. Riprova; se persiste segnala con i log."}
        )


# ════════════════════════════════════════════════════════════════════════
# Simulator V2 — Stateless engine con prezzi reali Polygon
# ════════════════════════════════════════════════════════════════════════
class SimV2StartReq(BaseModel):
    category: str = "geopolitico"
    num_steps: int = 4              # 3-5
    scenario_id: str | None = None  # opzionale, se None scelta random
    initial_capital: float = 100000.0
    # Costo per trade in bps (10 = 0.10%). None = default 10 bps.
    # Pass 0 per slippage rerun "no fees" comparativo.
    commission_bps: float | None = None


@app.post("/api/simulator/v2/start")
async def sim_v2_start(req: SimV2StartReq):
    """
    Inizializza una nuova partita simulator V2 (stateless).
    Ritorna scenario + portfolio iniziale + step_dates.
    Il browser tiene tutto e lo rimanda alla /step.
    """
    from simulator import v2_engine
    try:
        return await v2_engine.start_run(
            category=req.category,
            num_steps=req.num_steps,
            scenario_id=req.scenario_id,
            initial_capital=req.initial_capital,
            commission_bps=req.commission_bps,
        )
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        logger.error("[SIM-V2] start crash: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


class SimV2StepReq(BaseModel):
    scenario: dict
    portfolio: dict
    history: list[dict] = []
    step_index: int


@app.post("/api/simulator/v2/step")
async def sim_v2_step(req: SimV2StepReq):
    """
    Esegue uno step. Stateless: il browser fornisce tutto il contesto.
    """
    from simulator import v2_engine
    try:
        return await v2_engine.execute_step(
            scenario=req.scenario,
            portfolio=req.portfolio,
            history=req.history,
            step_index=req.step_index,
        )
    except ValueError as e:
        msg = str(e)
        is_transient = any(x in msg.lower() for x in ["429", "timeout", "503", "502", "504"])
        return JSONResponse(
            status_code=503,
            content={
                "error": msg,
                "type": "transient" if is_transient else "fatal",
                "hint": ("Modello sovraccarico, riprova tra qualche secondo"
                         if is_transient
                         else "Errore inaspettato. Controlla i log."),
            },
        )
    except Exception as e:
        logger.error("[SIM-V2] step %d crash: %s", req.step_index, e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


class SimV2FinalizeReq(BaseModel):
    scenario: dict
    portfolio: dict
    history: list[dict]
    persist: bool = True


@app.post("/api/simulator/v2/finalize")
async def sim_v2_finalize(req: SimV2FinalizeReq):
    """
    Chiude la partita: P&L finale, debrief, salvataggio nel DB.
    """
    from simulator import v2_engine
    try:
        return await v2_engine.finalize_run(
            scenario=req.scenario,
            portfolio=req.portfolio,
            history=req.history,
            persist=req.persist,
        )
    except Exception as e:
        logger.error("[SIM-V2] finalize crash: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


class SimV2SaveAdviceReq(BaseModel):
    scenario: dict
    history: list[dict]
    final_result: dict
    title: str
    text: str
    rationale: str = ""
    targets: list[str] = ["simulator"]   # "simulator" e/o "live"


@app.post("/api/simulator/v2/save-advice")
async def sim_v2_save_advice(req: SimV2SaveAdviceReq):
    """
    Salva un consiglio del debrief / advisor nella memoria.

    targets:
      - "simulator": memoria categorizzata sim_advisor (iniettata nei
        prossimi run del Simulator della stessa categoria)
      - "live": documento nella knowledge base del bot Live (categoria
        "lessons-learned"), iniettato nei prompt del Decision Agent
    """
    try:
        from agents import sim_advisor
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"import fallito: {e}"})

    title = (req.title or "").strip()
    text = (req.text or "").strip()
    if not title or not text:
        return JSONResponse(status_code=400, content={
            "error": "title e text sono obbligatori",
        })

    saved_to: list[str] = []
    errors: list[str] = []

    # ─── SIMULATOR memory ─────────────────────────────────────────────
    if "simulator" in (req.targets or []):
        try:
            # Componiamo un fake "run" record per detect_scenario_key
            cat = req.scenario.get("category", "unknown")
            v = (req.final_result.get("final_valuation") or {})
            fake_run = {
                "category": cat,
                "scenario_id": req.scenario.get("id"),
                "perf_1m": (v.get("total_pnl_pct", 0) or 0) / 100.0,
                "outcome": req.final_result.get("outcome", "yellow"),
                "asset_chosen": (req.final_result.get("asset_breakdown", [{}])[0]
                                 .get("asset")) if req.final_result.get("asset_breakdown") else None,
            }
            category_key, tags = sim_advisor.detect_scenario_key(fake_run)
            advice = {
                "run_id": req.final_result.get("persisted_run_id"),
                "scenario_category": category_key,
                "scenario_tags": tags,
                "title": title[:200],
                "text": text[:1000],
                "rationale": (req.rationale or "").strip()[:600],
            }
            aid = sim_advisor.save_advice(advice)
            saved_to.append(f"simulator:{category_key}:{aid}")
        except Exception as e:
            logger.error("sim_advisor save_advice fallito: %s", e, exc_info=True)
            errors.append(f"simulator: {e}")

    # ─── LIVE knowledge (documento) ───────────────────────────────────
    if "live" in (req.targets or []):
        try:
            scenario_title = req.scenario.get("title", "Scenario")
            doc_filename = (
                f"sim-lesson-{(req.scenario.get('id') or 'x')[:24]}-"
                f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}.txt"
            )
            doc_content = (
                f"# Lezione operativa estratta dal Simulator\n\n"
                f"Titolo: {title}\n"
                f"Scenario di partenza: {scenario_title}\n"
                f"Categoria: {req.scenario.get('category', '?')}\n"
                f"Periodo: {req.scenario.get('period_start', '?')} → "
                f"{req.scenario.get('period_end', '?')}\n\n"
                f"## Regola operativa\n{text}\n\n"
                f"## Razionale\n{req.rationale or '(nessuno)'}\n"
            )
            database.insert_document(
                doc_filename, doc_content, len(doc_content.encode("utf-8")),
                category="lessons-learned",
            )
            saved_to.append(f"live:{doc_filename}")
        except Exception as e:
            logger.error("insert_document live fallito: %s", e, exc_info=True)
            errors.append(f"live: {e}")

    if not saved_to and errors:
        return JSONResponse(status_code=500, content={"errors": errors})

    return {"status": "ok", "saved_to": saved_to, "errors": errors}


class SimV2AdvisorReq(BaseModel):
    scenario: dict
    history: list[dict]
    final_result: dict
    user_message: str
    chat_history: list[dict] = []


@app.post("/api/simulator/v2/advisor")
async def sim_v2_advisor(req: SimV2AdvisorReq):
    """
    Chat advisor end-of-game: risponde a domande sull'andamento della partita.
    Stateless: il client tiene la chat_history e la rimanda.
    """
    from simulator import v2_engine
    try:
        reply = await v2_engine.advisor_chat(
            scenario=req.scenario,
            history=req.history,
            final_result=req.final_result,
            user_message=req.user_message,
            chat_history=req.chat_history,
        )
        return {"reply": reply}
    except Exception as e:
        logger.error("[SIM-V2] advisor crash: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


# ════════════════════════════════════════════════════════════════════════
# Simulator V2 CRYPTO — partita 2-giorni-per-turno, AI = Decision Crypto R1
# ════════════════════════════════════════════════════════════════════════

class SimV2CryptoStartReq(BaseModel):
    category: str | None = None
    num_steps: int = 6           # 5-7 turni
    scenario_id: str | None = None
    initial_capital: float = 100000.0
    commission_bps: float | None = None   # default 10 bps


@app.get("/api/simulator/v2/crypto/scenarios")
async def sim_v2_crypto_scenarios(category: str | None = None):
    """Lista scenari crypto disponibili (filtro opzionale per categoria)."""
    from simulator import crypto_scenarios as _cs
    scenarios = _cs.list_crypto_scenarios(category)
    return [{"id": s["id"], "category": s["category"],
             "title": s["title"], "brief": s.get("brief", ""),
             "period_start": s["period_start"],
             "period_end": s["period_end"]}
            for s in scenarios]


@app.get("/api/simulator/v2/crypto/scenarios/counts")
async def sim_v2_crypto_scenario_counts():
    from simulator import crypto_scenarios as _cs
    return _cs.crypto_scenario_counts()


@app.post("/api/simulator/v2/crypto/start")
async def sim_v2_crypto_start(req: SimV2CryptoStartReq):
    from simulator import v2_crypto_engine as engine
    try:
        return await engine.start_crypto_run(
            category=req.category, num_steps=req.num_steps,
            scenario_id=req.scenario_id, initial_capital=req.initial_capital,
            commission_bps=req.commission_bps,
        )
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        logger.error("[SIM-CRYPTO] start crash: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


class SimV2CryptoStepReq(BaseModel):
    scenario: dict
    portfolio: dict
    history: list[dict] = []
    step_index: int


@app.post("/api/simulator/v2/crypto/step")
async def sim_v2_crypto_step(req: SimV2CryptoStepReq):
    from simulator import v2_crypto_engine as engine
    try:
        return await engine.execute_crypto_step(
            scenario=req.scenario, portfolio=req.portfolio,
            history=req.history, step_index=req.step_index,
        )
    except ValueError as e:
        msg = str(e)
        is_transient = any(x in msg.lower() for x in ["429", "timeout", "503", "502", "504"])
        return JSONResponse(
            status_code=503,
            content={"error": msg,
                     "type": "transient" if is_transient else "fatal"},
        )
    except Exception as e:
        logger.error("[SIM-CRYPTO] step %d crash: %s", req.step_index, e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


class SimV2CryptoFinalizeReq(BaseModel):
    scenario: dict
    portfolio: dict
    history: list[dict]
    persist: bool = True


@app.post("/api/simulator/v2/crypto/finalize")
async def sim_v2_crypto_finalize(req: SimV2CryptoFinalizeReq):
    from simulator import v2_crypto_engine as engine
    try:
        return await engine.finalize_crypto_run(
            scenario=req.scenario, portfolio=req.portfolio,
            history=req.history, persist=req.persist,
        )
    except Exception as e:
        logger.error("[SIM-CRYPTO] finalize crash: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


class SimV2CryptoAdvisorReq(BaseModel):
    scenario: dict
    history: list[dict]
    final_result: dict
    user_message: str
    chat_history: list[dict] = []


@app.post("/api/simulator/v2/crypto/advisor")
async def sim_v2_crypto_advisor(req: SimV2CryptoAdvisorReq):
    from simulator import v2_crypto_engine as engine
    try:
        reply = await engine.crypto_advisor_chat(
            scenario=req.scenario, history=req.history,
            final_result=req.final_result, user_message=req.user_message,
            chat_history=req.chat_history,
        )
        return {"reply": reply}
    except Exception as e:
        logger.error("[SIM-CRYPTO] advisor crash: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/simulator/active-runs")
async def sim_active_runs(include_recent: bool = Query(default=True)):
    """
    Lista dei run del Simulator attualmente in esecuzione (e quelli appena
    completati per qualche secondo, per dare il "flash di fine partita").

    Polling target: ogni 3-5 secondi dalla SimDashboard.

    Ogni record contiene: tracking_id, engine, mode (manual/auto), categoria,
    titolo scenario, step corrente / totale, status corrente, elapsed_seconds,
    e — solo se completato — outcome + pnl_pct + persisted_run_id.

    include_recent: se False filtra solo quelli ancora "running" (no completed/error).
    """
    try:
        from simulator import active_runs as _ar
        runs = _ar.list_active(include_recent=include_recent)
        return {"runs": runs, "count": len(runs)}
    except Exception as e:
        logger.error("[SIM] active_runs endpoint error: %s", e, exc_info=True)
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


@app.get("/api/simulator/win-by-scenario")
async def sim_win_by_scenario():
    """
    Win rate breakdown per scenario_id (NON solo categoria).
    Risponde a: "in QUALI scenari l'AI e' piu' intelligente?".

    Per ogni scenario_id ritorna:
      - title: nome leggibile
      - category: macro/geo/normale/crash_rally/bull_cycle/...
      - n_runs:   quante volte e' stato giocato
      - n_green:  quanti outcome verdi
      - win_rate: green / total
      - avg_pnl_1m: P&L medio a 1M (se presente)
      - avg_sharpe: Sharpe medio dei run V2 (se computato)

    Considera solo scenari con almeno 1 run (no rumore).
    """
    from simulator import db as sim_db
    from simulator import scenarios as _scn
    from simulator import crypto_scenarios as _cs

    runs = sim_db.list_runs(limit=2000)

    # Mappa scenario_id → metadata
    sid_meta = {}
    for s in _scn._all_scenarios():
        sid_meta[s.get("id")] = {"title": s.get("title", ""),
                                 "category": s.get("category", "")}
    try:
        for s in _cs.list_crypto_scenarios():
            sid_meta[s.get("id")] = {"title": s.get("title", ""),
                                     "category": s.get("category", "")}
    except Exception:
        pass

    # Aggrega per scenario_id
    by_sid: dict = {}
    for r in runs:
        sid = r.get("scenario_id")
        if not sid:
            continue
        bucket = by_sid.setdefault(sid, {"n_runs": 0, "n_green": 0,
                                          "pnl_sum": 0, "pnl_n": 0,
                                          "sharpe_sum": 0.0, "sharpe_n": 0})
        bucket["n_runs"] += 1
        if r.get("outcome") == "green":
            bucket["n_green"] += 1
        if r.get("perf_1m") is not None:
            bucket["pnl_sum"] += float(r.get("perf_1m") or 0)
            bucket["pnl_n"] += 1
        # Sharpe: solo nei run V2 lo abbiamo nel full_data
        full = r.get("full_data") or {}
        if isinstance(full, str):
            try:
                full = json.loads(full)
            except Exception:
                full = {}
        sh = (full.get("quant_metrics") or {}).get("sharpe_ratio") if isinstance(full, dict) else None
        if isinstance(sh, (int, float)):
            bucket["sharpe_sum"] += float(sh)
            bucket["sharpe_n"] += 1

    out = []
    for sid, b in by_sid.items():
        meta = sid_meta.get(sid, {})
        out.append({
            "scenario_id": sid,
            "title": meta.get("title", sid),
            "category": meta.get("category", "?"),
            "n_runs": b["n_runs"],
            "n_green": b["n_green"],
            "win_rate": round(b["n_green"] / b["n_runs"], 3) if b["n_runs"] else 0,
            "avg_pnl_1m": round(b["pnl_sum"] / b["pnl_n"], 4) if b["pnl_n"] else None,
            "avg_sharpe": round(b["sharpe_sum"] / b["sharpe_n"], 2) if b["sharpe_n"] else None,
        })
    # Ordina: piu' giocati prima, e win rate alto prima a parita' di n_runs
    out.sort(key=lambda x: (-x["n_runs"], -(x["win_rate"] or 0)))
    return {"scenarios": out, "total_scenarios_played": len(out)}


@app.get("/api/simulator/sentiment-drift")
async def sim_sentiment_drift():
    """
    "Sentiment Drift" — evoluzione della strategia nel tempo.

    Ritorna:
      - top_advice: lista dei 3 advice piu' applicati (apply_count desc) con
        i dettagli (title, text, scenario_category, apply_count).
      - sharpe_evolution: serie temporale dello Sharpe medio rolling sui run
        V2, ordinati per completed_at. Permette di vedere se l'applicazione
        crescente degli advice ha migliorato lo Sharpe.

    Vincolo: serve almeno qualche run V2 con quant_metrics per avere segnale.
    """
    from simulator import db as sim_db
    try:
        from agents import sim_advisor
        all_advice = sim_advisor.list_all_advice()
    except Exception as e:
        all_advice = {}
        logger.warning("sentiment_drift: advice load fallita: %s", e)

    # Flatten + sort by apply_count desc, top 3
    flat = []
    for cat_key, items in (all_advice or {}).items():
        for it in items:
            flat.append({
                "id": it.get("id"),
                "title": it.get("title", ""),
                "text": (it.get("text") or "")[:400],
                "scenario_category": cat_key,
                "apply_count": int(it.get("apply_count") or 0),
                "created_at": it.get("created_at", ""),
            })
    flat.sort(key=lambda x: x["apply_count"], reverse=True)
    top_advice = flat[:3]

    # Sharpe evolution: ordina i run V2 per completed_at e calcola rolling mean
    runs = sim_db.list_runs(limit=1000)
    runs.sort(key=lambda r: r.get("completed_at") or "")
    series = []
    rolling_window = 5
    sharpes = []
    for r in runs:
        full = r.get("full_data") or {}
        if isinstance(full, str):
            try:
                full = json.loads(full)
            except Exception:
                full = {}
        if not isinstance(full, dict):
            continue
        sh = (full.get("quant_metrics") or {}).get("sharpe_ratio")
        if not isinstance(sh, (int, float)):
            continue
        sharpes.append(float(sh))
        # Rolling mean ultimi N (cap window)
        window = sharpes[-rolling_window:]
        rolling = sum(window) / len(window)
        series.append({
            "completed_at": r.get("completed_at", ""),
            "scenario_id": r.get("scenario_id", ""),
            "category": r.get("category", ""),
            "sharpe": round(float(sh), 2),
            "rolling_mean_sharpe": round(rolling, 2),
            "n_in_window": len(window),
        })

    return {
        "top_advice": top_advice,
        "sharpe_evolution": series,
        "rolling_window": rolling_window,
        "total_advice_categories": len(all_advice or {}),
    }


# ────────────────────────────────────────────────────────────────────────────
# Slippage Sensitivity Rerun
# ────────────────────────────────────────────────────────────────────────────
class SimSlippageRerunReq(BaseModel):
    """
    Replay di un run V2 esistente con un commission_bps diverso.
    Permette di rispondere: "Se le fees fossero state 0.5% invece di 0.1%,
    questa strategia sarebbe ancora profittevole?".
    """
    run_id: str
    commission_bps_list: list[float] = [0.0, 10.0, 25.0, 50.0]   # default benchmark


@app.post("/api/simulator/slippage-rerun")
async def sim_slippage_rerun(req: SimSlippageRerunReq):
    """
    Re-applica i trade del run originale a vari livelli di commission_bps,
    ricalcola portfolio_value_series + metriche per ogni livello.
    Stateless rispetto al DB: non sovrascrive il run originale, ritorna
    solo gli scenari per la UI.
    """
    from simulator import db as sim_db
    from simulator import v2_engine, metrics as _metrics

    run = sim_db.get_run(req.run_id)
    if not run:
        return JSONResponse(status_code=404, content={"error": "Run non trovato"})

    full = run.get("full_data") or {}
    if isinstance(full, str):
        try:
            full = json.loads(full)
        except Exception:
            full = {}

    history = full.get("history") or []
    scenario = full.get("scenario") or {}
    if not history or not scenario:
        return JSONResponse(status_code=400, content={
            "error": "Run non rieseguibile: serve full_data.history e .scenario "
                     "(presenti solo nei run Simulator V2)."
        })

    initial = float(((full.get("final_valuation") or {}).get("initial_capital"))
                     or 100000.0)
    price_series = scenario.get("price_series") or full.get("price_series") or {}
    step_dates = scenario.get("step_dates") or full.get("step_dates") or []
    is_crypto = scenario.get("engine_mode") == "crypto" or run.get("category") == "crypto"
    benchmark_ticker = "BTC-USD" if is_crypto else "SPY"
    step_unit_days = 2.0 if is_crypto else 7.0
    periods_year = (_metrics.ANNUALIZATION_CRYPTO if is_crypto
                    else _metrics.ANNUALIZATION_EQUITY)

    out = []
    for bps in req.commission_bps_list:
        # Replay: parti da initial, applica i trade di ogni step a quel bps
        portfolio = v2_engine.make_initial_portfolio(initial, commission_bps=bps)
        equity_curve = [{"step_index": -1, "step_date": (step_dates[0] if step_dates else None),
                          "value": initial}]
        replayed_history = []
        # Tracciamo l'ultima valuation valida per il fallback finale (se nessuno
        # step ha prezzi → si usa lo state iniziale come final_val).
        last_valuation = v2_engine.compute_portfolio_value(portfolio, {})
        for h in history:
            sd = h.get("step_date")
            prices = v2_engine.extract_prices_at_date(price_series, sd) if sd else {}
            if not prices:
                # Salta step senza prezzi: equity invariata
                equity_curve.append({"step_index": h.get("step_index"),
                                     "step_date": sd,
                                     "value": equity_curve[-1]["value"]})
                continue
            ai_trades = h.get("ai_trades") or []
            apply_res = v2_engine.apply_trades(portfolio, ai_trades, prices,
                                                commission_bps=bps)
            portfolio = apply_res["portfolio"]
            last_valuation = v2_engine.compute_portfolio_value(portfolio, prices)
            equity_curve.append({"step_index": h.get("step_index"),
                                 "step_date": sd,
                                 "value": last_valuation["total_value"]})
            replayed_history.append({**h, "applied_trades": apply_res["applied_trades"],
                                      "valuation_after": last_valuation})

        # Punto finale
        final_date = step_dates[-1] if step_dates else None
        final_prices = v2_engine.extract_prices_at_date(price_series, final_date) if final_date else {}
        final_val = (v2_engine.compute_portfolio_value(portfolio, final_prices)
                     if final_prices else last_valuation)
        equity_curve.append({"step_index": len(history), "step_date": final_date,
                             "value": final_val["total_value"]})

        # Metriche
        qm = _metrics.compute_all_metrics(
            equity_curve=equity_curve,
            history=replayed_history,
            final_valuation=final_val,
            step_unit_days=step_unit_days,
            periods_per_year=periods_year,
        )
        # Benchmark series sulla stessa cadenza
        bench = v2_engine._build_benchmark_value_series(
            price_series, benchmark_ticker, step_dates, initial,
            replayed_history, final_date or step_dates[0],
        )
        out.append({
            "commission_bps": bps,
            "final_value": final_val["total_value"],
            "final_pnl_pct": final_val["total_pnl_pct"],
            "total_commissions_paid": final_val.get("total_commissions_paid", 0),
            "portfolio_value_series": equity_curve,
            "benchmark_value_series": bench,
            "sharpe_ratio": qm.get("sharpe_ratio"),
            "max_drawdown": qm.get("max_drawdown", {}).get("max_drawdown_pct"),
            "profit_factor": qm.get("profit_factor"),
            "expectancy": qm.get("expectancy", {}).get("expectancy_dollars"),
        })

    return {
        "run_id": req.run_id,
        "scenarios": out,
        "benchmark_ticker": benchmark_ticker,
        "is_crypto": is_crypto,
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


# ─── Coach Cards: synthesis settimanale del Sim Advisor → reminder Live ────

@app.get("/api/coach-cards")
async def coach_cards_list(active_only: bool = Query(default=True)):
    """Lista le Coach Cards attive (non scadute) per la sidebar Live."""
    try:
        from agents import coach_cards
        cards = coach_cards.list_cards(active_only=active_only)
        status = coach_cards.get_status()
        return {"cards": cards, "status": status, "total": len(cards)}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/coach-cards/synthesize")
async def coach_cards_synthesize(background_tasks: BackgroundTasks):
    """
    Trigger manuale della synthesis (utile per test). Lancia in background
    e ritorna subito 200. La synthesis dura 30-60s; risultati in
    /api/coach-cards.
    """
    async def _do():
        try:
            from agents import coach_cards
            result = await coach_cards.run_weekly_synthesis()
            logger.info("Coach cards synthesis manual: %s", result)
        except Exception as e:
            logger.error("Coach cards synthesis crash: %s", e, exc_info=True)

    background_tasks.add_task(_do)
    return {"status": "started"}


@app.delete("/api/coach-cards/{card_id}")
async def coach_cards_delete(card_id: str):
    """Elimina una Coach Card (es. per scartare false positives)."""
    try:
        from agents import coach_cards
        ok = coach_cards.delete_card(card_id)
        if not ok:
            return JSONResponse(status_code=404, content={"error": "Card non trovata"})
        return {"deleted": True, "id": card_id}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


class ChatToCardReq(BaseModel):
    title: str
    content: str


@app.post("/api/coach-cards/from-chat")
async def coach_cards_from_chat(req: ChatToCardReq):
    """
    Crea una Coach Card dal contenuto di un messaggio chat Live.

    Bug precedente: il `title` (default = primi 80 char del messaggio)
    era spesso markdown grezzo (es. "## Analisi ultime 5 operazioni..."),
    e l'euristica regex "DO/DON'T" non trovava nulla → l'intero content
    finiva nel campo `do` mentre `dont` era "(non specificato)".

    Fix: chiamata DeepSeek-V3 per ESTRARRE titolo + DO + DON'T strutturati
    dal messaggio. ~$0.0001 per save, output pulito.
    """
    try:
        from agents import coach_cards
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"import: {e}"})

    title = (req.title or "").strip()
    content = (req.content or "").strip()
    if not content:
        return JSONResponse(status_code=400, content={"error": "content mancante"})

    # Helper per sanitizzare un titolo da residui markdown
    import re as _re
    def _clean_title(t: str) -> str:
        t = _re.sub(r"^[#\s]+", "", t).strip()       # ## headers
        t = _re.sub(r"[\|`*_]+", " ", t)             # markdown chars
        t = _re.sub(r"\s+", " ", t).strip()
        return t[:120]

    # ── Estrazione strutturata via DeepSeek-V3 ─────────────────────────
    extracted = None
    try:
        api_key = (os.environ.get("DEEPSEEK_API_KEY", "")
                   or database.get_setting("deepseek_api_key", "")).strip()
        if api_key:
            import aiohttp as _aiohttp
            extract_prompt = (
                "Sei un estrattore di consigli operativi. Ricevi un messaggio "
                "(spesso markdown con tabelle / liste) e devi produrre SOLO JSON:\n"
                '{"title": "...", "do": "...", "dont": "...", '
                '"category_focus": "...", "scenarios_signature": "..."}\n\n'
                "Regole:\n"
                "- title: max 80 char, una frase azionabile (NO markdown headers)\n"
                "- do: 1-2 frasi su cosa FARE in scenari simili\n"
                "- dont: 1-2 frasi su cosa NON FARE\n"
                "- category_focus: macro|geopolitico|crypto|equity|risk_management|generale\n"
                "- scenarios_signature: tag breve tipo 'crypto_volatile' / 'equity_bear' / 'general'\n"
                "Solo JSON, niente preambolo o markdown."
            )
            payload = {
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": extract_prompt},
                    {"role": "user", "content": (
                        f"Titolo (suggerito dall'utente, può essere ignorato se vuoto): "
                        f"{title or '(nessun titolo)'}\n\n"
                        f"Contenuto del messaggio chat:\n{content[:3500]}"
                    )},
                ],
                "temperature": 0.3,
                "max_tokens": 600,
                "response_format": {"type": "json_object"},
            }
            async with _aiohttp.ClientSession() as sess:
                async with sess.post(
                    "https://api.deepseek.com/v1/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"},
                    timeout=_aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        raw = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
                        try:
                            extracted = json.loads(raw)
                        except Exception:
                            m = _re.search(r"\{[\s\S]*\}", raw)
                            if m:
                                try:
                                    extracted = json.loads(m.group(0))
                                except Exception:
                                    extracted = None
    except Exception as exc:
        logger.warning("coach card from-chat AI extract failed: %s", exc)
        extracted = None

    # ── Fallback se l'AI fallisce: heuristic + sanitize del titolo ─────
    if not isinstance(extracted, dict):
        # Sanitize title (rimuove markdown). Se vuoto, prima riga di testo.
        clean_title = _clean_title(title) if title else ""
        if not clean_title:
            for line in content.splitlines():
                line = _clean_title(line)
                if line and len(line) > 5:
                    clean_title = line[:80]
                    break
        if not clean_title:
            clean_title = "Consiglio dall'utente"
        extracted = {
            "title": clean_title,
            "do": content[:500],
            "dont": "(da specificare — l'utente non ha chiarito)",
            "category_focus": "generale",
            "scenarios_signature": "user_advice",
        }

    # Build & save
    card = {
        "week_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "category_focus": str(extracted.get("category_focus") or "generale")[:32],
        "title": _clean_title(str(extracted.get("title") or "Consiglio utente"))[:200],
        "do": str(extracted.get("do") or "(vuoto)")[:600],
        "dont": str(extracted.get("dont") or "(non specificato)")[:600],
        "rationale": (
            f"Consiglio salvato dall'utente dalla chat Live. "
            f"Estrazione AI: {'OK' if extracted else 'fallback'}. "
            f"Riferimento originale (primi 200 char): {content[:200]}"
        )[:600],
        "scenarios_signature": str(extracted.get("scenarios_signature") or "user_advice")[:48],
        "source_advice_count": 1,
    }

    try:
        cid = coach_cards.save_card(card)
        return {"saved": True, "id": cid, "card": card,
                "extraction_method": "ai" if extracted else "fallback"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ─── Daily scenario generator: ingestione scenari da GitHub Actions ────────

class DynamicScenarioPayload(BaseModel):
    """Payload di un singolo scenario inviato dal generator quotidiano."""
    id: str
    category: str        # 'normale' | 'geopolitico' | 'macro' | 'crash_rally'
    title: str
    brief: str
    period_start: str | None = None
    period_end: str | None = None
    asset_universe: list[str]
    headlines: list[str]
    market_data: list[dict]   # [{ticker, price_t0, change_24h, change_7d}]
    description_reveal: str
    source: str = "dynamic"   # tag origin (es. "dynamic_2026-05-06")
    expires_at: str | None = None  # ISO datetime, default +30 giorni


class DynamicScenarioBatch(BaseModel):
    scenarios: list[DynamicScenarioPayload]


def _validate_scenario(s: DynamicScenarioPayload) -> tuple[bool, str]:
    """Validazione qualitativa: rifiuta scenari malformati."""
    if not s.id or not s.category or not s.title:
        return False, "id/category/title obbligatori"
    if s.category not in ("normale", "geopolitico", "macro", "crash_rally"):
        return False, f"category '{s.category}' non valida"
    if len(s.asset_universe) < 5:
        return False, f"asset_universe troppo corto ({len(s.asset_universe)} < 5)"
    if len(s.headlines) < 3:
        return False, f"headlines insufficienti ({len(s.headlines)} < 3)"
    if len(s.market_data) < 5:
        return False, f"market_data insufficiente ({len(s.market_data)} < 5)"
    # Ogni market_data deve avere price_t0 valido
    for md in s.market_data:
        if not md.get("ticker") or not isinstance(md.get("price_t0"), (int, float)):
            return False, f"market_data malformato: {md}"
        if md["price_t0"] <= 0:
            return False, f"price_t0 non positivo per {md.get('ticker')}"
    if not s.description_reveal or len(s.description_reveal) < 30:
        return False, "description_reveal troppo breve (< 30 char)"
    return True, ""


@app.post("/api/simulator/scenarios/dynamic")
async def upload_dynamic_scenarios(
    batch: DynamicScenarioBatch,
    request: Request,
):
    """
    Ingestione di scenari dinamici prodotti dal generator giornaliero
    (GitHub Actions). Auth: header X-Scenario-Token confrontato con
    env SCENARIO_UPLOAD_TOKEN.

    Salva su sim_settings come `_sim_scenario::{id}` + indice dei dynamic
    in `_sim_scenario::dynamic_index`. La libreria scenarios.py legge
    automaticamente static + dynamic via merge.
    """
    # Auth
    expected_token = os.environ.get("SCENARIO_UPLOAD_TOKEN", "").strip()
    if not expected_token:
        return JSONResponse(status_code=503, content={
            "error": "SCENARIO_UPLOAD_TOKEN non configurato sul server",
        })
    provided = request.headers.get("X-Scenario-Token", "").strip()
    if not provided or provided != expected_token:
        return JSONResponse(status_code=401, content={
            "error": "Token mancante o non valido",
        })

    if not batch.scenarios:
        return {"accepted": 0, "rejected": 0, "rejected_reasons": []}

    # Default expires_at: +30 giorni
    default_expiry = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()

    accepted: list[str] = []
    rejected: list[dict] = []

    try:
        from simulator import db as sim_db
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"sim_db import: {e}"})

    for sc in batch.scenarios:
        ok, err = _validate_scenario(sc)
        if not ok:
            rejected.append({"id": sc.id, "reason": err})
            continue
        scenario_dict = sc.model_dump()
        if not scenario_dict.get("expires_at"):
            scenario_dict["expires_at"] = default_expiry
        scenario_dict["uploaded_at"] = datetime.now(timezone.utc).isoformat()
        try:
            sim_db.set_setting(
                f"_sim_scenario::{sc.id}",
                json.dumps(scenario_dict, default=str, ensure_ascii=False),
            )
            accepted.append(sc.id)
        except Exception as e:
            rejected.append({"id": sc.id, "reason": f"persistence error: {e}"})

    # Aggiorna l'indice dei dynamic scenarios
    try:
        idx_raw = sim_db.get_setting("_sim_scenario::dynamic_index", "[]")
        idx = json.loads(idx_raw) if isinstance(idx_raw, str) else (idx_raw or [])
        if not isinstance(idx, list):
            idx = []
        for aid in accepted:
            if aid not in idx:
                idx.append(aid)
        # Cap a 200 dynamic scenarios totali — cleanup degli scaduti gestito dal merger
        sim_db.set_setting("_sim_scenario::dynamic_index",
                            json.dumps(idx[-200:], default=str))
    except Exception as e:
        logger.warning("Errore aggiornamento indice scenari dinamici: %s", e)

    logger.info("Dynamic scenarios upload: %d accepted, %d rejected",
                len(accepted), len(rejected))

    return {
        "accepted": len(accepted),
        "rejected": len(rejected),
        "accepted_ids": accepted,
        "rejected_reasons": rejected,
    }


@app.get("/api/simulator/scenarios/dynamic")
async def list_dynamic_scenarios():
    """Lista gli scenari dinamici attualmente attivi (non scaduti)."""
    try:
        from simulator import db as sim_db
        from simulator import scenarios as _sc
        items = _sc.load_dynamic_scenarios(strip_reveal=False)
        return {
            "count": len(items),
            "scenarios": items,
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


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


# ════════════════════════════════════════════════════════════════════════
# Direttive Utente — istruzioni a priorità massima per gli agenti AI
# ════════════════════════════════════════════════════════════════════════

@app.get("/api/settings/directives")
async def get_directives():
    """Direttive utente correnti (testo libero a priorità massima)."""
    try:
        from agents.decision import get_user_directives_text
        return {"text": get_user_directives_text()}
    except Exception as e:
        logger.error("get_directives error: %s", e)
        return {"text": ""}


class DirectivesUpdateReq(BaseModel):
    text: str


@app.post("/api/settings/directives")
async def set_directives(req: DirectivesUpdateReq):
    """
    Aggiorna le direttive utente. Testo libero (max ~5000 char) iniettato
    IN CIMA al system prompt di Decision, Decision Crypto, Simulator V2 ed
    Equity/Crypto come "PRIORITÀ MASSIMA".
    """
    text = (req.text or "").strip()
    if len(text) > 5000:
        text = text[:5000]
    try:
        database.set_setting("user_directives_text", text)
        return {"status": "ok", "length": len(text), "text": text}
    except Exception as e:
        logger.error("set_directives error: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/settings/risk-profile")
async def get_risk_profile():
    """
    Restituisce il profilo di rischio attivo + tutti i preset disponibili
    per la UI Settings (mostrare la tabella comparativa).
    """
    try:
        import risk_profile as rp
        return rp.list_profiles()
    except Exception as e:
        logger.error("get_risk_profile error: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e)})


class RiskProfileUpdateReq(BaseModel):
    profile: str   # "conservative" | "moderate" | "aggressive"


@app.post("/api/settings/risk-profile")
async def set_risk_profile(req: RiskProfileUpdateReq):
    """
    Cambia il profilo di rischio attivo. Verrà applicato ai prossimi run del
    Decision Agent (equity + crypto) come hard-constraint nel system prompt
    e in execute_trade per la validazione.
    """
    try:
        import risk_profile as rp
        saved_key = rp.set_active_profile_key(req.profile)
        return {
            "status": "ok",
            "active_key": saved_key,
            "profile": rp.get_active_profile(),
        }
    except Exception as e:
        logger.error("set_risk_profile error: %s", e, exc_info=True)
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


@app.post("/api/portfolio/history/cleanup")
async def cleanup_portfolio_history(
    threshold_pct: float = Query(default=25.0, ge=10.0, le=100.0),
    mode: str = Query(default="median", regex="^(median|wipe)$"),
):
    """
    Pulisce portfolio_snapshots rimuovendo righe con total_value anomalo.

    Modalità:
    - mode=median (default): calcola la median degli ultimi 90 giorni e
      cancella righe che deviano oltre threshold_pct%. Conservativo.
    - mode=wipe: cancella TUTTI gli snapshot. Usalo se il chart è completamente
      compromesso (più step-jump consecutivi che il filtro mediano non riesce
      a recuperare). Ricostruzione automatica dal prossimo polling.

    Operazione IRREVERSIBILE.
    """
    # Wipe mode: tabula rasa, ricostruzione dal prossimo snapshot
    if mode == "wipe":
        try:
            try:
                from db_supabase import _get_client
                client = _get_client()
                if client is None:
                    raise RuntimeError("Supabase client non disponibile")
                # Supabase non supporta DELETE senza filtro: usiamo neq("id", 0)
                # che matcha tutto (id è sempre > 0 con autoincrement).
                r = client.table("portfolio_snapshots").delete().neq("id", 0).execute()
                deleted = len(r.data or [])
                logger.info("Portfolio history WIPE: rimossi %d snapshot (Supabase)", deleted)
                return {"status": "ok", "mode": "wipe", "deleted": deleted, "backend": "supabase",
                        "message": f"Wipe completato: {deleted} snapshot rimossi. Il grafico si ricostruirà dal prossimo polling."}
            except Exception as ex_sb:
                import db_sqlite
                with db_sqlite.get_db() as conn:
                    cur = conn.execute("DELETE FROM portfolio_snapshots")
                    deleted = cur.rowcount or 0
                    conn.commit()
                logger.info("Portfolio history WIPE: rimossi %d snapshot (SQLite, supabase err=%s)",
                            deleted, ex_sb)
                return {"status": "ok", "mode": "wipe", "deleted": deleted, "backend": "sqlite",
                        "message": f"Wipe completato: {deleted} snapshot rimossi."}
        except Exception as e:
            logger.error("cleanup wipe error: %s", e, exc_info=True)
            return JSONResponse(status_code=500, content={"status": "error", "error": str(e)})
    try:
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td

        # 1. Carica tutti gli snapshot recenti (90 giorni) per calcolare median
        history = database.get_portfolio_history(days=90)
        if not history or len(history) < 5:
            return {"status": "skipped", "reason": "history vuoto o troppo piccolo",
                    "samples": len(history or [])}

        values = sorted([float(h["total_value"]) for h in history
                         if h.get("total_value") is not None
                         and float(h["total_value"]) > 0])
        if not values:
            return {"status": "skipped", "reason": "nessun valore valido"}
        m = len(values) // 2
        median_val = values[m] if len(values) % 2 else (values[m - 1] + values[m]) / 2.0
        upper = median_val * (1 + threshold_pct / 100.0)
        lower = median_val * (1 - threshold_pct / 100.0)

        # 2. DELETE diretto su Supabase (filtro by range total_value)
        # La tabella reale è `portfolio_snapshots` (non portfolio_history).
        deleted_total = 0
        backend = "unknown"
        try:
            from db_supabase import _get_client
            client = _get_client()
            if client is None:
                raise RuntimeError("Supabase client non disponibile")
            since_iso = (_dt.now(_tz.utc) - _td(days=90)).isoformat()
            # Cancella valori sopra l'upper
            r1 = (client.table("portfolio_snapshots")
                  .delete()
                  .gt("total_value", upper)
                  .gte("timestamp", since_iso)
                  .execute())
            deleted_total += len(r1.data or [])
            # Cancella valori sotto il lower
            r2 = (client.table("portfolio_snapshots")
                  .delete()
                  .lt("total_value", lower)
                  .gte("timestamp", since_iso)
                  .execute())
            deleted_total += len(r2.data or [])
            backend = "supabase"
        except Exception as ex_sb:
            # Fallback SQLite
            try:
                import db_sqlite
                with db_sqlite.get_db() as conn:
                    cur = conn.execute(
                        "DELETE FROM portfolio_snapshots "
                        "WHERE total_value > ? OR total_value < ?",
                        (upper, lower),
                    )
                    deleted_total = cur.rowcount or 0
                    conn.commit()
                backend = "sqlite"
            except Exception as ex_sql:
                logger.error("cleanup error (Supabase: %s, SQLite: %s)", ex_sb, ex_sql)
                return JSONResponse(status_code=500,
                    content={"status": "error", "error": f"supabase={ex_sb}; sqlite={ex_sql}"})

        logger.info("Portfolio history cleanup: rimossi %d outlier (backend=%s, "
                    "median=%.2f, range=[%.2f, %.2f])",
                    deleted_total, backend, median_val, lower, upper)

        return {
            "status": "ok",
            "deleted": deleted_total,
            "backend": backend,
            "median": round(median_val, 2),
            "upper_bound": round(upper, 2),
            "lower_bound": round(lower, 2),
            "threshold_pct": threshold_pct,
            "message": f"Rimossi {deleted_total} snapshot anomali. Ricarica il grafico Analytics.",
        }
    except Exception as e:
        logger.error("cleanup_portfolio_history error: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"status": "error", "error": str(e)})


# ============================================================
# Portfolio Audit & Rebuild
# ============================================================
# Quando lo stato del portfolio (cash + positions) diverge da quello
# che ci si aspetterebbe ricostruendo dai trade in `trades`, questi
# endpoint diagnosticano e riparano. Usati per il bug "+69k$ phantom"
# dove total_value mostra valori più alti del giustificabile dai trade.

def _replay_trades_from_history(initial_balance: float, trades_asc: list) -> dict:
    """
    Ricostruisce lo stato del portafoglio replicando i trade in ordine cronologico.
    Stessa logica di portfolio.execute_buy / execute_sell ma in-memory.

    Returns:
        {
            "cash": float,
            "positions": {ticker: {"quantity": float, "avg_buy_price": float}},
            "anomalies": [{trade_id, reason, ...}]
        }
    """
    cash = float(initial_balance)
    positions: dict = {}
    anomalies: list = []

    for t in trades_asc:
        try:
            ticker = (t.get("ticker") or "").upper()
            action = (t.get("action") or t.get("side") or "").upper()
            qty = float(t.get("quantity") or 0)
            price = float(t.get("price") or 0)
            tid = t.get("id") or t.get("trade_id")

            if not ticker or qty <= 0 or price <= 0:
                anomalies.append({
                    "trade_id": tid, "ticker": ticker, "action": action,
                    "quantity": qty, "price": price,
                    "reason": "Trade malformato (qty/price <= 0 o ticker mancante)",
                    "timestamp": t.get("timestamp"),
                })
                continue

            value = qty * price

            if action == "BUY":
                # Sanity check: BUY > 90% del cash al momento è sospetto
                if cash > 0 and value > cash * 0.9:
                    anomalies.append({
                        "trade_id": tid, "ticker": ticker, "action": action,
                        "quantity": qty, "price": price, "value": round(value, 2),
                        "cash_before": round(cash, 2),
                        "reason": f"BUY anomalo: {value:.0f}$ con cash {cash:.0f}$ "
                                  f"({100*value/cash:.0f}% del cash)",
                        "timestamp": t.get("timestamp"),
                    })

                if value > cash:
                    # Trade impossibile: avrebbe portato cash negativo
                    anomalies.append({
                        "trade_id": tid, "ticker": ticker, "action": action,
                        "quantity": qty, "price": price, "value": round(value, 2),
                        "cash_before": round(cash, 2),
                        "reason": f"IMPOSSIBILE: BUY {value:.2f}$ con cash {cash:.2f}$ "
                                  f"(cash negativo a {cash - value:.2f}$). Trade ignorato nel replay.",
                        "timestamp": t.get("timestamp"),
                        "skipped": True,
                    })
                    continue

                cash -= value
                if ticker in positions:
                    p = positions[ticker]
                    old_total = p["avg_buy_price"] * p["quantity"]
                    new_qty = p["quantity"] + qty
                    p["avg_buy_price"] = (old_total + value) / new_qty if new_qty > 0 else price
                    p["quantity"] = new_qty
                else:
                    positions[ticker] = {"quantity": qty, "avg_buy_price": price}

            elif action == "SELL":
                p = positions.get(ticker)
                if p is None or p["quantity"] < qty - 1e-9:
                    anomalies.append({
                        "trade_id": tid, "ticker": ticker, "action": action,
                        "quantity": qty, "price": price,
                        "reason": f"SELL impossibile: posseduti "
                                  f"{p['quantity'] if p else 0} {ticker}, venduti {qty}",
                        "timestamp": t.get("timestamp"),
                        "skipped": True,
                    })
                    continue

                cash += value
                p["quantity"] -= qty
                if p["quantity"] <= 1e-9:
                    del positions[ticker]
            else:
                anomalies.append({
                    "trade_id": tid, "ticker": ticker, "action": action,
                    "reason": f"Action sconosciuta: {action!r}",
                    "timestamp": t.get("timestamp"),
                })
        except Exception as ex:
            anomalies.append({
                "trade_id": t.get("id"), "reason": f"Errore replay: {ex}",
                "timestamp": t.get("timestamp"),
            })

    return {"cash": cash, "positions": positions, "anomalies": anomalies}


@app.get("/api/portfolio/audit")
async def portfolio_audit():
    """
    Audit forensico del portafoglio: ricostruisce lo stato (cash + positions)
    dai trade storici e lo confronta con lo stato attuale del DB.

    Risponde alla domanda: "I $169k attuali sono giustificati dai trade
    eseguiti, o c'è uno scostamento (bug)?"

    Read-only — non modifica nulla.
    """
    try:
        # 1. Carica tutto: trades, current portfolio, current positions
        trades_desc = database.get_trades(limit=10000) or []
        # get_trades ritorna DESC, ribaltiamo a ASC per il replay cronologico
        trades_asc = list(reversed(trades_desc))

        portfolio = database.get_portfolio() or {}
        current_cash = float(portfolio.get("cash_balance") or 0)
        current_total = float(portfolio.get("total_value") or 0)

        ib_str = database.get_setting("initial_balance", "100000") or "100000"
        try:
            initial_balance = float(ib_str)
        except (ValueError, TypeError):
            initial_balance = 100000.0

        positions = database.get_positions() or []

        # 2. Replay cronologico
        replay = _replay_trades_from_history(initial_balance, trades_asc)
        reconstructed_cash = replay["cash"]
        reconstructed_positions = replay["positions"]
        anomalies = replay["anomalies"]

        # 3. Compute reconstructed total_value usando current_price delle position attuali
        current_prices = {p["ticker"]: float(p.get("current_price") or 0) for p in positions}
        reconstructed_position_value = 0.0
        for tk, rp in reconstructed_positions.items():
            cp = current_prices.get(tk, rp["avg_buy_price"])
            reconstructed_position_value += cp * rp["quantity"]
        reconstructed_total = reconstructed_cash + reconstructed_position_value

        # 4. Diff position-by-position
        position_diffs = []
        current_pos_map = {p["ticker"]: p for p in positions}
        all_tickers = set(current_pos_map.keys()) | set(reconstructed_positions.keys())
        for tk in sorted(all_tickers):
            cur = current_pos_map.get(tk)
            rec = reconstructed_positions.get(tk)
            cur_qty = float(cur.get("quantity") or 0) if cur else 0.0
            rec_qty = rec["quantity"] if rec else 0.0
            cur_avg = float(cur.get("avg_buy_price") or 0) if cur else 0.0
            rec_avg = rec["avg_buy_price"] if rec else 0.0
            qty_delta = cur_qty - rec_qty
            avg_delta = cur_avg - rec_avg
            if abs(qty_delta) > 1e-6 or abs(avg_delta) > 0.01:
                position_diffs.append({
                    "ticker": tk,
                    "current_quantity": round(cur_qty, 8),
                    "reconstructed_quantity": round(rec_qty, 8),
                    "quantity_delta": round(qty_delta, 8),
                    "current_avg_price": round(cur_avg, 2),
                    "reconstructed_avg_price": round(rec_avg, 2),
                    "avg_price_delta": round(avg_delta, 2),
                })

        # 4b. Per-position breakdown — chi contribuisce di più al total_value?
        # Utile per beccare positions con current_price gonfiato che inflano
        # il calcolo cash + sum(qty*current_price).
        position_breakdown = []
        for p in positions:
            qty = float(p.get("quantity") or 0)
            cp = float(p.get("current_price") or 0)
            avg = float(p.get("avg_buy_price") or 0)
            mkt_value = cp * qty
            cost_basis = avg * qty
            # Flag se current_price devia >50% dall'avg_buy_price (sospetto)
            price_drift_pct = ((cp - avg) / avg * 100) if avg > 0 else 0
            position_breakdown.append({
                "ticker": p.get("ticker"),
                "quantity": round(qty, 8),
                "avg_buy_price": round(avg, 2),
                "current_price": round(cp, 2),
                "market_value": round(mkt_value, 2),
                "cost_basis": round(cost_basis, 2),
                "unrealized_pnl": round(mkt_value - cost_basis, 2),
                "price_drift_pct": round(price_drift_pct, 1),
                "suspicious": abs(price_drift_pct) > 50,  # >50% drift = sospetto
            })
        position_breakdown.sort(key=lambda x: -x["market_value"])

        cash_delta = current_cash - reconstructed_cash
        total_delta = current_total - reconstructed_total

        # 5. Verdetto
        if abs(cash_delta) < 1.0 and abs(total_delta) < 1.0 and not position_diffs:
            verdict = "CLEAN"
            verdict_message = ("Portafoglio coerente: cash + posizioni attuali "
                               "matchano la replica dai trade.")
        elif abs(total_delta) > 100:
            verdict = "DIVERGENT"
            verdict_message = (
                f"DIVERGENZA RILEVATA: total_value attuale ${current_total:,.2f} vs "
                f"ricostruito ${reconstructed_total:,.2f} "
                f"(delta {'+' if total_delta >= 0 else ''}{total_delta:,.2f}$). "
                f"I {'+' if total_delta >= 0 else ''}{total_delta:,.0f}$ NON sono giustificati "
                f"dai trade nel DB."
            )
        else:
            verdict = "MINOR_DRIFT"
            verdict_message = (
                f"Lieve drift: delta {total_delta:,.2f}$. "
                "Probabile arrotondamento o trade pendente."
            )

        return {
            "verdict": verdict,
            "verdict_message": verdict_message,
            "initial_balance": round(initial_balance, 2),
            "trades_count": len(trades_asc),
            "anomalies_count": len(anomalies),
            "current": {
                "cash": round(current_cash, 2),
                "total_value": round(current_total, 2),
                "positions_count": len(positions),
            },
            "reconstructed": {
                "cash": round(reconstructed_cash, 2),
                "position_value": round(reconstructed_position_value, 2),
                "total_value": round(reconstructed_total, 2),
                "positions_count": len(reconstructed_positions),
            },
            "deltas": {
                "cash": round(cash_delta, 2),
                "total_value": round(total_delta, 2),
            },
            "position_diffs": position_diffs,
            "position_breakdown": position_breakdown,
            "anomalies": anomalies[:50],  # limit per response size
        }
    except Exception as e:
        logger.error("portfolio_audit error: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"status": "error", "error": str(e)})


@app.post("/api/portfolio/refresh-prices")
async def portfolio_refresh_prices():
    """
    Forza refresh dei current_price di TUTTE le posizioni aperte chiamando
    direttamente il data fetcher (yfinance/Polygon). Usa il sanity check
    standard (rifiuta variazioni >40% vs prev_close).

    Risolve il caso in cui calculate_total_value() restituisce un valore
    gonfiato perché qualche posizione ha un current_price stale/sbagliato
    nel DB.

    Dopo il refresh, ricalcola portfolio.total_value.
    """
    try:
        import data_fetchers
        positions = database.get_positions() or []
        if not positions:
            return {"status": "ok", "message": "Nessuna posizione da aggiornare", "updated": 0}

        updated, failed, suspicious = 0, [], []
        for p in positions:
            ticker = p.get("ticker")
            if not ticker:
                continue
            old_price = float(p.get("current_price") or 0)
            try:
                quote = data_fetchers.fetch_market_data(ticker, period_days=2) or {}
                data = quote.get("data") or []
                # data_fetchers ritorna lista di candele OHLC con campo "close"
                new_price = 0.0
                if data:
                    last = data[-1]
                    new_price = float(last.get("close") or last.get("c") or 0)
                if new_price <= 0:
                    failed.append({"ticker": ticker, "reason": "no price returned"})
                    continue

                # Sanity check: rifiuta variazione >40% vs old price (probabile bad data)
                if old_price > 0:
                    ratio = new_price / old_price
                    if ratio < 0.6 or ratio > 1.4:
                        suspicious.append({
                            "ticker": ticker,
                            "old_price": round(old_price, 2),
                            "new_price": round(new_price, 2),
                            "ratio": round(ratio, 2),
                            "action": "rejected_kept_old",
                        })
                        continue

                database.update_position_price(ticker, new_price)
                updated += 1
            except Exception as ex:
                failed.append({"ticker": ticker, "reason": str(ex)[:100]})

        # Forza ricalcolo portfolio.total_value (calculate_total_value scrive il valore in DB)
        import portfolio
        new_total = portfolio.calculate_total_value()
        new_portfolio = database.get_portfolio() or {}

        return {
            "status": "ok",
            "message": f"Refresh completato: {updated} posizioni aggiornate, "
                       f"{len(failed)} fallite, {len(suspicious)} sospette (rifiutate).",
            "updated": updated,
            "failed": failed,
            "suspicious": suspicious,
            "new_total_value": round(new_total, 2),
            "new_cash": round(float(new_portfolio.get("cash_balance") or 0), 2),
        }
    except Exception as e:
        logger.error("portfolio_refresh_prices error: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"status": "error", "error": str(e)})


@app.get("/api/portfolio/db-diagnostic")
async def portfolio_db_diagnostic():
    """
    Diagnostica completa scritture DB. Risponde alla domanda:
    "perche' gli agenti non riescono a eseguire trade?"

    Test:
    1. Insert trade con quantity frazionale 0.5 — fallisce se INTEGER
    2. Insert position con quantity frazionale 0.25 — idem
    3. Update portfolio
    4. Cleanup test rows

    Le righe di test usano ticker "TEST_DIAG_<timestamp>" e vengono rimosse.
    """
    import time as _time
    results = []
    test_ticker = f"TEST_DIAG_{int(_time.time())}"
    trade_id = None

    try:
        from db_supabase import _get_client
        client = _get_client()
    except Exception as e:
        return {
            "verdict": "ERROR_DB_CONNECTION",
            "verdict_message": f"Impossibile connettersi a Supabase: {e}",
            "results": [{"test": "DB connection", "status": "error", "error": str(e)}],
        }

    # Test 1: trade con quantity frazionale
    try:
        result = client.table("trades").insert({
            "ticker": test_ticker, "action": "BUY",
            "quantity": 0.5, "price": 100.0, "total_value": 50.0,
            "geopolitical_reasoning": "diagnostic test",
            "technical_reasoning": "diagnostic test",
            "final_decision": "diagnostic", "confidence_score": 50,
        }).execute()
        if result.data:
            trade_id = result.data[0].get("id")
            results.append({"test": "1. Insert trade quantity=0.5", "status": "ok",
                            "message": "Migration v11 NUMERIC applicata correttamente.",
                            "trade_id": trade_id})
        else:
            results.append({"test": "1. Insert trade quantity=0.5", "status": "warn",
                            "message": "Insert ritorna data vuoto (no error)."})
    except Exception as e:
        err_str = str(e)[:600]
        is_integer_bug = "invalid input syntax for type integer" in err_str.lower()
        results.append({
            "test": "1. Insert trade quantity=0.5", "status": "error",
            "error": err_str, "is_integer_migration_bug": is_integer_bug,
        })

    # Test 2: position con quantity frazionale
    try:
        client.table("positions").insert({
            "ticker": test_ticker, "quantity": 0.25,
            "avg_buy_price": 100.0, "current_price": 100.0, "unrealized_pnl": 0.0,
        }).execute()
        results.append({"test": "2. Insert position quantity=0.25", "status": "ok"})
    except Exception as e:
        err_str = str(e)[:600]
        is_integer_bug = "invalid input syntax for type integer" in err_str.lower()
        results.append({
            "test": "2. Insert position quantity=0.25", "status": "error",
            "error": err_str, "is_integer_migration_bug": is_integer_bug,
        })

    # Test 3: update portfolio
    try:
        portfolio_now = database.get_portfolio() or {}
        cur_cash = float(portfolio_now.get("cash_balance") or 0)
        cur_total = float(portfolio_now.get("total_value") or 0)
        database.update_portfolio(cur_cash, cur_total)
        results.append({"test": "3. Update portfolio", "status": "ok"})
    except Exception as e:
        results.append({"test": "3. Update portfolio", "status": "error",
                        "error": str(e)[:500]})

    # Cleanup
    try:
        if trade_id:
            client.table("trades").delete().eq("id", trade_id).execute()
        client.table("positions").delete().eq("ticker", test_ticker).execute()
    except Exception as e:
        results.append({"test": "4. Cleanup test rows", "status": "warn",
                        "error": str(e)[:200],
                        "manual_action": f"DELETE FROM trades WHERE id={trade_id}; DELETE FROM positions WHERE ticker='{test_ticker}';"})

    has_integer_bug = any(r.get("is_integer_migration_bug") for r in results)
    has_errors = any(r["status"] == "error" for r in results)

    if has_integer_bug:
        verdict = "MIGRATION_V11_NOT_APPLIED"
        verdict_message = (
            "Migration v11 (quantity INTEGER → NUMERIC) NON e' stata applicata. "
            "Gli agenti non possono eseguire trade con quantity frazionali. "
            "Soluzione: configurare DATABASE_URL (o SUPABASE_DB_PASSWORD + "
            "SUPABASE_URL) su Render → Environment, poi premere 'Esegui migration'."
        )
    elif has_errors:
        verdict = "DB_WRITE_ERROR"
        verdict_message = "Errori scritture DB rilevati — vedi dettaglio per ogni test."
    else:
        verdict = "OK"
        verdict_message = "Tutti i test passati. Il DB accetta scritture frazionali correttamente."

    return {"verdict": verdict, "verdict_message": verdict_message, "results": results}


@app.post("/api/portfolio/run-migrations")
async def portfolio_run_migrations():
    """
    Triggera manualmente _ensure_schema_migrations() che applica le ALTER
    TABLE pending (v11 quantity NUMERIC, v10 SL/TP, v9 chat tables, etc.).

    Idempotente. Richiede DATABASE_URL o (SUPABASE_DB_PASSWORD + SUPABASE_URL)
    configurato su Render → Environment Variables.
    """
    import os as _os
    db_url = _os.environ.get("DATABASE_URL", "").strip()
    db_pass = _os.environ.get("SUPABASE_DB_PASSWORD", "").strip()
    if not db_url and not db_pass:
        return JSONResponse(status_code=400, content={
            "status": "error",
            "error": "Né DATABASE_URL né SUPABASE_DB_PASSWORD sono configurati.",
            "hint": (
                "Render → Environment → aggiungi una di queste:\n"
                "  - DATABASE_URL = postgresql://postgres.<ref>:<pwd>@aws-0-eu-central-1.pooler.supabase.com:6543/postgres\n"
                "  - SUPABASE_DB_PASSWORD = la password del DB Postgres\n"
                "Poi redeploy e re-triggera questa migration."
            ),
        })

    try:
        from db_supabase import _ensure_schema_migrations
        _ensure_schema_migrations()
        return {
            "status": "ok",
            "message": (
                "Migration triggered. Esegui ora 'Diagnostica DB' per "
                "confermare che il DB accetti quantity frazionali."
            ),
            "has_database_url": bool(db_url),
            "has_db_password": bool(db_pass),
        }
    except Exception as e:
        logger.error("run-migrations error: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={
            "status": "error",
            "error": str(e),
            "hint": "Errore durante l'esecuzione delle migration. Verifica i log Render.",
        })


@app.post("/api/portfolio/adjust-cash")
async def portfolio_adjust_cash(
    delta: float = Query(..., description="Delta da applicare al cash_balance (positivo aggiunge, negativo sottrae)"),
    confirm: bool = Query(default=False),
):
    """
    Aggiusta cash_balance di un delta. Le posizioni non vengono toccate.
    total_value viene ricalcolato come nuovo cash + sum(qty * current_price).

    Subito dopo l'aggiornamento scrive UNO SNAPSHOT del nuovo stato in
    portfolio_snapshots, cosi' il chart riflette immediatamente il valore
    coerente col dashboard (non aspetta il prossimo polling tick).

    Richiede confirm=true. IRREVERSIBILE.
    """
    if not confirm:
        return JSONResponse(status_code=400, content={
            "status": "error",
            "error": "Richiede confirm=true. Operazione irreversibile.",
        })

    try:
        portfolio_now = database.get_portfolio() or {}
        old_cash = float(portfolio_now.get("cash_balance") or 0)
        old_total = float(portfolio_now.get("total_value") or 0)
        new_cash = old_cash + delta
        if new_cash < 0:
            return JSONResponse(status_code=400, content={
                "status": "error",
                "error": f"Delta {delta:+.2f}$ porterebbe cash a {new_cash:.2f}$ (negativo). Aborted.",
                "old_cash": round(old_cash, 2),
            })

        # Ricalcola total con le posizioni attuali
        positions = database.get_positions() or []
        positions_value = sum(
            float(p.get("current_price") or 0) * float(p.get("quantity") or 0)
            for p in positions
            if float(p.get("current_price") or 0) > 0
        )
        new_total = new_cash + positions_value

        database.update_portfolio(round(new_cash, 2), round(new_total, 2))

        # Scrivi un snapshot subito per allineare il chart
        try:
            database.insert_portfolio_snapshot(round(new_total, 2), round(new_cash, 2))
        except Exception as ex_snap:
            logger.warning("adjust_cash: insert_portfolio_snapshot fallito: %s", ex_snap)

        logger.info(
            "Portfolio cash ADJUSTED: delta=%+.2f, cash %.2f→%.2f, total %.2f→%.2f",
            delta, old_cash, new_cash, old_total, new_total,
        )

        return {
            "status": "ok",
            "message": (
                f"Liquidità aggiustata di {delta:+,.2f}$. "
                f"Cash: ${old_cash:,.2f} → ${new_cash:,.2f}. "
                f"Total: ${old_total:,.2f} → ${new_total:,.2f}. "
                f"Snapshot scritto."
            ),
            "delta": round(delta, 2),
            "old_cash": round(old_cash, 2),
            "new_cash": round(new_cash, 2),
            "old_total": round(old_total, 2),
            "new_total": round(new_total, 2),
            "positions_value": round(positions_value, 2),
        }
    except Exception as e:
        logger.error("portfolio_adjust_cash error: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"status": "error", "error": str(e)})


@app.get("/api/portfolio/cash-history")
async def portfolio_cash_history(days: int = Query(default=3, ge=1, le=30)):
    """
    Ritorna lo storico di cash_balance dai portfolio_snapshots con timestamp.
    Utile per identificare il valore del cash PRIMA che si "buggasse" e
    decidere a quale punto restorare.

    Read-only.
    """
    try:
        history = database.get_portfolio_history(days=days) or []
        # Downsample a max 200 punti per leggibilità
        if len(history) > 200:
            step = max(1, len(history) // 200)
            history = history[::step]

        rows = []
        cash_values = []
        for r in history:
            cash = float(r.get("cash_balance") or 0)
            tv = float(r.get("total_value") or 0)
            ts = r.get("timestamp")
            rows.append({
                "timestamp": ts,
                "cash_balance": round(cash, 2),
                "total_value": round(tv, 2),
            })
            if cash > 0:
                cash_values.append(cash)

        # Suggerimento automatico: cash più frequente nelle prime 75% dello storico
        # (assumendo che il bug sia recente, escludiamo l'ultimo quarto).
        suggested_cash = None
        if len(cash_values) >= 5:
            cutoff = max(1, int(len(cash_values) * 0.75))
            stable_segment = sorted(cash_values[:cutoff])
            mid = len(stable_segment) // 2
            suggested_cash = round(
                stable_segment[mid] if len(stable_segment) % 2 == 1
                else (stable_segment[mid - 1] + stable_segment[mid]) / 2.0,
                2,
            )

        return {
            "status": "ok",
            "days": days,
            "samples": len(rows),
            "history": rows,
            "suggested_cash_pre_bug": suggested_cash,
            "current_cash": round(float((database.get_portfolio() or {}).get("cash_balance") or 0), 2),
        }
    except Exception as e:
        logger.error("portfolio_cash_history error: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"status": "error", "error": str(e)})


@app.post("/api/portfolio/restore-cash")
async def portfolio_restore_cash(
    cash: float = Query(..., gt=0, description="Valore cash_balance da ripristinare"),
    confirm: bool = Query(default=False),
):
    """
    Ripristina cash_balance a un valore noto (preso da portfolio_snapshots
    pre-bug). Le posizioni non vengono toccate. total_value viene ricalcolato
    da calculate_total_value() = nuovo cash + sum(qty * current_price).

    Richiede confirm=true. IRREVERSIBILE.
    """
    if not confirm:
        return JSONResponse(status_code=400, content={
            "status": "error",
            "error": "Richiede confirm=true. Operazione irreversibile.",
        })

    try:
        portfolio_now = database.get_portfolio() or {}
        old_cash = float(portfolio_now.get("cash_balance") or 0)
        old_total = float(portfolio_now.get("total_value") or 0)

        # Calcola nuovo total: cash ripristinato + sum posizioni
        positions = database.get_positions() or []
        positions_value = sum(
            float(p.get("current_price") or 0) * float(p.get("quantity") or 0)
            for p in positions
            if float(p.get("current_price") or 0) > 0
        )
        new_total = cash + positions_value

        database.update_portfolio(round(cash, 2), round(new_total, 2))

        logger.info(
            "Portfolio cash RESTORE: cash %.2f→%.2f, total %.2f→%.2f, positions_value=%.2f",
            old_cash, cash, old_total, new_total, positions_value,
        )

        return {
            "status": "ok",
            "message": (
                f"Cash ripristinato: ${old_cash:,.2f} → ${cash:,.2f}. "
                f"Total: ${old_total:,.2f} → ${new_total:,.2f}."
            ),
            "old_cash": round(old_cash, 2),
            "new_cash": round(cash, 2),
            "old_total": round(old_total, 2),
            "new_total": round(new_total, 2),
            "positions_value": round(positions_value, 2),
            "positions_count": len(positions),
        }
    except Exception as e:
        logger.error("portfolio_restore_cash error: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"status": "error", "error": str(e)})


@app.post("/api/portfolio/set-target-total")
async def portfolio_set_target_total(
    target_total: float = Query(..., gt=0, description="Valore target in $ del patrimonio totale"),
    confirm: bool = Query(default=False),
):
    """
    Aggiusta il cash_balance per ottenere un total_value pari a `target_total`.

    Calcola: cash_balance = target_total - sum(qty * current_price) per ogni posizione.

    Use case: la liquidita' si e' "buggata" e adesso il calcolo
    cash + sum(positions) = X non corrisponde al patrimonio reale Y.
    Tu sai che dovrebbe essere Y → questo endpoint forza cash al delta giusto.

    Le posizioni (quantity, avg_buy_price, current_price) NON vengono toccate.
    Solo cash_balance viene aggiornato.

    Richiede confirm=true. IRREVERSIBILE.
    """
    if not confirm:
        return JSONResponse(status_code=400, content={
            "status": "error",
            "error": "Richiede confirm=true. Operazione irreversibile.",
        })

    try:
        positions = database.get_positions() or []
        portfolio_now = database.get_portfolio() or {}
        old_cash = float(portfolio_now.get("cash_balance") or 0)
        old_total = float(portfolio_now.get("total_value") or 0)

        # Somma valore di mercato delle posizioni
        positions_value = 0.0
        breakdown = []
        for p in positions:
            qty = float(p.get("quantity") or 0)
            cp = float(p.get("current_price") or 0)
            if qty <= 0 or cp <= 0:
                continue
            mv = qty * cp
            positions_value += mv
            breakdown.append({
                "ticker": p.get("ticker"),
                "quantity": round(qty, 8),
                "current_price": round(cp, 2),
                "market_value": round(mv, 2),
            })

        new_cash = target_total - positions_value
        if new_cash < 0:
            return JSONResponse(status_code=400, content={
                "status": "error",
                "error": (
                    f"Target ${target_total:,.2f} impossibile: somma posizioni "
                    f"${positions_value:,.2f} > target. cash_balance risulterebbe "
                    f"negativo (${new_cash:,.2f}). Riduci posizioni o alza il target."
                ),
                "positions_value": round(positions_value, 2),
                "would_be_cash": round(new_cash, 2),
            })

        # Aggiorna portfolio
        database.update_portfolio(round(new_cash, 2), round(target_total, 2))

        logger.info(
            "Portfolio target_total set: target=%.2f, cash %.2f→%.2f, total %.2f→%.2f, "
            "positions_value=%.2f",
            target_total, old_cash, new_cash, old_total, target_total, positions_value,
        )

        return {
            "status": "ok",
            "message": (
                f"Cash bilanciato: ${old_cash:,.2f} → ${new_cash:,.2f}. "
                f"Total value: ${old_total:,.2f} → ${target_total:,.2f}."
            ),
            "old_cash": round(old_cash, 2),
            "new_cash": round(new_cash, 2),
            "old_total": round(old_total, 2),
            "new_total": round(target_total, 2),
            "positions_value": round(positions_value, 2),
            "positions_count": len(breakdown),
            "breakdown": breakdown,
        }
    except Exception as e:
        logger.error("portfolio_set_target_total error: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"status": "error", "error": str(e)})


@app.post("/api/portfolio/rebuild")
async def portfolio_rebuild(confirm: bool = Query(default=False)):
    """
    Ricostruisce lo stato del portfolio (cash + positions) replicando i trade
    dall'inizio. SOSTITUISCE lo stato attuale.

    OPERAZIONE IRREVERSIBILE — richiede confirm=true esplicito.

    Trade malformati (qty/price ≤ 0) o impossibili (BUY > cash, SELL > posseduti)
    vengono SCARTATI nel replay → la ricostruzione potrebbe risultare diversa
    dallo stato attuale anche se quello attuale era "intenzionalmente" buggy.

    I current_price delle posizioni attuali vengono PRESERVATI dove possibile.
    """
    if not confirm:
        return JSONResponse(status_code=400, content={
            "status": "error",
            "error": "Richiede confirm=true. Operazione irreversibile.",
        })

    try:
        trades_desc = database.get_trades(limit=10000) or []
        trades_asc = list(reversed(trades_desc))

        ib_str = database.get_setting("initial_balance", "100000") or "100000"
        try:
            initial_balance = float(ib_str)
        except (ValueError, TypeError):
            initial_balance = 100000.0

        positions = database.get_positions() or []
        current_prices = {p["ticker"]: float(p.get("current_price") or 0) for p in positions}

        replay = _replay_trades_from_history(initial_balance, trades_asc)
        rec_cash = replay["cash"]
        rec_positions = replay["positions"]

        # 1. Sostituisci tutte le positions
        # 1a. Elimina le positions correnti che non esistono nella ricostruzione
        existing_tickers = {p["ticker"] for p in positions}
        rec_tickers = set(rec_positions.keys())

        deleted = 0
        for tk in existing_tickers - rec_tickers:
            try:
                database.delete_position(tk)
                deleted += 1
            except Exception as ex:
                logger.warning("rebuild: delete_position(%s) fallito: %s", tk, ex)

        # 1b. Upsert delle position ricostruite
        upserted = 0
        for tk, rp in rec_positions.items():
            try:
                cp = current_prices.get(tk, rp["avg_buy_price"])
                database.upsert_position(tk, rp["quantity"], rp["avg_buy_price"], cp)
                upserted += 1
            except Exception as ex:
                logger.warning("rebuild: upsert_position(%s) fallito: %s", tk, ex)

        # 2. Compute reconstructed total_value e aggiorna portfolio
        rec_position_value = sum(
            current_prices.get(tk, rp["avg_buy_price"]) * rp["quantity"]
            for tk, rp in rec_positions.items()
        )
        rec_total = rec_cash + rec_position_value
        try:
            database.update_portfolio(round(rec_cash, 2), round(rec_total, 2))
        except Exception as ex:
            logger.error("rebuild: update_portfolio fallito: %s", ex)
            return JSONResponse(status_code=500, content={
                "status": "error", "error": f"update_portfolio: {ex}"})

        logger.info(
            "Portfolio REBUILD completato: cash %.2f, %d position upserted, %d delete, "
            "anomalies=%d",
            rec_cash, upserted, deleted, len(replay["anomalies"]),
        )

        return {
            "status": "ok",
            "message": "Portfolio ricostruito dai trade. Lo stato è ora coerente con lo storico.",
            "cash": round(rec_cash, 2),
            "total_value": round(rec_total, 2),
            "positions_upserted": upserted,
            "positions_deleted": deleted,
            "anomalies_skipped": sum(1 for a in replay["anomalies"] if a.get("skipped")),
            "anomalies_total": len(replay["anomalies"]),
        }
    except Exception as e:
        logger.error("portfolio_rebuild error: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"status": "error", "error": str(e)})


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


@app.get("/api/scout/sources-health")
async def scout_sources_health():
    """
    Diagnostica: testa ogni fonte Scout in parallelo e ritorna status,
    count items, error e env vars necessarie. Usato dal frontend (Settings)
    per mostrare quali fonti funzionano e quali sono down/non configurate.
    """
    import data_fetchers as _df

    sources_spec = [
        ("GDELT", _df.fetch_gdelt_data, []),
        ("NEWSAPI", _df.fetch_newsapi_data, ["NEWS_API_KEY"]),
        ("YFINANCE_NEWS", _df.fetch_yfinance_news, []),
        ("REDDIT", _df.fetch_reddit_sentiment, ["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"]),
        ("X_TWITTER", _df.fetch_x_sentiment, ["X_BEARER_TOKEN"]),
        ("CONGRESSIONAL", _df.fetch_congressional_trades, []),
        ("COINGECKO", _df.fetch_coingecko_data, []),
    ]

    async def _probe(name, fn, env_keys):
        env_status = {k: ("set" if os.environ.get(k) else "missing") for k in env_keys}
        try:
            result = await fn()
            count = 0
            error = None
            if isinstance(result, dict):
                if result.get("error"):
                    error = str(result.get("error"))[:200]
                count = (len(result.get("items", []))
                         or len(result.get("articles", []))
                         or len(result.get("posts", []))
                         or len(result.get("tweets", []))
                         or len(result.get("trades", []))
                         or len(result.get("data", [])))
            elif hasattr(result, "__len__"):
                count = len(result)
            status = "ok" if (count > 0 and not error) else ("warn" if not error else "error")
            return {
                "name": name, "status": status, "items_count": count,
                "error": error, "env": env_status,
            }
        except Exception as exc:
            return {
                "name": name, "status": "error",
                "items_count": 0, "error": str(exc)[:200],
                "env": env_status,
            }

    import asyncio as _asyncio
    probes = await _asyncio.gather(*[_probe(n, f, e) for n, f, e in sources_spec])
    ok = sum(1 for p in probes if p["status"] == "ok")
    warn = sum(1 for p in probes if p["status"] == "warn")
    err = sum(1 for p in probes if p["status"] == "error")

    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "sources": probes,
        "totals": {"ok": ok, "warn": warn, "error": err, "total": len(probes)},
    }


@app.get("/api/risk-state")
async def get_risk_state():
    """
    Snapshot dello stato di rischio live: recovery mode, drawdown 24h,
    win rate ultime 10 chiusure, concentration risk. Usato dalla UI e
    dai diagnostici. Il dato e' lo stesso iniettato nel system prompt
    del Decision Agent ad ogni run.
    """
    try:
        import risk_state
        snapshot = risk_state.build_risk_state_snapshot()
        return snapshot
    except Exception as e:
        logger.error("get_risk_state: %s", e, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": str(e), "type": type(e).__name__},
        )


@app.post("/api/risk-state/exit-recovery")
async def force_exit_recovery_mode():
    """
    Uscita manuale dal recovery mode (per casi in cui il sistema di
    auto-exit non riconosce il ritorno al balance iniziale, es. dopo
    deposit/withdraw manuali). Usare con cautela.
    """
    try:
        import risk_state
        result = risk_state.exit_recovery_mode(reason="manual_admin_override")
        return result
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


class RiskStateSettingsPayload(BaseModel):
    """Toggle dei feature flag del risk_state. Tutti i campi opzionali —
    solo i campi forniti vengono aggiornati."""
    lock_in_enabled: bool | None = None
    trailing_enabled: bool | None = None
    circuit_breaker_enabled: bool | None = None


@app.post("/api/risk-state/settings")
async def update_risk_state_settings(payload: RiskStateSettingsPayload):
    """
    Aggiorna i flag del modulo risk_state. Default attuali:
      - circuit_breaker_enabled: TRUE (safety net)
      - lock_in_enabled: FALSE (opt-in, evita auto-exit indesiderati)
      - trailing_enabled: FALSE (opt-in, evita auto-exit indesiderati)
    """
    try:
        import risk_state as _rs
        updated = {}
        if payload.lock_in_enabled is not None:
            database.set_setting(_rs.SETTING_LOCK_IN_ENABLED,
                                  "true" if payload.lock_in_enabled else "false")
            updated["lock_in_enabled"] = payload.lock_in_enabled
        if payload.trailing_enabled is not None:
            database.set_setting(_rs.SETTING_TRAILING_ENABLED,
                                  "true" if payload.trailing_enabled else "false")
            updated["trailing_enabled"] = payload.trailing_enabled
        if payload.circuit_breaker_enabled is not None:
            database.set_setting(_rs.SETTING_CIRCUIT_BREAKER_ENABLED,
                                  "true" if payload.circuit_breaker_enabled else "false")
            updated["circuit_breaker_enabled"] = payload.circuit_breaker_enabled
        return {"updated": updated}
    except Exception as e:
        logger.error("update_risk_state_settings: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/risk-state/clear-auto-sls")
async def clear_risk_state_auto_sls():
    """
    Rimuove TUTTI gli stop-loss che sono stati settati automaticamente da
    risk_state (lock_in / trailing). Le posizioni tornano libere — il
    Decision Agent / utente puo' mettere il proprio SL al prossimo run.

    Da usare dopo aver disabilitato lock_in/trailing per liberare le
    posizioni che il sistema aveva "preso in gestione" prima del fix.
    """
    try:
        import risk_state
        result = risk_state.clear_risk_state_auto_sls()
        return result
    except Exception as e:
        logger.error("clear_risk_state_auto_sls: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/risk-state/force-circuit-breaker-check")
async def force_circuit_breaker_check(background_tasks: BackgroundTasks):
    """
    Forza un check immediato del circuit breaker 24h (e applica lock-in
    + trailing stop). Stesso codice del scheduler job ogni 5 min, ma
    triggerabile on-demand per testing/diagnosi.
    """
    async def _do():
        try:
            from scheduler import _risk_safety_job
            await _risk_safety_job()
        except Exception as e:
            logger.error("manual risk_safety check failed: %s", e, exc_info=True)

    background_tasks.add_task(_do)
    return {"status": "started"}


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


@app.post("/api/scout/force-8h-report")
async def force_8h_report(background_tasks: BackgroundTasks):
    """
    Forza un run del Scout 8H Report adesso, in background. Utile per
    recuperare l'intelligence di un weekend in cui il job e' stato
    saltato per insufficient_records.
    """
    import uuid as _uuid
    run_id = str(_uuid.uuid4())

    async def _do():
        try:
            from agents.scout import run_8h_report
            result = await run_8h_report(run_id)
            logger.info("Manual 8H report %s: skipped=%s, bias=%s",
                        run_id, result.get("skipped"), result.get("macro_bias"))
        except Exception as e:
            logger.error("Manual 8H report failed: %s", e, exc_info=True)

    background_tasks.add_task(_do)
    return {"status": "started", "run_id": run_id, "tier": "8H"}


@app.post("/api/scout/force-4d-report")
async def force_4d_report(background_tasks: BackgroundTasks):
    """
    Forza un run del Scout 4D Report adesso. Aggrega gli ultimi report
    8H in un singolo report 4D. Da usare quando manca l'intelligence
    settimanale e si vuole rigenerarla on-demand.
    """
    import uuid as _uuid
    run_id = str(_uuid.uuid4())

    async def _do():
        try:
            from agents.scout import run_4d_report
            result = await run_4d_report(run_id)
            logger.info("Manual 4D report %s: skipped=%s, bias=%s",
                        run_id, result.get("skipped"), result.get("macro_bias"))
        except Exception as e:
            logger.error("Manual 4D report failed: %s", e, exc_info=True)

    background_tasks.add_task(_do)
    return {"status": "started", "run_id": run_id, "tier": "4D"}


@app.post("/api/crypto-monitor/run")
async def trigger_crypto_monitor(background_tasks: BackgroundTasks):
    """
    Lancia subito un singolo run del CryptoMonitor in background.
    Utile per testare il monitor o forzare un check dopo aver aperto
    una nuova posizione crypto. No-op se non ci sono posizioni crypto.
    """
    import uuid as _uuid
    run_id = str(_uuid.uuid4())[:8]

    async def _do():
        try:
            from agents.crypto_monitor import run_crypto_monitor
            result = await run_crypto_monitor(run_id=run_id)
            logger.info("Manual CryptoMonitor run %s: %s", run_id, {
                k: v for k, v in result.items() if k != "alerts"
            })
        except Exception as e:
            logger.error("Manual CryptoMonitor failed: %s", e, exc_info=True)

    background_tasks.add_task(_do)
    return {"status": "started", "run_id": run_id}


@app.get("/api/crypto-monitor/status")
async def get_crypto_monitor_status():
    """
    Stato del CryptoMonitor: enabled flag, auto_tighten flag, ultimi
    alert dal agent_logs (max 20). Per la UI di management.
    """
    try:
        enabled_raw = (database.get_setting("crypto_monitor_enabled", "true") or "").strip().lower()
        enabled = enabled_raw not in ("0", "false", "no", "off")
        tighten_raw = (database.get_setting("crypto_monitor_auto_tighten_sl", "false") or "").strip().lower()
        auto_tighten = tighten_raw in ("1", "true", "yes", "on")

        # Recupera ultimi alert (best-effort)
        recent_alerts: list = []
        try:
            client = database.get_client() if hasattr(database, "get_client") else None
            if client:
                r = (client.table("agent_logs")
                     .select("*")
                     .eq("phase", "CRYPTO_MONITOR_ALERT")
                     .order("timestamp", desc=True)
                     .limit(20)
                     .execute())
                for row in (r.data or []):
                    try:
                        content = row.get("content", "")
                        if isinstance(content, str):
                            import json as _j
                            payload = _j.loads(content)
                        else:
                            payload = content
                        recent_alerts.append({
                            "timestamp": row.get("timestamp"),
                            **(payload if isinstance(payload, dict) else {}),
                        })
                    except Exception:
                        continue
        except Exception as e:
            logger.debug("crypto_monitor recent alerts read fail: %s", e)

        return {
            "enabled": enabled,
            "auto_tighten_sl": auto_tighten,
            "recent_alerts": recent_alerts,
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


class CryptoMonitorSettingsPayload(BaseModel):
    enabled: bool | None = None
    auto_tighten_sl: bool | None = None


@app.post("/api/crypto-monitor/settings")
async def update_crypto_monitor_settings(payload: CryptoMonitorSettingsPayload):
    """Aggiorna i flag del CryptoMonitor (enabled / auto_tighten_sl)."""
    try:
        updated = {}
        if payload.enabled is not None:
            database.set_setting("crypto_monitor_enabled",
                                 "true" if payload.enabled else "false")
            updated["enabled"] = payload.enabled
        if payload.auto_tighten_sl is not None:
            database.set_setting("crypto_monitor_auto_tighten_sl",
                                 "true" if payload.auto_tighten_sl else "false")
            updated["auto_tighten_sl"] = payload.auto_tighten_sl
        return {"updated": updated}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


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


# --- Montaggio dei file statici del frontend React ---
# Modalità ENCRYPTION_ENABLED=true: il server espone SOLO unlock.html e
# /encrypted-bundle. Il browser (Web Crypto API) decifra in locale dopo aver
# ricevuto il token. La build React in chiaro NON viene mai trasmessa.
# Modalità default: serve la build React normalmente (sviluppo).
_ENCRYPTION_ENABLED = os.environ.get("ENCRYPTION_ENABLED", "").lower() in ("1", "true", "yes")

_encrypted_dir = os.path.join(os.path.dirname(__file__), "encrypted_assets")
_encrypted_bundle = os.path.join(_encrypted_dir, "bundle.enc")
_unlock_html = os.path.join(_encrypted_dir, "unlock.html")
_about_html = os.path.join(_encrypted_dir, "about.html")


# /about: pagina pubblica di documentazione, fuori dal bundle cifrato.
# Servita SEMPRE (in entrambe le modalita': encryption ON e OFF), cosi' il
# link e' permanente e indicizzabile da motori di ricerca / crawler AI.
# DEVE essere registrata prima delle catch-all route in modo da avere precedenza.
@app.get("/about")
async def serve_public_about():
    """Documentazione tecnica pubblica di GeoInvest AI (no auth)."""
    if not os.path.isfile(_about_html):
        return JSONResponse(
            status_code=404,
            content={"detail": "about.html mancante"},
        )
    return FileResponse(
        _about_html,
        media_type="text/html; charset=utf-8",
        headers={
            "Cache-Control": "public, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )

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
