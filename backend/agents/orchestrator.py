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


# Crypto di fallback quando il Watchdog focalizza su equity ma i mercati
# equity sono chiusi: invece di bloccare, sostituiamo con queste 3 e lasciamo
# che R1 valuti se vale la pena operare. Top liquidity + volatilità garantita.
_DEFAULT_OVERNIGHT_CRYPTO = ["BTC-USD", "ETH-USD", "SOL-USD"]

# Log throttling: gli stessi eventi "blocked" vengono loggati al massimo
# ogni N secondi per non riempire la dashboard con righe identiche.
_BLOCK_LOG_THROTTLE_SECONDS = 600   # 10 minuti
_last_block_logs: dict[str, float] = {}


def _should_emit_block_log(reason_key: str) -> bool:
    """True se è passato abbastanza tempo dall'ultimo log con questa reason_key."""
    now = time.time()
    last = _last_block_logs.get(reason_key, 0.0)
    if now - last >= _BLOCK_LOG_THROTTLE_SECONDS:
        _last_block_logs[reason_key] = now
        return True
    return False


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
    # GATE: il Decision NORMALE opera SOLO con NYSE aperto.
    # Mercati chiusi → skip silenzioso. Le crypto sono dominio del
    # Decision Crypto agent (cron 1h indipendente). Niente più hybrid R1.
    # ──────────────────────────────────────────────────────────────
    try:
        from scheduler import is_market_open
        market_open = is_market_open()
    except Exception:
        market_open = False

    if not market_open:
        if _should_emit_block_log("market_closed"):
            logger.info("[%s][ORCHESTRATOR] Mercato chiuso — Decision normale skip "
                        "(Decision Crypto opera in autonomia ogni ora).", run_id)
            try:
                database.insert_agent_log(run_id, "ORCHESTRATOR", json.dumps({
                    "event": "decision_skipped_market_closed",
                    "urgency": urgency,
                    "reason": reason,
                }))
            except Exception:
                pass
        return {
            "run_id": run_id,
            "triggered": False,
            "urgency": urgency,
            "reason": reason,
            "blocked": "market_closed",
            "duration_seconds": round(time.time() - start, 2),
        }

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

    # ─── WORKFLOW A 4 FASI: Technical NON eseguito a priori ─────────────
    # Il Decision Agent deve seguire il workflow obbligatorio:
    #   FASE 1: commit_initial_assessment (analisi situazione corrente)
    #   FASE 2: request_technical_analysis (richiesta dati tecnici)
    #   FASE 3: commit_final_thesis (tesi integrata)
    #   FASE 4: execute_trade / do_nothing
    # Passiamo tech_report=None così il Decision si rende conto e chiede.
    tech_report = None

    # ─── Decision Agent ───
    # Passiamo i focus_tickers + reason dal Watchdog cosi' il Decision sa
    # quali ticker DEVE prioritizzare (prima il bug: il modello sceglieva
    # ticker autonomamente ignorando il trigger).
    try:
        from agents.decision import run_decision_agent
        decision_result = await run_decision_agent(
            run_id, tech_report,
            focus_tickers=focus_tickers,
            watchdog_reason=reason,
        )
    except Exception as e:
        # Logga eccezione COMPLETA nel DB (visibile dal frontend) — finora
        # vedevamo solo "decision: ERROR" senza traceback nel pannello logs.
        import traceback as _tb
        tb_str = _tb.format_exc()
        logger.error("[%s][ORCHESTRATOR] Decision fallito: %s\n%s", run_id, e, tb_str)
        try:
            database.insert_agent_log(run_id, "DECISION_ERROR", json.dumps({
                "event": "decision_exception",
                "error_type": type(e).__name__,
                "error_message": str(e)[:500],
                "traceback": tb_str[:2000],
            }, default=str))
        except Exception:
            pass
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
        "tech_engine": (tech_report or {}).get("engine", "deferred_to_decision"),
        "decision": decision_result.get("decision", "UNKNOWN"),
        "trades": decision_result.get("trades", []),
        "duration_seconds": duration,
    }


# ============================================================
# Pipeline Crypto (scheduled ogni 1h, 24/7)
# ============================================================

async def run_crypto_pipeline(run_id: str | None = None) -> dict:
    """
    Pipeline crypto-only ogni 1h, 24/7. Bypassa Watchdog.

    Sequenza:
      1. Technical Crypto (DeepSeek-V3) sul universo crypto top-6
      2. Decision Crypto (DeepSeek-R1) con tech_report + buffer + doc crypto
      3. Cooldown 50min per evitare doppi run da test manuali

    Costo per run: ~$0.0006 (Tech V3) + ~$0.006 (Decision R1) ≈ $0.007
    24 run/giorno × $0.007 = $0.17/giorno ≈ $5/mese
    """
    if not run_id:
        run_id = str(uuid4())

    start = time.time()
    logger.info("[%s][ORCHESTRATOR] Crypto pipeline avviata", run_id)

    # 1. Cooldown check
    try:
        from agents.decision_crypto import is_cooldown_active
        active, seconds_left = is_cooldown_active()
        if active:
            logger.info("[%s][ORCHESTRATOR] Cooldown crypto attivo (%ds) — skip",
                        run_id, seconds_left)
            return {"run_id": run_id, "skipped": "cooldown",
                    "cooldown_seconds_left": seconds_left,
                    "duration_seconds": round(time.time() - start, 2)}
    except Exception:
        pass

    # 2. Tickers default — top 6 crypto liquidità
    crypto_tickers = ["BTC-USD", "ETH-USD", "SOL-USD",
                      "DOGE-USD", "AVAX-USD", "LINK-USD"]

    database.insert_agent_log(run_id, "ORCHESTRATOR", json.dumps({
        "event": "crypto_pipeline_start",
        "tickers": crypto_tickers,
        "trigger_source": "scheduler_1h",
    }))

    # 3. WORKFLOW A 4 FASI: Technical Crypto NON eseguito a priori.
    # Il Decision Crypto deve richiederlo via tool durante FASE 2.
    tech_report = None

    # 4. Decision Crypto
    try:
        from agents.decision_crypto import run_crypto_decision
        decision_result = await run_crypto_decision(run_id, tech_report)
    except Exception as e:
        import traceback as _tb
        logger.error("[%s][ORCHESTRATOR] Decision Crypto fallito: %s\n%s",
                     run_id, e, _tb.format_exc())
        try:
            database.insert_agent_log(run_id, "DECISION_CRYPTO_ERROR", json.dumps({
                "error_type": type(e).__name__,
                "error_message": str(e)[:500],
                "traceback": _tb.format_exc()[:2000],
            }, default=str))
        except Exception:
            pass
        decision_result = {"decision": "ERROR", "trades": [], "error": str(e)}

    duration = round(time.time() - start, 1)
    database.insert_agent_log(run_id, "ORCHESTRATOR", json.dumps({
        "event": "crypto_pipeline_complete",
        "tech_engine": (tech_report or {}).get("engine", "deferred_to_decision"),
        "decision": decision_result.get("decision", "UNKNOWN"),
        "trades": len(decision_result.get("trades", [])),
        "duration_seconds": duration,
    }))

    return {
        "run_id": run_id,
        "tech_engine": (tech_report or {}).get("engine", "deferred_to_decision"),
        "decision": decision_result.get("decision", "UNKNOWN"),
        "trades": decision_result.get("trades", []),
        "duration_seconds": duration,
    }


# ============================================================
# Pipeline Decision 24h (DEPRECATED - sostituita da run_crypto_pipeline)
# ============================================================

async def run_scheduled_24h_pipeline(run_id: str | None = None) -> dict:
    """
    Pipeline SCHEDULED Decision 24h — bypassa il Watchdog.

    Eseguita dallo scheduler ogni 2h30 quando i mercati equity sono chiusi.
    Va dritta a Technical + Decision R1 con focus su crypto top-liquidity
    (BTC/ETH/SOL/AVAX/SOL/...). Logica:

      1. Se il mercato è APERTO → skip silenzioso (Sonnet gestisce orario).
      2. Se il cooldown R1 è ancora attivo → skip (ancora troppo presto).
      3. Altrimenti → Technical (DeepSeek-V3) → Decision (DeepSeek-R1).
         Decision R1 valuta autonomamente se operare o do_nothing.

    Differenza vs run_watchdog_pipeline:
      - NON dipende dal Watchdog (gira a tempo, non a evento)
      - Force engine: DECISION_ENGINE=deepseek-r1 (env override temporanea)
      - Default crypto top-3 se non c'è altra indicazione

    Costo per run: ~$0.001 V3 (Technical) + ~$0.006 R1 (Decision) = ~$0.007.
    Frequenza: 2h30 → ~10 run/giorno overnight × $0.007 = ~$0.07/giorno
    durante weekend/festività ($0.50/mese in modalità always-closed).
    """
    import os as _os

    if not run_id:
        run_id = str(uuid4())

    start = time.time()
    logger.info("[%s][ORCHESTRATOR] Decision 24h SCHEDULED pipeline avviata", run_id)

    # 1) Skip se mercato aperto (Sonnet gestisce questa fascia)
    try:
        from scheduler import is_market_open
        if is_market_open():
            logger.debug("[%s][ORCHESTRATOR] Mercati aperti — Decision 24h skipped (Sonnet attivo)", run_id)
            return {"run_id": run_id, "skipped": "market_open", "duration_seconds": round(time.time() - start, 2)}
    except Exception:
        pass  # se non riesco a verificare, procedo (fail-open)

    # 2) Soft anti-double-run: skip SOLO se R1 ha girato negli ultimi 60 min.
    # Il job stesso gira a 2h30 quindi non c'è bisogno del cooldown completo
    # — usiamo solo una finestra breve per evitare doppio run quando il
    # watchdog ha triggerato R1 pochi minuti prima del nostro scheduled fire.
    # PRIMA c'era un check del cooldown completo 2h30: race condition con la
    # schedule a 2h30 → il job fire 22 secondi PRIMA della scadenza, skippa,
    # e poi attende altri 2h30 = blackout di ~5h tra un R1 e l'altro.
    try:
        from agents.decision import R1_LAST_RUN_KEY
        last_iso = database.get_setting(R1_LAST_RUN_KEY, "") or ""
        if last_iso:
            last_dt = datetime.fromisoformat(last_iso)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            elapsed_min = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60.0
            if elapsed_min < 60:
                logger.info("[%s][ORCHESTRATOR] R1 ha girato %.1f min fa — skip scheduled (soft 60min)",
                            run_id, elapsed_min)
                return {
                    "run_id": run_id, "skipped": "recent_r1_run",
                    "elapsed_minutes": round(elapsed_min, 1),
                    "duration_seconds": round(time.time() - start, 2),
                }
    except Exception:
        pass  # fail-open: meglio runnare due volte che mai

    # 3) Tickers di default per il run scheduled crypto-only
    hot_tickers = list(_DEFAULT_OVERNIGHT_CRYPTO)

    database.insert_agent_log(run_id, "ORCHESTRATOR", json.dumps({
        "event": "decision_24h_scheduled_start",
        "focus_tickers": hot_tickers,
        "trigger_source": "scheduler_2h30",
    }))

    # ── WORKFLOW A 4 FASI: Technical NON eseguito a priori (24h scheduled) ──
    # Il Decision Agent (R1) chiede i dati tecnici durante FASE 2.
    tech_report = None

    # ── Decision Agent — FORZA engine R1 via env override temporanea ──
    # Salva il valore corrente, lo setta su deepseek-r1 SOLO per questo run,
    # poi lo ripristina (così durante orari di mercato torna a Sonnet).
    saved_engine = _os.environ.get("DECISION_ENGINE", "")
    _os.environ["DECISION_ENGINE"] = "deepseek-r1"
    try:
        from agents.decision import run_decision_agent
        decision_result = await run_decision_agent(run_id, tech_report)
    except Exception as e:
        logger.error("[%s][ORCHESTRATOR] Decision fallito (scheduled 24h): %s", run_id, e)
        decision_result = {"decision": "ERROR", "trades": [], "error": str(e)}
    finally:
        if saved_engine:
            _os.environ["DECISION_ENGINE"] = saved_engine
        else:
            _os.environ.pop("DECISION_ENGINE", None)

    duration = round(time.time() - start, 1)

    database.insert_agent_log(run_id, "ORCHESTRATOR", json.dumps({
        "event": "decision_24h_scheduled_complete",
        "decision": decision_result.get("decision", "UNKNOWN"),
        "trades": len(decision_result.get("trades", [])),
        "engine": decision_result.get("model", "deepseek-r1"),
        "duration_seconds": duration,
    }))

    return {
        "run_id": run_id,
        "scheduled": True,
        "tech_engine": (tech_report or {}).get("engine", "deferred_to_decision"),
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

    # 3. WORKFLOW A 4 FASI: Technical NON pre-eseguito (manual full).
    # Il Decision Agent fa request_technical_analysis durante FASE 2.
    tech_report = None
    result["phases"]["technical"] = {"status": "skipped_workflow_4fasi"}

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
