"""
Watchdog Agent — DeepSeek-V3
Monitor ultra-leggero ogni 5 minuti durante le ore di mercato.

Compito: decidere in <300ms se c'è qualcosa di abbastanza importante
da svegliare la pipeline Technical + Decision (Sonnet 4.5).

Input:  prezzi chiave (top movers), ultime 3 headlines dal buffer
Output: {"trigger": bool, "urgency": 1-10, "reason": str, "focus_tickers": [...]}

Throttle: Decision non parte mai più di 1 volta per ora.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta

import aiohttp

logger = logging.getLogger(__name__)

DEEPSEEK_MODEL = "deepseek-chat"
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_TIMEOUT = 20

# Soglia urgenza per triggerare la pipeline completa
URGENCY_THRESHOLD = 5  # 1-10

WATCHDOG_PROMPT = """You are a financial market watchdog. Your ONLY job is to decide in seconds
whether current market conditions justify waking up the Decision Agent (expensive model).

The user message will tell you the MARKET STATE (OPEN or CLOSED). Adjust accordingly:

═══════════════════════════════════════════════════════════════════════════
WHEN MARKET STATE = OPEN (NYSE/LSE/XETRA hours, no holiday):
═══════════════════════════════════════════════════════════════════════════
TRIGGER if you detect ANY of:
- Price move > 1.5% in last 5 minutes on any major index/ETF or watched stock
- Breaking geopolitical news (conflict, sanctions, elections, central bank)
- Earnings surprise or M&A announcement for watched tickers
- VIX spike or bond yield jump > 5 basis points
- Congressional trade on a ticker in the watchlist
- Unusual volume (>2x average)
- Significant crypto move (BTC/ETH > 3% in last hour)

DO NOT TRIGGER for:
- Normal market noise (< 0.5% moves)
- Already-known news (nothing new in last 20 min)
- Pre-market/after-hours with no catalyst

═══════════════════════════════════════════════════════════════════════════
WHEN MARKET STATE = CLOSED (overnight, weekend, US/UK/DE holiday):
═══════════════════════════════════════════════════════════════════════════
The equity market is CLOSED. Stock prices in the snapshot are STALE
(last close, not real-time). IGNORE any apparent "moves" on equity tickers
(MSFT, AAPL, XOM, etc.) — they are NOISE, not real movements.

TRIGGER ONLY if:
- Crypto move > 3% in last hour (BTC-USD, ETH-USD, SOL-USD…) — crypto IS 24/7
- Breaking geopolitical/macro news with implications for crypto or Monday open
- Major hack/regulation/exchange news affecting crypto
- focus_tickers MUST be crypto in yfinance format (BTC-USD, ETH-USD, ...) —
  never equity (MSFT, AAPL) since equity markets are closed

DO NOT TRIGGER if focus would be equity tickers — wait for market to reopen.

═══════════════════════════════════════════════════════════════════════════
Respond ONLY with valid JSON, no other text:
{
  "trigger": true/false,
  "urgency": 1-10,
  "reason": "one-line explanation",
  "focus_tickers": ["BTC-USD", "ETH-USD"]
}"""


def _get_deepseek_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        try:
            import database as _db
            key = _db.get_setting("deepseek_api_key", "") or ""
        except Exception:
            pass
    return key


def _get_last_decision_time(database) -> datetime | None:
    """Recupera il timestamp dell'ultimo run del Decision Agent."""
    try:
        client = database.get_client()
        if client:
            result = client.table("agent_checkpoints") \
                .select("updated_at") \
                .eq("agent_name", "decision") \
                .eq("status", "COMPLETED") \
                .order("updated_at", desc=True) \
                .limit(1) \
                .execute()
            if result.data:
                ts_str = result.data[0]["updated_at"]
                return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        pass

    # Fallback: controlla agent_logs — qualunque tentativo (success o crash)
    # IMPORTANTE: il throttle DEVE attivarsi anche se il Decision è crashato.
    # Prima guardavamo solo DECISION_COMPLETE: se Sonnet falliva (es. quota
    # API esaurita), il flag non si scriveva → throttle non scattava → loop
    # infinito di retry ogni 1 min × 8h = $$$. Ora throttiamo su QUALSIASI
    # log che indichi un tentativo recente:
    #   - DECISION_COMPLETE (run riuscito)
    #   - DECISION_ERROR (run crashato — già abbastanza per non riprovare subito)
    #   - DECISION_CONTEXT (context loaded → tentativo iniziato)
    #   - watchdog_triggered nell'ORCHESTRATOR (Watchdog ha già triggerato)
    try:
        client = database.get_client()
        if client:
            result = client.table("agent_logs") \
                .select("timestamp,phase") \
                .in_("phase", ["DECISION_COMPLETE", "DECISION_ERROR", "DECISION_CONTEXT"]) \
                .order("timestamp", desc=True) \
                .limit(1) \
                .execute()
            if result.data:
                ts_str = result.data[0]["timestamp"]
                return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        pass
    return None


def _count_recent_decision_errors(database, minutes: int = 60) -> int:
    """
    Conta i DECISION_ERROR negli ultimi N minuti. Usato per backoff
    esponenziale: se troppi errori consecutivi, throttle aggressivo.
    """
    try:
        client = database.get_client()
        if not client:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        result = client.table("agent_logs") \
            .select("id") \
            .eq("phase", "DECISION_ERROR") \
            .gte("timestamp", cutoff) \
            .limit(20) \
            .execute()
        return len(result.data or [])
    except Exception:
        return 0


def _is_throttled(database, throttle_minutes: int = 60) -> bool:
    """
    Ritorna True se il Decision Agent ha tentato di girare negli ultimi N min.

    BACKOFF SU ERRORI: se gli ultimi N minuti contengono 2+ DECISION_ERROR,
    estende il throttle a 4h (240 min) per evitare di bruciare token su
    chiamate API che falliscono sistematicamente (es. quota esaurita,
    chiave invalida, modello deprecato).
    """
    last = _get_last_decision_time(database)
    if last is None:
        return False
    elapsed_min = (datetime.now(timezone.utc) - last).total_seconds() / 60

    # Backoff esponenziale: se 2+ errori recenti, allunga il throttle
    error_count = _count_recent_decision_errors(database, minutes=60)
    effective_throttle = throttle_minutes
    if error_count >= 2:
        effective_throttle = max(throttle_minutes, 240)  # min 4h
        logger.info("Watchdog: %d errori Decision in 60min → backoff a %d min",
                    error_count, effective_throttle)

    if elapsed_min < effective_throttle:
        logger.debug("Watchdog throttled: ultimo Decision %.0f min fa (limit %d min)",
                     elapsed_min, effective_throttle)
        return True
    return False


def _get_recent_headlines(database, minutes: int = 10) -> list[str]:
    """Recupera le ultime headline dal buffer."""
    headlines = []
    try:
        client = database.get_client()
        if client:
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
            result = client.table("intelligence_buffer") \
                .select("micro_summary, sentiment_score") \
                .gte("timestamp", cutoff) \
                .order("timestamp", desc=True) \
                .limit(5) \
                .execute()
            if result.data:
                for rec in result.data:
                    score = rec.get("sentiment_score", 0)
                    summary = rec.get("micro_summary", "")
                    if summary:
                        sign = "+" if score >= 0 else ""
                        headlines.append(f"[{sign}{score:.2f}] {summary[:120]}")
    except Exception:
        pass
    return headlines


async def _get_price_snapshot(extra_tickers: list[str] | None = None) -> dict:
    """
    Snapshot rapido prezzi dalla cache price_quotes (aggiornata ogni 10 min
    da price_polling). Molto più veloce che chiamare yfinance ogni minuto.

    Args:
        extra_tickers: ticker addizionali da includere (tipicamente le
                       posizioni del portafoglio nel deep-check ogni 3 giri).
    """
    snapshot = {}
    try:
        from price_polling import get_cached_prices_bulk
        tickers = ["SPY", "QQQ", "XOM", "LMT", "GLD", "VIX",
                   "AAPL", "MSFT", "NVDA", "TLT",
                   "BTC-USD", "ETH-USD"]  # crypto incluse — si muovono 24/7
        if extra_tickers:
            # dedupa preservando ordine
            seen = set(tickers)
            for t in extra_tickers:
                if t and t not in seen:
                    tickers.append(t)
                    seen.add(t)
        # Cache age max 15 min (polling è ogni 10 min, lascio 5 min di margine
        # per evitare buchi se un ciclo di polling tarda).
        cached = await asyncio.to_thread(get_cached_prices_bulk, tickers, 900)
        for t, q in cached.items():
            snapshot[t] = {
                "price": round(q["price"], 2),
                "chg_pct": round(q.get("change_pct", 0), 3),
                "age_s": q.get("age_seconds", 0),
            }
    except Exception as e:
        logger.debug("Watchdog cache snapshot error: %s", e)
    return snapshot


def _get_portfolio_tickers(database) -> list[str]:
    """Ritorna i ticker delle posizioni aperte attualmente."""
    try:
        positions = database.get_positions() or []
        return [p.get("ticker", "") for p in positions if p.get("ticker")]
    except Exception:
        return []


def _get_headlines_for_tickers(database, tickers: list[str], minutes: int = 60) -> list[str]:
    """
    Cerca nel buffer headlines che menzionano almeno uno dei ticker forniti.
    Usato ogni 3 giri per il deep-check del portafoglio.
    """
    if not tickers:
        return []
    headlines = []
    try:
        client = database.get_client()
        if not client:
            return headlines
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        result = client.table("intelligence_buffer") \
            .select("micro_summary,raw_content,sentiment_score") \
            .gte("timestamp", cutoff) \
            .order("timestamp", desc=True) \
            .limit(50) \
            .execute()
        # Filtra in Python per ticker mention (case-insensitive)
        ticker_re_parts = [t.upper().split("-")[0] for t in tickers]
        for rec in (result.data or []):
            summary = (rec.get("micro_summary") or "")[:200]
            raw = (rec.get("raw_content") or "")[:400]
            haystack = (summary + " " + raw).upper()
            for tk in ticker_re_parts:
                if tk and tk in haystack:
                    score = rec.get("sentiment_score", 0)
                    sign = "+" if (score or 0) >= 0 else ""
                    headlines.append(f"[{tk} {sign}{score:.2f}] {summary[:140]}")
                    break  # un match basta per questa card
            if len(headlines) >= 8:
                break
    except Exception as e:
        logger.debug("Headlines per portfolio fallita: %s", e)
    return headlines


def _increment_watchdog_counter(database) -> int:
    """
    Incrementa contatore globale dei run watchdog. Ritorna il nuovo valore.
    Usato per attivare il portfolio deep-check ogni 3 giri.
    """
    try:
        cur = int(database.get_setting("watchdog_run_counter", "0") or 0)
    except Exception:
        cur = 0
    new = cur + 1
    try:
        database.set_setting("watchdog_run_counter", str(new))
    except Exception:
        pass
    return new


async def _call_deepseek(context: str) -> dict:
    """Chiama DeepSeek-V3 per la valutazione ultra-rapida."""
    key = _get_deepseek_key()
    if not key:
        return {"trigger": False, "urgency": 0, "reason": "no_deepseek_key", "focus_tickers": []}

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": WATCHDOG_PROMPT},
            {"role": "user", "content": context},
        ],
        "temperature": 0.1,
        "max_tokens": 200,
    }

    try:
        timeout = aiohttp.ClientTimeout(total=DEEPSEEK_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                DEEPSEEK_API_URL,
                json=payload,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            ) as resp:
                if resp.status != 200:
                    logger.warning("Watchdog DeepSeek HTTP %d", resp.status)
                    return {"trigger": False, "urgency": 0, "reason": f"http_{resp.status}", "focus_tickers": []}
                data = await resp.json()
                text = data["choices"][0]["message"]["content"].strip()
                # Estrai JSON dalla risposta in modo tollerante:
                # 1. Se c'è un blocco ``` ... ``` prendilo
                # 2. Altrimenti cerca il primo { e l'ultimo }
                payload = None
                if "```" in text:
                    try:
                        chunk = text.split("```")[1].replace("json", "", 1).strip()
                        payload = json.loads(chunk)
                    except Exception:
                        pass
                if payload is None:
                    j_start = text.find("{")
                    j_end = text.rfind("}") + 1
                    if j_start >= 0 and j_end > j_start:
                        try:
                            payload = json.loads(text[j_start:j_end])
                        except Exception:
                            pass
                if payload is None:
                    payload = json.loads(text)  # tenta diretto, lascia che fallisca se invalido
                return payload
    except json.JSONDecodeError as e:
        logger.warning("Watchdog JSON parse error: %s", e)
        return {"trigger": False, "urgency": 0, "reason": "json_parse_error", "focus_tickers": []}
    except Exception as e:
        logger.warning("Watchdog DeepSeek error: %s", e)
        return {"trigger": False, "urgency": 0, "reason": str(e)[:80], "focus_tickers": []}


async def run_watchdog(run_id: str) -> dict:
    """
    Esegue il ciclo Watchdog:
    1. Snapshot prezzi + headlines recenti
    2. DeepSeek mini-analisi
    3. Se trigger + non throttled → ritorna should_trigger=True
    """
    import database

    t0 = time.time()

    # 1. Controlla throttle prima di fare qualsiasi altra cosa
    if _is_throttled(database, throttle_minutes=60):
        database.insert_agent_log(run_id, "WATCHDOG", json.dumps({
            "event": "watchdog_throttled",
            "reason": "decision_ran_recently",
        }))
        return {"should_trigger": False, "reason": "throttled", "urgency": 0}

    # 1b. Counter globale: ogni 3 giri attiviamo il deep-check sul portafoglio
    #     (prezzi specifici delle posizioni + headlines che le menzionano).
    #     Tra un deep-check e l'altro, il watchdog usa solo i ticker base
    #     (SPY/QQQ/MSFT...) per restare ultra-rapido.
    counter = await asyncio.to_thread(_increment_watchdog_counter, database)
    deep_check = (counter % 3 == 0)
    portfolio_tickers: list[str] = []
    if deep_check:
        portfolio_tickers = await asyncio.to_thread(_get_portfolio_tickers, database)
        logger.info("[%s][WATCHDOG] DEEP-CHECK turno (run #%d): %d ticker portfolio %s",
                    run_id, counter, len(portfolio_tickers), portfolio_tickers)

    # 2. Raccogli dati in parallelo. Nel deep-check passiamo anche i ticker
    #    del portafoglio per arricchire lo snapshot prezzi e cerchiamo
    #    headlines specifiche.
    if deep_check and portfolio_tickers:
        price_snap, headlines, port_news = await asyncio.gather(
            _get_price_snapshot(extra_tickers=portfolio_tickers),
            asyncio.to_thread(_get_recent_headlines, database, 10),
            asyncio.to_thread(_get_headlines_for_tickers, database, portfolio_tickers, 60),
        )
    else:
        price_snap, headlines = await asyncio.gather(
            _get_price_snapshot(),
            asyncio.to_thread(_get_recent_headlines, database, 10),
        )
        port_news = []

    # 3. Costruisci contesto compatto
    context_parts = []

    # Header con MARKET STATE — il prompt usa questo per scegliere il
    # comportamento (open: trigger su qualunque cosa rilevante; closed: solo
    # crypto, ignora equity moves che sono sicuramente stale).
    try:
        from scheduler import is_market_open, is_market_holiday
        market_open = is_market_open()
        is_holiday = is_market_holiday()
        if market_open:
            state_line = "MARKET STATE: OPEN (equity + crypto tradabili)"
        else:
            why = "weekend" if datetime.now(timezone.utc).weekday() >= 5 else (
                "holiday" if is_holiday else "after-hours")
            state_line = f"MARKET STATE: CLOSED ({why}) — only crypto is tradable"
    except Exception:
        state_line = "MARKET STATE: UNKNOWN"
    context_parts.append(state_line)

    if price_snap:
        movers = sorted(price_snap.items(), key=lambda x: abs(x[1].get("chg_pct", 0)), reverse=True)
        price_lines = [f"{t}: ${v['price']} ({v['chg_pct']:+.2f}%)" for t, v in movers[:6]]
        context_parts.append("PRICE SNAPSHOT (last 5min):\n" + "\n".join(price_lines))

    if headlines:
        context_parts.append("RECENT INTELLIGENCE (last 10min):\n" + "\n".join(headlines))
    else:
        context_parts.append("RECENT INTELLIGENCE: none in last 10 minutes")

    # DEEP-CHECK ogni 3 giri: focus speciale sulle posizioni del portafoglio
    if deep_check and portfolio_tickers:
        port_lines = ["PORTFOLIO STOCKS DEEP-CHECK (every 3 runs):",
                      f"  Holdings: {', '.join(portfolio_tickers)}"]
        # prezzi specifici delle posizioni
        port_prices = []
        for tk in portfolio_tickers:
            if tk in price_snap:
                p = price_snap[tk]
                port_prices.append(f"    {tk}: ${p['price']} ({p['chg_pct']:+.2f}%)")
        if port_prices:
            port_lines.append("  Prices:")
            port_lines.extend(port_prices)
        # headlines specifiche per i ticker del portafoglio
        if port_news:
            port_lines.append("  News mentioning holdings (last 60min):")
            port_lines.extend(f"    {h}" for h in port_news[:6])
        else:
            port_lines.append("  News: none mentioning holdings in last 60min")
        port_lines.append("  → If any holding shows >2% adverse move OR negative")
        port_lines.append("    breaking news: TRIGGER (urgency >= 7) so Decision")
        port_lines.append("    can decide whether to take profit / stop-loss / re-balance.")
        context_parts.append("\n".join(port_lines))

    context_parts.append(f"Time: {datetime.now(timezone.utc).strftime('%H:%M UTC')}")
    context = "\n\n".join(context_parts)

    # 4. Chiama DeepSeek
    result = await _call_deepseek(context)

    urgency = result.get("urgency", 0)
    trigger = result.get("trigger", False) and urgency >= URGENCY_THRESHOLD
    reason = result.get("reason", "")
    focus_tickers = result.get("focus_tickers", [])

    elapsed = round(time.time() - t0, 2)

    # 5. Logga
    database.insert_agent_log(run_id, "WATCHDOG", json.dumps({
        "event": "watchdog_complete",
        "trigger": trigger,
        "urgency": urgency,
        "reason": reason,
        "focus_tickers": focus_tickers,
        "elapsed_seconds": elapsed,
        "deep_check": deep_check,
        "portfolio_tickers": portfolio_tickers if deep_check else [],
        "run_counter": counter,
    }))

    logger.info("[%s][WATCHDOG] trigger=%s urgency=%d reason='%s' (%.2fs)",
                run_id, trigger, urgency, reason, elapsed)

    return {
        "should_trigger": trigger,
        "urgency": urgency,
        "reason": reason,
        "focus_tickers": focus_tickers,
        "elapsed_seconds": elapsed,
    }
