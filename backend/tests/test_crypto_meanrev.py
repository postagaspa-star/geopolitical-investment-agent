"""
Test del mean-reversion conservativo (regola: meglio non far nulla che sbagliare).

Verifica le difese: compra solo su oversold FORTE, SOLO in laterale (no
falling-knife in downtrend), hard-stop se il prezzo continua a scendere,
target alla media, timeout, astensione di default, determinismo.
"""
import math
from agents.crypto_meanrev import (
    MeanRevParams, MeanRevState, step, zscore, FLAT, LONG,
)


def _bars(closes):
    return [{"open": c, "high": c * 1.005, "low": c * 0.995, "close": c, "volume": 100.0}
            for c in closes]


def _run(closes, regime="SIDEWAYS", params=None, nav=100_000):
    st = MeanRevState()
    p = params or MeanRevParams()
    acts = []
    for k in range(p.lookback, len(closes) + 1):
        acts.append(step(st, _bars(closes[:k]), nav=nav, regime=regime, params=p))
    return st, acts


def test_zscore_basic():
    flat = [100.0] * 19 + [100.0]
    assert zscore(flat, 20) is None or abs(zscore(flat, 20)) < 1e-6  # nessuna varianza
    dip = [100.0] * 19 + [90.0]
    z = zscore(dip, 20)
    assert z is not None and z < 0   # ultimo prezzo sotto la media → z negativo


def test_abstains_by_default():
    # oscillazione lieve: nessun eccesso → sempre NONE
    closes = [100 + math.sin(i / 2.0) * 0.5 for i in range(60)]
    st, acts = _run(closes)
    assert all(a.action == "NONE" for a in acts)
    assert st.position == FLAT


def test_buys_on_strong_oversold_in_sideways():
    # serie piatta poi un tonfo anomalo → z molto negativo → compra
    closes = [100 + math.sin(i / 2.0) * 0.5 for i in range(40)] + [94, 92, 90]
    st, acts = _run(closes, regime="SIDEWAYS")
    assert any(a.action == "OPEN_LONG" for a in acts)


def test_does_not_buy_in_downtrend():
    # stesso tonfo ma regime DOWNTREND → NON deve comprare (no falling knife)
    closes = [100 + math.sin(i / 2.0) * 0.5 for i in range(40)] + [94, 92, 90]
    st, acts = _run(closes, regime="DOWNTREND")
    assert all(a.action != "OPEN_LONG" for a in acts)
    assert st.position == FLAT


def test_hardstop_when_keeps_falling():
    # compra su un dip in banda (z tra -2 e -3.5), poi il prezzo sfonda lo
    # stop di prezzo → hard-stop taglia, e il COOLDOWN evita di ricomprare il knife.
    base = [100 + math.sin(i / 2.0) * 0.5 for i in range(40)]
    dump = [96.5, 96, 95.5, 90, 85, 80, 75]
    st, acts = _run(base + dump, regime="SIDEWAYS")
    assert any(a.action == "OPEN_LONG" for a in acts)
    assert any("hard-stop" in a.reason for a in acts if a.action == "CLOSE")


def test_cooldown_blocks_reentry_after_hardstop():
    # dopo un hard-stop, il robot NON deve ricomprare per reentry_cooldown_bars
    base = [100 + math.sin(i / 2.0) * 0.5 for i in range(40)]
    dump = [96.5, 96, 95.5, 90, 85, 80, 75, 72, 70, 68]
    st, acts = _run(base + dump, regime="SIDEWAYS",
                    params=MeanRevParams(reentry_cooldown_bars=10))
    opens = sum(1 for a in acts if a.action == "OPEN_LONG")
    # senza cooldown ricomprerebbe a ogni gradino; col cooldown max 1-2 ingressi
    assert opens <= 2


def test_does_not_enter_when_too_extended():
    # tonfo VERTICALE (z <= -3.5) → NON entra: non e' reversion, e' un crollo
    base = [100.0 + math.sin(i / 2.0) * 0.5 for i in range(40)]
    crash = [88]   # un singolo tonfo enorme → z molto sotto -3.5
    st, acts = _run(base + crash, regime="SIDEWAYS")
    last = acts[-1]
    assert last.action == "NONE" and "troppo esteso" in last.reason


def test_exits_at_mean():
    # compra sull'oversold, poi rimbalza alla media → CLOSE a target
    base = [100 + math.sin(i / 2.0) * 0.5 for i in range(40)]
    dip_recover = [93, 91, 90, 93, 97, 100, 101]   # tonfo poi rientro
    st, acts = _run(base + dip_recover, regime="SIDEWAYS")
    assert any(a.action == "OPEN_LONG" for a in acts)
    assert any(a.action == "CLOSE" and "target" in a.reason for a in acts)


def test_timeout_frees_capital():
    # compra, poi resta bloccato sotto media senza rimbalzo né crollo → timeout
    base = [100 + math.sin(i / 2.0) * 0.5 for i in range(40)]
    stuck = [93] + [94] * 20   # sotto media, fermo, non rimbalza a target
    st, acts = _run(base + stuck, regime="SIDEWAYS",
                    params=MeanRevParams(max_hold_bars=10))
    assert any(a.action == "OPEN_LONG" for a in acts)
    assert any("timeout" in a.reason for a in acts if a.action == "CLOSE")


def test_small_position_size():
    closes = [100 + math.sin(i / 2.0) * 0.5 for i in range(40)] + [94, 92, 90]
    st, acts = _run(closes, regime="SIDEWAYS")
    opened = next((a for a in acts if a.action == "OPEN_LONG"), None)
    assert opened is not None
    # size piccola: <= max_position_pct_nav (8%) del NAV
    assert opened.units * opened.price <= 100_000 * 0.08 + 1


def test_deterministic():
    closes = [100 + math.sin(i / 2.0) * 0.5 for i in range(40)] + [94, 92, 90, 95, 100]
    s1, a1 = _run(closes)
    s2, a2 = _run(closes)
    assert [a.action for a in a1] == [a.action for a in a2]
