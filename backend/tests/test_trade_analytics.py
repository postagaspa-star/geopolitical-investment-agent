"""
Test anti-regressione su trade_analytics.compute_closed_trades.

Blocca: P&L lordo invece che netto (fee non sottratte), segno SHORT
invertito, FIFO che mischia long e short, gestione is_manual.
Commissione = 10 bps (vedi conftest).
"""
import trade_analytics


def _t(ticker, action, direction, qty, price, ts, conf=70):
    return {"ticker": ticker, "action": action, "direction": direction,
            "quantity": qty, "price": price, "timestamp": ts, "confidence": conf}


def test_long_roundtrip_net_of_fees():
    trades = [
        _t("AAPL", "BUY", "LONG", 10, 100.0, "2026-01-01T10:00:00Z"),
        _t("AAPL", "SELL", "LONG", 10, 110.0, "2026-01-02T10:00:00Z"),
    ]
    closed = trade_analytics.compute_closed_trades(trades)
    assert len(closed) == 1
    c = closed[0]
    assert c["direction"] == "LONG"
    assert abs(c["gross_pnl_usd"] - 100.0) < 0.01
    # fee = 10bps*(1000 + 1100) = 1.0 + 1.1 = 2.1
    assert abs(c["fees_paid"] - 2.1) < 0.01
    assert abs(c["pnl_usd"] - 97.9) < 0.01


def test_short_roundtrip_direction_and_fees():
    trades = [
        _t("BTC-USD", "SELL", "SHORT", 1, 75000.0, "2026-01-01T10:00:00Z"),
        _t("BTC-USD", "BUY", "SHORT", 1, 70000.0, "2026-01-02T10:00:00Z"),
    ]
    closed = trade_analytics.compute_closed_trades(trades)
    assert len(closed) == 1
    c = closed[0]
    assert c["direction"] == "SHORT"
    # short profit: shortato a 75k, coperto a 70k → +5000 lordo
    assert abs(c["gross_pnl_usd"] - 5000.0) < 0.01
    # fee = 10bps*(75000 + 70000) = 75 + 70 = 145
    assert abs(c["fees_paid"] - 145.0) < 0.01
    assert abs(c["pnl_usd"] - 4855.0) < 0.01


def test_short_loss_when_price_rises():
    trades = [
        _t("XYZ", "SELL", "SHORT", 10, 100.0, "2026-01-01T10:00:00Z"),
        _t("XYZ", "BUY", "SHORT", 10, 120.0, "2026-01-02T10:00:00Z"),
    ]
    closed = trade_analytics.compute_closed_trades(trades)
    assert len(closed) == 1
    # short su prezzo che sale = perdita
    assert closed[0]["gross_pnl_usd"] < 0
    assert closed[0]["pnl_usd"] < 0


def test_long_and_short_same_ticker_dont_mix():
    # Una LONG chiusa + una SHORT aperta successivamente sullo stesso ticker
    # NON devono essere accoppiate tra loro.
    trades = [
        _t("AAPL", "BUY", "LONG", 10, 100.0, "2026-01-01T10:00:00Z"),
        _t("AAPL", "SELL", "LONG", 10, 110.0, "2026-01-02T10:00:00Z"),
        _t("AAPL", "SELL", "SHORT", 10, 110.0, "2026-01-03T10:00:00Z"),
        _t("AAPL", "BUY", "SHORT", 10, 105.0, "2026-01-04T10:00:00Z"),
    ]
    closed = trade_analytics.compute_closed_trades(trades)
    assert len(closed) == 2
    dirs = sorted(c["direction"] for c in closed)
    assert dirs == ["LONG", "SHORT"]


def test_include_fees_false_gives_gross():
    trades = [
        _t("AAPL", "BUY", "LONG", 10, 100.0, "2026-01-01T10:00:00Z"),
        _t("AAPL", "SELL", "LONG", 10, 110.0, "2026-01-02T10:00:00Z"),
    ]
    closed = trade_analytics.compute_closed_trades(trades, include_fees=False)
    assert abs(closed[0]["pnl_usd"] - 100.0) < 0.01  # lordo, nessuna fee


def test_manual_close_excluded_in_skill_view():
    # confidence == 100 = chiusura non-AI (manual/auto-exit)
    trades = [
        _t("AAPL", "BUY", "LONG", 10, 100.0, "2026-01-01T10:00:00Z", conf=70),
        _t("AAPL", "SELL", "LONG", 10, 110.0, "2026-01-02T10:00:00Z", conf=100),
    ]
    all_view = trade_analytics.compute_closed_trades(trades, exclude_manual_closes=False)
    skill_view = trade_analytics.compute_closed_trades(trades, exclude_manual_closes=True)
    assert len(all_view) == 1          # nei numeri di performance conta
    assert len(skill_view) == 0        # nella vista SKILL e' esclusa
