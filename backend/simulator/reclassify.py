# -*- coding: utf-8 -*-
"""
Core della riclassificazione retroattiva degli outcome sim_runs con i
criteri v2 (Step 4 analisi 13/07).

Usato da DUE punti:
  - backend/tools/reclassify_outcomes.py (script locale, dry-run default)
  - POST /api/simulator/reclassify-v2 (endpoint server-side: le credenziali
    Supabase stanno gia' sul server, nessuna key da maneggiare in locale)

Idempotente: outcome_legacy conserva SEMPRE l'outcome originale pre-v2
(mai sovrascritto); ri-eseguire ricalcola gli stessi valori.
"""
from __future__ import annotations

import os
from collections import Counter

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
    Ritorna {"rows": [...], "matrix": Counter, "skipped": [...], "applied": n}.
    apply=False (default): solo report, nessuna scrittura.
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
