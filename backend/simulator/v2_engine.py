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

[3] DECISIONE
    Output JSON STRUTTURATO. Schema esatto (NIENTE testo extra dopo):
    {
      "trades": [
        {
          "action": "BUY" | "SELL",
          "asset": "TICKER",
          "allocation_pct": <numero 1-100>,
          "conviction": "BASSA" | "MEDIA" | "ALTA",
          "thesis": "Una frase: perché questo trade per la prossima settimana"
        }
      ],
      "hold_summary": "Frase breve sulle posizioni che mantieni invariate (se ce ne sono)"
    }

REGOLE PER trades:
- Lista vuota [] = mantieni il portafoglio come è (skip turno)
- BUY: apre una nuova posizione long o aggiunge a esistente
- SELL: chiude posizione long esistente OPPURE apre short
- allocation_pct si riferisce al capitale TOTALE attuale del portafoglio
- Asset deve essere uno dei ticker dell'asset_universe fornito
- Massimo 5 trade per turno (focus, non sparare a caso)

ESEMPIO DI BUONA DECISIONE:
{
  "trades": [
    {"action": "BUY", "asset": "XOM", "allocation_pct": 25,
     "conviction": "ALTA",
     "thesis": "Tensione Medio Oriente persiste, oil supply a rischio nel breve"},
    {"action": "SELL", "asset": "TLT", "allocation_pct": 15,
     "conviction": "MEDIA",
     "thesis": "Yields probabili al rialzo se inflazione importata da oil"}
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

def make_initial_portfolio(initial_capital: float) -> dict:
    """Portafoglio vuoto al T0."""
    return {
        "cash": float(initial_capital),
        "initial_capital": float(initial_capital),
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

        if side == "long":
            mkt_value = qty * cur_price
            pnl = (cur_price - entry) * qty
            positions_value += mkt_value
        else:  # short
            # Per uno short: valore = cash trattenuto al sell, PnL = (entry - current) * qty
            mkt_value = qty * cur_price  # passivo da ricomprare
            pnl = (entry - cur_price) * qty
            positions_value -= mkt_value  # passività

        enriched_positions.append({
            **pos,
            "current_price": cur_price,
            "market_value": round(mkt_value, 2),
            "unrealized_pnl": round(pnl, 2),
            "unrealized_pnl_pct": round((pnl / (qty * entry) * 100) if (qty and entry) else 0, 2),
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
    }


def apply_trades(portfolio: dict, trades: list[dict], prices: dict[str, float]) -> dict:
    """
    Applica una lista di trade al portfolio.
    Modifica il portfolio (mutativo) e ritorna il nuovo stato.

    Logica:
      - BUY su asset esistente long → aumenta quantity, ricalcola avg_entry
      - BUY su asset esistente short → chiude lo short (riacquista)
      - SELL su asset esistente long → riduce/chiude long
      - SELL su asset NON esistente → apre short
      - SELL su asset esistente short → aumenta short (avg_entry ricalcolato)
    """
    # Copia profonda per non mutare l'input
    new_portfolio = {
        "cash": float(portfolio.get("cash", 0)),
        "initial_capital": float(portfolio.get("initial_capital", 0)),
        "positions": [
            {**p} for p in (portfolio.get("positions") or [])
        ],
    }
    valuation = compute_portfolio_value(new_portfolio, prices)
    total_value_now = valuation["total_value"]

    applied_trades = []
    for trade in trades or []:
        action = (trade.get("action") or "").upper()
        asset = trade.get("asset", "").upper()
        alloc_pct = float(trade.get("allocation_pct", 0))
        if not asset or alloc_pct <= 0 or action not in ("BUY", "SELL"):
            continue
        price = prices.get(asset)
        if not price or price <= 0:
            applied_trades.append({**trade, "status": "skipped",
                                   "reason": f"no price for {asset}"})
            continue

        # Calcola dollar amount = % del valore totale corrente
        dollar_amount = total_value_now * (alloc_pct / 100.0)
        quantity = round(dollar_amount / price, 4)
        if quantity <= 0:
            applied_trades.append({**trade, "status": "skipped",
                                   "reason": "quantity rounds to 0"})
            continue

        # Trova posizione esistente per questo asset
        existing = next((p for p in new_portfolio["positions"]
                         if p["asset"] == asset), None)

        if action == "BUY":
            cost = quantity * price
            if cost > new_portfolio["cash"] + 0.01:
                # Cash insufficiente: scala al massimo possibile
                quantity = round(new_portfolio["cash"] / price, 4)
                cost = quantity * price
                if quantity <= 0:
                    applied_trades.append({**trade, "status": "skipped",
                                           "reason": "cash exhausted"})
                    continue
            new_portfolio["cash"] -= cost
            if existing and existing.get("side") == "long":
                # Aumenta long, ricalcola avg
                new_qty = existing["quantity"] + quantity
                new_avg = (existing["avg_entry_price"] * existing["quantity"]
                           + price * quantity) / new_qty
                existing["quantity"] = round(new_qty, 4)
                existing["avg_entry_price"] = round(new_avg, 4)
            elif existing and existing.get("side") == "short":
                # BUY su short = chiude lo short (riacquista)
                close_qty = min(quantity, existing["quantity"])
                # Realizza P&L
                pnl = (existing["avg_entry_price"] - price) * close_qty
                new_portfolio["cash"] += pnl  # P&L cash
                existing["quantity"] -= close_qty
                if existing["quantity"] <= 0.0001:
                    new_portfolio["positions"].remove(existing)
            else:
                new_portfolio["positions"].append({
                    "asset": asset, "quantity": quantity,
                    "avg_entry_price": price, "side": "long",
                    "thesis": trade.get("thesis", "")[:300],
                    "conviction": trade.get("conviction", "MEDIA"),
                })
            applied_trades.append({**trade, "status": "executed",
                                   "executed_qty": quantity,
                                   "executed_price": price,
                                   "executed_value": round(quantity * price, 2)})

        else:  # SELL
            if existing and existing.get("side") == "long":
                # Chiude/riduce long
                close_qty = min(quantity, existing["quantity"])
                proceeds = close_qty * price
                new_portfolio["cash"] += proceeds
                existing["quantity"] -= close_qty
                if existing["quantity"] <= 0.0001:
                    new_portfolio["positions"].remove(existing)
                applied_trades.append({**trade, "status": "executed_close_long",
                                       "executed_qty": close_qty,
                                       "executed_price": price,
                                       "proceeds": round(proceeds, 2)})
            elif existing and existing.get("side") == "short":
                # Aumenta short
                new_qty = existing["quantity"] + quantity
                new_avg = (existing["avg_entry_price"] * existing["quantity"]
                           + price * quantity) / new_qty
                existing["quantity"] = round(new_qty, 4)
                existing["avg_entry_price"] = round(new_avg, 4)
                applied_trades.append({**trade, "status": "executed_add_short",
                                       "executed_qty": quantity})
            else:
                # Apre short: incassa il valore (sarà rimborsato a chiusura)
                proceeds = quantity * price
                new_portfolio["cash"] += proceeds
                new_portfolio["positions"].append({
                    "asset": asset, "quantity": quantity,
                    "avg_entry_price": price, "side": "short",
                    "thesis": trade.get("thesis", "")[:300],
                    "conviction": trade.get("conviction", "MEDIA"),
                })
                applied_trades.append({**trade, "status": "executed_open_short",
                                       "executed_qty": quantity,
                                       "executed_price": price,
                                       "proceeds": round(proceeds, 2)})

    return {"portfolio": new_portfolio, "applied_trades": applied_trades}


# ═════════════════════════════════════════════════════════════════════════
# AI CALL
# ═════════════════════════════════════════════════════════════════════════

async def _call_r1(system_prompt: str, user_message: str,
                   max_retries: int = 3) -> str:
    """Chiama DeepSeek-R1 con retry exponential backoff."""
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
        "max_tokens": 4500,
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
                        return data["choices"][0]["message"]["content"] or ""
                    body = await resp.text()
                    last_error = f"HTTP {resp.status}: {body[:200]}"
                    if resp.status == 429 or 500 <= resp.status < 600:
                        if attempt < max_retries - 1:
                            await asyncio.sleep(2 ** (attempt + 1))
                            continue
                    raise ValueError(f"DeepSeek-R1 {last_error}")
        except asyncio.TimeoutError:
            last_error = "timeout"
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** (attempt + 1))
                continue
            raise ValueError(f"DeepSeek-R1 timeout after {max_retries}")
        except aiohttp.ClientError as e:
            last_error = f"network: {e}"
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** (attempt + 1))
                continue
            raise ValueError(f"DeepSeek-R1 network: {e}")

    raise ValueError(f"DeepSeek-R1 failed after {max_retries}: {last_error}")


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
        trades_norm.append({
            "action": (t.get("action") or "").upper(),
            "asset": (t.get("asset") or "").upper(),
            "allocation_pct": float(t.get("allocation_pct", 0) or 0),
            "conviction": (t.get("conviction") or "MEDIA").upper(),
            "thesis": (t.get("thesis") or "")[:400],
        })

    hold_summary = ""
    if isinstance(decision_json, dict):
        hold_summary = (decision_json.get("hold_summary") or "")[:400]

    return {
        "reading": reading or txt[:600],
        "reasoning": reasoning or "",
        "trades": trades_norm,
        "hold_summary": hold_summary,
        "raw": txt,
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
    initial_capital: float = 100000.0
) -> dict:
    """
    Inizializza una nuova partita simulator V2.
    Ritorna il payload completo (scenario + step_dates + portfolio iniziale
    + price_series complete pre-fetched) da consegnare al client.

    Pre-fetch ALL prices at start: garantisce che tutti gli step abbiano
    prezzi disponibili e che il P&L sia computabile in modo deterministico
    senza dipendere da Polygon disponibilità a runtime.
    """
    from simulator import scenarios as _scen

    if scenario_id:
        scenario = _scen.get_scenario_by_id(scenario_id)
    else:
        scenario = _scen.get_random_scenario(category)
    if not scenario:
        raise ValueError(f"Nessuno scenario disponibile (category={category})")

    num_steps = max(3, min(5, int(num_steps)))   # clip 3-5
    step_dates = compute_step_dates(scenario["period_start"], num_steps)
    universe = scenario.get("asset_universe", [])

    # Hardcoded T0 prices come fallback per random walk
    hardcoded_t0 = {m["ticker"]: float(m["price_t0"])
                    for m in scenario.get("market_data", [])}

    # Pre-fetch SERIE COMPLETA di tutti i prezzi per tutti gli step
    price_series = await fetch_full_price_series(universe, step_dates, hardcoded_t0)
    t0_prices = extract_prices_at_date(price_series, step_dates[0])

    # Diagnostica: quanti ticker hanno dati reali vs fallback
    real_count = sum(1 for t in universe if price_series.get(t)
                     and len(price_series[t]) >= num_steps)
    logger.info("[SIM-V2] start_run scenario=%s steps=%d, %d/%d ticker con prezzi completi",
                scenario["id"], num_steps, real_count, len(universe))

    portfolio = make_initial_portfolio(initial_capital)

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
            # per gli step. Tutti i prezzi sono già qui dentro.
            "price_series": price_series,
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

    # 5. Call AI — inietta direttive utente in cima al system prompt
    try:
        from agents.decision import _build_directives_block
        sys_prompt = _build_directives_block() + SIM_V2_SYSTEM_PROMPT
    except Exception:
        sys_prompt = SIM_V2_SYSTEM_PROMPT
    raw = await _call_r1(sys_prompt, user_msg)
    parsed = _parse_response(raw)

    # 6. Apply trades
    apply_result = apply_trades(portfolio, parsed["trades"], prices)
    new_portfolio = apply_result["portfolio"]
    applied_trades = apply_result["applied_trades"]

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
    persist: bool = True
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

    # Costruisci serie portfolio_value per chart
    portfolio_value_series = []
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

    # Debrief AI
    debrief = await _generate_debrief(scenario, history, final_valuation,
                                       benchmark_pnl_pct, reveal)

    result = {
        "scenario_id": scenario.get("id"),
        "final_date": final_date,
        "final_prices": final_prices,
        "final_valuation": final_valuation,
        "benchmark_spy_pnl_pct": benchmark_pnl_pct,
        "outcome": _classify_outcome(final_valuation, benchmark_pnl_pct),
        "debrief": debrief,
        "description_reveal": reveal,
        "num_steps": scenario.get("num_steps"),
        "portfolio_value_series": portfolio_value_series,   # per chart
        "asset_breakdown": asset_breakdown,                 # per breakdown UI
        "price_series": price_series,                       # per chart prezzi
        "step_dates": step_dates,
    }

    if persist:
        try:
            persisted_id = _persist_run(scenario, history, result)
            result["persisted_run_id"] = persisted_id
        except Exception as e:
            logger.error("[SIM-V2] persist failed: %s", e, exc_info=True)
            result["persist_error"] = str(e)[:200]

    return result


def _classify_outcome(valuation: dict, benchmark_pct: Optional[float]) -> str:
    """Verde/giallo/rosso in base a P&L vs benchmark."""
    pnl = valuation.get("total_pnl_pct", 0)
    if benchmark_pct is not None:
        delta = pnl - benchmark_pct
        if delta > 1.0 and pnl > 0:
            return "green"
        if delta < -2.0 or pnl < -5:
            return "red"
        return "yellow"
    # No benchmark: solo absolute
    if pnl > 3:
        return "green"
    if pnl < -3:
        return "red"
    return "yellow"


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


def _persist_run(scenario: dict, history: list[dict], final_result: dict) -> str:
    """
    Salva la simulazione completata su Supabase per memoria storica.
    Usa la tabella sim_runs esistente (compatibilità con la dashboard).
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

    run_data = {
        "id": run_id,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "mode": "simulator_v2",
        "category": scenario.get("category", "unknown"),
        "scenario_type": "multi" if scenario.get("num_steps", 1) > 1 else "single",
        "steps": scenario.get("num_steps", 1),
        "scenario_id": scenario.get("id", "unknown"),
        "historical_period": (
            f"{scenario.get('period_start', '?')} → {scenario.get('period_end', '?')}"
        ),
        "asset_chosen": main_asset,
        "action_chosen": action_chosen,
        "conviction": "MEDIA",   # aggregata, no singolo trade
        "horizon": "1settimana",
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
            "final_valuation": final_result.get("final_valuation"),
            "benchmark_spy_pnl_pct": final_result.get("benchmark_spy_pnl_pct"),
            "final_prices": final_result.get("final_prices"),
        },
    }
    sim_db.insert_run(run_data)
    return run_id
