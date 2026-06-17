"""
Governor deterministico sull'uscita (enforce_stops).

Risolve "entra su trend giusto, rintraccia, il modello non vende sperando, perde
margine o chiude in rosso": (1) lo STOP-LOSS ora SCATTA davvero (chiusura piena al
tocco, su prezzi validati) e (2) un TRAILING alza lo SL sotto il prezzo (ratchet,
mai allentato) per bloccare il profitto. Indipendente dal racconto del modello.
"""
import pytest

import portfolio
import database


@pytest.fixture(autouse=True)
def _enforce_setup():
    # I settings persistono nel DB riusato tra i test: forza i kill-switch ON e
    # il trailing % prima di OGNI test (il test kill-switch li rimette a posto).
    database.set_setting("stop_enforcement_enabled", "true")
    database.set_setting("trailing_stop_enabled", "true")
    database.set_setting("trailing_stop_pct", "4")
    # i test usano BTC-USD (crypto): fisso anche il trail crypto a 4% per la math nota
    database.set_setting("trailing_stop_pct_crypto", "4")


def _open_long(ticker="BTC-USD", qty=2.0, price=1000.0):
    r = portfolio.execute_buy(ticker, qty, price, "g", "t", 80)
    assert r.get("success"), r


def test_hard_stop_fires_full_close_long():
    _open_long(price=1000.0)
    database.update_position_auto_exit("BTC-USD", stop_loss_price=950.0, set_by="agent")
    # prezzo sopra lo SL → niente
    assert portfolio.enforce_stops({"BTC-USD": 970.0}) == []
    assert portfolio.get_position("BTC-USD") is not None
    # prezzo sotto lo SL (oltre il margine) → CHIUSURA PIENA
    done = portfolio.enforce_stops({"BTC-USD": 930.0})
    assert len(done) == 1 and done[0]["ticker"] == "BTC-USD"
    assert portfolio.get_position("BTC-USD") is None


def test_trailing_ratchets_up_and_locks_profit_long():
    _open_long(price=1000.0)  # nessuno SL iniziale
    # sale a 1100 → SL trailing a 1056 (1100 * 0.96)
    portfolio.enforce_stops({"BTC-USD": 1100.0})
    assert abs(float(portfolio.get_position("BTC-USD")["stop_loss_price"]) - 1056.0) < 1e-3
    # sale a 1200 → SL a 1152
    portfolio.enforce_stops({"BTC-USD": 1200.0})
    assert abs(float(portfolio.get_position("BTC-USD")["stop_loss_price"]) - 1152.0) < 1e-3
    # ritraccia a 1140 (< 1152) → scatta lo stop, profitto BLOCCATO (chiuso > entry)
    done = portfolio.enforce_stops({"BTC-USD": 1140.0})
    assert len(done) == 1
    assert portfolio.get_position("BTC-USD") is None
    assert done[0]["fill_price"] > 1000.0   # uscita in profitto, non in perdita


def test_trailing_never_loosens_long():
    _open_long(price=1000.0)
    portfolio.enforce_stops({"BTC-USD": 1200.0})   # SL → 1152
    sl1 = float(portfolio.get_position("BTC-USD")["stop_loss_price"])
    # il prezzo scende ma resta sopra lo SL: lo SL NON si abbassa (ratchet)
    portfolio.enforce_stops({"BTC-USD": 1160.0})
    sl2 = float(portfolio.get_position("BTC-USD")["stop_loss_price"])
    assert sl2 == sl1


def test_short_hard_stop_covers():
    r = portfolio.execute_short("BTC-USD", 2.0, 1000.0, "g", "t", 80)
    assert r.get("success"), r
    database.update_position_auto_exit("BTC-USD", stop_loss_price=1100.0, set_by="agent")
    # short: SL SOPRA l'entry; il prezzo sale oltre → cover (chiusura)
    done = portfolio.enforce_stops({"BTC-USD": 1120.0})
    assert len(done) == 1
    assert portfolio.get_position("BTC-USD") is None


def test_kill_switch_blocks_enforcement():
    _open_long(price=1000.0)
    database.update_position_auto_exit("BTC-USD", stop_loss_price=950.0, set_by="agent")
    database.set_setting("stop_enforcement_enabled", "false")
    database.set_setting("trailing_stop_enabled", "false")
    try:
        assert portfolio.enforce_stops({"BTC-USD": 900.0}) == []
        assert portfolio.get_position("BTC-USD") is not None
    finally:
        database.set_setting("stop_enforcement_enabled", "true")
        database.set_setting("trailing_stop_enabled", "true")
