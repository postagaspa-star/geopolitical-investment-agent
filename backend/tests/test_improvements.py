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


def _fake_req(method, headers=None):
    class _R:
        pass
    r = _R()
    r.method = method
    r.headers = headers or {}
    return r


async def _passthrough(_req):
    return "PASSED"


def test_admin_guard_disabled_without_env(monkeypatch):
    # #1 auth OPT-IN: senza ADMIN_API_TOKEN nessuna auth (app invariata)
    import asyncio
    import main
    monkeypatch.delenv("ADMIN_API_TOKEN", raising=False)
    out = asyncio.run(main._admin_token_guard(_fake_req("POST"), _passthrough))
    assert out == "PASSED"


def test_admin_guard_blocks_post_without_token(monkeypatch):
    import asyncio
    import main
    from fastapi.responses import JSONResponse
    monkeypatch.setenv("ADMIN_API_TOKEN", "sekret-123")
    out = asyncio.run(main._admin_token_guard(_fake_req("POST"), _passthrough))
    assert isinstance(out, JSONResponse) and out.status_code == 401


def test_admin_guard_allows_post_with_token(monkeypatch):
    import asyncio
    import main
    monkeypatch.setenv("ADMIN_API_TOKEN", "sekret-123")
    out = asyncio.run(main._admin_token_guard(
        _fake_req("POST", {"x-admin-token": "sekret-123"}), _passthrough))
    assert out == "PASSED"


def test_admin_guard_rejects_wrong_token(monkeypatch):
    import asyncio
    import main
    from fastapi.responses import JSONResponse
    monkeypatch.setenv("ADMIN_API_TOKEN", "sekret-123")
    out = asyncio.run(main._admin_token_guard(
        _fake_req("POST", {"x-admin-token": "sbagliato"}), _passthrough))
    assert isinstance(out, JSONResponse) and out.status_code == 401


def test_admin_guard_ignores_get(monkeypatch):
    # le GET di sola lettura non sono toccate dall'auth
    import asyncio
    import main
    monkeypatch.setenv("ADMIN_API_TOKEN", "sekret-123")
    out = asyncio.run(main._admin_token_guard(_fake_req("GET"), _passthrough))
    assert out == "PASSED"


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
