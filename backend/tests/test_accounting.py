"""
Test anti-regressione sulla contabilita' del portafoglio (portfolio.py).

Bloccano i bug che ci hanno fatto sanguinare: segni cassa sbagliati,
NAV non direction-aware, short che non sono NAV-neutral, SELL/COVER
incrociati, leverage guard. Commissione = 10 bps (vedi conftest).
"""
import database
import portfolio

FEE = 0.001  # 10 bps


def test_buy_deducts_cash_and_creates_long():
    r = portfolio.execute_buy("AAPL", 10, 100.0, "g", "t", 70)
    assert r["success"], r
    p = database.get_portfolio()
    # cash = 100000 - 1000 (gross) - 1.0 (fee 10bps di 1000)
    assert abs(p["cash_balance"] - (100000 - 1000 - 1.0)) < 0.01
    pos = database.get_position("AAPL")
    assert pos is not None
    assert abs(float(pos["quantity"]) - 10) < 1e-9
    assert abs(float(pos["avg_buy_price"]) - 100.0) < 1e-9
    assert (pos.get("direction") or "LONG").upper() == "LONG"


def test_sell_adds_cash_and_closes_long():
    portfolio.execute_buy("AAPL", 10, 100.0, "g", "t", 70)
    r = portfolio.execute_sell("AAPL", 10, 110.0, "g", "t", 70)
    assert r["success"], r
    assert database.get_position("AAPL") is None
    p = database.get_portfolio()
    # cash = 100000 -1001 (buy) + 1100 -1.10 (sell net) = 100097.90
    assert abs(p["cash_balance"] - (100000 - 1001.0 + 1100 - 1.10)) < 0.01


def test_calculate_total_value_long_direction_aware():
    portfolio.execute_buy("AAPL", 10, 100.0, "g", "t", 70)
    database.update_position_price("AAPL", 120.0)
    nav = portfolio.calculate_total_value()
    # cash = 99999 ; posizione long vale +10*120 = 1200 → NAV = 99999 + 1200... no:
    # cash dopo buy = 100000 - 1001 = 98999 ; NAV = 98999 + 1200 = 100199
    assert abs(nav - (98999 + 1200)) < 0.01


def test_short_open_is_nav_neutral_minus_fee():
    nav_before = portfolio.calculate_total_value()
    r = portfolio.execute_short("ZZZ", 10, 100.0, "g", "t", 70)
    assert r["success"], r
    nav_after = portfolio.calculate_total_value()
    # Aprire short: incassi proventi (cassa su) ma assumi passivita' →
    # NAV cambia SOLO per la fee (10*100*0.001 = 1.0).
    assert abs(nav_after - (nav_before - 1.0)) < 0.01


def test_short_profits_when_price_drops():
    portfolio.execute_short("ZZZ", 10, 100.0, "g", "t", 70)
    database.update_position_price("ZZZ", 90.0)   # prezzo SCENDE → short in profitto
    nav = portfolio.calculate_total_value()
    assert nav > 100000, f"short doveva profittare, NAV={nav}"


def test_short_loses_when_price_rises():
    portfolio.execute_short("ZZZ", 10, 100.0, "g", "t", 70)
    database.update_position_price("ZZZ", 120.0)   # prezzo SALE → short in perdita
    nav = portfolio.calculate_total_value()
    assert nav < 100000, f"short doveva perdere, NAV={nav}"


def test_cover_realizes_short_pnl():
    portfolio.execute_short("ZZZ", 10, 100.0, "g", "t", 70)
    r = portfolio.execute_cover("ZZZ", 10, 90.0, "g", "t", 70)
    assert r["success"], r
    assert database.get_position("ZZZ") is None
    # realized = (100-90)*10 - cover_fee(900*0.001=0.9) = 99.1
    assert abs(r["realized_pnl"] - 99.1) < 0.01


def test_sell_on_short_is_rejected():
    portfolio.execute_short("ZZZ", 10, 100.0, "g", "t", 70)
    r = portfolio.execute_sell("ZZZ", 5, 100.0, "g", "t", 70)
    assert not r["success"], "SELL su una SHORT deve essere rifiutato (usa COVER)"


def test_cover_on_long_is_rejected():
    portfolio.execute_buy("AAPL", 10, 100.0, "g", "t", 70)
    r = portfolio.execute_cover("AAPL", 5, 100.0, "g", "t", 70)
    assert not r["success"], "COVER su una LONG deve essere rifiutato (usa SELL)"


def test_buy_on_existing_short_is_rejected():
    portfolio.execute_short("ZZZ", 10, 100.0, "g", "t", 70)
    r = portfolio.execute_buy("ZZZ", 5, 100.0, "g", "t", 70)
    assert not r["success"], "BUY long su un ticker con SHORT aperta deve essere rifiutato"


def test_leverage_guard_blocks_oversized_short():
    # NAV ~100k → short da 200k di nozionale deve essere bloccato (max 1x NAV)
    r = portfolio.execute_short("ZZZ", 4000, 50.0, "g", "t", 70)
    assert not r["success"], "lo short oltre 1x il NAV deve essere bloccato"


def test_buy_insufficient_cash_rejected():
    r = portfolio.execute_buy("AAPL", 100000, 100.0, "g", "t", 70)   # 10M
    assert not r["success"], "buy senza liquidita' sufficiente deve fallire"


def test_buy_rejects_nonpositive_qty_price():
    assert not portfolio.execute_buy("AAPL", 0, 100.0, "g", "t", 70)["success"]
    assert not portfolio.execute_buy("AAPL", 10, 0.0, "g", "t", 70)["success"]


def test_partial_sell_keeps_remaining_position():
    portfolio.execute_buy("AAPL", 10, 100.0, "g", "t", 70)
    r = portfolio.execute_sell("AAPL", 4, 110.0, "g", "t", 70)
    assert r["success"], r
    pos = database.get_position("AAPL")
    assert pos is not None
    assert abs(float(pos["quantity"]) - 6) < 1e-9
    assert (pos.get("direction") or "LONG").upper() == "LONG"
