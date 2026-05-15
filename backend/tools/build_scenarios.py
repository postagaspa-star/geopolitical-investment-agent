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

# Toggle Auriko/DeepSeek INLINE (non importa sim_llm: questo script gira su
# GitHub Actions con cwd != backend/, quindi resta standalone). Stessa
# semantica di backend/sim_llm.py: AURIKO_API_KEY presente → Auriko gateway,
# assente → DeepSeek diretto (default, comportamento storico).
_AURIKO_KEY = os.environ.get("AURIKO_API_KEY", "").strip()
if _AURIKO_KEY:
    _AURIKO_BASE = os.environ.get(
        "AURIKO_BASE_URL", "https://api.auriko.ai/v1"
    ).strip().rstrip("/")
    DEEPSEEK_API_URL = f"{_AURIKO_BASE}/chat/completions"
    DEEPSEEK_MODEL = os.environ.get("AURIKO_MODEL_V3", "deepseek-chat").strip()
    _LLM_PROVIDER = "auriko"
else:
    DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
    DEEPSEEK_MODEL = "deepseek-chat"   # V3 — economico per generazione strutturata
    _LLM_PROVIDER = "deepseek"

GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_QUERY = (
    'theme:ECON_INTEREST_RATE OR theme:ECON_INFLATION OR '
    'theme:GENERAL_GOVERNMENT OR theme:WB_2024_ENERGY OR '
    'theme:DEMOCRACY OR theme:ECON_STOCKMARKET'
)

# Target/run leggibile da env per consentire override da workflow_dispatch.
# Default 5: leggermente sovrastimato per coprire rejection durante validazione
# (target 5 → tipicamente 3-4 validi dopo validation rigorosa).
NUM_SCENARIOS_TARGET = int(os.environ.get("NUM_SCENARIOS_TARGET", "5") or "5")

# Guarantee minimo: se dopo validazione + retry abbiamo < di questo, exit fail.
# Sotto questa soglia il run è considerato un fallimento perché non garantisce
# nuovi scenari sufficienti per la giornata.
MIN_VALID_SCENARIOS = int(os.environ.get("MIN_VALID_SCENARIOS", "2") or "2")

NUM_NEWS_TO_PASS = 25      # news passate al modello come contesto

# Categorie scenari per garantire diversificazione
CATEGORY_THEMES = {
    "normale": "movimento di mercato standard senza catalyst dominante",
    "geopolitico": "shock geopolitico (conflitto/sanzioni/supply chain)",
    "macro": "evento macro (CPI/Fed/NFP/GDP/payrolls)",
    "crash_rally": "spike di volatilità (crash o relief rally)",
}


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


def _llm_configs_inline() -> list[tuple]:
    """
    Lista config (url, key, model, provider) con fallback inline.
    Auriko attivo → [auriko, deepseek]; altrimenti → [deepseek].
    Standalone: questo script gira su GitHub Actions, non importa sim_llm.
    """
    ds_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    ds_cfg = ("https://api.deepseek.com/v1/chat/completions",
              ds_key, "deepseek-chat", "deepseek")
    if _LLM_PROVIDER == "auriko" and _AURIKO_KEY:
        auriko_cfg = (DEEPSEEK_API_URL, _AURIKO_KEY, DEEPSEEK_MODEL, "auriko")
        # Fallback a DeepSeek diretto solo se la chiave DeepSeek esiste
        return [auriko_cfg, ds_cfg] if ds_key else [auriko_cfg]
    return [ds_cfg]


async def generate_scenarios_via_llm(session: aiohttp.ClientSession,
                                      news_text: str) -> list[dict]:
    """Genera scenari via V3 (DeepSeek o Auriko) con fallback automatico."""
    configs = _llm_configs_inline()
    if not configs or not configs[0][1]:
        raise RuntimeError(
            "Nessuna API key LLM configurata (DEEPSEEK_API_KEY o AURIKO_API_KEY)"
        )

    user_msg = (
        f"NEWS DELLE ULTIME 24H (top {NUM_NEWS_TO_PASS} via GDELT):\n\n"
        f"{news_text}\n\n"
        f"Produci {NUM_SCENARIOS_TARGET} scenari secondo lo schema specificato."
    )

    content = ""
    last_err = ""
    for cfg_i, (api_url, api_key, model, provider) in enumerate(configs):
        is_last_cfg = (cfg_i == len(configs) - 1)
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SCENARIO_GENERATOR_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0.6,
            "max_tokens": 6000,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        logger.info("Chiamata LLM provider=%s model=%s (news_chars=%d)...",
                    provider, model, len(news_text))
        try:
            async with session.post(
                api_url, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=180),
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    last_err = f"{provider} HTTP {resp.status}: {body[:300]}"
                    if not is_last_cfg:
                        logger.warning("%s fallito → fallback DeepSeek diretto",
                                        provider)
                        continue
                    raise RuntimeError(last_err)
                data = json.loads(body)
        except aiohttp.ClientError as e:
            last_err = f"{provider} network: {e}"
            if not is_last_cfg:
                logger.warning("%s network err → fallback DeepSeek: %s",
                                provider, str(e)[:120])
                continue
            raise RuntimeError(last_err)

        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        if content:
            break
        last_err = f"{provider}: content vuoto"
        if not is_last_cfg:
            logger.warning("%s content vuoto → fallback DeepSeek", provider)
            continue

    if not content:
        raise RuntimeError(f"Generazione fallita (tutte le config): {last_err}")

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


async def _generate_and_validate(session: aiohttp.ClientSession,
                                  news_text: str,
                                  attempt_label: str) -> list[dict]:
    """
    Single generation+validation attempt. Ritorna la lista di scenari validi.
    Separato in funzione per consentire retry con prompt rinforzato.
    """
    try:
        raw_scenarios = await generate_scenarios_via_llm(session, news_text)
    except Exception as e:
        logger.error("[%s] Generazione LLM fallita: %s", attempt_label, e)
        return []

    valid: list[dict] = []
    for i, s in enumerate(raw_scenarios):
        try:
            s = normalize_scenario(dict(s), i)
            ok, err = validate_scenario(s)
            if not ok:
                logger.warning("[%s] Scenario #%d scartato: %s", attempt_label, i, err)
                continue
            valid.append(s)
        except Exception as e:
            logger.warning("[%s] Errore normalizzazione scenario #%d: %s",
                            attempt_label, i, e)

    logger.info("[%s] Validi: %d/%d (rejection rate %.0f%%)",
                attempt_label, len(valid), len(raw_scenarios),
                100 * (1 - len(valid) / max(1, len(raw_scenarios))))
    return valid


def _fallback_news_brief() -> str:
    """
    Fallback news context quando GDELT è giù o non risponde. Usa temi
    macro/geo strutturali sempre rilevanti come "contesto sintetico" per
    permettere al modello di generare scenari anche senza news fresche.
    """
    return (
        "(GDELT non disponibile — usa i seguenti temi macro/geo strutturali "
        "come base per scenari plausibili)\n"
        "- Federal Reserve policy uncertainty (tassi, QT)\n"
        "- US-China tech/trade tensions (semiconductor sanctions, Taiwan)\n"
        "- Energy market volatility (OPEC+, Russia, Middle East)\n"
        "- Inflation dynamics (CPI, PPI, energy passthrough)\n"
        "- Geopolitical risk (Middle East, Eastern Europe, South China Sea)\n"
        "- Tech sector rotation (AI capex, regulatory, antitrust)\n"
        "- Banking sector stress (commercial real estate, deposits)\n"
        "- Crypto regulatory (SEC actions, ETF flows, stablecoins)\n"
    )


# ─── Main ──────────────────────────────────────────────────────────────────

async def main():
    # Codici di uscita per diagnosi rapida nei log GitHub:
    #   10 = config error (env vars mancanti)
    #   20 = generation error (DeepSeek + retry falliti)
    #   30 = upload error
    #   40 = insufficient valid scenarios (< MIN_VALID_SCENARIOS)
    base_url = os.environ.get("GEOINVEST_API_BASE_URL", "").strip().rstrip("/")
    token = os.environ.get("SCENARIO_UPLOAD_TOKEN", "").strip()

    if not base_url:
        logger.error("GEOINVEST_API_BASE_URL non configurato")
        sys.exit(10)
    if not token:
        logger.error("SCENARIO_UPLOAD_TOKEN non configurato")
        sys.exit(10)
    if not os.environ.get("DEEPSEEK_API_KEY", "").strip():
        logger.error("DEEPSEEK_API_KEY non configurato")
        sys.exit(10)

    logger.info("=" * 60)
    logger.info("Scenario Generator avviato")
    logger.info("  Target/run: %d", NUM_SCENARIOS_TARGET)
    logger.info("  Min guarantee: %d", MIN_VALID_SCENARIOS)
    logger.info("  Backend: %s", base_url)
    logger.info("=" * 60)

    async with aiohttp.ClientSession() as session:
        # ── 1. Fetch news (con fallback se GDELT vuoto/down) ─────────────
        articles = await fetch_gdelt_news(session)
        if articles:
            news_text = _summarize_news(articles)
            logger.info("GDELT OK: %d articoli, %d char contesto",
                        len(articles), len(news_text))
        else:
            logger.warning("GDELT non disponibile — uso fallback news brief")
            news_text = _fallback_news_brief()

        # ── 2. Prima generazione ─────────────────────────────────────────
        valid = await _generate_and_validate(session, news_text, "attempt-1")

        # ── 3. Retry se sotto guarantee (con prompt rinforzato/contesto extra)
        if len(valid) < MIN_VALID_SCENARIOS:
            logger.warning(
                "Sotto MIN_VALID_SCENARIOS (%d < %d) — retry con contesto rinforzato",
                len(valid), MIN_VALID_SCENARIOS,
            )
            # Per il retry: combina news GDELT + fallback brief per dare al
            # modello più materiale, e includi una richiesta esplicita di
            # diversità categoriale.
            reinforced = (
                news_text + "\n\n" + _fallback_news_brief() +
                "\n\nIMPORTANTE: l'attempt precedente ha prodotto pochi scenari "
                "validi. Sii più rigoroso sullo schema: ogni scenario DEVE "
                "avere asset_universe >= 5, market_data >= 5 con price_t0 > 0, "
                "headlines >= 3, description_reveal >= 30 char. Diversifica "
                "le categorie (normale/geopolitico/macro/crash_rally)."
            )
            extra = await _generate_and_validate(session, reinforced, "attempt-2")
            # Dedup per id (improbabile collision ma safe)
            existing_ids = {s["id"] for s in valid}
            for s in extra:
                if s["id"] not in existing_ids:
                    valid.append(s)
                    existing_ids.add(s["id"])

        if len(valid) == 0:
            logger.error("Nessuno scenario valido generato dopo retry — abort")
            sys.exit(20)

        if len(valid) < MIN_VALID_SCENARIOS:
            logger.error(
                "Solo %d scenari validi dopo retry, sotto guarantee %d — abort",
                len(valid), MIN_VALID_SCENARIOS,
            )
            sys.exit(40)

        logger.info("✓ %d scenari validi pronti per upload (>= guarantee %d)",
                    len(valid), MIN_VALID_SCENARIOS)

        # ── 4. POST ────────────────────────────────────────────────────
        try:
            result = await post_scenarios(session, base_url, token, valid)
        except Exception as e:
            logger.error("Upload fallito: %s", e)
            sys.exit(30)

        accepted = result.get("accepted", 0)
        rejected = result.get("rejected", 0)
        logger.info("=" * 60)
        logger.info("RISULTATO: %d accettati, %d rifiutati dal backend",
                    accepted, rejected)
        logger.info("=" * 60)
        if rejected:
            for r in result.get("rejected_reasons", []):
                logger.warning("  rifiutato %s: %s", r.get("id"), r.get("reason"))

        if accepted == 0:
            logger.error("Tutti gli scenari rifiutati dal backend (validazione server)")
            sys.exit(30)

        if accepted < MIN_VALID_SCENARIOS:
            logger.warning(
                "Solo %d accettati dal backend (sotto guarantee %d) "
                "— il run ha aggiunto meno scenari del minimo richiesto",
                accepted, MIN_VALID_SCENARIOS,
            )
            # Non sys.exit fail qui: ALMENO 1 scenario è stato aggiunto.
            # Il workflow successivo sopperisce.

        # Print degli ID accettati per audit nei log
        for aid in result.get("accepted_ids", []):
            logger.info("  ✓ %s", aid)

        logger.info("Generator completato con successo.")


if __name__ == "__main__":
    asyncio.run(main())
