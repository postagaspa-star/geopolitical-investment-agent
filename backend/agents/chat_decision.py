"""
Chat Decision — chat conversazionale con i Decision Agent (Standard / Crypto).

Differenze rispetto a chat_assistant.py (Coach Cards):
  - chat_assistant: read-only, analizza decisioni passate, non puo' eseguire
  - chat_decision:  ATTIVO, puo' proporre trade che l'utente conferma

Routing:
  - agent_type='standard' → Claude Sonnet 4.5 (stesso engine del Decision Agent)
  - agent_type='crypto'   → DeepSeek-R1 (stesso engine del Decision Crypto)

Output strutturato: il modello deve rispondere con un JSON che contiene sempre
un campo "text" (messaggio per l'utente in italiano) e opzionalmente un
"proposed_trade" se vuole proporre un'azione concreta. Il frontend renderizza
il proposed_trade come card con bottone "Esegui" — il trade viene eseguito
solo dopo conferma esplicita dell'utente.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

# ─── Engine config ──────────────────────────────────────────────────────────

CLAUDE_MODEL = "claude-sonnet-4-20250514"
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_R1_MODEL = "deepseek-reasoner"

MAX_HISTORY_MESSAGES = 30   # ultimi N msg passati al modello (per contenere context)
MAX_OHLCV_TICKERS = 8       # quanti ticker live OHLCV iniettare (top P&L positions)


# ─── System prompts ─────────────────────────────────────────────────────────

# Blocco azioni proposte — condiviso tra equity e crypto.
# L'utente puo' chiedere stop-loss / take-profit / direttive direttamente
# in chat. L'AI risponde con `proposed_actions[]` e l'utente conferma ogni
# singola azione cliccando il bottone nella card.
ACTIONS_BLOCK = """═══════════════════════════════════════════════════════════════════════
AZIONI CHE PUOI PROPORRE — array proposed_actions[]
═══════════════════════════════════════════════════════════════════════

Ogni azione e' un oggetto con un campo "type" + parametri specifici.
NIENTE viene eseguito automaticamente: l'utente conferma cliccando.
Puoi proporre piu' azioni nello stesso messaggio (array).

1) execute_trade — apri o chiudi una posizione
   {
     "type": "execute_trade",
     "ticker": "NVDA",
     "action": "BUY" | "SELL",
     "quantity": 5,
     "confidence_level": "HIGH" | "MEDIUM" | "LOW",
     "reasoning": "Sintesi 1-2 frasi"
   }

2) set_stop_loss — imposta stop-loss su una posizione esistente
   USA stop_loss_pct (es. -5 per -5% sotto entry) OPPURE stop_loss_price (assoluto).
   {
     "type": "set_stop_loss",
     "ticker": "NVDA",
     "stop_loss_pct": -5,
     "reasoning": "Per ridurre il rischio sotto $850"
   }

3) set_take_profit — imposta take-profit su una posizione esistente
   USA take_profit_pct (es. 15 per +15% sopra entry) OPPURE take_profit_price (assoluto).
   {
     "type": "set_take_profit",
     "ticker": "NVDA",
     "take_profit_pct": 15,
     "reasoning": "Target sul livello chiave $1000"
   }

4) add_directive — aggiungi una direttiva strategica permanente
   Verra' iniettata IN CIMA a TUTTI i prompt successivi (Decision Live + Sim).
   {
     "type": "add_directive",
     "text": "Evita posizioni nel settore tech per la prossima settimana",
     "reasoning": "Su richiesta utente"
   }
"""


SYSTEM_PROMPT_STANDARD = """Sei l'AI di trading equity di GeoInvest. L'utente dialoga con TE \
direttamente — sei lo stesso agente che opera autonomamente sul portafoglio durante \
le ore di mercato US. Il tuo ruolo qui in chat:

1. SPIEGARE le tue decisioni passate (quando, perche', risultato)
2. ANALIZZARE le posizioni aperte (P&L, rischio, prospettive)
3. RICEVERE direttive strategiche dall'utente (es. "evita il tech questa settimana")
4. PROPORRE azioni concrete: trade, stop-loss, take-profit, direttive

═══════════════════════════════════════════════════════════════════════
FORMATO OUTPUT — OBBLIGATORIO
═══════════════════════════════════════════════════════════════════════

Rispondi SEMPRE con un JSON valido di questa forma:

{
  "text": "messaggio per l'utente in italiano (markdown ok), 2-6 paragrafi",
  "proposed_actions": []
}

Se l'utente chiede esplicitamente di fare qualcosa (es. "imposta stop-loss
a -5% su NVDA", "metti una direttiva no-tech", "compra 5 NVDA") oppure se
TU vedi un'opportunita' chiara, popola proposed_actions[] con una o piu'
azioni nei formati descritti sotto. Se non ti sembra il caso di agire,
lascia proposed_actions = [].

""" + ACTIONS_BLOCK + """
═══════════════════════════════════════════════════════════════════════
REGOLE
═══════════════════════════════════════════════════════════════════════

- Solo equity/ETF: per crypto rimanda all'agente Crypto.
- Cita SEMPRE ticker e dati specifici (non generalizzare).
- Quando proponi un trade, verifica liquidita' del portafoglio e
  concentrazione del ticker.
- Stop-loss / take-profit: proponili SOLO su ticker che l'utente possiede
  gia' (vedi posizioni nel contesto). Se la posizione non esiste, spiegalo
  nel "text" e proponi prima un trade di apertura.
- Direttive: usa add_directive SOLO se l'utente esprime una preferenza
  strategica esplicita (es. "non investire in tech", "non operare il
  venerdi"). NON inventare direttive di tua iniziativa.
- Le DIRETTIVE UTENTE RECENTI nel contesto sono preferenze: rispettale.
- Non inventare prezzi: se proponi un trade, il sistema usa il prezzo corrente reale.
- Tono: analitico-diretto, niente preamboli tipo "ottima domanda". Vai al punto."""


SYSTEM_PROMPT_CRYPTO = """Sei l'AI di trading crypto di GeoInvest. L'utente dialoga con TE \
direttamente — sei lo stesso agente che opera autonomamente sui mercati crypto 24/7. \
Il tuo ruolo qui in chat:

1. SPIEGARE le tue decisioni crypto passate
2. ANALIZZARE posizioni crypto aperte (P&L, momentum, on-chain bias)
3. RICEVERE direttive strategiche (es. "non aprire posizioni durante eventi macro")
4. PROPORRE azioni concrete: trade crypto, stop-loss, take-profit, direttive

═══════════════════════════════════════════════════════════════════════
FORMATO OUTPUT — OBBLIGATORIO
═══════════════════════════════════════════════════════════════════════

Rispondi SEMPRE con un JSON valido di questa forma (eventuale reasoning
<think>...</think> prima del JSON viene scartato dal parser):

{
  "text": "messaggio per l'utente in italiano (markdown ok)",
  "proposed_actions": []
}

Se l'utente chiede di fare qualcosa o se vedi un setup chiaro, popola
proposed_actions[] con una o piu' azioni nei formati descritti sotto.

""" + ACTIONS_BLOCK + """
═══════════════════════════════════════════════════════════════════════
REGOLE
═══════════════════════════════════════════════════════════════════════

- Solo crypto: ticker formato X-USD (BTC-USD, ETH-USD, SOL-USD, ...).
- Cita SEMPRE livelli (Fibonacci, supporti, RSI, funding rate) quando li hai.
- Crypto sono volatili: confidence MEDIUM e' gia' alta, HIGH solo con confluenze forti.
- Frazioni OK e tipiche (BTC 0.025, ETH 0.5).
- Stop-loss / take-profit: proponili SOLO su ticker che l'utente possiede gia'.
- Direttive: usa add_directive SOLO su esplicita richiesta utente.
- DIRETTIVE UTENTE RECENTI nel contesto: preferenze da rispettare.
- Non inventare numeri: usa i dati live del contesto.
- Tono diretto, niente preamboli. Vai al punto."""


# ─── Helpers ────────────────────────────────────────────────────────────────

def _get_system_prompt(agent_type: str) -> str:
    return SYSTEM_PROMPT_CRYPTO if agent_type == "crypto" else SYSTEM_PROMPT_STANDARD


def _get_anthropic_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    try:
        import database
        return (database.get_setting("anthropic_api_key", "") or "").strip()
    except Exception:
        return ""


def _get_deepseek_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    try:
        import database
        return (database.get_setting("deepseek_api_key", "") or "").strip()
    except Exception:
        return ""


# ─── Live OHLCV per i ticker rilevanti ──────────────────────────────────────

async def _fetch_live_ohlcv(tickers: list[str]) -> dict:
    """
    Recupera OHLCV recenti (ultimi 5 giorni daily + ultimo prezzo) per i ticker
    indicati. Usa data_fetchers.fetch_market_data che gia' ha cache 5 min.
    Ritorna dict {ticker: {current_price, change_5d_pct, recent_candles}}.
    """
    if not tickers:
        return {}
    try:
        import data_fetchers
    except ImportError:
        return {}

    out = {}
    loop = asyncio.get_running_loop()

    async def _fetch_one(t: str):
        try:
            md = await loop.run_in_executor(None, data_fetchers.fetch_market_data, t, 7)
            if not md or not md.get("data"):
                return t, None
            data = md["data"]
            current = data[-1].get("close")
            five_back = data[-min(5, len(data))].get("close") if data else None
            change_pct = None
            if current and five_back and five_back > 0:
                change_pct = round(((current - five_back) / five_back) * 100, 2)
            # Ultime 5 candele compresse
            recent = []
            for c in data[-5:]:
                recent.append({
                    "d": str(c.get("date", ""))[:10],
                    "o": c.get("open"), "h": c.get("high"),
                    "l": c.get("low"), "c": c.get("close"),
                    "v": c.get("volume"),
                })
            return t, {
                "current_price": current,
                "change_5d_pct": change_pct,
                "recent_candles": recent,
            }
        except Exception as e:
            logger.debug("[CHAT] OHLCV %s failed: %s", t, e)
            return t, None

    results = await asyncio.gather(*[_fetch_one(t) for t in tickers], return_exceptions=True)
    for r in results:
        if isinstance(r, tuple) and r[1] is not None:
            out[r[0]] = r[1]
    return out


def _select_relevant_tickers(positions: list, agent_type: str) -> list[str]:
    """
    Sceglie i ticker piu' rilevanti per cui caricare OHLCV live:
    - tutte le posizioni aperte (filtrate per agent_type)
    - + watchlist top liquidita' (BTC/ETH/NVDA/SPY a seconda del tipo)
    Cap a MAX_OHLCV_TICKERS.
    """
    out: list[str] = []
    crypto_suffixes = ("-USD",)

    for p in positions or []:
        t = (p.get("ticker") or "").upper()
        if not t:
            continue
        is_crypto = t.endswith(crypto_suffixes) or t.startswith("X:")
        if agent_type == "crypto" and is_crypto:
            out.append(t)
        elif agent_type == "standard" and not is_crypto:
            out.append(t)

    # Watchlist di default per copertura
    if agent_type == "crypto":
        for t in ["BTC-USD", "ETH-USD", "SOL-USD"]:
            if t not in out:
                out.append(t)
    else:
        for t in ["SPY", "QQQ", "NVDA"]:
            if t not in out:
                out.append(t)

    return out[:MAX_OHLCV_TICKERS]


# ─── Context builder ────────────────────────────────────────────────────────

def _build_context_block(agent_type: str, live_ohlcv: dict) -> str:
    """
    Costruisce il blocco di contesto live da iniettare nel system prompt.
    Riusa chat_assistant.build_live_context() e aggiunge OHLCV live.
    """
    parts = []
    try:
        from agents import chat_assistant
        live_ctx = chat_assistant.build_live_context()
        if live_ctx:
            parts.append(live_ctx)
    except Exception as e:
        logger.warning("[CHAT-DEC] live_context failed: %s", e)

    if live_ohlcv:
        parts.append(
            "OHLCV LIVE (ultimi 5 giorni, top ticker rilevanti):\n```json\n"
            + json.dumps(live_ohlcv, indent=2, ensure_ascii=False, default=str)
            + "\n```"
        )

    parts.append(
        f"AGENT_TYPE attivo: {agent_type.upper()}\n"
        f"timestamp UTC: {datetime.now(timezone.utc).isoformat()}"
    )

    return "\n\n".join(parts)


# ─── Engine calls ───────────────────────────────────────────────────────────

async def _call_claude(system_prompt: str, history: list, user_message: str,
                       context_block: str) -> str:
    """Claude Sonnet 4.5 con system prompt + context iniettato. History compatibile."""
    from anthropic import Anthropic

    api_key = _get_anthropic_key()
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY non configurata")

    client = Anthropic(api_key=api_key)
    full_system = system_prompt + "\n\n═══════════════ CONTESTO LIVE ═══════════════\n\n" + context_block

    messages = []
    for h in history:
        role = h.get("role")
        content = h.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})

    def _sync():
        return client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2500,
            system=full_system,
            messages=messages,
        )

    response = await asyncio.to_thread(_sync)
    text = ""
    for block in response.content:
        if hasattr(block, "text"):
            text += block.text
    return text


async def _call_deepseek_r1(system_prompt: str, history: list, user_message: str,
                            context_block: str) -> str:
    """DeepSeek-R1 (deepseek-reasoner) — per crypto."""
    api_key = _get_deepseek_key()
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY non configurata")

    full_system = system_prompt + "\n\n═══════════════ CONTESTO LIVE ═══════════════\n\n" + context_block

    messages = [{"role": "system", "content": full_system}]
    for h in history:
        role = h.get("role")
        content = h.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": DEEPSEEK_R1_MODEL,
        "messages": messages,
        "max_tokens": 3000,
    }

    async with aiohttp.ClientSession() as sess:
        async with sess.post(
            DEEPSEEK_API_URL, json=payload, headers=headers,
            timeout=aiohttp.ClientTimeout(total=180),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise ValueError(f"DeepSeek-R1 HTTP {resp.status}: {body[:300]}")
            data = await resp.json()

    text = data["choices"][0]["message"].get("content") or ""
    # Strip <think>...</think> blocks (R1 reasoning)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    return text


# ─── Response parser ────────────────────────────────────────────────────────

def _validate_action(a: dict) -> dict | None:
    """
    Valida e normalizza una singola proposed_action. Ritorna None se invalida.
    Tipi supportati: execute_trade, set_stop_loss, set_take_profit, add_directive.
    """
    if not isinstance(a, dict):
        return None
    t = (a.get("type") or "").lower().strip()

    if t == "execute_trade":
        ticker = str(a.get("ticker", "")).upper().strip()
        action = str(a.get("action", "")).upper().strip()
        try:
            qty = float(a.get("quantity", 0))
        except (TypeError, ValueError):
            return None
        if not ticker or action not in ("BUY", "SELL") or qty <= 0:
            return None
        return {
            "type": "execute_trade",
            "ticker": ticker,
            "action": action,
            "quantity": qty,
            "confidence_level": (a.get("confidence_level") or "MEDIUM").upper(),
            "reasoning": str(a.get("reasoning", ""))[:500],
        }

    if t == "set_stop_loss":
        ticker = str(a.get("ticker", "")).upper().strip()
        if not ticker:
            return None
        pct = a.get("stop_loss_pct")
        price = a.get("stop_loss_price")
        out: dict = {"type": "set_stop_loss", "ticker": ticker,
                     "reasoning": str(a.get("reasoning", ""))[:500]}
        if price is not None:
            try:
                out["stop_loss_price"] = float(price)
                if out["stop_loss_price"] <= 0:
                    return None
            except (TypeError, ValueError):
                return None
        elif pct is not None:
            try:
                out["stop_loss_pct"] = float(pct)
            except (TypeError, ValueError):
                return None
        else:
            return None
        return out

    if t == "set_take_profit":
        ticker = str(a.get("ticker", "")).upper().strip()
        if not ticker:
            return None
        pct = a.get("take_profit_pct")
        price = a.get("take_profit_price")
        out = {"type": "set_take_profit", "ticker": ticker,
               "reasoning": str(a.get("reasoning", ""))[:500]}
        if price is not None:
            try:
                out["take_profit_price"] = float(price)
                if out["take_profit_price"] <= 0:
                    return None
            except (TypeError, ValueError):
                return None
        elif pct is not None:
            try:
                out["take_profit_pct"] = float(pct)
            except (TypeError, ValueError):
                return None
        else:
            return None
        return out

    if t == "add_directive":
        text = str(a.get("text", "")).strip()
        if not text or len(text) < 3 or len(text) > 800:
            return None
        return {
            "type": "add_directive",
            "text": text,
            "reasoning": str(a.get("reasoning", ""))[:500],
        }

    return None


def _parse_response(raw_text: str) -> dict:
    """
    Estrae {text, proposed_trade, proposed_actions} dal raw del modello.
    Tollerante a:
      - JSON puro
      - JSON in ```json ... ```
      - JSON con preambolo/coda di testo (cerca primo { e ultimo })

    Backward-compat:
      - se il modello usa il vecchio formato `proposed_trade`, lo
        converte automaticamente in una singola action di
        type=execute_trade in proposed_actions[].
      - mantiene anche `proposed_trade` come campo separato per non
        rompere i messaggi vecchi nel DB.

    Se il parsing fallisce, ritorna text=raw_text, no actions.
    """
    if not raw_text:
        return {"text": "", "proposed_trade": None, "proposed_actions": []}

    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    json_start = text.find("{")
    json_end = text.rfind("}")

    if json_start < 0 or json_end <= json_start:
        return {"text": text, "proposed_trade": None, "proposed_actions": []}

    candidate = text[json_start:json_end + 1]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return {"text": text, "proposed_trade": None, "proposed_actions": []}

    if not isinstance(parsed, dict) or "text" not in parsed:
        return {"text": text, "proposed_trade": None, "proposed_actions": []}

    user_text = str(parsed.get("text", "")).strip()

    # ── proposed_actions[] (nuovo formato) ─────────────────────────────
    raw_actions = parsed.get("proposed_actions") or []
    actions: list = []
    if isinstance(raw_actions, list):
        for a in raw_actions:
            v = _validate_action(a)
            if v:
                actions.append(v)

    # ── proposed_trade (formato legacy) → converti in execute_trade ────
    legacy_pt = parsed.get("proposed_trade")
    converted_pt: dict | None = None
    if legacy_pt and isinstance(legacy_pt, dict):
        legacy_action = dict(legacy_pt)
        legacy_action.setdefault("type", "execute_trade")
        v = _validate_action(legacy_action)
        if v:
            # Salva anche nel campo legacy per backward-compat (UI vecchia)
            converted_pt = {
                "ticker": v["ticker"], "action": v["action"],
                "quantity": v["quantity"],
                "confidence_level": v.get("confidence_level"),
                "reasoning": v.get("reasoning"),
            }
            # Se non e' gia' in proposed_actions, aggiungilo
            already = any(
                x.get("type") == "execute_trade" and x.get("ticker") == v["ticker"]
                and x.get("action") == v["action"] and x.get("quantity") == v["quantity"]
                for x in actions
            )
            if not already:
                actions.append(v)

    return {
        "text": user_text,
        "proposed_trade": converted_pt,
        "proposed_actions": actions,
    }


# ─── Public API ─────────────────────────────────────────────────────────────

async def chat_with_decision_agent(agent_type: str, user_message: str) -> dict:
    """
    Entry point principale. Carica history dal DB, costruisce contesto,
    chiama il modello, parse risposta, persiste user msg + assistant msg.

    Returns:
        {
          "conversation_id": int,
          "user_message_id": int,
          "assistant_message_id": int,
          "text": str,
          "proposed_trade": dict | None,
          "model": "claude-sonnet|deepseek-r1",
        }
    """
    import database

    agent_type = (agent_type or "standard").lower()
    if agent_type not in ("standard", "crypto"):
        agent_type = "standard"

    # 1. Conversazione attiva (crea se non esiste)
    conv_id = database.get_or_create_decision_chat_conversation(agent_type)
    if not conv_id:
        raise RuntimeError("Impossibile creare/recuperare conversazione")

    # 2. Salva subito il messaggio utente
    user_msg_id = database.insert_decision_chat_message(conv_id, "user", user_message)

    # 3. Carica history per contesto modello (escluso il msg appena inserito)
    raw_history = database.get_decision_chat_messages(conv_id, limit=MAX_HISTORY_MESSAGES + 5)
    history = []
    for m in raw_history:
        if m.get("id") == user_msg_id:
            continue
        history.append({"role": m.get("role"), "content": m.get("content", "")})
    history = history[-MAX_HISTORY_MESSAGES:]

    # 4. Costruisci contesto live (portfolio + positions + market + OHLCV)
    try:
        positions = database.get_positions() or []
    except Exception:
        positions = []
    relevant_tickers = _select_relevant_tickers(positions, agent_type)
    live_ohlcv = await _fetch_live_ohlcv(relevant_tickers)
    context_block = _build_context_block(agent_type, live_ohlcv)

    # 5. Chiama il modello
    system_prompt = _get_system_prompt(agent_type)
    try:
        if agent_type == "crypto":
            raw_response = await _call_deepseek_r1(system_prompt, history, user_message, context_block)
            model_used = "deepseek-r1"
        else:
            raw_response = await _call_claude(system_prompt, history, user_message, context_block)
            model_used = "claude-sonnet-4.5"
    except Exception as e:
        logger.error("[CHAT-DEC] %s engine failed: %s", agent_type, e, exc_info=True)
        # Salva un messaggio di errore come assistant per mantenere consistenza
        err_text = f"Errore tecnico: {str(e)[:200]}"
        asst_id = database.insert_decision_chat_message(conv_id, "assistant", err_text)
        return {
            "conversation_id": conv_id,
            "user_message_id": user_msg_id,
            "assistant_message_id": asst_id,
            "text": err_text,
            "proposed_trade": None,
            "model": "error",
            "error": str(e)[:200],
        }

    # 6. Parse risposta
    parsed = _parse_response(raw_response)
    text = parsed["text"] or "(nessuna risposta dal modello)"
    proposed = parsed["proposed_trade"]
    proposed_actions = parsed.get("proposed_actions") or []

    # 7. Salva messaggio assistant (con entrambi i campi: legacy + nuovo)
    asst_id = database.insert_decision_chat_message(
        conv_id, "assistant", text,
        proposed_trade=proposed,
        proposed_actions=proposed_actions,
    )

    return {
        "conversation_id": conv_id,
        "user_message_id": user_msg_id,
        "assistant_message_id": asst_id,
        "text": text,
        "proposed_trade": proposed,
        "proposed_actions": proposed_actions,
        "model": model_used,
    }
