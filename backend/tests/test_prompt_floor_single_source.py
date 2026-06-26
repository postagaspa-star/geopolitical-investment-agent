"""
test_prompt_floor_single_source.py — Step 4 della pulizia: il pavimento di
confidence vive in UN solo posto (PROFILES) e il testo del prompt e' GENERATO
da li' (build_risk_block), cosi' prompt e codice non possono piu' divergere.

Blinda due cose:
  1. build_risk_block rende il floor ESATTAMENTE uguale a get_active_profile()
     min_confidence, per ogni profilo;
  2. i prompt di default del Decision NON contengono piu' un numero-soglia di
     ammissione scritto a mano (es. ">= 50%" / "≥ 55%") che divergerebbe dal
     codice;
  3. il blocco rischio generato dichiara la regola VERA (gate EV), non il vecchio
     "sotto la soglia → NO TRADE" che contraddiceva il codice.
"""
import re

import pytest

import risk_profile as rp
from agents import decision as dec


@pytest.fixture
def _restore_profile():
    prev = rp.get_active_profile_key()
    yield
    rp.set_active_profile_key(prev)


@pytest.mark.parametrize("key,floor", [
    ("conservative", "0.80"),
    ("moderate", "0.65"),
    ("aggressive", "0.50"),
])
def test_risk_block_floor_matches_profile(_restore_profile, key, floor):
    rp.set_active_profile_key(key)
    block = rp.build_risk_block("equity")
    # il numero nel prompt e' generato dal profilo, non scritto a mano
    assert floor in block
    assert f"{rp.get_active_profile()['min_confidence']:.2f}" in block


def test_risk_block_states_ev_exception_not_blunt_no_trade(_restore_profile):
    # il single source deve dichiarare l'eccezione del valore atteso (gate EV),
    # non il vecchio "sotto la soglia → NO TRADE" che contraddiceva il codice
    rp.set_active_profile_key("moderate")
    block = rp.build_risk_block("equity").lower()
    assert "valore atteso" in block
    assert "r/r" in block or "reward" in block
    assert "0.45" in block


def test_decision_prompts_have_no_handtyped_admission_floor():
    # nessun numero-soglia di ammissione hard-coded nei prompt di default:
    # la riga storica ">= 50%" / "≥ 55%" e' stata sostituita dal riferimento al
    # pavimento del profilo. (I numeri nei COMMENTI di codice non contano: qui
    # si controllano SOLO le stringhe di prompt.)
    for prompt in (dec.DECISION_SYSTEM_PROMPT_DEFAULT,
                   dec.DECISION_R1_SYSTEM_PROMPT_DEFAULT):
        assert "threshold per operare: >= 50%" not in prompt
        assert "Confidence threshold ≥ 55%" not in prompt
        # nessuna riga "confidence ... >= NN%" come SOGLIA operativa hard-coded
        assert not re.search(r"threshold per operare:\s*>=\s*\d", prompt)
