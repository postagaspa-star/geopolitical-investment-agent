"""
test_weekend_report.py — ripristino del Weekend Intelligence Report.

Verifica che lo Scout torni a generare il recap del weekend (perso nel
passaggio da agent.py monolitico al multi-agente) e lo salvi nella tabella
weekend_intelligence esistente, con:
  - il giro completo genera+salva (modello mockato, DB SQLite reale);
  - key_events serializzato come STRINGA JSON (formato che il frontend tollera);
  - anti-double-run: skip se ne esiste già uno < 20h fa;
  - skip "no_material" se non c'è nulla da sintetizzare.
"""
import asyncio
import json

import database
from agents import scout


def _run(coro):
    return asyncio.run(coro)


def _fake_model_response():
    return json.dumps({
        "full_analysis": "Weekend dominato dalla de-escalation USA-Iran; petrolio giù, risk-on.",
        "key_events": [
            {"event": "Tregua USA-Iran", "impact": "Oil -8%, risk-on equity",
             "tickers": ["XOM", "USO", "SPY"]},
            {"event": "BTC sopra 75k", "impact": "Momentum crypto", "tickers": ["BTC-USD"]},
        ],
        "market_implications": "Lunedì probabile gap-up su tech e rotazione fuori dall'energia.",
        "priority_assets": ["NVDA", "XOM", "BTC-USD"],
    })


def _patch_material_and_model(monkeypatch, *, cards=None, model_text=None):
    """Mocka la raccolta dati (read-only) e il modello. DB resta reale."""
    cards = cards if cards is not None else [
        {"source_type": "GDELT", "micro_summary": "Iran-US ceasefire talks advance"},
        {"source_type": "CRYPTO_MARKET", "micro_summary": "BTC pumps to 75k on ETF inflows"},
    ]
    monkeypatch.setattr(scout, "_read_buffer_by_types",
                        lambda *a, **k: list(cards))
    monkeypatch.setattr(scout, "get_latest_aggregated_reports",
                        lambda *a, **k: [])

    async def _fake_deepseek(system_prompt, user_content, max_retries=3):
        return (model_text if model_text is not None else _fake_model_response(),
                "deepseek-test")
    monkeypatch.setattr(scout, "_call_deepseek", _fake_deepseek)


def test_weekend_report_generates_and_persists(monkeypatch):
    _patch_material_and_model(monkeypatch)

    result = _run(scout.run_weekend_report("run-wknd-1", force=True))

    assert not result.get("skipped"), f"non doveva skippare: {result}"
    assert not result.get("error"), f"non doveva errorare: {result}"

    rec = database.get_latest_weekend_intelligence()
    assert rec is not None
    assert "USA-Iran" in (rec.get("content") or "")
    # priority_assets appesi alle implicazioni (lo schema non ha colonna dedicata)
    assert "Asset prioritari" in (rec.get("market_implications") or "")
    assert "NVDA" in (rec.get("market_implications") or "")


def test_key_events_stored_as_json_string(monkeypatch):
    _patch_material_and_model(monkeypatch)

    _run(scout.run_weekend_report("run-wknd-2", force=True))

    rec = database.get_latest_weekend_intelligence()
    ke = rec.get("key_events")
    # Coerente col vecchio save_weekend_intelligence: stringa JSON, non lista.
    assert isinstance(ke, str)
    parsed = json.loads(ke)
    assert isinstance(parsed, list) and len(parsed) == 2
    assert parsed[0]["event"] == "Tregua USA-Iran"
    assert "XOM" in parsed[0]["tickers"]


def test_weekend_report_anti_double_run(monkeypatch):
    # Esiste già un report freschissimo → il secondo run deve skippare.
    database.insert_weekend_intelligence(
        run_id="seed", content="seed", key_events="[]",
        market_implications="seed")

    # Mock comunque presente: se non skippasse, scriverebbe un secondo record.
    _patch_material_and_model(monkeypatch)

    result = _run(scout.run_weekend_report("run-wknd-3"))  # force=False
    assert result.get("skipped") is True
    assert result.get("reason") == "too_recent"


def test_weekend_report_skips_without_material(monkeypatch):
    # Nessun report aggregato e nessuna card → niente da sintetizzare.
    monkeypatch.setattr(scout, "_read_buffer_by_types", lambda *a, **k: [])
    monkeypatch.setattr(scout, "get_latest_aggregated_reports", lambda *a, **k: [])

    called = {"model": False}

    async def _should_not_call(*a, **k):
        called["model"] = True
        return ("{}", "x")
    monkeypatch.setattr(scout, "_call_deepseek", _should_not_call)

    result = _run(scout.run_weekend_report("run-wknd-4", force=True))
    assert result.get("skipped") is True
    assert result.get("reason") == "no_material"
    assert called["model"] is False, "non deve chiamare il modello senza materiale"
