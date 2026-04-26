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

TRIGGER if you detect ANY of:
- Price move > 1.5% in last 5 minutes on any major index/ETF
- Breaking geopolitical news (conflict, sanctions, elections, central bank)
- Earnings surprise or M&A announcement for watched tickers
- VIX spike or bond yield jump > 5 basis points
- Congressional trade on a ticker in the watchlist
- Unusual volume (>2x average)

DO NOT TRIGGER for:
- Normal market noise (< 0.5% moves)
- Already-known news (nothing new in last 20 min)
- Pre-market/after-hours with no catalyst

Respond ONLY with valid JSON, no other text:
{
  "trigger": true/false,
  "urgency": 1-10,
  "reason": "one-line explanation",
  "focus_tickers": ["XOM", "LMT"]
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

    # Fallback: controlla agent_logs
    try:
        client = database.get_client()
        if client:
            result = client.table("agent_logs") \
                .select("timestamp") \
                .eq("phase", "DECISION_COMPLETE") \
                .order("timestamp", desc=True) \
                .limit(1) \
                .execute()
            if result.data:
                ts_str = result.data[0]["timestamp"]
                return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        pass
    return None


def _is_throttled(database, throttle_minutes: int = 60) -> bool:
    """Ritorna True se il Decision Agent ha girato negli ultimi N minuti."""
    last = _get_last_decision_time(database)
    if last is None:
        return False
    elapsed = (datetime.now(timezone.utc) - last).total_seconds() / 60
    if elapsed < throttle_minutes:
        logger.debug("Watchdog throttled: Decision ran %.0f min ago (limit %d min)",
                     elapsed, throttle_minutes)
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


async def _get_price_snapshot() -> dict:
    """Snapshot rapido prezzi: top movers da yfinance."""
    snapshot = {}
    try:
        import yfinance as yf
        tickers = ["SPY", "QQQ", "XOM", "LMT", "GLD", "VIX"]
        data = yf.download(tickers, period="1d", interval="5m",
                           progress=False, auto_adjust=True)
        if data is not None and not data.empty:
            closes = data["Close"].iloc[-2:] if len(data) >= 2 else None
            if closes is not None and len(closes) >= 2:
                for t in tickers:
                    if t in closes.columns:
                        prev = float(closes[t].iloc[-2])
                        curr = float(closes[t].iloc[-1])
                        if prev > 0:
                            pct = (curr - prev) / prev * 100
                            snapshot[t] = {"price": round(curr, 2), "chg_pct": round(pct, 3)}
    except Exception as e:
        logger.debug("Watchdog price snapshot error: %s", e)
        # Fallback: dati vuoti — lascio la decisione alle headlines
    return snapshot


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
                # Estrai JSON dalla risposta
                if "```" in text:
                    text = text.split("```")[1].replace("json", "").strip()
                return json.loads(text)
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

    # 2. Raccogli dati in parallelo
    price_snap, headlines = await asyncio.gather(
        _get_price_snapshot(),
        asyncio.to_thread(_get_recent_headlines, database, 10),
    )

    # 3. Costruisci contesto compatto
    context_parts = []

    if price_snap:
        movers = sorted(price_snap.items(), key=lambda x: abs(x[1].get("chg_pct", 0)), reverse=True)
        price_lines = [f"{t}: ${v['price']} ({v['chg_pct']:+.2f}%)" for t, v in movers[:6]]
        context_parts.append("PRICE SNAPSHOT (last 5min):\n" + "\n".join(price_lines))

    if headlines:
        context_parts.append("RECENT INTELLIGENCE (last 10min):\n" + "\n".join(headlines))
    else:
        context_parts.append("RECENT INTELLIGENCE: none in last 10 minutes")

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
