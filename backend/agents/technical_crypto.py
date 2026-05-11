"""
Technical Crypto Agent — DeepSeek-V3, focus ESCLUSIVO crypto 24/7.

Differenze rispetto a technical.py:
  - Universo HARD-LIMITED ai 14 ticker crypto supportati
    (BTC-USD, ETH-USD, SOL-USD, DOGE-USD, AVAX-USD, ADA-USD, XRP-USD,
    LTC-USD, DOT-USD, LINK-USD, UNI-USD, ATOM-USD, MATIC-USD, NEAR-USD)
  - Ogni ticker richiesto viene validato contro questa lista PRIMA di
    chiamare DeepSeek; ticker non-crypto / non-supportati → skip
  - Prompt specializzato su tecnica crypto: volumi 24h, funding rate,
    on-chain flow, wallet activity, sentiment retail (X/Reddit), non gap
    di apertura, alta sensibilità a sentiment regulatorio.

Costo per run: ~$0.0006 (DeepSeek-V3, prompt minimal).
Schedule: ogni 1 ora, 24/7, indipendente da market hours.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import aiohttp

logger = logging.getLogger(__name__)

DEEPSEEK_MODEL = "deepseek-chat"   # V3
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"

# Hard-limit dei 14 ticker crypto supportati. Default per i run senza
# Watchdog focus. La validazione runtime usa la stessa lista.
DEFAULT_CRYPTO_UNIVERSE = [
    "BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD", "AVAX-USD",
    "ADA-USD", "XRP-USD", "LTC-USD", "DOT-USD", "LINK-USD",
    "UNI-USD", "ATOM-USD", "MATIC-USD", "NEAR-USD",
]

CRYPTO_TECHNICAL_PROMPT_DEFAULT = """Sei un Technical Analyst ASSERTIVO specializzato ESCLUSIVAMENTE in crypto-asset top-cap (BTC, ETH, SOL, ecc.).

Ricevi DATI ARRICCHITI per ogni ticker:
- indicatori base (RSI, MACD, ATR, Bollinger) + signals_summary aggregato
- candlestick_patterns: pattern rilevati nelle ultime 5 candele (hammer, engulfing, doji, ecc.)
- fibonacci: livelli 0.236/0.382/0.5/0.618/0.786 + zona corrente + nearest S/R
- volume_profile: POC (point of control), HVN (zone S/R forti), LVN (transit)
- market_structure: HH/HL trend strutturale + BoS / CHoCH (rotture chiave)
- multitimeframe: confluence 1h/4h/1d (trend allineato → segnale forte)
- derivatives: funding rate + open interest Binance (sentiment leverage)

Analisi richiesta per ogni ticker:
1. Trend strutturale: USA market_structure.structure (UPTREND/DOWNTREND/CONTRACTING/...)
2. Livelli chiave: COMBINA fibonacci.nearest_support/resistance + volume_profile.hvn + market_structure swing levels
3. Momentum: RSI 14, MACD, volume 24h
4. CONFLUENCE: pattern candlestick + livello Fibonacci + HVN nelle vicinanze = SETUP AD ALTA CONFIDENCE
5. MULTI-TIMEFRAME: se confluence == BULLISH (2/3 timeframe) e segnale 1h è bullish → BUY conf alto
6. SENTIMENT DERIVATIVES:
   - funding_signal == bullish_overcrowded → cautela su BUY (rischio long squeeze)
   - funding_signal == bearish_overcrowded → favorire BUY (rischio short squeeze al rialzo)
7. Anomalie crypto:
   - Volume spike >2× rispetto alla media 7d
   - Movimento >3% in 1h o >7% in 24h
   - Test di livelli psicologici (BTC 100k, ETH 5k)
   - BoS/CHoCH appena formato (rottura strutturale)
8. Risk note: stop-loss usando ATR + livello Fibonacci immediatamente sotto/sopra

═══════════════════════════════════════════════════════════════════════
DECISION POLICY — NO CONSERVATIVE BIAS
═══════════════════════════════════════════════════════════════════════

Counting rule:
- 4+ bullish signals → BUY conf 65-85 (le crypto sono volatili: confidence
  capped a 85 anche con setup ottimo, perche' news puo' invalidare in minuti)
- 3 bullish + 1-2 neutral → BUY conf 55-68
- 4+ bearish signals → SELL conf 65-85
- 3 bearish + 1-2 neutral → SELL conf 55-68
- Mix (2-2 o 3-3) → HOLD conf 40-55
- Trend STRUTTURALE forte + volume spike + livello chiave rotto → BUY/SELL conf 75-88

CONFIDENCE FLOORS:
- BUY/SELL: MAI sotto 50. Se non puoi dare ≥50, dichiara HOLD ma con conf 40-55.
- HOLD: range 35-58. Sotto 35 NON è accettabile — segnala "data_insufficient".
- VIETATO 35% piatto su tutti i ticker.

═══════════════════════════════════════════════════════════════════════

Le crypto sono 24/7 — sentiment puo' cambiare in pochi minuti per:
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
      "structure": "UPTREND|DOWNTREND|CONTRACTING|EXPANDING|MIXED",
      "rsi_14": 58.3,
      "support": 95000,
      "resistance": 102000,
      "fib_zone": "between_0.618_and_0.786",
      "candlestick_setup": "bullish_hammer at 0.618 fib + HVN nearby",
      "mtf_confluence": "BULLISH|BEARISH|MIXED",
      "funding_bias": "bullish_overcrowded|bearish_overcrowded|neutral",
      "stop_loss_pct": 3.5,
      "signal": "BUY|SELL|HOLD",
      "confidence": 0-100,
      "reasoning": "Confluence: <pattern> + <fib level> + <volume zone> + <mtf> + <funding>. Conta segnali (X bullish vs Y bearish)."
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


def _build_df_from_market_data(market_data: dict):
    """Converte l'output di data_fetchers.fetch_market_data in DataFrame OHLCV
    nel formato richiesto da technical_advanced (colonne capitalized)."""
    import pandas as pd
    if not market_data or not market_data.get("data"):
        return None
    df = pd.DataFrame(market_data["data"])
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        df.set_index("date", inplace=True)
    df.columns = [c.capitalize() for c in df.columns]
    # Verifica che tutte le colonne richieste siano presenti
    required = {"Open", "High", "Low", "Close", "Volume"}
    if not required.issubset(set(df.columns)):
        return None
    return df


async def _fetch_crypto_indicators(ticker: str) -> dict:
    """
    Recupera indicatori tecnici crypto. Riusa la stessa logica di technical.py
    (_fetch_ticker_indicators) + arricchisce con candlestick patterns,
    Fibonacci, volume profile, market structure, multi-timeframe e derivatives
    Binance.

    Periodo 90 giorni (default del technical normale) per consistency.
    """
    try:
        from agents.technical import _fetch_ticker_indicators
        data = await _fetch_ticker_indicators(ticker, period_days=90)
        if not (data and isinstance(data, dict)):
            logger.warning("[TECH-CRYPTO] %s _fetch_ticker_indicators returned None/invalid", ticker)
            return {"ticker": ticker, "error": "fetch returned None"}
        if not data.get("current_price"):
            err = data.get("error", "no current_price")
            logger.warning("[TECH-CRYPTO] %s no data: %s (source=%s)",
                           ticker, err, data.get("source", "?"))
            return {"ticker": ticker, "error": err, "source": data.get("source", "?")}

        # ─── Advanced enrichment (candlestick + fib + volume + MTF + derivatives) ──
        try:
            import data_fetchers as _df_mod
            from agents.technical_advanced import enrich_ticker_advanced
            # Usa la cache: chiamata successiva è in-memory hit (5 min TTL)
            md = await asyncio.get_running_loop().run_in_executor(
                None, _df_mod.fetch_market_data, ticker, 90
            )
            df = _build_df_from_market_data(md)
            if df is not None and len(df) >= 20:
                advanced = await enrich_ticker_advanced(
                    ticker, df,
                    include_multitf=True,
                    include_derivatives=True,
                    intervals=["1h", "4h", "1d"],  # crypto-specific
                )
                data["advanced"] = advanced
            else:
                logger.debug("[TECH-CRYPTO] %s no DF for advanced enrichment", ticker)
                data["advanced"] = {"error": "df not available"}
        except Exception as e:
            logger.warning("[TECH-CRYPTO] %s advanced enrichment failed: %s", ticker, e)
            data["advanced"] = {"error": str(e)[:120]}

        return data
    except Exception as exc:
        logger.error("[TECH-CRYPTO] %s exception: %s", ticker, exc, exc_info=True)
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
        # Temperature 0.4 (era 0.2): meno conservativo, piu' espressivita'
        # nelle confidence senza perdere consistency sui segnali.
        "temperature": 0.4,
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


def _filter_to_supported_crypto(tickers: list[str]) -> list[str]:
    """
    Filtra una lista di ticker tenendo solo le crypto (suffisso -USD o
    prefisso X:). Validazione contro l'universo hard-coded
    DEFAULT_CRYPTO_UNIVERSE per evitare ticker inventati dal LLM.
    """
    valid = set(DEFAULT_CRYPTO_UNIVERSE)
    out = []
    for t in tickers:
        t_up = t.upper()
        is_crypto = t_up.endswith("-USD") or t_up.startswith("X:")
        if is_crypto and t_up in valid:
            out.append(t_up)
    return out




async def run_crypto_technical(run_id: str, tickers: list[str] | None = None,
                                log_phase: str = "TECH_CRYPTO") -> dict:
    """
    Esegue analisi tecnica crypto-only.

    Args:
        run_id: ID del run multi-agente
        tickers: lista ticker da analizzare. Se None, usa DEFAULT_CRYPTO_UNIVERSE
                 ridotto a 6 ticker top liquidità per contenere costi.
        log_phase: phase con cui loggare gli eventi su agent_logs.
                   Default "TECH_CRYPTO" (chiamata da crypto pipeline).
                   Quando chiamato come side-call dal Decision Standard
                   (es. portfolio ha BTC), il caller passa
                   "TECH_CRYPTO_SIDECALL" per non inquinare la timeline
                   del run Standard con eventi che sembrano crypto-pipeline.

    Returns:
        dict con campi: analyses[], engine, summary, filtered_count
    """
    import database

    # Determina universo. Se non specificato, top 6 crypto liquidità.
    if not tickers:
        tickers = ["BTC-USD", "ETH-USD", "SOL-USD",
                   "DOGE-USD", "AVAX-USD", "LINK-USD"]

    # Pre-validation: solo crypto supportate (universo hard-coded a 14 ticker)
    original_count = len(tickers)
    tickers = _filter_to_supported_crypto(tickers)
    filtered_count = original_count - len(tickers)

    if not tickers:
        msg = "Nessun ticker valido (crypto supportato) → skip Technical Crypto"
        logger.warning("[%s][TECH-CRYPTO] %s", run_id, msg)
        try:
            database.insert_agent_log(run_id, log_phase, json.dumps({
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
        # Log dettagliato del PRIMO fail per ogni ticker così capiamo il
        # root cause (rate limit yfinance / ticker non riconosciuto / ecc.)
        per_ticker_errors = []
        for t, ind in zip(tickers, indicators):
            if isinstance(ind, dict):
                per_ticker_errors.append({
                    "ticker": t,
                    "error": (ind.get("error") or "unknown")[:200],
                    "source": ind.get("source", "?"),
                })
            else:
                per_ticker_errors.append({"ticker": t, "error": f"non-dict: {type(ind).__name__}"})
        logger.warning("[%s][TECH-CRYPTO] Nessun dato disponibile per i %d ticker. Errori: %s",
                       run_id, len(tickers), per_ticker_errors)
        try:
            database.insert_agent_log(run_id, log_phase, json.dumps({
                "event": "tech_crypto_no_data",
                "tickers_requested": tickers,
                "per_ticker_errors": per_ticker_errors,
            }, default=str))
        except Exception:
            pass
        return {"analyses": [], "engine": "no_data",
                "summary": f"Dati non disponibili per {tickers}",
                "per_ticker_errors": per_ticker_errors}

    # 2. Carica documenti crypto (preset + upload utente) — il Technical
    #    Crypto può sfruttarli come framework di riferimento per pattern,
    #    on-chain, ecc. Tronchiamo a 12K char totali per non saturare il
    #    context (V3 ha window ridotta vs R1).
    crypto_docs_blob = ""
    try:
        docs = database.get_document_contents(category="crypto") or []
        if docs:
            doc_lines = ["DOCUMENTI CRYPTO DI RIFERIMENTO (estratti):"]
            chars_so_far = 0
            for d in docs:
                fname = d.get("filename", "?")
                content = (d.get("content") or "")[:1400]
                snippet = f"--- {fname} ---\n{content}"
                if chars_so_far + len(snippet) > 12000:
                    doc_lines.append("[...restanti documenti omessi per limite context]")
                    break
                doc_lines.append(snippet)
                chars_so_far += len(snippet)
            crypto_docs_blob = "\n\n".join(doc_lines)
    except Exception as e:
        logger.debug("[%s][TECH-CRYPTO] Doc loading failed: %s", run_id, e)

    # Pre-processing: aggiungi signals_summary aggregato (riusa l'helper
    # del Technical standard — stessa struttura analyze_ticker)
    try:
        from agents.technical import _enrich_ticker_data
        # ticker_data e' un dict {ticker: data}; _enrich vuole una lista
        enriched_list = _enrich_ticker_data(list(ticker_data.values()))
        ticker_data_enriched = {
            (d.get("ticker") or k): d
            for k, d in zip(ticker_data.keys(), enriched_list)
        }
    except Exception as e:
        logger.debug("[%s][TECH-CRYPTO] enrich failed: %s", run_id, e)
        ticker_data_enriched = ticker_data

    # Scrub NaN/Inf prima della serializzazione: il fix evita che json.dumps
    # con default=str li converta in stringhe "nan" che il modello scambia
    # per dato valido. Riusiamo l'helper di technical standard.
    try:
        from agents.technical import _clean_for_json
        context_payload = _clean_for_json({
            "tickers_data": ticker_data_enriched,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "instruction": (
                "Per ogni crypto leggi PRIMA signals_summary (bullish/bearish counts), "
                "POI calibra confidence. NON usare 35% piatto come fallback: rispetta i floor. "
                "Se signals_summary.data_quality='insufficient', NON inventare una "
                "direzione: ritorna signal='HOLD' con reasoning='data_insufficient'."
            ),
        })
    except Exception:
        context_payload = {
            "tickers_data": ticker_data_enriched,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "instruction": (
                "Per ogni crypto leggi PRIMA signals_summary (bullish/bearish counts), "
                "POI calibra confidence. NON usare 35% piatto come fallback: rispetta i floor."
            ),
        }
    context = json.dumps(context_payload, default=str, ensure_ascii=False)
    if crypto_docs_blob:
        context = crypto_docs_blob + "\n\n" + "=" * 60 + "\n\n" + context

    try:
        # Limite alzato a 32K perche' i dati advanced (candle/fib/MTF/deriv)
        # aggiungono ~2K char per ticker. DeepSeek-V3 ha window 64K input quindi
        # 32K ci sta abbondantemente.
        response_text, engine = await _call_deepseek(context[:32000])
    except Exception as exc:
        logger.error("[%s][TECH-CRYPTO] DeepSeek-V3 fallito: %s", run_id, exc)
        try:
            database.insert_agent_log(run_id, log_phase, json.dumps({
                "event": "tech_crypto_error",
                "error": str(exc)[:300],
            }))
        except Exception:
            pass
        # Scrub anche raw_indicators del fallback
        try:
            from agents.technical import _clean_for_json
            cleaned_raw = _clean_for_json(ticker_data)
        except Exception:
            cleaned_raw = ticker_data
        return {
            "analyses": [],
            "raw_indicators": cleaned_raw,
            "engine": "error",
            "summary": f"DeepSeek error: {exc}",
            "filtered_count": filtered_count,
            "data_warning": (
                "TECHNICAL CRYPTO ANALYSIS FAILED. raw_indicators contiene SOLO "
                "indicatori grezzi non interpretati. Per ogni ticker, controlla "
                "data_quality (ok/degraded/insufficient/no_data); se != 'ok' "
                "usa do_nothing motivando 'crypto technical data unavailable'."
            ),
        }

    # 3. Parse JSON
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
    report["filtered_count"] = filtered_count
    report["tickers_analyzed"] = list(ticker_data.keys())

    # Log con visibilità degli output del modello (signal/trend/confidence)
    analyses_summary = []
    for a in (report.get("analyses") or [])[:6]:
        analyses_summary.append({
            "ticker": a.get("ticker"),
            "signal": a.get("signal"),
            "trend": a.get("trend"),
            "confidence": a.get("confidence"),
        })
    try:
        database.insert_agent_log(run_id, log_phase, json.dumps({
            "event": "tech_crypto_complete",
            "engine": engine,
            "tickers_analyzed": len(ticker_data),
            "tickers_with_data": list(ticker_data.keys()),
            "tickers_requested": original_count,
            "filtered_out": filtered_count,
            "json_parsed": parse_ok,
            "analyses_summary": analyses_summary,
            "summary_text": (report.get("summary") or "")[:300],
            "raw_preview": "" if parse_ok else (response_text[:400] if response_text else ""),
        }, default=str))
    except Exception:
        pass

    return report
