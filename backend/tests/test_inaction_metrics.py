"""
Misurare l'omissione.

Ogni altra analytics del sistema ha per denominatore il numero di TRADE: se
l'agente smette di operare non produce metriche cattive, non ne produce
affatto, e l'edge tracker resta su "insufficient_data". Sbagliare agendo
lascia una traccia (commissione, stop, log di rifiuto); sbagliare non agendo
non lascia nulla. Questi test coprono la lettura dell'altra coda.

La distinzione centrale: "ho valutato il mercato e non c'era niente" e' un
giudizio; "non ho nemmeno valutato" e' un guasto o un throttle. Prima
finivano indistintamente in "il watchdog non ha triggerato".
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

import inaction_metrics as im


NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)


def _wd(reason, minutes_ago, trigger=False, event="watchdog_complete"):
    return {
        "phase": "WATCHDOG",
        "timestamp": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
        "content": json.dumps({"event": event, "trigger": trigger,
                               "reason": reason}),
    }


# ── Classificazione ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("reason,expected", [
    ("http_400", "guasto"),
    ("json_parse_error", "guasto"),
    ("no_deepseek_key", "guasto"),
    ("decision_failing_backoff", "guasto"),
    ("decision_ran_recently", "throttle"),
    ("decision_possibly_running", "throttle"),
    ("all_agents_busy", "throttle"),
    ("market_closed_no_recent_news", "risparmio"),
    ("No trigger condition met: equity moves <2.5%", "giudizio"),
])
def test_classificazione_dei_motivi(reason, expected):
    assert im.classify_reason(reason) == expected


def test_evento_di_fallimento_llm_e_guasto():
    assert im.classify_reason("price_gate: calmo", "watchdog_llm_failed") == "guasto"


# ── Il quadro d'insieme ─────────────────────────────────────────────────────

def test_breakdown_distingue_valutazione_da_silenzio():
    logs = ([_wd("http_400", i) for i in range(10)]
            + [_wd("decision_ran_recently", i) for i in range(10, 20)]
            + [_wd("no significant move", i) for i in range(20, 24)]
            + [_wd("LMT +3%", 25, trigger=True)])
    out = im.watchdog_breakdown(logs, hours=24, now=NOW)

    assert out["cycles"] == 25
    assert out["triggered"] == 1
    assert out["by_kind"]["guasto"] == 10
    assert out["by_kind"]["throttle"] == 10
    assert out["by_kind"]["giudizio"] == 4
    # 4 giudizi + 1 trigger su 25 cicli
    assert out["evaluated_pct"] == pytest.approx(20.0)


def test_salute_segnala_il_guasto():
    logs = [_wd("http_400", i) for i in range(20)]
    assert "GUASTO" in im.watchdog_breakdown(logs, hours=24, now=NOW)["health"]


def test_salute_segnala_il_silenzio_da_throttle():
    logs = [_wd("decision_ran_recently", i) for i in range(20)]
    out = im.watchdog_breakdown(logs, hours=24, now=NOW)
    assert "SILENZIOSO" in out["health"]


def test_salute_ok_quando_il_sistema_valuta():
    logs = [_wd("nothing relevant", i) for i in range(20)]
    assert im.watchdog_breakdown(logs, hours=24, now=NOW)["health"] == "ok"


def test_fuori_finestra_escluso():
    logs = [_wd("http_400", 60 * 48)]
    assert im.watchdog_breakdown(logs, hours=24, now=NOW)["cycles"] == 0


def test_log_di_altre_fasi_ignorati():
    logs = [{"phase": "SCOUT", "timestamp": NOW.isoformat(), "content": "{}"}]
    assert im.watchdog_breakdown(logs, hours=24, now=NOW)["cycles"] == 0


def test_contenuti_corrotti_non_esplodono():
    logs = [{"phase": "WATCHDOG", "timestamp": NOW.isoformat(), "content": "non-json"},
            {"phase": "WATCHDOG", "timestamp": "boh", "content": "{}"},
            _wd("http_400", 1)]
    assert im.watchdog_breakdown(logs, hours=24, now=NOW)["cycles"] == 1


def test_riproduce_il_caso_di_produzione():
    """
    Proporzioni reali dei log del 24-25/07: 250 guasti, 229 throttle, 27
    giudizi, 6 trigger su 512 cicli. Il 94% dei "non svegliare" non era una
    valutazione del mercato.
    """
    logs = ([_wd("http_400", i % 1000) for i in range(250)]
            + [_wd("decision_ran_recently", i % 1000) for i in range(229)]
            + [_wd("no trigger condition met", i % 1000) for i in range(27)]
            + [_wd("BTC +4%", i, trigger=True) for i in range(6)])
    out = im.watchdog_breakdown(logs, hours=48, now=NOW)
    assert out["cycles"] == 512
    assert out["by_kind"]["guasto"] == 250
    assert out["by_kind"]["throttle"] == 229
    assert out["evaluated_pct"] < 7.0
    assert "GUASTO" in out["health"]


# ── Portafoglio fermo ───────────────────────────────────────────────────────

def test_riassunto_inazione():
    trades = [{"ticker": "AAA", "timestamp": (NOW - timedelta(days=4)).isoformat()}]
    state = {"cash": 76_000, "total_value": 100_000, "positions": [{}, {}]}
    commitments = ([{"status": "expired"}] * 7 + [{"status": "triggered"}] * 3
                   + [{"status": "active"}] * 2)

    out = im.inaction_summary(trades, state, commitments, now=NOW)
    assert out["days_since_last_trade"] == pytest.approx(4.0, abs=0.1)
    assert out["trades_last_7d"] == 1
    assert out["cash_pct"] == pytest.approx(76.0)
    assert out["open_positions"] == 2
    # 7 promesse scadute su 10 risolte: "mi pongo un livello e aspetto".
    assert out["commitments"]["expired_unfulfilled"] == 7
    assert out["commitments"]["lapse_pct"] == pytest.approx(70.0)


def test_riassunto_senza_storia():
    out = im.inaction_summary([], {}, [], now=NOW)
    assert out["days_since_last_trade"] is None
    assert out["commitments"]["lapse_pct"] is None


# ── Costo opportunita' ──────────────────────────────────────────────────────

def test_costo_opportunita():
    out = im.opportunity_cost(76_000, 1.5)
    assert out["available"] is True
    assert out["opportunity_cost_usd"] == pytest.approx(1140.0)


def test_costo_opportunita_negativo_se_il_mercato_scende():
    """Il segno e' informativo: stare liquidi in un ribasso ha reso."""
    assert im.opportunity_cost(76_000, -2.0)["opportunity_cost_usd"] < 0


def test_costo_opportunita_non_inventa_numeri():
    assert im.opportunity_cost(76_000, None)["available"] is False
