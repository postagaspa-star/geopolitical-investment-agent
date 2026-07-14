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

  Il core vive in simulator/reclassify.py ed e' esposto anche come
  endpoint server-side: POST /api/simulator/reclassify-v2?apply=true
  (utile quando le credenziali Supabase non sono disponibili in locale).

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

from simulator.reclassify import reclassify  # noqa: E402  (re-export per i test)


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
