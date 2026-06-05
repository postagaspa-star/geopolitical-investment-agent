"""
Test del Crypto Airbag — scudo deterministico sopra l'LLM.

Tutto mockato (niente rete/DB reale): verifica la LOGICA di attivazione:
- flag OFF → no-op (non tocca nulla)
- mercato UP/SIDE → non interviene
- DOWNTREND + posizioni crypto → vende tutto (mette in cash)
- DOWNTREND ma gia' in cash → niente da vendere
- bear finito (era deployed) → si ri-arma, NON ricompra
- fail-safe: errore regime → non vende
"""
import sys
import types
import importlib


def _install_fakes(enabled=True, regime="UPTREND", positions=None, state="armed"):
    """Inietta fake per database/portfolio/agents.crypto_regime fetch, e
    monkeypatcha le funzioni del modulo airbag. Ritorna (airbag, calls)."""
    import agents.crypto_airbag as airbag
    importlib.reload(airbag)

    calls = {"sold": [], "settings": {}, "logs": []}

    # fake database
    fake_db = types.SimpleNamespace()
    store = {airbag.SETTING_ENABLED: "true" if enabled else "false",
             airbag.SETTING_STATE: state}

    def get_setting(k, default=None):
        return store.get(k, default)

    def set_setting(k, v):
        store[k] = v
        calls["settings"][k] = v

    def get_positions():
        return positions or []

    def insert_agent_log(rid, phase, content):
        calls["logs"].append((phase, content))

    fake_db.get_setting = get_setting
    fake_db.set_setting = set_setting
    fake_db.get_positions = get_positions
    fake_db.insert_agent_log = insert_agent_log
    sys.modules["database"] = fake_db

    # fake portfolio
    fake_pf = types.SimpleNamespace()

    def execute_sell(ticker, qty, price, geo_reasoning, tech_reasoning, confidence):
        calls["sold"].append({"ticker": ticker, "qty": qty, "price": price})
        return {"success": True}
    fake_pf.execute_sell = execute_sell
    sys.modules["portfolio"] = fake_pf

    # monkeypatch detect_market_regime + _current_price (evita rete)
    airbag.detect_market_regime = lambda: regime
    airbag._current_price = lambda t: 100.0

    return airbag, calls


def test_flag_off_is_noop():
    airbag, calls = _install_fakes(enabled=False, regime="DOWNTREND",
                                   positions=[{"ticker": "BTC-USD", "quantity": 1}])
    res = airbag.run_airbag("t")
    assert res["skipped"] is True
    assert "flag OFF" in res["reason"]
    assert calls["sold"] == []   # NON ha venduto nulla


def test_uptrend_does_not_intervene():
    airbag, calls = _install_fakes(regime="UPTREND",
                                   positions=[{"ticker": "BTC-USD", "quantity": 1}])
    res = airbag.run_airbag("t")
    assert calls["sold"] == []   # mercato sano: non tocca le posizioni dell'LLM


def test_downtrend_sells_all_crypto():
    pos = [{"ticker": "BTC-USD", "quantity": 0.5},
           {"ticker": "ETH-USD", "quantity": 3.0},
           {"ticker": "AAPL", "quantity": 10}]   # equity: NON deve toccarla
    airbag, calls = _install_fakes(regime="DOWNTREND", positions=pos)
    res = airbag.run_airbag("t")
    assert res["action"] == "deployed"
    sold_tickers = {s["ticker"] for s in calls["sold"]}
    assert sold_tickers == {"BTC-USD", "ETH-USD"}   # solo crypto
    assert "AAPL" not in sold_tickers
    assert calls["settings"].get(airbag.SETTING_STATE) == "deployed"


def test_downtrend_already_cash_noop():
    airbag, calls = _install_fakes(regime="DOWNTREND", positions=[])
    res = airbag.run_airbag("t")
    assert res["action"] == "already_cash"
    assert calls["sold"] == []


def test_bear_over_rearms_without_buying():
    # era 'deployed' (airbag scattato), ora il mercato torna UP → si ri-arma
    airbag, calls = _install_fakes(regime="UPTREND", positions=[], state="deployed")
    res = airbag.run_airbag("t")
    assert res["action"] == "rearmed"
    assert calls["settings"].get(airbag.SETTING_STATE) == "armed"
    assert calls["sold"] == []   # NON ricompra: lascia fare all'LLM


def test_regime_none_is_failsafe():
    airbag, calls = _install_fakes(regime="UPTREND",
                                   positions=[{"ticker": "BTC-USD", "quantity": 1}])
    airbag.detect_market_regime = lambda: None   # dati insufficienti
    res = airbag.run_airbag("t")
    assert res["skipped"] is True
    assert calls["sold"] == []   # fail-safe: nel dubbio NON vende
