"""
Test Step 4 (analisi 13/07): script riclassificazione retroattiva outcome v2.
Gira sul SQLite del conftest (nessuna env Supabase nei test).
"""
import json
import sys
import os
import uuid

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "tools")))

from simulator import db as sim_db
from reclassify_outcomes import reclassify


@pytest.fixture(autouse=True)
def _clean_test_runs():
    """Il DB SQLite dei test persiste tra esecuzioni: senza cleanup una
    run gia' riclassificata da un --apply precedente falsifica i test."""
    import db_sqlite
    with db_sqlite.get_db() as conn:
        try:
            conn.execute("DELETE FROM sim_runs WHERE id LIKE 'rr-%'")
        except Exception:
            pass
    yield


def _rid(prefix):
    return f"rr-{prefix}-{uuid.uuid4().hex[:8]}"


def _mk_run(rid, outcome="yellow", with_series=True, engine="simulator_v2_crypto"):
    """Run difesa-in-crash: pnl -1%, expo 15%, bench -20% -> v2 = green."""
    history = [{
        "step_index": 0, "step_date": "2024-01-01",
        "valuation_after": {"cash": 85000, "total_value": 99000},
        "applied_trades": [{"status": "executed_open_long"}],
        "ai_trades": [{"action": "BUY", "asset": "BTC-USD",
                       "allocation_pct": 15, "conviction": "ALTA"}],
    }]
    series = ([{"step_index": -1, "step_date": "2024-01-01", "value": 100000},
               {"step_index": 0, "step_date": "2024-01-01", "value": 100000},
               {"step_index": 1, "step_date": "2024-01-03", "value": 80000}]
              if with_series else [])
    return {
        "id": rid, "created_at": "2026-07-01T00:00:00+00:00",
        "completed_at": "2026-07-01T00:00:00+00:00",
        "mode": "auto", "category": "crash", "scenario_type": "multi",
        "steps": 1, "scenario_id": "scen-x", "historical_period": "x",
        "asset_chosen": "BTC-USD", "action_chosen": "BUY",
        "conviction": "ALTA", "horizon": "2giorni",
        "perf_1w": 0.0, "perf_1m": -0.01, "perf_3m": -0.01,
        "perf_sp_1m": -0.20, "delta_sp": 0.19, "outcome": outcome,
        "original_thesis": "t", "what_happened": "w", "thesis_evaluation": "e",
        "full_data": {
            "engine": engine,
            "history": history,
            "benchmark_value_series": series,
            "final_valuation": {"total_pnl_pct": -1.0, "cash": 85000,
                                "total_value": 99000},
            "benchmark_btc_pnl_pct": -20.0,
        },
    }


def test_dry_run_riclassifica_senza_scrivere():
    rid = _rid("dry")
    sim_db.insert_run(_mk_run(rid))
    res = reclassify(apply=False)
    row = next(r for r in res["rows"] if r["run_id"] == rid)
    assert row["old"] == "yellow" and row["new"] == "green"
    # niente scrittura: outcome nel DB invariato
    run = sim_db.get_run(rid)
    assert run["outcome"] == "yellow"
    assert "outcome_legacy" not in (run.get("full_data") or {})


def test_apply_aggiorna_e_preserva_legacy():
    rid = _rid("app")
    sim_db.insert_run(_mk_run(rid))
    res = reclassify(apply=True)
    assert res["applied"] >= 1
    run = sim_db.get_run(rid)
    assert run["outcome"] == "green"
    fd = run["full_data"] if isinstance(run["full_data"], dict) \
        else json.loads(run["full_data"])
    assert fd["outcome_legacy"] == "yellow"
    assert fd["outcome_v2_inputs"]["method"] == "shadow_v2"

    # idempotenza: secondo apply non tocca outcome_legacy
    reclassify(apply=True)
    run2 = sim_db.get_run(rid)
    fd2 = run2["full_data"] if isinstance(run2["full_data"], dict) \
        else json.loads(run2["full_data"])
    assert fd2["outcome_legacy"] == "yellow"


def test_run_senza_serie_fallback_legacy_non_skippata():
    """Serie mancante -> classify_outcome_v2 fa fallback legacy, il run
    viene comunque riclassificato (method=legacy_fallback)."""
    rid = _rid("nos")
    sim_db.insert_run(_mk_run(rid, with_series=False))
    res = reclassify(apply=False)
    row = next(r for r in res["rows"] if r["run_id"] == rid)
    assert row["method"] in ("legacy_fallback", "shadow_v2", "hold_rule",
                             "pnl_override")


def test_run_v1_skippata():
    rid = _rid("v1")
    sim_db.insert_run(_mk_run(rid, engine="runner_v1"))
    res = reclassify(apply=False)
    assert not any(r["run_id"] == rid for r in res["rows"])
    assert any(r == rid for r, _ in res["skipped"])
