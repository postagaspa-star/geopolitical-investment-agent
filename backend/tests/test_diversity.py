"""
test_diversity.py — Step 0: la metrica di diversità calcola correttamente su
log SINTETICI (nessun DB, nessuna rete).
"""
import json

import diversity


def _log(run_id, phase, content, ts):
    return {"run_id": run_id, "phase": phase, "content": json.dumps(content), "timestamp": ts}


def test_extract_tech_worker():
    out = diversity.tickers_from_log(
        "TECH_WORKER", {"analyses_summary": [{"ticker": "NVDA"}, {"ticker": "AAPL"}]})
    assert set(out["analyzed"]) == {"NVDA", "AAPL"}
    assert out["traded"] == []


def test_extract_phase1_uppercases():
    out = diversity.tickers_from_log("DECISION_PHASE1", {"asset_candidates": ["nvda", "GLD"]})
    assert out["analyzed"] == ["NVDA", "GLD"]


def test_extract_trade():
    out = diversity.tickers_from_log("DECISION_TRADE", {"ticker": "gld"})
    assert out["traded"] == ["GLD"]


def test_aggregate_and_metrics_deterministic():
    rows = [
        _log("r1", "DECISION_PHASE1", {"asset_candidates": ["NVDA", "AAPL"]}, "2026-06-01T10:00:00"),
        _log("r1", "TECH_WORKER", {"analyses_summary": [{"ticker": "NVDA"}]}, "2026-06-01T10:01:00"),
        _log("r2", "TECH_WORKER", {"analyses_summary": [{"ticker": "GLD"}]}, "2026-06-02T10:00:00"),
        _log("r2", "DECISION_TRADE", {"ticker": "GLD"}, "2026-06-02T10:05:00"),
        _log("r2", "SCOUT", {"irrelevant": True}, "2026-06-02T09:00:00"),  # ignorata
    ]
    agg = diversity.aggregate_logs(rows)
    # attention: NVDA=2 (phase1+tech), AAPL=1, GLD=1  -> total 4, breadth 3
    assert agg["attention"]["NVDA"] == 2
    assert agg["traded"]["GLD"] == 1

    m = diversity.compute_diversity(agg)
    assert m["total_attention"] == 4
    assert m["breadth"] == 3
    # HHI = (2/4)^2 + (1/4)^2 + (1/4)^2 = 0.375  -> eN = 2.67
    assert m["hhi"] == 0.375
    assert m["effective_n"] == 2.67
    # GLD non è mega -> off_mega = 1/4
    assert m["off_mega_share"] == 0.25
    # nessun crypto; GLD è uno dei 3 ETF commodity → bucket 'commodity_etf'
    assert m["crypto_share"] == 0.0
    assert m["bucket_dist"]["high_movement_equity"] == 3  # NVDA(2)+AAPL(1)
    assert m["bucket_dist"]["commodity_etf"] == 1         # GLD


def test_empty_window():
    m = diversity.compute_diversity(diversity.aggregate_logs([]))
    assert m["breadth"] == 0
    assert m["effective_n"] == 0.0
    assert m["novelty_rate"] is None


def test_malformed_content_does_not_crash():
    rows = [{"run_id": "x", "phase": "TECH_WORKER", "content": "NOT JSON", "timestamp": "2026-06-01"}]
    m = diversity.compute_diversity(diversity.aggregate_logs(rows))
    assert m["breadth"] == 0
