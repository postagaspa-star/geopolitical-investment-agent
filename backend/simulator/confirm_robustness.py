"""
Conferma di ROBUSTEZZA dello scudo settimanale — i due test che dirimono se
l'edge e' VERO o fortuna del ciclo 2021-24.

(A) FUORI CAMPIONE 2017-2020: il ciclo che il sistema non ha mai 'visto',
    incluso il bear -84% del 2018. Solo asset con dati veri da allora
    (BTC, ETH, LTC). Se l'edge regge qui → robusto.

(B) WALK-FORWARD ROLLING su 2021-2024: divide il periodo in sotto-finestre di
    ~6 mesi consecutive e misura l'edge in OGNI finestra. Se positivo nella
    maggioranza → edge consistente; se viene da 1-2 finestre → fortuna
    concentrata (illusione).

Scudo settimanale (regime su candele 1w), funding incluso, vs buy&hold.

Uso:  python -m simulator.confirm_robustness
"""
from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from agents.crypto_regime import RegimeParams
from simulator.backtest_swing import fetch_daily, _dt_ms
from simulator.backtest_regime import run_regime_backtest


def _weekly(bars):
    out = []
    for i in range(0, len(bars), 7):
        chunk = bars[i:i+7]
        if not chunk:
            continue
        out.append({"t": chunk[0]["t"], "open": chunk[0]["open"],
                    "high": max(b["high"] for b in chunk),
                    "low": min(b["low"] for b in chunk),
                    "close": chunk[-1]["close"],
                    "volume": sum(b["volume"] for b in chunk)})
    return out


_WK = dict(ema_fast=8, ema_slow=20, atr_len=10, short_confirm_bars=2)


def _run(weekly_bars, shield):
    p = RegimeParams(shield=shield, **_WK)
    fund = 0.21 if shield == "short" else 0.0
    r = run_regime_backtest(weekly_bars, p, funding_daily_pct=fund)
    return r["return_pct"], r["buy_hold_pct"], r["max_drawdown_pct"]


def part_A():
    print("=" * 78)
    print("(A) FUORI CAMPIONE 2017-2020 — ciclo mai visto (bear -84% 2018 incluso)")
    print("=" * 78)
    assets = [("BTC", "BTCUSDT"), ("ETH", "ETHUSDT"), ("LTC", "LTCUSDT")]
    print(f"{'asset':<6}{'B&H%':>9}{'cash%':>9}{'short%':>9}{'cashDD%':>9}{'shortDD%':>10}")
    print("-" * 78)
    for name, sym in assets:
        b = fetch_daily(sym, _dt_ms("2017-08-01"), _dt_ms("2020-12-31"))
        if len(b) < 200:
            print(f"{name:<6} dati insuff ({len(b)})"); continue
        wk = _weekly(b)
        cr, bh, cdd = _run(wk, "cash")
        sr, _, sdd = _run(wk, "short")
        print(f"{name:<6}{bh:>9.0f}{cr:>9.1f}{sr:>9.1f}{cdd:>9.1f}{sdd:>10.1f}")
    print("-" * 78)


def part_B():
    print("\n" + "=" * 78)
    print("(B) WALK-FORWARD ROLLING su 2021-2024 — edge per sotto-finestra di 6 mesi")
    print("=" * 78)
    windows = [
        ("2021 H1", "2021-01-01", "2021-06-30"),
        ("2021 H2", "2021-07-01", "2021-12-31"),
        ("2022 H1", "2022-01-01", "2022-06-30"),
        ("2022 H2", "2022-07-01", "2022-12-31"),
        ("2023 H1", "2023-01-01", "2023-06-30"),
        ("2023 H2", "2023-07-01", "2023-12-31"),
        ("2024 H1", "2024-01-01", "2024-06-30"),
        ("2024 H2", "2024-07-01", "2024-12-31"),
    ]
    # BTC come proxy (serie piena e lunga); 6 mesi = ~26 candele settimanali
    print(f"{'finestra':<10}{'B&H%':>9}{'cash%':>9}{'edge-cash':>11}{'short%':>9}{'edge-short':>12}")
    print("-" * 78)
    pos_cash = pos_short = tot = 0
    for name, d0, d1 in windows:
        b = fetch_daily("BTCUSDT", _dt_ms(d0), _dt_ms(d1))
        wk = _weekly(b)
        if len(wk) < 25:
            print(f"{name:<10} poche candele ({len(wk)})"); continue
        cr, bh, _ = _run(wk, "cash")
        sr, _, _ = _run(wk, "short")
        ec, es = cr - bh, sr - bh
        tot += 1
        pos_cash += 1 if ec > 0 else 0
        pos_short += 1 if es > 0 else 0
        print(f"{name:<10}{bh:>9.0f}{cr:>9.1f}{ec:>+11.1f}{sr:>9.1f}{es:>+12.1f}")
    print("-" * 78)
    if tot:
        print(f"finestre con edge POSITIVO: cash {pos_cash}/{tot}, short {pos_short}/{tot}")
        print("Edge VERO se positivo nella maggioranza; se ~meta', e' rumore/fortuna.")


def main(argv):
    part_A()
    part_B()
    print("\nNOTA: 2017-2020 solo BTC/ETH/LTC (le altre 14 non esistevano).")
    print("Funding 0.21%/sett sugli short. Sim al close settimanale.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
