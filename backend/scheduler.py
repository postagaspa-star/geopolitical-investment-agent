"""
Scheduler autonomo h24 per l'agente geopolitico di investimento.
Esegue l'agente ogni 20 minuti con comportamento diverso in base
all'orario e al giorno (weekend / pre-market / mercato aperto).
"""

import asyncio
import logging
import os
from datetime import datetime

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
    Controlla se almeno una borsa principale e' aperta.
    Borse considerate: NYSE/NASDAQ (ET), LSE (GMT), Euronext (CET).
    Restituisce True se almeno una e' aperta, False altrimenti.
    """
    now_utc = datetime.now(pytz.utc)

    # Weekend: sabato e domenica = borse chiuse
    if now_utc.weekday() >= 5:
        return False

    # NYSE/NASDAQ: 9:30-16:00 ET
    now_et = now_utc.astimezone(pytz.timezone('America/New_York'))
    nyse_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    nyse_close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    if nyse_open <= now_et <= nyse_close:
        return True

    # LSE: 8:00-16:30 GMT
    now_gmt = now_utc.astimezone(pytz.timezone('Europe/London'))
    lse_open = now_gmt.replace(hour=8, minute=0, second=0, microsecond=0)
    lse_close = now_gmt.replace(hour=16, minute=30, second=0, microsecond=0)
    if lse_open <= now_gmt <= lse_close:
        return True

    # Euronext (Parigi, Milano, Francoforte): 9:00-17:30 CET
    now_cet = now_utc.astimezone(pytz.timezone('Europe/Paris'))
    eu_open = now_cet.replace(hour=9, minute=0, second=0, microsecond=0)
    eu_close = now_cet.replace(hour=17, minute=30, second=0, microsecond=0)
    if eu_open <= now_cet <= eu_close:
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

async def _scheduled_agent_job():
    """
    Job principale eseguito ogni 20 minuti.
    Determina la modalita' e esegue l'agente di conseguenza.
    """
    global current_mode

    try:
        mode = get_current_mode()
        current_mode = mode

        logger.info("Scheduler job avviato - Modalita': %s", mode.upper())

        # Importazione ritardata per evitare dipendenze circolari
        from agent import run_agent

        # Pulizia periodica degli articoli vecchi
        database.cleanup_old_processed_articles(days=7)

        # Esegui l'agente con la modalita' appropriata
        await run_agent(mode=mode)

        logger.info("Scheduler job completato - Modalita': %s", mode.upper())

    except Exception as e:
        logger.error("Errore nel job schedulato: %s", e, exc_info=True)


def _sync_job_wrapper():
    """Wrapper sincrono che avvia il job asincrono nel loop corrente."""
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(_scheduled_agent_job())
    except Exception as e:
        logger.error("Errore nell'avvio del task schedulato: %s", e, exc_info=True)


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


def _sync_keep_alive_wrapper():
    """Wrapper sincrono per il keep-alive ping."""
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(_keep_alive_ping())
    except Exception:
        pass


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


def _sync_daily_recap_wrapper():
    """Wrapper sincrono per il Daily Recap."""
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(_daily_recap_job())
    except Exception as e:
        logger.error("Errore avvio Daily Recap task: %s", e)


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


def _sync_weekly_matrix_wrapper():
    """Wrapper sincrono per la Weekly Matrix."""
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(_weekly_matrix_job())
    except Exception as e:
        logger.error("Errore avvio Weekly Matrix task: %s", e)


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

    _scheduler.add_job(
        _sync_job_wrapper,
        trigger="interval",
        minutes=interval_minutes,
        id="agent_continuous_job",
        name="Monitoraggio continuo agente geopolitico",
        replace_existing=True,
    )

    # Keep-alive: pinga il servizio ogni 10 minuti per evitare lo sleep di Render
    _scheduler.add_job(
        _sync_keep_alive_wrapper,
        trigger="interval",
        minutes=10,
        id="keep_alive_ping",
        name="Keep-alive ping (anti-sleep Render)",
        replace_existing=True,
    )

    # Daily Recap: ogni giorno alle 23:59 CET
    from apscheduler.triggers.cron import CronTrigger
    _scheduler.add_job(
        _sync_daily_recap_wrapper,
        trigger=CronTrigger(hour=22, minute=59, timezone="Europe/Paris"),
        id="scout_daily_recap",
        name="Scout Daily Recap (23:59 CET)",
        replace_existing=True,
    )

    # Weekly Matrix: ogni domenica alle 23:59 CET
    _scheduler.add_job(
        _sync_weekly_matrix_wrapper,
        trigger=CronTrigger(day_of_week="sun", hour=23, minute=59, timezone="Europe/Paris"),
        id="scout_weekly_matrix",
        name="Scout Weekly Matrix (domenica 23:59 CET)",
        replace_existing=True,
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


def get_scheduler_info() -> dict:
    """Restituisce informazioni sullo stato dello scheduler."""
    running = is_scheduler_running()
    mode = get_current_mode() if running else "idle"
    market_open = is_market_open()

    info = {
        "running": running,
        "mode": mode,
        "market_open": market_open,
        "is_weekend": is_weekend(),
        "next_run": None,
        "last_run": None,
    }

    if _scheduler and running:
        job = _scheduler.get_job("agent_continuous_job")
        if job:
            if job.next_run_time:
                info["next_run"] = job.next_run_time.isoformat()

    # Prossima apertura mercato (se chiuso)
    if not market_open:
        try:
            info["next_market_open"] = get_next_market_open().isoformat()
        except Exception:
            info["next_market_open"] = None

    return info
