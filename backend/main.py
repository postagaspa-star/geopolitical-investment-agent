"""
Applicazione principale FastAPI per l'agente geopolitico di investimento.
Fornisce endpoint REST per il portafoglio, le posizioni, i trade e i log.
"""

import json
import logging
import os
import uuid
from contextlib import asynccontextmanager

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
    Avvia manualmente un'esecuzione singola dell'agente in background.
    Determina automaticamente la modalita' in base all'orario.
    """
    run_id = str(uuid.uuid4())
    mode = scheduler.get_current_mode()

    async def _run():
        try:
            logger.info(f"Esecuzione manuale dell'agente avviata (run_id: {run_id}, mode: {mode}).")
            await agent.run_agent(mode=mode)
            logger.info(f"Esecuzione manuale dell'agente completata (run_id: {run_id}).")
        except Exception as e:
            logger.error(
                f"Errore durante l'esecuzione manuale dell'agente (run_id: {run_id}): {e}",
                exc_info=True,
            )

    background_tasks.add_task(_run)
    return {"status": "started", "run_id": run_id, "mode": mode}


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


# --- Endpoint dei documenti tecnici ---


@app.get("/api/documents")
async def list_documents():
    """Restituisce la lista dei documenti tecnici caricati."""
    try:
        docs = database.get_documents()
        return docs
    except Exception as e:
        logger.error(f"Errore nel recupero dei documenti: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/documents/upload")
async def upload_document(file: UploadFile = File(...)):
    """
    Carica un documento PDF o TXT con strategie di analisi tecnica.
    Il contenuto viene estratto e salvato nel database.
    """
    try:
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
        database.insert_document(filename, content, file_size)
        logger.info(f"Documento caricato: {filename} ({file_size} bytes)")

        return {"status": "uploaded", "filename": filename, "size": file_size}
    except Exception as e:
        logger.error(f"Errore nel caricamento del documento: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.delete("/api/documents/{doc_id}")
async def remove_document(doc_id: int):
    """Elimina un documento tecnico per ID."""
    try:
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
    """Restituisce lo storico del valore del portafoglio."""
    try:
        days_map = {"7d": 7, "1w": 7, "30d": 30, "1m": 30, "90d": 90, "3m": 90, "all": 3650}
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
    """Testa la connessione a GDELT."""
    try:
        import aiohttp
        url = "https://api.gdeltproject.org/api/v2/doc/doc?query=test&mode=artlist&maxrecords=1&format=json"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    return {"status": "ok"}
                else:
                    return {"status": "error", "message": f"HTTP {resp.status}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


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
# Cerca la build del frontend in due posizioni:
# 1. backend/static (usata su Render dopo il build command che copia la build qui)
# 2. ../frontend/build (sviluppo locale)
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
    # Monta gli asset statici (JS, CSS, immagini) sotto /static
    _assets_dir = os.path.join(_spa_dir, "static")
    if os.path.isdir(_assets_dir):
        app.mount("/static", StaticFiles(directory=_assets_dir), name="static-assets")

    # Catch-all: per qualsiasi rotta non-API, restituisce index.html (SPA routing)
    @app.get("/{full_path:path}")
    async def serve_spa(request: Request, full_path: str):
        """Serve index.html per tutte le rotte non-API (SPA catch-all)."""
        # Se il file richiesto esiste nella directory statica, servilo direttamente
        file_path = os.path.join(_spa_dir, full_path)
        if full_path and os.path.isfile(file_path):
            return FileResponse(file_path)
        # Altrimenti restituisci index.html per il client-side routing
        index_path = os.path.join(_spa_dir, "index.html")
        if os.path.isfile(index_path):
            return FileResponse(index_path)
        return JSONResponse(status_code=404, content={"detail": "Frontend non trovato"})
