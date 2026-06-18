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


# ── Fase 2: feedback loop (base-rate + lezioni nel contesto dell'Auditor) ──
def test_experience_block_has_base_rate_and_lessons(monkeypatch):
    import risk_state
    from agents import coach_cards
    monkeypatch.setattr(risk_state, "get_recent_win_rate",
                        lambda limit=10: {"win_rate": 0.3, "wins": 3,
                                          "losses": 7, "sample_size": 10})
    monkeypatch.setattr(coach_cards, "get_active_cards_block_for_decision",
                        lambda: "LEZIONE: non tenere i perdenti oltre lo SL")
    block = pa._load_experience_block()
    assert "BASE RATE" in block and "30%" in block
    assert "LEZIONE" in block and "LEZIONI APPRESE" in block


def test_experience_block_empty_when_no_data(monkeypatch):
    import risk_state
    from agents import coach_cards
    monkeypatch.setattr(risk_state, "get_recent_win_rate",
                        lambda limit=10: {"win_rate": 0, "wins": 0,
                                          "losses": 0, "sample_size": 0})
    monkeypatch.setattr(coach_cards, "get_active_cards_block_for_decision", lambda: "")
    assert pa._load_experience_block() == ""


# ── Validatore della confutazione: P&L/entry inammissibili, l'EXIT prevale ──
def test_flag_bias_holds_detects_pnl_defense():
    text = "AVAX: CONFUTO - P&L -$10, break-even sostanziale, perderei poco. Tengo."
    assert pa.flag_bias_holds(text, ["AVAX-USD"]) == ["AVAX-USD"]


def test_flag_bias_holds_accepts_technical_defense():
    text = "LINK: CONFUTO - bullish engulfing a fib 0.236, HVN cluster, supporto tiene."
    assert pa.flag_bias_holds(text, ["LINK-USD"]) == []


def test_flag_bias_holds_mixed_keeps_if_technical_present():
    # se cita ANCHE un argomento tecnico (oltre al P&L) -> ammissibile, non flaggato
    text = "AVAX: RSI ipervenduto a supporto, struttura intatta. P&L vicino break-even."
    assert pa.flag_bias_holds(text, ["AVAX-USD"]) == []


def test_enforce_bias_holds_tightens_sl():
    portfolio.execute_buy("AVAX-USD", 2.0, 1000.0, "g", "t", 80)
    pos = [{"ticker": "AVAX-USD", "current_price": 950.0, "direction": "LONG"}]
    acted = pa.enforce_bias_holds("run-x", ["AVAX-USD"], positions=pos)
    assert acted == ["AVAX-USD"]
    sl = float(portfolio.get_position("AVAX-USD")["stop_loss_price"])
    assert abs(sl - 950.0 * 0.995) < 1e-3


# ── #2: Auditor consapevole di PROFONDITA' + DURATA del pullback ──────────────
def test_compute_pullback_deep_and_persistent():
    # Sale a 100 (barra 0), poi scende per 9 barre fino a 72 (current al minimo):
    # e' il pattern NEAR (profondo + persistente), che deve risultare evidente.
    bars = [{"high": 100, "low": 95, "close": 98}]
    for i in range(9):
        px = 96 - i * 3   # 96, 93, ..., 72
        bars.append({"high": px + 1, "low": px, "close": px})
    pb = pa._compute_pullback({"data": bars}, current_price=72.0)
    assert pb["bars_since_high"] == 9           # fermo da 9 barre sotto il massimo
    assert abs(pb["drawdown_from_high_pct"] - 28.0) < 0.6
    assert pb["retracement_of_swing"] >= 0.99   # current ~ al minimo dello swing


def test_compute_pullback_shallow():
    # swing 90..100, current 98 = appena sotto il massimo -> ritracciamento basso.
    bars = [{"high": 92, "low": 90, "close": 91}]
    for _ in range(4):
        bars.append({"high": 100, "low": 97, "close": 99})
    bars.append({"high": 99, "low": 98, "close": 98})
    pb = pa._compute_pullback({"data": bars}, current_price=98.0)
    assert abs(pb["drawdown_from_high_pct"] - 2.0) < 0.6
    assert pb["retracement_of_swing"] < 0.4     # dentro la zona sana


def test_compute_pullback_empty_safe():
    assert pa._compute_pullback({}, 0) == {}
    assert pa._compute_pullback({"data": []}, 50) == {}


def test_playbook_has_depth_duration_rules():
    pb = pa.AUDITOR_PLAYBOOK
    assert "PROFONDITA'" in pb
    assert "retracement_of_swing" in pb
    assert "0.618" in pb
    assert "bars_since_high" in pb
    # drawdown = metro PRIMARIO; in conflitto vince sul retracement (anti flip-flop)
    assert "VINCE IL DRAWDOWN" in pb
    assert "METRO PRIMARIO" in pb


def test_format_block_binding_only_on_crypto():
    # Il MANDATO VINCOLANTE compare solo con binding=True (crypto); sullo Standard
    # (equity, binding=False) l'Auditor resta SFIDANTE, senza promessa di enforcement.
    flagged = {"BTC-USD": {"verdict": "EXIT", "classification": "reversal",
                           "technical_reason": "CHoCH", "confidence": 85}}
    crypto = pa.format_auditor_block(flagged, binding=True)
    equity = pa.format_auditor_block(flagged, binding=False)
    assert "VINCOLANTE" in crypto and "RIFIUTATA" in crypto
    assert "VINCOLANTE" not in equity
    assert "CONFUTARLA" in equity.upper()    # la sfida tecnica c'e' sempre
    assert "BTC-USD" in equity
