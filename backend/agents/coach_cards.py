"""
Coach Cards — sintesi settimanale degli advice del Simulator → schede
operative per il bot Live.

Una volta a settimana (chiamato dallo scheduler), un'AI legge tutti i
consigli accumulati nella memoria del Sim Advisor e produce 3-5 "Coach
Cards" — ognuna è una raccomandazione operativa generale ("in regimi
macro bear su risk-on, riduci conviction massima a MEDIA") che il
Decision Agent Live legge come reminder durante i suoi run.

Storage: sim_settings (no DDL).
  _coach_card::list                         JSON list di card_id ordinate
  _coach_card::card::{id}                   JSON card singola
  _coach_card::last_synth_at                ISO timestamp ultimo run
  _coach_card::synth_status                 "ok" | "running" | "error"

Schema card:
  {
    "id": uuid,
    "week_of": "2026-05-04",
    "category_focus": str,        # categoria che ha generato la card
    "title": str (max 80 char),
    "do": str,                    # cosa fare in scenari simili
    "dont": str,                  # cosa NON fare
    "rationale": str,             # perche', basato su pattern degli advice
    "scenarios_signature": str,   # tag tipo "macro_bear" / "crypto_volatile"
    "source_advice_count": int,   # quanti advice della memoria sono stati
                                   # sintetizzati per produrre questa card
    "created_at": ISO,
    "expires_at": ISO,            # default +14 giorni
    "applied_count": int,         # quante volte iniettato nei run Live
  }

Engine: DeepSeek-V3 (cheap, fast — ~$0.001 per synth).
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone, timedelta

import aiohttp

logger = logging.getLogger(__name__)

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

# ─── Storage helpers ────────────────────────────────────────────────────────

KEY_LIST = "_coach_card::list"
KEY_CARD = "_coach_card::card::{id}"
KEY_LAST_SYNTH = "_coach_card::last_synth_at"
KEY_SYNTH_STATUS = "_coach_card::synth_status"
MAX_CARDS = 30   # cap totale, FIFO sui piu' vecchi


def _settings_get(key: str, default: str = "") -> str:
    try:
        from simulator import db as sim_db
        return sim_db.get_setting(key, default) or default
    except Exception:
        try:
            import database
            return database.get_setting(key, default) or default
        except Exception:
            return default


def _settings_set(key: str, value: str) -> None:
    try:
        from simulator import db as sim_db
        sim_db.set_setting(key, value)
        return
    except Exception:
        pass
    try:
        import database
        database.set_setting(key, value)
    except Exception as e:
        logger.warning("coach_cards settings_set %s fallita: %s", key, e)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── CRUD ─────────────────────────────────────────────────────────────────

def list_cards(active_only: bool = True) -> list[dict]:
    """Lista tutte le coach card. Se active_only filtra le scadute."""
    raw = _settings_get(KEY_LIST, "[]")
    try:
        ids = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(ids, list):
            return []
    except Exception:
        return []

    now = datetime.now(timezone.utc)
    out: list[dict] = []
    for cid in ids:
        raw_c = _settings_get(KEY_CARD.format(id=cid), "")
        if not raw_c:
            continue
        try:
            card = json.loads(raw_c) if isinstance(raw_c, str) else raw_c
        except Exception:
            continue
        if not isinstance(card, dict):
            continue
        if active_only:
            exp = card.get("expires_at")
            if exp:
                try:
                    if datetime.fromisoformat(exp.replace("Z", "+00:00")) < now:
                        continue
                except Exception:
                    pass
        out.append(card)
    # Più recenti per primi
    out.sort(key=lambda c: c.get("created_at", ""), reverse=True)
    return out


def save_card(card: dict) -> str:
    """Salva una card sul KV store + update indice."""
    cid = card.get("id") or str(uuid.uuid4())
    card["id"] = cid
    card.setdefault("created_at", _now_iso())
    card.setdefault("applied_count", 0)
    if not card.get("expires_at"):
        card["expires_at"] = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()

    _settings_set(KEY_CARD.format(id=cid), json.dumps(card, default=str))

    # Aggiorna indice
    raw = _settings_get(KEY_LIST, "[]")
    try:
        ids = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(ids, list):
            ids = []
    except Exception:
        ids = []
    if cid in ids:
        ids.remove(cid)
    ids.insert(0, cid)
    # Cap MAX_CARDS, FIFO sui più vecchi
    if len(ids) > MAX_CARDS:
        for old_id in ids[MAX_CARDS:]:
            try:
                _settings_set(KEY_CARD.format(id=old_id), "")
            except Exception:
                pass
        ids = ids[:MAX_CARDS]
    _settings_set(KEY_LIST, json.dumps(ids, default=str))
    return cid


def delete_card(card_id: str) -> bool:
    raw = _settings_get(KEY_LIST, "[]")
    try:
        ids = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(ids, list):
            return False
    except Exception:
        return False
    if card_id not in ids:
        return False
    ids.remove(card_id)
    _settings_set(KEY_LIST, json.dumps(ids, default=str))
    _settings_set(KEY_CARD.format(id=card_id), "")
    return True


def increment_applied(card_id: str) -> None:
    raw_c = _settings_get(KEY_CARD.format(id=card_id), "")
    if not raw_c:
        return
    try:
        card = json.loads(raw_c) if isinstance(raw_c, str) else raw_c
        if not isinstance(card, dict):
            return
        card["applied_count"] = int(card.get("applied_count") or 0) + 1
        _settings_set(KEY_CARD.format(id=card_id), json.dumps(card, default=str))
    except Exception:
        pass


def get_status() -> dict:
    return {
        "last_synth_at": _settings_get(KEY_LAST_SYNTH, ""),
        "synth_status": _settings_get(KEY_SYNTH_STATUS, ""),
        "active_cards": len(list_cards(active_only=True)),
    }


# ─── Synthesis pipeline ────────────────────────────────────────────────────

SYNTHESIS_PROMPT = """Sei il Coach AI di GeoInvest. Il tuo compito: analizzare i
consigli archiviati nella memoria del Sim Advisor (da run del Simulator) e
produrre 3-5 "Coach Cards" — raccomandazioni operative GENERALI da iniettare
nel system prompt del Decision Agent Live.

Le Coach Cards sono regole operative ad alto livello (NON consigli su singoli
ticker), valide per uno o piu' regimi di mercato. Si basano sui PATTERN
emersi dagli advice del Simulator.

Per ogni card produci:
- title: max 80 char (sintetico, azionabile)
- do: cosa FARE in scenari simili (max 2 righe)
- dont: cosa NON FARE in scenari simili (max 2 righe)
- rationale: perche' (basato sui pattern degli advice — max 2 righe)
- scenarios_signature: tag tipo "macro_bear" / "crypto_volatile" / "equity_neutral"
- category_focus: una delle categorie viste negli advice

Diversifica le card: copri scenari diversi (almeno 3 categorie diverse se
possibile dai dati). Se gli advice sono pochi (< 5), produci solo 1-2 card
strettamente derivate.

OUTPUT: SOLO JSON, schema esatto:

{
  "cards": [
    {
      "title": "...",
      "do": "...",
      "dont": "...",
      "rationale": "...",
      "scenarios_signature": "macro_bear",
      "category_focus": "macro"
    }
  ]
}

Niente preambolo, niente markdown — solo JSON puro.
"""


def _format_advice_for_synth(advice_groups: dict) -> str:
    """Compatta gli advice raggruppati per category_key in formato leggibile."""
    lines = []
    for key, items in advice_groups.items():
        if not items:
            continue
        lines.append(f"\n=== Categoria: {key} ({len(items)} advice) ===")
        for a in items[:8]:   # max 8 per categoria, taglio per token
            title = (a.get("title") or "").strip()
            text = (a.get("text") or "").strip()
            apply_cnt = a.get("apply_count") or 0
            lines.append(f"- [{title}] {text[:300]}  (applied: {apply_cnt}×)")
    return "\n".join(lines) if lines else "(nessun advice)"


async def run_weekly_synthesis() -> dict:
    """
    Pipeline settimanale di synthesis. Legge tutti gli advice dal Sim
    Advisor memory, chiama DeepSeek-V3, salva 3-5 Coach Cards.

    Ritorna {success: bool, cards_created: int, error: str | None}.
    """
    _settings_set(KEY_SYNTH_STATUS, "running")
    started = datetime.now(timezone.utc)

    try:
        # 1. Carica advice memory
        try:
            from agents import sim_advisor
            advice_groups = sim_advisor.list_all_advice()
        except Exception as e:
            return _synth_finalize(False, 0, f"sim_advisor import: {e}")

        total = sum(len(v) for v in advice_groups.values())
        if total == 0:
            return _synth_finalize(False, 0, "Memoria advice vuota — nessuna card prodotta")

        # 2. Chiama DeepSeek-V3
        api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not api_key:
            try:
                import database
                api_key = (database.get_setting("deepseek_api_key", "") or "").strip()
            except Exception:
                pass
        if not api_key:
            return _synth_finalize(False, 0, "DEEPSEEK_API_KEY non configurata")

        advice_text = _format_advice_for_synth(advice_groups)
        user_msg = (
            f"Memoria advice del Sim Advisor (totale {total} consigli, "
            f"{len(advice_groups)} categorie):\n{advice_text}\n\n"
            f"Produci 3-5 Coach Cards secondo lo schema."
        )

        payload = {
            "model": DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": SYNTHESIS_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0.5,
            "max_tokens": 3000,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                DEEPSEEK_API_URL, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    return _synth_finalize(False, 0,
                        f"DeepSeek HTTP {resp.status}: {body[:300]}")
                data = json.loads(body)

        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            m = re.search(r"\{[\s\S]*\}", content)
            if not m:
                return _synth_finalize(False, 0,
                    f"Output non parsabile: {content[:200]}")
            parsed = json.loads(m.group(0))

        cards = parsed.get("cards") or []
        if not isinstance(cards, list) or not cards:
            return _synth_finalize(False, 0, "Nessuna card valida nel response")

        # 3. Validazione + salvataggio
        week_of = started.strftime("%Y-%m-%d")
        saved_ids: list[str] = []
        for c in cards[:5]:   # cap a 5
            if not isinstance(c, dict):
                continue
            title = (c.get("title") or "").strip()
            do_t = (c.get("do") or "").strip()
            dont = (c.get("dont") or "").strip()
            if not title or not do_t or not dont:
                continue
            card = {
                "week_of": week_of,
                "category_focus": (c.get("category_focus") or "mixed")[:32],
                "title": title[:200],
                "do": do_t[:600],
                "dont": dont[:600],
                "rationale": (c.get("rationale") or "")[:500],
                "scenarios_signature": (c.get("scenarios_signature") or "")[:48],
                "source_advice_count": total,
            }
            try:
                cid = save_card(card)
                saved_ids.append(cid)
            except Exception as e:
                logger.warning("save_card fallita: %s", e)

        return _synth_finalize(True, len(saved_ids), None, saved_ids)

    except Exception as e:
        logger.error("Coach cards synthesis crash: %s", e, exc_info=True)
        return _synth_finalize(False, 0, str(e)[:300])


def _synth_finalize(success: bool, cards_created: int, error: str | None,
                     ids: list | None = None) -> dict:
    _settings_set(KEY_SYNTH_STATUS, "ok" if success else "error")
    _settings_set(KEY_LAST_SYNTH, _now_iso())
    return {
        "success": success,
        "cards_created": cards_created,
        "card_ids": ids or [],
        "error": error,
        "timestamp": _now_iso(),
    }


# ─── Helper per il Decision Agent (inietta cards nel prompt) ──────────────

def get_active_cards_block_for_decision() -> str:
    """
    Ritorna un blocco testo da iniettare nel system prompt del Decision
    Agent. Lista le active coach cards in modo conciso.
    Chiamato da decision.py / decision_crypto.py se vogliono usarle.
    """
    cards = list_cards(active_only=True)
    if not cards:
        return ""
    lines = [
        "═" * 60,
        f"COACH CARDS attive ({len(cards)} regole operative dalla memoria Simulator):",
        "Queste sono regole operative emerse dall'analisi pattern dei run del",
        "Simulator. Considerale come contesto, non assoluto. Cita la card nel",
        "logic_chain quando una si applica.",
        "═" * 60,
    ]
    for i, c in enumerate(cards[:10], 1):
        sig = c.get("scenarios_signature") or "?"
        lines.append(f"\n[Card {i}] {c.get('title', '')} ({sig})")
        lines.append(f"  ✓ DO: {c.get('do', '')}")
        lines.append(f"  ✗ DON'T: {c.get('dont', '')}")
    lines.append("═" * 60)
    return "\n".join(lines)
