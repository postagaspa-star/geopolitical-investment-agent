"""
Provider Health — sentinella oraria dei fornitori esterni (LLM + dati).

Motivazione (analisi 24/07): in 10 giorni il sistema si e' fermato TRE volte
per guasti a dipendenze esterne, ognuna scoperta a valle dal comportamento,
ore o giorni dopo:
  - 14/07: modelli Anthropic ritirati (404)
  - 20/07: DeepSeek a saldo zero (402)
  - 24/07: DeepSeek ha ritirato deepseek-chat/reasoner (400)
Un test orario da pochi token per fornitore li avrebbe intercettati in minuti.

Cosa fa:
  - Testa Anthropic (Haiku 4.5, il piu' economico) e DeepSeek (v4-flash) con
    una chiamata da 1 token.
  - Rileva la copertura delle fonti Scout dagli ultimi log (quante vive su 7).
  - Alza un alert SOLO sulle TRANSIZIONI (ok->down e down->ok), non ad ogni
    tick: niente spam se un fornitore resta giu' per giorni.
  - Alert = log PROVIDER_HEALTH_ALERT su DB + Telegram best-effort.

Telegram: si attiva SOLO se TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID sono
configurati (env o settings). Senza, l'alert resta sul log ed e' comunque
visibile via /api/settings/provider-health. Nessuna dipendenza dura.

Fail-safe: ogni errore interno non deve mai propagarsi al chiamante
(scheduler). Un guasto del monitor non deve fermare il sistema.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import aiohttp

logger = logging.getLogger(__name__)

STATE_KEY = "_provider_health::last_state"       # JSON {provider: "ok"|"down"}
LAST_RESULT_KEY = "_provider_health::last_result"  # JSON dump ultimo check completo

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_PING_MODEL = "deepseek-v4-flash"
ANTHROPIC_PING_MODEL = "claude-haiku-4-5"   # il piu' economico per un ping


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_key(setting_name: str, env_name: str) -> str:
    try:
        import database
        return (database.get_setting(setting_name, os.environ.get(env_name, "")) or "").strip()
    except Exception:
        return (os.environ.get(env_name, "") or "").strip()


# ─── Singoli test provider (ritornano (ok: bool, detail: str)) ──────────────

async def _check_deepseek(session: aiohttp.ClientSession) -> tuple[bool, str]:
    key = _get_key("deepseek_api_key", "DEEPSEEK_API_KEY")
    if not key:
        return False, "DEEPSEEK_API_KEY non configurata"
    try:
        async with session.post(
            DEEPSEEK_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": DEEPSEEK_PING_MODEL,
                  "messages": [{"role": "user", "content": "ping"}],
                  "max_tokens": 1},
            timeout=aiohttp.ClientTimeout(total=25),
        ) as resp:
            if resp.status == 200:
                return True, f"{DEEPSEEK_PING_MODEL} ok"
            body = (await resp.text())[:200]
            return False, f"HTTP {resp.status}: {body}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:150]}"


async def _check_anthropic(session: aiohttp.ClientSession) -> tuple[bool, str]:
    key = _get_key("anthropic_api_key", "ANTHROPIC_API_KEY")
    if not key:
        return False, "ANTHROPIC_API_KEY non configurata"
    try:
        async with session.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "Content-Type": "application/json"},
            json={"model": ANTHROPIC_PING_MODEL, "max_tokens": 1,
                  "messages": [{"role": "user", "content": "ping"}]},
            timeout=aiohttp.ClientTimeout(total=25),
        ) as resp:
            if resp.status == 200:
                return True, f"{ANTHROPIC_PING_MODEL} ok"
            body = (await resp.text())[:200]
            return False, f"HTTP {resp.status}: {body}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:150]}"


def _check_scout_sources() -> tuple[bool, str]:
    """Copertura fonti Scout dagli ultimi log: quante hanno prodotto record.
    Non e' un test attivo (non consuma quota), e' una lettura dello storico.
    down = meno di 2 fonti vive (il sistema decide quasi al buio)."""
    try:
        import database
        logs = database.get_agent_logs(limit=60) or []
        import json as _json
        for l in logs:
            if l.get("phase") != "SCOUT" and "scout_sources_fetched" not in str(l.get("content", "")):
                continue
            try:
                c = _json.loads(l.get("content") or "{}")
            except Exception:
                continue
            srcs = c.get("sources")
            if not isinstance(srcs, dict):
                continue
            alive = sum(1 for s in srcs.values()
                        if isinstance(s, dict) and (s.get("count") or 0) > 0)
            total = len(srcs)
            return (alive >= 2, f"{alive}/{total} fonti con dati")
        return True, "nessun log scout recente (skip)"
    except Exception as e:
        return True, f"check non eseguito: {type(e).__name__}"


# ─── Telegram best-effort ────────────────────────────────────────────────────

async def _send_telegram(session: aiohttp.ClientSession, text: str) -> bool:
    token = _get_key("telegram_bot_token", "TELEGRAM_BOT_TOKEN")
    chat_id = _get_key("telegram_chat_id", "TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False   # non configurato: no-op silenzioso
    try:
        async with session.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            return resp.status == 200
    except Exception as e:
        logger.warning("[PROVIDER-HEALTH] telegram fallito: %s", e)
        return False


# ─── Entry point ─────────────────────────────────────────────────────────────

async def run_check(alert: bool = True) -> dict:
    """
    Esegue il check di tutti i fornitori. Ritorna un dict con lo stato.
    Se alert=True, confronta con lo stato precedente e notifica SOLO le
    transizioni (Telegram + log). Fail-safe totale.
    """
    result: dict = {"checked_at": _now_iso(), "providers": {}}
    try:
        async with aiohttp.ClientSession() as session:
            for name, coro in (("anthropic", _check_anthropic(session)),
                               ("deepseek", _check_deepseek(session))):
                ok, detail = await coro
                result["providers"][name] = {"ok": ok, "detail": detail}
            s_ok, s_detail = _check_scout_sources()
            result["providers"]["scout_sources"] = {"ok": s_ok, "detail": s_detail}

            # Persisti l'ultimo risultato (visibile via API)
            import json as _json
            try:
                import database
                database.set_setting(LAST_RESULT_KEY, _json.dumps(result))
            except Exception:
                pass

            if alert:
                await _maybe_alert(session, result)
    except Exception as e:
        logger.error("[PROVIDER-HEALTH] run_check crash: %s", e, exc_info=True)
        result["error"] = str(e)
    return result


async def _maybe_alert(session: aiohttp.ClientSession, result: dict) -> None:
    """Notifica solo le transizioni ok<->down per ciascun provider."""
    import json as _json
    try:
        import database
        prev_raw = database.get_setting(STATE_KEY, "{}") or "{}"
        prev = _json.loads(prev_raw) if isinstance(prev_raw, str) else (prev_raw or {})
    except Exception:
        prev = {}

    cur = {name: ("ok" if info["ok"] else "down")
           for name, info in result["providers"].items()}

    transitions = []
    for name, state in cur.items():
        was = prev.get(name)
        if was is not None and was != state:
            detail = result["providers"][name]["detail"]
            transitions.append((name, was, state, detail))

    if transitions:
        lines = ["\U0001F6A8 GeoInvest — fornitori esterni:"]
        for name, was, state, detail in transitions:
            icon = "✅" if state == "ok" else "❌"
            verb = "RIPRISTINATO" if state == "ok" else "GIU'"
            lines.append(f"{icon} {name} {verb} — {detail}")
        msg = "\n".join(lines)
        try:
            import database
            database.insert_agent_log("provider_health", "PROVIDER_HEALTH_ALERT",
                                      _json.dumps({"transitions": transitions,
                                                   "current": cur}, default=str))
        except Exception:
            pass
        sent = await _send_telegram(session, msg)
        logger.warning("[PROVIDER-HEALTH] ALERT (telegram_sent=%s): %s",
                       sent, " | ".join(f"{n}:{w}->{s}" for n, w, s, _ in transitions))

    # Salva lo stato corrente per il prossimo confronto
    try:
        import database
        database.set_setting(STATE_KEY, _json.dumps(cur))
    except Exception:
        pass


def get_last_result() -> dict | None:
    """Ultimo risultato del check (per l'endpoint diagnostico)."""
    try:
        import database, json as _json
        raw = database.get_setting(LAST_RESULT_KEY, "")
        return _json.loads(raw) if raw else None
    except Exception:
        return None
