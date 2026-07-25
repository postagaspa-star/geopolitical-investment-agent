"""
Attrito sul dietrofront: 13 inversioni sullo stesso titolo in 72h su 51
operazioni (una su quattro) sono churn, non decisioni.

Il guard NON impedisce di operare — il sistema soffre gia' di troppi veti
sull'entrata. Impedisce due gesti specifici e patologici su orizzonte swing:
rientrare su un nome che ti ha appena stoppato, e aprire nella direzione
opposta a quella appena chiusa. Le uscite passano SEMPRE.
"""
from datetime import datetime, timedelta, timezone

import pytest

import churn_guard as cg


NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)


def _trade(ticker, action, hours_ago, execution_type="ai"):
    return {"ticker": ticker, "action": action,
            "timestamp": (NOW - timedelta(hours=hours_ago)).isoformat(),
            "execution_type": execution_type}


# ── Le uscite non si bloccano mai ───────────────────────────────────────────

@pytest.mark.parametrize("action", ["SELL", "COVER"])
def test_le_chiusure_passano_sempre(action):
    trades = [_trade("AAA", "SELL", 0.5, "stop_enforced")]
    ok, _ = cg.check_trade("AAA", action, trades, now=NOW)
    assert ok is True, "un guard non deve mai poter impedire di uscire"


# ── Rientro dopo uno stop ───────────────────────────────────────────────────

def test_rientro_subito_dopo_uno_stop_bloccato():
    trades = [_trade("AAA", "SELL", 1.0, "stop_enforced")]
    ok, why = cg.check_trade("AAA", "BUY", trades, now=NOW)
    assert ok is False
    assert "stop" in why


def test_rientro_dopo_il_cooldown_consentito():
    trades = [_trade("AAA", "SELL", 20.0, "stop_enforced")]
    ok, _ = cg.check_trade("AAA", "BUY", trades, now=NOW)
    assert ok is True


def test_chiusura_normale_non_blocca_il_rientro_stessa_direzione():
    """Una presa di profitto non e' un'invalidazione della tesi."""
    trades = [_trade("AAA", "SELL", 1.0, "ai")]
    ok, _ = cg.check_trade("AAA", "BUY", trades, now=NOW)
    assert ok is True


# ── Ribaltamento di direzione ───────────────────────────────────────────────

def test_inversione_rapida_bloccata():
    trades = [_trade("AAA", "SELL", 2.0)]          # chiuso un LONG 2h fa
    ok, why = cg.check_trade("AAA", "SHORT", trades, now=NOW)
    assert ok is False
    assert "inversione" in why


def test_inversione_dopo_un_giorno_consentita():
    trades = [_trade("AAA", "SELL", 30.0)]
    ok, _ = cg.check_trade("AAA", "SHORT", trades, now=NOW)
    assert ok is True


def test_inversione_da_short_a_long_bloccata():
    trades = [_trade("AAA", "COVER", 3.0)]         # chiuso uno SHORT
    ok, _ = cg.check_trade("AAA", "BUY", trades, now=NOW)
    assert ok is False


def test_altro_ticker_non_interferisce():
    trades = [_trade("BBB", "SELL", 1.0, "stop_enforced")]
    ok, _ = cg.check_trade("AAA", "BUY", trades, now=NOW)
    assert ok is True


def test_conta_solo_la_chiusura_piu_recente():
    """Una vecchia chiusura opposta non deve bloccare in eterno."""
    trades = [_trade("AAA", "SELL", 100.0),        # LONG chiuso 4 giorni fa
              _trade("AAA", "COVER", 2.0)]         # SHORT chiuso 2h fa
    ok, _ = cg.check_trade("AAA", "SHORT", trades, now=NOW)
    assert ok is True, "l'ultima direzione chiusa era SHORT: riaprirlo non e' un'inversione"


# ── Lo scenario reale ───────────────────────────────────────────────────────

def test_le_13_inversioni_in_72h_sarebbero_state_impedite():
    """Sequenza di dietrofront ravvicinati sullo stesso nome."""
    trades, blocked = [], 0
    direction_long = True
    for i in range(13):
        hours_ago = 72 - i * 5                      # una ogni ~5 ore
        closing = "SELL" if direction_long else "COVER"
        trades.append(_trade("NVDA", closing, hours_ago))
        next_action = "SHORT" if direction_long else "BUY"
        ok, _ = cg.check_trade("NVDA", next_action, trades,
                               now=NOW - timedelta(hours=hours_ago - 0.5))
        if not ok:
            blocked += 1
        direction_long = not direction_long
    assert blocked >= 11, f"attese quasi tutte bloccate, bloccate {blocked}/13"


# ── Robustezza ──────────────────────────────────────────────────────────────

def test_storico_vuoto_consente():
    assert cg.check_trade("AAA", "BUY", [], now=NOW)[0] is True


def test_timestamp_sporchi_ignorati():
    trades = [{"ticker": "AAA", "action": "SELL", "timestamp": "boh"},
              {"ticker": "AAA", "action": "SELL", "timestamp": None}]
    assert cg.check_trade("AAA", "SHORT", trades, now=NOW)[0] is True


def test_timestamp_nel_futuro_ignorato():
    trades = [_trade("AAA", "SELL", -5.0)]
    assert cg.check_trade("AAA", "SHORT", trades, now=NOW)[0] is True


def test_soglie_a_zero_disattivano_l_attrito():
    trades = [_trade("AAA", "SELL", 0.1, "stop_enforced")]
    ok, _ = cg.check_trade("AAA", "BUY", trades, now=NOW,
                           post_stop_hours=0, flip_hours=0)
    assert ok is True


def test_fail_open_se_il_db_non_risponde(monkeypatch):
    """Impone disciplina, non e' un governatore di sicurezza: in caso di
    guasto non deve congelare l'operativita'."""
    import database

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(database, "get_trades", boom)
    ok, why = cg.check_trade_live("AAA", "BUY", database)
    assert ok is True and why == ""


def test_flag_spegne_il_guard():
    import database
    database.set_setting(cg.SETTING_ENABLED, "false")
    try:
        assert cg.is_enabled(database) is False
        assert cg.check_trade_live("AAA", "BUY", database)[0] is True
    finally:
        database.set_setting(cg.SETTING_ENABLED, "true")
