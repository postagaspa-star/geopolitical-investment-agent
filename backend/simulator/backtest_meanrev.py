"""
Backtest del mean-reversion conservativo su periodi LATERALI reali.

La domanda (la tua, Andrea): nel laterale il mean-rev AGGIUNGE valore rispetto
a "non far nulla" (stare cash), oppure e' la trappola che la teoria avverte?

Il robot riceve il regime bar-per-bar da crypto_regime.classify_regime e opera
SOLO quando e' SIDEWAYS. Confronto:
  - mean-rev (solo laterale)  vs  cash fermo (0%)  vs  buy&hold
sui periodi macro reali (gli stessi 11), e in piu' su finestre tipicamente
LATERALI (estati crypto, consolidamenti).

Onesta': sim al close 1d, commissioni 10bps + slippage. Il valore atteso dal
laterale e' PICCOLO per natura → l'asticella e' "fa meglio di stare fermo
SENZA aumentare il rischio?".

Uso:  python -m simulator.backtest_meanrev
"""
from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from agents.crypto_meanrev import MeanRevParams, MeanRevState, step as mr_step, FLAT, LONG
from agents.crypto_regime import classify_regime, RegimeParams
from simulator.backtest_swing import fetch_daily, _dt_ms

COMMISSION_BPS = 10.0
INITIAL_NAV = 100_000.0


def _commission(n): return abs(n) * COMMISSION_BPS / 10_000.0


# Finestre tipicamente LATERALI/consolidamento (dove il mean-rev dovrebbe brillare)
SIDEWAYS_PERIODS = [
    ("BTC estate 2023",  "BTCUSDT", "2023-06-01", "2023-10-01"),
    ("BTC consolid 2024","BTCUSDT", "2024-05-01", "2024-09-01"),
    ("ETH estate 2023",  "ETHUSDT", "2023-06-01", "2023-10-01"),
    ("ETH range 2024",   "ETHUSDT", "2024-05-01", "2024-09-01"),
    ("BTC range 2019",   "BTCUSDT", "2019-05-01", "2019-09-01"),
    ("BTC laterale 2020","BTCUSDT", "2020-05-01", "2020-09-01"),
]


def run_meanrev_backtest(bars, params=None, slippage_bps=5.0) -> dict:
    p = params or MeanRevParams()
    rp = RegimeParams()
    st = MeanRevState()
    cash = INITIAL_NAV
    units = 0.0
    side = FLAT
    entry = 0.0
    equity = []
    trades = []
    fees = 0.0
    bars_in_sideways = 0
    warm = max(p.lookback, rp.ema_slow + 6)

    def nav_now(price):
        return cash + units * price if side == LONG else cash

    def close(price):
        nonlocal cash, units, side, entry, fees
        if side != LONG or units <= 0:
            return
        proceeds = units * price
        fee = _commission(proceeds)
        cash += proceeds - fee
        fees += fee
        trades.append({"entry": entry, "exit": price, "units": units})
        units = 0.0; side = FLAT; entry = 0.0

    def open_long(price, u):
        nonlocal cash, units, side, entry, fees
        fill = price * (1 + slippage_bps / 1e4)
        notional = u * fill
        fee = _commission(notional)
        cash -= notional + fee
        fees += fee
        units = u; side = LONG; entry = fill

    for k in range(warm, len(bars) + 1):
        window = bars[:k]
        closes = [b["close"] for b in window]
        price = closes[-1]
        regime = classify_regime(closes, rp) or "SIDEWAYS"
        if regime == "SIDEWAYS":
            bars_in_sideways += 1
        act = mr_step(st, window, nav=nav_now(price), regime=regime, params=p)
        if act.action == "CLOSE":
            close(price)
        elif act.action == "OPEN_LONG":
            open_long(price, act.units)
        equity.append(nav_now(price))

    if side == LONG:
        close(bars[-1]["close"]); equity.append(cash)

    final = equity[-1] if equity else INITIAL_NAV
    peak, max_dd = INITIAL_NAV, 0.0
    for v in equity:
        peak = max(peak, v)
        max_dd = min(max_dd, (v - peak) / peak * 100.0)
    wins = sum(1 for t in trades if t["exit"] > t["entry"])
    n = len(trades)
    bh = (bars[-1]["close"] / bars[warm - 1]["close"] - 1.0) * 100.0 if len(bars) > warm else 0.0
    ret = (final / INITIAL_NAV - 1.0) * 100.0
    pct_side = bars_in_sideways / max(1, len(bars) - warm) * 100.0
    return {"return_pct": round(ret, 2), "buy_hold_pct": round(bh, 2),
            "max_drawdown_pct": round(max_dd, 2), "trades": n,
            "win_rate_pct": round(wins / n * 100, 1) if n else 0.0,
            "pct_time_sideways": round(pct_side, 0), "bars": len(bars)}


def main(argv):
    rows = []
    for label, sym, d0, d1 in SIDEWAYS_PERIODS:
        print(f"[{label}] {sym} 1d {d0}->{d1}")
        bars = fetch_daily(sym, _dt_ms(d0), _dt_ms(d1))
        if len(bars) < 60:
            print(f"    SKIP ({len(bars)})"); continue
        r = run_meanrev_backtest(bars)
        r["label"] = label
        rows.append(r)
        print(f"    {len(bars)} candele, {r['pct_time_sideways']:.0f}% tempo in laterale")

    print("\n" + "=" * 90)
    print("MEAN-REVERSION conservativo (solo laterale) vs CASH-FERMO(0%) vs BUY&HOLD")
    print("=" * 90)
    print(f"{'periodo':<20}{'mean-rev%':>11}{'cash%':>8}{'B&H%':>9}{'DD%':>9}{'trades':>8}{'win%':>7}{'%side':>7}")
    print("-" * 90)
    for r in rows:
        print(f"{r['label']:<20}{r['return_pct']:>11.2f}{0.0:>8.1f}{r['buy_hold_pct']:>9.1f}"
              f"{r['max_drawdown_pct']:>9.1f}{r['trades']:>8}{r['win_rate_pct']:>7.1f}{r['pct_time_sideways']:>7.0f}")
    print("-" * 90)
    if rows:
        n = len(rows)
        beat_cash = sum(1 for r in rows if r["return_pct"] > 0)
        avg_ret = sum(r["return_pct"] for r in rows) / n
        worst_dd = min(r["max_drawdown_pct"] for r in rows)
        avg_tr = sum(r["trades"] for r in rows) / n
        print(f"  RUN={n}  meglio di cash-fermo(>0%)={beat_cash}/{n}  "
              f"return medio={avg_ret:+.2f}%  worstDD={worst_dd:.1f}%  trade medi={avg_tr:.0f}")
    print("=" * 90)
    print("ASTICELLA (regola Andrea): deve fare >0% (meglio che star fermo) SENZA")
    print("drawdown serio. Se return ~0 o DD alto → 'meglio non far nulla' vince.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
