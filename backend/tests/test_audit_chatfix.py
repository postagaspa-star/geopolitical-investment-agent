"""
Fix URGENTI chat decisionali.

BUG 2 (crypto ri-emette azioni vecchie):
  - SCOPE rule in ACTIONS_BLOCK + REGOLA FERREA vincolata al turno corrente
  - _build_history marca i turni assistant con azioni passate
  - separatore di confine prima del messaggio corrente (_call_claude/_r1)
  - decisioni autonome iniettate come background (non imperativo)

BUG 1 (standard "nessuna risposta" / testo vuoto):
  - _extract_json_object: estrazione JSON bilanciata (no greedy find/rfind)
  - _call_claude: retry col modello di fallback se il testo è vuoto
  - _final_plaintext_answer: ultimo giro in testo semplice
"""
import asyncio
import json

from agents import chat_decision


# ════════════════════ BUG 2 ════════════════════

def test_actions_block_has_scope_rule():
    blk = chat_decision.ACTIONS_BLOCK
    assert "RISPONDI SOLO ALLA RICHIESTA CORRENTE" in blk
    # l'esempio-innesco "short su ETH" della vecchia regola ferrea è rimosso
    assert "ti ripropongo lo short su ETH" not in blk
    # la ri-emissione è ora vincolata al messaggio corrente
    assert "MESSAGGIO CORRENTE" in blk


def test_both_system_prompts_include_scope():
    assert "RISPONDI SOLO ALLA RICHIESTA CORRENTE" in chat_decision.SYSTEM_PROMPT_CRYPTO
    assert "RISPONDI SOLO ALLA RICHIESTA CORRENTE" in chat_decision.SYSTEM_PROMPT_STANDARD


def test_build_history_marks_past_actions():
    raw = [
        {"id": 1, "role": "user", "content": "ciao"},
        {"id": 2, "role": "assistant", "content": "propongo short ETH",
         "proposed_actions": [{"type": "execute_trade"}]},
        {"id": 3, "role": "assistant", "content": "fatto",
         "executed_action_results": {"0": {"ok": True}}},
        {"id": 4, "role": "user", "content": "ultimo"},
    ]
    h = chat_decision._build_history(raw, exclude_id=4)
    assert len(h) == 3
    assert h[0]["content"] == "ciao"                      # user non marcato
    assert "PROPOSTA DI UN TURNO PRECEDENTE" in h[1]["content"]
    assert "propongo short ETH" in h[1]["content"]        # contenuto preservato
    assert "GIÀ ESEGUITA" in h[2]["content"]


def test_build_history_plain_assistant_not_marked():
    raw = [{"id": 1, "role": "assistant", "content": "solo testo, nessuna azione"}]
    h = chat_decision._build_history(raw)
    assert h[0]["content"] == "solo testo, nessuna azione"   # niente marcatore


def test_context_block_autonomous_decisions_are_background(monkeypatch):
    from agents import chat_assistant, decision
    monkeypatch.setattr(chat_assistant, "build_live_context", lambda: "LIVE", raising=False)
    monkeypatch.setattr(decision, "build_standard_regime_read_for_crypto",
                        lambda limit=3: "REGIME", raising=False)
    monkeypatch.setattr(decision, "_build_recent_decisions_block",
                        lambda agent_type="crypto", limit=4: "ETH short SELL", raising=False)
    block = chat_decision._build_context_block("crypto", {})
    assert "NON è una richiesta dell'utente" in block
    assert "DEFAULT resta questa" not in block


# ════════════════════ BUG 1 — parser bilanciato ════════════════════

def test_extract_json_object_braces_in_string():
    raw = '{"text": "imposta {\\"stop\\": -5} ok", "proposed_actions": []}'
    obj = chat_decision._extract_json_object(raw)
    assert json.loads(obj)["text"] == 'imposta {"stop": -5} ok'


def test_parse_response_trailing_prose_with_braces():
    # col vecchio rfind('}') greedy il candidate includeva "{vedi sopra}" →
    # JSON invalido → testo perso. Ora lo scan bilanciato si ferma all'oggetto.
    raw = '{"text": "ciao", "proposed_actions": []}\n\nNota a margine {vedi sopra}.'
    p = chat_decision._parse_response(raw)
    assert p["text"] == "ciao"


def test_parse_response_nested_object():
    raw = '{"needs_technical_analysis": {"tickers": ["BTC-USD"]}, "text": "x"} coda }'
    p = chat_decision._parse_response(raw)
    assert p["text"] == "x"
    assert p["needs_technical_analysis"]["tickers"] == ["BTC-USD"]


def test_parse_response_json_fence_in_prose():
    raw = 'Ecco:\n```json\n{"text": "risposta", "proposed_actions": []}\n```\nfine'
    p = chat_decision._parse_response(raw)
    assert p["text"] == "risposta"


# ════════════════════ BUG 1 — _call_claude robusto + separatore ════════════

class _FakeBlock:
    def __init__(self, text):
        self.text = text


class _FakeResp:
    def __init__(self, blocks, stop_reason="end_turn"):
        self.content = blocks
        self.stop_reason = stop_reason
        self.usage = None


class _FakeAnthropic:
    def __init__(self, responses):
        self.calls = []
        self._responses = responses

        class _Msgs:
            def __init__(self, outer):
                self.outer = outer

            def create(self, **kwargs):
                self.outer.calls.append(kwargs)
                return self.outer._responses.pop(0)

        self.messages = _Msgs(self)


def _patch_anthropic(monkeypatch, responses):
    import anthropic
    fake = _FakeAnthropic(responses)
    monkeypatch.setattr(chat_decision, "_get_anthropic_key", lambda: "k")
    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key=None: fake)
    return fake


def test_call_claude_retries_on_empty_with_fallback_model(monkeypatch):
    fake = _patch_anthropic(monkeypatch, [
        _FakeResp([], stop_reason="max_tokens"),          # 1° giro: vuoto
        _FakeResp([_FakeBlock('{"text":"ok"}')]),         # 2° giro: fallback ok
    ])
    out = asyncio.run(chat_decision._call_claude("SYS", [], "ciao", "CTX"))
    assert out == '{"text":"ok"}'
    assert len(fake.calls) == 2
    assert fake.calls[1]["model"] == chat_decision.CLAUDE_MODEL_FALLBACK


def test_call_claude_boundary_with_history(monkeypatch):
    fake = _patch_anthropic(monkeypatch, [_FakeResp([_FakeBlock('{"text":"ok"}')])])
    hist = [{"role": "user", "content": "vecchio"},
            {"role": "assistant", "content": "vecchia risposta"}]
    asyncio.run(chat_decision._call_claude("SYS", hist, "domanda nuova", "CTX"))
    last = fake.calls[0]["messages"][-1]
    assert last["role"] == "user"
    assert "FINE CRONOLOGIA STORICA" in last["content"]
    assert "domanda nuova" in last["content"]


def test_call_claude_no_boundary_without_history(monkeypatch):
    fake = _patch_anthropic(monkeypatch, [_FakeResp([_FakeBlock('{"text":"ok"}')])])
    asyncio.run(chat_decision._call_claude("SYS", [], "prima domanda", "CTX"))
    last = fake.calls[0]["messages"][-1]
    assert last["content"] == "prima domanda"   # nessun separatore al primo turno


def test_final_plaintext_answer_standard(monkeypatch):
    async def fake_claude(sys_p, hist, msg, ctx):
        return "risposta in testo semplice"
    monkeypatch.setattr(chat_decision, "_call_claude", fake_claude)
    out = asyncio.run(chat_decision._final_plaintext_answer("standard", [], "ciao", "CTX"))
    assert out == "risposta in testo semplice"
