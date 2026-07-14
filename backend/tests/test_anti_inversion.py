"""
Test Step 8 (analisi 13/07): isteresi anti-inversione (flag OFF default).

Nelle 3 settimane analizzate: 13 inversioni AI-vs-AI entro 72h su 51
trade AI (es. ETH comprato alle 02:02 e rivenduto alle 02:25). Il guard
richiede una giustificazione esplicita (thesis_invalidation) o
confidence alta per invertire direzione entro la finestra.
"""
import pytest

import db_sqlite
import database
import trade_guards


@pytest.fixture(autouse=True)
def _flag_on_per_test():
    """Il flag e' OFF di default in produzione; nei test lo accendiamo
    esplicitamente (e ripristiniamo OFF alla fine)."""
    database.set_setting(trade_guards.SETTING_ANTI_INVERSION, "true")
    database.set_setting(trade_guards.SETTING_ANTI_INVERSION_HOURS, "12")
    yield
    database.set_setting(trade_guards.SETTING_ANTI_INVERSION, "false")


def _insert_ai_trade(ticker, action="BUY", direction="LONG", hours_ago=1):
    minutes = int(hours_ago * 60)
    with db_sqlite.get_db() as conn:
        conn.execute(
            "INSERT INTO trades (ticker,action,quantity,price,total_value,"
            "geopolitical_reasoning,technical_reasoning,final_decision,"
            "confidence_score,direction,execution_type,timestamp) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now', ?))",
            (ticker, action, 1.0, 100.0, 100.0, "geo", "tech", "d",
             70, direction, "ai", f"-{minutes} minutes"))


GIUSTIFICAZIONE = ("Il breakout sopra resistenza e' fallito con volume in "
                   "calo: la tesi long e' invalidata dal breakdown del supporto.")


def test_inversione_senza_giustificazione_bloccata():
    _insert_ai_trade("AI1-USD", action="BUY", direction="LONG", hours_ago=2)
    ok, why = trade_guards.check_anti_inversion("AI1-USD", "SHORT", 70, None)
    assert not ok and "inversione" in why


def test_inversione_con_thesis_invalidation_passa():
    _insert_ai_trade("AI2-USD", action="BUY", direction="LONG", hours_ago=2)
    ok, _ = trade_guards.check_anti_inversion("AI2-USD", "SHORT", 70,
                                              GIUSTIFICAZIONE)
    assert ok


def test_giustificazione_troppo_corta_non_basta():
    _insert_ai_trade("AI3-USD", action="BUY", direction="LONG", hours_ago=2)
    ok, _ = trade_guards.check_anti_inversion("AI3-USD", "SHORT", 70, "boh")
    assert not ok


def test_confidence_alta_passa():
    _insert_ai_trade("AI4-USD", action="BUY", direction="LONG", hours_ago=2)
    ok, _ = trade_guards.check_anti_inversion("AI4-USD", "SHORT", 85, None)
    assert ok


def test_fuori_finestra_passa():
    _insert_ai_trade("AI5-USD", action="BUY", direction="LONG", hours_ago=20)
    ok, _ = trade_guards.check_anti_inversion("AI5-USD", "SHORT", 70, None)
    assert ok


def test_add_stessa_direzione_passa():
    _insert_ai_trade("AI6-USD", action="BUY", direction="LONG", hours_ago=2)
    ok, _ = trade_guards.check_anti_inversion("AI6-USD", "BUY", 70, None)
    assert ok


def test_chiusure_mai_bloccate():
    _insert_ai_trade("AI7-USD", action="BUY", direction="LONG", hours_ago=2)
    assert trade_guards.check_anti_inversion("AI7-USD", "SELL", 70, None)[0]
    assert trade_guards.check_anti_inversion("AI7-USD", "COVER", 70, None)[0]


def test_chiusura_precedente_non_conta_come_apertura():
    """L'ultimo trade AI e' la CHIUSURA di un long (SELL+LONG): un nuovo
    SHORT non e' un'inversione rispetto a una chiusura."""
    _insert_ai_trade("AI8-USD", action="SELL", direction="LONG", hours_ago=1)
    ok, _ = trade_guards.check_anti_inversion("AI8-USD", "SHORT", 70, None)
    assert ok


def test_short_poi_buy_e_inversione():
    _insert_ai_trade("AI9-USD", action="SELL", direction="SHORT", hours_ago=2)
    ok, _ = trade_guards.check_anti_inversion("AI9-USD", "BUY", 70, None)
    assert not ok


def test_flag_off_no_op():
    database.set_setting(trade_guards.SETTING_ANTI_INVERSION, "false")
    _insert_ai_trade("AI10-USD", action="BUY", direction="LONG", hours_ago=1)
    ok, _ = trade_guards.check_anti_inversion("AI10-USD", "SHORT", 70, None)
    assert ok


def test_trade_meccanico_non_e_trigger():
    """Solo i trade AI contano come 'ultima apertura': una chiusura
    stop_enforced non attiva l'isteresi."""
    with db_sqlite.get_db() as conn:
        conn.execute(
            "INSERT INTO trades (ticker,action,quantity,price,total_value,"
            "geopolitical_reasoning,technical_reasoning,final_decision,"
            "confidence_score,direction,execution_type,timestamp) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now', '-60 minutes'))",
            ("AI11-USD", "BUY", 1.0, 100.0, 100.0, "g", "t", "d",
             None, "LONG", "stop_enforced"))
    ok, _ = trade_guards.check_anti_inversion("AI11-USD", "SHORT", 70, None)
    assert ok
