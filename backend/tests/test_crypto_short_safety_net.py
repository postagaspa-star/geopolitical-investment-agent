"""
test_crypto_short_safety_net.py — la rete deterministica che semina uno SHORT
quando R1 rinuncia ma il core conferma il ribasso (fix Decision Crypto fermo).

Punti verificati:
  - se R1 non emette trade e il core propone uno SHORT pulito su un ticker FLAT,
    il seed viene instradato nello STESSO _handle_tool e, se ESEGUITO, appare in
    `trades` con source='core_safety_net';
  - se i governatori (risk_profile/drawdown/...) RESPINGONO il seed, si resta
    flat: la rete NON e' un bypass;
  - kill-switch crypto_short_safety_net=off: il core non viene nemmeno sondato;
  - i ticker gia' aperti sono esclusi dai candidati FLAT;
  - l'abstention-gap viene sempre loggato.

Eseguito sincrono via asyncio.run() (no dipendenza da pytest-asyncio).
"""
import asyncio
import json

import pytest

import database
from agents import decision_crypto as dc


def _run(coro):
    return asyncio.run(coro)


def _candidate(ticker="BTC-USD", conv=0.72):
    return {"ticker": ticker, "entry": 100.0, "stop_loss": 110.0,
            "size_units": 0.5, "conviction": conv, "regime": "bear",
            "completeness": 0.6, "reward_risk": 2.1}


@pytest.fixture(autouse=True)
def _patch_db(monkeypatch):
    logs = []
    monkeypatch.setattr(database, "get_setting",
                        lambda k, d=None: "on" if k == "crypto_short_safety_net" else d,
                        raising=False)
    monkeypatch.setattr(database, "get_positions", lambda: [], raising=False)
    monkeypatch.setattr(database, "insert_agent_log",
                        lambda *a, **k: logs.append(a), raising=False)
    return logs


def test_seeds_short_when_r1_abstains(monkeypatch, _patch_db):
    async def _fake_cands(run_id, flat, nav):
        return [_candidate()]

    async def _fake_handle(tool, inp, run_id, workflow_state=None, auditor_binding=None):
        assert tool == "execute_trade"
        # il seed riusa entry/SL/size DEL CORE, direzione SHORT
        assert inp["action"] == "SHORT"
        assert inp["stop_loss"] == 110.0
        assert inp["quantity"] == 0.5
        return json.dumps({"executed": True, "ticker": inp["ticker"], "action": "SHORT"})

    monkeypatch.setattr(dc, "_core_short_candidates", _fake_cands)
    monkeypatch.setattr(dc, "_handle_tool", _fake_handle)

    trades = []
    _run(dc._run_short_safety_net("run1", trades, "R1 ha rinunciato", None,
                                  {"total_value": 10000.0}, {}))
    assert len(trades) == 1
    assert trades[0]["source"] == "core_safety_net"
    assert trades[0]["action"] == "SHORT"
    assert any(a and a[1] == "DECISION_CRYPTO_ABSTENTION_GAP" for a in _patch_db)


def test_governor_rejection_keeps_flat(monkeypatch, _patch_db):
    async def _fake_cands(run_id, flat, nav):
        return [_candidate()]

    async def _fake_handle(tool, inp, run_id, workflow_state=None, auditor_binding=None):
        # i governatori respingono (es. drawdown cap): NESSUN bypass
        return json.dumps({"executed": False, "rejected": True,
                           "reason": "RISK_PROFILE: drawdown cap"})

    monkeypatch.setattr(dc, "_core_short_candidates", _fake_cands)
    monkeypatch.setattr(dc, "_handle_tool", _fake_handle)

    trades = []
    _run(dc._run_short_safety_net("run2", trades, "abstain", None,
                                  {"total_value": 10000.0}, {}))
    assert trades == []


def test_disabled_by_setting(monkeypatch, _patch_db):
    monkeypatch.setattr(database, "get_setting",
                        lambda k, d=None: "off" if k == "crypto_short_safety_net" else d,
                        raising=False)
    called = {"cands": False}

    async def _fake_cands(run_id, flat, nav):
        called["cands"] = True
        return [_candidate()]

    monkeypatch.setattr(dc, "_core_short_candidates", _fake_cands)

    trades = []
    _run(dc._run_short_safety_net("run3", trades, "abstain", None,
                                  {"total_value": 10000.0}, {}))
    assert trades == []
    assert called["cands"] is False  # kill-switch: il core non viene sondato


def test_open_ticker_excluded_from_flat(monkeypatch, _patch_db):
    monkeypatch.setattr(database, "get_positions",
                        lambda: [{"ticker": "BTC-USD"}], raising=False)
    seen = {"flat": None}

    async def _fake_cands(run_id, flat, nav):
        seen["flat"] = list(flat)
        return []

    monkeypatch.setattr(dc, "_core_short_candidates", _fake_cands)

    trades = []
    _run(dc._run_short_safety_net("run4", trades, "abstain",
                                  ["BTC-USD", "ETH-USD"], {"total_value": 5000.0}, {}))
    assert "BTC-USD" not in (seen["flat"] or [])
    assert "ETH-USD" in (seen["flat"] or [])


def test_no_nav_no_seed(monkeypatch, _patch_db):
    # NAV nullo -> niente sizing possibile -> nessuna semina
    called = {"cands": False}

    async def _fake_cands(run_id, flat, nav):
        called["cands"] = True
        return [_candidate()]

    monkeypatch.setattr(dc, "_core_short_candidates", _fake_cands)

    trades = []
    _run(dc._run_short_safety_net("run5", trades, "abstain", None,
                                  {"total_value": 0.0, "cash": 0.0}, {}))
    assert trades == []
    assert called["cands"] is False
