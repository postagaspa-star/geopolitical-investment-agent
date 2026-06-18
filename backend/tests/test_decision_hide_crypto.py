"""
Le crypto NON esistono per il Decision Standard: le posizioni crypto sono
filtrate dalla vista portafoglio (lista + conteggio), NAV/cash invariati (il
sizing equity gira sul NAV reale), e i prompt lo dichiarano esplicitamente.
"""
from agents import decision as dec


def test_hide_crypto_positions_filters_list_and_count():
    state = {"cash": 80000.0, "total_value": 104000.0, "open_positions_count": 3,
             "positions": [{"ticker": "AAPL", "quantity": 10},
                           {"ticker": "NEAR-USD", "quantity": 10000},
                           {"ticker": "X:BTCUSD", "quantity": 1}]}
    out = dec._hide_crypto_positions(state)
    assert [p["ticker"] for p in out["positions"]] == ["AAPL"]
    assert out["open_positions_count"] == 1
    # NAV/cash invariati DI PROPOSITO (non si tocca il sizing equity)
    assert out["cash"] == 80000.0 and out["total_value"] == 104000.0
    # non muta l'originale
    assert len(state["positions"]) == 3


def test_hide_crypto_positions_noop_without_crypto():
    state = {"positions": [{"ticker": "AAPL", "quantity": 10}],
             "open_positions_count": 1}
    assert dec._hide_crypto_positions(state) is state   # nessuna copia se inutile


def test_standard_prompts_declare_crypto_invisible():
    for p in (dec.DECISION_SYSTEM_PROMPT_DEFAULT, dec.DECISION_R1_SYSTEM_PROMPT_DEFAULT):
        assert "LE CRYPTO NON ESISTONO PER TE" in p
