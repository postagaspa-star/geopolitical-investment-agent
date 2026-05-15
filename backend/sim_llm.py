"""
sim_llm.py — toggle endpoint LLM per il SOLO Simulator (DeepSeek diretto
vs Auriko gateway).

Razionale: il Simulator usa esclusivamente DeepSeek (R1 per i run, V3 per
advisor/generator) — zero Anthropic, quindi nessun rischio sul prompt
caching nativo Anthropic. È il sandbox ideale per testare Auriko su
traffico reale ad alto volume (~40-50 run/giorno) ma a rischio finanziario
ZERO (paper trading su scenari storici).

Toggle via env var, reversibilità totale:
  - AURIKO_API_KEY presente  → il Simulator instrada via Auriko gateway
  - AURIKO_API_KEY assente    → path diretto DeepSeek (comportamento
                                identico a prima, zero rischio)

Per tornare a DeepSeek: rimuovi AURIKO_API_KEY + restart. Nessuna
modifica al codice necessaria.

Env vars opzionali:
  - AURIKO_BASE_URL    (default https://api.auriko.ai/v1)
  - AURIKO_MODEL_R1    (default deepseek-reasoner) — nome modello R1 lato gateway
  - AURIKO_MODEL_V3    (default deepseek-chat)     — nome modello V3 lato gateway

Fatti verificati dalla doc ufficiale (docs.auriko.ai):
  - Base URL: https://api.auriko.ai/v1 — endpoint /chat/completions
  - Auth: header "Authorization: Bearer ak_..." (le chiavi Auriko
    iniziano con "ak_"). Stesso schema Bearer di DeepSeek → drop-in.
  - 100% OpenAI-compatible: payload {model, messages, max_tokens, ...}
    identico. Il codice esistente che parsa
    data["choices"][0]["message"]["content"] continua a funzionare.
  - I canonical model ID sono provider-agnostic SENZA prefisso provider
    (es. "gpt-4o", "claude-sonnet-4-6"). Gli ID DeepSeek esatti NON
    sono nella doc statica: vanno letti live da GET /v1/directory/models
    con la propria chiave. Per questo il nome modello e' configurabile
    via env: se "deepseek-reasoner"/"deepseek-chat" non sono gli ID
    canonici giusti, basta correggere AURIKO_MODEL_R1/V3 — zero deploy.
  - La response Auriko include un campo extra "routing_metadata" con
    provider, provider_model_id, ttft_ms e cost.usd → utile per l'A/B
    test sui costi. Viene ignorato dal parsing esistente (no rischio).
  - Prompt caching Auriko: supportato per Anthropic/OpenAI/Fireworks/xAI.
    DeepSeek NON e' nella lista → per il Simulator non c'e' prompt
    caching via gateway, ma DeepSeek ha context caching server-side
    suo (lato deepseek.com). Se Auriko instrada DeepSeek a un host
    alternativo (Fireworks/Together) quel caching potrebbe non esserci:
    e' uno dei dati che l'A/B test sul Simulator deve misurare.

ATTENZIONE: questo test sul Simulator NON valida il prompt caching
Anthropic (il Simulator non usa Claude). Prima di mettere il Decision
Agent (Sonnet 4.5) su Auriko serve un test SEPARATO e dedicato.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Endpoint/modelli DeepSeek diretti (default, comportamento storico)
_DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
_DEEPSEEK_R1 = "deepseek-reasoner"
_DEEPSEEK_V3 = "deepseek-chat"


def _get_deepseek_key() -> str:
    """Chiave DeepSeek da env o DB settings (stesso pattern dei moduli sim)."""
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    try:
        import database
        return (database.get_setting("deepseek_api_key", "") or "").strip()
    except Exception:
        return ""


def get_sim_llm_config(tier: str) -> tuple[str, str, str, str]:
    """
    Ritorna (api_url, api_key, model_name, provider) per il Simulator.

    tier:
      - "reasoner" / "r1"  → modello reasoning (run scenari, v2_engine)
      - "chat" / "v3"      → modello chat (advisor, generator scenari)

    Se AURIKO_API_KEY è configurata → instrada via Auriko (OpenAI-compatible,
    payload identico). Altrimenti → DeepSeek diretto (default).

    provider è "auriko" o "deepseek" — usalo per logging/A-B comparison.
    """
    is_reasoner = str(tier).lower() in ("reasoner", "r1", "deepseek-reasoner")

    auriko_key = os.environ.get("AURIKO_API_KEY", "").strip()
    if auriko_key:
        base = os.environ.get(
            "AURIKO_BASE_URL", "https://api.auriko.ai/v1"
        ).strip().rstrip("/")
        url = f"{base}/chat/completions"
        if is_reasoner:
            model = os.environ.get("AURIKO_MODEL_R1", _DEEPSEEK_R1).strip()
        else:
            model = os.environ.get("AURIKO_MODEL_V3", _DEEPSEEK_V3).strip()
        # Log INFO solo quando Auriko è attivo (visibilità A/B test).
        # Quando è DeepSeek diretto NON logga → zero rumore, comportamento
        # storico invariato.
        logger.info("[SIM-LLM] routing via AURIKO gateway (tier=%s model=%s url=%s)",
                    tier, model, url)
        return url, auriko_key, model, "auriko"

    # Default: DeepSeek diretto — comportamento storico, rischio zero.
    key = _get_deepseek_key()
    model = _DEEPSEEK_R1 if is_reasoner else _DEEPSEEK_V3
    return _DEEPSEEK_URL, key, model, "deepseek"


def get_sim_llm_configs(tier: str) -> list[tuple[str, str, str, str]]:
    """
    Ritorna la LISTA ordinata di config da provare, per il fallback
    automatico:

      - Se Auriko e' attivo (AURIKO_API_KEY presente):
          [ (auriko...), (deepseek diretto...) ]
        → si prova Auriko; se fallisce TUTTI i retry (gateway down,
          modello sbagliato, auth, 5xx, timeout) si ripiega su DeepSeek
          diretto invece di far fallire il run del Simulator.

      - Se Auriko NON e' attivo:
          [ (deepseek diretto...) ]
        → comportamento storico, nessun fallback (gia' su DeepSeek).

    Il chiamante itera la lista: prova la config[0], se esaurisce i
    retry passa alla config[1], ecc. Solo se TUTTE falliscono → errore.
    """
    is_reasoner = str(tier).lower() in ("reasoner", "r1", "deepseek-reasoner")

    primary = get_sim_llm_config(tier)
    if primary[3] == "auriko":
        # Fallback esplicito: DeepSeek diretto (bypass gateway)
        ds_key = _get_deepseek_key()
        ds_model = _DEEPSEEK_R1 if is_reasoner else _DEEPSEEK_V3
        deepseek_fallback = (_DEEPSEEK_URL, ds_key, ds_model, "deepseek")
        # Includi il fallback solo se la chiave DeepSeek esiste davvero
        if ds_key:
            return [primary, deepseek_fallback]
        logger.warning(
            "[SIM-LLM] Auriko attivo ma DEEPSEEK_API_KEY assente: "
            "nessun fallback disponibile se Auriko fallisce"
        )
        return [primary]
    # Gia' su DeepSeek diretto: nessun fallback (sarebbe se stesso)
    return [primary]
