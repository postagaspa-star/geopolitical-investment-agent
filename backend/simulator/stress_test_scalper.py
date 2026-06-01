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


def _make_params(profile: str, max_pos: float | None = None):
    if profile == "aggressive":
        p = ScalperParams.aggressive()
    elif profile == "hybrid":
        p = ScalperParams.hybrid()
    else:
        p = ScalperParams()
    if max_pos is not None:
        p.max_position_pct_nav = max_pos
    return p


def _row(label, symbol, interval, bars, profile="baseline", max_pos=None, slippage=0.0):
    if not bars or len(bars) < 120:
        return {"label": label, "ok": False, "reason": f"dati insuff ({len(bars)})"}
    res = run_backtest(bars, _make_params(profile, max_pos), slippage_bps=slippage)
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


# ─── Cache dei dati: scarica UNA volta, ri-usa per ogni profilo/sizing ───────
_BARS_CACHE: dict = {}


def _collect_datasets(now_ms: int) -> list[tuple]:
    """Scarica (con cache) tutti i dataset: 8 segmenti walk-forward + 10 eventi.
    Ritorna lista di (label, symbol, interval, bars). now_ms passato dal main
    per non usare il clock dentro la logica."""
    datasets = []
    # Walk-forward
    wf_sym, wf_itv, wf_days, segs = "BTCUSDT", "15m", 120, 8
    key = f"wf:{wf_sym}:{wf_itv}:{wf_days}"
    if key not in _BARS_CACHE:
        print(f"[walk-forward] scarico {wf_sym} {wf_itv} {wf_days}g...")
        _BARS_CACHE[key] = _fetch(wf_sym, wf_itv, now_ms - wf_days * 86_400_000, now_ms)
        print(f"    {len(_BARS_CACHE[key])} candele totali")
    bars = _BARS_CACHE[key]
    if len(bars) >= segs * 200:
        seg_len = len(bars) // segs
        for i in range(segs):
            datasets.append((f"WF seg {i+1}/{segs}", wf_sym, wf_itv,
                             bars[i * seg_len:(i + 1) * seg_len]))
    # Eventi estremi
    for label, sym, itv, d0, d1 in EVENTS:
        key = f"ev:{label}"
        if key not in _BARS_CACHE:
            print(f"[evento] {label} ({sym} {itv} {d0}->{d1})")
            _BARS_CACHE[key] = _fetch(sym, itv, _dt_ms(d0), _dt_ms(d1))
            print(f"    {len(_BARS_CACHE[key])} candele")
            time.sleep(0.15)
        datasets.append((label, sym, itv, _BARS_CACHE[key]))
    return datasets


def run_suite(datasets, profile="baseline", max_pos=None, slippage=0.0) -> list[dict]:
    return [_row(lbl, sym, itv, bars, profile=profile, max_pos=max_pos, slippage=slippage)
            for (lbl, sym, itv, bars) in datasets]


def _agg(rows: list[dict]) -> dict:
    ok = [r for r in rows if r.get("ok")]
    if not ok:
        return {}
    n = len(ok)
    by_reg = {}
    for r in ok:
        by_reg.setdefault(r["regime"], []).append(r["edge"])
    return {
        "n": n,
        "avg_edge": sum(r["edge"] for r in ok) / n,
        "profitable": sum(1 for r in ok if r["return_pct"] > 0),
        "beat_bh": sum(1 for r in ok if r["edge"] > 0),
        "worst_dd": min(r["max_drawdown_pct"] for r in ok),
        "avg_trades": sum(r["trades"] for r in ok) / n,
        "by_reg": {k: sum(v) / len(v) for k, v in by_reg.items()},
    }


def _summary(rows: list[dict], title: str):
    ok = [r for r in rows if r.get("ok")]
    if not ok:
        print(f"[{title}] nessun run valido."); return
    print("\n" + "=" * 90)
    print(f"{title}")
    print("=" * 90)
    print(f"{'scenario':<22}{'regime':<11}{'scalp%':>8}{'B&H%':>8}{'edge':>8}"
          f"{'trades':>8}{'win%':>7}{'maxDD%':>8}")
    print("-" * 90)
    for r in ok:
        print(f"{r['label'][:21]:<22}{r['regime']:<11}{r['return_pct']:>8.2f}"
              f"{r['buy_hold_pct']:>8.2f}{r['edge']:>8.2f}{r['trades']:>8}"
              f"{r['win_rate_pct']:>7.1f}{r['max_drawdown_pct']:>8.2f}")
    print("-" * 90)
    a = _agg(rows)
    print(f"  RUN={a['n']}  edge medio={a['avg_edge']:+.2f}  "
          f"profittevoli={a['profitable']}/{a['n']}  battono_B&H={a['beat_bh']}/{a['n']}  "
          f"worstDD={a['worst_dd']:.2f}%  trade/run={a['avg_trades']:.0f}")
    for reg, e in sorted(a["by_reg"].items()):
        print(f"    edge[{reg:<9}]={e:+.2f}")


def main(argv):
    # clock SOLO qui nel CLI (mai nella logica deterministica)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    datasets = _collect_datasets(now_ms)
    n_data = len([d for d in datasets if d[3] and len(d[3]) >= 120])
    print(f"\n>>> DATASET PRONTI: {n_data} (>=15 richiesti)")

    # 1) BASELINE (no slippage, per confronto col run precedente)
    base = run_suite(datasets, profile="baseline", slippage=0.0)
    _summary(base, "BASELINE (difensivo as-is, no slippage)")

    # 2) HYBRID: trend-following in uptrend + difensivo 12% in downtrend
    hyb = run_suite(datasets, profile="hybrid", slippage=3.0)
    _summary(hyb, "HYBRID (trend-following uptrend + difensivo 12% downtrend, slippage 3bps)")

    # 3) confronto sintetico finale: l'hybrid deve alzare l'edge UPTREND
    #    SENZA distruggere l'edge DOWNTREND ne' il drawdown.
    print("\n" + "#" * 92)
    print("CONFRONTO (edge medio / profittevoli / worstDD / edge UP / edge DOWN / edge CHOP)")
    print("#" * 92)
    def line(name, rows):
        a = _agg(rows)
        up = a["by_reg"].get("UPTREND", float("nan"))
        dn = a["by_reg"].get("DOWNTREND", float("nan"))
        ch = a["by_reg"].get("CHOP", float("nan"))
        print(f"  {name:<22} edge={a['avg_edge']:+6.2f}  prof={a['profitable']:>2}/{a['n']}  "
              f"worstDD={a['worst_dd']:>7.2f}%  UP={up:+6.2f}  DOWN={dn:+6.2f}  CHOP={ch:+6.2f}")
    line("baseline difensivo", base)
    line("HYBRID", hyb)
    print("#" * 92)
    print("OBIETTIVO: HYBRID con UP>0 (trend-following funziona) mantenendo DOWN alto.")
    print("NOTA: slippage 3bps incluso nell'hybrid. Sim al close, no book depth.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
