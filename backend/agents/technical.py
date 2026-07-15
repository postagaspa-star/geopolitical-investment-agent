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
FALLBACK_MODEL = "claude-sonnet-4-5"   # era sonnet-4-20250514 (deprecato/in ritiro)

TECH_PROMPT_DEFAULT = """You are an ASSERTIVE quantitative technical analyst. You receive
raw OHLCV data, pre-calculated indicators, AND a pre-interpreted "signals_summary"
that already classifies each indicator as bullish/bearish/neutral.

For each ticker you ALSO receive an "advanced" block with:
- candlestick_patterns: pattern detected in last 5 candles (hammer, engulfing, doji, ...)
- fibonacci: levels 0.236 → 1.618 + current_zone + nearest_support/resistance
- volume_profile: POC (point of control), HVN (strong S/R nodes), LVN (transit)
- market_structure: UPTREND/DOWNTREND/CONTRACTING/EXPANDING + BoS / CHoCH detection
- multitimeframe: 15m / 1h / 1d / 1wk summary + confluence (BULLISH/BEARISH/MIXED)

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

CONFLUENCE BOOST (advanced data):
- Pattern candlestick BULLISH + prezzo a livello Fibonacci di supporto +
  HVN nelle vicinanze + multitimeframe.confluence == BULLISH → BUY conf 80-92
- BoS bullish appena formato + market_structure UPTREND → conferma BUY ad alto conf
- CHoCH detection → segnale di POSSIBILE INVERSIONE: usa con cautela ma nota in reasoning
- Multi-TF mismatch (1d bullish, 1h bearish) → preferisci HOLD o BUY conf medio (55-65),
  perche' segnali contrastanti riducono la convinzione

CONFIDENCE FLOORS (rispettare rigorosamente):
- BUY/SELL: confidence MAI sotto 50. Se non puoi dare ≥50 di confidence,
  preferisci HOLD ma con conf 45-55 (NON 35).
- HOLD: confidence range valido 35-60. Sotto 35 NON è accettabile —
  segnala invece "data_insufficient" nel reasoning.
- Vietato 35% piatto su tutti i ticker: se lo fai, stai sottoperformando.

INSTRUCTIONS:
- Leggi PRIMA signals_summary (gia' interpretato), POI integra con advanced
  (fib + volume + structure + MTF) per calibrare la confidence
- Calcola support/resistance combinando fibonacci.nearest_support/resistance + volume_profile.hvn
- Suggerisci stop-loss usando MAX(ATR×1.5, livello fib immediatamente sotto/sopra)
- Flag divergences (es. prezzo > SMA200, RSI < 50 → bearish divergence)
- Market regime determinato dal market_structure.structure (preferito) o dal trend pre-calcolato

═══════════════════════════════════════════════════════════════════════
CRITICAL — ANTI-HALLUCINATION RULE (NON-NEGOTIABLE):
═══════════════════════════════════════════════════════════════════════
Tutti i valori NUMERICI (current_price, support, resistance, atr,
rsi.value, macd.value, stoch.value, suggested_stop_loss) DEVONO essere
calcolati o copiati dai dati di INPUT del SINGOLO ticker. Ogni ticker
ha numeri propri.

VIETATO:
- Riusare gli stessi numeri tra ticker diversi.
- Inventare valori (es. RSI=35.2, price=105.50) se non corrispondono
  ai dati di INPUT del ticker in oggetto.
- Copiare lo schema sotto come se fosse un esempio "da imitare": e' uno
  SCHEMA, non un campione.

Se la tua risposta produce numeri identici tra ticker diversi (es.
stesso current_price o stesso RSI), verra' RIFIUTATA come hallucinated
e il sistema la sovrascrivera' a HOLD con safety override.

═══════════════════════════════════════════════════════════════════════
OUTPUT — JSON valido (no preamble, solo JSON). SCHEMA (NON un esempio):
═══════════════════════════════════════════════════════════════════════
{
  "analyses": [
    {
      "ticker": "<simbolo esatto dall'INPUT>",
      "signal": "<BUY | SELL | HOLD>",
      "confidence": <numero 50-90 per BUY/SELL, 35-60 per HOLD>,
      "current_price": <copia ESATTA dall'input di QUESTO ticker>,
      "support": <livello derivato dai dati di QUESTO ticker>,
      "resistance": <livello derivato dai dati di QUESTO ticker>,
      "atr": <copia dall'input di QUESTO ticker>,
      "rsi": {"value": <numero da input>, "signal": "<OVERSOLD_BUY|OVERBOUGHT_SELL|NEUTRAL|...>"},
      "macd": {"value": <numero da input>, "signal": "<BULLISH_CROSS|BEARISH_CROSS|NEUTRAL>"},
      "stoch": {"value": <numero>, "signal": "<OVERSOLD|OVERBOUGHT|NEUTRAL>"},
      "sma_cross": {"signal": "<GOLDEN_CROSS|DEATH_CROSS|NONE>"},
      "volume_trend": "<HIGH | LOW | NORMAL>",
      "trend": "<TRENDING_UP | TRENDING_DOWN | RANGING | UNKNOWN>",
      "structure": "<UPTREND | DOWNTREND | CONTRACTING | EXPANDING>",
      "fib_zone": "<etichetta descrittiva specifica di QUESTO ticker>",
      "candlestick_setup": "<pattern reale o 'none'>",
      "mtf_confluence": "<BULLISH | BEARISH | MIXED>",
      "suggested_stop_loss": <livello derivato per QUESTO ticker>,
      "reasoning": "<sintesi causale che DEVE citare i numeri specifici di QUESTO ticker: RSI X, MACD Y, prezzo Z. Se due 'reasoning' di ticker diversi sono identici la risposta verra' rifiutata.>"
    }
  ],
  "market_regime": "<RANGING | BULL | BEAR | VOLATILE>",
  "summary": "<N BUY (tickers), M SELL (tickers), K HOLD (tickers); +1 riga di contesto>"
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
        "max_tokens": 6000,   # era 4096 (su richiesta utente): evita tagli sui report tecnici
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
            max_tokens=6000,   # era 4096 (su richiesta utente)
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


async def _fetch_ticker_indicators(ticker: str, period_days: int = 365) -> dict:
    """
    Recupera dati OHLCV e calcola indicatori tecnici per un ticker.
    Usa yfinance (via data_fetchers).
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

    # ── DEFENSIVE: ordine cronologico ascending PRIMA di tutto ──────────────
    # current_price = data[-1], _calculate_atr e analyze_ticker assumono che la
    # serie sia oldest→newest. Se un provider restituisse newest-first (o un
    # reverse saltasse), avremmo: current_price = barra PIU' VECCHIA, e RSI/MACD
    # calcolati all'indietro → un titolo bullish letto come oversold (bug
    # osservato su un run standard). Le date sono ISO "YYYY-MM-DD" → ordinamento
    # lessicografico == cronologico. Riordina solo se tutte le righe hanno data.
    _data = market_data["data"]
    if _data and all(r.get("date") for r in _data):
        _data = sorted(_data, key=lambda r: str(r.get("date")))
        market_data["data"] = _data

    # Calcola indicatori
    df = pd.DataFrame(market_data["data"])
    df.set_index("date", inplace=True)
    df.columns = [c.capitalize() for c in df.columns]
    df.sort_index(inplace=True)  # ridondante ma esplicito (belt-and-suspenders)

    analysis = technical_analysis.analyze_ticker(df)

    # Aggiungi prezzo corrente e ATR (ora data[-1] e' garantito il piu' recente)
    current_price = market_data["data"][-1]["close"] if market_data["data"] else 0

    # Calcola ATR manualmente (14 periodi)
    atr = _calculate_atr(market_data["data"])

    # Freschezza OHLCV: una RSI calcolata su barre vecchie di giorni puo'
    # contraddire il prezzo live (provider free con ritardo o copertura scarsa
    # del simbolo). Esponi data/eta' dell'ultima barra cosi' il valore non viene
    # scambiato per "corrente" (l'altra causa del mismatch indicatore↔prezzo).
    last_bar_date = market_data["data"][-1].get("date") if market_data["data"] else None
    data_age_days = None
    if last_bar_date:
        try:
            from datetime import datetime as _dt, timezone as _tz
            _d = _dt.strptime(str(last_bar_date)[:10], "%Y-%m-%d").replace(tzinfo=_tz.utc)
            data_age_days = (_dt.now(_tz.utc) - _d).days
        except Exception:
            data_age_days = None

    raw = {
        "ticker": ticker,
        "current_price": current_price,
        "atr": atr,
        "analysis": analysis,
        "data_points": len(market_data["data"]),
        "source": market_data.get("source", "yfinance"),
        "last_bar_date": last_bar_date,
        "data_age_days": data_age_days,
    }

    # Sanity validation: scrub NaN/Inf, range checks, S/R sanity
    cleaned = _clean_for_json(raw)
    cleaned, warnings = _validate_indicators(cleaned)
    if warnings:
        logger.warning("[TECH] %s data_quality=degraded: %s", ticker, warnings[:3])

    # Staleness OHLCV: >5 giorni su timeframe daily e' sospetto (weekend = 2-3g).
    # Non blocca, ma rende VISIBILE che RSI/indicatori potrebbero non riflettere
    # il prezzo corrente — cosi' un mismatch "RSI oversold ma prezzo bullish"
    # non passa piu' in silenzio (lo vede sia il log sia l'LLM nel report).
    if isinstance(data_age_days, int) and data_age_days > 5:
        logger.warning("[TECH] %s OHLCV STALE: ultima barra %s (%dg fa, src=%s)",
                       ticker, last_bar_date, data_age_days,
                       market_data.get("source", "?"))
        cleaned.setdefault("data_warnings", []).append(
            f"OHLCV ultima barra {last_bar_date} ({data_age_days}g fa): "
            "RSI/indicatori potrebbero NON riflettere il prezzo corrente"
        )

    # ─── Advanced enrichment per equity ─────────────────────────────────────
    # Aggiunge candlestick patterns, Fibonacci, volume profile, market
    # structure e multi-timeframe (15m/1h/1d/1wk). NO derivatives Binance
    # (non applicabile a equity). Solo se data_quality e' ok.
    if cleaned.get("data_quality") == "ok" and df is not None and len(df) >= 20:
        try:
            from agents.technical_advanced import enrich_ticker_advanced
            advanced = await enrich_ticker_advanced(
                ticker, df,
                include_multitf=True,
                include_derivatives=False,
                intervals=["15m", "1h", "1d", "1wk"],
            )
            cleaned["advanced"] = advanced
        except Exception as e:
            logger.debug("[TECH] %s advanced enrichment failed: %s", ticker, e)
            cleaned["advanced"] = {"error": str(e)[:120]}

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

    # Il Technical normale è dedicato ai mercati tradizionali (equity + ETF).
    # Le crypto sono dominio esclusivo del Technical Crypto: filtra crypto
    # in modo che non sprechino token sull'engine equity.
    original = list(tickers)
    tickers = [t for t in tickers if not (t.endswith("-USD") or t.startswith("X:"))]
    skipped = [t for t in original if t not in tickers]
    if skipped:
        logger.info("[%s][TECH] Skipped %d ticker crypto (delegati a Technical Crypto): %s",
                    run_id, len(skipped), skipped)
    # Limita a 10 tickers
    tickers = tickers[:10]

    # 1. Recupera indicatori in parallelo CON SEMAFORO.
    # Fix causa-radice del "stessi dati per ticker diversi": yfinance/Polygon
    # condividono stato HTTP tra thread quando vengono colpiti in parallelo
    # (10 ticker simultanei → rate-limit silenzioso → ritornano l'ultimo
    # bar cached, identico per tutti). Stesso pattern di rotation_scan.py.
    _FETCH_CONC = 4   # piu' restrittivo di rotation_scan (6) per essere safe
    _FETCH_DELAY_SEC = 0.15
    _sem = asyncio.Semaphore(_FETCH_CONC)

    async def _bounded(t):
        async with _sem:
            r = await _fetch_ticker_indicators(t)
            await asyncio.sleep(_FETCH_DELAY_SEC)
            return r

    tasks = [_bounded(t) for t in tickers]
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

    # ── SAFETY NET: clone detection cross-ticker ────────────────────────────
    # Se due ticker hanno (current_price, atr, n_data_points) IDENTICI fino
    # al 4° decimale, e' praticamente certo che siano un clone (es. yfinance
    # silent rate-limit che restituisce SPY per tutti). Marca i secondi come
    # clone e li escludi dal contesto LLM — evita di mandare al modello
    # numeri identici per 5 ticker diversi che lo confondono.
    fingerprints: dict = {}
    clone_tickers: list[str] = []
    clean_data = []
    for td in ticker_data:
        if not isinstance(td, dict):
            clean_data.append(td)
            continue
        try:
            cp = round(float(td.get("current_price") or 0), 4)
            atr = round(float(td.get("atr") or 0), 4)
            n = int(td.get("data_points") or 0)
        except (TypeError, ValueError):
            clean_data.append(td)
            continue
        fp = (cp, atr, n)
        if cp > 0 and fp in fingerprints:
            clone_tickers.append({
                "ticker": td.get("ticker"),
                "cloned_from": fingerprints[fp],
                "fingerprint": {"current_price": cp, "atr": atr, "n_data_points": n},
            })
            # Marca esplicitamente e NON inviare al LLM (evita contaminazione)
            td["data_quality"] = "clone_suspected"
            td["clone_of"] = fingerprints[fp]
            continue
        if cp > 0:
            fingerprints[fp] = td.get("ticker")
        clean_data.append(td)
    if clone_tickers:
        logger.error(
            "[%s][TECH] CLONE DETECTED — %d ticker hanno dati identici a un altro: %s",
            run_id, len(clone_tickers), clone_tickers,
        )
        try:
            database.insert_agent_log(run_id, "TECH_CLONE_DETECTED", json.dumps({
                "event": "ticker_data_clone_detected",
                "clones": clone_tickers,
                "primary_tickers": list(fingerprints.values()),
            }, default=str))
        except Exception:
            pass
    ticker_data = clean_data

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
        # Limite alzato a 40K perche' i dati advanced (candle/fib/volume/MTF)
        # aggiungono ~2K char per ticker e qui possono esserci fino a 10 ticker.
        # DeepSeek-V3 ha window 64K input quindi 40K e' ok.
        response_text, engine = await _call_deepseek(context[:40000])
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

    # 5a. ENFORCEMENT data_quality: anche se il modello ha hallucinato un
    # BUY/SELL su ticker con data_quality=insufficient, lo sovrascriviamo
    # forzatamente a HOLD + reasoning="data_insufficient".
    # Questo previene "halluccinated buys" che il prompt da solo non
    # garantiva (il modello a volte ignora le istruzioni).
    qmap = {td.get("ticker"): td.get("data_quality") for td in ticker_data
            if isinstance(td, dict) and td.get("ticker")}
    forced = []
    for a in (report.get("analyses") or []):
        tk = a.get("ticker")
        dq = qmap.get(tk)
        if dq == "insufficient" and a.get("signal") in ("BUY", "SELL"):
            forced.append({
                "ticker": tk,
                "original_signal": a.get("signal"),
                "original_confidence": a.get("confidence"),
            })
            a["signal"] = "HOLD"
            a["confidence"] = 35
            a["reasoning"] = (
                f"data_insufficient (forced HOLD): il modello aveva proposto "
                f"{forced[-1]['original_signal']} confidence "
                f"{forced[-1]['original_confidence']} ma data_quality e' "
                f"insufficient — direzione sovrascritta dal sistema."
            )

    # 5b. SAFETY FALLBACK — HALLUCINATION DETECTION.
    # DeepSeek-V3 a temperatura bassa con esempio in prompt a volte
    # rigurgita gli stessi numeri tra ticker diversi (current_price RSI
    # MACD identici per asset diversi). Bug osservato: "il technical
    # restituisce gli stessi identici dati per tutti i ticker".
    #
    # Difesa in profondita': per ogni analysis confronta current_price
    # con il GROUND-TRUTH dei raw_indicators (yfinance/Polygon, per
    # ticker). Se devia oltre tolleranza → output hallucinated → HOLD
    # forzato + reasoning chiaro. Cosi' anche se il prompt-fix non
    # bastasse, l'agente Decision NON riceve dati inventati su cui
    # potrebbe operare.
    truth_price = {}
    for td in ticker_data:
        if isinstance(td, dict) and td.get("ticker"):
            cp = td.get("current_price")
            if isinstance(cp, (int, float)) and cp > 0:
                truth_price[td["ticker"]] = float(cp)

    # Inoltre: rileva DUPLICATI di current_price tra ticker diversi
    # (segnale chiarissimo di rigurgito).
    seen_prices: dict[float, str] = {}  # price → primo ticker che l'ha usato
    hallucinated_tickers: list[str] = []

    for a in (report.get("analyses") or []):
        tk = a.get("ticker")
        try:
            cp_llm = float(a.get("current_price") or 0)
        except (TypeError, ValueError):
            cp_llm = 0.0

        cp_truth = truth_price.get(tk)
        hallucinated_reason = None

        # Check 1: il prezzo del LLM diverge dal ground-truth oltre l'1%.
        if cp_truth and cp_llm > 0:
            diff_pct = abs(cp_llm - cp_truth) / cp_truth * 100
            if diff_pct > 1.0:
                hallucinated_reason = (
                    f"current_price del LLM ({cp_llm}) non corrisponde al "
                    f"ground-truth ({cp_truth}) per {tk} (diff {diff_pct:.1f}%)")

        # Check 2: stesso current_price gia' usato da un altro ticker.
        if cp_llm > 0 and not hallucinated_reason:
            rounded = round(cp_llm, 2)
            prev = seen_prices.get(rounded)
            if prev and prev != tk:
                hallucinated_reason = (
                    f"current_price {cp_llm} identico a quello di {prev}: "
                    "DeepSeek sta rigurgitando, non analizzando")
            else:
                seen_prices[rounded] = tk

        if hallucinated_reason:
            hallucinated_tickers.append(tk)
            a["signal"] = "HOLD"
            a["confidence"] = 35
            a["data_quality"] = "hallucinated"
            # current_price corretto al ground-truth se disponibile (per
            # non lasciare un valore inventato in giro nei log).
            if cp_truth:
                a["current_price"] = cp_truth
            a["reasoning"] = (
                f"SAFETY OVERRIDE — hallucination rilevata: "
                f"{hallucinated_reason}. Direzione forzata a HOLD. "
                "Il Decision Agent NON deve trattare questo segnale come "
                "tecnico valido.")

    if hallucinated_tickers:
        logger.error(
            "[%s][TECH] HALLUCINATION rilevata su %d ticker, forzati a HOLD: %s",
            run_id, len(hallucinated_tickers), hallucinated_tickers)

    # 5. Log con visibilità del contenuto effettivo
    analyses_summary = []
    for a in (report.get("analyses") or [])[:6]:
        analyses_summary.append({
            "ticker": a.get("ticker"),
            "signal": a.get("signal"),
            "trend": a.get("trend"),
            "confidence": a.get("confidence"),
        })
    if forced:
        logger.warning("[%s][TECH] Forzati a HOLD %d ticker con data_quality=insufficient: %s",
                       run_id, len(forced), forced)
    if hallucinated_tickers:
        report["hallucination_safety_override"] = {
            "tickers_overridden": hallucinated_tickers,
            "count": len(hallucinated_tickers),
            "note": ("DeepSeek-V3 ha prodotto valori non coerenti con i dati "
                     "di input. I ticker elencati sono stati forzati a HOLD "
                     "con data_quality=hallucinated. Il Decision Agent NON "
                     "deve operare su questi segnali."),
        }
    database.insert_agent_log(run_id, "TECH_WORKER",
        json.dumps({
            "event": "technical_analysis_complete",
            "engine": engine,
            "tickers_analyzed": len(report.get("analyses", [])),
            "tickers_requested": len(tickers),
            "json_parsed": parse_ok,
            "analyses_summary": analyses_summary,
            "tickers_hallucinated": hallucinated_tickers,
            # Cap generoso (6000): i summary tecnici (regime, ticker leader,
            # trend) non devono essere troncati nella Tech card del frontend.
            "summary_text": (report.get("summary") or "")[:6000],
            # Se parse failed, mostra raw response per debug
            "raw_preview": "" if parse_ok else (response_text[:400] if response_text else ""),
        }, default=str))

    logger.info("[%s][TECH] Analisi completata via %s: %d ticker",
                run_id, engine, len(report.get("analyses", [])))
    return report
