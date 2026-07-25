"""
Denominatore del cap di posizione: cash residuo (storico) vs NAV.

`max_position_pct_equity` si legge come "quota massima del PORTAFOGLIO per una
posizione", ma veniva applicata al CASH DISPONIBILE. La differenza e' una
progressione geometrica: ogni apertura prende al massimo il 15% di cio' che
RESTA, quindi il cash segue 100.000 x 0.85^n e il portafoglio ha una garanzia
strutturale di non essere mai pienamente investito.

Il portafoglio reale (2 posizioni, ~76% di liquidita') e' l'output aritmetico
di questa formula, non una scelta prudente dell'agente.

Il flag `sizing_denominator` sceglie la base. Default "cash" = comportamento
storico byte-identico: accendere il NAV e' una decisione sul capitale, non un
effetto collaterale di un deploy.
"""
import pytest

import database
import risk_profile as rp


@pytest.fixture(autouse=True)
def _default_flag():
    database.set_setting(rp.SETTING_SIZING_DENOMINATOR, "")
    yield
    database.set_setting(rp.SETTING_SIZING_DENOMINATOR, "")


def _state(cash, total_value):
    return {"cash": cash, "total_value": total_value}


# ── Il default non cambia nulla ─────────────────────────────────────────────

def test_default_e_cash():
    assert rp.sizing_denominator() == "cash"


def test_default_calcola_sul_cash_come_prima():
    # 15.000 su 50.000 di cash = 30% (col NAV sarebbe 15%)
    pct, base = rp.compute_allocation_pct(15_000, _state(50_000, 100_000))
    assert base == "cash"
    assert pct == pytest.approx(30.0)


def test_flag_nav_calcola_sul_patrimonio():
    database.set_setting(rp.SETTING_SIZING_DENOMINATOR, "nav")
    pct, base = rp.compute_allocation_pct(15_000, _state(50_000, 100_000))
    assert base == "nav"
    assert pct == pytest.approx(15.0)


def test_valore_ignoto_del_flag_ricade_su_cash():
    database.set_setting(rp.SETTING_SIZING_DENOMINATOR, "patrimonio")
    assert rp.sizing_denominator() == "cash"


# ── La progressione geometrica che produce il portafoglio liquido ───────────

def test_col_cash_il_portafoglio_non_puo_investirsi():
    """
    Simula aperture successive al cap del 15%: e' la dimostrazione aritmetica
    del 76% di liquidita' osservato in produzione.
    """
    cap = 15.0
    cash = 100_000.0
    for _ in range(6):                      # max_open_positions del profilo
        trade = cash * cap / 100.0          # il massimo consentito
        pct, _base = rp.compute_allocation_pct(trade, _state(cash, 100_000.0),
                                               denominator="cash")
        assert pct == pytest.approx(cap)    # sempre "al cap", eppure...
        cash -= trade

    # ...dopo SEI posizioni al massimo consentito resta ancora il 38% in cash.
    assert cash / 100_000.0 == pytest.approx(0.85 ** 6, rel=1e-6)
    assert cash > 37_000

    # E con due sole posizioni si ottiene esattamente il regime osservato.
    assert 100_000 * 0.85 ** 2 == pytest.approx(72_250)


def test_col_nav_sei_posizioni_investono_il_portafoglio():
    """Stesso cap, stessa sequenza, ma la base non si restringe."""
    database.set_setting(rp.SETTING_SIZING_DENOMINATOR, "nav")
    nav, cap = 100_000.0, 15.0
    cash = nav
    for _ in range(6):
        trade = nav * cap / 100.0
        pct, base = rp.compute_allocation_pct(trade, _state(cash, nav))
        assert base == "nav"
        assert pct == pytest.approx(cap)
        cash -= trade
    assert cash == pytest.approx(10_000)    # 90% investito, non 38% fermo


# ── Robustezza: mai allargare il cap per un dato mancante ───────────────────

def test_nav_mancante_ricade_sul_cash_senza_allargare():
    database.set_setting(rp.SETTING_SIZING_DENOMINATOR, "nav")
    pct, base = rp.compute_allocation_pct(15_000, {"cash": 50_000})
    assert base == "cash_fallback"
    assert pct == pytest.approx(30.0)       # la base piu' PICCOLA, non la piu' grande


def test_nav_da_positions_value_se_manca_total_value():
    database.set_setting(rp.SETTING_SIZING_DENOMINATOR, "nav")
    pct, base = rp.compute_allocation_pct(
        10_000, {"cash": 40_000, "positions_value": 60_000})
    assert base == "nav"
    assert pct == pytest.approx(10.0)


def test_base_nulla_fallisce_la_validazione():
    """Comportamento storico: base non positiva -> 999 -> il trade viene rifiutato."""
    pct, _ = rp.compute_allocation_pct(1_000, _state(0, 0))
    assert pct == 999.0


def test_input_sporchi_non_esplodono():
    pct, _ = rp.compute_allocation_pct("boh", _state(10_000, 10_000))
    assert pct == 999.0
    pct2, _ = rp.compute_allocation_pct(1_000, {"cash": None, "total_value": None})
    assert pct2 == 999.0


def test_db_rotto_ricade_su_cash(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(database, "get_setting", boom)
    assert rp.sizing_denominator() == "cash"
