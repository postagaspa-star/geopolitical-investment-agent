"""
diversity.py — Metrica di DIVERSITÀ DELLA COPERTURA (Step 0 del piano universo).

Misura quanto è ampio l'universo che la pipeline analizza DAVVERO, leggendo i
log storici (tabella agent_logs) e classificando i ticker via universe.py.

NON tocca nessuna logica di trading: legge soltanto. Serve a:
  1. fissare la BASELINE prima di allargare l'universo (Step 2+);
  2. verificare poi se l'allargamento ha funzionato (eN su, off-mega su,
     satellite su, a parità o meglio di rischio).

Sorgenti (da agent_logs.content, che è JSON):
  - phase TECH_WORKER              → analyses_summary[].ticker   (analizzati dal Technical)
  - phase DECISION_PHASE1          → asset_candidates[]          (candidati dal Decision)
  - phase DECISION_CRYPTO_COMPLETE → asset_candidates[]
  - phase DECISION_TRADE /
          DECISION_CRYPTO_TRADE    → ticker                      (effettivamente operati)

NB: TECH_WORKER logga solo i primi ~6 ticker in analyses_summary (cap a monte
in technical.py); per l'identità dei nomi la fonte più completa è
DECISION_PHASE1.asset_candidates. Le due fonti vengono unite e deduplicate.

Metriche prodotte:
  - breadth          : # ticker distinti analizzati
  - effective_n      : 1 / HHI sulle quote di attenzione (numero "effettivo" di nomi)
  - hhi              : Herfindahl (0..1, alto = attenzione concentrata su pochi nomi)
  - bucket_dist      : distribuzione dell'attenzione per bucket tematico
  - bucket_entropy   : entropia (bit) della distribuzione per bucket
  - satellite_share  : quota di attenzione su nomi 'satellite' (0 in Step 1)
  - off_mega_share   : quota di attenzione FUORI dai mega-cap
  - crypto_share / equity_share
  - novelty_rate     : quota di nomi nuovi nella metà recente vs metà precedente

TODO Step 2+: "inefficiency proxy" (ADV/market-cap mediano dei nomi analizzati)
richiede il layer dati (data_fetchers) e quindi non è incluso in questa baseline
a costo/rischio zero.

Uso CLI (dalla cartella backend/):
  python diversity.py                 # report leggibile sugli ultimi N log
  python diversity.py --limit 5000    # finestra più ampia
  python diversity.py --json          # output JSON
"""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

import universe


_ANALYZED_PHASES = {"TECH_WORKER", "DECISION_PHASE1", "DECISION_CRYPTO_COMPLETE"}
_TRADE_PHASES = {"DECISION_TRADE", "DECISION_CRYPTO_TRADE"}


def _safe_json(content: Any) -> dict:
    """Parsa il campo content (stringa JSON o già dict) in modo difensivo."""
    if isinstance(content, dict):
        return content
    if not content:
        return {}
    try:
        parsed = json.loads(content)
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def tickers_from_log(phase: str, content_json: dict) -> Dict[str, List[str]]:
    """Estrae {'analyzed': [...], 'traded': [...]} da una singola riga di log."""
    analyzed: List[str] = []
    traded: List[str] = []
    if phase == "TECH_WORKER":
        for a in content_json.get("analyses_summary") or []:
            if isinstance(a, dict) and a.get("ticker"):
                analyzed.append(str(a["ticker"]).upper().strip())
    elif phase in ("DECISION_PHASE1", "DECISION_CRYPTO_COMPLETE"):
        for t in content_json.get("asset_candidates") or []:
            if t:
                analyzed.append(str(t).upper().strip())
    elif phase in _TRADE_PHASES:
        t = content_json.get("ticker")
        if t:
            traded.append(str(t).upper().strip())
    return {"analyzed": analyzed, "traded": traded}


def aggregate_logs(rows: List[dict]) -> Dict[str, Any]:
    """
    rows: lista di dict {run_id, phase, content, timestamp} come da
    database.get_agent_logs(). Ritorna le strutture aggregate grezze.
    """
    # timestamp ISO ordina cronologicamente come stringa
    rows = sorted(rows, key=lambda r: str(r.get("timestamp") or ""))
    attention: Counter = Counter()           # ticker -> # log in cui è analizzato
    traded_counter: Counter = Counter()
    per_run_analyzed: Dict[str, set] = defaultdict(set)
    timeline: List[Tuple[str, str]] = []     # (timestamp, ticker) per novelty

    for r in rows:
        phase = r.get("phase") or ""
        if phase not in _ANALYZED_PHASES and phase not in _TRADE_PHASES:
            continue
        cj = _safe_json(r.get("content"))
        ex = tickers_from_log(phase, cj)
        run_id = r.get("run_id") or ""
        ts = str(r.get("timestamp") or "")
        for t in ex["analyzed"]:
            if not t:
                continue
            attention[t] += 1
            per_run_analyzed[run_id].add(t)
            timeline.append((ts, t))
        for t in ex["traded"]:
            if t:
                traded_counter[t] += 1

    return {
        "attention": attention,
        "traded": traded_counter,
        "per_run_analyzed": per_run_analyzed,
        "timeline": timeline,
        "n_rows": len(rows),
        "ts_min": str(rows[0].get("timestamp")) if rows else None,
        "ts_max": str(rows[-1].get("timestamp")) if rows else None,
    }


def _novelty_rate(timeline: List[Tuple[str, str]]) -> Optional[float]:
    """Quota di ticker nella metà recente NON visti nella metà precedente."""
    if len(timeline) < 4:
        return None
    ordered = sorted(timeline, key=lambda x: x[0])
    mid = len(ordered) // 2
    prev = {t for _, t in ordered[:mid]}
    recent = {t for _, t in ordered[mid:]}
    if not recent:
        return None
    return round(len(recent - prev) / len(recent), 4)


def compute_diversity(agg: Dict[str, Any]) -> Dict[str, Any]:
    """Calcola le metriche di diversità dalle strutture di aggregate_logs()."""
    attention: Counter = agg["attention"]
    total = sum(attention.values())
    breadth = len(attention)

    if total == 0:
        return {
            "breadth": 0, "effective_n": 0.0, "hhi": 0.0, "total_attention": 0,
            "bucket_dist": {}, "bucket_entropy": 0.0, "satellite_share": 0.0,
            "off_mega_share": 0.0, "crypto_share": 0.0, "equity_share": 0.0,
            "novelty_rate": None, "top_attention": [],
        }

    hhi = sum((c / total) ** 2 for c in attention.values())
    effective_n = 1.0 / hhi if hhi > 0 else 0.0

    bucket_w: Counter = Counter()
    sat_w = offmega_w = crypto_w = equity_w = 0
    for t, c in attention.items():
        info = universe.classify_ticker(t)
        bucket_w[info["bucket"]] += c
        if info["tier"] == "satellite":
            sat_w += c
        if not info["is_mega"]:
            offmega_w += c
        if info["asset_class"] == "crypto":
            crypto_w += c
        else:
            equity_w += c

    bucket_entropy = 0.0
    for c in bucket_w.values():
        p = c / total
        if p > 0:
            bucket_entropy -= p * math.log2(p)

    return {
        "breadth": breadth,
        "effective_n": round(effective_n, 2),
        "hhi": round(hhi, 4),
        "total_attention": total,
        "bucket_dist": dict(bucket_w.most_common()),
        "bucket_entropy": round(bucket_entropy, 3),
        "satellite_share": round(sat_w / total, 4),
        "off_mega_share": round(offmega_w / total, 4),
        "crypto_share": round(crypto_w / total, 4),
        "equity_share": round(equity_w / total, 4),
        "novelty_rate": _novelty_rate(agg["timeline"]),
        "top_attention": attention.most_common(15),
    }


def collect_and_compute(limit: int = 3000) -> Dict[str, Any]:
    """Legge gli ultimi `limit` agent_logs dal DB e calcola le metriche."""
    import database  # lazy: non toccare il DB all'import del modulo
    rows = database.get_agent_logs(limit=limit) or []
    agg = aggregate_logs(rows)
    metrics = compute_diversity(agg)
    metrics["_window"] = {
        "n_rows_scanned": agg["n_rows"],
        "ts_min": agg["ts_min"],
        "ts_max": agg["ts_max"],
    }
    return metrics


def _pct(x: Optional[float]) -> str:
    return "n/d" if x is None else f"{x:.0%}"


def format_report(m: Dict[str, Any]) -> str:
    w = m.get("_window", {})
    L = []
    L.append("═" * 66)
    L.append("  GEOINVEST — DIVERSITÀ DI COPERTURA (baseline universo)")
    L.append("═" * 66)
    L.append(f"  Finestra : {w.get('ts_min')} → {w.get('ts_max')}  ({w.get('n_rows_scanned')} righe log)")
    L.append(f"  Analisi totali (attenzione): {m['total_attention']}")
    L.append("")
    L.append(f"  Breadth (nomi distinti)    : {m['breadth']}")
    L.append(f"  Effective-N (1/HHI)        : {m['effective_n']}   <- numero 'effettivo' di nomi")
    L.append(f"  HHI (concentrazione)       : {m['hhi']}   (alto = pochi nomi dominano)")
    L.append(f"  Entropia bucket (bit)      : {m['bucket_entropy']}")
    L.append(f"  Quota fuori dai mega-cap   : {_pct(m['off_mega_share'])}")
    L.append(f"  Quota satellite            : {_pct(m['satellite_share'])}   (0% atteso in Step 1)")
    L.append(f"  Crypto / Equity            : {_pct(m['crypto_share'])} / {_pct(m['equity_share'])}")
    L.append(f"  Novelty (recente vs prima) : {_pct(m['novelty_rate'])}")
    L.append("")
    L.append("  Distribuzione attenzione per bucket tematico:")
    total = m["total_attention"] or 1
    for b, c in m["bucket_dist"].items():
        L.append(f"    {b:<22} {c:>5}   {c / total:.0%}")
    L.append("")
    L.append("  Top 15 nomi per attenzione:")
    for t, c in m["top_attention"]:
        info = universe.classify_ticker(t)
        L.append(f"    {t:<10} {c:>4}   [{info['bucket']}]")
    L.append("═" * 66)
    return "\n".join(L)


def main(argv: Optional[List[str]] = None) -> int:
    import sys
    import argparse
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # console Windows (cp1252) non digerisce ═/←
    except Exception:
        pass
    p = argparse.ArgumentParser(description="GeoInvest — diversità di copertura (baseline)")
    p.add_argument("--limit", type=int, default=3000, help="quanti agent_logs leggere (default 3000)")
    p.add_argument("--json", action="store_true", help="output JSON invece del report leggibile")
    args = p.parse_args(argv)
    m = collect_and_compute(limit=args.limit)
    if args.json:
        print(json.dumps(m, ensure_ascii=False, indent=2, default=str))
    else:
        print(format_report(m))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
