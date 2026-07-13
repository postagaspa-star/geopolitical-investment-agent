"""
Simulator V2 Crypto — Engine stateless dedicato al mondo crypto.

Differenze vs v2_engine.py (equity):
  - Universe ESCLUSIVAMENTE crypto (formato yfinance "BTC-USD")
  - Decision Crypto AI engine (DeepSeek-R1 reasoning, stesso modello del
    bot Live Decision Crypto)
  - Turni di 2 GIORNI invece di 7 (mercato crypto 24/7, volatilità più alta)
  - 5-7 turni configurabili
  - Prompt specifico crypto: leverage, liquidation, depeg, regulatory,
    on-chain narrative
  - Scenari da crypto_scenarios.py (eventi crypto-specifici: hack, ETF,
    halving, depeg, exchange collapse)

Riusa dal v2_engine:
  - apply_trades / compute_portfolio_value (stessa logica portafoglio)
  - fetch_full_price_series (Polygon con fallback random walk)
  - extract_prices_at_date
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

import aiohttp

from simulator.v2_engine import (
    apply_trades, compute_portfolio_value, fetch_full_price_series,
    extract_prices_at_date, make_initial_portfolio, _parse_response,
    _classify_outcome, classify_outcome_v2, DEEPSEEK_API_URL, DEEPSEEK_R1,
    _get_deepseek_key, _pnl_after_first_step, aggregate_run_conviction,
)

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════
# SYSTEM PROMPT — crypto-specific
# ═════════════════════════════════════════════════════════════════════════

SIM_CRYPTO_SYSTEM_PROMPT = """Sei un PORTFOLIO MANAGER CRYPTO AI in modalità SIMULATOR.

Stai gestendo un portafoglio crypto di fronte a uno scenario storico reale
(halving, ETF, hack, depeg, exchange collapse, ecc.). I mercati crypto
sono 24/7, ad alta volatilità, e guidati spesso da catalizzatori specifici
(narrativa, on-chain, regolamentari).

═══════════════════════════════════════════════════════════════════════
REGOLE OPERATIVE CRYPTO
═══════════════════════════════════════════════════════════════════════

1. ORIZZONTE FISSO 2 GIORNI PER TURNO
   Ogni decisione vale per 48 ore. Sui mercati crypto 2 giorni possono
   essere abbastanza per un major move — quindi pensa al breve.

2. MAX 30% PER SINGOLA POSIZIONE CRYPTO
   La volatilità crypto richiede sizing prudente. Non concentrare oltre
   il 30% del portafoglio su un singolo asset, anche se la conviction è
   massima. Distribuisci tra 2-4 posizioni.

3. SHORT DISPONIBILE
   Puoi shortare crypto se prevedi pump-and-dump terminale, sell-the-news
   o capitulation pattern. Su SHORT: stop tecnico OBBLIGATORIO mentale
   (gestito dal sistema).

4. FOCUS NARRATIVA
   Crypto si muove a narrative cycles: layer-1 wars, AI tokens, DePIN,
   memecoin season, ETF inflows, halving cycle. Nelle headline cerca
   il tema dominante e posizionati di conseguenza.

5. BTC-DOMINANCE COME RISK SIGNAL
   Quando BTC sale ma alts non seguono = rotation incomplete, attenzione
   al risk-off. Quando BTC crolla, alts crollano peggio (correlation = 1).

═══════════════════════════════════════════════════════════════════════
6. GESTIONE DEL RISCHIO (IL CUORE DEL MESTIERE — vale anche qui)
═══════════════════════════════════════════════════════════════════════
Le run crypto passate sono le PEGGIORI del sistema (bull: 0 vittorie su
12) proprio perché queste regole mancavano. Non sono divieti meccanici:
sono criteri osservabili da applicare col giudizio.

── A. CRASH/CAPITULATION: non prendere il coltello che cade ───────────
In un depeg/collapse/ban, il -20% può diventare -60%. Prima di comprare
un rimbalzo devi VEDERE almeno 2 segnali OSSERVABILI nei dati del turno
(non immaginarli): reversal dopo serie di turni molto negativi;
compressione dell'ampiezza dei movimenti (da -15%/48h a -2%/48h);
divergenza prezzo/momentum; esaurimento del volume di panico. Senza
segnali: cash, short della debolezza confermata, o niente. ATTENZIONE:
in crypto il "rimbalzo del +4% in 48h" dentro un crash sistemico è
RUMORE, non capitulation — è esattamente l'errore che ha bruciato le
run passate (comprare SOL/DOT al giorno 6 di un depeg). Quando i
segnali ARRIVANO, il rimbalzo post-capitulation è il miglior trade
che esista: riconosci il momento, non astenerti per sempre.

── B. FAI CORRERE I VINCITORI / TAGLIA I PERDENTI ─────────────────────
Nelle run passate: vincitori chiusi a +3% "per sicurezza" e perdenti
tenuti a -5% "aspettando l'inversione". È il contrario. Se la tesi che
ha aperto il trade REGGE → mantieni, anche se sei in profitto. Se la
tesi è INVALIDATA dai fatti del turno → esci SUBITO, anche se sei in
perdita. Chiudere un vincitore si giustifica solo con la tesi esaurita
o un segnale di inversione, non con l'ansia di incassare.

── C. REGOLA DI INGRESSO ASIMMETRICA (R/R >= 1.5) ─────────────────────
Prima di aprire, stima nel ragionamento: upside atteso se la tesi è
giusta vs downside plausibile se è sbagliata — su orizzonte 48h e
commisurato alla volatilità REALE dell'asset (una alt che oscilla
10%/48h non ha il downside di BTC). Apri SOLO se upside >= 1.5 ×
downside. Ratio sotto 1.5 = NON-trade, lascialo andare.

── D. PARTECIPAZIONE AL TREND (il fix del bull: 0% win) ───────────────
In un bull confermato (BTC e majors su da 2+ turni, nessun breakdown),
stare 50-85% in stablecoin/cash NON è prudenza: è perdere contro il
benchmark, che è esattamente come vieni valutato. Esposizione TARGET
in trend confermato: >= 60%. Cash oltre il 40% in un bull va motivato
esplicitamente a OGNI turno con un rischio osservabile, non col
comfort. La prudenza nei trend si fa con exit_plan seri e size
distribuite (regola 2), non stando fuori. NON vale nei crash (lì
comanda 6.A).

═══════════════════════════════════════════════════════════════════════
PROCEDURA OBBLIGATORIA (3 sezioni in ordine)
═══════════════════════════════════════════════════════════════════════

[1] LETTURA DEL CONTESTO
    Sintesi (3-5 righe):
    - Catalyst dominante nelle headline (regolamentare? on-chain? macro?)
    - BTC e ETH dove stanno andando, dominance trend
    - Stato del tuo portafoglio crypto (se non primo turno)

[2] RAGIONAMENTO STRATEGICO
    - Tesi: che succederà nelle PROSSIME 48 ORE?
    - Quali asset crypto risk-on/risk-off in questo regime?
    - Tesi precedente confermata/modificata/invalidata? Se cambi idea
      rispetto al turno scorso, DICHIARALO ("al T2 dicevo X, ora Y
      perché Z") — i ribaltoni silenziosi sono l'errore #1 delle run.
    - CHECK PIANI D'USCITA: per ogni posizione aperta rileggi l'exit_plan
      che avevi dichiarato (te lo ripresento accanto alla posizione):
      stop/target raggiunti? Agisci o deroga DICHIARANDOLO.
    - CHECK REGIME: crash → segnali di capitulation prima di comprare
      (6.A)? Bull confermato → esposizione >= 60% o cash motivato (6.D)?
    - Rischi: liquidation cascade, depeg, news inattesa

[3] DECISIONE
    Output JSON STRUTTURATO. Schema esatto:
    {
      "trades": [
        {
          "action": "BUY" | "SELL",
          "asset": "BTC-USD" | "ETH-USD" | "SOL-USD" | ...,
          "allocation_pct": <numero 1-30>,
          "conviction": "BASSA" | "MEDIA" | "ALTA",
          "thesis": "Una frase: tesi sui prossimi 2 giorni",
          "rr": "upside +X% vs downside -Y% in 48h → ratio Z (>= 1.5)",
          "exit_plan": "stop: <livello/evento che invalida la tesi> | target: <quando incassi o rivaluti>"
        }
      ],
      "hold_summary": "Frase su posizioni mantenute invariate"
    }

VINCOLI HARD:
- TUTTI gli asset DEVONO essere crypto (-USD suffix nel formato yfinance)
- allocation_pct max 30 per trade (gestione rischio)
- Massimo 4 trade per turno (focus su 2-4 high-conviction)
- Lista vuota [] = mantieni tutto invariato
- "rr" e "exit_plan" OBBLIGATORI su ogni trade: stima R/R esplicita
  (sezione 6.C) e piano d'uscita dichiarato PRIMA di entrare (ti verrà
  ripresentato a ogni turno accanto alla posizione)
"""


# ═════════════════════════════════════════════════════════════════════════
# UTILITY
# ═════════════════════════════════════════════════════════════════════════

def compute_crypto_step_dates(period_start: str, num_steps: int) -> list[str]:
    """Date dei turni: T0, T+2d, T+4d, ... (2 giorni per step)."""
    start = datetime.fromisoformat(period_start).date()
    return [(start + timedelta(days=2 * i)).isoformat() for i in range(num_steps + 1)]


# ═════════════════════════════════════════════════════════════════════════
# AI CALL — Decision Crypto via DeepSeek-R1
# ═════════════════════════════════════════════════════════════════════════

async def _call_crypto_r1(system_prompt: str, user_message: str,
                          max_retries: int = 3) -> str:
    """Chiama DeepSeek-R1 (stesso modello del Decision Crypto Live)."""
    api_key = _get_deepseek_key()
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY non configurata")

    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type": "application/json"}
    payload = {
        "model": DEEPSEEK_R1,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "max_tokens": 6000,   # era 4500: meno troncamenti (vedi v2_engine)
    }

    last_error = ""
    for attempt in range(max_retries):
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(
                    DEEPSEEK_API_URL, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=180)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        choice = (data.get("choices") or [{}])[0]
                        content = (choice.get("message") or {})\
                            .get("content") or ""
                        # Output troncato dal cap max_tokens → il parse
                        # perderebbe i trade in silenzio: retry con budget
                        # maggiore (stesso guardrail di v2_engine._call_r1).
                        if (choice.get("finish_reason") == "length"
                                and attempt < max_retries - 1):
                            payload["max_tokens"] = 6500
                            logger.warning("[SIM-CRYPTO] output troncato "
                                           "(finish_reason=length) — retry "
                                           "con max_tokens=6500")
                            continue
                        return content
                    body = await resp.text()
                    last_error = f"HTTP {resp.status}: {body[:200]}"
                    if resp.status == 429 or 500 <= resp.status < 600:
                        if attempt < max_retries - 1:
                            await asyncio.sleep(2 ** (attempt + 1))
                            continue
                    raise ValueError(f"Crypto R1 {last_error}")
        except asyncio.TimeoutError:
            last_error = "timeout"
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** (attempt + 1))
                continue
            raise ValueError(f"Crypto R1 timeout after {max_retries}")
        except aiohttp.ClientError as e:
            last_error = f"network: {e}"
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** (attempt + 1))
                continue
            raise ValueError(f"Crypto R1 network: {e}")

    raise ValueError(f"Crypto R1 failed after {max_retries}: {last_error}")


# ═════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ═════════════════════════════════════════════════════════════════════════

async def start_crypto_run(
    category: Optional[str], num_steps: int,
    scenario_id: Optional[str] = None,
    initial_capital: float = 100000.0,
    commission_bps: float | None = None,
    run_mode: str = "manual",
) -> dict:
    """
    Inizializza una nuova partita simulator V2 Crypto.

    commission_bps: 10 bps default. Crypto exchange tipici applicano fees
    piu' alte del retail equity (taker maker spread + spread bid/ask wider),
    ma manteniamo il default uniforme per coerenza vs equity. L'utente puo'
    sovrascrivere via slippage rerun.
    """
    from simulator import crypto_scenarios as _scen
    from simulator.metrics import normalize_commission_bps

    if scenario_id:
        scenario = _scen.get_crypto_scenario_by_id(scenario_id)
    else:
        scenario = _scen.get_random_crypto_scenario(category)
    if not scenario:
        raise ValueError(f"Nessuno scenario crypto disponibile (category={category})")
    # Anti-ripetizione: registra la giocata (contatore condiviso in
    # scenarios._PLAYS_KEY, usato da least_played_choice)
    try:
        from simulator.scenarios import bump_scenario_play
        bump_scenario_play(scenario.get("id"))
    except Exception:
        pass

    num_steps = max(5, min(7, int(num_steps)))   # clip 5-7
    step_dates = compute_crypto_step_dates(scenario["period_start"], num_steps)
    universe = scenario.get("asset_universe", [])

    hardcoded_t0 = {m["ticker"]: float(m["price_t0"])
                    for m in scenario.get("market_data", [])}

    # BTC-USD e' il benchmark crypto. Se gia' nell'universe (di solito si),
    # niente da aggiungere; altrimenti lo includiamo per il chart vs benchmark.
    fetch_pool = list(universe)
    if "BTC-USD" not in fetch_pool:
        fetch_pool.append("BTC-USD")

    price_series = await fetch_full_price_series(fetch_pool, step_dates, hardcoded_t0)
    t0_prices = extract_prices_at_date(price_series, step_dates[0])

    real_count = sum(1 for t in universe if price_series.get(t)
                     and len(price_series[t]) >= num_steps)
    logger.info("[SIM-CRYPTO] start scenario=%s steps=%d, %d/%d ticker complete",
                scenario["id"], num_steps, real_count, len(universe))

    bps = normalize_commission_bps(commission_bps)
    portfolio = make_initial_portfolio(initial_capital, commission_bps=bps)

    # ── Tracking ID per la live progress dashboard ────────────────────────
    tracking_id = str(uuid4())
    try:
        from simulator import active_runs as _ar
        _ar.register(
            run_id=tracking_id, engine="v2_crypto", mode=run_mode,
            category=scenario.get("category", "?"),
            scenario_id=scenario.get("id", "?"),
            scenario_title=scenario.get("title", ""),
            total_steps=num_steps,
        )
    except Exception as e:
        logger.debug("[SIM-CRYPTO] active_runs register fallita: %s", e)

    # ── Advice memory crypto: stessa logica di v2_engine.start_run ───────
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
            logger.info("[SIM-CRYPTO] iniettati %d advice per categoria %s",
                        len(ids), cat_key)
    except Exception as e:
        logger.debug("[SIM-CRYPTO] advice injection skipped: %s", e)

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
            "step_dates": step_dates,
            "headlines_master": scenario.get("headlines", []),
            "price_series": price_series,
            "engine_mode": "crypto",          # marker per UI/debrief
            "step_unit": "2 giorni",
            "benchmark_ticker": "BTC-USD",    # benchmark crypto invece di SPY
            "commission_bps": bps,
            "advice_block": advice_block_text,
            "advice_meta": advice_meta,
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
    if not headlines_master or num_steps <= 0:
        return headlines_master or []
    chunk = max(1, len(headlines_master) // num_steps)
    if step_index == 0:
        return headlines_master[: max(chunk, len(headlines_master) // 2)]
    start = chunk * step_index
    end = chunk * (step_index + 1)
    return headlines_master[start:end] or headlines_master[-chunk:]


def _build_crypto_step_message(
    scenario: dict, portfolio: dict, valuation: dict,
    prices: dict, prev_prices: dict, t0_prices: dict,
    headlines: list[str], history: list[dict],
    step_index: int, num_steps: int, target_date: str
) -> str:
    """Messaggio user con focus crypto: BTC dominance, narrative, on-chain."""
    parts = []
    parts.append("═" * 60)
    if step_index == 0:
        parts.append(f"TURNO 1 di {num_steps} — INIZIO PARTITA CRYPTO")
        parts.append(f"Data simulata: {target_date}")
        parts.append(f"Capitale iniziale: ${valuation['initial_capital']:,.2f}")
        parts.append("Portafoglio: VUOTO. Tutto cash. Universe esclusivamente crypto.")
    else:
        parts.append(f"TURNO {step_index + 1} di {num_steps} — Crypto run")
        parts.append(f"Data simulata: {target_date}")
        parts.append("Sono passati 2 giorni dal turno precedente.")
    parts.append("═" * 60)
    parts.append("")

    # Portafoglio
    parts.append("📊 PORTAFOGLIO CRYPTO ATTUALE:")
    parts.append(f"  Cash: ${valuation['cash']:,.2f}")
    parts.append(f"  Valore totale: ${valuation['total_value']:,.2f}")
    if step_index > 0 and valuation.get("total_pnl_pct") is not None:
        sign = "+" if valuation["total_pnl"] >= 0 else ""
        parts.append(f"  P&L: {sign}${valuation['total_pnl']:,.2f} "
                     f"({sign}{valuation['total_pnl_pct']:.2f}%)")
    if valuation.get("positions"):
        parts.append("  Posizioni crypto aperte:")
        for p in valuation["positions"]:
            side = "LONG" if p.get("side") == "long" else "SHORT"
            sign = "+" if p["unrealized_pnl"] >= 0 else ""
            parts.append(
                f"    [{side}] {p['asset']}: {p['quantity']:.4f} @ avg "
                f"${p['avg_entry_price']:.4f} (ora ${p['current_price']:.4f}) "
                f"valore ${p.get('market_value', 0):,.2f} | "
                f"P&L: {sign}${p['unrealized_pnl']:,.2f} "
                f"({sign}{p['unrealized_pnl_pct']:.2f}%)"
            )
            # Ripresenta il piano d'uscita dichiarato all'ingresso (check
            # esplicito nella procedura [2]): niente più stop "mentali"
            # dichiarati e mai più guardati.
            if p.get("exit_plan"):
                parts.append(f"      ⤷ IL TUO PIANO D'USCITA: {p['exit_plan']}")
    else:
        parts.append("  Posizioni: nessuna (100% cash)")
    parts.append("")

    # BTC dominance signal: BTC vs ETH vs alts performance dal T0
    btc_t0 = t0_prices.get("BTC-USD")
    btc_cur = prices.get("BTC-USD")
    btc_chg_total = ((btc_cur - btc_t0) / btc_t0 * 100) if (btc_t0 and btc_cur) else None
    if btc_chg_total is not None and step_index > 0:
        parts.append(f"📡 SIGNAL BTC: dal T0 BTC ha fatto {btc_chg_total:+.2f}%")
        # ETH vs BTC ratio
        eth_t0 = t0_prices.get("ETH-USD")
        eth_cur = prices.get("ETH-USD")
        if eth_t0 and eth_cur:
            eth_chg = (eth_cur - eth_t0) / eth_t0 * 100
            ratio_signal = "ETH OUTPERFORM" if eth_chg > btc_chg_total + 1 else \
                           "ETH UNDERPERFORM" if eth_chg < btc_chg_total - 1 else "ETH ALLINEATO"
            parts.append(f"   ETH/BTC ratio: {ratio_signal} (ETH {eth_chg:+.2f}%)")
        parts.append("")

    # Asset universe con delta
    parts.append(f"🪙 UNIVERSE CRYPTO ({target_date}):")
    parts.append(f"  {'TICKER':<12} {'PREZZO':>14}  {'2 GIORNI':>10}  {'DAL T0':>10}")
    for ticker in scenario.get("asset_universe", []):
        cur = prices.get(ticker)
        if not cur:
            parts.append(f"  {ticker:<12} {'(no data)':>14}")
            continue
        prev = prev_prices.get(ticker)
        t0 = t0_prices.get(ticker)
        chg_2d = ((cur - prev) / prev * 100) if prev else None
        chg_t0 = ((cur - t0) / t0 * 100) if (t0 and step_index > 0) else None
        chg_2d_str = f"{chg_2d:+.2f}%" if chg_2d is not None else "—"
        chg_t0_str = f"{chg_t0:+.2f}%" if chg_t0 is not None else "—"
        parts.append(f"  {ticker:<12} ${cur:>12.4f}  {chg_2d_str:>10}  {chg_t0_str:>10}")
    parts.append("")

    # Headlines
    if headlines:
        parts.append(f"📰 HEADLINE CRYPTO ({target_date}):")
        for h in headlines:
            parts.append(f"  • {h}")
        parts.append("")

    # Storico
    if step_index > 0 and history:
        parts.append("📜 LE TUE DECISIONI PRECEDENTI:")
        for h in history[-3:]:
            t_idx = h.get("step_index", 0)
            t_date = h.get("step_date", "?")
            trades = h.get("ai_trades", [])
            if trades:
                t_summary = ", ".join(
                    f"{t['action']} {t['asset']} {t['allocation_pct']:.0f}%"
                    for t in trades
                )
                parts.append(f"  Turno {t_idx + 1} ({t_date}): {t_summary}")
            else:
                parts.append(f"  Turno {t_idx + 1} ({t_date}): hold")

        last = history[-1]
        last_val = (last.get("valuation_after") or {}).get("total_value")
        cur_val = valuation.get("total_value")
        if last_val and cur_val:
            wk_chg = ((cur_val - last_val) / last_val * 100) if last_val else 0
            sign = "+" if wk_chg >= 0 else ""
            parts.append(f"  → Performance ultimi 2 giorni: {sign}{wk_chg:.2f}%")
        parts.append("")

    parts.append("═" * 60)
    parts.append("DECIDI ORA: cosa fai per le PROSSIME 48 ORE?")
    parts.append("Massimo 30% per posizione, focus su 2-4 trade ad alta conviction.")
    parts.append("Procedura: [1] Lettura, [2] Ragionamento, [3] Decisione JSON.")

    return "\n".join(parts)


def _parse_crypto_response(raw: str) -> dict:
    """Parse + validazione: ogni asset deve essere crypto, allocation max 30%."""
    parsed = _parse_response(raw)
    valid_trades = []
    for t in parsed.get("trades", []):
        asset = (t.get("asset") or "").upper()
        # Hard validation: deve essere crypto
        if not (asset.endswith("-USD") and len(asset) > 4):
            logger.warning("[SIM-CRYPTO] trade scartato (non crypto): %s", t)
            continue
        # Cap allocation_pct a 30
        alloc = min(float(t.get("allocation_pct", 0) or 0), 30.0)
        if alloc <= 0:
            continue
        valid_trades.append({**t, "allocation_pct": alloc})
    parsed["trades"] = valid_trades[:4]   # cap a 4 trade
    return parsed


async def execute_crypto_step(
    scenario: dict, portfolio: dict, history: list[dict], step_index: int
) -> dict:
    """Esegue uno step della partita crypto."""
    step_dates = scenario.get("step_dates", [])
    num_steps = scenario.get("num_steps", len(step_dates) - 1)
    if step_index >= num_steps:
        raise ValueError(f"step_index {step_index} >= num_steps {num_steps}")

    # Tracking: registry update prima della chiamata R1
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

    prices = extract_prices_at_date(price_series, target_date)
    if not prices:
        prices = {m["ticker"]: float(m["price_t0"])
                  for m in scenario.get("market_data", [])}

    t0_prices = extract_prices_at_date(price_series, step_dates[0])
    prev_prices = (extract_prices_at_date(price_series, step_dates[step_index - 1])
                   if step_index > 0 else {})

    valuation_before = compute_portfolio_value(portfolio, prices)
    headlines = _split_headlines_for_step(
        scenario.get("headlines_master", []), step_index, num_steps
    )

    user_msg = _build_crypto_step_message(
        scenario, portfolio, valuation_before, prices, prev_prices, t0_prices,
        headlines, history, step_index, num_steps, target_date
    )

    # Inietta direttive utente + advice memory in cima al system prompt
    try:
        from agents.decision import _build_directives_block
        sys_prompt = _build_directives_block() + SIM_CRYPTO_SYSTEM_PROMPT
    except Exception:
        sys_prompt = SIM_CRYPTO_SYSTEM_PROMPT
    advice_block = (scenario.get("advice_block") or "").strip()
    if advice_block:
        sys_prompt = advice_block + "\n\n" + ("═" * 60) + "\n" + sys_prompt
    raw = await _call_crypto_r1(sys_prompt, user_msg)
    parsed = _parse_crypto_response(raw)
    if parsed.get("parse_failed"):
        # Stesso guardrail dell'engine equity: JSON trade corrotto/troncato
        # → un retry.
        logger.warning("[SIM-CRYPTO] decisione non parsabile, retry singolo")
        raw = await _call_crypto_r1(sys_prompt, user_msg)
        parsed = _parse_crypto_response(raw)
        if parsed.get("parse_failed"):
            # NON abortire l'intera run per uno step non parsabile (vedi
            # v2_engine): degrado a NO-TRADE visibile e proseguo.
            logger.error("[SIM-CRYPTO] step %d: output non parsabile dopo "
                         "retry → degrado a NO-TRADE (run NON abortita)",
                         step_index)
            parsed = {
                "reading": parsed.get("reading") or "(output non parsabile)",
                "reasoning": parsed.get("reasoning") or "",
                "trades": [],
                "hold_summary": "⚠ PARSE FAILED: output AI troncato/corrotto, "
                                "step saltato senza trade (run non abortita).",
                "raw": parsed.get("raw", ""),
                "parse_failed": True,
            }

    if tracking_id:
        try:
            from simulator import active_runs as _ar
            _ar.update_status(tracking_id, "applying_trades")
        except Exception:
            pass
    apply_result = apply_trades(portfolio, parsed["trades"], prices)
    new_portfolio = apply_result["portfolio"]
    applied_trades = apply_result["applied_trades"]

    valuation_after = compute_portfolio_value(new_portfolio, prices)

    # Price changes per UI
    price_changes = {}
    for t in universe:
        cur = prices.get(t)
        if not cur:
            continue
        t0 = t0_prices.get(t)
        prev = prev_prices.get(t)
        price_changes[t] = {
            "current": round(cur, 4),
            "t0": round(t0, 4) if t0 else None,
            "prev": round(prev, 4) if prev else None,
            "chg_2d_pct": round((cur - prev) / prev * 100, 2) if prev else None,
            "chg_total_pct": round((cur - t0) / t0 * 100, 2) if t0 else None,
        }

    return {
        "step_index": step_index,
        "step_date": target_date,
        "is_last_step": (step_index + 1 >= num_steps),
        "prices": prices,
        "price_changes": price_changes,
        "headlines": headlines,
        "ai_reading": parsed["reading"],
        "ai_reasoning": parsed["reasoning"],
        "ai_trades": parsed["trades"],
        "ai_hold_summary": parsed["hold_summary"],
        "applied_trades": applied_trades,
        "valuation_before": valuation_before,
        "valuation_after": valuation_after,
        "new_portfolio": new_portfolio,
        "raw_response": parsed["raw"][:3000],
    }


async def finalize_crypto_run(
    scenario: dict, portfolio: dict, history: list[dict],
    persist: bool = True, run_mode: str = "manual",
) -> dict:
    """Chiusura partita crypto: P&L finale, benchmark BTC buy&hold, debrief."""
    step_dates = scenario.get("step_dates", [])
    if not step_dates:
        raise ValueError("scenario senza step_dates")
    final_date = step_dates[-1]
    price_series = scenario.get("price_series", {})

    final_prices = extract_prices_at_date(price_series, final_date)
    if not final_prices and history:
        final_prices = history[-1].get("prices", {})
    if not final_prices:
        final_prices = {m["ticker"]: float(m["price_t0"])
                        for m in scenario.get("market_data", [])}

    final_valuation = compute_portfolio_value(portfolio, final_prices)

    # Benchmark BTC buy & hold (vs SPY in equity sim)
    t0_date = step_dates[0]
    btc_t0 = price_series.get("BTC-USD", {}).get(t0_date)
    btc_final = price_series.get("BTC-USD", {}).get(final_date)
    benchmark_pnl_pct = None
    if btc_t0 and btc_final:
        benchmark_pnl_pct = round((btc_final - btc_t0) / btc_t0 * 100, 2)

    # Description reveal
    from simulator import crypto_scenarios as _scen
    full_scenario = _scen.get_crypto_scenario_by_id(scenario.get("id", ""))
    reveal = full_scenario.get("description_reveal", "") if full_scenario else ""

    # Series per chart. T0 = capitale iniziale (prima dei trade).
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
    portfolio_value_series.append({
        "step_index": len(history),
        "step_date": final_date,
        "value": final_valuation.get("total_value"),
    })

    # ── Benchmark equity curve BTC buy-and-hold ────────────────────────────
    # Riusa la helper di v2_engine: stessa logica della SPY series.
    from simulator.v2_engine import _build_benchmark_value_series
    benchmark_value_series = _build_benchmark_value_series(
        price_series, "BTC-USD", step_dates, initial, history, final_date
    )

    asset_breakdown = []
    for p in final_valuation.get("positions", []):
        asset_breakdown.append({
            "asset": p["asset"], "side": p["side"],
            "quantity": p["quantity"],
            "avg_entry_price": p["avg_entry_price"],
            "final_price": p["current_price"],
            "market_value": p["market_value"],
            "unrealized_pnl": p["unrealized_pnl"],
            "unrealized_pnl_pct": p["unrealized_pnl_pct"],
        })

    # ── METRICHE QUANTITATIVE crypto ───────────────────────────────────────
    # Step unit = 2 giorni → annualization ~182.5 periodi/anno.
    from simulator import metrics as _metrics
    quant_metrics = _metrics.compute_all_metrics(
        equity_curve=portfolio_value_series,
        history=history,
        final_valuation=final_valuation,
        step_unit_days=2.0,
        periods_per_year=_metrics.ANNUALIZATION_CRYPTO,
    )

    # Debrief AI (narrativa) + Lessons learned crypto-tailored, in parallelo.
    # _generate_lessons_learned è in v2_engine ed è generico (funziona anche
    # per crypto perché il prompt accetta qualsiasi tipo di scenario).
    from simulator.v2_engine import _generate_lessons_learned as _gen_lessons
    debrief, lessons_learned = await asyncio.gather(
        _generate_crypto_debrief(scenario, history, final_valuation,
                                  benchmark_pnl_pct, reveal),
        _gen_lessons(scenario, history, final_valuation,
                     benchmark_pnl_pct, reveal),
        return_exceptions=False,
    )

    # Outcome v2 (confronto a pari esposizione); il legacy resta salvato
    # per confronto/rollback nella riclassificazione retroattiva.
    _ov2 = classify_outcome_v2(final_valuation, history,
                               benchmark_value_series, benchmark_pnl_pct)

    result = {
        "scenario_id": scenario.get("id"),
        "engine_mode": "crypto",
        "final_date": final_date,
        "final_prices": final_prices,
        "final_valuation": final_valuation,
        "benchmark_btc_pnl_pct": benchmark_pnl_pct,    # NB: BTC, non SPY
        "benchmark_spy_pnl_pct": benchmark_pnl_pct,    # alias per UI generico
        "benchmark_value_series": benchmark_value_series,
        "outcome": _ov2["outcome"],
        "outcome_legacy": _classify_outcome(final_valuation, benchmark_pnl_pct),
        "outcome_v2_inputs": _ov2,
        "debrief": debrief,
        # Lessons learned: lista [{title, text, type}, ...] generata in parallel
        # al debrief; iniettata in _auto_save_thesis_advice per save automatico.
        "lessons_learned": lessons_learned or [],
        "description_reveal": reveal,
        "num_steps": scenario.get("num_steps"),
        "portfolio_value_series": portfolio_value_series,
        "asset_breakdown": asset_breakdown,
        "price_series": price_series,
        "step_dates": step_dates,
        # Metriche e fees
        "quant_metrics": quant_metrics,
        "total_commissions_paid": float(
            final_valuation.get("total_commissions_paid", 0) or 0
        ),
        "commission_bps": float(final_valuation.get("commission_bps", 0) or 0),
    }

    if persist:
        tracking_id = scenario.get("tracking_id")
        if tracking_id:
            try:
                from simulator import active_runs as _ar
                _ar.update_status(tracking_id, "finalizing")
            except Exception:
                pass
        try:
            persisted_id = _persist_crypto_run(scenario, history, result,
                                                 run_mode=run_mode)
            result["persisted_run_id"] = persisted_id
        except Exception as e:
            logger.error("[SIM-CRYPTO] persist failed: %s", e, exc_info=True)
            result["persist_error"] = str(e)[:200]
        # Auto-save Valutazione tesi crypto come advice (riusa l'helper di
        # v2_engine: la logica e' identica, distingue il flag is_crypto via
        # detect_scenario_key e tag).
        try:
            from simulator.v2_engine import _auto_save_thesis_advice
            _auto_save_thesis_advice(scenario, result, run_mode=run_mode)
        except Exception as _e:
            logger.warning("[SIM-CRYPTO] auto-save advice fallito (non critico): %s", _e)
        # Tracking: mark completed (anche se persist fail)
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


async def _generate_crypto_debrief(
    scenario: dict, history: list[dict], final_valuation: dict,
    benchmark_pct: Optional[float], reveal: str
) -> str:
    """Debrief crypto-tailored."""
    debrief_prompt = """Sei un coach di trading crypto. Riassumi in 4-6 frasi
(formato narrativo, non bullet) la performance della simulazione crypto appena
conclusa. Tono neutro-costruttivo: cosa ha funzionato, cosa no, dove la tesi
crypto si è confermata o smentita. Cita eventuali errori comuni: over-allocation,
late entry, missed rotation BTC→alt o viceversa. Massimo 600 caratteri."""

    history_summary = []
    for h in history:
        trades = h.get("ai_trades", [])
        if trades:
            t_str = "; ".join(
                f"{t['action']} {t['asset']} {t['allocation_pct']:.0f}%"
                for t in trades
            )
        else:
            t_str = "no action"
        history_summary.append(f"T{h.get('step_index', 0) + 1}: {t_str}")

    pnl = final_valuation.get("total_pnl_pct", 0)
    bench_str = f"{benchmark_pct:.2f}%" if benchmark_pct is not None else "n/d"
    user_msg = (
        f"Scenario crypto: {scenario.get('title')}\n"
        f"Decisioni: {' | '.join(history_summary)}\n"
        f"P&L portafoglio: {pnl:+.2f}%\n"
        f"Benchmark BTC buy&hold: {bench_str}\n"
        f"Cosa è successo davvero: {reveal[:600]}\n\n"
        f"Riassumi la partita in 4-6 frasi crypto-savvy."
    )

    try:
        raw = await _call_crypto_r1(debrief_prompt, user_msg, max_retries=2)
        text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        return text[:1200]
    except Exception as e:
        logger.warning("[SIM-CRYPTO] debrief fallito: %s", e)
        return (f"Partita crypto chiusa. P&L: {pnl:+.2f}% (benchmark BTC: "
                f"{bench_str}). Debrief automatico non disponibile.")


async def crypto_advisor_chat(
    scenario: dict, history: list[dict], final_result: dict,
    user_message: str, chat_history: list[dict] | None = None
) -> str:
    """Advisor chat post-game crypto: stessa logica del v2_engine ma context crypto."""
    advisor_prompt = """Sei un coach di trading crypto. L'utente ha appena
concluso una simulazione crypto e vuole capire le decisioni prese sui mercati
24/7 ad alta volatilità. Hai accesso a tutto lo storico: scenario,
catalyst, decisioni AI per turno (2 giorni ciascuno), P&L finale,
benchmark BTC buy&hold, e cosa è successo davvero nel periodo.

Stile risposte:
- Diretto, concreto, no preamboli
- Cita numeri specifici (P&L %, prezzi crypto, allocation)
- Su crypto sii specifico: BTC dominance, narrative, on-chain signals
- Tono didattico, non giudicante
- Massimo 200 parole"""

    chat_history = chat_history or []
    is_first = len(chat_history) == 0
    messages = [{"role": "system", "content": advisor_prompt}]

    if is_first:
        ctx = []
        ctx.append(f"SCENARIO CRYPTO: {scenario.get('title', '?')}")
        ctx.append(f"  Categoria: {scenario.get('category', '?')}")
        ctx.append(f"  Periodo: {scenario.get('period_start', '?')} → "
                   f"{scenario.get('period_end', '?')}")
        ctx.append("")
        ctx.append("DECISIONI PER TURNO (2 giorni ciascuno):")
        for h in history:
            t_idx = h.get("step_index", 0) + 1
            t_date = h.get("step_date", "?")
            trades = h.get("ai_trades", [])
            applied = h.get("applied_trades", [])
            if trades:
                ts = []
                for tr, ap in zip(trades, applied[:len(trades)]):
                    qty = ap.get("executed_qty", 0)
                    px = ap.get("executed_price", 0)
                    val = ap.get("executed_value", 0)
                    ts.append(f"{tr['action']} {tr['asset']} {qty:.4f}@${px:.4f}=${val:.0f} "
                              f"(\"{tr.get('thesis','')[:80]}\")")
                ctx.append(f"  T{t_idx} ({t_date}): " + " | ".join(ts))
            else:
                ctx.append(f"  T{t_idx} ({t_date}): hold")
            r = (h.get("ai_reasoning") or "")[:200]
            if r:
                ctx.append(f"    Ragionamento: {r}")
        ctx.append("")
        v = final_result.get("final_valuation", {})
        ctx.append("ESITO:")
        ctx.append(f"  Capitale iniziale: ${v.get('initial_capital', 0):,.2f}")
        ctx.append(f"  Valore finale: ${v.get('total_value', 0):,.2f}")
        ctx.append(f"  P&L: {v.get('total_pnl_pct', 0):+.2f}%")
        bench = final_result.get("benchmark_btc_pnl_pct")
        ctx.append(f"  Benchmark BTC: {bench:+.2f}%" if bench is not None else "  BTC: n/d")
        ctx.append(f"  Outcome: {final_result.get('outcome', '?')}")
        ctx.append("")
        ctx.append(f"COSA È SUCCESSO: {final_result.get('description_reveal', '')}")
        ctx.append("")
        ctx.append(f"DOMANDA: {user_message}")
        messages.append({"role": "user", "content": "\n".join(ctx)})
    else:
        for m in chat_history:
            role = m.get("role", "user")
            if role in ("user", "assistant"):
                messages.append({"role": role, "content": m.get("content", "")})
        messages.append({"role": "user", "content": user_message})

    api_key = _get_deepseek_key()
    if not api_key:
        return "Advisor non disponibile (DEEPSEEK_API_KEY mancante)."

    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type": "application/json"}
    payload = {"model": DEEPSEEK_R1, "messages": messages, "max_tokens": 800}

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
                clean = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
                return clean[:1500]
    except Exception as e:
        logger.error("[SIM-CRYPTO] advisor error: %s", e)
        return f"Errore advisor: {str(e)[:200]}"


def _persist_crypto_run(scenario: dict, history: list[dict], final_result: dict,
                          run_mode: str = "manual") -> str:
    """
    Salva la partita crypto su sim_runs (categoria='crypto').
    run_mode: 'manual' o 'auto' (cf. v2_engine._persist_run).
    """
    from simulator import db as sim_db
    run_id = str(uuid4())

    asset_counts: dict[str, int] = {}
    for h in history:
        for t in h.get("ai_trades", []):
            a = t.get("asset")
            if a:
                asset_counts[a] = asset_counts.get(a, 0) + 1
    main_asset = max(asset_counts, key=lambda k: asset_counts[k]) if asset_counts else None

    action_chosen = "HOLD"
    if main_asset:
        for p in final_result["final_valuation"].get("positions", []):
            if p["asset"] == main_asset:
                action_chosen = "BUY" if p.get("side") == "long" else "SELL"
                break

    pnl_pct = final_result["final_valuation"].get("total_pnl_pct", 0) / 100.0
    bench_pct = (final_result.get("benchmark_btc_pnl_pct") or 0) / 100.0

    _now_iso = datetime.now(timezone.utc).isoformat()
    run_data = {
        "id": run_id,
        # created_at ESPLICITO: campo su cui runs_today() filtra per il cap
        # giornaliero (vedi nota in v2_engine._persist_run).
        "created_at": _now_iso,
        "completed_at": _now_iso,
        # mode='auto' nei run avviati dallo scheduler (cap giornaliero),
        # 'simulator_v2_crypto' nei run manuali.
        "mode": "auto" if run_mode == "auto" else "simulator_v2_crypto",
        "category": scenario.get("category", "crypto"),
        "scenario_type": "multi",
        "steps": scenario.get("num_steps", 1),
        "scenario_id": scenario.get("id", "unknown"),
        "historical_period": (
            f"{scenario.get('period_start', '?')} → {scenario.get('period_end', '?')}"
        ),
        "asset_chosen": main_asset, "action_chosen": action_chosen,
        # Conviction REALE aggregata dai trade dell'agente (era hardcoded
        # "MEDIA": il 100% delle run risultava MEDIA — analisi 13/07).
        "conviction": aggregate_run_conviction(history),
        "horizon": "2giorni",
        # perf_1w = P&L dopo il 1° step (prima sempre 0); vedi v2_engine.
        "perf_1w": _pnl_after_first_step(final_result),
        "perf_1m": pnl_pct, "perf_3m": pnl_pct,
        "perf_sp_1m": bench_pct,    # benchmark BTC stored qui per coerenza UI
        "delta_sp": pnl_pct - bench_pct,
        "outcome": final_result.get("outcome", "yellow"),
        "original_thesis": (history[0].get("ai_reasoning", "") if history else "")[:1500],
        "what_happened": final_result.get("description_reveal", "")[:1500],
        "thesis_evaluation": final_result.get("debrief", "")[:1500],
        "full_data": {
            "engine": "simulator_v2_crypto",
            "scenario": scenario, "history": history,
            "outcome_legacy": final_result.get("outcome_legacy"),
            "outcome_v2_inputs": final_result.get("outcome_v2_inputs"),
            "final_valuation": final_result.get("final_valuation"),
            "benchmark_btc_pnl_pct": final_result.get("benchmark_btc_pnl_pct"),
            "benchmark_value_series": final_result.get("benchmark_value_series"),
            "portfolio_value_series": final_result.get("portfolio_value_series"),
            "final_prices": final_result.get("final_prices"),
            # Metriche crypto: Sharpe annualizzato a 182.5 periodi/anno (2-day step)
            "quant_metrics": final_result.get("quant_metrics"),
            "total_commissions_paid": final_result.get("total_commissions_paid", 0),
            "commission_bps": final_result.get("commission_bps", 0),
        },
    }
    sim_db.insert_run(run_data)
    return run_id
