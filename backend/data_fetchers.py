# ============================================================
# data_fetchers.py
# Modulo per il recupero di dati geopolitici e di mercato.
# Fonti: GDELT API, NewsAPI, yfinance.
# ============================================================

import os
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import aiohttp
import yfinance

# Configurazione del logger
logger = logging.getLogger(__name__)

# Chiave API per NewsAPI, letta dalle variabili d'ambiente
NEWS_API_KEY: str = os.environ.get("NEWS_API_KEY", "")

# Lista di titoli da monitorare, suddivisi per settore
WATCHLIST: Dict[str, List[str]] = {
    "energy": ["XOM", "CVX", "SHEL", "TTE", "ENI"],
    "defense": ["LMT", "RTX", "NOC", "BA", "LDOS"],
    "gold_commodities": ["GLD", "SLV", "USO", "UNG"],
    "etf_broad": ["SPY", "QQQ", "EEM", "VEA"],
    "europe": ["EWG", "EWI", "EWQ", "EWP"],
}

# Endpoint base di GDELT per la ricerca di articoli
GDELT_BASE_URL = (
    "https://api.gdeltproject.org/api/v2/doc/doc"
    "?query={keyword}&mode=artlist&maxrecords=25&timespan=24h&format=json"
)

# Parole chiave per le query GDELT
GDELT_KEYWORDS: List[str] = [
    "war conflict",
    "sanctions economy",
    "oil energy crisis",
    "military escalation",
    "trade war tariffs",
]

# Endpoint base di NewsAPI
NEWSAPI_BASE_URL = (
    "https://newsapi.org/v2/everything"
    "?q={query}&language=en&sortBy=publishedAt&pageSize=20&apiKey={api_key}"
)

# Query per NewsAPI
NEWSAPI_QUERIES: List[str] = [
    "geopolitical risk",
    "armed conflict",
    "economic sanctions",
    "energy crisis",
]


# ------------------------------------------------------------
# Funzione interna per eseguire una singola richiesta GDELT
# ------------------------------------------------------------
async def _fetch_gdelt_single(
    session: aiohttp.ClientSession, keyword: str
) -> Dict[str, Any]:
    """Esegue una singola query verso GDELT e restituisce i risultati."""
    url = GDELT_BASE_URL.format(keyword=keyword)
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                logger.warning(
                    "GDELT ha restituito stato %s per la keyword: %s",
                    resp.status,
                    keyword,
                )
                return {"keyword": keyword, "articles": [], "error": f"HTTP {resp.status}"}
            data = await resp.json(content_type=None)
            # GDELT restituisce gli articoli nella chiave "articles"
            articles = data.get("articles", []) if isinstance(data, dict) else []
            return {"keyword": keyword, "articles": articles, "error": None}
    except Exception as exc:
        logger.error("Errore durante il recupero GDELT per '%s': %s", keyword, exc)
        return {"keyword": keyword, "articles": [], "error": str(exc)}


async def fetch_gdelt_data() -> Dict[str, Any]:
    """
    Recupera dati geopolitici da GDELT in parallelo per tutte le keyword.

    Restituisce un dizionario con:
        - results: lista di risultati per ogni keyword
        - fetched_at: timestamp del recupero
        - source: "gdelt"
    """
    try:
        async with aiohttp.ClientSession() as session:
            # Lancia tutte le richieste in parallelo
            tasks = [
                _fetch_gdelt_single(session, kw) for kw in GDELT_KEYWORDS
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        # Gestisce eventuali eccezioni restituite da gather
        cleaned: List[Dict[str, Any]] = []
        for i, res in enumerate(results):
            if isinstance(res, Exception):
                logger.error(
                    "Eccezione nella query GDELT '%s': %s",
                    GDELT_KEYWORDS[i],
                    res,
                )
                cleaned.append({
                    "keyword": GDELT_KEYWORDS[i],
                    "articles": [],
                    "error": str(res),
                })
            else:
                cleaned.append(res)

        return {
            "results": cleaned,
            "fetched_at": datetime.utcnow().isoformat(),
            "source": "gdelt",
        }
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
    """Esegue una singola query verso NewsAPI e restituisce i risultati."""
    if not NEWS_API_KEY:
        logger.warning("NEWS_API_KEY non configurata; la query '%s' viene saltata.", query)
        return {"query": query, "articles": [], "error": "NEWS_API_KEY mancante"}

    url = NEWSAPI_BASE_URL.format(query=query, api_key=NEWS_API_KEY)
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                logger.warning(
                    "NewsAPI ha restituito stato %s per la query: %s",
                    resp.status,
                    query,
                )
                return {"query": query, "articles": [], "error": f"HTTP {resp.status}"}
            data = await resp.json(content_type=None)
            articles = data.get("articles", []) if isinstance(data, dict) else []
            return {"query": query, "articles": articles, "error": None}
    except Exception as exc:
        logger.error("Errore durante il recupero NewsAPI per '%s': %s", query, exc)
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
# Recupero dati di mercato tramite yfinance (sincrono)
# ------------------------------------------------------------
def fetch_market_data(ticker: str, period_days: int = 90) -> Dict[str, Any]:
    """
    Scarica i dati OHLCV per un singolo ticker usando yfinance.

    Parametri:
        ticker: simbolo del titolo (es. "XOM")
        period_days: numero di giorni di storico da recuperare (default 90)

    Restituisce un dizionario con:
        - ticker: simbolo del titolo
        - data: lista di record OHLCV giornalieri
        - fetched_at: timestamp del recupero
        - error: eventuale messaggio di errore
    """
    try:
        # Calcola le date di inizio e fine
        end_date = datetime.utcnow()
        start_date = end_date - timedelta(days=period_days)

        # Scarica i dati con yfinance (chiamata sincrona)
        df = yfinance.download(
            ticker,
            start=start_date.strftime("%Y-%m-%d"),
            end=end_date.strftime("%Y-%m-%d"),
            progress=False,
        )

        if df.empty:
            logger.warning("Nessun dato ricevuto da yfinance per il ticker: %s", ticker)
            return {
                "ticker": ticker,
                "data": [],
                "fetched_at": datetime.utcnow().isoformat(),
                "error": "Nessun dato disponibile",
            }

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

        return {
            "ticker": ticker,
            "data": records,
            "fetched_at": datetime.utcnow().isoformat(),
            "error": None,
        }
    except Exception as exc:
        logger.error("Errore durante il download di dati per '%s': %s", ticker, exc)
        return {
            "ticker": ticker,
            "data": [],
            "fetched_at": datetime.utcnow().isoformat(),
            "error": str(exc),
        }


def get_all_watchlist_tickers() -> List[str]:
    """
    Restituisce una lista unica di tutti i ticker presenti nella watchlist.

    I ticker vengono restituiti senza duplicati, in ordine alfabetico.
    """
    tutti_i_ticker: set = set()
    for settore, tickers in WATCHLIST.items():
        tutti_i_ticker.update(tickers)
    return sorted(tutti_i_ticker)
