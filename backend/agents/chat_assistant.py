"""
Chat Assistant — analista AI che dialoga con l'utente sulle decisioni
del Decision Agent (standard + crypto).

Usa DeepSeek-R1 (deepseek-reasoner) come engine: ha reasoning esplicito,
costo basso (~10x inferiore a Sonnet) e produce analisi profonde su
serie di decisioni di trading. Il prompt e' strettamente read-only:
l'AI NON puo' eseguire trade, modificare il portafoglio o influenzare
le decisioni future. E' solo un analista di pattern.

Mini-memoria: il backend conserva solo le ultime 10 conversazioni
(trim automatico dopo ogni nuova conversazione). All'interno di
una singola conversazione, l'history completo viene passato a R1.
"""
import json
import logging
import os
import re

import aiohttp

logger = logging.getLogger(__name__)

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_R1_MODEL = "deepseek-reasoner"

# Limite di output per evitare risposte fluviali e costi esagerati.
# R1 generera' un blocco <think> separato che NON viene contato qui.
MAX_RESPONSE_TOKENS = 2500

# Massimo numero di trade da iniettare nel contesto (scaglione di sicurezza
# in caso l'utente selezioni troppi trade — R1 ha un context da 64k token).
MAX_TRADES_IN_CONTEXT = 20


SYSTEM_PROMPT = """Sei l'analista AI del sistema di trading "GeoInvest". Il tuo ruolo e' aiutare \
l'utente a comprendere, valutare e identificare pattern nelle decisioni \
prese dal Decision Agent (sia standard che crypto).

**Comportamento**
- Rispondi in italiano, con tono analitico-neutro, professionale ma chiaro.
- Sii sintetico: 2-5 paragrafi tipici, lista puntata quando aiuta.
- Cita SEMPRE i ticker e le date specifiche dei trade quando rispondi.
- Se l'utente non specifica quali decisioni analizzare, usa quelle nel contesto.
- Se l'utente chiede di valutare qualita' di un trade, considera: confidence, reasoning, \
  outcome (P&L se disponibile), coerenza con il geopolitical_reasoning e technical_reasoning.
- Identifica bias sistematici se presenti (es. troppo conservativo, overconfidence, \
  pattern di entry/exit ricorrenti).

**Limiti rigorosi**
- NON puoi eseguire trade ne' modificare il portafoglio.
- NON dare advice di acquisto/vendita futura ("dovresti comprare X domani"). \
  Puoi pero' descrivere i criteri usati storicamente dal Decision Agent.
- NON inventare numeri o ticker non presenti nel contesto.
- Se ti mancano dati per rispondere, dillo esplicitamente."""


def _get_api_key() -> str:
    """Recupera la chiave DeepSeek da env o settings DB."""
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    try:
        import database
        return (database.get_setting("deepseek_api_key", "") or "").strip()
    except Exception:
        return ""


def _summarize_trade(t: dict) -> dict:
    """
    Riduce una riga della tabella trades a un dict compatto per il contesto AI.
    Tiene solo i campi rilevanti per l'analisi, troncando i reasoning lunghi.
    """
    def _trunc(s, n=600):
        if not s:
            return ""
        s = str(s)
        return s[:n] + "..." if len(s) > n else s

    return {
        "id": t.get("id"),
        "timestamp": t.get("timestamp"),
        "ticker": t.get("ticker"),
        "action": t.get("action"),
        "quantity": t.get("quantity"),
        "price": t.get("price"),
        "total_value": t.get("total_value"),
        "confidence": t.get("confidence_score"),
        "geopolitical_reasoning": _trunc(t.get("geopolitical_reasoning")),
        "technical_reasoning": _trunc(t.get("technical_reasoning")),
        "final_decision": _trunc(t.get("final_decision"), 1200),
        "cs_mirror_status": t.get("cs_mirror_status"),
    }


def build_decisions_context(trades: list) -> str:
    """
    Trasforma una lista di trade (gia' filtrati dall'utente o gli ultimi N)
    in un blocco di contesto JSON compatto da iniettare nel prompt user.
    """
    if not trades:
        return "Nessuna decisione selezionata."
    trades = trades[:MAX_TRADES_IN_CONTEXT]
    summarized = [_summarize_trade(t) for t in trades]
    return (
        f"Decisioni selezionate dall'utente ({len(summarized)} trade):\n"
        f"```json\n{json.dumps(summarized, indent=2, ensure_ascii=False, default=str)}\n```"
    )


def _strip_think(text: str) -> str:
    """Rimuove blocchi <think>...</think> dal reasoning di R1."""
    if not text:
        return ""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


async def chat_completion(
    system_prompt: str,
    history: list,
    user_message: str,
    decisions_context: str = "",
) -> tuple[str, str]:
    """
    Chiama DeepSeek-R1 con la conversazione corrente e ritorna
    (testo_risposta, reasoning_grezzo).

    history e' una lista di {role, content} con i messaggi precedenti
    (escluso l'ultimo user_message, che viene aggiunto qui).
    decisions_context, se non vuoto, viene prepended al user_message.
    """
    api_key = _get_api_key()
    if not api_key:
        return ("DEEPSEEK_API_KEY non configurata. Vai su Impostazioni → DeepSeek "
                "API Key e inseriscila per usare l'analista AI.", "")

    # Costruisci messaggi: system + history + user_message (con contesto)
    messages = [{"role": "system", "content": system_prompt}]
    for m in history:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        messages.append({"role": role, "content": m.get("content", "")})

    final_user = user_message
    if decisions_context:
        final_user = (
            f"[Contesto decisioni del Decision Agent]\n{decisions_context}\n\n"
            f"[Domanda dell'utente]\n{user_message}"
        )
    messages.append({"role": "user", "content": final_user})

    payload = {
        "model": DEEPSEEK_R1_MODEL,
        "messages": messages,
        "max_tokens": MAX_RESPONSE_TOKENS,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                DEEPSEEK_API_URL, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=180),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error("DeepSeek HTTP %d: %s", resp.status, body[:300])
                    return (f"Errore DeepSeek (HTTP {resp.status}). Riprova tra qualche istante.",
                            "")
                data = await resp.json()

        choice = data.get("choices", [{}])[0]
        msg = choice.get("message", {}) or {}
        # R1 restituisce sia content (risposta finale) che reasoning_content
        # (chain-of-thought). Esponiamo solo content all'utente.
        raw = msg.get("content") or ""
        reasoning = msg.get("reasoning_content") or ""
        text = _strip_think(raw)
        if not text:
            text = "(nessuna risposta generata)"
        return (text, reasoning)
    except aiohttp.ClientError as e:
        logger.error("DeepSeek client error: %s", e)
        return (f"Errore di rete contattando DeepSeek: {e}", "")
    except Exception as e:
        logger.error("DeepSeek unexpected error: %s", e, exc_info=True)
        return (f"Errore inatteso: {e}", "")


def auto_title_from_first_message(first_msg: str, max_len: int = 60) -> str:
    """
    Estrae un titolo conciso dal primo messaggio dell'utente.
    Strategia semplice: prime ~6 parole, capitalizzate.
    """
    s = (first_msg or "").strip().replace("\n", " ")
    if not s:
        return "Nuova conversazione"
    words = s.split()
    title = " ".join(words[:8])
    if len(title) > max_len:
        title = title[:max_len].rstrip() + "..."
    return title or "Nuova conversazione"
