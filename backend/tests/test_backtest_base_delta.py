"""
Prova storica della "posizione normale": il motore di calcolo deve essere
onesto per costruzione — niente sguardi al futuro, tetti rispettati, costi
che pesano davvero.
"""
import math

import pytest

from simulator import backtest_base_delta as bt


def _dates(n, start_month=1):
    """n sedute distribuite su mesi consecutivi (21 per mese)."""
    out = []
    month, day = start_month, 1
    for i in range(n):
        out.append(f"2020-{month:02d}-{day:02d}")
        day += 1
        if day > 21:
            day = 1
            month += 1
    return out


def _flat(n, start=100.0, step=0.0):
    return [start * (1 + step) ** i for i in range(n)]


def _wobble(n, start=100.0, up=0.004, down=-0.002):
    """Serie deterministica ma con ballo VERO (rendimenti alternati).

    Serve perche' una serie a rendimento costante ha volatilita' zero: il
    motore, correttamente, non la investe mai — e un test costruito sopra
    non proverebbe niente (e' successo alla prima stesura di questo file).
    """
    out = [start]
    for i in range(n - 1):
        out.append(out[-1] * (1 + (up if i % 2 == 0 else down)))
    return out


# ── pesi ────────────────────────────────────────────────────────────────────

def test_chi_balla_di_piu_pesa_di_meno():
    w = bt.inverse_vol_weights({"A": 0.10, "B": 0.20})
    assert w["A"] > w["B"]
    assert w["A"] + w["B"] == pytest.approx(1.0)


def test_tetto_crypto_rispettato():
    # BTC calmissimo → l'inverse-vol lo vorrebbe enorme → il tetto lo ferma
    w = bt.inverse_vol_weights({"BTC-USD": 0.01, "SPY": 0.20, "TLT": 0.20},
                               caps={"BTC-USD": bt.BTC_CAP})
    assert w["BTC-USD"] == pytest.approx(bt.BTC_CAP)
    assert sum(w.values()) == pytest.approx(1.0)


# ── niente sguardi al futuro ────────────────────────────────────────────────

def test_il_futuro_non_cambia_il_passato():
    n = 210
    dates = _dates(n)
    base = {"A": _wobble(n), "B": _wobble(n, up=0.003, down=-0.001)}
    alt = {t: list(v) for t, v in base.items()}
    for i in range(n - 20, n):                    # cambio SOLO la coda finale
        alt["A"][i] *= 3.0
    out1 = bt.simulate(dates, base, mode="base")
    out2 = bt.simulate(dates, alt, mode="base")
    assert out1["equity"][-1] != 1.0, "sanita': il motore deve aver investito"
    assert out1["equity"][: n - 25] == out2["equity"][: n - 25], (
        "i valori passati devono essere identici: se cambiano, il motore "
        "sta sbirciando il futuro")


# ── termometro ──────────────────────────────────────────────────────────────

def test_asset_nervoso_esposizione_ridotta():
    """Con un solo asset molto ballerino, il termometro scala l'esposizione:
    il ballo dell'equity resta vicino al bersaglio, non a quello dell'asset."""
    import random
    rng = random.Random(42)
    n = 400
    prices = [100.0]
    for _ in range(n - 1):
        prices.append(prices[-1] * (1 + rng.gauss(0.0005, 0.03)))  # ~48% annuo
    dates = _dates(n)
    out = bt.simulate(dates, {"X": prices}, mode="base", target_vol=0.07)
    vol = out["metrics"]["vol_pct"]
    assert vol < 12.0, f"ballo {vol}% troppo alto: il termometro non scala"


# ── costi ───────────────────────────────────────────────────────────────────

def test_le_commissioni_pesano():
    n = 210
    dates = _dates(n)
    closes = {"A": _wobble(n), "B": _wobble(n, up=0.003, down=-0.001)}
    con = bt.simulate(dates, closes, mode="base", cost_bps=10.0)["equity"][-1]
    senza = bt.simulate(dates, closes, mode="base", cost_bps=0.0)["equity"][-1]
    assert senza != 1.0, "sanita': il motore deve aver investito"
    assert con < senza


# ── metriche ────────────────────────────────────────────────────────────────

def test_max_perdita_calcolata_giusta():
    m = bt.compute_metrics([100.0, 120.0, 60.0, 90.0])
    assert m["max_dd_pct"] == pytest.approx(-50.0)


def test_pesi_fissi_semplici():
    n = 63
    dates = _dates(n)
    closes = {"A": _flat(n, step=0.001)}
    out = bt.simulate(dates, closes, mode="fixed",
                      fixed_weights={"A": 0.5}, cost_bps=0.0)
    # meta' esposizione → circa meta' del rendimento giornaliero composto
    expected = 1.0
    for _ in range(n - 1):
        expected *= 1 + 0.001 * 0.5
    assert out["equity"][-1] == pytest.approx(expected, rel=1e-9)


# ── quota cripto fissa (manopola aggiunta su richiesta di Andrea) ───────────

def test_quota_cripto_fissa_rispettata():
    vols = {"SPY": 0.15, "TLT": 0.14, "GLD": 0.13, "BTC-USD": 0.70}
    w = bt.base_weights(vols, btc_share=0.25)
    assert w["BTC-USD"] == pytest.approx(0.25)
    assert sum(w.values()) == pytest.approx(1.0)
    # gli altri si spartiscono il resto in inverse-vol: GLD (piu' calmo) > SPY
    assert w["GLD"] > w["SPY"]


def test_senza_quota_fissa_comportamento_storico():
    vols = {"SPY": 0.15, "TLT": 0.14, "GLD": 0.13, "BTC-USD": 0.01}
    a = bt.base_weights(vols, btc_share=None, caps={"BTC-USD": bt.BTC_CAP})
    b = bt.inverse_vol_weights(vols, caps={"BTC-USD": bt.BTC_CAP})
    assert a == b


def test_perche_la_cripto_esce_piccola_dalla_formula():
    """La risposta alla domanda di Andrea, come test: con volatilita'
    realistiche (BTC ~4x gli altri), l'inverse-vol da' alla cripto una fetta
    strutturalmente piccola — il tetto del 10% nemmeno interviene."""
    vols = {"SPY": 0.18, "TLT": 0.15, "GLD": 0.14, "BTC-USD": 0.65}
    w = bt.inverse_vol_weights(vols, caps={"BTC-USD": bt.BTC_CAP})
    assert w["BTC-USD"] < 0.08, "ballando 4x, pesa ~1/4: sotto il tetto da sola"
