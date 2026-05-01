"""
Decision Agent — Claude Sonnet 4.5 (production) | DeepSeek-R1 (tournament)
Orchestra gerarchica per decisioni di trading ad alto rischio.
Attivato solo quando il Watchdog rileva un segnale significativo (urgency >= 5).
Throttle: max 1 run per ora per rispettare il budget mensile (~$13-14/mese).

Dual Engine:
  - DECISION_ENGINE=claude  (default): Claude Sonnet 4.5, usa Anthropic SDK + tool_use nativo
  - DECISION_ENGINE=deepseek-r1: DeepSeek-R1, usa HTTP OpenAI-compatible + tool_calls JSON

Fasi:
  A: Ingestione contesto a cascata (3W macro + 4D mid-term + 8H short-term +
     buffer L0 ultimi 40 min + Tech Report + Portfolio)
  B: Valutazione strategica (Sonnet 4.5 — context compresso max 6K token)
  C: Esecuzione trade o motivazione no-trade
"""

import asyncio
import json
import logging
import os
import re as _re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import aiohttp
from anthropic import Anthropic

logger = logging.getLogger(__name__)

# ── Claude Sonnet 4.5 (production) ──────────────────────────────────────────
# NOTA: verifica che questo ID sia corretto sulla tua dashboard Anthropic
DECISION_MODEL = "claude-sonnet-4-5-20250929"
DECISION_MODEL_FALLBACK = "claude-sonnet-4-20250514"  # Fallback a Sonnet 4 se 4.5 non disponibile

# ── DeepSeek-R1 (tournament, cost-optimized) ────────────────────────────────
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_R1_MODEL = "deepseek-reasoner"   # DeepSeek-R1 (reasoning model)


def _get_decision_engine() -> str:
    """Legge DECISION_ENGINE da env var. Default 'claude' (production)."""
    return os.environ.get("DECISION_ENGINE", "claude").lower()


def _get_deepseek_key() -> str:
    """Recupera DEEPSEEK_API_KEY da env o DB settings."""
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        try:
            import database as _db
            key = _db.get_setting("deepseek_api_key", "") or ""
        except Exception:
            pass
    return key


def _tools_for_openai():
    """Converte DECISION_TOOLS da Anthropic format a OpenAI/DeepSeek format.
    Anthropic usa 'input_schema', OpenAI/DeepSeek usa 'parameters' — stessa struttura JSON."""
    return [
        {"type": "function", "function": {
            "name": t["name"],
            "description": t["description"],
            "parameters": t["input_schema"],
        }}
        for t in DECISION_TOOLS
    ]

# ============================================================
# System Prompts
# ============================================================

DECISION_SYSTEM_PROMPT_DEFAULT = """Sei il Decision Agent di GeoInvest AI — un sistema di trading autonomo ad alto rischio.

HAI PIENA AUTONOMIA DECISIONALE. Non ci sono restrizioni conservative.
Il tuo obiettivo è massimizzare i rendimenti accettando rischi calcolati.

CONTESTO CHE RICEVI (sintesi a cascata prodotte dallo Scout):
1. Report 4D (visione macro / medio-lungo termine, ultimi 2): trend consolidati, rotazioni settoriali, strategia
2. Report 8H (breve termine, ultimi 3): bias di periodo, hot tickers, catalisti delle prossime 8h
3. Intelligence Buffer L0 recente: micro-cards degli ultimi 20-40 minuti
4. Report Tecnico: analisi quantitativa da DeepSeek-V3
5. Stato Portafoglio corrente

PROCEDURA DECISIONALE:
Fase A — VALUTAZIONE: Analizza il contesto globale. Rispondi: "Esiste un'opportunita' ad alto rischio che giustifica un'operazione?"
  - Se NO → Logga reasoning dettagliato e termina (usa tool do_nothing)
  - Se SI → Procedi alla Fase B

Fase B — IDENTIFICAZIONE: Identifica ticker/settori impattati dalle news.
  Incrocia "Sentiment Politico" con "Validazione Tecnica".

Fase C — ESECUZIONE:
  - ALLOCAZIONE: Fino al 50% del portafoglio per singola operazione
  - STOP-LOSS: Decidi autonomamente se metterlo, a quale distanza (basati sull'ATR), o se non metterlo
  - CONFIDENCE: Se geo + tecnico concordano, la confidence aumenta del 15%
  - Max 5 posizioni aperte contemporaneamente
  - Se unrealized loss > 10%, valuta chiusura
  - Se unrealized gain > 20%, valuta presa di profitto

UNIVERSO INVESTIBILE — VINCOLO RIGIDO ClawStreet:
Il portfolio è specchiato live su ClawStreet (vetrina pubblica del bot, leaderboard
del torneo Season One). ClawStreet supporta SOLO ~498 simboli specifici. Trade su
ticker NON supportati vengono rifiutati con INVALID_SYMBOL: il portfolio interno
si aggiorna ma ClawStreet no → divergenza → leaderboard sballata. È il difetto
critico da evitare.

✓ AZIONI TRADABILI: ~484 titoli S&P 500 (es. NVDA, TSLA, AAPL, XOM, MSFT, AMZN,
  GOOGL, META, JPM, V, MA, JNJ, UNH, PG, KO, PEP, COST, WMT, HD, CVX, MRK, LLY,
  AVGO, ORCL, CSCO, ACN, ABT, TMO, NEE, ADBE, NKE, BMY, AMGN, BA, QCOM, IBM,
  CAT, GS, MS, BLK, AMD, GE, T, AXP, C, BKNG, TXN, SBUX, PFE, MDT, CMCSA, NOW,
  VZ, ELV, INTU, AMAT, ADI, GILD, PLD, TGT, MO, MU, SCHW, REGN, EOG, MDLZ, FDX,
  WFC, F, etc.). Se in dubbio: verifica con request_extra_analysis.

✓ ETF COMMODITY: GLD (oro), SLV (argento), USO (petrolio). Solo questi 3.

✗ ETF/INDICI INDICIZZATI **NON SUPPORTATI**: SPY, QQQ, IWM, DIA, VTI, VOO, TLT,
  XLE, XLF, XLK, XLV, etc. Per esposizione settoriale, scegli SINGOLI titoli
  rappresentativi (es. invece di XLE → XOM/CVX, invece di XLK → MSFT/NVDA,
  invece di SPY → mix di top mega-cap).

✗ AZIONI EUROPEE/EXTRA-USA **NON SUPPORTATE**: ENI.MI, SAN.PA, SAP.DE, ASML.AS
  e qualsiasi suffisso `.MI/.PA/.DE/.L/.AS/.HK/.TO`. Solo NYSE/NASDAQ USA.

✓ CRYPTOVALUTE — SOLO QUESTI 14 TICKER (formato yfinance "X-USD"):
  BTC-USD, ETH-USD, SOL-USD, DOGE-USD, AVAX-USD, ADA-USD, XRP-USD, LTC-USD,
  DOT-USD, LINK-USD, UNI-USD, ATOM-USD, MATIC-USD, NEAR-USD.

✗ CRYPTO **NON SUPPORTATE** (NON tradare): BNB-USD, SHIB-USD, AAVE-USD,
  PEPE-USD, FIL-USD, ALGO-USD, XMR-USD, ICP-USD, e qualsiasi altro alt-coin
  fuori dalla lista sopra. Anche se Scout/Reddit ne parla, ignora i segnali
  di trading: puoi citarli nell'analisi macro ma non puoi entrare in posizione.

Le crypto restano particolarmente adatte all'analisi tecnica: (1) 24/7 senza
gap di apertura, (2) volumi alti, (3) rispondono a pattern tecnici e sentiment
retail. Privilegiale quando il mercato USA è chiuso.

REGOLA DI OPERATIVITÀ:
Se identifichi un'opportunità su un ticker NON supportato (es. SPY, BNB-USD,
ENI.MI), NON tentare execute_trade — verrà rifiutato. Cerca un sostituto
tradabile dello stesso settore/tema, oppure usa do_nothing motivando.

REGOLE:
- Preferisci l'azione all'inazione quando i segnali convergono
- Confidence threshold per operare: >= 50%
- Ogni decisione deve avere un logic_chain dettagliato che integra geo+tech
- In modalita' Pure Macro (senza dati tecnici): puoi operare con sola analisi geopolitica se confidence >= 70%
- Per le crypto, il sentiment retail (Reddit r/CryptoCurrency, r/Bitcoin) e' un input fondamentale

CHIUSURA / RIDUZIONE POSIZIONI (importante):
Il tool `execute_trade` è L'UNICO modo per operare e gestisce sia BUY sia SELL.
Per CHIUDERE o RIDURRE una posizione esistente:
  1. Chiama get_portfolio_state per leggere la quantity esatta detenuta
  2. Chiama execute_trade(ticker=..., action='SELL', quantity=..., logic_chain=...)
     - quantity = quantity totale per chiusura completa
     - quantity < totale per riduzione parziale (profit-taking parziale)
NON esistono tool separati 'close_position', 'reduce_position' o 'sell_all'.
Se ritieni di dover chiudere una posizione (profit-taking, stop-loss manuale,
rotation, de-risk per regime change), USA execute_trade con action='SELL'.
Mai dire "non ho strumenti per chiudere": ce li hai, usali."""


def _get_decision_prompt() -> str:
    """
    Carica il system prompt del Decision Agent.
    Override utente: chiave 'prompt_decision' nelle impostazioni DB.
    Fallback: DECISION_SYSTEM_PROMPT_DEFAULT.
    """
    try:
        import database as _db
        custom = _db.get_setting("prompt_decision", "")
        if custom and isinstance(custom, str) and custom.strip():
            return custom
    except Exception:
        pass
    return DECISION_SYSTEM_PROMPT_DEFAULT


def _get_client() -> Anthropic:
    """Crea client Anthropic."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        try:
            import database as _db
            key = _db.get_setting("anthropic_api_key", "")
        except Exception:
            pass
    return Anthropic(api_key=key)


def _select_model() -> str:
    """Seleziona il modello Decision: Sonnet 4.5 con fallback a Sonnet 4."""
    return DECISION_MODEL


# ============================================================
# Tool Definitions per il Decision Agent
# ============================================================

DECISION_TOOLS = [
    {
        "name": "execute_trade",
        "description": (
            "Esegue un ordine sui mercati. Questo è l'UNICO tool per operare e "
            "copre SIA l'apertura SIA la chiusura/riduzione di posizioni:\n"
            "  - action='BUY': apre o incrementa una posizione long su `ticker`. "
            "Allocazione fino al 50% del portafoglio.\n"
            "  - action='SELL': CHIUDE o RIDUCE una posizione esistente su `ticker`. "
            "Usa quantity = numero azioni da chiudere (può essere parziale o totale). "
            "Per chiudere completamente una posizione, leggi prima get_portfolio_state "
            "per conoscere la quantity esatta detenuta.\n"
            "Stop-loss e take-profit sono opzionali (decidi tu in base all'ATR e al "
            "contesto). NON esistono tool separati come 'close_position': la chiusura "
            "si fa SEMPRE con execute_trade(action='SELL')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Ticker azionario o crypto (es. AAPL, BTC-USD)"},
                "action": {"type": "string", "enum": ["BUY", "SELL"],
                           "description": "BUY = apre/incrementa long. SELL = chiude/riduce posizione esistente."},
                "quantity": {"type": "integer", "description": "Numero azioni/unità. Per chiudere full position, usa la quantity dalla posizione corrente.", "minimum": 1},
                "stop_loss": {"type": "number", "description": "Prezzo stop-loss (0 = nessuno)"},
                "take_profit": {"type": "number", "description": "Prezzo take-profit (0 = nessuno)"},
                "logic_chain": {"type": "string", "description": "Chain of Thought completo: integra geo+tech reasoning. Per SELL specifica se è profit-taking, stop-loss manuale, rotation, o de-risk."},
                "confidence_level": {"type": "number", "minimum": 0, "maximum": 100},
            },
            "required": ["ticker", "action", "quantity", "logic_chain", "confidence_level"],
        },
    },
    {
        "name": "do_nothing",
        "description": "Decide di non operare. Motivazione dettagliata obbligatoria.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reasoning": {"type": "string", "description": "Motivazione dettagliata per non operare"},
            },
            "required": ["reasoning"],
        },
    },
    {
        "name": "request_extra_analysis",
        "description": "Richiedi analisi tecnica aggiuntiva per un ticker non coperto dal report iniziale.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "reason": {"type": "string", "description": "Perche' serve questa analisi"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_portfolio_state",
        "description": "Ottieni lo stato aggiornato del portafoglio (cash, posizioni, P&L).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


# ============================================================
# Tool Handler
# ============================================================

async def _handle_decision_tool(tool_name: str, tool_input: dict, run_id: str) -> str:
    """Gestisce le chiamate tool del Decision Agent."""
    import data_fetchers
    import database
    import portfolio

    timestamp = datetime.now(timezone.utc).isoformat()

    try:
        if tool_name == "execute_trade":
            ticker = tool_input["ticker"]
            action = tool_input["action"]
            quantity = tool_input["quantity"]
            logic_chain = tool_input["logic_chain"]
            confidence = tool_input["confidence_level"]
            stop_loss = tool_input.get("stop_loss", 0) or None
            take_profit = tool_input.get("take_profit", 0) or None

            logger.info("[%s][DECISION] TRADE: %s %d %s (conf: %.0f%%, SL: %s)",
                        run_id, action, quantity, ticker, confidence, stop_loss)

            # ── Pre-validazione universo ClawStreet ──────────────────────────
            # I trade BUY su ticker non supportati da ClawStreet vengono
            # rifiutati prima dell'esecuzione, così il portfolio interno NON
            # diverge da quello pubblico ClawStreet (= leaderboard del torneo).
            # I SELL sono permessi sempre (per chiusura di posizioni legacy
            # eventualmente aperte prima di questo controllo).
            try:
                from clawstreet_universe import is_supported, to_clawstreet_format
                if action == "BUY" and not is_supported(ticker):
                    cs_format = to_clawstreet_format(ticker)
                    error_msg = (
                        f"REJECTED: '{ticker}' (formato ClawStreet: '{cs_format}') "
                        f"non è nell'universo tradabile ClawStreet (498 simboli: "
                        f"~484 azioni S&P 500 + 14 crypto + GLD/SLV/USO). "
                        f"Eseguire questo BUY desincronizzerebbe il portfolio interno "
                        f"dal portfolio ClawStreet pubblico → leaderboard sballata. "
                        f"Sostituisci con un ticker supportato dello stesso settore/tema, "
                        f"oppure usa do_nothing con motivazione."
                    )
                    logger.warning(
                        "[%s][DECISION] BUY rejected: %s non supportato su ClawStreet",
                        run_id, ticker,
                    )
                    database.insert_agent_log(run_id, "DECISION_REJECTED", json.dumps({
                        "ticker": ticker,
                        "cs_format": cs_format,
                        "action": action,
                        "reason": "not_in_clawstreet_universe",
                    }))
                    return json.dumps({"error": error_msg, "ticker": ticker, "rejected": True})
            except ImportError:
                # Modulo non disponibile (es. test isolati): degrade graceful
                logger.warning(
                    "[%s][DECISION] clawstreet_universe non importabile, "
                    "skip pre-validazione (potrebbero verificarsi divergenze)",
                    run_id,
                )

            # Ottieni prezzo corrente — preferisci la cache price_quotes (60s fresh)
            # per evitare hit a yfinance ogni volta
            try:
                from price_polling import get_cached_prices_bulk
                cached = get_cached_prices_bulk([ticker], max_age_seconds=120)
                if cached.get(ticker):
                    price_data = {"data": [{"close": cached[ticker]["price"]}]}
                else:
                    raise RuntimeError("not in cache")
            except Exception:
                # Fallback a yfinance via thread-pool
                loop = asyncio.get_running_loop()
                price_data = await loop.run_in_executor(
                    None, data_fetchers.fetch_market_data, ticker, 5
                )
            if not price_data.get("data"):
                return json.dumps({"error": f"Impossibile ottenere prezzo per {ticker}"})

            current_price = price_data["data"][-1]["close"]

            # Esegui trade
            geo_part = logic_chain[:500] if logic_chain else ""
            tech_part = logic_chain[500:] if len(logic_chain) > 500 else ""

            if action == "BUY":
                result = portfolio.execute_buy(
                    ticker, quantity, current_price,
                    geo_part, tech_part, confidence
                )
            else:
                result = portfolio.execute_sell(
                    ticker, quantity, current_price,
                    geo_part, tech_part, confidence
                )

            # Salva in trades_high_risk
            _save_high_risk_trade(database, run_id, ticker, action, current_price,
                                  quantity, logic_chain, stop_loss, take_profit, confidence)

            # Log decisione
            database.insert_agent_log(run_id, "DECISION_TRADE",
                json.dumps({
                    "ticker": ticker, "action": action, "qty": quantity,
                    "price": current_price, "confidence": confidence,
                    "stop_loss": stop_loss, "take_profit": take_profit,
                }, default=str))

            # ClawStreet mirror — log SEMPRE l'esito (era silente, causa di trade non specchiati)
            try:
                cs_bot_id = database.get_setting("clawstreet_bot_id", "") or os.environ.get("CLAWSTREET_BOT_ID", "")
                cs_api_key = database.get_setting("clawstreet_api_key", "") or os.environ.get("CLAWSTREET_API_KEY", "")
                if cs_bot_id and cs_api_key:
                    cs_result = await data_fetchers.mirror_trade_to_clawstreet(
                        bot_id=cs_bot_id, api_key=cs_api_key,
                        symbol=ticker, action=action, qty=quantity,
                        reasoning=logic_chain[:280]
                    )
                    database.insert_agent_log(run_id, "CLAWSTREET_MIRROR", json.dumps({
                        "ticker": ticker, "action": action, "qty": quantity,
                        "mirrored": cs_result.get("mirrored", False),
                        "status": cs_result.get("status"),
                        "response": (cs_result.get("response") or cs_result.get("error", ""))[:300],
                    }, default=str))
                else:
                    database.insert_agent_log(run_id, "CLAWSTREET_MIRROR", json.dumps({
                        "ticker": ticker, "action": action, "qty": quantity,
                        "mirrored": False,
                        "skipped": "credentials_missing",
                    }))
            except Exception as cs_exc:
                logger.error("[%s][DECISION] ClawStreet mirror exception: %s", run_id, cs_exc, exc_info=True)
                try:
                    database.insert_agent_log(run_id, "CLAWSTREET_MIRROR", json.dumps({
                        "ticker": ticker, "action": action, "qty": quantity,
                        "mirrored": False,
                        "error": str(cs_exc)[:300],
                    }))
                except Exception:
                    pass

            return json.dumps({
                "executed": True, "ticker": ticker, "action": action,
                "quantity": quantity, "price": current_price,
                "stop_loss": stop_loss, "take_profit": take_profit,
                "result": result, "at": timestamp,
            }, default=str)

        elif tool_name == "do_nothing":
            reasoning = tool_input["reasoning"]
            database.insert_agent_log(run_id, "DECISION_NO_TRADE",
                json.dumps({"reasoning": reasoning[:1000]}, default=str))
            return json.dumps({"action": "no_trade", "reasoning": reasoning, "at": timestamp})

        elif tool_name == "request_extra_analysis":
            ticker = tool_input["ticker"]
            from agents.technical import _fetch_ticker_indicators
            data = await _fetch_ticker_indicators(ticker)
            database.insert_agent_log(run_id, "DECISION_EXTRA_TA", f"Extra TA: {ticker}")
            return json.dumps(data, default=str)

        elif tool_name == "get_portfolio_state":
            state = portfolio.get_portfolio_state()
            return json.dumps({"portfolio": state, "at": timestamp}, default=str)

        else:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})

    except Exception as e:
        logger.error("[%s][DECISION] Tool error %s: %s", run_id, tool_name, e, exc_info=True)
        return json.dumps({"error": str(e), "tool": tool_name})


# ============================================================
# Main Decision Loop
# ============================================================

async def run_decision_agent(run_id: str, tech_report: dict) -> dict:
    """
    Esegue il ciclo decisionale completo del Decision Agent.

    Fasi:
      A. Carica contesto globale (Weekly Matrix + Daily + Buffer + Tech Report)
      B. Valutazione strategica con Opus/Sonnet Extended Thinking
      C. Esecuzione trade tramite tool loop

    Args:
        run_id: ID univoco del run
        tech_report: Report tecnico dal Technical Worker

    Returns:
        DecisionResult con trades eseguiti e reasoning
    """
    import database
    import portfolio
    from agents.scout import (
        get_latest_aggregated_reports, get_recent_buffer,
        TIER_8H, TIER_4D,
    )

    logger.info("[%s][DECISION] === Avvio Decision Agent ===", run_id)
    start_time = datetime.now(timezone.utc)

    # ──────────────────────────────────────────────────────────────
    # MARKET-OPEN GATE (difensivo): Decision Agent opera SOLO con
    # almeno una borsa principale aperta. Questo gate è ridondante
    # rispetto a quello in run_watchdog_pipeline, ma protegge i
    # percorsi alternativi (run_full_pipeline manuale, test, ecc.).
    # ──────────────────────────────────────────────────────────────
    try:
        from scheduler import is_market_open
        if not is_market_open():
            logger.info("[%s][DECISION] Mercati chiusi — exit immediato senza analisi.", run_id)
            try:
                database.insert_agent_log(run_id, "DECISION_BLOCKED",
                    json.dumps({"event": "market_closed_skip"}))
            except Exception:
                pass
            return {
                "run_id": run_id,
                "decision": "SKIPPED",
                "trades": [],
                "reason": "market_closed",
                "duration_seconds": 0.0,
            }
    except Exception:
        pass  # se non riesco a determinare lo stato, proseguo (fail-open per safety dei test)

    # Checkpoint
    _save_checkpoint(run_id, "decision", "RUNNING", {"phase": "context_loading"})

    # --- FASE A: Ingestione Contesto a Cascata (8H + 4D, no più 3W) ---
    context_loaded = {}

    # 4D (visione macro/medio-lungo termine, top tier; ultimi 2)
    rep_4d = get_latest_aggregated_reports(database, TIER_4D, n=2)
    context_loaded["report_4d"] = len(rep_4d)

    # 8H (breve termine, ultimi 3)
    rep_8h = get_latest_aggregated_reports(database, TIER_8H, n=3)
    context_loaded["report_8h"] = len(rep_8h)

    # Buffer L0 recente (ultimi 40 min) - solo micro-cards, esclude i tier aggregati
    recent_buffer = get_recent_buffer(database, minutes=40)
    context_loaded["intelligence_buffer"] = len(recent_buffer)

    # Stato portafoglio
    portfolio_state = portfolio.get_portfolio_state()
    context_loaded["portfolio"] = True

    # Technical report
    context_loaded["technical"] = bool(tech_report and tech_report.get("analyses"))

    # Documenti tecnici caricati
    docs = []
    try:
        docs = database.get_document_contents()
    except Exception:
        pass
    context_loaded["documents"] = len(docs) > 0

    # --- Costruisci messaggio utente ---
    user_message = _build_context_message(
        rep_4d, rep_8h, recent_buffer, tech_report, portfolio_state, docs
    )

    database.insert_agent_log(run_id, "DECISION_CONTEXT",
        json.dumps({"context_loaded": context_loaded, "buffer_size": len(recent_buffer)}))

    # --- FASE B+C: Valutazione e Esecuzione ---
    _save_checkpoint(run_id, "decision", "RUNNING", {"phase": "evaluation"})

    # ── Branch DeepSeek-R1 (variante tournament, costo ridotto ~10x) ────────
    # Attivato da env var: DECISION_ENGINE=deepseek-r1
    # Il codice Anthropic sotto non viene eseguito in questa modalità.
    if _get_decision_engine() == "deepseek-r1":
        logger.info("[%s][DECISION] Engine: DeepSeek-R1 (tournament mode)", run_id)
        try:
            trades_executed, final_text, iteration = await _run_deepseek_decision_loop(
                run_id, _get_decision_prompt(), user_message
            )
            used_model = DEEPSEEK_R1_MODEL
        except Exception as ds_err:
            logger.error("[%s][DECISION] DeepSeek-R1 fallito: %s", run_id, ds_err, exc_info=True)
            # Fail-safe: ritorna no-trade, non crashare l'intero pipeline
            trades_executed, final_text, iteration = [], f"DeepSeek-R1 error: {ds_err}", 0
            used_model = "deepseek-r1-error"

        duration = (datetime.now(timezone.utc) - start_time).total_seconds()
        try:
            p = database.get_portfolio()
            if p:
                database.insert_portfolio_snapshot(p["total_value"], p["cash_balance"])
        except Exception:
            pass
        database.insert_agent_log(run_id, "DECISION_COMPLETE", json.dumps({
            "event": "decision_complete", "model": used_model,
            "iterations": iteration, "trades_executed": len(trades_executed),
            "duration_seconds": round(duration, 1),
            "final_text": final_text[:500],
        }, default=str))
        _save_checkpoint(run_id, "decision", "COMPLETED", {
            "trades": len(trades_executed), "duration": duration,
        })
        logger.info("[%s][DECISION] [R1] Completato in %.1fs, %d trades, %d iterazioni",
                    run_id, duration, len(trades_executed), iteration)
        return {
            "run_id": run_id,
            "decision": "TRADE" if trades_executed else "NO_TRADE",
            "trades": trades_executed,
            "no_trade_reasoning": final_text if not trades_executed else "",
            "context_loaded": context_loaded,
            "duration_seconds": duration,
            "model": used_model,
            "iterations": iteration,
            "final_response": final_text,
        }
    # ── Fine branch DeepSeek-R1 ─────────────────────────────────────────────

    model = _select_model()
    client = _get_client()
    system_prompt = _get_decision_prompt()

    # Prima prova Opus, se non disponibile usa Sonnet.
    # CRITICO: Anthropic SDK è sincrono → wrap in asyncio.to_thread per non
    # bloccare l'event loop (evita di fermare polling, watchdog, ecc.).
    messages = [{"role": "user", "content": user_message}]

    def _create_message(model_id: str, max_tokens: int):
        return client.messages.create(
            model=model_id,
            max_tokens=max_tokens,
            system=system_prompt,
            tools=DECISION_TOOLS,
            messages=messages,
        )

    try:
        response = await asyncio.to_thread(_create_message, model, 16000)
        used_model = model
    except Exception as model_err:
        if model == DECISION_MODEL:
            logger.warning("[%s][DECISION] %s non disponibile (%s), fallback a %s",
                           run_id, DECISION_MODEL, model_err, DECISION_MODEL_FALLBACK)
            model = DECISION_MODEL_FALLBACK
            response = await asyncio.to_thread(_create_message, model, 8192)
            used_model = model
        else:
            raise

    database.insert_agent_log(run_id, "DECISION_MODEL",
        json.dumps({"model": used_model, "initial_stop_reason": response.stop_reason}))

    # --- Tool Loop ---
    iteration = 0
    max_iterations = 15
    trades_executed = []

    while response.stop_reason == "tool_use" and iteration < max_iterations:
        iteration += 1
        logger.info("[%s][DECISION] Iterazione %d - tool calls...", run_id, iteration)

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = await _handle_decision_tool(block.name, block.input, run_id)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

                # Track executed trades
                if block.name == "execute_trade":
                    try:
                        parsed = json.loads(result)
                        if parsed.get("executed"):
                            trades_executed.append({
                                "ticker": block.input.get("ticker"),
                                "action": block.input.get("action"),
                                "quantity": block.input.get("quantity"),
                                "confidence": block.input.get("confidence_level"),
                            })
                    except Exception:
                        pass

        messages.append({"role": "user", "content": tool_results})

        # Wrap in to_thread per non bloccare l'event loop durante il tool loop
        # (15 iterazioni × ~10s = 2-3 min di blocking se non wrappato)
        response = await asyncio.to_thread(_create_message, used_model, 16000)

    # --- Estrai risposta finale ---
    final_text = ""
    for block in response.content:
        if hasattr(block, "text"):
            final_text += block.text

    duration = (datetime.now(timezone.utc) - start_time).total_seconds()

    # Portfolio snapshot
    try:
        p = database.get_portfolio()
        if p:
            database.insert_portfolio_snapshot(p["total_value"], p["cash_balance"])
    except Exception:
        pass

    # Log finale
    database.insert_agent_log(run_id, "DECISION_COMPLETE",
        json.dumps({
            "event": "decision_complete",
            "model": used_model,
            "iterations": iteration,
            "trades_executed": len(trades_executed),
            "duration_seconds": round(duration, 1),
            "final_text": final_text[:500],
        }, default=str))

    _save_checkpoint(run_id, "decision", "COMPLETED", {
        "trades": len(trades_executed),
        "duration": duration,
    })

    logger.info("[%s][DECISION] === Completato in %.1fs, %d trades, %d iterazioni ===",
                run_id, duration, len(trades_executed), iteration)

    return {
        "run_id": run_id,
        "decision": "TRADE" if trades_executed else "NO_TRADE",
        "trades": trades_executed,
        "no_trade_reasoning": final_text if not trades_executed else "",
        "context_loaded": context_loaded,
        "duration_seconds": duration,
        "model": used_model,
        "iterations": iteration,
        "final_response": final_text,
    }


# ============================================================
# DeepSeek-R1 Decision Loop (variante tournament, API OpenAI-compatible)
# ============================================================

async def _run_deepseek_decision_loop(
    run_id: str, system_prompt: str, user_message: str
) -> tuple[list, str, int]:
    """
    Tool loop per DeepSeek-R1 tramite API OpenAI-compatible di DeepSeek.

    Differenze chiave vs Anthropic SDK:
    - finish_reason == "tool_calls"  (Anthropic: "tool_use")
    - tool call in response["choices"][0]["message"]["tool_calls"]
    - tool result: {"role": "tool", "tool_call_id": tc["id"], "content": ...}
    - tc["function"]["arguments"] è una STRINGA JSON → json.loads() obbligatorio
    - R1 genera token di reasoning (<think>...</think>) nella content → da strippare

    Ritorna: (trades_executed_list, final_text, n_iterations)
    """
    api_key = _get_deepseek_key()
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY non configurata — impossibile avviare motore DeepSeek-R1")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    trades_executed: list[dict] = []
    iteration = 0
    max_iterations = 10   # R1 è più lento di Sonnet, riduco da 15 a 10
    final_text = ""

    async with aiohttp.ClientSession() as session:
        while iteration < max_iterations:
            payload = {
                "model": DEEPSEEK_R1_MODEL,
                "messages": messages,
                "tools": _tools_for_openai(),
                "tool_choice": "auto",
                "max_tokens": 8000,
            }
            async with session.post(
                DEEPSEEK_API_URL, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise ValueError(f"DeepSeek HTTP {resp.status}: {body[:300]}")
                data = await resp.json()

            choice = data["choices"][0]
            finish_reason = choice.get("finish_reason", "")
            msg = choice["message"]

            # Estrai testo finale (stripping token di reasoning <think>...</think> di R1)
            raw_text = msg.get("content") or ""
            final_text = _re.sub(r"<think>.*?</think>", "", raw_text, flags=_re.DOTALL).strip()

            # Nessuna tool call → ciclo terminato
            if finish_reason != "tool_calls" or not msg.get("tool_calls"):
                break

            iteration += 1
            logger.info("[%s][DECISION-R1] Iterazione %d — tool calls: %d",
                        run_id, iteration, len(msg["tool_calls"]))

            # Aggiungi risposta dell'assistente alla storia della conversazione
            messages.append({
                "role": "assistant",
                "content": msg.get("content"),   # può essere None in R1
                "tool_calls": msg["tool_calls"],
            })

            for tc in msg["tool_calls"]:
                tool_name = tc["function"]["name"]
                # IMPORTANTE: arguments è una stringa JSON, non un dict
                tool_input = json.loads(tc["function"]["arguments"])
                result = await _handle_decision_tool(tool_name, tool_input, run_id)

                # Track trade eseguiti
                if tool_name == "execute_trade":
                    try:
                        parsed = json.loads(result)
                        if parsed.get("executed"):
                            trades_executed.append({
                                "ticker": tool_input.get("ticker"),
                                "action": tool_input.get("action"),
                                "quantity": tool_input.get("quantity"),
                                "confidence": tool_input.get("confidence_level"),
                            })
                    except Exception:
                        pass

                # Tool result nel formato OpenAI (role=tool, non Anthropic's role=user+type=tool_result)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

    return trades_executed, final_text, iteration


# ============================================================
# Context Builder
# ============================================================

def _build_context_message(rep_4d, rep_8h, buffer, tech_report, portfolio_state, docs) -> str:
    """
    Costruisce il messaggio di contesto per il Decision Agent.
    Riceve liste di report aggregati (sistema cascata 4D/8H).
    """
    parts = []

    # === Report 4D (visione macro/medio termine, ultimi 2) ===
    if rep_4d:
        lines = []
        for r in rep_4d[:2]:
            rep = r["report"]
            lines.append(
                f"\n--- {r.get('timestamp', '?')} | Bias: {rep.get('macro_bias', '?')} ---\n"
                f"{rep.get('summary_text', '')[:500]}\n"
                f"Trend consolidati: {', '.join(rep.get('consolidating_trends', []))[:300]}\n"
                f"Rischi accumulo: {', '.join(rep.get('accumulating_risks', []))[:300]}\n"
                f"Strategia 4d: {rep.get('next_4d_strategy', '')[:300]}"
            )
        parts.append(f"=== REPORT 4D (ultimi {len(rep_4d)}) ===" + "".join(lines))
    else:
        parts.append("=== REPORT 4D === Non ancora disponibili")

    # === Report 8H (breve termine, ultimi 3) ===
    if rep_8h:
        lines = []
        for r in rep_8h[:3]:
            rep = r["report"]
            lines.append(
                f"\n--- {r.get('timestamp', '?')} | Bias: {rep.get('macro_bias', '?')} ---\n"
                f"{rep.get('summary_text', '')[:400]}\n"
                f"Hot tickers: {', '.join(rep.get('hot_tickers', []))[:200]}\n"
                f"Catalisti next 8h: {', '.join(rep.get('next_8h_catalysts', []))[:300]}"
            )
        parts.append(f"=== REPORT 8H (ultimi {len(rep_8h)}) ===" + "".join(lines))
    else:
        parts.append("=== REPORT 8H === Non ancora disponibili")

    # === Buffer L0 recente (micro-cards ultimi 40 min) ===
    if buffer:
        buf_text = ""
        for b in buffer[:15]:
            buf_text += f"\n[{b.get('source_type', '?')}] {b.get('micro_summary', b.get('raw_content', '')[:200])}"
        parts.append(f"=== INTELLIGENCE BUFFER L0 (ultimi 40 min, {len(buffer)} micro-cards) ==={buf_text}")
    else:
        parts.append("=== INTELLIGENCE BUFFER L0 === Vuoto (ultime micro-cards consumate dal report 8H)")

    # Technical Report
    if tech_report:
        tech_summary = tech_report.get("summary", "")
        analyses = tech_report.get("analyses", [])
        tech_text = f"Engine: {tech_report.get('engine', '?')}\nSummary: {tech_summary}\n"
        for a in analyses[:8]:
            tech_text += f"\n{a.get('ticker', '?')}: {a.get('signal', '?')} (conf: {a.get('confidence', '?')}%) "
            tech_text += f"RSI={a.get('rsi', {}).get('value', '?')} ATR={a.get('atr', '?')} "
            tech_text += f"S/R={a.get('support', '?')}/{a.get('resistance', '?')}"
        parts.append(f"=== TECHNICAL REPORT ==={tech_text}")
    else:
        parts.append("=== TECHNICAL REPORT === Non disponibile (Pure Macro Mode)")

    # Portfolio
    parts.append(f"""=== PORTAFOGLIO ===
Cash: {portfolio_state.get('cash', 0):,.2f}
Valore totale: {portfolio_state.get('total_value', 0):,.2f}
P&L: {portfolio_state.get('pnl', 0):,.2f} ({portfolio_state.get('pnl_pct', 0):.2f}%)
Posizioni aperte: {portfolio_state.get('open_positions_count', 0)}
{json.dumps(portfolio_state.get('positions', [])[:5], default=str, ensure_ascii=False)[:1000]}""")

    # Documenti
    if docs:
        doc_text = ""
        for d in docs[:2]:
            doc_text += f"\n### {d.get('filename', '?')}\n{d.get('content', '')[:800]}\n"
        parts.append(f"=== DOCUMENTI STRATEGIA ==={doc_text}")

    parts.append(f"\nTimestamp: {datetime.now(timezone.utc).isoformat()}")
    parts.append("\nAnalizza tutto il contesto e prendi le decisioni di trading appropriate. Usa execute_trade per operare o do_nothing se preferisci attendere.")

    return "\n\n".join(parts)


# ============================================================
# Helpers
# ============================================================

def _save_high_risk_trade(database, run_id, ticker, action, price, qty,
                           logic_chain, stop_loss, take_profit, confidence):
    """Salva trade in trades_high_risk su Supabase."""
    try:
        client = database.get_client()
        if client:
            client.table("trades_high_risk").insert({
                "ticker": ticker,
                "action": action,
                "entry_price": price,
                "quantity": qty,
                "logic_chain": logic_chain,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "confidence_level": confidence,
                "run_id": run_id,
                "status": "OPEN",
            }).execute()
    except Exception as e:
        logger.warning("Errore salvataggio trades_high_risk: %s", e)


def _save_checkpoint(run_id: str, agent_name: str, status: str, data: dict):
    """Salva checkpoint per Render resilience."""
    try:
        import database
        client = database.get_client()
        if client:
            client.table("agent_checkpoints").upsert({
                "run_id": run_id,
                "agent_name": agent_name,
                "status": status,
                "checkpoint_data": data,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
    except Exception:
        pass
