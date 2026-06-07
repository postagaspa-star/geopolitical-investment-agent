"""
universe.py — Fonte UNICA di verità per l'universo investibile di GeoInvest.

Prima di questo modulo l'universo era definito in 4 punti scollegati e a volte
contraddittori fra loro:
  - data_fetchers.WATCHLIST            (news + polling)
  - orchestrator._get_default_tickers  (fallback quando lo Scout tace)
  - rotation_scan.ROTATION_UNIVERSE    (scan di rotazione, ~60 ticker)
  - decision.py prompt "UNIVERSO INVESTIBILE — VINCOLO RIGIDO" (allowlist)

Qui vivono TUTTI come dato strutturato. I moduli sopra importano da qui.

IMPORTANTE — questo è uno Step 1 a COMPORTAMENTO INVARIATO:
  * Le liste hanno lo stesso identico contenuto di prima (vedi tests/test_universe.py).
  * L'allowlist resta SOFT: è prosa nel prompt + gate de-facto al layer dati
    (niente OHLCV → niente prezzo → niente trade). NON viene introdotto qui
    nessun nuovo gate hard di rifiuto.
  * La cintura "satellite" è PREDISPOSTA ma VUOTA e dietro feature-flag
    (EXPANDED_UNIVERSE, default OFF): serve agli Step 2+ del piano universo.

Questo modulo è volutamente SENZA dipendenze dal resto del backend (solo
stdlib) per evitare import circolari: data_fetchers / rotation_scan /
orchestrator / decision importano universe, mai il contrario.
"""

from __future__ import annotations

import os
from typing import Dict, List


# ════════════════════════════════════════════════════════════════════════
# FEATURE FLAG — cintura satellite (Step 2+). Default OFF: comportamento
# identico a prima. Quando ON, gli Step successivi popoleranno SATELLITE e i
# candidate-builder potranno proporre nomi meno efficienti, con i guardrail di
# liquidità/qualità-dati e il sizing ridotto definiti negli step seguenti.
# ════════════════════════════════════════════════════════════════════════
def expanded_universe_enabled() -> bool:
    """True se la cintura satellite è attiva (env EXPANDED_UNIVERSE=1/true/on)."""
    return os.getenv("EXPANDED_UNIVERSE", "").strip().lower() in {"1", "true", "on", "yes"}


# ════════════════════════════════════════════════════════════════════════
# CINTURA CORE — universo tradabile attuale (INVARIATO).
# ════════════════════════════════════════════════════════════════════════

# Le 14 crypto supportate (formato yfinance "X-USD"). Ordine = come nel prompt.
CORE_CRYPTO: List[str] = [
    "BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD", "AVAX-USD", "ADA-USD",
    "XRP-USD", "LTC-USD", "DOT-USD", "LINK-USD", "UNI-USD", "ATOM-USD",
    "MATIC-USD", "NEAR-USD",
]

# Crypto citabili nell'analisi macro ma NON tradabili (allowlist soft).
EXCLUDED_CRYPTO: List[str] = [
    "BNB-USD", "SHIB-USD", "AAVE-USD", "PEPE-USD", "FIL-USD", "ALGO-USD",
    "XMR-USD", "ICP-USD",
]

# Gli unici 3 ETF commodity ammessi.
COMMODITY_ETFS: List[str] = ["GLD", "SLV", "USO"]

# ETF/indici esplicitamente NON supportati (esempi citati nel prompt).
EXCLUDED_ETFS: List[str] = [
    "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "TLT",
    "XLE", "XLF", "XLK", "XLV",
]

# Suffissi non-USA non supportati (azioni estere).
NON_US_SUFFIXES: List[str] = [".MI", ".PA", ".DE", ".L", ".AS", ".HK", ".TO"]

# Esempi rappresentativi dell'universo equity core (~484 S&P 500). NON è la
# lista completa: l'appartenenza all'S&P 500 resta risolta SOFT (prompt +
# disponibilità dati). Serve da riferimento e per la classificazione metrica.
CORE_EQUITY_EXAMPLES: List[str] = [
    "NVDA", "TSLA", "AAPL", "XOM", "MSFT", "AMZN", "GOOGL", "META", "JPM",
    "V", "MA", "JNJ", "UNH", "PG", "KO", "PEP", "COST", "WMT", "HD", "CVX",
    "MRK", "LLY", "AVGO", "ORCL", "CSCO", "ACN", "ABT", "TMO", "NEE", "ADBE",
    "NKE", "BMY", "AMGN", "BA", "QCOM", "IBM", "CAT", "GS", "MS", "BLK",
    "AMD", "GE", "T", "AXP", "C", "BKNG", "TXN", "SBUX", "PFE", "MDT",
    "CMCSA", "NOW", "VZ", "ELV", "INTU", "AMAT", "ADI", "GILD", "PLD", "TGT",
    "MO", "MU", "SCHW", "REGN", "EOG", "MDLZ", "FDX", "WFC", "F",
]

# Mega-cap di riferimento per la metrica "quota fuori dai mega-cap".
MEGA_CAP_EQUITY: List[str] = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "AMZN", "META", "TSLA", "AVGO",
    "BRK.B", "LLY", "JPM", "V", "UNH", "XOM", "MA", "JNJ", "PG", "HD",
    "COST", "WMT", "ORCL", "NFLX", "ABBV", "BAC",
]


# ════════════════════════════════════════════════════════════════════════
# CINTURA SATELLITE (Step 2) — universo meno efficiente, dietro feature-flag
# EXPANDED_UNIVERSE (default OFF). Tutti ETF LIQUIDI (l'inefficienza sta nella
# minor copertura/efficienza dell'esposizione, non nella illiquidità del
# veicolo): settoriali, single-country/EM, tematici, materie prime oltre i 3
# core, size/style. Catturano rotazione + relazioni relative dove il vantaggio
# macro paga. Ogni nome entra come CANDIDATO solo superando i guardrail di
# liquidità/qualità (liquidity.py) ed è soggetto a sizing ridotto (risk_profile).
# Quando il flag è OFF questa cintura è inerte: nessun candidato satellite,
# nessun cambio di sizing, scan invariato.
# ════════════════════════════════════════════════════════════════════════
SATELLITE: Dict[str, List[str]] = {
    # 11 settori SPDR (rotazione settoriale pulita e liquida)
    "sector_etf": ["XLK", "XLF", "XLE", "XLI", "XLB", "XLRE", "XLC",
                   "XLU", "XLP", "XLV", "XLY"],
    # Single-country (decoupling geografico, meno efficiente di SPY)
    "single_country": ["EWZ", "INDA", "FXI", "EWJ", "EWG", "EWW", "EWY",
                       "EWT", "EWA", "EWC", "EWU", "TUR", "EZA"],
    # Broad EM / ex-US
    "broad_intl_em": ["EEM", "VWO", "EFA", "IEMG", "SCZ"],
    # Tematici / industrie (alta dispersione, dove il ciclo conta)
    "thematic": ["SMH", "SOXX", "XBI", "KRE", "JETS", "XOP", "GDX", "GDXJ",
                 "XME", "TAN", "URA", "KWEB", "ARKK"],
    # Materie prime oltre i 3 core (GLD/SLV/USO restano core)
    "commodity_broad": ["DBC", "DBA", "UNG", "USL", "CPER", "DBB", "PALL", "PPLT"],
    # Size / style (small/mid-cap e value/growth via ETF, non single-name)
    "size_style": ["IWM", "IJR", "IJH", "IWN", "IWD", "MDY"],
}

_SATELLITE_TICKER_TO_CAT: Dict[str, str] = {
    t: cat for cat, ts in SATELLITE.items() for t in ts
}


def satellite_universe_flat() -> List[str]:
    """Tutti i ticker satellite, dedup e ordinati."""
    return sorted({t for ts in SATELLITE.values() for t in ts})


def is_satellite(ticker: str) -> bool:
    return (ticker or "").upper().strip() in _SATELLITE_TICKER_TO_CAT


def satellite_category(ticker: str):
    """Bucket satellite del ticker, o None se non è satellite."""
    return _SATELLITE_TICKER_TO_CAT.get((ticker or "").upper().strip())


# ── Parametri della cintura satellite (tunabili; ADV anche via env) ──────
SATELLITE_MIN_ADV_USD = 5_000_000.0      # floor di liquidità ($/giorno) per ammettere un satellite
MAX_ROTATION_LEADERS_PER_CATEGORY = 2    # quanti leader per categoria nel blocco-candidati
MAX_SATELLITE_CANDIDATES = 8             # tetto candidati satellite per run
ROTATION_PAIR_MIN_SPREAD_PCT = 3.0       # spread RS minimo per segnalare una coppia anti-correlata


def satellite_min_adv_usd() -> float:
    """Floor ADV satellite, override via env SATELLITE_MIN_ADV_USD."""
    raw = os.getenv("SATELLITE_MIN_ADV_USD", "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return SATELLITE_MIN_ADV_USD


# ════════════════════════════════════════════════════════════════════════
# WATCHLIST settoriale (ex data_fetchers.WATCHLIST) — INVARIATA.
# ════════════════════════════════════════════════════════════════════════
WATCHLIST: Dict[str, List[str]] = {
    "energy": ["XOM", "CVX", "SHEL", "TTE", "ENI"],
    "defense": ["LMT", "RTX", "NOC", "BA", "LDOS"],
    "gold_commodities": ["GLD", "SLV", "USO", "UNG"],
    "etf_broad": ["SPY", "QQQ", "EEM", "VEA"],
    "europe": ["EWG", "EWI", "EWQ", "EWP"],
}

# Fallback quando lo Scout non produce ticker (ex orchestrator._get_default_tickers).
DEFAULT_FALLBACK_TICKERS: List[str] = ["SPY", "XOM", "LMT", "GLD", "QQQ", "EEM"]


# ════════════════════════════════════════════════════════════════════════
# UNIVERSO DI ROTAZIONE (ex rotation_scan.ROTATION_UNIVERSE) — INVARIATO.
# ~60 ticker liquidi cross-sector usati dallo scan di rotazione.
# ════════════════════════════════════════════════════════════════════════
ROTATION_UNIVERSE: Dict[str, List[str]] = {
    # Settori difensivi puri (anti-ciclici, dividend payers, demand inelastica)
    "defensive": [
        "XLU", "XLP", "XLV",
        "NEE", "DUK", "SO", "AEP",          # utilities
        "KO", "PG", "WMT", "COST", "PEP", "MO",  # consumer staples
        "JNJ", "UNH", "LLY",                 # healthcare mega-cap
    ],
    # Safe-haven monetari/reali (oro, treasuries, USD)
    "safe_haven": [
        "GLD", "IAU", "GDX",     # oro
        "SLV",                    # argento
        "TLT", "IEF", "SHY", "TIP",  # treasuries (long/mid/short/inflation-linked)
        "UUP",                    # USD index
    ],
    # Hedge diretti contro il mercato (inverse / volatilità)
    "hedge": [
        "SH", "PSQ", "RWM",   # inverse SPY/QQQ/IWM
        "VIXY",                # volatilità
    ],
    # Vincitori geopolitici (defense + aerospace)
    "geopolitical": [
        "LMT", "RTX", "NOC", "GD", "LHX",
        "ITA",   # aerospace & defense ETF
    ],
    # Energy + Commodity (beneficiari di shock supply/inflazione)
    "energy_commodity": [
        "XLE", "USO", "USL", "DBC",
        "OXY", "CVX", "XOM", "COP", "SLB",
    ],
    # Sector ETF rimanenti (per misurare rotazione settoriale completa)
    "sectors": [
        "XLK",   # tech
        "XLF",   # financials
        "XLY",   # consumer discretionary
        "XLI",   # industrials
        "XLB",   # materials
        "XLRE",  # real estate
        "XLC",   # communication
    ],
    # Bond corporate (segnali di stress credit / risk-on)
    "bonds": [
        "HYG",   # high yield (junk)
        "LQD",   # investment grade
        "AGG",   # aggregate
    ],
    # International / decoupling (rotazione geografica)
    "international": [
        "VGK",   # Europe
        "EWJ",   # Japan
        "INDA",  # India
        "EWZ",   # Brazil
        "FXI",   # China large cap
    ],
    # Factor (momentum / value / low-vol / quality)
    "factor": [
        "USMV",  # low volatility
        "QUAL",  # quality
        "MTUM",  # momentum
        "VLUE",  # value
    ],
    # Singoli titoli ad ALTO MOVIMENTO — semiconduttori, mega-cap tech, growth.
    "high_movement_equity": [
        "NVDA", "AMD", "INTC", "MU", "AVGO", "QCOM", "TXN", "AMAT",
        "LRCX", "ARM", "SMCI",
        "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "NFLX",
        "CRM", "ADBE", "ORCL", "NOW", "PLTR", "UBER",
        "COIN", "MSTR",
    ],
}

# Reverse lookup ticker → categoria
ROTATION_TICKER_TO_CATEGORY: Dict[str, str] = {
    t: cat for cat, ts in ROTATION_UNIVERSE.items() for t in ts
}

ALL_ROTATION_TICKERS: List[str] = sorted({
    t for ts in ROTATION_UNIVERSE.values() for t in ts
})

VALID_CATEGORIES: List[str] = list(ROTATION_UNIVERSE.keys())


# ════════════════════════════════════════════════════════════════════════
# BLOCCO PROMPT "UNIVERSO INVESTIBILE" del Decision Agent.
# Spostato qui VERBATIM dalla costante DECISION_SYSTEM_PROMPT_DEFAULT
# (decision.py) per avere UNA sola fonte. Il test test_universe.py verifica la
# coerenza fra questo testo e le liste strutturate (CORE_CRYPTO, COMMODITY_ETFS,
# EXCLUDED_CRYPTO, ...), così la deriva fra prosa e dato viene intercettata.
#
# NB: byte-identico all'originale — garantito dall'Edit a match esatto fatto in
# decision.py quando questo blocco è stato estratto.
# ════════════════════════════════════════════════════════════════════════
DECISION_UNIVERSE_BLOCK = """UNIVERSO INVESTIBILE — VINCOLO RIGIDO:
Il portfolio opera su un universo specifico di simboli. Trade su ticker NON
supportati vengono rifiutati. Rispetta SEMPRE le liste sottostanti.

✓ AZIONI TRADABILI: ~484 titoli S&P 500 (es. NVDA, TSLA, AAPL, XOM, MSFT, AMZN,
  GOOGL, META, JPM, V, MA, JNJ, UNH, PG, KO, PEP, COST, WMT, HD, CVX, MRK, LLY,
  AVGO, ORCL, CSCO, ACN, ABT, TMO, NEE, ADBE, NKE, BMY, AMGN, BA, QCOM, IBM,
  CAT, GS, MS, BLK, AMD, GE, T, AXP, C, BKNG, TXN, SBUX, PFE, MDT, CMCSA, NOW,
  VZ, ELV, INTU, AMAT, ADI, GILD, PLD, TGT, MO, MU, SCHW, REGN, EOG, MDLZ, FDX,
  WFC, F, etc.). Se in dubbio: verifica con request_extra_analysis.

✓ ETF COMMODITY: GLD (oro), SLV (argento), USO (petrolio). Solo questi 3.

✗ ETF/INDICI INDICIZZATI **NON SUPPORTATI**: SPY, QQQ, IWM, DIA, VTI, VOO, TLT,
  XLE, XLF, XLK, XLV, etc. Per esposizione settoriale, scegli SINGOLI titoli
  rappresentativi (es. invece di XLE → XOM/CVX, invece di XLK → MSFT/NVDA,
  invece di SPY → mix di top mega-cap).

✗ AZIONI EUROPEE/EXTRA-USA **NON SUPPORTATE**: ENI.MI, SAN.PA, SAP.DE, ASML.AS
  e qualsiasi suffisso `.MI/.PA/.DE/.L/.AS/.HK/.TO`. Solo NYSE/NASDAQ USA.

✓ CRYPTOVALUTE — SOLO QUESTI 14 TICKER (formato yfinance "X-USD"):
  BTC-USD, ETH-USD, SOL-USD, DOGE-USD, AVAX-USD, ADA-USD, XRP-USD, LTC-USD,
  DOT-USD, LINK-USD, UNI-USD, ATOM-USD, MATIC-USD, NEAR-USD.

✗ CRYPTO **NON SUPPORTATE** (NON tradare): BNB-USD, SHIB-USD, AAVE-USD,
  PEPE-USD, FIL-USD, ALGO-USD, XMR-USD, ICP-USD, e qualsiasi altro alt-coin
  fuori dalla lista sopra. Anche se Scout/Reddit ne parla, ignora i segnali
  di trading: puoi citarli nell'analisi macro ma non puoi entrare in posizione.

Le crypto restano particolarmente adatte all'analisi tecnica: (1) 24/7 senza
gap di apertura, (2) volumi alti, (3) rispondono a pattern tecnici e sentiment
retail. Privilegiale quando il mercato USA è chiuso.

REGOLA DI OPERATIVITÀ:
Se identifichi un'opportunità su un ticker NON supportato (es. SPY, BNB-USD,
ENI.MI), NON tentare execute_trade — verrà rifiutato. Cerca un sostituto
tradabile dello stesso settore/tema, oppure usa do_nothing motivando."""


_SATELLITE_LABELS = {
    "sector_etf": "ETF settoriali USA",
    "single_country": "Single-country / Emergenti",
    "broad_intl_em": "Broad EM / ex-US",
    "thematic": "Tematici",
    "commodity_broad": "Materie prime (oltre GLD/SLV/USO)",
    "size_style": "Size / Style",
}


def build_satellite_addendum() -> str:
    """
    Addendum al blocco universo, ATTIVO SOLO in modalità estesa. Risolve la
    contraddizione del prompt: il blocco core sopra vieta ETF settoriali/esteri,
    qui dichiariamo esplicitamente che — in modalità estesa — quei satellite
    SONO tradabili (override mirato), con sizing ridotto. Costruito da SATELLITE
    così non va mai in deriva rispetto alla cintura reale.
    """
    lines = [
        "MODALITÀ UNIVERSO ESTESO ATTIVA (EXPANDED_UNIVERSE):",
        "In aggiunta al core sopra, in questa modalità sono ANCHE TRADABILI i",
        "seguenti ETF 'satellite' — questo SUPERA il divieto del blocco sopra per",
        "questi specifici strumenti. Trattali con SIZING RIDOTTO (il risk profile",
        "applica un moltiplicatore satellite + un cap di esposizione aggregata) e",
        "solo se liquidi:",
    ]
    for cat, label in _SATELLITE_LABELS.items():
        names = SATELLITE.get(cat) or []
        if names:
            lines.append(f"  • {label}: {', '.join(names)}")
    lines.append("I nomi ATTIVI del momento (metriche + liquidità) sono nel blocco")
    lines.append("'UNIVERSO ESTESO — CANDIDATI'. Le azioni non-USA con suffisso estero")
    lines.append("(.MI/.PA/.DE/.L/.AS/.HK/.TO) restano NON supportate.")
    return "\n".join(lines)


def build_decision_universe_block() -> str:
    """
    Ritorna il blocco 'UNIVERSO INVESTIBILE' per il prompt del Decision Agent.

    Flag OFF (default): testo CORE invariato, byte-identico all'originale.
    Flag ON: core + addendum satellite, così il prompt NON è più contraddittorio
    (il core vieta i settoriali/esteri, l'addendum li riabilita in modalità estesa).
    NB: letto al momento della chiamata; in produzione il prompt è costruito a
    import di decision.py, quindi cambiare il flag richiede un restart (vedi runbook).
    """
    if expanded_universe_enabled():
        return DECISION_UNIVERSE_BLOCK + "\n\n" + build_satellite_addendum()
    return DECISION_UNIVERSE_BLOCK


# ════════════════════════════════════════════════════════════════════════
# CLASSIFICAZIONE TICKER — per la metrica di diversità (diversity.py).
# ════════════════════════════════════════════════════════════════════════
_CRYPTO_SET = set(CORE_CRYPTO) | set(EXCLUDED_CRYPTO)
_MEGA_SET = set(MEGA_CAP_EQUITY)
_COMMODITY_SET = set(COMMODITY_ETFS)


def is_crypto(ticker: str) -> bool:
    t = (ticker or "").upper()
    return t in _CRYPTO_SET or t.endswith("-USD") or t.endswith("USDT")


def is_mega_cap(ticker: str) -> bool:
    return (ticker or "").upper() in _MEGA_SET


def has_non_us_suffix(ticker: str) -> bool:
    t = (ticker or "").upper()
    return any(t.endswith(s.upper()) for s in NON_US_SUFFIXES)


def classify_ticker(ticker: str) -> Dict[str, object]:
    """
    Classifica un ticker in (asset_class, bucket, tier, is_mega) per la metrica
    di copertura. 'tier' è 'satellite' solo se il ticker è nella cintura
    SATELLITE (vuota in Step 1) — quindi oggi sempre 'core'/'unknown'.
    """
    t = (ticker or "").upper().strip()
    if not t:
        return {"ticker": t, "asset_class": "unknown", "bucket": "unknown",
                "tier": "unknown", "is_mega": False}

    # Cintura satellite (Step 2+)
    for cat, ts in SATELLITE.items():
        if t in ts:
            return {"ticker": t, "asset_class": "equity", "bucket": cat,
                    "tier": "satellite", "is_mega": False}

    if is_crypto(t):
        tradable = t in set(CORE_CRYPTO)
        return {"ticker": t, "asset_class": "crypto",
                "bucket": "crypto_core" if tradable else "crypto_excluded",
                "tier": "core", "is_mega": False}

    if t in _COMMODITY_SET:
        return {"ticker": t, "asset_class": "commodity", "bucket": "commodity_etf",
                "tier": "core", "is_mega": False}

    cat = ROTATION_TICKER_TO_CATEGORY.get(t)
    if cat:
        return {"ticker": t, "asset_class": "etf_or_equity", "bucket": cat,
                "tier": "core", "is_mega": is_mega_cap(t)}

    if has_non_us_suffix(t):
        return {"ticker": t, "asset_class": "equity", "bucket": "international_single",
                "tier": "core", "is_mega": False}

    # Default: azione USA singola (S&P 500 o altro nome equity).
    bucket = "mega_cap_equity" if is_mega_cap(t) else "core_equity"
    return {"ticker": t, "asset_class": "equity", "bucket": bucket,
            "tier": "core", "is_mega": is_mega_cap(t)}
