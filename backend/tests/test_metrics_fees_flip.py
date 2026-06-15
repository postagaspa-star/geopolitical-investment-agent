"""
#32 — profit_factor/expectancy sul P&L NETTO (non lordo).
#33 — lo scatter non perde piu' la posizione ri-aperta da un FLIP.

build_confidence_outcome_scatter costruisce i punti (conviction, outcome) da cui
derivano profit_factor/expectancy. Prima il P&L per-trade era LORDO (incoerente
con l'equity netta) e un FLIP faceva pop senza re-inserire la posizione opposta
(trade successivo perso). Questi test usano uno `history` sintetico.
"""
from simulator import metrics


def test_scatter_pnl_is_net_of_commissions():
    # long 10 @ 100 → chiuso @ 110. Lordo = 100. Fee 10bps/lato:
    # 0.10%*1000 + 0.10%*1100 = 1.0 + 1.1 = 2.1 → netto 97.9.
    history = [
        {"step_index": 0, "applied_trades": [
            {"asset": "BTC", "status": "executed_open_long",
             "executed_qty": 10, "executed_price": 100, "conviction": "ALTA"}]},
        {"step_index": 1, "applied_trades": [
            {"asset": "BTC", "status": "executed_close_long",
             "executed_qty": 10, "executed_price": 110, "conviction": "ALTA"}]},
    ]
    pts = metrics.build_confidence_outcome_scatter(history)
    assert len(pts) == 1
    assert abs(pts[0]["outcome_dollars"] - 97.9) < 1e-6


def test_flip_reopens_position_in_scatter():
    # long 10 @ 100 → FLIP short (vende 15 @ 90: chiude 10 long + apre 5 short)
    # → chiude lo short residuo 5 @ 80. Senza il fix la seconda chiusura veniva
    # persa (la posizione flip non era re-inserita).
    history = [
        {"step_index": 0, "applied_trades": [
            {"asset": "BTC", "status": "executed_open_long",
             "executed_qty": 10, "executed_price": 100, "conviction": "MEDIA"}]},
        {"step_index": 1, "applied_trades": [
            {"asset": "BTC", "status": "executed_flip_short",
             "executed_qty": 15, "executed_price": 90, "conviction": "ALTA"}]},
        {"step_index": 2, "applied_trades": [
            {"asset": "BTC", "status": "executed_close_short",
             "executed_qty": 5, "executed_price": 80, "conviction": "ALTA"}]},
    ]
    pts = metrics.build_confidence_outcome_scatter(history)
    assert len(pts) == 2                      # niente trade perso
    short_close = pts[1]
    assert short_close["action"] == "SELL"    # era una posizione short
    # short 5 aperto @ 90, chiuso @ 80: lordo (90-80)*5 = 50; netto
    # 50 - 0.10%*450 - 0.10%*400 = 50 - 0.45 - 0.40 = 49.15.
    assert abs(short_close["outcome_dollars"] - 49.15) < 1e-6


def test_open_flip_position_keeps_its_conviction():
    # Posizione aperta da un FLIP e ancora aperta a fine partita: deve ereditare
    # la conviction del FLIP (ALTA), non restare sul default.
    history = [
        {"step_index": 0, "applied_trades": [
            {"asset": "BTC", "status": "executed_open_long",
             "executed_qty": 10, "executed_price": 100, "conviction": "MEDIA"}]},
        {"step_index": 1, "applied_trades": [
            {"asset": "BTC", "status": "executed_flip_short",
             "executed_qty": 15, "executed_price": 90, "conviction": "ALTA"}]},
    ]
    scatter = metrics.build_confidence_outcome_scatter(history)
    final_val = {"positions": [
        {"asset": "BTC", "side": "short",
         "unrealized_pnl_pct": 5.0, "unrealized_pnl": 25.0}]}
    full = metrics.add_open_positions_to_scatter(scatter, final_val, history)
    open_pts = [p for p in full if p["status"] == "open"]
    assert len(open_pts) == 1
    assert open_pts[0]["conviction"] == "ALTA"
