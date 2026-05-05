"""
Library di scenari storici per il Simulator.

Ogni scenario è una situazione di mercato reale. L'agente vede headline, prezzi
e dati al momento T0 — ma NON il periodo storico (rivelato solo nel risultato).

Schema scenario:
  - id: stringa univoca
  - category: 'normale' | 'geopolitico' | 'macro' | 'crash_rally'
  - title: nome interno del scenario (per UI selezione)
  - brief: descrizione neutra non rivelativa ("Mercato USA, alta volatilità...")
  - period_start / period_end: ISO date del periodo reale (rivelato solo a posteriori)
  - asset_universe: ticker che l'agente può scegliere
  - headlines: lista di {date, text} - solo testo, no date mostrate al modello
  - market_data: list of {ticker, price_t0, change_24h, change_7d}
  - description_reveal: cosa è davvero successo nel periodo (mostrato in Result)
  - multi_step_updates (opzionale): per multi-step, lista di update progressivi
"""

# Libreria iniziale di 16 scenari (4 per categoria). Periodi reali simulati;
# dati di prezzo realistici ma indicativi — il sistema in produzione può
# arricchirli con yfinance storical. Per la fase 1, sono hard-coded.

SCENARIOS = [
    # ─── NORMALE (4) ───
    {
        "id": "norm-2024q2-tech-rally",
        "category": "normale",
        "title": "Q2 tech-led rally, no eventi macro forti",
        "brief": "Mercato USA, momentum tech moderato, dispersione settoriale, no eventi macro improvvisi",
        "period_start": "2024-04-15",
        "period_end": "2024-07-15",
        "asset_universe": ["MSFT", "GOOGL", "NVDA", "META", "AAPL", "AMZN", "JPM", "XOM", "GLD", "TLT"],
        "headlines": [
            "Tech earnings: NVDA guidance above consensus on AI demand",
            "Fed minutes: 'higher for longer' tone reaffirmed",
            "Dollar index stable around 105.0 — emerging markets neutral",
            "10-year yield trading 4.2-4.4% range, no breakouts",
            "VIX in 12-15 zone, low realized volatility",
            "S&P breadth widening: financials and industrials catch up",
        ],
        "market_data": [
            {"ticker": "MSFT", "price_t0": 425.0, "change_24h": 0.8, "change_7d": 2.3},
            {"ticker": "GOOGL", "price_t0": 175.0, "change_24h": 1.1, "change_7d": 3.1},
            {"ticker": "NVDA", "price_t0": 880.0, "change_24h": 2.5, "change_7d": 8.4},
            {"ticker": "META", "price_t0": 510.0, "change_24h": 0.4, "change_7d": 1.8},
            {"ticker": "AAPL", "price_t0": 188.0, "change_24h": 0.2, "change_7d": -0.8},
            {"ticker": "JPM", "price_t0": 198.0, "change_24h": 0.6, "change_7d": 1.5},
            {"ticker": "XOM", "price_t0": 117.0, "change_24h": -0.4, "change_7d": -1.2},
            {"ticker": "GLD", "price_t0": 217.0, "change_24h": 0.3, "change_7d": 1.0},
            {"ticker": "TLT", "price_t0": 91.0, "change_24h": -0.1, "change_7d": -0.6},
        ],
        "description_reveal": (
            "Aprile-luglio 2024: tech mega-cap continua trend rialzista guidato da NVDA/AI. "
            "S&P +5% in 3 mesi, NVDA +35%. Fed mantiene tassi fermi, niente shock. "
            "Energy settore peggiore (-3%), tech migliore (+12%)."
        ),
    },
    {
        "id": "norm-2023q3-mixed",
        "category": "normale",
        "title": "Estate, range-bound, banche regional",
        "brief": "Mercato laterale, attenzione su trimestrali consumer, banche regionali sotto i riflettori",
        "period_start": "2023-07-10",
        "period_end": "2023-10-10",
        "asset_universe": ["JPM", "BAC", "WFC", "USB", "GS", "MS", "C", "AAPL", "WMT", "COST"],
        "headlines": [
            "Q2 earnings: large banks beat, regional banks soft on NIM compression",
            "Consumer spending resilient — WMT raises guidance",
            "Fed pause expected at September meeting",
            "Credit card delinquency at 3-year high but still below pre-pandemic",
            "Office REITs continue struggle — CRE refinancing wall ahead",
        ],
        "market_data": [
            {"ticker": "JPM", "price_t0": 152.0, "change_24h": 0.3, "change_7d": 0.8},
            {"ticker": "BAC", "price_t0": 30.5, "change_24h": -0.5, "change_7d": -1.5},
            {"ticker": "WFC", "price_t0": 44.0, "change_24h": 0.2, "change_7d": -0.6},
            {"ticker": "GS", "price_t0": 350.0, "change_24h": 0.8, "change_7d": 2.1},
            {"ticker": "AAPL", "price_t0": 191.0, "change_24h": 0.0, "change_7d": -0.3},
            {"ticker": "WMT", "price_t0": 156.0, "change_24h": 1.2, "change_7d": 3.5},
            {"ticker": "COST", "price_t0": 545.0, "change_24h": 0.4, "change_7d": 1.1},
        ],
        "description_reveal": (
            "Lug-Ott 2023: range-bound. Banche regionali continuano a scontare crisi NIM. "
            "Fed pausa a settembre. Consumer staples (WMT, COST) outperform. "
            "Tech mega-cap consolida dopo rally H1."
        ),
    },
    {
        "id": "norm-2024q4-rotation",
        "category": "normale",
        "title": "Rotazione settoriale, small-cap ripartono",
        "brief": "Cambio di leadership: small-cap e value sovraperformano momentum tech",
        "period_start": "2024-10-01",
        "period_end": "2025-01-01",
        "asset_universe": ["IWM", "MSFT", "META", "JPM", "XOM", "CAT", "DE", "AAPL", "GOOGL", "TSLA"],
        "headlines": [
            "Russell 2000 outperforms S&P by 5pp in 4 weeks",
            "Soft landing thesis gaining traction — yields stable",
            "Small-cap valuations at multi-year discount vs S&P",
            "CAT, DE earnings strong — capex cycle picking up",
        ],
        "market_data": [
            {"ticker": "MSFT", "price_t0": 412.0, "change_24h": -0.5, "change_7d": -2.1},
            {"ticker": "META", "price_t0": 575.0, "change_24h": 0.1, "change_7d": 1.0},
            {"ticker": "JPM", "price_t0": 222.0, "change_24h": 0.8, "change_7d": 3.2},
            {"ticker": "XOM", "price_t0": 119.0, "change_24h": 0.3, "change_7d": 0.5},
            {"ticker": "CAT", "price_t0": 388.0, "change_24h": 1.5, "change_7d": 4.8},
            {"ticker": "DE", "price_t0": 405.0, "change_24h": 1.1, "change_7d": 3.5},
            {"ticker": "TSLA", "price_t0": 245.0, "change_24h": -2.1, "change_7d": -5.5},
        ],
        "description_reveal": (
            "Ott 2024-Gen 2025: rotazione classica fine ciclo. Small-cap (IWM) +12%, "
            "industriali (CAT/DE) +10%, mentre tech mega-cap soft (-3%). Soft landing in atto."
        ),
    },
    {
        "id": "norm-2025q1-stable",
        "category": "normale",
        "title": "Inverno tranquillo, focus AI capex",
        "brief": "Mercato stabile, narrativa AI capex prosegue, no shock",
        "period_start": "2025-01-15",
        "period_end": "2025-04-15",
        "asset_universe": ["NVDA", "AVGO", "AMD", "MSFT", "GOOGL", "META", "AAPL", "TSLA", "AMZN", "ORCL"],
        "headlines": [
            "Hyperscaler capex guidance for 2025: +35% YoY",
            "AVGO reports custom AI chip backlog",
            "AAPL services revenue at all-time high",
            "AMD Q4 datacenter revenue accelerates",
        ],
        "market_data": [
            {"ticker": "NVDA", "price_t0": 135.0, "change_24h": 1.8, "change_7d": 6.0},
            {"ticker": "AVGO", "price_t0": 220.0, "change_24h": 1.2, "change_7d": 4.5},
            {"ticker": "AMD", "price_t0": 122.0, "change_24h": 2.4, "change_7d": 8.0},
            {"ticker": "MSFT", "price_t0": 420.0, "change_24h": 0.5, "change_7d": 1.8},
            {"ticker": "GOOGL", "price_t0": 195.0, "change_24h": 0.7, "change_7d": 2.3},
        ],
        "description_reveal": "Q1 2025 stabile, AI capex domina, NVDA +18%, AVGO +15%, AMD +25%.",
    },

    # ─── GEOPOLITICO (4) ───
    {
        "id": "geo-russia-ukraine-feb22",
        "category": "geopolitico",
        "title": "Conflitto in Europa orientale all'inizio",
        "brief": "Crisi militare improvvisa in Europa, sanzioni in arrivo, oil & defense in focus",
        "period_start": "2022-02-22",
        "period_end": "2022-05-22",
        "asset_universe": ["XOM", "CVX", "LMT", "RTX", "NOC", "BA", "GLD", "SPY", "QQQ", "DBA"],
        "headlines": [
            "Major military operation launched in Eastern Europe",
            "Western governments coordinate sanctions package",
            "Brent crude jumps above $100/bbl on supply fears",
            "European gas prices triple intraday",
            "Defense contractors trading higher pre-market",
            "Wheat futures hit limit-up multiple sessions",
        ],
        "market_data": [
            {"ticker": "XOM", "price_t0": 78.0, "change_24h": 1.5, "change_7d": 3.8},
            {"ticker": "CVX", "price_t0": 138.0, "change_24h": 1.2, "change_7d": 3.2},
            {"ticker": "LMT", "price_t0": 388.0, "change_24h": 4.5, "change_7d": 9.5},
            {"ticker": "RTX", "price_t0": 95.0, "change_24h": 3.2, "change_7d": 7.5},
            {"ticker": "NOC", "price_t0": 410.0, "change_24h": 5.1, "change_7d": 12.0},
            {"ticker": "GLD", "price_t0": 178.0, "change_24h": 1.8, "change_7d": 4.2},
            {"ticker": "SPY", "price_t0": 420.0, "change_24h": -2.5, "change_7d": -5.5},
            {"ticker": "DBA", "price_t0": 22.0, "change_24h": 2.4, "change_7d": 6.5},
        ],
        "description_reveal": (
            "Russia invade l'Ucraina (24 feb 2022). Energy +25% in 2 mesi, Defense +30% (LMT, NOC), "
            "Gold +12%, S&P -8% (correzione poi recupero parziale). Wheat (DBA) +35% in 3 mesi."
        ),
    },
    {
        "id": "geo-iran-tensions",
        "category": "geopolitico",
        "title": "Tensioni Medio Oriente, oil shock immediato",
        "brief": "Escalation regionale Medio Oriente, blocco rotte commerciali oil-based",
        "period_start": "2024-04-13",
        "period_end": "2024-07-13",
        "asset_universe": ["XOM", "CVX", "OXY", "USO", "LMT", "RTX", "GLD", "SPY", "EWZ", "QQQ"],
        "headlines": [
            "Direct exchange of strikes between regional powers",
            "Strait of Hormuz traffic disrupted — insurance premiums double",
            "Brent up 8% intraday, WTI follows",
            "Defense contractors halt cease earnings buybacks",
        ],
        "market_data": [
            {"ticker": "XOM", "price_t0": 122.0, "change_24h": 2.2, "change_7d": 4.5},
            {"ticker": "CVX", "price_t0": 165.0, "change_24h": 1.8, "change_7d": 3.2},
            {"ticker": "OXY", "price_t0": 68.0, "change_24h": 2.5, "change_7d": 5.0},
            {"ticker": "USO", "price_t0": 82.0, "change_24h": 6.5, "change_7d": 12.0},
            {"ticker": "LMT", "price_t0": 460.0, "change_24h": 1.5, "change_7d": 4.2},
            {"ticker": "GLD", "price_t0": 220.0, "change_24h": 1.2, "change_7d": 3.5},
            {"ticker": "SPY", "price_t0": 510.0, "change_24h": -1.5, "change_7d": -2.8},
        ],
        "description_reveal": (
            "Apr-Lug 2024: tensioni Iran-Israele, attacchi diretti reciproci ad aprile. "
            "Oil +15% iniziale poi mean-reversion (-8%). Defense flat, S&P -3% poi recupero."
        ),
    },
    {
        "id": "geo-china-taiwan",
        "category": "geopolitico",
        "title": "Tensioni Asia-Pacifico, semi e shipping nel mirino",
        "brief": "Crisi diplomatica regione Asia, supply chain semi sotto stress, USD-CNY volatilità",
        "period_start": "2022-08-01",
        "period_end": "2022-11-01",
        "asset_universe": ["TSM", "AVGO", "INTC", "AMD", "NVDA", "QCOM", "AMAT", "FXI", "EWT", "SPY"],
        "headlines": [
            "Senior US officials visit Asian island nation, regional power conducts naval drills",
            "TSM ADR drops 5% premarket — supply chain concerns",
            "Yuan weakens past 6.85 vs dollar",
            "Pentagon orders strategic semiconductor stockpile review",
        ],
        "market_data": [
            {"ticker": "TSM", "price_t0": 95.0, "change_24h": -3.5, "change_7d": -7.0},
            {"ticker": "AVGO", "price_t0": 540.0, "change_24h": -1.5, "change_7d": -3.0},
            {"ticker": "INTC", "price_t0": 35.0, "change_24h": -1.0, "change_7d": -2.5},
            {"ticker": "AMD", "price_t0": 92.0, "change_24h": -2.5, "change_7d": -5.0},
            {"ticker": "NVDA", "price_t0": 175.0, "change_24h": -2.0, "change_7d": -4.5},
            {"ticker": "FXI", "price_t0": 30.5, "change_24h": -2.5, "change_7d": -5.5},
            {"ticker": "SPY", "price_t0": 410.0, "change_24h": -0.8, "change_7d": -1.5},
        ],
        "description_reveal": (
            "Ago-Nov 2022: Pelosi visita Taiwan ad agosto. Semi -15% (TSM -20%), recupero Q4. "
            "FXI -12%. NVDA inizia il tonfo strutturale post-AI hype."
        ),
    },
    {
        "id": "geo-elections-uncertainty",
        "category": "geopolitico",
        "title": "Elezioni USA contestate, volatilità politica",
        "brief": "Periodo elettorale ad alta intensità, mercato anticipa policy shift",
        "period_start": "2024-09-01",
        "period_end": "2024-12-01",
        "asset_universe": ["SPY", "QQQ", "TSLA", "GLD", "VIX", "DJT", "XLE", "XLF", "XLV", "BTC-USD"],
        "headlines": [
            "Polls tighten in key swing states",
            "Tariff policy debate dominates earnings calls",
            "Tax-cut vs corporate-tax narrative diverges between candidates",
            "VIX futures curve in backwardation",
        ],
        "market_data": [
            {"ticker": "SPY", "price_t0": 555.0, "change_24h": -0.5, "change_7d": -1.0},
            {"ticker": "TSLA", "price_t0": 240.0, "change_24h": 1.5, "change_7d": 3.5},
            {"ticker": "GLD", "price_t0": 245.0, "change_24h": 0.5, "change_7d": 2.0},
            {"ticker": "XLE", "price_t0": 88.0, "change_24h": -0.8, "change_7d": -1.5},
            {"ticker": "XLF", "price_t0": 45.0, "change_24h": 0.3, "change_7d": 0.8},
            {"ticker": "BTC-USD", "price_t0": 62000.0, "change_24h": 2.5, "change_7d": 8.0},
        ],
        "description_reveal": (
            "Set-Dic 2024: Trump rieletto. Post-election rally tech (TSLA +60%), banche (+10%), "
            "BTC +50%. Energy mixed. Gold flat. Volatility comprime dopo l'esito."
        ),
    },

    # ─── MACRO (4) ───
    {
        "id": "macro-fed-pivot-2023",
        "category": "macro",
        "title": "Pivot Fed atteso, banche sotto stress",
        "brief": "Banche centrali pronte al pivot, curve dei rendimenti invertita, stress regionale",
        "period_start": "2023-03-01",
        "period_end": "2023-06-01",
        "asset_universe": ["JPM", "BAC", "USB", "ZION", "KRE", "TLT", "GLD", "SPY", "QQQ", "XLF"],
        "headlines": [
            "Major regional bank receives emergency liquidity",
            "Fed considers emergency rate cut",
            "Yield curve 2y-10y inverts further to -50bp",
            "Bank deposits flow to money market funds",
        ],
        "market_data": [
            {"ticker": "JPM", "price_t0": 138.0, "change_24h": -2.0, "change_7d": -5.5},
            {"ticker": "BAC", "price_t0": 32.0, "change_24h": -3.5, "change_7d": -8.5},
            {"ticker": "USB", "price_t0": 39.0, "change_24h": -5.5, "change_7d": -15.0},
            {"ticker": "ZION", "price_t0": 30.0, "change_24h": -8.5, "change_7d": -25.0},
            {"ticker": "KRE", "price_t0": 51.0, "change_24h": -7.0, "change_7d": -18.0},
            {"ticker": "TLT", "price_t0": 105.0, "change_24h": 2.5, "change_7d": 6.5},
            {"ticker": "GLD", "price_t0": 184.0, "change_24h": 1.5, "change_7d": 4.0},
            {"ticker": "SPY", "price_t0": 395.0, "change_24h": -1.5, "change_7d": -3.5},
        ],
        "description_reveal": (
            "Mar-Giu 2023: SVB collapse a marzo. KRE -25% in 2 settimane, poi recupero parziale. "
            "Treasuries rally (TLT +8%). Gold +10%. Mega-cap tech outperform (QQQ +15%)."
        ),
    },
    {
        "id": "macro-inflation-shock-2022",
        "category": "macro",
        "title": "Inflazione fuori controllo, Fed aggressiva",
        "brief": "CPI sopra le attese 9 mesi consecutivi, Fed accelera tightening, growth in difficoltà",
        "period_start": "2022-05-01",
        "period_end": "2022-08-01",
        "asset_universe": ["TLT", "GLD", "USO", "DBA", "XLE", "QQQ", "ARKK", "TSLA", "MSFT", "JPM"],
        "headlines": [
            "CPI prints 9.1% YoY — 40-year high",
            "Fed delivers 75bp hike, signals more",
            "10y yield breaks above 3.5%",
            "Growth stocks rate-sensitive — ARKK -30% YTD",
        ],
        "market_data": [
            {"ticker": "TLT", "price_t0": 113.0, "change_24h": -1.2, "change_7d": -3.5},
            {"ticker": "USO", "price_t0": 78.0, "change_24h": 1.5, "change_7d": 4.0},
            {"ticker": "ARKK", "price_t0": 50.0, "change_24h": -3.5, "change_7d": -8.5},
            {"ticker": "TSLA", "price_t0": 280.0, "change_24h": -2.5, "change_7d": -6.0},
            {"ticker": "MSFT", "price_t0": 270.0, "change_24h": -1.0, "change_7d": -3.0},
            {"ticker": "GLD", "price_t0": 170.0, "change_24h": 0.3, "change_7d": -1.5},
        ],
        "description_reveal": (
            "Mag-Ago 2022: rate-shock estivo. TLT -10%, ARKK -25%, TSLA -25%, MSFT -8%. "
            "Energy outperform (USO +18%). Commodity peak metà giugno."
        ),
    },
    {
        "id": "macro-disinflation-2024",
        "category": "macro",
        "title": "Disinflazione conferma, tagli tassi prezzati",
        "brief": "CPI continua a scendere, mercato prezza 6 tagli in 12 mesi, growth e duration premiati",
        "period_start": "2024-01-15",
        "period_end": "2024-04-15",
        "asset_universe": ["TLT", "QQQ", "ARKK", "TSLA", "NVDA", "GS", "JPM", "GLD", "USD", "EEM"],
        "headlines": [
            "CPI prints 3.1% YoY — fastest disinflation in 40 years",
            "Fed dot-plot pencils in 3 cuts for 2024",
            "Long bonds rally — TLT breaks above 100",
            "Growth/Value spread widens to 18-month high",
        ],
        "market_data": [
            {"ticker": "TLT", "price_t0": 95.0, "change_24h": 1.5, "change_7d": 3.5},
            {"ticker": "QQQ", "price_t0": 410.0, "change_24h": 1.0, "change_7d": 3.0},
            {"ticker": "ARKK", "price_t0": 48.0, "change_24h": 2.5, "change_7d": 6.0},
            {"ticker": "TSLA", "price_t0": 195.0, "change_24h": 1.5, "change_7d": -2.5},
            {"ticker": "NVDA", "price_t0": 720.0, "change_24h": 2.5, "change_7d": 8.5},
        ],
        "description_reveal": (
            "Gen-Apr 2024: rally disinflation parziale. NVDA +30%, TLT -3% (yields back up). "
            "Mercato si rende conto che tagli arriveranno tardi. ARKK volatile."
        ),
    },
    {
        "id": "macro-eu-recession-fears",
        "category": "macro",
        "title": "Eurozona in recessione tecnica",
        "brief": "Dati PMI in contrazione, Bund yield in calo, USD forza relativa",
        "period_start": "2023-09-01",
        "period_end": "2023-12-01",
        "asset_universe": ["EWG", "EWQ", "FXE", "DAX", "VGK", "SPY", "TLT", "GLD", "XLE", "EMB"],
        "headlines": [
            "Eurozone Q3 GDP contracts 0.1%",
            "ECB hints at rate cuts in 2024 H1",
            "Bund yields drop 50bp from peak",
            "EUR/USD breaks below 1.05",
        ],
        "market_data": [
            {"ticker": "EWG", "price_t0": 28.0, "change_24h": -1.0, "change_7d": -2.5},
            {"ticker": "EWQ", "price_t0": 35.0, "change_24h": -0.5, "change_7d": -1.8},
            {"ticker": "VGK", "price_t0": 60.0, "change_24h": -0.8, "change_7d": -2.0},
            {"ticker": "SPY", "price_t0": 445.0, "change_24h": 0.3, "change_7d": -1.5},
            {"ticker": "TLT", "price_t0": 88.0, "change_24h": -0.5, "change_7d": -2.5},
        ],
        "description_reveal": (
            "Set-Dic 2023: Eurozona recessione tecnica. EWG -5%, S&P stable, USD forza relativa. "
            "EUR/USD scivola da 1.10 a 1.05. Bund -100bp."
        ),
    },

    # ─── CRASH/RALLY (4) ───
    {
        "id": "crash-covid-march20",
        "category": "crash_rally",
        "title": "Pandemia globale, lockdown imminenti",
        "brief": "Shock biologico, lockdown imminenti, panic selling, defensives prima vittime",
        "period_start": "2020-02-20",
        "period_end": "2020-05-20",
        "asset_universe": ["SPY", "VIX", "TLT", "GLD", "ZM", "AMZN", "NFLX", "XLE", "USO", "DAL"],
        "headlines": [
            "WHO declares pandemic — countries lock down",
            "Fed cuts to zero, announces unlimited QE",
            "VIX spikes above 80",
            "Oil futures negative for first time ever",
            "Airlines suspended, travel ban worldwide",
        ],
        "market_data": [
            {"ticker": "SPY", "price_t0": 295.0, "change_24h": -7.5, "change_7d": -15.0},
            {"ticker": "TLT", "price_t0": 168.0, "change_24h": 3.5, "change_7d": 8.0},
            {"ticker": "GLD", "price_t0": 162.0, "change_24h": 0.5, "change_7d": -1.5},
            {"ticker": "ZM", "price_t0": 110.0, "change_24h": 8.5, "change_7d": 25.0},
            {"ticker": "AMZN", "price_t0": 105.0, "change_24h": 2.5, "change_7d": 5.5},
            {"ticker": "DAL", "price_t0": 38.0, "change_24h": -12.0, "change_7d": -35.0},
            {"ticker": "USO", "price_t0": 5.5, "change_24h": -8.0, "change_7d": -25.0},
        ],
        "description_reveal": (
            "Feb-Mag 2020: COVID crash + V-recovery. S&P -34% poi +30% in 6 settimane. "
            "ZM +400%, AMZN +50%, DAL -65%. Oil collapse storico (USO -75% poi rebound)."
        ),
    },
    {
        "id": "crash-svb-2023",
        "category": "crash_rally",
        "title": "Crollo banca tech, contagion fears",
        "brief": "Bank run improvviso su una banca tier-2 con esposizione tech, contagion incipiente",
        "period_start": "2023-03-08",
        "period_end": "2023-04-08",
        "asset_universe": ["KRE", "ZION", "SCHW", "FRC", "SBNY", "JPM", "BAC", "TLT", "GLD", "SPY"],
        "headlines": [
            "Bank announces $1.8B loss on bond portfolio",
            "VC firms tell startups to pull funds",
            "FDIC steps in over weekend",
            "Fed launches BTFP emergency lending facility",
        ],
        "market_data": [
            {"ticker": "KRE", "price_t0": 60.0, "change_24h": -8.5, "change_7d": -15.0},
            {"ticker": "ZION", "price_t0": 32.0, "change_24h": -10.0, "change_7d": -20.0},
            {"ticker": "SCHW", "price_t0": 55.0, "change_24h": -12.0, "change_7d": -25.0},
            {"ticker": "JPM", "price_t0": 132.0, "change_24h": -2.5, "change_7d": -5.0},
            {"ticker": "TLT", "price_t0": 100.0, "change_24h": 3.5, "change_7d": 8.5},
            {"ticker": "GLD", "price_t0": 175.0, "change_24h": 2.0, "change_7d": 5.5},
        ],
        "description_reveal": (
            "Mar-Apr 2023: SVB collapse. KRE -22%, ZION -35% picco. Mega-bank flat-positive (JPM). "
            "Fed BTFP ferma il contagion. Treasuries +10%, Gold +12%."
        ),
    },
    {
        "id": "rally-ai-jan23",
        "category": "crash_rally",
        "title": "Mania AI improvvisa, mega-cap tech +40% in 6 mesi",
        "brief": "Hype AI esplode, mega-cap tech in rally verticale, narrative reset",
        "period_start": "2023-01-15",
        "period_end": "2023-04-15",
        "asset_universe": ["NVDA", "MSFT", "GOOGL", "META", "AMD", "AVGO", "AAPL", "TSLA", "AMZN", "NFLX"],
        "headlines": [
            "ChatGPT hits 100M users in 2 months — fastest ever",
            "MSFT to invest $10B in OpenAI",
            "NVDA Q4 datacenter revenue beats by 30%",
            "Goldman raises mega-cap AI plays — narrative shift",
        ],
        "market_data": [
            {"ticker": "NVDA", "price_t0": 195.0, "change_24h": 4.5, "change_7d": 12.0},
            {"ticker": "MSFT", "price_t0": 245.0, "change_24h": 2.5, "change_7d": 6.5},
            {"ticker": "META", "price_t0": 175.0, "change_24h": 5.5, "change_7d": 18.0},
            {"ticker": "AMD", "price_t0": 80.0, "change_24h": 3.5, "change_7d": 10.0},
            {"ticker": "AVGO", "price_t0": 580.0, "change_24h": 2.0, "change_7d": 5.5},
        ],
        "description_reveal": (
            "Gen-Apr 2023: AI hype esplode. NVDA +90%, META +65%, MSFT +20%. "
            "Mega-cap rally drammatico, S&P +12% trainato dai Magnificent 7."
        ),
    },
    {
        "id": "crash-yencarry-aug24",
        "category": "crash_rally",
        "title": "Yen carry trade unwind, Nikkei in caduta libera",
        "brief": "BoJ aumenta tassi a sorpresa, yen apprezza 8% in 3 giorni, deleveraging globale",
        "period_start": "2024-07-30",
        "period_end": "2024-10-30",
        "asset_universe": ["FXY", "EWJ", "SPY", "QQQ", "VIX", "NVDA", "TSLA", "TLT", "GLD", "BTC-USD"],
        "headlines": [
            "BoJ surprises with 25bp hike — first in 17 years",
            "USD/JPY crashes from 162 to 145 in 3 sessions",
            "Nikkei drops 12% in single day — circuit breakers triggered",
            "Margin calls cascade through hedge funds",
        ],
        "market_data": [
            {"ticker": "FXY", "price_t0": 60.0, "change_24h": 4.5, "change_7d": 10.0},
            {"ticker": "EWJ", "price_t0": 70.0, "change_24h": -7.5, "change_7d": -15.0},
            {"ticker": "SPY", "price_t0": 540.0, "change_24h": -3.5, "change_7d": -7.5},
            {"ticker": "QQQ", "price_t0": 470.0, "change_24h": -4.0, "change_7d": -8.5},
            {"ticker": "NVDA", "price_t0": 105.0, "change_24h": -6.5, "change_7d": -15.0},
            {"ticker": "TSLA", "price_t0": 215.0, "change_24h": -5.0, "change_7d": -12.0},
            {"ticker": "BTC-USD", "price_t0": 64000.0, "change_24h": -8.0, "change_7d": -18.0},
        ],
        "description_reveal": (
            "Lug-Ott 2024: yen carry unwind dopo BoJ hike. EWJ -20%, NVDA -25%, BTC -22% in 1 settimana. "
            "Recupero rapido in 4-6 settimane (S&P torna ai massimi a settembre)."
        ),
    },
]


def get_scenarios_by_category(category: str) -> list[dict]:
    """Ritorna gli scenari di una categoria (lista di dict, senza il reveal)."""
    return [_strip_reveal(s) for s in SCENARIOS if s["category"] == category]


def get_scenario_counts() -> dict:
    """{category: count} per la UI di selezione."""
    counts = {}
    for s in SCENARIOS:
        counts[s["category"]] = counts.get(s["category"], 0) + 1
    return counts


def get_scenario_by_id(scenario_id: str) -> dict | None:
    """Ritorna lo scenario completo (con reveal) — usato dal runner backend."""
    for s in SCENARIOS:
        if s["id"] == scenario_id:
            return s
    return None


def get_random_scenario(category: str) -> dict | None:
    """Sceglie uno scenario random nella categoria."""
    import random
    candidates = [s for s in SCENARIOS if s["category"] == category]
    return random.choice(candidates) if candidates else None


def _strip_reveal(s: dict) -> dict:
    """Rimuove i campi 'rivelativi' dal dict per esporlo all'UI di selezione."""
    return {
        "id": s["id"],
        "category": s["category"],
        "title": s["title"],
        "brief": s["brief"],
    }
