"""
Un downtrend strutturale confermato e' un setup SHORT, non un motivo per aspettare.
Il blocco short lo dice esplicito; il PILASTRO 2 del protocollo [MACRO] ha il
carve-out (reazione a struttura GIA' rotta != previsione dell'evento), con
l'eccezione delle ~2-3h prima di un evento binario.
"""
from agents import decision as dec


def test_short_block_exploits_confirmed_downtrend():
    b = dec._build_short_selling_block()
    assert "DOWNTREND CONFERMATO" in b           # downtrend confermato = setup short
    assert "non l'attesa" in b                   # si shorta, non si aspetta
    assert "evento binario" in b.lower()         # eccezione finestra pre-evento


def test_macro_pillar2_carveout_for_confirmed_downtrend():
    p = dec._build_regime_protocol_block(asset_class="crypto")
    assert "REACTION OVER PREDICTION" in p
    assert "struttura GIA' rotta" in p           # carve-out presente
    assert "AMMESSO" in p                        # lo short sul trend confermato e' ammesso
