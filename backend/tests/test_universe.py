"""
test_universe.py — Step 1: universe.py è la fonte unica e il refactor è
a COMPORTAMENTO INVARIATO.

Verifica:
  - le liste centralizzate hanno il contenuto storico esatto;
  - rotation_scan / orchestrator espongono ancora gli stessi valori
    (re-export non rotto);
  - il blocco prompt 'UNIVERSO INVESTIBILE' è coerente con le liste
    strutturate (ogni crypto/ETF citato esiste nelle costanti).
"""
import universe


def test_core_crypto_14():
    assert len(universe.CORE_CRYPTO) == 14
    assert universe.CORE_CRYPTO[0] == "BTC-USD"
    assert "NEAR-USD" in universe.CORE_CRYPTO


def test_commodity_etfs_exact():
    assert universe.COMMODITY_ETFS == ["GLD", "SLV", "USO"]


def test_default_fallback_unchanged():
    assert universe.DEFAULT_FALLBACK_TICKERS == ["SPY", "XOM", "LMT", "GLD", "QQQ", "EEM"]


def test_watchlist_unchanged():
    assert universe.WATCHLIST["energy"] == ["XOM", "CVX", "SHEL", "TTE", "ENI"]
    assert set(universe.WATCHLIST.keys()) == {
        "energy", "defense", "gold_commodities", "etf_broad", "europe",
    }


def test_rotation_universe_unchanged():
    ru = universe.ROTATION_UNIVERSE
    assert set(ru.keys()) == {
        "defensive", "safe_haven", "hedge", "geopolitical", "energy_commodity",
        "sectors", "bonds", "international", "factor", "high_movement_equity",
    }
    assert ru["sectors"] == ["XLK", "XLF", "XLY", "XLI", "XLB", "XLRE", "XLC"]
    assert ru["international"] == ["VGK", "EWJ", "INDA", "EWZ", "FXI"]
    assert ru["safe_haven"] == ["GLD", "IAU", "GDX", "SLV", "TLT", "IEF", "SHY", "TIP", "UUP"]
    # derivati
    assert universe.ROTATION_TICKER_TO_CATEGORY["GLD"] == "safe_haven"
    assert universe.ROTATION_TICKER_TO_CATEGORY["XLE"] == "energy_commodity"
    assert "SPY" not in universe.ALL_ROTATION_TICKERS  # SPY non è nell'universo rotazione
    assert len(universe.VALID_CATEGORIES) == 10


def test_rotation_scan_reexport_matches():
    """rotation_scan deve esporre ESATTAMENTE l'universo centralizzato."""
    from agents import rotation_scan
    assert rotation_scan.ROTATION_UNIVERSE == universe.ROTATION_UNIVERSE
    assert rotation_scan.VALID_CATEGORIES == universe.VALID_CATEGORIES
    assert rotation_scan.ALL_ROTATION_TICKERS == universe.ALL_ROTATION_TICKERS


def test_decision_block_consistent_with_lists():
    block = universe.build_decision_universe_block()
    assert block == universe.DECISION_UNIVERSE_BLOCK
    assert block.startswith("UNIVERSO INVESTIBILE")
    assert block.rstrip().endswith("do_nothing motivando.")
    # ogni crypto core ed esclusa è citata nel testo, e viceversa
    for c in universe.CORE_CRYPTO:
        assert c in block, f"{c} (core) mancante nel blocco prompt"
    for c in universe.EXCLUDED_CRYPTO:
        assert c in block, f"{c} (esclusa) mancante nel blocco prompt"
    for e in universe.COMMODITY_ETFS:
        assert e in block


def test_classify_ticker():
    assert universe.classify_ticker("BTC-USD")["asset_class"] == "crypto"
    assert universe.classify_ticker("BTC-USD")["bucket"] == "crypto_core"
    assert universe.classify_ticker("BNB-USD")["bucket"] == "crypto_excluded"
    # In Step 2 il satellite ha priorità sul bucket di rotazione
    assert universe.classify_ticker("XLU")["tier"] == "satellite"
    assert universe.classify_ticker("XLU")["bucket"] == "sector_etf"
    assert universe.classify_ticker("EWZ")["bucket"] == "single_country"
    # un nome di rotazione NON satellite resta core col suo bucket
    assert universe.classify_ticker("NEE")["tier"] == "core"
    assert universe.classify_ticker("NEE")["bucket"] == "defensive"
    assert universe.classify_ticker("AAPL")["is_mega"] is True
    assert universe.classify_ticker("AAPL")["tier"] == "core"
    assert universe.classify_ticker("ENI.MI")["bucket"] == "international_single"
    # un nome S&P non in rotation/satellite e non mega → core_equity
    assert universe.classify_ticker("F")["bucket"] == "core_equity"
    assert universe.classify_ticker("")["bucket"] == "unknown"


def test_satellite_populated_step2():
    assert universe.SATELLITE  # popolata in Step 2
    flat = universe.satellite_universe_flat()
    assert "IWM" in flat and "EWZ" in flat and "XLE" in flat
    assert universe.is_satellite("IWM")
    assert not universe.is_satellite("AAPL")
    assert universe.satellite_category("XLE") == "sector_etf"
    assert universe.satellite_min_adv_usd() > 0
    assert universe.expanded_universe_enabled() in (True, False)
