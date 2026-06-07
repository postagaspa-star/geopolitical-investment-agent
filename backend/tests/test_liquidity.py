"""
test_liquidity.py — Step 3: guardrail di liquidità/qualità (puri, no DB/rete).
"""
import liquidity as L


def test_compute_adv():
    assert L.compute_adv_usd([10, 10, 10], [1e6, 1e6, 1e6]) == 1e7
    assert L.compute_adv_usd([], []) is None
    assert L.compute_adv_usd([10], [0]) is None  # volume 0 scartato


def test_adv_from_bars():
    bars = [{"close": 10, "volume": 1e6}, {"close": 20, "volume": 2e6}]
    assert L.adv_usd_from_bars(bars) == (10 * 1e6 + 20 * 2e6) / 2


def test_floor():
    assert L.passes_liquidity_floor(6e6, 5e6)
    assert not L.passes_liquidity_floor(4e6, 5e6)
    assert not L.passes_liquidity_floor(None, 5e6)


def test_validate_symbol():
    assert L.validate_symbol("EWZ")[0]
    assert not L.validate_symbol("ENI.MI")[0]
    assert not L.validate_symbol("")[0]
    assert L.validate_symbol("ENI.MI", allow_non_us=True)[0]


def test_quality_min_bars():
    good = [{"close": float(i), "volume": 1, "date": "2026-06-01"} for i in range(1, 40)]
    assert L.ohlcv_quality_ok(good, min_bars=30)[0]
    assert not L.ohlcv_quality_ok([{"close": 1, "volume": 1}] * 10, min_bars=30)[0]


def test_quality_corrupt():
    bars = [{"close": 10.0, "volume": 1, "date": "2026-06-01"}] * 40
    ok, reason = L.ohlcv_quality_ok(bars, min_bars=30)
    assert not ok and "corrotta" in reason


def test_quality_stale():
    bars = [{"close": float(i), "volume": 1, "date": "2026-01-01"} for i in range(1, 40)]
    ok, reason = L.ohlcv_quality_ok(bars, min_bars=30, asof_date="2026-06-01")
    assert not ok and "stant" in reason.lower()
    # senza asof_date la staleness non si valuta
    assert L.ohlcv_quality_ok(bars, min_bars=30)[0]
