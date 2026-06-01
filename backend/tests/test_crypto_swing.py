"""
Test del Crypto Swing — trend-following bidirezionale con Chandelier stop.

Verifica: warmup, breakout long in uptrend, breakout short in downtrend,
chandelier che TIENE la posizione nei ritracciamenti ma taglia sulla rottura,
asimmetria short, determinismo.
"""
from agents.crypto_swing import (
    SwingParams, SwingState, step, _donchian, FLAT, LONG, SHORT,
)


def _bars(closes, vol_frac=0.01):
    out = []
    for c in closes:
        sp = c * vol_frac
        out.append({"open": c, "high": c + sp, "low": c - sp, "close": c, "volume": 100.0})
    return out


def _run(closes, params=None, nav=100_000, vol_frac=0.01):
    st = SwingState()
    p = params or SwingParams()
    warm = max(p.ema_slow, p.atr_len, int(p.donchian_entry * p.short_donchian_mult)) + 2
    acts = []
    for k in range(warm, len(closes) + 1):
        acts.append(step(st, _bars(closes[:k], vol_frac), nav=nav, params=p))
    return st, acts


def test_donchian_excludes_current():
    # ora basato sui CLOSE, esclude la candela corrente (ultima)
    closes = [10, 11, 12, 13, 20]   # 20 = close corrente, escluso
    hi, lo = _donchian(closes, 3)
    assert hi == 13 and lo == 11    # max/min dei 3 close precedenti (11,12,13)


def test_warmup_no_trade():
    st, acts = _run([100, 101, 102])
    assert all(a.action == "NONE" for a in acts)
    assert st.position == FLAT


def test_opens_long_on_uptrend_breakout():
    # uptrend pulito e prolungato → breakout long
    closes = [100 + i * 1.0 for i in range(90)]
    st, acts = _run(closes)
    assert any(a.action == "OPEN_LONG" for a in acts)
    # e deve TENERE il long fino in fondo (cavalca il trend)
    assert st.position == LONG


def test_rides_through_pullback():
    # sale, ritraccia poco (dentro il chandelier), risale → NON deve chiudere
    up = [100 + i * 1.0 for i in range(80)]
    pull = [up[-1] - i * 0.5 for i in range(1, 6)]   # ritracciamento piccolo
    up2 = [pull[-1] + i * 1.0 for i in range(1, 30)]
    st, acts = _run(up + pull + up2)
    assert any(a.action == "OPEN_LONG" for a in acts)
    # con chandelier 3xATR un ritracciamento piccolo non stoppa: resta LONG
    assert st.position == LONG


def test_chandelier_cuts_on_real_breakdown():
    # sale, apre long, poi crolla forte → il chandelier DEVE tagliare
    up = [100 + i * 1.0 for i in range(80)]
    crash = [up[-1] - i * 4.0 for i in range(1, 25)]
    st, acts = _run(up + crash)
    assert any(a.action == "OPEN_LONG" for a in acts)
    assert any(a.action == "CLOSE" for a in acts)


def test_opens_short_on_downtrend_breakdown():
    # downtrend pulito e prolungato → breakout short
    closes = [300 - i * 1.2 for i in range(120)]
    st, acts = _run(closes)
    assert any(a.action == "OPEN_SHORT" for a in acts)


def test_short_can_be_disabled():
    closes = [300 - i * 1.2 for i in range(120)]
    p = SwingParams(allow_short=False)
    st, acts = _run(closes, params=p)
    assert all(a.action != "OPEN_SHORT" for a in acts)


def test_every_open_has_stop():
    closes = [100 + i * 1.0 for i in range(90)]
    st, acts = _run(closes)
    for a in acts:
        if a.action in ("OPEN_LONG", "OPEN_SHORT"):
            assert a.stop_loss is not None and a.stop_loss > 0


def test_deterministic():
    closes = [100 + i * 0.8 for i in range(100)]
    s1, a1 = _run(closes)
    s2, a2 = _run(closes)
    assert [a.action for a in a1] == [a.action for a in a2]
    assert s1.position == s2.position
