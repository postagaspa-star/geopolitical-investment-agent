"""
Binding asimmetrico dell'Auditor sul Decision Crypto (#4).

Se l'Auditor segnala EXIT/TRIM su un ticker, il Decision NON puo' AGGIUNGERE
esposizione nella direzione della posizione (BUY su una LONG, SHORT su una
SHORT); puo' SEMPRE ridurre/chiudere (SELL/COVER) — asimmetrico, nessun override
narrativo. Qui si testa il gate puro `_auditor_blocks_add`; l'enforcement nel
loop esecuzione lo usa prima di ogni execute_trade.
"""
from agents import decision_crypto as dc


def test_blocks_buy_on_exit_long():
    ab = {"NEAR-USD": {"verdict": "EXIT", "direction": "LONG", "reason": "reversal"}}
    blocked = dc._auditor_blocks_add(ab, "NEAR-USD", "BUY")
    assert blocked is not None and blocked["verdict"] == "EXIT"


def test_allows_sell_on_exit_long():
    ab = {"NEAR-USD": {"verdict": "EXIT", "direction": "LONG"}}
    assert dc._auditor_blocks_add(ab, "NEAR-USD", "SELL") is None


def test_blocks_add_on_trim_long():
    ab = {"BTC-USD": {"verdict": "TRIM", "direction": "LONG"}}
    assert dc._auditor_blocks_add(ab, "BTC-USD", "BUY") is not None


def test_short_position_blocks_short_allows_cover():
    ab = {"ETH-USD": {"verdict": "EXIT", "direction": "SHORT"}}
    assert dc._auditor_blocks_add(ab, "ETH-USD", "SHORT") is not None   # add allo short
    assert dc._auditor_blocks_add(ab, "ETH-USD", "COVER") is None       # de-risk
    assert dc._auditor_blocks_add(ab, "ETH-USD", "BUY") is None         # non aggiunge allo short


def test_unflagged_and_empty_pass():
    ab = {"A-USD": {"verdict": "EXIT", "direction": "LONG"}}
    assert dc._auditor_blocks_add(ab, "B-USD", "BUY") is None
    assert dc._auditor_blocks_add({}, "B-USD", "BUY") is None
    assert dc._auditor_blocks_add(None, "B-USD", "BUY") is None


def test_hold_verdict_not_in_binding_passes():
    # nel binding finiscono SOLO EXIT/TRIM; un HOLD non vincola nulla.
    ab = {"SOL-USD": {"verdict": "TRIM", "direction": "LONG"}}
    assert dc._auditor_blocks_add(ab, "SOL-USD", "BUY") is not None
    assert dc._auditor_blocks_add(ab, "SOL-USD", "sell") is None        # case-insensitive action


def test_case_insensitive_ticker():
    ab = {"NEAR-USD": {"verdict": "EXIT", "direction": "LONG"}}
    assert dc._auditor_blocks_add(ab, "near-usd", "BUY") is not None
