"""
Chat Assistant — analista AI che dialoga con l'utente sulle decisioni
del Decision Agent (standard + crypto).

Usa DeepSeek-R1 (deepseek-reasoner) come engine: ha reasoning esplicito,
costo basso (~10x inferiore a Sonnet) e produce analisi profonde su
serie di decisioni di trading. Il prompt e' strettamente read-only:
l'AI NON puo' eseguire trade, modificare il portafoglio o influenzare
le decisioni future. E' solo un analista di pattern.

Contesto live: ogni richiesta include automaticamente lo stato corrente
del portafoglio, posizioni aperte, stato del mercato e ultime briefing
geopolitiche. L'AI ha quindi accesso ai "fatti di base" senza che
l'utente debba fornirli.

Mini-memoria: il backend conserva solo le ultime 10 conversazioni
(trim automatico dopo ogni nuova conversazione). All'interno di
una singola conversazione, l'history completo viene passato a R1.
"""
import json
import logging
import os
import re
from datetime import datetime, timezone

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

**Cosa hai a disposizione (sempre)**
- Stato corrente del portafoglio: cash, total value, P&L totale e percentuale
- Posizioni aperte con prezzo medio di carico, prezzo attuale, P&L unrealized
- Stato del mercato (open/closed) e prossima apertura se chiuso
- Ultime decisioni del Decision Agent (standard + crypto)
- Pre-market briefing e weekend intelligence quando disponibili
- Ulteriori trade selezionati dall'utente come contesto specifico

**Comportamento**
- Rispondi in italiano, con tono analitico-neutro, professionale ma chiaro.
- Sii sintetico: 2-5 paragrafi tipici, lista puntata quando aiuta.
- Cita SEMPRE i ticker e le date specifiche dei trade quando rispondi.
- Se l'utente chiede della "situazione attuale", usa il blocco PORTFOLIO/MARKET.
- Se l'utente chiede di valutare un trade specifico, considera: confidence, reasoning, \
  outcome (P&L attuale se la posizione è ancora aperta), coerenza con il \
  geopolitical_reasoning e technical_reasoning.
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
        return ""
    trades = trades[:MAX_TRADES_IN_CONTEXT]
    summarized = [_summarize_trade(t) for t in trades]
    return (
        f"Decisioni selezionate dall'utente ({len(summarized)} trade):\n"
        f"```json\n{json.dumps(summarized, indent=2, ensure_ascii=False, default=str)}\n```"
    )


def build_live_context() -> str:
    """
    Costruisce un blocco "fatti di base" sempre iniettato nel prompt:
    portafoglio, posizioni con P&L attuale, stato mercato, ultime decisioni
    e ultime briefing. Tutte le query sono best-effort: se una fallisce
    (es. tabella mancante, errore di rete), il blocco viene comunque
    generato senza quella sezione.
    """
    sections: list[str] = []

    # 1. Portfolio
    try:
        import database
        portfolio = database.get_portfolio() or {}
        positions = database.get_positions() or []
        cash = float(portfolio.get("cash_balance") or 0)
        total = float(portfolio.get("total_value") or 0)

        # Initial balance da settings; fallback 100k
        try:
            init_str = database.get_setting("initial_balance", "100000")
            initial = float(init_str) if init_str else 100000.0
        except Exception:
            initial = 100000.0

        pnl = total - initial
        pnl_pct = (pnl / initial * 100) if initial > 0 else 0
        invested = sum((p.get("avg_buy_price") or 0) * (p.get("quantity") or 0) for p in positions)
        current_value = sum((p.get("current_price") or 0) * (p.get("quantity") or 0) for p in positions)

        port_obj = {
            "total_value_usd": round(total, 2),
            "cash_balance_usd": round(cash, 2),
            "invested_usd": round(invested, 2),
            "positions_current_value_usd": round(current_value, 2),
            "initial_balance_usd": round(initial, 2),
            "pnl_total_usd": round(pnl, 2),
            "pnl_total_pct": round(pnl_pct, 2),
            "n_open_positions": len(positions),
        }
        sections.append(
            "PORTFOLIO (live):\n```json\n"
            + json.dumps(port_obj, indent=2, ensure_ascii=False)
            + "\n```"
        )
    except Exception as e:
        logger.warning("build_live_context: portfolio fetch failed: %s", e)

    # 2. Posizioni aperte con P&L unrealized
    try:
        import database
        positions = database.get_positions() or []
        if positions:
            pos_list = []
            for p in positions[:30]:  # cap a 30 per safety
                qty = p.get("quantity") or 0
                avg = p.get("avg_buy_price") or 0
                cur = p.get("current_price") or 0
                upnl = (cur - avg) * qty if cur > 0 else 0
                upnl_pct = ((cur - avg) / avg * 100) if avg > 0 else 0
                pos_list.append({
                    "ticker": p.get("ticker"),
                    "quantity": qty,
                    "avg_buy_price": round(avg, 4),
                    "current_price": round(cur, 4),
                    "unrealized_pnl_usd": round(upnl, 2),
                    "unrealized_pnl_pct": round(upnl_pct, 2),
                    "opened_at": str(p.get("opened_at", "")),
                })
            sections.append(
                f"POSITIONS aperte ({len(pos_list)}):\n```json\n"
                + json.dumps(pos_list, indent=2, ensure_ascii=False, default=str)
                + "\n```"
            )
    except Exception as e:
        logger.warning("build_live_context: positions fetch failed: %s", e)

    # 3. Market state
    try:
        from scheduler import is_market_open, get_next_market_open
        market_open = bool(is_market_open())
        market_obj = {
            "us_market_open_now": market_open,
            "current_utc_time": datetime.now(timezone.utc).isoformat(),
        }
        if not market_open:
            try:
                nxt = get_next_market_open()
                market_obj["next_market_open_utc"] = nxt.isoformat() if nxt else None
            except Exception:
                pass
        sections.append(
            "MARKET STATE:\n```json\n"
            + json.dumps(market_obj, indent=2, ensure_ascii=False)
            + "\n```"
        )
    except Exception as e:
        logger.warning("build_live_context: market state failed: %s", e)

    # 4. Ultime 8 decisioni (sintesi)
    try:
        import database
        recent = database.get_trades(limit=8) or []
        if recent:
            mini = [{
                "id": t.get("id"),
                "ts": str(t.get("timestamp", "")),
                "ticker": t.get("ticker"),
                "action": t.get("action"),
                "qty": t.get("quantity"),
                "price": t.get("price"),
                "confidence": t.get("confidence_score"),
            } for t in recent]
            sections.append(
                "RECENT DECISIONS (ultime 8):\n```json\n"
                + json.dumps(mini, indent=2, ensure_ascii=False, default=str)
                + "\n```"
            )
    except Exception as e:
        logger.warning("build_live_context: recent trades fetch failed: %s", e)

    # 5. Pre-market briefing (se presente, troncato)
    try:
        import database
        briefing = database.get_latest_pre_market_briefing()
        if briefing and briefing.get("content"):
            content = str(briefing["content"])[:1500]
            sections.append(
                f"PRE-MARKET BRIEFING (latest, troncato a 1500 char):\n{content}"
            )
    except Exception:
        pass

    # 6. Weekend intelligence (se presente, troncato)
    try:
        import database
        wknd = database.get_latest_weekend_intelligence()
        if wknd and wknd.get("content"):
            content = str(wknd["content"])[:1200]
            sections.append(
                f"WEEKEND INTELLIGENCE (latest, troncato a 1200 char):\n{content}"
            )
    except Exception:
        pass

    if not sections:
        return ""
    header = "=" * 60 + "\nLIVE CONTEXT (snapshot al momento della richiesta):\n" + "=" * 60
    return header + "\n\n" + "\n\n".join(sections)


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
    live_context: str = "",
) -> tuple[str, str]:
    """
    Chiama DeepSeek-R1 con la conversazione corrente e ritorna
    (testo_risposta, reasoning_grezzo).

    history e' una lista di {role, content} con i messaggi precedenti
    (escluso l'ultimo user_message, che viene aggiunto qui).
    live_context contiene SEMPRE portfolio+market state.
    decisions_context contiene i trade selezionati dall'utente (se presenti).
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

    # Componi user message con i contesti
    parts: list[str] = []
    if live_context:
        parts.append(live_context)
    if decisions_context:
        parts.append(f"[DECISIONI SELEZIONATE DALL'UTENTE]\n{decisions_context}")
    parts.append(f"[DOMANDA DELL'UTENTE]\n{user_message}")
    final_user = "\n\n".join(parts)
    messages.append({"role": "user", "content": final_user})

    # DeepSeek-R1 (deepseek-reasoner) NON supporta:
    # temperature, top_p, presence_penalty, frequency_penalty, tools.
    # Solo max_tokens. Ne' system message tools-enabled.
    payload = {
        "model": DEEPSEEK_R1_MODEL,
        "messages": messages,
        "max_tokens": MAX_RESPONSE_TOKENS,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    logger.info(
        "DeepSeek chat call: msgs=%d, total_chars=%d, has_live=%s, has_decisions=%s",
        len(messages),
        sum(len(m.get("content", "")) for m in messages),
        bool(live_context), bool(decisions_context),
    )

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                DEEPSEEK_API_URL, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=180),
            ) as resp:
                body_text = await resp.text()
                if resp.status != 200:
                    logger.error(
                        "DeepSeek HTTP %d: %s",
                        resp.status, body_text[:500],
                    )
                    # Estrai messaggio di errore da JSON se possibile
                    err_msg = f"HTTP {resp.status}"
                    try:
                        ej = json.loads(body_text)
                        if "error" in ej:
                            err = ej["error"]
                            if isinstance(err, dict):
                                err_msg = err.get("message", err_msg)
                            else:
                                err_msg = str(err)
                    except Exception:
                        pass
                    return (
                        f"Errore DeepSeek: {err_msg}. Riprova tra qualche istante.",
                        "",
                    )
                try:
                    data = json.loads(body_text)
                except Exception as e:
                    logger.error("DeepSeek response non-JSON: %s", body_text[:300])
                    return (f"Risposta DeepSeek non valida: {e}", "")

        choice = (data.get("choices") or [{}])[0]
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


def ensure_chat_tables() -> tuple[bool, str]:
    """
    Verifica che le tabelle chat_conversations / chat_messages esistano.
    Se mancano (Supabase con DATABASE_URL non configurato all'avvio),
    tenta di crearle al volo via psycopg2.

    Ritorna (True, "") se tutto ok, (False, error_message) altrimenti.
    """
    # SQLite: le tabelle vengono create da init_db, quindi se ci siamo
    # va sempre bene. Test rapido: prova una select.
    try:
        import database
        # Prova a leggere — se la tabella non esiste questo fallisce
        _ = database.get_chat_conversations(limit=1)
        return (True, "")
    except Exception as e:
        # Probabile: tabella non esiste su Supabase
        logger.warning("ensure_chat_tables: probe failed (%s), tentativo creazione...", e)

    # Tentativo creazione via psycopg2 (solo Supabase)
    db_url = os.environ.get("DATABASE_URL", "").strip()
    if not db_url:
        # Prova a derivarla da SUPABASE_URL + SUPABASE_DB_PASSWORD
        sup_url = os.environ.get("SUPABASE_URL", "")
        db_pass = os.environ.get("SUPABASE_DB_PASSWORD", "").strip()
        if sup_url and db_pass:
            try:
                ref = sup_url.split("//")[1].split(".")[0]
                db_url = (
                    f"postgresql://postgres.{ref}:{db_pass}"
                    f"@aws-0-eu-central-1.pooler.supabase.com:6543/postgres"
                )
            except Exception:
                pass

    if not db_url:
        return (False,
                "Tabelle chat_* mancanti. Configurare DATABASE_URL su Render "
                "oppure eseguire migration manualmente su Supabase.")

    try:
        import psycopg2
    except ImportError:
        return (False, "psycopg2 non installato — impossibile creare tabelle al volo.")

    sql = """
        CREATE TABLE IF NOT EXISTS chat_conversations (
            id BIGSERIAL PRIMARY KEY,
            title TEXT NOT NULL DEFAULT 'Nuova conversazione',
            selected_decisions TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS chat_messages (
            id BIGSERIAL PRIMARY KEY,
            conversation_id BIGINT NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
            content TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_chat_messages_conv
            ON chat_messages (conversation_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_chat_conversations_updated
            ON chat_conversations (updated_at DESC);
    """
    try:
        with psycopg2.connect(db_url, connect_timeout=15) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
        logger.info("Tabelle chat_* create con successo via psycopg2.")
        return (True, "")
    except Exception as e:
        logger.error("Creazione tabelle chat_* fallita: %s", e)
        return (False, f"Creazione tabelle fallita: {e}")


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
