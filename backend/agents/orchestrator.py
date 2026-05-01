"""
Orchestrator — Wires Scout → Watchdog → Technical → Decision agents.

Architettura a 4 agenti (budget ~€18/mese):
  - Scout (Haiku 3, ogni ora): raccoglie intelligence nel buffer
  - Watchdog (DeepSeek-V3, ogni 5 min): monitora e decide se triggerare
  - Technical (DeepSeek-V3, on-demand): analisi tecnica quando Watchdog trigghera
  - Decision (Sonnet 4.5, max 1/ora): decisione trading quando Watchdog trigghera

Flussi:
  run_watchdog_pipeline() — ogni 5 min durante ore di mercato
  run_scout_pipeline()    — ogni ora, sempre
  run_full_pipeline()     — legacy, usato per test manuali
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from uuid import uuid4

import database

logger = logging.getLogger(__name__)


# ============================================================
# Pipeline principale: Watchdog (ogni 5 min, market hours)
# ============================================================

async def run_watchdog_pipeline(run_id: str | None = None) -> dict:
    """
    Ciclo veloce ogni 5 minuti:
    1. Watchdog analizza mercato (DeepSeek-V3, ~2-3s)
    2. Se trigger=True e non throttled → avvia Technical + Decision
    3. Se trigger=False → exit subito (costo: quasi zero)
    """
    if not run_id:
        run_id = str(uuid4())

    start = time.time()

    # ─── Watchdog ───
    try:
        from agents.watchdog import run_watchdog
        watchdog_result = await run_watchdog(run_id)
    except Exception as e:
        logger.error("[%s][ORCHESTRATOR] Watchdog fallito: %s", run_id, e)
        return {"run_id": run_id, "triggered": False, "error": str(e)}

    should_trigger = watchdog_result.get("should_trigger", False)
    urgency = watchdog_result.get("urgency", 0)
    reason = watchdog_result.get("reason", "")
    focus_tickers = watchdog_result.get("focus_tickers", [])

    if not should_trigger:
        logger.debug("[%s][WATCHDOG] No trigger (urgency=%d, reason='%s')",
                     run_id, urgency, reason)
        return {
            "run_id": run_id,
            "triggered": False,
            "urgency": urgency,
            "reason": reason,
            "duration_seconds": round(time.time() - start, 2),
        }

    # ──────────────────────────────────────────────────────────────
    # GATE IBRIDO: market-open vs overnight crypto
    #
    # Mercati equity APERTI (NYSE/LSE/XETRA):
    #     → Decision Agent gira con engine Sonnet 4.5 (default GEO).
    #
    # Mercati equity CHIUSI (notti, weekend):
    #     → Decision Agent gira con engine DeepSeek-R1 SOLO se:
    #       1) almeno un focus_ticker è una crypto (BTC-USD, ETH-USD, ...)
    #       2) il cooldown 2h30 dall'ultimo run R1 è scaduto
    #     → Altrimenti blocca: niente da fare overnight.
    # ──────────────────────────────────────────────────────────────
    try:
        from scheduler import is_market_open
        market_open = is_market_open()
    except Exception:
        market_open = False

    if not market_open:
        # Filtra crypto dai focus tickers (overnight: solo crypto è tradabile)
        crypto_in_focus = [t for t in focus_tickers if _is_crypto_ticker(t)]

        if not crypto_in_focus:
            logger.info("[%s][ORCHESTRATOR] Mercati equity chiusi e nessuna crypto nei "
                        "focus_tickers (focus=%s) — Decision NON avviato.",
                        run_id, focus_tickers)
            database.insert_agent_log(run_id, "ORCHESTRATOR", json.dumps({
                "event": "decision_blocked_no_crypto_overnight",
                "urgency": urgency,
                "reason": reason,
                "focus_tickers": focus_tickers,
            }))
            return {
                "run_id": run_id,
                "triggered": False,
                "urgency": urgency,
                "reason": reason,
                "blocked": "no_crypto_overnight",
                "duration_seconds": round(time.time() - start, 2),
            }

        # Cooldown 2h30 R1
        try:
            from agents.decision import is_r1_cooldown_active
            cooldown_active, seconds_left = is_r1_cooldown_active()
        except Exception:
            cooldown_active, seconds_left = False, 0

        if cooldown_active:
            logger.info("[%s][ORCHESTRATOR] Cooldown R1 attivo: ancora %ds (~%.1fh) — "
                        "Decision NON avviato.",
                        run_id, seconds_left, seconds_left / 3600.0)
            database.insert_agent_log(run_id, "ORCHESTRATOR", json.dumps({
                "event": "decision_blocked_r1_cooldown",
                "urgency": urgency,
                "reason": reason,
                "seconds_left": seconds_left,
            }))
            return {
                "run_id": run_id,
                "triggered": False,
                "urgency": urgency,
                "reason": reason,
                "blocked": "r1_cooldown",
                "cooldown_seconds_left": seconds_left,
                "duration_seconds": round(time.time() - start, 2),
            }

        # Restringi focus_tickers SOLO alle crypto (overnight): non ha senso
        # passare AAPL/TSLA al Decision quando le borse sono chiuse.
        focus_tickers = crypto_in_focus
        logger.info("[%s][ORCHESTRATOR] OVERNIGHT MODE: market closed, %d crypto in focus "
                    "(%s), cooldown R1 OK — avvio R1 Decision.",
                    run_id, len(crypto_in_focus), crypto_in_focus)

    # Trigger! Avvia pipeline completa
    logger.info("[%s][WATCHDOG] TRIGGER urgency=%d: %s — avvio Technical+Decision",
                run_id, urgency, reason)

    database.insert_agent_log(run_id, "ORCHESTRATOR", json.dumps({
        "event": "watchdog_triggered",
        "urgency": urgency,
        "reason": reason,
        "focus_tickers": focus_tickers,
        "architecture": "multi-agent",
    }))

    # ─── Technical Worker ───
    hot_tickers = focus_tickers if focus_tickers else _get_default_tickers()
    hot_tickers = hot_tickers[:6]  # Max 6 tickers per contenere i costi

    try:
        from agents.technical import run_technical_analysis
        tech_report = await run_technical_analysis(run_id, hot_tickers)
    except Exception as e:
        logger.error("[%s][ORCHESTRATOR] Technical fallito: %s", run_id, e)
        tech_report = {"analyses": [], "engine": "error", "summary": str(e)}

    # ─── Decision Agent ───
    try:
        from agents.decision import run_decision_agent
        decision_result = await run_decision_agent(run_id, tech_report)
    except Exception as e:
        logger.error("[%s][ORCHESTRATOR] Decision fallito: %s", run_id, e)
        decision_result = {"decision": "ERROR", "trades": [], "error": str(e)}

    duration = round(time.time() - start, 1)

    database.insert_agent_log(run_id, "ORCHESTRATOR", json.dumps({
        "event": "watchdog_pipeline_complete",
        "triggered": True,
        "urgency": urgency,
        "decision": decision_result.get("decision", "UNKNOWN"),
        "trades": len(decision_result.get("trades", [])),
        "duration_seconds": duration,
    }))

    return {
        "run_id": run_id,
        "triggered": True,
        "urgency": urgency,
        "reason": reason,
        "tech_engine": tech_report.get("engine", "unknown"),
        "decision": decision_result.get("decision", "UNKNOWN"),
        "trades": decision_result.get("trades", []),
        "duration_seconds": duration,
    }


# ============================================================
# Pipeline Scout (ogni ora, sempre — non solo market hours)
# ============================================================

async def run_scout_pipeline(run_id: str | None = None) -> dict:
    """
    Ciclo orario: Scout raccoglie intelligence e popola il buffer.
    Usa Claude Haiku 3 — economico per funzionare 24/7.
    """
    if not run_id:
        run_id = str(uuid4())

    start = time.time()
    logger.info("[%s][ORCHESTRATOR] Scout pipeline avviata", run_id)

    try:
        from agents.scout import run_scout_20min
        micro_cards = await run_scout_20min(run_id)
        duration = round(time.time() - start, 1)

        database.insert_agent_log(run_id, "ORCHESTRATOR", json.dumps({
            "event": "scout_pipeline_complete",
            "micro_cards": len(micro_cards),
            "duration_seconds": duration,
        }))

        return {
            "run_id": run_id,
            "architecture": "scout-only",
            "cards": len(micro_cards),
            "duration_seconds": duration,
        }
    except Exception as e:
        logger.error("[%s][ORCHESTRATOR] Scout pipeline fallita: %s", run_id, e)
        return {
            "run_id": run_id,
            "architecture": "scout-only",
            "error": str(e),
            "duration_seconds": round(time.time() - start, 1),
        }


# ============================================================
# Pipeline legacy FULL (test manuali / backward compatibility)
# ============================================================

async def run_full_pipeline(run_id: str | None = None) -> dict:
    """
    Pipeline completa forzata (ignora throttle Watchdog).
    Usata per il pulsante "Esegui Manuale" nel frontend.
    """
    if not run_id:
        run_id = str(uuid4())

    start = time.time()
    logger.info("[%s][ORCHESTRATOR] Pipeline FULL manuale avviata", run_id)

    database.insert_agent_log(run_id, "ORCHESTRATOR", json.dumps({
        "event": "pipeline_start",
        "architecture": "multi-agent",
        "mode": "full_manual",
    }))

    result = {"run_id": run_id, "architecture": "multi-agent", "mode": "full", "phases": {}}

    # 1. Scout
    try:
        from agents.scout import run_scout_20min
        micro_cards = await run_scout_20min(run_id)
        result["phases"]["scout"] = {"status": "ok", "cards": len(micro_cards)}
    except Exception as e:
        logger.error("[%s] Scout fallito: %s", run_id, e)
        micro_cards = []
        result["phases"]["scout"] = {"status": "error", "error": str(e)}

    # 2. Tickers
    hot_tickers = _extract_hot_tickers(micro_cards)
    if not hot_tickers:
        hot_tickers = _get_default_tickers()
    hot_tickers = hot_tickers[:6]

    # 3. Technical
    try:
        from agents.technical import run_technical_analysis
        tech_report = await run_technical_analysis(run_id, hot_tickers)
        result["phases"]["technical"] = {
            "status": "ok",
            "engine": tech_report.get("engine", "unknown"),
            "analyses": len(tech_report.get("analyses", [])),
        }
    except Exception as e:
        logger.error("[%s] Technical fallito: %s", run_id, e)
        tech_report = {"analyses": [], "engine": "none", "summary": str(e)}
        result["phases"]["technical"] = {"status": "error", "error": str(e)}

    # 4. Decision
    try:
        from agents.decision import run_decision_agent
        decision_result = await run_decision_agent(run_id, tech_report)
        result["phases"]["decision"] = {
            "status": "ok",
            "decision": decision_result.get("decision", "UNKNOWN"),
            "trades": len(decision_result.get("trades", [])),
            "model": decision_result.get("model", "unknown"),
        }
        result["final_response"] = decision_result.get("final_response", "")
    except Exception as e:
        logger.error("[%s] Decision fallito: %s", run_id, e)
        result["phases"]["decision"] = {"status": "error", "error": str(e)}
        result["final_response"] = f"Decision error: {e}"

    duration = round(time.time() - start, 1)
    result["duration_seconds"] = duration

    database.insert_agent_log(run_id, "ORCHESTRATOR", json.dumps({
        "event": "pipeline_complete",
        "duration_seconds": duration,
        "phases": result["phases"],
    }, default=str))

    logger.info("[%s][ORCHESTRATOR] Pipeline FULL completata in %.1fs", run_id, duration)
    return result


# ============================================================
# Scout-only (legacy per weekend/pre-market)
# ============================================================

async def run_scout_only(run_id: str | None = None) -> dict:
    """Alias di run_scout_pipeline per compatibilità con agent.py."""
    return await run_scout_pipeline(run_id)


# ============================================================
# Helpers
# ============================================================

def _get_default_tickers() -> list[str]:
    """Tickers di default se Watchdog non ne specifica."""
    try:
        import database as _db
        watchlist = _db.get_setting("watchlist")
        if watchlist:
            import json as _json
            wl = _json.loads(watchlist)
            tickers = []
            for tickers_list in wl.values():
                tickers.extend(tickers_list[:2])
            return tickers[:6]
    except Exception:
        pass
    return ["SPY", "XOM", "LMT", "GLD", "QQQ", "EEM"]


def _extract_hot_tickers(micro_cards: list[dict]) -> list[str]:
    """Estrae tickers dalle micro-schede Scout."""
    tickers = []
    seen = set()
    for card in micro_cards:
        for t in card.get("key_tickers", []):
            t_upper = t.upper().strip()
            if t_upper and t_upper not in seen:
                tickers.append(t_upper)
                seen.add(t_upper)
    return tickers


def _is_crypto_ticker(ticker: str) -> bool:
    """
    True se il ticker è una crypto (universo ClawStreet 24/7).

    Riconosce sia il formato yfinance ("BTC-USD") sia ClawStreet ("X:BTCUSD").
    Usata dall'orchestrator per decidere se overnight + watchdog trigger
    deve effettivamente avviare il Decision Agent (engine R1).
    """
    if not ticker:
        return False
    t = ticker.upper().strip()
    # Formato ClawStreet: "X:BTCUSD"
    if t.startswith("X:"):
        return True
    # Formato yfinance: "BTC-USD", "ETH-USD", ...
    if t.endswith("-USD") and len(t) > 4:
        return True
    return False
