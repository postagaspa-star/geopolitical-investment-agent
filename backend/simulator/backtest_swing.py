"""
Backtest del Crypto Swing su candele GIORNALIERE reali Binance, periodi lunghi.

La domanda decisiva (la sola che conta dopo ~130 backtest sullo scalper):
LO SWING BATTE IL BUY&HOLD IN UPTREND, e regge nei crash?

Confronta su periodi MACRO reali (bull 2021, bear 2022, recovery 2023, 2024,
multi-anno completo): swing trend-following (long+short, chandelier stop) vs
buy&hold. Commissioni 10bps/lato + slippage opzionale.

Onesta': sim al close giornaliero, no intrabar, no book depth, no funding sugli
short multi-giorno (che nella realta' costerebbe). Lo short multi-giorno qui e'
una semplificazione ottimistica → i numeri short reali sarebbero un po' peggiori.
Serve a capire se la TESI trend-following regge, non a promettere rendimenti.

Uso:  python -m simulator.backtest_swing            (suite completa)
      python -m simulator.backtest_swing BTCUSDT 2021-01-01 2021-12-31
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from agents.crypto_swing import SwingParams, SwingState, step, FLAT, LONG, SHORT

COMMISSION_BPS = 10.0
INITIAL_NAV = 100_000.0
BINANCE = "https://api.binance.com/api/v3/klines"


def _dt_ms(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def fetch_daily(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    out, cur, span = [], start_ms, 86_400_000
    guard = 0
    while cur < end_ms and guard < 40:
        guard += 1
        params = urllib.parse.urlencode({"symbol": symbol, "interval": "1d",
                                         "startTime": cur, "endTime": end_ms, "limit": 1000})
        try:
            req = urllib.request.Request(f"{BINANCE}?{params}",
                                         headers={"User-Agent": "geoinvest-swing"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                arr = json.loads(resp.read().decode())
        except Exception as e:
            print(f"    fetch error: {str(e)[:80]}"); break
        if not arr:
            break
        for k in arr:
            out.append({"t": k[0], "open": float(k[1]), "high": float(k[2]),
                        "low": float(k[3]), "close": float(k[4]), "volume": float(k[5])})
        if arr[-1][0] <= cur or len(arr) < 1000:
            break
        cur = arr[-1][0] + span
        time.sleep(0.1)
    return out


def _commission(n): return abs(n) * COMMISSION_BPS / 10_000.0


def run_swing_backtest(bars: list[dict], params: SwingParams | None = None,
                       slippage_bps: float = 0.0) -> dict:
    p = params or SwingParams()
    st = SwingState()
    cash = INITIAL_NAV
    pos_units = 0.0
    pos_side = FLAT
    pos_entry = 0.0
    equity, trades, fees = [], [], 0.0
    warm = max(p.ema_slow, p.atr_len, int(p.donchian_entry * p.short_donchian_mult)) + 2

    def nav_now(price):
        if pos_side == LONG:
            return cash + pos_units * price
        if pos_side == SHORT:
            return cash + pos_units * (2 * pos_entry - price)
        return cash

    def close_pos(price, reason):
        nonlocal cash, pos_units, pos_side, pos_entry, fees
        if pos_side == FLAT or pos_units <= 0:
            return
        if pos_side == LONG:
            proceeds = pos_units * price
            fee = _commission(proceeds)
            cash += proceeds - fee
        else:
            pnl = pos_units * (pos_entry - price)
            fee = _commission(pos_units * price)
            cash += pnl - fee
        fees += fee
        trades.append({"side": pos_side, "entry": pos_entry, "exit": price, "units": pos_units})
        pos_units = 0.0; pos_side = FLAT; pos_entry = 0.0

    def open_pos(side, price, units):
        nonlocal cash, pos_units, pos_side, pos_entry, fees
        fill = price * (1 + slippage_bps / 1e4) if side == LONG else price * (1 - slippage_bps / 1e4)
        notional = units * fill
        fee = _commission(notional)
        cash -= fee
        if side == LONG:
            cash -= notional
        fees += fee
        pos_units = units; pos_side = side; pos_entry = fill

    for k in range(warm, len(bars) + 1):
        window = bars[:k]
        price = window[-1]["close"]
        act = step(st, window, nav=nav_now(price), params=p)
        if act.action in ("CLOSE", "FLIP_TO_LONG", "FLIP_TO_SHORT"):
            close_pos(price, act.reason)
        if act.action in ("OPEN_LONG", "FLIP_TO_LONG"):
            open_pos(LONG, price, act.units)
        elif act.action in ("OPEN_SHORT", "FLIP_TO_SHORT"):
            open_pos(SHORT, price, act.units)
        equity.append(nav_now(price))

    if pos_side != FLAT:
        close_pos(bars[-1]["close"], "fine-serie")
        equity.append(cash)

    final = equity[-1] if equity else INITIAL_NAV
    peak, max_dd = INITIAL_NAV, 0.0
    for v in equity:
        peak = max(peak, v)
        max_dd = min(max_dd, (v - peak) / peak * 100.0)
    wins = sum(1 for t in trades if _pnl(t) > 0)
    n = len(trades)
    bh = (bars[-1]["close"] / bars[warm - 1]["close"] - 1.0) * 100.0 if len(bars) > warm else 0.0
    ret = (final / INITIAL_NAV - 1.0) * 100.0
    return {"return_pct": round(ret, 2), "buy_hold_pct": round(bh, 2),
            "edge": round(ret - bh, 2), "max_drawdown_pct": round(max_dd, 2),
            "trades": n, "win_rate_pct": round(wins / n * 100, 1) if n else 0.0,
            "bars": len(bars)}


def _pnl(t):
    return (t["exit"] - t["entry"]) * t["units"] if t["side"] == LONG else (t["entry"] - t["exit"]) * t["units"]


# Periodi MACRO reali (Binance ha BTC dal 2017, ETH/SOL dopo)
PERIODS = [
    ("BTC bull 2021",     "BTCUSDT", "2021-01-01", "2021-12-31"),
    ("BTC bear 2022",     "BTCUSDT", "2022-01-01", "2022-12-31"),
    ("BTC recovery 2023", "BTCUSDT", "2023-01-01", "2023-12-31"),
    ("BTC 2024",          "BTCUSDT", "2024-01-01", "2024-12-31"),
    ("BTC multi 21-24",   "BTCUSDT", "2021-01-01", "2024-12-31"),
    ("ETH bull 2021",     "ETHUSDT", "2021-01-01", "2021-12-31"),
    ("ETH bear 2022",     "ETHUSDT", "2022-01-01", "2022-12-31"),
    ("ETH 2023-24",       "ETHUSDT", "2023-01-01", "2024-12-31"),
    ("SOL bull 2021",     "SOLUSDT", "2021-01-01", "2021-12-31"),
    ("SOL bear 2022",     "SOLUSDT", "2022-01-01", "2022-12-31"),
    ("SOL 2023-24",       "SOLUSDT", "2023-01-01", "2024-12-31"),
]


def _classify(bh):
    return "UPTREND" if bh >= 15 else ("DOWNTREND" if bh <= -15 else "SIDEWAYS")


def main(argv):
    if len(argv) > 3:
        sym, d0, d1 = argv[1], argv[2], argv[3]
        bars = fetch_daily(sym, _dt_ms(d0), _dt_ms(d1))
        r = run_swing_backtest(bars, slippage_bps=5.0)
        print(f"{sym} {d0}->{d1}: swing {r['return_pct']:+.1f}% vs B&H {r['buy_hold_pct']:+.1f}% "
              f"(edge {r['edge']:+.1f}) DD {r['max_drawdown_pct']:.1f}% trades {r['trades']}")
        return 0

    rows = []
    for label, sym, d0, d1 in PERIODS:
        print(f"[{label}] scarico {sym} 1d {d0}->{d1}...")
        bars = fetch_daily(sym, _dt_ms(d0), _dt_ms(d1))
        if len(bars) < 80:
            print(f"    SKIP ({len(bars)} candele)"); continue
        r = run_swing_backtest(bars, slippage_bps=5.0)
        r["label"] = label; r["regime"] = _classify(r["buy_hold_pct"])
        rows.append(r)
        print(f"    {len(bars)} candele")

    print("\n" + "=" * 86)
    print("SWING TREND-FOLLOWING (1d, slippage 5bps) vs BUY&HOLD — periodi macro reali")
    print("=" * 86)
    print(f"{'periodo':<20}{'regime':<11}{'swing%':>9}{'B&H%':>9}{'edge':>9}{'DD%':>9}{'trades':>8}{'win%':>7}")
    print("-" * 86)
    for r in rows:
        print(f"{r['label']:<20}{r['regime']:<11}{r['return_pct']:>9.1f}{r['buy_hold_pct']:>9.1f}"
              f"{r['edge']:>9.1f}{r['max_drawdown_pct']:>9.1f}{r['trades']:>8}{r['win_rate_pct']:>7.1f}")
    print("-" * 86)
    if rows:
        n = len(rows)
        beat = sum(1 for r in rows if r["edge"] > 0)
        prof = sum(1 for r in rows if r["return_pct"] > 0)
        avg_edge = sum(r["edge"] for r in rows) / n
        by = {}
        for r in rows:
            by.setdefault(r["regime"], []).append(r["edge"])
        print(f"  RUN={n}  edge medio={avg_edge:+.1f}  battono_B&H={beat}/{n}  profittevoli={prof}/{n}")
        for reg, e in sorted(by.items()):
            print(f"    edge[{reg:<9}]={sum(e)/len(e):+.1f}  (n={len(e)})")
    print("=" * 86)
    print("NOTA: short multi-giorno senza costo funding = ottimistico sugli short.")
    print("Sim al close giornaliero, no intrabar/book depth.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
