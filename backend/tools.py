"""
Definizioni degli strumenti Claude e gestori di esecuzione.
Questo modulo definisce i tool disponibili per l'agente e gestisce le chiamate.
"""

import json
import logging
from datetime import datetime, timezone

# Importazioni dai moduli del progetto
import data_fetchers
import technical_analysis
import portfolio
import database

logger = logging.getLogger(__name__)

# ============================================================
# Definizioni degli strumenti nel formato tool_use di Anthropic
# ============================================================

TOOL_DEFINITIONS = [
    {
        # Strumento per recuperare dati geopolitici in tempo reale
        "name": "get_geopolitical_data",
        "description": (
            "Recupera notizie geopolitiche in tempo reale da GDELT e NewsAPI. "
            "Restituisce un riepilogo degli eventi geopolitici attuali che "
            "potrebbero influenzare i mercati finanziari."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        # Strumento per ottenere analisi tecnica di un titolo
        "name": "get_technical_analysis",
        "description": (
            "Restituisce indicatori tecnici per un dato ticker azionario, "
            "inclusi medie mobili, RSI, MACD e bande di Bollinger."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {
                    "type": "string",
                    "description": "Il simbolo del ticker azionario (es. AAPL, MSFT, GOOGL).",
                },
                "period_days": {
                    "type": "integer",
                    "description": "Numero di giorni da analizzare. Default: 90.",
                    "default": 90,
                },
            },
            "required": ["ticker"],
        },
    },
    {
        # Strumento per visualizzare lo stato attuale del portafoglio
        "name": "get_portfolio_state",
        "description": (
            "Restituisce lo stato attuale del portafoglio: liquidit\u00e0 disponibile, "
            "posizioni aperte, valore totale e profitti/perdite."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        # Strumento per eseguire un ordine di compravendita simulato
        "name": "execute_trade",
        "description": (
            "Esegue un ordine simulato di acquisto (BUY) o vendita (SELL). "
            "Richiede motivazioni geopolitiche e tecniche dettagliate."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {
                    "type": "string",
                    "description": "Il simbolo del ticker azionario da negoziare.",
                },
                "action": {
                    "type": "string",
                    "enum": ["BUY", "SELL"],
                    "description": "Tipo di ordine: BUY per acquistare, SELL per vendere.",
                },
                "quantity": {
                    "type": "integer",
                    "description": "Numero di azioni da negoziare.",
                },
                "geopolitical_reasoning": {
                    "type": "string",
                    "description": "Motivazione geopolitica dettagliata per questa operazione.",
                },
                "technical_reasoning": {
                    "type": "string",
                    "description": "Motivazione tecnica dettagliata basata sugli indicatori.",
                },
                "confidence_score": {
                    "type": "number",
                    "description": "Punteggio di fiducia da 0 a 100 per questa operazione.",
                    "minimum": 0,
                    "maximum": 100,
                },
            },
            "required": [
                "ticker",
                "action",
                "quantity",
                "geopolitical_reasoning",
                "technical_reasoning",
                "confidence_score",
            ],
        },
    },
    {
        # Strumento per salvare intelligence weekend
        "name": "save_weekend_intelligence",
        "description": (
            "Salva l'analisi geopolitica del weekend per uso futuro "
            "quando le borse apriranno. Usa questo strumento SOLO durante il weekend."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "key_events": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Lista degli eventi geopolitici chiave identificati",
                },
                "market_implications": {
                    "type": "string",
                    "description": "Analisi delle implicazioni per i mercati alla riapertura",
                },
                "priority_assets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ticker da monitorare prioritariamente alla riapertura",
                },
                "full_analysis": {
                    "type": "string",
                    "description": "Analisi completa del contesto geopolitico attuale",
                },
            },
            "required": ["key_events", "market_implications", "priority_assets", "full_analysis"],
        },
    },
    {
        # Strumento per salvare briefing pre-market
        "name": "save_pre_market_briefing",
        "description": (
            "Salva un briefing pre-market con le priorita' per l'apertura dei mercati. "
            "Usa questo strumento SOLO quando le borse sono chiuse in giorno feriale."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "market_session": {
                    "type": "string",
                    "description": "Sessione di mercato di riferimento (es. EU_OPEN, US_OPEN)",
                },
                "key_events": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Eventi chiave che influenzeranno l'apertura",
                },
                "priority_assets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ticker da monitorare prioritariamente all'apertura",
                },
                "briefing_content": {
                    "type": "string",
                    "description": "Contenuto completo del briefing pre-market",
                },
            },
            "required": ["market_session", "key_events", "priority_assets", "briefing_content"],
        },
    },
    {
        # Strumento per quando l'agente decide di non operare
        "name": "do_nothing",
        "description": (
            "L'agente decide di non effettuare operazioni in questo ciclo. "
            "Deve fornire una motivazione dettagliata."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reasoning": {
                    "type": "string",
                    "description": "Motivazione dettagliata per cui non si effettuano operazioni.",
                },
            },
            "required": ["reasoning"],
        },
    },
]


# ============================================================
# Gestore delle chiamate agli strumenti
# ============================================================


async def handle_tool_call(tool_name: str, tool_input: dict, run_id: str) -> str:
    """
    Smista la chiamata allo strumento appropriato e restituisce il risultato.

    Args:
        tool_name: Nome dello strumento da eseguire.
        tool_input: Parametri di input per lo strumento.
        run_id: Identificatore univoco dell'esecuzione corrente dell'agente.

    Returns:
        Stringa JSON con il risultato dell'esecuzione dello strumento.
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    result = None

    try:
        if tool_name == "get_geopolitical_data":
            # Recupera dati geopolitici da entrambe le fonti
            logger.info("[%s] Recupero dati geopolitici in corso...", run_id)
            gdelt_data = await data_fetchers.fetch_gdelt_data()
            newsapi_data = await data_fetchers.fetch_newsapi_data()

            # Salva gli snapshot nel database per riferimento futuro
            database.insert_geopolitical_snapshot(
                run_id=run_id,
                source="GDELT",
                raw_data=json.dumps(gdelt_data),
                processed_summary=json.dumps(gdelt_data.get("results", [])[:5]),
            )
            database.insert_geopolitical_snapshot(
                run_id=run_id,
                source="NEWSAPI",
                raw_data=json.dumps(newsapi_data),
                processed_summary=json.dumps(newsapi_data.get("results", [])[:5]),
            )
            database.insert_agent_log(run_id=run_id, phase="GEOPOLITICAL",
                content="Dati geopolitici recuperati da GDELT e NewsAPI")

            result = {
                "gdelt_events": gdelt_data,
                "news_articles": newsapi_data,
                "fetched_at": timestamp,
            }

        elif tool_name == "get_technical_analysis":
            # Recupera dati di mercato e calcola indicatori tecnici
            ticker = tool_input["ticker"]
            period_days = tool_input.get("period_days", 90)
            logger.info(
                "[%s] Analisi tecnica per %s (%d giorni)...",
                run_id, ticker, period_days,
            )

            market_data = data_fetchers.fetch_market_data(
                ticker=ticker, period_days=period_days,
            )
            # Converti i dati di mercato in DataFrame per l'analisi tecnica
            import pandas as pd
            if market_data.get("data"):
                df = pd.DataFrame(market_data["data"])
                df.set_index("date", inplace=True)
                df.columns = [c.capitalize() for c in df.columns]
                analysis = technical_analysis.analyze_ticker(df)
            else:
                analysis = {"error": market_data.get("error", "Nessun dato disponibile")}
            database.insert_agent_log(run_id=run_id, phase="TECHNICAL",
                content=f"Analisi tecnica completata per {ticker}")

            result = {
                "ticker": ticker,
                "period_days": period_days,
                "analysis": analysis,
                "analyzed_at": timestamp,
            }

        elif tool_name == "get_portfolio_state":
            # Restituisce lo stato corrente del portafoglio
            logger.info("[%s] Recupero stato del portafoglio...", run_id)
            state = portfolio.get_portfolio_state()

            result = {
                "portfolio": state,
                "retrieved_at": timestamp,
            }

        elif tool_name == "execute_trade":
            # Esegue l'operazione di compravendita simulata
            ticker = tool_input["ticker"]
            action = tool_input["action"]
            quantity = tool_input["quantity"]
            geopolitical_reasoning = tool_input["geopolitical_reasoning"]
            technical_reasoning = tool_input["technical_reasoning"]
            confidence_score = tool_input["confidence_score"]

            logger.info(
                "[%s] Esecuzione ordine: %s %d %s (fiducia: %.1f%%)",
                run_id, action, quantity, ticker, confidence_score,
            )

            # Ottieni il prezzo corrente del titolo
            price_data = data_fetchers.fetch_market_data(ticker, period_days=5)
            if price_data.get("data"):
                current_price = price_data["data"][-1]["close"]
            else:
                return json.dumps({"error": f"Impossibile ottenere il prezzo per {ticker}"})

            # Esegui acquisto o vendita in base all'azione
            if action == "BUY":
                trade_result = portfolio.execute_buy(
                    ticker=ticker, quantity=quantity, price=current_price,
                    geo_reasoning=geopolitical_reasoning,
                    tech_reasoning=technical_reasoning,
                    confidence=confidence_score,
                )
            else:
                trade_result = portfolio.execute_sell(
                    ticker=ticker, quantity=quantity, price=current_price,
                    geo_reasoning=geopolitical_reasoning,
                    tech_reasoning=technical_reasoning,
                    confidence=confidence_score,
                )
            database.insert_agent_log(run_id=run_id, phase="DECISION",
                content=f"{action} {quantity} {ticker} @ {current_price:.2f} (conf: {confidence_score})")

            result = {
                "trade_executed": True,
                "ticker": ticker,
                "action": action,
                "quantity": quantity,
                "geopolitical_reasoning": geopolitical_reasoning,
                "technical_reasoning": technical_reasoning,
                "confidence_score": confidence_score,
                "trade_result": trade_result,
                "executed_at": timestamp,
            }

        elif tool_name == "save_weekend_intelligence":
            # Salva l'analisi geopolitica del weekend
            key_events = tool_input["key_events"]
            market_implications = tool_input["market_implications"]
            priority_assets = tool_input["priority_assets"]
            full_analysis = tool_input["full_analysis"]

            logger.info("[%s] Salvataggio intelligence weekend...", run_id)

            database.insert_weekend_intelligence(
                run_id=run_id,
                content=full_analysis,
                key_events=json.dumps(key_events),
                market_implications=market_implications,
            )

            database.insert_agent_log(
                run_id=run_id,
                phase="GEOPOLITICAL",
                content=f"Intelligence weekend salvata: {len(key_events)} eventi chiave, {len(priority_assets)} asset prioritari",
            )

            result = {
                "saved": True,
                "key_events_count": len(key_events),
                "priority_assets": priority_assets,
                "saved_at": timestamp,
            }

        elif tool_name == "save_pre_market_briefing":
            # Salva il briefing pre-market
            market_session = tool_input["market_session"]
            key_events = tool_input["key_events"]
            priority_assets = tool_input["priority_assets"]
            briefing_content = tool_input["briefing_content"]

            logger.info("[%s] Salvataggio briefing pre-market (%s)...", run_id, market_session)

            database.insert_pre_market_briefing(
                run_id=run_id,
                market_session=market_session,
                content=briefing_content,
                priority_assets=json.dumps(priority_assets),
            )

            database.insert_agent_log(
                run_id=run_id,
                phase="GEOPOLITICAL",
                content=f"Briefing pre-market salvato ({market_session}): {len(key_events)} eventi, {len(priority_assets)} asset",
            )

            result = {
                "saved": True,
                "market_session": market_session,
                "priority_assets": priority_assets,
                "saved_at": timestamp,
            }

        elif tool_name == "do_nothing":
            # L'agente ha deciso di non operare in questo ciclo
            reasoning = tool_input["reasoning"]
            logger.info(
                "[%s] Nessuna operazione eseguita. Motivo: %s",
                run_id, reasoning,
            )

            database.insert_agent_log(run_id=run_id, phase="DECISION",
                content=f"Nessuna operazione: {reasoning}")
            result = {
                "action": "no_trade",
                "reasoning": reasoning,
                "decided_at": timestamp,
            }

        else:
            # Strumento sconosciuto
            logger.warning("[%s] Strumento sconosciuto: %s", run_id, tool_name)
            result = {"error": f"Strumento sconosciuto: {tool_name}"}

    except Exception as e:
        # Gestione degli errori durante l'esecuzione dello strumento
        logger.error(
            "[%s] Errore nell'esecuzione di %s: %s",
            run_id, tool_name, str(e), exc_info=True,
        )
        result = {
            "error": str(e),
            "tool_name": tool_name,
            "failed_at": timestamp,
        }

    # Registra ogni chiamata dello strumento nel database
    database.insert_agent_log(
        run_id=run_id,
        phase="INFO",
        content=json.dumps({
            "tool_name": tool_name,
            "tool_input": tool_input,
        }, default=str),
    )

    # Restituisce il risultato serializzato come stringa JSON
    return json.dumps(result, ensure_ascii=False, default=str)
