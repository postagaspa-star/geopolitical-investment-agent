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
        positions = database.get_positions() or []
        for p in positions:
            t = (p.get("ticker") or "").upper().strip()
            if t:
                tickers.add(t)
    except Exception as e:
        logger.warning("Errore lettura posizioni per polling: %s", e)

    custom_watchlist_loaded = False
    try:
        import database
        wl_raw = database.get_setting("watchlist", "")
        if wl_raw:
            wl = json.loads(wl_raw)
            for sector_tickers in wl.values():
                for t in sector_tickers:
                    tickers.add(t.upper().strip())
            custom_watchlist_loaded = bool(wl)
    except Exception:
        pass

    # Se non c'e' watchlist personalizzata, integra con la default
    # (sempre, anche se ci sono posizioni — vogliamo monitorare anche i ticker chiave)
    if not custom_watchlist_loaded:
        tickers.update(DEFAULT_WATCHLIST_TICKERS)

    # Hard cap a 30 ticker
    return sorted(tickers)[:30]


# Cache giornaliera per prev_close (evita di chiamare l'endpoint ogni 60s)
_prev_close_cache: dict[str, tuple[str, float]] = {}  # {ticker: (date_str, prev_close)}


async def _fetch_massive_prev_close(session, ticker: str, api_key: str) -> float | None:
    """Ritorna prev_close per il ticker, con cache giornaliera."""
    today_str = datetime.now(timezone.utc).date().isoformat()
    cached = _prev_close_cache.get(ticker)
    if cached and cached[0] == today_str:
        return cached[1]

    url = f"{MASSIVE_BASE_URL}/v2/aggs/ticker/{ticker}/prev"
    params = {"adjusted": "true", "apiKey": api_key}
    try:
        async with session.get(url, params=params,
                               timeout=aiohttp.ClientTimeout(total=MASSIVE_TIMEOUT)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            results = data.get("results") or []
            if results:
                pc = float(results[0].get("c") or 0)
                if pc > 0:
                    _prev_close_cache[ticker] = (today_str, pc)
                    return pc
    except Exception:
        pass
    return None


async def _fetch_massive_current(session, ticker: str, api_key: str,
                                 retries: int = 2) -> dict | None:
    """Ultimo close 1-min via Massive aggregates. Retry automatico su 429."""
    today = datetime.now(timezone.utc).date()
    from_date = (today - timedelta(days=4)).isoformat()
    to_date = today.isoformat()
    url = f"{MASSIVE_BASE_URL}/v2/aggs/ticker/{ticker}/range/1/minute/{from_date}/{to_date}"
    params = {"adjusted": "true", "sort": "desc", "limit": 1, "apiKey": api_key}

    for attempt in range(retries + 1):
        try:
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=MASSIVE_TIMEOUT)) as resp:
                if resp.status == 429:
                    # Rate limit: backoff esponenziale
                    await asyncio.sleep(0.5 * (2 ** attempt))
                    continue
                if resp.status != 200:
                    return None
                data = await resp.json()
                results = data.get("results") or []
                if not results:
                    return None
                bar = results[0]
                price = float(bar.get("c") or 0)
                if price <= 0:
                    return None
                ts_ms = bar.get("t", 0)
                bar_ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat() \
                         if ts_ms else datetime.now(timezone.utc).isoformat()
                return {
                    "price": round(price, 4),
                    "volume": int(bar.get("v") or 0),
                    "day_high": round(float(bar.get("h") or price), 4),
                    "day_low": round(float(bar.get("l") or price), 4),
                    "timestamp": bar_ts,
                }
        except Exception:
            if attempt < retries:
                await asyncio.sleep(0.5 * (2 ** attempt))
                continue
            return None
    return None


async def _fetch_massive_one(session, ticker: str, api_key: str) -> dict | None:
    """Combina current price + prev_close (con cache) in un singolo dict ticker."""
    current = await _fetch_massive_current(session, ticker, api_key)
    if not current:
        return None

    prev_close = await _fetch_massive_prev_close(session, ticker, api_key)
    if prev_close is None:
        prev_close = current["price"]

    change_pct = ((current["price"] - prev_close) / prev_close * 100) if prev_close > 0 else 0
    return {
        "price": current["price"],
        "prev_close": round(prev_close, 4),
        "change_pct": round(change_pct, 4),
        "volume": current["volume"],
        "day_high": current["day_high"],
        "day_low": current["day_low"],
        "timestamp": current["timestamp"],
    }


async def _fetch_massive_quotes(tickers: list[str], api_key: str) -> dict[str, dict]:
    """
    Batch fetch via Massive API con concurrency limitata e logging diagnostico.
    Free tier: ~5 req/sec → cap concurrency a 4 e dilato richieste con piccoli delay.
    """
    quotes: dict[str, dict] = {}
    if not tickers or not api_key:
        return quotes

    sem = asyncio.Semaphore(4)
    failed_tickers: list[str] = []

    async def _fetch_with_sem(session, t):
        async with sem:
            result = await _fetch_massive_one(session, t, api_key)
            if result is None:
                failed_tickers.append(t)
            return t, result

    try:
        async with aiohttp.ClientSession() as session:
            results = await asyncio.gather(*[_fetch_with_sem(session, t) for t in tickers])
            for ticker, q in results:
                if q is not None:
                    quotes[ticker] = q
    except Exception as e:
        logger.warning("Massive batch error: %s", e)

    if failed_tickers:
        logger.info("Massive: %d/%d ticker falliti (es. %s)",
                    len(failed_tickers), len(tickers), ", ".join(failed_tickers[:5]))

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
        # NB: yfinance ora richiede curl_cffi internamente — niente session=requests
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
    massive_quotes = {}
    massive_key = _get_massive_key()
    if massive_key:
        try:
            massive_quotes = await _fetch_massive_quotes(tickers, massive_key)
        except Exception as e:
            logger.warning("Massive provider failed: %s", e)

    # 2. Per i ticker mancanti, completa con yfinance
    missing_tickers = [t for t in tickers if t not in massive_quotes]
    yf_quotes = {}
    if missing_tickers:
        yf_quotes = await asyncio.to_thread(_fetch_yfinance_quotes, missing_tickers)

    # 3. Combina e salva con source corretta per ogni ticker
    qw_total, hw_total = 0, 0
    if massive_quotes:
        qw, hw = await asyncio.to_thread(_upsert_quotes, massive_quotes, market_state, "massive")
        qw_total += qw; hw_total += hw
    if yf_quotes:
        qw, hw = await asyncio.to_thread(_upsert_quotes, yf_quotes, market_state, "yfinance")
        qw_total += qw; hw_total += hw

    # 4. Aggiorna current_price + unrealized_pnl di ogni posizione aperta
    #    e salva uno snapshot del portfolio (per popolare l'equity curve).
    #    Solo durante market hours per non gonfiare il DB con snapshot stagnanti.
    all_quotes = {**yf_quotes, **massive_quotes}  # massive ha la precedenza
    if market_state == "REGULAR":
        positions_updated, snapshot_saved = await asyncio.to_thread(
            _update_positions_and_snapshot, all_quotes
        )
    else:
        positions_updated, snapshot_saved = 0, False

    if massive_quotes and yf_quotes:
        source_used = f"massive+yfinance"
    elif massive_quotes:
        source_used = "massive"
    elif yf_quotes:
        source_used = "yfinance"
    else:
        source_used = "none"

    duration = round(time.time() - start, 2)
    logger.info(
        "Price polling [%s]: %d ticker richiesti, %d quotes salvate (massive=%d, yf=%d), "
        "%d storia, %d posizioni aggiornate, snapshot=%s (%.1fs)",
        source_used, len(tickers), qw_total, len(massive_quotes), len(yf_quotes),
        hw_total, positions_updated, snapshot_saved, duration,
    )

    return {
        "tickers": len(tickers),
        "quotes_written": qw_total,
        "history_written": hw_total,
        "positions_updated": positions_updated,
        "snapshot_saved": snapshot_saved,
        "source": source_used,
        "massive_count": len(massive_quotes),
        "yfinance_count": len(yf_quotes),
        "duration_seconds": duration,
        "market_state": market_state,
    }


def _update_positions_and_snapshot(all_quotes: dict[str, dict]) -> tuple[int, bool]:
    """
    Per ogni posizione aperta aggiorna current_price + unrealized_pnl
    usando il prezzo dalla cache. Poi salva uno snapshot del portfolio totale.
    Ritorna (n_posizioni_aggiornate, snapshot_salvato).
    """
    positions_updated = 0
    snapshot_saved = False

    try:
        import database
        positions = database.get_positions() or []
    except Exception as e:
        logger.warning("Errore lettura positions: %s", e)
        return 0, False

    if not positions:
        # Nessuna posizione aperta — comunque salva uno snapshot del portfolio
        try:
            import database
            portfolio = database.get_portfolio()
            if portfolio:
                database.insert_portfolio_snapshot(
                    portfolio.get("total_value", 0),
                    portfolio.get("cash_balance", portfolio.get("cash", 0)),
                )
                snapshot_saved = True
        except Exception as e:
            logger.debug("Errore snapshot portfolio (no positions): %s", e)
        return 0, snapshot_saved

    # 1. Update current_price + pnl per ogni ticker con quote disponibile
    total_position_value = 0.0
    for p in positions:
        ticker = p.get("ticker")
        qty = float(p.get("quantity", 0) or 0)
        avg = float(p.get("avg_buy_price", 0) or 0)
        if not ticker or qty <= 0:
            continue
        quote = all_quotes.get(ticker)
        if quote and "price" in quote:
            current_price = float(quote["price"])
            try:
                import database
                database.update_position_price(ticker, current_price)
                positions_updated += 1
            except Exception as e:
                logger.debug("Errore update %s: %s", ticker, e)
            total_position_value += current_price * qty
        else:
            # Quote non disponibile per questo ticker: usa l'ultimo current_price noto
            cp = float(p.get("current_price") or avg)
            total_position_value += cp * qty

    # 2. Salva snapshot del portfolio totale (cash + valore posizioni)
    try:
        import database
        portfolio = database.get_portfolio()
        if portfolio:
            cash = float(portfolio.get("cash_balance", portfolio.get("cash", 0)) or 0)
            total_value = cash + total_position_value
            database.insert_portfolio_snapshot(round(total_value, 2), round(cash, 2))
            snapshot_saved = True
    except Exception as e:
        logger.debug("Errore insert portfolio_snapshot: %s", e)

    return positions_updated, snapshot_saved


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
