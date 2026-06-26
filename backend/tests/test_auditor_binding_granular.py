"""
test_auditor_binding_granular.py — il binding asimmetrico dell'Auditor diventa
VERDICT-GRANULARE (fix Decision Crypto che "individua il downtrend ma resta fermo").

Prima EXIT e TRIM collassavano in UN solo blocco cieco: un TRIM ("riduci, struttura
intatta") fermava l'aggiunta di conviction su uno short confermato esattamente come
un EXIT da struttura rotta. Ora:
  - EXIT + struttura rotta (reversal/topping/giveback) + confidence >= 75 -> BLOCCO
    duro (stesso predicato con cui enforce_auditor_verdicts mette i denti).
  - TRIM, o EXIT a bassa conviction / struttura non rotta -> CAP morbido alla size
    attuale (no piramidazione, ma l'add/refresh passa).
  - qty non nota -> fail-closed = block.
"""
from agents import decision_crypto as dc


def test_trim_caps_not_blocks_same_direction_add():
    binding = {"BTC-USD": {"verdict": "TRIM", "direction": "SHORT",
                           "classification": "healthy_pullback",
                           "confidence": 80, "qty": 0.5}}
    out = dc._auditor_blocks_add(binding, "BTC-USD", "SHORT")
    assert out is not None and out["mode"] == "cap" and out["cap_qty"] == 0.5


def test_exit_broken_structure_highconf_hard_blocks():
    binding = {"ETH-USD": {"verdict": "EXIT", "direction": "SHORT",
                           "classification": "reversal", "confidence": 90, "qty": 1.0}}
    out = dc._auditor_blocks_add(binding, "ETH-USD", "SHORT")
    assert out is not None and out["mode"] == "block"


def test_exit_lowconf_caps_not_blocks():
    binding = {"SOL-USD": {"verdict": "EXIT", "direction": "SHORT",
                           "classification": "unclear", "confidence": 50, "qty": 2.0}}
    assert dc._auditor_blocks_add(binding, "SOL-USD", "SHORT")["mode"] == "cap"


def test_exit_highconf_but_structure_not_broken_caps():
    # EXIT alta conviction ma classification NON "rotta" -> cap, non blocco
    binding = {"AVAX-USD": {"verdict": "EXIT", "direction": "SHORT",
                            "classification": "healthy_pullback",
                            "confidence": 90, "qty": 3.0}}
    assert dc._auditor_blocks_add(binding, "AVAX-USD", "SHORT")["mode"] == "cap"


def test_missing_qty_fails_closed_to_block():
    # senza una size nota non possiamo CAP-pare l'aggiunta -> fail-closed = block
    binding = {"BTC-USD": {"verdict": "TRIM", "direction": "SHORT",
                           "classification": "healthy_pullback", "confidence": 80}}
    assert dc._auditor_blocks_add(binding, "BTC-USD", "SHORT")["mode"] == "block"


def test_flat_ticker_new_short_unconstrained():
    # un ticker non vincolato (nessuna posizione) non e' mai toccato
    assert dc._auditor_blocks_add({}, "DOGE-USD", "SHORT") is None


def test_derisk_cover_on_flagged_short_unconstrained():
    # COVER su una SHORT flaggata = de-risk -> nessun vincolo (asimmetrico)
    binding = {"BTC-USD": {"verdict": "EXIT", "direction": "SHORT",
                           "classification": "reversal", "confidence": 90, "qty": 0.3}}
    assert dc._auditor_blocks_add(binding, "BTC-USD", "COVER") is None


def test_buy_on_short_position_not_add():
    # BUY su una posizione SHORT non "aggiunge" nella direzione -> nessun vincolo
    binding = {"ETH-USD": {"verdict": "EXIT", "direction": "SHORT",
                           "classification": "reversal", "confidence": 90, "qty": 1.0}}
    assert dc._auditor_blocks_add(binding, "ETH-USD", "BUY") is None


def test_block_dict_carries_verdict_for_logging():
    # il dict di blocco deve esporre verdict/direction/reason per i log esistenti
    binding = {"ETH-USD": {"verdict": "EXIT", "direction": "SHORT",
                           "classification": "topping", "confidence": 85, "qty": 1.0}}
    out = dc._auditor_blocks_add(binding, "ETH-USD", "SHORT")
    assert out["verdict"] == "EXIT" and out["direction"] == "SHORT"
