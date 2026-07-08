"""
Test dei bug-fix del Simulator (8 lug 2026, 2° round):
  - Bug 1: cap giornaliero run auto (runs_today robusto, contatore)
  - Bug 2: win rate per categoria include le categorie crypto
  - Bug 3: perf_1w reale (non più 0), flag auto coerente col run_mode
"""
import uuid

import pytest


# ─── Bug 1: cap giornaliero ─────────────────────────────────────────────


def test_auto_counter_bump_and_read():
    from simulator import db as sim_db
    # Azzera il contatore di oggi usando una chiave isolata via monkeypatch
    # del giorno non serve: bump poi read deve crescere.
    before = sim_db._auto_daily_counter()
    sim_db._bump_auto_daily_counter()
    sim_db._bump_auto_daily_counter()
    assert sim_db._auto_daily_counter() == before + 2


def test_runs_today_uses_counter_when_no_table(monkeypatch):
    from simulator import db as sim_db
    # Nessun client Supabase → runs_today('auto') deve tornare il contatore,
    # NON 0 (era il bug: cap mai applicato in fallback/SQLite).
    monkeypatch.setattr(sim_db, "_get_client", lambda: None)
    base = sim_db._auto_daily_counter()
    sim_db._bump_auto_daily_counter()
    assert sim_db.runs_today(mode="auto") == base + 1


def test_insert_run_auto_bumps_counter(monkeypatch):
    from simulator import db as sim_db
    # insert_run con mode='auto' deve incrementare il contatore giornaliero
    # (è ciò che permette il conteggio anche quando i run vanno nel fallback).
    monkeypatch.setattr(sim_db, "_get_client", lambda: None)
    monkeypatch.setattr(sim_db, "_put_in_local_cache", lambda *a, **k: None)
    monkeypatch.setattr(sim_db, "_run_fallback_save_run", lambda *a, **k: True)
    base = sim_db._auto_daily_counter()
    sim_db.insert_run({"id": f"t-{uuid.uuid4()}", "mode": "auto",
                        "category": "macro", "scenario_type": "multi",
                        "scenario_id": "x"})
    assert sim_db._auto_daily_counter() == base + 1


def test_insert_run_manual_does_not_bump(monkeypatch):
    from simulator import db as sim_db
    monkeypatch.setattr(sim_db, "_get_client", lambda: None)
    monkeypatch.setattr(sim_db, "_put_in_local_cache", lambda *a, **k: None)
    monkeypatch.setattr(sim_db, "_run_fallback_save_run", lambda *a, **k: True)
    base = sim_db._auto_daily_counter()
    sim_db.insert_run({"id": f"t-{uuid.uuid4()}", "mode": "simulator_v2",
                        "category": "macro", "scenario_type": "multi",
                        "scenario_id": "x"})
    assert sim_db._auto_daily_counter() == base   # invariato


# ─── Bug 2: win rate per categoria (crypto incluso) ─────────────────────


def test_all_categories_include_crypto():
    import main
    for c in ["bull_cycle", "crash", "regulatory_event", "sideways"]:
        assert c in main._SIM_ALL_CATEGORIES
    for c in ["normale", "geopolitico", "macro", "crash_rally"]:
        assert c in main._SIM_ALL_CATEGORIES


def test_categories_present_adds_unknown_extra():
    import main
    runs = [{"category": "macro"}, {"category": "bull_cycle"},
            {"category": "una_nuova_categoria"}]
    cats = main._sim_categories_present(runs)
    assert "bull_cycle" in cats
    assert "una_nuova_categoria" in cats
    # le canoniche restano in testa, nell'ordine
    assert cats[:8] == main._SIM_ALL_CATEGORIES


# ─── Bug 3: perf_1w reale + flag auto ───────────────────────────────────


def test_pnl_after_first_step():
    from simulator.v2_engine import _pnl_after_first_step
    fr = {"portfolio_value_series": [
        {"step_index": -1, "value": 100000.0},
        {"step_index": 0, "value": 101000.0},   # +1% dopo il 1° step
        {"step_index": 1, "value": 103000.0},
    ]}
    assert _pnl_after_first_step(fr) == pytest.approx(0.01)


def test_pnl_after_first_step_empty():
    from simulator.v2_engine import _pnl_after_first_step
    assert _pnl_after_first_step({}) == 0.0
    assert _pnl_after_first_step({"portfolio_value_series": [{"value": 100}]}) == 0.0
