# ============================================================
# data_fetchers.py
# Modulo per il recupero di dati geopolitici e di mercato.
# Fonti: GDELT API, NewsAPI, yfinance.
# ============================================================

import os
import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import aiohttp
import yfinance

# Configurazione del logger
logger = logging.getLogger(__name__)

# Cache in-memory per yfinance (evita rate-limit 429)
# Struttura: { "TICKER:period_days": (timestamp, result) }
_yfinance_cache: Dict[str, tuple] = {}
_YFINANCE_CACHE_TTL = 300  # 5 minuti di TTL

# Cache GDELT — singolo result globale (evita 429 su run consecutivi dello Scout)
_gdelt_cache: Dict[str, tuple] = {}  # { "all": (timestamp, result) }
_GDELT_CACHE_TTL = 600  # 10 minuti

# Cache Reddit per evitare ban da User-Agent generico
_reddit_cache: Dict[str, tuple] = {}
_REDDIT_CACHE_TTL = 600  # 10 minuti

# Cache CoinGecko (rate limit free tier: 30 req/min)
_coingecko_cache: Dict[str, tuple] = {}
_COINGECKO_CACHE_TTL = 300  # 5 minuti

# User-Agent realistico per evitare blocchi
_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; GeoInvestAI/1.0; +https://geopolitical-investment-agent.onrender.com)"
}

# Chiave API per NewsAPI (fallback alla variabile d'ambiente)
_NEWS_API_KEY_ENV: str = os.environ.get("NEWS_API_KEY", "")


def _get_news_api_key() -> str:
    """Restituisce la chiave NewsAPI da DB settings o variabile d'ambiente."""
    try:
        import database as _db
        key = _db.get_setting("news_api_key", None)
        if key:
            return key
    except Exception:
        pass
    return _NEWS_API_KEY_ENV

# Lista di titoli da monitorare, suddivisi per settore
WATCHLIST: Dict[str, List[str]] = {
    "energy": ["XOM", "CVX", "SHEL", "TTE", "ENI"],
    "defense": ["LMT", "RTX", "NOC", "BA", "LDOS"],
    "gold_commodities": ["GLD", "SLV", "USO", "UNG"],
    "etf_broad": ["SPY", "QQQ", "EEM", "VEA"],
    "europe": ["EWG", "EWI", "EWQ", "EWP"],
}

# Endpoint base di GDELT per la ricerca di articoli
# Ridotto maxrecords da 25 a 10 per evitare rate-limit
GDELT_BASE_URL = (
    "https://api.gdeltproject.org/api/v2/doc/doc"
    "?query={keyword}&mode=artlist&maxrecords=10&timespan=24h&format=json"
)

# Parole chiave GDELT — ridotte a 3 per minimizzare 429 (era 5).
# Lo Scout gira ogni 20 min, quindi 3 keywords * 72 run/giorno = 216 chiamate/giorno
# (sotto la soglia di rate-limit GDELT).
GDELT_KEYWORDS: List[str] = [
    "war conflict sanctions",
    "oil energy crisis tariffs",
    "central bank rates inflation",
]

# Endpoint base di NewsAPI
NEWSAPI_BASE_URL = (
    "https://newsapi.org/v2/everything"
    "?q={query}&language=en&sortBy=publishedAt&pageSize=20&apiKey={api_key}"
)

# Query per NewsAPI — ridotto da 4 a 2 per stare nel free tier (100 req/giorno).
# Con Scout ogni 20 min e cache 1h: 2 query * 24 run/giorno = 48 chiamate/giorno.
NEWSAPI_QUERIES: List[str] = [
    "geopolitical risk OR armed conflict",   # accorpa le due query precedenti
    "economic sanctions OR energy crisis",   # accorpa le altre due
]

# Cache NewsAPI per query (TTL 1h: NewsAPI free è in delay 1h comunque)
_newsapi_cache: Dict[str, tuple] = {}  # { query: (timestamp, articles) }
_NEWSAPI_CACHE_TTL = 3600  # 1 ora

# Cooldown globale dopo un 429: NewsAPI free reset ogni 24h. Quando riceviamo
# 429, fermiamo le chiamate per 6h (compromesso fra "aspetta il reset" e
# "potrebbero essere transienti"). Salviamo l'epoch fino a cui restare zitti.
_newsapi_cooldown_until: float = 0.0
_NEWSAPI_COOLDOWN_AFTER_429 = 6 * 3600  # 6h


# ------------------------------------------------------------
# Funzione interna per eseguire una singola richiesta GDELT
# Con retry esponenziale per gestire 429 rate-limit
# ------------------------------------------------------------
async def _fetch_gdelt_single(
    session: aiohttp.ClientSession, keyword: str, max_retries: int = 3
) -> Dict[str, Any]:
    """Esegue una singola query verso GDELT e restituisce i risultati.
    Include retry con backoff esponenziale (2s/4s/8s) per 429 rate-limit."""
    from urllib.parse import quote_plus
    url = GDELT_BASE_URL.format(keyword=quote_plus(keyword))
    for attempt in range(max_retries):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30), headers=_HTTP_HEADERS) as resp:
                if resp.status == 429:
                    # Backoff piu' lungo: 5s, 10s, 20s
                    wait = 5 * (2 ** attempt)
                    logger.warning(
                        "GDELT 429 rate limit per '%s', retry in %ds... (tentativo %d/%d)",
                        keyword, wait, attempt + 1, max_retries,
                    )
                    await asyncio.sleep(wait)
                    continue
                if resp.status != 200:
                    logger.warning(
                        "GDELT ha restituito stato %s per la keyword: %s",
                        resp.status,
                        keyword,
                    )
                    return {"keyword": keyword, "articles": [], "error": f"HTTP {resp.status}"}

                # ROBUST PARSING: GDELT a volte ritorna HTTP 200 con body HTML
                # (rate-limit camuffato, "Your query rate is too high", maintenance page).
                # Per questo non possiamo fidarci di resp.json() — leggiamo come testo
                # e proviamo a parsare manualmente, gestendo tutti i fallimenti.
                try:
                    body = await resp.text()
                except Exception as read_err:
                    logger.warning("GDELT body read fallita per '%s': %s", keyword, read_err)
                    return {"keyword": keyword, "articles": [], "error": f"body_read: {read_err}"}

                stripped = body.lstrip()
                # Heuristics per HTML / messaggi di rate-limit lato server
                if not stripped or stripped[0] not in "{[":
                    snippet = stripped[:200].replace("\n", " ")
                    logger.warning(
                        "GDELT body non-JSON per '%s' (probabile rate-limit/HTML). Inizio body: %s",
                        keyword, snippet,
                    )
                    # Backoff e retry se sembra un rate-limit, altrimenti exit
                    if attempt < max_retries - 1 and ("query rate" in body.lower() or "<html" in body.lower()[:500]):
                        wait = 5 * (2 ** attempt)
                        logger.warning("GDELT soft rate-limit, retry in %ds...", wait)
                        await asyncio.sleep(wait)
                        continue
                    return {"keyword": keyword, "articles": [], "error": "html_or_rate_limit"}

                try:
                    import json as _json
                    data = _json.loads(body)
                except Exception as parse_err:
                    logger.warning(
                        "GDELT JSON parse fallita per '%s': %s. Body: %s",
                        keyword, parse_err, body[:200].replace("\n", " "),
                    )
                    return {"keyword": keyword, "articles": [], "error": f"json_parse: {parse_err}"}

                articles = data.get("articles", []) if isinstance(data, dict) else []
                return {"keyword": keyword, "articles": articles, "error": None}
        except Exception as exc:
            if attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)
                logger.warning(
                    "Errore GDELT per '%s' (tentativo %d/%d), retry in %ds: %s",
                    keyword, attempt + 1, max_retries, wait, exc,
                )
                await asyncio.sleep(wait)
                continue
            logger.error("Errore durante il recupero GDELT per '%s': %s", keyword, exc)
            return {"keyword": keyword, "articles": [], "error": str(exc)}
    # Se tutti i retry sono falliti per 429
    return {"keyword": keyword, "articles": [], "error": "429 rate limit dopo tutti i retry"}


async def fetch_gdelt_data() -> Dict[str, Any]:
    """
    Recupera dati geopolitici da GDELT in sequenza con rate-limiting.
    Le richieste sono distanziate di 2 secondi per evitare 429.
    Usa cache globale (10 min TTL) — lo Scout gira ogni 20 min, quindi 1 hit reale ogni 2 run.

    Restituisce un dizionario con:
        - results: lista di risultati per ogni keyword
        - fetched_at: timestamp del recupero
        - source: "gdelt"
    """
    # Cache check
    now_ts = time.time()
    if "all" in _gdelt_cache:
        cached_time, cached_result = _gdelt_cache["all"]
        if now_ts - cached_time < _GDELT_CACHE_TTL:
            logger.debug("GDELT cache hit (age: %ds)", int(now_ts - cached_time))
            return {**cached_result, "from_cache": True}

    try:
        cleaned: List[Dict[str, Any]] = []
        async with aiohttp.ClientSession(headers=_HTTP_HEADERS) as session:
            # Richieste in sequenza con 2s di pausa tra una e l'altra (era 1s)
            for kw in GDELT_KEYWORDS:
                result = await _fetch_gdelt_single(session, kw)
                cleaned.append(result)
                await asyncio.sleep(2.0)

        result = {
            "results": cleaned,
            "fetched_at": datetime.utcnow().isoformat(),
            "source": "gdelt",
        }
        # Salva in cache solo se almeno un risultato ha articoli (evita di cachare 429 globale)
        if any(r.get("articles") for r in cleaned):
            _gdelt_cache["all"] = (now_ts, result)
        return result
    except Exception as exc:
        logger.error("Errore critico in fetch_gdelt_data: %s", exc)
        return {
            "results": [],
            "fetched_at": datetime.utcnow().isoformat(),
            "source": "gdelt",
            "error": str(exc),
        }


# ------------------------------------------------------------
# Funzione interna per eseguire una singola richiesta NewsAPI
# ------------------------------------------------------------
async def _fetch_newsapi_single(
    session: aiohttp.ClientSession, query: str
) -> Dict[str, Any]:
    """Esegue una singola query verso NewsAPI e restituisce i risultati.

    Robustezza:
    - Cache in-memory 1h per query (NewsAPI free è già delayed di 1h)
    - Cooldown globale 6h dopo un 429 (rate limit free = 100/giorno)
    - Anche durante il cooldown, ritorna l'ultima cache valida se presente
    """
    global _newsapi_cooldown_until
    now_ts = time.time()

    # 1. Cache hit?
    cached = _newsapi_cache.get(query)
    if cached and (now_ts - cached[0]) < _NEWSAPI_CACHE_TTL:
        return {"query": query, "articles": cached[1], "error": None, "cached": True}

    # 2. Siamo in cooldown post-429? Ritorna cache stale (se c'è) o lista vuota.
    if now_ts < _newsapi_cooldown_until:
        if cached:
            logger.debug("NewsAPI in cooldown, uso cache stale per '%s'", query)
            return {"query": query, "articles": cached[1], "error": None,
                    "cached": True, "stale": True}
        return {"query": query, "articles": [],
                "error": "rate_limit_cooldown", "cached": False}

    api_key = _get_news_api_key()
    if not api_key:
        logger.warning("NEWS_API_KEY non configurata; la query '%s' viene saltata.", query)
        return {"query": query, "articles": [], "error": "NEWS_API_KEY mancante"}

    url = NEWSAPI_BASE_URL.format(query=query, api_key=api_key)
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status == 429:
                # Quota giornaliera esaurita: attiva cooldown per 6h e ritorna
                # cache stale se disponibile.
                _newsapi_cooldown_until = now_ts + _NEWSAPI_COOLDOWN_AFTER_429
                logger.warning(
                    "NewsAPI 429 (quota giornaliera esaurita). Cooldown attivato per 6h. Query: %s",
                    query,
                )
                if cached:
                    return {"query": query, "articles": cached[1], "error": None,
                            "cached": True, "stale": True}
                return {"query": query, "articles": [], "error": "HTTP 429"}
            if resp.status != 200:
                logger.warning(
                    "NewsAPI ha restituito stato %s per la query: %s",
                    resp.status,
                    query,
                )
                return {"query": query, "articles": [], "error": f"HTTP {resp.status}"}
            data = await resp.json(content_type=None)
            articles = data.get("articles", []) if isinstance(data, dict) else []
            # Salva in cache solo se abbiamo articoli (evita di cachare empty)
            if articles:
                _newsapi_cache[query] = (now_ts, articles)
            return {"query": query, "articles": articles, "error": None, "cached": False}
    except Exception as exc:
        logger.error("Errore durante il recupero NewsAPI per '%s': %s", query, exc)
        # Su errore di rete, ritorna cache stale se c'è
        if cached:
            return {"query": query, "articles": cached[1], "error": str(exc),
                    "cached": True, "stale": True}
        return {"query": query, "articles": [], "error": str(exc)}


async def fetch_newsapi_data() -> Dict[str, Any]:
    """
    Recupera notizie geopolitiche da NewsAPI in parallelo per tutte le query.

    Restituisce un dizionario con:
        - results: lista di risultati per ogni query
        - fetched_at: timestamp del recupero
        - source: "newsapi"
    """
    try:
        async with aiohttp.ClientSession() as session:
            # Lancia tutte le richieste in parallelo
            tasks = [
                _fetch_newsapi_single(session, q) for q in NEWSAPI_QUERIES
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        # Gestisce eventuali eccezioni restituite da gather
        cleaned: List[Dict[str, Any]] = []
        for i, res in enumerate(results):
            if isinstance(res, Exception):
                logger.error(
                    "Eccezione nella query NewsAPI '%s': %s",
                    NEWSAPI_QUERIES[i],
                    res,
                )
                cleaned.append({
                    "query": NEWSAPI_QUERIES[i],
                    "articles": [],
                    "error": str(res),
                })
            else:
                cleaned.append(res)

        return {
            "results": cleaned,
            "fetched_at": datetime.utcnow().isoformat(),
            "source": "newsapi",
        }
    except Exception as exc:
        logger.error("Errore critico in fetch_newsapi_data: %s", exc)
        return {
            "results": [],
            "fetched_at": datetime.utcnow().isoformat(),
            "source": "newsapi",
            "error": str(exc),
        }


# ============================================================
# OHLCV via Polygon-compatible API (Polygon.io e Massive.com)
# Usato come PRIMARIO da fetch_market_data (yfinance è solo fallback).
# ============================================================
POLYGON_BASE_URL = "https://api.polygon.io"
MASSIVE_BASE_URL_OHLCV = "https://api.massive.com"
PROVIDER_OHLCV_TIMEOUT = 15  # secondi (OHLCV può essere più lento del current price)


def _get_polygon_key() -> str:
    """API key Polygon.io da env var o DB settings."""
    key = os.environ.get("POLYGON_API_KEY", "")
    if not key:
        try:
            import database as _db
            key = _db.get_setting("polygon_api_key", "") or ""
        except Exception:
            pass
    return key


def _get_massive_key_ohlcv() -> str:
    """API key Massive.com da env var o DB settings."""
    key = os.environ.get("MASSIVE_API_KEY", "")
    if not key:
        try:
            import database as _db
            key = _db.get_setting("massive_api_key", "") or ""
        except Exception:
            pass
    return key


async def _fetch_provider_ohlcv_async(
    base_url: str, ticker: str, period_days: int, api_key: str, provider: str
) -> Dict[str, Any]:
    """
    Fetch OHLCV daily bars via API Polygon-compatible.
    Endpoint: /v2/aggs/ticker/{ticker}/range/1/day/{from}/{to}

    Returns: {ticker, data: [{date, open, high, low, close, volume}], error, source}

    Conversione automatica formato crypto: yfinance usa "BTC-USD",
    Polygon vuole "X:BTCUSD". Senza questa conversione, ogni richiesta
    crypto live risponde 404/no-results e il cascade scende a yfinance
    rate-limited 24/7 → tech_crypto_no_data cronico.
    """
    if not api_key:
        return {"ticker": ticker, "data": [], "error": f"{provider}: no API key", "source": provider}

    # Converti crypto da formato yfinance al formato Polygon prima di costruire l'URL.
    # Lo facciamo qui (non dal caller) cosi' tutti i path sync/async sono coperti.
    polygon_ticker = ticker
    if "-USD" in ticker and not ticker.startswith("X:"):
        polygon_ticker = f"X:{ticker.replace('-USD', 'USD')}"

    end_date = datetime.utcnow().date()
    start_date = end_date - timedelta(days=period_days + 5)  # buffer per weekend
    url = (
        f"{base_url}/v2/aggs/ticker/{polygon_ticker}/range/1/day/"
        f"{start_date.isoformat()}/{end_date.isoformat()}"
    )
    params = {"adjusted": "true", "sort": "asc", "limit": 5000, "apiKey": api_key}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, params=params,
                timeout=aiohttp.ClientTimeout(total=PROVIDER_OHLCV_TIMEOUT)
            ) as resp:
                if resp.status == 429:
                    return {"ticker": ticker, "data": [],
                            "error": f"{provider}: rate limit (429)", "source": provider}
                if resp.status != 200:
                    body = await resp.text()
                    return {"ticker": ticker, "data": [],
                            "error": f"{provider}: HTTP {resp.status}: {body[:150]}",
                            "source": provider}
                data = await resp.json()
                results = data.get("results") or []
                if not results:
                    return {"ticker": ticker, "data": [],
                            "error": f"{provider}: no results", "source": provider}
                records: List[Dict[str, Any]] = []
                for bar in results:
                    ts_ms = bar.get("t", 0)
                    if not ts_ms:
                        continue
                    date_str = datetime.fromtimestamp(
                        ts_ms / 1000, tz=__import__("datetime").timezone.utc
                    ).date().isoformat()
                    records.append({
                        "date": date_str,
                        "open": float(bar.get("o") or 0),
                        "high": float(bar.get("h") or 0),
                        "low": float(bar.get("l") or 0),
                        "close": float(bar.get("c") or 0),
                        "volume": int(bar.get("v") or 0),
                    })
                if not records:
                    return {"ticker": ticker, "data": [],
                            "error": f"{provider}: empty records", "source": provider}
                return {
                    "ticker": ticker,
                    "data": records,
                    "fetched_at": datetime.utcnow().isoformat(),
                    "error": None,
                    "source": provider,
                }
    except Exception as exc:
        return {"ticker": ticker, "data": [],
                "error": f"{provider}: {str(exc)[:200]}", "source": provider}


async def fetch_historical_range_async(
    ticker: str, from_date: str, to_date: str
) -> Dict[str, Any]:
    """
    Fetch OHLCV bars per un range di date specifico (formato ISO 'YYYY-MM-DD').
    Usato dal Simulator per ottenere prezzi reali durante lo scenario.

    Cascade: Polygon → Massive (entrambi Polygon-compatible API).
    Returns: {ticker, data: [{date, open, high, low, close, volume}], error, source}

    NOTA: per crypto usa formato yfinance "BTC-USD" che è incompatibile con
    Polygon. Polygon vuole "X:BTCUSD". Conversione gestita automaticamente.
    """
    # Conversione formato crypto: yfinance ("BTC-USD") → Polygon ("X:BTCUSD")
    polygon_ticker = ticker
    if "-USD" in ticker and not ticker.startswith("X:"):
        polygon_ticker = f"X:{ticker.replace('-USD', 'USD')}"

    polygon_key = _get_polygon_key()
    massive_key = _get_massive_key_ohlcv()

    async def _try_provider(base_url: str, api_key: str, provider: str):
        if not api_key:
            return None
        url = (
            f"{base_url}/v2/aggs/ticker/{polygon_ticker}/range/1/day/"
            f"{from_date}/{to_date}"
        )
        params = {"adjusted": "true", "sort": "asc", "limit": 5000, "apiKey": api_key}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, params=params,
                    timeout=aiohttp.ClientTimeout(total=PROVIDER_OHLCV_TIMEOUT)
                ) as resp:
                    if resp.status != 200:
                        return None
                    payload = await resp.json()
                    results = payload.get("results") or []
                    records = []
                    for bar in results:
                        ts_ms = bar.get("t", 0)
                        if not ts_ms:
                            continue
                        date_str = datetime.fromtimestamp(
                            ts_ms / 1000,
                            tz=__import__("datetime").timezone.utc
                        ).date().isoformat()
                        records.append({
                            "date": date_str,
                            "open": float(bar.get("o") or 0),
                            "high": float(bar.get("h") or 0),
                            "low": float(bar.get("l") or 0),
                            "close": float(bar.get("c") or 0),
                            "volume": int(bar.get("v") or 0),
                        })
                    if records:
                        return {
                            "ticker": ticker, "data": records,
                            "fetched_at": datetime.utcnow().isoformat(),
                            "error": None, "source": provider,
                        }
        except Exception:
            pass
        return None

    # Polygon primario
    result = await _try_provider(POLYGON_BASE_URL, polygon_key, "polygon")
    if result:
        return result
    # Massive fallback
    result = await _try_provider(MASSIVE_BASE_URL_OHLCV, massive_key, "massive")
    if result:
        return result
    # Nessun provider disponibile o tutti falliti
    return {
        "ticker": ticker, "data": [],
        "error": "no historical data available (polygon/massive failed)",
        "source": "none",
    }


def fetch_historical_range_sync(
    ticker: str, from_date: str, to_date: str
) -> Dict[str, Any]:
    """Wrapper sincrono per fetch_historical_range_async."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(
                lambda: asyncio.run(
                    fetch_historical_range_async(ticker, from_date, to_date)
                )
            ).result(timeout=PROVIDER_OHLCV_TIMEOUT + 5)
    return asyncio.run(fetch_historical_range_async(ticker, from_date, to_date))


def _fetch_provider_ohlcv_sync(base_url: str, ticker: str, period_days: int,
                               api_key: str, provider: str) -> Dict[str, Any]:
    """
    Wrapper sincrono per _fetch_provider_ohlcv_async.
    Necessario perché fetch_market_data è sincrona e chiamata da
    contesti misti (sync da scheduler, async da agenti).
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # Già in event loop: thread pool per evitare deadlock
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(
                lambda: asyncio.run(
                    _fetch_provider_ohlcv_async(base_url, ticker, period_days,
                                                api_key, provider)
                )
            ).result(timeout=PROVIDER_OHLCV_TIMEOUT + 5)
    return asyncio.run(
        _fetch_provider_ohlcv_async(base_url, ticker, period_days, api_key, provider)
    )


def _is_ohlcv_corrupted(records: List[Dict[str, Any]]) -> bool:
    """
    Detect OHLCV corruption: se ≥90% dei close sono identici, i dati sono
    stantii/cached da feed corrotto (yfinance rate-limit silenzioso).
    """
    if len(records) < 5:
        return False
    closes = [r.get("close", 0) for r in records if r.get("close", 0) > 0]
    if len(closes) < 5:
        return False
    from collections import Counter
    most_common_close, count = Counter(round(c, 2) for c in closes).most_common(1)[0]
    return count >= max(5, int(len(closes) * 0.9))


# ------------------------------------------------------------
# Recupero dati di mercato (cascade Polygon → Massive → yfinance)
# ------------------------------------------------------------
def fetch_historical_price_at(ticker: str, target_dt: "datetime") -> dict | None:
    """
    Recupera il prezzo storico INTRADAY di un ticker al timestamp dato.
    Usa yfinance con interval 5m (default) o 15m per copertura ~60 giorni.
    Ritorna {price, candle_open, candle_close, candle_time, source} oppure
    None se nessun dato disponibile.

    Usato dall'endpoint admin /api/admin/fix-position-entry-prices per
    ricostruire i prezzi di apertura corretti delle posizioni gia' aperte
    (basandosi su opened_at) quando il prezzo nel DB risulta sbagliato.

    Strategia: fetcha le candele 5m delle ultime 60 giorni, trova quella
    piu' vicina a target_dt, ritorna il close (= prezzo a fine candela,
    proxy del prezzo effettivo durante quella finestra).
    """
    try:
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td

        # Normalizza target_dt a UTC
        if target_dt.tzinfo is None:
            target_dt = target_dt.replace(tzinfo=_tz.utc)

        now = _dt.now(_tz.utc)
        days_back = (now - target_dt).days + 2

        # yfinance interval 5m supporta fino a 60 giorni
        if days_back > 60:
            # Fallback a 1h (730 giorni) per posizioni piu' vecchie
            interval = "1h"
            period_days = min(days_back + 5, 720)
        elif days_back > 7:
            interval = "15m"
            period_days = min(days_back + 2, 60)
        else:
            interval = "5m"
            period_days = min(days_back + 2, 60)

        # Estraggo intraday — uso yfinance direttamente (Polygon free tier
        # non da intraday su crypto e equity in modo affidabile)
        start_date = (target_dt - _td(hours=4)).strftime("%Y-%m-%d")
        end_date = (target_dt + _td(hours=4)).strftime("%Y-%m-%d")

        df = yfinance.download(
            ticker,
            start=start_date,
            end=end_date,
            interval=interval,
            progress=False,
            auto_adjust=False,
            threads=False,
        )
        if df is None or df.empty:
            # Tenta un range piu' ampio
            df = yfinance.download(
                ticker,
                period=f"{min(period_days, 60)}d",
                interval=interval,
                progress=False,
                auto_adjust=False,
                threads=False,
            )
        if df is None or df.empty:
            logger.warning("fetch_historical_price_at %s @ %s: no data (interval %s)",
                           ticker, target_dt.isoformat(), interval)
            return None

        # MultiIndex columns case (multi-ticker download)
        if hasattr(df.columns, "levels"):
            try:
                df = df[ticker]
            except KeyError:
                pass

        # Trova la candela piu' vicina a target_dt
        target_ts = target_dt.timestamp()
        best_idx = None
        best_diff = None
        for idx in df.index:
            try:
                cand_dt = idx.to_pydatetime()
                if cand_dt.tzinfo is None:
                    cand_dt = cand_dt.replace(tzinfo=_tz.utc)
                diff = abs((cand_dt - target_dt).total_seconds())
                if best_diff is None or diff < best_diff:
                    best_diff = diff
                    best_idx = idx
            except Exception:
                continue

        if best_idx is None:
            return None

        row = df.loc[best_idx]
        c_open = float(row.get("Open", 0) or 0)
        c_close = float(row.get("Close", 0) or 0)
        # Prezzo "rappresentativo": media open+close (proxy del prezzo medio
        # durante la candela). In alternativa si potrebbe usare close.
        price = (c_open + c_close) / 2.0 if (c_open > 0 and c_close > 0) else c_close
        return {
            "price": round(price, 6),
            "candle_open": round(c_open, 6),
            "candle_close": round(c_close, 6),
            "candle_time": best_idx.to_pydatetime().replace(tzinfo=_tz.utc).isoformat(),
            "interval": interval,
            "delta_seconds": int(best_diff),
            "source": "yfinance",
        }
    except Exception as exc:
        logger.warning("fetch_historical_price_at %s failed: %s", ticker, exc)
        return None


def fetch_fresh_current_price(ticker: str) -> float | None:
    """
    Helper specializzato per execute_buy / execute_sell: forza il fetch
    fresco di un prezzo corrente, bypassando la cache di 5 min. Ritorna
    il close della candela piu' recente, oppure None se nessun feed
    risponde.

    Razionale: senza questo, una BUY/SELL puo' essere eseguita al prezzo
    cached (stale fino a 5 min). Se nel frattempo il prezzo si e' mosso,
    si crea un avg_buy_price disallineato dalla realta' → P&L immediato
    artificiale (positivo o negativo). Caso documentato: LINK-USD aperta
    a prezzo cached, subito dopo il polling aggiorna il current_price,
    appare una perdita/profitto inesistente.
    """
    try:
        md = fetch_market_data(ticker, period_days=2, bypass_cache=True)
        if md and md.get("data"):
            data = md["data"]
            last = data[-1]
            price = last.get("close")
            if price and float(price) > 0:
                return round(float(price), 6)
    except Exception as exc:
        logger.warning("fetch_fresh_current_price %s failed: %s", ticker, exc)
    return None


def fetch_market_data(ticker: str, period_days: int = 90,
                      bypass_cache: bool = False) -> Dict[str, Any]:
    """
    Scarica i dati OHLCV per un singolo ticker.
    Cascade: Polygon.io → Massive → yfinance (con corruption check).

    Cache in-memory (5 min TTL) condivisa tra tutte le fonti.

    Parametri:
        ticker: simbolo del titolo (es. "XOM")
        period_days: numero di giorni di storico da recuperare (default 90)
        bypass_cache: se True, FORZA il fetch ignorando la cache.
            Usato per esecuzioni di trade reali dove serve il prezzo
            piu' fresco possibile (evita scoperte di entry/exit con
            prezzo cached stale di 1-5 min, che possono produrre
            PnL apparente fittizio se il prezzo si e' mosso tra la
            decisione e l'esecuzione).

    Restituisce un dizionario con:
        - ticker, data, fetched_at, error, source
    """
    # Controlla la cache (chiave indipendente da fonte) — saltata se bypass
    cache_key = f"{ticker}:{period_days}"
    now = time.time()
    if not bypass_cache and cache_key in _yfinance_cache:
        cached_time, cached_result = _yfinance_cache[cache_key]
        if now - cached_time < _YFINANCE_CACHE_TTL:
            logger.debug("OHLCV cache hit per %s (source=%s)",
                         cache_key, cached_result.get("source", "?"))
            return cached_result

    # ── Provider 1: Polygon.io (primario) ────────────────────────────────────
    polygon_key = _get_polygon_key()
    if polygon_key:
        try:
            result = _fetch_provider_ohlcv_sync(
                POLYGON_BASE_URL, ticker, period_days, polygon_key, "polygon"
            )
            if result.get("data") and not result.get("error"):
                logger.debug("Polygon OK per %s (%d bars)", ticker, len(result["data"]))
                _yfinance_cache[cache_key] = (now, result)
                return result
            else:
                logger.info("Polygon fallito per %s: %s — provo Massive",
                            ticker, result.get("error"))
        except Exception as exc:
            logger.warning("Polygon eccezione per %s: %s — provo Massive", ticker, exc)

    # ── Provider 2: Massive (secondario, Polygon-compatible) ─────────────────
    massive_key = _get_massive_key_ohlcv()
    if massive_key:
        try:
            result = _fetch_provider_ohlcv_sync(
                MASSIVE_BASE_URL_OHLCV, ticker, period_days, massive_key, "massive"
            )
            if result.get("data") and not result.get("error"):
                logger.debug("Massive OK per %s (%d bars)", ticker, len(result["data"]))
                _yfinance_cache[cache_key] = (now, result)
                return result
            else:
                logger.info("Massive fallito per %s: %s — provo yfinance",
                            ticker, result.get("error"))
        except Exception as exc:
            logger.warning("Massive eccezione per %s: %s — provo yfinance", ticker, exc)

    # ── Provider 3: yfinance (ultima spiaggia) ───────────────────────────────
    yfinance_error = None
    try:
        # Pausa breve tra richieste consecutive per evitare rate-limit
        time.sleep(0.5)

        # Calcola le date di inizio e fine
        end_date = datetime.utcnow()
        start_date = end_date - timedelta(days=period_days)

        # IMPORTANTE: yfinance ora richiede curl_cffi (non requests). Lasciamolo
        # gestire la sessione internamente — passare session=requests.Session()
        # rompe le chiamate.
        df = yfinance.download(
            ticker,
            start=start_date.strftime("%Y-%m-%d"),
            end=end_date.strftime("%Y-%m-%d"),
            progress=False,
            auto_adjust=False,
        )

        if df.empty:
            logger.warning("yfinance vuoto per %s", ticker)
            yfinance_error = "Nessun dato disponibile da yfinance"
        else:
            # yfinance >= 0.2.31 restituisce colonne multi-index (Price, Ticker).
            # Appiattisci prendendo solo il primo livello.
            if isinstance(df.columns, __import__('pandas').MultiIndex):
                df.columns = df.columns.get_level_values(0)

            # Converte il DataFrame in una lista di dizionari
            records: List[Dict[str, Any]] = []
            for idx, row in df.iterrows():
                records.append({
                    "date": idx.strftime("%Y-%m-%d"),
                    "open": float(row.get("Open", 0)),
                    "high": float(row.get("High", 0)),
                    "low": float(row.get("Low", 0)),
                    "close": float(row.get("Close", 0)),
                    "volume": int(row.get("Volume", 0)),
                })

            # ── Corruption guard: yfinance può restituire stesso bar cached
            # per tutti i timestamp quando rate-limited. Se ≥90% dei close
            # sono identici, scartiamo i dati e passiamo al fallback successivo.
            if _is_ohlcv_corrupted(records):
                logger.warning(
                    "⚠ yfinance OHLCV CORROTTO per %s: ≥90%% close identici — scarto", ticker
                )
                yfinance_error = "yfinance OHLCV corruption (identical closes)"
            else:
                result = {
                    "ticker": ticker,
                    "data": records,
                    "fetched_at": datetime.utcnow().isoformat(),
                    "error": None,
                    "source": "yfinance",
                }
                # Salva in cache
                _yfinance_cache[cache_key] = (now, result)
                return result

    except Exception as exc:
        logger.error("Errore yfinance per '%s': %s", ticker, exc)
        yfinance_error = str(exc)

    # Tutte le fonti hanno fallito
    result = {
        "ticker": ticker,
        "data": [],
        "fetched_at": datetime.utcnow().isoformat(),
        "error": f"yfinance: {yfinance_error}",
    }
    # Cache anche gli errori per 60 secondi (evita spam di retry)
    _yfinance_cache[cache_key] = (now - _YFINANCE_CACHE_TTL + 60, result)
    return result


def get_all_watchlist_tickers() -> List[str]:
    """
    Restituisce una lista unica di tutti i ticker presenti nella watchlist.

    I ticker vengono restituiti senza duplicati, in ordine alfabetico.
    """
    tutti_i_ticker: set = set()
    for settore, tickers in WATCHLIST.items():
        tutti_i_ticker.update(tickers)
    return sorted(tutti_i_ticker)


# ============================================================
# Finnhub Congressional Trades
# ============================================================


async def fetch_congressional_trades() -> Dict[str, Any]:
    """
    Recupera i trade recenti dei membri del Congresso USA via Finnhub.
    Questi trade sono segnali insider forti perché i congressisti siedono
    su commissioni che regolano i settori in cui investono.
    """
    import database as _db
    api_key = _db.get_setting("finnhub_api_key", os.environ.get("FINNHUB_API_KEY", ""))
    if not api_key:
        logger.warning("FINNHUB_API_KEY non configurata; congressional trades saltati.")
        return {
            "trades": [],
            "fetched_at": datetime.utcnow().isoformat(),
            "source": "finnhub_congressional",
            "error": "FINNHUB_API_KEY mancante",
        }

    url = f"https://finnhub.io/api/v1/stock/congressional-trading?token={api_key}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    logger.warning("Finnhub congressional trading ha restituito stato %s", resp.status)
                    return {
                        "trades": [],
                        "fetched_at": datetime.utcnow().isoformat(),
                        "source": "finnhub_congressional",
                        "error": f"HTTP {resp.status}",
                    }
                data = await resp.json(content_type=None)

        # data è una lista di trade. Filtra per amount > 50000 e raggruppa per ticker
        trades = data if isinstance(data, list) else data.get("data", [])

        # Filtra trade significativi (ultimi 30 giorni, amount > 50000)
        cutoff = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
        significant = []
        for t in trades:
            tx_date = t.get("transactionDate", "")
            amount_low = t.get("amountFrom", 0) or 0
            if tx_date >= cutoff and amount_low >= 50000:
                significant.append(t)

        # Raggruppa per ticker
        grouped = {}
        for t in significant:
            sym = t.get("symbol", "UNKNOWN")
            if sym not in grouped:
                grouped[sym] = {"buys": 0, "sells": 0, "total_amount": 0, "representatives": set()}
            tx_type = t.get("transactionType", "").lower()
            if "purchase" in tx_type or "buy" in tx_type:
                grouped[sym]["buys"] += 1
            elif "sale" in tx_type or "sell" in tx_type:
                grouped[sym]["sells"] += 1
            grouped[sym]["total_amount"] += (t.get("amountFrom", 0) or 0)
            rep = t.get("representative", "Unknown")
            grouped[sym]["representatives"].add(rep)

        # Converti in lista ordinata per numero di congressisti
        result_trades = []
        for sym, info in grouped.items():
            result_trades.append({
                "ticker": sym,
                "buy_count": info["buys"],
                "sell_count": info["sells"],
                "total_representatives": len(info["representatives"]),
                "total_amount_estimate": info["total_amount"],
                "representatives": list(info["representatives"])[:5],
            })
        result_trades.sort(key=lambda x: x["total_representatives"], reverse=True)

        return {
            "trades": result_trades[:20],
            "total_significant_trades": len(significant),
            "fetched_at": datetime.utcnow().isoformat(),
            "source": "finnhub_congressional",
            "error": None,
        }
    except Exception as exc:
        logger.error("Errore in fetch_congressional_trades: %s", exc)
        return {
            "trades": [],
            "fetched_at": datetime.utcnow().isoformat(),
            "source": "finnhub_congressional",
            "error": str(exc),
        }


# ============================================================
# yFinance News — headlines per ticker watchlist
# ============================================================

async def fetch_yfinance_news(max_per_ticker: int = 3, max_tickers: int = 10) -> Dict[str, Any]:
    """
    Recupera le news piu' recenti via yfinance.Ticker(symbol).news per i ticker
    della watchlist + posizioni aperte. Funziona senza API key.
    """
    items: List[Dict[str, Any]] = []
    try:
        import database as _db
        # Combina watchlist + posizioni aperte
        tickers: List[str] = []
        try:
            positions = _db.get_positions() or []
            for p in positions:
                if isinstance(p, dict):
                    t = p.get("ticker") or p.get("symbol")
                    if t and t not in tickers:
                        tickers.append(t)
        except Exception:
            pass
        for tlist in WATCHLIST.values():
            for t in tlist[:2]:
                if t not in tickers:
                    tickers.append(t)
        tickers = tickers[:max_tickers]

        # Usa il running loop (non get_event_loop, deprecato e bug-prone in 3.12+)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()

        def _fetch_one(symbol: str) -> List[Dict[str, Any]]:
            try:
                t_obj = yfinance.Ticker(symbol)
                news = getattr(t_obj, "news", None) or []
                out = []
                for n in news[:max_per_ticker]:
                    out.append({
                        "ticker": symbol,
                        "title": n.get("title") or n.get("content", {}).get("title", ""),
                        "publisher": n.get("publisher") or n.get("content", {}).get("provider", {}).get("displayName", ""),
                        "link": n.get("link") or n.get("content", {}).get("canonicalUrl", {}).get("url", ""),
                        "published": n.get("providerPublishTime") or 0,
                    })
                return out
            except Exception as e:
                logger.debug("yfinance news error %s: %s", symbol, e)
                return []

        # In parallelo (limitato dal GIL ma I/O di rete -> OK)
        coros = [loop.run_in_executor(None, _fetch_one, t) for t in tickers]
        results = await asyncio.gather(*coros, return_exceptions=True)
        for r in results:
            if isinstance(r, list):
                items.extend(r)
    except Exception as e:
        logger.warning("fetch_yfinance_news error: %s", e)

    return {
        "items": items,
        "count": len(items),
        "fetched_at": datetime.utcnow().isoformat(),
        "source": "yfinance_news",
        "error": None,
    }


# ============================================================
# Reddit — sentiment retail (no auth, public JSON API)
# ============================================================

async def fetch_reddit_sentiment(max_per_sub: int = 8) -> Dict[str, Any]:
    """
    Pesca i top post recenti dai subreddit retail-investing (no auth richiesta).
    Reddit consente accesso pubblico ai feed JSON con un User-Agent custom.
    """
    # Cache check (10 min TTL)
    now_ts = time.time()
    if "all" in _reddit_cache:
        cached_time, cached_result = _reddit_cache["all"]
        if now_ts - cached_time < _REDDIT_CACHE_TTL:
            logger.debug("Reddit cache hit (age: %ds)", int(now_ts - cached_time))
            return {**cached_result, "from_cache": True}

    subs = [
        # Equity / opzioni retail
        "wallstreetbets", "stocks", "investing", "options",
        # Crypto retail (le crypto sono 24/7, sentiment Reddit e' un leading indicator)
        "CryptoCurrency", "CryptoMarkets", "Bitcoin", "ethtrader",
    ]
    # User-Agent strict per Reddit: deve essere unico e descrittivo, altrimenti 429
    headers = {
        "User-Agent": "linux:com.geoinvest.ai:v1.0 (by /u/geoinvest_bot)",
        "Accept": "application/json",
    }
    posts: List[Dict[str, Any]] = []

    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async def _one(sub: str) -> List[Dict[str, Any]]:
                url = f"https://www.reddit.com/r/{sub}/hot.json?limit={max_per_sub}"
                try:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            logger.debug("Reddit %s HTTP %d", sub, resp.status)
                            return []
                        data = await resp.json(content_type=None)
                except Exception as e:
                    logger.debug("Reddit %s err: %s", sub, e)
                    return []
                out = []
                for child in (data.get("data", {}).get("children", []) or [])[:max_per_sub]:
                    p = child.get("data", {}) or {}
                    if p.get("stickied"):
                        continue
                    out.append({
                        "subreddit": sub,
                        "title": p.get("title", "")[:200],
                        "score": p.get("score", 0),
                        "num_comments": p.get("num_comments", 0),
                        "upvote_ratio": p.get("upvote_ratio", 0),
                        "permalink": "https://reddit.com" + (p.get("permalink") or ""),
                        "created_utc": p.get("created_utc", 0),
                    })
                return out

            results = await asyncio.gather(*[_one(s) for s in subs], return_exceptions=True)
            for r in results:
                if isinstance(r, list):
                    posts.extend(r)
    except Exception as e:
        logger.warning("fetch_reddit_sentiment error: %s", e)

    # Ordina per engagement (score + commenti)
    posts.sort(key=lambda x: x.get("score", 0) + x.get("num_comments", 0), reverse=True)

    result = {
        "posts": posts[:30],
        "count": len(posts),
        "fetched_at": datetime.utcnow().isoformat(),
        "source": "reddit_public",
        "error": None,
    }
    # Salva in cache solo se abbiamo ottenuto qualcosa (non cachare risultati vuoti)
    if posts:
        _reddit_cache["all"] = (now_ts, result)
    return result


# ============================================================
# X (Twitter) — sentiment retail via API v2 con bearer token (opzionale)
# ============================================================

async def fetch_x_sentiment(max_results: int = 30) -> Dict[str, Any]:
    """
    Recupera tweet recenti sulle keyword finance. Richiede X_API_BEARER nelle
    settings (chiave API X v2). Se assente, restituisce risultato vuoto silenzioso.
    """
    try:
        import database as _db
        bearer = _db.get_setting("x_api_bearer", "") or os.environ.get("X_API_BEARER", "")
    except Exception:
        bearer = os.environ.get("X_API_BEARER", "")

    if not bearer:
        return {
            "tweets": [],
            "count": 0,
            "fetched_at": datetime.utcnow().isoformat(),
            "source": "x_api_v2",
            "error": "x_api_bearer_not_configured",
        }

    # Query mirata su sentiment finance + cashtag dei top ticker
    from urllib.parse import quote as _urlquote
    query = "(stocks OR market OR FOMC OR earnings) (lang:en) -is:retweet"
    url = (
        "https://api.twitter.com/2/tweets/search/recent"
        f"?query={_urlquote(query, safe='')}"
        f"&max_results={min(max_results, 100)}"
        "&tweet.fields=public_metrics,created_at,lang"
    )

    tweets: List[Dict[str, Any]] = []
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers={"Authorization": f"Bearer {bearer}"}) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:200]
                    return {
                        "tweets": [],
                        "count": 0,
                        "fetched_at": datetime.utcnow().isoformat(),
                        "source": "x_api_v2",
                        "error": f"HTTP {resp.status}: {body}",
                    }
                data = await resp.json(content_type=None)
        for t in data.get("data", []) or []:
            metrics = t.get("public_metrics", {}) or {}
            tweets.append({
                "id": t.get("id"),
                "text": (t.get("text") or "")[:280],
                "lang": t.get("lang"),
                "created_at": t.get("created_at"),
                "likes": metrics.get("like_count", 0),
                "retweets": metrics.get("retweet_count", 0),
                "replies": metrics.get("reply_count", 0),
            })
        # Ordina per engagement
        tweets.sort(key=lambda x: x.get("likes", 0) + x.get("retweets", 0) * 2, reverse=True)
    except Exception as e:
        logger.warning("fetch_x_sentiment error: %s", e)
        return {
            "tweets": [],
            "count": 0,
            "fetched_at": datetime.utcnow().isoformat(),
            "source": "x_api_v2",
            "error": str(e),
        }

    return {
        "tweets": tweets[:max_results],
        "count": len(tweets),
        "fetched_at": datetime.utcnow().isoformat(),
        "source": "x_api_v2",
        "error": None,
    }


# ============================================================
# CoinGecko — dati di mercato crypto (top coin + trending + dominance + Fear&Greed)
# Free tier: nessuna chiave necessaria, 30 req/min
# Doc: https://www.coingecko.com/en/api/documentation
# ============================================================

async def fetch_coingecko_data(top_n: int = 25) -> Dict[str, Any]:
    """
    Recupera dati di mercato crypto da CoinGecko (free, no auth):
      - Top N coin per market cap (price, 24h change, volume)
      - Global market: total market cap, BTC dominance, ETH dominance
      - Trending: 7 cripto trending del giorno
      - Fear & Greed Index (via alternative.me, sempre free no auth)

    Cache: 5 min TTL — rispetta il rate limit del free tier.
    """
    cache_key = f"all:{top_n}"
    now_ts = time.time()
    if cache_key in _coingecko_cache:
        cached_time, cached_result = _coingecko_cache[cache_key]
        if now_ts - cached_time < _COINGECKO_CACHE_TTL:
            return {**cached_result, "from_cache": True}

    base_url = "https://api.coingecko.com/api/v3"
    headers = {**_HTTP_HEADERS, "Accept": "application/json"}
    timeout = aiohttp.ClientTimeout(total=20)

    out: Dict[str, Any] = {
        "top_coins": [],
        "global": {},
        "trending": [],
        "fear_greed": None,
        "fetched_at": datetime.utcnow().isoformat(),
        "source": "coingecko",
        "error": None,
    }

    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            # 1) Top N coins per market cap
            try:
                url = f"{base_url}/coins/markets"
                params = {
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": min(top_n, 50),
                    "page": 1,
                    "price_change_percentage": "1h,24h,7d",
                }
                async with session.get(url, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        out["top_coins"] = [
                            {
                                "symbol": c.get("symbol", "").upper(),
                                "name": c.get("name"),
                                "yfinance_ticker": f"{c.get('symbol', '').upper()}-USD",
                                "price_usd": c.get("current_price"),
                                "market_cap": c.get("market_cap"),
                                "volume_24h": c.get("total_volume"),
                                "change_1h": c.get("price_change_percentage_1h_in_currency"),
                                "change_24h": c.get("price_change_percentage_24h"),
                                "change_7d": c.get("price_change_percentage_7d_in_currency"),
                                "ath_change_pct": c.get("ath_change_percentage"),
                            }
                            for c in (data or [])
                        ]
                    else:
                        logger.warning("CoinGecko markets HTTP %d", resp.status)
            except Exception as e:
                logger.warning("CoinGecko markets fail: %s", e)

            # 2) Global market overview
            try:
                async with session.get(f"{base_url}/global") as resp:
                    if resp.status == 200:
                        g = (await resp.json()).get("data", {})
                        mcap = g.get("market_cap_percentage", {}) or {}
                        out["global"] = {
                            "total_market_cap_usd": (g.get("total_market_cap") or {}).get("usd"),
                            "total_volume_24h_usd": (g.get("total_volume") or {}).get("usd"),
                            "market_cap_change_24h_pct": g.get("market_cap_change_percentage_24h_usd"),
                            "btc_dominance": mcap.get("btc"),
                            "eth_dominance": mcap.get("eth"),
                            "active_cryptocurrencies": g.get("active_cryptocurrencies"),
                        }
            except Exception as e:
                logger.warning("CoinGecko global fail: %s", e)

            # 3) Trending coins (top 7 della giornata su CG)
            try:
                async with session.get(f"{base_url}/search/trending") as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        out["trending"] = [
                            {
                                "symbol": (c.get("item") or {}).get("symbol", "").upper(),
                                "name": (c.get("item") or {}).get("name"),
                                "yfinance_ticker": f"{(c.get('item') or {}).get('symbol', '').upper()}-USD",
                                "market_cap_rank": (c.get("item") or {}).get("market_cap_rank"),
                                "score": (c.get("item") or {}).get("score"),
                            }
                            for c in (data.get("coins") or [])
                        ]
            except Exception as e:
                logger.warning("CoinGecko trending fail: %s", e)

            # 4) Fear & Greed (alternative.me — gratis, no auth)
            try:
                async with session.get("https://api.alternative.me/fng/?limit=1") as resp:
                    if resp.status == 200:
                        fg = (await resp.json()).get("data", [{}])[0]
                        out["fear_greed"] = {
                            "value": int(fg.get("value", 0)) if fg.get("value") else None,
                            "classification": fg.get("value_classification"),
                            "timestamp": fg.get("timestamp"),
                        }
            except Exception as e:
                logger.warning("Fear&Greed fail: %s", e)

    except Exception as e:
        out["error"] = str(e)
        logger.error("CoinGecko global error: %s", e)

    _coingecko_cache[cache_key] = (now_ts, out)
    return out
