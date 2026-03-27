"""
Orchestrator — Wires Scout → Technical → Decision agents.

Replaces the old multi_agent.py with the new hierarchical async system.
Ogni 20 min: Scout raccoglie intel → Technical analizza ticker caldi → Decision valuta e opera.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from uuid import uuid4

import database

logger = logging.getLogger(__name__)


async def run_full_pipeline(run_id: str | None = None) -> dict:
    """
    Pipeline completa per il ciclo di mercato aperto (mode=full).

    Flusso:
      1. Scout 20-min → micro-schede intelligence_buffer
      2. Identifica hot tickers dal buffer + daily
      3. Technical Worker → analisi tecnica sui tickers
      4. Decision Agent → valutazione strategica + esecuzione trade
    """
    if not run_id:
        run_id = str(uuid4())

    start = time.time()
    logger.info("[%s][ORCHESTRATOR] === Pipeline FULL avviata ===", run_id)

    database.insert_agent_log(run_id, "ORCHESTRATOR", json.dumps({
        "event": "pipeline_start",
        "architecture": "multi-agent",
        "mode": "full",
    }))

    result = {
        "run_id": run_id,
        "architecture": "multi-agent",
        "mode": "full",
        "phases": {},
    }

    # ─── FASE 1: Scout 20-min ───
    try:
        from agents.scout import run_scout_20min
        micro_cards = await run_scout_20min(run_id)
        result["phases"]["scout"] = {
            "status": "ok",
            "cards": len(micro_cards),
        }
    except Exception as e:
        logger.error("[%s][ORCHESTRATOR] Scout fallito: %s", run_id, e, exc_info=True)
        micro_cards = []
        result["phases"]["scout"] = {"status": "error", "error": str(e)}

    # ─── FASE 2: Identifica tickers da analizzare ───
    hot_tickers = _extract_hot_tickers(micro_cards)

    # Arricchisci con daily snapshots hot tickers
    try:
        from agents.scout import get_latest_daily_snapshots
        dailies = get_latest_daily_snapshots(database, n=1)
        if dailies:
            for d in dailies:
                for t in d.get("hot_tickers", []):
                    if t not in hot_tickers:
                        hot_tickers.append(t)
    except Exception:
        pass

    # Fallback: watchlist default
    if not hot_tickers:
        hot_tickers = ["SPY", "XOM", "LMT", "GLD", "QQQ"]

    # Limita a 8 tickers
    hot_tickers = hot_tickers[:8]

    database.insert_agent_log(run_id, "ORCHESTRATOR", json.dumps({
        "event": "tickers_identified",
        "tickers": hot_tickers,
    }))

    # ─── FASE 3: Technical Worker ───
    try:
        from agents.technical import run_technical_analysis
        tech_report = await run_technical_analysis(run_id, hot_tickers)
        result["phases"]["technical"] = {
            "status": "ok",
            "engine": tech_report.get("engine", "unknown"),
            "analyses": len(tech_report.get("analyses", [])),
        }
    except Exception as e:
        logger.error("[%s][ORCHESTRATOR] Technical Worker fallito: %s", run_id, e, exc_info=True)
        tech_report = {
            "analyses": [],
            "engine": "none",
            "summary": f"Technical analysis failed: {e}",
        }
        result["phases"]["technical"] = {"status": "error", "error": str(e)}

    # ─── FASE 4: Decision Agent ───
    try:
        from agents.decision import run_decision_agent
        decision_result = await run_decision_agent(run_id, tech_report)
        result["phases"]["decision"] = {
            "status": "ok",
            "decision": decision_result.get("decision", "UNKNOWN"),
            "trades": len(decision_result.get("trades", [])),
            "model": decision_result.get("model", "unknown"),
            "iterations": decision_result.get("iterations", 0),
        }
        result["final_response"] = decision_result.get("final_response", "")
    except Exception as e:
        logger.error("[%s][ORCHESTRATOR] Decision Agent fallito: %s", run_id, e, exc_info=True)
        result["phases"]["decision"] = {"status": "error", "error": str(e)}
        result["final_response"] = f"Decision Agent error: {e}"

    # ─── Finalizzazione ───
    duration = time.time() - start
    result["duration_seconds"] = round(duration, 1)

    database.insert_agent_log(run_id, "ORCHESTRATOR", json.dumps({
        "event": "pipeline_complete",
        "duration_seconds": result["duration_seconds"],
        "phases": result["phases"],
    }, default=str))

    logger.info("[%s][ORCHESTRATOR] === Pipeline FULL completata in %.1fs ===",
                run_id, duration)
    return result


async def run_scout_only(run_id: str | None = None) -> dict:
    """
    Pipeline per weekend/pre-market: solo Scout (intelligence gathering).
    Non fa analisi tecnica ne' decisioni di trading.
    """
    if not run_id:
        run_id = str(uuid4())

    start = time.time()
    logger.info("[%s][ORCHESTRATOR] Scout-only pipeline avviata", run_id)

    try:
        from agents.scout import run_scout_20min
        micro_cards = await run_scout_20min(run_id)
        duration = time.time() - start
        return {
            "run_id": run_id,
            "architecture": "scout-only",
            "cards": len(micro_cards),
            "duration_seconds": round(duration, 1),
        }
    except Exception as e:
        logger.error("[%s][ORCHESTRATOR] Scout-only fallito: %s", run_id, e)
        return {
            "run_id": run_id,
            "architecture": "scout-only",
            "error": str(e),
            "duration_seconds": round(time.time() - start, 1),
        }


def _extract_hot_tickers(micro_cards: list[dict]) -> list[str]:
    """Estrae tickers menzionati nelle micro-schede dello Scout."""
    tickers = []
    seen = set()
    for card in micro_cards:
        for t in card.get("key_tickers", []):
            t_upper = t.upper().strip()
            if t_upper and t_upper not in seen:
                tickers.append(t_upper)
                seen.add(t_upper)
    return tickers
