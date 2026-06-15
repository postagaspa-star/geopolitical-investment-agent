"""
Simulator metrics — KPI quantitativi e dataset per i grafici statistici.

Contiene tutte le funzioni "pure" (no I/O, no DB) che trasformano la storia
di una partita V2 in:

  • KPI scalari:    sharpe, max_drawdown, profit_factor, expectancy, avg_hold_time
  • Serie per UI:   underwater curve, histogram returns, conf-vs-outcome scatter

Conventions:
  • equity_curve = list[{step_index, step_date, value}]  (V2 portfolio_value_series)
  • per_step_returns = ritorni step-on-step, derivati dall'equity_curve
  • per_trade_pnls = list[float] in dollari (positivo = winner, negativo = loser)
  • commission_bps = costo di transazione in basis points (10 bps = 0.10%)

Tutto deterministico: stessi input → stessi output. Nessuna chiamata di rete.
"""
from __future__ import annotations

import math
from typing import Optional


# Numero di "periodi" all'anno per annualizzare lo Sharpe.
# Equity sim: 1 step = 1 settimana → 52 periodi/anno.
# Crypto sim: 1 step = 2 giorni  → ~182 periodi/anno.
ANNUALIZATION_EQUITY = 52.0
ANNUALIZATION_CRYPTO = 182.5


# ─── Helpers basici ────────────────────────────────────────────────────────

def _safe_div(num: float, den: float, default: float = 0.0) -> float:
    if not den or den == 0:
        return default
    return num / den


def equity_curve_to_returns(equity_curve: list[dict]) -> list[float]:
    """
    Estrae ritorni step-on-step da una equity_curve (list di {value: ...}).
    Ritorna una list di float (lunghezza N-1 se equity_curve ha N punti).
    """
    out: list[float] = []
    prev = None
    for pt in equity_curve or []:
        v = pt.get("value") if isinstance(pt, dict) else pt
        if v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if prev is not None and prev > 0:
            out.append((v - prev) / prev)
        prev = v
    return out


# ─── KPI scalari ───────────────────────────────────────────────────────────

def compute_sharpe(returns: list[float], periods_per_year: float = ANNUALIZATION_EQUITY,
                    risk_free_per_period: float = 0.0) -> Optional[float]:
    """
    Sharpe Ratio annualizzato.
      Sharpe = sqrt(periods_per_year) * (mean(excess_returns) / std(excess_returns))

    Ritorna None se non ci sono abbastanza dati o se std=0 (no variabilita').

    Soglie di lettura per il Simulator:
      > 2.0  → eccellente
      1.0–2 → buono
      0–1   → mediocre
      < 0   → peggio del cash
    """
    if not returns or len(returns) < 2:
        return None
    excess = [r - risk_free_per_period for r in returns]
    mean = sum(excess) / len(excess)
    var = sum((r - mean) ** 2 for r in excess) / len(excess)
    std = math.sqrt(var)
    if std <= 1e-9:
        return None
    sharpe_per_period = mean / std
    return round(sharpe_per_period * math.sqrt(periods_per_year), 3)


def compute_max_drawdown(equity_curve: list[dict]) -> dict:
    """
    Max Drawdown peak-to-trough running.

    Ritorna {
      "max_drawdown_pct": -X.XX  (sempre <= 0; -100 = wipeout),
      "peak_value": float,
      "trough_value": float,
      "peak_index": int,
      "trough_index": int,
      "duration_steps": int        # quanti step tra peak e trough
    }
    """
    if not equity_curve:
        return {"max_drawdown_pct": 0, "peak_value": 0, "trough_value": 0,
                "peak_index": 0, "trough_index": 0, "duration_steps": 0}

    values = []
    for pt in equity_curve:
        v = pt.get("value") if isinstance(pt, dict) else pt
        if v is None:
            continue
        try:
            values.append(float(v))
        except (TypeError, ValueError):
            continue
    if not values:
        return {"max_drawdown_pct": 0, "peak_value": 0, "trough_value": 0,
                "peak_index": 0, "trough_index": 0, "duration_steps": 0}

    peak = values[0]
    peak_idx = 0
    max_dd = 0.0
    trough = values[0]
    trough_idx = 0
    cur_peak = values[0]
    cur_peak_idx = 0
    for i, v in enumerate(values):
        if v > cur_peak:
            cur_peak = v
            cur_peak_idx = i
        dd = (v - cur_peak) / cur_peak if cur_peak > 0 else 0
        if dd < max_dd:
            max_dd = dd
            peak = cur_peak
            peak_idx = cur_peak_idx
            trough = v
            trough_idx = i
    return {
        "max_drawdown_pct": round(max_dd * 100, 2),
        "peak_value": round(peak, 2),
        "trough_value": round(trough, 2),
        "peak_index": peak_idx,
        "trough_index": trough_idx,
        "duration_steps": max(0, trough_idx - peak_idx),
    }


def compute_profit_factor(per_trade_pnls: list[float]) -> Optional[float]:
    """
    Profit Factor = sum(positive_pnls) / |sum(negative_pnls)|.

    Ritorna:
      > 2.0  → eccellente
      1–2   → discreto
      < 1   → sistema in perdita
      None  → no trade
      math.inf → solo winner (no perdite)
    """
    if not per_trade_pnls:
        return None
    winners = sum(p for p in per_trade_pnls if p > 0)
    losers_abs = abs(sum(p for p in per_trade_pnls if p < 0))
    if losers_abs == 0:
        return float("inf") if winners > 0 else None
    return round(winners / losers_abs, 3)


def compute_expectancy(per_trade_pnls: list[float]) -> dict:
    """
    Expectancy: P&L medio per trade in $.

    Ritorna {
      "expectancy_dollars": float,    # media dei pnl
      "win_rate": 0..1,
      "avg_winner": float,            # solo positivi
      "avg_loser":  float,            # solo negativi (negativo)
      "n_winners": int,
      "n_losers": int,
      "n_total": int
    }

    L'expectancy "Tharp-style":
      E = win_rate * avg_winner - (1-win_rate) * |avg_loser|
    e' equivalente a mean(pnls) — manteniamo il calcolo diretto.
    """
    if not per_trade_pnls:
        return {"expectancy_dollars": 0, "win_rate": 0, "avg_winner": 0,
                "avg_loser": 0, "n_winners": 0, "n_losers": 0, "n_total": 0}
    winners = [p for p in per_trade_pnls if p > 0]
    losers = [p for p in per_trade_pnls if p < 0]
    n = len(per_trade_pnls)
    expectancy = sum(per_trade_pnls) / n
    return {
        "expectancy_dollars": round(expectancy, 2),
        "win_rate": round(len(winners) / n, 3),
        "avg_winner": round(sum(winners) / len(winners), 2) if winners else 0,
        "avg_loser": round(sum(losers) / len(losers), 2) if losers else 0,
        "n_winners": len(winners),
        "n_losers": len(losers),
        "n_total": n,
    }


def compute_avg_hold_time(history: list[dict], step_unit_days: float = 7.0) -> dict:
    """
    Tempo medio di detenzione delle posizioni.

    Per ogni asset, traccia (open_step, close_step) inferito dalle azioni:
      • BUY su asset non posseduto → apre posizione (open_step)
      • SELL che porta quantita' a 0 → chiude posizione (close_step)
      • Posizioni ancora aperte alla fine → close_step = ultimo step

    step_unit_days: 7 per equity (1 settimana), 2 per crypto.

    Ritorna {
      "avg_hold_steps": float,         # numero medio di step tenuti
      "avg_hold_days": float,
      "n_open_close_pairs": int,
      "still_open_at_end": int
    }
    """
    if not history:
        return {"avg_hold_steps": 0, "avg_hold_days": 0,
                "n_open_close_pairs": 0, "still_open_at_end": 0}

    # Per ogni asset, lista di eventi (step_idx, action)
    events: dict[str, list[tuple[int, str]]] = {}
    for h in history:
        step_idx = h.get("step_index", 0)
        for tr in h.get("applied_trades", []) or []:
            asset = tr.get("asset")
            status = tr.get("status", "")
            if not asset or "skipped" in status:
                continue
            # Mappa lo status al tipo di evento
            if status.startswith("executed_open_long") or status.startswith("executed_open_short"):
                events.setdefault(asset, []).append((step_idx, "open"))
            elif status.startswith("executed_close_long") or status.startswith("executed_close_short"):
                events.setdefault(asset, []).append((step_idx, "close"))
            elif status.startswith("executed_flip_"):
                # Flip = chiudi e riapri lato opposto (1 close + 1 open allo stesso step)
                events.setdefault(asset, []).append((step_idx, "close"))
                events.setdefault(asset, []).append((step_idx, "open"))
            elif status.startswith("executed_add_"):
                # Add = posizione esistente, no nuovo open. Skip.
                pass

    last_step = max((h.get("step_index", 0) for h in history), default=0)

    holds: list[int] = []
    still_open = 0
    for asset, evlist in events.items():
        evlist.sort(key=lambda e: (e[0], 0 if e[1] == "open" else 1))
        open_step: Optional[int] = None
        for step, action in evlist:
            if action == "open":
                if open_step is None:
                    open_step = step
            elif action == "close" and open_step is not None:
                holds.append(max(0, step - open_step))
                open_step = None
        if open_step is not None:
            # Posizione ancora aperta a fine partita
            holds.append(max(0, last_step - open_step))
            still_open += 1

    if not holds:
        return {"avg_hold_steps": 0, "avg_hold_days": 0,
                "n_open_close_pairs": 0, "still_open_at_end": still_open}
    avg_steps = sum(holds) / len(holds)
    return {
        "avg_hold_steps": round(avg_steps, 2),
        "avg_hold_days": round(avg_steps * step_unit_days, 1),
        "n_open_close_pairs": len(holds) - still_open,
        "still_open_at_end": still_open,
    }


# ─── Dataset per i grafici ─────────────────────────────────────────────────

def build_underwater_curve(equity_curve: list[dict]) -> list[dict]:
    """
    Underwater chart: per ogni punto, drawdown % rispetto al peak running.

    Ritorna list[{step_index, step_date, drawdown_pct}].
    drawdown_pct e' sempre <= 0 (zero quando si e' al picco).
    """
    out = []
    cur_peak = 0.0
    for pt in equity_curve or []:
        v = pt.get("value") if isinstance(pt, dict) else pt
        if v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if v > cur_peak:
            cur_peak = v
        dd = ((v - cur_peak) / cur_peak * 100) if cur_peak > 0 else 0
        out.append({
            "step_index": pt.get("step_index") if isinstance(pt, dict) else None,
            "step_date": pt.get("step_date") if isinstance(pt, dict) else None,
            "drawdown_pct": round(dd, 2),
        })
    return out


def build_returns_histogram(per_step_returns: list[float], n_bins: int = 10) -> dict:
    """
    Histogram dei ritorni step-on-step.

    Ritorna {
      "bins": list[{lo: pct, hi: pct, count: int}],
      "min_return_pct": float,
      "max_return_pct": float,
      "n_samples": int,
    }
    """
    if not per_step_returns:
        return {"bins": [], "min_return_pct": 0, "max_return_pct": 0, "n_samples": 0}

    pcts = [r * 100 for r in per_step_returns]
    lo = min(pcts)
    hi = max(pcts)
    if hi - lo < 1e-6:
        # Tutti i ritorni uguali: una sola bin
        return {
            "bins": [{"lo": round(lo, 2), "hi": round(hi, 2), "count": len(pcts)}],
            "min_return_pct": round(lo, 2),
            "max_return_pct": round(hi, 2),
            "n_samples": len(pcts),
        }

    n_bins = max(3, min(20, n_bins))
    width = (hi - lo) / n_bins
    bins = [{"lo": lo + i * width, "hi": lo + (i + 1) * width, "count": 0}
            for i in range(n_bins)]
    for p in pcts:
        idx = min(n_bins - 1, int((p - lo) / width))
        bins[idx]["count"] += 1
    # Round per UI
    for b in bins:
        b["lo"] = round(b["lo"], 2)
        b["hi"] = round(b["hi"], 2)
    return {
        "bins": bins,
        "min_return_pct": round(lo, 2),
        "max_return_pct": round(hi, 2),
        "n_samples": len(pcts),
    }


# Mapping conviction → score 0..1 (per scatter plot e analisi overconfidence)
_CONV_SCORE = {"BASSA": 0.50, "MEDIA": 0.75, "ALTA": 1.00}


def conviction_score(conviction: str) -> float:
    """Mappa la conviction stringa al punteggio numerico per scatter plot."""
    return _CONV_SCORE.get((conviction or "").upper(), 0.65)


def build_confidence_outcome_scatter(history: list[dict]) -> list[dict]:
    """
    Per ogni trade eseguito, calcola (conviction_score, outcome_pct).
    L'outcome_pct e' il P&L realizzato sul trade close-quantity, oppure
    l'unrealized P&L corrente per posizioni ancora aperte.

    Ritorna list[{
      asset, action, conviction, conviction_score,
      outcome_pct, outcome_dollars,
      step_index, status
    }].

    Usato dalla UI per uno scatter plot:
      X = conviction_score, Y = outcome_pct
      Pattern overconfident: punti ad ALTA conviction in basso (negativi).
    """
    points = []
    # Tracker per asset: (open_step, side, qty, avg_entry_price)
    positions: dict[str, dict] = {}

    for h in history:
        step_idx = h.get("step_index", 0)
        for tr in h.get("applied_trades", []) or []:
            asset = tr.get("asset")
            status = tr.get("status", "")
            if not asset or "skipped" in status or "executed" not in status:
                continue
            qty = float(tr.get("executed_qty", 0) or 0)
            price = float(tr.get("executed_price", 0) or 0)
            if qty <= 0 or price <= 0:
                continue
            conviction = tr.get("conviction", "MEDIA") or "MEDIA"

            if status.startswith("executed_open_long"):
                positions[asset] = {"side": "long", "qty": qty, "avg": price,
                                     "open_step": step_idx, "conviction": conviction}
            elif status.startswith("executed_open_short"):
                positions[asset] = {"side": "short", "qty": qty, "avg": price,
                                     "open_step": step_idx, "conviction": conviction}
            elif status.startswith("executed_add_"):
                # Aggiunta a posizione esistente: ricalcola avg
                pos = positions.get(asset)
                if pos:
                    new_qty = pos["qty"] + qty
                    pos["avg"] = (pos["avg"] * pos["qty"] + price * qty) / new_qty
                    pos["qty"] = new_qty
            elif status.startswith("executed_close_") or status.startswith("executed_flip_"):
                pos = positions.pop(asset, None)
                if pos:
                    close_qty = min(qty, pos["qty"])
                    if pos["side"] == "long":
                        pnl_dollars = (price - pos["avg"]) * close_qty
                    else:
                        pnl_dollars = (pos["avg"] - price) * close_qty
                    # #32: P&L NETTO delle commissioni (apertura + chiusura),
                    # coerente con l'equity netta da cui derivano Sharpe/maxDD.
                    # Prima era LORDO → profit_factor/expectancy ottimistici e
                    # incoerenti con gli altri KPI nello stesso dict.
                    pnl_dollars -= (
                        commission_amount(pos["avg"] * close_qty, DEFAULT_COMMISSION_BPS)
                        + commission_amount(price * close_qty, DEFAULT_COMMISSION_BPS)
                    )
                    pnl_pct = ((price - pos["avg"]) / pos["avg"]
                                if pos["side"] == "long"
                                else (pos["avg"] - price) / pos["avg"])
                    points.append({
                        "asset": asset,
                        "action": "BUY" if pos["side"] == "long" else "SELL",
                        "conviction": pos["conviction"],
                        "conviction_score": conviction_score(pos["conviction"]),
                        "outcome_pct": round(pnl_pct * 100, 2),
                        "outcome_dollars": round(pnl_dollars, 2),
                        "step_index": pos["open_step"],
                        "close_step": step_idx,
                        "status": "closed",
                    })
                    # #33: un FLIP CHIUDE e RIAPRE sul lato opposto. Prima la
                    # posizione flip-aperta veniva persa (solo pop) → il suo
                    # trade successivo era scartato / con conviction sbagliata.
                    # Re-inseriscila con la qty residua (oltre la chiusura).
                    if status.startswith("executed_flip_"):
                        residual = qty - close_qty
                        if residual > 1e-12:
                            positions[asset] = {
                                "side": "short" if pos["side"] == "long" else "long",
                                "qty": residual, "avg": price,
                                "open_step": step_idx, "conviction": conviction,
                            }

    # Posizioni ancora aperte a fine partita: usa final_valuation se disponibile
    # (verra' iniettato dal caller via posizioni rimanenti)
    return points


def add_open_positions_to_scatter(
    scatter: list[dict], final_valuation: dict, history: list[dict]
) -> list[dict]:
    """
    Estende lo scatter con le posizioni ancora aperte alla fine,
    valutate ai final_prices dell'ultimo step.

    Cerca la conviction registrata nell'apertura originale (history applied_trades).
    """
    out = list(scatter)
    if not final_valuation:
        return out

    # Mappa asset → conviction registrata all'apertura
    asset_conv: dict[str, tuple[str, int]] = {}
    for h in history or []:
        step_idx = h.get("step_index", 0)
        for tr in h.get("applied_trades", []) or []:
            asset = tr.get("asset")
            status = tr.get("status", "")
            # #33: anche i FLIP aprono una posizione (lato opposto): includili,
            # altrimenti una posizione flip-aperta ancora aperta a fine partita
            # non trova la sua conviction (resta sul default).
            if not asset or not (status.startswith("executed_open_")
                                 or status.startswith("executed_flip_")):
                continue
            asset_conv[asset] = (tr.get("conviction", "MEDIA") or "MEDIA", step_idx)

    for p in final_valuation.get("positions", []) or []:
        asset = p.get("asset")
        if not asset:
            continue
        conv, open_step = asset_conv.get(asset, ("MEDIA", 0))
        out.append({
            "asset": asset,
            "action": "BUY" if p.get("side") == "long" else "SELL",
            "conviction": conv,
            "conviction_score": conviction_score(conv),
            "outcome_pct": float(p.get("unrealized_pnl_pct", 0) or 0),
            "outcome_dollars": float(p.get("unrealized_pnl", 0) or 0),
            "step_index": open_step,
            "close_step": None,
            "status": "open",
        })
    return out


# ─── Per-trade P&L extraction (per profit_factor e expectancy) ────────────

def extract_per_trade_pnls(scatter_points: list[dict]) -> list[float]:
    """
    Dallo scatter (che ha gia' calcolato outcome_dollars per ogni trade),
    estrae la lista flat di P&L $ per il calcolo di profit_factor/expectancy.
    """
    return [p.get("outcome_dollars", 0.0) for p in scatter_points or []
            if p.get("outcome_dollars") is not None]


# ─── Aggregato top-level: un singolo dict con tutto ────────────────────────

def compute_all_metrics(
    equity_curve: list[dict],
    history: list[dict],
    final_valuation: dict,
    step_unit_days: float = 7.0,
    periods_per_year: float = ANNUALIZATION_EQUITY,
) -> dict:
    """
    Calcola TUTTI i KPI in una sola chiamata. Ritorno usato da finalize_run
    per essere salvato nel run e mostrato dal frontend.
    """
    returns = equity_curve_to_returns(equity_curve)
    sharpe = compute_sharpe(returns, periods_per_year=periods_per_year)
    mdd = compute_max_drawdown(equity_curve)
    underwater = build_underwater_curve(equity_curve)
    histogram = build_returns_histogram(returns, n_bins=10)

    scatter_closed = build_confidence_outcome_scatter(history)
    scatter_full = add_open_positions_to_scatter(scatter_closed, final_valuation, history)
    per_trade_pnls = extract_per_trade_pnls(scatter_full)

    profit_factor = compute_profit_factor(per_trade_pnls)
    expectancy = compute_expectancy(per_trade_pnls)
    hold_stats = compute_avg_hold_time(history, step_unit_days=step_unit_days)

    return {
        # KPI scalari
        "sharpe_ratio": sharpe,
        "max_drawdown": mdd,
        "profit_factor": profit_factor if (profit_factor is not None
                                           and not math.isinf(profit_factor)) else None,
        "profit_factor_infinite": (profit_factor is not None
                                   and math.isinf(profit_factor)),
        "expectancy": expectancy,
        "avg_hold_time": hold_stats,
        # Serie per grafici
        "underwater_curve": underwater,
        "returns_histogram": histogram,
        "confidence_outcome_scatter": scatter_full,
        # Metadata
        "n_returns": len(returns),
        "annualization_factor": periods_per_year,
        "step_unit_days": step_unit_days,
    }


# ═══════════════════════════════════════════════════════════════════════════
# COMMISSION / SLIPPAGE — usato dal Portfolio Engine in entrambi i lati
# ═══════════════════════════════════════════════════════════════════════════

DEFAULT_COMMISSION_BPS = 10.0   # 10 bps = 0.10% per trade
MIN_COMMISSION_BPS = 0.0        # 0 = nessuna commissione (test slippage 0)
MAX_COMMISSION_BPS = 100.0      # 100 bps = 1% (limite sanity, broker peggiori)


def normalize_commission_bps(commission_bps: float | None,
                              default: float = DEFAULT_COMMISSION_BPS) -> float:
    """Clamp + default safe per il commission rate."""
    if commission_bps is None:
        return default
    try:
        bps = float(commission_bps)
    except (TypeError, ValueError):
        return default
    if bps < MIN_COMMISSION_BPS:
        return MIN_COMMISSION_BPS
    if bps > MAX_COMMISSION_BPS:
        return MAX_COMMISSION_BPS
    return bps


def commission_amount(gross_value: float, commission_bps: float) -> float:
    """
    Calcola la commissione $ da un valore lordo del trade.
    1 bps = 0.01% = 0.0001.
    """
    return abs(float(gross_value)) * (float(commission_bps) / 10_000.0)
