"""
Cricchetto a senso unico sullo stop-loss (anti-allargamento).

Su una posizione APERTA lo stop puo' solo STRINGERE (avvicinarsi al prezzo nel
verso che riduce il rischio) o restare; mai allargarsi ne' essere rimosso.
Riproduce il caso NEAR (LONG: SL 2.15 -> 1.95 "per dare respiro pre-FOMC") e
verifica che oggi quel widening venga RIFIUTATO. Tutto mockato (niente DB/rete);
i tightener interni (trailing/teeth/lock-in) NON passano da set_stop_loss e
restano liberi di stringere.
"""
import portfolio
import database


def _patch_pos(monkeypatch, direction, current, existing_sl):
    pos = {"ticker": "TST", "quantity": 10, "direction": direction,
           "current_price": current, "avg_buy_price": current,
           "stop_loss_price": existing_sl}
    monkeypatch.setattr(portfolio, "get_position", lambda t: pos)
    monkeypatch.setattr(portfolio, "_refresh_current_price", lambda t, f: current)
    monkeypatch.setattr(portfolio, "update_position_auto_exit", lambda *a, **k: True)
    monkeypatch.setattr(database, "insert_agent_log", lambda *a, **k: None,
                        raising=False)
    return pos


# ── LONG: il caso NEAR ────────────────────────────────────────────────────────
def test_long_widen_blocked_near_case(monkeypatch):
    # NEAR-like: LONG con SL 2.15, l'agente prova ad allargarlo a 1.95.
    _patch_pos(monkeypatch, "LONG", current=2.16, existing_sl=2.15)
    res = portfolio.set_stop_loss("TST", 1.95, run_id="agent-x")
    assert res["success"] is False
    assert "allarg" in res["reason"].lower()


def test_long_tighten_allowed(monkeypatch):
    # Stringere (avvicinare al prezzo: 2.15 -> 2.20) e' permesso.
    _patch_pos(monkeypatch, "LONG", current=2.30, existing_sl=2.15)
    res = portfolio.set_stop_loss("TST", 2.20, run_id="agent-x")
    assert res["success"] is True, res


def test_long_removal_blocked(monkeypatch):
    # Rimuovere lo stop (=0) e' l'allargamento massimo -> bloccato.
    _patch_pos(monkeypatch, "LONG", current=2.30, existing_sl=2.15)
    res = portfolio.set_stop_loss("TST", 0.0, run_id="agent-x")
    assert res["success"] is False


def test_long_initial_set_allowed(monkeypatch):
    # Nessuno stop preesistente -> il primo set e' libero (entro il sanity-check).
    _patch_pos(monkeypatch, "LONG", current=100.0, existing_sl=0)
    res = portfolio.set_stop_loss("TST", 90.0, run_id="agent-x")
    assert res["success"] is True, res


def test_long_same_value_allowed(monkeypatch):
    # Re-set allo stesso livello (idempotente) -> non e' allargamento.
    _patch_pos(monkeypatch, "LONG", current=2.30, existing_sl=2.15)
    res = portfolio.set_stop_loss("TST", 2.15, run_id="agent-x")
    assert res["success"] is True, res


# ── SHORT: verso opposto ──────────────────────────────────────────────────────
def test_short_widen_blocked(monkeypatch):
    # SHORT: lo stop sta SOPRA il prezzo; allargare = alzarlo (100 -> 105).
    _patch_pos(monkeypatch, "SHORT", current=98.0, existing_sl=100.0)
    res = portfolio.set_stop_loss("TST", 105.0, run_id="agent-x")
    assert res["success"] is False
    assert "allarg" in res["reason"].lower()


def test_short_tighten_allowed(monkeypatch):
    # SHORT: stringere = abbassare lo stop verso il prezzo (100 -> 99).
    _patch_pos(monkeypatch, "SHORT", current=95.0, existing_sl=100.0)
    res = portfolio.set_stop_loss("TST", 99.0, run_id="agent-x")
    assert res["success"] is True, res
