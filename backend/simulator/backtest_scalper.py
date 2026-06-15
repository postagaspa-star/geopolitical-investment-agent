"""
Backtest del Crypto Scalper su klines intraday REALI di Binance.

Scarica candele storiche 5m/15m (Binance public API, no key, startTime/endTime),
fa girare la macchina a stati bar-per-bar con commissioni realistiche
(10 bps/lato, come il portfolio live), e misura cosa succede DAVVERO:
  - P&L netto vs buy&hold
  - max drawdown, win-rate, numero di trade, commissioni totali
  - comportamento in finestre TRENDING vs LATERALI (il banco di prova vero:
    in laterale uno scalper a flip si dissangua se il vol-gate non funziona)

Questo e' il "prima lo provo su dati reali" promesso ad Andrea PRIMA di
accenderlo sul live. Onesto: e' comunque una simulazione semplificata
(esecuzione al close della candela, nessuno slippage oltre la commissione,
nessun book depth). Serve a capire l'ordine di grandezza e il profilo di
rischio, non a promettere rendimenti.

Uso:
    python -m simulator.backtest_scalper                  # default: BTC 5m 30g
    python -m simulator.backtest_scalper ETHUSDT 15m 60   # symbol interval days
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from agents.crypto_scalper import ScalperParams, ScalperState, step, FLAT, LONG, SHORT

COMMISSION_BPS = 10.0          # 10 bps per lato (come il portfolio live)
INITIAL_NAV = 100_000.0
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
_INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}


def fetch_klines(symbol: str, interval: str, days: int) -> list[dict]:
    """Scarica klines storici paginando a ritroso (Binance max 1000/righe)."""
    span_ms = _INTERVAL_MS.get(interval, 300_000)
    # NB: i timestamp servono in ms; calcolati dal caller (non usiamo Date.now
    # qui dentro per riproducibilita' — passiamo end=now via param dal main).
    raise RuntimeError("usa fetch_klines_range")  # placeholder, vedi sotto


def fetch_klines_range(symbol: str, interval: str, start_ms: int, end_ms: int) -> list[dict]:
    out: list[dict] = []
    cur = start_ms
    span = _INTERVAL_MS.get(interval, 300_000)
    while cur < end_ms:
        params = urllib.parse.urlencode({
            "symbol": symbol, "interval": interval,
            "startTime": cur, "endTime": end_ms, "limit": 1000,
        })
        req = urllib.request.Request(f"{BINANCE_KLINES}?{params}",
                                     headers={"User-Agent": "geoinvest-backtest"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            arr = json.loads(resp.read().decode())
        if not arr:
            break
        for k in arr:
            out.append({"t": k[0], "open": float(k[1]), "high": float(k[2]),
                        "low": float(k[3]), "close": float(k[4]),
                        "volume": float(k[5])})
        last = arr[-1][0]
        if last <= cur:
            break
        cur = last + span
        if len(arr) < 1000:
            break
    return out


def _commission(notional: float) -> float:
    return abs(notional) * COMMISSION_BPS / 10_000.0


def run_backtest(bars: list[dict], params: ScalperParams | None = None,
                 slippage_bps: float = 0.0) -> dict:
    """Simula la macchina a stati bar-per-bar. Contabilita' cash+posizione,
    commissione a ogni apertura/chiusura, slippage opzionale sui fill.
    Ritorna metriche."""
    p = params or ScalperParams()
    st = ScalperState()
    cash = INITIAL_NAV
    pos_units = 0.0
    pos_side = FLAT
    pos_entry = 0.0
    equity_curve = []
    trades = []
    fees_total = 0.0

    warm = max(p.ema_slow, p.atr_len, p.rsi_len) + 2

    def nav_now(price):
        if pos_side == LONG:
            return cash + pos_units * price
        if pos_side == SHORT:
            # cash-neutral (#11): coerente col close (pos_units*(pos_entry-price));
            # 2*pos_entry-price gonfiava NAV e sottostimava il DD degli short.
            return cash + pos_units * (pos_entry - price)
        return cash

    def close_pos(price, reason):
        nonlocal cash, pos_units, pos_side, pos_entry, fees_total
        if pos_side == FLAT or pos_units <= 0:
            return
        if pos_side == LONG:
            proceeds = pos_units * price
            fee = _commission(proceeds)
            cash += proceeds - fee
        else:  # SHORT: profitto se prezzo sceso
            pnl = pos_units * (pos_entry - price)
            notional = pos_units * price
            fee = _commission(notional)
            cash += pnl - fee
        fees_total += fee
        trades.append({"side": pos_side, "entry": pos_entry, "exit": price,
                       "units": pos_units, "reason": reason})
        pos_units = 0.0
        pos_side = FLAT
        pos_entry = 0.0

    def open_pos(side, price, units):
        nonlocal cash, pos_units, pos_side, pos_entry, fees_total
        # slippage: paghi un po' peggio del close (fill realistico)
        fill = price * (1 + slippage_bps / 10_000.0) if side == LONG else price * (1 - slippage_bps / 10_000.0)
        notional = units * fill
        fee = _commission(notional)
        cash -= fee
        if side == LONG:
            cash -= notional
        fees_total += fee
        pos_units = units
        pos_side = side
        pos_entry = fill

    def add_pos(side, price, units):
        """Pyramiding: aggiunge `units` alla posizione esistente, media il
        prezzo d'ingresso. Slippage + fee come una nuova apertura."""
        nonlocal cash, pos_units, pos_side, pos_entry, fees_total
        if pos_side != side or pos_units <= 0:
            return
        fill = price * (1 + slippage_bps / 10_000.0) if side == LONG else price * (1 - slippage_bps / 10_000.0)
        notional = units * fill
        fee = _commission(notional)
        cash -= fee
        if side == LONG:
            cash -= notional
        fees_total += fee
        # media ponderata dell'entry
        tot = pos_units + units
        pos_entry = (pos_entry * pos_units + fill * units) / tot if tot > 0 else fill
        pos_units = tot

    for k in range(warm, len(bars) + 1):
        window = bars[:k]
        price = window[-1]["close"]
        act = step(st, window, nav=nav_now(price), params=p)

        if act.action in ("CLOSE", "FLIP_TO_LONG", "FLIP_TO_SHORT"):
            close_pos(price, act.reason)
        if act.action == "OPEN_LONG":
            open_pos(LONG, price, act.units)
        elif act.action == "OPEN_SHORT":
            open_pos(SHORT, price, act.units)
        elif act.action == "FLIP_TO_LONG":
            open_pos(LONG, price, act.units)
        elif act.action == "FLIP_TO_SHORT":
            open_pos(SHORT, price, act.units)
        elif act.action == "ADD_LONG":
            add_pos(LONG, price, act.units)
        elif act.action == "ADD_SHORT":
            add_pos(SHORT, price, act.units)

        equity_curve.append(nav_now(price))

    # chiudi a fine serie
    if pos_side != FLAT:
        close_pos(bars[-1]["close"], "fine-serie")
        equity_curve.append(cash)

    # metriche
    final = equity_curve[-1] if equity_curve else INITIAL_NAV
    peak = INITIAL_NAV
    max_dd = 0.0
    for v in equity_curve:
        peak = max(peak, v)
        dd = (v - peak) / peak * 100.0
        max_dd = min(max_dd, dd)
    wins = sum(1 for t in trades if _trade_pnl(t) > 0)
    n = len(trades)
    bh = (bars[-1]["close"] / bars[warm - 1]["close"] - 1.0) * 100.0 if len(bars) > warm else 0.0
    return {
        "final_nav": round(final, 2),
        "return_pct": round((final / INITIAL_NAV - 1.0) * 100.0, 2),
        "buy_hold_pct": round(bh, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "trades": n,
        "win_rate_pct": round(wins / n * 100.0, 1) if n else 0.0,
        "fees_total": round(fees_total, 2),
        "bars": len(bars),
    }


def _trade_pnl(t):
    if t["side"] == LONG:
        return (t["exit"] - t["entry"]) * t["units"]
    return (t["entry"] - t["exit"]) * t["units"]


def main(argv):
    symbol = argv[1] if len(argv) > 1 else "BTCUSDT"
    interval = argv[2] if len(argv) > 2 else "5m"
    days = int(argv[3]) if len(argv) > 3 else 30
    # end = ora; usiamo il clock REALE solo qui nel main (lo script CLI),
    # mai dentro la logica deterministica.
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * 86_400_000
    print(f"Scarico {symbol} {interval} ~{days}g da Binance...")
    bars = fetch_klines_range(symbol, interval, start_ms, end_ms)
    print(f"  {len(bars)} candele scaricate")
    if len(bars) < 100:
        print("Dati insufficienti."); return 1

    res = run_backtest(bars)
    print("=" * 64)
    print(f"BACKTEST SCALPER — {symbol} {interval} ({days}g, {res['bars']} candele)")
    print("=" * 64)
    print(f"  Return scalper : {res['return_pct']:+.2f}%   (NAV {res['final_nav']:,})")
    print(f"  Buy & Hold     : {res['buy_hold_pct']:+.2f}%")
    print(f"  Max drawdown   : {res['max_drawdown_pct']:.2f}%")
    print(f"  Trade totali   : {res['trades']}   win-rate {res['win_rate_pct']}%")
    print(f"  Commissioni    : {res['fees_total']:,} (10bps/lato)")
    print("=" * 64)
    edge = res["return_pct"] - res["buy_hold_pct"]
    print(f"  Edge vs B&H    : {edge:+.2f} punti")
    print("  NOTA: simulazione al close, no slippage/book depth. Ordine di")
    print("  grandezza, non promessa di rendimento.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
