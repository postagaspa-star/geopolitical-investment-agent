"""
Test Step 7 (analisi 13/07): fix loop fail-closed del cricchetto SL +
cooldown ri-entrata per-ticker dopo chiusure meccaniche.

Caso reale riprodotto: SOL 29/06 18:02 — BUY (conf 78) → il cricchetto
rifiuta lo stop proposto → fail-closed SELL dell'intera posizione → il
Decision ricompra 30s dopo → di nuovo fail-closed. 4 trade in 34 secondi.
"""
import json
import types

import db_sqlite
import database
import trade_guards
from agents.decision_crypto import _enforce_open_has_stop


class _FakePortfolio:
    def __init__(self, existing_sl=0.0):
        self.existing_sl = existing_sl
        self.closed = []

    def get_position(self, ticker):
        return {"ticker": ticker, "stop_loss_price": self.existing_sl}

    def execute_sell(self, ticker, qty, price, **kw):
        self.closed.append(("SELL", ticker, qty))
        return {"success": True}

    def execute_cover(self, ticker, qty, price, **kw):
        self.closed.append(("COVER", ticker, qty))
        return {"success": True}


class _FakeDb:
    def __init__(self):
        self.logs = []

    def insert_agent_log(self, run_id, phase, content):
        self.logs.append((phase, content))


SL_FAIL = {"success": False,
           "reason": "Cricchetto anti-allargamento: lo stop non puo' allargarsi"}


# ── fix fail-closed ──────────────────────────────────────────────────────

def test_sl_rifiutato_con_stop_esistente_non_chiude():
    """Scenario SOL: ADD su posizione con SL valido, cricchetto rifiuta il
    nuovo SL -> la posizione va MANTENUTA con lo stop esistente."""
    pf, db = _FakePortfolio(existing_sl=72.0), _FakeDb()
    res = _enforce_open_has_stop("BUY", "SOL-USD", 35.0, 75.31,
                                 stop_loss=52.7, sl_result=SL_FAIL,
                                 run_id="t", portfolio=pf, database=db)
    assert res is not None
    assert res["kept_existing_stop"] is True
    assert res["aborted"] is False
    assert res["existing_sl"] == 72.0
    assert pf.closed == []                       # NESSUNA chiusura
    assert any(p == "DECISION_CRYPTO_SL_KEPT" for p, _ in db.logs)


def test_sl_rifiutato_posizione_naked_chiude_ancora():
    """Regressione: posizione DAVVERO senza stop -> fail-closed resta."""
    pf, db = _FakePortfolio(existing_sl=0.0), _FakeDb()
    res = _enforce_open_has_stop("BUY", "SOL-USD", 35.0, 75.31,
                                 stop_loss=52.7, sl_result=SL_FAIL,
                                 run_id="t", portfolio=pf, database=db)
    assert res is not None and res["aborted"] is True
    assert ("SELL", "SOL-USD", 35.0) in pf.closed
    assert any(p == "DECISION_CRYPTO_NAKED_ABORT" for p, _ in db.logs)


def test_sl_ok_nessun_intervento():
    pf, db = _FakePortfolio(existing_sl=0.0), _FakeDb()
    res = _enforce_open_has_stop("BUY", "SOL-USD", 35.0, 75.31,
                                 stop_loss=70.0,
                                 sl_result={"success": True},
                                 run_id="t", portfolio=pf, database=db)
    assert res is None and pf.closed == []


def test_short_naked_chiude_con_cover():
    pf, db = _FakePortfolio(existing_sl=0.0), _FakeDb()
    res = _enforce_open_has_stop("SHORT", "ETH-USD", 5.0, 1800.0,
                                 stop_loss=1900.0, sl_result=SL_FAIL,
                                 run_id="t", portfolio=pf, database=db)
    assert res["aborted"] is True
    assert ("COVER", "ETH-USD", 5.0) in pf.closed


# ── cooldown ri-entrata ──────────────────────────────────────────────────

def _insert_mech_close(ticker, direction="LONG", exec_type="fail_closed",
                       minutes_ago=0):
    with db_sqlite.get_db() as conn:
        conn.execute(
            "INSERT INTO trades (ticker,action,quantity,price,total_value,"
            "geopolitical_reasoning,technical_reasoning,final_decision,"
            "confidence_score,direction,execution_type,timestamp) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now', ?))",
            (ticker, "SELL" if direction == "LONG" else "BUY",
             1.0, 100.0, 100.0, "FAIL-CLOSED: test", "t", "d",
             None, direction, exec_type, f"-{minutes_ago} minutes"))


def test_cooldown_blocca_stessa_direzione():
    database.set_setting(trade_guards.SETTING_REENTRY_COOLDOWN_MIN, "60")
    _insert_mech_close("CDT-USD", direction="LONG", minutes_ago=5)
    ok, why = trade_guards.check_reentry_cooldown("CDT-USD", "BUY")
    assert not ok and "cooldown" in why


def test_cooldown_permette_chiusure_e_direzione_opposta():
    database.set_setting(trade_guards.SETTING_REENTRY_COOLDOWN_MIN, "60")
    _insert_mech_close("CDT2-USD", direction="LONG", minutes_ago=5)
    assert trade_guards.check_reentry_cooldown("CDT2-USD", "SELL")[0]
    assert trade_guards.check_reentry_cooldown("CDT2-USD", "COVER")[0]
    assert trade_guards.check_reentry_cooldown("CDT2-USD", "SHORT")[0]


def test_cooldown_scade():
    database.set_setting(trade_guards.SETTING_REENTRY_COOLDOWN_MIN, "60")
    _insert_mech_close("CDT3-USD", direction="LONG", minutes_ago=90)
    assert trade_guards.check_reentry_cooldown("CDT3-USD", "BUY")[0]


def test_cooldown_zero_disattiva():
    database.set_setting(trade_guards.SETTING_REENTRY_COOLDOWN_MIN, "0")
    _insert_mech_close("CDT4-USD", direction="LONG", minutes_ago=1)
    assert trade_guards.check_reentry_cooldown("CDT4-USD", "BUY")[0]
    database.set_setting(trade_guards.SETTING_REENTRY_COOLDOWN_MIN, "60")


def test_cooldown_short_bloccato_dopo_stop_su_short():
    database.set_setting(trade_guards.SETTING_REENTRY_COOLDOWN_MIN, "60")
    _insert_mech_close("CDT5-USD", direction="SHORT",
                       exec_type="stop_enforced", minutes_ago=5)
    ok, _ = trade_guards.check_reentry_cooldown("CDT5-USD", "SHORT")
    assert not ok
    assert trade_guards.check_reentry_cooldown("CDT5-USD", "BUY")[0]


def test_trade_ai_non_attiva_cooldown():
    database.set_setting(trade_guards.SETTING_REENTRY_COOLDOWN_MIN, "60")
    _insert_mech_close("CDT6-USD", direction="LONG", exec_type="ai",
                       minutes_ago=5)
    assert trade_guards.check_reentry_cooldown("CDT6-USD", "BUY")[0]
