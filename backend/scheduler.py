"""
Scheduler autonomo h24 per l'agente geopolitico di investimento.
Esegue l'agente ogni 20 minuti con comportamento diverso in base
all'orario e al giorno (weekend / pre-market / mercato aperto).
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import database

# URL del servizio per il self-ping keep-alive (Render free tier)
_RENDER_EXTERNAL_URL = os.environ.get(
    "RENDER_EXTERNAL_URL",
    "https://geopolitical-investment-agent.onrender.com"
)

logger = logging.getLogger(__name__)

# Istanza globale dello scheduler
_scheduler: AsyncIOScheduler | None = None

# Stato corrente della modalita' agente
current_mode: str = "idle"  # idle | weekend | pre_market | full


# ============================================================
# Funzioni di rilevamento stato mercato
# ============================================================

def is_market_open():
    """
    Controlla se almeno una delle borse OBIETTIVO e' aperta.
    Borse considerate (definite dall'utente): USA (NYSE/NASDAQ), UK (LSE),
    Germania (XETRA/Francoforte). Restituisce True se almeno una e' aperta.

    Watchdog e Decision Agent operano SOLO quando questa funzione restituisce True.
    Lo Scout invece gira sempre (24/7).
    """
    now_utc = datetime.now(pytz.utc)

    # Weekend: sabato e domenica = borse chiuse ovunque
    if now_utc.weekday() >= 5:
        return False

    # USA — NYSE/NASDAQ: 9:30-16:00 ET (Eastern Time, gestisce DST automaticamente)
    now_et = now_utc.astimezone(pytz.timezone('America/New_York'))
    nyse_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    nyse_close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    if nyse_open <= now_et <= nyse_close:
        return True

    # UK — LSE: 8:00-16:30 ora di Londra (GMT/BST)
    now_uk = now_utc.astimezone(pytz.timezone('Europe/London'))
    lse_open = now_uk.replace(hour=8, minute=0, second=0, microsecond=0)
    lse_close = now_uk.replace(hour=16, minute=30, second=0, microsecond=0)
    if lse_open <= now_uk <= lse_close:
        return True

    # Germania — XETRA Francoforte: 9:00-17:30 ora di Berlino (CET/CEST)
    now_de = now_utc.astimezone(pytz.timezone('Europe/Berlin'))
    xetra_open = now_de.replace(hour=9, minute=0, second=0, microsecond=0)
    xetra_close = now_de.replace(hour=17, minute=30, second=0, microsecond=0)
    if xetra_open <= now_de <= xetra_close:
        return True

    return False


def is_weekend():
    """Restituisce True se e' sabato o domenica (UTC)."""
    return datetime.now(pytz.utc).weekday() >= 5


def get_current_mode():
    """Determina la modalita' corrente dell'agente."""
    if is_weekend():
        return "weekend"
    elif is_market_open():
        return "full"
    else:
        return "pre_market"


def get_next_market_open():
    """
    Calcola il prossimo orario di apertura di mercato (Euronext 9:00 CET
    come prima apertura in settimana).
    Restituisce un datetime UTC.
    """
    now_utc = datetime.now(pytz.utc)
    cet = pytz.timezone('Europe/Paris')
    now_cet = now_utc.astimezone(cet)

    # Parti da domani e cerca il prossimo giorno feriale
    from datetime import timedelta
    candidate = now_cet + timedelta(days=1)
    candidate = candidate.replace(hour=9, minute=0, second=0, microsecond=0)

    while candidate.weekday() >= 5:  # Salta weekend
        candidate += timedelta(days=1)

    # Se oggi e' un giorno feriale e non ha ancora aperto
    today_open = now_cet.replace(hour=9, minute=0, second=0, microsecond=0)
    if now_cet.weekday() < 5 and now_cet < today_open:
        candidate = today_open

    return candidate.astimezone(pytz.utc)


# ============================================================
# Job principale dello scheduler
# ============================================================

async def _watchdog_job():
    """
    Job Watchdog — ogni 1 minuto, 24/7.
    Ultra-leggero: DeepSeek-V3 decide se triggerare Technical+Decision.
    Se trigger=False, costo quasi zero. Se trigger=True, avvia pipeline completa.

    NOTA: gira 24/7 perche' le crypto (BTC-USD, ETH-USD, ecc.) vivono sempre,
    e movimenti rilevanti accadono spesso di notte e nel weekend.
    Il filtro DeepSeek decide autonomamente se l'evento merita la pipeline pesante.

    Costo stimato: ~1440 chiamate/giorno DeepSeek (~$1/mese).
    """
    global current_mode

    # Modalita' dinamica: 'full' durante orari di mercato, 'crypto_24h' fuori orario
    current_mode = "full" if is_market_open() else "crypto_24h"
    try:
        from uuid import uuid4
        from agents.orchestrator import run_watchdog_pipeline
        run_id = str(uuid4())
        result = await run_watchdog_pipeline(run_id=run_id)
        if result.get("triggered"):
            logger.info("Watchdog TRIGGER: urgency=%d, decision=%s",
                        result.get("urgency", 0), result.get("decision", "?"))
        else:
            logger.debug("Watchdog: no trigger (reason='%s')", result.get("reason", ""))
    except Exception as e:
        logger.error("Errore nel job Watchdog: %s", e, exc_info=True)


async def _price_polling_job():
    """
    Job Price Polling — ogni 60 secondi.
    Aggiorna price_quotes con prezzi yfinance per posizioni aperte + watchlist.
    Frequenza ridotta a ogni 5 min se mercati chiusi (per snapshot di chiusura).
    """
    try:
        from price_polling import update_price_cache
        await update_price_cache()
    except Exception as e:
        logger.error("Errore Price Polling: %s", e, exc_info=False)


async def _scout_hourly_job():
    """
    Job Scout — ogni 20 minuti, sempre (24/7, anche weekend e fuori orario).
    Sonnet 4.5 raccoglie e analizza notizie da GDELT, yFinance News, NewsAPI,
    Reddit (sentiment retail) e X. Popola intelligence_buffer.
    """
    global current_mode

    mode = get_current_mode()
    current_mode = mode

    try:
        from uuid import uuid4
        from agents.orchestrator import run_scout_pipeline
        run_id = str(uuid4())
        result = await run_scout_pipeline(run_id=run_id)
        logger.info("Scout orario completato: %d micro-schede", result.get("cards", 0))

        # Pulizia periodica
        database.cleanup_old_processed_articles(days=7)

    except Exception as e:
        logger.error("Errore nel job Scout orario: %s", e, exc_info=True)


async def _scheduled_agent_job():
    """
    Job legacy — mantenuto per compatibilità ma NON più usato dallo scheduler principale.
    Sostituito da _watchdog_job (ogni 5 min) + _scout_hourly_job (ogni ora).
    """
    global current_mode

    try:
        mode = get_current_mode()
        current_mode = mode

        logger.info("Scheduler job legacy avviato - Modalita': %s", mode.upper())

        from agent import run_agent
        database.cleanup_old_processed_articles(days=7)
        await run_agent(mode=mode)

        logger.info("Scheduler job legacy completato - Modalita': %s", mode.upper())

    except Exception as e:
        logger.error("Errore nel job schedulato: %s", e, exc_info=True)


async def _keep_alive_ping():
    """
    Pinga il proprio health endpoint per evitare che Render free tier
    metta il servizio in sleep dopo 15 minuti di inattivita'.
    """
    import aiohttp
    url = f"{_RENDER_EXTERNAL_URL}/health"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(url) as resp:
                logger.debug("Keep-alive ping: %s -> %d", url, resp.status)
    except Exception as e:
        logger.warning("Keep-alive ping fallito: %s", e)


async def _daily_recap_job():
    """Job per il Daily Recap dello Scout (23:59 CET)."""
    from uuid import uuid4
    run_id = str(uuid4())
    try:
        from agents.scout import run_daily_recap
        result = await run_daily_recap(run_id)
        logger.info("Daily Recap completato: %s", result.get("macro_bias", "?"))
    except Exception as e:
        logger.error("Errore Daily Recap: %s", e, exc_info=True)


async def _clawstreet_reconcile_job():
    """
    Riconciliazione automatica ClawStreet ogni ora durante orari di mercato.
    Confronta i trade locali con quelli su ClawStreet e invia quelli mancanti.
    Aiuta a mantenere allineata la vetrina pubblica con il portafoglio interno.
    """
    if not is_market_open():
        return  # Eseguiamo solo durante orari di mercato per non spammare CS

    try:
        cs_bot_id = database.get_setting("clawstreet_bot_id", "") or os.environ.get("CLAWSTREET_BOT_ID", "")
        cs_api_key = database.get_setting("clawstreet_api_key", "") or os.environ.get("CLAWSTREET_API_KEY", "")
        if not cs_bot_id or not cs_api_key or cs_bot_id == "GEO":
            return

        # Riusa la stessa logica dell'endpoint /api/clawstreet/reconcile
        # importandolo direttamente
        import aiohttp
        url = f"https://geopolitical-investment-agent.onrender.com/api/clawstreet/reconcile?since_hours=24"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    sent = data.get("sent", 0)
                    if sent > 0:
                        logger.info("ClawStreet reconcile: %d trade specchiati automaticamente", sent)
    except Exception as e:
        logger.warning("Errore reconcile ClawStreet automatico: %s", e)


async def _weekly_matrix_job():
    """Job per la Weekly Matrix dello Scout (domenica 23:59 CET)."""
    from uuid import uuid4
    run_id = str(uuid4())
    try:
        from agents.scout import run_weekly_matrix
        result = await run_weekly_matrix(run_id)
        logger.info("Weekly Matrix completata: %s", result.get("macro_strategy", "?")[:100])
    except Exception as e:
        logger.error("Errore Weekly Matrix: %s", e, exc_info=True)


# ============================================================
# Gestione scheduler
# ============================================================

def start_scheduler() -> AsyncIOScheduler:
    """
    Avvia lo scheduler con intervallo di 20 minuti.
    Salva lo stato nel database.
    """
    global _scheduler

    if _scheduler is not None:
        logger.warning("Scheduler gia' attivo, nessuna azione necessaria.")
        return _scheduler

    # Leggi intervallo da impostazioni (ore), con fallback a 20 minuti
    try:
        hours_str = database.get_setting("agent_run_interval_hours", None)
        if hours_str is not None:
            interval_minutes = max(5, int(float(hours_str) * 60))
        else:
            interval_minutes = 20  # Default: 20 minuti
    except (ValueError, TypeError):
        interval_minutes = 20

    logger.info("Avvio scheduler autonomo (intervallo: %d minuti)...", interval_minutes)

    _scheduler = AsyncIOScheduler()

    # ── Price Polling: ogni 60 secondi (yfinance → Supabase) ──
    # AsyncIOScheduler accetta funzioni async direttamente — niente sync wrapper.
    _scheduler.add_job(
        _price_polling_job,
        trigger="interval",
        seconds=60,
        id="price_polling_job",
        name="Price Polling 60s (yfinance cache)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # ── Watchdog: ogni 1 minuto, MA solo durante orari di mercato (US/UK/DE) ──
    # Il check interno is_market_open() fa exit immediato fuori orario.
    _scheduler.add_job(
        _watchdog_job,
        trigger="interval",
        minutes=1,
        id="watchdog_job",
        name="Watchdog 1min market-only (DeepSeek-V3 trigger filter)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # ── Scout: ogni 20 minuti, 24/7 (Sonnet 4.5 — raccolta intelligence) ──
    _scheduler.add_job(
        _scout_hourly_job,
        trigger="interval",
        minutes=20,
        id="scout_hourly_job",
        name="Scout 20min (Sonnet 4.5 intelligence buffer 24/7)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        # Avvia il primo run dopo 30 secondi dall'avvio dello scheduler
        next_run_time=datetime.now(pytz.utc) + timedelta(seconds=30),
    )

    # Keep-alive: pinga il servizio ogni 10 minuti per evitare lo sleep di Render
    _scheduler.add_job(
        _keep_alive_ping,
        trigger="interval",
        minutes=10,
        id="keep_alive_ping",
        name="Keep-alive ping (anti-sleep Render)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Daily Recap: ogni giorno alle 23:59 CET
    from apscheduler.triggers.cron import CronTrigger
    _scheduler.add_job(
        _daily_recap_job,
        trigger=CronTrigger(hour=22, minute=59, timezone="Europe/Paris"),
        id="scout_daily_recap",
        name="Scout Daily Recap (23:59 CET)",
        replace_existing=True,
        max_instances=1,
    )

    # Weekly Matrix: ogni domenica alle 23:59 CET
    _scheduler.add_job(
        _weekly_matrix_job,
        trigger=CronTrigger(day_of_week="sun", hour=23, minute=59, timezone="Europe/Paris"),
        id="scout_weekly_matrix",
        name="Scout Weekly Matrix (domenica 23:59 CET)",
        replace_existing=True,
        max_instances=1,
    )

    # ClawStreet auto-reconcile: ogni ora durante orari di mercato
    _scheduler.add_job(
        _clawstreet_reconcile_job,
        trigger="interval",
        minutes=60,
        id="clawstreet_reconcile",
        name="ClawStreet auto-reconcile (60 min, market-only)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    _scheduler.start()

    # Salva stato nel database
    database.set_setting("agent_running", "true")

    logger.info("Scheduler avviato con successo. Prossimo run tra %d minuti.", interval_minutes)
    return _scheduler


def stop_scheduler(persist=True):
    """
    Ferma lo scheduler.
    Se persist=True (stop manuale da utente), salva lo stato nel DB.
    Se persist=False (shutdown durante deploy), NON aggiorna il DB
    cosi' al prossimo avvio lo scheduler riparte automaticamente.
    """
    global _scheduler, current_mode

    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
            logger.info("Scheduler arrestato con successo.")
        except Exception as e:
            logger.error("Errore durante l'arresto dello scheduler: %s", e, exc_info=True)
        finally:
            _scheduler = None
            current_mode = "idle"
    else:
        logger.warning("Tentativo di arrestare lo scheduler, ma non era attivo.")

    if persist:
        database.set_setting("agent_running", "false")


def is_scheduler_running() -> bool:
    """Restituisce True se lo scheduler e' attivo."""
    return _scheduler is not None and _scheduler.running


def _last_log_for_phases(phase_prefixes: list[str], limit: int = 200) -> str | None:
    """
    Cerca tra gli ultimi N log la prima entry il cui phase inizia con uno
    dei prefissi forniti. Restituisce il timestamp ISO o None.
    Usato per ricostruire l'ultimo run di Technical/Decision (job on-demand).
    """
    try:
        logs = database.get_agent_logs(limit=limit) or []
        for log in logs:
            phase = (log.get("phase") or "").upper()
            for prefix in phase_prefixes:
                if phase.startswith(prefix.upper()):
                    return log.get("timestamp")
    except Exception:
        return None
    return None


def get_scheduler_info() -> dict:
    """
    Restituisce informazioni sullo stato dello scheduler.
    Espone next_run / last_run per ogni agente del workflow:
      - watchdog (mini-Scout, ogni 1min market-only)
      - scout (Sonnet 4.5, ogni 20min 24/7)
      - technical (on-demand, triggerato da watchdog)
      - decision (on-demand, max 1/h, triggerato da watchdog)
    """
    running = is_scheduler_running()
    mode = get_current_mode() if running else "idle"
    market_open = is_market_open()

    # --- Per i job APScheduler (Watchdog, Scout) leggiamo direttamente next_run_time ---
    watchdog_next = None
    scout_next = None
    if _scheduler and running:
        wj = _scheduler.get_job("watchdog_job")
        if wj and wj.next_run_time:
            watchdog_next = wj.next_run_time.isoformat()
        sj = _scheduler.get_job("scout_hourly_job")
        if sj and sj.next_run_time:
            scout_next = sj.next_run_time.isoformat()

    # --- Per i job on-demand cerchiamo l'ultimo log corrispondente ---
    watchdog_last = _last_log_for_phases(["WATCHDOG_"])
    scout_last = _last_log_for_phases(["SCOUT_20MIN", "SCOUT_DAILY", "SCOUT_WEEKLY"])
    technical_last = _last_log_for_phases(["TECH_", "TECHNICAL_"])
    decision_last = _last_log_for_phases(["DECISION_"])

    agents = {
        "watchdog": {
            "schedule": "ogni 1 min (24/7, equity + crypto)",
            "next_run": watchdog_next,
            "last_run": watchdog_last,
            "active": running,
        },
        "scout": {
            "schedule": "ogni 20 min (24/7)",
            "next_run": scout_next,
            "last_run": scout_last,
            "active": running,
        },
        "technical": {
            "schedule": "on-demand (trigger Watchdog)",
            "next_run": None,
            "last_run": technical_last,
            "active": running,
        },
        "decision": {
            "schedule": "on-demand (trigger Watchdog, max 1/ora)",
            "next_run": None,
            "last_run": decision_last,
            "active": running,
        },
    }

    info = {
        "running": running,
        "mode": mode,
        "market_open": market_open,
        "is_weekend": is_weekend(),
        # Backward compat: next_run = watchdog (il job piu' frequente)
        "next_run": watchdog_next,
        "last_run": watchdog_last,
        "agents": agents,
    }

    # Prossima apertura mercato (se chiuso)
    if not market_open:
        try:
            info["next_market_open"] = get_next_market_open().isoformat()
        except Exception:
            info["next_market_open"] = None

    return info
