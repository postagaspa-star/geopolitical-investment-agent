"""
Decision Crypto Agent — DeepSeek-R1 reasoning, focus ESCLUSIVO crypto 24/7.

Riceve l'output di technical_crypto.py + buffer intelligence + report aggregati
+ documenti specifici crypto. Decide se eseguire trade su crypto ClawStreet.

Differenze rispetto a decision.py:
  - SOLO crypto: pre-validation hard-fails su qualunque ticker non in
    ClawStreet's crypto subset
  - Documenti separati: carica solo doc con category='crypto' (non i doc
    generici dei mercati equity)
  - Prompt specializzato su rischio crypto: leverage, liquidation, depeg,
    regulatory, hack, exchange risk
  - Engine fisso DeepSeek-R1 (no fallback Sonnet — no Anthropic dependency)

Costo per run: ~$0.006 (R1 reasoning).
Schedule: ogni 1 ora, 24/7.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from uuid import uuid4

import aiohttp

logger = logging.getLogger(__name__)

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_R1_MODEL = "deepseek-reasoner"

# Cooldown per evitare doppi run nello stesso ora (es. test manuali)
CRYPTO_COOLDOWN_KEY = "last_decision_crypto_run_at"
CRYPTO_COOLDOWN_SECONDS = 50 * 60   # 50 min: lascia 10 min di margine vs schedule 1h


CRYPTO_DECISION_PROMPT_DEFAULT = """Sei il Decision Agent CRYPTO di GeoInvest AI — sistema autonomo focalizzato ESCLUSIVAMENTE sui mercati crypto (BTC, ETH, SOL, ecc.).

UNIVERSO INVESTIBILE — VINCOLO RIGIDO:
Solo i 14 ticker crypto supportati da ClawStreet (formato yfinance):
  BTC-USD, ETH-USD, SOL-USD, DOGE-USD, AVAX-USD, ADA-USD, XRP-USD,
  LTC-USD, DOT-USD, LINK-USD, UNI-USD, ATOM-USD, MATIC-USD, NEAR-USD.

NON tradare crypto fuori da questa lista (BNB, SHIB, AAVE, PEPE, ecc.).
NON tradare equity (azioni/ETF) — quelli sono dominio del Decision normale.

CONTESTO CHE RICEVI:
1. Report tecnico crypto-specifico (tech_report) da Technical Crypto Agent
2. Documenti tecnici dedicati al crypto (analisi on-chain, framework di
   rischio, strategie di entry/exit)
3. Buffer intelligence ultime 60 min (sentiment retail Reddit/X, news
   regolamentari, hack, depeg, whale alerts)
4. Stato portafoglio corrente (cash + posizioni crypto già aperte)

REGOLE OPERATIVE:
- Allocazione max 30% del cash per singola posizione crypto (volatilità alta)
- Stop-loss tecnico OBBLIGATORIO (non opzionale come per l'equity)
- Confidence threshold per BUY: >= 60%
- Confidence threshold per SELL/take-profit: >= 50%
- Mai più di 5 posizioni crypto aperte contemporaneamente
- Se unrealized loss > 8% e tecnico ha bias bearish → considera SELL stop-loss
- Se unrealized gain > 25% → valuta SELL parziale (50%) per take-profit

RISCHI CRYPTO-SPECIFIC da valutare prima di operare:
- Liquidità: per altcoin minori (DOT, ATOM, NEAR) il book può svuotarsi
- Regolamentazione: SEC/MiCA possono cambiare regime di un singolo asset
- Sentiment regime: bull market → bias BUY su breakouts, bear → bias SELL su rallies
- Funding rate squeezes: se rilevati nel buffer, attesa fino a stabilizzazione

FASE DECISIONALE:
A. Valuta: ci sono setup tecnici con confidence >= 60%?
B. Cross-check con sentiment retail (FOMO/FUD/equilibrato)
C. Se SI → execute_trade. Se NO → do_nothing con reasoning dettagliato.

Per CHIUDERE una posizione: get_portfolio_state per leggere quantity,
poi execute_trade(action='SELL', quantity=...) — parziale o totale.

Output: usa SOLO i tool messi a disposizione (execute_trade, do_nothing,
get_portfolio_state). MAI rispondere in plain text, sempre tool call."""


def _get_deepseek_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        try:
            import database as _db
            key = _db.get_setting("deepseek_api_key", "") or ""
        except Exception:
            pass
    return key


def _get_crypto_decision_prompt() -> str:
    """Carica il prompt custom dalle settings (chiave 'prompt_decision_crypto')."""
    try:
        import database as _db
        custom = _db.get_setting("prompt_decision_crypto", "")
        if custom and isinstance(custom, str) and custom.strip():
            return custom
    except Exception:
        pass
    return CRYPTO_DECISION_PROMPT_DEFAULT


def is_cooldown_active() -> tuple[bool, int]:
    """Ritorna (True, seconds_left) se cooldown 50min ancora attivo."""
    try:
        import database as _db
        last_iso = _db.get_setting(CRYPTO_COOLDOWN_KEY, "") or ""
        if not last_iso:
            return False, 0
        last_dt = datetime.fromisoformat(last_iso)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
        if elapsed < CRYPTO_COOLDOWN_SECONDS:
            return True, int(CRYPTO_COOLDOWN_SECONDS - elapsed)
        return False, 0
    except Exception:
        return False, 0


def _record_run_timestamp():
    try:
        import database as _db
        _db.set_setting(CRYPTO_COOLDOWN_KEY, datetime.now(timezone.utc).isoformat())
    except Exception:
        pass


# ─── Tools ──────────────────────────────────────────────────────────────────

CRYPTO_DECISION_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "execute_trade",
            "description": (
                "Esegue un trade crypto. action='BUY' apre/incrementa posizione, "
                "action='SELL' chiude/riduce. quantity = numero unità (intero)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string",
                               "description": "Crypto in formato yfinance (BTC-USD, ETH-USD, ...)"},
                    "action": {"type": "string", "enum": ["BUY", "SELL"]},
                    "quantity": {"type": "integer", "minimum": 1},
                    "stop_loss": {"type": "number"},
                    "take_profit": {"type": "number"},
                    "logic_chain": {"type": "string", "description": "Reasoning tecnico+sentiment"},
                    "confidence_level": {"type": "number", "minimum": 0, "maximum": 100},
                },
                "required": ["ticker", "action", "quantity", "logic_chain", "confidence_level"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "do_nothing",
            "description": "Non operare; motivazione obbligatoria.",
            "parameters": {
                "type": "object",
                "properties": {"reasoning": {"type": "string"}},
                "required": ["reasoning"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_portfolio_state",
            "description": "Stato corrente cash + posizioni.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


# ─── Tool handler ───────────────────────────────────────────────────────────

async def _handle_tool(tool_name: str, tool_input: dict, run_id: str) -> str:
    import data_fetchers
    import database
    import portfolio

    timestamp = datetime.now(timezone.utc).isoformat()

    try:
        if tool_name == "execute_trade":
            ticker = tool_input["ticker"]
            action = tool_input["action"]
            quantity = int(tool_input["quantity"])
            logic_chain = tool_input["logic_chain"]
            confidence = tool_input["confidence_level"]
            stop_loss = tool_input.get("stop_loss") or None
            take_profit = tool_input.get("take_profit") or None

            # Pre-validation HARD: solo crypto ClawStreet supportate
            try:
                from clawstreet_universe import is_supported, to_clawstreet_format
                if not is_supported(ticker):
                    cs_format = to_clawstreet_format(ticker)
                    error_msg = (
                        f"REJECTED: '{ticker}' (CS: '{cs_format}') non è una crypto "
                        f"ClawStreet-supported. Universo crypto consentito: BTC-USD, "
                        f"ETH-USD, SOL-USD, DOGE-USD, AVAX-USD, ADA-USD, XRP-USD, "
                        f"LTC-USD, DOT-USD, LINK-USD, UNI-USD, ATOM-USD, MATIC-USD, NEAR-USD."
                    )
                    database.insert_agent_log(run_id, "DECISION_CRYPTO_REJECTED", json.dumps({
                        "ticker": ticker, "cs_format": cs_format, "action": action,
                        "reason": "not_in_clawstreet_crypto_universe",
                    }))
                    return json.dumps({"error": error_msg, "rejected": True})
                # Verifica anche che sia crypto (non equity erroneamente whitelisted)
                t_up = ticker.upper()
                is_crypto = t_up.endswith("-USD") or t_up.startswith("X:")
                if not is_crypto:
                    return json.dumps({
                        "error": f"REJECTED: '{ticker}' non è crypto. Decision Crypto opera SOLO su crypto.",
                        "rejected": True,
                    })
            except ImportError:
                pass  # degrade graceful se modulo manca

            current_price = await data_fetchers.fetch_current_price(ticker)
            if not current_price or current_price <= 0:
                return json.dumps({"error": f"Prezzo non disponibile per {ticker}"})

            if action == "BUY":
                result = portfolio.execute_buy(
                    ticker, quantity, current_price,
                    geo_reasoning="(crypto-only run)", tech_reasoning=logic_chain,
                    confidence=confidence,
                )
            else:
                result = portfolio.execute_sell(
                    ticker, quantity, current_price,
                    geo_reasoning="(crypto-only run)", tech_reasoning=logic_chain,
                    confidence=confidence,
                )

            database.insert_agent_log(run_id, "DECISION_CRYPTO_TRADE", json.dumps({
                "ticker": ticker, "action": action, "qty": quantity,
                "price": current_price, "confidence": confidence,
                "stop_loss": stop_loss, "take_profit": take_profit,
            }, default=str))

            # ClawStreet mirror via wrapper centralizzato
            try:
                from clawstreet_mirror import mirror_trade as _mirror
                trade_id = result.get("trade_id") if isinstance(result, dict) else None
                await _mirror(
                    trade_id=trade_id,
                    ticker=ticker, action=action, quantity=quantity,
                    reasoning=logic_chain[:280],
                    run_id=run_id,
                )
            except Exception as exc:
                logger.warning("[%s] Mirror crypto failed: %s", run_id, exc)

            return json.dumps({
                "executed": True, "ticker": ticker, "action": action,
                "quantity": quantity, "price": current_price,
                "result": result, "at": timestamp,
            }, default=str)

        elif tool_name == "do_nothing":
            reasoning = tool_input["reasoning"]
            database.insert_agent_log(run_id, "DECISION_CRYPTO_NO_TRADE",
                json.dumps({"reasoning": reasoning[:1000]}))
            return json.dumps({"action": "no_trade", "reasoning": reasoning})

        elif tool_name == "get_portfolio_state":
            state = portfolio.get_portfolio_state()
            # Filtra a sole posizioni crypto per chiarezza
            try:
                from clawstreet_universe import to_clawstreet_format, is_supported
                positions = state.get("positions", [])
                state["positions_crypto"] = [
                    p for p in positions
                    if (p.get("ticker", "").upper().endswith("-USD")
                        and is_supported(to_clawstreet_format(p.get("ticker", ""))))
                ]
            except ImportError:
                pass
            return json.dumps({"portfolio": state, "at": timestamp}, default=str)

        else:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})

    except Exception as e:
        logger.error("[%s][DECISION-CRYPTO] Tool error %s: %s", run_id, tool_name, e, exc_info=True)
        return json.dumps({"error": str(e), "tool": tool_name})


# ─── Context builder ────────────────────────────────────────────────────────

def _load_crypto_documents(database) -> list[dict]:
    """
    Carica solo i documenti con category='crypto'.
    Se la colonna `category` non esiste ancora (migrazione non applicata),
    ritorna lista vuota (degrade graceful).
    """
    try:
        return database.get_document_contents(category="crypto") or []
    except Exception:
        return []


def _build_context(tech_report: dict, recent_buffer: list, portfolio_state: dict,
                   crypto_docs: list) -> str:
    parts = []
    parts.append(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
    parts.append("=" * 60)
    parts.append("REPORT TECNICO CRYPTO (DeepSeek-V3):")
    parts.append(json.dumps(tech_report, default=str, ensure_ascii=False)[:8000])
    parts.append("=" * 60)
    parts.append(f"PORTAFOGLIO CORRENTE:")
    parts.append(json.dumps(portfolio_state, default=str, ensure_ascii=False)[:3000])
    parts.append("=" * 60)
    parts.append(f"INTELLIGENCE BUFFER (ultimi 60 min, {len(recent_buffer)} card):")
    for c in recent_buffer[:20]:
        s = c.get("micro_summary", "")[:200]
        st = c.get("source_type", "?")
        parts.append(f"  [{st}] {s}")
    parts.append("=" * 60)
    if crypto_docs:
        parts.append(f"DOCUMENTI CRYPTO ({len(crypto_docs)} caricati):")
        for d in crypto_docs[:5]:
            name = d.get("filename") or d.get("name", "?")
            content = (d.get("content") or "")[:1500]
            parts.append(f"--- {name} ---\n{content}")
    else:
        parts.append("DOCUMENTI CRYPTO: nessuno caricato (vai in Impostazioni → Documenti Crypto)")
    return "\n".join(parts)


# ─── Tool loop ──────────────────────────────────────────────────────────────

async def _run_r1_loop(run_id: str, system_prompt: str, user_message: str
                        ) -> tuple[list, str, int]:
    api_key = _get_deepseek_key()
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY non configurata")

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    trades_executed: list[dict] = []
    iteration = 0
    max_iterations = 8
    final_text = ""

    async with aiohttp.ClientSession() as session:
        while iteration < max_iterations:
            payload = {
                "model": DEEPSEEK_R1_MODEL,
                "messages": messages,
                "tools": CRYPTO_DECISION_TOOLS,
                "tool_choice": "auto",
                "max_tokens": 6000,
            }
            async with session.post(
                DEEPSEEK_API_URL, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise ValueError(f"DeepSeek-R1 HTTP {resp.status}: {body[:300]}")
                data = await resp.json()

            choice = data["choices"][0]
            finish_reason = choice.get("finish_reason", "")
            msg = choice["message"]

            import re as _re
            raw_text = msg.get("content") or ""
            final_text = _re.sub(r"<think>.*?</think>", "", raw_text,
                                 flags=_re.DOTALL).strip()

            if finish_reason != "tool_calls" or not msg.get("tool_calls"):
                break

            iteration += 1
            messages.append({
                "role": "assistant",
                "content": msg.get("content"),
                "tool_calls": msg["tool_calls"],
            })

            for tc in msg["tool_calls"]:
                tool_name = tc["function"]["name"]
                tool_input = json.loads(tc["function"]["arguments"])
                result = await _handle_tool(tool_name, tool_input, run_id)

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

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

    return trades_executed, final_text, iteration


# ─── Main entry ─────────────────────────────────────────────────────────────

async def run_crypto_decision(run_id: str, tech_report: dict) -> dict:
    """Esegue il Decision Crypto Agent con DeepSeek-R1 reasoning."""
    import database
    from agents.scout import get_recent_buffer
    import portfolio

    logger.info("[%s][DECISION-CRYPTO] === Avvio ===", run_id)
    start_time = datetime.now(timezone.utc)

    # Carica contesto specifico crypto
    recent_buffer = get_recent_buffer(database, minutes=60)
    portfolio_state = portfolio.get_portfolio_state()
    crypto_docs = _load_crypto_documents(database)

    user_message = _build_context(tech_report, recent_buffer, portfolio_state, crypto_docs)

    database.insert_agent_log(run_id, "DECISION_CRYPTO_CONTEXT", json.dumps({
        "tech_engine": tech_report.get("engine", "?"),
        "buffer_size": len(recent_buffer),
        "crypto_docs": len(crypto_docs),
        "portfolio_total": portfolio_state.get("total_value", 0),
    }, default=str))

    try:
        trades, final_text, iterations = await _run_r1_loop(
            run_id, _get_crypto_decision_prompt(), user_message,
        )
        used_model = DEEPSEEK_R1_MODEL
        _record_run_timestamp()
    except Exception as exc:
        logger.error("[%s][DECISION-CRYPTO] R1 fallito: %s", run_id, exc, exc_info=True)
        try:
            import traceback as _tb
            database.insert_agent_log(run_id, "DECISION_CRYPTO_ERROR", json.dumps({
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:500],
                "traceback": _tb.format_exc()[:2000],
            }, default=str))
        except Exception:
            pass
        return {"decision": "ERROR", "trades": [], "error": str(exc),
                "model": "deepseek-r1-error"}

    duration = (datetime.now(timezone.utc) - start_time).total_seconds()

    database.insert_agent_log(run_id, "DECISION_CRYPTO_COMPLETE", json.dumps({
        "model": used_model,
        "iterations": iterations,
        "trades_executed": len(trades),
        "duration_seconds": round(duration, 1),
        "final_text": final_text[:500],
    }, default=str))

    return {
        "run_id": run_id,
        "decision": "TRADE" if trades else "NO_TRADE",
        "trades": trades,
        "no_trade_reasoning": final_text if not trades else "",
        "duration_seconds": duration,
        "model": used_model,
        "iterations": iterations,
    }
