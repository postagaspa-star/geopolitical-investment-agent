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
# Alzato 2500 → 4000: le risposte dell'analista venivano troncate a meta'.
MAX_RESPONSE_TOKENS = 4000

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
    }


def build_decisions_context(trades: list) -> str:
    """
    Trasforma una lista di trade (gia' filtrati dall'utente o gli ultimi N)
    in un blocco di contesto JSON compatto da iniettare nel prompt user.
    """
    if not trades:
        return ""
    import trade_analytics
    # Escludi le chiusure non-AI (confidence 100): non sono decisioni
    # dell'AI e inquinerebbero l'analisi delle decisioni selezionate.
    trades = [t for t in trades
              if not trade_analytics.is_manual_close(
                  t.get("confidence_score", t.get("confidence")))]
    if not trades:
        return ""
    trades = trades[:MAX_TRADES_IN_CONTEXT]
    summarized = [_summarize_trade(t) for t in trades]
    return (
        f"Decisioni selezionate dall'utente ({len(summarized)} trade):\n"
        f"```json\n{json.dumps(summarized, indent=2, ensure_ascii=False, default=str)}\n```"
    )


# Cap totale del live context per evitare di saturare i 64k token di R1.
# Lasciamo ~30k char per evitare di sforare considerando system prompt +
# history + decisions_context + risposta. R1 = ~256k char totali, ma
# vogliamo lasciare margine per output_tokens (max 2500).
LIVE_CONTEXT_HARD_CAP_CHARS = 35000
TRADES_FULL_LIMIT = 200    # tutti i trade compressi
LOGS_RECENT_LIMIT = 60     # ultimi log degli agenti
HISTORY_SAMPLE_POINTS = 30 # equity curve sampling


def _compact_trade(t: dict) -> dict:
    """Versione ULTRA compressa di un trade per il bulk listing."""
    return {
        "id": t.get("id"),
        "ts": str(t.get("timestamp", ""))[:19],  # solo "YYYY-MM-DD HH:MM:SS"
        "tk": t.get("ticker"),
        "a": t.get("action"),
        "q": t.get("quantity"),
        "p": t.get("price"),
        "c": t.get("confidence_score"),
    }


def _compact_log(l: dict) -> dict:
    """Versione compressa di un agent_log."""
    content_str = str(l.get("content", ""))
    # Tronca contenuti molto lunghi mantenendo i primi 200 char
    if len(content_str) > 200:
        content_str = content_str[:200] + "..."
    return {
        "ts": str(l.get("timestamp", ""))[:19],
        "phase": l.get("phase"),
        "content": content_str,
    }


def _sample_evenly(items: list, n: int) -> list:
    """Campiona n elementi distribuiti uniformemente su una lista."""
    if not items or n >= len(items):
        return items
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


def build_live_context() -> str:
    """
    Costruisce un blocco "fatti di base" sempre iniettato nel prompt:
    TUTTI i dati significativi della piattaforma, compressi per stare
    sotto LIVE_CONTEXT_HARD_CAP_CHARS (~35k char).

    Sezioni:
      1. PORTFOLIO + POSITIONS (con P&L)
      2. MARKET STATE (us_open, ora UTC)
      3. ALL TRADES (fino a 200, compressi)
      4. EQUITY CURVE (campionata a 30 punti su 90 giorni)
      5. AGENT LOGS (ultimi 60, compressi)
      6. PRE-MARKET BRIEFING (latest)
      7. WEEKEND INTELLIGENCE (latest)
      8. SETTINGS rilevanti (no API keys, solo flag operativi)
      9. DOCUMENTS (metadata: filename, size, category)
      10. SIM RUNS (count per categoria)

    Tutte le query sono best-effort: se una fallisce, la sezione viene
    omessa ma il blocco continua. Logging esteso per diagnostica.
    """
    sections: list[str] = []
    counters: dict[str, str] = {}

    # 1. Portfolio + Positions (sempre prioritario)
    try:
        import database
        portfolio = database.get_portfolio() or {}
        positions = database.get_positions() or []
        cash = float(portfolio.get("cash_balance") or 0)
        total = float(portfolio.get("total_value") or 0)

        try:
            init_str = database.get_setting("initial_balance", "100000")
            initial = float(init_str) if init_str else 100000.0
        except Exception:
            initial = 100000.0

        pnl = total - initial
        pnl_pct = (pnl / initial * 100) if initial > 0 else 0
        # DIRECTION-AWARE (canonico accounting): valori FIRMATI del NAV.
        # SHORT contribuisce NEGATIVO (passività).
        import accounting as _acc
        invested = _acc.positions_value(positions, price_key="avg_buy_price")
        current_value = _acc.positions_value(positions, price_key="current_price")

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
        counters["portfolio"] = "ok"

        # Positions con P&L (DIRECTION-AWARE)
        if positions:
            pos_list = []
            for p in positions[:50]:
                qty = p.get("quantity") or 0
                avg = p.get("avg_buy_price") or 0
                cur = p.get("current_price") or 0
                d = (p.get("direction") or "LONG").upper()
                # P&L direction-aware (canonico accounting)
                import accounting as _acc
                upnl = _acc.unrealized_pnl(qty, avg, cur, d) if cur > 0 else 0
                upnl_pct = _acc.unrealized_pnl_pct(avg, cur, d)
                pos_list.append({
                    "ticker": p.get("ticker"),
                    "direction": d,
                    "quantity": qty,
                    "avg_buy_price": round(avg, 4),
                    "current_price": round(cur, 4),
                    "unrealized_pnl_usd": round(upnl, 2),
                    "unrealized_pnl_pct": round(upnl_pct, 2),
                    "opened_at": str(p.get("opened_at", ""))[:19],
                })
            sections.append(
                f"POSITIONS aperte ({len(pos_list)}):\n```json\n"
                + json.dumps(pos_list, indent=2, ensure_ascii=False, default=str)
                + "\n```"
            )
            counters["positions"] = str(len(pos_list))
    except Exception as e:
        logger.warning("build_live_context[portfolio/positions]: %s", e)
        counters["portfolio"] = f"error: {e}"

    # 1b. RISK STATE LIVE (governor): recovery mode, drawdown 24h, win-rate
    # ultime 10, concentrazione max, circuit breaker, feature-flag automatici.
    # È un blocco-stringa GIÀ formattato. Prima la chat non lo vedeva → a
    # "siamo in recovery?" / "drawdown 24h?" / "quanto sono concentrato?"
    # inventava o non sapeva. Ora risponde coi numeri reali del governor.
    try:
        import risk_state as _rs
        _rs_block = _rs.build_risk_state_prompt_block()
        if _rs_block:
            sections.append(_rs_block)
            counters["risk_state"] = "ok"
    except Exception as e:
        logger.debug("build_live_context[risk_state]: %s", e)

    # 2. Market State
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
        counters["market"] = "ok"
    except Exception as e:
        logger.warning("build_live_context[market]: %s", e)

    # 3. ALL TRADES (compressi, fino a 200)
    try:
        import database
        import trade_analytics
        all_trades = database.get_trades(limit=TRADES_FULL_LIMIT) or []
        # Le operazioni a confidence 100 sono chiusure NON-AI
        # (circuit breaker / auto-exit / manuali): non devono entrare
        # nel ragionamento dell'AI, falserebbero la lettura del track
        # record. Stessa regola di edge-tracker e diagnosi.
        all_trades = [t for t in all_trades
                      if not trade_analytics.is_manual_close(
                          t.get("confidence_score", t.get("confidence")))]
        if all_trades:
            compact_trades = [_compact_trade(t) for t in all_trades]
            # Statistiche aggregate
            n_buy = sum(1 for t in all_trades if t.get("action") == "BUY")
            n_sell = sum(1 for t in all_trades if t.get("action") == "SELL")
            unique_tickers = sorted({t.get("ticker") for t in all_trades if t.get("ticker")})
            stats = {
                "total_trades": len(all_trades),
                "buys": n_buy,
                "sells": n_sell,
                "unique_tickers": len(unique_tickers),
                "tickers_traded": unique_tickers,
                "legend": "tk=ticker, a=action, q=qty, p=price, c=confidence%",
            }
            sections.append(
                f"ALL TRADES STATS:\n```json\n"
                + json.dumps(stats, indent=2, ensure_ascii=False)
                + f"\n```\n\nALL TRADES (fino a {TRADES_FULL_LIMIT}, compressi, ordine desc):\n```json\n"
                + json.dumps(compact_trades, ensure_ascii=False, default=str)
                + "\n```"
            )
            # #4: TESI dei trade più RECENTI (geopolitical/technical reasoning)
            # per rispondere a "perché hai comprato X?" col reasoning ORIGINALE,
            # non una ricostruzione inventata. Solo i primi ~12 (ordine desc =
            # più recenti) per non sfondare il cap ~35k char. Prima _compact_trade
            # buttava via il reasoning.
            thesis = []
            for t in all_trades[:12]:
                geo = str(t.get("geopolitical_reasoning") or "")[:280]
                tech = str(t.get("technical_reasoning") or "")[:280]
                if geo or tech:
                    thesis.append({
                        "ts": str(t.get("timestamp", ""))[:19],
                        "tk": t.get("ticker"), "a": t.get("action"),
                        "geo": geo, "tech": tech,
                    })
            if thesis:
                sections.append(
                    "TESI DEI TRADE RECENTI (per spiegare PERCHÉ una posizione è "
                    "stata aperta — usa QUESTO reasoning, non inventarne uno):\n```json\n"
                    + json.dumps(thesis, ensure_ascii=False, default=str)
                    + "\n```"
                )
            counters["trades"] = str(len(compact_trades))
    except Exception as e:
        logger.warning("build_live_context[trades]: %s", e)

    # 4. Equity curve (campionata)
    try:
        import database
        history = database.get_portfolio_history(days=90) or []
        if history:
            sampled = _sample_evenly(history, HISTORY_SAMPLE_POINTS)
            curve = [{
                "ts": str(h.get("timestamp", ""))[:10],
                "v": round(float(h.get("total_value", 0)), 2),
            } for h in sampled]
            sections.append(
                f"EQUITY CURVE (campionata a {len(curve)} punti su 90 giorni):\n```json\n"
                + json.dumps(curve, ensure_ascii=False, default=str)
                + "\n```"
            )
            counters["equity_curve"] = str(len(curve))
    except Exception as e:
        logger.warning("build_live_context[equity_curve]: %s", e)

    # 5. AGENT LOGS recenti (compressi)
    try:
        import database
        logs = database.get_agent_logs(limit=LOGS_RECENT_LIMIT) or []
        if logs:
            compact_logs = [_compact_log(l) for l in logs]
            # Statistiche per phase
            phase_counts: dict[str, int] = {}
            for l in logs:
                p = l.get("phase", "?")
                phase_counts[p] = phase_counts.get(p, 0) + 1
            sections.append(
                f"AGENT LOGS (ultimi {len(compact_logs)}, compressi):\n"
                f"Conteggi per phase: {json.dumps(phase_counts, ensure_ascii=False)}\n"
                f"```json\n"
                + json.dumps(compact_logs, ensure_ascii=False, default=str)
                + "\n```"
            )
            counters["logs"] = str(len(compact_logs))
    except Exception as e:
        logger.warning("build_live_context[agent_logs]: %s", e)

    # 6. Pre-market briefing
    try:
        import database
        briefing = database.get_latest_pre_market_briefing()
        if briefing and briefing.get("content"):
            content = str(briefing["content"])[:1500]
            sections.append(
                f"PRE-MARKET BRIEFING (latest, troncato 1500 char):\n{content}"
            )
            counters["briefing"] = "yes"
    except Exception:
        pass

    # 7. Weekend intelligence
    try:
        import database
        wknd = database.get_latest_weekend_intelligence()
        if wknd and wknd.get("content"):
            content = str(wknd["content"])[:1200]
            sections.append(
                f"WEEKEND INTELLIGENCE (latest, troncato 1200 char):\n{content}"
            )
            counters["weekend"] = "yes"
    except Exception:
        pass

    # 8. Settings rilevanti (NO chiavi API)
    try:
        import database
        all_settings = database.get_all_settings() or {}
        # Filtra: rimuovi chiavi sensibili (API keys, password)
        SENSITIVE = {"deepseek_api_key", "anthropic_api_key", "news_api_key",
                     "fred_api_key", "openai_api_key",
                     "supabase_db_password", "polygon_api_key", "massive_api_key"}
        filtered = {}
        for k, v in all_settings.items():
            if k.lower() in SENSITIVE or "api_key" in k.lower() or "password" in k.lower():
                filtered[k] = "(redacted)"
            else:
                # Tronca valori molto lunghi (es. prompt)
                vs = str(v)
                if len(vs) > 200:
                    vs = vs[:200] + f"...[{len(vs)} char totali]"
                filtered[k] = vs
        sections.append(
            f"SETTINGS attivi ({len(filtered)} chiavi):\n```json\n"
            + json.dumps(filtered, indent=2, ensure_ascii=False, default=str)
            + "\n```"
        )
        counters["settings"] = str(len(filtered))
    except Exception as e:
        logger.warning("build_live_context[settings]: %s", e)

    # 9. Documenti (solo metadata)
    try:
        import database
        docs = database.get_documents() or []
        if docs:
            metas = [{
                "id": d.get("id"),
                "filename": d.get("filename"),
                "size": d.get("file_size"),
                "category": d.get("category", "generic"),
                "is_preset": bool(d.get("is_preset")),
            } for d in docs]
            sections.append(
                f"TECHNICAL DOCUMENTS ({len(metas)} file caricati, solo metadata):\n```json\n"
                + json.dumps(metas, indent=2, ensure_ascii=False, default=str)
                + "\n```"
            )
            counters["documents"] = str(len(metas))
    except Exception as e:
        logger.warning("build_live_context[documents]: %s", e)

    # 10. Sim runs (counts)
    try:
        from simulator import db as sim_db
        runs = sim_db.list_runs() if hasattr(sim_db, "list_runs") else []
        if runs:
            cat_counts: dict[str, int] = {}
            for r in runs:
                cat = r.get("category", "?")
                cat_counts[cat] = cat_counts.get(cat, 0) + 1
            sections.append(
                f"SIMULATOR RUNS (totale {len(runs)}, per categoria):\n```json\n"
                + json.dumps(cat_counts, indent=2, ensure_ascii=False)
                + "\n```"
            )
            counters["sim_runs"] = str(len(runs))
    except Exception as e:
        logger.warning("build_live_context[sim_runs]: %s", e)

    # 11. Geopolitical snapshots (counts)
    try:
        import database
        snaps = database.get_geopolitical_snapshots(limit=20) or []
        if snaps:
            sources = {}
            for s in snaps:
                src = s.get("source", "?")
                sources[src] = sources.get(src, 0) + 1
            sections.append(
                f"GEOPOLITICAL SNAPSHOTS (ultimi 20, conteggi per source):\n"
                f"```json\n{json.dumps(sources, ensure_ascii=False)}\n```"
            )
            counters["geo_snapshots"] = str(len(snaps))
    except Exception:
        pass

    if not sections:
        return ""

    logger.info("build_live_context: counters=%s", counters)

    header = "=" * 60 + "\nLIVE PLATFORM CONTEXT (snapshot al momento della richiesta).\nL'analista AI ha accesso a TUTTI i dati seguenti.\n" + "=" * 60
    full = header + "\n\n" + "\n\n".join(sections)

    # Hard cap per evitare di saturare il context di R1
    if len(full) > LIVE_CONTEXT_HARD_CAP_CHARS:
        # Tronca preservando l'header e una nota di troncamento
        truncated = full[:LIVE_CONTEXT_HARD_CAP_CHARS]
        truncated += (
            f"\n\n[NOTA: contesto troncato a {LIVE_CONTEXT_HARD_CAP_CHARS} caratteri "
            f"(originale {len(full)}). Alcune sezioni finali potrebbero essere tagliate.]"
        )
        full = truncated
        logger.warning(
            "build_live_context: troncato da %d a %d char",
            len(full), LIVE_CONTEXT_HARD_CAP_CHARS,
        )

    return full


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

    Nota: NON usiamo `database.get_chat_conversations` come probe perche'
    quella swallow le eccezioni (try/except interno → ritorna []) e quindi
    non rileva la tabella mancante. Facciamo invece un probe DIRETTO al
    client Supabase, che propaga l'eccezione "relation does not exist".
    """
    # Probe diretto. Usiamo `get_client()` (pubblico, senza underscore) per
    # determinare il backend: ritorna un Client su Supabase, None su SQLite.
    # NOTA: hasattr(database, "_get_client") NON funziona perche' Python
    # non esporta i simboli con underscore via `from module import *`.
    probe_error = None
    backend = "unknown"
    try:
        import database
        client = None
        try:
            client = database.get_client()  # Supabase: Client; SQLite: None
        except Exception:
            client = None

        if client is not None:
            backend = "supabase"
            try:
                client.table("chat_conversations").select("id").limit(1).execute()
                return (True, "")
            except Exception as e:
                probe_error = str(e)
                logger.warning("ensure_chat_tables: Supabase probe failed: %s", e)
        else:
            backend = "sqlite"
            try:
                import db_sqlite
                with db_sqlite.get_db() as conn:
                    conn.execute("SELECT id FROM chat_conversations LIMIT 1").fetchone()
                return (True, "")
            except Exception as e:
                probe_error = str(e)
                logger.warning("ensure_chat_tables: SQLite probe failed: %s", e)
    except Exception as e:
        probe_error = str(e)
        logger.warning("ensure_chat_tables: import failed: %s", e)

    logger.info("ensure_chat_tables: backend=%s, probe_error=%s", backend, probe_error)

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

    if backend == "sqlite":
        # SQLite path: creiamo le tabelle DIRETTAMENTE invece di affidarci
        # a init_db() (che potrebbe avere ALTER TABLE legacy che fallisce
        # silenziosamente o non rieseguire i CREATE su DB esistente).
        try:
            import db_sqlite
            with db_sqlite.get_db() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS chat_conversations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        title TEXT NOT NULL DEFAULT 'Nuova conversazione',
                        selected_decisions TEXT,
                        created_at TEXT NOT NULL DEFAULT (datetime('now')),
                        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS chat_messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        conversation_id INTEGER NOT NULL,
                        role TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
                        content TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT (datetime('now'))
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_chat_messages_conv
                        ON chat_messages (conversation_id, created_at)
                """)
            # Probe finale
            with db_sqlite.get_db() as conn:
                conn.execute("SELECT id FROM chat_conversations LIMIT 1").fetchone()
            logger.info("ensure_chat_tables: SQLite tables created successfully")
            return (True, "")
        except Exception as e:
            logger.error("ensure_chat_tables: SQLite create failed: %s", e, exc_info=True)
            return (False,
                    f"Creazione tabelle SQLite fallita ({type(e).__name__}: {e}). "
                    f"Probe originale: {probe_error}")

    if not db_url:
        # Su Supabase senza DATABASE_URL non possiamo fare DDL. Ma c'e' un
        # fallback automatico in db_supabase.py che usa la tabella `settings`
        # come storage chiave-valore per la chat. Verifichiamo che funzioni
        # provando una conversazione di test.
        try:
            import database
            test_id = database.create_chat_conversation(
                title="__fallback_test__", selected_decisions=None,
            )
            if test_id:
                # Cleanup
                try:
                    database.delete_chat_conversation(test_id)
                except Exception:
                    pass
                logger.info(
                    "ensure_chat_tables: fallback settings-based attivo (DATABASE_URL "
                    "non configurato, le chat usano la tabella settings)"
                )
                return (True, "")
        except Exception as fb_err:
            logger.warning("ensure_chat_tables: fallback test fallito: %s", fb_err)
        return (False,
                f"Tabelle chat_* mancanti su Supabase E fallback settings non funziona "
                f"(probe error: {probe_error}). "
                "Configurare DATABASE_URL su Render oppure creare le tabelle "
                "manualmente da Supabase Dashboard → SQL Editor.")

    try:
        import psycopg2
    except ImportError:
        return (False, f"psycopg2 non installato — impossibile creare tabelle al volo. "
                       f"Probe error originale: {probe_error}")

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
        # Verifica che siano effettivamente accessibili dal client Supabase ora
        try:
            import database as _db
            client = _db._get_client()
            client.table("chat_conversations").select("id").limit(1).execute()
            return (True, "")
        except Exception as verify_err:
            logger.warning("psycopg2 OK ma client Supabase ancora ko: %s", verify_err)
            return (True, "")  # le tabelle ci sono, sistemera' al ritry
    except Exception as e:
        logger.error("Creazione tabelle chat_* fallita: %s", e)
        return (False,
                f"Creazione tabelle fallita ({type(e).__name__}: {e}). "
                f"Probe originale: {probe_error}")


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
