"""
Stress test suite del Crypto Scalper — >=15 run su dati REALI Binance.

Due famiglie (richiesta Andrea: "entrambi"):
  (A) WALK-FORWARD: scarica una serie 5m lunga e la divide in N segmenti
      consecutivi; ogni segmento e' un test "fuori campione" indipendente.
      Misura se l'edge regge su finestre diverse o e' fortuna di una finestra.
  (B) EVENTI ESTREMI REALI: finestre storiche di crash/pump documentati
      (Luna, FTX, ETF spot, China-ban, depeg USDC, halving 2024, ...).
      Ogni evento e' un range temporale fisso scaricato per timestamp.

Motore testato AS-IS (nessuna miglioria applicata): e' il BASELINE onesto
chiesto da Andrea ("testo com'e' ora, poi decidiamo").

Onesta': simulazione al close, commissioni 10bps/lato, NIENTE slippage/book
depth → i numeri reali sarebbero un po' peggiori. Serve a mappare DOVE si
rompe il motore, non a promettere rendimenti.

Uso:  python -m simulator.stress_test_scalper           (full suite, ~lento)
      python -m simulator.stress_test_scalper events    (solo eventi)
      python -m simulator.stress_test_scalper wf         (solo walk-forward)
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

from agents.crypto_scalper import ScalperParams
from simulator.backtest_scalper import run_backtest, _INTERVAL_MS

BINANCE = "https://api.binance.com/api/v3/klines"


def _fetch(symbol: str, interval: str, start_ms: int, end_ms: int) -> list[dict]:
    out: list[dict] = []
    cur = start_ms
    span = _INTERVAL_MS.get(interval, 300_000)
    guard = 0
    while cur < end_ms and guard < 60:
        guard += 1
        params = urllib.parse.urlencode({"symbol": symbol, "interval": interval,
                                         "startTime": cur, "endTime": end_ms,
                                         "limit": 1000})
        req = urllib.request.Request(f"{BINANCE}?{params}",
                                     headers={"User-Agent": "geoinvest-stress"})
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                arr = json.loads(resp.read().decode())
        except Exception as e:
            print(f"    fetch error: {str(e)[:80]}")
            break
        if not arr:
            break
        for k in arr:
            out.append({"t": k[0], "open": float(k[1]), "high": float(k[2]),
                        "low": float(k[3]), "close": float(k[4]),
                        "volume": float(k[5])})
        if arr[-1][0] <= cur or len(arr) < 1000:
            break
        cur = arr[-1][0] + span
        time.sleep(0.15)
    return out


def _dt_ms(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def _classify(bh: float) -> str:
    if bh >= 5:
        return "UPTREND"
    if bh <= -5:
        return "DOWNTREND"
    return "CHOP"


def _row(label, symbol, interval, bars):
    if not bars or len(bars) < 120:
        return {"label": label, "ok": False, "reason": f"dati insuff ({len(bars)})"}
    res = run_backtest(bars, ScalperParams())
    res.update({"label": label, "ok": True, "symbol": symbol, "interval": interval,
                "regime": _classify(res["buy_hold_pct"]),
                "edge": round(res["return_pct"] - res["buy_hold_pct"], 2)})
    return res


# ─── (B) Eventi estremi storici reali ────────────────────────────────────────
EVENTS = [
    ("Luna collapse",    "BTCUSDT", "15m", "2022-05-08", "2022-05-16"),
    ("Luna contagion",   "ETHUSDT", "15m", "2022-05-08", "2022-05-16"),
    ("FTX collapse",     "BTCUSDT", "15m", "2022-11-06", "2022-11-14"),
    ("FTX SOL dump",     "SOLUSDT", "15m", "2022-11-06", "2022-11-14"),
    ("USDC depeg",       "ETHUSDT", "15m", "2023-03-10", "2023-03-14"),
    ("BTC ETF approval", "BTCUSDT", "15m", "2024-01-08", "2024-01-13"),
    ("Halving 2024",     "BTCUSDT", "15m", "2024-04-18", "2024-04-23"),
    ("Aug-2024 crash",   "BTCUSDT", "15m", "2024-08-04", "2024-08-08"),
    ("Aug-2024 ETH",     "ETHUSDT", "15m", "2024-08-04", "2024-08-08"),
    ("Trump-win pump",   "BTCUSDT", "15m", "2024-11-05", "2024-11-12"),
]


def run_events() -> list[dict]:
    rows = []
    for label, sym, itv, d0, d1 in EVENTS:
        print(f"[evento] {label} ({sym} {itv} {d0}->{d1})")
        bars = _fetch(sym, itv, _dt_ms(d0), _dt_ms(d1))
        print(f"    {len(bars)} candele")
        rows.append(_row(label, sym, itv, bars))
        time.sleep(0.2)
    return rows


# ─── (A) Walk-forward ────────────────────────────────────────────────────────
def run_walkforward(symbol="BTCUSDT", interval="15m", days=120, segments=8) -> list[dict]:
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * 86_400_000
    print(f"[walk-forward] scarico {symbol} {interval} {days}g...")
    bars = _fetch(symbol, interval, start_ms, end_ms)
    print(f"    {len(bars)} candele totali")
    rows = []
    if len(bars) < segments * 200:
        return [{"label": "walk-forward", "ok": False, "reason": "dati insuff"}]
    seg_len = len(bars) // segments
    for i in range(segments):
        chunk = bars[i * seg_len:(i + 1) * seg_len]
        rows.append(_row(f"WF seg {i+1}/{segments}", symbol, interval, chunk))
    return rows


def _summary(rows: list[dict]):
    ok = [r for r in rows if r.get("ok")]
    if not ok:
        print("Nessun run valido."); return
    print("\n" + "=" * 88)
    print(f"{'scenario':<22}{'regime':<11}{'scalp%':>8}{'B&H%':>8}{'edge':>8}"
          f"{'trades':>8}{'win%':>7}{'maxDD%':>8}")
    print("-" * 88)
    for r in ok:
        print(f"{r['label'][:21]:<22}{r['regime']:<11}{r['return_pct']:>8.2f}"
              f"{r['buy_hold_pct']:>8.2f}{r['edge']:>8.2f}{r['trades']:>8}"
              f"{r['win_rate_pct']:>7.1f}{r['max_drawdown_pct']:>8.2f}")
    print("-" * 88)
    n = len(ok)
    avg_edge = sum(r["edge"] for r in ok) / n
    profitable = sum(1 for r in ok if r["return_pct"] > 0)
    beat_bh = sum(1 for r in ok if r["edge"] > 0)
    worst_dd = min(r["max_drawdown_pct"] for r in ok)
    avg_tr = sum(r["trades"] for r in ok) / n
    by_reg = {}
    for r in ok:
        by_reg.setdefault(r["regime"], []).append(r["edge"])
    print(f"RUN VALIDI: {n}")
    print(f"  edge medio vs B&H : {avg_edge:+.2f} punti")
    print(f"  run profittevoli  : {profitable}/{n}  ({profitable/n*100:.0f}%)")
    print(f"  battono B&H       : {beat_bh}/{n}  ({beat_bh/n*100:.0f}%)")
    print(f"  worst drawdown    : {worst_dd:.2f}%")
    print(f"  trade medi/run    : {avg_tr:.0f}")
    for reg, edges in sorted(by_reg.items()):
        print(f"  edge medio [{reg:<9}]: {sum(edges)/len(edges):+.2f}  (n={len(edges)})")
    print("=" * 88)
    print("NOTA: no slippage/book depth -> reale leggermente peggiore. Baseline as-is.")


def main(argv):
    mode = argv[1] if len(argv) > 1 else "all"
    rows = []
    if mode in ("all", "wf"):
        rows += run_walkforward()
    if mode in ("all", "events"):
        rows += run_events()
    print(f"\n>>> TOTALE RUN: {len([r for r in rows if r.get('ok')])} validi / {len(rows)} tentati")
    _summary(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
