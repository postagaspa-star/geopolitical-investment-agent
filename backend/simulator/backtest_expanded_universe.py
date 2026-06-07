"""
backtest_expanded_universe.py — IL GATE dello Step 4.

Backtest deterministico (no LLM) che confronta una semplice strategia di
ROTAZIONE MOMENTUM eseguita su:
  - CORE      = universo di rotazione attuale (~liquido, equity/ETF)
  - ESTESO    = CORE + cintura SATELLITE (settoriali/single-country/EM/tematici)

Applica un modello di slippage ONESTO (liquidity.slippage_bps, ∝ size/ADV) così
i nomi meno liquidi NON sembrano gratis. Riporta, per entrambi gli universi,
diversità di copertura (breadth, effective-N) e rischio/rendimento (return,
Sharpe, max drawdown). È il segnale per decidere se attivare EXPANDED_UNIVERSE.

NB: la funzione di simulazione `simulate()` è PURA (niente rete) ed è testata su
dati sintetici. `main()` invece scarica storia reale via data_fetchers e quindi
richiede rete — eseguilo dalla cartella backend:

    python -m simulator.backtest_expanded_universe 2022-01-01 2024-12-31 8

Argomenti: from_date to_date top_k  (default 2022-01-01 → 2024-12-31, top_k=8).
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

import universe
import liquidity

try:
    from simulator.metrics import compute_sharpe
except Exception:  # pragma: no cover - fallback se import package fallisce
    def compute_sharpe(returns, periods_per_year=252.0, risk_free=0.0):
        if not returns:
            return None
        mean = sum(returns) / len(returns)
        var = sum((r - mean) ** 2 for r in returns) / len(returns)
        sd = math.sqrt(var)
        if sd == 0:
            return None
        return (mean / sd) * math.sqrt(periods_per_year)


def _max_drawdown_pct(equity: Sequence[float]) -> float:
    """Max drawdown % dalla curva equity (peak-to-trough)."""
    peak = -1e18
    mdd = 0.0
    for v in equity:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak * 100.0
            if dd > mdd:
                mdd = dd
    return round(mdd, 2)


def _effective_n(weights: Sequence[float]) -> float:
    """Numero 'effettivo' di nomi = 1/HHI sui pesi di portafoglio."""
    s = sum(weights)
    if s <= 0:
        return 0.0
    hhi = sum((w / s) ** 2 for w in weights)
    return (1.0 / hhi) if hhi > 0 else 0.0


def simulate(
    dates: List[str],
    closes_by_ticker: Dict[str, List[Optional[float]]],
    adv_by_ticker: Dict[str, float],
    *,
    top_k: int = 8,
    lookback: int = 63,
    rebalance_every: int = 21,
    commission_bps: float = 10.0,
    slip_base_bps: float = 5.0,
    slip_impact: float = 50.0,
    start_cash: float = 100_000.0,
) -> Dict[str, object]:
    """
    Strategia: ogni `rebalance_every` barre, momentum = close[t]/close[t-lookback]-1;
    tiene equal-weight i top_k per momentum; costi = commissione + slippage ADV-aware.
    closes_by_ticker: serie ALLINEATE sulle stesse `dates` (None se mancante).
    Ritorna metriche di rischio/rendimento + diversità.
    """
    n = len(dates)
    cash = start_cash
    holdings: Dict[str, float] = {}      # ticker -> unità
    equity_curve: List[float] = []
    names_held: set = set()
    eff_ns: List[float] = []

    def price(tk: str, i: int) -> Optional[float]:
        s = closes_by_ticker.get(tk)
        if not s or i >= len(s):
            return None
        return s[i]

    def portfolio_value(i: int) -> float:
        val = cash
        for tk, units in holdings.items():
            px = price(tk, i)
            if px:
                val += units * px
        return val

    def trade_cost(tk: str, notional: float) -> float:
        bps = commission_bps + liquidity.slippage_bps(
            adv_by_ticker.get(tk), abs(notional), slip_base_bps, slip_impact)
        return abs(notional) * bps / 10_000.0

    for i in range(n):
        is_rebal = (i >= lookback) and ((i - lookback) % rebalance_every == 0)
        if is_rebal:
            # momentum dei nomi con storia sufficiente e prezzo valido
            scored: List[Tuple[str, float]] = []
            for tk, s in closes_by_ticker.items():
                p_now = price(tk, i)
                p_old = price(tk, i - lookback)
                if p_now and p_old and p_old > 0:
                    scored.append((tk, p_now / p_old - 1.0))
            scored.sort(key=lambda x: x[1], reverse=True)
            target = [tk for tk, m in scored[:top_k] if m > 0]  # solo momentum positivo

            # liquida ciò che non è più target
            for tk in list(holdings.keys()):
                if tk not in target:
                    px = price(tk, i)
                    if px:
                        notional = holdings[tk] * px
                        cash += notional - trade_cost(tk, notional)
                    holdings.pop(tk, None)

            # alloca equal-weight sul valore investibile
            pv = portfolio_value(i)
            if target:
                target_val = pv / len(target)
                for tk in target:
                    px = price(tk, i)
                    if not px:
                        continue
                    cur_val = holdings.get(tk, 0.0) * px
                    diff = target_val - cur_val
                    if diff > 0 and cash >= diff:
                        cash -= diff + trade_cost(tk, diff)
                        holdings[tk] = holdings.get(tk, 0.0) + diff / px
                    elif diff < 0:
                        sell_units = min(holdings.get(tk, 0.0), (-diff) / px)
                        notional = sell_units * px
                        cash += notional - trade_cost(tk, notional)
                        holdings[tk] = holdings.get(tk, 0.0) - sell_units
                    names_held.add(tk)
            # diversità di questo ribilancio
            weights = [holdings[tk] * (price(tk, i) or 0) for tk in holdings]
            eff_ns.append(_effective_n(weights))

        equity_curve.append(portfolio_value(i))

    final_val = equity_curve[-1] if equity_curve else start_cash
    rets = [equity_curve[j] / equity_curve[j - 1] - 1.0
            for j in range(1, len(equity_curve)) if equity_curve[j - 1] > 0]
    sat = [t for t in names_held if universe.is_satellite(t)]
    return {
        "return_pct": round((final_val / start_cash - 1.0) * 100.0, 2),
        "sharpe": (round(compute_sharpe(rets, 252.0) or 0.0, 2) if rets else None),
        "max_drawdown_pct": _max_drawdown_pct(equity_curve),
        "breadth": len(names_held),
        "avg_effective_n": round(sum(eff_ns) / len(eff_ns), 2) if eff_ns else 0.0,
        "satellite_share": round(len(sat) / len(names_held), 3) if names_held else 0.0,
        "final_value": round(final_val, 2),
        "n_rebalances": len(eff_ns),
    }


# ════════════════════════════════════════════════════════════════════════
# Caricamento storia reale (rete) + allineamento
# ════════════════════════════════════════════════════════════════════════
def _load_prices(tickers: List[str], from_date: str, to_date: str
                 ) -> Tuple[List[str], Dict[str, List[Optional[float]]], Dict[str, float]]:
    """Scarica OHLCV giornaliero, allinea sulle date-unione (forward-fill),
    e calcola l'ADV in $ per ogni ticker. Richiede rete."""
    from data_fetchers import fetch_historical_range_sync

    raw: Dict[str, List[dict]] = {}
    for tk in tickers:
        try:
            res = fetch_historical_range_sync(tk, from_date, to_date)
            bars = (res or {}).get("data") or []
            if len(bars) >= 80:
                raw[tk] = bars
        except Exception:
            continue

    all_dates = sorted({b["date"] for bars in raw.values() for b in bars if b.get("date")})
    closes: Dict[str, List[Optional[float]]] = {}
    adv: Dict[str, float] = {}
    for tk, bars in raw.items():
        by_date = {b["date"]: b for b in bars}
        series: List[Optional[float]] = []
        last = None
        for d in all_dates:
            b = by_date.get(d)
            if b and b.get("close"):
                last = float(b["close"])
            series.append(last)  # forward-fill
        closes[tk] = series
        a = liquidity.adv_usd_from_bars(bars, n=20)
        adv[tk] = a if a is not None else 0.0
    return all_dates, closes, adv


def _print_row(label: str, r: Dict[str, object]) -> None:
    print(f"  {label:<10} ret {r['return_pct']:+.1f}%  Sharpe {r['sharpe']}  "
          f"maxDD {r['max_drawdown_pct']:.1f}%  breadth {r['breadth']}  "
          f"eN {r['avg_effective_n']}  sat {float(r['satellite_share']):.0%}")


def main(argv: Optional[List[str]] = None) -> int:
    import sys
    argv = argv if argv is not None else sys.argv[1:]
    from_date = argv[0] if len(argv) > 0 else "2022-01-01"
    to_date = argv[1] if len(argv) > 1 else "2024-12-31"
    top_k = int(argv[2]) if len(argv) > 2 else 8

    core_tickers = list(universe.ALL_ROTATION_TICKERS)
    expanded_tickers = sorted(set(core_tickers) | set(universe.satellite_universe_flat()))

    print("=" * 72)
    print(f"  BACKTEST UNIVERSO: CORE vs ESTESO — {from_date} → {to_date}  (top_k={top_k})")
    print("  (rotazione momentum, slippage ADV-aware: il GATE per EXPANDED_UNIVERSE)")
    print("=" * 72)
    print(f"  Scarico storia: core={len(core_tickers)}, esteso={len(expanded_tickers)} ticker...")

    dates, closes, adv = _load_prices(expanded_tickers, from_date, to_date)
    if not dates:
        print("  Nessun dato scaricato (rete?). Interrompo.")
        return 1

    core_closes = {t: closes[t] for t in core_tickers if t in closes}
    core_adv = {t: adv.get(t, 0.0) for t in core_closes}

    core = simulate(dates, core_closes, core_adv, top_k=top_k)
    expanded = simulate(dates, closes, adv, top_k=top_k)

    print(f"  Barre allineate: {len(dates)}")
    _print_row("CORE", core)
    _print_row("ESTESO", expanded)
    edge = float(expanded["return_pct"]) - float(core["return_pct"])
    d_eN = float(expanded["avg_effective_n"]) - float(core["avg_effective_n"])
    print("-" * 72)
    print(f"  EDGE return: {edge:+.1f}%   Δ effective-N: {d_eN:+.2f}   "
          f"satellite usati: {float(expanded['satellite_share']):.0%}")
    print("  GATE: attiva l'universo esteso solo se l'edge è positivo o la")
    print("        diversità sale SENZA peggiorare Sharpe/maxDD in modo materiale.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
