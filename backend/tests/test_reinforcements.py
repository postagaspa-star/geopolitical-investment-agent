"""
Test Step 9 (analisi 13/07): rafforzamenti.
  (a) blocco calibrazione conviction -> esito (auto-skip sotto n minimo)
  (b) trend participation sim (parser clamp 30 vs 50) e live
      (build_risk_block + validate_trade via unica fonte)
  (c) debrief allineato agli input outcome v2
"""
import uuid

import pytest

import database
import db_sqlite
import risk_profile
from agents.sim_advisor import build_conviction_calibration_block
from simulator import db as sim_db
from simulator.v2_crypto_engine import _parse_crypto_response
from simulator.v2_engine import _outcome_v2_debrief_lines


# ── (a) calibrazione conviction ─────────────────────────────────────────

@pytest.fixture
def seeded_conviction_runs():
    """12 run crypto ALTA (2 verdi) + 11 MEDIA (7 verdi) + 3 BASSA (sotto
    soglia min_n=10, va omessa). Il DB dei test e' condiviso tra file:
    si parte da sim_runs VUOTA per avere conteggi deterministici."""
    with db_sqlite.get_db() as conn:
        conn.execute("DELETE FROM sim_runs")
    ids = []

    def _mk(conv, outcome, pnl):
        rid = f"cal-{uuid.uuid4().hex[:10]}"
        ids.append(rid)
        return {
            "id": rid, "created_at": "2026-07-01T00:00:00+00:00",
            "completed_at": "2026-07-01T00:00:00+00:00", "mode": "auto",
            "category": "bull_cycle", "scenario_type": "multi", "steps": 3,
            "scenario_id": "s", "historical_period": "x",
            "asset_chosen": "BTC-USD", "action_chosen": "BUY",
            "conviction": conv, "horizon": "2giorni",
            "perf_1w": 0.0, "perf_1m": pnl, "perf_3m": pnl,
            "perf_sp_1m": 0.0, "delta_sp": pnl, "outcome": outcome,
            "original_thesis": "t", "what_happened": "w",
            "thesis_evaluation": "e", "full_data": {"engine": "simulator_v2_crypto"},
        }

    for i in range(12):
        sim_db.insert_run(_mk("ALTA", "green" if i < 2 else "red", -0.01))
    for i in range(11):
        sim_db.insert_run(_mk("MEDIA", "green" if i < 7 else "yellow", 0.005))
    for i in range(3):
        sim_db.insert_run(_mk("BASSA", "green", 0.01))
    yield
    with db_sqlite.get_db() as conn:
        conn.execute("DELETE FROM sim_runs WHERE id LIKE 'cal-%'")


def test_calibrazione_percentuali_e_omissioni(seeded_conviction_runs):
    block = build_conviction_calibration_block(is_crypto=True, min_n=10)
    assert "ALTA: 12 run" in block
    assert "17%" in block          # 2/12 verdi
    assert "MEDIA: 11 run" in block
    assert "64%" in block          # 7/11 verdi
    assert "BASSA" not in block    # n=3 < min_n -> omessa


def test_calibrazione_vuota_senza_dati():
    with db_sqlite.get_db() as conn:
        conn.execute("DELETE FROM sim_runs WHERE horizon = '1settimana'")
    assert build_conviction_calibration_block(is_crypto=False, min_n=10) == ""


# ── (b) trend participation: sim parser ─────────────────────────────────

RAW = '''[3] DECISIONE
{"trades": [{"action": "BUY", "asset": "BTC-USD", "allocation_pct": 45,
             "conviction": "ALTA", "thesis": "t", "rr": "2", "exit_plan": "s|t"}],
 "hold_summary": ""}'''


def test_parser_clamp_default_30():
    parsed = _parse_crypto_response(RAW)
    assert parsed["trades"][0]["allocation_pct"] == 30.0


def test_parser_clamp_50_con_trend_participation():
    parsed = _parse_crypto_response(RAW, max_alloc=50.0)
    assert parsed["trades"][0]["allocation_pct"] == 45.0


# ── (b) trend participation: live risk_profile ──────────────────────────

def test_apply_trend_participation_inattiva():
    cap, active = risk_profile._apply_trend_participation(12.0, "crypto",
                                                          active=False)
    assert cap == 12.0 and not active


def test_apply_trend_participation_attiva_x15_cap30():
    cap, active = risk_profile._apply_trend_participation(12.0, "crypto",
                                                          active=True)
    assert cap == 18.0 and active
    cap, _ = risk_profile._apply_trend_participation(25.0, "crypto",
                                                     active=True)
    assert cap == 30.0             # tetto assoluto 30% NAV


def test_trend_participation_flag_off_inattiva(monkeypatch):
    database.set_setting(risk_profile.SETTING_LIVE_TREND_PARTICIPATION, "false")
    assert risk_profile.trend_participation_active("crypto") is False


def test_trend_participation_richiede_uptrend(monkeypatch):
    database.set_setting(risk_profile.SETTING_LIVE_TREND_PARTICIPATION, "true")
    from agents import crypto_airbag
    monkeypatch.setattr(crypto_airbag, "detect_market_regime", lambda: "UPTREND")
    assert risk_profile.trend_participation_active("crypto") is True
    monkeypatch.setattr(crypto_airbag, "detect_market_regime", lambda: "DOWNTREND")
    assert risk_profile.trend_participation_active("crypto") is False
    monkeypatch.setattr(crypto_airbag, "detect_market_regime", lambda: None)
    assert risk_profile.trend_participation_active("crypto") is False
    assert risk_profile.trend_participation_active("equity") is False
    database.set_setting(risk_profile.SETTING_LIVE_TREND_PARTICIPATION, "false")


def test_validate_trade_cap_alzato_solo_con_flag(monkeypatch):
    """Prompt e governor condividono il calcolo: con trend participation
    attiva validate_trade accetta il 15% su profilo moderate crypto (cap
    12 -> 18); senza, lo rifiuta."""
    database.set_setting(risk_profile.SETTING_LIVE_TREND_PARTICIPATION, "true")
    from agents import crypto_airbag
    monkeypatch.setattr(crypto_airbag, "detect_market_regime", lambda: "UPTREND")
    ok, _ = risk_profile.validate_trade(
        asset_class="crypto", confidence=0.9, allocation_pct=15.0,
        open_positions_count=0)
    assert ok
    database.set_setting(risk_profile.SETTING_LIVE_TREND_PARTICIPATION, "false")
    ok, why = risk_profile.validate_trade(
        asset_class="crypto", confidence=0.9, allocation_pct=15.0,
        open_positions_count=0)
    assert not ok


# ── (c) debrief allineato a outcome v2 ───────────────────────────────────

def test_debrief_lines_contengono_input_v2():
    ov2 = {"outcome": "green", "method": "shadow_v2",
           "shadow_benchmark_pct": -3.0, "delta_eq": 2.5,
           "exposure_avg": 0.15}
    lines = _outcome_v2_debrief_lines(ov2)
    assert "GREEN" in lines
    assert "-3.00%" in lines
    assert "+2.50" in lines
    assert "15%" in lines
    assert "coerente" in lines


def test_debrief_lines_vuote_senza_v2():
    assert _outcome_v2_debrief_lines(None) == ""
    assert _outcome_v2_debrief_lines({}) == ""
