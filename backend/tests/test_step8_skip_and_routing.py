"""
test_step8_skip_and_routing.py — Step 8 della pulizia.

8a — l'early-skip dello Standard CEDE a un movimento reale: la signature include
     ora il prezzo coarse (~1%) delle posizioni, e c'e' un kill-switch
     (decision_early_skip_enabled='off') per disattivarlo del tutto.
8b — il routing del watchdog non SCARTA piu' l'equity su trigger misto: con
     route_dispatch_both='on' dispaccia ANCHE la pipeline standard (default OFF
     = comportamento storico).
"""
import asyncio

import pytest

import database
from agents import decision as dec
from agents import orchestrator as orch


def _run(coro):
    return asyncio.run(coro)


# ─────────────────────────── 8a: early-skip ────────────────────────────

def test_coarse_price_3_sig_figs():
    assert dec._coarse_price(102.37) == 102.0
    assert dec._coarse_price(0) == 0.0
    assert dec._coarse_price(None) == 0.0
    assert dec._coarse_price(0.123456) == 0.123


def test_signature_changes_on_meaningful_price_move():
    base = {"positions": [{"ticker": "AAPL", "quantity": 10, "current_price": 100.0}],
            "cash": 5000.0}
    sig_a = dec._compute_context_signature(base, [], [], [])
    # +0.3% non cambia il bucket coarse -> stessa signature (efficienza preservata)
    small = {"positions": [{"ticker": "AAPL", "quantity": 10, "current_price": 100.3}],
             "cash": 5000.0}
    assert dec._compute_context_signature(small, [], [], []) == sig_a
    # +2% cambia il bucket -> signature diversa -> l'early-skip CEDE
    big = {"positions": [{"ticker": "AAPL", "quantity": 10, "current_price": 102.0}],
           "cash": 5000.0}
    assert dec._compute_context_signature(big, [], [], []) != sig_a


def test_kill_switch_disables_early_skip(monkeypatch):
    monkeypatch.setattr(database, "get_setting",
                        lambda k, d=None: "off" if k == "decision_early_skip_enabled" else d,
                        raising=False)
    skip, why = dec._should_skip_decision_run(
        current_signature="same", watchdog_reason=None, focus_tickers=None,
        agent_type="standard")
    assert skip is False
    assert "disattivato" in why


# ─────────────────────────── 8b: routing ───────────────────────────────

def _wire_routing_mocks(monkeypatch, dispatch_both: bool):
    import agents.watchdog as wd
    import scheduler
    from agents import decision as _decision

    monkeypatch.setattr(database, "insert_agent_log", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(database, "get_setting",
                        lambda k, d=None: ("on" if (k == "route_dispatch_both" and dispatch_both) else d),
                        raising=False)

    async def fake_wd(run_id):
        return {"should_trigger": True, "urgency": 5, "reason": "macro FOMC",
                "focus_tickers": ["BTC-USD", "AAPL"], "rebalance": False}
    monkeypatch.setattr(wd, "run_watchdog", fake_wd)

    monkeypatch.setattr(scheduler, "is_market_open", lambda: True, raising=False)
    monkeypatch.setattr(scheduler, "is_us_market_open", lambda: True, raising=False)
    monkeypatch.setattr(scheduler, "is_standard_agent_active", lambda: True, raising=False)

    async def fake_crypto(**kw):
        return {"decision": "TRADE", "trades": [{"ticker": "BTC-USD"}]}
    monkeypatch.setattr(orch, "run_crypto_pipeline", fake_crypto)

    eq_calls = []

    async def fake_eq(*a, **k):
        eq_calls.append(k)
        return {"decision": "TRADE", "trades": []}
    monkeypatch.setattr(_decision, "run_decision_agent", fake_eq)
    return eq_calls


def test_dispatch_both_off_drops_equity_by_default(monkeypatch):
    eq_calls = _wire_routing_mocks(monkeypatch, dispatch_both=False)
    res = _run(orch.run_watchdog_pipeline("run-off"))
    assert res["route"] == "crypto"
    assert "equity_route" not in res
    assert len(eq_calls) == 0  # comportamento storico: equity scartato


def test_dispatch_both_on_dispatches_equity(monkeypatch):
    eq_calls = _wire_routing_mocks(monkeypatch, dispatch_both=True)
    res = _run(orch.run_watchdog_pipeline("run-on"))
    assert res["route"] == "crypto+equity"
    assert len(eq_calls) == 1
    assert eq_calls[0].get("focus_tickers") == ["AAPL"]  # solo il ticker equity
