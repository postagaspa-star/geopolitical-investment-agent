# ============================================================
# data_fetchers.py
# Modulo per il recupero di dati geopolitici e di mercato.
# Fonti: GDELT API, NewsAPI, yfinance, ClawStreet.
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


# ------------------------------------------------------------
# ClawStreet price fallback — usato quando yfinance fallisce
# ------------------------------------------------------------
async def fetch_clawstreet_price(ticker: str) -> Dict[str, Any]:
    """
    Recupera dati di prezzo per un singolo ticker da ClawStreet sentiment API.
    Usato come fallback quando yfinance non è disponibile (429, dati vuoti, eccezioni).

    Restituisce un dizionario con:
        - ticker: simbolo del titolo
        - price: prezzo corrente (se disponibile)
        - data: dati grezzi dalla risposta ClawStreet
        - error: eventuale messaggio di errore
    """
    url = f"{CLAWSTREET_BASE}/data/sentiment?symbol={ticker}&quant=1"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=CLAWSTREET_TIMEOUT) as resp:
                if resp.status != 200:
                    return {"ticker": ticker, "price": None, "data": None, "error": f"HTTP {resp.status}"}
                data = await resp.json(content_type=None)
                # Estrai prezzo dalla risposta ClawStreet
                price = None
                if isinstance(data, dict):
                    # ClawStreet può restituire il prezzo in vari campi
                    price = (
                        data.get("price")
                        or data.get("currentPrice")
                        or data.get("last_price")
                        or data.get("close")
                    )
                    # Cerca anche dentro un eventuale sotto-oggetto "quote" o "data"
                    if price is None and isinstance(data.get("data"), dict):
                        inner = data["data"]
                        price = (
                            inner.get("price")
                            or inner.get("currentPrice")
                            or inner.get("last_price")
                            or inner.get("close")
                        )
                return {"ticker": ticker, "price": price, "data": data, "error": None}
    except Exception as exc:
        logger.warning("ClawStreet price fallback error for %s: %s", ticker, exc)
        return {"ticker": ticker, "price": None, "data": None, "error": str(exc)}


async def fetch_clawstreet_price_data(ticker: str, period_days: int = 90) -> Dict[str, Any]:
    """
    Recupera dati OHLCV-like da ClawStreet per un singolo ticker.
    Drop-in fallback per fetch_market_data — restituisce lo stesso formato.

    Parametri:
        ticker: simbolo del titolo (es. "XOM")
        period_days: ignorato (ClawStreet restituisce solo dati correnti)

    Restituisce un dizionario con:
        - ticker: simbolo del titolo
        - data: lista di record OHLCV (un solo record con dati correnti)
        - fetched_at: timestamp del recupero
        - error: eventuale messaggio di errore
        - source: "clawstreet"
    """
    price_result = await fetch_clawstreet_price(ticker)

    if price_result.get("error") or price_result.get("price") is None:
        # Prova a estrarre almeno qualcosa dai dati grezzi
        raw_data = price_result.get("data")
        if raw_data and isinstance(raw_data, dict):
            # Costruisci un record parziale se ci sono dati utili
            inner = raw_data.get("data", raw_data)
            if isinstance(inner, dict):
                price = inner.get("price") or inner.get("close") or inner.get("currentPrice")
                if price is not None:
                    try:
                        price = float(price)
                        record = {
                            "date": datetime.utcnow().strftime("%Y-%m-%d"),
                            "open": price,
                            "high": price,
                            "low": price,
                            "close": price,
                            "volume": int(inner.get("volume", 0) or 0),
                        }
                        return {
                            "ticker": ticker,
                            "data": [record],
                            "fetched_at": datetime.utcnow().isoformat(),
                            "error": None,
                            "source": "clawstreet",
                        }
                    except (ValueError, TypeError):
                        pass

        return {
            "ticker": ticker,
            "data": [],
            "fetched_at": datetime.utcnow().isoformat(),
            "error": price_result.get("error") or "Nessun dato di prezzo da ClawStreet",
            "source": "clawstreet",
        }

    # Costruisci un record OHLCV dal prezzo corrente
    price = float(price_result["price"])
    raw_data = price_result.get("data", {})
    inner = raw_data.get("data", raw_data) if isinstance(raw_data, dict) else {}
    if not isinstance(inner, dict):
        inner = {}

    record = {
        "date": datetime.utcnow().strftime("%Y-%m-%d"),
        "open": float(inner.get("open", price)),
        "high": float(inner.get("high", price)),
        "low": float(inner.get("low", price)),
        "close": price,
        "volume": int(inner.get("volume", 0) or 0),
    }

    return {
        "ticker": ticker,
        "data": [record],
        "fetched_at": datetime.utcnow().isoformat(),
        "error": None,
        "source": "clawstreet",
    }


# ------------------------------------------------------------
# Recupero dati di mercato tramite yfinance (sincrono)
# Con fallback a ClawStreet se yfinance fallisce
# ------------------------------------------------------------
def fetch_market_data(ticker: str, period_days: int = 90) -> Dict[str, Any]:
    """
    Scarica i dati OHLCV per un singolo ticker usando yfinance.
    Include cache in-memory (5 min TTL) per evitare rate-limit 429.
    Se yfinance fallisce (dati vuoti, eccezione, 429), prova ClawStreet come fallback.

    Parametri:
        ticker: simbolo del titolo (es. "XOM")
        period_days: numero di giorni di storico da recuperare (default 90)

    Restituisce un dizionario con:
        - ticker: simbolo del titolo
        - data: lista di record OHLCV giornalieri
        - fetched_at: timestamp del recupero
        - error: eventuale messaggio di errore
    """
    # Controlla la cache
    cache_key = f"{ticker}:{period_days}"
    now = time.time()
    if cache_key in _yfinance_cache:
        cached_time, cached_result = _yfinance_cache[cache_key]
        if now - cached_time < _YFINANCE_CACHE_TTL:
            logger.debug("yfinance cache hit per %s", cache_key)
            return cached_result

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
            logger.warning("Nessun dato ricevuto da yfinance per il ticker: %s — provo ClawStreet fallback", ticker)
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

            result = {
                "ticker": ticker,
                "data": records,
                "fetched_at": datetime.utcnow().isoformat(),
                "error": None,
            }
            # Salva in cache
            _yfinance_cache[cache_key] = (now, result)
            return result

    except Exception as exc:
        logger.error("Errore yfinance per '%s': %s — provo ClawStreet fallback", ticker, exc)
        yfinance_error = str(exc)

    # ---- Fallback a ClawStreet ----
    logger.info("Tentativo fallback ClawStreet per %s", ticker)
    try:
        # Esegui la funzione async in modo sincrono
        loop = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # Siamo già in un event loop — crea un task con future
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                clawstreet_result = pool.submit(
                    lambda: asyncio.run(fetch_clawstreet_price_data(ticker, period_days))
                ).result(timeout=20)
        else:
            clawstreet_result = asyncio.run(fetch_clawstreet_price_data(ticker, period_days))

        if clawstreet_result.get("data") and not clawstreet_result.get("error"):
            logger.info("ClawStreet fallback riuscito per %s", ticker)
            # Aggiungi nota che i dati vengono da ClawStreet
            clawstreet_result["source"] = "clawstreet"
            clawstreet_result["yfinance_error"] = yfinance_error
            # Cache il risultato ClawStreet (TTL ridotto a 60s)
            _yfinance_cache[cache_key] = (now - _YFINANCE_CACHE_TTL + 60, clawstreet_result)
            return clawstreet_result
        else:
            logger.warning("Anche ClawStreet fallback ha fallito per %s: %s", ticker, clawstreet_result.get("error"))
    except Exception as cs_exc:
        logger.error("Errore nel fallback ClawStreet per '%s': %s", ticker, cs_exc)

    # Entrambe le fonti hanno fallito
    result = {
        "ticker": ticker,
        "data": [],
        "fetched_at": datetime.utcnow().isoformat(),
        "error": f"yfinance: {yfinance_error}; ClawStreet fallback anche fallito",
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
# ClawStreet API — dati di mercato gratuiti (no auth)
# ============================================================

CLAWSTREET_BASE = "https://www.clawstreet.io/api"
CLAWSTREET_TIMEOUT = aiohttp.ClientTimeout(total=15)  # Aumentato da 5s a 15s — API può essere lenta


async def fetch_clawstreet_sentiment(ticker: str) -> Dict[str, Any]:
    """
    Sentiment news per singolo ticker (-1 a +1) più dati quantitativi:
    put/call ratio, implied volatility, short interest.
    """
    url = f"{CLAWSTREET_BASE}/data/sentiment?symbol={ticker}&quant=1"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=CLAWSTREET_TIMEOUT) as resp:
                if resp.status != 200:
                    return {"ticker": ticker, "error": f"HTTP {resp.status}"}
                data = await resp.json(content_type=None)
                return {"ticker": ticker, "data": data, "error": None}
    except Exception as exc:
        logger.warning("ClawStreet sentiment error for %s: %s", ticker, exc)
        return {"ticker": ticker, "error": str(exc)}


async def fetch_clawstreet_market_context() -> Dict[str, Any]:
    """
    Contesto macro generale: SPY return, sentiment di mercato,
    performance per settore.
    """
    url = f"{CLAWSTREET_BASE}/data/market"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=CLAWSTREET_TIMEOUT) as resp:
                if resp.status != 200:
                    return {"error": f"HTTP {resp.status}"}
                data = await resp.json(content_type=None)
                return {"data": data, "error": None}
    except Exception as exc:
        logger.warning("ClawStreet market context error: %s", exc)
        return {"error": str(exc)}


async def fetch_clawstreet_economy() -> Dict[str, Any]:
    """
    Segnali obbligazionari: TLT, SHY, direzione della yield curve.
    Indica risk-on vs risk-off del mercato.
    """
    url = f"{CLAWSTREET_BASE}/data/economy"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=CLAWSTREET_TIMEOUT) as resp:
                if resp.status != 200:
                    return {"error": f"HTTP {resp.status}"}
                data = await resp.json(content_type=None)
                return {"data": data, "error": None}
    except Exception as exc:
        logger.warning("ClawStreet economy error: %s", exc)
        return {"error": str(exc)}


async def fetch_clawstreet_screener(indicator: str = "rsi", below: int = 35) -> Dict[str, Any]:
    """
    Screener bulk: tutti i simboli con RSI sotto una soglia.
    Utile per trovare asset in ipervenduto.
    """
    url = f"{CLAWSTREET_BASE}/data/scan?indicator={indicator}&below={below}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=CLAWSTREET_TIMEOUT) as resp:
                if resp.status != 200:
                    return {"error": f"HTTP {resp.status}"}
                data = await resp.json(content_type=None)
                return {"data": data, "error": None}
    except Exception as exc:
        logger.warning("ClawStreet screener error: %s", exc)
        return {"error": str(exc)}


async def fetch_clawstreet_fundamentals(ticker: str) -> Dict[str, Any]:
    """
    Fondamentali trimestrali: revenue, EPS, P/E, debt/equity, cash flow.
    """
    url = f"{CLAWSTREET_BASE}/data/fundamentals?symbol={ticker}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=CLAWSTREET_TIMEOUT) as resp:
                if resp.status != 200:
                    return {"ticker": ticker, "error": f"HTTP {resp.status}"}
                data = await resp.json(content_type=None)
                return {"ticker": ticker, "data": data, "error": None}
    except Exception as exc:
        logger.warning("ClawStreet fundamentals error for %s: %s", ticker, exc)
        return {"ticker": ticker, "error": str(exc)}


async def check_clawstreet_market_status() -> Dict[str, Any]:
    """Controlla se i mercati USA sono aperti secondo ClawStreet."""
    url = f"{CLAWSTREET_BASE}/market-status"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=CLAWSTREET_TIMEOUT) as resp:
                if resp.status != 200:
                    return {"open": False, "error": f"HTTP {resp.status}"}
                data = await resp.json(content_type=None)
                return {"data": data, "error": None}
    except Exception as exc:
        logger.warning("ClawStreet market-status error: %s", exc)
        return {"open": False, "error": str(exc)}


async def mirror_trade_to_clawstreet(
    bot_id: str, api_key: str, symbol: str, action: str,
    qty: int, reasoning: str
) -> Dict[str, Any]:
    """
    Invia un trade mirror a ClawStreet. SEMPRE — anche se il mercato e' chiuso,
    perche' ClawStreet e' una vetrina pubblica che mostra TUTTI i trade del bot
    (non un broker). La feature "market check" precedente bloccava il mirror dopo
    le 16:00 ET introducendo silenziosi fallimenti.
    """
    # Converti il formato crypto da yfinance a ClawStreet.
    # yfinance: "BTC-USD", "ETH-USD", "SOL-USD" (con trattino prima di USD)
    # ClawStreet: "X:BTCUSD", "X:ETHUSD", "X:SOLUSD" (X: prefix, senza trattino)
    # BUG FIX: il codice precedente produceva "X:BTC-USD" (con trattino) che
    # ClawStreet rifiutava silenziosamente con INVALID_SYMBOL.
    if not symbol.startswith("X:"):
        if "-USD" in symbol and len(symbol) > 5:
            # BTC-USD → BTCUSD → X:BTCUSD
            clean = symbol.replace("-USD", "USD")
            symbol = f"X:{clean}"
        elif symbol.endswith("USD") and len(symbol) > 5 and "-" not in symbol:
            # BTCUSD (già senza trattino, ma senza X: prefix) → X:BTCUSD
            symbol = f"X:{symbol}"
    # Se già "X:BTCUSD" o ticker azionario (AAPL, MSFT...) → lascia invariato

    # Endpoint corretto: /trades (plurale), non /trade
    url = f"{CLAWSTREET_BASE}/bots/{bot_id}/trades"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "symbol": symbol,
        "action": action.lower(),
        "qty": qty,
        "reasoning": reasoning[:280],
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=CLAWSTREET_TIMEOUT) as resp:
                body = await resp.text()
                if resp.status in (200, 201):
                    logger.info("ClawStreet mirror OK: %s %d %s -> HTTP %d", action, qty, symbol, resp.status)
                    return {"mirrored": True, "status": resp.status, "response": body[:500]}

                # Identifica errori "expected" (non-fatali) per skipparli senza spam
                error_code = ""
                try:
                    err_obj = __import__("json").loads(body)
                    error_code = (err_obj.get("error", {}) or {}).get("code", "") if isinstance(err_obj.get("error"), dict) else ""
                except Exception:
                    pass

                if error_code in ("INVALID_SYMBOL", "INSUFFICIENT_BUYING_POWER", "INSUFFICIENT_POSITION"):
                    logger.info("ClawStreet skip %s %d %s: %s",
                                action, qty, symbol, error_code)
                    return {"mirrored": False, "skipped": True, "reason": error_code,
                            "status": resp.status, "response": body[:500]}

                logger.warning("ClawStreet mirror FAIL: %s %d %s -> HTTP %d body=%s",
                               action, qty, symbol, resp.status, body[:200])
                return {"mirrored": False, "status": resp.status, "response": body[:500]}
    except Exception as exc:
        logger.warning("ClawStreet trade mirror error: %s", exc)
        return {"mirrored": False, "error": str(exc)}


async def register_clawstreet_bot(
    name: str, ticker: str, strategy: str, personality: str, bio: str
) -> Dict[str, Any]:
    """Registra un nuovo bot su ClawStreet."""
    url = f"{CLAWSTREET_BASE}/bots/register"
    payload = {
        "name": name,
        "ticker": ticker,
        "strategy": strategy,
        "personality": personality,
        "bio": bio,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=CLAWSTREET_TIMEOUT) as resp:
                body = await resp.json(content_type=None)
                if resp.status in (200, 201):
                    return {"success": True, "data": body}
                else:
                    return {"success": False, "status": resp.status, "data": body}
    except Exception as exc:
        logger.error("ClawStreet registration error: %s", exc)
        return {"success": False, "error": str(exc)}


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
