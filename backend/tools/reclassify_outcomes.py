# -*- coding: utf-8 -*-
"""
Riclassificazione retroattiva degli outcome sim_runs con i criteri v2
(shadow benchmark a pari esposizione — Step 4 analisi 13/07).

COSA FA
  Rilegge i run v2 salvati (engine simulator_v2 / simulator_v2_crypto),
  ricalcola l'outcome con classify_outcome_v2 (STESSO codice del branch,
  nessuna divergenza col deploy) e stampa la matrice vecchio->nuovo.
  In modalita' --apply scrive il nuovo outcome e salva quello vecchio in
  full_data.outcome_legacy (mai sovrascritto se gia' presente: rollback
  sempre possibile).

ENV
  SUPABASE_URL / SUPABASE_KEY  -> lavora sul DB di produzione.
  Nessuna env                  -> lavora sul SQLite locale (dev/test).

USO (da repo root)
  python backend/tools/reclassify_outcomes.py              # DRY-RUN (default)
  python backend/tools/reclassify_outcomes.py --apply      # scrive davvero
  python backend/tools/reclassify_outcomes.py --limit 20 --offset 0

Idempotente: ri-eseguirlo ricalcola gli stessi valori; outcome_legacy
conserva SEMPRE l'outcome originale pre-v2.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

# Bootstrap: lo script vive in backend/tools/, i moduli in backend/
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

V2_ENGINES = {"simulator_v2", "simulator_v2_crypto"}


def _bench_pct(full_data: dict) -> float | None:
    """Benchmark % totale del run: BTC per crypto, SPY per equity."""
    for key in ("benchmark_btc_pnl_pct", "benchmark_spy_pnl_pct"):
        v = full_data.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


def reclassify(apply: bool = False, limit: int | None = None,
               offset: int = 0) -> dict:
    """
    Core riusabile (testato in test_reclassify_outcomes.py).
    Ritorna {"rows": [...], "matrix": Counter, "skipped": [...], "applied": n}.
    """
    from simulator import db as sim_db
    from simulator.v2_engine import classify_outcome_v2

    supabase_mode = bool(os.environ.get("SUPABASE_URL")
                         and os.environ.get("SUPABASE_KEY"))

    light = sim_db.list_runs(limit=500) or []
    ids = [r.get("id") for r in light if r.get("id")]
    ids = ids[offset: offset + limit if limit else None]

    rows, skipped, applied = [], [], 0
    matrix: Counter = Counter()

    for rid in ids:
        run = sim_db.get_run(rid)
        if not run:
            skipped.append((rid, "run non trovato"))
            continue
        fd = run.get("full_data") or {}
        if not isinstance(fd, dict):
            skipped.append((rid, "full_data non parsabile"))
            continue
        engine = fd.get("engine")
        if engine not in V2_ENGINES:
            skipped.append((rid, f"engine {engine or 'v1'} non v2"))
            continue
        # Prudenza: in modalita' Supabase applica solo righe lette dal
        # tier primario (get_run ha fallback multipli non aggiornabili).
        if apply and supabase_mode and run.get("_source") != "supabase":
            skipped.append((rid, f"sorgente {run.get('_source')} non primaria"))
            continue

        history = fd.get("history") or []
        series = fd.get("benchmark_value_series") or []
        valuation = fd.get("final_valuation") or {}
        if not history or not valuation:
            skipped.append((rid, "history/final_valuation mancanti"))
            continue

        old = run.get("outcome") or "?"
        v2 = classify_outcome_v2(valuation, history, series, _bench_pct(fd))
        new = v2["outcome"]
        matrix[f"{old}->{new}"] += 1
        rows.append({"run_id": rid, "old": old, "new": new,
                     "delta_eq": v2.get("delta_eq"), "method": v2["method"]})

        if apply:
            fd.setdefault("outcome_legacy", old)   # mai sovrascritto
            fd["outcome_v2_inputs"] = v2
            if sim_db.update_run_outcome(rid, new, fd):
                applied += 1
            else:
                skipped.append((rid, "update fallito"))

    return {"rows": rows, "matrix": matrix, "skipped": skipped,
            "applied": applied}


def main() -> int:
    ap = argparse.ArgumentParser(description="Riclassifica outcome sim_runs (v2)")
    ap.add_argument("--apply", action="store_true",
                    help="Scrive davvero (default: dry-run)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--offset", type=int, default=0)
    args = ap.parse_args()

    res = reclassify(apply=args.apply, limit=args.limit, offset=args.offset)

    print(f"{'MODALITA APPLY' if args.apply else 'DRY-RUN (nessuna scrittura)'}")
    print(f"{'run_id':38} {'old':7} {'new':7} {'delta_eq':>9} method")
    for r in res["rows"]:
        de = f"{r['delta_eq']:+.2f}" if r["delta_eq"] is not None else "-"
        print(f"{r['run_id']:38} {r['old']:7} {r['new']:7} {de:>9} {r['method']}")

    print("\nMatrice old->new:")
    for k, v in sorted(res["matrix"].items()):
        print(f"  {k}: {v}")
    new_counts = Counter(r["new"] for r in res["rows"])
    old_counts = Counter(r["old"] for r in res["rows"])
    print(f"\nPrima : {dict(old_counts)}")
    print(f"Dopo  : {dict(new_counts)}")

    if res["skipped"]:
        print(f"\nSaltati ({len(res['skipped'])}):")
        for rid, why in res["skipped"]:
            print(f"  {rid}: {why}")
    if args.apply:
        print(f"\nAggiornati: {res['applied']}/{len(res['rows'])}")
    else:
        print("\nNessuna scrittura. Rilancia con --apply per applicare.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
