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
    api_key = _get_news_api_key()
    if not api_key:
        logger.warning("NEWS_API_KEY non configurata; la query '%s' viene saltata.", query)
        return {"query": query, "articles": [], "error": "NEWS_API_KEY mancante"}

    url = NEWSAPI_BASE_URL.format(query=query, api_key=api_key)
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


# ============================================================
# ClawStreet API — dati di mercato gratuiti (no auth)
# ============================================================

CLAWSTREET_BASE = "https://www.clawstreet.io/api"
CLAWSTREET_TIMEOUT = aiohttp.ClientTimeout(total=5)


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
    Invia un trade mirror a ClawStreet. Restituisce il risultato.
    Per azioni, verifica che il mercato sia aperto (skip per crypto X:).
    """
    # Converti crypto prefix se necessario
    is_crypto = symbol.startswith("X:") or symbol.endswith("USD") and len(symbol) > 5
    if is_crypto and not symbol.startswith("X:"):
        symbol = f"X:{symbol}"

    # Per titoli azionari, controlla che i mercati siano aperti
    if not is_crypto:
        mkt = await check_clawstreet_market_status()
        mkt_data = mkt.get("data", {})
        # Se il campo open/isOpen esiste e indica chiuso, skippa
        is_open = mkt_data.get("isOpen", mkt_data.get("open", True))
        if not is_open:
            return {"mirrored": False, "reason": "Mercati USA chiusi, trade non inviato a ClawStreet"}

    url = f"{CLAWSTREET_BASE}/bots/{bot_id}/trade"
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
                    return {"mirrored": True, "status": resp.status, "response": body}
                else:
                    return {"mirrored": False, "status": resp.status, "response": body}
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
