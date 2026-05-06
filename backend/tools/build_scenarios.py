"""
Daily scenario generator per GeoInvest AI Simulator.

Eseguito da GitHub Actions ogni giorno alle 06:00 UTC. Produce 2-3 scenari
freschi basati sulle news macro/geo dell'ultimo giorno e li carica
sull'endpoint POST /api/simulator/scenarios/dynamic.

Pipeline:
  1. Fetch top news GDELT 24h (free, no key)
  2. Filtro temi rilevanti (macro, geo, mercati)
  3. Chiama DeepSeek-V3 con prompt strutturato → 2-3 scenari JSON
  4. Validazione client-side per scartare malformati prima del POST
  5. POST autenticato all'endpoint Render

Costo per esecuzione: ~$0.001 (DeepSeek-V3 è ~10× più economico di Sonnet).

Env vars richieste:
  - DEEPSEEK_API_KEY:        chiave API DeepSeek
  - SCENARIO_UPLOAD_TOKEN:   token condiviso con il backend (header auth)
  - GEOINVEST_API_BASE_URL:  base URL del backend (es. https://geoinvest.onrender.com)

Uso manuale (test locale):
  DEEPSEEK_API_KEY=... SCENARIO_UPLOAD_TOKEN=... GEOINVEST_API_BASE_URL=... \
      python backend/tools/build_scenarios.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Any

import aiohttp
import asyncio

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("scenario_generator")

# ─── Configurazione ─────────────────────────────────────────────────────────

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"   # V3 — economico per generazione strutturata

GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_QUERY = (
    'theme:ECON_INTEREST_RATE OR theme:ECON_INFLATION OR '
    'theme:GENERAL_GOVERNMENT OR theme:WB_2024_ENERGY OR '
    'theme:DEMOCRACY OR theme:ECON_STOCKMARKET'
)

NUM_SCENARIOS_TARGET = 3   # quanti scenari produrre/giorno
NUM_NEWS_TO_PASS = 25      # news passate al modello come contesto


# ─── 1. Fetch news GDELT ────────────────────────────────────────────────────

async def fetch_gdelt_news(session: aiohttp.ClientSession,
                            num: int = NUM_NEWS_TO_PASS) -> list[dict]:
    """Fetch top news macro/geo via GDELT (free, no key)."""
    params = {
        "query": GDELT_QUERY,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": num,
        "sort": "DateDesc",
        "timespan": "1d",
    }
    try:
        async with session.get(
            GDELT_DOC_URL, params=params,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                logger.warning("GDELT HTTP %d", resp.status)
                return []
            text = await resp.text()
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                logger.warning("GDELT response non-JSON (%d char)", len(text))
                return []
            articles = data.get("articles", []) if isinstance(data, dict) else []
            logger.info("GDELT: %d articoli ricevuti", len(articles))
            return articles
    except Exception as e:
        logger.warning("GDELT fetch fallito: %s", e)
        return []


def _summarize_news(articles: list[dict], max_items: int = 25) -> str:
    """Compatta news GDELT in formato compatto per il prompt LLM."""
    if not articles:
        return "(nessuna news disponibile)"
    lines = []
    for a in articles[:max_items]:
        title = (a.get("title") or "").strip()
        source = (a.get("sourcecountry") or "").strip()
        if not title:
            continue
        if len(title) > 140:
            title = title[:140] + "..."
        suffix = f" [{source}]" if source else ""
        lines.append(f"- {title}{suffix}")
    return "\n".join(lines) if lines else "(news non parsabili)"


# ─── 2. Genera scenari via DeepSeek-V3 ──────────────────────────────────────

SCENARIO_GENERATOR_PROMPT = """Sei uno Scenario Designer per un Simulator di trading didattico.

Il tuo compito: produrre 2-3 SCENARI di mercato CONCRETI basati sulle news macro/geo
delle ultime 24h. Ogni scenario rappresenta una situazione di mercato in cui un
agente AI dovrà prendere decisioni di trading senza conoscere il periodo storico.

REGOLE per ogni scenario:
- DIVERSIFICA le categorie: produci almeno 2 scenari di categorie DIVERSE
  (categorie disponibili: 'normale', 'geopolitico', 'macro', 'crash_rally').
- TICKER: usa SOLO ticker reali del mercato US (S&P 500 large cap), commodity ETF
  (GLD, SLV, USO), e crypto top (BTC-USD, ETH-USD, SOL-USD). NO ticker non quotati,
  NO indici (SPY/QQQ).
- PREZZI: usa livelli plausibili noti da training. Se incerto, scegli ticker
  con prezzi prevedibili (mega-cap stabili). I change_24h e change_7d devono
  riflettere il TEMA dello scenario (es. shock geo → -3% / -8% sui risk-on).
- HEADLINES: 5-7 headline brevi (max 120 char), realistiche e coerenti col tema.
  USA INGLESE. Niente date esplicite nelle headline.
- DESCRIPTION_REVEAL: 2-4 frasi che spiegano cosa è successo davvero nel periodo
  ipotetico. Italiano OK.

OUTPUT: JSON puro (NO markdown, NO commenti). Schema esatto:

{
  "scenarios": [
    {
      "id": "dyn-{YYYY-MM-DD}-{slug-tema}-{N}",
      "category": "normale|geopolitico|macro|crash_rally",
      "title": "Titolo breve (max 70 char)",
      "brief": "Descrizione neutra non rivelativa (max 140 char)",
      "asset_universe": ["TICK1", "TICK2", ...] (8-10 ticker),
      "headlines": ["...", "..."] (5-7 frasi),
      "market_data": [
        {"ticker": "TICK1", "price_t0": 123.45, "change_24h": 0.5, "change_7d": 2.1}
      ] (almeno 8 entries, tutte con price_t0 > 0),
      "description_reveal": "Cosa è successo in 2-4 frasi italiano."
    }
  ]
}

VINCOLI HARD:
- Almeno 2 scenari di categorie DIVERSE
- Ogni scenario: asset_universe >= 5, market_data >= 5, headlines >= 3
- price_t0 sempre > 0
- description_reveal sempre >= 30 caratteri
- Nessun preambolo prima del JSON
"""


def _new_scenario_id(category: str, slug: str, idx: int) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    safe_slug = "".join(c if c.isalnum() else "-" for c in slug.lower())[:25]
    safe_slug = safe_slug.strip("-") or "topic"
    return f"dyn-{today}-{safe_slug}-{idx}"


async def generate_scenarios_via_llm(session: aiohttp.ClientSession,
                                      news_text: str) -> list[dict]:
    """Chiama DeepSeek-V3 e parsea i scenari JSON."""
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY non configurata")

    user_msg = (
        f"NEWS DELLE ULTIME 24H (top {NUM_NEWS_TO_PASS} via GDELT):\n\n"
        f"{news_text}\n\n"
        f"Produci {NUM_SCENARIOS_TARGET} scenari secondo lo schema specificato."
    )

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": SCENARIO_GENERATOR_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.6,   # un po' di varieta' tra scenari, no random extreme
        "max_tokens": 6000,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    logger.info("Chiamata DeepSeek-V3 (model=%s, news_chars=%d)...",
                DEEPSEEK_MODEL, len(news_text))

    async with session.post(
        DEEPSEEK_API_URL, json=payload, headers=headers,
        timeout=aiohttp.ClientTimeout(total=180),
    ) as resp:
        body = await resp.text()
        if resp.status != 200:
            raise RuntimeError(f"DeepSeek HTTP {resp.status}: {body[:300]}")
        data = json.loads(body)

    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    if not content:
        raise RuntimeError("DeepSeek ha ritornato content vuoto")

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        # Tentativo di estrazione fallback: cerca primo {...} nel testo
        import re
        m = re.search(r"\{[\s\S]*\}", content)
        if not m:
            raise RuntimeError(f"Impossibile parsare JSON: {e}\nContent: {content[:500]}")
        parsed = json.loads(m.group(0))

    scenarios = parsed.get("scenarios", []) if isinstance(parsed, dict) else []
    if not isinstance(scenarios, list):
        raise RuntimeError(f"Schema non valido: 'scenarios' non e' una lista")

    logger.info("DeepSeek ha prodotto %d scenari", len(scenarios))
    return scenarios


# ─── 3. Validazione client-side (mirror del backend) ───────────────────────

VALID_CATEGORIES = {"normale", "geopolitico", "macro", "crash_rally"}


def validate_scenario(s: dict) -> tuple[bool, str]:
    """Validazione locale prima del POST per evitare round-trip inutili."""
    if not isinstance(s, dict):
        return False, "non e' un dict"
    if not s.get("id"):
        return False, "id mancante"
    if s.get("category") not in VALID_CATEGORIES:
        return False, f"category '{s.get('category')}' non valida"
    if not s.get("title") or len(s["title"]) < 5:
        return False, "title troppo breve"
    au = s.get("asset_universe") or []
    if not isinstance(au, list) or len(au) < 5:
        return False, f"asset_universe insufficiente ({len(au)} < 5)"
    headlines = s.get("headlines") or []
    if not isinstance(headlines, list) or len(headlines) < 3:
        return False, f"headlines insufficienti ({len(headlines)} < 3)"
    md = s.get("market_data") or []
    if not isinstance(md, list) or len(md) < 5:
        return False, f"market_data insufficiente ({len(md)} < 5)"
    for entry in md:
        if not isinstance(entry, dict):
            return False, "market_data entry non-dict"
        if not entry.get("ticker"):
            return False, "ticker mancante in market_data"
        p = entry.get("price_t0")
        if not isinstance(p, (int, float)) or p <= 0:
            return False, f"price_t0 non valido per {entry.get('ticker')}"
    reveal = s.get("description_reveal", "")
    if len(reveal) < 30:
        return False, "description_reveal troppo breve"
    return True, ""


def normalize_scenario(s: dict, idx: int) -> dict:
    """Normalizza/arricchisce lo scenario prima del POST."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Forza id univoco con timestamp
    if not s.get("id") or not s["id"].startswith("dyn-"):
        slug = s.get("title", "topic").split()[0] if s.get("title") else "topic"
        s["id"] = _new_scenario_id(s.get("category", "normale"), slug, idx)

    # Tag source con data
    s["source"] = f"dynamic_{today}"

    # Default expires_at: +30 giorni
    if not s.get("expires_at"):
        exp = datetime.now(timezone.utc) + timedelta(days=30)
        s["expires_at"] = exp.isoformat()

    # Brief: se mancante, deriva dal title
    if not s.get("brief"):
        s["brief"] = s.get("title", "")[:140]

    # Periodi storici: lascia null se non specificati (sono opzionali)
    return s


# ─── 4. POST batch all'endpoint backend ─────────────────────────────────────

async def post_scenarios(session: aiohttp.ClientSession,
                          base_url: str, token: str,
                          scenarios: list[dict]) -> dict:
    """POST autenticato del batch scenari all'endpoint."""
    url = f"{base_url.rstrip('/')}/api/simulator/scenarios/dynamic"
    headers = {
        "Content-Type": "application/json",
        "X-Scenario-Token": token,
    }
    payload = {"scenarios": scenarios}

    logger.info("POST %s con %d scenari...", url, len(scenarios))

    # Retry: 3 tentativi con backoff su 5xx/network errors
    last_err = None
    for attempt in range(3):
        try:
            async with session.post(
                url, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                body = await resp.text()
                if resp.status == 200:
                    return json.loads(body) if body else {}
                if 400 <= resp.status < 500:
                    # Errore client (auth, validazione): no retry
                    raise RuntimeError(f"POST rejected HTTP {resp.status}: {body[:300]}")
                last_err = f"HTTP {resp.status}: {body[:200]}"
        except Exception as e:
            last_err = str(e)
        if attempt < 2:
            logger.warning("Tentativo %d fallito (%s), retry in %ds...",
                            attempt + 1, last_err, 2 * (attempt + 1))
            await asyncio.sleep(2 * (attempt + 1))

    raise RuntimeError(f"POST fallito dopo 3 tentativi: {last_err}")


# ─── Main ──────────────────────────────────────────────────────────────────

async def main():
    base_url = os.environ.get("GEOINVEST_API_BASE_URL", "").strip().rstrip("/")
    token = os.environ.get("SCENARIO_UPLOAD_TOKEN", "").strip()

    if not base_url:
        logger.error("GEOINVEST_API_BASE_URL non configurato")
        sys.exit(1)
    if not token:
        logger.error("SCENARIO_UPLOAD_TOKEN non configurato")
        sys.exit(1)
    if not os.environ.get("DEEPSEEK_API_KEY", "").strip():
        logger.error("DEEPSEEK_API_KEY non configurato")
        sys.exit(1)

    async with aiohttp.ClientSession() as session:
        # 1. Fetch news
        articles = await fetch_gdelt_news(session)
        news_text = _summarize_news(articles)

        # 2. Genera via LLM
        try:
            raw_scenarios = await generate_scenarios_via_llm(session, news_text)
        except Exception as e:
            logger.error("Generazione LLM fallita: %s", e)
            sys.exit(1)

        # 3. Valida e normalizza
        valid: list[dict] = []
        for i, s in enumerate(raw_scenarios):
            try:
                s = normalize_scenario(dict(s), i)
                ok, err = validate_scenario(s)
                if not ok:
                    logger.warning("Scenario #%d scartato: %s", i, err)
                    continue
                valid.append(s)
            except Exception as e:
                logger.warning("Errore normalizzazione scenario #%d: %s", i, e)

        if not valid:
            logger.error("Nessuno scenario valido generato — abort")
            sys.exit(1)

        logger.info("%d scenari validi pronti per upload", len(valid))

        # 4. POST
        try:
            result = await post_scenarios(session, base_url, token, valid)
        except Exception as e:
            logger.error("Upload fallito: %s", e)
            sys.exit(1)

        accepted = result.get("accepted", 0)
        rejected = result.get("rejected", 0)
        logger.info("Risultato: %d accettati, %d rifiutati", accepted, rejected)
        if rejected:
            for r in result.get("rejected_reasons", []):
                logger.warning("  rifiutato %s: %s", r.get("id"), r.get("reason"))

        if accepted == 0:
            logger.error("Tutti gli scenari sono stati rifiutati dal backend")
            sys.exit(1)

        logger.info("Generator completato con successo.")


if __name__ == "__main__":
    asyncio.run(main())
