"""
Scout Agent — Claude Sonnet 4.5
Intelligence gathering 24/7 con compattazione temporale.

Task:
  - Ogni 20 min (24/7): Interroga GDELT, NewsAPI, yFinance News,
    Reddit (sentiment retail) e X. Filtra rumore e scrive intelligence_buffer.
  - Ogni giorno 23:59 CET: Compatta buffer in Daily Snapshot
  - Ogni domenica 23:59 CET: Aggrega 7 Daily in Weekly Matrix
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone, timedelta

from anthropic import Anthropic

logger = logging.getLogger(__name__)

# Claude Sonnet 4.5 — il modello "potente" che raccoglie e analizza notizie ogni 20 min.
# Nota: Sonnet e' caro, ma il volume e' contenuto (72 run/giorno x ~3K token in/out)
# Costo stimato: ~$8-12/mese (compatibile con il budget).
SCOUT_MODEL = "claude-sonnet-4-5-20250929"
SCOUT_MODEL_FALLBACK = "claude-sonnet-4-20250514"

# ============================================================
# Prompt Templates
# ============================================================

SCOUT_20MIN_PROMPT = """Sei uno Scout Agent specializzato in intelligence geopolitica e di mercato.
Ricevi dati grezzi da multiple fonti:
  - GDELT (eventi geopolitici globali)
  - NEWSAPI (news mainstream)
  - YFINANCE_NEWS (news per ticker watchlist)
  - REDDIT (sentiment retail: wallstreetbets/stocks/investing/options)
  - X (sentiment finance Twitter)
  - CLAWSTREET (contesto mercati)
  - CONGRESSIONAL (insider trades USA)

Il tuo compito:
1. FILTRA il rumore — ignora contenuti irrilevanti per i mercati
2. Per ogni evento/cluster di rilievo genera una MICRO-SCHEDA con:
   - Sintesi azionabile in 2-3 frasi
   - Sentiment score (-1 bearish a +1 bullish)
   - Sentiment_type: "INSTITUTIONAL" (news, GDELT) o "RETAIL" (Reddit, X)
   - Ticker impattati
   - Keyword di rischio
3. Se NON ci sono notizie significative restituisci comunque almeno UNA micro-scheda
   con micro_summary="Nessun evento rilevante" e sentiment_score=0 — serve per dare
   visibilita' del fatto che lo Scout ha girato.

OUTPUT JSON array di micro-schede:
[
  {
    "source_type": "GDELT|NEWSAPI|YFINANCE_NEWS|REDDIT|X|CLAWSTREET|CONGRESSIONAL",
    "micro_summary": "...",
    "sentiment_score": -1.0 to 1.0,
    "sentiment_type": "INSTITUTIONAL|RETAIL",
    "key_tickers": ["XOM", "LMT"],
    "risk_keywords": ["conflict", "sanctions"]
  }
]"""

DAILY_RECAP_PROMPT = """Sei uno Scout Agent che deve produrre il Daily Snapshot delle ultime 24 ore.

Ricevi tutte le micro-schede dell'intelligence_buffer di oggi.
Il tuo compito:
1. Sintetizza la giornata in un paragrafo denso e azionabile
2. Identifica i TOP 5 eventi chiave con impatto sul mercato
3. Determina il macro_bias (BULLISH/BEARISH/NEUTRAL)
4. Lista i ticker "caldi" da monitorare domani
5. Valuta se il sentiment sta migliorando, peggiorando o stabile

OUTPUT JSON:
{
  "summary_text": "Sintesi della giornata...",
  "key_events": [{"event": "...", "impact": "...", "tickers": [...]}],
  "macro_bias": "BULLISH|BEARISH|NEUTRAL",
  "hot_tickers": ["XOM", "LMT", ...],
  "sentiment_shift": "IMPROVING|WORSENING|STABLE"
}"""

WEEKLY_MATRIX_PROMPT = """Sei uno Scout Agent che deve produrre la Weekly Matrix.

Ricevi le 7 Daily Snapshots della settimana appena conclusa.
Il tuo compito:
1. Sintetizza la VISIONE MACRO per la settimana entrante
2. Identifica i rischi a medio-lungo termine
3. Segnala rotazioni settoriali in atto
4. Proponi una strategia macro (risk-on, risk-off, settoriale)

OUTPUT JSON:
{
  "synthesis": "Visione macro della settimana entrante...",
  "long_term_risks": "Rischi identificati...",
  "sector_rotation_signals": {"energy": "OVERWEIGHT", "tech": "UNDERWEIGHT", ...},
  "macro_strategy": "Strategia suggerita..."
}"""


def _get_client() -> Anthropic:
    """Crea client Anthropic con API key da env."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        try:
            import database as _db
            key = _db.get_setting("anthropic_api_key", "")
        except Exception:
            pass
    return Anthropic(api_key=key)


# ============================================================
# Task 20-min: Continuous Intelligence Stream
# ============================================================

async def run_scout_20min(run_id: str) -> list[dict]:
    """
    Interroga le API, filtra il rumore con Sonnet 4.6,
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
        "CLAWSTREET_MARKET", "CONGRESSIONAL", "CLAWSTREET_ECONOMY",
    ]
    tasks = [
        data_fetchers.fetch_gdelt_data(),
        data_fetchers.fetch_newsapi_data(),
        data_fetchers.fetch_yfinance_news(),
        data_fetchers.fetch_reddit_sentiment(),
        data_fetchers.fetch_x_sentiment(),
        data_fetchers.fetch_clawstreet_market_context(),
        data_fetchers.fetch_congressional_trades(),
        data_fetchers.fetch_clawstreet_economy(),
    ]

    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    # 2. Prepara contesto per Sonnet + summary per logging visibile
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

    # 2b. Logga SUBITO il riepilogo fonti — visibile nel frontend anche se Sonnet fallisce.
    try:
        database.insert_agent_log(run_id, "SCOUT", json.dumps({
            "event": "scout_sources_fetched",
            "sources": sources_summary,
            "total_sources": len(source_names),
        }, default=str))
    except Exception:
        pass

    # 3. Chiama Sonnet 4.6 per filtrare e sintetizzare
    _save_checkpoint(run_id, "scout", "RUNNING", {"phase": "analysis"})

    used_model = SCOUT_MODEL
    try:
        client = _get_client()
        try:
            response = client.messages.create(
                model=SCOUT_MODEL,
                max_tokens=4096,
                system=SCOUT_20MIN_PROMPT,
                messages=[{
                    "role": "user",
                    "content": f"Analizza questi dati e produci le micro-schede JSON:\n\n{full_context[:20000]}"
                }],
            )
        except Exception as model_err:
            # Fallback automatico se Sonnet 4.5 non disponibile
            logger.warning("[%s][SCOUT] %s non disponibile (%s), fallback a %s",
                           run_id, SCOUT_MODEL, model_err, SCOUT_MODEL_FALLBACK)
            used_model = SCOUT_MODEL_FALLBACK
            response = client.messages.create(
                model=SCOUT_MODEL_FALLBACK,
                max_tokens=4096,
                system=SCOUT_20MIN_PROMPT,
                messages=[{
                    "role": "user",
                    "content": f"Analizza questi dati e produci le micro-schede JSON:\n\n{full_context[:20000]}"
                }],
            )

        response_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                response_text += block.text

        # Parse JSON
        micro_cards = _parse_json_array(response_text)

    except Exception as e:
        logger.error("[%s][SCOUT] Errore Sonnet: %s", run_id, e)
        micro_cards = []

    # 4. Scrivi nel buffer su Supabase
    written = 0
    for card in micro_cards:
        try:
            _write_intelligence_buffer(
                database=database,
                run_id=run_id,
                source_type=card.get("source_type", "UNKNOWN"),
                raw_content=json.dumps(card, default=str),
                micro_summary=card.get("micro_summary", ""),
                sentiment_score=float(card.get("sentiment_score", 0)),
            )
            written += 1
        except Exception as e:
            logger.warning("[%s][SCOUT] Errore scrittura buffer: %s", run_id, e)

    # 5. Log finale — SEMPRE scritto (anche se 0 schede), cosi' il frontend
    # mostra ogni esecuzione dello Scout con count fonti e numero schede.
    database.insert_agent_log(
        run_id=run_id,
        phase="SCOUT",
        content=json.dumps({
            "event": "scout_20min_complete",
            "micro_cards": len(micro_cards),
            "written_to_buffer": written,
            "sources_queried": len(tasks),
            "sources_summary": sources_summary,
            "model_used": used_model,
        }, default=str),
    )

    _save_checkpoint(run_id, "scout", "COMPLETED", {
        "phase": "complete",
        "cards_produced": len(micro_cards),
    })

    logger.info("[%s][SCOUT] Completato: %d micro-schede prodotte, %d scritte nel buffer",
                run_id, len(micro_cards), written)
    return micro_cards


# ============================================================
# Task Daily Recap (23:59 CET)
# ============================================================

async def run_daily_recap(run_id: str) -> dict:
    """
    Recupera tutti i record dell'intelligence_buffer delle ultime 24 ore.
    Genera una Daily Snapshot condensata e pulisce il buffer vecchio.
    """
    import database

    logger.info("[%s][SCOUT] Avvio Daily Recap...", run_id)
    _save_checkpoint(run_id, "scout_daily", "RUNNING", {"phase": "buffer_read"})

    # 1. Recupera buffer ultime 24h
    buffer_records = _get_buffer_last_24h(database)

    if not buffer_records:
        logger.info("[%s][SCOUT] Nessun record nel buffer, skip daily recap.", run_id)
        return {"skipped": True, "reason": "empty_buffer"}

    # 2. Prepara contesto
    summaries = []
    for rec in buffer_records:
        summaries.append(f"[{rec.get('source_type', '?')}] {rec.get('micro_summary', rec.get('raw_content', '')[:200])}")
    context = f"Intelligence buffer delle ultime 24 ore ({len(buffer_records)} record):\n\n" + "\n".join(summaries)

    # 3. Chiama Sonnet per compattazione
    try:
        client = _get_client()
        response = client.messages.create(
            model=SCOUT_MODEL,
            max_tokens=4096,
            system=DAILY_RECAP_PROMPT,
            messages=[{"role": "user", "content": context[:20000]}],
        )
        response_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                response_text += block.text

        snapshot = _parse_json_object(response_text)
    except Exception as e:
        logger.error("[%s][SCOUT] Errore daily recap: %s", run_id, e)
        snapshot = {
            "summary_text": f"Errore generazione recap: {e}",
            "key_events": [],
            "macro_bias": "NEUTRAL",
            "hot_tickers": [],
            "sentiment_shift": "STABLE",
        }

    # 4. Salva su Supabase
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _save_daily_snapshot(database, today, snapshot, len(buffer_records))

    # 5. Pulisci buffer vecchio (> 48h)
    _cleanup_old_buffer(database, hours=48)

    database.insert_agent_log(
        run_id=run_id,
        phase="SCOUT_DAILY",
        content=json.dumps({
            "event": "daily_recap_complete",
            "date": today,
            "buffer_records": len(buffer_records),
            "macro_bias": snapshot.get("macro_bias"),
        }),
    )

    _save_checkpoint(run_id, "scout_daily", "COMPLETED", {"date": today})
    logger.info("[%s][SCOUT] Daily Recap completato per %s (%d record processati)",
                run_id, today, len(buffer_records))
    return snapshot


# ============================================================
# Task Weekly Matrix (Domenica 23:59 CET)
# ============================================================

async def run_weekly_matrix(run_id: str) -> dict:
    """
    Aggrega le 7 Daily Snapshots in una Weekly Matrix
    con strategia macro per la settimana entrante.
    """
    import database

    logger.info("[%s][SCOUT] Avvio Weekly Matrix...", run_id)
    _save_checkpoint(run_id, "scout_weekly", "RUNNING", {"phase": "daily_aggregation"})

    # 1. Recupera ultime 7 daily snapshots
    dailies = _get_last_n_daily_snapshots(database, n=7)

    if not dailies:
        logger.info("[%s][SCOUT] Nessuna daily snapshot, skip weekly matrix.", run_id)
        return {"skipped": True, "reason": "no_dailies"}

    # 2. Prepara contesto
    context_parts = []
    for d in dailies:
        context_parts.append(f"=== {d.get('date', '?')} (Bias: {d.get('macro_bias', '?')}) ===\n{d.get('summary_text', '')}")
    context = f"Daily Snapshots degli ultimi 7 giorni ({len(dailies)} disponibili):\n\n" + "\n\n".join(context_parts)

    # 3. Chiama Sonnet
    try:
        client = _get_client()
        response = client.messages.create(
            model=SCOUT_MODEL,
            max_tokens=4096,
            system=WEEKLY_MATRIX_PROMPT,
            messages=[{"role": "user", "content": context[:20000]}],
        )
        response_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                response_text += block.text

        matrix = _parse_json_object(response_text)
    except Exception as e:
        logger.error("[%s][SCOUT] Errore weekly matrix: %s", run_id, e)
        matrix = {"synthesis": f"Errore: {e}", "long_term_risks": "", "sector_rotation_signals": {}, "macro_strategy": ""}

    # 4. Salva su Supabase
    now = datetime.now(timezone.utc)
    week_id = f"{now.year}-W{now.isocalendar()[1]:02d}"
    _save_weekly_matrix(database, week_id, matrix, len(dailies))

    database.insert_agent_log(
        run_id=run_id,
        phase="SCOUT_WEEKLY",
        content=json.dumps({
            "event": "weekly_matrix_complete",
            "week_id": week_id,
            "dailies_used": len(dailies),
        }),
    )

    _save_checkpoint(run_id, "scout_weekly", "COMPLETED", {"week_id": week_id})
    logger.info("[%s][SCOUT] Weekly Matrix completata per %s (%d daily usate)",
                run_id, week_id, len(dailies))
    return matrix


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


def _get_buffer_last_24h(database) -> list[dict]:
    """Recupera record intelligence_buffer delle ultime 24 ore."""
    try:
        client = database.get_client()
        if client:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            result = client.table("intelligence_buffer") \
                .select("*") \
                .gte("timestamp", cutoff) \
                .order("timestamp", desc=True) \
                .limit(500) \
                .execute()
            return result.data if result.data else []
    except Exception as e:
        logger.warning("Errore lettura buffer: %s", e)
    return []


def _cleanup_old_buffer(database, hours: int = 48):
    """Pulisce record buffer piu' vecchi di N ore."""
    try:
        client = database.get_client()
        if client:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            client.table("intelligence_buffer") \
                .delete() \
                .lt("timestamp", cutoff) \
                .execute()
    except Exception as e:
        logger.warning("Errore cleanup buffer: %s", e)


def _save_daily_snapshot(database, date_str: str, snapshot: dict, buffer_count: int):
    """Salva daily snapshot su Supabase."""
    try:
        client = database.get_client()
        if client:
            client.table("daily_snapshots").upsert({
                "date": date_str,
                "summary_text": snapshot.get("summary_text", ""),
                "key_events": snapshot.get("key_events", []),
                "macro_bias": snapshot.get("macro_bias", "NEUTRAL"),
                "hot_tickers": snapshot.get("hot_tickers", []),
            }).execute()
        # Fallback: usa weekend_intelligence per compatibilita'
        database.insert_weekend_intelligence(
            run_id=f"daily_{date_str}",
            content=snapshot.get("summary_text", ""),
            key_events=json.dumps(snapshot.get("key_events", [])),
            market_implications=snapshot.get("macro_bias", "NEUTRAL"),
        )
    except Exception as e:
        logger.warning("Errore salvataggio daily snapshot: %s", e)


def _get_last_n_daily_snapshots(database, n: int = 7) -> list[dict]:
    """Recupera ultime N daily snapshots."""
    try:
        client = database.get_client()
        if client:
            result = client.table("daily_snapshots") \
                .select("*") \
                .order("date", desc=True) \
                .limit(n) \
                .execute()
            return result.data if result.data else []
    except Exception as e:
        logger.warning("Errore lettura daily snapshots: %s", e)
    return []


def _save_weekly_matrix(database, week_id: str, matrix: dict, dailies_used: int):
    """Salva weekly matrix su Supabase."""
    try:
        client = database.get_client()
        if client:
            client.table("weekly_matrix").upsert({
                "week_id": week_id,
                "synthesis": matrix.get("synthesis", ""),
                "long_term_risks": matrix.get("long_term_risks", ""),
                "sector_rotation_signals": matrix.get("sector_rotation_signals", {}),
                "macro_strategy": matrix.get("macro_strategy", ""),
            }).execute()
    except Exception as e:
        logger.warning("Errore salvataggio weekly matrix: %s", e)


def get_latest_weekly_matrix(database) -> dict | None:
    """Recupera l'ultima weekly matrix disponibile."""
    try:
        client = database.get_client()
        if client:
            result = client.table("weekly_matrix") \
                .select("*") \
                .order("week_id", desc=True) \
                .limit(1) \
                .execute()
            return result.data[0] if result.data else None
    except Exception:
        pass
    return None


def get_latest_daily_snapshots(database, n: int = 3) -> list[dict]:
    """Recupera ultime N daily snapshots."""
    return _get_last_n_daily_snapshots(database, n)


def get_recent_buffer(database, minutes: int = 40) -> list[dict]:
    """Recupera intelligence buffer degli ultimi N minuti."""
    try:
        client = database.get_client()
        if client:
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
            result = client.table("intelligence_buffer") \
                .select("*") \
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
