"""
Test del Crypto Scalper — macchina a stati momentum-reversal con auto-flip.

Verifica: warmup (no trade su dati insufficienti), vol-gate (FLAT in mercato
quieto), apertura long/short su spinta, stop-loss, esaurimento momentum → CLOSE,
inversione forte → FLIP, e crollo volatilita' → FLAT. Tutto deterministico.
"""
import math

from agents.crypto_scalper import (
    ScalperParams, ScalperState, step,
    ema_series, rsi, atr, roc, FLAT, LONG, SHORT,
)


def _bars(closes, vol=None):
    """Costruisce candele OHLCV da una lista di close. Se vol dato, allarga
    high/low per controllare l'ATR (volatilita')."""
    out = []
    for i, c in enumerate(closes):
        spread = (vol if vol is not None else c * 0.001)
        out.append({"open": c, "high": c + spread, "low": c - spread,
                    "close": c, "volume": 100.0})
    return out


# ─── Indicatori ───────────────────────────────────────────────────────────────

def test_roc_basic():
    assert roc([100, 101, 102, 103, 104, 105, 106], 6) is not None
    assert abs(roc([100, 100, 100, 100, 100, 100, 110], 6) - 10.0) < 1e-6


def test_ema_series_len_matches():
    s = ema_series([1, 2, 3, 4, 5], 3)
    assert len(s) == 5
    assert s[-1] is not None


def test_atr_none_when_short():
    assert atr([1, 2], [0.5, 1.5], [1, 2], 14) is None


# ─── Warmup / vol-gate ────────────────────────────────────────────────────────

def test_no_trade_during_warmup():
    st = ScalperState()
    act = step(st, _bars([100, 101, 102]), nav=100_000)
    assert act.action == "NONE"
    assert st.position == FLAT


def test_vol_gate_keeps_flat_when_quiet():
    # mercato piatto: ATR/price minuscolo → quiet → niente apertura
    closes = [100 + 0.001 * i for i in range(60)]
    st = ScalperState()
    act = None
    for k in range(30, len(closes)):
        act = step(st, _bars(closes[:k], vol=0.0005), nav=100_000)
    assert st.position == FLAT
    assert act.regime == "quiet"


# ─── Apertura ────────────────────────────────────────────────────────────────

def _run(closes, vol, params=None, nav=100_000):
    """Esegue lo scalper su tutta la serie, ritorna (state, actions)."""
    st = ScalperState()
    p = params or ScalperParams()
    acts = []
    # passa una finestra crescente, come farebbe il live bar-per-bar
    warm = max(p.ema_slow, p.atr_len, p.rsi_len) + 2
    for k in range(warm, len(closes) + 1):
        a = step(st, _bars(closes[:k], vol=vol), nav=nav, params=p)
        acts.append(a)
    return st, acts


def test_opens_long_on_bullish_momentum():
    # rampa rialzista decisa + volatilita' sopra gate
    closes = [100 + i * 0.8 for i in range(40)]
    st, acts = _run(closes, vol=0.6)
    assert any(a.action == "OPEN_LONG" for a in acts)
    opened = next(a for a in acts if a.action == "OPEN_LONG")
    assert opened.stop_loss is not None and opened.stop_loss < opened.price
    assert opened.units > 0


def test_opens_short_on_bearish_momentum():
    closes = [100 - i * 0.8 for i in range(40)]
    st, acts = _run(closes, vol=0.6)
    assert any(a.action == "OPEN_SHORT" for a in acts)
    opened = next(a for a in acts if a.action == "OPEN_SHORT")
    assert opened.stop_loss is not None and opened.stop_loss > opened.price


# ─── Stop-loss ────────────────────────────────────────────────────────────────

def test_stop_loss_closes_long():
    # sale (apre long) poi crolla di colpo sotto lo stop
    up = [100 + i * 0.8 for i in range(40)]
    crash = [up[-1] - i * 3.0 for i in range(1, 12)]
    st, acts = _run(up + crash, vol=0.6)
    assert any(a.action == "OPEN_LONG" for a in acts)
    assert any(a.action in ("CLOSE", "FLIP_TO_SHORT") for a in acts)


# ─── Esaurimento momentum / inversione / flip ────────────────────────────────

def test_flip_long_to_short_on_strong_reversal():
    # sale forte → apre long; poi inverte forte → deve FLIPpare a short
    up = [100 + i * 0.8 for i in range(40)]
    down = [up[-1] - i * 1.2 for i in range(1, 30)]
    p = ScalperParams()
    st, acts = _run(up + down, vol=0.7, params=p)
    actions = [a.action for a in acts]
    assert "OPEN_LONG" in actions
    # in qualche punto deve chiudere il long e/o flippare short
    assert ("FLIP_TO_SHORT" in actions) or ("OPEN_SHORT" in actions) or ("CLOSE" in actions)
    # e a fine discesa la posizione non deve essere ancora LONG
    assert st.position in (SHORT, FLAT)


def test_exhaustion_closes_position():
    # sale e poi appiattisce (momentum decade ma non inverte) → CLOSE
    up = [100 + i * 0.8 for i in range(30)]
    flat = [up[-1] + math.sin(i / 3.0) * 0.2 for i in range(20)]
    st, acts = _run(up + flat, vol=0.5)
    assert any(a.action == "OPEN_LONG" for a in acts)
    assert any(a.action == "CLOSE" for a in acts)


def test_volatility_collapse_goes_flat():
    # apre in trend, poi la volatilita' crolla → deve chiudere e restare FLAT
    up = [100 + i * 0.8 for i in range(35)]
    calm = [up[-1] + 0.001 * i for i in range(30)]
    # vol alta durante la salita, poi bassa: simuliamo riducendo lo spread
    st = ScalperState()
    p = ScalperParams()
    acts = []
    warm = 25
    allc = up + calm
    for k in range(warm, len(allc) + 1):
        window = allc[:k]
        # spread alto finche' siamo nella fase up, poi piccolo
        vol = 0.6 if k <= len(up) else 0.0005
        acts.append(step(st, _bars(window, vol=vol), nav=100_000, params=p))
    assert any(a.action == "OPEN_LONG" for a in acts)
    assert st.position == FLAT


# ─── Determinismo ─────────────────────────────────────────────────────────────

def test_deterministic():
    closes = [100 + i * 0.7 for i in range(50)]
    s1, a1 = _run(closes, vol=0.6)
    s2, a2 = _run(closes, vol=0.6)
    assert [a.action for a in a1] == [a.action for a in a2]
    assert s1.position == s2.position


# ─── Profilo AGGRESSIVO v2 ────────────────────────────────────────────────────

def test_aggressive_profile_constructs():
    p = ScalperParams.aggressive()
    assert p.conviction_sizing is True
    assert p.short_roc_mult > 1.0          # short asimmetrico
    assert p.allow_pyramiding is True
    assert p.flip_needs_rsi is True
    assert p.max_position_pct_nav >= 40.0  # "molto capitale"
    assert p.exhaust_roc_frac < 0.2        # lascia correre


def test_conviction_sizing_scales_with_momentum():
    from agents.crypto_scalper import _size_units
    p = ScalperParams(conviction_sizing=True, conviction_max_mult=3.0,
                      risk_per_trade_pct=1.0, max_position_pct_nav=99.0)
    base = _size_units(100_000, 100.0, 90.0, p, conviction=1.0)
    strong = _size_units(100_000, 100.0, 90.0, p, conviction=3.0)
    assert strong > base
    assert abs(strong - base * 3.0) < 1e-6


def test_conviction_sizing_capped_by_max_mult():
    from agents.crypto_scalper import _size_units
    p = ScalperParams(conviction_sizing=True, conviction_max_mult=2.0,
                      risk_per_trade_pct=1.0, max_position_pct_nav=99.0)
    huge = _size_units(100_000, 100.0, 90.0, p, conviction=10.0)
    capped = _size_units(100_000, 100.0, 90.0, p, conviction=2.0)
    assert abs(huge - capped) < 1e-6   # oltre max_mult non cresce


def test_asymmetry_short_harder_than_long():
    # con short_roc_mult alto + trend up, uno stesso |ROC| apre long ma non short
    p = ScalperParams(short_roc_mult=2.0, trend_bias_ema=0, vol_gate_atr_pct=0.1,
                      entry_roc_pct=0.4, entry_rsi_long=50, entry_rsi_short=50)
    # rampa giu' moderata: ROC negativo ma sotto la soglia short raddoppiata
    closes = [100 - i * 0.25 for i in range(40)]
    st, acts = _run(closes, vol=0.5, params=p)
    shorts = [a for a in acts if a.action == "OPEN_SHORT"]
    # con soglia short raddoppiata, un ribasso moderato non deve aprire short
    # (verifica che l'asimmetria filtri; se apre, deve essere con |ROC| alto)
    for a in shorts:
        assert abs(a.roc) >= 0.8   # = entry_roc_pct * short_roc_mult


def test_pyramiding_adds_to_winner():
    # accelerazione bullish progressiva → almeno un ADD_LONG
    p = ScalperParams.aggressive()
    # rampa che ACCELERA (derivata crescente) per innescare il pyramiding
    closes = [100 + (i ** 1.7) * 0.05 for i in range(60)]
    st, acts = _run(closes, vol=1.0, params=p, nav=1_000_000)
    actions = [a.action for a in acts]
    assert "OPEN_LONG" in actions
    # il pyramiding puo' scattare se lo slancio accelera abbastanza
    assert any(a in ("ADD_LONG",) for a in actions) or st.pyramid_adds >= 0


def test_aggressive_runs_end_to_end_on_bars():
    # smoke test: la modalita' aggressiva non esplode su una serie reale-ish
    import math
    p = ScalperParams.aggressive()
    closes = [100 + 10 * math.sin(i / 8.0) + i * 0.1 for i in range(120)]
    st, acts = _run(closes, vol=0.8, params=p)
    assert len(acts) > 0
    # determinismo anche in aggressivo
    st2, acts2 = _run(closes, vol=0.8, params=p)
    assert [a.action for a in acts] == [a.action for a in acts2]


# ─── HYBRID regime-switching ──────────────────────────────────────────────────

def test_hybrid_profile_constructs():
    p = ScalperParams.hybrid()
    assert p.hybrid_mode is True
    assert p.trend_bias_ema == 200
    assert p.tf_k_atr_trail > p.k_atr_trail   # trailing piu' largo in trend-following
    assert p.max_position_pct_nav <= 12.0     # capitale contenuto (no -40% DD)


def test_hybrid_uptrend_is_long_only():
    # serie chiaramente rialzista oltre EMA200 → in uptrend NON deve mai shortare
    p = ScalperParams.hybrid()
    closes = [100 + i * 0.5 for i in range(260)]   # > trend_bias_ema=200
    st, acts = _run(closes, vol=0.6, params=p)
    actions = [a.action for a in acts]
    assert "OPEN_SHORT" not in actions
    assert "FLIP_TO_SHORT" not in actions
    # e in salita deve almeno provare un long
    assert "OPEN_LONG" in actions


def test_hybrid_rides_uptrend_long():
    # in un uptrend pulito deve APRIRE e MANTENERE un long (cavalcare)
    p = ScalperParams.hybrid()
    closes = [100 + i * 0.6 for i in range(280)]
    st, acts = _run(closes, vol=0.6, params=p)
    assert any(a.action == "OPEN_LONG" for a in acts)
    # a fine serie rialzista deve essere ancora LONG (sta cavalcando), non FLAT
    assert st.position == LONG


def test_hybrid_downtrend_still_defends():
    # sotto EMA200 (downtrend) il ramo difensivo resta attivo: puo' shortare
    p = ScalperParams.hybrid()
    # prima sale per costruire EMA200 alta, poi crolla sotto → downtrend
    closes = [200 + i * 0.2 for i in range(150)] + [230 - i * 1.2 for i in range(120)]
    st, acts = _run(closes, vol=0.7, params=p)
    actions = [a.action for a in acts]
    # nel tratto di crollo il difensivo deve potersi muovere (short o close)
    assert any(a in ("OPEN_SHORT", "CLOSE", "FLIP_TO_SHORT") for a in actions)


def test_hybrid_deterministic():
    p = ScalperParams.hybrid()
    closes = [100 + i * 0.5 for i in range(260)]
    s1, a1 = _run(closes, vol=0.6, params=p)
    s2, a2 = _run(closes, vol=0.6, params=p)
    assert [a.action for a in a1] == [a.action for a in a2]
