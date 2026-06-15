"""
#21 — direttive utente + RECOVERY REVIEW in TESTA al system su Claude.

Sul motore di produzione (Claude) il directives_block (direttive utente ad alta
priorita' + RECOVERY REVIEW) era appeso in CODA al dynamic_block, dopo ~7-8k
token di static: istruzioni depotenziate, in modo incoerente col path R1 (che
le mette gia' in testa). Ora _get_decision_prompt_split lo restituisce SEPARATO
cosi' il path Claude lo piazza come blocco a se' PRIMA dello static.
"""
from agents import decision


def test_directives_block_returned_separately(monkeypatch):
    monkeypatch.setattr(decision, "_build_directives_block",
                        lambda: "DIRECTIVE_MARKER_XYZ - istruzione utente\n")
    out = decision._get_decision_prompt_split("claude")
    assert len(out) == 4                       # ora 4 elementi
    directives, static, dynamic, _ids = out
    assert "DIRECTIVE_MARKER_XYZ" in directives  # blocco separato (andra' in testa)
    assert "DIRECTIVE_MARKER_XYZ" not in dynamic  # NON piu' in coda
    assert "DIRECTIVE_MARKER_XYZ" not in static
