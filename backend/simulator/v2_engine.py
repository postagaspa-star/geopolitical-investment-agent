"""
Simulator V2 — Engine stateless basato su prezzi storici reali (Polygon).

Filosofia:
  - Server STATELESS: il browser tiene tutto lo stato (scenario, portfolio,
    history dei turni). Ogni request manda l'intero contesto.
  - Prezzi REALI da Polygon per ogni step date dello scenario.
  - L'AI gestisce un PORTAFOGLIO (non singole BUY/SELL/HOLD): allocation %
    su uno o più asset. Decide tra LONG, SHORT, FLAT per ogni asset.
  - Orizzonte fisso: 1 settimana per turno (esplicito nel prompt).
  - 3-5 turni configurabili dall'utente.

Flow:
  start_run(scenario_id, num_steps, initial_capital)
    → ritorna: scenario_payload + step_dates + portfolio iniziale
  execute_step(scenario, portfolio, history, step_index)
    → fetcha prezzi reali a step_dates[step_index]
    → chiama AI con context (scenario + history + portfolio + nuovi prezzi)
    → applica trade al portfolio
    → ritorna: nuovo portfolio + step_data
  finalize_run(scenario, portfolio, history)
    → fetcha prezzi finali (chiusura ultimo step)
    → calcola P&L finale + benchmark vs SPY
    → genera debrief AI
    → salva nel DB per memoria
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

import aiohttp

logger = logging.getLogger(__name__)

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_R1 = "deepseek-reasoner"


# ═════════════════════════════════════════════════════════════════════════
# SYSTEM PROMPT — riscritto da zero per il portfolio multi-asset
# ═════════════════════════════════════════════════════════════════════════

SIM_V2_SYSTEM_PROMPT = """Sei un PORTFOLIO MANAGER AI in modalità SIMULATOR.

Stai gestendo un portafoglio reale di fronte a uno scenario storico.
Hai a disposizione un BUDGET INIZIALE in dollari e devi decidere come allocarlo
nei vari asset disponibili.

═══════════════════════════════════════════════════════════════════════
REGOLE CHIAVE (LEGGI CON ATTENZIONE)
═══════════════════════════════════════════════════════════════════════

1. ORIZZONTE FISSO 1 SETTIMANA PER TURNO
   Ogni decisione che prendi vale per UNA SETTIMANA. Il prossimo turno potrai
   riconsiderare la posizione, ma fino ad allora il portafoglio resta come
   l'hai impostato. Quindi pensaci bene: cosa succederà nei prossimi 7 giorni?

2. PUOI FARE QUANTI TRADE VUOI PER TURNO
   Non sei costretto a una sola operazione. Puoi:
   - Aprire più posizioni in parallelo (es. 30% XOM + 20% GLD)
   - Chiudere posizioni esistenti (SELL) per liberare cash
   - Mantenere posizioni aperte (NESSUNA azione = mantieni)

3. ALLOCAZIONE IN PERCENTUALE
   Quando dici "BUY XOM 30%" significa: usa il 30% del CAPITALE TOTALE
   corrente per comprare XOM. Il sistema calcola la quantità esatta dal
   prezzo corrente. La somma delle nuove BUY non deve superare il cash
   disponibile.

4. SHORT SELLING DISPONIBILE
   Puoi shortare un asset se prevedi che scenderà. SELL su asset NON
   posseduto = apre uno short. SELL su asset già posseduto = chiude la
   posizione long.

5. PENSA GLOBALMENTE — ROTAZIONE SETTORIALE
   Ogni scenario include un CONTESTO GLOBALE con macro indicators e segnali
   cross-settoriali. Leggilo SEMPRE e ragiona sulle implicazioni per TUTTI
   i settori disponibili nell'asset_universe, non solo quello primario.

   Se il settore principale è sotto pressione e non vedi asset positivi al
   suo interno → di norma NON stare in cash: individua il settore
   BENEFICIARIO e ruota lì. La rotazione settoriale è spesso la mossa più
   redditizia.

   GERARCHIA (chi comanda quando): questa regola vale nei regimi
   NORMALE/TREND/MACRO. In un CRASH conclamato comanda la sezione 6.A:
   se i segnali di capitulation NON ci sono, "stare in cash / shortare /
   ruotare SOLO su safe-haven" È la mossa giusta e questa regola 5 NON
   ti obbliga a comprare altro. Non usare la regola 5 per giustificare
   acquisti dentro un crash senza i segnali della 6.A.

   Correlazioni fondamentali da applicare:
   • Conflitto militare  →  Defense ↑, Energy ↑, Gold ↑, Tech ↓, Bond ↑
   • Crisi bancaria       →  Finanziari ↓, TLT ↑, Gold ↑, Mega-cap tech ↑
   • Inflazione alta      →  Commodity ↑, Energy ↑, Bond ↓, Growth ↓
   • Pivot/taglio tassi   →  Growth ↑, TLT ↑, Value ↓, Crypto ↑
   • Panico (VIX 30+)    →  Safe-haven ↑ (GLD, TLT), tutto il resto ↓
   • Yen carry unwind     →  JPY ↑, globale ↓ inizialmente, poi rimbalzo
   • Elezioni USA         →  Settori policy-sensitive divergono per candidato

   Regola pratica: per ogni scenario, chiediti "CHI VINCE in questo contesto?"
   Poi cerca quel settore nell'asset_universe e allocaci.

═══════════════════════════════════════════════════════════════════════
6. GESTIONE DEL RISCHIO E TATTICA PER REGIME (IL CUORE DEL MESTIERE)
═══════════════════════════════════════════════════════════════════════
Questa sezione separa un gestore che sopravvive da uno che brucia capitale.
NON sono divieti meccanici: sono criteri OSSERVABILI che devi imparare a
riconoscere e applicare col tuo giudizio. L'analisi di 300 simulazioni ha
mostrato 3 errori sistematici che ti costano la maggior parte delle perdite.

── A. CRASH/RALLY: NON prendere il coltello che cade ──────────────────
L'errore #1 nei crash: comprare la PRIMA gamba di discesa "anticipando il
bottom". In un crash il prezzo che è sceso del 5% può scendere ancora del
15%: il "sembra economico" è la trappola che uccide.

Prima di comprare un rimbalzo in un crash, devi VEDERE almeno 2 di questi
segnali OSSERVABILI di capitulation/stabilizzazione (non immaginarli —
devono essere nei dati del turno):
  • Un turno con variazione settimanale vicina a 0% o un reversal positivo
    DOPO una serie di turni fortemente negativi (la discesa si è fermata).
  • Volume in climax: picco di volume sul minimo, poi volume in calo
    (esaurimento dei venditori).
  • L'ampiezza dei movimenti settimanali si COMPRIME dopo gli estremi
    (da -8%/sett a -1%/sett = panico che sfuma).
  • Divergenza: nuovo minimo di prezzo ma momentum/RSI NON fa nuovo minimo.

Finché NON vedi questi segnali nel crash: l'azione corretta NON è "comprare
sperando", è una tra — restare in cash, SHORTARE la debolezza confermata
(il trend ribassista è il tuo amico finché non si rompe), o ruotare su
safe-haven (GLD/TLT) che salgono nel panico. Imparare a NON agire sul
coltello che cade vale più di mille rimbalzi azzeccati per fortuna.
Quando i segnali ARRIVANO, allora sì: il rimbalzo post-capitulation è uno
dei trade a più alto rendimento che esistano. L'obiettivo è riconoscere
il MOMENTO, non astenersi per sempre.

── B. CRYPTO ≠ EQUITY in regimi di stress ─────────────────────────────
L'errore #2: applicare alle crypto lo stesso sizing e le stesse soglie
dell'equity durante CRASH o REGULATORY-EVENT. Le crypto in stress hanno
volatilità 3-5× quella azionaria: scendono PRIMA, PIÙ VELOCE e PIÙ A FONDO.
In un panico NON sono un rifugio, sono l'epicentro.

Principi quando l'asset è crypto (BTC/ETH/SOL/AVAX/DOT...) E il regime è
CRASH o REGULATORY-EVENT:
  • Size DIMEZZATA (o meno) rispetto a quella che useresti per un equity
    con la stessa tesi. Una posizione crypto al 25% in un crash è un
    errore di sopravvivenza.
  • Apri long crypto in stress SOLO con conviction ALTA (mai MEDIA/BASSA):
    se non sei molto convinto, in un panico crypto la risposta è no.
  • Nel panico le crypto NON sono un target di rotazione difensiva:
    il flusso difensivo va su GLD/TLT/USD, NON su BTC.
  • Lo "stop" mentale di una crypto va commisurato alla SUA volatilità:
    un asset che oscilla 8%/settimana non si gestisce con le soglie di
    un titolo che ne oscilla 2%. Soglie da equity su crypto = stop-out
    da rumore garantito.

── C. ASIMMETRIA RISCHIO/RENDIMENTO (il fix di Normale/Geopolitico) ────
L'errore #3, il più costoso in assoluto: nelle 300 run la perdita media è
~DOPPIA del guadagno medio (es. +0.74% vs -1.56%). Con questa asimmetria
NESSUN win-rate ti salva: il sistema è strutturalmente in perdita.

La causa NON è "lo stop è troppo largo" (uno stop fisso strettissimo ti
farebbe solo stoppare dal rumore, peggiorando tutto). La causa è duplice
e va corretta col giudizio:
  1. NON FAI CORRERE I VINCITORI: chiudi a +0.7% per "portare a casa"
     mentre la tesi era ancora valida. Se la tesi che ha aperto il trade
     REGGE ancora, MANTIENI: è così che l'Avg Win diventa +3% invece di
     +0.7%. Chiudi un vincitore solo quando la tesi è esaurita/invalidata,
     non per ansia.
  2. NON TAGLI I PERDENTI: tieni un trade in perdita "sperando nel
     ritorno" dopo che la tesi è stata INVALIDATA dai fatti. Quando il
     turno mostra che la tua tesi era sbagliata, ESCI subito — la perdita
     piccola e tesi-driven è sana; la perdita grande da speranza no.

REGOLA DI INGRESSO ASIMMETRICA (applicala SEMPRE, ogni regime):
prima di aprire un trade, stima in chiaro nel ragionamento:
  - upside atteso se la tesi è giusta (in % sul prezzo, orizzonte 1 sett.)
  - downside atteso se la tesi è sbagliata (movimento avverso plausibile,
    commisurato alla volatilità REALE di QUELL'asset)
Apri SOLO se upside_atteso >= 1.5 × downside_atteso. Se il miglior caso
rende quanto (o meno di) quanto rischi nel caso peggiore, è un NON-trade:
lascialo andare. È meglio mancare 10 trade mediocri che farne 1 con
R/R sfavorevole.

── D. PARTECIPAZIONE AL TREND (il fix di Bull/Normale) ─────────────────
L'errore #4, speculare al coltello che cade: NON PARTECIPARE ai mercati
che salgono. I dati storici delle run sono brutali: nei regimi bull il
win rate è 0% e nei regimi normali 9% — non per trade sbagliati, ma per
SOTTOESPOSIZIONE CRONICA (50-85% di cash costante). Il tuo risultato è
misurato CONTRO il mercato: se il mercato fa +3% e tu +0.9% perché eri
mezzo fermo, HAI PERSO. In un trend che sale, il cash non è prudenza:
è un trade MANCATO, esattamente come comprare il coltello che cade è
un trade sbagliato.

Regole operative:
  • Se il regime è TREND CONFERMATO (la maggioranza degli asset sale da
    2+ turni, nessun segnale di rottura): l'esposizione TARGET è >= 60%
    del capitale. Sotto quel livello stai scommettendo CONTRO il trend
    senza dirlo.
  • Cash sopra il 40% in un trend confermato = una POSIZIONE ATTIVA che
    va motivata esplicitamente nel ragionamento a OGNI turno ("resto
    liquido perché X osservabile"), non un default di comfort.
  • La prudenza vera nei trend non è stare fuori: è stare DENTRO con
    stop chiari sulle posizioni (sezione uscite) e size sensate.
  • Questa regola NON vale nei CRASH (lì comanda 6.A) né quando i
    segnali sono contraddittori (lì il cash è legittimo e va comunque
    motivato).

═══════════════════════════════════════════════════════════════════════
PROCEDURA OBBLIGATORIA (3 sezioni in ordine, NESSUNA OMISSIONE)
═══════════════════════════════════════════════════════════════════════

[1] LETTURA DEL CONTESTO
    Sintesi di cosa stai vedendo (3-5 righe):
    - Tema/regime suggerito dalle headline
    - Asset più mossi e direzione
    - Stato del tuo portafoglio attuale (se non è il primo turno)

[2] RAGIONAMENTO STRATEGICO
    - Tesi: cosa pensi succederà nella PROSSIMA SETTIMANA?
    - Quali asset performeranno bene? Quali male?
    - Stai confermando, modificando o invalidando la tua tesi precedente?
    - Rischi principali
    - GESTIONE POSIZIONI APERTE: per ogni posizione che tieni, la tesi che
      l'ha aperta REGGE ancora? Se sì → mantieni e lascia correre (non
      chiudere un vincitore per ansia). Se è INVALIDATA → chiudi subito,
      non sperare nel ritorno (sezione 6.C).
    - CHECK PIANI D'USCITA: ogni posizione aperta ha il suo exit_plan
      DICHIARATO DA TE (te lo ripresento nel portafoglio). Rileggilo:
      lo stop o il target sono stati raggiunti/violati? Se sì hai DUE
      sole opzioni oneste: (a) agire di conseguenza, o (b) derogare
      DICHIARANDOLO ("il mio piano diceva X, non lo eseguo perché Y
      osservabile"). Ignorare in silenzio il proprio piano è l'errore
      più costoso rilevato nelle run passate.
    - CHECK REGIME: se CRASH → hai i segnali di capitulation per comprare
      un rimbalzo? (sezione 6.A) Se l'asset è crypto in stress → size
      dimezzata + solo conviction ALTA? (sezione 6.B) Se TREND CONFERMATO
      → esposizione >= 60% o cash motivato esplicitamente? (sezione 6.D)

[3] DECISIONE
    Output JSON STRUTTURATO. Schema esatto (NIENTE testo extra dopo):
    {
      "trades": [
        {
          "action": "BUY" | "SELL",
          "asset": "TICKER",
          "allocation_pct": <numero 1-100>,
          "conviction": "BASSA" | "MEDIA" | "ALTA",
          "thesis": "Una frase: perché questo trade per la prossima settimana",
          "rr": "upside +X% vs downside -Y% → ratio Z (deve essere >= 1.5)",
          "exit_plan": "stop: <condizione/livello che invalida la tesi> | target: <quando incassi o rivaluti>",
          "stop_loss_target": null | <PREZZO ASSOLUTO (es. 150.50), NON percentuale>
        }
      ],
      "hold_summary": "Frase breve sulle posizioni che mantieni invariate (se ce ne sono)"
    }

REGOLE PER trades:
- Lista vuota [] = mantieni il portafoglio come è (skip turno) — è una
  decisione legittima e spesso la migliore: NON forzare trade mediocri
- BUY: apre una nuova posizione long o aggiunge a esistente
- SELL: chiude posizione long esistente OPPURE apre short
- allocation_pct si riferisce al capitale TOTALE attuale del portafoglio
- Asset deve essere uno dei ticker dell'asset_universe fornito
- Massimo 5 trade per turno (focus, non sparare a caso)
- "rr" OBBLIGATORIO su ogni trade: stima esplicita upside vs downside
  (downside commisurato alla volatilità REALE di quell'asset, non un
  numero generico). Se ratio < 1.5 il trade NON va aperto — è un
  NON-trade. Questo campo rende verificabile la disciplina R/R della
  sezione 6.C: un trade senza rr coerente è un errore di processo.
- "stop_loss_target" FACOLTATIVO ma, se popolato, VINCOLANTE: prezzo
  assoluto a cui la posizione viene chiusa AUTOMATICAMENTE dal motore
  prima del tuo turno successivo (long: prezzo <= stop; short: >= stop)
- "exit_plan" OBBLIGATORIO su ogni BUY e ogni SHORT: PRIMA di entrare
  dichiari a che condizioni esci — uno stop (il livello/evento che
  invalida la tesi) e un target (quando incassi o rivaluti). Livelli in
  % o prezzo, commisurati alla volatilità dell'asset. Il piano ti verrà
  RIPRESENTATO a ogni turno accanto alla posizione: entrare senza piano
  d'uscita è un errore di processo (le run passate mostrano stop
  "mentali" dichiarati e mai più guardati).

ESEMPIO DI BUONA DECISIONE:
{
  "trades": [
    {"action": "BUY", "asset": "XOM", "allocation_pct": 25,
     "conviction": "ALTA",
     "thesis": "Tensione Medio Oriente persiste, oil supply a rischio nel breve",
     "rr": "upside +5% vs downside -2.5% → ratio 2.0",
     "exit_plan": "stop: chiusura sotto $102 (supporto) o de-escalation | target: +5-6% o headline risolutiva"},
    {"action": "SELL", "asset": "TLT", "allocation_pct": 15,
     "conviction": "MEDIA",
     "thesis": "Yields probabili al rialzo se inflazione importata da oil",
     "rr": "upside +3% vs downside -2% → ratio 1.5",
     "exit_plan": "stop: CPI sotto attese o TLT +2% contro di me | target: +3% sul short"}
  ],
  "hold_summary": "Mantengo GLD long (10%) come hedge, cash al 50% per opportunità"
}
"""


# ═════════════════════════════════════════════════════════════════════════
# UTILITY — date/scenario expansion
# ═════════════════════════════════════════════════════════════════════════

def compute_step_dates(period_start: str, num_steps: int) -> list[str]:
    """
    Genera le date di ogni turno: T0, T+7d, T+14d, T+21d, ... (1 settimana per step).
    Returns: ['2023-10-06', '2023-10-13', '2023-10-20', ...]
    """
    start = datetime.fromisoformat(period_start).date()
    return [(start + timedelta(days=7 * i)).isoformat() for i in range(num_steps + 1)]
    # +1 perché abbiamo bisogno anche del prezzo finale (post-ultimo turno)


def _get_deepseek_key() -> str:
    return os.environ.get("DEEPSEEK_API_KEY", "")


# ═════════════════════════════════════════════════════════════════════════
# PRICE FETCHER — usa Polygon historical
# ═════════════════════════════════════════════════════════════════════════

async def fetch_prices_at_date(
    tickers: list[str], target_date: str, lookback_days: int = 5
) -> dict[str, float]:
    """
    Per ogni ticker, ritorna il close più recente <= target_date.
    Lookback per gestire weekend/festivi (es. domenica → venerdì).

    Returns: {"XOM": 85.42, "GLD": 217.10, ...}. Asset senza dati = omesso.
    """
    from data_fetchers import fetch_historical_range_async

    target = datetime.fromisoformat(target_date).date()
    from_date = (target - timedelta(days=lookback_days)).isoformat()
    to_date = target.isoformat()

    async def _fetch_one(ticker: str) -> tuple[str, Optional[float]]:
        result = await fetch_historical_range_async(ticker, from_date, to_date)
        records = result.get("data") or []
        if not records:
            return ticker, None
        # Prendi il più recente <= target_date
        valid = [r for r in records if r["date"] <= target_date]
        if not valid:
            return ticker, None
        last = valid[-1]
        return ticker, float(last.get("close", 0)) or None

    results = await asyncio.gather(*[_fetch_one(t) for t in tickers])
    return {t: p for t, p in results if p}


# ═════════════════════════════════════════════════════════════════════════
# PORTFOLIO ENGINE — apply trades, compute P&L
# ═════════════════════════════════════════════════════════════════════════

def make_initial_portfolio(initial_capital: float,
                            commission_bps: float | None = None) -> dict:
    """
    Portafoglio vuoto al T0.

    commission_bps: 10 bps default = 0.10% per trade. Pass 0 per test
    "no fees" (slippage rerun comparativo).
    """
    from simulator.metrics import normalize_commission_bps
    bps = normalize_commission_bps(commission_bps)
    return {
        "cash": float(initial_capital),
        "initial_capital": float(initial_capital),
        "commission_bps": bps,
        "total_commissions_paid": 0.0,
        "positions": [],   # list of {asset, quantity, avg_entry_price, side: 'long'|'short'}
    }


def compute_portfolio_value(portfolio: dict, prices: dict[str, float]) -> dict:
    """
    Calcola valore totale del portfolio ai prezzi correnti.
    Returns: {total_value, cash, positions_value, unrealized_pnl, positions: [...with current_price/pnl]}
    """
    cash = float(portfolio.get("cash", 0))
    enriched_positions = []
    positions_value = 0.0

    for pos in portfolio.get("positions", []):
        asset = pos["asset"]
        qty = float(pos.get("quantity", 0))
        entry = float(pos.get("avg_entry_price", 0))
        side = pos.get("side", "long")
        cur_price = prices.get(asset, entry)

        # Math canonica (accounting): UN solo posto per NAV/P&L direction-aware.
        # Il simulator usa side='long'/'short' → mappo a direction.
        import accounting as _acc
        _dir = "SHORT" if side == "short" else "LONG"
        mkt_value = qty * cur_price  # valore lordo (display: sempre positivo)
        pnl = _acc.unrealized_pnl(qty, entry, cur_price, _dir)
        positions_value += _acc.signed_position_value(qty, cur_price, _dir)

        enriched_positions.append({
            **pos,
            "current_price": cur_price,
            "market_value": round(mkt_value, 2),
            "unrealized_pnl": round(pnl, 2),
            "unrealized_pnl_pct": round(_acc.unrealized_pnl_pct(entry, cur_price, _dir), 2),
        })

    total_value = cash + positions_value
    initial = float(portfolio.get("initial_capital", total_value))
    total_pnl = total_value - initial
    total_pnl_pct = (total_pnl / initial * 100) if initial else 0

    return {
        "total_value": round(total_value, 2),
        "cash": round(cash, 2),
        "positions_value": round(positions_value, 2),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl_pct, 2),
        "initial_capital": initial,
        "positions": enriched_positions,
        # Commissioni: utili per la UI ("hai pagato $X di fees finora")
        # e per il rerun slippage sensitivity.
        "commission_bps": float(portfolio.get("commission_bps", 0) or 0),
        "total_commissions_paid": round(
            float(portfolio.get("total_commissions_paid", 0) or 0), 2
        ),
    }


def apply_trades(portfolio: dict, trades: list[dict], prices: dict[str, float],
                  commission_bps: float | None = None) -> dict:
    """
    Applica una lista di trade al portfolio con simulazione commissioni/slippage.
    Modifica il portfolio (mutativo) e ritorna il nuovo stato.

    Args:
      portfolio: stato corrente {cash, positions, ...}
      trades: lista di {action, asset, allocation_pct, ...}
      prices: prezzi correnti {ticker: price}
      commission_bps: costo per trade in basis points (default 10 = 0.10%).
                       Sottratto dal cash su BUY (oltre al cost) e dal proceeds
                       su SELL. Senza questo, il sim era leggermente troppo
                       ottimistico — i trade aggressivi sembravano "gratis"
                       e l'AI sviluppava strategie irrealistiche.
                       Pass 0 per disabilitare (rerun "no fees" comparativo).

    Logica:
      - BUY su asset esistente long → aumenta quantity, ricalcola avg_entry
      - BUY su asset esistente short → chiude lo short (riacquista)
      - SELL su asset esistente long → riduce/chiude long
      - SELL su asset NON esistente → apre short
      - SELL su asset esistente short → aumenta short (avg_entry ricalcolato)
    """
    from simulator.metrics import normalize_commission_bps, commission_amount

    # Risolvi commission_bps. Priorita':
    # 1. parametro esplicito (override del caller, es. slippage rerun)
    # 2. portfolio.commission_bps (settato a start_run dal client/server)
    # 3. default 10 bps
    if commission_bps is None:
        commission_bps = portfolio.get("commission_bps")
    commission_bps = normalize_commission_bps(commission_bps)
    fee_factor = commission_bps / 10_000.0   # 10 bps → 0.001

    # Copia profonda per non mutare l'input
    new_portfolio = {
        "cash": float(portfolio.get("cash", 0)),
        "initial_capital": float(portfolio.get("initial_capital", 0)),
        "commission_bps": commission_bps,
        "total_commissions_paid": float(portfolio.get("total_commissions_paid", 0)),
        "positions": [
            {**p} for p in (portfolio.get("positions") or [])
        ],
    }
    applied_trades = []
    for trade in trades or []:
        action = (trade.get("action") or "").upper()
        asset = trade.get("asset", "").upper()
        alloc_pct = float(trade.get("allocation_pct", 0))
        # _force_close (chiave privata, usata da enforce_sim_stops): chiude
        # la posizione INTERA a qty esatta, senza matematica allocation_pct.
        force_close = bool(trade.get("_force_close"))
        if not asset or (alloc_pct <= 0 and not force_close) \
                or action not in ("BUY", "SELL"):
            continue
        price = prices.get(asset)
        if not price or price <= 0:
            applied_trades.append({**trade, "status": "skipped",
                                   "reason": f"no price for {asset}"})
            continue

        # FIX: total_value ricalcolato per OGNI trade (non una sola volta a inizio
        # loop). Bug precedente: il primo trade chiudeva uno short con P&L grosso
        # ma il secondo trade calcolava dollar_amount sul total_value PRE-chiusura.
        valuation = compute_portfolio_value(new_portfolio, prices)
        total_value_now = valuation["total_value"]

        # Calcola dollar amount = % del valore totale corrente
        dollar_amount = total_value_now * (alloc_pct / 100.0)
        quantity = round(dollar_amount / price, 4)

        # Trova posizione esistente per questo asset
        existing = next((p for p in new_portfolio["positions"]
                         if p["asset"] == asset), None)

        if force_close:
            if not existing:
                applied_trades.append({**trade, "status": "skipped",
                                       "reason": "no position to force-close"})
                continue
            quantity = existing["quantity"]

        if quantity <= 0:
            applied_trades.append({**trade, "status": "skipped",
                                   "reason": "quantity rounds to 0"})
            continue

        # Trade-record builder che riflette gli effettivi valori eseguiti
        # (FIX: prima si scriveva l'allocation_pct ORIGINALE anche dopo
        # scaling per cash insufficient → confondeva la diagnostica).
        def _record(executed_qty: float, status: str, fee: float = 0.0,
                    **extra) -> dict:
            ev = executed_qty * price
            actual_pct = (ev / total_value_now * 100) if total_value_now else 0
            return {
                **trade, "status": status,
                "executed_qty": round(executed_qty, 4),
                "executed_price": round(price, 4),
                "executed_value": round(ev, 2),
                "actual_allocation_pct": round(actual_pct, 2),
                "commission": round(fee, 4),
                "commission_bps": commission_bps,
                **extra,
            }

        if action == "BUY":
            gross_cost = quantity * price
            fee = commission_amount(gross_cost, commission_bps)
            total_cost = gross_cost + fee
            if total_cost > new_portfolio["cash"] + 0.01:
                # Cash insufficiente: scala al massimo possibile INCLUDENDO la
                # commissione (max_qty * price * (1 + fee_factor) = cash).
                effective_unit_cost = price * (1 + fee_factor)
                quantity = round(new_portfolio["cash"] / effective_unit_cost, 4)
                if quantity <= 0:
                    applied_trades.append({**trade, "status": "skipped",
                                           "reason": "cash exhausted"})
                    continue
                gross_cost = quantity * price
                fee = commission_amount(gross_cost, commission_bps)
                total_cost = gross_cost + fee
            new_portfolio["cash"] -= total_cost
            new_portfolio["total_commissions_paid"] += fee
            if existing and existing.get("side") == "long":
                new_qty = existing["quantity"] + quantity
                new_avg = (existing["avg_entry_price"] * existing["quantity"]
                           + price * quantity) / new_qty
                existing["quantity"] = round(new_qty, 4)
                existing["avg_entry_price"] = round(new_avg, 4)
                if trade.get("exit_plan"):
                    existing["exit_plan"] = str(trade["exit_plan"])[:250]
                if _stop_target_of(trade) is not None:
                    existing["stop_loss_target"] = _stop_target_of(trade)
                applied_trades.append(_record(quantity, "executed_add_long", fee=fee))
            elif existing and existing.get("side") == "short":
                # BUY su SHORT = riacquista (chiude). FIX accounting:
                # all'apertura dello short avevamo INCASSATO avg*qty nel cash.
                # Ora paghiamo `gross_cost = quantity*price` + commissione.
                # Il P&L sul close è IMPLICITO nel cash flow netto:
                #   net = -close_qty*price - fee_open - fee_close + close_qty*avg
                # La fee qui copre l'intera quantity (close + eventuale residual).
                close_qty = min(quantity, existing["quantity"])
                pnl_realized = (existing["avg_entry_price"] - price) * close_qty
                existing["quantity"] -= close_qty
                if existing["quantity"] <= 0.0001:
                    new_portfolio["positions"].remove(existing)
                residual = quantity - close_qty
                if residual > 0:
                    new_portfolio["positions"].append({
                        "asset": asset, "quantity": round(residual, 4),
                        "avg_entry_price": round(price, 4), "side": "long",
                        "thesis": trade.get("thesis", "")[:300],
                        "conviction": trade.get("conviction", "MEDIA"),
                        "exit_plan": (trade.get("exit_plan") or "")[:250],
                        "stop_loss_target": _stop_target_of(trade),
                    })
                    applied_trades.append(_record(quantity, "executed_flip_short_to_long",
                                                   fee=fee,
                                                   close_qty=round(close_qty, 4),
                                                   pnl_realized=round(pnl_realized, 2),
                                                   long_residual=round(residual, 4)))
                else:
                    applied_trades.append(_record(close_qty, "executed_close_short",
                                                   fee=fee,
                                                   pnl_realized=round(pnl_realized, 2)))
            else:
                new_portfolio["positions"].append({
                    "asset": asset, "quantity": quantity,
                    "avg_entry_price": price, "side": "long",
                    "thesis": trade.get("thesis", "")[:300],
                    "conviction": trade.get("conviction", "MEDIA"),
                    "exit_plan": (trade.get("exit_plan") or "")[:250],
                    "stop_loss_target": _stop_target_of(trade),
                })
                applied_trades.append(_record(quantity, "executed_open_long", fee=fee))

        else:  # SELL
            if existing and existing.get("side") == "long":
                # Chiude/riduce long. Se qty supera il long, il residuo apre
                # uno SHORT netto — ma SOLO se l'eccesso è sostanziale
                # (> 30% della posizione). L'AI esprime il SELL in % del
                # capitale totale: un close espresso come "11.5%" su una
                # posizione che vale l'11.4% del NAV produceva residui
                # micro-short involontari (osservato su run reale: 0.125 SOL
                # e 0.47 DOT rimasti aperti short con thesis "Exit...to
                # reduce risk"). Il prompt (regola C4) dice "SELL su
                # posseduto = chiude": l'eccesso da arrotondamento è intent
                # di chiusura, non di flip.
                if (quantity > existing["quantity"]
                        and quantity - existing["quantity"]
                        <= 0.30 * existing["quantity"]):
                    quantity = existing["quantity"]
                close_qty = min(quantity, existing["quantity"])
                gross_proceeds = close_qty * price
                fee_close = commission_amount(gross_proceeds, commission_bps)
                net_proceeds = gross_proceeds - fee_close
                new_portfolio["cash"] += net_proceeds
                new_portfolio["total_commissions_paid"] += fee_close
                existing["quantity"] -= close_qty
                if existing["quantity"] <= 0.0001:
                    new_portfolio["positions"].remove(existing)
                residual = quantity - close_qty
                if residual > 0:
                    # Apre SHORT netto col residuo: incassa proceeds netti
                    short_gross = residual * price
                    fee_short = commission_amount(short_gross, commission_bps)
                    new_portfolio["cash"] += short_gross - fee_short
                    new_portfolio["total_commissions_paid"] += fee_short
                    new_portfolio["positions"].append({
                        "asset": asset, "quantity": round(residual, 4),
                        "avg_entry_price": round(price, 4), "side": "short",
                        "thesis": trade.get("thesis", "")[:300],
                        "conviction": trade.get("conviction", "MEDIA"),
                        "exit_plan": (trade.get("exit_plan") or "")[:250],
                        "stop_loss_target": _stop_target_of(trade),
                    })
                    applied_trades.append(_record(quantity, "executed_flip_long_to_short",
                                                   fee=fee_close + fee_short,
                                                   close_qty=round(close_qty, 4),
                                                   short_residual=round(residual, 4)))
                else:
                    applied_trades.append(_record(close_qty, "executed_close_long",
                                                   fee=fee_close,
                                                   proceeds=round(net_proceeds, 2)))
            elif existing and existing.get("side") == "short":
                gross_proceeds = quantity * price
                fee = commission_amount(gross_proceeds, commission_bps)
                net_proceeds = gross_proceeds - fee
                new_qty = existing["quantity"] + quantity
                new_avg = (existing["avg_entry_price"] * existing["quantity"]
                           + price * quantity) / new_qty
                existing["quantity"] = round(new_qty, 4)
                existing["avg_entry_price"] = round(new_avg, 4)
                # Incassa il proceeds NETTO anche sull'aumento short
                new_portfolio["cash"] += net_proceeds
                new_portfolio["total_commissions_paid"] += fee
                if trade.get("exit_plan"):
                    existing["exit_plan"] = str(trade["exit_plan"])[:250]
                if _stop_target_of(trade) is not None:
                    existing["stop_loss_target"] = _stop_target_of(trade)
                applied_trades.append(_record(quantity, "executed_add_short", fee=fee))
            else:
                gross_proceeds = quantity * price
                fee = commission_amount(gross_proceeds, commission_bps)
                net_proceeds = gross_proceeds - fee
                new_portfolio["cash"] += net_proceeds
                new_portfolio["total_commissions_paid"] += fee
                new_portfolio["positions"].append({
                    "asset": asset, "quantity": quantity,
                    "avg_entry_price": price, "side": "short",
                    "thesis": trade.get("thesis", "")[:300],
                    "conviction": trade.get("conviction", "MEDIA"),
                    "exit_plan": (trade.get("exit_plan") or "")[:250],
                    "stop_loss_target": _stop_target_of(trade),
                })
                applied_trades.append(_record(quantity, "executed_open_short",
                                               fee=fee,
                                               proceeds=round(net_proceeds, 2)))

    # Round dei totali per pulizia
    new_portfolio["total_commissions_paid"] = round(
        new_portfolio["total_commissions_paid"], 2
    )
    return {"portfolio": new_portfolio, "applied_trades": applied_trades}


def _stop_target_of(trade: dict) -> float | None:
    """stop_loss_target del trade come float positivo, o None."""
    try:
        f = float(trade.get("stop_loss_target"))
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


# ── Stop enforcement sim (Step 5 analisi 13/07, flag OFF di default) ─────
# Il Live esegue gli stop meccanicamente (watchdog/enforce_stops); il
# Simulator prima NO: gli exit plan erano solo testo e ~meta' degli stop
# dichiarati toccati non produceva chiusure. Divergenza Sim/Live che
# invalidava il transfer dei risultati.

SETTING_SIM_STOP_ENFORCEMENT = "sim_stop_enforcement_enabled"


def _sim_stop_enforcement_enabled() -> bool:
    try:
        from simulator import db as sim_db
        val = sim_db.get_setting(SETTING_SIM_STOP_ENFORCEMENT, "false")
    except Exception:
        return False
    return (val or "false").strip().lower() in ("1", "true", "yes", "on")


def enforce_sim_stops(portfolio: dict, prices: dict[str, float],
                      commission_bps: float | None = None
                      ) -> tuple[dict, list[dict]]:
    """
    Esegue gli stop_loss_target dichiarati dall'agente PRIMA del turno LLM
    (long: price <= stop; short: price >= stop), chiudendo la POSIZIONE
    INTERA con la contabilita' di apply_trades (fee incluse).

    Ritorna (portfolio, forced_trades). Con flag OFF o nessuno stop
    crossato: input invariato e lista vuota.
    """
    if not _sim_stop_enforcement_enabled():
        return portfolio, []
    forced = []
    for p in portfolio.get("positions") or []:
        stop = _stop_target_of(p)
        if stop is None:
            continue
        px = prices.get(p.get("asset"))
        if not px or px <= 0:
            continue
        side = p.get("side")
        if (side == "long" and px <= stop) or (side == "short" and px >= stop):
            forced.append({
                "action": "SELL" if side == "long" else "BUY",
                "asset": p["asset"], "allocation_pct": 0,
                "_force_close": True, "forced_by": "stop_loss_sim",
                "thesis": (f"STOP LOSS automatico: {side} su {p['asset']} "
                           f"chiuso a {px} (stop dichiarato {stop})"),
            })
    if not forced:
        return portfolio, []
    result = apply_trades(portfolio, forced, prices, commission_bps)
    for r in result["applied_trades"]:
        if str(r.get("status", "")).startswith("executed"):
            r["status"] = "executed_stop_loss_sim"
    return result["portfolio"], result["applied_trades"]


def _sim_stop_notice(forced_trades: list[dict]) -> str:
    """Blocco per il messaggio step: informa l'LLM degli stop eseguiti."""
    lines = ["⚠ STOP LOSS ESEGUITI AUTOMATICAMENTE PRIMA DEL TUO TURNO:"]
    for t in forced_trades:
        if str(t.get("status", "")).startswith("executed"):
            lines.append(f"  • {t.get('action')} {t.get('asset')} "
                         f"qty {t.get('executed_qty')} @ {t.get('executed_price')}"
                         f" — {t.get('thesis', '')}")
    lines.append("Le posizioni chiuse dallo stop NON sono piu' in portafoglio.")
    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════════════
# AI CALL
# ═════════════════════════════════════════════════════════════════════════

async def _call_r1(system_prompt: str, user_message: str,
                   max_retries: int = 3) -> str:
    """Chiama R1 con retry + fallback automatico Auriko→DeepSeek."""
    # Toggle Auriko/DeepSeek con fallback (sim_llm). Se Auriko esaurisce
    # i retry si ripiega su DeepSeek diretto invece di far fallire il run.
    from sim_llm import get_sim_llm_configs
    configs = get_sim_llm_configs("reasoner")
    if not configs or not configs[0][1]:
        raise ValueError("Nessuna API key LLM configurata (DEEPSEEK_API_KEY o AURIKO_API_KEY)")

    from sim_llm import (auriko_attempt_budget, record_auriko_failure,
                          record_auriko_success)

    last_error = ""
    for cfg_i, (api_url, api_key, model, provider) in enumerate(configs):
        is_last_cfg = (cfg_i == len(configs) - 1)
        has_fallback = not is_last_cfg
        # FAST-FAIL: Auriko con fallback → 1 tentativo, timeout 50s.
        # Critico per V2 multi-step: 3-5 step × budget run 480s. Senza
        # questo, un Auriko lento su un solo step abortiva l'intero run.
        cfg_retries, cfg_timeout = auriko_attempt_budget(provider, has_fallback)
        headers = {"Authorization": f"Bearer {api_key}",
                   "Content-Type": "application/json"}
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            # 6000 (era 4500): il prompt equity è lungo (regole regime +
            # settori + exit_plan) → 4500 token di output troncavano spesso
            # il JSON dei trade → parse_failed. Più margine = meno troncamenti.
            "max_tokens": 6000,
        }
        cfg_failed = False
        for attempt in range(cfg_retries):
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.post(
                        api_url, json=payload, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=cfg_timeout)
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            choice = (data.get("choices") or [{}])[0]
                            content = (choice.get("message") or {})\
                                .get("content") or ""
                            # Output troncato dal cap max_tokens: il JSON
                            # dei trade arriva a metà e il parse produce un
                            # hold SILENZIOSO (trade decisi ma mai eseguiti,
                            # osservato su run reale). Ritenta con budget
                            # maggiore finché ci sono tentativi.
                            if (choice.get("finish_reason") == "length"
                                    and attempt < cfg_retries - 1):
                                payload["max_tokens"] = 6500
                                last_error = (f"{provider} output troncato "
                                              "(finish_reason=length)")
                                logger.warning("[SIM-V2] %s — retry con "
                                               "max_tokens=6500", last_error)
                                continue
                            if provider == "auriko":
                                record_auriko_success()
                            return content
                        body = await resp.text()
                        last_error = f"{provider} HTTP {resp.status}: {body[:200]}"
                        if resp.status == 429 or 500 <= resp.status < 600:
                            if attempt < cfg_retries - 1:
                                await asyncio.sleep(2 ** (attempt + 1))
                                continue
                            cfg_failed = True
                            break
                        logger.warning(
                            "[SIM-V2] %s errore non-transitorio: %s — %s",
                            provider, last_error,
                            "fallback DeepSeek" if has_fallback else "no fallback"
                        )
                        cfg_failed = True
                        break
            except asyncio.TimeoutError:
                last_error = f"{provider} timeout ({cfg_timeout}s)"
                if attempt < cfg_retries - 1:
                    await asyncio.sleep(2 ** (attempt + 1))
                    continue
                cfg_failed = True
                break
            except aiohttp.ClientError as e:
                last_error = f"{provider} network: {e}"
                if attempt < cfg_retries - 1:
                    await asyncio.sleep(2 ** (attempt + 1))
                    continue
                cfg_failed = True
                break
        if cfg_failed:
            if provider == "auriko":
                record_auriko_failure()   # alimenta il circuit breaker
            if has_fallback:
                logger.warning("[SIM-V2] config '%s' fallita (%s) → fallback",
                                provider, last_error)
                continue
            break

    raise ValueError(f"Tutte le config LLM fallite (Simulator V2 R1): {last_error}")


def _parse_response(raw: str) -> dict:
    """Estrae [1] reading, [2] reasoning, [3] decision JSON."""
    txt = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

    def grab(label: str, until: list[str]) -> str:
        patterns = [
            rf"\[{label[0]}\]\s*{re.escape(label[2:])}",
            rf"\*\*\[{label[0]}\][^\*]*\*\*",
        ]
        for p in patterns:
            m = re.search(p, txt, re.IGNORECASE | re.MULTILINE)
            if m:
                start = m.end()
                end = len(txt)
                for u in until:
                    pat = u.replace("[", r"\[").replace("]", r"\]")
                    um = re.search(pat, txt[start:])
                    if um:
                        end = min(end, start + um.start())
                return txt[start:end].strip()
        return ""

    reading = grab("1 LETTURA", ["[2]", "[3]"])
    reasoning = grab("2 RAGIONAMENTO", ["[3]"])

    # Estrai JSON dalla sezione [3] o dall'intero testo
    decision_section = grab("3 DECISIONE", [])
    decision_json = {}
    candidates = [decision_section, txt]
    for candidate in candidates:
        if not candidate:
            continue
        # Cerca il blocco JSON con "trades": [...]
        m = re.search(r'\{[\s\S]*?"trades"[\s\S]*?\]\s*[,}][\s\S]*?\}', candidate)
        if m:
            try:
                decision_json = json.loads(m.group(0))
                break
            except Exception:
                pass
        # Fallback: blocco JSON generico
        m = re.search(r'\{[\s\S]*\}', candidate)
        if m:
            try:
                parsed = json.loads(m.group(0))
                if "trades" in parsed:
                    decision_json = parsed
                    break
            except Exception:
                pass

    trades = decision_json.get("trades", []) if isinstance(decision_json, dict) else []
    if not isinstance(trades, list):
        trades = []
    # Normalizza
    trades_norm = []
    for t in trades[:5]:
        if not isinstance(t, dict):
            continue
        # SAFE PARSE: se l'LLM manda allocation_pct non numerico (es. "high"),
        # float() lanciava ValueError e abortiva il parse dell'INTERO step
        # (perdendo anche i trade validi). Ora il trade malformato vale 0.
        try:
            alloc_pct = float(t.get("allocation_pct", 0) or 0)
        except (TypeError, ValueError):
            alloc_pct = 0.0
        trades_norm.append({
            "action": (t.get("action") or "").upper(),
            "asset": (t.get("asset") or "").upper(),
            "allocation_pct": alloc_pct,
            "conviction": (t.get("conviction") or "MEDIA").upper(),
            "thesis": (t.get("thesis") or "")[:400],
            # exit_plan: piano d'uscita dichiarato PRIMA di entrare
            # (stop + target). Viene attaccato alla posizione e
            # RIPRESENTATO all'AI a ogni turno — chiude il loop
            # "stop mentale dichiarato e mai più guardato". Come rr:
            # non enforced meccanicamente (insegnare, non vietare).
            "exit_plan": (t.get("exit_plan") or "")[:250],
            # rr: stima R/R dichiarata dall'AU (sezione 6.C del prompt).
            # NON enforced meccanicamente (insegnare, non vietare): viene
            # preservato per il debrief/memoria advice → l'AI puo' imparare
            # confrontando il R/R che aveva stimato col risultato reale
            # (es. "stimavi downside -1% ma hai perso -4%: tara il rischio").
            "rr": (t.get("rr") or "")[:200],
            # stop_loss_target: prezzo assoluto, VINCOLANTE se il flag
            # sim_stop_enforcement_enabled e' attivo (enforce_sim_stops).
            "stop_loss_target": _stop_target_of(t),
        })

    hold_summary = ""
    if isinstance(decision_json, dict):
        hold_summary = (decision_json.get("hold_summary") or "")[:400]

    # Distingue "hold intenzionale" da "JSON dei trade presente ma corrotto/
    # troncato": nel secondo caso i trade decisi andrebbero persi in
    # SILENZIO (lo step diventa un finto hold). I caller ritentano/abortono.
    parse_failed = bool(re.search(r'"trades"', txt)) and not decision_json

    return {
        "reading": reading or txt[:600],
        "reasoning": reasoning or "",
        "trades": trades_norm,
        "hold_summary": hold_summary,
        "raw": txt,
        "parse_failed": parse_failed,
    }


# ═════════════════════════════════════════════════════════════════════════
# PUBLIC API — start, step, finalize
# ═════════════════════════════════════════════════════════════════════════

async def fetch_full_price_series(
    tickers: list[str], step_dates: list[str], hardcoded_t0: dict[str, float] | None = None
) -> dict[str, dict[str, float]]:
    """
    Fetcha i close di TUTTI i ticker per TUTTE le step_dates in un'unica
    chiamata (range completo) per provider. Molto più efficiente che
    chiamare fetch_prices_at_date N×M volte.

    Returns: {ticker: {date: close_price, ...}}

    Strategia:
    1. Una chiamata Polygon range [step_dates[0], step_dates[-1]+5d] per ticker
    2. Per ogni step_date estrae il close più recente <= quella data
    3. Se Polygon vuoto e ticker presente in hardcoded_t0 → simula con
       random walk leggero a partire dal prezzo hardcoded (per scenari
       pre-2020 dove Polygon free tier non copre, o ticker non disponibili)
    """
    from data_fetchers import fetch_historical_range_async

    if not step_dates:
        return {}

    # Range allargato per gestire weekend/festivi e dati post-ultimo step
    start = step_dates[0]
    end_dt = datetime.fromisoformat(step_dates[-1]).date()
    end = (end_dt + timedelta(days=10)).isoformat()

    async def _fetch_one(ticker: str) -> tuple[str, dict[str, float]]:
        result = await fetch_historical_range_async(ticker, start, end)
        records = result.get("data") or []
        if not records:
            return ticker, {}
        # Mappa date → close
        date_to_close = {r["date"]: float(r["close"])
                         for r in records if r.get("close")}
        # Per ogni step_date, trova il close più recente <= quella data
        out = {}
        sorted_dates = sorted(date_to_close.keys())
        for sd in step_dates:
            valid = [d for d in sorted_dates if d <= sd]
            if valid:
                out[sd] = date_to_close[valid[-1]]
        return ticker, out

    results = await asyncio.gather(*[_fetch_one(t) for t in tickers])
    series: dict[str, dict[str, float]] = {t: prices for t, prices in results}

    # Fallback: se un ticker non ha dati Polygon ma ha hardcoded T0,
    # simula random-walk realistico per riempire i buchi (così i prezzi
    # cambiano tra i turni anche senza Polygon → P&L != 0).
    if hardcoded_t0:
        import random
        for t in tickers:
            if not series.get(t) and t in hardcoded_t0:
                base = float(hardcoded_t0[t])
                random.seed(hash(t + step_dates[0]) & 0xFFFFFFFF)
                walk = {}
                cur = base
                for sd in step_dates:
                    walk[sd] = round(cur, 2)
                    # Drift settimanale realistico: ±3% mean-reverting verso base
                    drift = random.uniform(-0.04, 0.04)
                    mr = (base - cur) / base * 0.3   # forza di mean reversion
                    cur = cur * (1 + drift + mr)
                series[t] = walk
                logger.info("[SIM-V2] Fallback random-walk per %s da $%.2f", t, base)

    return series


def extract_prices_at_date(
    series: dict[str, dict[str, float]], target_date: str
) -> dict[str, float]:
    """Estrae da una serie completa il prezzo a una data specifica."""
    return {t: prices[target_date] for t, prices in series.items()
            if target_date in prices}


async def start_run(
    category: str, num_steps: int, scenario_id: Optional[str] = None,
    initial_capital: float = 100000.0,
    commission_bps: float | None = None,
    run_mode: str = "manual",
) -> dict:
    """
    Inizializza una nuova partita simulator V2.
    Ritorna il payload completo (scenario + step_dates + portfolio iniziale
    + price_series complete pre-fetched + benchmark SPY) da consegnare al client.

    Pre-fetch ALL prices at start: garantisce che tutti gli step abbiano
    prezzi disponibili e che il P&L sia computabile in modo deterministico
    senza dipendere da Polygon disponibilità a runtime.

    commission_bps: costo per trade in basis points (default 10 bps = 0.10%).
    Pre-baked nel portfolio: ogni step lo eredita automaticamente.
    Pass 0 per test "no fees" comparativo (slippage rerun).
    """
    from simulator import scenarios as _scen
    from simulator.metrics import normalize_commission_bps

    if scenario_id:
        scenario = _scen.get_scenario_by_id(scenario_id)
    else:
        scenario = _scen.get_random_scenario(category)
    if not scenario:
        raise ValueError(f"Nessuno scenario disponibile (category={category})")
    # Anti-ripetizione: registra la giocata (anche per scenario_id esplicito)
    try:
        _scen.bump_scenario_play(scenario.get("id"))
    except Exception:
        pass

    num_steps = max(3, min(5, int(num_steps)))   # clip 3-5
    step_dates = compute_step_dates(scenario["period_start"], num_steps)
    universe = scenario.get("asset_universe", [])

    # Hardcoded T0 prices come fallback per random walk
    hardcoded_t0 = {m["ticker"]: float(m["price_t0"])
                    for m in scenario.get("market_data", [])}

    # Garantisce SPY nel pool del prefetch per il benchmark equity curve.
    # Anche se SPY non e' nell'universe (l'AI non puo' tradarlo), serve per
    # disegnare la linea S&P sovrapposta a quella del portafoglio.
    fetch_pool = list(universe)
    if "SPY" not in fetch_pool:
        fetch_pool.append("SPY")

    # Pre-fetch SERIE COMPLETA di tutti i prezzi per tutti gli step
    price_series = await fetch_full_price_series(fetch_pool, step_dates, hardcoded_t0)
    t0_prices = extract_prices_at_date(price_series, step_dates[0])

    # Diagnostica: quanti ticker hanno dati reali vs fallback
    real_count = sum(1 for t in universe if price_series.get(t)
                     and len(price_series[t]) >= num_steps)
    logger.info("[SIM-V2] start_run scenario=%s steps=%d, %d/%d ticker con prezzi completi",
                scenario["id"], num_steps, real_count, len(universe))

    bps = normalize_commission_bps(commission_bps)
    portfolio = make_initial_portfolio(initial_capital, commission_bps=bps)

    # ── Tracking ID per la live progress dashboard ────────────────────────
    # Il client carry-forward this ID nel scenario, gli endpoint /step e
    # /finalize lo usano per aggiornare active_runs registry.
    tracking_id = str(uuid4())
    try:
        from simulator import active_runs as _ar
        _ar.register(
            run_id=tracking_id, engine="v2_equity", mode=run_mode,
            category=scenario.get("category", "?"),
            scenario_id=scenario.get("id", "?"),
            scenario_title=scenario.get("title", ""),
            total_steps=num_steps,
        )
    except Exception as e:
        logger.debug("[SIM-V2] active_runs register fallita: %s", e)

    # ── Carica advice memory UNA SOLA VOLTA al start del run ─────────────
    # Stesso pattern del V1 runner. Lo storiamo nello scenario cosi' il client
    # lo rispedisce ad ogni step e l'execute_step lo inietta nel system prompt.
    # increment_apply_count una sola volta al start (non per ogni step).
    advice_block_text = ""
    advice_meta = {"category_key": "", "ids": []}
    try:
        from agents import sim_advisor
        block, cat_key, ids = sim_advisor.get_advice_block_for_runner(
            scenario, max_items=5
        )
        if block:
            advice_block_text = block
            advice_meta = {"category_key": cat_key, "ids": ids}
            try:
                sim_advisor.increment_apply_count(ids, cat_key)
            except Exception:
                pass
            logger.info("[SIM-V2] iniettati %d advice per categoria %s",
                        len(ids), cat_key)
    except Exception as e:
        logger.debug("[SIM-V2] advice injection skipped: %s", e)

    return {
        "scenario": {
            "id": scenario["id"],
            "category": scenario["category"],
            "title": scenario["title"],
            "brief": scenario.get("brief", ""),
            "asset_universe": universe,
            "period_start": scenario["period_start"],
            "period_end": scenario["period_end"],
            "num_steps": num_steps,
            "step_dates": step_dates,   # [T0, T+1w, ..., T+Nw]
            "headlines_master": scenario.get("headlines", []),
            # SERIE PREZZI PRE-FETCH: il client non chiama più Polygon
            # per gli step. Tutti i prezzi sono già qui dentro (compreso SPY).
            "price_series": price_series,
            "benchmark_ticker": "SPY",   # marker per UI/finalize
            "commission_bps": bps,
            # Advice memory pre-caricata: il client la rispedisce ad ogni
            # step → execute_step la inietta nel system prompt.
            "advice_block": advice_block_text,
            "advice_meta": advice_meta,
            # Tracking ID per la "Run in corso" dashboard
            "tracking_id": tracking_id,
            "run_mode": run_mode,
        },
        "portfolio": portfolio,
        "t0_prices": t0_prices,
        "total_steps": num_steps,
    }


def _split_headlines_for_step(
    headlines_master: list[str], step_index: int, num_steps: int
) -> list[str]:
    """
    Distribuisce le headline tra gli step in modo deterministico.
    Step 0 vede le prime ~50%, gli altri step ricevono porzioni successive.
    """
    if not headlines_master:
        return []
    if num_steps <= 0:
        return headlines_master
    chunk = max(1, len(headlines_master) // num_steps)
    if step_index == 0:
        # T0 vede una porzione più ampia (setup)
        return headlines_master[: max(chunk, len(headlines_master) // 2)]
    start = chunk * step_index
    end = chunk * (step_index + 1)
    return headlines_master[start:end] or headlines_master[-chunk:]


async def execute_step(
    scenario: dict, portfolio: dict, history: list[dict], step_index: int
) -> dict:
    """
    Esegue lo step N:
      1. Estrae prezzi dalla serie pre-fetched (no più chiamate Polygon)
      2. Costruisce contesto AI con price changes vs T0 e vs settimana scorsa
      3. Chiama AI
      4. Applica trade al portfolio
      5. Ritorna step_data dettagliato (qty, price, $ amount per ogni trade)
    """
    step_dates = scenario.get("step_dates", [])
    num_steps = scenario.get("num_steps", len(step_dates) - 1)
    if step_index >= num_steps:
        raise ValueError(f"step_index {step_index} >= num_steps {num_steps}")

    # Tracking: aggiorna registry "calling_ai" prima della chiamata R1
    tracking_id = scenario.get("tracking_id")
    if tracking_id:
        try:
            from simulator import active_runs as _ar
            _ar.update_step(tracking_id, step_index, status="calling_ai")
        except Exception:
            pass

    target_date = step_dates[step_index]
    universe = scenario.get("asset_universe", [])
    price_series = scenario.get("price_series", {})

    # 1. Estrai prezzi dalla serie pre-fetched (no fallback runtime: già
    #    gestito al start_run)
    prices = extract_prices_at_date(price_series, target_date)
    if not prices:
        # Caso patologico: serie vuota → usa hardcoded
        prices = {m["ticker"]: float(m["price_t0"])
                  for m in scenario.get("market_data", [])}

    # T0 prices per calcolare i delta cumulati
    t0_prices = extract_prices_at_date(price_series, step_dates[0])
    # Prezzi settimana scorsa
    prev_prices = {}
    if step_index > 0:
        prev_prices = extract_prices_at_date(price_series, step_dates[step_index - 1])

    # 1b. Stop enforcement sim (flag OFF default): esegue gli stop dichiarati
    #     PRIMA del turno LLM, come il watchdog nel Live.
    portfolio, forced_stop_trades = enforce_sim_stops(portfolio, prices)

    # 2. Valuta portfolio prima dei trade
    valuation_before = compute_portfolio_value(portfolio, prices)

    # 3. Headline per questo step
    headlines = _split_headlines_for_step(
        scenario.get("headlines_master", []), step_index, num_steps
    )

    # 4. Build user message arricchito (con price changes)
    user_msg = _build_step_message(
        scenario, portfolio, valuation_before, prices, prev_prices, t0_prices,
        headlines, history, step_index, num_steps, target_date
    )
    if forced_stop_trades:
        user_msg = _sim_stop_notice(forced_stop_trades) + "\n\n" + user_msg

    # 5. Call AI — inietta direttive utente + advice memory in cima al system prompt
    try:
        from agents.decision import _build_directives_block
        sys_prompt = _build_directives_block() + SIM_V2_SYSTEM_PROMPT
    except Exception:
        sys_prompt = SIM_V2_SYSTEM_PROMPT
    # Advice memory: pre-caricata da start_run e veicolata via scenario.
    # Stesso pattern dei "MEMORIA — run recenti" del V1, ma stateless: il
    # blocco e' calcolato una volta sola al T0 e rispedito ad ogni step.
    advice_block = (scenario.get("advice_block") or "").strip()
    if advice_block:
        sys_prompt = advice_block + "\n\n" + ("═" * 60) + "\n" + sys_prompt
    raw = await _call_r1(sys_prompt, user_msg)
    parsed = _parse_response(raw)
    if parsed.get("parse_failed"):
        # JSON dei trade presente ma corrotto/troncato: un retry.
        logger.warning("[SIM-V2] decisione non parsabile, retry singolo")
        raw = await _call_r1(sys_prompt, user_msg)
        parsed = _parse_response(raw)
        if parsed.get("parse_failed"):
            # BUGFIX crash sistematico run EQUITY: prima qui si faceva
            # raise ValueError → lo scheduler abortiva l'INTERA run
            # multi-step. Il prompt equity è più lungo del crypto → si
            # tronca più spesso → le run equity crashavano quasi sempre
            # (auto-mode salvava ~solo crypto, 4:1). NON crashiamo più la
            # run: degradiamo questo SINGOLO step a NO-TRADE in modo
            # VISIBILE (log ERROR + hold_summary esplicito), così la run
            # prosegue e si completa. Non è un hold silenzioso.
            logger.error("[SIM-V2] step %d: output non parsabile dopo retry "
                         "→ degrado a NO-TRADE (run NON abortita)", step_index)
            parsed = {
                "reading": parsed.get("reading") or "(output non parsabile)",
                "reasoning": parsed.get("reasoning") or "",
                "trades": [],
                "hold_summary": "⚠ PARSE FAILED: output AI troncato/corrotto, "
                                "step saltato senza trade (run non abortita).",
                "raw": parsed.get("raw", ""),
                "parse_failed": True,
            }

    # 6. Apply trades — aggiorna lo status del registry
    if tracking_id:
        try:
            from simulator import active_runs as _ar
            _ar.update_status(tracking_id, "applying_trades")
        except Exception:
            pass
    apply_result = apply_trades(portfolio, parsed["trades"], prices)
    new_portfolio = apply_result["portfolio"]
    # Gli stop forzati appaiono in testa: sono avvenuti PRIMA dei trade LLM
    applied_trades = forced_stop_trades + apply_result["applied_trades"]

    # 7. Valuta dopo i trade (con stessi prezzi, cambia solo composizione)
    valuation_after = compute_portfolio_value(new_portfolio, prices)

    # 8. Costruisci price changes dict per UI (asset → {curr, prev, t0, chg_1w, chg_total})
    price_changes = {}
    for t in universe:
        cur = prices.get(t)
        if not cur:
            continue
        t0 = t0_prices.get(t)
        prev = prev_prices.get(t)
        price_changes[t] = {
            "current": round(cur, 2),
            "t0": round(t0, 2) if t0 else None,
            "prev": round(prev, 2) if prev else None,
            "chg_1w_pct": round((cur - prev) / prev * 100, 2) if prev else None,
            "chg_total_pct": round((cur - t0) / t0 * 100, 2) if t0 else None,
        }

    return {
        "step_index": step_index,
        "step_date": target_date,
        "is_last_step": (step_index + 1 >= num_steps),
        "prices": prices,
        "price_changes": price_changes,   # NUOVO: per UI ricca
        "headlines": headlines,
        "ai_reading": parsed["reading"],
        "ai_reasoning": parsed["reasoning"],
        "ai_trades": parsed["trades"],
        "ai_hold_summary": parsed["hold_summary"],
        "applied_trades": applied_trades,   # con executed_qty + executed_price + executed_value
        "valuation_before": valuation_before,
        "valuation_after": valuation_after,
        "new_portfolio": new_portfolio,
        "raw_response": parsed["raw"][:3000],
    }


def _build_step_message(
    scenario: dict, portfolio: dict, valuation: dict,
    prices: dict, prev_prices: dict, t0_prices: dict,
    headlines: list[str], history: list[dict],
    step_index: int, num_steps: int, target_date: str
) -> str:
    """Costruisce il messaggio user per il modello con prezzi + delta arricchiti."""
    parts = []

    # Header turno
    parts.append("═" * 60)
    if step_index == 0:
        parts.append(f"TURNO 1 di {num_steps} — INIZIO PARTITA")
        parts.append(f"Data simulata: {target_date}")
        parts.append(f"Capitale iniziale: ${valuation['initial_capital']:,.2f}")
        parts.append("Portafoglio: VUOTO. Hai tutto il capitale in cash.")
    else:
        parts.append(f"TURNO {step_index + 1} di {num_steps}")
        parts.append(f"Data simulata: {target_date}")
        parts.append("È passata 1 settimana dal turno precedente.")
    parts.append("═" * 60)
    parts.append("")

    # Stato portafoglio corrente
    parts.append("📊 IL TUO PORTAFOGLIO ATTUALE:")
    parts.append(f"  Cash disponibile: ${valuation['cash']:,.2f}")
    parts.append(f"  Valore totale: ${valuation['total_value']:,.2f}")
    if valuation.get("total_pnl_pct") is not None and step_index > 0:
        sign = "+" if valuation["total_pnl"] >= 0 else ""
        parts.append(f"  P&L totale: {sign}${valuation['total_pnl']:,.2f} "
                     f"({sign}{valuation['total_pnl_pct']:.2f}%)")
    if valuation.get("positions"):
        parts.append("  Posizioni aperte:")
        for p in valuation["positions"]:
            side_label = "LONG" if p.get("side") == "long" else "SHORT"
            sign = "+" if p["unrealized_pnl"] >= 0 else ""
            value_now = p.get("market_value", 0)
            parts.append(
                f"    [{side_label}] {p['asset']}: {p['quantity']:.2f} unità "
                f"@ avg ${p['avg_entry_price']:.2f} (ora ${p['current_price']:.2f}) "
                f"valore ${value_now:,.2f} | P&L: {sign}${p['unrealized_pnl']:,.2f} "
                f"({sign}{p['unrealized_pnl_pct']:.2f}%)"
            )
            # Ripresenta il piano d'uscita dichiarato all'ingresso: l'AI
            # deve confrontarlo con lo stato attuale a OGNI turno (check
            # esplicito nella procedura [2]) invece di dimenticarselo.
            if p.get("exit_plan"):
                parts.append(f"      ⤷ IL TUO PIANO D'USCITA: {p['exit_plan']}")
    else:
        parts.append("  Posizioni aperte: nessuna")
    parts.append("")

    # Asset universe + prezzi correnti CON DELTA (1w + cumulato)
    parts.append(f"🎯 ASSET DISPONIBILI ({target_date}) — prezzo + variazioni:")
    parts.append(f"  {'TICKER':<8} {'PREZZO':>10}  {'1 SETT.':>10}  {'DAL T0':>10}")
    for ticker in scenario.get("asset_universe", []):
        cur = prices.get(ticker)
        if not cur:
            parts.append(f"  {ticker:<8} {'(no data)':>10}")
            continue
        prev = prev_prices.get(ticker)
        t0 = t0_prices.get(ticker)
        chg_1w = ((cur - prev) / prev * 100) if prev else None
        chg_t0 = ((cur - t0) / t0 * 100) if (t0 and step_index > 0) else None
        chg_1w_str = f"{chg_1w:+.2f}%" if chg_1w is not None else "—"
        chg_t0_str = f"{chg_t0:+.2f}%" if chg_t0 is not None else "—"
        parts.append(f"  {ticker:<8} ${cur:>9.2f}  {chg_1w_str:>10}  {chg_t0_str:>10}")
    parts.append("")

    # Headlines
    if headlines:
        parts.append(f"📰 HEADLINE (settimana del {target_date}):")
        for h in headlines:
            parts.append(f"  • {h}")
        parts.append("")

    # ── CONTESTO GLOBALE — cross-sector + macro ───────────────────────────
    # Questo blocco fornisce segnali su settori ESTERNI al primario dello
    # scenario, permettendo rotazione settoriale intelligente. Mostrato ad
    # ogni turno: il regime macro è costante nel periodo simulato.
    global_ctx = scenario.get("global_context", {})
    if global_ctx:
        parts.append("🌍 CONTESTO GLOBALE (guarda oltre il settore primario):")
        macro = global_ctx.get("macro", [])
        if macro:
            parts.append("  📊 Macro: " + " | ".join(macro))
        cross = global_ctx.get("cross_sector", [])
        if cross:
            parts.append("  🔄 Segnali cross-settoriali:")
            for cs in cross:
                direction = cs.get("direction", "neutral")
                icon = "🟢" if direction == "bullish" else (
                    "🔴" if direction == "bearish" else "🟡"
                )
                parts.append(f"    {icon} {cs['sector']}: {cs['signal']}")
        second_order = global_ctx.get("second_order", "")
        if second_order:
            parts.append(f"  💡 Effetti di 2° ordine: {second_order}")
        parts.append("")
    # ─────────────────────────────────────────────────────────────────────

    # Storico decisioni (se non è il primo turno)
    if step_index > 0 and history:
        parts.append("📜 LE TUE DECISIONI PRECEDENTI:")
        for h in history[-3:]:   # ultimi 3 turni
            t_idx = h.get("step_index", 0)
            t_date = h.get("step_date", "?")
            trades = h.get("ai_trades", [])
            if trades:
                trade_summary = ", ".join(
                    f"{t['action']} {t['asset']} {t['allocation_pct']:.0f}%"
                    for t in trades
                )
                parts.append(f"  Turno {t_idx + 1} ({t_date}): {trade_summary}")
            else:
                parts.append(f"  Turno {t_idx + 1} ({t_date}): nessuna azione (hold)")

        # Performance dell'ultimo turno
        last = history[-1]
        last_val = (last.get("valuation_after") or {}).get("total_value")
        cur_val = valuation.get("total_value")
        if last_val and cur_val:
            wk_chg = ((cur_val - last_val) / last_val * 100) if last_val else 0
            sign = "+" if wk_chg >= 0 else ""
            parts.append(f"  → Performance settimana scorsa: {sign}{wk_chg:.2f}%")
        parts.append("")

    parts.append("═" * 60)
    parts.append("DECIDI ORA: cosa fai per la PROSSIMA SETTIMANA?")
    parts.append("Ricorda: questa decisione sarà riconsiderata solo tra 7 giorni.")
    parts.append("Procedura OBBLIGATORIA: [1] Lettura, [2] Ragionamento, [3] Decisione JSON.")

    return "\n".join(parts)


async def finalize_run(
    scenario: dict, portfolio: dict, history: list[dict],
    persist: bool = True, run_mode: str = "manual",
) -> dict:
    """
    Chiude la simulazione:
      1. Estrae prezzi finali dalla serie pre-fetched
      2. Calcola P&L finale + benchmark SPY
      3. Genera debrief AI
      4. Costruisce serie portfolio_value per chart
      5. Salva nel DB se persist=True
    """
    step_dates = scenario.get("step_dates", [])
    if not step_dates:
        raise ValueError("scenario senza step_dates")
    final_date = step_dates[-1]   # T+Nw
    price_series = scenario.get("price_series", {})
    universe = scenario.get("asset_universe", [])

    # Prezzi finali dalla serie pre-fetched
    final_prices = extract_prices_at_date(price_series, final_date)
    if not final_prices and history:
        final_prices = history[-1].get("prices", {})
    if not final_prices:
        # Fallback estremo
        final_prices = {m["ticker"]: float(m["price_t0"])
                        for m in scenario.get("market_data", [])}

    final_valuation = compute_portfolio_value(portfolio, final_prices)

    # Benchmark SPY dalla serie pre-fetched (se SPY è nell'universo)
    t0_date = step_dates[0]
    spy_t0 = price_series.get("SPY", {}).get(t0_date)
    spy_final = price_series.get("SPY", {}).get(final_date)
    if not (spy_t0 and spy_final):
        # SPY non nell'universo: fetcha al volo solo lui
        from data_fetchers import fetch_historical_range_async
        end_dt = (datetime.fromisoformat(final_date).date() + timedelta(days=10)).isoformat()
        spy_data = await fetch_historical_range_async("SPY", t0_date, end_dt)
        spy_records = spy_data.get("data") or []
        if spy_records:
            spy_t0_rec = next((r for r in spy_records if r["date"] >= t0_date), None)
            spy_final_rec = next((r for r in reversed(spy_records)
                                  if r["date"] <= final_date), None)
            if spy_t0_rec and spy_final_rec:
                spy_t0 = float(spy_t0_rec["close"])
                spy_final = float(spy_final_rec["close"])

    benchmark_pnl_pct = None
    if spy_t0 and spy_final:
        benchmark_pnl_pct = round((spy_final - spy_t0) / spy_t0 * 100, 2)

    # Description reveal
    from simulator import scenarios as _scen
    full_scenario = _scen.get_scenario_by_id(scenario.get("id", ""))
    reveal = full_scenario.get("description_reveal", "") if full_scenario else ""

    # Costruisci serie portfolio_value per chart.
    # T0 = capitale iniziale (prima di qualunque trade). Senza questo punto la
    # equity curve "parte" dal valore dopo il primo step e perde il segmento
    # iniziale di crescita/perdita.
    initial = float(final_valuation.get("initial_capital",
                     final_valuation.get("total_value", 100000)))
    portfolio_value_series = [{
        "step_index": -1,
        "step_date": step_dates[0],
        "value": initial,
    }]
    for h in history:
        portfolio_value_series.append({
            "step_index": h.get("step_index"),
            "step_date": h.get("step_date"),
            "value": (h.get("valuation_after") or {}).get("total_value"),
        })
    # Aggiungi punto finale (valuation con prezzi finali)
    portfolio_value_series.append({
        "step_index": len(history),
        "step_date": final_date,
        "value": final_valuation.get("total_value"),
    })

    # ── Benchmark equity curve (SPY) sovrapposta al portafoglio ────────────
    # Stessa cadenza degli step: $initial all'inizio, scalato per il return SPY
    # cumulato a ogni data. Permette al chart "Equity vs Benchmark" di mostrare
    # le due linee sulla stessa scala.
    benchmark_value_series = _build_benchmark_value_series(
        price_series, "SPY", step_dates, initial, history, final_date
    )

    # Per-asset performance breakdown (cosa ha guadagnato/perso ciascuna posizione)
    asset_breakdown = []
    for p in final_valuation.get("positions", []):
        asset_breakdown.append({
            "asset": p["asset"],
            "side": p["side"],
            "quantity": p["quantity"],
            "avg_entry_price": p["avg_entry_price"],
            "final_price": p["current_price"],
            "market_value": p["market_value"],
            "unrealized_pnl": p["unrealized_pnl"],
            "unrealized_pnl_pct": p["unrealized_pnl_pct"],
        })

    # ── Sanitizza la serie equity PRIMA delle metriche ────────────────────
    # I punti con value None/NaN/<=0 (es. valuation mancante a uno step)
    # venivano 'skippati' silenziosamente dentro metrics, degradando
    # Sharpe/MDD/drawdown senza alcun segnale d'errore. Ora li scartiamo
    # esplicitamente e logghiamo se la serie e' incompleta.
    _clean_equity = []
    _dropped_pts = 0
    for _pt in portfolio_value_series:
        try:
            _vf = float(_pt.get("value"))
        except (TypeError, ValueError):
            _vf = None
        if _vf is None or _vf != _vf or _vf <= 0:
            _dropped_pts += 1
            continue
        _clean_equity.append(_pt)
    if _dropped_pts:
        logger.warning("[SIM] finalize: %d/%d punti equity invalidi (None/NaN/<=0) "
                       "scartati prima delle metriche — risultati su serie ridotta",
                       _dropped_pts, len(portfolio_value_series))
    if len(_clean_equity) >= 2:
        portfolio_value_series = _clean_equity

    # ── METRICHE QUANTITATIVE: Sharpe, MDD, Profit Factor, Expectancy, ecc.
    # Calcolate da metrics.py su equity_curve + history.
    from simulator import metrics as _metrics
    quant_metrics = _metrics.compute_all_metrics(
        equity_curve=portfolio_value_series,
        history=history,
        final_valuation=final_valuation,
        step_unit_days=7.0,
        periods_per_year=_metrics.ANNUALIZATION_EQUITY,
    )

    # Debrief AI (narrativa) + Lessons learned (insights azionabili).
    # Eseguiti in parallelo per minimizzare la latenza del finalize_run:
    # entrambi sono chiamate AI separate ma indipendenti.
    debrief, lessons_learned = await asyncio.gather(
        _generate_debrief(scenario, history, final_valuation,
                          benchmark_pnl_pct, reveal),
        _generate_lessons_learned(scenario, history, final_valuation,
                                   benchmark_pnl_pct, reveal),
        return_exceptions=False,
    )

    # Outcome v2 (confronto a pari esposizione); il legacy resta salvato
    # per confronto/rollback nella riclassificazione retroattiva.
    _ov2 = classify_outcome_v2(final_valuation, history,
                               benchmark_value_series, benchmark_pnl_pct)

    result = {
        "scenario_id": scenario.get("id"),
        "final_date": final_date,
        "final_prices": final_prices,
        "final_valuation": final_valuation,
        "benchmark_spy_pnl_pct": benchmark_pnl_pct,
        "benchmark_value_series": benchmark_value_series,   # per chart vs benchmark
        "outcome": _ov2["outcome"],
        "outcome_legacy": _classify_outcome(final_valuation, benchmark_pnl_pct),
        "outcome_v2_inputs": _ov2,
        "debrief": debrief,
        # Lista [{title, text, type}, ...] iniettata in _auto_save_thesis_advice
        "lessons_learned": lessons_learned or [],
        "description_reveal": reveal,
        "num_steps": scenario.get("num_steps"),
        "portfolio_value_series": portfolio_value_series,   # per chart
        "asset_breakdown": asset_breakdown,                 # per breakdown UI
        "price_series": price_series,                       # per chart prezzi
        "step_dates": step_dates,
        # Metriche quantitative + dataset per i grafici statistici
        "quant_metrics": quant_metrics,
        # Riassunto fees per UI
        "total_commissions_paid": float(
            final_valuation.get("total_commissions_paid", 0) or 0
        ),
        "commission_bps": float(final_valuation.get("commission_bps", 0) or 0),
    }

    if persist:
        # Tracking: marca finalizing prima del persist
        tracking_id = scenario.get("tracking_id")
        if tracking_id:
            try:
                from simulator import active_runs as _ar
                _ar.update_status(tracking_id, "finalizing")
            except Exception:
                pass
        try:
            persisted_id = _persist_run(scenario, history, result, run_mode=run_mode)
            result["persisted_run_id"] = persisted_id
        except Exception as e:
            logger.error("[SIM-V2] persist failed: %s", e, exc_info=True)
            result["persist_error"] = str(e)[:200]
        # Auto-save Valutazione tesi (debrief) come advice nella memoria
        # categorizzata. Cosi' ogni run completato contribuisce automatica-
        # mente alla "memoria operativa" del Simulator senza richiedere
        # all'utente di cliccare "Salva consiglio" nella UI Analizza & Migliora.
        try:
            _auto_save_thesis_advice(scenario, result, run_mode=run_mode)
        except Exception as _e:
            logger.warning("[SIM-V2] auto-save advice fallito (non critico): %s", _e)
        # Tracking: marca completed (anche su persist fail, il run e' fatto)
        if tracking_id:
            try:
                from simulator import active_runs as _ar
                _ar.mark_completed(
                    tracking_id,
                    outcome=result.get("outcome"),
                    pnl_pct=(result.get("final_valuation") or {}).get("total_pnl_pct"),
                    persisted_run_id=result.get("persisted_run_id"),
                )
            except Exception:
                pass

    return result


def _build_benchmark_value_series(
    price_series: dict[str, dict[str, float]], benchmark_ticker: str,
    step_dates: list[str], initial: float,
    history: list[dict], final_date: str,
) -> list[dict]:
    """
    Costruisce la equity curve del benchmark (SPY o BTC) sulla stessa cadenza
    di step della partita. Allineata 1-a-1 col portfolio_value_series cosi'
    la UI puo' sovrapporre le due linee sullo stesso asse temporale.

    Strategia:
      - T0: $initial
      - Ad ogni step_date: $initial * (price_at_step / price_at_T0)

    Se il benchmark non ha dati (nessuna serie), ritorna lista vuota → il
    chart mostra solo il portfolio.
    """
    bench = price_series.get(benchmark_ticker, {})
    if not bench:
        return []
    t0 = bench.get(step_dates[0])
    if not t0 or t0 <= 0:
        return []

    # Punto T0
    out = [{"step_index": -1, "step_date": step_dates[0], "value": round(initial, 2)}]
    # Per ogni step della history, prendi il valore del benchmark a quella data
    for h in history or []:
        sd = h.get("step_date")
        bp = bench.get(sd) if sd else None
        if bp and bp > 0:
            v = initial * (bp / t0)
        else:
            # Se manca il prezzo a quella data, mantieni l'ultimo valore (no jump)
            v = out[-1]["value"]
        out.append({
            "step_index": h.get("step_index"),
            "step_date": sd,
            "value": round(v, 2),
        })
    # Punto finale
    bp_final = bench.get(final_date)
    if bp_final and bp_final > 0:
        v_final = initial * (bp_final / t0)
    else:
        v_final = out[-1]["value"]
    out.append({
        "step_index": len(history) if history else 0,
        "step_date": final_date,
        "value": round(v_final, 2),
    })
    return out


def _classify_outcome(valuation: dict, benchmark_pct: Optional[float]) -> str:
    """
    Verde/giallo/rosso in base a P&L e P&L vs benchmark.

    REGOLE (in ordine di priorita'):

    VERDE — qualsiasi di queste condizioni:
      - pnl >= 3.0%                            (performance assoluta forte)
      - pnl > 0 AND delta_vs_benchmark >= 0.5  (batte benchmark con margine)
      - pnl > 0 AND benchmark e' None (no benchmark: basta positivo)

    ROSSO — qualsiasi di queste condizioni:
      - pnl <= -3.0%                           (perdita significativa)
      - delta_vs_benchmark <= -2.0             (lontano sotto benchmark)

    GIALLO — tutto il resto (tipicamente: pnl basso, o positivo ma vicino
    al benchmark, o leggermente negativo).

    RAZIONALE FIX: la versione precedente richiedeva
    delta > 1.0% AND pnl > 0 per essere verde. Una simulazione con
    pnl = +2.86%, benchmark = +1.84% (delta = +1.02%) era classificata
    yellow se delta veniva computato con piccole differenze di precisione
    (es. 0.99 vs 1.01) — soglia troppo stretta. La nuova logica:
      • Da' peso a performance assoluta forte (>=3% green indipendentemente)
      • Soglia delta abbassata a 0.5% (margine ragionevole vs benchmark)
      • Mantiene la simmetria red sotto -3% o delta -2%
    """
    pnl = float(valuation.get("total_pnl_pct", 0) or 0)
    delta = None
    if benchmark_pct is not None:
        delta = pnl - float(benchmark_pct)

    # ── Casi GREEN (priorita' alla performance assoluta) ────────────────
    # 1) Performance assoluta forte: >= 3% e' oggettivamente buono
    #    indipendentemente dal benchmark.
    if pnl >= 3.0:
        return "green"

    # 2) Batte benchmark con margine ragionevole (>= 0.5%) E pnl positivo.
    if pnl > 0 and delta is not None and delta >= 0.5:
        return "green"

    # 3) No benchmark + pnl positivo qualsiasi.
    if pnl > 0 and delta is None:
        return "green"

    # ── Casi RED (gravita' assoluta — pnl > 0 NON e' mai red) ───────────
    # Un bot con pnl positivo non viene mai marcato red anche se ha
    # sotto-performato il benchmark. Sotto-performance vs benchmark con
    # pnl positivo e' yellow.
    if pnl <= -3.0:
        return "red"
    # Sotto-performance vs benchmark, ma solo se pnl < 0 (bot in perdita)
    if pnl < 0 and delta is not None and delta <= -2.0:
        return "red"

    # ── Tutto il resto: giallo ──────────────────────────────────────────
    return "yellow"


def _step_exposure(valuation: dict | None) -> float | None:
    """
    Esposizione NETTA del portafoglio da una valuation di step:
    1 - cash/total_value. Con gli short il cash cresce oltre il total
    → esposizione negativa (beta negativo vs benchmark): corretto per il
    calcolo dello shadow. Clamp [-1.5, 1.5] contro valuation degeneri.
    None se la valuation non e' utilizzabile.
    """
    if not isinstance(valuation, dict):
        return None
    try:
        total = float(valuation.get("total_value") or 0)
        cash = float(valuation.get("cash") or 0)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    return round(max(-1.5, min(1.5, 1.0 - cash / total)), 6)


def compute_shadow_return_pct(history: list[dict],
                              benchmark_value_series: list[dict]) -> float | None:
    """
    Rendimento % del "benchmark ombra": cosa avrebbe reso un portafoglio
    passivo con la STESSA esposizione per-step dell'agente, investita sul
    benchmark. E' il confronto a pari prudenza — il benchmark 100% investito
    contro un portafoglio al 12-17% e' la causa n.1 del giallo-tutto
    (analisi 13/07).

    shadow = 100 * Σ_k exposure(t_k) * (bench[k+1]/bench[k] - 1)

    dove exposure(t_k) e' l'esposizione netta del portafoglio alla data del
    punto k della serie benchmark (dopo i trade dello step k; 0 al punto T0
    pre-trade). Somma semplice, non composta: adeguata su run brevi.

    Ritorna None se serie o history non sono utilizzabili (il chiamante
    fa fallback sui criteri legacy).
    """
    series = [p for p in (benchmark_value_series or [])
              if isinstance(p, dict) and p.get("value")]
    if len(series) < 2 or not history:
        return None

    # step_index -> esposizione dopo i trade di quello step
    exp_map: dict[int, float] = {}
    for h in history:
        if not isinstance(h, dict):
            continue
        e = _step_exposure(h.get("valuation_after"))
        if e is not None and h.get("step_index") is not None:
            exp_map[int(h["step_index"])] = e
    if not exp_map:
        return None

    shadow = 0.0
    last_exp = 0.0
    for k in range(len(series) - 1):
        try:
            v0 = float(series[k]["value"])
            v1 = float(series[k + 1]["value"])
        except (TypeError, ValueError, KeyError):
            continue
        if v0 <= 0:
            continue
        idx = series[k].get("step_index")
        if idx is not None and int(idx) in exp_map:
            last_exp = exp_map[int(idx)]
        exp = 0.0 if (idx is not None and int(idx) == -1) else last_exp
        shadow += exp * (v1 / v0 - 1.0)
    return round(shadow * 100.0, 4)


def classify_outcome_v2(final_valuation: dict, history: list[dict],
                        benchmark_value_series: list[dict],
                        benchmark_pnl_pct: Optional[float]) -> dict:
    """
    Outcome v2: giudica la DECISIONE a pari esposizione, non il portafoglio
    contro un benchmark full-invested.

    Sostituisce i criteri legacy (_classify_outcome) che producevano ~60%
    di run gialle: le difese riuscite in crash (pnl<0 ma molto meglio del
    mercato) e il cash-drag nei rally (pnl>0 ma molto peggio) non avevano
    mai un verdetto netto (analisi 13/07).

    REGOLE (in ordine):
      1. pnl <= -3.0%                              -> red   (override gravita')
      2. HOLD puro (zero trade eseguiti, |expo|~0):
           bench <= -1.5% -> green (danno evitato)
           bench >= +1.5% -> red   (occasione persa)
           altrimenti     -> yellow
      3. delta_eq = pnl - shadow (benchmark a pari esposizione):
           >= +0.4 -> green ; <= -0.4 -> red ; zona morta -> yellow
      4. Dati insufficienti -> fallback criteri legacy.

    Ritorna un dict con outcome + input di calcolo (salvati in full_data
    per trasparenza e per la riclassificazione retroattiva).
    """
    pnl = float((final_valuation or {}).get("total_pnl_pct", 0) or 0)

    exposures = []
    for h in history or []:
        if isinstance(h, dict):
            e = _step_exposure(h.get("valuation_after"))
            if e is not None:
                exposures.append(e)
    exposure_avg = (round(sum(exposures) / len(exposures), 4)
                    if exposures else None)

    def _result(outcome, method, shadow=None, delta_eq=None):
        return {
            "outcome": outcome, "method": method,
            "shadow_benchmark_pct": shadow, "delta_eq": delta_eq,
            "exposure_avg": exposure_avg,
        }

    # 1) Override gravita': una perdita grossa resta rossa comunque.
    if pnl <= -3.0:
        return _result("red", "pnl_override")

    # 2) HOLD puro: giudica la scelta di stare fuori dal mercato.
    executed = any(
        str(t.get("status", "")).startswith("executed")
        for h in (history or []) if isinstance(h, dict)
        for t in (h.get("applied_trades") or []) if isinstance(t, dict)
    )
    abs_expo_avg = (sum(abs(e) for e in exposures) / len(exposures)
                    if exposures else 0.0)
    if not executed and abs_expo_avg < 0.02 and benchmark_pnl_pct is not None:
        b = float(benchmark_pnl_pct)
        if b <= -1.5:
            return _result("green", "hold_rule")
        if b >= 1.5:
            return _result("red", "hold_rule")
        return _result("yellow", "hold_rule")

    # 3) Confronto a pari esposizione.
    shadow = compute_shadow_return_pct(history, benchmark_value_series)
    if shadow is not None:
        delta_eq = round(pnl - shadow, 4)
        if delta_eq >= 0.4:
            return _result("green", "shadow_v2", shadow, delta_eq)
        if delta_eq <= -0.4:
            return _result("red", "shadow_v2", shadow, delta_eq)
        return _result("yellow", "shadow_v2", shadow, delta_eq)

    # 4) Fallback: dati insufficienti -> criteri legacy.
    return _result(_classify_outcome(final_valuation, benchmark_pnl_pct),
                   "legacy_fallback")


async def _generate_lessons_learned(
    scenario: dict, history: list[dict], final_valuation: dict,
    benchmark_pct: Optional[float], reveal: str
) -> list[dict]:
    """
    Genera 2-4 LESSON LEARNED specifiche e azionabili dal run, via DeepSeek-R1.

    Differenza con il debrief:
      - Debrief: narrativa di 4-6 frasi che riassume "cosa è successo"
      - Lessons: 2-4 insights ATTUABILI, ognuno trasferibile a scenari simili
        futuri (es. "Quando il VIX supera 25 in conflitto militare, allocare
        15-25% in Defense entro 1 settimana dall'evento")

    Ogni lesson è un dict {title, text, type} dove:
      - type: "rotazione" | "timing" | "size" | "stop_loss" | "diversificazione"
              | "regime" | "altro"
      - title: max 60 char (sintesi della lesson)
      - text: max 250 char (azione specifica + condizione di applicabilità)

    Best-effort: se la chiamata fallisce o il JSON è malformato, ritorna []
    e l'auto-save proseguirà con il solo debrief.
    """
    if not history:
        return []

    lessons_prompt = """Sei un coach di trading post-mortem. L'utente ha completato
una simulazione su uno scenario storico. Estrai 2-4 LESSON LEARNED specifiche e
azionabili da applicare in scenari futuri simili.

Ogni lesson DEVE essere:
- SPECIFICA: riferita a una decisione/momento preciso del run, non generica
- AZIONABILE: contiene una regola pratica (cosa fare quando si presenta X)
- CATEGORIZZATA: un type tra: rotazione, timing, size, stop_loss, diversificazione, regime, altro
- BREVE: title max 60 char, text max 250 char

ESEMPI di lesson buone:
  • "Rotazione defense in conflitto militare" / "Quando VIX > 25 e c'è
    escalation geopolitica, allocare 15-25% in LMT/RTX entro 1 settimana"
  • "Stop-loss su tech in rate shock" / "Se 10y yield sale > 50bp in 1 mese,
    chiudere o ridurre del 50% le posizioni growth (NVDA, ARKK)"

OUTPUT: SOLO JSON, nessun testo extra:
{
  "lessons": [
    {"title": "...", "text": "...", "type": "rotazione|timing|size|stop_loss|diversificazione|regime|altro"}
  ]
}"""

    # Costruzione contesto: storia decisioni + esito finale
    decisions = []
    for h in history:
        trades = h.get("ai_trades") or []
        if trades:
            ts = "; ".join(
                f"{t.get('action', '?')} {t.get('asset', '?')} {t.get('allocation_pct', 0):.0f}%"
                for t in trades
            )
        else:
            ts = "no action"
        decisions.append(f"T{h.get('step_index', 0) + 1}: {ts}")

    pnl_pct = final_valuation.get("total_pnl_pct", 0)
    bench_str = f"{benchmark_pct:.2f}%" if benchmark_pct is not None else "n/d"
    user_msg = (
        f"Scenario: {scenario.get('title', '?')}\n"
        f"Categoria: {scenario.get('category', '?')}\n"
        f"Decisioni ({len(history)} turni): {' | '.join(decisions)}\n"
        f"P&L finale portafoglio: {pnl_pct:+.2f}%\n"
        f"Benchmark SPY: {bench_str}\n"
        f"Cosa è successo davvero: {reveal[:600]}\n\n"
        f"Genera 2-4 lessons in JSON come da schema."
    )

    try:
        raw = await _call_r1(lessons_prompt, user_msg, max_retries=2)
    except Exception as e:
        logger.warning("[SIM-V2] _generate_lessons_learned: AI call fallita: %s", e)
        return []

    # Strip <think> e parse JSON (best-effort: cerca il primo blob {})
    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

    payload = None
    if "```" in text:
        try:
            chunk = text.split("```")[1].replace("json", "", 1).strip()
            payload = json.loads(chunk)
        except Exception:
            pass
    if payload is None:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                payload = json.loads(m.group(0))
            except Exception:
                pass

    if not isinstance(payload, dict):
        logger.warning("[SIM-V2] _generate_lessons_learned: JSON non parsabile")
        return []

    raw_lessons = payload.get("lessons") or []
    if not isinstance(raw_lessons, list):
        return []

    valid_types = {"rotazione", "timing", "size", "stop_loss",
                   "diversificazione", "regime", "altro"}
    out: list[dict] = []
    for ls in raw_lessons[:4]:   # max 4
        if not isinstance(ls, dict):
            continue
        title = str(ls.get("title") or "").strip()[:60]
        body = str(ls.get("text") or "").strip()[:250]
        if not title or not body:
            continue
        ltype = str(ls.get("type") or "altro").strip().lower()
        if ltype not in valid_types:
            ltype = "altro"
        out.append({"title": title, "text": body, "type": ltype})
    return out


def _auto_save_thesis_advice(scenario: dict, final_result: dict,
                              run_mode: str = "manual") -> None:
    """
    Salva automaticamente al termine del run:
      1. Il DEBRIEF come 1 advice (narrativa di 4-6 frasi)
      2. Le LESSONS LEARNED come 2-4 advice separati (insights azionabili)

    Differenza vs versione precedente: anche se persist fallisce, si tenta
    comunque il save (con UUID generato). Così la memoria si arricchisce
    a OGNI run, indipendentemente dalla persistenza nel DB.

    Tag in scenario_tags:
      - auto: True (per filtro Auto vs Manual nella UI)
      - run_mode: "manual" | "auto" (per analytics)
      - lesson_type: "rotazione|timing|size|..." (solo nelle lessons,
                     non nel debrief)
      - source: "debrief" | "lesson"

    Schedulato come sync wrapper attorno a chiamata async (lessons gen
    è async). Se chiamato da contesto sync, le lessons vengono skippate
    silenziosamente — il debrief comunque viene salvato.
    """
    import uuid as _uuid

    debrief = (final_result.get("debrief") or "").strip()
    persisted_run_id = final_result.get("persisted_run_id") or f"unpersisted-{_uuid.uuid4()}"

    try:
        from agents import sim_advisor
    except Exception as e:
        logger.debug("[SIM-V2] sim_advisor import fallito: %s", e)
        return

    # ── Detect scenario key + tags base ──────────────────────────────────
    fake_run = {
        "category": scenario.get("category", "unknown"),
        "scenario_id": scenario.get("id"),
        "outcome": final_result.get("outcome", "yellow"),
        "perf_1m": (final_result.get("final_valuation") or {}).get("total_pnl_pct", 0) / 100.0,
        "asset_chosen": None,
        "full_data": {
            "scenario": {
                "asset_universe": scenario.get("asset_universe"),
                "market_data": scenario.get("market_data"),
            },
            "steps": [{
                "context": {
                    "market_data": scenario.get("market_data") or [],
                    "asset_universe": scenario.get("asset_universe") or [],
                },
            }],
        },
    }
    try:
        category_key, base_tags = sim_advisor.detect_scenario_key(fake_run)
    except Exception as e:
        logger.debug("[SIM-V2] detect_scenario_key fallita: %s", e)
        category_key = scenario.get("category", "unknown")
        base_tags = {"category": category_key}

    # BUG FIX: "auto" era hardcoded True anche sui run MANUALI, rendendo
    # inutile il filtro Auto/Manual della UI. Deve riflettere run_mode.
    base_tags = {**(base_tags or {}), "auto": run_mode == "auto",
                 "run_mode": run_mode}

    pnl_pct = (final_result.get("final_valuation") or {}).get("total_pnl_pct", 0)
    bench_pct = final_result.get("benchmark_spy_pnl_pct")
    bench_str = f"{bench_pct:+.2f}%" if isinstance(bench_pct, (int, float)) else "n/d"
    rationale_base = (
        f"P&L finale: {pnl_pct:+.2f}% · Benchmark: {bench_str} · "
        f"Outcome: {final_result.get('outcome', 'yellow')} · "
        f"Steps: {scenario.get('num_steps', '?')} · "
        f"Run mode: {run_mode}"
    )[:600]

    saved_count = 0

    # ── 1. SAVE DEBRIEF (se non vuoto) ──────────────────────────────────
    if debrief and len(debrief) >= 30:
        debrief_title = (
            f"[Auto] {scenario.get('title', 'Scenario')[:60]} → "
            f"{final_result.get('outcome', 'yellow').upper()} "
            f"(P&L {pnl_pct:+.1f}%)"
        )[:200]
        debrief_advice = {
            "run_id": persisted_run_id,
            "scenario_category": category_key,
            "scenario_tags": {**base_tags, "source": "debrief"},
            "title": debrief_title,
            "text": debrief[:1000],
            "rationale": rationale_base,
        }
        try:
            # ARCHIVIO, non bucket advice: il debrief è un log di run, non
            # una lezione. Salvato nei bucket occupava gli slot delle 5
            # lezioni iniettate nei prompt (93/400 record erano debrief).
            aid = sim_advisor.save_run_log(debrief_advice)
            logger.info("[SIM-V2] auto-saved DEBRIEF (archivio): id=%s category=%s",
                        aid, category_key)
            saved_count += 1
        except Exception as e:
            logger.warning("[SIM-V2] save_run_log debrief fallito: %s", e)

    # ── 2. SAVE LESSONS (lista pre-generata in final_result.lessons) ────
    # Le lessons sono generate in finalize_run via _generate_lessons_learned
    # e iniettate qui in final_result["lessons_learned"]. Se assenti, skip.
    lessons = final_result.get("lessons_learned") or []
    for idx, lesson in enumerate(lessons[:4], start=1):
        if not isinstance(lesson, dict):
            continue
        l_title = str(lesson.get("title") or "").strip()[:200]
        l_text = str(lesson.get("text") or "").strip()[:1000]
        l_type = str(lesson.get("type") or "altro").strip().lower()
        if not l_title or not l_text:
            continue
        lesson_advice = {
            "run_id": f"{persisted_run_id}-lesson-{idx}",
            "scenario_category": category_key,
            "scenario_tags": {**base_tags, "source": "lesson", "lesson_type": l_type},
            "title": f"[{l_type.upper()}] {l_title}"[:200],
            "text": l_text,
            "rationale": rationale_base,
        }
        try:
            aid = sim_advisor.save_advice(lesson_advice)
            logger.info("[SIM-V2] auto-saved LESSON advice [%s]: id=%s title='%s'",
                        l_type, aid, l_title[:50])
            saved_count += 1
        except Exception as e:
            logger.warning("[SIM-V2] save_advice lesson #%d fallito: %s", idx, e)

    if saved_count == 0:
        logger.warning("[SIM-V2] auto-save: NESSUN advice salvato per run %s "
                       "(debrief='%s', lessons=%d)",
                       persisted_run_id, debrief[:40] if debrief else "(empty)",
                       len(lessons))
    else:
        logger.info("[SIM-V2] auto-save COMPLETATO: %d advice salvati per run %s "
                    "(category %s)", saved_count, persisted_run_id, category_key)


async def _generate_debrief(
    scenario: dict, history: list[dict], final_valuation: dict,
    benchmark_pct: Optional[float], reveal: str
) -> str:
    """Genera un debrief sintetico (1 chiamata R1 leggera)."""
    debrief_prompt = """Sei un coach di trading. Riassumi in 4-6 frasi (formato narrativo,
non bullet) la performance del portafoglio sulla simulazione appena conclusa.
Tono neutro-costruttivo: cosa ha funzionato, cosa no, perché.
Non superare 600 caratteri totali."""

    # Compatta la storia
    history_summary = []
    for h in history:
        trades = h.get("ai_trades", [])
        if trades:
            t_str = "; ".join(
                f"{t['action']} {t['asset']} {t['allocation_pct']:.0f}% "
                f"({t.get('conviction', '?')})"
                for t in trades
            )
        else:
            t_str = "no action"
        history_summary.append(f"T{h.get('step_index', 0) + 1}: {t_str}")

    pnl = final_valuation.get("total_pnl_pct", 0)
    bench_str = f"{benchmark_pct:.2f}%" if benchmark_pct is not None else "n/d"
    user_msg = (
        f"Scenario: {scenario.get('title')}\n"
        f"Storia delle decisioni: {' | '.join(history_summary)}\n"
        f"P&L finale portafoglio: {pnl:+.2f}%\n"
        f"P&L benchmark SPY: {bench_str}\n"
        f"Cosa è successo davvero: {reveal[:600]}\n\n"
        f"Riassumi la partita in 4-6 frasi."
    )

    try:
        raw = await _call_r1(debrief_prompt, user_msg, max_retries=2)
        # Strippo eventuali tag <think>
        text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        return text[:1200]
    except Exception as e:
        logger.warning("[SIM-V2] debrief generation failed: %s", e)
        return (f"Partita conclusa. P&L finale: {pnl:+.2f}% "
                f"(benchmark SPY: {bench_str}). Debrief automatico non disponibile.")


async def advisor_chat(
    scenario: dict, history: list[dict], final_result: dict,
    user_message: str, chat_history: list[dict] | None = None
) -> str:
    """
    Chat advisor end-of-game: l'utente fa domande sulla partita appena
    conclusa, l'AI risponde guardando l'intero storico e l'outcome.
    Stateless: il client manda chat_history a ogni messaggio.
    """
    advisor_prompt = """Sei un coach di trading esperto. L'utente ha appena concluso
una simulazione e vuole capire le decisioni prese. Hai accesso a TUTTO lo
storico della partita: scenario, decisioni dell'AI a ogni turno, P&L finale,
benchmark SPY, e la spiegazione di cosa è successo davvero.

Stile delle risposte:
- Diretto e concreto, no preamboli
- Cita numeri specifici (P&L, %, prezzi) quando rilevante
- Se l'utente chiede "perché X" o "cosa si poteva fare meglio", sii onesto
  ma costruttivo
- Tono didattico, non giudicante
- Massimo 200 parole per risposta"""

    # Costruisci context (one-shot iniziale, poi solo chat_history nei seguenti)
    chat_history = chat_history or []
    is_first_turn = len(chat_history) == 0

    messages = [{"role": "system", "content": advisor_prompt}]

    if is_first_turn:
        # Primo turno: inserisco l'intero context come user message
        ctx_parts = []
        ctx_parts.append(f"SCENARIO: {scenario.get('title', '?')}")
        ctx_parts.append(f"  Categoria: {scenario.get('category', '?')}")
        ctx_parts.append(f"  Periodo: {scenario.get('period_start', '?')} → "
                         f"{scenario.get('period_end', '?')}")
        ctx_parts.append("")

        ctx_parts.append("DECISIONI A OGNI TURNO:")
        for h in history:
            t_idx = h.get("step_index", 0) + 1
            t_date = h.get("step_date", "?")
            trades = h.get("ai_trades", [])
            applied = h.get("applied_trades", [])
            if trades:
                t_strs = []
                for tr, ap in zip(trades, applied[:len(trades)]):
                    qty = ap.get("executed_qty", 0)
                    px = ap.get("executed_price", 0)
                    val = ap.get("executed_value", 0)
                    t_strs.append(
                        f"{tr['action']} {tr['asset']} {qty:.2f}@${px:.2f}=${val:.0f} "
                        f"(\"{tr.get('thesis','')[:80]}\")"
                    )
                ctx_parts.append(f"  T{t_idx} ({t_date}): " + " | ".join(t_strs))
            else:
                ctx_parts.append(f"  T{t_idx} ({t_date}): nessuna azione")
            reasoning = (h.get("ai_reasoning") or "")[:200]
            if reasoning:
                ctx_parts.append(f"    Ragionamento: {reasoning}")
        ctx_parts.append("")

        v = final_result.get("final_valuation", {})
        ctx_parts.append("ESITO FINALE:")
        ctx_parts.append(f"  Capitale iniziale: ${v.get('initial_capital', 0):,.2f}")
        ctx_parts.append(f"  Valore finale: ${v.get('total_value', 0):,.2f}")
        ctx_parts.append(f"  P&L: {v.get('total_pnl_pct', 0):+.2f}%")
        bench = final_result.get("benchmark_spy_pnl_pct")
        ctx_parts.append(f"  Benchmark SPY: {bench:+.2f}%" if bench is not None else "  Benchmark SPY: n/d")
        ctx_parts.append(f"  Outcome: {final_result.get('outcome', '?')}")
        ctx_parts.append("")
        ctx_parts.append(f"COSA È SUCCESSO DAVVERO: {final_result.get('description_reveal', '')}")
        ctx_parts.append("")
        ctx_parts.append(f"DOMANDA UTENTE: {user_message}")

        messages.append({"role": "user", "content": "\n".join(ctx_parts)})
    else:
        # Turni successivi: solo storia chat
        for m in chat_history:
            role = m.get("role", "user")
            if role in ("user", "assistant"):
                messages.append({"role": role, "content": m.get("content", "")})
        messages.append({"role": "user", "content": user_message})

    # Chiamata diretta DeepSeek-R1 (light: max 800 tokens)
    api_key = _get_deepseek_key()
    if not api_key:
        return "Advisor non disponibile (DEEPSEEK_API_KEY mancante)."

    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type": "application/json"}
    payload = {
        "model": DEEPSEEK_R1,
        "messages": messages,
        "max_tokens": 800,
    }

    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                DEEPSEEK_API_URL, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=90)
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    return f"Errore advisor: HTTP {resp.status}: {body[:150]}"
                data = await resp.json()
                raw = data["choices"][0]["message"]["content"] or ""
                # Strip <think>
                clean = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
                return clean[:1500]
    except Exception as e:
        logger.error("[SIM-V2] advisor_chat error: %s", e)
        return f"Errore advisor: {str(e)[:200]}"


_VALID_CONVICTIONS = {"ALTA", "MEDIA", "BASSA"}


def aggregate_run_conviction(history: list[dict]) -> str | None:
    """
    Conviction aggregata della run dai trade REALI dichiarati dall'agente
    (history[].ai_trades[].conviction).

    Regola: conviction del trade con allocation_pct massima nel PRIMO step
    che contiene trade (e' la decisione d'ingresso, quella che definisce la
    tesi della run). Se quel primo step non ha conviction valide, fallback:
    moda pesata per allocation_pct su tutta la run.

    Nessun trade in tutta la run -> None: l'assenza di trade non e' una
    conviction, e "MEDIA" come default nascondeva il dato (tutte le run
    risultavano MEDIA — vedi analisi 13/07).
    """
    def _conv(t: dict) -> str | None:
        c = str(t.get("conviction") or "").strip().upper()
        return c if c in _VALID_CONVICTIONS else None

    def _alloc(t: dict) -> float:
        try:
            return float(t.get("allocation_pct") or 0)
        except (TypeError, ValueError):
            return 0.0

    # 1) primo step con trade: vince la conviction del trade piu' grosso
    for step in history or []:
        trades = [t for t in (step.get("ai_trades") or []) if isinstance(t, dict)]
        if not trades:
            continue
        valid = [t for t in trades if _conv(t)]
        if valid:
            return _conv(max(valid, key=_alloc))
        break  # trade presenti ma senza conviction valida -> fallback globale

    # 2) fallback: moda pesata per allocazione su tutta la run
    weights: dict[str, float] = {}
    for step in history or []:
        for t in (step.get("ai_trades") or []):
            if not isinstance(t, dict):
                continue
            c = _conv(t)
            if c:
                # peso minimo 1: un trade senza allocazione conta comunque
                weights[c] = weights.get(c, 0.0) + max(_alloc(t), 1.0)
    if weights:
        return max(weights, key=lambda k: weights[k])
    return None


def _pnl_after_first_step(final_result: dict) -> float:
    """
    Frazione di P&L dopo il PRIMO step, dai portfolio_value_series.
    Usato per popolare perf_1w: prima era sempre assente (solo il V1 legacy
    lo scriveva) → la barra "Performance per orizzonte / 1 settimana" e la
    colonna 1S risultavano SEMPRE a 0. Ora mostra un datapoint reale
    "precoce" del run. Ritorna 0.0 se non calcolabile.
    """
    pvs = final_result.get("portfolio_value_series") or []
    if len(pvs) < 2:
        return 0.0
    try:
        initial = float(pvs[0].get("value") or 0)
        first = float(pvs[1].get("value") or 0)
        if initial:
            return round((first / initial) - 1.0, 6)
    except Exception:
        pass
    return 0.0


def _persist_run(scenario: dict, history: list[dict], final_result: dict,
                  run_mode: str = "manual") -> str:
    """
    Salva la simulazione completata su Supabase per memoria storica.
    Usa la tabella sim_runs esistente (compatibilità con la dashboard).

    run_mode: 'manual' (default, run avviato dall'utente) o 'auto' (run
    avviato dal job scheduler.simulator_auto_run). Usato da
    `sim_db.runs_today(mode='auto')` per rispettare il daily_cap.
    """
    from simulator import db as sim_db
    run_id = str(uuid4())

    # Estrae l'asset principale (più trade fatti) e la action net finale
    asset_counts: dict[str, int] = {}
    for h in history:
        for t in h.get("ai_trades", []):
            a = t.get("asset")
            if a:
                asset_counts[a] = asset_counts.get(a, 0) + 1
    main_asset = max(asset_counts.keys(), key=lambda k: asset_counts[k]) if asset_counts else None

    # Action chosen: se ho long alla fine = BUY, short = SELL, niente = HOLD
    action_chosen = "HOLD"
    if main_asset and final_result.get("final_valuation"):
        for p in final_result["final_valuation"].get("positions", []):
            if p["asset"] == main_asset:
                action_chosen = "BUY" if p.get("side") == "long" else "SELL"
                break

    pnl_pct = final_result["final_valuation"].get("total_pnl_pct", 0) / 100.0
    bench_pct = (final_result.get("benchmark_spy_pnl_pct") or 0) / 100.0

    _now_iso = datetime.now(timezone.utc).isoformat()
    run_data = {
        "id": run_id,
        # created_at ESPLICITO (non affidarsi al DEFAULT NOW() del DB): è il
        # campo su cui runs_today() filtra per il cap giornaliero. Un insert
        # senza created_at su un path che non applica il default renderebbe
        # la riga non contata → cap che non ferma.
        "created_at": _now_iso,
        "completed_at": _now_iso,
        # mode='auto' nei run avviati dallo scheduler (per il cap giornaliero);
        # 'simulator_v2' (default) per i run manuali. Tracciato da runs_today.
        "mode": "auto" if run_mode == "auto" else "simulator_v2",
        "category": scenario.get("category", "unknown"),
        "scenario_type": "multi" if scenario.get("num_steps", 1) > 1 else "single",
        "steps": scenario.get("num_steps", 1),
        "scenario_id": scenario.get("id", "unknown"),
        "historical_period": (
            f"{scenario.get('period_start', '?')} → {scenario.get('period_end', '?')}"
        ),
        "asset_chosen": main_asset,
        "action_chosen": action_chosen,
        # Conviction REALE aggregata dai trade dell'agente (era hardcoded
        # "MEDIA": il 100% delle run risultava MEDIA — analisi 13/07).
        "conviction": aggregate_run_conviction(history),
        "horizon": "1settimana",
        # perf_1w = P&L dopo il 1° step (datapoint precoce reale, non più 0);
        # perf_1m = P&L finale del run. perf_3m resta = perf_1m: un run V2
        # dura settimane, non 3 mesi → non esiste un orizzonte a 3 mesi
        # distinto (il valore è comunque il rendimento reale del run).
        "perf_1w": _pnl_after_first_step(final_result),
        "perf_1m": pnl_pct,
        "perf_3m": pnl_pct,
        "perf_sp_1m": bench_pct,
        "delta_sp": pnl_pct - bench_pct,
        "outcome": final_result.get("outcome", "yellow"),
        "original_thesis": (history[0].get("ai_reasoning", "") if history else "")[:1500],
        "what_happened": final_result.get("description_reveal", "")[:1500],
        "thesis_evaluation": final_result.get("debrief", "")[:1500],
        "full_data": {
            "engine": "simulator_v2",
            "scenario": scenario,
            "history": history,
            "outcome_legacy": final_result.get("outcome_legacy"),
            "outcome_v2_inputs": final_result.get("outcome_v2_inputs"),
            "final_valuation": final_result.get("final_valuation"),
            "benchmark_spy_pnl_pct": final_result.get("benchmark_spy_pnl_pct"),
            "benchmark_value_series": final_result.get("benchmark_value_series"),
            "portfolio_value_series": final_result.get("portfolio_value_series"),
            "final_prices": final_result.get("final_prices"),
            # Metriche quantitative — usate dalla UI Result e per il rerun
            # slippage sensitivity (re-applicare i trade con commission_bps
            # diverso).
            "quant_metrics": final_result.get("quant_metrics"),
            "total_commissions_paid": final_result.get("total_commissions_paid", 0),
            "commission_bps": final_result.get("commission_bps", 0),
        },
    }
    sim_db.insert_run(run_data)
    return run_id
