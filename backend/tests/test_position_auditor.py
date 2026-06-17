"""
Position Auditor (V3) — revisore tecnico imparziale delle posizioni aperte.

Test del motore (la chiamata LLM e' mockata): parsing dei verdetti, blocco-sfida
iniettato nel Decision (solo su EXIT/TRIM), e le "teeth" misurate (stringe lo SL
su EXIT ad alta conviction + struttura rotta, senza market-dumpare).
"""
import asyncio
import json

from agents import position_auditor as pa
import portfolio
import database


def test_audit_positions_parses_verdicts(monkeypatch):
    monkeypatch.setattr(pa, "_get_deepseek_key", lambda: "fake-key")
    monkeypatch.setattr(pa, "_load_crypto_docs", lambda max_chars=6000: "")

    async def fake_tech(ticker):
        return {"ticker": ticker, "current_price": 950.0, "rsi": 40,
                "advanced": {"market_structure": {"structure": "DOWNTREND"}}}
    monkeypatch.setattr(pa, "_fetch_technicals", fake_tech)

    async def fake_v3(context, max_retries=2):
        return json.dumps({"audits": [
            {"ticker": "BTC-USD", "verdict": "EXIT", "classification": "reversal",
             "technical_reason": "CHoCH bearish", "confidence": 85}]})
    monkeypatch.setattr(pa, "_call_v3", fake_v3)

    pos = [{"ticker": "BTC-USD", "quantity": 2.0, "avg_buy_price": 1000.0,
            "current_price": 950.0, "direction": "LONG", "stop_loss_price": 0}]
    verdicts = asyncio.run(pa.audit_positions("run-x", positions=pos))
    assert verdicts["BTC-USD"]["verdict"] == "EXIT"
    assert verdicts["BTC-USD"]["classification"] == "reversal"
    assert verdicts["BTC-USD"]["confidence"] == 85


def test_audit_skips_without_api_key(monkeypatch):
    monkeypatch.setattr(pa, "_get_deepseek_key", lambda: "")
    pos = [{"ticker": "BTC-USD", "quantity": 2.0, "avg_buy_price": 1000.0,
            "current_price": 950.0, "direction": "LONG"}]
    assert asyncio.run(pa.audit_positions("run-x", positions=pos)) == {}


def test_format_block_challenges_on_exit_only():
    assert pa.format_auditor_block({}) == ""
    only_hold = {"BTC-USD": {"verdict": "HOLD", "classification": "healthy_pullback",
                             "technical_reason": "ok", "confidence": 70}}
    assert pa.format_auditor_block(only_hold) == ""
    flagged = {"BTC-USD": {"verdict": "EXIT", "classification": "reversal",
                           "technical_reason": "CHoCH", "confidence": 85}}
    block = pa.format_auditor_block(flagged)
    assert "BTC-USD" in block and "EXIT" in block and "CONFUTARLA" in block.upper()


def test_teeth_tighten_sl_on_high_conf_reversal():
    portfolio.execute_buy("BTC-USD", 2.0, 1000.0, "g", "t", 80)
    pos = [{"ticker": "BTC-USD", "current_price": 900.0, "direction": "LONG"}]
    verdicts = {"BTC-USD": {"verdict": "EXIT", "classification": "reversal",
                            "technical_reason": "CHoCH", "confidence": 85}}
    acted = pa.enforce_auditor_verdicts("run-x", verdicts, positions=pos)
    assert acted == ["BTC-USD"]
    sl = float(portfolio.get_position("BTC-USD")["stop_loss_price"])
    assert abs(sl - 900.0 * 0.995) < 1e-3


def test_teeth_skip_low_conf_or_healthy():
    portfolio.execute_buy("BTC-USD", 2.0, 1000.0, "g", "t", 80)
    pos = [{"ticker": "BTC-USD", "current_price": 900.0, "direction": "LONG"}]
    assert pa.enforce_auditor_verdicts("run-x", {"BTC-USD": {"verdict": "EXIT",
        "classification": "reversal", "confidence": 60}}, positions=pos) == []
    assert pa.enforce_auditor_verdicts("run-x", {"BTC-USD": {"verdict": "HOLD",
        "classification": "healthy_pullback", "confidence": 90}}, positions=pos) == []
    assert float(portfolio.get_position("BTC-USD")["stop_loss_price"]) == 0.0
