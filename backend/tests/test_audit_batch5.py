"""
Round 5 audit — governor di riallocazione capitale (capital_orchestrator).

40.  Il cap 25% NAV era validato su `estimated_proceeds_usd` fornito dal modello
     → il modello poteva dichiararlo basso e vendere molto di più. Ora il cap è
     sul valore REALE qty*current_price.
277. _validate_directive verificava solo `from_agent` asserito dal modello, non
     il proprietario reale `agent_owner` dello snapshot → un agente poteva
     liquidare una PROPRIA posizione spacciandola per riallocazione cross-agent.
20.  orchestrator: l'alert crypto event-driven del watchdog ora passa force=True
     (bypassa il cooldown 50min). [one-liner, verificato da review + regressione]
"""
from agents import capital_orchestrator as co


def _snap(nav, positions):
    return {"total_nav_usd": nav, "cash_usd": nav, "positions": positions}


def _pos(ticker, qty, price, owner, age=100, pnl=5.0):
    return {"ticker": ticker, "quantity": qty, "current_price": price,
            "avg_buy_price": price, "age_hours": age, "pnl_pct": pnl,
            "agent_owner": owner}


def test_cap_uses_real_price_not_estimated():
    # ETH 10 @ 500 = 5000 = 50% di NAV 10000 → oltre il cap 25%, anche se il
    # modello dichiara estimated_proceeds_usd=1.
    snap = _snap(10000.0, [_pos("ETH-USD", 10, 500.0, owner="crypto")])
    directive = {"decision": "approve", "actions": [
        {"ticker": "ETH-USD", "from_agent": "crypto", "quantity_to_sell": 10,
         "estimated_proceeds_usd": 1}]}
    ok, reason = co._validate_directive(directive, snap, requesting_agent="standard")
    assert ok is False and "NAV" in reason


def test_cap_passes_when_real_value_under_cap():
    # ETH 1 @ 500 = 500 = 5% di NAV 10000 → sotto il cap → ok
    snap = _snap(10000.0, [_pos("ETH-USD", 1, 500.0, owner="crypto")])
    directive = {"decision": "approve", "actions": [
        {"ticker": "ETH-USD", "from_agent": "crypto", "quantity_to_sell": 1,
         "estimated_proceeds_usd": 999999}]}
    ok, reason = co._validate_directive(directive, snap, requesting_agent="standard")
    assert ok is True, reason


def test_rejects_liquidating_own_position():
    # richiedente 'crypto', posizione di proprietà 'crypto' (anche se il modello
    # mente dichiarando from_agent='standard') → non è cross-agent.
    snap = _snap(100000.0, [_pos("BTC-USD", 1, 100.0, owner="crypto")])
    directive = {"decision": "approve", "actions": [
        {"ticker": "BTC-USD", "from_agent": "standard", "quantity_to_sell": 1,
         "estimated_proceeds_usd": 100}]}
    ok, reason = co._validate_directive(directive, snap, requesting_agent="crypto")
    assert ok is False and "richiedente" in reason


def test_cross_agent_real_owner_passes():
    # richiedente 'standard', posizione di 'crypto' → riallocazione legittima
    snap = _snap(100000.0, [_pos("BTC-USD", 1, 100.0, owner="crypto")])
    directive = {"decision": "approve", "actions": [
        {"ticker": "BTC-USD", "from_agent": "crypto", "quantity_to_sell": 1,
         "estimated_proceeds_usd": 100}]}
    ok, reason = co._validate_directive(directive, snap, requesting_agent="standard")
    assert ok is True, reason
