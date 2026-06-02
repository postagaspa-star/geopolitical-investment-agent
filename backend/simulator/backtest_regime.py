"""
Backtest del META-CERVELLO a 3 stati (crypto_regime) su periodi macro reali.

LA domanda finale: il sistema "UP→tieni / DOWN→scudo / SIDE→flat totale" cattura
i rialzi COME il buy&hold MA evita i crash? E quanto costa il laterale (whipsaw)?

Confronta su bull/bear/recovery reali (BTC/ETH/SOL, candele 1d):
  - META cash  : in downtrend esce e basta (prudente)
  - META short : in downtrend shorta (piu' aggressivo)
  - buy&hold   : riferimento

Conta i cambi di regime (= operazioni = commissioni) per misurare il whipsaw.
Onesta': sim al close giornaliero, commissioni 10bps/lato + slippage, no funding
sugli short → short multi-giorno un filo ottimistico.

Uso:  python -m simulator.backtest_regime
"""
from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from agents.crypto_regime import RegimeParams, RegimeState, step, FLAT, LONG, SHORT
from simulator.backtest_swing import fetch_daily, _dt_ms, PERIODS, _classify

COMMISSION_BPS = 10.0
INITIAL_NAV = 100_000.0


def _commission(n): return abs(n) * COMMISSION_BPS / 10_000.0


def run_regime_backtest(bars, params=None, slippage_bps=5.0,
                        funding_daily_pct=0.03) -> dict:
    """funding_daily_pct: costo giornaliero di tenere uno SHORT aperto (% del
    notional). ~0.03%/giorno = 0.01%/8h, tipico funding crypto. E' il costo che
    rende lo short multi-giorno MENO redditizio di quanto sembri. 0 = ignora."""
    p = params or RegimeParams()
    st = RegimeState()
    cash = INITIAL_NAV
    units = 0.0
    side = FLAT
    entry = 0.0
    equity = []
    regime_changes = 0
    fees = 0.0
    funding_paid = 0.0
    warm = p.ema_slow + 6

    def nav_now(price):
        if side == LONG:
            return cash + units * price
        if side == SHORT:
            return cash + units * (2 * entry - price)
        return cash

    def close(price):
        nonlocal cash, units, side, entry, fees
        if side == FLAT or units <= 0:
            return
        if side == LONG:
            proceeds = units * price
            fee = _commission(proceeds)
            cash += proceeds - fee
        else:
            fee = _commission(units * price)
            cash += units * (entry - price) - fee
        fees += fee
        units = 0.0; side = FLAT; entry = 0.0

    def open_(new_side, price, u):
        nonlocal cash, units, side, entry, fees
        fill = price * (1 + slippage_bps / 1e4) if new_side == LONG else price * (1 - slippage_bps / 1e4)
        notional = u * fill
        fee = _commission(notional)
        cash -= fee
        if new_side == LONG:
            cash -= notional
        fees += fee
        units = u; side = new_side; entry = fill

    for k in range(warm, len(bars) + 1):
        window = bars[:k]
        price = window[-1]["close"]
        # FUNDING: ogni candela in cui sei SHORT paghi il costo di mantenimento
        # (% del notional). E' la voce che mancava e gonfiava lo short.
        if side == SHORT and units > 0 and funding_daily_pct > 0:
            cost = units * price * funding_daily_pct / 100.0
            cash -= cost
            funding_paid += cost
        act = step(st, window, nav=nav_now(price), params=p)
        if act.action == "CLOSE_ALL":
            close(price); regime_changes += 1
        elif act.action == "OPEN_LONG":
            close(price); open_(LONG, price, act.units); regime_changes += 1
        elif act.action == "OPEN_SHORT":
            close(price); open_(SHORT, price, act.units); regime_changes += 1
        equity.append(nav_now(price))

    if side != FLAT:
        close(bars[-1]["close"]); equity.append(cash)

    final = equity[-1] if equity else INITIAL_NAV
    peak, max_dd = INITIAL_NAV, 0.0
    for v in equity:
        peak = max(peak, v)
        max_dd = min(max_dd, (v - peak) / peak * 100.0)
    bh = (bars[-1]["close"] / bars[warm - 1]["close"] - 1.0) * 100.0 if len(bars) > warm else 0.0
    ret = (final / INITIAL_NAV - 1.0) * 100.0
    return {"return_pct": round(ret, 2), "buy_hold_pct": round(bh, 2),
            "edge": round(ret - bh, 2), "max_drawdown_pct": round(max_dd, 2),
            "regime_changes": regime_changes, "bars": len(bars),
            "funding_paid": round(funding_paid, 0)}


def main(argv):
    for shield in ("cash", "short"):
        rows = []
        print(f"\n### Scarico dati e simulo META shield={shield} ...")
        for label, sym, d0, d1 in PERIODS:
            bars = fetch_daily(sym, _dt_ms(d0), _dt_ms(d1))
            if len(bars) < 80:
                continue
            r = run_regime_backtest(bars, RegimeParams(shield=shield), slippage_bps=5.0)
            r["label"] = label
            r["regime"] = _classify(r["buy_hold_pct"])
            rows.append(r)

        print("\n" + "=" * 92)
        print(f"META-CERVELLO  shield={shield.upper()}  (UP=tieni / DOWN=scudo / SIDE=flat)  vs BUY&HOLD")
        print("=" * 92)
        print(f"{'periodo':<20}{'regime':<11}{'meta%':>9}{'B&H%':>10}{'edge':>9}{'DD%':>9}{'cambi':>7}")
        print("-" * 92)
        for r in rows:
            print(f"{r['label']:<20}{r['regime']:<11}{r['return_pct']:>9.1f}{r['buy_hold_pct']:>10.1f}"
                  f"{r['edge']:>9.1f}{r['max_drawdown_pct']:>9.1f}{r['regime_changes']:>7}")
        print("-" * 92)
        if rows:
            n = len(rows)
            beat = sum(1 for r in rows if r["edge"] > 0)
            prof = sum(1 for r in rows if r["return_pct"] > 0)
            avg_dd = sum(r["max_drawdown_pct"] for r in rows) / n
            bh_dd = "n/a"
            by = {}
            for r in rows:
                by.setdefault(r["regime"], []).append(r["edge"])
            print(f"  RUN={n}  battono_B&H={beat}/{n}  profittevoli={prof}/{n}  "
                  f"DD medio meta={avg_dd:.1f}%  cambi medi={sum(r['regime_changes'] for r in rows)/n:.0f}")
            for reg, e in sorted(by.items()):
                print(f"    edge[{reg:<9}]={sum(e)/len(e):+.1f}  (n={len(e)})")
        print("=" * 92)
    print("\nNOTA: sim al close 1d, commissioni 10bps + slippage 5bps, no funding short.")
    print("OBIETTIVO: edge>=0 in UPTREND (cattura i rialzi) + DD molto < buy&hold (protegge).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
