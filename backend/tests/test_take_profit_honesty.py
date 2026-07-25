"""
Il take-profit non deve promettere cio' che il sistema non fa.

set_take_profit rispondeva SEMPRE "Il take-profit verra' eseguito
automaticamente al raggiungimento". Ma l'unico esecutore di
take_profit_price e' check_and_execute_auto_exits, spento di default dopo
l'incidente NVDA — e quando e' spento esce in silenzio, senza nemmeno un
alert. Risultato: l'agente pianificava un'uscita che nessuno avrebbe onorato,
il target veniva raggiunto e non succedeva nulla. In un sistema che si sveglia
di rado, quei profitti tornavano indietro.

Due correzioni, nessuna delle quali riattiva l'esecuzione automatica (fu
disattivata per una ragione):
  - la risposta dice la verita' sullo stato reale;
  - il target raggiunto diventa un EVENTO interrogabile invece di silenzio.
"""
import json

import pytest

import database
import portfolio


@pytest.fixture(autouse=True)
def _setup():
    database.set_setting("stop_enforcement_enabled", "true")
    database.set_setting("trailing_stop_enabled", "false")   # isola il TP
    database.set_setting("auto_exits_enabled", "false")      # default di prod
    yield
    database.set_setting("trailing_stop_enabled", "true")


def _logs(phase):
    rows = database.get_agent_logs(limit=200) or []
    return [r for r in rows if r.get("phase") == phase]


def test_set_take_profit_non_promette_esecuzione_se_spenta():
    portfolio.execute_buy("TST", 10, 100.0, "g", "t", 80)
    out = portfolio.set_take_profit("TST", 120.0, run_id="test")
    assert out["success"] is True
    assert out["auto_execution_enabled"] is False
    assert "DISATTIVATA" in out["note"]
    assert "verra' eseguito automaticamente al raggiungimento." not in out["note"]


def test_set_take_profit_promette_solo_se_accesa():
    database.set_setting("auto_exits_enabled", "true")
    try:
        portfolio.execute_buy("TST", 10, 100.0, "g", "t", 80)
        out = portfolio.set_take_profit("TST", 120.0, run_id="test")
        assert out["auto_execution_enabled"] is True
        assert "automaticamente" in out["note"]
    finally:
        database.set_setting("auto_exits_enabled", "false")


def test_target_raggiunto_genera_evento_ma_non_chiude():
    portfolio.execute_buy("TST", 10, 100.0, "g", "t", 80)
    database.update_position_auto_exit("TST", take_profit_price=120.0, set_by="test")

    portfolio.enforce_stops({"TST": 110.0})          # sotto il target
    assert _logs("TAKE_PROFIT_REACHED") == []

    portfolio.enforce_stops({"TST": 121.0})          # target raggiunto
    events = _logs("TAKE_PROFIT_REACHED")
    assert len(events) == 1
    payload = json.loads(events[0]["content"])
    assert payload["ticker"] == "TST"
    assert payload["executed"] is False

    # La posizione resta aperta: qui non si esegue nulla, si segnala soltanto.
    assert portfolio.get_position("TST") is not None


def test_short_target_al_ribasso():
    portfolio.execute_short("TST", 10, 100.0, "g", "t", 80)
    database.update_position_auto_exit("TST", take_profit_price=80.0, set_by="test")
    portfolio.enforce_stops({"TST": 90.0})
    assert _logs("TAKE_PROFIT_REACHED") == []
    portfolio.enforce_stops({"TST": 79.0})
    assert len(_logs("TAKE_PROFIT_REACHED")) == 1
    assert portfolio.get_position("TST") is not None


def test_nessun_target_nessun_evento():
    portfolio.execute_buy("TST", 10, 100.0, "g", "t", 80)
    portfolio.enforce_stops({"TST": 500.0})
    assert _logs("TAKE_PROFIT_REACHED") == []
