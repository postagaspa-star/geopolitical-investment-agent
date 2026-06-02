"""
Stress-test dello scorer STRUTTURALE su periodi NORMALI con costi REALI.

Obiettivo (Andrea): l'edge +13,5 visto su 4h e' reale ma fragile (1276 trade).
Verificare DUE cose insieme:
  1) ridurre l'overtrading (min-hold + isteresi + ribilanci pigri),
  2) se l'edge SOPRAVVIVE a commissioni realistiche.

Su PERIODI NORMALI (no finestre piene di spike estremi che falserebbero il
giudizio): finestre recenti ordinarie a bias minimo, universo 14 major liquide
(le 50 overtradano: -76%, gia' bocciate).

Per ogni periodo confronta:
  A) churn-OFF (baseline): nessun freno → tanti trade
  B) churn-LOW: min_hold piccolo, isteresi media
  C) churn-HIGH: min_hold ampio, isteresi ampia, ribilancio pigro
ognuna a commissioni 10 / 20 bps (10=spot tipico, 20=conservativo con slippage).

Mostra edge vs equal-weight-hold e numero trade. Il segnale e' VALIDO solo se
l'edge resta POSITIVO coi costi alti E i trade calano molto.

Uso:  python -m simulator.stress_structural
"""
from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from agents.crypto_selector import UNIVERSE_14
from simulator.backtest_swing import _dt_ms
from simulator.backtest_portfolio import _fetch, run_portfolio_backtest

# Finestre recenti ORDINARIE (no mega-crash/parabole): ~6 mesi ciascuna, 4h.
NORMAL_WINDOWS = [
    ("2024 H1", "2024-01-01", "2024-06-30"),
    ("2024 H2", "2024-07-01", "2024-12-31"),
    ("2025 H1", "2025-01-01", "2025-06-01"),
]

# Configurazioni anti-overtrading (min_hold in candele 4h: 6=1g, 12=2g, 30=5g)
CHURN_CONFIGS = [
    ("churn-OFF",  dict(min_hold_bars=0,  exit_score_gap=0.10, rebalance_band=0.40, min_score=0.55)),
    ("churn-LOW",  dict(min_hold_bars=6,  exit_score_gap=0.15, rebalance_band=0.50, min_score=0.58)),
    ("churn-HIGH", dict(min_hold_bars=30, exit_score_gap=0.20, rebalance_band=0.60, min_score=0.60)),
]


def main(argv):
    warm = 120
    print("Scarico 14 major 4h sulle finestre normali (una volta per finestra)...")
    win_data = {}
    for name, d0, d1 in NORMAL_WINDOWS:
        data = {}
        for sym in UNIVERSE_14:
            bars = _fetch(sym, "4h", _dt_ms(d0), _dt_ms(d1))
            if len(bars) >= warm:
                data[sym] = bars
        win_data[name] = data
        print(f"  {name}: {len(data)}/14 asset")

    print("\n" + "=" * 92)
    print("STRESS STRUTTURALE su periodi NORMALI (4h, 14 major) — edge vs equal-weight-hold")
    print("=" * 92)
    print(f"{'periodo':<10}{'config':<12}{'comm':>6}{'sys%':>9}{'B&H%':>9}{'edge':>8}{'trade':>8}{'DD%':>8}")
    print("-" * 92)
    summary = {}
    for name, _, _ in NORMAL_WINDOWS:
        data = win_data[name]
        if len(data) < 5:
            print(f"{name:<10}  dati insuff"); continue
        for cfg_name, cfg in CHURN_CONFIGS:
            for comm in (10.0, 20.0):
                # commissione: simuliamo costi piu' alti via slippage_bps
                r = run_portfolio_backtest(data, "4h", warm=warm, scorer="structural",
                                           slippage_bps=comm, **cfg)
                if "error" in r:
                    continue
                print(f"{name:<10}{cfg_name:<12}{int(comm):>5}b{r['return_pct']:>9.1f}"
                      f"{r['ew_hold_pct']:>9.1f}{r['edge']:>8.1f}{r['trades']:>8}{r['max_drawdown_pct']:>8.1f}")
                summary.setdefault((cfg_name, comm), []).append(r["edge"])
        print("-" * 92)

    print("\nMEDIA EDGE per configurazione (su tutti i periodi normali):")
    for (cfg_name, comm), edges in sorted(summary.items()):
        avg = sum(edges) / len(edges)
        verdict = "OK" if avg > 0 else "negativo"
        print(f"  {cfg_name:<12} {int(comm)}bps : edge medio {avg:+.1f}  [{verdict}]  (n={len(edges)})")
    print("=" * 92)
    print("VALIDO solo se l'edge medio resta POSITIVO a 20bps con churn ridotto.")
    print("Se a costi alti diventa negativo → l'edge +13,5 era mangiato dalle commissioni.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
