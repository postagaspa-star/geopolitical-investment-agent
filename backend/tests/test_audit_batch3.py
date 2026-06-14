"""
Round 3 audit — robustezza dei risk governor.

Bug chiusi:
  15. risk_profile.validate_trade: fail-OPEN (`except: pass` saltava il controllo
      e NaN passava) → ora fail-CLOSED su input non valido/non finito.
  16. decision._handle_decision_tool: `except Exception` nella validazione rischio
      cadeva su execute SENZA controllo → ora fail-closed (verificato da review +
      regressione: ramo inline in handler async, mirror dell'ImportError gia'
      fail-closed; nessun test comportamentale isolato).
  17. risk_state.build_risk_state_prompt_block: crash (TypeError) se recovery
      attivo ma target None → il blocco rischio spariva dal prompt. Ora formatta
      in modo difensivo.
  37. db_sqlite.add_agent_commitment: expires_at salvato isoformat ('T'+offset)
      non confrontabile come TEXT con datetime('now') → mai scaduto. Ora
      'YYYY-MM-DD HH:MM:SS'.
  39. capital_orchestrator._execute_liquidation: coprire una SHORT (spende cash)
      veniva contato come capitale liberato → ora proceeds=0 per le SHORT.
"""
import pytest

import database
import risk_profile
import risk_state
import portfolio
from agents import capital_orchestrator as co


# ── 15) validate_trade fail-closed + NaN/Inf ─────────────────────────────────

def test_validate_trade_nan_confidence_blocks():
    ok, reason = risk_profile.validate_trade(
        asset_class="equity", confidence=float("nan"),
        allocation_pct=0.5, open_positions_count=0)
    assert ok is False and "sicurezza" in reason


def test_validate_trade_nonnumeric_confidence_blocks():
    ok, reason = risk_profile.validate_trade(
        asset_class="equity", confidence="abc",
        allocation_pct=0.5, open_positions_count=0)
    assert ok is False and "sicurezza" in reason


def test_validate_trade_inf_allocation_blocks():
    ok, reason = risk_profile.validate_trade(
        asset_class="equity", confidence=0.99,
        allocation_pct=float("inf"), open_positions_count=0)
    assert ok is False and "sicurezza" in reason


def test_validate_trade_nonnumeric_open_count_blocks():
    ok, reason = risk_profile.validate_trade(
        asset_class="equity", confidence=0.99,
        allocation_pct=0.5, open_positions_count="xx")
    assert ok is False and "sicurezza" in reason


def test_validate_trade_valid_inputs_pass():
    ok, reason = risk_profile.validate_trade(
        asset_class="equity", confidence=0.99,
        allocation_pct=0.5, open_positions_count=0)
    assert ok is True


# ── 17) build_risk_state_prompt_block: niente crash con target None ──────────

def _recovery_snapshot(target):
    return {
        "recovery_mode": True, "recovery_target_balance": target,
        "drawdown_24h_pct": -5.0, "wr_sample_size": 10, "win_rate_last_10": 0.5,
        "wr_wins": 5, "wr_losses": 5, "max_position_concentration_pct": 20.0,
        "max_concentration_ticker": "AAPL", "concentration_trigger_active": False,
    }


def test_risk_state_prompt_no_crash_when_target_none(monkeypatch):
    monkeypatch.setattr(risk_state, "build_risk_state_snapshot",
                        lambda: _recovery_snapshot(None))
    block = risk_state.build_risk_state_prompt_block()   # prima: TypeError
    assert "RECOVERY MODE" in block and "n/d" in block


def test_risk_state_prompt_shows_target_when_set(monkeypatch):
    monkeypatch.setattr(risk_state, "build_risk_state_snapshot",
                        lambda: _recovery_snapshot(123456.0))
    block = risk_state.build_risk_state_prompt_block()
    assert "123456.00" in block


# ── 37) commitment expires_at in formato confrontabile ───────────────────────

def test_commitment_expires_at_comparable_format():
    cid = database.add_agent_commitment(
        agent_type="crypto", commitment_type="monitor",
        condition_text="x", ticker="BTC-USD", expires_in_hours=1)
    assert cid is not None
    import db_sqlite
    with db_sqlite.get_db() as conn:
        row = conn.execute(
            "SELECT expires_at FROM agent_commitments WHERE id=?", (cid,)).fetchone()
        exp = row["expires_at"]
        assert exp and "T" not in exp   # 'YYYY-MM-DD HH:MM:SS', non isoformat
        # la deadline (~+1h) deve risultare < (now+2h) con confronto TEXT SQL —
        # col vecchio isoformat ('T') il confronto falliva sempre
        match = conn.execute(
            "SELECT 1 FROM agent_commitments WHERE id=? "
            "AND expires_at < datetime('now','+2 hours')", (cid,)).fetchone()
        assert match is not None


# ── 39) liquidazione: una SHORT coperta non libera capitale ──────────────────

def test_execute_liquidation_short_proceeds_zero(monkeypatch):
    monkeypatch.setattr(database, "get_position",
                        lambda t: {"ticker": t, "direction": "SHORT",
                                   "current_price": 50.0, "quantity": 2})
    monkeypatch.setattr(portfolio, "execute_cover", lambda **k: {"success": True})
    res = co._execute_liquidation({"ticker": "BTC-USD", "quantity_to_sell": 2}, "rid")
    assert res["success"] is True
    assert res["proceeds"] == 0.0   # cover SPENDE cash → niente liquidità liberata


def test_execute_liquidation_long_proceeds_positive(monkeypatch):
    monkeypatch.setattr(database, "get_position",
                        lambda t: {"ticker": t, "direction": "LONG",
                                   "current_price": 50.0, "quantity": 2})
    monkeypatch.setattr(portfolio, "execute_sell", lambda **k: {"success": True})
    res = co._execute_liquidation({"ticker": "AAPL", "quantity_to_sell": 2}, "rid")
    assert res["success"] is True
    assert res["proceeds"] == 100.0
