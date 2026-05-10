"""
Scenari storici reali del mondo crypto per il Simulator V2 Crypto.

Differenze vs scenarios.py (equity):
  - Universe ESCLUSIVAMENTE crypto (ticker formato yfinance "BTC-USD")
  - Compatibili con i 14 ticker crypto supportati: BTC, ETH, SOL, DOGE, AVAX,
    ADA, XRP, LTC, DOT, LINK, UNI, ATOM, MATIC, NEAR
  - Period_start scelto su eventi crypto-specifici (halving, ETF,
    hack, bull/bear cicli, depeg, exchange collapse)

Schema identico a scenarios.py:
  id, category, title, brief, period_start, period_end,
  asset_universe, headlines, market_data (price_t0 fallback),
  description_reveal
"""

CRYPTO_SCENARIOS = [
    # ─── BULL CYCLE / RALLY ────────────────────────────────────────────
    {
        "id": "crypto-bull-2021q1",
        "category": "bull_cycle",
        "title": "Q1 2021 — bull esplosivo, BTC verso 60k",
        "brief": "Mania retail su crypto, Tesla buying BTC, COIN listing imminente",
        "period_start": "2021-02-08",
        "period_end": "2021-04-15",
        "asset_universe": ["BTC-USD", "ETH-USD", "SOL-USD", "ADA-USD", "DOGE-USD",
                           "DOT-USD", "LINK-USD", "MATIC-USD"],
        "headlines": [
            "Tesla annuncia $1.5B di Bitcoin in treasury, Nasdaq salta",
            "Coinbase IPO direct listing date confermato Aprile 14",
            "Ether ATH $1,800 — DeFi TVL cresce a 50B$",
            "MicroStrategy aggiunge altri 1k BTC, totale 90k+",
            "Mastercard annuncia supporto crypto su rete",
            "BNY Mellon offrirà custody crypto a clientela istituzionale",
            "Retail interesse via Robinhood/eToro a livelli record",
            "Solana TVL DeFi accelera, ecosistema si espande rapidamente",
        ],
        "market_data": [
            {"ticker": "BTC-USD", "price_t0": 38500.0, "change_24h": 2.5, "change_7d": 12.0},
            {"ticker": "ETH-USD", "price_t0": 1650.0, "change_24h": 3.2, "change_7d": 18.0},
            {"ticker": "SOL-USD", "price_t0": 6.5, "change_24h": 5.0, "change_7d": 25.0},
            {"ticker": "ADA-USD", "price_t0": 0.85, "change_24h": 4.0, "change_7d": 15.0},
            {"ticker": "DOGE-USD", "price_t0": 0.058, "change_24h": 8.0, "change_7d": 30.0},
            {"ticker": "DOT-USD", "price_t0": 27.0, "change_24h": 2.0, "change_7d": 8.0},
            {"ticker": "LINK-USD", "price_t0": 32.0, "change_24h": 1.5, "change_7d": 5.0},
            {"ticker": "MATIC-USD", "price_t0": 0.16, "change_24h": 6.0, "change_7d": 35.0},
        ],
        "description_reveal": (
            "Feb-Apr 2021: BTC da 38.5k → 60k+ (peak Aprile), ETH da 1.6k → 2.4k+. "
            "SOL +400% nel periodo (da 6.5 a 30+), DOGE in mania retail (+1000%). "
            "Coinbase IPO il 14 Aprile = picco simbolico. Bull alimentato da "
            "treasury corporate (Tesla, MicroStrategy) e adozione retail."
        ),
    },
    {
        "id": "crypto-eth-merge-2022",
        "category": "bull_cycle",
        "title": "Settembre 2022 — Ethereum Merge in arrivo",
        "brief": "Transizione PoS imminente, narrazione ESG/yield attiva",
        "period_start": "2022-08-15",
        "period_end": "2022-10-30",
        "asset_universe": ["BTC-USD", "ETH-USD", "MATIC-USD", "ADA-USD",
                           "SOL-USD", "DOT-USD", "ATOM-USD", "LINK-USD"],
        "headlines": [
            "Ethereum Merge data confermata: 15 settembre 2022",
            "Vitalik conferma transizione completa a Proof-of-Stake",
            "Staking yield post-Merge stimato 5-7% APY",
            "Sell-the-news rumor circola, hedge funds shortano ETH",
            "Cardano e Solana come 'vincoli alternativi PoS' guadagnano attenzione",
            "Powell hawkish a Jackson Hole — risk assets sotto pressione",
            "Tesla scarica 75% delle proprie holding BTC",
            "MATIC vede sviluppo zkEVM, narrativa scaling Ethereum cresce",
        ],
        "market_data": [
            {"ticker": "BTC-USD", "price_t0": 24300.0, "change_24h": -1.5, "change_7d": -3.0},
            {"ticker": "ETH-USD", "price_t0": 1900.0, "change_24h": 0.5, "change_7d": 8.0},
            {"ticker": "MATIC-USD", "price_t0": 0.92, "change_24h": 1.0, "change_7d": 12.0},
            {"ticker": "ADA-USD", "price_t0": 0.50, "change_24h": -0.5, "change_7d": -2.0},
            {"ticker": "SOL-USD", "price_t0": 39.0, "change_24h": 0.8, "change_7d": 5.0},
            {"ticker": "DOT-USD", "price_t0": 8.50, "change_24h": -0.2, "change_7d": -1.5},
            {"ticker": "ATOM-USD", "price_t0": 12.0, "change_24h": 0.5, "change_7d": 3.0},
            {"ticker": "LINK-USD", "price_t0": 8.20, "change_24h": -0.5, "change_7d": -2.5},
        ],
        "description_reveal": (
            "Agosto-Ottobre 2022: classico 'sell the news'. ETH da 1.9k → 2.0k pre-Merge "
            "ma poi crash a 1.3k post-evento (Settembre 15). BTC sceso da 24k a 18k. "
            "Macro hawkish + delusione post-Merge hanno guidato la correzione. "
            "Solana e Cardano sotto-performance vs ETH a parità di rischio."
        ),
    },

    # ─── BEAR / CRASH ──────────────────────────────────────────────────
    {
        "id": "crypto-luna-collapse-may22",
        "category": "crash",
        "title": "Maggio 2022 — Terra Luna depeg + crash",
        "brief": "UST stablecoin perde il peg, contagion verso resto crypto",
        "period_start": "2022-05-08",
        "period_end": "2022-06-15",
        "asset_universe": ["BTC-USD", "ETH-USD", "SOL-USD", "AVAX-USD",
                           "DOT-USD", "ATOM-USD", "ADA-USD", "LINK-USD"],
        "headlines": [
            "UST stablecoin scende a $0.98, Anchor protocol sotto pressione",
            "Do Kwon (Terra) annuncia 'risposta coordinata' al depeg",
            "Luna in flash crash da $80 a $30 in 24h",
            "Curve 3pool flussi 1B$ in uscita verso USDC/USDT",
            "Genesis trading sospende withdrawals temporaneamente",
            "Janet Yellen pressa Congresso su stablecoin regulation",
            "Tether mostra impatto minimo, USDC peg saldo",
            "BTC perde 30k support, sell-off cross-asset accelera",
        ],
        "market_data": [
            {"ticker": "BTC-USD", "price_t0": 33500.0, "change_24h": -3.5, "change_7d": -8.0},
            {"ticker": "ETH-USD", "price_t0": 2400.0, "change_24h": -4.0, "change_7d": -12.0},
            {"ticker": "SOL-USD", "price_t0": 65.0, "change_24h": -5.0, "change_7d": -15.0},
            {"ticker": "AVAX-USD", "price_t0": 35.0, "change_24h": -6.0, "change_7d": -18.0},
            {"ticker": "DOT-USD", "price_t0": 11.0, "change_24h": -4.5, "change_7d": -10.0},
            {"ticker": "ATOM-USD", "price_t0": 13.5, "change_24h": -3.5, "change_7d": -8.0},
            {"ticker": "ADA-USD", "price_t0": 0.48, "change_24h": -3.0, "change_7d": -9.0},
            {"ticker": "LINK-USD", "price_t0": 7.50, "change_24h": -4.0, "change_7d": -11.0},
        ],
        "description_reveal": (
            "Maggio-Giugno 2022: catena dominoes. Luna da $80 → $0.0001 (essentially zero) "
            "in una settimana. Contagion: BTC da 33k → 17.5k (Giugno), ETH da 2.4k → 880, "
            "SOL da 65 → 24. 3AC (Three Arrows Capital) bancarotta poco dopo, Celsius "
            "freeze withdrawals, Voyager bankruptcy. Bear iniziato che durerà 18 mesi."
        ),
    },
    {
        "id": "crypto-ftx-collapse-nov22",
        "category": "crash",
        "title": "Novembre 2022 — FTX exchange collapse",
        "brief": "Insolvenza secondo exchange globale, panic withdrawals ovunque",
        "period_start": "2022-11-06",
        "period_end": "2022-12-15",
        "asset_universe": ["BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD",
                           "AVAX-USD", "MATIC-USD", "LINK-USD", "DOT-USD"],
        "headlines": [
            "CoinDesk pubblica balance sheet Alameda — 50% in FTT illiquido",
            "Binance annuncia liquidazione completa posizione FTT",
            "FTX international sospende withdrawals — 'liquidity issues'",
            "SBF cerca 8B$ bailout in 48h, fallisce",
            "Binance offerta acquisto FTX, ritirata 24h dopo",
            "FTX dichiara Chapter 11 bankruptcy, $32B valutazione → 0",
            "Genesis, BlockFi sospensione withdrawals — contagion",
            "BTC -25% in una settimana, panic generalizzato",
            "SOL -60% (FTX/Alameda erano holders maggiori)",
            "Stablecoin USDD, USDN sotto pressione brevemente",
        ],
        "market_data": [
            {"ticker": "BTC-USD", "price_t0": 21000.0, "change_24h": -3.0, "change_7d": -1.5},
            {"ticker": "ETH-USD", "price_t0": 1620.0, "change_24h": -2.5, "change_7d": 2.0},
            {"ticker": "SOL-USD", "price_t0": 32.0, "change_24h": -4.5, "change_7d": -8.0},
            {"ticker": "DOGE-USD", "price_t0": 0.12, "change_24h": 5.0, "change_7d": 80.0},
            {"ticker": "AVAX-USD", "price_t0": 17.0, "change_24h": -3.0, "change_7d": -5.0},
            {"ticker": "MATIC-USD", "price_t0": 0.95, "change_24h": -1.5, "change_7d": -3.0},
            {"ticker": "LINK-USD", "price_t0": 8.00, "change_24h": -2.0, "change_7d": -4.0},
            {"ticker": "DOT-USD", "price_t0": 6.50, "change_24h": -2.5, "change_7d": -5.0},
        ],
        "description_reveal": (
            "Novembre-Dicembre 2022: FTX collassa in 5 giorni (6→11 Nov). BTC da 21k → 15.5k. "
            "SOL da 32 → 8 (-75% in 30 giorni, era proxy diretto FTX/Alameda). ETH da 1.6k → 1.2k. "
            "Capitulation totale, capitulation low del bear cycle. Da qui partirà il rally di "
            "ricostruzione 2023."
        ),
    },

    # ─── REGULATORY / ETF / NARRATIVE ──────────────────────────────────
    {
        "id": "crypto-btc-etf-jan24",
        "category": "regulatory_event",
        "title": "Gennaio 2024 — Bitcoin Spot ETF approvati SEC",
        "brief": "Decisione SEC su 11 ETF spot, decisione storica dopo decade di rifiuti",
        "period_start": "2024-01-08",
        "period_end": "2024-02-29",
        "asset_universe": ["BTC-USD", "ETH-USD", "SOL-USD", "AVAX-USD",
                           "LINK-USD", "MATIC-USD", "DOT-USD", "DOGE-USD"],
        "headlines": [
            "SEC scheduled meeting 10 Gennaio per decisione finale spot BTC ETF",
            "BlackRock, Fidelity, ARK Invest tra i 11 applicants",
            "Gary Gensler dissent sospetto, vote split previsto 3-2",
            "Twitter SEC hackerato, fake announcement causa flash crash 7%",
            "10 Gennaio: SEC approva tutti gli 11 ETF spot Bitcoin",
            "Volume primo giorno trading $4.6B, record asset class",
            "GBTC outflows iniziano (premium → expectation arbitrage)",
            "ETH spot ETF speculation parte, target maggio 2024",
        ],
        "market_data": [
            {"ticker": "BTC-USD", "price_t0": 46500.0, "change_24h": 1.2, "change_7d": 3.0},
            {"ticker": "ETH-USD", "price_t0": 2280.0, "change_24h": 0.8, "change_7d": 5.0},
            {"ticker": "SOL-USD", "price_t0": 96.0, "change_24h": -1.5, "change_7d": -3.0},
            {"ticker": "AVAX-USD", "price_t0": 38.0, "change_24h": 1.0, "change_7d": 2.0},
            {"ticker": "LINK-USD", "price_t0": 14.5, "change_24h": 0.5, "change_7d": 1.0},
            {"ticker": "MATIC-USD", "price_t0": 0.83, "change_24h": -0.8, "change_7d": -1.5},
            {"ticker": "DOT-USD", "price_t0": 7.80, "change_24h": -0.5, "change_7d": -2.0},
            {"ticker": "DOGE-USD", "price_t0": 0.082, "change_24h": -1.0, "change_7d": -2.5},
        ],
        "description_reveal": (
            "Gennaio-Febbraio 2024: classic 'buy the rumor sell the news' iniziale (BTC -10% "
            "subito post-approval, da 46.5k → 41k). Poi rally violento da metà febbraio: "
            "BTC verso 60k+ entro fine mese. ETF flussi netti positivi 1B$/giorno hanno "
            "guidato il move. ETH followed (+30%). SOL, AVAX outperform vs altri alt."
        ),
    },
    {
        "id": "crypto-china-ban-may21",
        "category": "regulatory_event",
        "title": "Maggio 2021 — China crypto ban + Tesla retract",
        "brief": "Doppio shock: ban PBoC + Musk no-BTC Tesla payments",
        "period_start": "2021-05-12",
        "period_end": "2021-07-01",
        "asset_universe": ["BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD",
                           "ADA-USD", "DOT-USD", "LINK-USD", "MATIC-USD"],
        "headlines": [
            "Elon Musk: Tesla sospende pagamenti in BTC per concerns ambientali",
            "Energy use BTC 'insane' tweet causa flash crash -15%",
            "PBOC reitera divieto attività crypto in Cina",
            "Mining ban annunciato in Sichuan, Inner Mongolia",
            "Hashrate BTC -50% in 6 settimane mentre miners migrano",
            "Difficulty adjustment massimo storico down 28%",
            "Saylor reitera HODL, MicroStrategy compra dip",
            "DeFi summer fade, TVL crolla con ETH",
        ],
        "market_data": [
            {"ticker": "BTC-USD", "price_t0": 49000.0, "change_24h": -8.0, "change_7d": -15.0},
            {"ticker": "ETH-USD", "price_t0": 3500.0, "change_24h": -10.0, "change_7d": -18.0},
            {"ticker": "SOL-USD", "price_t0": 28.0, "change_24h": -12.0, "change_7d": -22.0},
            {"ticker": "DOGE-USD", "price_t0": 0.45, "change_24h": -15.0, "change_7d": -30.0},
            {"ticker": "ADA-USD", "price_t0": 1.65, "change_24h": -8.0, "change_7d": -12.0},
            {"ticker": "DOT-USD", "price_t0": 28.0, "change_24h": -10.0, "change_7d": -15.0},
            {"ticker": "LINK-USD", "price_t0": 38.0, "change_24h": -9.0, "change_7d": -14.0},
            {"ticker": "MATIC-USD", "price_t0": 1.55, "change_24h": -11.0, "change_7d": -20.0},
        ],
        "description_reveal": (
            "Mid-Maggio – Luglio 2021: BTC da 49k → 30k entro Luglio (-40%). ETH da 3.5k → 1.8k "
            "(-49%). DOGE collassa -75% dal peak Maggio. SOL fa +400% da fine giugno (era $25 → "
            "ATH $260 a Novembre). Bull cycle non era finito: prima major correction, poi rally "
            "verso ATH BTC $69k Novembre 2021."
        ),
    },

    # ─── SIDEWAYS / RANGE ──────────────────────────────────────────────
    {
        "id": "crypto-summer-2024",
        "category": "sideways",
        "title": "Estate 2024 — range-bound dopo rally Q1",
        "brief": "BTC consolida 60-70k, alts in ritirata, scarso momentum",
        "period_start": "2024-06-01",
        "period_end": "2024-08-15",
        "asset_universe": ["BTC-USD", "ETH-USD", "SOL-USD", "AVAX-USD",
                           "LINK-USD", "DOT-USD", "ATOM-USD", "NEAR-USD"],
        "headlines": [
            "BTC oscilla 60-70k, volume in declino dal pico Marzo",
            "Mt. Gox repayment trustee invia avviso creditori — sell-off rumor",
            "ETH spot ETF approvati ma flussi sotto le aspettative",
            "Powell colomba a Jackson Hole — rate cut atteso settembre",
            "Solana DePIN narrative pump-and-dump pattern",
            "DEFI TVL stabile, no major hack o exploit",
            "Hash rate BTC ATH, miner rivenue under pressure post-halving",
            "Gli ETF BTC accumulano modestamente, no inflows record",
        ],
        "market_data": [
            {"ticker": "BTC-USD", "price_t0": 67500.0, "change_24h": 0.5, "change_7d": 1.2},
            {"ticker": "ETH-USD", "price_t0": 3500.0, "change_24h": 0.3, "change_7d": -1.0},
            {"ticker": "SOL-USD", "price_t0": 145.0, "change_24h": 1.0, "change_7d": -3.0},
            {"ticker": "AVAX-USD", "price_t0": 27.0, "change_24h": -0.5, "change_7d": -5.0},
            {"ticker": "LINK-USD", "price_t0": 14.0, "change_24h": 0.0, "change_7d": -2.0},
            {"ticker": "DOT-USD", "price_t0": 6.50, "change_24h": -1.0, "change_7d": -4.0},
            {"ticker": "ATOM-USD", "price_t0": 7.80, "change_24h": 0.2, "change_7d": -1.5},
            {"ticker": "NEAR-USD", "price_t0": 5.20, "change_24h": 1.5, "change_7d": 3.0},
        ],
        "description_reveal": (
            "Giugno-Agosto 2024: range-bound. BTC oscilla 53k-71k, chiude periodo a 58k. "
            "ETH cala da 3.5k a 2.4k post-ETF launch (sell the news classico). "
            "SOL da 145 a 130 (resilienza relativa). Alts in generale -20-30%. Settembre "
            "rate-cut Fed sarà il catalyst per la ripresa."
        ),
    },

    # ─── CRYPTO-SPECIFIC SHOCK ─────────────────────────────────────────
    {
        "id": "crypto-binance-doj-feb24",
        "category": "crash",
        "title": "Novembre 2023 — DOJ settlement Binance + CZ steps down",
        "brief": "$4B settlement, CZ resigns, regulatory overhang globale",
        "period_start": "2023-11-21",
        "period_end": "2023-12-31",
        "asset_universe": ["BTC-USD", "ETH-USD", "SOL-USD", "AVAX-USD",
                           "DOGE-USD", "LINK-USD", "DOT-USD", "MATIC-USD"],
        "headlines": [
            "DOJ annuncia settlement $4.3B con Binance per AML violations",
            "CZ Zhao si dimette CEO immediato, riconosce colpe penali",
            "Binance non perde license operativa, deal sopravvivenza",
            "BTC reagisce calmo: -2% iniziale poi recupera",
            "Tether mostra forza, USDC outflows minori",
            "BTC ETF approval atteso 'imminentemente' — Gary Gensler dovish",
            "Saylor prezza target $1M BTC entro 2030",
            "Coinbase dichiara compliance superiore vs Binance — capital inflow",
        ],
        "market_data": [
            {"ticker": "BTC-USD", "price_t0": 37300.0, "change_24h": -1.5, "change_7d": 2.0},
            {"ticker": "ETH-USD", "price_t0": 2050.0, "change_24h": -1.0, "change_7d": 4.0},
            {"ticker": "SOL-USD", "price_t0": 60.0, "change_24h": 1.0, "change_7d": 15.0},
            {"ticker": "AVAX-USD", "price_t0": 23.0, "change_24h": 2.0, "change_7d": 18.0},
            {"ticker": "DOGE-USD", "price_t0": 0.082, "change_24h": -0.5, "change_7d": 8.0},
            {"ticker": "LINK-USD", "price_t0": 14.5, "change_24h": 0.5, "change_7d": 12.0},
            {"ticker": "DOT-USD", "price_t0": 5.50, "change_24h": -0.5, "change_7d": 5.0},
            {"ticker": "MATIC-USD", "price_t0": 0.86, "change_24h": -1.0, "change_7d": 3.0},
        ],
        "description_reveal": (
            "Fine Novembre - Dicembre 2023: il settlement Binance NON è stato il crash "
            "atteso. BTC da 37.3k → 42k entro fine Dicembre (anticipa ETF approval). "
            "SOL +60% nel periodo (rinascita narrativa post-FTX), AVAX +50%. ETH da 2.05k "
            "→ 2.4k. Lezione: bad regulatory news già priced in se asset ha basato; rotation "
            "verso 'Solana ecosystem' inizia in questo periodo."
        ),
    },
]


def get_crypto_scenario_by_id(scenario_id: str) -> dict | None:
    for s in CRYPTO_SCENARIOS:
        if s["id"] == scenario_id:
            return s
    return None


def get_random_crypto_scenario(category: str | None = None) -> dict | None:
    import random
    pool = (CRYPTO_SCENARIOS if not category
            else [s for s in CRYPTO_SCENARIOS if s["category"] == category])
    return random.choice(pool) if pool else None


def list_crypto_scenarios(category: str | None = None) -> list[dict]:
    if not category:
        return list(CRYPTO_SCENARIOS)
    return [s for s in CRYPTO_SCENARIOS if s["category"] == category]


def crypto_scenario_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for s in CRYPTO_SCENARIOS:
        c = s.get("category", "other")
        counts[c] = counts.get(c, 0) + 1
    return counts
