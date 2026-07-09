"""
Test del flusso a settori dei Decision (feature B).
  - Il tool FASE 1 condiviso ha i campi settore (equity), required
  - apply_tool_transition li memorizza nell'initial_assessment
  - _build_reasoning_text li mostra nel log (sezione 🧭 SETTORI)
  - Il tool crypto ha i campi come NARRATIVE, required, senza rotation_summary
"""
import pytest


# ─── Tool condiviso (equity) ────────────────────────────────────────────

def test_shared_tool_has_sector_fields():
    from agents.decision_workflow import COMMIT_INITIAL_ASSESSMENT_TOOL as T
    props = T["input_schema"]["properties"]
    req = T["input_schema"]["required"]
    assert "sectors_to_investigate" in props
    assert "sectors_skipped" in props
    assert "sectors_to_investigate" in req
    assert "sectors_skipped" in req
    # equity mantiene rotation_summary
    assert "rotation_summary" in props


def test_transition_stores_sectors():
    from agents.decision_workflow import (
        WorkflowState, apply_tool_transition, PHASE_INITIAL_DONE)
    st = WorkflowState()
    st = apply_tool_transition(st, "commit_initial_assessment", {
        "situation_overview": "x" * 200,
        "rotation_summary": "safe-haven guida",
        "sectors_to_investigate": ["safe_haven", "defensive"],
        "sectors_skipped": "Salto tech: RS negativa",
        "asset_candidates": ["GLD", "XLU"],
        "technical_questions": ["RSI GLD"],
    }, "{}")
    assert st.phase == PHASE_INITIAL_DONE
    ia = st.initial_assessment
    assert ia["sectors_to_investigate"] == ["safe_haven", "defensive"]
    assert ia["sectors_skipped"] == "Salto tech: RS negativa"


def test_reasoning_text_shows_sectors():
    from agents.decision import _build_reasoning_text
    ia = {
        "situation_overview": "situazione",
        "sectors_to_investigate": ["safe_haven", "energy_commodity"],
        "sectors_skipped": "Salto bonds: nessun catalyst",
    }
    txt = _build_reasoning_text(ia, {}, "conclusione")
    assert "🧭 SETTORI" in txt
    assert "safe_haven" in txt
    assert "Salto bonds" in txt


def test_reasoning_text_no_sectors_ok():
    # Retro-compatibile: senza campi settore la sezione non appare, niente crash
    from agents.decision import _build_reasoning_text
    txt = _build_reasoning_text({"situation_overview": "s"}, {}, "c")
    assert "🧭 SETTORI" not in txt
    assert "situazione" in txt.lower() or "s" in txt


# ─── Tool crypto (narrative) ────────────────────────────────────────────

def test_crypto_tool_sectors_as_narratives():
    from agents.decision_crypto import _CRYPTO_INITIAL_TOOL as T
    props = T["input_schema"]["properties"]
    req = T["input_schema"]["required"]
    assert "sectors_to_investigate" in props
    assert "sectors_to_investigate" in req
    assert "sectors_skipped" in req
    # crypto NON ha rotation_summary (concetto equity)
    assert "rotation_summary" not in props
    assert "rotation_summary" not in req
    # la descrizione parla di narrative
    assert "narrative" in props["sectors_to_investigate"]["description"].lower()
