"""
Test anti-regressione su main._replay_trades_from_history (audit/rebuild).

Blocca i bug che rendevano l'audit inutilizzabile:
- SHORT open (SELL+SHORT) marcato come "SELL impossibile" (proventi persi)
- SHORT cover (BUY+SHORT) trattato come LONG fantasma
- fee ignorate → cash ricostruito sovrastimato
Commissione = 10 bps (vedi conftest).
"""
import main


def _t(ticker, action, direction, qty, price, ts):
    return {"ticker": ticker, "action": action, "direction": direction,
            "quantity": qty, "price": price, "timestamp": ts}


def test_replay_long_roundtrip_with_fees():
    trades = [
        _t("AAPL", "BUY", "LONG", 10, 100.0, "2026-01-01T10:00:00Z"),
        _t("AAPL", "SELL", "LONG", 10, 110.0, "2026-01-02T10:00:00Z"),
    ]
    res = main._replay_trades_from_history(100000.0, trades)
    # cash = 100000 - (1000 + 1.0) + (1100 - 1.1) = 100097.9
    assert abs(res["cash"] - 100097.9) < 0.01
    assert res["positions"] == {}
    assert abs(res["total_fees_replayed"] - 2.1) < 0.01
    assert not [a for a in res["anomalies"] if a.get("skipped")]


def test_replay_short_open_and_cover_with_fees():
    trades = [
        _t("BTC-USD", "SELL", "SHORT", 1, 75000.0, "2026-01-01T10:00:00Z"),
        _t("BTC-USD", "BUY", "SHORT", 1, 70000.0, "2026-01-02T10:00:00Z"),
    ]
    res = main._replay_trades_from_history(100000.0, trades)
    # open: +75000 -75 ; cover: -70000 -70 → 100000 +74925 -70070 = 104855
    assert abs(res["cash"] - 104855.0) < 0.01
    assert res["positions"] == {}
    assert abs(res["total_fees_replayed"] - 145.0) < 0.01
    assert not [a for a in res["anomalies"] if a.get("skipped")]


def test_replay_short_open_not_marked_impossible():
    # Regressione: prima SHORT open veniva marcato "SELL impossibile" e saltato
    trades = [_t("XYZ", "SELL", "SHORT", 10, 100.0, "2026-01-01T10:00:00Z")]
    res = main._replay_trades_from_history(100000.0, trades)
    assert "XYZ" in res["positions"]
    assert res["positions"]["XYZ"]["direction"] == "SHORT"
    assert not [a for a in res["anomalies"] if a.get("skipped")]


def test_replay_long_open_leaves_position():
    trades = [_t("AAPL", "BUY", "LONG", 10, 100.0, "2026-01-01T10:00:00Z")]
    res = main._replay_trades_from_history(100000.0, trades)
    assert "AAPL" in res["positions"]
    assert res["positions"]["AAPL"]["direction"] == "LONG"
    assert abs(res["cash"] - (100000 - 1001.0)) < 0.01
