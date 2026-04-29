"""
Decision Agent — Claude Sonnet 4.5
Orchestra gerarchica per decisioni di trading ad alto rischio.
Attivato solo quando il Watchdog rileva un segnale significativo (urgency >= 5).
Throttle: max 1 run per ora per rispettare il budget mensile (~$13-14/mese).

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
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from anthropic import Anthropic

logger = logging.getLogger(__name__)

# Claude Sonnet 4.5 — decisioni di trading (max 1/ora, ~$13/mese)
# NOTA: verifica che questo ID sia corretto sulla tua dashboard Anthropic
DECISION_MODEL = "claude-sonnet-4-5-20250929"
DECISION_MODEL_FALLBACK = "claude-sonnet-4-20250514"  # Fallback a Sonnet 4 se 4.5 non disponibile

# ============================================================
# System Prompts
# ============================================================

DECISION_SYSTEM_PROMPT_DEFAULT = """Sei il Decision Agent di GeoInvest AI — un sistema di trading autonomo ad alto rischio.

HAI PIENA AUTONOMIA DECISIONALE. Non ci sono restrizioni conservative.
Il tuo obiettivo è massimizzare i rendimenti accettando rischi calcolati.

CONTESTO CHE RICEVI (sintesi a cascata prodotte dallo Scout):
1. Report 3W (macro 3 settimane): regime di mercato, trend strutturali, rischi sistemici
2. Report 4D (medio termine, ultimi 2): trend consolidati, rotazioni settoriali
3. Report 8H (breve termine, ultimi 3): bias di periodo, hot tickers, catalisti
4. Intelligence Buffer L0 recente: micro-cards degli ultimi 20-40 minuti
5. Report Tecnico: analisi quantitativa da DeepSeek-V3
6. Stato Portafoglio corrente

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

UNIVERSO INVESTIBILE:
Hai accesso a TUTTI i mercati liquidi via yfinance, inclusi:
- Azioni USA (NYSE/NASDAQ): es. NVDA, TSLA, AAPL, XOM
- ETF e indici: SPY, QQQ, GLD, USO
- Azioni europee: ENI.MI, SAN.PA, SAP.DE
- CRYPTOVALUTE 24/7 (formato yfinance "TICKER-USD"):
  BTC-USD, ETH-USD, SOL-USD, BNB-USD, XRP-USD, ADA-USD, DOGE-USD,
  AVAX-USD, DOT-USD, LINK-USD, MATIC-USD, LTC-USD
- Le crypto sono particolarmente adatte all'analisi tecnica perche':
  (1) sono 24/7 senza gap di apertura, (2) hanno volumi alti e liquidita',
  (3) rispondono fortemente ai pattern tecnici e al sentiment retail.

REGOLE:
- Preferisci l'azione all'inazione quando i segnali convergono
- Confidence threshold per operare: >= 50%
- Ogni decisione deve avere un logic_chain dettagliato che integra geo+tech
- In modalita' Pure Macro (senza dati tecnici): puoi operare con sola analisi geopolitica se confidence >= 70%
- Per le crypto, il sentiment retail (Reddit r/CryptoCurrency, r/Bitcoin) e' un input fondamentale
- Durante orari extra-market (notte/weekend), opera prevalentemente su crypto"""


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
            "Esegue un ordine BUY o SELL. Allocazione fino al 50% del portafoglio. "
            "Stop-loss opzionale (decidi tu in base all'ATR e al contesto)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Ticker azionario"},
                "action": {"type": "string", "enum": ["BUY", "SELL"]},
                "quantity": {"type": "integer", "description": "Numero azioni", "minimum": 1},
                "stop_loss": {"type": "number", "description": "Prezzo stop-loss (0 = nessuno)"},
                "take_profit": {"type": "number", "description": "Prezzo take-profit (0 = nessuno)"},
                "logic_chain": {"type": "string", "description": "Chain of Thought completo: integra geo+tech reasoning"},
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
        TIER_8H, TIER_4D, TIER_3W,
    )

    logger.info("[%s][DECISION] === Avvio Decision Agent ===", run_id)
    start_time = datetime.now(timezone.utc)

    # Checkpoint
    _save_checkpoint(run_id, "decision", "RUNNING", {"phase": "context_loading"})

    # --- FASE A: Ingestione Contesto a Cascata ---
    context_loaded = {}

    # 3W macro (lungo termine)
    rep_3w = get_latest_aggregated_reports(database, TIER_3W, n=1)
    context_loaded["report_3w"] = len(rep_3w) > 0

    # 4D (medio termine, ultimi 2)
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
        rep_3w, rep_4d, rep_8h, recent_buffer, tech_report, portfolio_state, docs
    )

    database.insert_agent_log(run_id, "DECISION_CONTEXT",
        json.dumps({"context_loaded": context_loaded, "buffer_size": len(recent_buffer)}))

    # --- FASE B+C: Valutazione e Esecuzione ---
    _save_checkpoint(run_id, "decision", "RUNNING", {"phase": "evaluation"})

    model = _select_model()
    client = _get_client()
    system_prompt = _get_decision_prompt()

    # Prima prova Opus, se non disponibile usa Sonnet
    try:
        messages = [{"role": "user", "content": user_message}]

        response = client.messages.create(
            model=model,
            max_tokens=16000,
            system=system_prompt,
            tools=DECISION_TOOLS,
            messages=messages,
        )
        used_model = model
    except Exception as model_err:
        if model == DECISION_MODEL:
            logger.warning("[%s][DECISION] %s non disponibile (%s), fallback a %s",
                           run_id, DECISION_MODEL, model_err, DECISION_MODEL_FALLBACK)
            model = DECISION_MODEL_FALLBACK
            response = client.messages.create(
                model=model,
                max_tokens=8192,
                system=system_prompt,
                tools=DECISION_TOOLS,
                messages=messages,
            )
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

        response = client.messages.create(
            model=used_model,
            max_tokens=16000,
            system=system_prompt,
            tools=DECISION_TOOLS,
            messages=messages,
        )

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
# Context Builder
# ============================================================

def _build_context_message(rep_3w, rep_4d, rep_8h, buffer, tech_report, portfolio_state, docs) -> str:
    """
    Costruisce il messaggio di contesto per il Decision Agent.
    Riceve liste di report aggregati (dal nuovo sistema cascata 3W/4D/8H).
    """
    parts = []

    # === Report 3W (macro, lungo termine) ===
    if rep_3w:
        r = rep_3w[0]["report"]
        ts = rep_3w[0].get("timestamp", "?")
        parts.append(f"""=== REPORT MACRO 3W ({ts}) ===
Regime: {r.get('regime', '?')}
Sintesi: {r.get('synthesis', r.get('summary_text', 'N/A'))}
Rischi strutturali: {', '.join(r.get('structural_risks', []))[:600]}
Temi lungo termine: {', '.join(r.get('long_term_themes', []))[:400]}
Rotazione settoriale: {json.dumps(r.get('sector_rotation', {}), ensure_ascii=False)[:300]}
Strategia macro: {r.get('macro_strategy', 'N/A')[:500]}""")
    else:
        parts.append("=== REPORT MACRO 3W === Non ancora disponibile (servono almeno 3 settimane di history)")

    # === Report 4D (medio termine, ultimi 2) ===
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
