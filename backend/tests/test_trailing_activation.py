"""
Soglia di attivazione del trailing stop: smettere di tagliare i vincitori.

La condizione era il solo `cur > avg`: bastava UN CENTESIMO di guadagno perche'
lo stop venisse portato a prezzo-4%. Conseguenze su un orizzonte swing
(giorni-settimane, che e' l'orizzonte dichiarato del sistema):

  - il profilo autorizza stop fra il 7% e il 15% dall'ingresso (midpoint
    d'ufficio 11%), ma il trailing lo riduceva di fatto a ~4% appena la
    posizione toccava il pareggio: un terzo del respiro previsto, deciso da
    un altro sottosistema e scritto in DB senza passare da set_stop_loss;
  - una posizione salita del 2% e poi rientrata veniva chiusa IN PERDITA dal
    trailing stesso (vedi test sotto), pur non avendo mai toccato lo stop di
    profilo.

Il payoff che ne risultava e' l'opposto di quello che serve: perdite piene
fino allo stop, vincitori tagliati al primo rintracciamento.

Ora il trailing entra solo con un cuscinetto reale (default: la stessa
distanza del trailing, quindi al primo scatto lo stop finisce al pareggio).
`trailing_activation_pct = 0` ripristina il comportamento storico.
"""
import pytest

import database
import portfolio


@pytest.fixture(autouse=True)
def _setup():
    database.set_setting("stop_enforcement_enabled", "true")
    database.set_setting("trailing_stop_enabled", "true")
    database.set_setting("trailing_stop_pct", "4")
    database.set_setting("trailing_stop_pct_crypto", "4")
    database.set_setting(portfolio.SETTING_TRAILING_ACTIVATION_PCT, "")
    yield
    database.set_setting(portfolio.SETTING_TRAILING_ACTIVATION_PCT, "")


def _open_long(ticker="BTC-USD", qty=2.0, price=1000.0):
    assert portfolio.execute_buy(ticker, qty, price, "g", "t", 80).get("success")


def _sl(ticker="BTC-USD"):
    pos = portfolio.get_position(ticker)
    return float(pos["stop_loss_price"] or 0) if pos else None


# ── La soglia ───────────────────────────────────────────────────────────────

def test_un_centesimo_di_guadagno_non_attiva_il_trailing():
    _open_long(price=1000.0)
    portfolio.enforce_stops({"BTC-USD": 1000.1})     # +0,01%
    assert _sl() == 0, "il trailing non deve stringere sul nulla"


def test_guadagno_sotto_soglia_non_attiva():
    _open_long(price=1000.0)
    portfolio.enforce_stops({"BTC-USD": 1030.0})     # +3% < 4%
    assert _sl() == 0


def test_guadagno_a_soglia_mette_lo_stop_al_pareggio():
    _open_long(price=1000.0)
    portfolio.enforce_stops({"BTC-USD": 1041.0})     # +4,1%
    # 1041 * 0,96 = 999,36 -> praticamente il pareggio, mai sotto di molto
    assert _sl() == pytest.approx(999.36, abs=0.01)
    assert _sl() > 990, "al primo scatto lo stop non deve finire ben sotto l'entry"


def test_oltre_soglia_il_ratchet_funziona_come_prima():
    _open_long(price=1000.0)
    portfolio.enforce_stops({"BTC-USD": 1100.0})
    assert _sl() == pytest.approx(1056.0, abs=1e-3)
    portfolio.enforce_stops({"BTC-USD": 1200.0})
    assert _sl() == pytest.approx(1152.0, abs=1e-3)


# ── Il danno che la soglia evita ────────────────────────────────────────────

def test_vincitore_modesto_non_viene_chiuso_in_perdita():
    """
    IL CASO CHE COSTAVA SOLDI. Entry 1000, sale a 1020 (+2%), poi rientra a 980.

    Comportamento storico: a +2% il trailing metteva lo SL a 1020*0,96 = 979,2;
    il rientro a 980 lo sfiorava e la posizione veniva chiusa a ~-2%
    dall'ingresso. Un trade mai andato in perdita oltre il rumore veniva
    chiuso IN PERDITA dal meccanismo che doveva proteggerlo, senza aver mai
    toccato lo stop di profilo (che stava intorno a 890).
    """
    _open_long(price=1000.0)
    portfolio.enforce_stops({"BTC-USD": 1020.0})     # +2%: sotto soglia
    assert _sl() == 0

    done = portfolio.enforce_stops({"BTC-USD": 980.0})
    assert done == [], "nessuno stop deve scattare: -2% e' dentro il respiro previsto"
    assert portfolio.get_position("BTC-USD") is not None


def test_comportamento_storico_ripristinabile():
    """Con la soglia a 0 si torna esattamente a prima (via di fuga immediata)."""
    database.set_setting(portfolio.SETTING_TRAILING_ACTIVATION_PCT, "0")
    _open_long(price=1000.0)
    portfolio.enforce_stops({"BTC-USD": 1020.0})     # +2%
    assert _sl() == pytest.approx(979.2, abs=1e-3)   # stop SOTTO l'entry


def test_soglia_esplicita_viene_rispettata():
    database.set_setting(portfolio.SETTING_TRAILING_ACTIVATION_PCT, "10")
    _open_long(price=1000.0)
    portfolio.enforce_stops({"BTC-USD": 1080.0})     # +8% < 10%
    assert _sl() == 0
    portfolio.enforce_stops({"BTC-USD": 1120.0})     # +12% >= 10%
    assert _sl() == pytest.approx(1075.2, abs=1e-3)


def test_setting_non_numerico_usa_il_default(monkeypatch):
    database.set_setting(portfolio.SETTING_TRAILING_ACTIVATION_PCT, "boh")
    assert portfolio._trail_activation_pct(4.0) == 4.0


# ── Short: la simmetria deve reggere ────────────────────────────────────────

def test_short_sotto_soglia_non_attiva():
    assert portfolio.execute_short("BTC-USD", 2.0, 1000.0, "g", "t", 80).get("success")
    portfolio.enforce_stops({"BTC-USD": 980.0})      # +2% di guadagno per uno short
    assert _sl() == 0


def test_short_oltre_soglia_attiva():
    assert portfolio.execute_short("BTC-USD", 2.0, 1000.0, "g", "t", 80).get("success")
    portfolio.enforce_stops({"BTC-USD": 950.0})      # +5% di guadagno
    assert _sl() == pytest.approx(988.0, abs=1e-3)   # 950 * 1,04
