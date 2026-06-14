"""
Migliorie tangibili (non bug) — quick win.

#2 (chat dati completi): la chat ora vede il RISK STATE del governor (recovery,
   drawdown, concentrazione, circuit breaker) e il BENCHMARK vs S&P, così
   risponde a "siamo in recovery?" e "sto battendo l'S&P?" coi numeri reali.
   - risk_state iniettato in build_live_context (testato qui)
   - benchmark iniettato nel handler async via lazy-import dell'endpoint
     (review-verified, dipende da fetch S&P reale)
"""
from agents import chat_assistant


def test_live_context_includes_risk_state(monkeypatch):
    import risk_state
    monkeypatch.setattr(risk_state, "build_risk_state_prompt_block",
                        lambda: "RISK_STATE_MARKER_XYZ")
    ctx = chat_assistant.build_live_context()
    assert "RISK_STATE_MARKER_XYZ" in ctx


def test_live_context_survives_risk_state_failure(monkeypatch):
    # se il blocco risk_state esplode, il contesto NON deve rompersi (best-effort)
    import risk_state

    def _boom():
        raise RuntimeError("x")
    monkeypatch.setattr(risk_state, "build_risk_state_prompt_block", _boom)
    ctx = chat_assistant.build_live_context()
    assert isinstance(ctx, str) and len(ctx) > 0   # le altre sezioni reggono
