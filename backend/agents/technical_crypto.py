"""
Technical Crypto Agent — DeepSeek-V3, focus ESCLUSIVO crypto ClawStreet 24/7.

Differenze rispetto a technical.py:
  - Universo HARD-LIMITED ai 14 ticker crypto supportati da ClawStreet
    (BTC-USD, ETH-USD, SOL-USD, DOGE-USD, AVAX-USD, ADA-USD, XRP-USD,
    LTC-USD, DOT-USD, LINK-USD, UNI-USD, ATOM-USD, MATIC-USD, NEAR-USD)
  - Ogni ticker richiesto viene validato via clawstreet_universe.is_supported()
    PRIMA di chiamare DeepSeek; ticker non-crypto / non-supportati → skip
  - Prompt specializzato su tecnica crypto: volumi 24h, funding rate,
    on-chain flow, wallet activity, sentiment retail (X/Reddit), non gap
    di apertura, alta sensibilità a sentiment regulatorio.

Costo per run: ~$0.0006 (DeepSeek-V3, prompt minimal).
Schedule: ogni 1 ora, 24/7, indipendente da market hours.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import aiohttp

logger = logging.getLogger(__name__)

DEEPSEEK_MODEL = "deepseek-chat"   # V3
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"

# Hard-limit dei 14 ticker crypto supportati da ClawStreet.
# La verifica live via clawstreet_universe.is_supported() li include sempre,
# ma teniamo questa lista come default per i run senza Watchdog focus.
DEFAULT_CRYPTO_UNIVERSE = [
    "BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD", "AVAX-USD",
    "ADA-USD", "XRP-USD", "LTC-USD", "DOT-USD", "LINK-USD",
    "UNI-USD", "ATOM-USD", "MATIC-USD", "NEAR-USD",
]

CRYPTO_TECHNICAL_PROMPT_DEFAULT = """Sei un Technical Analyst specializzato ESCLUSIVAMENTE in crypto-asset top-cap (BTC, ETH, SOL, ecc.).

Analisi richiesta per ogni ticker:
1. Trend strutturale 4H/1D: bullish / bearish / range-bound
2. Livelli chiave: support / resistance / next breakout target
3. Momentum indicators: RSI 14, MACD, volume 24h vs 7d avg
4. Anomalie crypto-specific:
   - Volume spike >2× rispetto alla media 7d
   - Movimento >3% in 1h o >7% in 24h
   - Test di livelli psicologici (BTC 100k, ETH 5k, ecc.)
5. Bias retail vs institutional (se Reddit/X buffer fornisce indizi)
6. Risk note: stop-loss tecnico suggerito (in % vs current price)

Le crypto sono 24/7 — non c'è "gap di apertura" né "after hours".
Considera SEMPRE che il sentiment può cambiare in pochi minuti per:
- News regolamentari (SEC, MiCA, banche centrali)
- Hack / exploit / depeg
- Movimenti di whale wallet
- Funding rate squeeze sui derivati

OUTPUT JSON (nessun preambolo, solo JSON):
{
  "analyses": [
    {
      "ticker": "BTC-USD",
      "trend": "BULLISH|BEARISH|NEUTRAL",
      "rsi_14": 58.3,
      "support": 95000,
      "resistance": 102000,
      "stop_loss_pct": 3.5,
      "signal": "BUY|SELL|HOLD",
      "confidence": 0-100,
      "reasoning": "2-3 frasi tecniche."
    }
  ],
  "summary": "Sintesi 1-2 frasi del setup crypto complessivo."
}"""


def _get_deepseek_key() -> str:
    return os.environ.get("DEEPSEEK_API_KEY", "")


def _get_crypto_technical_prompt() -> str:
    """Carica il prompt custom dalle settings, fallback al default."""
    try:
        import database as _db
        custom = _db.get_setting("prompt_technical_crypto", "")
        if custom and isinstance(custom, str) and custom.strip():
            return custom
    except Exception:
        pass
    return CRYPTO_TECHNICAL_PROMPT_DEFAULT


async def _fetch_crypto_indicators(ticker: str) -> dict:
    """
    Recupera indicatori tecnici crypto da yfinance.
    Usa periodo 30 giorni per avere abbastanza dati per RSI/MACD.
    """
    import data_fetchers
    try:
        # Riusa il fetcher generico — yfinance funziona bene con BTC-USD ecc.
        data = await data_fetchers.fetch_yfinance_indicators(ticker, period_days=30)
        if data and not data.get("error"):
            return data
        return {"ticker": ticker, "error": data.get("error", "no data") if data else "fetch failed"}
    except Exception as exc:
        return {"ticker": ticker, "error": str(exc)[:200]}


async def _call_deepseek(context: str, max_retries: int = 2) -> tuple[str, str]:
    """Chiama DeepSeek-V3. Ritorna (response_text, engine_used)."""
    api_key = _get_deepseek_key()
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY non configurata")

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": _get_crypto_technical_prompt()},
            {"role": "user", "content": context},
        ],
        "temperature": 0.2,
        "max_tokens": 3000,
    }

    last_error = None
    for attempt in range(max_retries):
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(
                    DEEPSEEK_API_URL, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data["choices"][0]["message"]["content"], "deepseek-v3"
                    else:
                        body = await resp.text()
                        last_error = f"HTTP {resp.status}: {body[:200]}"
        except Exception as e:
            last_error = str(e)
        if attempt < max_retries - 1:
            import asyncio
            await asyncio.sleep(2 ** (attempt + 1))

    raise ValueError(f"DeepSeek-V3 failed after {max_retries} attempts: {last_error}")


def _filter_to_clawstreet_crypto(tickers: list[str]) -> list[str]:
    """
    Filtra una lista di ticker tenendo solo quelli che sono crypto E supportati
    da ClawStreet. Tutti gli altri (equity, crypto non supportate) sono scartati.
    """
    try:
        from clawstreet_universe import is_supported
    except ImportError:
        # Fallback: usa la lista hard-coded
        valid = set(DEFAULT_CRYPTO_UNIVERSE)
        return [t for t in tickers if t.upper() in valid]

    out = []
    for t in tickers:
        t_up = t.upper()
        # Deve essere crypto (suffisso -USD o prefisso X:) E supportato
        is_crypto = t_up.endswith("-USD") or t_up.startswith("X:")
        if is_crypto and is_supported(t_up):
            out.append(t_up)
    return out


async def run_crypto_technical(run_id: str, tickers: list[str] | None = None) -> dict:
    """
    Esegue analisi tecnica crypto-only.

    Args:
        run_id: ID del run multi-agente
        tickers: lista ticker da analizzare. Se None, usa DEFAULT_CRYPTO_UNIVERSE
                 ridotto a 6 ticker top liquidità per contenere costi.

    Returns:
        dict con campi: analyses[], engine, summary, filtered_count
    """
    import database

    # Determina universo. Se non specificato, top 6 crypto liquidità.
    if not tickers:
        tickers = ["BTC-USD", "ETH-USD", "SOL-USD",
                   "DOGE-USD", "AVAX-USD", "LINK-USD"]

    # Pre-validation: solo crypto supportate da ClawStreet
    original_count = len(tickers)
    tickers = _filter_to_clawstreet_crypto(tickers)
    filtered_count = original_count - len(tickers)

    if not tickers:
        msg = "Nessun ticker valido (crypto + ClawStreet-supported) → skip Technical Crypto"
        logger.warning("[%s][TECH-CRYPTO] %s", run_id, msg)
        try:
            database.insert_agent_log(run_id, "TECH_CRYPTO", json.dumps({
                "event": "tech_crypto_skipped",
                "reason": "no_valid_tickers",
                "filtered_out": filtered_count,
            }))
        except Exception:
            pass
        return {"analyses": [], "engine": "skipped", "summary": msg}

    logger.info("[%s][TECH-CRYPTO] Analisi su %d ticker: %s",
                run_id, len(tickers), tickers)

    # 1. Recupera indicatori per ogni ticker
    import asyncio
    indicators = await asyncio.gather(*[_fetch_crypto_indicators(t) for t in tickers])
    ticker_data = {t: ind for t, ind in zip(tickers, indicators)
                   if ind and not ind.get("error")}

    if not ticker_data:
        logger.warning("[%s][TECH-CRYPTO] Nessun dato disponibile per i %d ticker",
                       run_id, len(tickers))
        try:
            database.insert_agent_log(run_id, "TECH_CRYPTO", json.dumps({
                "event": "tech_crypto_no_data",
                "tickers_requested": tickers,
            }))
        except Exception:
            pass
        return {"analyses": [], "engine": "no_data",
                "summary": f"Dati non disponibili per {tickers}"}

    # 2. Costruisci contesto e chiama DeepSeek-V3
    context = json.dumps({
        "tickers_data": ticker_data,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "instruction": "Analyze these crypto technical indicators. Output JSON only.",
    }, default=str, ensure_ascii=False)

    try:
        response_text, engine = await _call_deepseek(context[:18000])
    except Exception as exc:
        logger.error("[%s][TECH-CRYPTO] DeepSeek-V3 fallito: %s", run_id, exc)
        try:
            database.insert_agent_log(run_id, "TECH_CRYPTO", json.dumps({
                "event": "tech_crypto_error",
                "error": str(exc)[:300],
            }))
        except Exception:
            pass
        return {"analyses": [], "raw_indicators": ticker_data,
                "engine": "error",
                "summary": f"DeepSeek error: {exc}",
                "filtered_count": filtered_count}

    # 3. Parse JSON
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
    report["filtered_count"] = filtered_count
    report["tickers_analyzed"] = list(ticker_data.keys())

    try:
        database.insert_agent_log(run_id, "TECH_CRYPTO", json.dumps({
            "event": "tech_crypto_complete",
            "engine": engine,
            "tickers_analyzed": len(ticker_data),
            "tickers_requested": original_count,
            "filtered_out": filtered_count,
        }))
    except Exception:
        pass

    return report
