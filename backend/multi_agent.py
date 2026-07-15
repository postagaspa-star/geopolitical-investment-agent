"""
Sistema Multi-Agent Gerarchico per GeoInvest AI.

Architettura Manager-Worker:
- Manager Agent (Claude Sonnet): Orchestratore decisionale
- Geopolitical Worker (Claude Sonnet): Analisi geopolitica, congressional trades, market intel
- Technical Worker (DeepSeek-V3): Analisi tecnica con indicatori quantitativi

Il Manager riceve i report dei Worker e prende decisioni finali di trading.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import aiohttp
from anthropic import Anthropic

import data_fetchers
import database
import portfolio
import technical_analysis

logger = logging.getLogger(__name__)

# --- Configurazione modelli ---
CLAUDE_MODEL = "claude-sonnet-4-5"   # era sonnet-4-20250514 (deprecato/in ritiro)
DEEPSEEK_MODEL = "deepseek-chat"
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"


def _get_anthropic_key() -> str:
    key = database.get_setting("anthropic_api_key")
    return key if key else os.environ.get("ANTHROPIC_API_KEY", "")


def _get_deepseek_key() -> str:
    return os.environ.get("DEEPSEEK_API_KEY", "")


# ============================================================
# Worker: Geopolitical Analyst (Claude Sonnet)
# ============================================================

GEO_WORKER_PROMPT = """Sei un analista geopolitico specializzato in intelligence per mercati finanziari.
Il tuo compito e' raccogliere e sintetizzare dati geopolitici, congressional trades e market intelligence
in un report strutturato per il Manager Agent.

REGOLE:
- Analizza TUTTI i dati forniti in modo critico e sintetico
- Identifica i TOP 5 eventi geopolitici piu' rilevanti per i mercati
- Per ogni evento, stima l'impatto su settori specifici (energy, defense, tech, commodities)
- Identifica segnali di rischio (conflitti, sanzioni, instabilita') e opportunita'
- Integra i dati congressional trades per individuare insider trading patterns
- Il tuo output deve essere un JSON strutturato, NON testo libero

OUTPUT FORMAT (JSON):
{
  "risk_level": "LOW|MEDIUM|HIGH|CRITICAL",
  "top_events": [
    {"event": "...", "impact": "...", "affected_sectors": [...], "sentiment": "BULLISH|BEARISH|NEUTRAL"}
  ],
  "congressional_signals": [
    {"ticker": "...", "direction": "BUY|SELL", "significance": "LOW|MEDIUM|HIGH"}
  ],
  "market_sentiment": {"overall": "...", "sectors": {...}},
  "recommended_tickers": {"watch": [...], "avoid": [...]},
  "summary": "..."
}"""


async def run_geo_worker(run_id: str) -> dict:
    """Esegue il Geopolitical Worker: raccoglie e analizza dati geopolitici."""
    logger.info("[%s][GEO_WORKER] Avvio raccolta intelligence geopolitica...", run_id)

    # Raccogli dati in parallelo
    tasks = [
        data_fetchers.fetch_gdelt_data(),
        data_fetchers.fetch_newsapi_data(),
        data_fetchers.fetch_congressional_trades(),
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    gdelt_data = results[0] if not isinstance(results[0], Exception) else {"error": str(results[0])}
    newsapi_data = results[1] if not isinstance(results[1], Exception) else {"error": str(results[1])}
    congressional = results[2] if not isinstance(results[2], Exception) else {"error": str(results[2])}
    market_ctx = {}

    # Salva snapshot geopolitici
    try:
        database.insert_geopolitical_snapshot(run_id, "GDELT", json.dumps(gdelt_data, default=str), "")
        database.insert_geopolitical_snapshot(run_id, "NEWSAPI", json.dumps(newsapi_data, default=str), "")
    except Exception:
        pass

    # Prepara contesto per Claude
    context = json.dumps({
        "gdelt_events": gdelt_data,
        "news_articles": newsapi_data,
        "congressional_trades": congressional,
        "market_context": market_ctx,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }, default=str, ensure_ascii=False)

    # Chiama Claude per analisi
    try:
        client = Anthropic(api_key=_get_anthropic_key())
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4096,
            system=GEO_WORKER_PROMPT,
            messages=[{
                "role": "user",
                "content": f"Analizza questi dati geopolitici e di mercato e produci il report JSON:\n\n{context[:15000]}"
            }],
        )

        report_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                report_text += block.text

        # Parse JSON dal report
        try:
            # Trova il JSON nel testo (potrebbe avere testo intorno)
            json_start = report_text.find("{")
            json_end = report_text.rfind("}") + 1
            if json_start >= 0 and json_end > json_start:
                report = json.loads(report_text[json_start:json_end])
            else:
                report = {"raw_analysis": report_text, "risk_level": "MEDIUM"}
        except json.JSONDecodeError:
            report = {"raw_analysis": report_text, "risk_level": "MEDIUM"}

        database.insert_agent_log(run_id, "GEO_WORKER",
            f"Report completato: risk={report.get('risk_level', 'N/A')}, "
            f"events={len(report.get('top_events', []))}")

        logger.info("[%s][GEO_WORKER] Analisi completata. Risk: %s", run_id, report.get("risk_level"))
        return report

    except Exception as e:
        logger.error("[%s][GEO_WORKER] Errore: %s", run_id, e, exc_info=True)
        database.insert_agent_log(run_id, "GEO_WORKER", f"Errore: {e}")
        return {"error": str(e), "risk_level": "MEDIUM", "raw_data_available": True}


# ============================================================
# Worker: Technical Analyst (DeepSeek-V3)
# ============================================================

TECH_WORKER_PROMPT = """You are a quantitative technical analyst for financial markets.
You receive raw technical indicator data for multiple tickers and must produce a structured analysis.

RULES:
- Analyze ALL indicators: RSI, MACD, Bollinger Bands, SMA crossovers, momentum
- For each ticker, provide a clear BUY/SELL/HOLD signal with confidence (0-100)
- Identify key support/resistance levels
- Flag any divergences between indicators
- Your output MUST be valid JSON

OUTPUT FORMAT (JSON):
{
  "analyses": [
    {
      "ticker": "...",
      "signal": "BUY|SELL|HOLD",
      "confidence": 0-100,
      "key_levels": {"support": ..., "resistance": ...},
      "indicators": {
        "rsi": {"value": ..., "signal": "..."},
        "macd": {"value": ..., "signal": "..."},
        "bollinger": {"position": "...", "signal": "..."},
        "sma_cross": {"signal": "..."}
      },
      "reasoning": "..."
    }
  ],
  "market_regime": "TRENDING_UP|TRENDING_DOWN|RANGING|VOLATILE",
  "summary": "..."
}"""


async def _call_deepseek(prompt: str, context: str) -> str:
    """Chiama DeepSeek-V3 via API OpenAI-compatible."""
    api_key = _get_deepseek_key()
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY non configurata")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": context},
        ],
        "max_tokens": 4096,
        "temperature": 0.3,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(DEEPSEEK_API_URL, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise ValueError(f"DeepSeek API error {resp.status}: {body[:500]}")
            data = await resp.json()
            return data["choices"][0]["message"]["content"]


async def _fetch_ticker_data(ticker: str, period_days: int = 90) -> dict:
    """Recupera dati di mercato e calcola indicatori tecnici per un ticker."""
    import pandas as pd

    loop = asyncio.get_event_loop()
    market_data = await loop.run_in_executor(
        None, data_fetchers.fetch_market_data, ticker, period_days
    )

    if not market_data.get("data"):
        return {"ticker": ticker, "error": market_data.get("error", "No data")}

    df = pd.DataFrame(market_data["data"])
    df.set_index("date", inplace=True)
    df.columns = [c.capitalize() for c in df.columns]
    analysis = technical_analysis.analyze_ticker(df)

    return {"ticker": ticker, "analysis": analysis}


async def run_tech_worker(run_id: str, tickers: list[str]) -> dict:
    """Esegue il Technical Worker: analisi tecnica parallela con DeepSeek-V3."""
    logger.info("[%s][TECH_WORKER] Avvio analisi tecnica per %d tickers: %s",
                run_id, len(tickers), tickers)

    if not tickers:
        return {"analyses": [], "summary": "Nessun ticker da analizzare"}

    # Recupera dati tecnici in parallelo
    tasks = [_fetch_ticker_data(t) for t in tickers[:10]]  # Max 10 tickers
    raw_data = await asyncio.gather(*tasks, return_exceptions=True)

    ticker_analyses = []
    for i, result in enumerate(raw_data):
        if isinstance(result, Exception):
            ticker_analyses.append({"ticker": tickers[i], "error": str(result)})
        else:
            ticker_analyses.append(result)

    context = json.dumps({
        "tickers_data": ticker_analyses,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }, default=str, ensure_ascii=False)

    # Prova DeepSeek, fallback a Claude
    try:
        deepseek_key = _get_deepseek_key()
        if deepseek_key:
            logger.info("[%s][TECH_WORKER] Usando DeepSeek-V3 per analisi tecnica...", run_id)
            report_text = await _call_deepseek(TECH_WORKER_PROMPT, context[:15000])
            engine = "deepseek-v3"
        else:
            raise ValueError("No DeepSeek key, fallback to Claude")
    except Exception as ds_err:
        logger.warning("[%s][TECH_WORKER] DeepSeek fallback a Claude: %s", run_id, ds_err)
        client = Anthropic(api_key=_get_anthropic_key())
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4096,
            system=TECH_WORKER_PROMPT,
            messages=[{
                "role": "user",
                "content": f"Analyze these technical indicators and produce the JSON report:\n\n{context[:15000]}"
            }],
        )
        report_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                report_text += block.text
        engine = "claude-sonnet"

    # Parse JSON
    try:
        json_start = report_text.find("{")
        json_end = report_text.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            report = json.loads(report_text[json_start:json_end])
        else:
            report = {"raw_analysis": report_text, "analyses": []}
    except json.JSONDecodeError:
        report = {"raw_analysis": report_text, "analyses": []}

    report["engine"] = engine
    report["raw_indicators"] = ticker_analyses  # Include raw data for Manager

    database.insert_agent_log(run_id, "TECH_WORKER",
        f"Analisi completata ({engine}): {len(report.get('analyses', []))} tickers analizzati")

    logger.info("[%s][TECH_WORKER] Analisi completata via %s", run_id, engine)
    return report


# ============================================================
# Manager Agent (Claude Sonnet) - Orchestratore Decisionale
# ============================================================

MANAGER_SYSTEM_PROMPT = """Sei il Manager Agent di GeoInvest AI, un sistema multi-agente per investimenti geopolitici.

Ricevi report da due Worker specializzati:
1. **Geopolitical Worker**: Analisi geopolitica, congressional trades, market intelligence
2. **Technical Worker**: Analisi tecnica quantitativa con indicatori (RSI, MACD, Bollinger, SMA)

Il tuo compito e' INTEGRARE i report dei Worker e prendere DECISIONI DI TRADING autonome.

MODALITA' HIGH-RISK:
- Sei autorizzato a operare con piena autonomia
- Non essere conservativo: se i segnali sono chiari, AGISCI
- Puoi allocare fino al 30% del portafoglio in una singola posizione
- Confidence threshold per operare: >= 55% (non 70% come in modalita' standard)
- Preferisci l'azione all'inazione quando i segnali convergono

PROCEDURA DECISIONALE:
1. Leggi il Geopolitical Report: identifica rischi e opportunita' macro
2. Leggi il Technical Report: identifica segnali tecnici per i ticker chiave
3. CROSS-REFERENCE: Quando geo + tecnico concordano, la confidence aumenta del 15%
4. Valuta il portafoglio attuale: evita overconcentration, gestisci P&L
5. Per ogni opportunita', usa lo strumento `execute_trade` o `do_nothing`

REGOLE DI POSITION SIZING:
- Max 30% del portafoglio in un singolo trade
- Max 5 posizioni aperte contemporaneamente
- Se una posizione ha unrealized loss > 8%, valuta la chiusura
- Se una posizione ha unrealized gain > 15%, valuta la presa di profitto

OUTPUT: Per ogni decisione, fornisci reasoning dettagliato che integra entrambi i report."""


def _build_manager_prompt(mode: str, geo_report: dict, tech_report: dict) -> str:
    """Costruisce il messaggio per il Manager con i report dei Worker."""
    portfolio_state = portfolio.get_portfolio_state()

    # Weekend intelligence e pre-market briefing come contesto aggiuntivo
    extra_context = ""
    if mode == "full":
        weekend_intel = database.get_latest_weekend_intelligence()
        if weekend_intel and weekend_intel.get("content"):
            extra_context += f"\n\n--- WEEKEND INTELLIGENCE ---\n{weekend_intel['content'][:2000]}\n"

        pre_market = database.get_latest_pre_market_briefing()
        if pre_market and pre_market.get("content"):
            extra_context += f"\n\n--- PRE-MARKET BRIEFING ---\n{pre_market['content'][:2000]}\n"

    # Documenti tecnici caricati
    docs = database.get_document_contents()
    docs_context = ""
    if docs:
        for doc in docs[:3]:
            docs_context += f"\n### {doc['filename']}\n{doc['content'][:1500]}\n"

    return f"""Ecco i report dei tuoi Worker Agent e lo stato del portafoglio.
Analizza tutto e prendi le decisioni di trading appropriate.

=== GEOPOLITICAL WORKER REPORT ===
{json.dumps(geo_report, ensure_ascii=False, default=str)[:6000]}

=== TECHNICAL WORKER REPORT ===
{json.dumps(tech_report, ensure_ascii=False, default=str)[:6000]}

=== STATO PORTAFOGLIO ATTUALE ===
{json.dumps(portfolio_state, ensure_ascii=False, default=str)}
{extra_context}
{f'=== DOCUMENTI ANALISI ==={docs_context}' if docs_context else ''}

Timestamp: {datetime.now(timezone.utc).isoformat()}

Procedi con le tue decisioni di trading. Usa `execute_trade` per operare o `do_nothing` se preferisci attendere."""


# Tool definitions per il Manager (solo decisioni)
MANAGER_TOOLS = [
    {
        "name": "execute_trade",
        "description": (
            "Esegue un ordine di acquisto (BUY) o vendita (SELL). "
            "Richiede motivazioni integrate geo+tech dai report dei Worker."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Ticker azionario"},
                "action": {"type": "string", "enum": ["BUY", "SELL"], "description": "BUY o SELL"},
                "quantity": {"type": "integer", "description": "Numero di azioni"},
                "geopolitical_reasoning": {"type": "string", "description": "Motivazione geopolitica dal Geo Worker Report"},
                "technical_reasoning": {"type": "string", "description": "Motivazione tecnica dal Tech Worker Report"},
                "confidence_score": {"type": "number", "minimum": 0, "maximum": 100, "description": "Confidence integrata geo+tech"},
            },
            "required": ["ticker", "action", "quantity", "geopolitical_reasoning", "technical_reasoning", "confidence_score"],
        },
    },
    {
        "name": "do_nothing",
        "description": "Decide di non operare. Fornisci motivazione dettagliata.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reasoning": {"type": "string", "description": "Motivazione per non operare"},
            },
            "required": ["reasoning"],
        },
    },
    {
        "name": "get_technical_analysis",
        "description": "Richiedi analisi tecnica aggiuntiva per un ticker specifico non coperto dal Tech Worker.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Ticker da analizzare"},
                "period_days": {"type": "integer", "description": "Giorni di storico", "default": 90},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_portfolio_state",
        "description": "Richiedi lo stato aggiornato del portafoglio.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


async def _handle_manager_tool(tool_name: str, tool_input: dict, run_id: str) -> str:
    """Gestisce le chiamate tool del Manager Agent."""
    timestamp = datetime.now(timezone.utc).isoformat()

    try:
        if tool_name == "execute_trade":
            ticker = tool_input["ticker"]
            action = tool_input["action"]
            quantity = tool_input["quantity"]
            geo_reasoning = tool_input["geopolitical_reasoning"]
            tech_reasoning = tool_input["technical_reasoning"]
            confidence = tool_input["confidence_score"]

            logger.info("[%s][MANAGER] Trade: %s %d %s (conf: %.1f%%)", run_id, action, quantity, ticker, confidence)

            # Ottieni prezzo corrente
            loop = asyncio.get_event_loop()
            price_data = await loop.run_in_executor(
                None, data_fetchers.fetch_market_data, ticker, 5
            )
            if not price_data.get("data"):
                return json.dumps({"error": f"Impossibile ottenere prezzo per {ticker}"})

            current_price = price_data["data"][-1]["close"]

            if action == "BUY":
                result = portfolio.execute_buy(ticker, quantity, current_price, geo_reasoning, tech_reasoning, confidence)
            else:
                result = portfolio.execute_sell(ticker, quantity, current_price, geo_reasoning, tech_reasoning, confidence)

            database.insert_agent_log(run_id, "MANAGER_DECISION",
                f"{action} {quantity} {ticker} @ {current_price:.2f} (conf: {confidence})")

            return json.dumps({
                "trade_executed": True, "ticker": ticker, "action": action,
                "quantity": quantity, "price": current_price, "result": result,
                "executed_at": timestamp,
            }, default=str)

        elif tool_name == "do_nothing":
            reasoning = tool_input["reasoning"]
            database.insert_agent_log(run_id, "MANAGER_DECISION", f"No trade: {reasoning}")
            return json.dumps({"action": "no_trade", "reasoning": reasoning, "decided_at": timestamp})

        elif tool_name == "get_technical_analysis":
            ticker = tool_input["ticker"]
            period_days = tool_input.get("period_days", 90)
            data = await _fetch_ticker_data(ticker, period_days)
            database.insert_agent_log(run_id, "MANAGER_TOOL", f"Extra TA for {ticker}")
            return json.dumps(data, default=str)

        elif tool_name == "get_portfolio_state":
            state = portfolio.get_portfolio_state()
            return json.dumps({"portfolio": state, "retrieved_at": timestamp}, default=str)

        else:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})

    except Exception as e:
        logger.error("[%s][MANAGER] Tool error %s: %s", run_id, tool_name, e, exc_info=True)
        return json.dumps({"error": str(e), "tool_name": tool_name})


async def run_manager(run_id: str, mode: str, geo_report: dict, tech_report: dict) -> dict:
    """Esegue il Manager Agent con i report dei Worker."""
    logger.info("[%s][MANAGER] Avvio fase decisionale...", run_id)

    client = Anthropic(api_key=_get_anthropic_key())
    user_message = _build_manager_prompt(mode, geo_report, tech_report)

    messages = [{"role": "user", "content": user_message}]

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=8096,
        system=MANAGER_SYSTEM_PROMPT,
        tools=MANAGER_TOOLS,
        messages=messages,
    )

    iteration = 0
    max_iterations = 15

    while response.stop_reason == "tool_use" and iteration < max_iterations:
        iteration += 1
        logger.info("[%s][MANAGER] Iterazione %d - Tool calls...", run_id, iteration)

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = await _handle_manager_tool(block.name, block.input, run_id)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        messages.append({"role": "user", "content": tool_results})

        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=8096,
            system=MANAGER_SYSTEM_PROMPT,
            tools=MANAGER_TOOLS,
            messages=messages,
        )

    final_response = ""
    for block in response.content:
        if hasattr(block, "text"):
            final_response += block.text

    database.insert_agent_log(run_id, "MANAGER",
        json.dumps({"event": "manager_complete", "iterations": iteration,
                     "response": final_response[:500]}, default=str))

    logger.info("[%s][MANAGER] Decisioni completate in %d iterazioni.", run_id, iteration)
    return {
        "final_response": final_response,
        "iterations": iteration,
        "stop_reason": response.stop_reason,
    }


# ============================================================
# Orchestratore Multi-Agent
# ============================================================

async def run_multi_agent(run_id: str, mode: str = "full") -> dict:
    """
    Orchestratore principale del sistema multi-agente.

    Fasi:
    1. Workers in parallelo (Geo + Tech)
    2. Manager riceve report e decide
    3. Risultati aggregati

    Args:
        run_id: ID univoco dell'esecuzione
        mode: "full" per ciclo completo

    Returns:
        Dizionario con risultati aggregati
    """
    logger.info("[%s] === MULTI-AGENT SYSTEM START (mode=%s) ===", run_id, mode)
    start_time = datetime.now(timezone.utc)

    database.insert_agent_log(run_id, "ORCHESTRATOR",
        json.dumps({"event": "multi_agent_start", "mode": mode}))

    # --- Determina tickers prioritari ---
    # Usa posizioni aperte + watchlist core
    positions = database.get_positions()
    held_tickers = [p["ticker"] for p in positions]

    # Tickers da analizzare: posizioni aperte + top watchlist
    priority_tickers = list(set(
        held_tickers +
        ["SPY", "QQQ", "XOM", "LMT", "GLD"] +  # Core watchlist
        data_fetchers.WATCHLIST.get("energy", [])[:2] +
        data_fetchers.WATCHLIST.get("defense", [])[:2]
    ))[:10]

    # --- FASE 1: Workers in parallelo ---
    database.insert_agent_log(run_id, "ORCHESTRATOR",
        f"Fase 1: Workers in parallelo. Tickers: {priority_tickers}")

    geo_task = run_geo_worker(run_id)
    tech_task = run_tech_worker(run_id, priority_tickers)

    geo_report, tech_report = await asyncio.gather(geo_task, tech_task)

    # Aggiungi tickers raccomandati dal Geo Worker ai tickers tecnici se non gia' presenti
    geo_recommended = geo_report.get("recommended_tickers", {}).get("watch", [])
    extra_tickers = [t for t in geo_recommended if t not in priority_tickers][:3]

    extra_tech = {}
    if extra_tickers:
        database.insert_agent_log(run_id, "ORCHESTRATOR",
            f"Analisi tecnica aggiuntiva per tickers geo-recommended: {extra_tickers}")
        extra_results = await run_tech_worker(run_id, extra_tickers)
        tech_report["extra_analyses"] = extra_results.get("analyses", [])

    # --- FASE 2: Manager Decision ---
    database.insert_agent_log(run_id, "ORCHESTRATOR",
        "Fase 2: Manager Agent - decisioni di trading")

    manager_result = await run_manager(run_id, mode, geo_report, tech_report)

    # --- FASE 3: Post-processing ---
    end_time = datetime.now(timezone.utc)
    duration = (end_time - start_time).total_seconds()

    # Portfolio snapshot
    try:
        p = database.get_portfolio()
        if p:
            database.insert_portfolio_snapshot(p["total_value"], p["cash_balance"])
    except Exception:
        pass

    database.insert_agent_log(run_id, "ORCHESTRATOR",
        json.dumps({
            "event": "multi_agent_complete",
            "duration_seconds": duration,
            "geo_risk": geo_report.get("risk_level", "N/A"),
            "tech_engine": tech_report.get("engine", "N/A"),
            "manager_iterations": manager_result.get("iterations", 0),
        }))

    logger.info("[%s] === MULTI-AGENT SYSTEM COMPLETE (%.1fs) ===", run_id, duration)

    return {
        "run_id": run_id,
        "mode": mode,
        "duration_seconds": duration,
        "geo_report_summary": {
            "risk_level": geo_report.get("risk_level"),
            "events_count": len(geo_report.get("top_events", [])),
        },
        "tech_report_summary": {
            "engine": tech_report.get("engine"),
            "tickers_analyzed": len(tech_report.get("analyses", [])),
        },
        "manager_result": manager_result,
        "final_response": manager_result.get("final_response", ""),
    }
