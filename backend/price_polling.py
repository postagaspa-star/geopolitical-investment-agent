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
POLYGON_BASE_URL = "https://api.polygon.io"
PROVIDER_TIMEOUT = 10  # secondi (uguale per Polygon e Massive)
MASSIVE_TIMEOUT = PROVIDER_TIMEOUT  # back-compat


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


def _get_polygon_key() -> str:
    """Recupera la API key di Polygon da env var o database settings."""
    key = os.environ.get("POLYGON_API_KEY", "")
    if not key:
        try:
            import database as _db
            key = _db.get_setting("polygon_api_key", "") or ""
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


# Cache giornaliera per prev_close (evita di chiamare l'endpoint ogni 60s).
# Chiave: f"{provider}:{ticker}" — separa Polygon e Massive (potrebbero divergere)
_prev_close_cache: dict[str, tuple[str, float]] = {}


async def _fetch_provider_prev_close(session, base_url: str, ticker: str,
                                     api_key: str, provider: str = "massive") -> float | None:
    """
    Ritorna prev_close via API Polygon-compatible (funziona per Polygon e Massive,
    stessa API). Con cache giornaliera per ridurre chiamate.
    """
    today_str = datetime.now(timezone.utc).date().isoformat()
    cache_key = f"{provider}:{ticker}"
    cached = _prev_close_cache.get(cache_key)
    if cached and cached[0] == today_str:
        return cached[1]

    url = f"{base_url}/v2/aggs/ticker/{ticker}/prev"
    params = {"adjusted": "true", "apiKey": api_key}
    try:
        async with session.get(url, params=params,
                               timeout=aiohttp.ClientTimeout(total=PROVIDER_TIMEOUT)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            results = data.get("results") or []
            if results:
                pc = float(results[0].get("c") or 0)
                if pc > 0:
                    _prev_close_cache[cache_key] = (today_str, pc)
                    return pc
    except Exception:
        pass
    return None


async def _fetch_provider_current(session, base_url: str, ticker: str, api_key: str,
                                  retries: int = 2) -> dict | None:
    """
    Ultimo close 1-min via API Polygon-compatible. Retry automatico su 429.
    Funziona per Polygon e Massive (stessa API).
    """
    today = datetime.now(timezone.utc).date()
    from_date = (today - timedelta(days=4)).isoformat()
    to_date = today.isoformat()
    url = f"{base_url}/v2/aggs/ticker/{ticker}/range/1/minute/{from_date}/{to_date}"
    params = {"adjusted": "true", "sort": "desc", "limit": 1, "apiKey": api_key}

    for attempt in range(retries + 1):
        try:
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=PROVIDER_TIMEOUT)) as resp:
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


# ── Wrapper Massive (back-compat) ─────────────────────────────────────────────
async def _fetch_massive_prev_close(session, ticker: str, api_key: str) -> float | None:
    return await _fetch_provider_prev_close(session, MASSIVE_BASE_URL, ticker, api_key, "massive")


async def _fetch_massive_current(session, ticker: str, api_key: str,
                                 retries: int = 2) -> dict | None:
    return await _fetch_provider_current(session, MASSIVE_BASE_URL, ticker, api_key, retries)


async def _fetch_provider_one(session, base_url: str, ticker: str, api_key: str,
                              provider: str = "massive") -> dict | None:
    """Combina current price + prev_close (con cache) in un singolo dict ticker."""
    current = await _fetch_provider_current(session, base_url, ticker, api_key)
    if not current:
        return None

    prev_close = await _fetch_provider_prev_close(session, base_url, ticker, api_key, provider)
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


async def _fetch_provider_quotes(tickers: list[str], api_key: str, base_url: str,
                                 provider: str = "massive",
                                 max_concurrency: int = 4) -> dict[str, dict]:
    """
    Batch fetch via API Polygon-compatible (Polygon o Massive).
    Concurrency cap per non saturare il rate limit del free tier.

    Args:
        tickers: lista ticker da fetchare
        api_key: API key del provider
        base_url: base URL (es. POLYGON_BASE_URL o MASSIVE_BASE_URL)
        provider: nome label per logging e cache key (es. "polygon", "massive")
        max_concurrency: max chiamate parallele
    """
    quotes: dict[str, dict] = {}
    if not tickers or not api_key:
        return quotes

    sem = asyncio.Semaphore(max_concurrency)
    failed_tickers: list[str] = []

    async def _fetch_with_sem(session, t):
        async with sem:
            result = await _fetch_provider_one(session, base_url, t, api_key, provider)
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
        logger.warning("%s batch error: %s", provider, e)

    if failed_tickers:
        logger.info("%s: %d/%d ticker falliti (es. %s)",
                    provider, len(failed_tickers), len(tickers),
                    ", ".join(failed_tickers[:5]))

    return quotes


# ── Wrapper Polygon (primario) e Massive (fallback) ───────────────────────────
async def _fetch_polygon_quotes(tickers: list[str], api_key: str) -> dict[str, dict]:
    """Polygon.io free tier: ~5 req/sec — concurrency 4."""
    return await _fetch_provider_quotes(tickers, api_key, POLYGON_BASE_URL,
                                        provider="polygon", max_concurrency=4)


async def _fetch_massive_quotes(tickers: list[str], api_key: str) -> dict[str, dict]:
    """Massive.com (Polygon-compatible) — fallback se Polygon non risponde."""
    return await _fetch_provider_quotes(tickers, api_key, MASSIVE_BASE_URL,
                                        provider="massive", max_concurrency=4)


# ── Back-compat alias (codice esterno potrebbe importarlo) ───────────────────
async def _fetch_massive_one(session, ticker: str, api_key: str) -> dict | None:
    return await _fetch_provider_one(session, MASSIVE_BASE_URL, ticker, api_key, "massive")


def _detect_yfinance_corruption(quotes: dict[str, dict]) -> tuple[bool, str]:
    """
    Detect yFinance batch corruption: quando il batch download è rate-limited,
    yFinance restituisce silenziosamente lo stesso bar cached per tutti i ticker,
    producendo prezzi identici (bug documentato in yfinance#2022).

    Soglia: 3+ ticker con prezzo identico (arrotondato a 2 dp), OPPURE
    ≥50% dei ticker se il batch è ≥4 elementi.

    Returns:
        (is_corrupted, description_string)
    """
    if len(quotes) < 3:
        return False, ""

    from collections import Counter
    price_counter = Counter(
        round(q.get("price", 0), 2) for q in quotes.values() if q.get("price", 0) > 0
    )
    if not price_counter:
        return False, ""

    most_common_price, most_common_count = price_counter.most_common(1)[0]
    total = len(quotes)

    is_corrupted = most_common_count >= 3 or (
        total >= 4 and most_common_count >= max(2, total // 2)
    )
    if is_corrupted:
        corrupted_tickers = [
            t for t, q in quotes.items()
            if round(q.get("price", 0), 2) == most_common_price
        ]
        desc = (
            f"{most_common_count}/{total} ticker con prezzo identico "
            f"${most_common_price} ({', '.join(corrupted_tickers[:6])})"
        )
        return True, desc
    return False, ""


def _fetch_yfinance_per_ticker_fallback(tickers: list[str]) -> dict[str, dict]:
    """
    Fallback per-ticker: chiama yf.Ticker(t).fast_info uno alla volta
    quando il batch download fallisce. Ritorna best-effort: ticker che
    rispondono singolarmente vengono salvati, gli altri saltati.
    """
    quotes: dict[str, dict] = {}
    try:
        import yfinance as yf
        for t in tickers:
            try:
                ticker_obj = yf.Ticker(t)
                fi = ticker_obj.fast_info
                price = float(fi.get("last_price") or fi.get("lastPrice") or 0)
                prev = float(fi.get("previous_close") or fi.get("previousClose") or price)
                if price <= 0:
                    continue
                change_pct = ((price - prev) / prev * 100) if prev > 0 else 0
                quotes[t] = {
                    "price": round(price, 4),
                    "prev_close": round(prev, 4),
                    "change_pct": round(change_pct, 4),
                    "volume": int(fi.get("last_volume", 0) or 0),
                    "day_high": round(float(fi.get("day_high", price) or price), 4),
                    "day_low": round(float(fi.get("day_low", price) or price), 4),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            except Exception as e:
                logger.debug("yfinance per-ticker fallback fail %s: %s", t, e)
                continue
        if quotes:
            logger.info("yfinance fallback per-ticker: recuperati %d/%d ticker", len(quotes), len(tickers))
    except Exception as e:
        logger.warning("yfinance fallback critico: %s", e)
    return quotes


def _fetch_yfinance_quotes(tickers: list[str]) -> dict[str, dict]:
    """
    Scarica gli ultimi prezzi 1-min via yfinance.
    Ritorna: {ticker: {price, prev_close, change_pct, volume, day_high, day_low}}

    Robustezza: retry con backoff su errori transitori (429, network, timeouts).
    Tre tentativi totali con pause 2s/4s.
    """
    if not tickers:
        return {}

    quotes: dict[str, dict] = {}
    import time as _t
    try:
        import yfinance as yf
        # NB: yfinance ora richiede curl_cffi internamente — niente session=requests

        # Retry con backoff: yfinance free è frequentemente rate-limitato (429)
        # o ritorna body vuoto sotto carico. Riproviamo fino a 3 volte.
        # Query LIGHT: period=5d, interval=5m → ~390 bars/ticker invece di
        # ~780 con interval=1m. Riduce drasticamente il payload e i 429.
        # 5m granularity è più che sufficiente per prezzo corrente + prev_close.
        data = None
        last_error = None
        for attempt in range(3):
            try:
                data = yf.download(
                    tickers,
                    period="5d",          # Serve >= 2 giorni per prev_close affidabile (5d copre weekend)
                    interval="5m",        # 5-min bars: light, abbastanza fresco, meno rate-limit
                    progress=False,
                    auto_adjust=True,
                    threads=True,
                    group_by="ticker",
                )
                if data is not None and not data.empty:
                    break  # successo
                last_error = "empty_response"
            except Exception as ex:
                last_error = str(ex)
                logger.warning("yfinance attempt %d/3 fallito: %s", attempt + 1, str(ex)[:200])
            if attempt < 2:
                wait = 2 * (attempt + 1)
                _t.sleep(wait)

        if data is None or data.empty:
            logger.warning("yfinance: %d ticker, tutti i 3 tentativi falliti (last=%s) — "
                           "tento fallback per-ticker singolo",
                           len(tickers), last_error)
            # Fallback per-ticker: alcuni ticker potrebbero rispondere singolarmente
            # anche se il batch fallisce. Costoso ma più resiliente.
            return _fetch_yfinance_per_ticker_fallback(tickers)

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

    # ── Corruption guard ──────────────────────────────────────────────────────
    # Se yfinance ha restituito lo stesso prezzo per N≥3 ticker diversi, il
    # batch è corrotto (rate-limit silenzioso → tutti i ticker ricevono lo
    # stesso bar cached). Scartiamo i risultati e ritentiamo per-ticker.
    if quotes:
        corrupted, corruption_desc = _detect_yfinance_corruption(quotes)
        if corrupted:
            logger.warning(
                "⚠ yfinance BATCH CORROTTO — %s. "
                "Scarto risultati e retry per-ticker singolo.", corruption_desc
            )
            # Invalida cache interna yfinance (best-effort: API non pubblica)
            try:
                import yfinance as _yf
                if hasattr(_yf, "shared") and hasattr(_yf.shared, "_DFS"):
                    _yf.shared._DFS.clear()
            except Exception:
                pass
            # Per-ticker usa fast_info: path diverso, non soffre del batch bug
            fallback_quotes = _fetch_yfinance_per_ticker_fallback(tickers)
            # Secondo controllo: se anche il fallback ritorna corruzione, scarta tutto
            if fallback_quotes:
                still_corrupted, _ = _detect_yfinance_corruption(fallback_quotes)
                if still_corrupted:
                    logger.error(
                        "yfinance per-ticker ancora corrotto — feed non affidabile. "
                        "Ritorno dict vuoto (il Decision Agent userà data_quality=feed_corrupted)."
                    )
                    return {}
            return fallback_quotes
    # ─────────────────────────────────────────────────────────────────────────

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

    # ── Cascade: Polygon (primario) → Massive (fallback) → yFinance (ultima spiaggia)
    # FIX CRITICO: corruption detection ora applicata a TUTTI i provider, non
    # solo yfinance. Bug precedente: se Polygon free tier sotto carico
    # restituiva lo stesso prezzo cached per N ticker (corruzione silenziosa
    # documentata), il bot lo accettava come golden source senza degradare.
    # 1. Provider primario: Polygon.io (se key configurata)
    polygon_quotes = {}
    polygon_key = _get_polygon_key()
    if polygon_key:
        try:
            polygon_quotes = await _fetch_polygon_quotes(tickers, polygon_key)
            # Apply corruption guard
            corrupted, msg = _detect_yfinance_corruption(polygon_quotes)
            if corrupted:
                logger.error("[POLLING] POLYGON corruzione rilevata, scartato: %s", msg)
                polygon_quotes = {}
        except Exception as e:
            logger.warning("Polygon provider failed: %s", e)

    # 2. Provider secondario: Massive (per ticker mancanti da Polygon)
    missing_after_polygon = [t for t in tickers if t not in polygon_quotes]
    massive_quotes = {}
    if missing_after_polygon:
        massive_key = _get_massive_key()
        if massive_key:
            try:
                massive_quotes = await _fetch_massive_quotes(missing_after_polygon, massive_key)
                corrupted, msg = _detect_yfinance_corruption(massive_quotes)
                if corrupted:
                    logger.error("[POLLING] MASSIVE corruzione rilevata, scartato: %s", msg)
                    massive_quotes = {}
            except Exception as e:
                logger.warning("Massive provider failed: %s", e)

    # 3. Ultima spiaggia: yfinance (per ticker ancora mancanti)
    #    yfinance ha il corruption detector integrato, quindi se restituisce
    #    dati corrotti (stesso prezzo per N ticker) li scarta automaticamente.
    missing_after_massive = [
        t for t in tickers
        if t not in polygon_quotes and t not in massive_quotes
    ]
    yf_quotes = {}
    if missing_after_massive:
        yf_quotes = await asyncio.to_thread(_fetch_yfinance_quotes, missing_after_massive)

    # 4. Combina e salva con source corretta per ogni ticker
    qw_total, hw_total = 0, 0
    if polygon_quotes:
        qw, hw = await asyncio.to_thread(_upsert_quotes, polygon_quotes, market_state, "polygon")
        qw_total += qw; hw_total += hw
    if massive_quotes:
        qw, hw = await asyncio.to_thread(_upsert_quotes, massive_quotes, market_state, "massive")
        qw_total += qw; hw_total += hw
    if yf_quotes:
        qw, hw = await asyncio.to_thread(_upsert_quotes, yf_quotes, market_state, "yfinance")
        qw_total += qw; hw_total += hw

    # 5. Aggiorna current_price + unrealized_pnl di ogni posizione aperta
    #    e salva uno snapshot del portfolio (per popolare l'equity curve).
    #    SEMPRE — anche fuori orario di mercato:
    #      - le crypto (BTC-USD, ETH-USD, ...) si muovono 24/7
    #      - per le equity i prezzi restano fermi al last close, ma è
    #        comunque corretto aggiornare current_price = last close
    #        e mantenere snapshot continui per l'equity curve.
    # Precedenza in caso di sovrapposizioni: Polygon > Massive > yfinance
    all_quotes = {**yf_quotes, **massive_quotes, **polygon_quotes}
    positions_updated, snapshot_saved = await asyncio.to_thread(
        _update_positions_and_snapshot, all_quotes
    )

    sources_active = [
        s for s, q in [("polygon", polygon_quotes),
                       ("massive", massive_quotes),
                       ("yfinance", yf_quotes)] if q
    ]
    source_used = "+".join(sources_active) if sources_active else "none"

    duration = round(time.time() - start, 2)
    logger.info(
        "Price polling [%s]: %d ticker richiesti, %d quotes salvate "
        "(polygon=%d, massive=%d, yf=%d), %d storia, %d posizioni aggiornate, "
        "snapshot=%s (%.1fs)",
        source_used, len(tickers), qw_total, len(polygon_quotes),
        len(massive_quotes), len(yf_quotes),
        hw_total, positions_updated, snapshot_saved, duration,
    )

    return {
        "tickers": len(tickers),
        "quotes_written": qw_total,
        "history_written": hw_total,
        "positions_updated": positions_updated,
        "snapshot_saved": snapshot_saved,
        "source": source_used,
        "polygon_count": len(polygon_quotes),
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

    # 1. Update current_price + pnl per ogni ticker con quote disponibile.
    # Sanity check: rifiuta prezzi che si discostano >40% dal previous_close
    # (probabile bad data: pre-market spike, fat-finger, ticker mismatch).
    total_position_value = 0.0
    for p in positions:
        ticker = p.get("ticker")
        qty = float(p.get("quantity", 0) or 0)
        avg = float(p.get("avg_buy_price", 0) or 0)
        old_current = float(p.get("current_price") or avg)
        if not ticker or qty <= 0:
            continue
        quote = all_quotes.get(ticker)

        # Sanity check sul prezzo del quote PRIMA di usarlo
        if quote and "price" in quote:
            new_price = float(quote.get("price") or 0)
            prev_close = float(quote.get("prev_close") or 0)
            # Reference per il sanity check: prev_close dal quote ESCLUSIVAMENTE.
            # BUG FIXATO: prima fallback a old_current creava un loop — il
            # prezzo veniva confrontato contro se stesso, sempre validato
            # anche se stale. Ora se prev_close manca, accetta solo se la
            # variazione vs old_current e' < 10% (sanity light); oltre il
            # 10% richiediamo prev_close del quote source.
            if new_price <= 0:
                logger.warning("[POLLING] Prezzo NULLO/NEGATIVO per %s: %.4f, scartato",
                               ticker, new_price)
                quote = None
            elif prev_close > 0:
                ratio = new_price / prev_close
                if ratio < 0.6 or ratio > 1.4:
                    logger.warning(
                        "[POLLING] Prezzo IMPLAUSIBILE per %s: nuovo=%.2f vs prev_close=%.2f "
                        "(ratio %.2f). RIFIUTATO. Source=%s",
                        ticker, new_price, prev_close, ratio, quote.get("source", "?"),
                    )
                    quote = None
            elif old_current > 0:
                # Nessun prev_close → check soft: se varia < 10% accetta;
                # se >10% rifiuta (potrebbe essere fat-finger)
                ratio = new_price / old_current
                if ratio < 0.9 or ratio > 1.1:
                    logger.warning(
                        "[POLLING] Prezzo SUSPECT per %s: nuovo=%.2f vs old_current=%.2f "
                        "(ratio %.2f, no prev_close). RIFIUTATO per cautela.",
                        ticker, new_price, old_current, ratio,
                    )
                    quote = None
        if quote and "price" in quote:
            current_price = float(quote["price"])
            try:
                import database
                database.update_position_price(ticker, current_price)
                positions_updated += 1
                # Log esplicito quando il prezzo cambia significativamente — utile diagnostica
                if old_current > 0:
                    delta_pct = ((current_price - old_current) / old_current) * 100
                    if abs(delta_pct) > 5:
                        logger.info("[POLLING] %s: %.2f → %.2f (%+.1f%%)",
                                    ticker, old_current, current_price, delta_pct)
            except Exception as e:
                # Era debug, ora warning: vogliamo VEDERE i fallimenti DB
                logger.warning("[POLLING] Errore update_position_price %s: %s", ticker, e)
            total_position_value += current_price * qty
        else:
            # Quote non disponibile per questo ticker: usa l'ultimo current_price noto.
            # Logghiamo se è > 24h che non si aggiorna (potenziale stale data).
            cp = float(p.get("current_price") or avg)
            total_position_value += cp * qty
            logger.debug("[POLLING] %s: nessun quote, mantengo current_price=%.2f", ticker, cp)

    # 2. AUTO-EXIT TRIGGER (CRITICAL FIX): controlla SL/TP e chiude le
    # posizioni che hanno toccato i livelli. Bug precedente:
    # `check_and_execute_auto_exits` non era mai chiamato → tutti gli SL/TP
    # impostati dal Decision Agent erano DEAD letters (mai eseguiti).
    # L'utente pensava di essere protetto e in realtà non lo era.
    try:
        import portfolio as _portfolio
        # Costruisce dict {ticker: price} dagli aggiornamenti appena fatti
        prices_for_exit: dict[str, float] = {}
        for ticker, q in (all_quotes or {}).items():
            try:
                px = float(q.get("price") or 0)
                if px > 0:
                    prices_for_exit[ticker] = px
            except Exception:
                continue
        if prices_for_exit:
            executed = _portfolio.check_and_execute_auto_exits(prices_for_exit)
            if executed:
                triggered = [e for e in executed if e.get("trigger")]
                if triggered:
                    logger.info("[POLLING] AUTO-EXIT triggherati: %d (%s)",
                                len(triggered),
                                ", ".join(f"{e['ticker']}={e['trigger']}"
                                          for e in triggered[:5]))
    except Exception as e:
        logger.warning("[POLLING] check_and_execute_auto_exits failed: %s", e)

    # 3. Salva snapshot del portfolio totale (cash + valore posizioni)
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
