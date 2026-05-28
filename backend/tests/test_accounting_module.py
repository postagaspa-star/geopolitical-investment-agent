"""
Test diretti sul modulo CANONICO accounting.py (funzioni pure).
Lock-in del contratto usato da portfolio, trade_analytics, main, simulator,
agenti. Se cambia qui, deve restare coerente ovunque.
"""
import accounting as a


def test_commission():
    assert abs(a.commission(1000, 10) - 1.0) < 1e-9        # 10 bps
    assert abs(a.commission(1000, 0) - 0.0) < 1e-9
    assert abs(a.commission(-1000, 10) - 1.0) < 1e-9       # usa il valore assoluto
    assert a.commission("xx", 10) == 0.0                    # input invalido → 0


def test_signed_position_value():
    assert a.signed_position_value(10, 100, "LONG") == 1000
    assert a.signed_position_value(10, 100, "SHORT") == -1000
    assert a.signed_position_value(10, 100, None) == 1000   # default LONG


def test_positions_value_direction_aware():
    positions = [
        {"quantity": 10, "current_price": 100, "direction": "LONG"},   # +1000
        {"quantity": 5, "current_price": 200, "direction": "SHORT"},   # -1000
        {"quantity": 0, "current_price": 50, "direction": "LONG"},     # skip (qty 0)
        {"quantity": 3, "current_price": 0, "direction": "LONG"},      # skip (price 0)
    ]
    assert abs(a.positions_value(positions) - 0.0) < 1e-9


def test_positions_value_alt_price_key():
    positions = [{"quantity": 10, "avg_buy_price": 50, "direction": "LONG"}]
    assert abs(a.positions_value(positions, price_key="avg_buy_price") - 500) < 1e-9


def test_unrealized_pnl():
    assert abs(a.unrealized_pnl(10, 100, 110, "LONG") - 100) < 1e-9   # long sale → +
    assert abs(a.unrealized_pnl(10, 100, 90, "LONG") + 100) < 1e-9    # long scende → -
    assert abs(a.unrealized_pnl(10, 100, 90, "SHORT") - 100) < 1e-9   # short scende → +
    assert abs(a.unrealized_pnl(10, 100, 110, "SHORT") + 100) < 1e-9  # short sale → -


def test_unrealized_pnl_pct():
    assert abs(a.unrealized_pnl_pct(100, 110, "LONG") - 10.0) < 1e-9
    assert abs(a.unrealized_pnl_pct(100, 90, "SHORT") - 10.0) < 1e-9
    assert a.unrealized_pnl_pct(0, 90, "LONG") == 0.0                  # avg 0 → 0


def test_is_manual_close():
    assert a.is_manual_close(100) is True
    assert a.is_manual_close(101) is True
    assert a.is_manual_close(85) is False
    assert a.is_manual_close(None) is False
