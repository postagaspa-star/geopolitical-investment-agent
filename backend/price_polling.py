"""
Price Polling Service — Massive API (primary) + yfinance (fallback) → Supabase ogni 60s.

Aggiorna la tabella `price_quotes` con i prezzi correnti delle posizioni aperte
e dei ticker della watchlist. Mantiene anche uno storico minuto-per-minuto
in `price_history` per analisi e dashboard.

Provider:
  1. Massive API (https://massive.com) — se MASSIVE_API_KEY env var presente
     - /v2/aggs/ticker/{ticker}/range/1/minute/{from}/{to} per current price
     - /v2/aggs/ticker/{ticker}/prev per previous close
     - Free tier: dati con delay ~15 min (US stocks)
  2. yfinance — fallback se Massive non disponibile o errore

Costo: $0 (entrambi gratuiti).
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Iterable

import aiohttp

logger = logging.getLogger(__name__)

MASSIVE_BASE_URL = "https://api.massive.com"
MASSIVE_TIMEOUT = 10  # secondi


def _get_massive_key() -> str:
    """Recupera la API key di Massive da env var o database settings."""
    key = os.environ.get("MASSIVE_API_KEY", "")
    if not key:
        try:
            import database as _db
            key = _db.get_setting("massive_api_key", "") or ""
        except Exception:
            pass
    return key

# Watchlist principale per polling continuo (max 25 ticker per non saturare yfinance)
DEFAULT_WATCHLIST_TICKERS = [
    "SPY", "QQQ", "EEM", "VEA",          # ETF broad
    "XOM", "CVX", "SHEL",                # Energy
    "LMT", "RTX", "NOC", "BA",           # Defense
    "GLD", "SLV", "USO", "UNG",          # Commodities
    "EWG", "EWI", "EWQ",                 # Europe
    "AAPL", "MSFT", "GOOGL", "NVDA",     # Tech mega caps
    "JPM", "TLT", "VIX",                 # Financials / bonds / vol
]


def _collect_polling_tickers() -> list[str]:
    """
    Raccoglie i ticker da polling-are:
    - Tutte le posizioni aperte (sempre)
    - Watchlist da settings (se presente)
    - Default watchlist (fallback)
    Limita a max 30 ticker per non saturare yfinance.
    """
    tickers: set[str] = set()

    try:
        import database
        positions = database.get_open_positions() or []
        for p in positions:
            t = (p.get("ticker") or "").upper().strip()
            if t:
                tickers.add(t)
    except Exception as e:
        logger.debug("Errore lettura posizioni: %s", e)

    try:
        import database
        wl_raw = database.get_setting("watchlist", "")
        if wl_raw:
            wl = json.loads(wl_raw)
            for sector_tickers in wl.values():
                for t in sector_tickers:
                    tickers.add(t.upper().strip())
    except Exception:
        pass

    if not tickers:
        tickers.update(DEFAULT_WATCHLIST_TICKERS)

    # Hard cap a 30 ticker
    return sorted(tickers)[:30]


async def _fetch_massive_one(session, ticker: str, api_key: str) -> dict | None:
    """
    Chiama Massive API per un singolo ticker:
    - /v2/aggs/ticker/{ticker}/range/1/minute/{from}/{to}?limit=1&sort=desc → ultima candela 1-min
    - /v2/aggs/ticker/{ticker}/prev → previous close
    Le 2 call sono lanciate in parallelo.
    """
    today = datetime.now(timezone.utc).date()
    # Range di 4 giorni indietro per gestire weekend/festività
    from_date = (today - timedelta(days=4)).isoformat()
    to_date = today.isoformat()

    url_current = f"{MASSIVE_BASE_URL}/v2/aggs/ticker/{ticker}/range/1/minute/{from_date}/{to_date}"
    params_current = {"adjusted": "true", "sort": "desc", "limit": 1, "apiKey": api_key}

    url_prev = f"{MASSIVE_BASE_URL}/v2/aggs/ticker/{ticker}/prev"
    params_prev = {"adjusted": "true", "apiKey": api_key}

    async def _get(url, params):
        try:
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=MASSIVE_TIMEOUT)) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
        except Exception:
            return None

    current_data, prev_data = await asyncio.gather(_get(url_current, params_current),
                                                    _get(url_prev, params_prev))

    if not current_data or not (current_data.get("results")):
        return None

    bar = current_data["results"][0]
    price = float(bar.get("c") or 0)
    if price <= 0:
        return None

    prev_close = price
    if prev_data and prev_data.get("results"):
        try:
            prev_close = float(prev_data["results"][0].get("c") or price)
        except Exception:
            pass

    change_pct = ((price - prev_close) / prev_close * 100) if prev_close > 0 else 0
    timestamp_ms = bar.get("t", 0)
    bar_ts = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat() if timestamp_ms else datetime.now(timezone.utc).isoformat()

    return {
        "price": round(price, 4),
        "prev_close": round(prev_close, 4),
        "change_pct": round(change_pct, 4),
        "volume": int(bar.get("v") or 0),
        "day_high": round(float(bar.get("h") or price), 4),
        "day_low": round(float(bar.get("l") or price), 4),
        "timestamp": bar_ts,
    }


async def _fetch_massive_quotes(tickers: list[str], api_key: str) -> dict[str, dict]:
    """Batch parallel fetch via Massive API. Ritorna {ticker: quote}."""
    quotes: dict[str, dict] = {}
    if not tickers or not api_key:
        return quotes

    # Concurrency cap a 10 per non saturare il free tier (~5 req/sec)
    sem = asyncio.Semaphore(10)

    async def _fetch_with_sem(session, t):
        async with sem:
            return t, await _fetch_massive_one(session, t, api_key)

    try:
        async with aiohttp.ClientSession() as session:
            results = await asyncio.gather(*[_fetch_with_sem(session, t) for t in tickers])
            for ticker, q in results:
                if q is not None:
                    quotes[ticker] = q
    except Exception as e:
        logger.warning("Massive batch error: %s", e)

    return quotes


def _fetch_yfinance_quotes(tickers: list[str]) -> dict[str, dict]:
    """
    Scarica gli ultimi prezzi 1-min via yfinance.
    Ritorna: {ticker: {price, prev_close, change_pct, volume, day_high, day_low}}
    """
    if not tickers:
        return {}

    quotes: dict[str, dict] = {}
    try:
        import yfinance as yf
        # Batch download (più efficiente di chiamate singole)
        data = yf.download(
            tickers,
            period="2d",          # Serve almeno 2 giorni per avere prev_close affidabile
            interval="1m",
            progress=False,
            auto_adjust=True,
            threads=True,
            group_by="ticker",
        )

        if data is None or data.empty:
            logger.warning("yfinance: dati vuoti per %d ticker", len(tickers))
            return {}

        for ticker in tickers:
            try:
                # Multi-ticker download → DataFrame con MultiIndex columns
                if len(tickers) > 1:
                    if ticker not in data.columns.get_level_values(0):
                        continue
                    tdf = data[ticker].dropna()
                else:
                    tdf = data.dropna()

                if tdf.empty or len(tdf) < 2:
                    continue

                last_row = tdf.iloc[-1]
                price = float(last_row["Close"])
                volume = int(last_row.get("Volume", 0) or 0)
                day_high = float(tdf["High"].max())
                day_low = float(tdf["Low"].min())

                # Prev close = chiusura del giorno precedente
                # Cerchiamo l'ultimo timestamp del giorno precedente
                last_ts = tdf.index[-1]
                prev_day_data = tdf[tdf.index.date < last_ts.date()]
                if not prev_day_data.empty:
                    prev_close = float(prev_day_data["Close"].iloc[-1])
                else:
                    prev_close = float(tdf["Close"].iloc[0])

                change_pct = ((price - prev_close) / prev_close * 100) if prev_close > 0 else 0

                quotes[ticker] = {
                    "price": round(price, 4),
                    "prev_close": round(prev_close, 4),
                    "change_pct": round(change_pct, 4),
                    "volume": volume,
                    "day_high": round(day_high, 4),
                    "day_low": round(day_low, 4),
                    "timestamp": last_ts.to_pydatetime().replace(tzinfo=timezone.utc).isoformat(),
                }
            except Exception as e:
                logger.debug("yfinance parse error %s: %s", ticker, e)
                continue

    except Exception as e:
        logger.warning("yfinance batch error: %s", e)

    return quotes


def _upsert_quotes(quotes: dict[str, dict], market_state: str = "REGULAR", source: str = "yfinance"):
    """Upsert dei prezzi correnti in price_quotes + insert in price_history."""
    if not quotes:
        return 0, 0

    written_quotes = 0
    written_history = 0

    try:
        import database
        client = database.get_client()
        if not client:
            return 0, 0

        now_iso = datetime.now(timezone.utc).isoformat()

        # 1. Upsert price_quotes (uno per ticker)
        rows_quotes = []
        for ticker, q in quotes.items():
            rows_quotes.append({
                "ticker": ticker,
                "price": q["price"],
                "prev_close": q["prev_close"],
                "change_pct": q["change_pct"],
                "volume": q["volume"],
                "day_high": q.get("day_high"),
                "day_low": q.get("day_low"),
                "market_state": market_state,
                "updated_at": now_iso,
                "source": source,
            })
        if rows_quotes:
            try:
                client.table("price_quotes").upsert(rows_quotes).execute()
                written_quotes = len(rows_quotes)
            except Exception as e:
                logger.warning("Errore upsert price_quotes: %s", e)

        # 2. Insert price_history (uno per ticker per minuto)
        rows_hist = []
        for ticker, q in quotes.items():
            rows_hist.append({
                "ticker": ticker,
                "timestamp": q.get("timestamp", now_iso),
                "price": q["price"],
                "volume": q["volume"],
            })
        if rows_hist:
            try:
                # ON CONFLICT (ticker, timestamp) DO NOTHING — idempotente
                client.table("price_history").upsert(
                    rows_hist,
                    on_conflict="ticker,timestamp",
                    ignore_duplicates=True,
                ).execute()
                written_history = len(rows_hist)
            except Exception as e:
                logger.debug("Errore insert price_history: %s", e)

    except Exception as e:
        logger.warning("Errore generale _upsert_quotes: %s", e)

    return written_quotes, written_history


async def update_price_cache() -> dict:
    """
    Job principale: chiama yfinance per tutti i ticker monitorati,
    aggiorna price_quotes e price_history su Supabase.

    Returns:
        {tickers, quotes_written, history_written, duration_seconds}
    """
    import time
    start = time.time()

    tickers = _collect_polling_tickers()
    if not tickers:
        return {"tickers": 0, "quotes_written": 0, "history_written": 0,
                "duration_seconds": round(time.time() - start, 2)}

    # Determina market state (semplificato: REGULAR se NYSE aperta)
    try:
        from scheduler import is_market_open
        market_state = "REGULAR" if is_market_open() else "CLOSED"
    except Exception:
        market_state = "UNKNOWN"

    # 1. Provider primario: Massive API (se key configurata)
    quotes = {}
    source_used = "yfinance"
    massive_key = _get_massive_key()
    if massive_key:
        try:
            quotes = await _fetch_massive_quotes(tickers, massive_key)
            if quotes:
                source_used = "massive"
        except Exception as e:
            logger.warning("Massive provider failed: %s — fallback yfinance", e)
            quotes = {}

    # 2. Fallback yfinance se Massive vuoto/non disponibile
    if not quotes:
        quotes = await asyncio.to_thread(_fetch_yfinance_quotes, tickers)
        source_used = "yfinance"

    # 3. Upsert in Supabase (sincrono → thread)
    qw, hw = await asyncio.to_thread(_upsert_quotes, quotes, market_state, source_used)

    duration = round(time.time() - start, 2)
    logger.info("Price polling [%s]: %d tickers richiesti, %d quotes salvate, %d storia (%.1fs)",
                source_used, len(tickers), qw, hw, duration)

    return {
        "tickers": len(tickers),
        "quotes_written": qw,
        "history_written": hw,
        "source": source_used,
        "duration_seconds": duration,
        "market_state": market_state,
    }


def get_cached_price(ticker: str, max_age_seconds: int = 120) -> dict | None:
    """
    Helper sincrono per leggere il prezzo dalla cache.
    Ritorna None se il prezzo è troppo vecchio o assente.

    Args:
        ticker: simbolo (case-insensitive)
        max_age_seconds: età massima accettata (default 120s)
    """
    try:
        import database
        client = database.get_client()
        if not client:
            return None

        result = client.table("price_quotes") \
            .select("*") \
            .eq("ticker", ticker.upper()) \
            .limit(1) \
            .execute()

        if not result.data:
            return None

        row = result.data[0]
        updated_at = datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - updated_at).total_seconds()

        if age > max_age_seconds:
            return None

        return {
            "ticker": row["ticker"],
            "price": float(row["price"]),
            "prev_close": float(row.get("prev_close") or 0),
            "change_pct": float(row.get("change_pct") or 0),
            "volume": int(row.get("volume") or 0),
            "age_seconds": round(age, 1),
        }
    except Exception:
        return None


def get_cached_prices_bulk(tickers: list[str], max_age_seconds: int = 120) -> dict[str, dict]:
    """
    Versione bulk: ritorna dict {ticker: quote} solo per i ticker freschi.
    """
    result = {}
    if not tickers:
        return result
    try:
        import database
        client = database.get_client()
        if not client:
            return result

        upper_tickers = [t.upper() for t in tickers]
        rows = client.table("price_quotes") \
            .select("*") \
            .in_("ticker", upper_tickers) \
            .execute()

        if not rows.data:
            return result

        now = datetime.now(timezone.utc)
        for row in rows.data:
            try:
                updated_at = datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00"))
                age = (now - updated_at).total_seconds()
                if age <= max_age_seconds:
                    result[row["ticker"]] = {
                        "price": float(row["price"]),
                        "prev_close": float(row.get("prev_close") or 0),
                        "change_pct": float(row.get("change_pct") or 0),
                        "age_seconds": round(age, 1),
                    }
            except Exception:
                continue
    except Exception:
        pass
    return result
