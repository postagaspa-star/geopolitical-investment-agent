"""
Test Step 6 (analisi 13/07): execution_type su trades + igiene confidence.

Prima del fix: i trade meccanici erano distinguibili solo dai tag testuali
in geopolitical_reasoning, i marcatori 0/100 inquinavano le analisi della
confidence, e almeno un trade reale aveva confidence 0.72 (frazione salvata
al posto di 72).
"""
import db_sqlite
import database
import portfolio


def _last_trade():
    # NON get_trades(limit=1): ordina per timestamp (precisione 1s) e due
    # trade nello stesso secondo hanno ordine ambiguo. L'id e' monotono.
    with db_sqlite.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row)


# ── normalizzazione confidence ───────────────────────────────────────────

def test_normalize_confidence():
    f = db_sqlite._normalize_confidence
    assert f(None) is None
    assert f(0.72) == 72          # frazione riscalata (caso reale NEAR 08/07)
    assert f(1) == 100            # 1.0 = frazione piena
    assert f(72) == 72
    assert f(0) == 0
    assert f(150) == 100          # clamp alto
    assert f(-5) == 0             # clamp basso
    assert f("boh") is None


def test_insert_trade_normalizza_confidence():
    db_sqlite.insert_trade("TST", "BUY", 1, 100.0, "g", "t", "BUY 1 TST",
                           confidence=0.72)
    assert _last_trade()["confidence_score"] == 72


def test_insert_trade_confidence_none_resta_null():
    db_sqlite.insert_trade("TST", "BUY", 1, 100.0, "g", "t", "BUY 1 TST",
                           confidence=None, execution_type="auto_exit")
    t = _last_trade()
    assert t["confidence_score"] is None
    assert t["execution_type"] == "auto_exit"


# ── execution_type: default e propagazione ───────────────────────────────

def test_execute_buy_default_ai():
    res = portfolio.execute_buy("AAPL", 2, 100.0, "geo", "tech", 70)
    assert res.get("success")
    t = _last_trade()
    assert t["execution_type"] == "ai"
    assert t["confidence_score"] == 70


def test_execute_sell_execution_type_custom():
    portfolio.execute_buy("MSFT", 2, 100.0, "geo", "tech", 70)
    res = portfolio.execute_sell("MSFT", 2, 105.0, "stop_enforced",
                                 "STOP ENFORCED test", confidence=None,
                                 execution_type="stop_enforced")
    assert res.get("success")
    t = _last_trade()
    assert t["execution_type"] == "stop_enforced"
    assert t["confidence_score"] is None


def test_execute_short_cover_propagano_tipo():
    portfolio.execute_short("NVDA", 1, 200.0, "geo", "tech", 70,
                            execution_type="scalper")
    assert _last_trade()["execution_type"] == "scalper"
    portfolio.execute_cover("NVDA", 1, 190.0, "geo", "tech", None,
                            execution_type="fail_closed")
    t = _last_trade()
    assert t["execution_type"] == "fail_closed"
    assert t["confidence_score"] is None


def test_close_position_market_propaga_tipo():
    portfolio.execute_buy("AMD", 3, 100.0, "geo", "tech", 70)
    res = portfolio.close_position_market(
        "AMD", 3, 101.0, "CIRCUIT_BREAKER_LIQUIDATE: test", "forced",
        confidence=None, execution_type="liquidation")
    assert res.get("success")
    assert _last_trade()["execution_type"] == "liquidation"


# ── get_recent_trades_by_ticker ──────────────────────────────────────────

def test_get_recent_trades_by_ticker_filtra():
    portfolio.execute_buy("LINK-USD", 10, 8.0, "geo", "tech", 70)  # ai
    portfolio.execute_sell("LINK-USD", 10, 8.1, "stop_enforced", "t",
                           confidence=None, execution_type="stop_enforced")
    all_rows = database.get_recent_trades_by_ticker("LINK-USD", hours=1)
    assert len(all_rows) >= 2
    mech = database.get_recent_trades_by_ticker(
        "LINK-USD", hours=1, execution_types=["stop_enforced", "fail_closed"])
    assert len(mech) >= 1
    assert all(r["execution_type"] == "stop_enforced" for r in mech)
    ai = database.get_recent_trades_by_ticker(
        "LINK-USD", hours=1, execution_types=["ai"])
    assert all(str(r.get("execution_type") or "ai") == "ai" for r in ai)
    # ordine: dal piu' recente
    ts = [r["timestamp"] for r in all_rows]
    assert ts == sorted(ts, reverse=True)


# ── migrazione idempotente ───────────────────────────────────────────────

def test_doppio_init_db_idempotente():
    database.init_db()
    database.init_db()
    # la colonna esiste e insert funziona ancora
    db_sqlite.insert_trade("TST2", "BUY", 1, 50.0, "g", "t", "d", 60)
    assert _last_trade()["execution_type"] == "ai"
