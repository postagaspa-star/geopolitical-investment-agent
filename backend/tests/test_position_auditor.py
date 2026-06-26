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


def test_tight_stop_helper_is_single_writer(monkeypatch):
    # Step 6: teeth/escalation e bias-reject passano TUTTE per _apply_tight_stop
    # (prima era logica copia-incollata in due punti). Un solo writer, un log
    # per via, prezzo preso dalla lista positions del chiamante.
    calls = []

    def fake_helper(run_id, ticker, cur, is_short, *, set_by, log_type, payload=None):
        calls.append((ticker, set_by, log_type, cur))
        return True

    monkeypatch.setattr(pa, "_apply_tight_stop", fake_helper)
    pos = [{"ticker": "BTC-USD", "current_price": 900.0, "direction": "LONG"}]
    pa.enforce_auditor_verdicts("run-z", {"BTC-USD": {"verdict": "EXIT",
        "classification": "reversal", "confidence": 85}}, positions=pos)
    pa.enforce_bias_holds("run-z", ["BTC-USD"], positions=pos)
    assert ("BTC-USD", "position_auditor", "POSITION_AUDIT_TEETH", 900.0) in calls
    assert ("BTC-USD", "auditor_bias_reject", "POSITION_AUDIT_BIAS_REJECT", 900.0) in calls
    assert len(calls) == 2  # un solo writer, invocato una volta per ciascuna via


# ── #2: Auditor consapevole di PROFONDITA' + DURATA del pullback ──────────────
def test_compute_pullback_uses_recent_pivot_not_stale_high():
    # vecchio massimo 100 (barra 0), crollo, RECUPERO a ~86 (pivot recente), poi
    # pullback a 80. La deterioration RECENTE e' da ~86 (~7%), NON da 100 (~20%):
    # e' esattamente il caso NEAR (drawdown 28% da un massimo pre-ingresso).
    bars = [{"high": 100, "low": 98, "close": 99}]
    for px in (90, 80, 72, 70):
        bars.append({"high": px + 1, "low": px, "close": px})
    for px in (78, 83, 85):
        bars.append({"high": px + 1, "low": px - 1, "close": px})
    for px in (84, 82, 80):
        bars.append({"high": px + 1, "low": px - 1, "close": px})
    pb = pa._compute_pullback({"data": bars}, current_price=80.0)
    assert pb["structural_high"] >= 100        # vecchio massimo: solo CONTESTO
    assert pb["recent_high"] < 95              # riferimento = picco RECENTE (~86)
    assert pb["drawdown_from_high_pct"] < 12   # deterioration recente lieve, non ~20%


def test_compute_pullback_shallow_recent():
    bars = [{"high": 90, "low": 88, "close": 89}]
    for _ in range(4):
        bars.append({"high": 100, "low": 98, "close": 99})    # plateau ~100 (pivot)
    for _ in range(3):
        bars.append({"high": 97, "low": 95, "close": 96})     # pullback lieve a ~96
    pb = pa._compute_pullback({"data": bars}, current_price=96.0)
    assert abs(pb["drawdown_from_high_pct"] - 4.0) < 1.5


def test_compute_pullback_empty_safe():
    assert pa._compute_pullback({}, 0) == {}
    assert pa._compute_pullback({"data": []}, 50) == {}


def test_playbook_has_depth_duration_rules():
    pb = pa.AUDITOR_PLAYBOOK
    assert "PROFONDITA'" in pb
    assert "METRO PRIMARIO" in pb
    assert "PICCO RECENTE" in pb
    assert "structural_high" in pb
    assert "bars_since_high" in pb


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


# ── Escalation: de-risk (TRIM/EXIT) persistente -> denti (SL stretto) ─────────
def test_derisk_streak_counts_consecutive():
    import database
    tk = "STREAKA-USD"
    for verdict in ("TRIM", "EXIT", "TRIM"):   # 3 de-risk, nessun HOLD
        database.insert_agent_log("r", "POSITION_AUDIT", json.dumps(
            {"verdicts": {tk: {"verdict": verdict, "confidence": 70}}}))
    assert pa._consecutive_derisk_streak(tk) == 3


def test_derisk_streak_breaks_on_hold():
    import database
    tk = "STREAKB-USD"
    for verdict in ("TRIM", "HOLD", "TRIM"):   # un HOLD spezza la serie
        database.insert_agent_log("r", "POSITION_AUDIT", json.dumps(
            {"verdicts": {tk: {"verdict": verdict, "confidence": 70}}}))
    # con un HOLD in mezzo lo streak non puo' raggiungere 3 (a prescindere
    # dall'ordine dei timestamp a parita' di secondo).
    assert pa._consecutive_derisk_streak(tk) < 3


def test_escalation_tightens_sl_after_3_trim():
    import database
    tk = "ESCALA-USD"
    portfolio.execute_buy(tk, 2.0, 100.0, "g", "t", 70)
    for _ in range(3):                         # 3 TRIM consecutivi ignorati
        database.insert_agent_log("r", "POSITION_AUDIT", json.dumps(
            {"verdicts": {tk: {"verdict": "TRIM", "confidence": 65}}}))
    pos = [{"ticker": tk, "current_price": 95.0, "direction": "LONG"}]
    verdicts = {tk: {"verdict": "TRIM", "classification": "giveback", "confidence": 65}}
    acted = pa.enforce_auditor_verdicts("r", verdicts, positions=pos)
    assert acted == [tk]
    sl = float(portfolio.get_position(tk)["stop_loss_price"])
    assert abs(sl - 95.0 * 0.995) < 1e-3       # SL stretto a ridosso del prezzo


def test_no_escalation_with_short_streak():
    import database
    tk = "ESCALB-USD"
    portfolio.execute_buy(tk, 2.0, 100.0, "g", "t", 70)
    database.insert_agent_log("r", "POSITION_AUDIT", json.dumps(
        {"verdicts": {tk: {"verdict": "TRIM", "confidence": 65}}}))   # solo 1
    pos = [{"ticker": tk, "current_price": 95.0, "direction": "LONG"}]
    verdicts = {tk: {"verdict": "TRIM", "classification": "giveback", "confidence": 65}}
    assert pa.enforce_auditor_verdicts("r", verdicts, positions=pos) == []
