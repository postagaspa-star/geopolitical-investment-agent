"""
TP/SL PARZIALI a ladder (position_exit_orders).

Verifica il motore: piu' livelli per posizione, ognuno con la sua quantita';
al trigger chiude SOLO quella quantita' (chiusura parziale) e lascia attivi gli
altri livelli; cap per-kind sulla quantita' posseduta; kill-switch; ordini
orfani cancellati; direzione SHORT corretta.
"""
import pytest

import portfolio
import database


@pytest.fixture(autouse=True)
def _partial_exits_setup(monkeypatch):
    # _refresh_current_price puo' fare fetch di rete: nei test usa il fallback
    # (il prezzo passato), cosi' la sanity +-50% e' relativa all'entry ed e'
    # deterministica e offline.
    monkeypatch.setattr(portfolio, "_refresh_current_price",
                        lambda ticker, fallback=0: float(fallback or 0))
    # I settings persistono nel DB riusato (anche tra run di pytest) e NON sono
    # azzerati da reset_portfolio_data: forza il kill-switch ON prima di OGNI
    # test, altrimenti un 'false' lasciato da kill_switch inquina gli altri.
    database.set_setting("partial_exits_enabled", "true")


def _open_long(ticker="BTC-USD", qty=2.0, price=1000.0):
    r = portfolio.execute_buy(ticker, qty, price, "geo", "tech", 80)
    assert r.get("success"), r
    return r


def test_add_partial_exit_caps_on_position_and_per_kind():
    _open_long(qty=2.0, price=1000.0)
    # qty > posizione → rifiutato
    assert not portfolio.add_partial_exit("BTC-USD", "TP", 1100, 3.0)["success"]
    # due TP da 1 = 2 (ok), il terzo da 0.5 sfora il per-kind
    assert portfolio.add_partial_exit("BTC-USD", "TP", 1100, 1.0)["success"]
    assert portfolio.add_partial_exit("BTC-USD", "TP", 1200, 1.0)["success"]
    assert not portfolio.add_partial_exit("BTC-USD", "TP", 1300, 0.5)["success"]
    # ma uno SL puo' coprire tutta la posizione (cap e' per-kind)
    assert portfolio.add_partial_exit("BTC-USD", "SL", 900, 2.0)["success"]


def test_ladder_partial_trigger_closes_only_that_level():
    _open_long(qty=2.0, price=1000.0)
    portfolio.add_partial_exit("BTC-USD", "TP", 1100, 1.0)   # livello 1
    portfolio.add_partial_exit("BTC-USD", "TP", 1200, 1.0)   # livello 2
    # prezzo 1150: scatta solo il TP@1100 (1150 >= 1100*1.01), non il TP@1200
    done = portfolio.check_and_execute_partial_exits({"BTC-USD": 1150.0})
    assert len(done) == 1 and done[0]["qty"] == 1.0
    pos = portfolio.get_position("BTC-USD")
    assert float(pos["quantity"]) == 1.0                     # chiusura PARZIALE
    active = database.get_active_exit_orders("BTC-USD")
    assert len(active) == 1 and float(active[0]["trigger_price"]) == 1200.0


def test_second_level_triggers_later():
    _open_long(qty=2.0, price=1000.0)
    portfolio.add_partial_exit("BTC-USD", "TP", 1100, 1.0)
    portfolio.add_partial_exit("BTC-USD", "TP", 1200, 1.0)
    portfolio.check_and_execute_partial_exits({"BTC-USD": 1150.0})   # chiude liv.1
    # ora il prezzo sale a 1250: scatta anche il livello 2 → posizione a 0
    done = portfolio.check_and_execute_partial_exits({"BTC-USD": 1250.0})
    assert len(done) == 1
    assert portfolio.get_position("BTC-USD") is None
    assert database.get_active_exit_orders("BTC-USD") == []


def test_kill_switch_blocks_execution():
    _open_long(qty=2.0, price=1000.0)
    portfolio.add_partial_exit("BTC-USD", "TP", 1100, 1.0)
    database.set_setting("partial_exits_enabled", "false")
    try:
        done = portfolio.check_and_execute_partial_exits({"BTC-USD": 1150.0})
        assert done == []
        assert float(portfolio.get_position("BTC-USD")["quantity"]) == 2.0
    finally:
        # i settings persistono tra i test (a differenza di portfolio/posizioni):
        # ripristina il flag, altrimenti i test successivi trovano il kill-switch.
        database.set_setting("partial_exits_enabled", "true")


def test_orphan_order_cancelled_when_position_gone():
    _open_long(qty=2.0, price=1000.0)
    portfolio.add_partial_exit("BTC-USD", "SL", 900, 2.0)
    # chiusura manuale totale della posizione
    portfolio.execute_sell("BTC-USD", 2.0, 1000.0, "geo", "tech", 80)
    assert portfolio.get_position("BTC-USD") is None
    portfolio.check_and_execute_partial_exits({"BTC-USD": 850.0})
    assert database.get_active_exit_orders("BTC-USD") == []   # ordine orfano cancellato


def test_short_take_profit_triggers_below():
    r = portfolio.execute_short("BTC-USD", 2.0, 1000.0, "geo", "tech", 80)
    assert r.get("success"), r
    # SHORT: TP sotto l'entry (profitto se scende)
    assert portfolio.add_partial_exit("BTC-USD", "TP", 900, 1.0)["success"]
    done = portfolio.check_and_execute_partial_exits({"BTC-USD": 850.0})
    assert len(done) == 1 and done[0]["qty"] == 1.0
    pos = portfolio.get_position("BTC-USD")
    assert pos is not None and float(pos["quantity"]) == 1.0


# ── Wiring AI: l'azione della chat con 'quantity' crea un ordine parziale ──
def test_chat_action_quantity_creates_partial_order():
    import main
    _open_long("BTC-USD", qty=2.0, price=1000.0)
    res = main._exec_action_set_take_profit({
        "ticker": "BTC-USD", "take_profit_price": 1100.0,
        "quantity": 1.0, "reasoning": "scale out su 1 BTC",
    })
    assert res.get("ok") and res.get("partial") is True
    orders = database.get_active_exit_orders("BTC-USD")
    assert len(orders) == 1
    assert orders[0]["kind"] == "TP" and float(orders[0]["quantity"]) == 1.0


def test_chat_action_without_quantity_stays_whole_position():
    import main
    _open_long("BTC-USD", qty=2.0, price=1000.0)
    res = main._exec_action_set_take_profit({
        "ticker": "BTC-USD", "take_profit_price": 1100.0, "reasoning": "tutto",
    })
    assert res.get("ok") and not res.get("partial")
    # nessun ordine ladder: il TP classico resta sulla colonna singola
    assert database.get_active_exit_orders("BTC-USD") == []
    assert float(portfolio.get_position("BTC-USD").get("take_profit_price") or 0) == 1100.0
