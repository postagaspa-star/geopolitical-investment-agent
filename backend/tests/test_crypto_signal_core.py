"""
Test del Crypto Signal Core — il nucleo deterministico del Decision Crypto.

Copre: EMA, regime BTC, confluence (bull/bear/neutral + fail-closed sui dati
mancanti), sizing ATR (risk-based + cap + clamp SL), conviction, e i 3 gate
della decisione (conviction minima, filtro regime contro-trend, SL obbligatorio).
"""
import math

from agents.crypto_signal_core import (
    CoreParams, CoreDecision, ema, classify_btc_regime, score_confluence,
    position_size_atr, compute_conviction, decide,
    core_inputs_from_analysis, reconcile,
    snapshot_for_replay, replay, CORE_VERSION,
)


# ─── EMA ──────────────────────────────────────────────────────────────────────

def test_ema_insufficient_data_returns_none():
    assert ema([1, 2, 3], 200) is None
    assert ema([], 10) is None


def test_ema_constant_series_equals_value():
    assert abs(ema([100.0] * 50, 200 if False else 10) - 100.0) < 1e-9


def test_ema_ignores_nan_and_none():
    # valori sporchi non devono far esplodere
    val = ema([10, None, 10, float("nan"), 10, 10, 10, 10, 10, 10], 5)
    assert val is not None and abs(val - 10.0) < 1e-6


# ─── Regime BTC ───────────────────────────────────────────────────────────────

def test_regime_unknown_when_too_few_closes():
    regime, _ = classify_btc_regime([100] * 10)
    assert regime == "unknown"


def test_regime_bull_when_price_above_ema200():
    # 200 chiusure in trend rialzista forte → prezzo > EMA200
    closes = [100 + i for i in range(200)]
    regime, detail = classify_btc_regime(closes)
    assert regime == "bull"
    assert "EMA200" in detail


def test_regime_bear_when_price_below_ema200():
    closes = [100 - i * 0.4 for i in range(200)]  # discesa costante
    regime, _ = classify_btc_regime(closes)
    assert regime == "bear"


def test_regime_low_confidence_between_50_and_200():
    closes = [100 + i for i in range(60)]
    regime, detail = classify_btc_regime(closes)
    assert regime == "bull"
    assert "bassa confidenza" in detail


# ─── Confluence ───────────────────────────────────────────────────────────────

def test_confluence_pure_bull():
    details = {
        "signal_count": {"bullish": 4, "bearish": 1},
        "signal": "BUY",
        "trend": "TRENDING_UP",
        "rsi_14": 30,                 # oversold → bull
        "candlestick_setup": "bullish_hammer at fib",
    }
    c = score_confluence(details)
    assert c["bull"] > 0
    assert c["bear"] == 0
    assert c["net"] > 0
    assert c["present"] == 5


def test_confluence_funding_is_contrarian():
    # funding bullish_overcrowded deve contare come BEAR (rischio long squeeze)
    c = score_confluence({"funding_bias": "bullish_overcrowded"})
    assert c["bear"] > 0 and c["bull"] == 0
    c2 = score_confluence({"funding_bias": "bearish_overcrowded"})
    assert c2["bull"] > 0 and c2["bear"] == 0


def test_confluence_fail_closed_on_missing_data():
    # input vuoto: nessun fattore inventato
    c = score_confluence({})
    assert c["present"] == 0
    assert c["bull"] == 0 and c["bear"] == 0
    assert c["completeness"] == 0.0
    assert c["factors"] == []


def test_confluence_neutral_funding_not_counted():
    c = score_confluence({"funding_bias": "neutral"})
    assert c["present"] == 0  # neutral non e' un segnale


def test_confluence_ema_stack_requires_all_three():
    # senza price non si valuta lo stack
    c = score_confluence({}, raw={"ema50": 100, "ema200": 90})
    assert all(f.name != "ema_stack" for f in c["factors"])
    # con price+ema50+ema200 allineati bull
    c2 = score_confluence({"current_price": 110}, raw={"ema50": 100, "ema200": 90})
    assert any(f.name == "ema_stack" and f.direction == "bull" for f in c2["factors"])


# ─── Sizing ATR ───────────────────────────────────────────────────────────────

def test_sizing_risk_based_units():
    # NAV 100k, rischio 1%/trade = $1000. ATR 1000, k=1.5 → SL dist 1500 (1.5%)
    # ma il profilo crypto ha sl_min 12% → clamp a 12% → SL dist 12000
    # units = 1000 / 12000 = 0.0833...
    p = CoreParams(risk_per_trade_pct=1.0, k_atr_sl=1.5,
                   sl_min_pct=12.0, sl_max_pct=25.0, max_position_pct_nav=50.0)
    r = position_size_atr(nav=100_000, entry=100_000, atr=1000, side="long", params=p)
    assert r["sl_pct"] == 12.0          # clampato al minimo
    assert abs(r["units"] - (1000.0 / 12000.0)) < 1e-6
    assert r["stop_loss"] == 88_000.0   # 100k - 12%


def test_sizing_long_sl_below_short_sl_above():
    p = CoreParams(sl_min_pct=10, sl_max_pct=10)  # forza SL 10%
    long = position_size_atr(nav=100_000, entry=1000, atr=0, side="long", params=p)
    short = position_size_atr(nav=100_000, entry=1000, atr=0, side="short", params=p)
    assert long["stop_loss"] == 900.0
    assert short["stop_loss"] == 1100.0


def test_sizing_capped_by_max_position_pct():
    # rischio enorme forzerebbe size enorme → deve cappare al max_position_pct
    p = CoreParams(risk_per_trade_pct=50.0, sl_min_pct=2.0, sl_max_pct=50.0,
                   k_atr_sl=1.0, max_position_pct_nav=12.0)
    r = position_size_atr(nav=100_000, entry=100, atr=2, side="long", params=p)
    assert r["capped"] is True
    # notional <= 12% NAV → units <= 12000/100 = 120
    assert r["units"] <= 120.0 + 1e-6
    assert r["size_pct_nav"] <= 12.0 + 1e-6


def test_sizing_reward_risk_positive():
    p = CoreParams(k_atr_sl=1.5, k_atr_tp=3.0, sl_min_pct=0.1, sl_max_pct=100)
    r = position_size_atr(nav=100_000, entry=1000, atr=10, side="long", params=p)
    assert r["reward_risk"] is not None and r["reward_risk"] > 1.0


def test_sizing_invalid_inputs_zero():
    p = CoreParams()
    r = position_size_atr(nav=0, entry=1000, atr=10, side="long", params=p)
    assert r["units"] == 0.0 and r["stop_loss"] is None


# ─── Conviction ───────────────────────────────────────────────────────────────

def test_conviction_zero_when_no_signals():
    assert compute_conviction({"total_weight": 0, "net": 0}, 0.0) == 0.0


def test_conviction_higher_when_pure_and_complete():
    weak = compute_conviction({"total_weight": 2, "net": 1}, 0.2)
    strong = compute_conviction({"total_weight": 6, "net": 6}, 1.0)
    assert strong > weak
    assert 0.0 <= strong <= 1.0


# ─── decide() — integrazione e gate ──────────────────────────────────────────

def _bull_details():
    return {
        "current_price": 100.0,
        "atr": 3.0,
        "signal_count": {"bullish": 4, "bearish": 0},
        "signal": "BUY",
        "trend": "TRENDING_UP",
        "rsi_14": 32,
        "candlestick_setup": "bullish engulfing",
    }


def test_decide_strong_bull_returns_buy_with_sl_below_entry():
    d = decide(ticker="BTC-USD", details=_bull_details(), nav=100_000,
               btc_daily_closes=[100 + i for i in range(200)])  # regime bull
    assert d.action == "BUY"
    assert d.direction == "long"
    assert d.stop_loss is not None and d.stop_loss < d.entry
    assert d.size_units > 0
    assert d.conviction >= 0.55
    assert not d.blocked


def test_decide_low_conviction_blocks():
    # un solo segnale debole + dati scarsi → conviction sotto soglia
    d = decide(ticker="ETH-USD", details={"current_price": 100, "atr": 2,
               "signal": "BUY"}, nav=100_000,
               btc_daily_closes=[100 + i for i in range(200)])
    assert d.blocked is True
    assert "conviction" in d.block_reason


def test_decide_counter_trend_blocked_in_bear_regime():
    # setup long ma regime bear e conviction non altissima → bloccato
    details = {
        "current_price": 100.0, "atr": 3.0,
        "signal_count": {"bullish": 3, "bearish": 1},
        "signal": "BUY", "trend": "TRENDING_UP",
    }
    bear_btc = [200 - i * 0.5 for i in range(200)]
    d = decide(ticker="SOL-USD", details=details, nav=100_000,
               btc_daily_closes=bear_btc,
               params=CoreParams(counter_trend_conviction=0.95))
    assert d.regime == "bear"
    assert d.blocked is True
    assert "contro regime" in d.block_reason


def test_decide_neutral_confluence_blocks():
    # bull e bear si annullano → net 0
    details = {
        "signal_count": {"bullish": 2, "bearish": 2},
        "current_price": 100.0, "atr": 2.0,
    }
    d = decide(ticker="ADA-USD", details=details, nav=100_000)
    assert d.blocked is True
    assert "neutra" in d.block_reason


def test_decide_never_opens_without_stop_loss():
    # qualunque decisione BUY/SHORT deve avere uno stop-loss valido
    d = decide(ticker="BTC-USD", details=_bull_details(), nav=100_000,
               btc_daily_closes=[100 + i for i in range(200)])
    if d.action in ("BUY", "SHORT"):
        assert d.stop_loss is not None and d.stop_loss > 0


def test_decide_no_invented_numbers_when_data_empty():
    # dati praticamente assenti → nessun trade, completeness bassa
    d = decide(ticker="XRP-USD", details={"current_price": 1.0}, nav=100_000)
    assert d.blocked is True
    assert d.data_completeness == 0.0


def test_decide_output_serializable():
    d = decide(ticker="BTC-USD", details=_bull_details(), nav=100_000,
               btc_daily_closes=[100 + i for i in range(200)])
    out = d.to_dict()
    assert out["ticker"] == "BTC-USD"
    assert isinstance(out["factors"], list)
    # i factor devono essere dict (serializzabili), non dataclass
    if out["factors"]:
        assert isinstance(out["factors"][0], dict)


# ─── Adapter (mappa indicatori reali → input core) ───────────────────────────

def test_adapter_maps_pure_indicator_shape():
    # shape grezza di _fetch_ticker_indicators (technical.py)
    ind = {
        "ticker": "BTC-USD", "current_price": 100, "rsi": 28, "atr": 3,
        "macd": {"line": 1.0, "signal": 0.5, "histogram": 0.4},
        "sma_50": 95, "sma_200": 90, "support": 98, "resistance": 110,
        "trend": "TRENDING_UP", "bullish_signals": 3, "bearish_signals": 1,
    }
    details, raw, mkt = core_inputs_from_analysis(ind)
    assert details["rsi_14"] == 28
    assert details["signal_count"] == {"bullish": 3, "bearish": 1}
    assert raw["macd_hist"] == 0.4
    assert raw["ema50"] == 95 and raw["ema200"] == 90


def test_adapter_maps_analyses_shape():
    # shape 'analyses' del Technical Crypto
    ind = {"ticker": "ETH-USD", "current_price": 50, "rsi_14": 70,
           "signal": "SELL", "funding_bias": "bullish_overcrowded",
           "signal_count": {"bullish": 1, "bearish": 4}}
    details, raw, mkt = core_inputs_from_analysis(ind)
    assert details["signal"] == "SELL"
    assert details["funding_bias"] == "bullish_overcrowded"
    assert details["rsi_14"] == 70


def test_adapter_omits_missing_keys():
    details, raw, mkt = core_inputs_from_analysis({"current_price": 100})
    assert "rsi_14" not in details
    assert mkt == {}


def test_adapter_drives_decide_end_to_end():
    ind = {
        "ticker": "BTC-USD", "current_price": 100, "rsi": 28, "atr": 3,
        "macd": {"histogram": 0.4}, "sma_50": 95, "sma_200": 90,
        "trend": "TRENDING_UP", "bullish_signals": 4, "bearish_signals": 0,
        "support": 98, "resistance": 130,
    }
    details, raw, mkt = core_inputs_from_analysis(ind)
    d = decide(ticker="BTC-USD", details=details, nav=100_000, raw=raw,
               market_ctx=mkt, btc_daily_closes=[100 + i for i in range(200)])
    assert d.action == "BUY"
    assert d.stop_loss is not None and d.stop_loss < d.entry


# ─── Reconcile (R1 puo' solo ridurre / vetare) ───────────────────────────────

def test_reconcile_veto_when_core_blocks():
    d = CoreDecision(ticker="X", action="HOLD", direction="none",
                     conviction=0.1, blocked=True, block_reason="low conv")
    r = reconcile(d, side="long", llm_units=5)
    assert r["allowed"] is False
    assert r["size_units"] == 0.0


def test_reconcile_opposite_direction_veto():
    d = decide(ticker="BTC-USD", details=_bull_details(), nav=100_000,
               btc_daily_closes=[100 + i for i in range(200)])
    assert d.action == "BUY"
    r = reconcile(d, side="short", llm_units=1)  # R1 vuole short, core long
    assert r["allowed"] is False


def test_reconcile_reduces_never_increases_size():
    d = decide(ticker="BTC-USD", details=_bull_details(), nav=100_000,
               btc_daily_closes=[100 + i for i in range(200)])
    core_units = d.size_units
    # R1 vuole 10x → deve essere clampato alla size del core
    r = reconcile(d, side="long", llm_units=core_units * 10)
    assert r["allowed"] is True
    assert r["size_units"] <= core_units + 1e-9
    assert r["stop_loss"] == d.stop_loss
    # R1 vuole meta' → si tiene la size piu' piccola di R1
    r2 = reconcile(d, side="long", llm_units=core_units * 0.5)
    assert abs(r2["size_units"] - core_units * 0.5) < 1e-6


# ─── Forense: determinismo + replay da snapshot ──────────────────────────────

def test_decide_is_deterministic():
    # stessi input → output identico (proprieta' forense fondamentale)
    kw = dict(ticker="BTC-USD", details=_bull_details(), nav=100_000,
              btc_daily_closes=[100 + i for i in range(200)])
    d1 = decide(**kw)
    d2 = decide(**kw)
    assert d1.to_dict() == d2.to_dict()


def test_regime_override_takes_priority():
    # con regime forzato, btc_daily_closes viene ignorato
    d = decide(ticker="BTC-USD", details=_bull_details(), nav=100_000,
               regime="bear", btc_daily_closes=[100 + i for i in range(200)])
    assert d.regime == "bear"


def test_snapshot_then_replay_reconstructs_decision():
    # la decisione live deve essere ricostruibile ESATTAMENTE dal suo snapshot
    details = _bull_details()
    raw = {"ema50": 99, "ema200": 95, "macd_hist": 0.3}
    mkt = {"fear_greed": 20}
    params = CoreParams(sl_min_pct=12, sl_max_pct=25, max_position_pct_nav=12,
                        min_conviction=0.55)
    regime, _ = classify_btc_regime([100 + i for i in range(200)])
    original = decide(ticker="BTC-USD", details=details, nav=100_000, raw=raw,
                      market_ctx=mkt, regime=regime, params=params)

    snap = snapshot_for_replay(ticker="BTC-USD", details=details, nav=100_000,
                               raw=raw, market_ctx=mkt, regime=regime,
                               params=params)
    # snapshot serializzabile + versionato
    import json
    assert json.dumps(snap)  # non esplode
    assert snap["core_version"] == CORE_VERSION

    restored = replay(snap)
    assert restored.to_dict() == original.to_dict()


def test_replay_tolerates_partial_snapshot():
    # snapshot minimale non deve far esplodere il replay
    d = replay({"ticker": "ETH-USD", "details": {"current_price": 100}})
    assert isinstance(d, CoreDecision)
    assert d.blocked is True  # dati insufficienti → nessun trade
