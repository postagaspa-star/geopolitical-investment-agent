"""
Scheduler autonomo h24 per l'agente geopolitico di investimento.
Esegue l'agente ogni 20 minuti con comportamento diverso in base
all'orario e al giorno (weekend / pre-market / mercato aperto).
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

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

# ── Festività NYSE/NASDAQ (USA) — 2025-2027 ───────────────────────────────
# In questi giorni la borsa USA è CHIUSA anche se è un giorno feriale.
# Aggiornare ogni anno (NYSE pubblica il calendario ufficiale ~12 mesi prima).
# Senza questo controllo, is_market_open() restituiva True nei festivi USA,
# il bot apriva trade su S&P 500 nei giorni di chiusura → divergenza.
NYSE_HOLIDAYS = {
    # 2025
    "2025-01-01", "2025-01-09",  # New Year, Day of mourning Carter
    "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
    "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03",  # 4 luglio cade di sabato → osservato il 3
    "2026-09-07", "2026-11-26", "2026-12-25",
    # 2027
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18",  # 19 giugno cade di sabato → osservato il 18
    "2027-07-05",  # 4 luglio cade di domenica → osservato il 5
    "2027-09-06", "2027-11-25", "2027-12-24",  # 25 dic cade di sabato → 24
    # 2028 (estensione: bug precedente scadeva nel 2027 → bot apriva trade
    # equity nei festivi US 2028)
    "2028-01-17", "2028-02-21", "2028-04-14", "2028-05-29",
    "2028-06-19", "2028-07-04", "2028-09-04", "2028-11-23", "2028-12-25",
    # 2029
    "2029-01-01", "2029-01-15", "2029-02-19", "2029-03-30", "2029-05-28",
    "2029-06-19", "2029-07-04", "2029-09-03", "2029-11-22", "2029-12-25",
}

# Warning automatico: se siamo a meno di 6 mesi dalla scadenza dei festivi
# definiti, logga un warning all'avvio per ricordare di aggiornarli.
def _check_holiday_calendar_freshness():
    """Logga warning se il calendario festivi sta scadendo."""
    from datetime import datetime as _dt
    try:
        max_year = max(int(d.split("-")[0]) for d in NYSE_HOLIDAYS)
        # 6 mesi prima dell'ultimo anno coperto → warning
        warning_cutoff = _dt(max_year, 7, 1)
        if _dt.now() > warning_cutoff:
            logger = __import__("logging").getLogger(__name__)
            logger.warning(
                "NYSE_HOLIDAYS coperti solo fino al %d. AGGIORNARE in scheduler.py "
                "(stesso pattern per LSE_HOLIDAYS e XETRA_HOLIDAYS).", max_year
            )
    except Exception:
        pass

# ── Festività LSE (UK Bank Holidays) — 2025-2027 ──────────────────────────
LSE_HOLIDAYS = {
    "2025-01-01", "2025-04-18", "2025-04-21", "2025-05-05", "2025-05-26",
    "2025-08-25", "2025-12-25", "2025-12-26",
    "2026-01-01", "2026-04-03", "2026-04-06", "2026-05-04", "2026-05-25",
    "2026-08-31", "2026-12-25", "2026-12-28",  # 26 dic cade di sabato → 28
    "2027-01-01", "2027-03-26", "2027-03-29", "2027-05-03", "2027-05-31",
    "2027-08-30", "2027-12-27", "2027-12-28",
}

# ── Festività XETRA (Germania) — 2025-2027 ────────────────────────────────
XETRA_HOLIDAYS = {
    "2025-01-01", "2025-04-18", "2025-04-21", "2025-05-01", "2025-12-24",
    "2025-12-25", "2025-12-26", "2025-12-31",
    "2026-01-01", "2026-04-03", "2026-04-06", "2026-05-01", "2026-12-24",
    "2026-12-25", "2026-12-31",
    "2027-01-01", "2027-03-26", "2027-03-29", "2027-12-24", "2027-12-27",
    "2027-12-31",
}


def is_market_open():
    """
    Controlla se almeno una delle borse OBIETTIVO e' aperta.
    Borse considerate (definite dall'utente): USA (NYSE/NASDAQ), UK (LSE),
    Germania (XETRA/Francoforte). Restituisce True se almeno una e' aperta.

    Tiene conto di festività ufficiali (NYSE_HOLIDAYS, LSE_HOLIDAYS, XETRA_HOLIDAYS):
    se è un giorno feriale ma è festivo per la borsa, quel mercato è chiuso.

    Watchdog e Decision Agent operano SOLO quando questa funzione restituisce True
    (in modalità Sonnet 4.5). Quando ritorna False, l'agent passa in modalità
    overnight crypto con DeepSeek-R1.
    """
    now_utc = datetime.now(pytz.utc)

    # Weekend: sabato e domenica = borse chiuse ovunque
    if now_utc.weekday() >= 5:
        return False

    # USA — NYSE/NASDAQ: 9:30-16:00 ET (Eastern Time, gestisce DST automaticamente)
    now_et = now_utc.astimezone(pytz.timezone('America/New_York'))
    if now_et.strftime("%Y-%m-%d") not in NYSE_HOLIDAYS:
        nyse_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        nyse_close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
        if nyse_open <= now_et <= nyse_close:
            return True

    # UK — LSE: 8:00-16:30 ora di Londra (GMT/BST)
    now_uk = now_utc.astimezone(pytz.timezone('Europe/London'))
    if now_uk.strftime("%Y-%m-%d") not in LSE_HOLIDAYS:
        lse_open = now_uk.replace(hour=8, minute=0, second=0, microsecond=0)
        lse_close = now_uk.replace(hour=16, minute=30, second=0, microsecond=0)
        if lse_open <= now_uk <= lse_close:
            return True

    # Germania — XETRA Francoforte: 9:00-17:30 ora di Berlino (CET/CEST)
    now_de = now_utc.astimezone(pytz.timezone('Europe/Berlin'))
    if now_de.strftime("%Y-%m-%d") not in XETRA_HOLIDAYS:
        xetra_open = now_de.replace(hour=9, minute=0, second=0, microsecond=0)
        xetra_close = now_de.replace(hour=17, minute=30, second=0, microsecond=0)
        if xetra_open <= now_de <= xetra_close:
            return True

    return False


def is_market_holiday() -> bool:
    """
    True se OGGI è un festivo per ALMENO una delle 3 borse target.
    Usato per logging e per disabilitare trade equity nei giorni festivi
    anche se l'orario formalmente combacia con l'apertura.
    """
    now_utc = datetime.now(pytz.utc)
    if now_utc.weekday() >= 5:
        return False  # weekend, non "holiday"
    today_et = now_utc.astimezone(pytz.timezone('America/New_York')).strftime("%Y-%m-%d")
    today_uk = now_utc.astimezone(pytz.timezone('Europe/London')).strftime("%Y-%m-%d")
    today_de = now_utc.astimezone(pytz.timezone('Europe/Berlin')).strftime("%Y-%m-%d")
    return (today_et in NYSE_HOLIDAYS
            or today_uk in LSE_HOLIDAYS
            or today_de in XETRA_HOLIDAYS)


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


_last_scout_run_ts: float = 0.0


async def _scout_hourly_job():
    """
    Job Scout — ogni 20 minuti durante la settimana lavorativa (lun-ven),
    ridotto a ogni 60 minuti (ogni ora) su weekend e festivi.

    Razionale: il bot opera anche su crypto 24/7 (Decision Crypto + R1) e
    serve intelligence costante anche nei weekend per le crypto. Cadenza 60min
    nei weekend = 24 run/giorno (vs 72 weekday) = -67% di chiamate. Buon
    compromesso tra costo token e copertura news.

    Implementazione: l'APScheduler fa partire il job ogni 20 min, ma se siamo
    nel weekend/festivo e l'ultimo run e' < 60 min fa, esce subito (no-op).
    """
    global current_mode, _last_scout_run_ts

    mode = get_current_mode()
    current_mode = mode

    # Gate weekend/holiday: cadenza 60 min invece di 20 min
    now_ts = time.time()
    is_off_market = is_weekend() or is_market_holiday()
    if is_off_market:
        gap_seconds = now_ts - _last_scout_run_ts
        if gap_seconds < 3600:  # 60 min in secondi
            mins_remaining = (3600 - gap_seconds) / 60.0
            logger.info(
                "Scout skip (weekend/holiday): ultimo run %.0f min fa, "
                "cadenza 60min → ~%.0f min al prossimo run",
                gap_seconds / 60.0, mins_remaining,
            )
            return

    _last_scout_run_ts = now_ts

    try:
        from uuid import uuid4
        from agents.orchestrator import run_scout_pipeline
        run_id = str(uuid4())
        result = await run_scout_pipeline(run_id=run_id)
        logger.info(
            "Scout completato (mode=%s): %d micro-schede",
            "weekend/holiday-60min" if is_off_market else "weekday-20min",
            result.get("cards", 0),
        )

        # Pulizia periodica
        database.cleanup_old_processed_articles(days=7)

    except Exception as e:
        logger.error("Errore nel job Scout: %s", e, exc_info=True)


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


async def _scout_8h_report_job():
    """
    Job aggregato 8H — ogni 8 ore.
    Lo Scout legge le micro-cards delle ultime 8h dal buffer, le sintetizza
    in un report 8H, e ELIMINA le micro-cards consumate.
    """
    from uuid import uuid4
    run_id = str(uuid4())
    try:
        from agents.scout import run_8h_report
        result = await run_8h_report(run_id)
        if result.get("skipped"):
            logger.info("Scout 8H skipped: %s", result.get("reason"))
        else:
            logger.info("Scout 8H completato: bias=%s, consumed=%d",
                        result.get("macro_bias", "?"), result.get("consumed_records", 0))
    except Exception as e:
        logger.error("Errore Scout 8H: %s", e, exc_info=True)


async def _scout_4d_report_job():
    """
    Job aggregato 4D — ogni 4 giorni.
    Lo Scout legge i report 8H degli ultimi 4 giorni, li sintetizza in un
    report 4D, e ELIMINA i report 8H consumati.
    """
    from uuid import uuid4
    run_id = str(uuid4())
    try:
        from agents.scout import run_4d_report
        result = await run_4d_report(run_id)
        if result.get("skipped"):
            logger.info("Scout 4D skipped: %s", result.get("reason"))
        else:
            logger.info("Scout 4D completato: bias=%s, consumed=%d 8H-reports",
                        result.get("macro_bias", "?"), result.get("consumed_records", 0))
    except Exception as e:
        logger.error("Errore Scout 4D: %s", e, exc_info=True)


async def _crypto_pipeline_job():
    """
    Crypto pipeline — cron ogni ora a :00, 24/7.

    Pipeline focalizzata SOLO su crypto:
      1. Technical Crypto (DeepSeek-V3) su top-6 crypto liquidità
      2. Decision Crypto (DeepSeek-R1 reasoning) con context crypto-only

    Schedule cron-fisso (non interval): se il watchdog/run extra triggera
    una crypto run alle 14:35, il prossimo cron run è comunque alle 15:00.
    """
    try:
        from agents.orchestrator import run_crypto_pipeline
        from uuid import uuid4
        run_id = str(uuid4())
        result = await run_crypto_pipeline(run_id=run_id)
        if result.get("skipped"):
            logger.debug("[%s] Crypto pipeline skipped: %s",
                         run_id, result["skipped"])
        else:
            logger.info("[%s] Crypto pipeline OK: %s, trades=%d",
                        run_id, result.get("decision", "?"),
                        len(result.get("trades", [])))
    except Exception as e:
        logger.error("Errore crypto pipeline: %s", e, exc_info=True)


async def _standard_pipeline_job():
    """
    Standard pipeline (Technical + Decision Sonnet) — cron a ore precise.

    Triggera SEMPRE alle ore programmate (non interval da avvio scheduler):
    13:00, 15:00, 17:00, 19:00, 21:00 UTC = 9:00, 11:00, 13:00, 15:00, 17:00 ET
    Lunedì-Venerdì, solo se mercati aperti.

    Bypassa il watchdog: gira a tempo fisso. Le esecuzioni extra del watchdog
    su eventi urgenti restano possibili (con throttle 60min) ma NON spostano
    la cadenza dei run programmati.

    Skip se mercato chiuso (festività non US-only) o se l'ultimo Decision
    Sonnet è girato negli ultimi 30 min (evita doppio run subito dopo un
    watchdog-trigger).
    """
    try:
        if not is_market_open():
            logger.debug("Standard pipeline: mercato chiuso, skip")
            return
        # Soft anti-double-run: se Decision Sonnet ha girato nei 30 min, skip
        try:
            last_iso = database.get_setting("last_decision_sonnet_run_at", "") or ""
            if last_iso:
                last_dt = datetime.fromisoformat(last_iso)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=pytz.utc)
                elapsed_min = (datetime.now(pytz.utc) - last_dt).total_seconds() / 60.0
                if elapsed_min < 30:
                    logger.info("Standard pipeline: Sonnet girato %.0f min fa, skip", elapsed_min)
                    return
        except Exception:
            pass

        from agents.orchestrator import run_full_pipeline
        from uuid import uuid4
        run_id = str(uuid4())
        result = await run_full_pipeline(run_id=run_id)
        logger.info("[%s] Standard pipeline (cron) OK: %s, trades=%d",
                    run_id, result.get("decision", "?"),
                    len(result.get("trades", [])))
    except Exception as e:
        logger.error("Errore standard pipeline: %s", e, exc_info=True)


async def _coach_cards_weekly_job():
    """
    Synthesis settimanale Coach Cards — ogni Lunedì alle 06:00 UTC.

    Legge la memoria del Sim Advisor (advice salvati nei run del Simulator),
    chiama DeepSeek-V3 per produrre 3-5 Coach Cards (regole operative
    high-level), le salva su sim_settings. Le card vengono poi mostrate
    nella sidebar Live ed eventualmente iniettate nel system prompt del
    Decision Agent come reminder.

    Costo: ~$0.001 per run (V3 cheap model).
    """
    try:
        from agents import coach_cards
        result = await coach_cards.run_weekly_synthesis()
        logger.info("Coach Cards weekly synthesis: %s", result)
    except Exception as e:
        logger.error("Coach Cards weekly job crash: %s", e, exc_info=True)


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

    # CRITICO: timezone esplicito UTC. Senza, APScheduler usa la TZ del
    # server (es. su Render potrebbe essere UTC, ma in altri host locale).
    # Tutte le CronTrigger sono dichiarate in UTC nei commenti, quindi la
    # scheduler stessa deve operare in UTC per coerenza.
    _scheduler = AsyncIOScheduler(timezone=pytz.utc)

    # ── Price Polling: ogni 10 minuti (yfinance/Massive → Supabase) ──
    # Frequenza ridotta da 60s a 600s per:
    #   1. Ridurre la pressione di rate-limit su yfinance (1800/h → 180/h)
    #   2. Eliminare i "buchi" causati da 429 di yfinance free tier
    #   3. Allinearsi al delay nativo dei provider (~15 min su free tier)
    # next_run_time: primo run dopo 15s dall'avvio scheduler — così dopo
    # un deploy non aspettiamo 10 min per vedere i prezzi aggiornati.
    _scheduler.add_job(
        _price_polling_job,
        trigger="interval",
        seconds=600,
        id="price_polling_job",
        name="Price Polling 10min (massive+yfinance)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(pytz.utc) + timedelta(seconds=15),
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

    # ── Scout: ogni 20 min lun-ven, ogni 60 min su weekend/festivi ──
    # APScheduler fa partire il job ogni 20 min; il job stesso ha un gate
    # interno (vedi _scout_hourly_job) che salta i run extra nei weekend.
    _scheduler.add_job(
        _scout_hourly_job,
        trigger="interval",
        minutes=20,
        id="scout_hourly_job",
        name="Scout (20min weekday, 60min weekend/holiday)",
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

    # ── Cascade aggregation: 8h → 4d → 3w (sostituisce daily/weekly) ──
    # Ogni livello consuma il precedente e lo elimina dopo aver scritto il
    # report aggregato. Storage: tutti su intelligence_buffer con tier =
    # AGG_8H / AGG_4D / AGG_3W (vedi backend/agents/scout.py).
    now_utc = datetime.now(pytz.utc)

    # 8H — ogni 8 ore. Primo run dopo 10 min dal restart.
    # Il job stesso fa un check anti-double-run leggendo l'ultimo AGG_8H
    # dal buffer (skip se < 6h fa, evita spreco token su deploy frequenti).
    _scheduler.add_job(
        _scout_8h_report_job,
        trigger="interval",
        hours=8,
        id="scout_8h_report",
        name="Scout 8H Report (consume L0 micro-cards)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        next_run_time=now_utc + timedelta(minutes=10),
    )

    # 4D — ogni 4 giorni. Primo run dopo 15 min dal restart.
    # PRIMA: next_run = now + 24h → ogni deploy resettava il timer e il
    # job non girava mai. Dopo 5 giorni di running con deploy frequenti
    # avevamo 17 AGG_8H ma 0 AGG_4D. Fix: schedula presto, e il job stesso
    # fa skip se l'ultimo AGG_4D è < 3 giorni fa.
    _scheduler.add_job(
        _scout_4d_report_job,
        trigger="interval",
        days=4,
        id="scout_4d_report",
        name="Scout 4D Report (consume L1 8H-reports)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        next_run_time=now_utc + timedelta(minutes=15),
    )


    # ── Crypto pipeline: CRON ogni ora a :00, 24/7 ──
    # Schedule cron-fisso (non interval). Se watchdog triggera crypto a 14:35,
    # il prossimo cron resta alle 15:00 — la cadenza non viene sballata.
    _scheduler.add_job(
        _crypto_pipeline_job,
        trigger=CronTrigger(minute=0),   # ogni ora a :00
        id="crypto_pipeline_job",
        name="Crypto pipeline (cron :00, 24/7, V3+R1)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # ── Standard pipeline (Tech + Decision Sonnet): CRON solo NYSE hours ──
    # 14, 16, 18, 20 UTC (= 10:00, 12:00, 14:00, 16:00 ET) Lun-Ven.
    # NYSE apre 13:30 UTC, chiude 20:00 UTC → primo run a 14:00, ultimo a 20:00.
    # Skip auto se mercato chiuso (festività). Watchdog può comunque triggerare
    # extra runs durante NYSE hours.
    _scheduler.add_job(
        _standard_pipeline_job,
        trigger=CronTrigger(hour="14,16,18,20", minute=0,
                            day_of_week="mon-fri"),
        id="standard_pipeline_job",
        name="Standard pipeline (cron 14/16/18/20 UTC L-V, V3+Sonnet, NYSE-only)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # ── Coach Cards weekly synthesis: ogni Lunedi' alle 06:00 UTC ──
    # Legge la memoria del Sim Advisor e produce 3-5 Coach Cards per il
    # Decision Live. Cheap (~$0.001 per run via DeepSeek-V3).
    _scheduler.add_job(
        _coach_cards_weekly_job,
        trigger=CronTrigger(day_of_week="mon", hour=6, minute=0),
        id="coach_cards_weekly",
        name="Coach Cards weekly synthesis (Mon 06:00 UTC)",
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

    # --- Per i job APScheduler leggiamo direttamente next_run_time ---
    watchdog_next = None
    scout_next = None
    scout_8h_next = None
    scout_4d_next = None
    crypto_pipeline_next = None
    standard_pipeline_next = None
    if _scheduler and running:
        wj = _scheduler.get_job("watchdog_job")
        if wj and wj.next_run_time:
            watchdog_next = wj.next_run_time.isoformat()
        sj = _scheduler.get_job("scout_hourly_job")
        if sj and sj.next_run_time:
            scout_next = sj.next_run_time.isoformat()
        s8 = _scheduler.get_job("scout_8h_report")
        if s8 and s8.next_run_time:
            scout_8h_next = s8.next_run_time.isoformat()
        s4 = _scheduler.get_job("scout_4d_report")
        if s4 and s4.next_run_time:
            scout_4d_next = s4.next_run_time.isoformat()
        cj = _scheduler.get_job("crypto_pipeline_job")
        if cj and cj.next_run_time:
            crypto_pipeline_next = cj.next_run_time.isoformat()
        spj = _scheduler.get_job("standard_pipeline_job")
        if spj and spj.next_run_time:
            standard_pipeline_next = spj.next_run_time.isoformat()

    # --- Per i job on-demand cerchiamo l'ultimo log corrispondente ---
    watchdog_last = _last_log_for_phases(["WATCHDOG_"])
    scout_last = _last_log_for_phases(["SCOUT_20MIN", "SCOUT", "SCOUT_8H", "SCOUT_4D"])
    scout_8h_last = _last_log_for_phases(["SCOUT_8H"])
    scout_4d_last = _last_log_for_phases(["SCOUT_4D"])
    technical_last = _last_log_for_phases(["TECH_", "TECHNICAL_"])

    # --- Decision split: Sonnet (orari mercato) vs R1 (overnight crypto) ---
    # last_run = ultimo run COMPLETATO con successo (DECISION_COMPLETE)
    # last_failure = ultimo ERRORE — per warning UI
    # last_started = ultimo CONTEXT (run in corso) — per status "in corso"
    #
    # Bug precedente: il warning FAIL si attivava quando last_attempt
    # (qualunque tentativo, anche un CONTEXT in corso normalmente)
    # era piu' recente di last_run. Risultato: durante ogni run di 2 min,
    # la sidebar mostrava FAIL anche se non c'era nessun fail.
    # Fix: warning solo su ERROR, non su CONTEXT (in-progress).
    try:
        decision_sonnet_last = (database.get_setting("last_decision_sonnet_run_at", "")
                                or _last_log_for_phases(["DECISION_COMPLETE"]))
        decision_r1_last = database.get_setting("last_decision_r1_run_at", "") or None
        decision_last_failure = _last_log_for_phases(["DECISION_ERROR"])
        decision_last_started = _last_log_for_phases(["DECISION_CONTEXT"])
    except Exception:
        decision_sonnet_last = _last_log_for_phases(["DECISION_COMPLETE"])
        decision_r1_last = None
        decision_last_failure = None
        decision_last_started = None

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
        "scout_8h": {
            "schedule": "ogni 8 ore (aggrega micro-cards)",
            "next_run": scout_8h_next,
            "last_run": scout_8h_last,
            "active": running,
        },
        "scout_4d": {
            "schedule": "ogni 4 giorni (aggrega report 8H — top tier macro)",
            "next_run": scout_4d_next,
            "last_run": scout_4d_last,
            "active": running,
        },
        "technical": {
            "schedule": "cron L-V 13/15/17/19/21 UTC + on-demand watchdog (DeepSeek-V3)",
            "next_run": standard_pipeline_next,
            "last_run": technical_last,
            "active": running and market_open,
        },
        "decision": {
            "schedule": "cron L-V 13/15/17/19/21 UTC + on-demand watchdog (Sonnet 4.5)",
            "next_run": standard_pipeline_next,
            "last_run": decision_sonnet_last,
            # last_failure = ultimo ERROR; mostra "FAIL" solo se ERROR
            # è successivo all'ultimo run completato con successo.
            "last_failure": decision_last_failure,
            # last_started = ultimo CONTEXT, per status "in corso" senza FAIL
            "last_started": decision_last_started,
            "active": running and market_open,
            "model": "claude-sonnet-4-5",
        },
        "technical_crypto": {
            "schedule": "ogni 1h, 24/7 (DeepSeek-V3, solo crypto)",
            "next_run": crypto_pipeline_next,
            "last_run": _last_log_for_phases(["TECH_CRYPTO"]),
            "active": running,
            "model": "deepseek-v3",
        },
        "decision_crypto": {
            "schedule": "ogni 1h, 24/7 (DeepSeek-R1 reasoning, solo crypto)",
            "next_run": crypto_pipeline_next,
            "last_run": _last_log_for_phases(["DECISION_CRYPTO_COMPLETE"]),
            "last_failure": _last_log_for_phases(["DECISION_CRYPTO_ERROR"]),
            "last_started": _last_log_for_phases(["DECISION_CRYPTO_CONTEXT"]),
            "active": running,
            "model": "deepseek-r1",
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
