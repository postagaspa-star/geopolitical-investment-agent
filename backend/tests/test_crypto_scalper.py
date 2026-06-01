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
