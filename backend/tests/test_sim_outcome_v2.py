"""
Test Step 2 (analisi 13/07): classify_outcome_v2 — shadow benchmark a
pari esposizione al posto dei criteri che producevano ~60% di run gialle.
"""
from simulator.v2_engine import (
    classify_outcome_v2, compute_shadow_return_pct, _step_exposure,
    _classify_outcome,
)


def _series(values):
    """Serie benchmark: T0 (step_index -1) + uno step per valore successivo."""
    out = [{"step_index": -1, "step_date": "d0", "value": values[0]}]
    for i, v in enumerate(values[1:]):
        out.append({"step_index": i, "step_date": f"d{i+1}", "value": v})
    return out


def _hist_step(idx, cash, total, applied=None):
    return {
        "step_index": idx,
        "valuation_after": {"cash": cash, "total_value": total},
        "applied_trades": applied if applied is not None
        else [{"status": "executed_open_long"}],
        "ai_trades": [],
    }


def _valuation(pnl_pct):
    return {"total_pnl_pct": pnl_pct, "cash": 0, "total_value": 100000}


# ── _step_exposure ───────────────────────────────────────────────────────

def test_esposizione_long():
    assert _step_exposure({"cash": 85000, "total_value": 100000}) == 0.15


def test_esposizione_short_negativa():
    # short: il cash supera il totale -> esposizione negativa
    assert _step_exposure({"cash": 110000, "total_value": 100000}) == -0.1


def test_esposizione_valuation_rotta():
    assert _step_exposure(None) is None
    assert _step_exposure({"cash": 0, "total_value": 0}) is None
    assert _step_exposure({"cash": "x", "total_value": 100}) is None


# ── compute_shadow_return_pct ────────────────────────────────────────────

def test_shadow_esposizione_piena():
    # benchmark -10% in uno step, esposizione 100% -> shadow -10%
    history = [_hist_step(0, cash=0, total=100000)]
    series = _series([100000, 100000, 90000])
    # intervallo T0->step0: expo 0 (pre-trade, stesse date tipicamente);
    # ma qui T0 100k -> step0 100k = 0% comunque; step0->finale con expo 1.0
    assert abs(compute_shadow_return_pct(history, series) - (-10.0)) < 0.01


def test_shadow_esposizione_parziale():
    # benchmark -20%, esposizione 15% -> shadow -3%
    history = [_hist_step(0, cash=85000, total=100000)]
    series = _series([100000, 100000, 80000])
    assert abs(compute_shadow_return_pct(history, series) - (-3.0)) < 0.01


def test_shadow_dati_mancanti():
    assert compute_shadow_return_pct([], _series([1, 2])) is None
    assert compute_shadow_return_pct([_hist_step(0, 0, 100)], []) is None


# ── classify_outcome_v2: i casi che il legacy sbagliava ─────────────────

def test_difesa_in_crash_diventa_verde():
    """pnl -1% con esposizione 15% mentre il benchmark fa -20%:
    l'ombra a pari esposizione avrebbe fatto -3% -> delta_eq +2 -> GREEN.
    Il legacy la marcava yellow (pnl<0 non puo' essere green)."""
    history = [_hist_step(0, cash=85000, total=99000)]
    series = _series([100000, 100000, 80000])
    r = classify_outcome_v2(_valuation(-1.0), history, series, -20.0)
    assert r["outcome"] == "green" and r["method"] == "shadow_v2"
    assert _classify_outcome(_valuation(-1.0), -20.0) == "yellow"  # il bug storico


def test_cash_drag_in_rally_diventa_rosso():
    """pnl +0.3% con esposizione 100% mentre il benchmark fa +10%:
    l'ombra avrebbe fatto +10% -> delta_eq -9.7 -> RED.
    Il legacy la salvava yellow (pnl>0 mai red)."""
    history = [_hist_step(0, cash=0, total=100300)]
    series = _series([100000, 100000, 110000])
    r = classify_outcome_v2(_valuation(0.3), history, series, 10.0)
    assert r["outcome"] == "red" and r["method"] == "shadow_v2"
    assert _classify_outcome(_valuation(0.3), 10.0) == "yellow"  # il bug storico


def test_hold_in_crash_verde():
    history = [_hist_step(0, cash=100000, total=100000, applied=[])]
    r = classify_outcome_v2(_valuation(0.0), history, _series([1, 1, 1]), -5.0)
    assert r["outcome"] == "green" and r["method"] == "hold_rule"


def test_hold_in_rally_rosso():
    history = [_hist_step(0, cash=100000, total=100000, applied=[])]
    r = classify_outcome_v2(_valuation(0.0), history, _series([1, 1, 1]), 4.0)
    assert r["outcome"] == "red" and r["method"] == "hold_rule"


def test_hold_in_laterale_giallo():
    history = [_hist_step(0, cash=100000, total=100000, applied=[])]
    r = classify_outcome_v2(_valuation(0.0), history, _series([1, 1, 1]), 0.5)
    assert r["outcome"] == "yellow" and r["method"] == "hold_rule"


def test_zona_morta_giallo():
    # esposizione 50%, benchmark +1% -> shadow +0.5; pnl +0.6 -> delta_eq +0.1
    history = [_hist_step(0, cash=50000, total=100000)]
    series = _series([100000, 100000, 101000])
    r = classify_outcome_v2(_valuation(0.6), history, series, 1.0)
    assert r["outcome"] == "yellow" and r["method"] == "shadow_v2"
    assert abs(r["delta_eq"] - 0.1) < 0.01


def test_override_perdita_grossa_sempre_rosso():
    # pnl -4% ma delta_eq ottimo: resta RED
    history = [_hist_step(0, cash=85000, total=96000)]
    series = _series([100000, 100000, 60000])  # crash -40%
    r = classify_outcome_v2(_valuation(-4.0), history, series, -40.0)
    assert r["outcome"] == "red" and r["method"] == "pnl_override"


def test_short_in_crash_segno_shadow():
    """Book short (esposizione netta negativa) in un crash: l'ombra
    GUADAGNA (expo -0.4 x -10% = +4%). Un pnl +1 e' SOTTO l'ombra -> red."""
    history = [_hist_step(0, cash=140000, total=100000)]
    series = _series([100000, 100000, 90000])
    shadow = compute_shadow_return_pct(history, series)
    assert abs(shadow - 4.0) < 0.01
    r = classify_outcome_v2(_valuation(1.0), history, series, -10.0)
    assert r["outcome"] == "red"


def test_serie_mancante_fallback_legacy():
    history = [_hist_step(0, cash=85000, total=100000)]
    r = classify_outcome_v2(_valuation(2.0), history, [], 1.0)
    assert r["method"] == "legacy_fallback"
    assert r["outcome"] == _classify_outcome(_valuation(2.0), 1.0)


def test_output_contiene_input_di_calcolo():
    history = [_hist_step(0, cash=85000, total=100000)]
    series = _series([100000, 100000, 90000])
    r = classify_outcome_v2(_valuation(-0.5), history, series, -10.0)
    assert r["shadow_benchmark_pct"] is not None
    assert r["delta_eq"] is not None
    assert r["exposure_avg"] == 0.15
