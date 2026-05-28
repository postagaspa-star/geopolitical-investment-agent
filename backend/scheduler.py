"""
Scheduler autonomo h24 per l'agente geopolitico di investimento.
Esegue l'agente ogni 20 minuti con comportamento diverso in base
all'orario e al giorno (weekend / pre-market / mercato aperto).
"""

import asyncio
import json
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

# ────────────────────────────────────────────────────────────────────────────
# Serializzazione "live pipeline" — versione semplice via boolean flag.
#
# Crypto pipeline (CronTrigger minute=0, 24/7) e Standard pipeline
# (CronTrigger hour=14,16,18,20 minute=2) potrebbero sovrapporsi se la
# crypto sfora i 2 min. Entrambe girano nello stesso event loop e fanno
# decine di chiamate sync a Supabase + Anthropic SDK wrapped in to_thread.
# Senza serializzazione il Decision Standard si stalla per contention.
#
# PRIMA VERSIONE (rimossa): asyncio.Lock con wait_for(lock.acquire(), timeout).
# Problema noto: il pattern wait_for + Lock.acquire() ha race conditions su
# cancel (Python issue #45098) che possono lasciare il Lock "stuck", e in
# caso di crash di una pipeline il Lock non viene rilasciato — la pipeline
# successiva aspetta 8 min e skippa, all'infinito. Sintomo osservato:
# "i due agenti cripto non partono piu'".
#
# NUOVA VERSIONE: due boolean flag in-memory. Crypto pipeline imposta il
# proprio flag a True quando inizia e a False quando finisce (sempre,
# anche su exception, via finally). Standard pipeline polla il flag crypto
# all'inizio: se True, aspetta polling-style (sleep 5s) fino a max 8 min,
# poi se ancora True skippa quel run.
# Vantaggi vs Lock:
#   - Nessun rischio di "stuck lock" persistente: il flag e' read/write
#     boolean, non ha stato di acquisition pending.
#   - In caso di crash hard del processo, i flag si resettano automaticamente
#     al riavvio (sono in-memory).
#   - Codice piu' leggibile, meno gotcha asyncio.
# Tradeoff: il polling consuma cycle dell'event loop ogni 5s, ma e' trascurabile.
_crypto_pipeline_running: bool = False
_standard_pipeline_running: bool = False
_LIVE_PIPELINE_WAIT_MAX_SEC = 8 * 60   # max attesa standard per crypto: 8 min


async def _wait_for_crypto_pipeline_done(max_wait_sec: int = _LIVE_PIPELINE_WAIT_MAX_SEC) -> bool:
    """
    Polling helper: aspetta che _crypto_pipeline_running torni False.
    Ritorna True se completato in tempo, False se timeout.
    """
    if not _crypto_pipeline_running:
        return True
    waited = 0
    while _crypto_pipeline_running and waited < max_wait_sec:
        await asyncio.sleep(5)
        waited += 5
    return not _crypto_pipeline_running


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


def is_us_market_open() -> bool:
    """
    True SOLO se NYSE/NASDAQ e' ORA in orario di contrattazione
    (9:30-16:00 ET, giorno feriale, no festivita' NYSE).

    DISTINTA da is_market_open(), che ritorna True se UNA QUALSIASI tra
    NYSE/LSE/XETRA e' aperta. L'universo investibile del bot e'
    INTERAMENTE USA (azioni ed ETF quotati NYSE/NASDAQ). Per ESEGUIRE
    un trade equity bisogna gateare su QUESTA funzione: gateare su
    is_market_open() faceva sì che durante la mattina europea (LSE/XETRA
    aperte, Wall Street ancora chiusa) il bot aprisse/chiudesse posizioni
    USA col mercato USA CHIUSO — a prezzi non eseguibili.
    """
    now_utc = datetime.now(pytz.utc)
    if now_utc.weekday() >= 5:
        return False
    now_et = now_utc.astimezone(pytz.timezone('America/New_York'))
    if now_et.strftime("%Y-%m-%d") in NYSE_HOLIDAYS:
        return False
    nyse_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    nyse_close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    return nyse_open <= now_et <= nyse_close


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


def is_standard_agent_active() -> bool:
    """
    True se l'agente Decision Standard puo' essere triggerato per gestire
    asset equity (NYSE/LSE/XETRA).

    A differenza di is_market_open() (che richiede l'orario di apertura
    di almeno una borsa), questa controlla solo:
      - Non e' weekend (sabato/domenica)
      - Non e' festivita' di NESSUNA delle 3 borse target

    Razionale: durante un giorno feriale non festivo, anche pre-market o
    after-hours, l'agente Standard e' "attivo" — puo' essere triggerato
    da un rebalance e l'orchestrator decidera' se eseguire subito o
    attendere l'apertura. Durante weekend e festivita', invece, NON
    deve partire (non avrebbe modo di eseguire trade equity).
    """
    now_utc = datetime.now(pytz.utc)
    if now_utc.weekday() >= 5:
        return False
    today_et = now_utc.astimezone(pytz.timezone('America/New_York')).strftime("%Y-%m-%d")
    today_uk = now_utc.astimezone(pytz.timezone('Europe/London')).strftime("%Y-%m-%d")
    today_de = now_utc.astimezone(pytz.timezone('Europe/Berlin')).strftime("%Y-%m-%d")
    # Festivita' su tutte e 3 le borse target → standard agent NON attivo
    if (today_et in NYSE_HOLIDAYS
            and today_uk in LSE_HOLIDAYS
            and today_de in XETRA_HOLIDAYS):
        return False
    # Festivita' US specifica + non altre: per asset US-listed (la maggior
    # parte) l'agente non puo' eseguire. Conservatively: skip.
    if today_et in NYSE_HOLIDAYS:
        return False
    return True


def is_crypto_agent_active() -> bool:
    """
    True sempre. Il Decision Crypto opera 24/7/365 — i mercati crypto
    non hanno chiusure di weekend ne' festivita'.
    """
    return True


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
    finally:
        # La pipeline (technical multi-ticker + decision) e' il picco di RAM:
        # restituiamo subito la memoria liberata all'OS per non avvicinarci
        # al tetto dei 512MB tra un run e l'altro (fix OOM).
        try:
            import memory_utils
            _loop = asyncio.get_running_loop()
            await _loop.run_in_executor(None, memory_utils.trim_memory, "post_pipeline")
        except Exception:
            pass


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


async def _memory_trim_job():
    """
    Job riduzione memoria — ogni 30 minuti (backstop al trim post-pipeline).
    gc.collect() + malloc_trim(0): restituisce all'OS la RAM liberata che
    glibc altrimenti tratterrebbe → tiene l'RSS lontano dal tetto 512MB.
    """
    try:
        import memory_utils
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, memory_utils.trim_memory, "scheduled_30min")
    except Exception as e:
        logger.debug("Memory trim job error: %s", e)


async def _health_check_job():
    """
    Job Cruscotto di salute — ogni 6 ore.
    Esegue i controlli read-only (invariante cassa, sync grafico, allarmi
    recenti, provenienza cassa) e scrive un HEALTH_DIGEST su agent_logs.
    Trasforma i bug silenziosi in allarmi visibili.
    """
    try:
        import asyncio as _asyncio
        import health_check
        # I check sono sincroni (query DB) → off-load nel threadpool per non
        # bloccare l'event loop.
        loop = _asyncio.get_running_loop()
        report = await loop.run_in_executor(None, health_check.run_health_checks)
        if report and report.get("status") != "OK":
            logger.warning("[HEALTH] stato=%s — %s",
                           report.get("status"), report.get("summary"))
    except Exception as e:
        logger.error("Errore Health Check job: %s", e, exc_info=False)


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

    Serializzazione: imposta _crypto_pipeline_running=True all'inizio e
    False alla fine (sempre, via finally). La standard pipeline al :02 polla
    questo flag e aspetta se True.

    NON aspetta nessun altro flag: la crypto pipeline parte sempre,
    indipendentemente da cosa sta facendo la standard. Questo perche':
      1. La crypto cron e' a :00 e standard a :02 (crypto parte sempre prima).
      2. La crypto e' 24/7, standard solo NYSE hours.
      3. Se la standard ha qualche problema, non vogliamo bloccare la crypto.
    """
    global _crypto_pipeline_running
    tick_ts = datetime.now(pytz.utc).strftime("%H:%M:%S UTC")
    logger.info("Crypto pipeline job: TICK %s", tick_ts)
    _crypto_pipeline_running = True
    try:
        from agents.orchestrator import run_crypto_pipeline
        from uuid import uuid4
        run_id = str(uuid4())
        # Hard timeout 12 min: oltre questa soglia il run e' patologico
        # (es. hang di Anthropic/DeepSeek SDK su rate-limit retries).
        try:
            result = await asyncio.wait_for(
                run_crypto_pipeline(run_id=run_id),
                timeout=12 * 60,
            )
        except asyncio.TimeoutError:
            logger.error("[%s] Crypto pipeline TIMEOUT (>12min), abort", run_id)
            try:
                database.insert_agent_log(run_id, "ORCHESTRATOR", json.dumps({
                    "event": "crypto_pipeline_timeout", "limit_min": 12,
                }))
            except Exception:
                pass
            return
        if result.get("skipped"):
            logger.info("[%s] Crypto pipeline skipped: %s",
                        run_id, result["skipped"])
        else:
            logger.info("[%s] Crypto pipeline OK: %s, trades=%d",
                        run_id, result.get("decision", "?"),
                        len(result.get("trades", [])))
    except Exception as e:
        logger.error("Errore crypto pipeline: %s", e, exc_info=True)
    finally:
        _crypto_pipeline_running = False
        logger.debug("Crypto pipeline job: flag cleared")


async def _risk_safety_job():
    """
    Risk Safety job — ogni 5 minuti, 24/7.

    Applica le 5 regole stateful di risk management che il Decision Agent
    da solo non puo' enforce-are (richiedono persistenza + dati storici):

      1. 24h drawdown circuit breaker: se DD > 5% in 24h → liquida tutto
         + entra in recovery mode (SL ≤ 5%, confidence ≥ 0.75).
      2. Recovery mode exit: se portafoglio torna >= initial → esce auto.
      3. Lock-in 0.5%: su posizioni a +5% di profitto, imposta SL a
         entry + 0.5% per proteggere il profitto minimo.
      4. Trailing stop dinamico 3-4%: su posizioni vincenti, aggiorna lo
         SL al max(SL corrente, peak_price * 0.96) — sale, mai scende.
      5. Concentration check: log se una posizione supera il 35% del NAV
         (l'azione di rebalance la prende il Decision Agent al prossimo run).

    Tutte le operazioni sono idempotenti e safe-by-default: niente effetti
    se il portafoglio e' vuoto, prezzi non disponibili, ecc.
    """
    tick_ts = datetime.now(pytz.utc).strftime("%H:%M:%S UTC")
    logger.debug("Risk safety job: TICK %s", tick_ts)
    try:
        import risk_state

        # 1. Circuit breaker check — NO PIU' auto-liquidazione.
        # Default DISATTIVO. Anche se ATTIVATO, alza solo un ALERT
        # (agent_log + log warning). La liquidazione effettiva richiede
        # chiamata esplicita a POST /api/risk-state/manual-liquidate-all.
        #
        # Rationale: una corruzione di snapshot puo' inflare il running_max
        # del drawdown 24h e triggerare un falso positivo che liquida
        # tutto il portafoglio. Mai piu'.
        if risk_state.is_circuit_breaker_enabled():
            try:
                triggered, dd_pct = risk_state.is_circuit_breaker_triggered()
                if triggered and not risk_state.is_in_recovery_mode():
                    logger.warning(
                        "Risk safety: CIRCUIT BREAKER ALERT (DD 24h = %.2f%%) "
                        "→ logging only, NO auto-liquidation. "
                        "User must explicitly call /api/risk-state/manual-liquidate-all.",
                        dd_pct,
                    )
                    try:
                        database.insert_agent_log(
                            "risk_safety", "RISK_CIRCUIT_BREAKER_ALERT",
                            json.dumps({
                                "event": "circuit_breaker_alert",
                                "drawdown_24h_pct": dd_pct,
                                "threshold": risk_state.DEFAULT_CIRCUIT_BREAKER_24H_PCT,
                                "action_taken": "ALERT_ONLY",
                                "note": ("user-triggered liquidation required via "
                                         "POST /api/risk-state/manual-liquidate-all"),
                            }, default=str),
                        )
                    except Exception:
                        pass
            except Exception as e:
                logger.error("Risk safety circuit breaker check failed: %s", e,
                             exc_info=True)
        else:
            logger.debug("Risk safety: circuit breaker DISABLED via setting")

        # 2. Recovery mode exit (sempre attivo se in recovery — non blocca
        #    l'uscita anche se il circuit breaker e' stato disabilitato dopo).
        try:
            exited = risk_state.check_recovery_exit_condition()
            if exited:
                logger.info("Risk safety: EXITED recovery mode (portfolio recovered)")
        except Exception as e:
            logger.error("Risk safety recovery exit check failed: %s", e)

        # 3-4. Lock-in 0.5% + trailing stop dinamico — OPT-IN (default OFF).
        # IMPORTANTE: anche se attivati, NON sovrascrivono mai SL settati
        # da Decision Agent o utente (guard in risk_state.should_apply_lock_in
        # e apply_trailing_stop_if_needed). Solo SL == 0 o SL settato da
        # risk_state stesso possono essere aggiornati.
        lock_in_on = risk_state.is_lock_in_enabled()
        trailing_on = risk_state.is_trailing_enabled()

        if lock_in_on or trailing_on:
            try:
                import database
                positions = database.get_positions() or []
                lock_in_applied = 0
                trailing_applied = 0
                for p in positions:
                    try:
                        avg = float(p.get("avg_buy_price") or 0)
                        cur = float(p.get("current_price") or 0)
                        if avg <= 0 or cur <= 0:
                            continue
                        # DIRECTION-AWARE: SHORT skipped (lock-in/trailing non
                        # supportano SHORT, vedi risk_state.py).
                        if (p.get("direction") or "LONG").upper() == "SHORT":
                            continue
                        pnl_pct = (cur - avg) / avg * 100.0
                        if pnl_pct < 5.0:
                            continue

                        # 3. Lock-in 0.5% (priority: applicato al primo
                        #    passaggio sopra +5%)
                        if lock_in_on:
                            lock_res = risk_state.apply_lock_in_protection(p, current_price=cur)
                            if lock_res.get("applied"):
                                lock_in_applied += 1
                                logger.info(
                                    "Risk safety lock-in: %s → SL=%.4f (PnL=%.1f%%)",
                                    p.get("ticker"), lock_res["new_stop_loss"], pnl_pct,
                                )
                                try:
                                    p = database.get_position(p.get("ticker")) or p
                                except Exception:
                                    pass

                        # 4. Trailing stop dinamico (solo se PnL >= 10%)
                        if trailing_on and pnl_pct >= 10.0:
                            trail_res = risk_state.apply_trailing_stop_if_needed(
                                p, trailing_pct=4.0, min_profit_pct=10.0,
                            )
                            if trail_res.get("applied"):
                                trailing_applied += 1
                                logger.info(
                                    "Risk safety trailing: %s → SL=%.4f "
                                    "(peak=%.4f, prev SL=%.4f)",
                                    p.get("ticker"), trail_res["new_stop_loss"],
                                    trail_res["peak_price"], trail_res["previous_sl"],
                                )
                    except Exception as e:
                        logger.debug("Risk safety per-position fail %s: %s",
                                     p.get("ticker"), e)
                        continue

                if lock_in_applied or trailing_applied:
                    logger.info(
                        "Risk safety: lock-in=%d, trailing=%d posizioni aggiornate",
                        lock_in_applied, trailing_applied,
                    )
            except Exception as e:
                logger.error("Risk safety lock-in/trailing failed: %s", e, exc_info=True)
        else:
            logger.debug(
                "Risk safety: lock-in/trailing DISABLED via setting "
                "(no auto-SL updates)"
            )

        # 5. Concentration check (solo log + agent_log per visibilita')
        try:
            conc = risk_state.compute_position_concentration()
            if conc["max_concentration_pct"] > risk_state.DEFAULT_CONCENTRATION_TRIGGER_PCT:
                logger.warning(
                    "Risk safety: CONCENTRATION TRIGGER — %s al %.1f%% NAV "
                    "(soglia %.0f%%)",
                    conc["max_ticker"], conc["max_concentration_pct"],
                    risk_state.DEFAULT_CONCENTRATION_TRIGGER_PCT,
                )
                try:
                    database.insert_agent_log(
                        "risk_safety", "RISK_CONCENTRATION_ALERT",
                        json.dumps({
                            "event": "concentration_trigger",
                            "ticker": conc["max_ticker"],
                            "pct_nav": conc["max_concentration_pct"],
                            "threshold": risk_state.DEFAULT_CONCENTRATION_TRIGGER_PCT,
                            "total_nav": conc["total_nav"],
                        }, default=str),
                    )
                except Exception:
                    pass
        except Exception as e:
            logger.debug("Risk safety concentration check failed: %s", e)

    except Exception as e:
        logger.error("Risk safety job error: %s", e, exc_info=True)


async def _crypto_monitor_job():
    """
    CryptoMonitor — ogni 15 minuti, 24/7.

    Sorveglianza intelligente delle posizioni crypto aperte: legge le
    ultime ~25 candele 15-min per ogni asset e chiede a DeepSeek-V3 di
    valutare se il trend di entrata e' ancora sano oppure mostra segnali
    di esaurimento / inversione. Su REVERSAL_CONFIRMED triggera alert +
    (opzionale) restringe lo stop-loss.

    Costo: ~$0.0001/run × 96 run/giorno = ~$0.01/giorno.
    No-op se non ci sono posizioni crypto aperte.
    """
    tick_ts = datetime.now(pytz.utc).strftime("%H:%M:%S UTC")
    logger.debug("CryptoMonitor job: TICK %s", tick_ts)
    try:
        from agents.crypto_monitor import run_crypto_monitor
        from uuid import uuid4
        run_id = str(uuid4())[:8]
        result = await run_crypto_monitor(run_id=run_id)
        if result.get("skipped"):
            logger.debug("[%s] CryptoMonitor skipped: %s",
                         run_id, result.get("reason", "?"))
        else:
            n_alerts = len(result.get("alerts", []))
            n_tightened = len(result.get("tightened_sl", []))
            if n_alerts or n_tightened:
                logger.info(
                    "[%s] CryptoMonitor: %d analizzate, %d alert, %d SL tightened",
                    run_id, result.get("positions_analyzed", 0),
                    n_alerts, n_tightened,
                )
            else:
                logger.debug(
                    "[%s] CryptoMonitor: %d analizzate, tutte HEALTHY",
                    run_id, result.get("positions_analyzed", 0),
                )
    except Exception as e:
        logger.error("Errore CryptoMonitor: %s", e, exc_info=True)


async def _standard_pipeline_job():
    """
    Standard pipeline (Technical + Decision Sonnet) — cron a ore precise.

    Triggera SEMPRE alle ore programmate (non interval da avvio scheduler):
    14:02, 16:02, 18:02, 20:02 UTC Lun-Ven, solo se mercati aperti.

    Il :02 invece di :00 e' deliberato: la crypto pipeline gira a :00 e
    questo job aspetta che la crypto finisca tramite poll del flag
    `_crypto_pipeline_running`. Tipicamente al :02 la crypto e' gia'
    completata (~2-3 min totali); se per qualche motivo e' ancora in
    corso (es. R1 reasoning lungo), aspetta polling-style max 8 min.

    Bypassa il watchdog: gira a tempo fisso. Le esecuzioni extra del watchdog
    su eventi urgenti restano possibili (con throttle 60min) ma NON spostano
    la cadenza dei run programmati.

    Skip se mercato chiuso (festività non US-only) o se l'ultimo Decision
    Sonnet è girato negli ultimi 30 min (evita doppio run subito dopo un
    watchdog-trigger).
    """
    global _standard_pipeline_running
    tick_ts = datetime.now(pytz.utc).strftime("%H:%M:%S UTC")
    logger.info("Standard pipeline job: TICK %s", tick_ts)
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

        # Aspetta che la crypto pipeline finisca (polling, max 8 min).
        if _crypto_pipeline_running:
            logger.info("Standard pipeline: crypto in corso, attendo polling-style max %dmin",
                        _LIVE_PIPELINE_WAIT_MAX_SEC // 60)
            done = await _wait_for_crypto_pipeline_done()
            if not done:
                logger.warning("Standard pipeline: crypto ancora in corso dopo %dmin, "
                               "skip questo run. Prossimo cron tra 2h.",
                               _LIVE_PIPELINE_WAIT_MAX_SEC // 60)
                return
            logger.info("Standard pipeline: crypto finita, procedo")

        _standard_pipeline_running = True
        try:
            from agents.orchestrator import run_full_pipeline
            from uuid import uuid4
            run_id = str(uuid4())
            # Hard timeout 12 min: oltre questa soglia il run e' patologico
            # (probabile hang di Anthropic SDK su rate-limit retries).
            try:
                result = await asyncio.wait_for(
                    run_full_pipeline(run_id=run_id),
                    timeout=12 * 60,
                )
            except asyncio.TimeoutError:
                logger.error("[%s] Standard pipeline TIMEOUT (>12min), abort", run_id)
                try:
                    database.insert_agent_log(run_id, "ORCHESTRATOR", json.dumps({
                        "event": "standard_pipeline_timeout", "limit_min": 12,
                    }))
                except Exception:
                    pass
                return
            logger.info("[%s] Standard pipeline (cron) OK: %s, trades=%d",
                        run_id, result.get("decision", "?"),
                        len(result.get("trades", [])))
        finally:
            _standard_pipeline_running = False
            logger.debug("Standard pipeline job: flag cleared")
    except Exception as e:
        logger.error("Errore standard pipeline: %s", e, exc_info=True)


async def _simulator_auto_run_job():
    """
    Simulator auto-mode — ogni 30 minuti.

    Se l'utente ha abilitato la modalita' automatica dalla SimDashboard,
    questo job scieglie uno scenario random (cyclando le categorie),
    avvia un run V2 multi-step, lo porta a termine completo (start →
    N step → finalize) e salva su sim_runs. La memoria advice viene
    iniettata automaticamente dai prompt V2.

    Vincoli:
      - Skip se auto_mode_enabled = false
      - Skip se runs_today >= daily_cap (rispetta il budget configurato)
      - Skip se DEEPSEEK_API_KEY non configurata
      - Lock soft via sim_settings.auto_run_in_progress per evitare run
        sovrapposti (max 1 alla volta)
      - Timeout 8 min: se sfora, marca il flag come stale al prossimo tick

    Scelta scenario: alterna equity (norm/geo/macro/crash_rally) e crypto
    in modo round-robin per costruire memoria diversificata. Round-robin
    state in sim_settings.auto_last_engine.
    """
    import asyncio as _asyncio
    from uuid import uuid4 as _uuid4

    try:
        from simulator import db as sim_db
    except Exception as e:
        logger.warning("[SIM-AUTO] sim_db import failed: %s", e)
        return

    enabled = sim_db.get_setting("auto_mode_enabled", "false") == "true"
    if not enabled:
        return

    # Budget
    cap = int(sim_db.get_setting("auto_mode_daily_cap", "5") or 5)
    today = sim_db.runs_today(mode="auto")
    if today >= cap:
        logger.info("[SIM-AUTO] cap raggiunto (%d/%d), skip", today, cap)
        return

    # API key
    if not os.environ.get("DEEPSEEK_API_KEY"):
        logger.warning("[SIM-AUTO] DEEPSEEK_API_KEY mancante, skip")
        return

    # Lock soft (cross-pod best-effort)
    in_progress = sim_db.get_setting("auto_run_in_progress", "") or ""
    if in_progress:
        try:
            from datetime import datetime as _dt
            ts = _dt.fromisoformat(in_progress.replace("Z", "+00:00"))
            elapsed = (datetime.now(pytz.utc) - ts).total_seconds() / 60.0
            if elapsed < 9:
                logger.info("[SIM-AUTO] altro run in corso da %.1fmin, skip", elapsed)
                return
            logger.warning("[SIM-AUTO] lock stale (%.1fmin), forzo riavvio", elapsed)
        except Exception:
            # Lock corrotto, lo resetto
            pass

    sim_db.set_setting("auto_run_in_progress",
                        datetime.now(pytz.utc).isoformat())

    # Round-robin engine: equity / crypto alternati
    last_engine = sim_db.get_setting("auto_last_engine", "")
    use_crypto = (last_engine != "crypto")   # alterna
    sim_db.set_setting("auto_last_engine", "crypto" if use_crypto else "equity")

    run_id_for_log = "auto-" + str(_uuid4())[:8]
    logger.info("[SIM-AUTO][%s] avvio run automatico (engine=%s, today=%d/%d)",
                run_id_for_log, "crypto" if use_crypto else "equity", today, cap)

    try:
        # Esegue UN run completo (3-5 step equity, 5-7 step crypto) con timeout
        await _asyncio.wait_for(
            _execute_one_auto_run(use_crypto, run_id_for_log),
            timeout=480,   # 8 minuti hard cap
        )
    except _asyncio.TimeoutError:
        logger.error("[SIM-AUTO][%s] TIMEOUT (>8min), abort", run_id_for_log)
    except Exception as e:
        logger.error("[SIM-AUTO][%s] crash: %s", run_id_for_log, e, exc_info=True)
    finally:
        sim_db.set_setting("auto_run_in_progress", "")


async def _execute_one_auto_run(use_crypto: bool, log_id: str):
    """
    Esegue UNA partita simulator V2 end-to-end:
      1. start_run: sceglie random scenario, fetcha prezzi
      2. execute_step * num_steps: ad ogni step, applica trade dell'AI
      3. finalize_run: persiste su sim_runs con metriche complete

    Il flag mode='auto' viene poi propagato in `_persist_run` (categoria
    auto). Cosi' `runs_today(mode="auto")` puo' contarli.
    """
    import random as _random

    if use_crypto:
        from simulator import v2_crypto_engine as engine
        # Categorie crypto: bull_cycle, crash, regulatory_event, sideways
        category = _random.choice(["bull_cycle", "crash",
                                    "regulatory_event", "sideways"])
        num_steps = _random.choice([5, 6, 7])
        # run_mode="auto" propaga a active_runs.register e a sim_runs.mode
        start_kw = {"category": category, "num_steps": num_steps,
                    "run_mode": "auto"}
        start_fn = engine.start_crypto_run
        step_fn = engine.execute_crypto_step
        finalize_fn = engine.finalize_crypto_run
        engine_label = "CRYPTO"
    else:
        from simulator import v2_engine as engine
        category = _random.choice(["normale", "geopolitico",
                                    "macro", "crash_rally"])
        num_steps = _random.choice([3, 4, 5])
        start_kw = {"category": category, "num_steps": num_steps,
                    "run_mode": "auto"}
        start_fn = engine.start_run
        step_fn = engine.execute_step
        finalize_fn = engine.finalize_run
        engine_label = "EQUITY"

    logger.info("[SIM-AUTO][%s] %s: cat=%s steps=%d",
                log_id, engine_label, category, num_steps)

    payload = await start_fn(**start_kw)
    scenario = payload["scenario"]
    portfolio = payload["portfolio"]
    history: list = []
    total = payload["total_steps"]
    tracking_id = scenario.get("tracking_id")

    for i in range(total):
        try:
            step_data = await step_fn(scenario=scenario, portfolio=portfolio,
                                       history=history, step_index=i)
            portfolio = step_data["new_portfolio"]
            history.append(step_data)
        except Exception as e:
            logger.error("[SIM-AUTO][%s] step %d crash: %s — abort run",
                         log_id, i, e)
            # Marca errore nel registry cosi' l'utente vede il fallimento
            if tracking_id:
                try:
                    from simulator import active_runs as _ar
                    _ar.mark_error(tracking_id,
                                    f"step {i} crash: {str(e)[:200]}")
                except Exception:
                    pass
            return

    # finalize_fn accetta run_mode='auto' che propaga al record salvato:
    # sim_runs.mode = 'auto' → contato da runs_today() per il daily_cap.
    final_result = await finalize_fn(scenario=scenario, portfolio=portfolio,
                                      history=history, persist=True,
                                      run_mode="auto")

    pnl_pct = (final_result.get("final_valuation") or {}).get("total_pnl_pct", 0)
    sharpe = (final_result.get("quant_metrics") or {}).get("sharpe_ratio")
    logger.info("[SIM-AUTO][%s] run completato: P&L=%+.2f%% Sharpe=%s outcome=%s",
                log_id, pnl_pct,
                f"{sharpe:.2f}" if sharpe is not None else "n/d",
                final_result.get("outcome"))


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

    # ── Health Check: ogni 6 ore (cruscotto di salute) ──
    # Controlli read-only (invariante cassa, sync grafico, allarmi, provenienza
    # cassa) → scrive HEALTH_DIGEST su agent_logs. Primo run dopo 90s dal boot.
    _scheduler.add_job(
        _health_check_job,
        trigger="interval",
        hours=6,
        id="health_check_job",
        name="Health Check 6h (cruscotto salute, read-only)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(pytz.utc) + timedelta(seconds=90),
    )

    # ── Memory trim: ogni 30 min (fix OOM, backstop al trim post-pipeline) ──
    _scheduler.add_job(
        _memory_trim_job,
        trigger="interval",
        minutes=30,
        id="memory_trim_job",
        name="Memory trim 30min (gc + malloc_trim)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(pytz.utc) + timedelta(seconds=120),
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

    # BACKFILL: se l'ultimo AGG_8H e' > 12h fa (es. weekend in cui non e'
    # girato) facciamo girare il job in modo "force" subito al boot.
    # Stesso per AGG_4D se > 5 giorni.
    scout_8h_first_run = now_utc + timedelta(minutes=10)
    scout_4d_first_run = now_utc + timedelta(minutes=15)
    try:
        from agents.scout import _last_aggregated_age_hours, TIER_8H, TIER_4D
        last_8h_age = _last_aggregated_age_hours(database, TIER_8H)
        if last_8h_age is None or last_8h_age > 12:
            scout_8h_first_run = now_utc + timedelta(minutes=2)
            logger.info("Scout 8H backfill: ultimo report %s, forzo first_run a +2min",
                        f"{last_8h_age:.1f}h fa" if last_8h_age else "mai eseguito")
        last_4d_age = _last_aggregated_age_hours(database, TIER_4D)
        if last_4d_age is None or last_4d_age > 24 * 5:  # > 5 giorni
            scout_4d_first_run = now_utc + timedelta(minutes=4)
            logger.info("Scout 4D backfill: ultimo report %s, forzo first_run a +4min",
                        f"{last_4d_age/24:.1f}gg fa" if last_4d_age else "mai eseguito")
    except Exception as exc:
        logger.debug("Scout backfill check fallito: %s", exc)

    # 8H — ogni 8 ore. Il job stesso fa un check anti-double-run leggendo
    # l'ultimo AGG_8H dal buffer (skip se < 6h fa).
    _scheduler.add_job(
        _scout_8h_report_job,
        trigger="interval",
        hours=8,
        id="scout_8h_report",
        name="Scout 8H Report (consume L0 micro-cards)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        next_run_time=scout_8h_first_run,
    )

    # 4D — ogni 4 giorni. Il job stesso fa skip se l'ultimo AGG_4D è
    # < 3 giorni fa.
    _scheduler.add_job(
        _scout_4d_report_job,
        trigger="interval",
        days=4,
        id="scout_4d_report",
        name="Scout 4D Report (consume L1 8H-reports)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        next_run_time=scout_4d_first_run,
    )


    # ── Crypto pipeline: CRON ogni ora a :00, 24/7 ──
    # Schedule cron-fisso (non interval). Se watchdog triggera crypto a 14:35,
    # il prossimo cron resta alle 15:00 — la cadenza non viene sballata.
    #
    # FIX deploy-frequenti: con CronTrigger puro, ogni pod restart attende il
    # prossimo :00. Se i deploy avvengono nel range [:01–:59] in modo
    # consecutivo (oggi: 4 deploy in 3 ore), il job non gira MAI perche'
    # ogni :00 cade durante una transizione del pod. Soluzione: leggiamo
    # l'ultimo timestamp `last_decision_crypto_run_at` e, se >65min fa,
    # forziamo un run anticipato 90s dopo il boot. Il cron normale prosegue.
    crypto_first_run = None
    try:
        last_crypto = database.get_setting("last_decision_crypto_run_at", "")
        if last_crypto:
            from datetime import datetime as _dt
            last_dt = _dt.fromisoformat(last_crypto.replace("Z", "+00:00"))
            elapsed_min = (now_utc - last_dt).total_seconds() / 60.0
            if elapsed_min > 65:
                crypto_first_run = now_utc + timedelta(seconds=90)
                logger.info("Crypto pipeline: ultimo run %.0fmin fa (>65), "
                            "schedulo first_run a +90s dal boot", elapsed_min)
        else:
            # Mai eseguito → schedula subito
            crypto_first_run = now_utc + timedelta(seconds=90)
            logger.info("Crypto pipeline: mai eseguito, first_run a +90s dal boot")
    except Exception as exc:
        logger.debug("Crypto first_run check fallito: %s", exc)

    crypto_kwargs = dict(
        trigger=CronTrigger(minute=0),
        id="crypto_pipeline_job",
        name="Crypto pipeline (cron :00, 24/7, V3+R1)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    if crypto_first_run is not None:
        crypto_kwargs["next_run_time"] = crypto_first_run
    _scheduler.add_job(_crypto_pipeline_job, **crypto_kwargs)

    # ── CryptoMonitor: ogni 15 minuti, 24/7 ──────────────────────────────
    # Sorveglianza trend-health delle posizioni crypto aperte tramite
    # DeepSeek-V3. Vede 25 candele 15-min per ogni asset e classifica:
    # HEALTHY / WARNING / REVERSAL_CONFIRMED. Su REVERSAL_CONFIRMED logga
    # alert + (se auto_tighten enabled) restringe lo SL. NON esegue trade
    # autonomi — alza la bandiera e lascia decidere al Decision Crypto.
    #
    # No-op se non ci sono posizioni crypto aperte → costo zero quando
    # non e' rilevante. Per disabilitare in toto: set
    # crypto_monitor_enabled=false in settings.
    _scheduler.add_job(
        _crypto_monitor_job,
        trigger="interval",
        minutes=15,
        id="crypto_monitor_job",
        name="CryptoMonitor 15min (DeepSeek-V3 trend health)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        # Avvia 60s dopo il boot per dare tempo al price polling
        next_run_time=now_utc + timedelta(seconds=60),
    )

    # ── Risk Safety: ogni 5 minuti, 24/7 ────────────────────────────────
    # Applica le regole stateful di risk management:
    #   - 24h drawdown circuit breaker (liquida tutto + recovery mode)
    #   - Recovery mode exit (auto quando torna >= initial balance)
    #   - Lock-in 0.5% sui posizioni a +5% di profitto
    #   - Trailing stop dinamico 4% dal picco su posizioni > +10%
    #   - Concentration trigger log (alert se posizione > 35% NAV)
    # No-op se portafoglio vuoto o prezzi non aggiornati.
    _scheduler.add_job(
        _risk_safety_job,
        trigger="interval",
        minutes=5,
        id="risk_safety_job",
        name="Risk Safety 5min (circuit breaker + lock-in + trailing)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        next_run_time=now_utc + timedelta(seconds=90),
    )

    # ── Standard pipeline (Tech + Decision Sonnet): CRON solo NYSE hours ──
    # 14, 16, 18, 20 UTC (= 10:00, 12:00, 14:00, 16:00 ET) Lun-Ven.
    # NYSE apre 13:30 UTC, chiude 20:00 UTC → primo run a 14:00, ultimo a 20:00.
    # Skip auto se mercato chiuso (festività). Watchdog può comunque triggerare
    # extra runs durante NYSE hours.
    # Stesso fix deploy-frequenti del crypto pipeline: se ultimo run >2h e
    # NYSE attualmente aperta, schedula run anticipato 120s dal boot.
    standard_first_run = None
    try:
        last_sonnet = database.get_setting("last_decision_sonnet_run_at", "")
        if last_sonnet and is_market_open():
            from datetime import datetime as _dt
            last_dt = _dt.fromisoformat(last_sonnet.replace("Z", "+00:00"))
            elapsed_min = (now_utc - last_dt).total_seconds() / 60.0
            if elapsed_min > 125:   # >2h05min (cron a 14/16/18/20 = ogni 2h)
                standard_first_run = now_utc + timedelta(seconds=120)
                logger.info("Standard pipeline: ultimo run %.0fmin fa (>125) "
                            "+ NYSE aperta, schedulo first_run a +120s",
                            elapsed_min)
    except Exception as exc:
        logger.debug("Standard first_run check fallito: %s", exc)

    # CRITICO: minute=2 (non 0). La crypto pipeline gira a :00 24/7 e le
    # due si serializzano via live_pipeline_lock (vedi nota in cima al file).
    # Shift di 2 min permette alla crypto di partire prima e ridurre la
    # finestra di contesa al boot. La differenza percepita dall'utente e'
    # trascurabile (2 min su orari aperti).
    standard_kwargs = dict(
        trigger=CronTrigger(hour="14,16,18,20", minute=2,
                            day_of_week="mon-fri"),
        id="standard_pipeline_job",
        name="Standard pipeline (cron 14:02/16:02/18:02/20:02 UTC L-V)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    if standard_first_run is not None:
        standard_kwargs["next_run_time"] = standard_first_run
    _scheduler.add_job(_standard_pipeline_job, **standard_kwargs)

    # ── Coach Cards weekly synthesis: ogni Lunedi' alle 06:00 UTC ──
    # Legge la memoria del Sim Advisor e produce 3-5 Coach Cards per il
    # Decision Live. Cheap (~$0.001 per run via DeepSeek-V3).
    #
    # FIX backfill: se non e' mai stato eseguito (o l'ultimo synth e' >7gg
    # fa) facciamo girare subito al boot per chiudere il loop Sim → Live
    # senza dover aspettare il prossimo Lunedi'.
    cc_first_run = None
    try:
        from agents import coach_cards as _cc_mod
        last_synth = _cc_mod._settings_get("_coach_card::last_synth_at", "")
        if not last_synth:
            cc_first_run = now_utc + timedelta(minutes=3)
            logger.info("Coach Cards: mai sintetizzato, first_run a +3min dal boot")
        else:
            from datetime import datetime as _dt
            last_dt = _dt.fromisoformat(last_synth.replace("Z", "+00:00"))
            elapsed_d = (now_utc - last_dt).total_seconds() / 86400.0
            if elapsed_d > 7:
                cc_first_run = now_utc + timedelta(minutes=3)
                logger.info("Coach Cards: ultimo synth %.1fgg fa (>7), "
                            "first_run a +3min dal boot", elapsed_d)
    except Exception as exc:
        logger.debug("Coach Cards first_run check fallito: %s", exc)

    cc_kwargs = dict(
        trigger=CronTrigger(day_of_week="mon", hour=6, minute=0),
        id="coach_cards_weekly",
        name="Coach Cards weekly synthesis (Mon 06:00 UTC)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    if cc_first_run is not None:
        cc_kwargs["next_run_time"] = cc_first_run
    _scheduler.add_job(_coach_cards_weekly_job, **cc_kwargs)

    # ── Simulator AUTO-MODE: ogni 30 minuti ──────────────────────────────
    # No-op se auto_mode_enabled = false. Quando abilitato, fa girare un
    # run V2 completo (alterna equity/crypto) rispettando il daily_cap
    # configurato dalla SimDashboard. Idempotente, lock soft cross-pod.
    _scheduler.add_job(
        _simulator_auto_run_job,
        trigger="interval",
        minutes=30,
        id="simulator_auto_mode",
        name="Simulator auto-mode (30min, opt-in via UI)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        next_run_time=now_utc + timedelta(minutes=2),
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
            "schedule": "ogni 20 min L-V, ogni 60 min weekend/festivi",
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
