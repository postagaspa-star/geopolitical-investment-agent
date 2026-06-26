"""
test_risk_confidence_ev_gate.py — gate Expected-Value sul pavimento di confidence.

Fix della paralisi del Decision Standard ("non fa mai un trade"): il pavimento di
confidence del profilo non e' piu' un muro cieco. Una tesi SOTTO il pavimento e'
ammessa SOLO se confidence × min(R/R atteso, 5) raggiunge comunque il pavimento,
con confidence DURA >= 0.45 e MAI in recovery. Chiude la "dead band" tra prompt
(>=50%) e codice (moderate 0.65) SENZA abbassare min_confidence e senza allargare
il rischio sui trade a basso payoff.
"""
import pytest

import risk_profile as rp


@pytest.fixture(autouse=True)
def _force_moderate():
    rp.set_active_profile_key("moderate")  # min_confidence 0.65, equity cap 15
    yield


def _ok(**kw):
    kw.setdefault("asset_class", "equity")
    kw.setdefault("allocation_pct", 5)
    kw.setdefault("open_positions_count", 0)
    return rp.validate_trade(**kw)[0]


def test_subfloor_without_rr_rejected():
    # 0.58 < 0.65 e nessun R/R -> NO TRADE (comportamento storico preservato)
    assert not _ok(confidence=0.58)


def test_subfloor_with_strong_rr_admitted():
    # 0.58 × min(2.0, 5) = 1.16 >= 0.65 e conf >= 0.45 -> ammesso
    assert _ok(confidence=0.58, expected_reward_risk=2.0)


def test_subfloor_with_weak_rr_rejected():
    # 0.58 × 1.0 = 0.58 < 0.65 -> NO TRADE
    assert not _ok(confidence=0.58, expected_reward_risk=1.0)


def test_hard_confidence_floor_045():
    # 0.40 < 0.45 anche con R/R enorme -> NO TRADE (pavimento duro)
    assert not _ok(confidence=0.40, expected_reward_risk=5.0)


def test_rr_below_one_rejected():
    # R/R < 1 non e' payoff asimmetrico -> NO TRADE
    assert not _ok(confidence=0.60, expected_reward_risk=0.9)


def test_rr_capped_at_five():
    # 0.30 × min(20, 5) = 1.5 supererebbe il floor MA conf 0.30 < 0.45 -> NO TRADE
    assert not _ok(confidence=0.30, expected_reward_risk=20.0)


def test_recovery_disables_ev_gate():
    # in recovery il gate EV e' disattivato: 0.58 con R/R alto resta NO TRADE
    assert not _ok(confidence=0.58, expected_reward_risk=3.0, recovery=True)


def test_at_or_above_floor_still_ok():
    # 0.70 >= 0.65 -> ammesso senza bisogno di R/R
    assert _ok(confidence=0.70)


def test_ev_gate_does_not_bypass_allocation_cap():
    # EV supera il floor di confidence ma l'allocation cap (15) rifiuta comunque:
    # il gate tocca SOLO il pavimento di confidence, non gli altri governatori.
    assert not _ok(confidence=0.58, expected_reward_risk=2.0, allocation_pct=20)


def test_crypto_path_inert_without_rr():
    # il Decision Crypto chiama validate_trade SENZA expected_reward_risk:
    # default None -> path EV inerte, comportamento crypto invariato.
    assert not rp.validate_trade(asset_class="crypto", confidence=0.50,
                                 allocation_pct=5, open_positions_count=0)[0]


def test_nan_confidence_still_blocked_with_rr():
    # input anomalo: un NaN non deve passare nemmeno con R/R presente (fail-closed)
    assert not _ok(confidence=float("nan"), expected_reward_risk=3.0)
