"""
#11 — NAV mark-to-market degli SHORT nei backtest.

Prima il NAV di una posizione short usava cash + units*(2*entry - price), che
aggiungeva un units*entry spurio (~un intero notional): curva traslata in alto e
max drawdown degli short falsato. La convenzione corretta e' cash-neutral
(coerente con la chiusura): cash + units*(entry - price).

Test deterministico via backtest_regime, con `step` monkeypatchato per forzare
uno short a prezzo PIATTO: un tale short non guadagna ne' perde (solo fee), quindi
il drawdown deve essere ~0. Col vecchio bug la curva parte a ~2x il NAV e poi
"crolla" al valore realizzato corretto alla chiusura → max_dd ~-50% (test rosso).
"""
import types

from simulator import backtest_regime as br


def test_regime_short_nav_is_cash_neutral(monkeypatch):
    p = br.RegimeParams()
    warm = p.ema_slow + 6
    n = warm + 10
    # Prezzo costante a 100 per tutta la serie.
    bars = [{"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0,
             "volume": 1.0} for _ in range(n)]

    calls = {"k": 0}

    def fake_step(st, window, nav=0.0, params=None):
        # Apre UNO short full-notional alla prima barra utile, poi HOLD.
        calls["k"] += 1
        if calls["k"] == 1:
            return types.SimpleNamespace(action="OPEN_SHORT", units=1000.0)
        return types.SimpleNamespace(action="HOLD", units=0.0)

    monkeypatch.setattr(br, "step", fake_step)

    res = br.run_regime_backtest(bars, params=p, slippage_bps=0.0,
                                 funding_daily_pct=0.0)

    # Short full-notional (1000 * 100 = 100k = INITIAL_NAV) tenuto a prezzo
    # piatto: profitto nullo, solo le fee. Quindi:
    #  - drawdown ~0 (col vecchio 2*entry-price sarebbe ~-50%);
    #  - return ~ -0.2% (sole commissioni apertura+chiusura, 10bps/lato).
    assert res["max_drawdown_pct"] >= -5.0, res
    assert -2.0 <= res["return_pct"] <= 0.5, res
