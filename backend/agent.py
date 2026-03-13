"""
Logica principale dell'agente Claude Opus per investimenti geopolitici.
Supporta tre modalita' operative:
- WEEKEND: solo raccolta e analisi notizie geopolitiche
- PRE_MARKET: briefing preparatorio per apertura mercati
- FULL: ciclo completo con analisi tecnica e decisioni di trade
"""

import json
import logging
import os
from datetime import datetime, timezone
from uuid import uuid4

from anthropic import Anthropic

from tools import TOOL_DEFINITIONS, handle_tool_call
import database

logger = logging.getLogger(__name__)

# Modello di default
DEFAULT_MODEL = "claude-opus-4-5-20250514"

# Stato globale dell'agente, condiviso con il resto dell'applicazione
agent_status: dict = {
    "status": "idle",       # idle | running | error
    "last_run_id": None,
    "last_run_at": None,
    "error_details": None,
    "mode": "idle",         # idle | weekend | pre_market | full
}

# ============================================================
# Prompt di sistema per le diverse modalita'
# ============================================================

DEFAULT_SYSTEM_PROMPT = """Sei un agente di investimento geopolitico avanzato. Il tuo compito e' analizzare
la situazione geopolitica globale e prendere decisioni di investimento informate.

Segui questa procedura rigorosa ad ogni ciclo:

1. **Analisi Geopolitica**: Prima di tutto, chiama `get_geopolitical_data` per ottenere
   le ultime notizie e gli eventi geopolitici. Analizza attentamente conflitti, sanzioni,
   accordi commerciali, elezioni e altri eventi che possono influenzare i mercati.

2. **Stato del Portafoglio**: Chiama `get_portfolio_state` per conoscere le posizioni
   attuali, la liquidita' disponibile e le performance complessive.

3. **Analisi Tecnica**: Basandoti sulla tua analisi geopolitica, identifica i ticker
   piu' rilevanti e chiama `get_technical_analysis` per ciascuno di essi.
   Valuta medie mobili, RSI, MACD e bande di Bollinger.

4. **Decisione**: Per ogni opportunita' identificata, decidi se:
   - Eseguire un'operazione tramite `execute_trade`, fornendo motivazioni dettagliate
   - Non operare tramite `do_nothing`, spiegando il motivo

Criteri di decisione:
- Peso dell'analisi: 50% geopolitica, 50% tecnica
- Fornisci SEMPRE motivazioni dettagliate per ogni decisione
- Sii conservativo: preferisci la qualita' alla quantita' delle operazioni
- Non operare se le condizioni non sono chiaramente favorevoli
- Considera sempre il rischio di ribasso prima del potenziale di rialzo
- Diversifica le posizioni per ridurre il rischio geopolitico concentrato
"""

WEEKEND_SYSTEM_PROMPT = """Sei un agente di intelligence geopolitica. I mercati sono CHIUSI (weekend).
Il tuo compito e' SOLO raccogliere e analizzare notizie geopolitiche per prepararti alla settimana.

PROCEDURA WEEKEND:
1. Chiama `get_geopolitical_data` per raccogliere le ultime notizie geopolitiche.
2. Analizza attentamente gli eventi: conflitti, sanzioni, accordi, elezioni, crisi energetiche.
3. Chiama `save_weekend_intelligence` per salvare la tua analisi con:
   - key_events: lista degli eventi chiave identificati
   - market_implications: come questi eventi influenzeranno i mercati alla riapertura
   - priority_assets: ticker da monitorare prioritariamente lunedi'
   - full_analysis: analisi geopolitica completa

REGOLE WEEKEND:
- NON chiamare get_technical_analysis (borse chiuse, dati non aggiornati)
- NON chiamare execute_trade (non si opera nel weekend)
- NON chiamare get_portfolio_state (non necessario ora)
- Concentrati SOLO sull'analisi geopolitica e il salvataggio dell'intelligence
- Sii sintetico ma preciso nell'analisi
"""

PRE_MARKET_SYSTEM_PROMPT = """Sei un agente di preparazione pre-market. I mercati sono CHIUSI ma e' un giorno feriale.
Il tuo compito e' preparare un briefing per l'apertura dei mercati.

PROCEDURA PRE-MARKET:
1. Chiama `get_geopolitical_data` per raccogliere le ultime notizie.
2. Analizza gli eventi e la loro rilevanza per l'apertura imminente.
3. Chiama `save_pre_market_briefing` per salvare il briefing con:
   - market_session: quale mercato sta per aprire (EU_OPEN o US_OPEN)
   - key_events: eventi chiave per l'apertura
   - priority_assets: ticker da monitorare all'apertura
   - briefing_content: briefing completo con raccomandazioni

REGOLE PRE-MARKET:
- NON chiamare execute_trade (borse chiuse)
- NON chiamare get_technical_analysis (dati non aggiornati con borse chiuse)
- Puoi chiamare get_portfolio_state per verificare le posizioni attuali
- Concentrati sulla preparazione strategica per l'apertura
- Identifica rischi e opportunita' basati sulle notizie recenti
"""


def _build_system_prompt(mode="full"):
    """Costruisce il system prompt in base alla modalita' operativa."""
    if mode == "full":
        custom_prompt = database.get_setting("system_prompt")
        base_prompt = custom_prompt if custom_prompt else DEFAULT_SYSTEM_PROMPT
    elif mode == "weekend":
        base_prompt = WEEKEND_SYSTEM_PROMPT
    elif mode == "pre_market":
        base_prompt = PRE_MARKET_SYSTEM_PROMPT
    else:
        base_prompt = DEFAULT_SYSTEM_PROMPT

    # Aggiungi intelligence accumulata (per full e pre_market)
    if mode in ("full", "pre_market"):
        weekend_intel = database.get_latest_weekend_intelligence()
        if weekend_intel:
            base_prompt += "\n\n--- INTELLIGENCE WEEKEND ACCUMULATA ---\n"
            base_prompt += weekend_intel["content"]
            if weekend_intel.get("market_implications"):
                base_prompt += f"\n\nIMPLICAZIONI MERCATO: {weekend_intel['market_implications']}"
            base_prompt += "\n--- FINE INTELLIGENCE WEEKEND ---\n"

    if mode == "full":
        pre_market = database.get_latest_pre_market_briefing()
        if pre_market:
            base_prompt += "\n\n--- BRIEFING PRE-MARKET ---\n"
            base_prompt += pre_market["content"]
            base_prompt += "\n--- FINE BRIEFING PRE-MARKET ---\n"

    # Aggiungi il contenuto dei documenti tecnici come contesto (solo full)
    if mode == "full":
        docs = database.get_document_contents()
        if docs:
            base_prompt += "\n\n--- DOCUMENTI DI ANALISI TECNICA ---\n"
            for doc in docs:
                base_prompt += f"\n### {doc['filename']}\n{doc['content']}\n"
            base_prompt += "\n--- FINE DOCUMENTI ---\n"

    return base_prompt


def _get_tools_for_mode(mode="full"):
    """Restituisce i tool disponibili in base alla modalita'."""
    if mode == "weekend":
        allowed = {"get_geopolitical_data", "save_weekend_intelligence", "do_nothing"}
    elif mode == "pre_market":
        allowed = {"get_geopolitical_data", "get_portfolio_state",
                   "save_pre_market_briefing", "do_nothing"}
    else:
        return TOOL_DEFINITIONS

    return [t for t in TOOL_DEFINITIONS if t["name"] in allowed]


def _get_model_name():
    """Restituisce il nome del modello Claude da usare."""
    return database.get_setting("model_name", DEFAULT_MODEL)


def _get_api_key():
    """Restituisce la chiave API Anthropic (da DB o da env)."""
    key = database.get_setting("anthropic_api_key")
    return key if key else os.environ.get("ANTHROPIC_API_KEY", "")


def _get_max_tokens(mode="full"):
    """Restituisce max_tokens in base alla modalita' (ottimizzazione costi)."""
    if mode == "weekend":
        return 2048
    elif mode == "pre_market":
        return 4096
    else:
        return 8096


def _get_user_message(mode="full"):
    """Restituisce il messaggio iniziale in base alla modalita'."""
    if mode == "weekend":
        return (
            "Siamo nel weekend, i mercati sono chiusi. "
            "Esegui la tua analisi geopolitica raccogliendo le ultime notizie "
            "e salva l'intelligence per la prossima settimana."
        )
    elif mode == "pre_market":
        return (
            "I mercati sono attualmente chiusi ma e' un giorno feriale. "
            "Prepara un briefing pre-market raccogliendo le ultime notizie "
            "e analizzando le implicazioni per l'apertura imminente."
        )
    else:
        return (
            "Esegui il tuo ciclo di analisi completo. "
            "Analizza la situazione geopolitica attuale, "
            "valuta il portafoglio e prendi le decisioni "
            "di investimento appropriate."
        )


# ============================================================
# Funzione principale dell'agente
# ============================================================


async def run_agent(mode="full") -> dict:
    """
    Esegue un ciclo dell'agente di investimento nella modalita' specificata.

    Args:
        mode: "weekend", "pre_market", o "full"

    Returns:
        Dizionario con il riepilogo dell'esecuzione.
    """
    global agent_status

    run_id = str(uuid4())
    run_start = datetime.now(timezone.utc).isoformat()

    agent_status = {
        "status": "running",
        "last_run_id": run_id,
        "last_run_at": run_start,
        "error_details": None,
        "mode": mode,
    }

    logger.info("Avvio esecuzione agente. Run ID: %s, Modalita': %s", run_id, mode.upper())

    try:
        api_key = _get_api_key()
        model_name = _get_model_name()
        system_prompt = _build_system_prompt(mode=mode)
        tools = _get_tools_for_mode(mode=mode)
        max_tokens = _get_max_tokens(mode=mode)
        user_message = _get_user_message(mode=mode)

        client = Anthropic(api_key=api_key)

        messages = [
            {"role": "user", "content": user_message},
        ]

        database.insert_agent_log(
            run_id=run_id,
            phase="INFO",
            content=json.dumps({
                "event": "agent_start",
                "model": model_name,
                "run_id": run_id,
                "mode": mode,
            }),
        )

        response = client.messages.create(
            model=model_name,
            max_tokens=max_tokens,
            system=system_prompt,
            tools=tools,
            messages=messages,
        )

        max_iterations = 25 if mode == "full" else 10
        iteration = 0

        while response.stop_reason == "tool_use" and iteration < max_iterations:
            iteration += 1
            logger.info(
                "[%s] Iterazione %d (%s) - Elaborazione chiamate strumenti...",
                run_id, iteration, mode.upper(),
            )

            messages.append({
                "role": "assistant",
                "content": response.content,
            })

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    tool_name = block.name
                    tool_input = block.input
                    tool_use_id = block.id

                    logger.info(
                        "[%s] Chiamata strumento: %s (ID: %s)",
                        run_id, tool_name, tool_use_id,
                    )

                    result = await handle_tool_call(
                        tool_name=tool_name,
                        tool_input=tool_input,
                        run_id=run_id,
                    )

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": result,
                    })

            messages.append({
                "role": "user",
                "content": tool_results,
            })

            response = client.messages.create(
                model=model_name,
                max_tokens=max_tokens,
                system=system_prompt,
                tools=tools,
                messages=messages,
            )

        if iteration >= max_iterations:
            logger.warning(
                "[%s] Raggiunto il limite massimo di iterazioni (%d).",
                run_id, max_iterations,
            )

        final_response = ""
        for block in response.content:
            if hasattr(block, "text"):
                final_response += block.text

        logger.info("[%s] Esecuzione completata (%s). Risposta finale ricevuta.", run_id, mode.upper())

        run_end = datetime.now(timezone.utc).isoformat()
        database.insert_agent_log(
            run_id=run_id,
            phase="INFO",
            content=json.dumps({
                "event": "agent_complete",
                "final_response": final_response[:500],
                "iterations": iteration,
                "stop_reason": response.stop_reason,
                "mode": mode,
            }),
        )

        summary = {
            "run_id": run_id,
            "started_at": run_start,
            "completed_at": run_end,
            "iterations": iteration,
            "stop_reason": response.stop_reason,
            "final_response": final_response,
            "status": "completed",
            "mode": mode,
        }

        agent_status = {
            "status": "idle",
            "last_run_id": run_id,
            "last_run_at": run_end,
            "error_details": None,
            "mode": mode,
        }

        return summary

    except Exception as e:
        error_msg = str(e)
        logger.error(
            "[%s] Errore durante l'esecuzione dell'agente (%s): %s",
            run_id, mode, error_msg, exc_info=True,
        )

        error_time = datetime.now(timezone.utc).isoformat()
        try:
            database.insert_agent_log(
                run_id=run_id,
                phase="ERROR",
                content=json.dumps({"error": error_msg, "mode": mode}),
            )
        except Exception as log_err:
            logger.error(
                "[%s] Impossibile registrare l'errore nel database: %s",
                run_id, str(log_err),
            )

        agent_status = {
            "status": "error",
            "last_run_id": run_id,
            "last_run_at": error_time,
            "error_details": error_msg,
            "mode": mode,
        }

        return {
            "run_id": run_id,
            "started_at": run_start,
            "completed_at": error_time,
            "iterations": 0,
            "stop_reason": "error",
            "final_response": None,
            "status": "error",
            "error": error_msg,
            "mode": mode,
        }
