"""
Regressione: le run EQUITY del Simulator crashavano sistematicamente perché
un output AI non parsabile faceva `raise ValueError` → lo scheduler abortiva
l'intera run multi-step. Il prompt equity (più lungo del crypto) si troncava
più spesso → auto-mode salvava ~solo crypto (4:1). Fix: degrado a NO-TRADE
visibile invece di crashare la run.

Qui verifichiamo il TRIGGER (rilevamento troncamento) e che il budget di
output sia stato alzato per ridurre i troncamenti in partenza.
"""
import json


def test_parse_response_flags_truncated_trades():
    from simulator.v2_engine import _parse_response
    # Output che CONTIENE "trades" ma con JSON troncato/non chiuso → parse_failed
    truncated = '[1] LETTURA\nok\n[3] DECISIONE\n{"trades": [{"action": "BUY", "asset": "AAP'
    parsed = _parse_response(truncated)
    assert parsed.get("parse_failed") is True
    assert parsed.get("trades") == []


def test_parse_response_ok_not_flagged():
    from simulator.v2_engine import _parse_response
    good = json.dumps({"trades": [{"action": "BUY", "asset": "XOM",
                                    "allocation_pct": 20, "conviction": "ALTA",
                                    "thesis": "t"}], "hold_summary": ""})
    parsed = _parse_response(good)
    assert not parsed.get("parse_failed")
    assert len(parsed["trades"]) == 1


def test_no_raise_valueerror_on_parse_fail_in_engines():
    # Il codice NON deve più contenere il raise che abortiva la run.
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / "simulator"
    for fn in ("v2_engine.py", "v2_crypto_engine.py"):
        src = (root / fn).read_text(encoding="utf-8")
        assert "step abortito per non perdere trade in silenzio" not in src, \
            f"{fn}: il raise che abortiva la run è ancora presente"
        assert "degrado a NO-TRADE" in src, \
            f"{fn}: manca il fallback graceful a NO-TRADE"


def test_max_tokens_bumped():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / "simulator"
    for fn in ("v2_engine.py", "v2_crypto_engine.py"):
        src = (root / fn).read_text(encoding="utf-8")
        assert '"max_tokens": 6000' in src, f"{fn}: max_tokens non alzato a 6000"
