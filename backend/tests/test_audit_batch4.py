"""
Round 4 audit — prezzi/metriche/coordinamento.

Bug chiusi:
  18. watchdog._check_position_overweight: una SHORT veniva contata come +cur*qty
      → gonfiava il NAV e poteva far scattare un rebalance trigger assurdo. Ora
      la SHORT sottrae dal NAV ed è esclusa dagli overweight.
  27. tools execute_trade: fill su prezzo CACHED → ora bypass_cache=True (prezzo
      fresco). [inline in handler async → verificato da review + regressione]
  30. sim_advisor.detect_scenario_key: i run v2 (full_data.scenario.market_data,
      senza steps) davano regime sempre 'neutral' → ora fallback su scenario.
  908. watchdog.run_watchdog: urgency "8"/null → TypeError disattivava il ciclo;
      ora coercizione difensiva. [inline in handler → review + regressione]
  auto-mode POST: daily_cap non numerico → 500 con stack trace; ora 400 gestito.
"""
import asyncio
import types

import main
from agents import watchdog, sim_advisor


# ── 18) overweight: SHORT esclusa, NAV firmato ───────────────────────────────

def test_overweight_excludes_short_and_signs_nav():
    db = types.SimpleNamespace()
    db.get_portfolio = lambda: {"cash_balance": 10000.0}
    # LONG piccolo + SHORT enorme allo stesso prezzo: con NAV firmato il LONG
    # risulta overweight; con il vecchio NAV unsigned (gonfiato dalla short) no.
    db.get_positions = lambda: [
        {"ticker": "AAPL", "quantity": 10, "direction": "LONG",
         "current_price": 100.0, "avg_buy_price": 100.0},
        {"ticker": "BTC-USD", "quantity": 100, "direction": "SHORT",
         "current_price": 100.0, "avg_buy_price": 100.0},
    ]
    needs, top, pct, allow = watchdog._check_position_overweight(db)
    tickers = [t for t, _ in allow]
    assert "BTC-USD" not in tickers   # la SHORT non è un overweight "da vendere"
    assert needs is True and top == "AAPL"   # NAV firmato → LONG concentrato


def test_overweight_long_only_normal():
    db = types.SimpleNamespace()
    db.get_portfolio = lambda: {"cash_balance": 100000.0}
    db.get_positions = lambda: [
        {"ticker": "AAPL", "quantity": 10, "direction": "LONG",
         "current_price": 100.0, "avg_buy_price": 100.0},
    ]
    needs, top, pct, allow = watchdog._check_position_overweight(db)
    # 1000 su NAV 101000 ≈ 1% → nessun overweight
    assert needs is False


# ── 30) detect_scenario_key legge i dati dei run v2 ──────────────────────────

def test_detect_scenario_key_reads_v2_market_data(monkeypatch):
    captured = {}
    monkeypatch.setattr(sim_advisor, "_detect_market_regime",
                        lambda md: (captured.__setitem__("md", md), "bear")[1])
    monkeypatch.setattr(sim_advisor, "_detect_asset_class", lambda au: "crypto")
    full = {"category": "macro",
            "full_data": {"scenario": {"market_data": [{"close": 1}],
                                       "asset_universe": ["BTC-USD"]}}}
    key, tags = sim_advisor.detect_scenario_key(full)
    assert captured["md"] == [{"close": 1}]   # prima era [] → regime 'neutral'
    assert key == "macro__bear" and tags["regime"] == "bear"


def test_detect_scenario_key_v1_steps_still_work(monkeypatch):
    captured = {}
    monkeypatch.setattr(sim_advisor, "_detect_market_regime",
                        lambda md: (captured.__setitem__("md", md), "bull")[1])
    monkeypatch.setattr(sim_advisor, "_detect_asset_class", lambda au: "equity")
    full = {"category": "normale",
            "full_data": {"steps": [{"context": {"market_data": [{"close": 9}],
                                                 "asset_universe": ["AAPL"]}}]}}
    key, tags = sim_advisor.detect_scenario_key(full)
    assert captured["md"] == [{"close": 9}]   # ramo V1 invariato


# ── auto-mode POST: validazione daily_cap ────────────────────────────────────

def test_auto_mode_invalid_daily_cap_returns_400():
    from fastapi.responses import JSONResponse
    res = asyncio.run(main.sim_set_auto_mode({"enabled": True, "daily_cap": "abc"}))
    assert isinstance(res, JSONResponse) and res.status_code == 400


def test_auto_mode_valid(monkeypatch):
    from simulator import db as sim_db
    monkeypatch.setattr(sim_db, "set_setting", lambda *a, **k: None)
    res = asyncio.run(main.sim_set_auto_mode({"enabled": True, "daily_cap": 7}))
    assert res["daily_cap"] == 7 and res["enabled"] is True
