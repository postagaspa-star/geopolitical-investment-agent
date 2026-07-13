"""
Test Step 3 (analisi 13/07): benchmark monkey + buy&hold asset per le
run crypto. Prima del fix perf_monkey_1m/delta_monkey erano SEMPRE 0
per crypto: esisteva solo il confronto con BTC buy&hold.
"""
from simulator.v2_crypto_engine import _compute_crypto_benchmarks, _persist_crypto_run


def _scenario():
    # 2 step, 3 asset. BTC +10%, ETH -10%, SOL +50%.
    return {
        "id": "scen-bench-test",
        "category": "bull_cycle",
        "num_steps": 2,
        "step_dates": ["2024-01-01", "2024-01-03", "2024-01-05"],
        "asset_universe": ["BTC-USD", "ETH-USD", "SOL-USD"],
        "price_series": {
            "BTC-USD": {"2024-01-01": 100.0, "2024-01-03": 105.0, "2024-01-05": 110.0},
            "ETH-USD": {"2024-01-01": 100.0, "2024-01-03": 95.0, "2024-01-05": 90.0},
            "SOL-USD": {"2024-01-01": 100.0, "2024-01-03": 120.0, "2024-01-05": 150.0},
        },
    }


def _history(exposure=0.5):
    # esposizione costante: cash = (1-e)*total
    def step(idx, date):
        return {
            "step_index": idx, "step_date": date,
            "valuation_after": {"cash": (1 - exposure) * 100000,
                                "total_value": 100000},
            "applied_trades": [{"status": "executed_open_long"}],
            "ai_trades": [{"action": "BUY", "asset": "ETH-USD",
                           "allocation_pct": exposure * 100,
                           "conviction": "ALTA"}],
        }
    return [step(0, "2024-01-01"), step(1, "2024-01-03")]


def test_monkey_deterministico_per_run_id():
    scen, hist = _scenario(), _history()
    r1 = _compute_crypto_benchmarks(scen, hist, "run-abc", "ETH-USD", 0.01)
    r2 = _compute_crypto_benchmarks(scen, hist, "run-abc", "ETH-USD", 0.01)
    assert r1[2]["monkey_asset"] == r2[2]["monkey_asset"]
    assert r1[0] == r2[0]


def test_monkey_matematica_a_mano():
    """Con seed noto, verifico lo shadow del monkey a mano."""
    scen, hist = _scenario(), _history(exposure=0.5)
    monkey_frac, delta_frac, block = _compute_crypto_benchmarks(
        scen, hist, "run-xyz", "ETH-USD", 0.02)
    asset = block["monkey_asset"]
    assert asset in scen["asset_universe"]
    # shadow atteso: expo 0 su T0->step0 (stessa data, r=0), poi 0.5 * r per
    # ogni intervallo successivo dell'asset scelto
    ps = scen["price_series"][asset]
    r1 = ps["2024-01-03"] / ps["2024-01-01"] - 1
    r2 = ps["2024-01-05"] / ps["2024-01-03"] - 1
    expected_pct = (0.5 * r1 + 0.5 * r2) * 100
    assert abs(block["monkey_shadow_pct"] - expected_pct) < 0.05
    assert abs(monkey_frac - expected_pct / 100) < 0.001
    assert abs(delta_frac - (0.02 - monkey_frac)) < 1e-9


def test_main_asset_buy_hold():
    scen, hist = _scenario(), _history()
    _, _, block = _compute_crypto_benchmarks(scen, hist, "r", "SOL-USD", 0.0)
    assert abs(block["main_asset_bh_pct"] - 50.0) < 0.01


def test_dati_mancanti_non_crasha():
    assert _compute_crypto_benchmarks({}, [], "r", None, 0.0) == (None, None, {})
    scen = _scenario()
    scen["price_series"] = {}
    assert _compute_crypto_benchmarks(scen, _history(), "r", "ETH-USD", 0.0)[0] is None


def test_persist_scrive_monkey_nel_run_data(monkeypatch):
    captured = {}

    def fake_insert(run_data):
        captured.update(run_data)
        return True

    import simulator.db as sim_db
    monkeypatch.setattr(sim_db, "insert_run", fake_insert)

    final_result = {
        "final_valuation": {"total_pnl_pct": 2.0, "positions": [],
                            "cash": 100000, "total_value": 102000},
        "benchmark_btc_pnl_pct": 10.0,
        "outcome": "yellow",
        "description_reveal": "x", "debrief": "x",
        "portfolio_value_series": [], "benchmark_value_series": [],
    }
    _persist_crypto_run(_scenario(), _history(), final_result, run_mode="auto")
    assert captured["perf_monkey_1m"] is not None
    assert captured["delta_monkey"] is not None
    assert captured["full_data"]["crypto_benchmarks"]["monkey_asset"]
