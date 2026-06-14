"""
Round 1 audit — cluster SHORT.

Bug chiusi:
  1. set_stop_loss / set_take_profit con is_long hardcoded → SL/TP di ogni SHORT
     rifiutato (posizione naked).
  2. close_position_market: routing per direzione (SHORT→cover, LONG→sell).
  3. liquidate_all_positions: instradava sempre execute_sell → SHORT mai chiuse.
  4. endpoint /positions/close: 200 anche su fallimento; manual-liquidate-all
     contava una SHORT non chiusa come "liquidata".
  5. executor SL/TP della chat: prezzo calcolato sempre sul lato LONG.

Tutto mockato (niente DB/rete). Regressione + stress (portafogli misti,
segni pct invertiti).
"""
import asyncio
import pytest

import portfolio
import main
import data_fetchers
import database


# ─────────────────────────────────────────────────────────────────────────────
# 1) set_stop_loss / set_take_profit direction-aware  (root fix)
# ─────────────────────────────────────────────────────────────────────────────

def _patch_pos(monkeypatch, direction, current=100.0):
    pos = {"ticker": "TST", "quantity": 10, "direction": direction,
           "current_price": current, "avg_buy_price": current}
    monkeypatch.setattr(portfolio, "get_position", lambda t: pos)
    monkeypatch.setattr(portfolio, "_refresh_current_price", lambda t, f: current)
    monkeypatch.setattr(portfolio, "update_position_auto_exit", lambda *a, **k: True)
    monkeypatch.setattr(database, "insert_agent_log", lambda *a, **k: None, raising=False)
    return pos


def test_sl_short_above_current_now_accepted(monkeypatch):
    # PRIMA: is_long=True hardcoded → SL>current rifiutato → short naked.
    _patch_pos(monkeypatch, "SHORT", current=100.0)
    res = portfolio.set_stop_loss("TST", 105.0)
    assert res["success"] is True, res


def test_sl_short_below_current_rejected(monkeypatch):
    _patch_pos(monkeypatch, "SHORT", current=100.0)
    res = portfolio.set_stop_loss("TST", 95.0)   # sotto = lato sbagliato per SHORT
    assert res["success"] is False
    assert "SOPRA" in res["reason"]


def test_sl_long_below_ok_above_rejected(monkeypatch):
    _patch_pos(monkeypatch, "LONG", current=100.0)
    assert portfolio.set_stop_loss("TST", 95.0)["success"] is True
    _patch_pos(monkeypatch, "LONG", current=100.0)
    bad = portfolio.set_stop_loss("TST", 105.0)
    assert bad["success"] is False and "SOTTO" in bad["reason"]


def test_tp_short_below_ok_above_rejected(monkeypatch):
    _patch_pos(monkeypatch, "SHORT", current=100.0)
    assert portfolio.set_take_profit("TST", 95.0)["success"] is True
    _patch_pos(monkeypatch, "SHORT", current=100.0)
    assert portfolio.set_take_profit("TST", 105.0)["success"] is False


def test_tp_long_above_ok(monkeypatch):
    _patch_pos(monkeypatch, "LONG", current=100.0)
    assert portfolio.set_take_profit("TST", 105.0)["success"] is True


# ─────────────────────────────────────────────────────────────────────────────
# 2) close_position_market: routing per direzione
# ─────────────────────────────────────────────────────────────────────────────

def _capture_exec(monkeypatch, positions):
    """positions: dict ticker->direction. Mocka get_position + execute_*."""
    calls = {"sell": [], "cover": []}
    monkeypatch.setattr(portfolio, "get_position",
                        lambda t: ({"ticker": t, "direction": positions.get(t), "quantity": 5}
                                   if t in positions else None))
    monkeypatch.setattr(portfolio, "execute_sell",
                        lambda *a, **k: (calls["sell"].append(a[0]), {"success": True, "action": "SELL"})[1])
    monkeypatch.setattr(portfolio, "execute_cover",
                        lambda *a, **k: (calls["cover"].append(a[0]), {"success": True, "action": "COVER"})[1])
    return calls


def test_close_market_short_routes_to_cover(monkeypatch):
    calls = _capture_exec(monkeypatch, {"BTC-USD": "SHORT"})
    res = portfolio.close_position_market("BTC-USD", 5, 100.0, "g", "t")
    assert res["action"] == "COVER"
    assert calls["cover"] == ["BTC-USD"] and calls["sell"] == []


def test_close_market_long_routes_to_sell(monkeypatch):
    calls = _capture_exec(monkeypatch, {"AAPL": "LONG"})
    res = portfolio.close_position_market("AAPL", 5, 100.0, "g", "t")
    assert res["action"] == "SELL"
    assert calls["sell"] == ["AAPL"] and calls["cover"] == []


def test_close_market_no_position(monkeypatch):
    _capture_exec(monkeypatch, {})
    res = portfolio.close_position_market("X", 5, 100.0, "g", "t")
    assert res["success"] is False


# ─────────────────────────────────────────────────────────────────────────────
# 3) liquidate_all_positions: stress portafoglio misto LONG/SHORT
# ─────────────────────────────────────────────────────────────────────────────

def test_liquidate_all_routes_short_to_cover_mixed(monkeypatch):
    positions = [
        {"ticker": "AAPL", "quantity": 10, "direction": "LONG", "current_price": 100.0, "avg_buy_price": 90},
        {"ticker": "BTC-USD", "quantity": 2, "direction": "SHORT", "current_price": 50.0, "avg_buy_price": 60},
        {"ticker": "ETH-USD", "quantity": 3, "direction": "SHORT", "current_price": 20.0, "avg_buy_price": 25},
    ]
    by_ticker = {p["ticker"]: p for p in positions}
    monkeypatch.setattr(portfolio, "get_positions", lambda: positions)
    monkeypatch.setattr(portfolio, "get_position", lambda t: by_ticker.get(t))
    calls = {"sell": [], "cover": []}
    monkeypatch.setattr(portfolio, "execute_sell",
                        lambda ticker, *a, **k: (calls["sell"].append(ticker), {"success": True})[1])
    monkeypatch.setattr(portfolio, "execute_cover",
                        lambda ticker, *a, **k: (calls["cover"].append(ticker), {"success": True})[1])
    executed = portfolio.liquidate_all_positions(reason="test")
    assert calls["sell"] == ["AAPL"]
    assert set(calls["cover"]) == {"BTC-USD", "ETH-USD"}
    assert all(e["result"]["success"] for e in executed)


# ─────────────────────────────────────────────────────────────────────────────
# 4) endpoint close (400 su fallimento) + conteggio onesto manual-liquidate-all
# ─────────────────────────────────────────────────────────────────────────────

def test_close_endpoint_short_success(monkeypatch):
    monkeypatch.setattr(database, "get_position",
                        lambda t: {"ticker": t, "quantity": 2, "direction": "SHORT", "current_price": 50.0})
    monkeypatch.setattr(data_fetchers, "fetch_market_data",
                        lambda t, period_days=5: {"data": [{"close": 50.0}]})
    monkeypatch.setattr(portfolio, "close_position_market",
                        lambda **k: {"success": True, "action": "COVER"})
    res = asyncio.run(main.close_position(main.ClosePositionPayload(ticker="BTC-USD")))
    assert res["success"] is True and res["action"] == "COVER"


def test_close_endpoint_failure_returns_400(monkeypatch):
    from fastapi.responses import JSONResponse
    monkeypatch.setattr(database, "get_position",
                        lambda t: {"ticker": t, "quantity": 2, "direction": "SHORT", "current_price": 50.0})
    monkeypatch.setattr(data_fetchers, "fetch_market_data",
                        lambda t, period_days=5: {"data": [{"close": 50.0}]})
    monkeypatch.setattr(portfolio, "close_position_market",
                        lambda **k: {"success": False, "reason": "boom"})
    res = asyncio.run(main.close_position(main.ClosePositionPayload(ticker="BTC-USD")))
    assert isinstance(res, JSONResponse) and res.status_code == 400


def test_manual_liquidate_counts_only_real_success(monkeypatch):
    executed = [
        {"ticker": "AAPL", "result": {"success": True}},
        {"ticker": "BTC-USD", "result": {"success": False, "reason": "x"}},  # SHORT non chiusa
        {"ticker": "ETH-USD", "error": "boom"},
    ]
    monkeypatch.setattr(portfolio, "liquidate_all_positions", lambda **k: executed)
    res = asyncio.run(main.manual_liquidate_all(
        main.ManualLiquidatePayload(confirm="I_UNDERSTAND_LIQUIDATE_ALL", reason="t")))
    assert res["positions_liquidated"] == 1   # solo AAPL
    assert res["errors"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# 5) executor SL/TP della chat: prezzo direction-aware (dalla magnitudine)
# ─────────────────────────────────────────────────────────────────────────────

def _patch_chat_pos(monkeypatch, direction, entry=200.0):
    pos = {"ticker": "TST", "direction": direction, "avg_buy_price": entry, "quantity": 5}
    monkeypatch.setattr(database, "get_positions", lambda: [pos])
    monkeypatch.setattr(database, "update_position_auto_exit", lambda *a, **k: True)


def test_chat_sl_short_above_entry(monkeypatch):
    _patch_chat_pos(monkeypatch, "SHORT", entry=200.0)
    res = main._exec_action_set_stop_loss({"ticker": "TST", "stop_loss_pct": 5})
    assert res["ok"] is True
    assert res["stop_loss_price"] == pytest.approx(210.0)   # +5% sopra per SHORT


def test_chat_sl_long_below_entry(monkeypatch):
    _patch_chat_pos(monkeypatch, "LONG", entry=200.0)
    res = main._exec_action_set_stop_loss({"ticker": "TST", "stop_loss_pct": 5})
    assert res["stop_loss_price"] == pytest.approx(190.0)   # -5% sotto per LONG


def test_chat_sl_ignores_wrong_sign(monkeypatch):
    # anche col segno sbagliato dell'LLM, la magnitudine decide il lato giusto
    _patch_chat_pos(monkeypatch, "SHORT", entry=100.0)
    res = main._exec_action_set_stop_loss({"ticker": "TST", "stop_loss_pct": -8})
    assert res["stop_loss_price"] == pytest.approx(108.0)   # SHORT → sopra comunque


def test_chat_tp_short_below_long_above(monkeypatch):
    _patch_chat_pos(monkeypatch, "SHORT", entry=100.0)
    assert main._exec_action_set_take_profit(
        {"ticker": "TST", "take_profit_pct": 10})["take_profit_price"] == pytest.approx(90.0)
    _patch_chat_pos(monkeypatch, "LONG", entry=100.0)
    assert main._exec_action_set_take_profit(
        {"ticker": "TST", "take_profit_pct": 10})["take_profit_price"] == pytest.approx(110.0)
