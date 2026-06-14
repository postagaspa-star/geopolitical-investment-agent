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


def test_settings_get_redacts_secrets(monkeypatch):
    # #2 sicurezza: GET /api/settings non deve esporre i segreti in chiaro,
    # ma deve lasciare intatti prompt/flag (la UI carica i prompt da qui).
    import asyncio
    import database
    import main
    monkeypatch.setattr(database, "get_config_settings",
                        lambda: {"anthropic_api_key": "sk-secret-123456",
                                 "deepseek_key": "dk-xyz",
                                 "prompt_scout": "ciao mondo",
                                 "commission_bps": "10"}, raising=False)
    res = asyncio.run(main.get_settings())
    s = res["settings"]
    assert "sk-secret-123456" not in str(s["anthropic_api_key"])
    assert "configurata" in s["anthropic_api_key"]
    assert "dk-xyz" not in str(s["deepseek_key"])     # *_key mascherato
    assert s["prompt_scout"] == "ciao mondo"          # prompt NON mascherato
    assert s["commission_bps"] == "10"                # flag NON mascherato


def test_live_context_survives_risk_state_failure(monkeypatch):
    # se il blocco risk_state esplode, il contesto NON deve rompersi (best-effort)
    import risk_state

    def _boom():
        raise RuntimeError("x")
    monkeypatch.setattr(risk_state, "build_risk_state_prompt_block", _boom)
    ctx = chat_assistant.build_live_context()
    assert isinstance(ctx, str) and len(ctx) > 0   # le altre sezioni reggono
