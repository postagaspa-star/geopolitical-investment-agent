"""
clawstreet_universe.py — Sorgente di verità sui ticker tradabili da ClawStreet.

ClawStreet supporta ~498 simboli (≈484 azioni S&P 500 + 14 crypto + 3 commodity ETF).
Endpoint ufficiale: GET https://www.clawstreet.io/api/data/symbols
                    (richiede header Authorization: Bearer <api_key>)

Questo modulo:
  - Carica e mette in cache l'elenco simboli (TTL 24h)
  - Fornisce `is_supported(ticker)` per pre-validare i trade prima di colpire ClawStreet
  - Fornisce `to_clawstreet_format(ticker)` per centralizzare la conversione
    yfinance "BTC-USD" → ClawStreet "X:BTCUSD"
  - Ha un fallback hard-coded usato se l'API non risponde (universo congelato al
    01-05-2026), così il bot non si blocca mai.

Uso tipico (in decision.py prima di execute_buy):
    from clawstreet_universe import is_supported, format_for_clawstreet
    if action == "BUY" and not is_supported(ticker):
        return {"error": f"{ticker} non è tradabile su ClawStreet."}
"""

from __future__ import annotations

import json as _json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

CLAWSTREET_SYMBOLS_URL = "https://www.clawstreet.io/api/data/symbols"
CACHE_TTL_SECONDS = 24 * 3600   # 24h: l'universo cambia raramente

# ── Fallback hard-coded (snapshot 2026-05-01) ───────────────────────────────
# Usato SOLO se l'API non risponde. Lista normalizzata in formato ClawStreet:
#   - azioni: ticker maiuscolo nudo (es. "AAPL", "BRK.B")
#   - crypto: prefisso "X:" e suffisso "USD" senza trattino (es. "X:BTCUSD")
_FALLBACK_CRYPTO = {
    "X:BTCUSD", "X:ETHUSD", "X:SOLUSD", "X:DOGEUSD", "X:AVAXUSD",
    "X:ADAUSD", "X:XRPUSD", "X:LTCUSD", "X:DOTUSD", "X:LINKUSD",
    "X:UNIUSD", "X:ATOMUSD", "X:MATICUSD", "X:NEARUSD",
}
# Subset top-100 azioni S&P 500 — fallback minimale se l'API è giù.
# Non è esaustivo: in caso di fallback il bot avrà accesso solo a queste.
_FALLBACK_STOCKS = {
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "NVDA", "META", "TSLA", "BRK.B",
    "UNH", "JNJ", "JPM", "V", "PG", "XOM", "HD", "MA", "CVX", "MRK", "ABBV",
    "LLY", "PEP", "KO", "COST", "AVGO", "WMT", "MCD", "CSCO", "ACN", "ABT",
    "DHR", "TMO", "NEE", "ADBE", "NKE", "PM", "BMY", "UNP", "RTX", "HON",
    "INTC", "IBM", "UPS", "LOW", "AMGN", "BA", "QCOM", "ORCL", "CAT", "GS",
    "AMD", "GE", "T", "MS", "BLK", "DE", "AXP", "C", "BKNG", "TXN", "SBUX",
    "ISRG", "PFE", "MDT", "CMCSA", "NOW", "VZ", "ELV", "INTU", "AMAT", "ADI",
    "PYPL", "GILD", "PLD", "TGT", "MO", "MU", "REGN", "CB", "USB", "PNC",
    "DUK", "BSX", "SO", "SCHW", "TFC", "ZTS", "EOG", "MDLZ", "GS", "ETN",
    "WFC", "EMR", "AON", "PGR", "MMC", "VRTX", "SPGI", "FDX", "BX", "F",
    "GLD", "SLV", "USO",   # commodity ETF supportati
    # Index/sector ETF molto tradati (QQQ/SPY/IWM/DIA sono i 4 ETF più
    # liquidi al mondo, IL FALLBACK NE ERA SPROVVISTO → Technical scartava
    # i trigger watchdog su QQQ e SPY).
    "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "VEA", "VWO",
    "XLF", "XLK", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB",
    "XLRE", "XLC", "TLT", "HYG", "LQD", "EFA", "EEM", "GDX", "ARKK",
}
_FALLBACK_UNIVERSE = _FALLBACK_CRYPTO | _FALLBACK_STOCKS


# ── Cache thread-safe ───────────────────────────────────────────────────────
_cache_lock = threading.Lock()
_cache_data: Optional[set[str]] = None
_cache_loaded_at: float = 0.0


def _fetch_universe_from_api(api_key: str) -> Optional[set[str]]:
    """Chiama GET /api/data/symbols con auth Bearer. None se fallisce."""
    if not api_key:
        return None
    req = urllib.request.Request(
        CLAWSTREET_SYMBOLS_URL,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                logger.warning(
                    "ClawStreet symbols endpoint HTTP %d", resp.status,
                )
                return None
            body = _json.loads(resp.read().decode("utf-8"))
        symbols = body.get("symbols")
        if not isinstance(symbols, list) or not symbols:
            logger.warning("ClawStreet symbols payload sospetto: %r", body)
            return None
        return {s.upper().strip() for s in symbols if isinstance(s, str)}
    except urllib.error.HTTPError as exc:
        logger.warning(
            "ClawStreet symbols HTTP %s: %s",
            exc.code, exc.read().decode("utf-8", errors="replace")[:200],
        )
        return None
    except Exception as exc:
        logger.warning("ClawStreet symbols fetch fallito: %s", exc)
        return None


def _resolve_api_key() -> str:
    """Prende la API key da env (ordine: CLAWSTREET_API_KEY → DB settings)."""
    key = os.environ.get("CLAWSTREET_API_KEY", "").strip()
    if key:
        return key
    try:
        import database as _db
        return (_db.get_setting("clawstreet_api_key", "") or "").strip()
    except Exception:
        return ""


def _load_universe(force_refresh: bool = False) -> set[str]:
    """
    Restituisce il set di simboli ClawStreet (formato ufficiale, MAIUSCOLO).
    Cache TTL 24h. Fallback hard-coded se l'API non risponde.
    """
    global _cache_data, _cache_loaded_at

    with _cache_lock:
        now = time.time()
        cache_fresh = (
            _cache_data is not None
            and (now - _cache_loaded_at) < CACHE_TTL_SECONDS
        )
        if cache_fresh and not force_refresh:
            return _cache_data

        # Cache scaduta o non inizializzata: prova a ricaricare
        api_key = _resolve_api_key()
        fetched = _fetch_universe_from_api(api_key)

        if fetched:
            _cache_data = fetched
            _cache_loaded_at = now
            logger.info(
                "ClawStreet universe loaded da API: %d simboli (%d crypto, %d stock)",
                len(fetched),
                sum(1 for s in fetched if s.startswith("X:")),
                sum(1 for s in fetched if not s.startswith("X:")),
            )
        else:
            # Fallback: usa la lista cacheata se esiste, altrimenti hard-coded
            if _cache_data is None:
                _cache_data = set(_FALLBACK_UNIVERSE)
                _cache_loaded_at = now
                logger.warning(
                    "ClawStreet API non disponibile — uso fallback hard-coded "
                    "(%d simboli). Universo ridotto: alcuni ticker S&P 500 "
                    "potrebbero essere temporaneamente bloccati.",
                    len(_cache_data),
                )
            else:
                logger.warning(
                    "ClawStreet API non disponibile — riuso cache stale "
                    "(età: %ds, %d simboli)",
                    int(now - _cache_loaded_at), len(_cache_data),
                )
        return _cache_data


def to_clawstreet_format(ticker: str) -> str:
    """
    Converte un ticker yfinance/free-form al formato canonico ClawStreet.

    Esempi:
      "btc-usd"  → "X:BTCUSD"
      "BTC-USD"  → "X:BTCUSD"
      "X:BTCUSD" → "X:BTCUSD"     (idempotente)
      "BTCUSD"   → "X:BTCUSD"     (manca prefisso)
      "AAPL"     → "AAPL"
      "aapl"     → "AAPL"
      "BRK.B"    → "BRK.B"
      "ENI.MI"   → "ENI.MI"       (passa through, non valido su CS — la
                                    validazione avviene in is_supported)
    """
    if not ticker:
        return ""
    t = ticker.strip().upper()
    if t.startswith("X:"):
        return t
    # Crypto yfinance: TICKER-USD
    if t.endswith("-USD") and len(t) > 4:
        clean = t.replace("-USD", "USD")
        return f"X:{clean}"
    # Crypto senza trattino e senza prefisso (raro): "BTCUSD"
    if t.endswith("USD") and "-" not in t and len(t) >= 5 and t not in {"USD"}:
        # Heuristica: se il ticker è 5+ char e finisce in USD ed è in
        # _FALLBACK_CRYPTO senza prefisso, considera crypto
        if f"X:{t}" in _FALLBACK_CRYPTO:
            return f"X:{t}"
    return t


def is_supported(ticker: str) -> bool:
    """
    True se il ticker è tradabile su ClawStreet.
    Idempotente sul formato: accetta "BTC-USD", "btc-usd", "X:BTCUSD" — tutti
    risolvono allo stesso simbolo canonico.
    """
    if not ticker:
        return False
    cs_symbol = to_clawstreet_format(ticker)
    universe = _load_universe()
    return cs_symbol in universe


def get_universe_summary() -> dict:
    """Riassunto dell'universo per debug / messaggi all'agent."""
    universe = _load_universe()
    crypto = sorted(s for s in universe if s.startswith("X:"))
    stocks = sorted(s for s in universe if not s.startswith("X:"))
    return {
        "total": len(universe),
        "stocks": len(stocks),
        "crypto": len(crypto),
        "crypto_list": crypto,
        "loaded_at": _cache_loaded_at,
    }


def refresh_universe() -> dict:
    """Forza un refresh della cache. Utile in admin/ops."""
    _load_universe(force_refresh=True)
    return get_universe_summary()
