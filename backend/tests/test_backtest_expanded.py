"""
test_backtest_expanded.py — Step 4: la simulate() del backtest del gate è pura e
corretta su dati sintetici (no rete).
"""
from simulator.backtest_expanded_universe import simulate, _effective_n, _max_drawdown_pct


def _dates(n):
    return [f"d{i:03d}" for i in range(n)]


def test_picks_momentum_winner():
    n = 120
    closes = {
        "UP": [100.0 + i for i in range(n)],     # trend su
        "FLAT": [100.0] * n,                       # piatto
        "DN": [200.0 - i for i in range(n)],      # trend giù
    }
    adv = {k: 1e12 for k in closes}
    r = simulate(_dates(n), closes, adv, top_k=1, lookback=10, rebalance_every=10)
    assert r["return_pct"] > 0
    assert r["breadth"] == 1  # solo UP ha momentum positivo


def test_effective_n():
    assert _effective_n([1, 1, 1, 1]) == 4.0
    assert round(_effective_n([3, 1]), 2) == 1.6
    assert _effective_n([0, 0]) == 0.0


def test_max_drawdown():
    assert _max_drawdown_pct([100, 120, 90, 110]) == 25.0
    assert _max_drawdown_pct([100, 101, 102]) == 0.0


def test_slippage_penalizes_illiquid():
    n = 120
    up = [100.0 + i for i in range(n)]
    illiquid = simulate(_dates(n), {"UP": up}, {"UP": 5e4},
                        top_k=1, lookback=10, rebalance_every=10)
    liquid = simulate(_dates(n), {"UP": up}, {"UP": 1e12},
                      top_k=1, lookback=10, rebalance_every=10)
    assert illiquid["return_pct"] < liquid["return_pct"]


def test_satellite_share_tracked():
    n = 120
    closes = {
        "XLE": [100.0 + i for i in range(n)],        # satellite (sector_etf)
        "NEE": [100.0 + 0.5 * i for i in range(n)],  # core (rotation defensive)
    }
    adv = {k: 1e12 for k in closes}
    r = simulate(_dates(n), closes, adv, top_k=2, lookback=10, rebalance_every=10)
    assert 0.0 < r["satellite_share"] <= 1.0  # XLE è satellite
