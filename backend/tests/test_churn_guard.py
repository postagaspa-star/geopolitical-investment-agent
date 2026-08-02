"""
Attrito sul dietrofront e sul ricompro compulsivo.

Prima versione: 13 inversioni sullo stesso titolo in 72h -> guard su flip e
rientri post-stop. Poi la produzione (26-30/07/2026, sistema sbloccato) ha
trovato i due buchi rimasti: il "vendo e ricompro stessa direzione" era
PERMESSO per scelta (ETH venduto e ricomprato in 5 minuti, 80 operazioni in
4 giorni), e lo storico degli short veniva letto al contrario (aperti come
SELL, chiusi come BUY) rendendo cieca la protezione sui flip verso short —
quelli del 29/07 sera, richiusi il 30/07 con ~1.100 EUR di perdita.

Questi test codificano ANCHE i due incidenti, perche' non si ripetano.
"""
from datetime import datetime, timedelta, timezone

import pytest

import churn_guard as cg


NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


def _trade(ticker, action, hours_ago, execution_type="ai", direction="LONG"):
    return {"ticker": ticker, "action": action,
            "timestamp": (NOW - timedelta(hours=hours_ago)).isoformat(),
            "execution_type": execution_type, "direction": direction}


# ── Lettura dello storico: la sola action MENTE sugli short ─────────────────

@pytest.mark.parametrize("action,direction,expected", [
    ("BUY", "LONG", ("open", "LONG")),
    ("SELL", "LONG", ("close", "LONG")),
    ("SELL", "SHORT", ("open", "SHORT")),     # execute_short scrive COSI'
    ("BUY", "SHORT", ("close", "SHORT")),     # execute_cover scrive COSI'
    ("COVER", "SHORT", ("close", "SHORT")),
    ("SHORT", "SHORT", ("open", "SHORT")),
])
def test_classificazione_storico(action, direction, expected):
    row = {"action": action, "direction": direction}
    assert cg.classify_history_row(row) == expected


def test_apertura_short_nello_storico_non_e_una_chiusura():
    """IL BUG DEL 29/07: una riga SELL+SHORT (apertura short) veniva letta
    come 'chiuso un long' e inquinava ogni valutazione."""
    trades = [_trade("AAA", "SELL", 1.0, direction="SHORT")]   # short APERTO
    ok, _ = cg.check_trade("AAA", "BUY", trades, now=NOW)
    assert ok is True, "nessuna chiusura in storia: niente da bloccare"


def test_timestamp_con_spazio_parsato():
    trades = [{"ticker": "AAA", "action": "SELL", "direction": "LONG",
               "timestamp": "2026-07-30 10:00:00", "execution_type": "ai"}]
    ok, why = cg.check_trade("AAA", "BUY", trades, now=NOW)
    assert ok is False and "chiuso 2.0h fa" in why


# ── Le uscite non si bloccano mai ───────────────────────────────────────────

@pytest.mark.parametrize("action", ["SELL", "COVER"])
def test_le_chiusure_passano_sempre(action):
    trades = [_trade("AAA", "SELL", 0.1, "stop_enforced")]
    assert cg.check_trade("AAA", action, trades, now=NOW)[0] is True


# ── Regola 3 (nuova): ricompro a freddo ─────────────────────────────────────

def test_ricompro_in_5_minuti_bloccato():
    """L'INCIDENTE DEL 27/07: ETH venduto alle 6:47 e ricomprato alle 6:52."""
    trades = [_trade("ETH-USD", "SELL", 0.08, "partial_tp")]
    ok, why = cg.check_trade("ETH-USD", "BUY", trades, now=NOW)
    assert ok is False
    assert "riaprirlo" in why


def test_ricompro_dopo_il_cooldown_consentito():
    trades = [_trade("AAA", "SELL", 5.0)]
    assert cg.check_trade("AAA", "BUY", trades, now=NOW)[0] is True


def test_riapertura_short_dopo_cover_recente_bloccata():
    trades = [_trade("AAA", "BUY", 2.0, direction="SHORT")]    # cover 2h fa
    ok, why = cg.check_trade("AAA", "SHORT", trades, now=NOW)
    assert ok is False and "riaprirlo" in why


# ── Regola 1: rientro dopo uno stop ─────────────────────────────────────────

def test_rientro_subito_dopo_uno_stop_bloccato():
    trades = [_trade("AAA", "SELL", 5.0, "stop_enforced")]
    ok, why = cg.check_trade("AAA", "BUY", trades, now=NOW)
    assert ok is False and "stop" in why


def test_stop_su_short_riconosciuto():
    """Prima invisibile: lo stop che copre uno short e' BUY+SHORT."""
    trades = [_trade("AAA", "BUY", 5.0, "stop_enforced", direction="SHORT")]
    ok, why = cg.check_trade("AAA", "SHORT", trades, now=NOW)
    assert ok is False and "stop" in why


def test_rientro_dopo_stop_cooldown_scaduto():
    trades = [_trade("AAA", "SELL", 20.0, "stop_enforced")]
    assert cg.check_trade("AAA", "BUY", trades, now=NOW)[0] is True


# ── Regola 2: ribaltamento ──────────────────────────────────────────────────

def test_inversione_rapida_bloccata():
    trades = [_trade("AAA", "SELL", 6.0)]          # chiuso LONG 6h fa
    ok, why = cg.check_trade("AAA", "SHORT", trades, now=NOW)
    assert ok is False and "inversione" in why


def test_flip_long_dopo_cover_recente_bloccato():
    """IL CASO DEL 29-30/07 in salsa opposta, ora visibile: chiudo lo short
    (BUY+SHORT) e provo subito il long."""
    trades = [_trade("AAA", "BUY", 6.0, direction="SHORT")]
    ok, why = cg.check_trade("AAA", "BUY", trades, now=NOW)
    assert ok is False and "inversione" in why


def test_inversione_dopo_un_giorno_consentita():
    trades = [_trade("AAA", "SELL", 30.0)]
    assert cg.check_trade("AAA", "SHORT", trades, now=NOW)[0] is True


def test_conta_solo_la_chiusura_piu_recente():
    trades = [_trade("AAA", "SELL", 100.0),                     # LONG chiuso 4gg fa
              _trade("AAA", "BUY", 2.0, direction="SHORT")]     # cover 2h fa
    # stessa direzione dell'ultimo chiuso (SHORT): niente flip, ma il
    # ricompro a freddo blocca comunque entro le 4h...
    assert cg.check_trade("AAA", "SHORT", trades, now=NOW)[0] is False
    # ...e dopo 4h la riapertura short e' libera (il LONG di 4 giorni fa
    # non deve bloccare piu' nulla).
    later = NOW + timedelta(hours=3)
    assert cg.check_trade("AAA", "SHORT", trades, now=later)[0] is True


def test_altro_ticker_non_interferisce():
    trades = [_trade("BBB", "SELL", 0.5, "stop_enforced")]
    assert cg.check_trade("AAA", "BUY", trades, now=NOW)[0] is True


# ── Gli scenari reali, come regressione ─────────────────────────────────────

def test_le_13_inversioni_in_72h_sarebbero_state_impedite():
    trades, blocked = [], 0
    direction_long = True
    for i in range(13):
        hours_ago = 72 - i * 5
        closing = "SELL" if direction_long else "COVER"
        d = "LONG" if direction_long else "SHORT"
        trades.append(_trade("NVDA", closing, hours_ago, direction=d))
        next_action = "SHORT" if direction_long else "BUY"
        ok, _ = cg.check_trade("NVDA", next_action, trades,
                               now=NOW - timedelta(hours=hours_ago - 0.5))
        if not ok:
            blocked += 1
        direction_long = not direction_long
    assert blocked >= 11


def test_le_80_operazioni_in_4_giorni_sarebbero_state_dimezzate():
    """Il pattern del 28/07: vendi alle 22:09, ricompra alle 23:19 (SOL),
    vendi alle 23:47, ricompra alle 00:41 (TRX)... ogni ricompro entro 4h
    ora e' bloccato."""
    for gap_h in (0.1, 0.9, 1.2, 3.9):
        trades = [_trade("SOL-USD", "SELL", gap_h)]
        assert cg.check_trade("SOL-USD", "BUY", trades, now=NOW)[0] is False, gap_h


# ── Robustezza e configurazione ─────────────────────────────────────────────

def test_storico_vuoto_consente():
    assert cg.check_trade("AAA", "BUY", [], now=NOW)[0] is True


def test_timestamp_sporchi_ignorati():
    trades = [{"ticker": "AAA", "action": "SELL", "timestamp": "boh"},
              {"ticker": "AAA", "action": "SELL", "timestamp": None}]
    assert cg.check_trade("AAA", "SHORT", trades, now=NOW)[0] is True


def test_timestamp_nel_futuro_ignorato():
    trades = [_trade("AAA", "SELL", -5.0)]
    assert cg.check_trade("AAA", "SHORT", trades, now=NOW)[0] is True


def test_soglie_a_zero_disattivano_tutto():
    trades = [_trade("AAA", "SELL", 0.1, "stop_enforced")]
    ok, _ = cg.check_trade("AAA", "BUY", trades, now=NOW,
                           post_stop_hours=0, flip_hours=0, reopen_hours=0)
    assert ok is True


def test_fail_open_se_il_db_non_risponde(monkeypatch):
    import database

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(database, "get_trades", boom)
    assert cg.check_trade_live("AAA", "BUY", database)[0] is True


def test_flag_spegne_il_guard():
    import database
    database.set_setting(cg.SETTING_ENABLED, "false")
    try:
        assert cg.check_trade_live("AAA", "BUY", database)[0] is True
    finally:
        database.set_setting(cg.SETTING_ENABLED, "true")


def test_setting_reopen_personalizzabile():
    """Col DB vero: un trade appena chiuso blocca il ricompro (default 4h),
    e il setting a 0 riapre la porta senza toccare codice."""
    import database
    import portfolio
    assert portfolio.execute_buy("AAA", 10, 100.0, "g", "t", 80).get("success")
    assert portfolio.execute_sell("AAA", 10, 101.0, "g", "t", 80).get("success")
    ok, why = cg.check_trade_live("AAA", "BUY", database)
    assert ok is False and "riaprirlo" in why
    database.set_setting(cg.SETTING_REOPEN_HOURS, "0")
    try:
        assert cg.check_trade_live("AAA", "BUY", database)[0] is True
    finally:
        database.set_setting(cg.SETTING_REOPEN_HOURS, "")
