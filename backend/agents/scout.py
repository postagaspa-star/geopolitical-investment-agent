"""
Scout Agent — DeepSeek-V3 (primary) + Claude Sonnet (fallback)
Intelligence gathering 24/7 con compattazione temporale a cascata.

Architettura a 4 livelli (tutti su intelligence_buffer):
  - L0  Micro-cards (ogni 20 min, 24/7): ~9 fonti → micro-schede JSON
  - L1  Report 8h: legge L0 della finestra, sintetizza, ELIMINA L0 consumati
  - L2  Report 4d: legge L1 della finestra, sintetizza, ELIMINA L1 consumati
  - L3  Report 3w: legge L2 della finestra, sintetizza, ELIMINA L2 consumati

I report L1/L2/L3 sono salvati con source_type AGG_8H / AGG_4D / AGG_3W.
Il Decision Agent legge: ultimo AGG_3W + ultimi 2 AGG_4D + ultimi 3 AGG_8H +
buffer L0 ultimi 40 min. Lo Scout 20-min stesso riceve come contesto macro
gli ultimi report aggregati.

Modello: DeepSeek-V3 (~$0.27/M token) per ridurre i costi rispetto a Sonnet 4.5.
Fallback automatico a Claude Sonnet se DeepSeek non risponde.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone, timedelta

import aiohttp

logger = logging.getLogger(__name__)

# DeepSeek-V3 — modello primario per lo Scout (JSON extraction, molto più economico di Sonnet)
DEEPSEEK_MODEL = "deepseek-chat"
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"

# Claude Sonnet — fallback se DeepSeek non risponde
SCOUT_MODEL_FALLBACK = "claude-sonnet-4-20250514"

# ============================================================
# Source-type taxonomy
# ============================================================

# Micro-cards di livello L0 (prodotte da run_scout_20min ogni 20 min).
# Include sia i nomi canonici interni sia gli alias usati nei prompt utente
# (es. ROLE.txt usa YFINANCE/TWITTER mentre il codice usa YFINANCE_NEWS/X).
# La consistenza è essenziale altrimenti il cascade aggregator esclude i
# record dei prompt utente.
MICRO_CARD_TYPES = [
    "GDELT",
    "NEWSAPI",
    "YFINANCE_NEWS", "YFINANCE",          # alias yfinance news
    "REDDIT",
    "X", "TWITTER",                        # alias twitter/X
    "CONGRESSIONAL",
    "CRYPTO_MARKET",
    "SYSTEM",                              # error/diagnostic cards (vedi ROLE.txt § ERROR HANDLING)
]

# Tier aggregati: source_type identificativi
TIER_8H = "AGG_8H"
TIER_4D = "AGG_4D"
TIER_3W = "AGG_3W"

# ============================================================
# Prompt Templates
# ============================================================

SCOUT_20MIN_PROMPT_DEFAULT = """Sei uno Scout Agent specializzato in intelligence geopolitica e di mercato.
Copri SIA mercati equity (azioni/ETF) SIA crypto (BTC, ETH, SOL e altcoin top 100).
Le crypto sono 24/7 e particolarmente sensibili a sentiment retail e regolamentazione.

Ricevi dati grezzi da multiple fonti:
  - GDELT (eventi geopolitici globali, regolamentazione crypto, sanzioni)
  - NEWSAPI (news mainstream, anche crypto-specifiche)
  - YFINANCE_NEWS (news per ticker watchlist, equity + crypto BTC-USD/ETH-USD/...)
  - REDDIT (sentiment retail: wallstreetbets/stocks/investing/options + CryptoCurrency/Bitcoin/ethtrader/CryptoMarkets)
  - X (sentiment finance Twitter, anche crypto twitter)
  - CONGRESSIONAL (insider trades USA)
  - COINGECKO (dati crypto: top 25 coin con prezzo/volume/change_1h/24h/7d, BTC dominance,
    Fear&Greed Index, trending coins del giorno. Usa questi dati per identificare
    pump/dump crypto e generare schede CRYPTO_MARKET con sentiment basato sui movimenti.)

IMPORTANTE: Quando vedi sentiment Reddit dai subreddit crypto, marca sentiment_type="RETAIL"
e usa key_tickers in formato yfinance: BTC-USD, ETH-USD, SOL-USD, ecc.

REGOLE FONDAMENTALI:
1. Per OGNI fonte che ha ALMENO 1 articolo/post/trade non vuoto, DEVI generare almeno
   1 micro-scheda. Se la fonte ha 5+ articoli interessanti puoi generarne fino a 3.
2. Se una fonte e' VUOTA o ha solo errori, NON generare scheda per quella fonte.
3. Anche se i contenuti sono "noise" produci comunque una micro-scheda di sintesi
   (es. "Reddit: discussioni su FOMC senza catalizzatori specifici, sentiment neutro").
4. NESSUN preambolo o testo extra. RISPONDI SOLO CON IL JSON ARRAY.

OUTPUT (SOLO JSON, nessun altro testo):
[
  {
    "source_type": "GDELT|NEWSAPI|YFINANCE_NEWS|REDDIT|X|CONGRESSIONAL|CRYPTO_MARKET",
    "micro_summary": "Sintesi azionabile in 2-3 frasi (max 280 char)",
    "sentiment_score": -1.0 to 1.0,
    "sentiment_type": "INSTITUTIONAL|RETAIL",
    "key_tickers": ["XOM", "LMT"],
    "risk_keywords": ["conflict", "sanctions"]
  }
]"""

REPORT_8H_PROMPT = """Sei lo Scout Agent. Devi produrre un REPORT AGGREGATO 8H sintetizzando
tutte le micro-schede degli ultimi ~480 minuti (equity + crypto, mercati 24/7).

REGOLE:
1. Tono compatto, denso, azionabile. Niente paragrafi lunghi.
2. Identifica i temi che HANNO DOMINATO la finestra (5-7 al massimo).
3. Estrai i tickers caldi (azionari + crypto, formato yfinance).
4. Stima il macro_bias del periodo (BULLISH/BEARISH/NEUTRAL).
5. Indica catalisti probabili nelle prossime 8 ore (eventi attesi, livelli tecnici).
6. RISPONDI SOLO CON JSON, nessun preambolo.

OUTPUT JSON:
{
  "tier": "8H",
  "macro_bias": "BULLISH|BEARISH|NEUTRAL",
  "summary_text": "Sintesi 4-6 frasi del periodo, integrando equity e crypto",
  "key_events": [{"event": "...", "impact": "...", "tickers": [...]}],
  "hot_tickers": ["NVDA", "BTC-USD", ...],
  "sentiment_shift": "IMPROVING|WORSENING|STABLE",
  "next_8h_catalysts": ["FOMC speech 14:00", "BTC test resistenza 75k", ...]
}"""

REPORT_4D_PROMPT = """Sei lo Scout Agent. Devi produrre un REPORT AGGREGATO 4 GIORNI
partendo dai report 8h degli ultimi 4 giorni (~12 report).

REGOLE:
1. Visione di MEDIO TERMINE: cosa sta consolidando vs cosa è solo rumore.
2. Identifica i trend settoriali in atto e le rotazioni.
3. Lista i rischi che si stanno accumulando.
4. Suggerisci una strategia di posizionamento per i prossimi 4 giorni.
5. RISPONDI SOLO CON JSON.

OUTPUT JSON:
{
  "tier": "4D",
  "macro_bias": "BULLISH|BEARISH|NEUTRAL",
  "summary_text": "Visione 4 giorni: trend consolidati e dinamiche settoriali",
  "consolidating_trends": ["..."],
  "noise_to_ignore": ["..."],
  "sector_rotation": {"energy": "OVERWEIGHT", "tech": "UNDERWEIGHT", "crypto": "NEUTRAL"},
  "accumulating_risks": ["..."],
  "next_4d_strategy": "Posizionamento suggerito per i prossimi 4 giorni"
}"""

REPORT_3W_PROMPT = """Sei lo Scout Agent. Devi produrre un REPORT MACRO 3 SETTIMANE
partendo dai report 4d degli ultimi 21 giorni (~5-6 report).

REGOLE:
1. Visione di LUNGO TERMINE: regime di mercato, trend strutturali, rischi sistemici.
2. Identifica il regime corrente (RISK_ON / RISK_OFF / TRANSITION / VOLATILE).
3. Indica i cambi di regime osservati nell'arco delle 3 settimane.
4. Lista rischi strutturali (geopolitici, regolamentari, macro).
5. Suggerisci una strategia macro complessiva.
6. RISPONDI SOLO CON JSON.

OUTPUT JSON:
{
  "tier": "3W",
  "regime": "RISK_ON|RISK_OFF|TRANSITION|VOLATILE",
  "synthesis": "Visione macro 3 settimane: regime, struttura, narrative dominante",
  "regime_shifts": ["es. da RISK_OFF a TRANSITION attorno al 10/04 per Fed pivot"],
  "structural_risks": ["..."],
  "long_term_themes": ["AI capex", "crypto institutional adoption", ...],
  "sector_rotation": {"energy": "OVERWEIGHT", ...},
  "macro_strategy": "Strategia complessiva per le prossime 3 settimane"
}"""


def _get_scout_prompt() -> str:
    """
    Carica il system prompt dello Scout.
    Override utente: chiave 'prompt_scout' nelle impostazioni DB.
    Fallback: SCOUT_20MIN_PROMPT_DEFAULT.
    """
    try:
        import database as _db
        custom = _db.get_setting("prompt_scout", "")
        if custom and isinstance(custom, str) and custom.strip():
            return custom
    except Exception:
        pass
    return SCOUT_20MIN_PROMPT_DEFAULT


def _get_deepseek_key() -> str:
    """Legge DEEPSEEK_API_KEY da env."""
    return os.environ.get("DEEPSEEK_API_KEY", "")


async def _call_deepseek(system_prompt: str, user_content: str, max_retries: int = 3) -> tuple[str, str]:
    """
    Chiama DeepSeek-V3 con retry esponenziale.
    Ritorna (response_text, engine_used).
    Lancia eccezione se tutti i retry falliscono.
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
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
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
                        logger.warning("[SCOUT] DeepSeek 429, retry in %ds...", wait)
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


async def _call_claude_fallback(system_prompt: str, user_content: str) -> tuple[str, str]:
    """
    Fallback async: usa Claude Sonnet se DeepSeek non risponde.
    Wrappa la chiamata SDK sincrona in asyncio.to_thread per non bloccare
    l'event loop (era un bug serio: una chiamata Sonnet fermava polling,
    watchdog e tutti gli altri agenti per 5-30 secondi).
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
            model=SCOUT_MODEL_FALLBACK,
            max_tokens=4096,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )

    response = await asyncio.to_thread(_sync_call)
    text = ""
    for block in response.content:
        if hasattr(block, "text"):
            text += block.text
    return text, f"claude-sonnet-fallback ({SCOUT_MODEL_FALLBACK})"


# ============================================================
# Task 20-min: Continuous Intelligence Stream
# ============================================================

async def run_scout_20min(run_id: str) -> list[dict]:
    """
    Interroga le API, filtra il rumore con DeepSeek-V3 (fallback: Claude Sonnet),
    e scrive le micro-schede nell'intelligence_buffer su Supabase.

    Returns:
        Lista di micro-schede prodotte.
    """
    import data_fetchers
    import database

    logger.info("[%s][SCOUT] Avvio raccolta intelligence 20-min...", run_id)

    # Salva checkpoint
    _save_checkpoint(run_id, "scout", "RUNNING", {"phase": "data_collection"})

    # 1. Raccogli dati in parallelo da TUTTE le fonti
    source_names = [
        "GDELT", "NEWSAPI", "YFINANCE_NEWS",
        "REDDIT", "X",
        "CONGRESSIONAL",
        "COINGECKO",
    ]
    tasks = [
        data_fetchers.fetch_gdelt_data(),
        data_fetchers.fetch_newsapi_data(),
        data_fetchers.fetch_yfinance_news(),
        data_fetchers.fetch_reddit_sentiment(),
        data_fetchers.fetch_x_sentiment(),
        data_fetchers.fetch_congressional_trades(),
        data_fetchers.fetch_coingecko_data(),
    ]

    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    # 2. Prepara contesto per DeepSeek + summary per logging visibile
    context_parts = []
    sources_summary = {}  # per agent_logs frontend
    for i, result in enumerate(raw_results):
        source = source_names[i] if i < len(source_names) else f"SOURCE_{i}"
        if isinstance(result, Exception):
            context_parts.append(f"[{source}] ERROR: {result}")
            sources_summary[source] = {"status": "error", "error": str(result)[:120]}
        else:
            # Calcola count items per il frontend
            try:
                if isinstance(result, dict):
                    count = (
                        len(result.get("items", []))
                        or len(result.get("articles", []))
                        or len(result.get("posts", []))
                        or len(result.get("tweets", []))
                        or len(result.get("trades", []))
                        or len(result.get("data", []))
                    )
                else:
                    count = len(result) if hasattr(result, "__len__") else 0
            except Exception:
                count = 0
            err = result.get("error") if isinstance(result, dict) else None
            sources_summary[source] = {
                "status": "ok" if not err else "warn",
                "count": count,
                "error": str(err)[:120] if err else None,
            }
            # Truncate per non superare il context window
            data_str = json.dumps(result, default=str, ensure_ascii=False)
            if len(data_str) > 4000:
                data_str = data_str[:4000] + "...(truncated)"
            context_parts.append(f"[{source}]\n{data_str}")

    full_context = "\n\n".join(context_parts)

    # 2a-bis. CONTESTO MACRO: inietta gli ultimi report aggregati (3W + 4D + 8H)
    # in modo che lo Scout 20-min sappia in che regime/trend ci troviamo
    # quando classifica le micro-schede.
    try:
        macro_ctx = _build_macro_context_for_scout(database)
        if macro_ctx:
            full_context = macro_ctx + "\n\n" + full_context
    except Exception as _e:
        logger.warning("[%s][SCOUT] Impossibile iniettare contesto macro: %s", run_id, _e)

    # 2b. Logga SUBITO il riepilogo fonti — visibile nel frontend anche se Sonnet fallisce.
    try:
        database.insert_agent_log(run_id, "SCOUT", json.dumps({
            "event": "scout_sources_fetched",
            "sources": sources_summary,
            "total_sources": len(source_names),
        }, default=str))
    except Exception:
        pass

    # 3. Chiama DeepSeek-V3 per filtrare e sintetizzare (fallback: Claude Sonnet)
    _save_checkpoint(run_id, "scout", "RUNNING", {"phase": "analysis"})

    used_model = DEEPSEEK_MODEL
    response_text = ""
    scout_prompt = _get_scout_prompt()
    user_msg = f"Analizza questi dati e produci le micro-schede JSON:\n\n{full_context[:20000]}"

    try:
        try:
            response_text, used_model = await _call_deepseek(scout_prompt, user_msg)
        except Exception as ds_err:
            logger.warning("[%s][SCOUT] DeepSeek non disponibile (%s), fallback a Claude Sonnet", run_id, ds_err)
            response_text, used_model = await _call_claude_fallback(scout_prompt, user_msg)

        # Parse JSON robusto: prova array, poi cerca blocchi ```json```, poi estrae oggetti singoli
        micro_cards = _parse_json_array_robust(response_text)

        # Se 0 schede ma response_text non vuoto -> logga raw per diagnostica
        if not micro_cards and response_text.strip():
            logger.warning("[%s][SCOUT] 0 schede da response non vuota (model=%s). Raw: %s",
                           run_id, used_model, response_text[:500])
            try:
                database.insert_agent_log(run_id, "SCOUT_PARSE_FAIL", json.dumps({
                    "event": "json_parse_failed",
                    "model": used_model,
                    "raw_response": response_text[:1500],
                    "context_length": len(full_context),
                }, default=str))
            except Exception:
                pass

    except Exception as e:
        logger.error("[%s][SCOUT] Errore analisi: %s", run_id, e, exc_info=True)
        micro_cards = []
        try:
            database.insert_agent_log(run_id, "SCOUT_ERROR", json.dumps({
                "event": "scout_analysis_error",
                "error": str(e)[:500],
                "model_attempted": used_model,
            }))
        except Exception:
            pass

    # 4. Deduplicazione SEMANTICA (Jaccard sui token significativi).
    #    Lo Scout girando ogni 20 min su input mostly-statici tendeva a
    #    produrre summary ripetuti → spreco di storage + rumore per il
    #    Decision Agent. Ora usiamo Jaccard >= 0.55 sullo stesso source_type:
    #    riconosce paraphrase ovvi, non solo match esatti.
    #    Soglia 4h: oltre questa finestra anche un summary simile è
    #    informativo (= "il tema persiste nel ciclo successivo").
    recent_signatures = _load_recent_card_signatures(database, hours=4)
    written = 0
    skipped_duplicate = 0
    dedup_log_samples: list[dict] = []   # per diagnostica frontend (max 5)
    for card in micro_cards:
        try:
            source_type = card.get("source_type", "UNKNOWN")
            summary = card.get("micro_summary", "")

            is_dup, matched = _is_card_duplicate(
                source_type, summary, recent_signatures,
                threshold=DEDUP_JACCARD_THRESHOLD,
            )
            if is_dup:
                skipped_duplicate += 1
                if len(dedup_log_samples) < 5 and matched:
                    dedup_log_samples.append({
                        "skipped_summary": summary[:120],
                        "matched_summary": (matched.get("summary") or "")[:120],
                        "similarity": round(matched.get("similarity", 1.0), 3),
                        "source_type": source_type,
                    })
                continue

            _write_intelligence_buffer(
                database=database,
                run_id=run_id,
                source_type=source_type,
                raw_content=json.dumps(card, default=str),
                micro_summary=summary,
                sentiment_score=float(card.get("sentiment_score", 0)),
            )
            # Aggiungi alla collezione per dedup intra-run
            new_tokens = _tokenize_for_dedup(summary)
            if new_tokens:
                recent_signatures.append({
                    "source_type": source_type,
                    "summary": summary,
                    "tokens": new_tokens,
                    "fp": _fingerprint_card(source_type, summary),
                })
            written += 1
        except Exception as e:
            logger.warning("[%s][SCOUT] Errore scrittura buffer: %s", run_id, e)

    if skipped_duplicate > 0:
        logger.info("[%s][SCOUT] Dedup semantica: skippate %d card simili "
                    "(Jaccard >= %.2f, finestra 4h)",
                    run_id, skipped_duplicate, DEDUP_JACCARD_THRESHOLD)
        try:
            database.insert_agent_log(run_id, "SCOUT_DEDUP", json.dumps({
                "event": "scout_dedup_summary",
                "skipped_duplicates": skipped_duplicate,
                "threshold": DEDUP_JACCARD_THRESHOLD,
                "samples": dedup_log_samples,
            }, default=str))
        except Exception:
            pass

    # 5. Log finale — SEMPRE scritto (anche se 0 schede), cosi' il frontend
    # mostra ogni esecuzione dello Scout con count fonti e numero schede.
    database.insert_agent_log(
        run_id=run_id,
        phase="SCOUT",
        content=json.dumps({
            "event": "scout_20min_complete",
            "micro_cards": len(micro_cards),
            "written_to_buffer": written,
            "skipped_duplicates": skipped_duplicate,
            "sources_queried": len(tasks),
            "sources_summary": sources_summary,
            "model_used": used_model,
        }, default=str),
    )

    _save_checkpoint(run_id, "scout", "COMPLETED", {
        "phase": "complete",
        "cards_produced": len(micro_cards),
    })

    # Cleanup periodico: hard-delete record con processed=true e age > 7 giorni
    # (free pass: lo facciamo qui per evitare un job dedicato)
    try:
        _purge_stale_buffer(database, ttl_days=7)
    except Exception:
        pass

    logger.info("[%s][SCOUT] Completato: %d micro-schede prodotte, %d scritte nel buffer",
                run_id, len(micro_cards), written)
    return micro_cards


# ============================================================
# Cascade aggregation: 8h → 4d → 3w
# Ogni livello legge i record del livello precedente nella finestra
# temporale, sintetizza, scrive il nuovo report e ELIMINA i record consumati.
# ============================================================

async def _cascade_aggregate(
    run_id: str,
    *,
    tier_label: str,
    source_types_to_consume: list[str],
    output_source_type: str,
    window_hours: int,
    prompt: str,
    min_records: int = 1,
    log_phase: str,
) -> dict:
    """
    Generic cascade aggregator.
    1. Legge i record di intelligence_buffer con source_type IN source_types_to_consume
       e timestamp >= now - window_hours.
    2. Se < min_records, skip senza eliminare nulla.
    3. Chiama DeepSeek-V3 (fallback Sonnet) col `prompt` fornito per sintetizzare.
    4. Inserisce un nuovo record in intelligence_buffer con source_type=output_source_type
       (raw_content = JSON completo del report, micro_summary = summary_text).
    5. ELIMINA i record consumati (per id) dopo aver scritto il report.
    """
    import database

    logger.info("[%s][SCOUT] Avvio aggregazione %s (finestra %dh)...",
                run_id, tier_label, window_hours)
    _save_checkpoint(run_id, f"scout_{tier_label.lower()}", "RUNNING",
                     {"phase": "buffer_read", "tier": tier_label})

    # 1. Lettura input dal buffer
    records = _read_buffer_by_types(
        database,
        source_types=source_types_to_consume,
        window_hours=window_hours,
    )

    if len(records) < min_records:
        logger.info("[%s][SCOUT] Skip %s: solo %d record disponibili (min=%d)",
                    run_id, tier_label, len(records), min_records)
        try:
            database.insert_agent_log(run_id, log_phase, json.dumps({
                "event": f"{tier_label.lower()}_skipped",
                "reason": "insufficient_records",
                "found": len(records),
                "needed": min_records,
            }))
        except Exception:
            pass
        return {"skipped": True, "reason": "insufficient_records", "found": len(records)}

    # 2. Costruisci contesto per DeepSeek
    period_end = datetime.now(timezone.utc)
    period_start = period_end - timedelta(hours=window_hours)

    context_lines = [
        f"Periodo aggregato: {period_start.isoformat()} → {period_end.isoformat()}",
        f"Numero record da sintetizzare: {len(records)}",
        "",
        "RECORD DA SINTETIZZARE:",
        "",
    ]
    for r in records:
        st = r.get("source_type", "?")
        summary = r.get("micro_summary") or r.get("raw_content", "")[:300]
        # Se è un report aggregato, raw_content contiene JSON denso → mostralo
        raw = r.get("raw_content", "")
        if st in (TIER_8H, TIER_4D, TIER_3W) and raw:
            context_lines.append(f"--- [{st}] {r.get('timestamp', '?')} ---")
            context_lines.append(raw[:3000])
        else:
            context_lines.append(f"[{st}] {summary[:280]}")
    context = "\n".join(context_lines)

    # 3. Chiama DeepSeek (fallback Sonnet)
    used_model = "unknown"
    try:
        try:
            response_text, used_model = await _call_deepseek(prompt, context[:20000])
        except Exception as ds_err:
            logger.warning("[%s][SCOUT] DeepSeek fallback %s (%s)", run_id, tier_label, ds_err)
            response_text, used_model = await _call_claude_fallback(prompt, context[:20000])
        report_obj = _parse_json_object(response_text)
        if not report_obj:
            raise ValueError("Empty JSON object from model")
    except Exception as e:
        logger.error("[%s][SCOUT] Errore aggregazione %s: %s", run_id, tier_label, e)
        # Non scrivere nulla, non eliminare nulla
        try:
            database.insert_agent_log(run_id, log_phase, json.dumps({
                "event": f"{tier_label.lower()}_error",
                "error": str(e)[:500],
                "model": used_model,
            }))
        except Exception:
            pass
        return {"error": str(e)}

    # Arricchisci il report con metadati di periodo
    report_obj.setdefault("tier", tier_label)
    report_obj["period_start"] = period_start.isoformat()
    report_obj["period_end"] = period_end.isoformat()
    report_obj["consumed_records"] = len(records)
    report_obj["model_used"] = used_model

    # 4. Inserisci il nuovo report aggregato in intelligence_buffer
    summary_text = (
        report_obj.get("summary_text")
        or report_obj.get("synthesis")
        or f"{tier_label} report ({len(records)} record sintetizzati)"
    )
    sentiment = 0.0
    bias = (report_obj.get("macro_bias") or "").upper()
    if bias == "BULLISH":
        sentiment = 0.5
    elif bias == "BEARISH":
        sentiment = -0.5

    try:
        _write_intelligence_buffer(
            database=database,
            run_id=run_id,
            source_type=output_source_type,
            raw_content=json.dumps(report_obj, default=str, ensure_ascii=False),
            micro_summary=summary_text[:500],
            sentiment_score=sentiment,
        )
    except Exception as e:
        logger.error("[%s][SCOUT] Errore scrittura report %s: %s", run_id, tier_label, e)
        return {"error": f"write_failed: {e}"}

    # 5. SOFT-DELETE: marca i record come processed=true (TTL hard-delete a 7 giorni)
    marked = _mark_buffer_records_processed(database, [r.get("id") for r in records if r.get("id")])

    # 6. Log
    database.insert_agent_log(
        run_id=run_id,
        phase=log_phase,
        content=json.dumps({
            "event": f"{tier_label.lower()}_complete",
            "tier": tier_label,
            "consumed": len(records),
            "marked_processed": marked,
            "model": used_model,
            "macro_bias": report_obj.get("macro_bias"),
            "regime": report_obj.get("regime"),
        }, default=str),
    )

    _save_checkpoint(run_id, f"scout_{tier_label.lower()}", "COMPLETED",
                     {"tier": tier_label, "consumed": len(records), "marked": marked})

    logger.info("[%s][SCOUT] %s completato: %d record consumati, %d marcati processed",
                run_id, tier_label, len(records), marked)
    return report_obj


def _last_aggregated_age_hours(database, source_type: str) -> float | None:
    """
    Età in ore dell'ultimo record aggregato di un dato tier.
    Ritorna None se non esiste ancora.
    Usato per anti-double-run check (evita di rigenerare un AGG_8H/AGG_4D
    poco dopo un deploy se l'ultimo è ancora "fresco").
    """
    try:
        client = database.get_client()
        if not client:
            return None
        result = client.table("intelligence_buffer") \
            .select("timestamp") \
            .eq("source_type", source_type) \
            .order("timestamp", desc=True) \
            .limit(1) \
            .execute()
        if not result.data:
            return None
        ts_str = result.data[0].get("timestamp", "")
        if not ts_str:
            return None
        last_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - last_ts).total_seconds() / 3600.0
    except Exception:
        return None


async def run_8h_report(run_id: str) -> dict:
    """L1 — Sintetizza le micro-cards delle ultime 8 ore in un singolo report."""
    import database
    # Anti-double-run: se l'ultimo AGG_8H è < 6h fa, skip (evita spreco
    # token quando Render fa restart frequenti).
    age = _last_aggregated_age_hours(database, TIER_8H)
    if age is not None and age < 6.0:
        logger.info("[%s][SCOUT] 8H skip: ultimo report di %.1fh fa (< 6h)",
                    run_id, age)
        return {"skipped": True, "reason": "too_recent", "last_age_hours": age}
    return await _cascade_aggregate(
        run_id,
        tier_label="8H",
        source_types_to_consume=MICRO_CARD_TYPES,
        output_source_type=TIER_8H,
        window_hours=8,
        prompt=REPORT_8H_PROMPT,
        # min_records=1: storicamente era 3 (= 1 ciclo Scout 20min) ma nei
        # weekend / festivi di mercati calmi lo Scout produce 0-2 micro-cards
        # e l'8H skippa con "insufficient_records" → nessuna intelligence
        # aggregata viene scritta per tutto il weekend. Con min=1 anche un
        # singolo segnale (es. notizia geopolitica del sabato) genera un
        # report 8H, garantendo continuita' nell'intelligence cascade.
        min_records=1,
        log_phase="SCOUT_8H",
    )


async def run_4d_report(run_id: str) -> dict:
    """L2 — Sintetizza i report 8h degli ultimi 4 giorni in un report 4d."""
    import database
    # Anti-double-run: se l'ultimo AGG_4D è < 72h (3gg) fa, skip.
    age = _last_aggregated_age_hours(database, TIER_4D)
    if age is not None and age < 72.0:
        logger.info("[%s][SCOUT] 4D skip: ultimo report di %.1fh fa (< 72h)",
                    run_id, age)
        return {"skipped": True, "reason": "too_recent", "last_age_hours": age}
    return await _cascade_aggregate(
        run_id,
        tier_label="4D",
        source_types_to_consume=[TIER_8H],
        output_source_type=TIER_4D,
        window_hours=24 * 4,
        prompt=REPORT_4D_PROMPT,
        min_records=2,  # almeno 2 report 8h (= ~16h di intelligence)
        log_phase="SCOUT_4D",
    )


async def run_3w_report(run_id: str) -> dict:
    """
    DEPRECATO — il tier 3W è stato rimosso dopo evidenza di valore marginale
    (sintesi della sintesi della sintesi → diminishing returns).
    Lasciato per backward-compat. Ritorna immediatamente.
    """
    return {"skipped": True, "reason": "3w_tier_deprecated"}


# ============================================================
# Weekend Intelligence Report (ripristino)
# ============================================================
# Il vecchio agente monolitico (agent.py, mode="weekend") produceva un recap
# geopolitico del weekend, lo salvava in weekend_intelligence e lo iniettava nel
# contesto del lunedì. Col passaggio all'architettura multi-agente quel job è
# stato dismesso (_scheduled_agent_job non è più registrato) e il report ha
# smesso di essere generato. Questa funzione lo ricostruisce nel paradigma
# nuovo: sintetizza i report aggregati (AGG_4D + AGG_8H) e le card del weekend
# con DeepSeek (fallback Claude) e salva il recap nella tabella esistente.
# A differenza della cascade 8H/4D è READ-ONLY sul buffer: NON consuma né
# elimina micro-card.

WEEKEND_REPORT_PROMPT = """Sei lo Scout Agent. I mercati azionari sono CHIUSI (weekend).
Devi produrre un REPORT INTELLIGENCE DEL WEEKEND che prepari la settimana di trading.

Ricevi: i report aggregati recenti (4D macro + 8H) e le notizie geopolitiche/
finanziarie raccolte durante il weekend (GDELT, NewsAPI, Congressional, crypto).

REGOLE:
1. Sintetizza cosa è successo nel weekend e cosa conta per l'apertura di lunedì.
2. Identifica gli eventi chiave con il loro impatto e i ticker coinvolti.
3. Stima le implicazioni di mercato CONCRETE per la riapertura.
4. Indica gli asset prioritari da monitorare lunedì.
5. Le crypto sono 24/7: includi i movimenti crypto rilevanti del weekend.
6. RISPONDI SOLO CON JSON, nessun preambolo.

OUTPUT JSON:
{
  "full_analysis": "Analisi geopolitica e di mercato del weekend, 6-10 frasi dense",
  "key_events": [
    {"event": "Titolo evento", "impact": "Come impatta i mercati", "tickers": ["XOM", "BTC-USD"]}
  ],
  "market_implications": "Implicazioni concrete per l'apertura di lunedì (4-6 frasi)",
  "priority_assets": ["LMT", "XOM", "BTC-USD"]
}"""


def _last_weekend_report_age_hours(database) -> float | None:
    """
    Età in ore dell'ultimo weekend_intelligence salvato (None se mai).
    Usato per l'anti-double-run del weekend report (skip se troppo recente).
    """
    try:
        rec = database.get_latest_weekend_intelligence()
        if not rec:
            return None
        ts_str = rec.get("saved_at") or rec.get("timestamp") or ""
        if not ts_str:
            return None
        last_ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last_ts).total_seconds() / 3600.0
    except Exception:
        return None


async def run_weekend_report(run_id: str, *, force: bool = False) -> dict:
    """
    Genera il report intelligence del weekend e lo salva in weekend_intelligence.

    Anti-double-run: skip se ne esiste già uno < 20h fa (salvo force=True),
    così su restart frequenti di Render resta al massimo 1 report al giorno.
    READ-ONLY sul buffer: NON consuma/elimina micro-card.
    """
    import database

    if not force:
        age = _last_weekend_report_age_hours(database)
        if age is not None and age < 20.0:
            logger.info("[%s][SCOUT] Weekend report skip: ultimo di %.1fh fa (<20h)",
                        run_id, age)
            return {"skipped": True, "reason": "too_recent", "last_age_hours": age}

    # 1. Raccogli il materiale del weekend (report aggregati + card grezze).
    #    only_unprocessed=False: includiamo anche le card già consumate dalla
    #    cascade 8H/4D — qui leggiamo, non aggreghiamo.
    rep_4d = get_latest_aggregated_reports(database, TIER_4D, n=1)
    rep_8h = get_latest_aggregated_reports(database, TIER_8H, n=4)
    geo_cards = _read_buffer_by_types(
        database,
        source_types=["GDELT", "NEWSAPI", "CONGRESSIONAL", "YFINANCE_NEWS",
                      "YFINANCE", "CRYPTO_MARKET", "REDDIT", "X"],
        window_hours=72,
        only_unprocessed=False,
        limit=120,
    )

    if not rep_4d and not rep_8h and not geo_cards:
        logger.info("[%s][SCOUT] Weekend report skip: nessun materiale", run_id)
        try:
            database.insert_agent_log(run_id, "SCOUT_WEEKEND", json.dumps({
                "event": "weekend_skipped", "reason": "no_material",
            }))
        except Exception:
            pass
        return {"skipped": True, "reason": "no_material"}

    # 2. Costruisci il contesto per il modello
    lines: list[str] = []
    if rep_4d:
        r = rep_4d[0]["report"]
        lines.append("=== REPORT MACRO 4 GIORNI ===")
        lines.append(f"Bias: {r.get('macro_bias', '?')}")
        lines.append((r.get("summary_text") or r.get("synthesis") or "")[:1200])
        if r.get("sector_rotation"):
            lines.append(f"Rotazione settoriale: "
                         f"{json.dumps(r.get('sector_rotation'), ensure_ascii=False)}")
    if rep_8h:
        lines.append("\n=== REPORT 8H RECENTI ===")
        for r0 in rep_8h:
            r = r0["report"]
            lines.append(f"--- {r0['timestamp']} | Bias: {r.get('macro_bias', '?')} ---")
            lines.append((r.get("summary_text") or "")[:400])
            if r.get("hot_tickers"):
                lines.append(f"Hot: {', '.join(r.get('hot_tickers', [])[:10])}")
    if geo_cards:
        lines.append("\n=== NOTIZIE / CARD DEL WEEKEND ===")
        for c in geo_cards[:80]:
            st = c.get("source_type", "?")
            ms = c.get("micro_summary") or ""
            if ms:
                lines.append(f"[{st}] {ms[:200]}")
    context = "\n".join(lines)

    # 3. DeepSeek-V3 (fallback Claude Sonnet)
    used_model = "unknown"
    try:
        try:
            response_text, used_model = await _call_deepseek(
                WEEKEND_REPORT_PROMPT, context[:20000])
        except Exception as ds_err:
            logger.warning("[%s][SCOUT] Weekend DeepSeek fallback (%s)", run_id, ds_err)
            response_text, used_model = await _call_claude_fallback(
                WEEKEND_REPORT_PROMPT, context[:20000])
        report_obj = _parse_json_object(response_text)
        if not report_obj:
            raise ValueError("Empty JSON object from model")
    except Exception as e:
        logger.error("[%s][SCOUT] Weekend report errore modello: %s", run_id, e)
        try:
            database.insert_agent_log(run_id, "SCOUT_WEEKEND", json.dumps({
                "event": "weekend_error", "error": str(e)[:400], "model": used_model,
            }))
        except Exception:
            pass
        return {"error": str(e)}

    # 4. Salva nella tabella weekend_intelligence. key_events come stringa JSON,
    #    coerente col vecchio save_weekend_intelligence (il frontend tollera sia
    #    stringa sia array). priority_assets non ha colonna dedicata: lo
    #    appendiamo al testo implicazioni così resta visibile.
    full_analysis = (report_obj.get("full_analysis")
                     or report_obj.get("synthesis")
                     or report_obj.get("summary_text") or "")
    key_events = report_obj.get("key_events") or []
    market_implications = report_obj.get("market_implications") or ""
    priority_assets = report_obj.get("priority_assets") or []
    if priority_assets:
        market_implications = (
            market_implications
            + f"\n\nAsset prioritari lunedì: {', '.join(map(str, priority_assets))}")

    try:
        database.insert_weekend_intelligence(
            run_id=run_id,
            content=full_analysis,
            key_events=json.dumps(key_events, ensure_ascii=False, default=str),
            market_implications=market_implications,
        )
    except Exception as e:
        logger.error("[%s][SCOUT] Weekend report write fallito: %s", run_id, e)
        return {"error": f"write_failed: {e}"}

    # 5. Log visibile nel frontend
    n_events = len(key_events) if isinstance(key_events, list) else 0
    n_assets = len(priority_assets) if isinstance(priority_assets, list) else 0
    try:
        database.insert_agent_log(run_id, "SCOUT_WEEKEND", json.dumps({
            "event": "weekend_complete",
            "key_events": n_events,
            "priority_assets": n_assets,
            "model": used_model,
            "input_4d": len(rep_4d), "input_8h": len(rep_8h),
            "input_cards": len(geo_cards),
        }, default=str))
    except Exception:
        pass

    logger.info("[%s][SCOUT] Weekend report OK: %d eventi, %d asset prioritari, model=%s",
                run_id, n_events, n_assets, used_model)
    return report_obj


# ============================================================
# Backward-compat aliases (i vecchi nomi rimangono importabili
# ma puntano alle nuove funzioni a cascata)
# ============================================================

async def run_daily_recap(run_id: str) -> dict:
    """Alias deprecato: ora corrisponde a run_8h_report."""
    return await run_8h_report(run_id)


async def run_weekly_matrix(run_id: str) -> dict:
    """Alias deprecato: ora corrisponde a run_4d_report."""
    return await run_4d_report(run_id)


# ============================================================
# Helper functions — Supabase I/O
# ============================================================

def _save_checkpoint(run_id: str, agent_name: str, status: str, data: dict):
    """Salva checkpoint su Supabase per Render resilience."""
    try:
        import database
        client = database.get_client()
        if client:
            client.table("agent_checkpoints").upsert({
                "run_id": run_id,
                "agent_name": agent_name,
                "status": status,
                "checkpoint_data": data,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
    except Exception:
        pass  # Non-blocking


# ─── Stopwords IT+EN per la dedup semantica ────────────────────────────────
# Lista compatta di parole funzionali (preposizioni, articoli, congiunzioni)
# da escludere dalla bag-of-words usata per il calcolo Jaccard.
_DEDUP_STOPWORDS = frozenset({
    # IT
    "che", "con", "per", "una", "uno", "del", "della", "dello", "delle", "dei",
    "alla", "allo", "alle", "agli", "nel", "nella", "nelle", "negli", "sul",
    "sulla", "sulle", "sugli", "ed", "anche", "come", "non", "più", "molto",
    "essere", "stato", "stata", "sono", "era", "erano", "sarà", "saranno",
    "dopo", "prima", "questo", "questa", "questi", "queste", "loro",
    # EN
    "the", "and", "for", "with", "are", "was", "were", "this", "that", "these",
    "those", "from", "have", "has", "had", "but", "not", "into", "about",
    "would", "could", "should", "their", "there", "where", "which", "while",
    "more", "most", "less", "than", "after", "before", "over", "under",
    "between", "during", "such", "only", "very",
})


def _tokenize_for_dedup(text: str) -> set:
    """Estrae set di token significativi (>3 char, no stopwords) da un summary."""
    import re as _re
    if not text:
        return set()
    s = text.lower()
    s = _re.sub(r"[^\w\s]", " ", s)
    s = _re.sub(r"\s+", " ", s).strip()
    return {w for w in s.split()
            if len(w) > 3 and w not in _DEDUP_STOPWORDS and not w.isdigit()}


def _jaccard_similarity(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _fingerprint_card(source_type: str, summary: str) -> str:
    """
    Genera fingerprint stretto (mantenuto per back-compat con vecchio
    fast-path exact-match: se il summary è IDENTICO, hash uguale).
    Per la similarità semantica vera, usa _tokenize_for_dedup + _jaccard.
    """
    import hashlib as _h
    import re as _re
    if not summary:
        return ""
    text = summary.lower()
    text = _re.sub(r"[^\w\s]", " ", text)
    text = _re.sub(r"\s+", " ", text).strip()
    words = [w for w in text.split() if len(w) > 2][:12]
    canonical = f"{source_type.upper()}|{' '.join(words)}"[:60]
    return _h.sha1(canonical.encode("utf-8")).hexdigest()[:16]


# Soglia di similarità Jaccard sopra cui due card sono considerate duplicate.
# 0.55 = 55% di token in comune (calibrato per IT+EN: con stopwords filtrate
# e tokens >3 char, è abbastanza stringente da non scartare news distinte
# sullo stesso ticker, ma riconosce paraphrase ovvi).
DEDUP_JACCARD_THRESHOLD = 0.55


def _load_recent_card_signatures(database, hours: int = 4) -> list[dict]:
    """
    Carica (source_type, summary, tokens) delle micro-schede recenti.
    Ritorna lista per permettere similarità Jaccard contro ogni record
    invece del solo match esatto via fingerprint.
    """
    signatures: list[dict] = []
    try:
        client = database.get_client()
        if not client:
            return signatures
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        result = client.table("intelligence_buffer") \
            .select("source_type,micro_summary") \
            .in_("source_type", MICRO_CARD_TYPES) \
            .gte("timestamp", cutoff) \
            .order("timestamp", desc=True) \
            .limit(200) \
            .execute()
        for row in (result.data or []):
            st = row.get("source_type", "")
            ms = row.get("micro_summary", "")
            tokens = _tokenize_for_dedup(ms)
            if tokens:
                signatures.append({
                    "source_type": st,
                    "summary": ms,
                    "tokens": tokens,
                    "fp": _fingerprint_card(st, ms),
                })
    except Exception as exc:
        logger.warning("Impossibile caricare signatures recenti per dedup: %s", exc)
    return signatures


def _is_card_duplicate(card_source: str, card_summary: str,
                       recent_signatures: list[dict],
                       threshold: float = DEDUP_JACCARD_THRESHOLD
                       ) -> tuple[bool, dict | None]:
    """
    Verifica se una nuova card è duplicato semantico di una recente.

    Ritorna (is_duplicate, matched_signature).
    Confronta SOLO con card dello stesso source_type per evitare falsi
    positivi (due fonti diverse che parlano dello stesso evento sono
    informative entrambe).
    """
    new_tokens = _tokenize_for_dedup(card_summary)
    if not new_tokens:
        return False, None
    new_fp = _fingerprint_card(card_source, card_summary)
    for sig in recent_signatures:
        # Fast path: hash exact match (stesso summary normalizzato)
        if sig["fp"] == new_fp:
            return True, sig
        # Stesso source_type richiesto per Jaccard
        if sig["source_type"] != card_source:
            continue
        sim = _jaccard_similarity(new_tokens, sig["tokens"])
        if sim >= threshold:
            return True, {**sig, "similarity": sim}
    return False, None


# Wrapper legacy mantenuto per chiamanti che si aspettavano set[str]
def _load_recent_fingerprints(database, hours: int = 4) -> set[str]:
    sigs = _load_recent_card_signatures(database, hours)
    return {s["fp"] for s in sigs if s.get("fp")}


def _write_intelligence_buffer(database, run_id: str, source_type: str,
                                raw_content: str, micro_summary: str,
                                sentiment_score: float):
    """Scrive una micro-scheda nell'intelligence_buffer."""
    try:
        client = database.get_client()
        if client:
            client.table("intelligence_buffer").insert({
                "source_type": source_type,
                "raw_content": raw_content,
                "micro_summary": micro_summary,
                "sentiment_score": sentiment_score,
                "run_id": run_id,
                "processed": False,
            }).execute()
        else:
            # Fallback: usa insert_agent_log
            database.insert_agent_log(
                run_id=run_id,
                phase=f"SCOUT_BUFFER_{source_type}",
                content=json.dumps({"micro_summary": micro_summary, "sentiment": sentiment_score}),
            )
    except Exception as e:
        logger.warning("Errore scrittura intelligence_buffer: %s", e)


def _read_buffer_by_types(database, source_types: list[str], window_hours: int,
                           limit: int = 1000, only_unprocessed: bool = True) -> list[dict]:
    """
    Legge record dell'intelligence_buffer filtrando per source_type IN (lista)
    e timestamp nell'ultima finestra `window_hours`.
    Se only_unprocessed=True (default), include solo record con processed=false
    o processed NULL (non ancora consumati da un'aggregazione).
    Ritorna ordinato per timestamp ASCENDENTE (cronologico).
    """
    try:
        client = database.get_client()
        if not client:
            return []
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
        query = client.table("intelligence_buffer") \
            .select("*") \
            .in_("source_type", source_types) \
            .gte("timestamp", cutoff)
        if only_unprocessed:
            # Supabase/PostgREST: includiamo record con processed=false E
            # quelli con processed=NULL (legacy data, record non ancora settati).
            # `.neq("processed", True)` da solo NON include i NULL perché
            # `NULL != TRUE` valuta a NULL/falsy. Usiamo `.or_` esplicito.
            query = query.or_("processed.is.null,processed.eq.false")
        result = query.order("timestamp", desc=False).limit(limit).execute()
        return result.data if result.data else []
    except Exception as e:
        logger.warning("Errore lettura buffer (types=%s): %s", source_types, e)
        return []


def _mark_buffer_records_processed(database, ids: list) -> int:
    """
    SOFT-DELETE: marca i record come processed=true invece di eliminarli.
    Mantiene un periodo di grazia (~7 giorni) per recupero/diagnosi prima
    della cancellazione definitiva (gestita da _purge_stale_buffer).
    Ritorna il numero di id processati.
    """
    ids = [i for i in ids if i is not None]
    if not ids:
        return 0
    try:
        client = database.get_client()
        if not client:
            return 0
        client.table("intelligence_buffer") \
            .update({"processed": True}) \
            .in_("id", ids) \
            .execute()
        return len(ids)
    except Exception as e:
        logger.warning("Errore mark processed buffer per id: %s", e)
        return 0


def _purge_stale_buffer(database, ttl_days: int = 7) -> int:
    """
    Hard-delete dei record marcati processed=true e più vecchi di ttl_days.
    Da chiamare periodicamente (es. dal job Scout 20min) per evitare crescita
    indefinita della tabella.
    """
    try:
        client = database.get_client()
        if not client:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=ttl_days)).isoformat()
        client.table("intelligence_buffer") \
            .delete() \
            .eq("processed", True) \
            .lt("timestamp", cutoff) \
            .execute()
        logger.debug("Purge buffer: rimossi record processed=true < %s", cutoff)
        return 1
    except Exception as e:
        logger.warning("Errore purge buffer: %s", e)
        return 0


def get_latest_aggregated_reports(database, tier: str, n: int = 1) -> list[dict]:
    """
    Recupera gli ultimi N report aggregati di un tier (AGG_8H | AGG_4D | AGG_3W).
    Ritorna lista di dict con il JSON del report già parsato in chiave 'report'.
    Lista vuota se nulla disponibile.
    """
    try:
        client = database.get_client()
        if not client:
            return []
        result = client.table("intelligence_buffer") \
            .select("*") \
            .eq("source_type", tier) \
            .order("timestamp", desc=True) \
            .limit(n) \
            .execute()
        rows = result.data if result.data else []
        out = []
        for r in rows:
            raw = r.get("raw_content") or "{}"
            try:
                report = json.loads(raw)
            except Exception:
                report = {"summary_text": raw[:500]}
            out.append({
                "id": r.get("id"),
                "timestamp": r.get("timestamp"),
                "tier": tier,
                "report": report,
                "summary": r.get("micro_summary", ""),
            })
        return out
    except Exception as e:
        logger.warning("Errore lettura aggregati tier=%s: %s", tier, e)
        return []


def _build_macro_context_for_scout(database) -> str:
    """
    Costruisce un blocco testuale con gli ultimi report aggregati (3W + 4D + 8H)
    da iniettare come PREFIX nel contesto dello Scout 20-min.
    Così lo Scout sa il regime macro corrente quando classifica le micro-cards.
    """
    parts = []

    # 4D (visione di medio-lungo termine, top tier dopo rimozione 3W; ultimi 2)
    rep_4d = get_latest_aggregated_reports(database, TIER_4D, n=2)
    if rep_4d:
        lines = ["=== CONTESTO 4D (ultimi 2 report) ==="]
        for r in rep_4d:
            rep = r["report"]
            lines.append(
                f"--- {r['timestamp']} | Bias: {rep.get('macro_bias', '?')} ---\n"
                f"{(rep.get('summary_text') or '')[:400]}\n"
                f"Trend: {', '.join(rep.get('consolidating_trends', [])[:5])}"
            )
        parts.append("\n".join(lines))

    # 8H (breve termine, ultimi 3)
    rep_8h = get_latest_aggregated_reports(database, TIER_8H, n=3)
    if rep_8h:
        lines = ["=== CONTESTO 8H (ultimi 3 report) ==="]
        for r in rep_8h:
            rep = r["report"]
            lines.append(
                f"--- {r['timestamp']} | Bias: {rep.get('macro_bias', '?')} ---\n"
                f"{(rep.get('summary_text') or '')[:300]}\n"
                f"Hot: {', '.join(rep.get('hot_tickers', [])[:8])}"
            )
        parts.append("\n".join(lines))

    if not parts:
        return ""
    return "\n\n".join(parts)


# ============================================================
# Backward-compat: vecchi getter ora puntano ai tier aggregati
# ============================================================

def get_latest_weekly_matrix(database) -> dict | None:
    """
    DEPRECATO: ora ritorna l'ultimo report 3W in formato compatibile col vecchio
    schema weekly_matrix. Il Decision Agent vecchio continua a funzionare.
    """
    rep = get_latest_aggregated_reports(database, TIER_3W, n=1)
    if not rep:
        # fallback: ultimo 4D se non c'è ancora un 3W
        rep = get_latest_aggregated_reports(database, TIER_4D, n=1)
        if not rep:
            return None
    r = rep[0]["report"]
    return {
        "week_id": rep[0]["timestamp"],
        "synthesis": r.get("synthesis") or r.get("summary_text", ""),
        "long_term_risks": ", ".join(r.get("structural_risks", []) or r.get("accumulating_risks", [])),
        "sector_rotation_signals": r.get("sector_rotation", {}),
        "macro_strategy": r.get("macro_strategy") or r.get("next_4d_strategy", ""),
    }


def get_latest_daily_snapshots(database, n: int = 3) -> list[dict]:
    """
    DEPRECATO: ora ritorna gli ultimi N report 8H in formato compatibile col
    vecchio schema daily_snapshots.
    """
    rep = get_latest_aggregated_reports(database, TIER_8H, n=n)
    out = []
    for r in rep:
        rr = r["report"]
        out.append({
            "date": r["timestamp"],
            "summary_text": rr.get("summary_text", ""),
            "key_events": rr.get("key_events", []),
            "macro_bias": rr.get("macro_bias", "NEUTRAL"),
            "hot_tickers": rr.get("hot_tickers", []),
        })
    return out


def get_recent_buffer(database, minutes: int = 40) -> list[dict]:
    """
    Recupera intelligence buffer degli ultimi N minuti, SOLO micro-cards L0
    (esclude i record aggregati AGG_8H/AGG_4D/AGG_3W).
    """
    try:
        client = database.get_client()
        if client:
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
            result = client.table("intelligence_buffer") \
                .select("*") \
                .in_("source_type", MICRO_CARD_TYPES) \
                .gte("timestamp", cutoff) \
                .order("timestamp", desc=True) \
                .limit(100) \
                .execute()
            return result.data if result.data else []
    except Exception:
        pass
    return []


# ============================================================
# JSON parsing helpers
# ============================================================

def _parse_json_array(text: str) -> list:
    """Estrae un JSON array dal testo (tollerante a testo extra intorno)."""
    try:
        start = text.find("[")
        end = text.rfind("]") + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end])
    except json.JSONDecodeError:
        pass
    return []


def _parse_json_array_robust(text: str) -> list:
    """
    Parsing JSON array tollerante:
      1. Prima prova array completo [...]
      2. Poi prova a estrarre da ```json ... ```
      3. Poi cerca tutti gli oggetti {...} e li raggruppa
    """
    if not text:
        return []
    # 1. Array completo
    cards = _parse_json_array(text)
    if cards:
        return cards

    # 2. Blocchi ```json ... ```
    import re
    code_blocks = re.findall(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    for block in code_blocks:
        cards = _parse_json_array(block)
        if cards:
            return cards
        # prova come oggetto singolo
        try:
            obj = json.loads(block.strip())
            if isinstance(obj, dict):
                return [obj]
            if isinstance(obj, list):
                return obj
        except json.JSONDecodeError:
            pass

    # 3. Estrai tutti gli oggetti {...} bilanciati
    objects = []
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    obj = json.loads(text[start:i + 1])
                    if isinstance(obj, dict) and obj.get("source_type"):
                        objects.append(obj)
                except json.JSONDecodeError:
                    pass
                start = -1
    return objects


def _parse_json_object(text: str) -> dict:
    """Estrae un JSON object dal testo (tollerante a testo extra intorno)."""
    try:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end])
    except json.JSONDecodeError:
        pass
    return {}
