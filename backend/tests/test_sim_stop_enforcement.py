"""
Test Step 5 (analisi 13/07): stop_loss_target numerico + enforcement
pre-step nel Simulator (flag sim_stop_enforcement_enabled, OFF default).

Prima del fix gli exit plan erano solo testo: ~meta' degli stop dichiarati
toccati non produceva chiusure — il Sim validava un agente senza la rete
di protezione che il Live ha.
"""
import pytest

from simulator import db as sim_db
from simulator.v2_engine import (
    apply_trades, enforce_sim_stops, make_initial_portfolio,
    SETTING_SIM_STOP_ENFORCEMENT, _parse_response,
)


@pytest.fixture
def flag_on():
    sim_db.set_setting(SETTING_SIM_STOP_ENFORCEMENT, "true")
    yield
    sim_db.set_setting(SETTING_SIM_STOP_ENFORCEMENT, "false")


@pytest.fixture(autouse=True)
def flag_off_default():
    # il DB dei test persiste: garantisci lo stato di partenza OFF
    sim_db.set_setting(SETTING_SIM_STOP_ENFORCEMENT, "false")
    yield


def _pf_with_long(stop=90.0):
    pf = make_initial_portfolio(100000)
    res = apply_trades(pf, [{
        "action": "BUY", "asset": "BTC-USD", "allocation_pct": 20,
        "conviction": "ALTA", "thesis": "t", "stop_loss_target": stop,
    }], {"BTC-USD": 100.0})
    return res["portfolio"]


def test_stop_salvato_sulla_posizione():
    pf = _pf_with_long(stop=90.0)
    pos = pf["positions"][0]
    assert pos["stop_loss_target"] == 90.0


def test_flag_off_non_tocca_nulla():
    pf = _pf_with_long(stop=90.0)
    new_pf, forced = enforce_sim_stops(pf, {"BTC-USD": 80.0})
    assert forced == []
    assert new_pf is pf
    assert len(new_pf["positions"]) == 1


def test_long_stop_crossato_chiude_intera_posizione(flag_on):
    pf = _pf_with_long(stop=90.0)
    qty = pf["positions"][0]["quantity"]
    cash_before = pf["cash"]
    new_pf, forced = enforce_sim_stops(pf, {"BTC-USD": 85.0})
    assert len(new_pf["positions"]) == 0
    assert len(forced) == 1
    assert forced[0]["status"] == "executed_stop_loss_sim"
    assert forced[0]["executed_qty"] == qty
    # cash accreditato: qty * 85 meno la fee
    assert new_pf["cash"] > cash_before
    assert new_pf["cash"] < cash_before + qty * 85.0  # fee sottratta
    assert new_pf["total_commissions_paid"] > pf["total_commissions_paid"] - 1e-9


def test_long_stop_non_crossato_intatto(flag_on):
    pf = _pf_with_long(stop=90.0)
    new_pf, forced = enforce_sim_stops(pf, {"BTC-USD": 95.0})
    assert forced == []
    assert len(new_pf["positions"]) == 1


def test_short_stop_crossato_al_rialzo(flag_on):
    pf = make_initial_portfolio(100000)
    res = apply_trades(pf, [{
        "action": "SELL", "asset": "ETH-USD", "allocation_pct": 15,
        "conviction": "ALTA", "thesis": "short",
        "stop_loss_target": 110.0,
    }], {"ETH-USD": 100.0})
    pf = res["portfolio"]
    assert pf["positions"][0]["side"] == "short"
    new_pf, forced = enforce_sim_stops(pf, {"ETH-USD": 115.0})
    assert len(new_pf["positions"]) == 0
    assert forced[0]["status"] == "executed_stop_loss_sim"


def test_posizione_senza_stop_intatta(flag_on):
    pf = _pf_with_long(stop=None)
    assert pf["positions"][0].get("stop_loss_target") is None
    new_pf, forced = enforce_sim_stops(pf, {"BTC-USD": 10.0})
    assert forced == []
    assert len(new_pf["positions"]) == 1


def test_force_close_qty_esatta_no_residuo_short():
    """_force_close chiude la qty esatta anche se la posizione vale piu'
    del 30% del NAV (niente flip long->short da arrotondamento)."""
    pf = make_initial_portfolio(100000)
    pf = apply_trades(pf, [{
        "action": "BUY", "asset": "SOL-USD", "allocation_pct": 30,
        "conviction": "ALTA", "thesis": "t", "stop_loss_target": 50.0,
    }], {"SOL-USD": 100.0})["portfolio"]
    res = apply_trades(pf, [{
        "action": "SELL", "asset": "SOL-USD", "allocation_pct": 0,
        "_force_close": True,
    }], {"SOL-USD": 40.0})
    assert len(res["portfolio"]["positions"]) == 0
    assert res["applied_trades"][0]["status"] == "executed_close_long"


def test_parser_normalizza_stop_loss_target():
    raw = '''[3] DECISIONE
    {"trades": [{"action": "BUY", "asset": "BTC-USD", "allocation_pct": 20,
                 "conviction": "ALTA", "thesis": "t", "rr": "2",
                 "exit_plan": "stop sotto 90", "stop_loss_target": 90.5}],
     "hold_summary": ""}'''
    parsed = _parse_response(raw)
    assert parsed["trades"][0]["stop_loss_target"] == 90.5

    raw_bad = raw.replace("90.5", '"boh"')
    parsed = _parse_response(raw_bad)
    assert parsed["trades"][0]["stop_loss_target"] is None
