"""
test_coai_volatility.py — i token ULTRA-VOLATILI (es. COAI) non devono essere
"tagliati" dalla validazione prezzi: uno spike legittimo passa, mentre per gli
altri crypto lo stesso spike viene scartato. Solo garbage estremo è bloccato.
"""
import price_polling as pp


def test_coai_big_spike_up_accepted():
    # +60% (ratio 1.6 vs prev_close): per un crypto normale verrebbe scartato
    # (>1.5), per COAI deve PASSARE (resta nel prezzo/NAV).
    ok, _ = pp._validate_price_tiered("COAI-USD", 1.6, prev_close=1.0,
                                      old_current=1.0, avg=1.0, source="binance_fut")
    assert ok is True


def test_coai_big_drop_accepted():
    # -70% (ratio 0.3): per COAI deve passare (è la sua natura).
    ok, _ = pp._validate_price_tiered("COAI-USD", 0.3, prev_close=1.0,
                                      old_current=1.0, avg=1.0, source="binance_fut")
    assert ok is True


def test_normal_crypto_big_spike_rejected():
    # Lo stesso +60% su un crypto normale resta scartato: l'esenzione è SOLO
    # per gli ultra-volatili.
    ok, _ = pp._validate_price_tiered("BTC-USD", 1.6, prev_close=1.0,
                                      old_current=1.0, avg=1.0, source="binance")
    assert ok is False


def test_coai_extreme_garbage_still_rejected():
    # Decimal-shift / valore assurdo (1000x cost basis): bloccato anche per COAI.
    ok, _ = pp._validate_price_tiered("COAI-USD", 1000.0, prev_close=0.0,
                                      old_current=0.0, avg=1.0, source="binance_fut")
    assert ok is False


def test_coai_zero_still_rejected():
    ok, _ = pp._validate_price_tiered("COAI-USD", 0.0, prev_close=1.0,
                                      old_current=1.0, avg=1.0, source="binance_fut")
    assert ok is False
