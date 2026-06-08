"""
test_chat_decision_fixes.py — anti-regressione sui fix della chat decisionale:
  1. SHORT/COVER accettati come azioni (prima solo BUY/SELL → short non eseguibili)
  2. detector ticker crypto per il pre-fetch automatico del Technical Crypto
  3. formatter del report technical (helper condiviso)

Funzioni pure, nessuna rete/API.
"""
import agents.chat_decision as cd


# ── 1. SHORT/COVER ───────────────────────────────────────────────────────────
def test_validate_action_accepts_short_and_cover():
    for act in ("BUY", "SELL", "SHORT", "COVER"):
        v = cd._validate_action(
            {"type": "execute_trade", "ticker": "BTC-USD", "action": act, "quantity": 2})
        assert v is not None, f"azione {act} deve essere valida"
        assert v["action"] == act
        assert v["ticker"] == "BTC-USD"
        assert v["quantity"] == 2


def test_validate_action_rejects_unknown_action():
    assert cd._validate_action(
        {"type": "execute_trade", "ticker": "BTC-USD", "action": "FOO", "quantity": 1}) is None
    # quantita' non valida
    assert cd._validate_action(
        {"type": "execute_trade", "ticker": "BTC-USD", "action": "SHORT", "quantity": 0}) is None


# ── 2. Detector ticker crypto ────────────────────────────────────────────────
def test_extract_crypto_tickers_symbols_and_names():
    assert cd._extract_crypto_tickers("come sta BTC oggi?") == ["BTC-USD"]
    assert cd._extract_crypto_tickers("analizza ETH-USD e solana") == ["ETH-USD", "SOL-USD"]
    # nessun ticker → lista vuota (niente pre-fetch inutile)
    assert cd._extract_crypto_tickers("ciao, come va il portafoglio?") == []
    # case-insensitive
    assert cd._extract_crypto_tickers("Ethereum è in trend?") == ["ETH-USD"]


def test_extract_crypto_tickers_none_safe():
    assert cd._extract_crypto_tickers("") == []
    assert cd._extract_crypto_tickers(None) == []


# ── 3. Formatter report technical ────────────────────────────────────────────
def test_format_ta_report_with_analyses():
    rep = {"analyses": [{"ticker": "BTC-USD", "signal": "BULLISH",
                          "rsi_14": 61, "reasoning": "trend su"}],
           "summary": "ok"}
    out = cd._format_ta_report(rep)
    assert out is not None
    assert "BTC-USD" in out and "TECHNICAL AGENT" in out


def test_format_ta_report_error_and_empty():
    assert cd._format_ta_report({"analyses": [], "error": "no data"}) is not None
    # niente analisi, niente errore → None (il caller deciderà la nota)
    assert cd._format_ta_report({"analyses": []}) is None
    assert cd._format_ta_report({}) is None
