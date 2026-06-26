"""
test_btc_regime_band.py — Step 5 della pulizia: la banda morta del regime BTC e'
un PARAMETRO. Con la banda larga (strict, setting btc_regime_strict_trend='on')
un BTC appena ~2% sopra l'EMA200 e' "neutral", non "bull": cosi' uno SHORT su un
alt in downtrend confermato non viene piu' bloccato contro-trend quando BTC e'
solo piatto-leggermente-su. Default ±1% = comportamento storico invariato.
"""
from agents.crypto_signal_core import classify_btc_regime
from agents import decision_crypto as dc
import database


def test_default_band_is_historical_1pct():
    closes = [100.0] * 199 + [102.0]  # BTC ~2% sopra EMA200
    # default e ±1% esplicito danno lo stesso (bull): nessuna regressione
    assert classify_btc_regime(closes)[0] == "bull"
    assert classify_btc_regime(closes, 0.01)[0] == "bull"


def test_wide_band_demotes_marginal_bull_to_neutral():
    closes = [100.0] * 199 + [102.0]  # ~2% sopra EMA200: NON e' un trend genuino
    assert classify_btc_regime(closes, 0.03)[0] == "neutral"


def test_wide_band_keeps_genuine_trend_bull():
    closes = [100.0] * 199 + [108.0]  # ~8% sopra EMA200: trend vero
    assert classify_btc_regime(closes, 0.03)[0] == "bull"


def test_wide_band_demotes_marginal_bear_to_neutral():
    closes = [100.0] * 199 + [98.0]   # ~2% sotto EMA200
    assert classify_btc_regime(closes, 0.01)[0] == "bear"
    assert classify_btc_regime(closes, 0.03)[0] == "neutral"


def test_band_helper_reads_setting(monkeypatch):
    monkeypatch.setattr(database, "get_setting",
                        lambda k, d=None: "on" if k == "btc_regime_strict_trend" else d,
                        raising=False)
    assert dc._btc_regime_band_pct() == 0.03
    monkeypatch.setattr(database, "get_setting",
                        lambda k, d=None: "off" if k == "btc_regime_strict_trend" else d,
                        raising=False)
    assert dc._btc_regime_band_pct() == 0.01


def test_detail_text_reflects_band():
    closes = [100.0] * 199 + [100.5]  # entro banda
    assert "±1%" in classify_btc_regime(closes, 0.01)[1]
    assert "±3%" in classify_btc_regime(closes, 0.03)[1]
