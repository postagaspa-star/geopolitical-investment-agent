"""
Test dello scorer strutturale (rottura S/R + conferma volume).

Verifica: warmup None, breakout con volume alto score alto, breakout SENZA
volume (falsa rottura) score piu' basso, prezzo dentro il range score medio,
score in [0,1], determinismo.
"""
from agents.crypto_structural import structural_score, StructuralParams


def _series(closes, vols, hi_frac=0.01, lo_frac=0.01):
    highs = [c * (1 + hi_frac) for c in closes]
    lows = [c * (1 - lo_frac) for c in closes]
    return closes, highs, lows, vols


def test_warmup_none():
    c, h, l, v = _series([100] * 5, [100] * 5)
    assert structural_score(c, h, l, v) is None


def test_breakout_with_volume_scores_high():
    # range piatto a 100 per 30 candele, poi rottura a 108 con volume TRIPLO
    base = [100 + (1 if i % 2 else -1) * 0.5 for i in range(30)]
    vols = [1000.0] * 30
    base.append(108.0); vols.append(3500.0)   # breakout + volume forte
    c, h, l, v = _series(base, vols)
    s = structural_score(c, h, l, v)
    assert s is not None and s >= 0.65   # rottura confermata = score alto


def test_breakout_without_volume_scores_lower():
    # stessa rottura ma volume NORMALE (falsa rottura sospetta)
    base = [100 + (1 if i % 2 else -1) * 0.5 for i in range(30)]
    vols = [1000.0] * 30
    base.append(108.0); vols.append(1000.0)    # breakout SENZA volume
    c, h, l, v = _series(base, vols)
    s_no_vol = structural_score(c, h, l, v)

    base2 = base[:-1] + [108.0]
    vols2 = [1000.0] * 30 + [3500.0]
    c2, h2, l2, v2 = _series(base2, vols2)
    s_with_vol = structural_score(c2, h2, l2, v2)

    assert s_no_vol is not None and s_with_vol is not None
    # la conferma di volume DEVE alzare lo score (la chiave anti-falsa-rottura)
    assert s_with_vol > s_no_vol


def test_inside_range_scores_moderate():
    # prezzo fermo dentro il range, nessuna rottura → score non alto
    base = [100 + (1 if i % 2 else -1) * 0.5 for i in range(31)]
    vols = [1000.0] * 31
    c, h, l, v = _series(base, vols)
    s = structural_score(c, h, l, v)
    assert s is not None and s < 0.65


def test_score_in_range():
    import math
    base = [100 + math.sin(i / 3.0) * 8 for i in range(40)]
    vols = [1000 + math.cos(i / 2.0) * 300 for i in range(40)]
    c, h, l, v = _series(base, vols)
    s = structural_score(c, h, l, v)
    assert s is not None and 0.0 <= s <= 1.0


def test_deterministic():
    base = [100 + (1 if i % 2 else -1) * 0.5 for i in range(30)] + [108.0]
    vols = [1000.0] * 30 + [3500.0]
    c, h, l, v = _series(base, vols)
    assert structural_score(c, h, l, v) == structural_score(c, h, l, v)
