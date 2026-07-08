"""
Test delle "6 mosse" (08/07/2026) — miglioramenti al ragionamento del
Simulator emersi dall'analisi delle run:
  1. Regola di partecipazione al trend nei prompt (bull 0% win)
  2. Advice memory: dedup, archivio debrief, selezione per qualità, prune
  3. Guardrail chiave (R/R, capitulation, winners/losers) nel prompt crypto
  4. Gerarchia rotazione-obbligatoria vs protocollo capitulation
  5. exit_plan obbligatorio, attaccato alla posizione e ripresentato
  6. Anti-ripetizione scenari (least played)
"""
import json
import uuid

import pytest


# ─── Mossa 2: advice memory ─────────────────────────────────────────────


def _mk_lesson(key, title, text="Regola con soglia 30% precisa", **kw):
    return {
        "scenario_category": key,
        "scenario_tags": {"source": "lesson", "lesson_type": "timing"},
        "title": title,
        "text": text,
        "rationale": "perché sì",
        **kw,
    }


def test_save_advice_dedups_similar_titles():
    from agents import sim_advisor as sa
    key = f"testcat__{uuid.uuid4().hex[:8]}"
    a1 = sa.save_advice(_mk_lesson(key, "[TIMING] Sell the news su Ethereum Merge"))
    a2 = sa.save_advice(_mk_lesson(key, "[TIMING] Sell the news su Merge hype"))
    # Il secondo è un clone: NON crea un nuovo record, torna l'id esistente
    assert a1 == a2
    items = sa._load_bucket(key)
    assert len(items) == 1
    assert int(items[0].get("dup_count") or 0) == 1


def test_save_advice_keeps_distinct_lessons():
    from agents import sim_advisor as sa
    key = f"testcat__{uuid.uuid4().hex[:8]}"
    sa.save_advice(_mk_lesson(key, "[TIMING] Sell the news su Merge"))
    sa.save_advice(_mk_lesson(key, "[STOP_LOSS] Trailing stop su altcoin in range"))
    assert len(sa._load_bucket(key)) == 2


def test_quality_selection_puts_lessons_before_logs():
    from agents import sim_advisor as sa
    key = f"testcat__{uuid.uuid4().hex[:8]}"
    # Log di run ([Auto]) salvato DOPO la lezione → con la vecchia selezione
    # per recency vincerebbe lui; con la qualità deve finire in fondo.
    sa.save_advice(_mk_lesson(key, "[SIZE] Max 2% su eventi di contagio"))
    sa.save_advice({
        "scenario_category": key,
        "scenario_tags": {"source": "debrief"},
        "title": "[Auto] Scenario X → RED (P&L -2.0%)",
        "text": "Riassunto della partita andata male con tanti dettagli",
    })
    top = sa.load_advice_for_key(key, max_items=1)
    assert top and top[0]["title"].startswith("[SIZE]")


def test_save_run_log_goes_to_archive_not_bucket():
    from agents import sim_advisor as sa
    key = f"testcat__{uuid.uuid4().hex[:8]}"
    sa.save_run_log({
        "scenario_category": key,
        "title": "[Auto] Scenario Y → GREEN (P&L +1.0%)",
        "text": "Debrief della partita, almeno trenta caratteri di testo",
    })
    assert sa._load_bucket(key) == []          # bucket advice intatto
    raw = sa._settings_get(f"{sa.ADVICE_ARCHIVE_PREFIX}{key}", "[]")
    assert "[Auto]" in raw                      # archivio popolato


def test_prune_moves_logs_and_merges_clones():
    from agents import sim_advisor as sa
    key = f"testcat__{uuid.uuid4().hex[:8]}"
    # Seed manuale del bucket: 1 debrief + 2 lezioni clone + 1 distinta
    items = [
        {"id": "d1", "scenario_category": key, "title": "[Auto] Run → YELLOW (P&L -0.5%)",
         "text": "debrief con abbastanza testo da sembrare vero qui",
         "scenario_tags": {"source": "debrief"}, "created_at": "2026-06-01T00:00:00+00:00"},
        {"id": "l1", "scenario_category": key, "title": "[TIMING] Sell the news su Merge",
         "text": "regola", "scenario_tags": {"source": "lesson"},
         "created_at": "2026-06-02T00:00:00+00:00", "apply_count": 2},
        {"id": "l2", "scenario_category": key, "title": "[TIMING] Sell the news su Merge hype",
         "text": "regola bis", "scenario_tags": {"source": "lesson"},
         "created_at": "2026-06-03T00:00:00+00:00", "apply_count": 3},
        {"id": "l3", "scenario_category": key, "title": "[REGIME] Match portfolio al regime macro",
         "text": "regola distinta", "scenario_tags": {"source": "lesson"},
         "created_at": "2026-06-04T00:00:00+00:00"},
    ]
    sa._settings_set(f"{sa.ADVICE_KEY_PREFIX}{key}", json.dumps(items))
    idx = sa._load_index()
    if key not in idx:
        idx.append(key)
        sa._save_index(idx)

    sa.prune_advice_memory()

    kept = sa._load_bucket(key)
    titles = [it["title"] for it in kept]
    assert not any(t.startswith("[Auto]") for t in titles)   # log archiviato
    assert len(kept) == 2                                     # clone fuso
    merged = next(it for it in kept if it["title"].startswith("[TIMING]"))
    assert int(merged.get("dup_count") or 0) >= 1
    assert int(merged.get("apply_count") or 0) == 5           # 2 + 3 sommati


# ─── Mossa 5: exit_plan end-to-end ──────────────────────────────────────


def test_parse_response_extracts_exit_plan():
    from simulator.v2_engine import _parse_response
    raw = json.dumps({
        "trades": [{"action": "BUY", "asset": "XOM", "allocation_pct": 20,
                     "conviction": "ALTA", "thesis": "t",
                     "rr": "up +4% vs down -2% → 2.0",
                     "exit_plan": "stop: sotto $100 | target: +5%"}],
        "hold_summary": "",
    })
    parsed = _parse_response(raw)
    assert parsed["trades"][0]["exit_plan"] == "stop: sotto $100 | target: +5%"


def test_apply_trades_attaches_exit_plan_to_position():
    from simulator.v2_engine import apply_trades, make_initial_portfolio
    pf = make_initial_portfolio(100000.0, commission_bps=0)
    res = apply_trades(pf, [{"action": "BUY", "asset": "XOM",
                              "allocation_pct": 20, "conviction": "ALTA",
                              "thesis": "t",
                              "exit_plan": "stop: -3% | target: +6%"}],
                        {"XOM": 100.0})
    pos = res["portfolio"]["positions"][0]
    assert pos["exit_plan"] == "stop: -3% | target: +6%"


def test_step_message_re_presents_exit_plan():
    from simulator.v2_engine import (_build_step_message, apply_trades,
                                      compute_portfolio_value,
                                      make_initial_portfolio)
    pf = make_initial_portfolio(100000.0, commission_bps=0)
    pf = apply_trades(pf, [{"action": "BUY", "asset": "XOM",
                             "allocation_pct": 20, "conviction": "ALTA",
                             "thesis": "t",
                             "exit_plan": "stop: -3% | target: +6%"}],
                       {"XOM": 100.0})["portfolio"]
    prices = {"XOM": 102.0}
    valuation = compute_portfolio_value(pf, prices)
    msg = _build_step_message(
        scenario={"asset_universe": ["XOM"], "global_context": {}},
        portfolio=pf, valuation=valuation, prices=prices,
        prev_prices={"XOM": 100.0}, t0_prices={"XOM": 100.0},
        headlines=[], history=[{}], step_index=1, num_steps=3,
        target_date="2024-01-08",
    )
    assert "IL TUO PIANO D'USCITA: stop: -3% | target: +6%" in msg


# ─── Mossa 6: least played ──────────────────────────────────────────────


def test_least_played_choice_prefers_unplayed(monkeypatch):
    from simulator import scenarios as sc
    candidates = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    monkeypatch.setattr(sc, "_load_play_counts",
                        lambda: {"a": 5, "b": 0, "c": 3})
    for _ in range(10):
        assert sc.least_played_choice(candidates)["id"] == "b"


def test_bump_scenario_play_increments():
    from simulator import scenarios as sc
    sid = f"test-scen-{uuid.uuid4().hex[:8]}"
    sc.bump_scenario_play(sid)
    sc.bump_scenario_play(sid)
    assert sc._load_play_counts().get(sid) == 2


# ─── Mosse 1/3/4: contenuto dei prompt ──────────────────────────────────


def test_equity_prompt_has_trend_participation_and_hierarchy():
    from simulator.v2_engine import SIM_V2_SYSTEM_PROMPT as p
    assert "PARTECIPAZIONE AL TREND" in p          # mossa 1
    assert ">= 60%" in p
    assert "GERARCHIA" in p                         # mossa 4
    assert "exit_plan" in p                         # mossa 5
    assert "CHECK PIANI D'USCITA" in p


def test_crypto_prompt_has_guardrails():
    from simulator.v2_crypto_engine import SIM_CRYPTO_SYSTEM_PROMPT as p
    assert "coltello che cade" in p                 # mossa 3 (capitulation)
    assert "FAI CORRERE I VINCITORI" in p           # mossa 3 (6.B)
    assert "1.5" in p                               # mossa 3 (R/R gate)
    assert "PARTECIPAZIONE AL TREND" in p           # mossa 1
    assert "exit_plan" in p                         # mossa 5
