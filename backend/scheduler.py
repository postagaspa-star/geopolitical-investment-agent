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

# ── Festività NYSE/NASDAQ (USA) — 2025-2027 ───────────────────────────────
# In questi giorni la borsa USA è CHIUSA anche se è un giorno feriale.
# Aggiornare ogni anno (NYSE pubblica il calendario ufficiale ~12 mesi prima).
# Senza questo controllo, is_market_open() restituiva True nei festivi USA,
# il bot apriva trade su S&P 500 con ClawStreet che li rifiutava → divergenza.
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
}

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


async def _decision_24h_scheduled_job():
    """
    Decision 24h scheduled — ogni 2h30, market-closed only.

    Bypassa il Watchdog: gira a tempo fisso, non a evento. Esegue Technical
    (DeepSeek-V3) + Decision (DeepSeek-R1) con focus default crypto top-3.

    Skip automatico se:
      - Mercati equity aperti (Sonnet attivo, no necessità di R1)
      - Cooldown R1 ancora non scaduto

    Costo: ~$0.007/run × ~10 run/giorno overnight = ~$0.07/giorno.
    """
    try:
        from agents.orchestrator import run_scheduled_24h_pipeline
        from uuid import uuid4
        run_id = str(uuid4())
        result = await run_scheduled_24h_pipeline(run_id=run_id)
        if result.get("skipped"):
            logger.debug("[%s] Decision 24h scheduled: skipped (%s)",
                         run_id, result["skipped"])
        else:
            logger.info("[%s] Decision 24h scheduled completed: %s, trades=%d",
                        run_id, result.get("decision", "?"),
                        len(result.get("trades", [])))
    except Exception as e:
        logger.error("Errore Decision 24h scheduled: %s", e, exc_info=True)


async def _clawstreet_mirror_retry_job():
    """
    Retry trade falliti — ogni 15 min, 24/7.

    Cerca tutti i trade con cs_mirror_status in (pending, failed) nelle ultime
    24h e li riprova. Marca come 'ok'/'failed'/'skipped' a seconda dell'esito.
    Limita a 5 tentativi per trade per evitare loop infiniti su trade
    permanentemente non specchiabili (simbolo non supportato, ecc.).

    Funziona 24/7 (non solo market hours): le crypto possono essere mirrored
    a qualunque ora; i trade equity falliti restano in coda fino al prossimo
    market open.
    """
    try:
        cs_bot_id = database.get_setting("clawstreet_bot_id", "") or os.environ.get("CLAWSTREET_BOT_ID", "")
        cs_api_key = database.get_setting("clawstreet_api_key", "") or os.environ.get("CLAWSTREET_API_KEY", "")
        if not cs_bot_id or not cs_api_key or cs_bot_id == "GEO":
            return  # credenziali non configurate

        from clawstreet_mirror import retry_pending_mirrors
        result = await retry_pending_mirrors(window_hours=24, limit=20)
        if result.get("retried", 0) > 0:
            logger.info("ClawStreet mirror retry: %s", result)
    except Exception as e:
        logger.warning("Errore retry mirror ClawStreet: %s", e)


async def _clawstreet_reconcile_job():
    """
    Riconciliazione completa ClawStreet — ogni 6 ore, 24/7.

    Variante più "pesante" del retry job: confronta TUTTI i trade locali
    delle ultime 48h con quelli effettivamente presenti su ClawStreet via
    GET /bots/{id}/trades. Recupera trade locali che, per qualche motivo,
    non hanno la riga cs_mirror_status correttamente popolata (es. legacy
    trade pre-migration, o se la tabella ha avuto problemi).
    """
    try:
        cs_bot_id = database.get_setting("clawstreet_bot_id", "") or os.environ.get("CLAWSTREET_BOT_ID", "")
        cs_api_key = database.get_setting("clawstreet_api_key", "") or os.environ.get("CLAWSTREET_API_KEY", "")
        if not cs_bot_id or not cs_api_key or cs_bot_id == "GEO":
            return

        # Self-call all'endpoint /api/clawstreet/reconcile (già implementato)
        import aiohttp
        url = f"https://geopolitical-investment-agent.onrender.com/api/clawstreet/reconcile?since_hours=48"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    sent = data.get("sent", 0)
                    failed = data.get("failed", 0)
                    if sent > 0 or failed > 0:
                        logger.info("ClawStreet 6h reconcile: sent=%d failed=%d", sent, failed)
    except Exception as e:
        logger.warning("Errore reconcile ClawStreet 6h: %s", e)




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

    # ── Cascade aggregation: 8h → 4d → 3w (sostituisce daily/weekly) ──
    # Ogni livello consuma il precedente e lo elimina dopo aver scritto il
    # report aggregato. Storage: tutti su intelligence_buffer con tier =
    # AGG_8H / AGG_4D / AGG_3W (vedi backend/agents/scout.py).
    now_utc = datetime.now(pytz.utc)

    # 8H — ogni 8 ore. Primo run dopo 1h (per accumulare almeno 3 cicli Scout).
    _scheduler.add_job(
        _scout_8h_report_job,
        trigger="interval",
        hours=8,
        id="scout_8h_report",
        name="Scout 8H Report (consume L0 micro-cards)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        next_run_time=now_utc + timedelta(hours=1),
    )

    # 4D — ogni 4 giorni. Primo run dopo 24h (per avere almeno 3 report 8H).
    _scheduler.add_job(
        _scout_4d_report_job,
        trigger="interval",
        days=4,
        id="scout_4d_report",
        name="Scout 4D Report (consume L1 8H-reports)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        next_run_time=now_utc + timedelta(hours=24),
    )


    # ── Decision 24h scheduled: ogni 2h30, market-closed only ──
    # Garantisce che il Decision R1 giri a tempo fisso quando i mercati sono
    # chiusi, anche se il Watchdog non triggera (evento più frequente nelle
    # festività e nei weekend, dove il Watchdog ha meno news rilevanti).
    # Skip automatico se mercato aperto (Sonnet attivo) o cooldown attivo.
    _scheduler.add_job(
        _decision_24h_scheduled_job,
        trigger="interval",
        minutes=150,   # 2h30
        id="decision_24h_scheduled",
        name="Decision 24h scheduled (2h30, market-closed only, R1)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(pytz.utc) + timedelta(minutes=5),
    )

    # ── ClawStreet mirror retry: ogni 15 min, 24/7 ──
    # Riprova i trade con cs_mirror_status='failed' o 'pending'. Velocissimo
    # (~1s) se non ci sono pending. Garantisce che le mirror failures
    # transitorie (network, 500, timeout) vengano sistemate entro 15 min.
    _scheduler.add_job(
        _clawstreet_mirror_retry_job,
        trigger="interval",
        minutes=15,
        id="clawstreet_mirror_retry",
        name="ClawStreet mirror retry (15 min, 24/7)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(pytz.utc) + timedelta(minutes=2),
    )

    # ── ClawStreet reconcile: ogni 6 ore (deep check via GET /trades) ──
    # Questo è il "safety net" — confronta lo storico effettivo CS vs locale.
    _scheduler.add_job(
        _clawstreet_reconcile_job,
        trigger="interval",
        hours=6,
        id="clawstreet_reconcile",
        name="ClawStreet deep reconcile (6h, fetch CS trades)",
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
    decision_24h_next = None
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
        d24 = _scheduler.get_job("decision_24h_scheduled")
        if d24 and d24.next_run_time:
            decision_24h_next = d24.next_run_time.isoformat()

    # --- Per i job on-demand cerchiamo l'ultimo log corrispondente ---
    watchdog_last = _last_log_for_phases(["WATCHDOG_"])
    scout_last = _last_log_for_phases(["SCOUT_20MIN", "SCOUT", "SCOUT_8H", "SCOUT_4D"])
    scout_8h_last = _last_log_for_phases(["SCOUT_8H"])
    scout_4d_last = _last_log_for_phases(["SCOUT_4D"])
    technical_last = _last_log_for_phases(["TECH_", "TECHNICAL_"])

    # --- Decision split: Sonnet (orari mercato) vs R1 (overnight crypto) ---
    # Letto dai setting key aggiornati da decision.py dopo ogni run completato.
    # Sicurezza: se i setting non esistono ancora (DB pulito), fall back ai
    # log generici.
    try:
        decision_sonnet_last = (database.get_setting("last_decision_sonnet_run_at", "")
                                or _last_log_for_phases(["DECISION_"]))
        decision_r1_last = database.get_setting("last_decision_r1_run_at", "") or None
    except Exception:
        decision_sonnet_last = _last_log_for_phases(["DECISION_"])
        decision_r1_last = None

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
            "schedule": "ogni 2h30 + on-demand watchdog (DeepSeek-V3, sempre attivo)",
            # next_run = il prossimo trigger scheduled certo (ogni 2h30 con Decision 24h);
            # il Watchdog può comunque triggerarlo prima durante orari di mercato.
            "next_run": decision_24h_next,
            "last_run": technical_last,
            "active": running,
        },
        "decision": {
            "schedule": "on-demand orari mercato (Sonnet 4.5, max 1/ora)",
            "next_run": None,
            "last_run": decision_sonnet_last,
            "active": running and market_open,
            "model": "claude-sonnet-4-5",
        },
        "decision_24h": {
            "schedule": "ogni 2h30 quando mercati chiusi (DeepSeek-R1, scheduled)",
            "next_run": decision_24h_next,
            "last_run": decision_r1_last,
            "active": running and not market_open,
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
