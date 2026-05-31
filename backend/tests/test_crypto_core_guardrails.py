"""
Test di INTEGRAZIONE su decision_crypto._compute_core_guardrails — il punto in
cui il core deterministico si innesta nel Decision Crypto (flag core_bound).

Tutto mockato: nessuna rete, nessun LLM, nessun DB reale. Verifica:
  - VETO quando il core blocca (es. trade contro-trend in regime bear);
  - RIDUZIONE size = min(R1, core), mai aumento;
  - lo SL del core diventa vincolante;
  - il log guardrails contiene snapshot + core_version + provenance (forense);
  - ASTENSIONE (allowed, nessuna modifica) quando gli indicatori mancano.

Eseguito in modo sincrono via asyncio.run() (no dipendenza da pytest-asyncio).
"""
import asyncio

import agents.decision_crypto as dc
from agents import crypto_signal_core as core


class _FakeTechCrypto:
    """Stand-in per agents.technical_crypto con _fetch_crypto_indicators."""
    def __init__(self, indicators):
        self._ind = indicators

    async def _fetch_crypto_indicators(self, ticker):
        return dict(self._ind, ticker=ticker)


def _install_mocks(monkeypatch, *, indicators, nav=100_000.0,
                   btc_closes=None, profile=None):
    import sys
    import types
    import data_fetchers
    import portfolio
    import risk_profile

    # technical_crypto._fetch_crypto_indicators (import locale dentro la funzione)
    fake_tc = types.ModuleType("agents.technical_crypto")
    fake = _FakeTechCrypto(indicators)
    fake_tc._fetch_crypto_indicators = fake._fetch_crypto_indicators
    monkeypatch.setitem(sys.modules, "agents.technical_crypto", fake_tc)

    # data_fetchers.fetch_market_data → chiusure BTC per il regime
    closes = btc_closes if btc_closes is not None else [100 + i for i in range(200)]
    monkeypatch.setattr(data_fetchers, "fetch_market_data",
                        lambda t, d: {"data": [{"close": c} for c in closes]})

    # portfolio.get_portfolio_state → NAV
    monkeypatch.setattr(portfolio, "get_portfolio_state",
                        lambda: {"total_value": nav, "cash": nav, "positions": []})

    # risk_profile.get_active_profile → soglie
    prof = profile or {
        "sl_min_pct_crypto": 12.0, "sl_max_pct_crypto": 25.0,
        "max_position_pct_crypto": 12.0, "min_confidence": 0.55,
    }
    monkeypatch.setattr(risk_profile, "get_active_profile", lambda: prof)


def _bull_indicators():
    return {
        "current_price": 100.0, "atr": 3.0,
        "signal_count": {"bullish": 4, "bearish": 0},
        "signal": "BUY", "trend": "TRENDING_UP", "rsi_14": 33,
        "candlestick_setup": "bullish hammer", "support": 96, "resistance": 130,
        "ema50": 99, "ema200": 95,
    }


def test_guardrails_allow_and_reduce_size(monkeypatch):
    _install_mocks(monkeypatch, indicators=_bull_indicators(), nav=100_000,
                   btc_closes=[100 + i for i in range(200)])  # regime bull
    # R1 propone una size enorme: 100 unità; il core deve ridurla
    out = asyncio.run(dc._compute_core_guardrails(
        "run1", "BTC-USD", "long", 100.0, llm_units=100.0, llm_sl=None))
    assert out is not None
    assert out["allowed"] is True
    assert out["size_units"] <= 100.0           # mai aumentata
    assert out["stop_loss"] and out["stop_loss"] < 100.0  # SL long sotto entry
    log = out["log"]
    assert log["core_version"] == core.CORE_VERSION
    assert "snapshot" in log and log["snapshot"]["ticker"] == "BTC-USD"
    assert isinstance(log["factors"], list) and len(log["factors"]) > 0


def test_guardrails_veto_counter_trend(monkeypatch):
    # setup long, ma regime BEAR e conviction non altissima → core VETO
    _install_mocks(
        monkeypatch,
        indicators={"current_price": 100.0, "atr": 3.0,
                    "signal_count": {"bullish": 3, "bearish": 1},
                    "signal": "BUY", "trend": "TRENDING_UP"},
        btc_closes=[300 - i for i in range(200)],  # regime bear
    )
    out = asyncio.run(dc._compute_core_guardrails(
        "run2", "SOL-USD", "long", 100.0, llm_units=5.0, llm_sl=None))
    assert out is not None
    assert out["allowed"] is False
    assert out["size_units"] == 0.0


def test_guardrails_abstain_when_no_indicators(monkeypatch):
    # indicatori in errore → astensione (allowed, nessuna modifica), MAI veto
    _install_mocks(monkeypatch, indicators={"error": "no data"})
    out = asyncio.run(dc._compute_core_guardrails(
        "run3", "DOGE-USD", "long", 50.0, llm_units=50.0, llm_sl=None))
    assert out is not None
    assert out["allowed"] is True
    assert out["size_units"] is None  # nessuna modifica alla proposta R1


def test_guardrails_snapshot_replay_matches(monkeypatch):
    # la decisione loggata dev'essere riproducibile dallo snapshot salvato
    _install_mocks(monkeypatch, indicators=_bull_indicators(), nav=100_000,
                   btc_closes=[100 + i for i in range(200)])
    out = asyncio.run(dc._compute_core_guardrails(
        "run4", "BTC-USD", "long", 100.0, llm_units=100.0, llm_sl=None))
    snap = out["log"]["snapshot"]
    restored = core.replay(snap)
    assert restored.action == "BUY"
    assert restored.stop_loss == out["stop_loss"]
