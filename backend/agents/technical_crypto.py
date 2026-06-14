"""
Technical Crypto Agent — DeepSeek-V3, focus ESCLUSIVO crypto 24/7.

Differenze rispetto a technical.py:
  - Universo APERTO: qualsiasi crypto in formato yfinance (-USD) o
    Polygon-compatible (X:TICKER) e' analizzabile. Nessuna whitelist
    hardcoded — il Decision Agent decide i ticker su base liquidita',
    tesi, e disponibilita' dati. DEFAULT_CRYPTO_UNIVERSE sotto e' solo
    una lista di DEFAULT/seed (top-14 liquidita') per i run senza
    Watchdog focus.
  - L'unico vincolo runtime e' la PRESENZA di dati OHLCV (yfinance fetch).
    Ticker senza dati restituiscono error, ticker validi vengono analizzati.
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

# DEFAULT (NON whitelist): top-14 crypto liquidita' usati come lista
# di partenza quando un run non specifica focus_tickers (es. cron orario
# senza watchdog trigger). NON e' un vincolo: qualsiasi ticker -USD
# o X:TICKER puo' essere analizzato passandolo esplicitamente a
# `run_crypto_technical(run_id, tickers=[...])`.
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
        "max_tokens": 6000,   # era 3000 (su richiesta utente): evita tagli sui report crypto
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
    Filtra una lista di ticker tenendo SOLO crypto: suffisso -USD o
    prefisso X:. Nessuna whitelist hardcoded.

    Prima questa funzione validava contro DEFAULT_CRYPTO_UNIVERSE (14
    ticker) e scartava qualsiasi altro. Ora accetta qualsiasi crypto in
    formato standard — la validita' effettiva e' demandata al fetch dati
    (yfinance): ticker senza dati restituiscono error e vengono saltati
    da run_crypto_technical, ma NON sono bloccati pre-emptive.

    Filtri applicati:
      - rimuove duplicati (case-insensitive)
      - rimuove equity / ETF (non hanno -USD ne' X:)
      - normalizza a uppercase
    """
    out = []
    seen: set[str] = set()
    for t in tickers or []:
        if not t:
            continue
        t_up = str(t).upper().strip()
        if not t_up or t_up in seen:
            continue
        is_crypto = t_up.endswith("-USD") or t_up.startswith("X:")
        if is_crypto:
            out.append(t_up)
            seen.add(t_up)
    return out




async def _analyze_crypto_chunk(run_id: str, chunk_data: dict,
                                 crypto_docs_blob: str = "") -> dict:
    """Analizza UN gruppo di ticker con UNA sola chiamata DeepSeek-V3.

    Estratto da run_crypto_technical per il CHUNKING: l'universo crypto
    completo (~20 ticker) non entra in un solo contesto (32K), quindi i
    ticker vengono spezzati in gruppi e ogni gruppo è una chiamata
    indipendente (eseguita in parallelo). Best-effort: un errore DeepSeek
    NON solleva, ritorna engine='error' così gli altri chunk proseguono.

    Ritorna: {analyses, summary, engine, parse_ok, raw_preview, error?}.
    """
    try:
        from agents.technical import _clean_for_json
        context_payload = _clean_for_json({
            "tickers_data": chunk_data,
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
            "tickers_data": chunk_data,
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
        response_text, engine = await _call_deepseek(context[:32000])
    except Exception as exc:
        logger.error("[%s][TECH-CRYPTO] DeepSeek-V3 chunk fallito (%d ticker): %s",
                     run_id, len(chunk_data), exc)
        return {"analyses": [], "summary": "", "engine": "error",
                "parse_ok": False, "error": str(exc)[:300]}

    parse_ok = False
    try:
        json_start = response_text.find("{")
        json_end = response_text.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            rep = json.loads(response_text[json_start:json_end])
            parse_ok = True
        else:
            rep = {}
    except json.JSONDecodeError:
        rep = {}

    return {
        "analyses": rep.get("analyses") or [],
        "summary": rep.get("summary") or "",
        "engine": engine,
        "parse_ok": parse_ok,
        "raw_preview": "" if parse_ok else (response_text[:400] if response_text else ""),
    }


async def run_crypto_technical(run_id: str, tickers: list[str] | None = None,
                                log_phase: str = "TECH_CRYPTO") -> dict:
    """
    Esegue analisi tecnica crypto-only.

    Args:
        run_id: ID del run multi-agente
        tickers: lista ticker da analizzare. Se None, usa l'INTERO universo
                 crypto tradeable (universe.CORE_CRYPTO). I run con molti
                 ticker vengono spezzati in chunk per evitare il troncamento
                 del contesto (vedi _analyze_crypto_chunk).
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

    # Determina universo. Se non specificato, l'INTERO universo crypto
    # tradeable (universe.CORE_CRYPTO): il Technical Crypto copre SEMPRE tutta
    # la "board" su cui si può operare, non un sottoinsieme (richiesta Andrea).
    if not tickers:
        try:
            import universe as _universe
            tickers = list(_universe.CORE_CRYPTO)
        except Exception:
            tickers = list(DEFAULT_CRYPTO_UNIVERSE)

    # Pre-validation: rimuove ticker non-crypto (equity, ETF). Qualsiasi
    # crypto (-USD / X:) e' ACCETTATA, no whitelist.
    original_count = len(tickers)
    tickers = _filter_to_supported_crypto(tickers)
    filtered_count = original_count - len(tickers)

    if not tickers:
        msg = "Nessun ticker crypto valido (richiesti -USD o X:) → skip Technical Crypto"
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

    # 1. Recupera indicatori per ogni ticker CON SEMAFORO.
    # Stesso fix di technical.py: fetch parallelo senza throttle puo' far
    # rate-limitare yfinance/Polygon → tutti i ticker tornano con la stessa
    # risposta cached (identical-data bug).
    import asyncio
    _CC_FETCH_CONC = 4
    _CC_FETCH_DELAY_SEC = 0.15
    _cc_sem = asyncio.Semaphore(_CC_FETCH_CONC)

    async def _bounded_crypto(t):
        async with _cc_sem:
            r = await _fetch_crypto_indicators(t)
            await asyncio.sleep(_CC_FETCH_DELAY_SEC)
            return r

    indicators = await asyncio.gather(*[_bounded_crypto(t) for t in tickers])
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
                # Cap ridotto (era 12K): il blob viene ora PRE-posto ad OGNI
                # chunk, quindi va tenuto compatto per non rubare budget ai dati.
                if chars_so_far + len(snippet) > 6000:
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

    # ─── Analisi in CHUNK ───────────────────────────────────────────────
    # L'universo crypto tradeable completo (~20 ticker) NON entra in una sola
    # chiamata: 20×~3-4K char di dati arricchiti + documenti sforano il budget
    # di contesto (32K) → troncamento e ticker persi. Spezziamo in gruppi
    # piccoli, UNA chiamata DeepSeek-V3 per gruppo IN PARALLELO (bounded), poi
    # uniamo le analisi. Con pochi ticker (chat / side-call) resta un solo
    # chunk = comportamento invariato.
    _CHUNK_SIZE = 5
    _items = list(ticker_data_enriched.items())
    _chunks = [dict(_items[i:i + _CHUNK_SIZE])
               for i in range(0, len(_items), _CHUNK_SIZE)]

    _AN_CONC = 3
    _an_sem = asyncio.Semaphore(_AN_CONC)

    async def _bounded_analyze(_ch):
        async with _an_sem:
            return await _analyze_crypto_chunk(run_id, _ch, crypto_docs_blob)

    chunk_reports = await asyncio.gather(*[_bounded_analyze(c) for c in _chunks])

    all_analyses: list = []
    summaries: list = []
    engines: list = []
    raw_previews: list = []
    chunk_errors: list = []
    parse_ok = True
    for cr in chunk_reports:
        all_analyses.extend(cr.get("analyses") or [])
        if cr.get("summary"):
            summaries.append(cr["summary"])
        if cr.get("engine") and cr["engine"] != "error":
            engines.append(cr["engine"])
        if cr.get("raw_preview"):
            raw_previews.append(cr["raw_preview"])
        if cr.get("error"):
            chunk_errors.append(cr["error"])
        if not cr.get("parse_ok"):
            parse_ok = False

    # Nessuna analisi prodotta E almeno un chunk in errore → report d'errore
    # con gli indicatori grezzi (stesso contratto del vecchio path single-call,
    # così il Decision degrada a do_nothing).
    if not all_analyses and chunk_errors:
        logger.error("[%s][TECH-CRYPTO] tutti i %d chunk falliti: %s",
                     run_id, len(_chunks), chunk_errors[:3])
        try:
            database.insert_agent_log(run_id, log_phase, json.dumps({
                "event": "tech_crypto_error",
                "errors": chunk_errors[:5],
                "chunks": len(_chunks),
            }))
        except Exception:
            pass
        try:
            from agents.technical import _clean_for_json
            cleaned_raw = _clean_for_json(ticker_data)
        except Exception:
            cleaned_raw = ticker_data
        return {
            "analyses": [],
            "raw_indicators": cleaned_raw,
            "engine": "error",
            "summary": f"DeepSeek error su tutti i chunk: {chunk_errors[0]}",
            "filtered_count": filtered_count,
            "data_warning": (
                "TECHNICAL CRYPTO ANALYSIS FAILED. raw_indicators contiene SOLO "
                "indicatori grezzi non interpretati. Per ogni ticker, controlla "
                "data_quality (ok/degraded/insufficient/no_data); se != 'ok' "
                "usa do_nothing motivando 'crypto technical data unavailable'."
            ),
        }

    report = {
        "analyses": all_analyses,
        "engine": engines[0] if engines else "no_data",
        "summary": " | ".join(summaries)[:3000],
        "filtered_count": filtered_count,
        "tickers_analyzed": list(ticker_data.keys()),
    }

    # Log con visibilità degli output del modello (signal/trend/confidence).
    # Cap alzato a 25 (era 6): ora i ticker analizzati sono l'intero universo
    # e la Tech card del frontend deve poterli mostrare tutti.
    analyses_summary = []
    for a in (report.get("analyses") or [])[:25]:
        analyses_summary.append({
            "ticker": a.get("ticker"),
            "signal": a.get("signal"),
            "trend": a.get("trend"),
            "confidence": a.get("confidence"),
        })
    try:
        database.insert_agent_log(run_id, log_phase, json.dumps({
            "event": "tech_crypto_complete",
            "engine": report["engine"],
            "chunks": len(_chunks),
            "tickers_analyzed": len(ticker_data),
            "tickers_with_data": list(ticker_data.keys()),
            "tickers_requested": original_count,
            "filtered_out": filtered_count,
            "chunk_errors": chunk_errors[:5],
            "json_parsed": parse_ok,
            "analyses_summary": analyses_summary,
            # Cap 300 → 2500: i summary tecnici crypto (BTC.D, funding,
            # leader/laggard altcoin) raramente entrano in 300 char.
            "summary_text": (report.get("summary") or "")[:2500],
            "raw_preview": (raw_previews[0] if raw_previews else ""),
        }, default=str))
    except Exception:
        pass

    return report
