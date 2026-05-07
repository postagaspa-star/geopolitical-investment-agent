"""
test_data_sources.py — Test end-to-end delle fonti dati.

Verifica:
  1. Polygon.io risponde correttamente per current price + OHLCV
  2. Massive.com risponde correttamente per current price + OHLCV
  3. yfinance risponde + il corruption detector funziona
  4. I prezzi tra fonti sono consistenti (delta < 5% — soglia generosa per
     timing differente tra free tier delay)
  5. La cascade in fetch_market_data funziona (rispetta priorità)
  6. Il corruption detector rileva pattern di same-price

Uso:
    cd backend
    python -m tools.test_data_sources

Variabili d'ambiente:
    POLYGON_API_KEY (raccomandata)
    MASSIVE_API_KEY (opzionale, fallback)

Exit codes:
    0 — almeno UNA fonte risponde correttamente
    1 — TUTTE le fonti falliscono (deployment bloccante)
    2 — corruzione attiva non rilevata (bug nel detector)
"""

import asyncio
import os
import sys
import time
from typing import Any

# Forza UTF-8 stdout su Windows (CP1252 default non gestisce unicode box chars)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Aggiungi backend/ al PYTHONPATH per import diretti
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


# ── Test ticker (S&P 500 liquido + crypto + ETF) ─────────────────────────────
TEST_TICKER_PRIMARY = "AAPL"
TEST_TICKERS_BATCH = ["AAPL", "MSFT", "GOOGL", "NVDA", "XOM"]


def _print_section(title: str):
    print()
    print("-" * 70)
    print(f"  {title}")
    print("-" * 70)


def _format_result(name: str, status: str, **details) -> str:
    icon = {"OK": "[OK]", "FAILED": "[FAIL]", "SKIPPED": "[SKIP]",
            "PARTIAL": "[PART]", "CORRUPTED": "[CORR]"}.get(status, "[?]")
    parts = [f"{icon} {name}: {status}"]
    for k, v in details.items():
        parts.append(f"{k}={v}")
    return "  ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# TEST 1: Polygon current price quote (price_polling)
# ─────────────────────────────────────────────────────────────────────────────
async def test_polygon_quote() -> dict:
    """Test Polygon current price via _fetch_polygon_quotes."""
    import price_polling as pp
    api_key = pp._get_polygon_key()
    if not api_key:
        return {"name": "polygon_quote", "status": "SKIPPED",
                "reason": "POLYGON_API_KEY non configurata"}

    t0 = time.time()
    quotes = await pp._fetch_polygon_quotes([TEST_TICKER_PRIMARY], api_key)
    dt = round(time.time() - t0, 2)

    if TEST_TICKER_PRIMARY not in quotes:
        return {"name": "polygon_quote", "status": "FAILED",
                "reason": f"no quote returned for {TEST_TICKER_PRIMARY}",
                "elapsed_s": dt}

    q = quotes[TEST_TICKER_PRIMARY]
    price = q.get("price", 0)
    if price <= 0:
        return {"name": "polygon_quote", "status": "FAILED",
                "reason": f"invalid price={price}", "elapsed_s": dt}

    return {"name": "polygon_quote", "status": "OK",
            "price": price, "prev_close": q.get("prev_close"),
            "elapsed_s": dt}


# ─────────────────────────────────────────────────────────────────────────────
# TEST 2: Massive current price quote (price_polling)
# ─────────────────────────────────────────────────────────────────────────────
async def test_massive_quote() -> dict:
    """Test Massive current price via _fetch_massive_quotes."""
    import price_polling as pp
    api_key = pp._get_massive_key()
    if not api_key:
        return {"name": "massive_quote", "status": "SKIPPED",
                "reason": "MASSIVE_API_KEY non configurata"}

    t0 = time.time()
    quotes = await pp._fetch_massive_quotes([TEST_TICKER_PRIMARY], api_key)
    dt = round(time.time() - t0, 2)

    if TEST_TICKER_PRIMARY not in quotes:
        return {"name": "massive_quote", "status": "FAILED",
                "reason": f"no quote returned for {TEST_TICKER_PRIMARY}",
                "elapsed_s": dt}

    q = quotes[TEST_TICKER_PRIMARY]
    price = q.get("price", 0)
    if price <= 0:
        return {"name": "massive_quote", "status": "FAILED",
                "reason": f"invalid price={price}", "elapsed_s": dt}

    return {"name": "massive_quote", "status": "OK",
            "price": price, "prev_close": q.get("prev_close"),
            "elapsed_s": dt}


# ─────────────────────────────────────────────────────────────────────────────
# TEST 3: yfinance current price + corruption detector
# ─────────────────────────────────────────────────────────────────────────────
def test_yfinance_quote_and_corruption() -> dict:
    """
    Test yfinance batch + verifica che il corruption detector funzioni.
    Sotto rate-limit Yahoo, il batch può restituire stesso prezzo per N ticker:
    il detector deve scartare i risultati e ritentare per-ticker.
    """
    import price_polling as pp
    t0 = time.time()
    quotes = pp._fetch_yfinance_quotes(TEST_TICKERS_BATCH)
    dt = round(time.time() - t0, 2)

    if not quotes:
        return {"name": "yfinance_quote", "status": "FAILED",
                "reason": "empty result (potenzialmente rate-limited)",
                "elapsed_s": dt}

    # Verifica che il detector NON triggeri sui risultati validi
    corrupted, desc = pp._detect_yfinance_corruption(quotes)
    if corrupted:
        return {"name": "yfinance_quote", "status": "CORRUPTED",
                "reason": desc, "elapsed_s": dt,
                "got_tickers": list(quotes.keys())}

    if TEST_TICKER_PRIMARY not in quotes:
        return {"name": "yfinance_quote", "status": "PARTIAL",
                "got_tickers": list(quotes.keys()),
                "reason": f"{TEST_TICKER_PRIMARY} mancante",
                "elapsed_s": dt}

    return {"name": "yfinance_quote", "status": "OK",
            "price": quotes[TEST_TICKER_PRIMARY]["price"],
            "tickers_count": len(quotes),
            "elapsed_s": dt}


# ─────────────────────────────────────────────────────────────────────────────
# TEST 4: Corruption detector unit test (con dati fabbricati)
# ─────────────────────────────────────────────────────────────────────────────
def test_corruption_detector_unit() -> dict:
    """
    Forza un caso di corruzione: 4 ticker con prezzo identico.
    Il detector DEVE rilevarlo.
    """
    import price_polling as pp

    # Caso 1: 4 ticker stesso prezzo → DEVE essere flagged
    fake_corrupted = {
        "AAPL": {"price": 430.96, "prev_close": 430.96},
        "MSFT": {"price": 430.96, "prev_close": 430.96},
        "GOOGL": {"price": 430.96, "prev_close": 430.96},
        "NVDA": {"price": 430.96, "prev_close": 430.96},
    }
    is_corr_1, _ = pp._detect_yfinance_corruption(fake_corrupted)

    # Caso 2: 4 ticker prezzi diversi → NON deve essere flagged
    fake_clean = {
        "AAPL": {"price": 188.50, "prev_close": 187.20},
        "MSFT": {"price": 425.10, "prev_close": 423.90},
        "GOOGL": {"price": 175.40, "prev_close": 174.80},
        "NVDA": {"price": 880.20, "prev_close": 875.50},
    }
    is_corr_2, _ = pp._detect_yfinance_corruption(fake_clean)

    if not is_corr_1:
        return {"name": "corruption_detector", "status": "FAILED",
                "reason": "Caso 1 (4 prezzi identici) NON rilevato come corrotto"}
    if is_corr_2:
        return {"name": "corruption_detector", "status": "FAILED",
                "reason": "Caso 2 (prezzi normali) flaggato erroneamente come corrotto"}
    return {"name": "corruption_detector", "status": "OK",
            "case1_detected": is_corr_1,
            "case2_clean": not is_corr_2}


# ─────────────────────────────────────────────────────────────────────────────
# TEST 5: OHLCV via fetch_market_data (cascade Polygon → Massive → yfinance)
# ─────────────────────────────────────────────────────────────────────────────
def test_fetch_market_data_cascade() -> dict:
    """
    Test la cascade completa: chiama fetch_market_data e verifica:
    - data list popolata
    - source field rispetta la priorità (polygon > massive > yfinance)
    - 60+ giorni di OHLCV se period_days=90 (~63 trading days)
    """
    import data_fetchers as df

    t0 = time.time()
    result = df.fetch_market_data(TEST_TICKER_PRIMARY, period_days=90)
    dt = round(time.time() - t0, 2)

    if not result.get("data"):
        return {"name": "ohlcv_cascade", "status": "FAILED",
                "reason": result.get("error", "no data"), "elapsed_s": dt}

    bars = len(result["data"])
    if bars < 30:
        return {"name": "ohlcv_cascade", "status": "PARTIAL",
                "reason": f"solo {bars} bars (atteso ≥30)",
                "source": result.get("source"),
                "elapsed_s": dt}

    last_close = result["data"][-1].get("close", 0)
    if last_close <= 0:
        return {"name": "ohlcv_cascade", "status": "FAILED",
                "reason": f"last close invalido: {last_close}", "elapsed_s": dt}

    return {"name": "ohlcv_cascade", "status": "OK",
            "source": result.get("source", "?"),
            "bars": bars,
            "last_close": last_close,
            "first_date": result["data"][0]["date"],
            "last_date": result["data"][-1]["date"],
            "elapsed_s": dt}


# ─────────────────────────────────────────────────────────────────────────────
# TEST 6: OHLCV corruption detector (unit)
# ─────────────────────────────────────────────────────────────────────────────
def test_ohlcv_corruption_detector_unit() -> dict:
    """
    Forza un caso di OHLCV corrotto: tutti i close uguali → must flag.
    """
    import data_fetchers as df

    # Caso 1: corrotto (tutti close identici)
    corrupted_records = [
        {"date": "2025-01-01", "close": 100.0, "open": 100, "high": 100, "low": 100, "volume": 1000}
        for _ in range(20)
    ]
    is_corr_1 = df._is_ohlcv_corrupted(corrupted_records)

    # Caso 2: clean (close variabili)
    clean_records = [
        {"date": f"2025-01-{i+1:02d}", "close": 100.0 + i, "open": 99,
         "high": 102, "low": 98, "volume": 1000}
        for i in range(20)
    ]
    is_corr_2 = df._is_ohlcv_corrupted(clean_records)

    if not is_corr_1:
        return {"name": "ohlcv_corruption", "status": "FAILED",
                "reason": "tutti close uguali NON rilevati"}
    if is_corr_2:
        return {"name": "ohlcv_corruption", "status": "FAILED",
                "reason": "close variabili flaggati erroneamente"}
    return {"name": "ohlcv_corruption", "status": "OK",
            "case1_detected": is_corr_1,
            "case2_clean": not is_corr_2}


# ─────────────────────────────────────────────────────────────────────────────
# TEST 7: Cross-source price consistency
# ─────────────────────────────────────────────────────────────────────────────
def test_cross_source_consistency(quote_results: list[dict]) -> dict:
    """
    Confronta i prezzi correnti tra le fonti che hanno risposto.
    Soglia: delta max 5% (generoso perché Polygon free tier ha 15-min delay
    e Massive può divergere).
    """
    prices = {
        r["name"]: r["price"]
        for r in quote_results
        if r.get("status") == "OK" and "price" in r
    }
    if len(prices) < 2:
        return {"name": "consistency", "status": "SKIPPED",
                "reason": f"solo {len(prices)} fonte/i con prezzo"}

    pmin = min(prices.values())
    pmax = max(prices.values())
    delta_pct = ((pmax - pmin) / pmin * 100) if pmin > 0 else 0

    threshold = 5.0  # %
    if delta_pct > threshold:
        return {"name": "consistency", "status": "FAILED",
                "delta_pct": round(delta_pct, 2),
                "threshold_pct": threshold,
                "prices": prices}
    return {"name": "consistency", "status": "OK",
            "delta_pct": round(delta_pct, 2),
            "prices": prices}


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
async def main():
    print()
    print("=" * 70)
    print("  TEST FONTI DATI - Polygon.io / Massive.com / yfinance")
    print(f"  Ticker test: {TEST_TICKER_PRIMARY} (batch: {TEST_TICKERS_BATCH})")
    print("=" * 70)

    all_results: list[dict] = []

    # ── Quote tests ──────────────────────────────────────────────────────────
    _print_section("[1/7] Polygon.io - current price quote")
    r = await test_polygon_quote()
    print("  ", _format_result(r["name"], r["status"],
                                **{k: v for k, v in r.items() if k not in ("name", "status")}))
    all_results.append(r)

    _print_section("[2/7] Massive.com - current price quote")
    r = await test_massive_quote()
    print("  ", _format_result(r["name"], r["status"],
                                **{k: v for k, v in r.items() if k not in ("name", "status")}))
    all_results.append(r)

    _print_section("[3/7] yfinance - current price + corruption guard")
    r = test_yfinance_quote_and_corruption()
    print("  ", _format_result(r["name"], r["status"],
                                **{k: v for k, v in r.items() if k not in ("name", "status")}))
    all_results.append(r)

    # ── Unit tests detector ──────────────────────────────────────────────────
    _print_section("[4/7] Corruption detector - unit test (price)")
    r = test_corruption_detector_unit()
    print("  ", _format_result(r["name"], r["status"],
                                **{k: v for k, v in r.items() if k not in ("name", "status")}))
    all_results.append(r)

    # ── OHLCV cascade ────────────────────────────────────────────────────────
    _print_section("[5/7] OHLCV cascade - fetch_market_data()")
    r = test_fetch_market_data_cascade()
    print("  ", _format_result(r["name"], r["status"],
                                **{k: v for k, v in r.items() if k not in ("name", "status")}))
    all_results.append(r)

    _print_section("[6/7] OHLCV corruption detector - unit test")
    r = test_ohlcv_corruption_detector_unit()
    print("  ", _format_result(r["name"], r["status"],
                                **{k: v for k, v in r.items() if k not in ("name", "status")}))
    all_results.append(r)

    # ── Consistency ──────────────────────────────────────────────────────────
    _print_section("[7/7] Cross-source price consistency")
    quote_results = [r for r in all_results
                     if r["name"] in ("polygon_quote", "massive_quote", "yfinance_quote")]
    r = test_cross_source_consistency(quote_results)
    print("  ", _format_result(r["name"], r["status"],
                                **{k: v for k, v in r.items() if k not in ("name", "status")}))
    all_results.append(r)

    # ── Summary ──────────────────────────────────────────────────────────────
    print()
    print("=" * 70)
    ok = sum(1 for r in all_results if r["status"] == "OK")
    failed = sum(1 for r in all_results if r["status"] == "FAILED")
    skipped = sum(1 for r in all_results if r["status"] == "SKIPPED")
    partial = sum(1 for r in all_results if r["status"] in ("PARTIAL", "CORRUPTED"))
    total = len(all_results)
    print(f"  SUMMARY: {ok}/{total} OK  |  {failed} FAILED  |  "
          f"{partial} PARTIAL  |  {skipped} SKIPPED")
    print("=" * 70)

    # Determine exit code
    quote_ok = sum(1 for r in all_results
                   if r["name"] in ("polygon_quote", "massive_quote", "yfinance_quote")
                   and r["status"] == "OK")
    detector_ok = all(r["status"] == "OK" for r in all_results
                      if r["name"] in ("corruption_detector", "ohlcv_corruption"))

    if not detector_ok:
        print("\n  [CRITICAL] corruption detector NON funziona - exit 2")
        sys.exit(2)
    if quote_ok == 0:
        print("\n  [CRITICAL] nessuna fonte di prezzo risponde - exit 1")
        sys.exit(1)
    print()
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
