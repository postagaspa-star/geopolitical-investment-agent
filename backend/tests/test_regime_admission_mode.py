"""
test_regime_admission_mode.py — Step 7 della pulizia: il Regime Protocol dello
Standard puo' funzionare in due modalita', dietro il setting regime_admission_mode.

  - 'legacy_veto' (DEFAULT): prosa storica, LATERAL/MACRO defaultano a NO_TRADE e
    alzano il pavimento NEL PROMPT (sopra il gate di codice) -> radice del
    "Standard non fa mai un trade". Invariato finche' Andrea non flippa il flag.
  - 'context': il regime e' solo CONTESTO di conviction; l'ammissione la decide
    UNA volta il gate rischio a valle. Niente default NO_TRADE, niente boost.

Gli hard-cap del Risk Profile restano in entrambi i casi (validati altrove).
"""
import database
from agents import decision as dec


def test_default_mode_is_legacy_veto(monkeypatch):
    # nessun setting -> default legacy_veto -> prosa storica col veto
    monkeypatch.setattr(database, "get_setting", lambda k, d=None: d, raising=False)
    block = dec._build_regime_protocol_block("equity")
    assert "NO_TRADE" in block
    assert "Default = NO_TRADE" in block  # il default veto storico e' presente


def test_context_mode_drops_default_no_trade_and_floor_boost(monkeypatch):
    monkeypatch.setattr(database, "get_setting",
                        lambda k, d=None: "context" if k == "regime_admission_mode" else d,
                        raising=False)
    block = dec._build_regime_protocol_block("equity")
    # niente piu' veto di default ne' boost del pavimento NEL PROMPT
    assert "Default = NO_TRADE" not in block
    assert "floor + 0.05" not in block
    assert "+0.10" not in block
    # il regime e' contesto; il gate a valle decide
    assert "CONTESTO" in block
    assert "gate" in block.lower()


def test_context_mode_crypto_still_shorts_downtrend(monkeypatch):
    monkeypatch.setattr(database, "get_setting",
                        lambda k, d=None: "context" if k == "regime_admission_mode" else d,
                        raising=False)
    block = dec._build_regime_protocol_block("crypto").upper()
    assert "SHORTA" in block  # un downtrend confermato si shorta (anche crypto)
