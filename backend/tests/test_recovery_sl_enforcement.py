"""
#13 Recovery enforcement + #14 SL range equity — ora applicati in CODICE.

Prima i vincoli recovery (confidence >= 0.75, SL piu' stretto) e la validazione
del range SL su equity vivevano solo nel prompt: il governor non li leggeva mai.
Questi test bloccano la regressione: in recovery il floor di confidence sale, lo
SL massimo si stringe, e l'auto-set d'ufficio non produce mai uno SL che la
validazione poi rifiuterebbe (range effettivo = unica fonte di verita').
"""
import risk_profile as rp
import risk_state


_PROFILE_CONSERVATIVO = {
    "label": "Conservativo", "min_confidence": 0.80,
    "max_position_pct_equity": 100.0, "max_position_pct_crypto": 100.0,
    "max_open_positions": 99, "max_open_positions_crypto": 99,
    "max_portfolio_drawdown_pct": 99.0,
    "sl_min_pct_equity": 4.0, "sl_max_pct_equity": 8.0,
    "sl_min_pct_crypto": 8.0, "sl_max_pct_crypto": 15.0,
}
_PROFILE_MODERATO = {
    "label": "Moderato", "min_confidence": 0.65,
    "max_position_pct_equity": 100.0, "max_position_pct_crypto": 100.0,
    "max_open_positions": 99, "max_open_positions_crypto": 99,
    "max_portfolio_drawdown_pct": 99.0,
    "sl_min_pct_equity": 7.0, "sl_max_pct_equity": 15.0,
    "sl_min_pct_crypto": 12.0, "sl_max_pct_crypto": 25.0,
}


def test_effective_sl_bounds_recovery_clamps_and_never_inverts(monkeypatch):
    monkeypatch.setattr(rp, "get_active_profile", lambda: _PROFILE_CONSERVATIVO)
    monkeypatch.setattr(risk_state, "is_in_recovery_mode", lambda: True)
    # equity [4,8]: il max scende a 5 (recovery)
    assert rp.effective_sl_bounds("equity") == (4.0, 5.0)
    # crypto [8,15]: sl_min 8 > rec_max 5 → collassa a [8,8], MAI invertito
    lo, hi = rp.effective_sl_bounds("crypto")
    assert lo == 8.0 and hi == 8.0 and hi >= lo
    # fuori recovery: range invariato
    monkeypatch.setattr(risk_state, "is_in_recovery_mode", lambda: False)
    assert rp.effective_sl_bounds("equity") == (4.0, 8.0)


def test_validate_trade_recovery_raises_confidence_floor(monkeypatch):
    monkeypatch.setattr(rp, "get_active_profile", lambda: _PROFILE_MODERATO)
    monkeypatch.setattr(risk_state, "is_in_recovery_mode", lambda: True)
    # 0.70: ok col floor base 0.65, ma in recovery il floor e' 0.75 → rifiutato
    ok, why = rp.validate_trade(asset_class="equity", confidence=0.70,
                                allocation_pct=1.0, open_positions_count=0)
    assert not ok and "0.75" in why and "RECOVERY" in why
    # 0.80 passa anche in recovery
    ok2, _ = rp.validate_trade(asset_class="equity", confidence=0.80,
                               allocation_pct=1.0, open_positions_count=0)
    assert ok2
    # override esplicito recovery=False → torna il floor base 0.65 (0.70 passa)
    ok3, _ = rp.validate_trade(asset_class="equity", confidence=0.70,
                               allocation_pct=1.0, open_positions_count=0,
                               recovery=False)
    assert ok3


def test_validate_sl_range_recovery_rejects_wide_sl(monkeypatch):
    monkeypatch.setattr(rp, "get_active_profile", lambda: _PROFILE_CONSERVATIVO)
    monkeypatch.setattr(risk_state, "is_in_recovery_mode", lambda: True)
    # entry 100, SL 93 = 7%: fuori recovery ok (<=8), in recovery NO (>5)
    ok, _ = rp.validate_sl_range(asset_class="equity", entry_price=100.0, sl_price=93.0)
    assert not ok
    # SL 95.5 = 4.5% → dentro [4,5]
    ok2, _ = rp.validate_sl_range(asset_class="equity", entry_price=100.0, sl_price=95.5)
    assert ok2
    # fuori recovery 7% torna valido
    monkeypatch.setattr(risk_state, "is_in_recovery_mode", lambda: False)
    ok3, _ = rp.validate_sl_range(asset_class="equity", entry_price=100.0, sl_price=93.0)
    assert ok3


def test_autoset_midpoint_always_passes_validation(monkeypatch):
    # Invariante #14: il midpoint del range EFFETTIVO e' sempre dentro il range,
    # quindi l'auto-set d'ufficio (equity e crypto) non produce mai uno SL che
    # validate_sl_range poi rifiuta — nemmeno in recovery (dove il max si stringe).
    for prof in (_PROFILE_CONSERVATIVO, _PROFILE_MODERATO):
        monkeypatch.setattr(rp, "get_active_profile", lambda p=prof: p)
        for rec in (True, False):
            monkeypatch.setattr(risk_state, "is_in_recovery_mode", lambda r=rec: r)
            for ac in ("equity", "crypto"):
                lo, hi = rp.effective_sl_bounds(ac)
                mid = (lo + hi) / 2.0
                entry = 100.0
                for side, sl in (("long", entry * (1 - mid / 100.0)),
                                 ("short", entry * (1 + mid / 100.0))):
                    ok, why = rp.validate_sl_range(asset_class=ac, entry_price=entry,
                                                   sl_price=sl, side=side)
                    assert ok, f"{ac} {side} rec={rec} mid={mid}: {why}"
