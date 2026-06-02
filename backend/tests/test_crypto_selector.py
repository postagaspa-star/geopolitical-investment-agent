"""
Test del selettore deterministico (opportunity score + ranking universo).

Verifica: score alto per trend sano, basso per downtrend, None su warmup,
ranking ordina correttamente, select_top rispetta soglia e K, determinismo.
"""
from agents.crypto_selector import (
    SelectorParams, opportunity_score, rank_universe, select_top, UNIVERSE_14,
)


def _bars(closes, vol_frac=0.01):
    return [{"open": c, "high": c * (1 + vol_frac), "low": c * (1 - vol_frac),
             "close": c, "volume": 100.0} for c in closes]


def _ohlc(closes, vf=0.01):
    b = _bars(closes, vf)
    return [x["close"] for x in b], [x["high"] for x in b], [x["low"] for x in b]


def test_universe_has_14():
    assert len(UNIVERSE_14) == 14
    assert "BTCUSDT" in UNIVERSE_14


def test_warmup_returns_none():
    c, h, l = _ohlc([100, 101, 102])
    assert opportunity_score(c, h, l) is None


def test_strong_uptrend_high_score():
    c, h, l = _ohlc([100 + i * 1.0 for i in range(80)])
    s = opportunity_score(c, h, l)
    assert s is not None and s >= 0.6   # trend sano → opportunita' alta


def test_downtrend_low_score():
    c, h, l = _ohlc([200 - i * 1.0 for i in range(80)])
    s = opportunity_score(c, h, l)
    assert s is not None and s <= 0.4   # downtrend → opportunita' bassa


def test_score_in_range():
    import math
    c, h, l = _ohlc([100 + math.sin(i / 4.0) * 5 for i in range(80)])
    s = opportunity_score(c, h, l)
    assert s is not None and 0.0 <= s <= 1.0


def test_ranking_orders_by_score():
    # un asset in forte uptrend deve battere uno in downtrend
    up = _bars([100 + i * 1.0 for i in range(80)])
    down = _bars([200 - i * 1.0 for i in range(80)])
    flat = _bars([100 + (1 if i % 2 else -1) for i in range(80)])
    ranked = rank_universe({"UP": up, "DOWN": down, "FLAT": flat})
    syms = [r.symbol for r in ranked]
    assert syms[0] == "UP"
    assert syms.index("UP") < syms.index("DOWN")


def test_select_top_respects_k_and_threshold():
    up1 = _bars([100 + i * 1.2 for i in range(80)])
    up2 = _bars([100 + i * 0.9 for i in range(80)])
    down = _bars([200 - i * 1.0 for i in range(80)])
    data = {"UP1": up1, "UP2": up2, "DOWN": down}
    top = select_top(data, k=2, min_score=0.55)
    assert len(top) <= 2
    # il downtrend (score basso) non deve essere selezionato
    assert all(r.symbol != "DOWN" for r in top)


def test_select_top_empty_when_nothing_good():
    # tutto in downtrend → nessuno supera la soglia → cash (lista vuota)
    data = {f"D{i}": _bars([200 - j * 1.0 for j in range(80)]) for i in range(5)}
    top = select_top(data, k=3, min_score=0.55)
    assert top == []


def test_deterministic():
    up = _bars([100 + i * 1.0 for i in range(80)])
    r1 = rank_universe({"A": up})
    r2 = rank_universe({"A": up})
    assert r1[0].score == r2[0].score
