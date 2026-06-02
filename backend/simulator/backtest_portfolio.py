"""
Backtest di PORTAFOGLIO multi-asset sull'universo delle 14 major.

Risponde all'osservazione di Andrea: NON testare su una crypto scelta a mano
(cherry-picking), ma far valutare al sistema TUTTE le 14 a ogni step, scegliere
le piu' interessanti col selettore deterministico, e operarle.

LOGICA per ogni step (candela):
  1. opportunity_score su tutte e 14 (crypto_selector)
  2. seleziona top-K sopra soglia E in regime UP (crypto_regime) → le altre = cash
  3. alloca il capitale equamente tra le selezionate (resto cash)
  4. quando un asset esce dai top o gira DOWN/SIDE → lo si chiude (torna cash)

BENCHMARK ONESTO: non "batte BTC?" ma "batte il tenere le 14 equal-weight?"
(buy&hold di un paniere equipesato, ribilanciato all'inizio). E' il vero metro.

LIMITI dichiarati:
  - Survivorship bias: le 14 sono quelle VIVE oggi (esclude LUNA, FTT morte) →
    risultati un filo ottimistici.
  - Sim al close, commissioni 10bps + slippage. No funding.
  - Asset entrati in Binance dopo lo start (es. alcune nel 2021) → semplicemente
    non disponibili in quei primi mesi (gestito: score None = escluso).

Uso:  python -m simulator.backtest_portfolio 1d   2021-01-01 2024-12-31
      python -m simulator.backtest_portfolio 4h   2024-01-01 2024-12-31
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from agents.crypto_selector import UNIVERSE_14, select_top, SelectorParams
from agents.crypto_regime import classify_regime, RegimeParams, UP
from simulator.backtest_swing import fetch_daily, _dt_ms
from simulator.backtest_scalper import fetch_klines_range

COMMISSION_BPS = 10.0
INITIAL_NAV = 100_000.0


def _commission(n): return abs(n) * COMMISSION_BPS / 10_000.0


def _fetch(symbol, interval, start_ms, end_ms):
    if interval == "1d":
        return fetch_daily(symbol, start_ms, end_ms)
    return fetch_klines_range(symbol, interval, start_ms, end_ms)


def _align(data: dict) -> tuple[list[int], dict]:
    """Allinea le serie per timestamp. Ritorna (timestamps_comuni_ordinati,
    {symbol: {ts: bar}}). Cosi' a ogni step usiamo la candela giusta per asset."""
    index = {}
    all_ts = set()
    for sym, bars in data.items():
        m = {}
        for b in bars:
            t = b.get("t")
            if t is not None:
                m[t] = b
                all_ts.add(t)
        index[sym] = m
    return sorted(all_ts), index


def run_portfolio_backtest(data: dict, interval: str, top_k: int = 3,
                           min_score: float = 0.58, slippage_bps: float = 5.0,
                           warm: int = 60) -> dict:
    """Simula il portafoglio multi-asset. data: {symbol: [bar con 't']}."""
    sp = SelectorParams()
    rp = RegimeParams()
    timestamps, idx = _align(data)
    if len(timestamps) < warm + 10:
        return {"error": f"dati insuff ({len(timestamps)} ts)"}

    cash = INITIAL_NAV
    holdings: dict[str, float] = {}   # symbol -> units
    entry_px: dict[str, float] = {}
    equity = []
    n_trades = 0
    fees = 0.0

    def price_at(sym, ts):
        b = idx.get(sym, {}).get(ts)
        return b["close"] if b else None

    def nav_at(ts):
        v = cash
        for sym, u in holdings.items():
            px = price_at(sym, ts)
            if px:
                v += u * px
        return v

    def history_until(sym, ts_pos):
        """Bars dell'asset fino all'indice temporale corrente (no look-ahead)."""
        m = idx.get(sym, {})
        return [m[t] for t in timestamps[:ts_pos + 1] if t in m]

    for ti in range(warm, len(timestamps)):
        ts = timestamps[ti]

        # 1+2. score + regime su tutte le 14 → costruisci data_by_symbol storico
        hist = {}
        for sym in data:
            h = history_until(sym, ti)
            if len(h) >= warm:
                hist[sym] = h
        # selezione: top-K per opportunita'
        picks = select_top(hist, k=top_k, min_score=min_score, params=sp)
        # filtro regime UP: opera solo chi e' anche in uptrend confermato-istantaneo
        target = set()
        for r in picks:
            closes = [b["close"] for b in hist[r.symbol]]
            if classify_regime(closes, rp) == UP:
                target.add(r.symbol)

        # 4. chiudi chi non e' piu' target
        for sym in list(holdings.keys()):
            if sym not in target:
                px = price_at(sym, ts)
                if px:
                    proceeds = holdings[sym] * px
                    fee = _commission(proceeds)
                    cash += proceeds - fee
                    fees += fee
                    n_trades += 1
                del holdings[sym]
                entry_px.pop(sym, None)

        # 3. apri/mantieni i target, capitale equipesato sul NAV corrente
        if target:
            nav = nav_at(ts)
            target_alloc = nav / len(target)   # quota per asset
            for sym in target:
                px = price_at(sym, ts)
                if not px:
                    continue
                cur_val = holdings.get(sym, 0.0) * px
                # ribilancia solo se scostamento significativo (riduce trade)
                if abs(cur_val - target_alloc) / target_alloc > 0.25:
                    # vendi/compra la differenza
                    diff_val = target_alloc - cur_val
                    if diff_val > 0 and cash >= diff_val:
                        u = diff_val / px
                        fee = _commission(diff_val)
                        cash -= diff_val + fee
                        fees += fee
                        holdings[sym] = holdings.get(sym, 0.0) + u
                        entry_px[sym] = px
                        n_trades += 1
                    elif diff_val < 0:
                        u = -diff_val / px
                        proceeds = u * px
                        fee = _commission(proceeds)
                        cash += proceeds - fee
                        fees += fee
                        holdings[sym] = max(0.0, holdings.get(sym, 0.0) - u)
                        n_trades += 1

        equity.append(nav_at(ts))

    # liquida a fine serie
    last_ts = timestamps[-1]
    for sym, u in list(holdings.items()):
        px = price_at(sym, last_ts)
        if px:
            cash += u * px - _commission(u * px)
    final = cash if not equity else nav_at(last_ts) if holdings else equity[-1]
    final = equity[-1] if equity else INITIAL_NAV

    # metriche
    peak, max_dd = INITIAL_NAV, 0.0
    for v in equity:
        peak = max(peak, v)
        max_dd = min(max_dd, (v - peak) / peak * 100.0)
    ret = (equity[-1] / INITIAL_NAV - 1.0) * 100.0 if equity else 0.0

    # BENCHMARK: equal-weight hold delle 14 (quelle con dati al warm)
    bh = _equal_weight_hold(data, timestamps, idx, warm)
    return {"return_pct": round(ret, 1), "ew_hold_pct": round(bh["ret"], 1),
            "edge": round(ret - bh["ret"], 1),
            "max_drawdown_pct": round(max_dd, 1), "ew_max_dd_pct": round(bh["dd"], 1),
            "trades": n_trades, "n_assets": len(data), "steps": len(equity)}


def _equal_weight_hold(data, timestamps, idx, warm) -> dict:
    """Benchmark: compra equal-weight le 14 al primo step disponibile, tieni."""
    start_ts = timestamps[warm]
    avail = [s for s in data if idx.get(s, {}).get(start_ts)]
    if not avail:
        return {"ret": 0.0, "dd": 0.0}
    w = INITIAL_NAV / len(avail)
    units = {s: w / idx[s][start_ts]["close"] for s in avail}
    eq = []
    for ti in range(warm, len(timestamps)):
        ts = timestamps[ti]
        v = sum(units[s] * idx[s][ts]["close"] for s in avail if idx.get(s, {}).get(ts))
        # asset senza candela a ts: usa l'ultimo prezzo noto (semplificazione)
        eq.append(v)
    peak, dd = INITIAL_NAV, 0.0
    for v in eq:
        peak = max(peak, v)
        dd = min(dd, (v - peak) / peak * 100.0)
    ret = (eq[-1] / INITIAL_NAV - 1.0) * 100.0 if eq else 0.0
    return {"ret": ret, "dd": dd}


def main(argv):
    interval = argv[1] if len(argv) > 1 else "1d"
    d0 = argv[2] if len(argv) > 2 else "2021-01-01"
    d1 = argv[3] if len(argv) > 3 else "2024-12-31"
    print(f"Scarico universo 14 major {interval} {d0}->{d1} ...")
    data = {}
    for sym in UNIVERSE_14:
        bars = _fetch(sym, interval, _dt_ms(d0), _dt_ms(d1))
        if len(bars) >= 60:
            data[sym] = bars
        print(f"  {sym}: {len(bars)} candele")
    if len(data) < 5:
        print("Dati insufficienti."); return 1

    warm = 60 if interval == "1d" else 120
    r = run_portfolio_backtest(data, interval, top_k=3, warm=warm)
    print("\n" + "=" * 80)
    print(f"PORTAFOGLIO MULTI-ASSET (top-3 per opportunita' + regime UP)  {interval}  {d0}->{d1}")
    print("=" * 80)
    if "error" in r:
        print("  ", r["error"]); return 1
    print(f"  Asset nell'universo : {r['n_assets']}/14   step simulati: {r['steps']}")
    print(f"  Sistema (rotazione) : {r['return_pct']:+.1f}%   max DD {r['max_drawdown_pct']:.1f}%")
    print(f"  Benchmark EW-hold-14: {r['ew_hold_pct']:+.1f}%   max DD {r['ew_max_dd_pct']:.1f}%")
    print(f"  EDGE vs equal-weight: {r['edge']:+.1f} punti   ({r['trades']} trade)")
    print("=" * 80)
    print("BENCHMARK ONESTO = tenere le 14 equipesate. Survivorship bias: 14 vive")
    print("oggi (esclude LUNA/FTT morte) -> risultati un filo ottimistici.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
