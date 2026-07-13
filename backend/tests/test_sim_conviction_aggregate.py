"""
Test Step 1 (analisi 13/07): conviction reale aggregata nei sim_runs.

Prima del fix, _persist_run e _persist_crypto_run scrivevano "MEDIA"
hardcoded: il 100% delle run risultava MEDIA e la calibrazione
conviction/esito era impossibile.
"""
from simulator.v2_engine import aggregate_run_conviction
from simulator import v2_engine, v2_crypto_engine


def _step(trades):
    return {"ai_trades": trades}


def _trade(conviction, alloc=10.0, asset="BTC-USD", action="BUY"):
    return {"action": action, "asset": asset,
            "allocation_pct": alloc, "conviction": conviction}


# ── aggregate_run_conviction: funzione pura ─────────────────────────────

def test_primo_step_con_trade_vince_max_allocazione():
    history = [
        _step([]),  # step senza trade: saltato
        _step([_trade("ALTA", alloc=20), _trade("MEDIA", alloc=10)]),
        _step([_trade("BASSA", alloc=30)]),  # step successivi ignorati
    ]
    assert aggregate_run_conviction(history) == "ALTA"


def test_max_allocazione_non_ordine():
    history = [_step([_trade("MEDIA", alloc=5), _trade("BASSA", alloc=25)])]
    assert aggregate_run_conviction(history) == "BASSA"


def test_zero_trade_ritorna_none():
    assert aggregate_run_conviction([]) is None
    assert aggregate_run_conviction([_step([]), _step([])]) is None
    assert aggregate_run_conviction(None) is None


def test_stringhe_invalide_non_crashano_e_usano_fallback():
    # primo step con trade ma conviction spazzatura -> fallback moda pesata
    history = [
        _step([_trade("FORTISSIMA", alloc=50)]),
        _step([_trade("media", alloc=10), _trade("MEDIA", alloc=10)]),
        _step([_trade("ALTA", alloc=5)]),
    ]
    # pesi: MEDIA 20 vs ALTA 5 -> MEDIA
    assert aggregate_run_conviction(history) == "MEDIA"


def test_case_insensitive():
    history = [_step([_trade("alta", alloc=15)])]
    assert aggregate_run_conviction(history) == "ALTA"


def test_allocazione_non_numerica_non_crasha():
    history = [_step([_trade("BASSA", alloc="boh")])]
    assert aggregate_run_conviction(history) == "BASSA"


def test_solo_conviction_invalide_ritorna_none():
    history = [_step([_trade("X", alloc=10)]), _step([_trade(None, alloc=10)])]
    assert aggregate_run_conviction(history) is None


# ── persist: il record salvato porta la conviction vera ─────────────────

def _fake_final_result():
    return {
        "final_valuation": {"total_pnl_pct": 1.5, "positions": [],
                            "cash": 100000, "total_value": 100000},
        "benchmark_btc_pnl_pct": 2.0,
        "benchmark_spy_pnl_pct": 2.0,
        "outcome": "yellow",
        "description_reveal": "test", "debrief": "test",
        "portfolio_value_series": [], "benchmark_value_series": [],
    }


def _capture_insert(monkeypatch):
    captured = {}

    def fake_insert(run_data):
        captured.update(run_data)
        return True

    import simulator.db as sim_db
    monkeypatch.setattr(sim_db, "insert_run", fake_insert)
    return captured


def test_persist_crypto_run_salva_conviction_reale(monkeypatch):
    captured = _capture_insert(monkeypatch)
    history = [_step([_trade("ALTA", alloc=25, asset="ETH-USD")]),
               _step([_trade("MEDIA", alloc=10, asset="ETH-USD")])]
    v2_crypto_engine._persist_crypto_run(
        {"id": "scen-test", "category": "crash", "num_steps": 2},
        history, _fake_final_result(), run_mode="auto")
    assert captured["conviction"] == "ALTA"


def test_persist_crypto_run_hold_puro_conviction_none(monkeypatch):
    captured = _capture_insert(monkeypatch)
    v2_crypto_engine._persist_crypto_run(
        {"id": "scen-test", "category": "sideways", "num_steps": 2},
        [_step([]), _step([])], _fake_final_result(), run_mode="auto")
    assert captured["conviction"] is None


def test_persist_run_equity_salva_conviction_reale(monkeypatch):
    captured = _capture_insert(monkeypatch)
    history = [_step([_trade("BASSA", alloc=12, asset="XOM")])]
    v2_engine._persist_run(
        {"id": "scen-eq", "category": "macro", "num_steps": 1},
        history, _fake_final_result(), run_mode="manual")
    assert captured["conviction"] == "BASSA"
