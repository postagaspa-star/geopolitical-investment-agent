"""
Technical Worker — DeepSeek-V3
Analisi tecnica quantitativa con fallback a Claude Sonnet.

Riceve ticker dal Decision Agent, esegue calcoli tecnici (RSI, MACD, Bollinger,
ATR, supporti/resistenze) e restituisce un JSON puramente tecnico.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

DEEPSEEK_MODEL = "deepseek-chat"
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
FALLBACK_MODEL = "claude-sonnet-4-20250514"

TECH_PROMPT_DEFAULT = """You are a quantitative technical analyst. You receive raw OHLCV data and pre-calculated indicators.

RULES:
- Analyze ALL indicators: RSI, MACD, Bollinger Bands, SMA crossovers, Stochastic, ATR, Volume
- For each ticker give a clear BUY/SELL/HOLD signal with confidence (0-100)
- Calculate key support/resistance levels from the data
- Use ATR for suggested stop-loss distance
- Flag divergences between price and indicators
- Determine the market regime: TRENDING_UP, TRENDING_DOWN, RANGING, VOLATILE

OUTPUT MUST be valid JSON:
{
  "analyses": [
    {
      "ticker": "XOM",
      "signal": "BUY",
      "confidence": 72,
      "current_price": 105.50,
      "support": 102.00,
      "resistance": 110.00,
      "atr": 2.15,
      "rsi": {"value": 35.2, "signal": "OVERSOLD_BUY"},
      "macd": {"value": 0.45, "signal": "BULLISH_CROSS"},
      "bollinger": {"position": "LOWER_BAND", "signal": "BUY"},
      "sma_cross": {"signal": "GOLDEN_CROSS"},
      "stochastic": {"value": 22.5, "signal": "OVERSOLD"},
      "volume_trend": "HIGH",
      "market_regime": "TRENDING_DOWN",
      "reasoning": "RSI oversold near strong support with bullish MACD divergence..."
    }
  ],
  "market_regime": "RANGING",
  "summary": "Overall market assessment..."
}"""


def _get_tech_prompt() -> str:
    """
    Carica il system prompt del Technical Agent.
    Override utente: chiave 'prompt_technical' nelle impostazioni DB.
    Fallback: TECH_PROMPT_DEFAULT.
    """
    try:
        import database as _db
        custom = _db.get_setting("prompt_technical", "")
        if custom and isinstance(custom, str) and custom.strip():
            return custom
    except Exception:
        pass
    return TECH_PROMPT_DEFAULT


def _get_deepseek_key() -> str:
    return os.environ.get("DEEPSEEK_API_KEY", "")


async def _call_deepseek(context: str, max_retries: int = 3) -> tuple[str, str]:
    """
    Chiama DeepSeek-V3 con retry esponenziale.
    Ritorna (response_text, engine_used).
    Fallback a Claude se DeepSeek fallisce.
    """
    api_key = _get_deepseek_key()
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY non configurata")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": _get_tech_prompt()},
            {"role": "user", "content": context},
        ],
        "max_tokens": 4096,
        "temperature": 0.2,
    }

    last_error = None
    for attempt in range(max_retries):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    DEEPSEEK_API_URL, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=60)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data["choices"][0]["message"]["content"], "deepseek-v3"
                    elif resp.status == 429:
                        wait = 2 ** (attempt + 1)
                        logger.warning("[TECH] DeepSeek 429, retry in %ds...", wait)
                        await asyncio.sleep(wait)
                        continue
                    else:
                        body = await resp.text()
                        last_error = f"HTTP {resp.status}: {body[:200]}"
        except Exception as e:
            last_error = str(e)
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** (attempt + 1))
                continue

    raise ValueError(f"DeepSeek failed after {max_retries} attempts: {last_error}")


async def _call_claude_fallback(context: str) -> tuple[str, str]:
    """
    Fallback: usa Claude Sonnet se DeepSeek non risponde.
    Wrappa la chiamata SDK sincrona in asyncio.to_thread per non bloccare
    l'event loop (era un bug: una chiamata Sonnet fermava polling, watchdog,
    e tutti gli altri agenti per 5-30 secondi).
    """
    from anthropic import Anthropic

    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        try:
            import database as _db
            key = _db.get_setting("anthropic_api_key", "")
        except Exception:
            pass

    client = Anthropic(api_key=key)

    def _sync_call():
        return client.messages.create(
            model=FALLBACK_MODEL,
            max_tokens=4096,
            system=_get_tech_prompt(),
            messages=[{"role": "user", "content": context}],
        )

    response = await asyncio.to_thread(_sync_call)
    text = ""
    for block in response.content:
        if hasattr(block, "text"):
            text += block.text
    return text, "claude-sonnet-fallback"


async def _fetch_ticker_indicators(ticker: str, period_days: int = 90) -> dict:
    """
    Recupera dati OHLCV e calcola indicatori tecnici per un ticker.
    Usa yfinance con fallback ClawStreet (via data_fetchers).
    """
    import data_fetchers
    import technical_analysis
    import pandas as pd

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
    market_data = await loop.run_in_executor(
        None, data_fetchers.fetch_market_data, ticker, period_days
    )

    if not market_data.get("data"):
        return {
            "ticker": ticker,
            "error": market_data.get("error", "No data"),
            "source": market_data.get("source", "unknown"),
        }

    # Calcola indicatori
    df = pd.DataFrame(market_data["data"])
    df.set_index("date", inplace=True)
    df.columns = [c.capitalize() for c in df.columns]

    analysis = technical_analysis.analyze_ticker(df)

    # Aggiungi prezzo corrente e ATR
    current_price = market_data["data"][-1]["close"] if market_data["data"] else 0

    # Calcola ATR manualmente (14 periodi)
    atr = _calculate_atr(market_data["data"])

    return {
        "ticker": ticker,
        "current_price": current_price,
        "atr": atr,
        "analysis": analysis,
        "data_points": len(market_data["data"]),
        "source": market_data.get("source", "yfinance"),
    }


def _calculate_atr(data: list[dict], period: int = 14) -> float:
    """Calcola Average True Range dagli ultimi N periodi."""
    if len(data) < period + 1:
        return 0.0

    true_ranges = []
    for i in range(1, len(data)):
        high = data[i].get("high", 0)
        low = data[i].get("low", 0)
        prev_close = data[i - 1].get("close", 0)
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)

    if len(true_ranges) < period:
        return sum(true_ranges) / len(true_ranges) if true_ranges else 0.0

    return sum(true_ranges[-period:]) / period


async def run_technical_analysis(run_id: str, tickers: list[str]) -> dict:
    """
    Esegue analisi tecnica parallela per una lista di ticker.
    Usa DeepSeek-V3 come motore principale, Claude come fallback.

    Args:
        run_id: ID del run corrente
        tickers: Lista di ticker da analizzare (max 10)

    Returns:
        dict con analisi per ogni ticker + regime di mercato
    """
    import database

    logger.info("[%s][TECH] Avvio analisi tecnica per %d tickers: %s",
                run_id, len(tickers), tickers[:10])

    if not tickers:
        return {"analyses": [], "summary": "Nessun ticker da analizzare", "engine": "none"}

    # Normalizza ticker UPPERCASE (yfinance è case-sensitive su crypto: btc-usd ≠ BTC-USD)
    tickers = [t.upper().strip() for t in tickers if t]
    # Limita a 10 tickers
    tickers = tickers[:10]

    # 1. Recupera indicatori in parallelo
    tasks = [_fetch_ticker_indicators(t) for t in tickers]
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    ticker_data = []
    failed_tickers: list[str] = []
    for i, result in enumerate(raw_results):
        if isinstance(result, Exception):
            failed_tickers.append(tickers[i])
            continue
        # _fetch_ticker_indicators ritorna un dict con 'error' se i dati non sono disponibili
        if isinstance(result, dict) and result.get("error") and not result.get("current_price"):
            failed_tickers.append(tickers[i])
            continue
        # Solo ticker con dati validi vanno al LLM (evita "?" values nel report)
        ticker_data.append(result)

    if failed_tickers:
        logger.warning("[%s][TECH] %d/%d ticker senza dati indicatori (saltati): %s",
                       run_id, len(failed_tickers), len(tickers), failed_tickers)

    # Se TUTTI i ticker sono falliti, esci subito con un report vuoto coerente
    # invece di mandare un contesto vuoto a DeepSeek (che produrrebbe '?' values).
    if not ticker_data:
        try:
            database.insert_agent_log(run_id, "TECH_WORKER", json.dumps({
                "event": "technical_skipped_no_data",
                "tickers_requested": len(tickers),
                "tickers_failed": failed_tickers,
            }))
        except Exception:
            pass
        return {
            "analyses": [],
            "summary": f"Nessun dato disponibile per i {len(tickers)} ticker richiesti (yfinance/Massive falliti). Decision Agent userà Pure Macro mode.",
            "engine": "skipped_no_data",
            "failed_tickers": failed_tickers,
        }

    # 2. Prepara contesto per DeepSeek
    context = json.dumps({
        "tickers_data": ticker_data,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "instruction": "Analyze these technical indicators and produce the JSON report.",
    }, default=str, ensure_ascii=False)

    # 3. Chiama DeepSeek-V3 (engine FISSO, no fallback Claude per controllo costi)
    # Il Technical Agent gira sempre 24/7 a costo predicibile (~$0.0006/run).
    # Se DeepSeek fallisce, andiamo direttamente in Pure Macro mode (solo
    # indicatori grezzi) invece di pagare Sonnet 4 che costerebbe ~50× tanto.
    engine = "none"
    try:
        deepseek_key = _get_deepseek_key()
        if not deepseek_key:
            raise ValueError("DEEPSEEK_API_KEY non configurata")
        response_text, engine = await _call_deepseek(context[:20000])
    except Exception as ds_err:
        logger.warning("[%s][TECH] DeepSeek-V3 fallito (%s) — Pure Macro mode (no Claude fallback)",
                       run_id, ds_err)
        database.insert_agent_log(run_id, "TECH_WORKER", json.dumps({
            "event": "technical_pure_macro_fallback",
            "error": str(ds_err)[:200],
            "tickers": list(ticker_data.keys()),
        }))
        return {
            "analyses": [],
            "raw_indicators": ticker_data,
            "engine": "pure_macro_fallback",
            "error": f"DeepSeek failed: {ds_err}",
            "summary": "Technical DeepSeek-V3 non disponibile — Decision opera in Pure Macro mode con indicatori grezzi.",
        }

    # 4. Parse risultato
    try:
        json_start = response_text.find("{")
        json_end = response_text.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            report = json.loads(response_text[json_start:json_end])
        else:
            report = {"analyses": [], "raw_analysis": response_text}
    except json.JSONDecodeError:
        report = {"analyses": [], "raw_analysis": response_text}

    report["engine"] = engine
    report["raw_indicators"] = ticker_data

    # 5. Log
    database.insert_agent_log(run_id, "TECH_WORKER",
        json.dumps({
            "event": "technical_analysis_complete",
            "engine": engine,
            "tickers_analyzed": len(report.get("analyses", [])),
            "tickers_requested": len(tickers),
        }))

    logger.info("[%s][TECH] Analisi completata via %s: %d ticker",
                run_id, engine, len(report.get("analyses", [])))
    return report
