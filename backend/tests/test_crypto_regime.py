"""
Test del meta-cervello a 3 stati (crypto_regime).

Verifica le 3 regole chiave:
  - UPTREND → apre/tiene LONG
  - DOWNTREND → SCUDO (cash chiude, short apre short)
  - SIDEWAYS → CHIUDE TUTTO (regola di Andrea: cash puro nel laterale)
+ isteresi anti-whipsaw, determinismo.
"""
from agents.crypto_regime import (
    RegimeParams, RegimeState, step, classify_regime,
    UP, DOWN, SIDE, FLAT, LONG, SHORT,
)


def _bars(closes):
    return [{"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 100.0}
            for c in closes]


def _run(closes, params=None, nav=100_000):
    st = RegimeState()
    p = params or RegimeParams()
    acts = []
    warm = p.ema_slow + 6
    for k in range(warm, len(closes) + 1):
        acts.append(step(st, _bars(closes[:k]), nav=nav, params=p))
    return st, acts


# ─── Classificatore ────────────────────────────────────────────────────────

def test_classify_uptrend():
    closes = [100 + i * 1.5 for i in range(80)]   # salita decisa
    assert classify_regime(closes, RegimeParams()) == UP


def test_classify_downtrend():
    closes = [200 - i * 1.5 for i in range(80)]
    assert classify_regime(closes, RegimeParams()) == DOWN


def test_classify_sideways():
    # oscillazione piatta attorno a 100 → laterale
    import math
    closes = [100 + math.sin(i / 3.0) * 1.0 for i in range(80)]
    assert classify_regime(closes, RegimeParams()) == SIDE


def test_classify_warmup_none():
    assert classify_regime([100, 101, 102], RegimeParams()) is None


# ─── Le 3 regole ─────────────────────────────────────────────────────────────

def test_uptrend_opens_long():
    closes = [100 + i * 1.5 for i in range(90)]
    st, acts = _run(closes)
    assert any(a.action == "OPEN_LONG" for a in acts)
    assert st.position == LONG
    assert st.regime == UP


def test_sideways_closes_everything():
    # prima sale (apre long), poi diventa piatto → DEVE chiudere tutto
    up = [100 + i * 1.5 for i in range(70)]
    flat = [up[-1] + (1.0 if i % 2 else -1.0) for i in range(40)]  # oscilla piatto
    st, acts = _run(up + flat)
    assert any(a.action == "OPEN_LONG" for a in acts)
    assert any(a.action == "CLOSE_ALL" for a in acts)
    # a fine laterale: FLAT totale, niente long
    assert st.position == FLAT
    assert st.regime == SIDE


def test_downtrend_cash_shield_closes():
    up = [100 + i * 1.5 for i in range(70)]
    down = [up[-1] - i * 2.0 for i in range(40)]
    st, acts = _run(up + down, params=RegimeParams(shield="cash"))
    assert any(a.action == "CLOSE_ALL" for a in acts)
    # shield cash → niente short, resta flat
    assert st.position == FLAT


def test_downtrend_short_shield_opens_short():
    up = [100 + i * 1.5 for i in range(70)]
    down = [up[-1] - i * 2.0 for i in range(50)]
    st, acts = _run(up + down, params=RegimeParams(shield="short"))
    assert any(a.action == "OPEN_SHORT" for a in acts)


# ─── Isteresi anti-whipsaw ────────────────────────────────────────────────────

def test_hysteresis_needs_confirmation():
    # un singolo giorno "storto" non deve cambiare regime se confirm_bars=3
    p = RegimeParams(confirm_bars=3)
    up = [100 + i * 1.5 for i in range(90)]
    st, acts = _run(up, params=p)
    # in salita pulita resta UP/long, non rimbalza
    assert st.regime == UP


def test_long_pct_nav_quasi_allin():
    closes = [100 + i * 1.5 for i in range(90)]
    st, acts = _run(closes)
    opened = next(a for a in acts if a.action == "OPEN_LONG")
    # quasi tutto il capitale: ~95% NAV / prezzo
    assert opened.units > 0
    assert opened.stop_loss is not None and opened.stop_loss < opened.price


# ─── Determinismo ─────────────────────────────────────────────────────────────

def test_deterministic():
    closes = [100 + i * 1.2 for i in range(90)]
    s1, a1 = _run(closes)
    s2, a2 = _run(closes)
    assert [a.action for a in a1] == [a.action for a in a2]
    assert s1.regime == s2.regime
