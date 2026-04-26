"""
Price Polling Service — yfinance → Supabase ogni 60 secondi.

Aggiorna la tabella `price_quotes` con i prezzi correnti delle posizioni aperte
e dei ticker della watchlist. Mantiene anche uno storico minuto-per-minuto
in `price_history` per analisi e dashboard.

Costo: $0 (yfinance gratis).
Limite: dati USA con delay 15-20 min (limite di yfinance, non aggirabile gratis).
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Iterable

logger = logging.getLogger(__name__)

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


def _upsert_quotes(quotes: dict[str, dict], market_state: str = "REGULAR"):
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
                "source": "yfinance",
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

    # yfinance è sincrono — esegui in thread pool per non bloccare event loop
    quotes = await asyncio.to_thread(_fetch_yfinance_quotes, tickers)

    # Upsert in Supabase (anche questo sincrono → thread)
    qw, hw = await asyncio.to_thread(_upsert_quotes, quotes, market_state)

    duration = round(time.time() - start, 2)
    logger.info("Price polling: %d tickers, %d quotes salvate, %d storia (%.1fs)",
                len(tickers), qw, hw, duration)

    return {
        "tickers": len(tickers),
        "quotes_written": qw,
        "history_written": hw,
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
