"""
Portafoglio 14 major con SCUDO-SHORT SETTIMANALE (effetto aggregato realistico).

Finora lo scudo-short settimanale e' stato testato su SINGOLI asset (BTC, SOL...).
Qui lo applichiamo al PORTAFOGLIO: capitale diviso equamente tra le 14 major,
ogni asset gestito dal meta-regime SETTIMANALE (long in uptrend / cash o SHORT
in downtrend macro confermato / cash in laterale), funding incluso sugli short.

Confronto onesto: aggregato del sistema vs equal-weight-hold delle 14.

Resample 1d->1w deterministico (settimane da 7 candele). Sim al close
settimanale, commissioni 10bps, funding 0.21%/settimana sugli short.

Uso:  python -m simulator.backtest_portfolio_weekly_shield [start] [end] [cash|short]
"""
from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from agents.crypto_selector import UNIVERSE_14
from agents.crypto_regime import RegimeParams, RegimeState, step, FLAT, LONG, SHORT
from simulator.backtest_swing import fetch_daily, _dt_ms

INITIAL_NAV = 100_000.0
COMMISSION_BPS = 10.0


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


def _commission(n):
    return abs(n) * COMMISSION_BPS / 10_000.0


def _run_one_asset(bars, shield, funding_wk=0.21):
    """Equity curve (lista NAV per candela) di UN asset gestito dal meta-regime
    settimanale, partendo da capitale 1.0 (frazione). Long/short/cash + funding."""
    p = RegimeParams(shield=shield, short_confirm_bars=2,
                     ema_slow=20, ema_fast=8, atr_len=10)
    st = RegimeState()
    cash = 1.0
    units = 0.0
    side = FLAT
    entry = 0.0
    warm = p.ema_slow + 6
    curve = []  # (index_settimana, valore)

    def nav(price):
        if side == LONG:
            return cash + units * price
        if side == SHORT:
            # cash-neutral (#11): coerente col close (units*(entry-price));
            # 2*entry-price gonfiava NAV e sottostimava il DD degli short.
            return cash + units * (entry - price)
        return cash

    def close(price):
        nonlocal cash, units, side, entry
        if side == FLAT or units <= 0:
            return
        if side == LONG:
            cash += units * price - _commission(units * price)
        else:
            cash += units * (entry - price) - _commission(units * price)
        units = 0.0; side = FLAT; entry = 0.0

    def open_(ns, price, u):
        nonlocal cash, units, side, entry
        cash -= _commission(u * price)
        if ns == LONG:
            cash -= u * price
        units = u; side = ns; entry = price

    for k in range(warm, len(bars) + 1):
        price = bars[k-1]["close"]
        if side == SHORT and units > 0 and funding_wk > 0:
            cash -= units * price * funding_wk / 100.0
        act = step(st, bars[:k], nav=nav(price), params=p)
        if act.action == "CLOSE_ALL":
            close(price)
        elif act.action == "OPEN_LONG":
            close(price); open_(LONG, price, act.units)
        elif act.action == "OPEN_SHORT":
            close(price); open_(SHORT, price, act.units)
        curve.append((k, nav(price)))
    if side != FLAT:
        close(bars[-1]["close"])
        if curve:
            curve[-1] = (curve[-1][0], cash)
    return curve, warm


def main(argv):
    d0 = argv[1] if len(argv) > 1 else "2021-01-01"
    d1 = argv[2] if len(argv) > 2 else "2024-12-31"
    shield = argv[3] if len(argv) > 3 else "short"

    print(f"Scarico 14 major 1d {d0}->{d1}, resample settimanale, scudo={shield} ...")
    weekly = {}
    for sym in UNIVERSE_14:
        b = fetch_daily(sym, _dt_ms(d0), _dt_ms(d1))
        if len(b) >= 200:
            weekly[sym] = _weekly(b)
    n = len(weekly)
    print(f"  {n}/14 asset")
    if n < 5:
        print("Dati insuff."); return 1

    # AGGREGAZIONE EQUIPESATA semplice e robusta: ogni asset gestito dal regime
    # settimanale produce una sua curva NAV (frazione, base 1.0). Il portafoglio
    # equipesato e' la MEDIA delle curve (ogni asset pesa 1/n). Cosi' return e
    # drawdown del portafoglio sono quelli della curva media. Stessa cosa per il
    # buy&hold (media delle curve prezzo normalizzate).
    sys_curves = []
    bh_curves = []
    for sym, wk in weekly.items():
        curve, warm = _run_one_asset(wk, shield)
        sys_vals = [v for (_, v) in curve]          # base 1.0
        if not sys_vals:
            continue
        sys_curves.append(sys_vals)
        prices = [b["close"] for b in wk][warm:]
        if prices:
            p0 = prices[0]
            bh_curves.append([px / p0 for px in prices])

    min_len = min(len(c) for c in sys_curves)
    bh_min = min(len(c) for c in bh_curves)
    L = min(min_len, bh_min)

    # curva media (equipesata), in NAV assoluto (base INITIAL_NAV)
    sys_curve = [INITIAL_NAV * sum(c[i] for c in sys_curves) / len(sys_curves) for i in range(L)]
    bh_curve = [INITIAL_NAV * sum(c[i] for c in bh_curves) / len(bh_curves) for i in range(L)]

    def metrics(curve):
        if not curve:
            return 0.0, 0.0
        peak, dd = curve[0], 0.0
        for v in curve:
            peak = max(peak, v)
            if peak > 0:
                dd = min(dd, (v - peak) / peak * 100.0)
        ret = (curve[-1] / curve[0] - 1.0) * 100.0
        return ret, dd

    sret, sdd = metrics(sys_curve)
    bret, bdd = metrics(bh_curve)
    print("\n" + "=" * 76)
    print(f"PORTAFOGLIO 14 MAJOR — scudo {shield} SETTIMANALE  {d0}->{d1}")
    print("=" * 76)
    print(f"  Sistema (long/scudo) : {sret:+.1f}%   max DD {sdd:.1f}%")
    print(f"  Equal-weight-hold 14 : {bret:+.1f}%   max DD {bdd:.1f}%")
    print(f"  EDGE                 : {sret - bret:+.1f} punti")
    print("=" * 76)
    print("Funding 0.21%/sett sugli short incluso. Sim al close settimanale.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
