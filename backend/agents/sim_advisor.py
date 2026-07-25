"""
Sim Advisor — Coach AI per il Simulator.

Analizza i run completati, propone consigli per migliorare il Decision Agent
in scenari simili, e mantiene una memoria categorizzata degli advice.
Ogni nuovo run del Simulator riceve gli advice della propria categoria
iniettati nel system prompt come "lezioni apprese".

Engine: DeepSeek-R1 (deepseek-reasoner) — stesso pattern di chat_assistant.py.

Storage: usa la tabella `sim_settings` come storage chiave-valore JSON
(no DDL richiesta), con fallback automatico al `database.settings` se
sim_settings non è disponibile.

Schema chiavi:
  _sim_advice::{category_key}              JSON list di advice records
  _sim_advice::index                        JSON list dei category_key noti
  _sim_advisor_chat::{run_id}               JSON state della chat di un run

Ogni advice record:
  {
    "id": uuid,
    "run_id": uuid,                         # run da cui è nato
    "scenario_category": "macro__bear",     # chiave category_regime
    "scenario_tags": { "category", "regime", "asset_class" },
    "title": "<= 80 char",
    "text": "<= 500 char (regola operativa)",
    "rationale": "<= 400 char (perché)",
    "created_at": iso8601,
    "apply_count": int,                     # quante volte iniettato in run successivi
    "quality_score": float | None,          # rating utente futuro
  }
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

import aiohttp

logger = logging.getLogger(__name__)

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
# Per l'advisor usiamo DeepSeek-V3 (deepseek-chat), NON R1 (deepseek-reasoner):
#   - V3 risponde in ~10-20s, R1 in 40-90s
#   - Per il task (coaching su trade-decision, output strutturato JSON),
#     V3 e' piu' che sufficiente; il reasoning esplicito di R1 e' overkill
#   - Costo simile (~$0.0006/run V3 vs ~$0.005/run R1) ma latenza incomparabile
DEEPSEEK_MODEL = "deepseek-v4-flash"
MAX_RESPONSE_TOKENS = 3000

# ─── Storage helpers (sim_settings con fallback a database.settings) ────────

ADVICE_KEY_PREFIX = "_sim_advice::"
ADVICE_INDEX_KEY = "_sim_advice::index"
ADVISOR_CHAT_KEY_PREFIX = "_sim_advisor_chat::"
# Archivio dei DEBRIEF di run ("[Auto] scenario → OUTCOME"): sono LOG,
# non lezioni. Prima venivano salvati nei bucket advice e occupavano gli
# slot delle 5 lezioni iniettate nei prompt (93 su 400 record erano
# debrief). Ora vivono qui: consultabili per statistiche, mai iniettati.
ADVICE_ARCHIVE_PREFIX = "_sim_advice_archive::"
ARCHIVE_CAP_PER_KEY = 150


def _settings_get(key: str, default: str = "") -> str:
    """Get da sim_settings con fallback a database.settings."""
    try:
        from simulator import db as sim_db
        v = sim_db.get_setting(key, "")
        if v:
            return v
    except Exception:
        pass
    try:
        import database
        return database.get_setting(key, default) or default
    except Exception:
        return default


def _settings_set(key: str, value: str) -> bool:
    """Set su sim_settings; se fallisce, fallback a database.settings.
    Ritorna True se almeno uno dei due ha avuto successo."""
    ok = False
    try:
        from simulator import db as sim_db
        sim_db.set_setting(key, value)
        ok = True
    except Exception as e:
        logger.debug("sim_settings set fallita per %s: %s", key, e)
    if not ok:
        try:
            import database
            database.set_setting(key, value)
            ok = True
        except Exception as e:
            logger.warning("database.settings set fallita per %s: %s", key, e)
    return ok


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── Scenario detection ─────────────────────────────────────────────────────

CRYPTO_TICKERS = {
    "BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD", "AVAX-USD",
    "ADA-USD", "XRP-USD", "LTC-USD", "DOT-USD", "LINK-USD",
    "UNI-USD", "ATOM-USD", "MATIC-USD", "NEAR-USD",
}
COMMODITY_TICKERS = {
    "GLD", "SLV", "USO", "DBA", "DBC", "CORN", "WEAT",
    "UNG", "PALL", "PPLT",
}


def _detect_market_regime(market_data: list) -> str:
    """
    Deriva il regime di mercato dai change_7d aggregati:
      - 'volatile': media delle variazioni assolute > 7%
      - 'bear': media direzionale < -2.5%
      - 'bull': media direzionale > +2.5%
      - 'neutral': altrimenti
    """
    if not market_data:
        return "neutral"
    changes = [m.get("change_7d") for m in market_data if m.get("change_7d") is not None]
    if not changes:
        return "neutral"
    avg = sum(changes) / len(changes)
    abs_avg = sum(abs(c) for c in changes) / len(changes)
    if abs_avg > 7:
        return "volatile"
    if avg < -2.5:
        return "bear"
    if avg > 2.5:
        return "bull"
    return "neutral"


def _detect_asset_class(asset_universe: list) -> str:
    """crypto / commodity / equity / mixed in base alla composizione."""
    if not asset_universe:
        return "mixed"
    n = len(asset_universe)
    crypto = sum(1 for a in asset_universe
                 if (a or "").upper() in CRYPTO_TICKERS or (a or "").endswith("-USD"))
    commodity = sum(1 for a in asset_universe if (a or "").upper() in COMMODITY_TICKERS)

    if crypto / n > 0.5:
        return "crypto"
    if commodity / n > 0.4:
        return "commodity"
    if (crypto + commodity) == 0:
        return "equity"
    return "mixed"


def detect_scenario_key(run_or_scenario: dict) -> tuple[str, dict]:
    """
    Calcola (category_key, tags) per un run o uno scenario.

    Accetta sia run completati (con full_data) sia scenario raw.
    category_key formato: '{category}__{regime}' es. 'macro__bear'.
    """
    # I run completati hanno category top-level, full_data.steps[0].context.market_data, ...
    category = run_or_scenario.get("category", "unknown")
    market_data = []
    asset_universe = []

    # Prova prima: scenario raw
    if "market_data" in run_or_scenario:
        market_data = run_or_scenario.get("market_data", []) or []
        asset_universe = run_or_scenario.get("asset_universe", []) or []
    else:
        # Run completato: pesca dal full_data.steps
        full = run_or_scenario.get("full_data") or {}
        steps = full.get("steps") or []
        if steps:
            ctx = (steps[0] or {}).get("context") or {}
            market_data = ctx.get("market_data") or []
            asset_universe = ctx.get("asset_universe") or []
        if not market_data:
            # Run v2: niente steps[].context — i dati stanno in
            # full_data.scenario.market_data (prima → regime sempre 'neutral'
            # e memoria advisor frammentata su bucket diversi per lo stesso run).
            scen = full.get("scenario") or {}
            market_data = scen.get("market_data") or market_data
            asset_universe = scen.get("asset_universe") or asset_universe

    regime = _detect_market_regime(market_data)
    asset_class = _detect_asset_class(asset_universe)
    tags = {"category": category, "regime": regime, "asset_class": asset_class}
    key = f"{category}__{regime}"
    return key, tags


# ─── CRUD su advice memory ─────────────────────────────────────────────────

def _load_index() -> list[str]:
    raw = _settings_get(ADVICE_INDEX_KEY, "[]")
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _save_index(idx: list[str]) -> None:
    _settings_set(ADVICE_INDEX_KEY, json.dumps(sorted(set(idx))))


def _normalize_title_tokens(title: str) -> set:
    """Token-set del titolo per il confronto di similarità: via i tag
    [TIMING]/[Auto]/..., lowercase, solo parole alfanumeriche >2 char."""
    t = re.sub(r"^\[[^\]]+\]\s*", "", title or "").lower()
    tokens = set(re.findall(r"[a-z0-9]{3,}", t))
    # Stopwords minime it/en che gonfiano la similarità senza informazione
    return tokens - {"the", "con", "per", "del", "della", "sul", "sulla",
                     "una", "uno", "nel", "nella", "dopo", "che", "non"}


def _title_similarity(a: str, b: str) -> float:
    """Jaccard sui token dei titoli. 0..1."""
    ta, tb = _normalize_title_tokens(a), _normalize_title_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# Soglia dedup: 0.6 cattura i cloni osservati ("Sell the news su Merge
# hype" vs "Sell the news su Ethereum Merge" ≈ 0.6-0.8) senza fondere
# lezioni distinte della stessa categoria.
DEDUP_SIMILARITY_THRESHOLD = 0.6


def _quality_score(it: dict) -> float:
    """
    Punteggio qualità DETERMINISTICO di un advice — usato per scegliere
    le 5 lezioni iniettate nei prompt. Prima la selezione era per pura
    recency (il campo quality_score esisteva ma non veniva mai calcolato):
    l'ultima lezione scritta vinceva sempre, anche se contraddiceva le 10
    precedenti o era un log di run.

    Criteri (dal design del sistema: le lezioni buone sono CONCRETE,
    AZIONABILI, GENERALIZZABILI):
      -5  log di run ([Auto]/debrief) — non è una lezione
      +3  lezione vera taggata ([TIMING], [SIZE], ...)
      +2  contiene numeri/soglie (regola concreta, non vaga)
      +1  ha un rationale
      +0.5 × dup_count (cap +2): ri-imparata in più run = pattern reale
      + quality_score utente se presente (rating manuale futuro, additivo)
      + recency come SPAREGGIO (max +1, decade in ~60 giorni)
    """
    score = 0.0
    title = it.get("title") or ""
    tags = it.get("scenario_tags") or {}
    if title.startswith("[Auto]") or tags.get("source") == "debrief":
        score -= 5.0
    if tags.get("source") == "lesson" or re.match(r"^\[[A-Z_]+\]", title):
        score += 3.0
    if re.search(r"\d", it.get("text") or ""):
        score += 2.0
    if (it.get("rationale") or "").strip():
        score += 1.0
    score += min(int(it.get("dup_count") or 0), 4) * 0.5
    if isinstance(it.get("quality_score"), (int, float)):
        score += float(it["quality_score"])
    # Recency: spareggio lineare 0..1 su 60 giorni
    try:
        created = datetime.fromisoformat(
            str(it.get("created_at", "")).replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - created).total_seconds() / 86400
        score += max(0.0, 1.0 - age_days / 60.0)
    except Exception:
        pass
    return score


def _load_bucket(category_key: str) -> list[dict]:
    raw = _settings_get(f"{ADVICE_KEY_PREFIX}{category_key}", "[]")
    try:
        items = json.loads(raw) if isinstance(raw, str) else raw
        return items if isinstance(items, list) else []
    except Exception:
        return []


def load_advice_for_key(category_key: str, max_items: int = 5) -> list[dict]:
    """
    Migliori N advice per la category_key, ordinati per QUALITÀ
    (punteggio deterministico, recency solo come spareggio — vedi
    _quality_score). I log di run finiscono in fondo e di fatto non
    vengono mai iniettati.
    """
    items = _load_bucket(category_key)
    items.sort(key=lambda x: (_quality_score(x), x.get("created_at", "")),
               reverse=True)
    return items[:max_items]


def save_advice(advice: dict) -> str:
    """
    Salva (o aggiorna) un advice. Ritorna l'id.
    L'advice deve avere `scenario_category` (la chiave) e almeno title+text.
    """
    key = advice.get("scenario_category")
    if not key:
        raise ValueError("advice.scenario_category mancante")
    aid = advice.get("id") or str(uuid.uuid4())

    payload = {
        "id": aid,
        "run_id": advice.get("run_id"),
        "scenario_category": key,
        "scenario_tags": advice.get("scenario_tags") or {},
        "title": (advice.get("title") or "")[:200],
        "text": (advice.get("text") or "")[:1000],
        "rationale": (advice.get("rationale") or "")[:600],
        "created_at": advice.get("created_at") or _now_iso(),
        "apply_count": int(advice.get("apply_count") or 0),
        "quality_score": advice.get("quality_score"),
    }

    items = _load_bucket(key)

    # Update if exists, else prepend
    found = False
    for i, it in enumerate(items):
        if it.get("id") == aid:
            items[i] = payload
            found = True
            break

    if not found:
        # DEDUP: la memoria si riempiva di cloni (8 varianti di "sell the
        # news sul Merge" dallo stesso scenario rigiocato 11 volte) che
        # occupavano gli slot delle 5 lezioni iniettate. Se esiste già una
        # lezione con titolo molto simile, NON creiamo il doppione:
        # rinfreschiamo quella esistente e contiamo la ri-conferma in
        # dup_count (che ne ALZA il quality_score: una lezione ri-imparata
        # in più run è un pattern reale, non rumore).
        for it in items:
            if _title_similarity(payload["title"], it.get("title", "")) \
                    >= DEDUP_SIMILARITY_THRESHOLD:
                it["dup_count"] = int(it.get("dup_count") or 0) + 1
                it["created_at"] = payload["created_at"]
                # Il testo più recente può essere più raffinato: tienilo
                # se più lungo/concreto di quello esistente.
                if len(payload["text"]) > len(it.get("text") or ""):
                    it["text"] = payload["text"]
                _settings_set(f"{ADVICE_KEY_PREFIX}{key}",
                              json.dumps(items, default=str))
                logger.info("[SIM-ADVISOR] dedup: '%s' ricondotta a advice "
                            "esistente %s (dup_count=%d)",
                            payload["title"][:60], it.get("id"),
                            it["dup_count"])
                return it.get("id") or aid
        items.insert(0, payload)
    items = items[:50]  # cap per categoria

    _settings_set(f"{ADVICE_KEY_PREFIX}{key}", json.dumps(items, default=str))

    # Index
    idx = _load_index()
    if key not in idx:
        idx.append(key)
        _save_index(idx)

    return aid


def save_run_log(advice: dict) -> str:
    """
    Salva un DEBRIEF di run nell'ARCHIVIO (non nei bucket advice).
    Stesso shape di save_advice, ma: niente dedup, niente iniezione nei
    prompt, cap più alto. Serve per statistiche storiche e consultazione,
    non per l'apprendimento (un riassunto-partita non è una regola).
    """
    key = advice.get("scenario_category")
    if not key:
        raise ValueError("advice.scenario_category mancante")
    aid = advice.get("id") or str(uuid.uuid4())
    payload = {
        "id": aid,
        "run_id": advice.get("run_id"),
        "scenario_category": key,
        "scenario_tags": advice.get("scenario_tags") or {},
        "title": (advice.get("title") or "")[:200],
        "text": (advice.get("text") or "")[:1000],
        "rationale": (advice.get("rationale") or "")[:600],
        "created_at": advice.get("created_at") or _now_iso(),
    }
    raw = _settings_get(f"{ADVICE_ARCHIVE_PREFIX}{key}", "[]")
    try:
        items = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(items, list):
            items = []
    except Exception:
        items = []
    items.insert(0, payload)
    items = items[:ARCHIVE_CAP_PER_KEY]
    _settings_set(f"{ADVICE_ARCHIVE_PREFIX}{key}", json.dumps(items, default=str))
    return aid


def prune_advice_memory() -> dict:
    """
    Pulizia ONE-OFF della memoria advice accumulata (giu 2026: 400 record,
    di cui 93 debrief-log e decine di cloni). Per ogni bucket:
      1. Sposta i DEBRIEF ([Auto]/source=debrief) nell'archivio — sono
         log di run, non lezioni: inquinavano la selezione.
      2. Dedup delle lezioni per similarità di titolo (>= soglia): tiene
         la più recente, somma apply_count, registra dup_count (che alza
         il quality_score della lezione superstite).

    Idempotente nei fatti (dopo il primo giro non trova più nulla da
    spostare/fondere). Il chiamante (main.lifespan) usa un marker per
    non rieseguirla a ogni boot. Ritorna un report per il log.
    """
    report = {"buckets": 0, "archived_logs": 0, "merged_dups": 0, "kept": 0}
    for key in _load_index():
        items = _load_bucket(key)
        if not items:
            continue
        report["buckets"] += 1

        lessons: list[dict] = []
        for it in items:
            title = it.get("title") or ""
            tags = it.get("scenario_tags") or {}
            if title.startswith("[Auto]") or tags.get("source") == "debrief":
                try:
                    save_run_log(it)
                    report["archived_logs"] += 1
                except Exception as e:
                    logger.warning("[SIM-ADVISOR] prune: archive fallita "
                                   "per %s: %s", it.get("id"), e)
                    lessons.append(it)   # non perderla se l'archive fallisce
                continue
            lessons.append(it)

        # Dedup: più recente prima, le successive simili vengono fuse
        lessons.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        kept: list[dict] = []
        for it in lessons:
            merged = False
            for k in kept:
                if _title_similarity(it.get("title", ""), k.get("title", "")) \
                        >= DEDUP_SIMILARITY_THRESHOLD:
                    k["dup_count"] = int(k.get("dup_count") or 0) + 1
                    k["apply_count"] = (int(k.get("apply_count") or 0)
                                        + int(it.get("apply_count") or 0))
                    report["merged_dups"] += 1
                    merged = True
                    break
            if not merged:
                kept.append(it)

        report["kept"] += len(kept)
        _settings_set(f"{ADVICE_KEY_PREFIX}{key}",
                      json.dumps(kept, default=str))
    return report


def delete_advice(advice_id: str, category_key: str) -> bool:
    raw = _settings_get(f"{ADVICE_KEY_PREFIX}{category_key}", "[]")
    try:
        items = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(items, list):
            return False
    except Exception:
        return False
    new_items = [it for it in items if it.get("id") != advice_id]
    if len(new_items) == len(items):
        return False
    _settings_set(f"{ADVICE_KEY_PREFIX}{category_key}", json.dumps(new_items, default=str))
    return True


def list_all_advice() -> dict:
    """Ritorna { category_key: [advice, ...] } per tutta la memoria."""
    out: dict = {}
    for key in _load_index():
        items = load_advice_for_key(key, max_items=50)
        if items:
            out[key] = items
    return out


def increment_apply_count(advice_ids: list[str], category_key: str) -> None:
    """Incrementa apply_count quando degli advice vengono iniettati in un run."""
    if not advice_ids:
        return
    raw = _settings_get(f"{ADVICE_KEY_PREFIX}{category_key}", "[]")
    try:
        items = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(items, list):
            return
    except Exception:
        return
    changed = False
    for it in items:
        if it.get("id") in advice_ids:
            it["apply_count"] = int(it.get("apply_count") or 0) + 1
            changed = True
    if changed:
        _settings_set(f"{ADVICE_KEY_PREFIX}{category_key}",
                      json.dumps(items, default=str))


def format_advice_for_prompt(items: list[dict], category_key: str) -> str:
    """
    Formatta gli advice come blocco di prompt per il Decision Agent del
    Simulator. Iniettato all'inizio del context da runner.py.
    """
    if not items:
        return ""
    lines = [
        "═" * 60,
        f"LEZIONI APPRESE DA RUN PRECEDENTI (categoria: {category_key}):",
        "Sono regole operative emerse dall'analisi di scenari simili.",
        "Considerale come contesto, non come regole assolute — il contesto",
        "attuale potrebbe richiedere deroghe motivate.",
        "Se due lezioni puntano in direzioni OPPOSTE (succede: nascono da",
        "scenari diversi), scegli in base alla condizione che distingue lo",
        "scenario CORRENTE (crash sistemico vs evento singolo già prezzato,",
        "trend vs range) e DICHIARA nel ragionamento quale lezione segui e",
        "perché. Non citarle mai come pretesto per ciò che volevi già fare.",
        "═" * 60,
    ]
    for i, it in enumerate(items, 1):
        title = it.get("title") or "(senza titolo)"
        text = it.get("text") or ""
        lines.append(f"\n[Lezione {i}] {title}")
        lines.append(f"  → {text}")
    lines.append("═" * 60)
    return "\n".join(lines)


# ─── Chat state per run ─────────────────────────────────────────────────────

def load_advisor_chat(run_id: str) -> dict:
    """Carica lo stato della chat advisor per un run. {} se inesistente."""
    raw = _settings_get(f"{ADVISOR_CHAT_KEY_PREFIX}{run_id}", "")
    if not raw:
        return {}
    try:
        return json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception:
        return {}


def save_advisor_chat(run_id: str, state: dict) -> None:
    _settings_set(f"{ADVISOR_CHAT_KEY_PREFIX}{run_id}",
                  json.dumps(state, default=str))


# ─── DeepSeek-R1 chat call ─────────────────────────────────────────────────

ADVISOR_SYSTEM_PROMPT = """Sei l'Investment Process Coach del sistema GeoInvest Simulator.

Il tuo ruolo: analizzare un run del Simulator e proporre consigli concreti per
migliorare il Decision Agent in scenari simili futuri.

I consigli che proponi:
- Sono REGOLE OPERATIVE, non analisi del singolo trade
- Sono GENERALIZZABILI: non riferirti al singolo ticker specifico, ma al
  pattern dello scenario (es. "in regimi macro bear con asset risk-on", non
  "su BTC quando RSI > 70")
- Sono CONCRETE: includono soglie, livelli, condizioni misurabili
- Sono BREVI: ogni consiglio occupa 2-3 righe massimo
- Si riferiscono ai PUNTI DI MIGLIORAMENTO del ragionamento dell'agente:
  conviction calibration, gestione SL/TP, scelta orizzonte, riconoscimento
  pattern, gestione rischio, identificazione tesi invalidata.

═══════════════════════════════════════════════════════════════════════
FORMATO RISPOSTA (OBBLIGATORIO)
═══════════════════════════════════════════════════════════════════════

Prima dai un'analisi discorsiva (3-6 paragrafi) che:
1. Identifica il pattern dello scenario (regime, asset class, dinamica)
2. Valuta la decisione presa: cosa è stato fatto bene, cosa poteva essere migliorato
3. Propone 1-3 consigli operativi (vedi formato JSON sotto)

Alla fine, includi un blocco JSON con i consigli proposti, racchiuso in
```json ... ``` ESATTAMENTE in questo formato:

```json
{
  "proposed_advices": [
    {
      "title": "Titolo breve (max 80 char)",
      "text": "Regola operativa concreta (max 3 righe). Es: 'In regimi macro bear con asset risk-on, ridurre conviction massima a MEDIA anche con setup bullish ottimo, perché le rotation sono fragili.'",
      "rationale": "Perché questo aiuterebbe in scenari simili (max 2 righe)."
    }
  ]
}
```

Se l'utente ti chiede follow-up o di rivedere un consiglio, puoi:
- Discutere senza generare JSON (per chiarimenti)
- Riemettere un blocco JSON aggiornato se proponi advice nuovi/modificati

I consigli vengono salvati MANUALMENTE dall'utente cliccando "Salva" nella UI.
Tu non salvi nulla automaticamente.

LIMITI:
- Non puoi modificare il portafoglio Live né eseguire trade.
- Non inventare numeri non presenti nel run data.
- Se ti mancano dati, dillo esplicitamente."""


def build_run_context(run_data: dict, existing_advices: list[dict]) -> str:
    """
    Costruisce il blocco di contesto da iniettare nel primo messaggio user.
    Include il run completo (decisione, P&L, ragionamento) + advice già
    archiviati per la stessa categoria (per evitare duplicati).
    """
    full = run_data.get("full_data") or {}
    last_decision = full.get("decision_full") or {}

    sections: list[str] = []

    # 1. Identificazione scenario
    scenario_block = {
        "run_id": run_data.get("id"),
        "category": run_data.get("category"),
        "scenario_type": run_data.get("scenario_type"),
        "scenario_id": run_data.get("scenario_id"),
        "historical_period": run_data.get("historical_period"),
        "steps_total": run_data.get("steps"),
    }
    sections.append("SCENARIO:\n" + json.dumps(scenario_block, indent=2,
                                                ensure_ascii=False, default=str))

    # 2. Decisione finale + reasoning
    decision_block = {
        "asset": run_data.get("asset_chosen"),
        "action": run_data.get("action_chosen"),
        "conviction": run_data.get("conviction"),
        "horizon": run_data.get("horizon"),
        "stop_loss_target": last_decision.get("stop_loss_target"),
        "take_profit_target": last_decision.get("take_profit_target"),
        "exit_strategy": last_decision.get("exit_strategy"),
        "risk_identified": full.get("risk_identified", "")[:300],
    }
    sections.append("DECISIONE FINALE:\n" + json.dumps(decision_block, indent=2,
                                                        ensure_ascii=False, default=str))

    # 3. Reasoning (lettura + tesi)
    reading = (full.get("reading_text") or "")[:1500]
    reasoning = (full.get("reasoning_full") or run_data.get("original_thesis", ""))[:2000]
    if reading:
        sections.append("LETTURA DEL CONTESTO:\n" + reading)
    if reasoning:
        sections.append("RAGIONAMENTO DELL'AGENTE:\n" + reasoning)

    # 4. Risultato e benchmark
    result_block = {
        "outcome": run_data.get("outcome"),
        "perf_1w": run_data.get("perf_1w"),
        "perf_1m": run_data.get("perf_1m"),
        "perf_3m": run_data.get("perf_3m"),
        "delta_sp": run_data.get("delta_sp"),
        "delta_sector": run_data.get("delta_sector"),
        "delta_monkey": run_data.get("delta_monkey"),
        "what_happened": (run_data.get("what_happened") or "")[:600],
        "thesis_evaluation": run_data.get("thesis_evaluation"),
    }
    sections.append("RISULTATO:\n" + json.dumps(result_block, indent=2,
                                                  ensure_ascii=False, default=str))

    # 5. SL/TP analysis (se presenti)
    sl_tp = full.get("sl_tp_analysis") or {}
    if sl_tp.get("stop_loss_target") or sl_tp.get("take_profit_target"):
        sections.append("SL/TP ANALYSIS:\n" + json.dumps(sl_tp, indent=2,
                                                          ensure_ascii=False, default=str))

    # 6. Step breakdown (per multi-step)
    steps_data = full.get("steps_data") or []
    if len(steps_data) > 1:
        compact = [{
            "t": s.get("step_index"),
            "act": s.get("action"),
            "asset": s.get("asset"),
            "conv": s.get("conviction"),
            "horiz": s.get("horizon"),
            "sl": s.get("stop_loss_target"),
            "tp": s.get("take_profit_target"),
        } for s in steps_data]
        sections.append("DECISIONI STEP-BY-STEP:\n" + json.dumps(compact, indent=2,
                                                                   ensure_ascii=False, default=str))

    # 7. Advice già archiviati per questa categoria (evita duplicati)
    if existing_advices:
        compact_adv = [{
            "title": a.get("title"),
            "text": a.get("text"),
            "apply_count": a.get("apply_count", 0),
        } for a in existing_advices[:10]]
        sections.append(
            "ADVICE GIÀ IN MEMORIA per questa categoria (NON duplicare, "
            "puoi proporne di nuovi che li integrano o li raffinano):\n"
            + json.dumps(compact_adv, indent=2, ensure_ascii=False, default=str)
        )

    return "\n\n".join(sections)


def _strip_think(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def parse_proposed_advices(response_text: str) -> list[dict]:
    """
    Estrae i blocchi JSON con `proposed_advices` dalla risposta del modello.
    Tollerante: cerca ```json...``` e fallback su qualsiasi {...} con la chiave.
    """
    if not response_text:
        return []

    # Pattern 1: ```json ... ```
    matches = re.findall(r"```json\s*(\{[\s\S]*?\})\s*```", response_text)
    for m in matches:
        try:
            obj = json.loads(m)
            advices = obj.get("proposed_advices")
            if isinstance(advices, list):
                return [a for a in advices if isinstance(a, dict) and a.get("text")]
        except Exception:
            continue

    # Pattern 2: bare JSON object con "proposed_advices"
    m = re.search(r'\{[^{}]*"proposed_advices"\s*:\s*\[[\s\S]*?\]\s*\}', response_text)
    if m:
        try:
            obj = json.loads(m.group(0))
            advices = obj.get("proposed_advices")
            if isinstance(advices, list):
                return [a for a in advices if isinstance(a, dict) and a.get("text")]
        except Exception:
            pass

    return []


def _get_api_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    try:
        import database
        return (database.get_setting("deepseek_api_key", "") or "").strip()
    except Exception:
        return ""


async def chat_with_advisor(
    history: list[dict],
    user_message: str,
    run_context: str,
) -> tuple[str, str, list[dict]]:
    """
    Chiama DeepSeek-R1. Ritorna (response_text, reasoning, proposed_advices).

    history: lista di {role, content} (esclude l'ultimo user_message).
    user_message: messaggio dell'utente attuale.
    run_context: blocco contestuale del run (da iniettare nel primo turno).
    """
    # Toggle Auriko/DeepSeek con fallback (sim_llm). tier="chat" = V3.
    # configs = [auriko, deepseek] se Auriko attivo, [deepseek] altrimenti.
    from sim_llm import get_sim_llm_configs
    configs = get_sim_llm_configs("chat")
    if not configs or not configs[0][1]:
        return ("Nessuna API key LLM configurata (DEEPSEEK_API_KEY o "
                "AURIKO_API_KEY). Impostala in Settings o env.", "", [])

    messages = [{"role": "system", "content": ADVISOR_SYSTEM_PROMPT}]

    # Cap totale del run_context per non saturare il context window
    run_context_capped = (run_context or "")[:12000]

    if not history:
        # Primo turno: il run_context fa da contesto principale
        first_user = (
            f"{run_context_capped}\n\n"
            + "═" * 60 + "\n"
            "PRIMA RICHIESTA: analizza questo run e proponi 1-3 consigli "
            "operativi per migliorare il Decision Agent in scenari simili. "
            "Rispondi nel formato richiesto (analisi discorsiva + JSON proposed_advices)."
        )
        messages.append({"role": "user", "content": first_user})
    else:
        # Turni successivi: replay history (cap a ultimi 6 msg per token saving)
        for m in history[-6:]:
            role = m.get("role")
            if role not in ("user", "assistant"):
                continue
            content = m.get("content", "") or ""
            messages.append({"role": role, "content": content[:6000]})
        reminder = (
            "[CONTEXT REMINDER — riepilogo run per rispondere accuratamente]\n"
            f"{run_context_capped[:6000]}\n\n"
            f"[NUOVO MESSAGGIO UTENTE]\n{user_message}"
        )
        messages.append({"role": "user", "content": reminder})

    logger.info(
        "sim_advisor call: msgs=%d, total_chars=%d, history_len=%d",
        len(messages),
        sum(len(m.get("content", "")) for m in messages),
        len(history),
    )

    # Loop sulle config con fallback automatico: prova Auriko, se fallisce
    # (status!=200 / rete / parsing / timeout) ripiega su DeepSeek diretto.
    from sim_llm import (auriko_attempt_budget, record_auriko_failure,
                          record_auriko_success)
    data = None
    last_err = ""
    for cfg_i, (api_url, api_key, model, provider) in enumerate(configs):
        is_last_cfg = (cfg_i == len(configs) - 1)
        has_fallback = not is_last_cfg
        # FAST-FAIL: Auriko con fallback → timeout breve (50s) anziche'
        # 180s, cosi' la chat advisor non resta appesa su un Auriko lento.
        _, cfg_timeout = auriko_attempt_budget(provider, has_fallback)
        payload = {
            "model": model,
            # V3 supporta temperature → consistency calibrata
            "temperature": 0.5,
            "messages": messages,
            "max_tokens": MAX_RESPONSE_TOKENS,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    api_url, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=cfg_timeout),
                ) as resp:
                    body = await resp.text()
                    if resp.status != 200:
                        logger.error("sim_advisor %s HTTP %d: %s",
                                     provider, resp.status, body[:400])
                        err_msg = f"HTTP {resp.status}"
                        try:
                            ej = json.loads(body)
                            if isinstance(ej.get("error"), dict):
                                err_msg = ej["error"].get("message", err_msg)
                        except Exception:
                            pass
                        last_err = f"{provider}: {err_msg}"
                        if provider == "auriko":
                            record_auriko_failure()
                        if not is_last_cfg:
                            logger.warning("sim_advisor: %s fallito → fallback DeepSeek",
                                           provider)
                        continue
                    try:
                        data = json.loads(body)
                        if provider == "auriko":
                            record_auriko_success()
                        break  # successo
                    except Exception as e:
                        last_err = f"{provider}: risposta non parsabile: {e}"
                        if provider == "auriko":
                            record_auriko_failure()
                        if not is_last_cfg:
                            continue
        except aiohttp.ClientError as e:
            last_err = f"{provider}: errore di rete: {e}"
            if provider == "auriko":
                record_auriko_failure()
            if not is_last_cfg:
                logger.warning("sim_advisor: %s network err → fallback DeepSeek: %s",
                               provider, str(e)[:120])
            continue
        except Exception as e:
            logger.error("sim_advisor %s unexpected error: %s",
                         provider, e, exc_info=True)
            last_err = f"{provider}: errore inatteso: {e}"
            if provider == "auriko":
                record_auriko_failure()
            if not is_last_cfg:
                continue

    if data is None:
        return (f"Errore advisor (tutte le config fallite): {last_err}", "", [])

    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message", {}) or {}
    raw = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or ""
    text = _strip_think(raw)
    if not text:
        text = "(nessuna risposta generata)"
    advices = parse_proposed_advices(text)
    return (text, reasoning, advices)


# ─── Helper esposto al runner per iniettare advice ──────────────────────────

def build_conviction_calibration_block(is_crypto: bool, min_n: int = 10) -> str:
    """
    Tabella calibrazione conviction -> esito dalle run REALI (Step 9a
    analisi 13/07: la conviction ALTA rendeva PEGGIO della MEDIA — P&L
    medio -0.84% vs -0.29% — ma l'agente non poteva saperlo perche' il
    campo salvato era hardcoded MEDIA).

    Conviction a livello di RUN (trade dominante, v. v2_engine.
    aggregate_run_conviction). Classi con meno di min_n run omesse;
    stringa vuota se nessuna classe qualifica (auto-skip: il blocco
    diventa informativo solo quando si accumulano run col fix attivo).
    """
    try:
        from simulator import db as sim_db
        runs = sim_db.list_runs(limit=500) or []
    except Exception as e:
        logger.debug("calibrazione conviction: list_runs fallita: %s", e)
        return ""
    # horizon discrimina il motore sui record light: crypto=2giorni
    want_horizon = "2giorni" if is_crypto else "1settimana"
    per_class: dict[str, dict] = {}
    for r in runs:
        if r.get("horizon") != want_horizon:
            continue
        conv = str(r.get("conviction") or "").upper()
        if conv not in ("ALTA", "MEDIA", "BASSA"):
            continue
        d = per_class.setdefault(conv, {"n": 0, "green": 0, "pnl": 0.0})
        d["n"] += 1
        d["green"] += 1 if r.get("outcome") == "green" else 0
        try:
            d["pnl"] += float(r.get("perf_1m") or 0)
        except (TypeError, ValueError):
            pass
    lines = []
    for conv in ("ALTA", "MEDIA", "BASSA"):
        d = per_class.get(conv)
        if not d or d["n"] < min_n:
            continue
        lines.append(f"  • {conv}: {d['n']} run, verdi "
                     f"{d['green'] / d['n'] * 100:.0f}%, P&L medio "
                     f"{d['pnl'] / d['n'] * 100:+.2f}%")
    if not lines:
        return ""
    return ("CALIBRAZIONE CONVICTION (dalle tue run passate; conviction "
            "a livello di RUN = trade dominante):\n" + "\n".join(lines) +
            "\n  → Se la tua ALTA non rende più della tua MEDIA, la "
            "conviction dichiarata è mal calibrata: alza l'asticella "
            "per dichiarare ALTA.")


def get_advice_block_for_runner(scenario: dict, max_items: int = 5) -> tuple[str, str, list[str]]:
    """
    Chiamato da runner.py all'inizio di un run. Ritorna:
      (formatted_block_for_prompt, category_key, list_of_advice_ids_used)

    Il runner inietta `formatted_block_for_prompt` nel system prompt e poi
    chiama `increment_apply_count(advice_ids, category_key)` per tracking.
    """
    try:
        category_key, _tags = detect_scenario_key(scenario)
    except Exception as e:
        logger.warning("get_advice_block_for_runner: detect failed: %s", e)
        return "", "", []

    items = load_advice_for_key(category_key, max_items=max_items)
    if not items:
        return "", category_key, []

    block = format_advice_for_prompt(items, category_key)
    ids = [it.get("id") for it in items if it.get("id")]
    return block, category_key, ids
