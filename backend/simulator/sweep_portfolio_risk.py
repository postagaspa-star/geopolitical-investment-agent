"""
Sweep delle leve di rischio del portafoglio — UNA leva alla volta, isolata.

Scarica i dati UNA volta, poi confronta:
  A) baseline (tutto OFF) = la versione migliore +349%/-66%
  B) cash-floor a vari livelli (100/85/70/55% investito) — leva SENZA
     vendi-ricompra: abbassa il DD in proporzione lasciando cash a riposo
  C) fast-stop per-asset da solo (12/18/25%) — per i crash veloci, isolato dal
     circuit breaker (che era la causa del peggioramento)

Mostra per ognuna: return, max DD, edge vs equal-weight, e il RAPPORTO
guadagno/dolore (return / |DD|) — il vero metro quando si bilancia rischio.

Uso:  python -m simulator.sweep_portfolio_risk [1d|4h] [start] [end]
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from agents.crypto_selector import UNIVERSE_14
from simulator.backtest_swing import _dt_ms
from simulator.backtest_portfolio import _fetch, run_portfolio_backtest


def _row(label, data, interval, warm, **kw):
    r = run_portfolio_backtest(data, interval, warm=warm, **kw)
    if "error" in r:
        return {"label": label, "err": r["error"]}
    dd = r["max_drawdown_pct"]
    ratio = r["return_pct"] / abs(dd) if dd != 0 else 0.0
    return {"label": label, "ret": r["return_pct"], "dd": dd, "edge": r["edge"],
            "ratio": round(ratio, 2), "avg_pos": r["avg_positions"], "trades": r["trades"]}


def main(argv):
    interval = argv[1] if len(argv) > 1 else "1d"
    d0 = argv[2] if len(argv) > 2 else "2021-01-01"
    d1 = argv[3] if len(argv) > 3 else "2024-12-31"
    warm = 60 if interval == "1d" else 120

    print(f"Scarico universo 14 {interval} {d0}->{d1} (una volta) ...")
    data = {}
    for sym in UNIVERSE_14:
        bars = _fetch(sym, interval, _dt_ms(d0), _dt_ms(d1))
        if len(bars) >= warm:
            data[sym] = bars
    print(f"  {len(data)}/14 asset disponibili\n")
    if len(data) < 5:
        print("Dati insufficienti."); return 1

    rows = []
    rows.append(_row("baseline (tutto OFF)", data, interval, warm))
    # B) cash-floor isolato
    for mp in (85.0, 70.0, 55.0):
        rows.append(_row(f"cash-floor {mp:.0f}%", data, interval, warm, max_invested_pct=mp))
    # C) fast-stop per-asset isolato (senza circuit breaker)
    for fs in (25.0, 18.0, 12.0):
        rows.append(_row(f"fast-stop -{fs:.0f}%", data, interval, warm, asset_fast_stop_pct=fs))
    # D) combo migliore plausibile: cash-floor 70 + fast-stop 18
    rows.append(_row("cash-floor70 + faststop18", data, interval, warm,
                     max_invested_pct=70.0, asset_fast_stop_pct=18.0))

    print("=" * 84)
    print(f"SWEEP LEVE DI RISCHIO (isolate)  {interval}  {d0}->{d1}   [benchmark EW-hold-14]")
    print("=" * 84)
    print(f"{'configurazione':<28}{'return%':>9}{'maxDD%':>9}{'edge':>9}{'ret/DD':>9}{'pos':>6}{'trade':>7}")
    print("-" * 84)
    for r in rows:
        if "err" in r:
            print(f"{r['label']:<28}  ERR: {r['err']}"); continue
        print(f"{r['label']:<28}{r['ret']:>9.0f}{r['dd']:>9.1f}{r['edge']:>9.0f}"
              f"{r['ratio']:>9.2f}{r['avg_pos']:>6.1f}{r['trades']:>7}")
    print("-" * 84)
    print("ret/DD = guadagno per unita' di dolore (piu' alto = meglio bilanciato).")
    print("Obiettivo Andrea: DD sotto -25%. Vediamo quale leva ci arriva e a che")
    print("costo di guadagno. Circuit breaker ESCLUSO (bocciato: vende-ricompra).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
