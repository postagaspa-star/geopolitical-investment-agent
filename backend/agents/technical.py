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

TECH_PROMPT_DEFAULT = """You are an ASSERTIVE quantitative technical analyst. You receive
raw OHLCV data, pre-calculated indicators, AND a pre-interpreted "signals_summary"
that already classifies each indicator as bullish/bearish/neutral.

═══════════════════════════════════════════════════════════════════════
DECISION POLICY — DO NOT BE CONSERVATIVE BY DEFAULT
═══════════════════════════════════════════════════════════════════════

Your job is to AGGREGATE the pre-interpreted signals into a clear directional call.
Counting rule (use this as floor, not ceiling):

- 4+ bullish signals (out of: SMA, RSI, MACD, Stochastic, Momentum, Volume) → BUY conf 70-85
- 3 bullish + 1-2 neutral → BUY conf 60-72
- 4+ bearish signals → SELL conf 70-85
- 3 bearish + 1-2 neutral → SELL conf 60-72
- Mix di bullish e bearish (es. 2-2 o 3-3) → HOLD conf 45-55
- Trend molto forte (TRENDING_UP) + 2-3 conferme → BUY conf 75-90
- Pattern raro (golden cross, oversold bounce con RSI<30) → BUY conf 80-90

CONFIDENCE FLOORS (rispettare rigorosamente):
- BUY/SELL: confidence MAI sotto 50. Se non puoi dare ≥50 di confidence,
  preferisci HOLD ma con conf 45-55 (NON 35).
- HOLD: confidence range valido 35-60. Sotto 35 NON è accettabile —
  segnala invece "data_insufficient" nel reasoning.
- Vietato 35% piatto su tutti i ticker: se lo fai, stai sottoperformando.

INSTRUCTIONS:
- Leggi PRIMA il campo signals_summary (gia' interpretato), POI integra con i numeri raw
  per calibrare la confidence
- Calcola support/resistance dai livelli forniti in support_resistance
- Suggerisci stop-loss usando ATR (typical: SL = current - 1.5×ATR per BUY)
- Flag divergences (es. prezzo SMA200, RSI < 50 → bearish divergence)
- Market regime determinato dal trend pre-calcolato

═══════════════════════════════════════════════════════════════════════
OUTPUT — JSON valido (no preamble, solo JSON):
═══════════════════════════════════════════════════════════════════════
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
      "stoch": {"value": 22.5, "signal": "OVERSOLD"},
      "sma_cross": {"signal": "GOLDEN_CROSS"},
      "volume_trend": "HIGH",
      "trend": "TRENDING_UP",
      "suggested_stop_loss": 102.30,
      "reasoning": "4 segnali bullish (RSI oversold bounce, MACD bullish cross, golden cross SMA, volume HIGH) + trend up → BUY conf 72."
    }
  ],
  "market_regime": "RANGING",
  "summary": "2 BUY (XOM, NVDA), 1 SELL (MSFT), 1 HOLD (GLD). Tech sector mostra rotazione positiva."
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
        # Temperature 0.4 (era 0.2): piu' espressivita' nelle confidence
        # senza perdere consistency sui segnali. 0.2 era troppo conservativa
        # e produceva HOLD/35% sistematici.
        "temperature": 0.4,
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


def _is_bad_number(x) -> bool:
    """True se il valore e' NaN, Inf, None, o non numerico."""
    if x is None:
        return True
    try:
        import math
        if isinstance(x, (int, float)):
            return math.isnan(x) or math.isinf(x)
    except Exception:
        return True
    return False


def _clean_for_json(obj):
    """
    Walk ricorsivo che sostituisce NaN/Inf/numpy NaN con None.
    Risolve il bug per cui json.dumps(..., default=str) trasformava NaN in
    "nan" (stringa) — il modello la scambiava per dato valido.
    """
    import math
    # numpy types: NaN/Inf check senza importare numpy esplicitamente
    if obj is None:
        return None
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, (int, str, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: _clean_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean_for_json(v) for v in obj]
    # Tipi numpy: forza float-like check
    try:
        v = float(obj)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    except (TypeError, ValueError):
        pass
    return obj


def _validate_indicators(ticker_data: dict) -> tuple[dict, list]:
    """
    Sanity-check sui dati indicatori. Ritorna (cleaned_data, warnings).

    Controlli:
      - prezzo corrente > 0
      - RSI in [0, 100], altrimenti None
      - ATR >= 0
      - support < current_price (altrimenti potrebbe essere swappato)
      - resistance > current_price
      - rimuove NaN/Inf da tutti i campi numerici
    """
    warnings: list[str] = []
    if not isinstance(ticker_data, dict):
        return ticker_data, ["non-dict ticker_data"]

    ticker = ticker_data.get("ticker", "?")
    cp = ticker_data.get("current_price")
    if _is_bad_number(cp) or (isinstance(cp, (int, float)) and cp <= 0):
        warnings.append(f"current_price invalido ({cp})")
        ticker_data["current_price"] = None

    atr = ticker_data.get("atr")
    if _is_bad_number(atr) or (isinstance(atr, (int, float)) and atr < 0):
        warnings.append(f"ATR invalido ({atr})")
        ticker_data["atr"] = None

    analysis = ticker_data.get("analysis") or {}
    indicators = analysis.get("indicators") or {}

    # RSI: range valido [0, 100]
    rsi = indicators.get("rsi_14")
    if rsi is not None:
        if _is_bad_number(rsi) or not (0 <= float(rsi) <= 100):
            warnings.append(f"RSI fuori range ({rsi})")
            indicators["rsi_14"] = None

    # MACD histogram: niente NaN
    for key in ("macd_histogram", "macd_signal", "macd_value"):
        v = indicators.get(key)
        if _is_bad_number(v):
            indicators[key] = None
            if v is not None:
                warnings.append(f"{key} NaN/Inf")

    # Support/Resistance: rispetto al prezzo corrente, segnaliamo se swappati
    sr = analysis.get("support_resistance") or {}
    sup = sr.get("support")
    res = sr.get("resistance")
    cur = ticker_data.get("current_price")
    if cur and isinstance(cur, (int, float)) and cur > 0:
        if isinstance(sup, (int, float)) and sup > cur * 1.5:
            warnings.append(f"support {sup} > current {cur}*1.5 (sospetto)")
            sr["support"] = None
        if isinstance(res, (int, float)) and res < cur * 0.7:
            warnings.append(f"resistance {res} < current {cur}*0.7 (sospetto)")
            sr["resistance"] = None
    # NaN check su S/R
    if _is_bad_number(sr.get("support")):
        sr["support"] = None
    if _is_bad_number(sr.get("resistance")):
        sr["resistance"] = None

    # Data points: < 20 = troppi pochi per indicatori affidabili
    dp = ticker_data.get("data_points") or 0
    if dp < 20:
        warnings.append(f"only {dp} data points (< 20) — indicators unreliable")

    # Aggiungi flag esplicito di qualita' dati
    if warnings:
        ticker_data["data_quality"] = "degraded"
        ticker_data["data_warnings"] = warnings[:6]
    else:
        ticker_data["data_quality"] = "ok"

    return ticker_data, warnings


async def _fetch_ticker_indicators(ticker: str, period_days: int = 90) -> dict:
    """
    Recupera dati OHLCV e calcola indicatori tecnici per un ticker.
    Usa yfinance con fallback ClawStreet (via data_fetchers).
    Output validato con sanity check + scrubbed di NaN/Inf.
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
            "data_quality": "no_data",
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

    raw = {
        "ticker": ticker,
        "current_price": current_price,
        "atr": atr,
        "analysis": analysis,
        "data_points": len(market_data["data"]),
        "source": market_data.get("source", "yfinance"),
    }

    # Sanity validation: scrub NaN/Inf, range checks, S/R sanity
    cleaned = _clean_for_json(raw)
    cleaned, warnings = _validate_indicators(cleaned)
    if warnings:
        logger.warning("[TECH] %s data_quality=degraded: %s", ticker, warnings[:3])
    return cleaned


def _build_signals_summary(ticker_data: dict) -> dict:
    """
    Estrae i signals interpretati da analyze_ticker (technical_analysis.py)
    e li riassume come "bullish/bearish/neutral counts" + lista chiara dei
    setup attivi. Cosi' il LLM vede subito il quadro aggregato e non deve
    indovinare partendo dai numeri raw.

    Se i dati sono insufficienti (no analysis, no signals) ritorna esplicito
    `data_quality: "insufficient"` invece di mascherarsi come trend UNKNOWN
    + zero segnali (che il modello scambiava per HOLD legittimo).

    Returns:
        {
          "bullish_count": int, "bearish_count": int, "neutral_count": int,
          "bullish_signals": [str], "bearish_signals": [str],
          "trend": str, "key_observations": [str],
          "data_quality": "ok" | "insufficient",
          "aggregated_bias": "BULLISH" | "BEARISH" | "MIXED" | "INSUFFICIENT_DATA",
        }
    """
    out = {
        "bullish_count": 0, "bearish_count": 0, "neutral_count": 0,
        "bullish_signals": [], "bearish_signals": [],
        "trend": ticker_data.get("analysis", {}).get("trend", "UNKNOWN"),
        "key_observations": [],
        "data_quality": "ok",
    }
    analysis = ticker_data.get("analysis") or {}
    signals = analysis.get("signals") or {}
    indicators = analysis.get("indicators") or {}

    # ── Detect early data insufficiency ─────────────────────────────────────
    # Se i 3 campi essenziali mancano → flag esplicito che il LLM legge.
    # Cosi' il modello sa che NON deve produrre BUY/SELL/HOLD su questi dati.
    has_signals = bool(signals)
    has_indicators = bool(indicators)
    rsi_present = indicators.get("rsi_14") is not None
    macd_present = (indicators.get("macd_value") is not None
                    or indicators.get("macd_histogram") is not None)
    if not (has_signals and has_indicators and (rsi_present or macd_present)):
        out["data_quality"] = "insufficient"
        out["aggregated_bias"] = "INSUFFICIENT_DATA"
        out["key_observations"].append(
            "DATA QUALITY: insufficient — non producing directional call. "
            "Skip ticker or use do_nothing."
        )
        # Riporta anche eventuali warnings dal validator
        warns = ticker_data.get("data_warnings") or []
        if warns:
            out["data_warnings"] = warns
        return out

    # Mappa parole-chiave → bullish/bearish (sia inglese che italiano,
    # perche' technical_analysis.py potrebbe ritornare l'una o l'altra)
    bullish_kw = ["BUY", "BULLISH", "GOLDEN", "OVERSOLD", "POSITIVE", "ACQUISTO",
                  "RIALZISTA", "BULLISH_CROSS", "OVERSOLD_BUY"]
    bearish_kw = ["SELL", "BEARISH", "DEATH", "OVERBOUGHT", "NEGATIVE", "VENDITA",
                  "RIBASSISTA", "BEARISH_CROSS", "OVERBOUGHT_SELL"]

    def _classify(sig_value: str) -> str:
        s = (sig_value or "").upper()
        if any(kw in s for kw in bullish_kw):
            return "bullish"
        if any(kw in s for kw in bearish_kw):
            return "bearish"
        return "neutral"

    for indicator_name, sig in signals.items():
        # sig puo' essere una stringa direttamente o un dict {signal, value, ...}
        if isinstance(sig, dict):
            sig_str = sig.get("signal") or sig.get("interpretazione") or ""
        else:
            sig_str = str(sig)

        cat = _classify(sig_str)
        label = f"{indicator_name.upper()}: {sig_str}"
        if cat == "bullish":
            out["bullish_count"] += 1
            out["bullish_signals"].append(label)
        elif cat == "bearish":
            out["bearish_count"] += 1
            out["bearish_signals"].append(label)
        else:
            out["neutral_count"] += 1

    # Key observations dal trend e indicatori chiave
    trend = out["trend"]
    if trend in ("TRENDING_UP", "RIALZISTA"):
        out["key_observations"].append("Trend STRUTTURALE rialzista (SMA20 > SMA50 > SMA200)")
    elif trend in ("TRENDING_DOWN", "RIBASSISTA"):
        out["key_observations"].append("Trend STRUTTURALE ribassista")

    rsi = indicators.get("rsi_14")
    if rsi is not None:
        if rsi < 30:
            out["key_observations"].append(f"RSI {rsi:.0f} → OVERSOLD estremo (potenziale bounce)")
        elif rsi > 70:
            out["key_observations"].append(f"RSI {rsi:.0f} → OVERBOUGHT (potenziale pullback)")
        elif 50 <= rsi <= 65:
            out["key_observations"].append(f"RSI {rsi:.0f} → momentum positivo in trend")

    macd_h = indicators.get("macd_histogram")
    if macd_h is not None:
        if macd_h > 0:
            out["key_observations"].append("MACD histogram positivo (espansione bullish)")
        else:
            out["key_observations"].append("MACD histogram negativo (espansione bearish)")

    # Verdict aggregato (suggerisce all'LLM la direzione)
    if out["bullish_count"] >= out["bearish_count"] + 2:
        out["aggregated_bias"] = "BULLISH"
    elif out["bearish_count"] >= out["bullish_count"] + 2:
        out["aggregated_bias"] = "BEARISH"
    else:
        out["aggregated_bias"] = "MIXED"

    return out


def _enrich_ticker_data(ticker_data_list: list) -> list:
    """Aggiunge signals_summary a ogni ticker_data prima di mandarlo al LLM."""
    enriched = []
    for td in ticker_data_list:
        if not isinstance(td, dict):
            enriched.append(td)
            continue
        td_copy = dict(td)
        try:
            td_copy["signals_summary"] = _build_signals_summary(td)
        except Exception as e:
            logger.debug("[TECH] _build_signals_summary failed for %s: %s",
                         td.get("ticker"), e)
        enriched.append(td_copy)
    return enriched


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

    # Pre-validation ClawStreet: il Technical normale è dedicato ai mercati
    # tradizionali (equity + ETF commodity GLD/SLV/USO). Le crypto sono dominio
    # esclusivo del Technical Crypto. Filtra fuori le crypto e i ticker non
    # ClawStreet-supported per non sprecare token.
    try:
        from clawstreet_universe import is_supported
        original = list(tickers)
        # Rimuovi crypto (dominio del Technical Crypto)
        tickers = [t for t in tickers if not (t.endswith("-USD") or t.startswith("X:"))]
        # Tieni solo ticker supportati su ClawStreet
        tickers = [t for t in tickers if is_supported(t)]
        skipped = [t for t in original if t not in tickers]
        if skipped:
            logger.info("[%s][TECH] Skipped %d ticker non-equity/non-CS: %s",
                        run_id, len(skipped), skipped)
    except ImportError:
        pass  # degrade graceful
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

    # 2. Pre-processing: aggiungi signals_summary aggregato per ogni ticker
    # Cosi' il LLM vede subito quanti segnali bullish/bearish ci sono e non
    # deve indovinare partendo dai numeri raw → meno bias HOLD/35%.
    ticker_data_enriched = _enrich_ticker_data(ticker_data)

    # 3. Prepara contesto per DeepSeek — scrub NaN/Inf prima della serializzazione
    # (altrimenti json.dumps con default=str li converte in stringa "nan"
    # che il modello scambia per dato valido).
    payload = _clean_for_json({
        "tickers_data": ticker_data_enriched,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "instruction": (
            "Per ogni ticker leggi PRIMA signals_summary (gia' aggregato), "
            "POI calibra confidence integrando indicatori raw. NON usare "
            "confidence 35% piatto come fallback: rispetta i floor del prompt. "
            "Se signals_summary.data_quality='insufficient', NON inventare "
            "una direzione: ritorna signal='HOLD' con reasoning='data_insufficient'."
        ),
    })
    context = json.dumps(payload, default=str, ensure_ascii=False)

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
        try:
            database.insert_agent_log(run_id, "TECH_WORKER", json.dumps({
                "event": "technical_pure_macro_fallback",
                "error": str(ds_err)[:200],
                "tickers": [t.get("ticker") for t in ticker_data if isinstance(t, dict)],
            }))
        except Exception:
            pass
        # Scrub NaN/Inf prima di tornarli al Decision Agent.
        cleaned_raw = _clean_for_json(ticker_data)
        return {
            "analyses": [],
            "raw_indicators": cleaned_raw,
            "engine": "pure_macro_fallback",
            "error": f"DeepSeek failed: {ds_err}",
            "data_warning": (
                "TECHNICAL ANALYSIS FAILED. raw_indicators contiene SOLO numeri "
                "grezzi (RSI, ATR, support/resistance) NON interpretati da un LLM. "
                "Ogni ticker ha un campo 'data_quality' = ok|degraded|insufficient. "
                "Per i ticker con data_quality != 'ok', usa do_nothing motivando "
                "'technical data unavailable'. NON inventare confidence."
            ),
            "summary": (
                "Technical DeepSeek-V3 NON disponibile — il Decision Agent deve "
                "operare in Pure Macro mode. Vedi data_warning + raw_indicators."
            ),
        }

    # 4. Parse risultato
    parse_ok = False
    try:
        json_start = response_text.find("{")
        json_end = response_text.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            report = json.loads(response_text[json_start:json_end])
            parse_ok = True
        else:
            report = {"analyses": [], "raw_analysis": response_text}
    except json.JSONDecodeError:
        report = {"analyses": [], "raw_analysis": response_text}

    report["engine"] = engine
    report["raw_indicators"] = ticker_data

    # 5. Log con visibilità del contenuto effettivo
    analyses_summary = []
    for a in (report.get("analyses") or [])[:6]:
        analyses_summary.append({
            "ticker": a.get("ticker"),
            "signal": a.get("signal"),
            "trend": a.get("trend"),
            "confidence": a.get("confidence"),
        })
    database.insert_agent_log(run_id, "TECH_WORKER",
        json.dumps({
            "event": "technical_analysis_complete",
            "engine": engine,
            "tickers_analyzed": len(report.get("analyses", [])),
            "tickers_requested": len(tickers),
            "json_parsed": parse_ok,
            "analyses_summary": analyses_summary,
            "summary_text": (report.get("summary") or "")[:300],
            # Se parse failed, mostra raw response per debug
            "raw_preview": "" if parse_ok else (response_text[:400] if response_text else ""),
        }, default=str))

    logger.info("[%s][TECH] Analisi completata via %s: %d ticker",
                run_id, engine, len(report.get("analyses", [])))
    return report
