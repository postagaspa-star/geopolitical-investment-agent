"""
test_risk_satellite.py — Step 3: il tier 'satellite' stringe il sizing e applica
il cap di esposizione aggregata, SENZA toccare il core (tier='core' default) né
il crypto. Profilo forzato a 'moderate' per determinismo.
"""
import pytest

import risk_profile as rp


@pytest.fixture(autouse=True)
def _force_moderate():
    rp.set_active_profile_key("moderate")  # equity cap 15, sat_mult 0.4, sat_cap 15
    yield


def test_core_unchanged():
    assert rp.validate_trade(asset_class="equity", confidence=0.9,
                             allocation_pct=10, open_positions_count=0)[0]
    assert not rp.validate_trade(asset_class="equity", confidence=0.9,
                                 allocation_pct=20, open_positions_count=0)[0]


def test_satellite_sizing_tighter():
    # cap satellite = 15 * 0.4 = 6 → alloc 10 RIFIUTATA, alloc 5 OK
    assert not rp.validate_trade(asset_class="equity", confidence=0.9, allocation_pct=10,
                                 open_positions_count=0, tier="satellite")[0]
    assert rp.validate_trade(asset_class="equity", confidence=0.9, allocation_pct=5,
                             open_positions_count=0, tier="satellite")[0]


def test_satellite_aggregate_cap():
    # cap aggregato 15: 13 + 5 = 18 > 15 → reject
    assert not rp.validate_trade(asset_class="equity", confidence=0.9, allocation_pct=5,
                                 open_positions_count=0, tier="satellite",
                                 satellite_exposure_pct=13.0)[0]
    # 10 + 2 = 12 < 15 → ok (e 2 < cap per-trade 6)
    assert rp.validate_trade(asset_class="equity", confidence=0.9, allocation_pct=2,
                             open_positions_count=0, tier="satellite",
                             satellite_exposure_pct=10.0)[0]


def test_crypto_ignores_tier():
    # crypto cap moderate 12; il tier satellite non riduce il crypto
    assert rp.validate_trade(asset_class="crypto", confidence=0.9, allocation_pct=10,
                             open_positions_count=0, tier="satellite")[0]
