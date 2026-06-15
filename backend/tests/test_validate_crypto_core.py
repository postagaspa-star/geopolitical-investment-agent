"""
#34 — il gate di validazione del Crypto Signal Core non e' piu' vacuo.

Prima l'harness dava al core SOLO segnali concordi col regime (non poteva mai
sbagliare), il legacy era sempre LONG (strawman che perdeva solo nei crash), e
`all_sl` contava anche gli HOLD come PASS → il "gate" della Fase 4 non poteva
fallire. Ora: segnali concordi/discordi/neutri a rotazione, legacy con la STESSA
direzione del core ma senza stop, SL verificato solo sulle vere aperture.
"""
from simulator import validate_crypto_core as vcc
from simulator import metrics


def test_harness_is_not_vacuous():
    _, _, _, _, rows = vcc.build_equity_curves()
    openings = [r for r in rows if r["core_action"] != "HOLD"]
    # Gate non vacuo: ci sono vere aperture e OGNUNA ha uno SL (Gate 3 ora
    # verificato davvero, non piu' tautologico contando gli HOLD come PASS).
    assert len(openings) > 0
    assert all(r["core_has_sl"] for r in openings)
    # Il core affronta scenari DISCORDI e a volte viene stoppato: prima era
    # sempre concorde, quindi non poteva mai sbagliare.
    stopped = [r for r in rows if r["core_outcome"] == "stopped"]
    assert len(stopped) > 0
    # Ci sono anche neutri → HOLD (astensione).
    assert any(r["core_action"] == "HOLD" for r in rows)


def test_core_bounds_drawdown_vs_legacy():
    cc, cl, _, _, _ = vcc.build_equity_curves()
    dd_core = abs(metrics.compute_max_drawdown(cc)["max_drawdown_pct"])
    dd_legacy = abs(metrics.compute_max_drawdown(cl)["max_drawdown_pct"])
    # Il legacy (stessa direzione del core ma SENZA stop) ha un drawdown REALE...
    assert dd_legacy > 0.0
    # ...e il core lo LIMITA: e' la proprieta' che il gate deve dimostrare.
    assert dd_core <= dd_legacy


def test_gate_returns_pass():
    # End-to-end: entrambe le condizioni del gate (SL su ogni apertura +
    # drawdown core <= legacy) devono passare → exit code 0.
    assert vcc.main() == 0
