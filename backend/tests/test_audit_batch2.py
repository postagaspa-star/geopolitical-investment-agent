"""
Round 2 audit.

Bug chiusi:
  7/8. decision_crypto: apertura con SL obbligatorio non settato → fail-closed
       (chiude subito la posizione naked).
  10.  scalper: idem su OPEN_SHORT/OPEN_LONG/FLIP (prima l'esito di
       set_stop_loss era ignorato → short senza hard-stop).
  22.  _call_deepseek_r1: crash su risposta 200 senza 'choices' → ora guardia.
  23.  history chat: prima caricava i messaggi piu' VECCHI; ora i piu' recenti
       in ordine cronologico.
  38.  crypto_monitor: add_agent_commitment con kwargs/tipo sbagliati →
       commitment perso; ora kwargs corretti e tipo 'monitor' valido.
"""
import asyncio
import types
import pytest

import database
from agents import decision_crypto, crypto_scalper, chat_decision, crypto_monitor


# ── 7/8) decision_crypto: _enforce_open_has_stop (fail-closed) ───────────────

def _fake_pf(calls):
    pf = types.SimpleNamespace()
    pf.execute_cover = lambda *a, **k: (calls.setdefault("cover", []).append(a), {"success": True})[1]
    pf.execute_sell = lambda *a, **k: (calls.setdefault("sell", []).append(a), {"success": True})[1]
    return pf


def _fake_db():
    db = types.SimpleNamespace()
    db.insert_agent_log = lambda *a, **k: None
    return db


def test_enforce_short_naked_covers():
    calls = {}
    res = decision_crypto._enforce_open_has_stop(
        "SHORT", "BTC-USD", 2.0, 100.0, stop_loss=110.0,
        sl_result={"success": False, "reason": "x"}, run_id="t",
        portfolio=_fake_pf(calls), database=_fake_db())
    assert res is not None and res["aborted"] is True and res["close_success"] is True
    assert calls.get("cover") and not calls.get("sell")


def test_enforce_long_naked_sells():
    calls = {}
    res = decision_crypto._enforce_open_has_stop(
        "BUY", "AAPL", 5.0, 100.0, stop_loss=95.0,
        sl_result={"success": False, "reason": "x"}, run_id="t",
        portfolio=_fake_pf(calls), database=_fake_db())
    assert res["aborted"] is True
    assert calls.get("sell") and not calls.get("cover")


def test_enforce_no_abort_when_sl_ok():
    calls = {}
    res = decision_crypto._enforce_open_has_stop(
        "SHORT", "BTC-USD", 2.0, 100.0, stop_loss=110.0,
        sl_result={"success": True}, run_id="t",
        portfolio=_fake_pf(calls), database=_fake_db())
    assert res is None and not calls


def test_enforce_no_abort_when_no_sl_required():
    calls = {}
    res = decision_crypto._enforce_open_has_stop(
        "SHORT", "BTC-USD", 2.0, 100.0, stop_loss=0,
        sl_result=None, run_id="t",
        portfolio=_fake_pf(calls), database=_fake_db())
    assert res is None and not calls


# ── 10) scalper: _execute_scalper_action fail-closed ─────────────────────────

def _scalper_pf(sl_success, calls):
    pf = types.SimpleNamespace()
    pf.execute_short = lambda *a, **k: (calls.setdefault("short", []).append(a), {"success": True})[1]
    pf.execute_buy = lambda *a, **k: (calls.setdefault("buy", []).append(a), {"success": True})[1]
    pf.execute_sell = lambda *a, **k: (calls.setdefault("sell", []).append(a), {"success": True})[1]
    pf.execute_cover = lambda *a, **k: (calls.setdefault("cover", []).append(a), {"success": True})[1]
    pf.set_stop_loss = lambda *a, **k: {"success": sl_success}
    return pf


def _scalper_db(ticker):
    db = types.SimpleNamespace()
    db.get_positions = lambda: [{"ticker": ticker, "quantity": 1.0}]
    db.insert_agent_log = lambda *a, **k: None
    return db


def test_scalper_open_short_sl_fail_closes_and_returns_false():
    calls = {}
    act = crypto_scalper.ScalperAction(action="OPEN_SHORT", price=100.0, stop_loss=110.0, units=1.0, reason="t")
    ok = asyncio.run(crypto_scalper._execute_scalper_action(
        _scalper_pf(False, calls), _scalper_db("BTC-USD"), "rid", "BTC-USD", act))
    assert ok is False
    assert calls.get("short") and calls.get("cover")   # aperta poi coperta (fail-closed)


def test_scalper_open_short_sl_ok_returns_true():
    calls = {}
    act = crypto_scalper.ScalperAction(action="OPEN_SHORT", price=100.0, stop_loss=110.0, units=1.0, reason="t")
    ok = asyncio.run(crypto_scalper._execute_scalper_action(
        _scalper_pf(True, calls), _scalper_db("BTC-USD"), "rid", "BTC-USD", act))
    assert ok is True
    assert calls.get("short") and not calls.get("cover")


def test_scalper_open_long_sl_fail_sells():
    calls = {}
    act = crypto_scalper.ScalperAction(action="OPEN_LONG", price=100.0, stop_loss=95.0, units=1.0, reason="t")
    ok = asyncio.run(crypto_scalper._execute_scalper_action(
        _scalper_pf(False, calls), _scalper_db("BTC-USD"), "rid", "BTC-USD", act))
    assert ok is False
    assert calls.get("buy") and calls.get("sell")


# ── 22) chat_decision: _parse_r1_response difensivo ──────────────────────────

def test_r1_parse_normal_strips_think():
    data = {"choices": [{"message": {"content": "<think>ragiono</think>Ciao"}}]}
    assert chat_decision._parse_r1_response(data) == "Ciao"


def test_r1_parse_empty_choices_raises():
    with pytest.raises(ValueError):
        chat_decision._parse_r1_response({"choices": []})


def test_r1_parse_missing_choices_raises():
    with pytest.raises(ValueError):
        chat_decision._parse_r1_response({"id": "x"})


def test_r1_parse_choice_without_message():
    assert chat_decision._parse_r1_response({"choices": [{}]}) == ""


# ── 23) history chat: i piu' recenti, in ordine cronologico ──────────────────

def test_decision_chat_history_returns_recent_chronological():
    conv = database.get_or_create_decision_chat_conversation("standard")
    for i in range(10):
        database.insert_decision_chat_message(conv, "user", f"msg{i}")
    got = database.get_decision_chat_messages(conv, limit=3)
    # i 3 piu' RECENTI (7,8,9), in ordine cronologico — non i 3 piu' vecchi
    assert [m["content"] for m in got] == ["msg7", "msg8", "msg9"]


# ── 38) crypto_monitor: commitment con kwargs corretti + tipo valido ─────────

def test_record_commitment_uses_correct_kwargs(monkeypatch):
    captured = {}
    monkeypatch.setattr(database, "add_agent_commitment",
                        lambda **k: (captured.update(k), 123)[1])
    analysis = {"verdict": "BUY", "confidence": 0.8, "reasoning": "x",
                "suggested_action": "BUY", "signals_seen": []}
    crypto_monitor._record_commitment_for_decision("BTC-USD", analysis, "rid")
    assert captured["agent_type"] == "crypto"
    assert captured["commitment_type"] == "monitor"
    assert captured["condition_text"]
    assert captured["ticker"] == "BTC-USD"
    assert "commitment_text" not in captured and "related_ticker" not in captured


def test_add_agent_commitment_accepts_monitor_type():
    # 'monitor' deve passare la CHECK/whitelist (prima 'monitor_alert' la
    # violava → commitment perso silenziosamente)
    cid = database.add_agent_commitment(
        agent_type="crypto", commitment_type="monitor",
        condition_text="alert BTC", ticker="BTC-USD", expires_in_hours=4)
    assert cid is not None
