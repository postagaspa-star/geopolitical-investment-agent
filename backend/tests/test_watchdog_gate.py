"""
Il cancello di risveglio: un guasto non deve travestirsi da giudizio.

Contesto (produzione, 24-25/07/2026). Su 512 cicli del watchdog:
  - 250 usciti con reason="http_400" (modelli DeepSeek dismessi)
  - 229 usciti con "decision_ran_recently"
  -  ~30 giudizi veri sul mercato
Il 94% dei "non svegliare" non era una valutazione del mercato. Peggio: i due
guasti si alimentavano fra loro, perche' un Decision FALLITO contava come
"aver deciso" e comprava 2h di throttle (4h col backoff su errori).

Qui si verifica che:
  1. quando l'LLM non risponde, il sistema guarda comunque i prezzi;
  2. fallire non compra piu' silenzio.
"""
from datetime import datetime, timedelta, timezone

import pytest

from agents import watchdog


# ══════════════════════════════════════════════════════════════════════════
# Cancello deterministico (funzione pura)
# ══════════════════════════════════════════════════════════════════════════

def test_gate_senza_prezzi_non_triggera():
    out = watchdog.evaluate_price_gate({})
    assert out["trigger"] is False
    assert "nessun prezzo" in out["reason"]


def test_gate_mercato_calmo_non_triggera():
    out = watchdog.evaluate_price_gate({"SPY": {"chg_pct": 0.4}})
    assert out["trigger"] is False
    # Il motivo deve dire quanto e' stata la mossa: serve a distinguere
    # "ho guardato e non c'era niente" da "non ho guardato".
    assert "0.40%" in out["reason"]


def test_gate_triggera_su_watchlist_sopra_soglia():
    out = watchdog.evaluate_price_gate({"SPY": {"chg_pct": 3.5}})
    assert out["trigger"] is True
    assert out["focus_tickers"] == ["SPY"]
    assert out["urgency"] >= watchdog.URGENCY_THRESHOLD


def test_gate_soglia_piu_bassa_sulle_posizioni_aperte():
    quote = {"NVDA": {"chg_pct": -2.2}}
    # Come semplice nome osservato, 2.2% non basta (soglia 3%).
    assert watchdog.evaluate_price_gate(quote)["trigger"] is False
    # Come posizione aperta si': li' il movimento richiede una decisione.
    held = watchdog.evaluate_price_gate(quote, ["NVDA"])
    assert held["trigger"] is True
    assert held["urgency"] > watchdog.URGENCY_THRESHOLD


def test_gate_da_precedenza_alle_posizioni_aperte():
    """Un 5% su un nome che non ho conta meno di un 2.1% su uno che ho."""
    out = watchdog.evaluate_price_gate(
        {"SPY": {"chg_pct": 5.0}, "NVDA": {"chg_pct": -2.1}}, ["NVDA"])
    assert out["focus_tickers"] == ["NVDA"]


def test_gate_ignora_quote_corrotte_senza_esplodere():
    out = watchdog.evaluate_price_gate(
        {"X": {"chg_pct": None}, "Y": {"chg_pct": "boh"}, "Z": {"chg_pct": 3.9}})
    assert out["trigger"] is True
    assert out["focus_tickers"] == ["Z"]


def test_gate_urgency_mai_sopra_il_cap():
    """Il 10 resta ai trigger di rebalance forzato."""
    out = watchdog.evaluate_price_gate({"AAA": {"chg_pct": 80.0}}, ["AAA"])
    assert out["urgency"] <= 9


# ══════════════════════════════════════════════════════════════════════════
# Throttle: successo, guasto e run in corso sono cose diverse
# ══════════════════════════════════════════════════════════════════════════

def _minutes_ago(n):
    return datetime.now(timezone.utc) - timedelta(minutes=n)


@pytest.fixture
def wd(monkeypatch):
    """Isola _is_throttled dal DB: si controllano i tre segnali a mano."""
    state = {"success": None, "start": None, "errors": 0, "last_error": None,
             "degraded": []}

    monkeypatch.setattr(watchdog, "_get_last_decision_success",
                        lambda db: state["success"])
    monkeypatch.setattr(watchdog, "_get_last_decision_start",
                        lambda db: state["start"])
    monkeypatch.setattr(watchdog, "_count_recent_decision_errors",
                        lambda db, minutes=60: state["errors"])
    monkeypatch.setattr(watchdog, "_last_log_time",
                        lambda db, phases: state["last_error"])
    monkeypatch.setattr(watchdog, "_log_degraded",
                        lambda db, kind, detail: state["degraded"].append(kind))
    return state


def test_nessuna_storia_non_throttla(wd):
    throttled, reason = watchdog._is_throttled(None)
    assert throttled is False and reason == ""


def test_successo_recente_throttla(wd):
    wd["success"] = _minutes_ago(30)
    throttled, reason = watchdog._is_throttled(None, throttle_minutes=120)
    assert throttled is True
    assert reason == "decision_ran_recently"


def test_successo_vecchio_non_throttla(wd):
    wd["success"] = _minutes_ago(200)
    assert watchdog._is_throttled(None, throttle_minutes=120)[0] is False


def test_un_singolo_errore_non_compra_silenzio(wd):
    """IL BUG DEL 24/07: un Decision fallito valeva come uno riuscito."""
    wd["errors"] = 1
    wd["last_error"] = _minutes_ago(5)
    wd["success"] = None          # nessuna decisione e' mai riuscita
    throttled, reason = watchdog._is_throttled(None)
    assert throttled is False, (
        "un fallimento non e' una decisione: non deve zittire il watchdog")


def test_guasto_ripetuto_rallenta_ma_lo_dichiara(wd):
    """Il backoff resta (non si martella un'API rotta) ma diventa rumoroso."""
    wd["errors"] = 3
    wd["last_error"] = _minutes_ago(5)
    throttled, reason = watchdog._is_throttled(None)
    assert throttled is True
    assert reason == "decision_failing_backoff"
    # Deve lasciare traccia interrogabile: e' cio' che mancava del tutto.
    assert "decision_failing" in wd["degraded"]


def test_backoff_molto_piu_corto_di_prima(wd):
    """Era 240 min. Un guasto non deve comprare mezza giornata di silenzio."""
    assert watchdog.ERROR_BACKOFF_MIN <= 60
    wd["errors"] = 5
    wd["last_error"] = _minutes_ago(watchdog.ERROR_BACKOFF_MIN + 1)
    assert watchdog._is_throttled(None)[0] is False


def test_run_in_corso_evita_il_doppio_decision(wd):
    """Il bug storico opposto: due Decision a un minuto di distanza."""
    wd["start"] = _minutes_ago(3)
    wd["success"] = None
    throttled, reason = watchdog._is_throttled(None)
    assert throttled is True
    assert reason == "decision_possibly_running"


def test_run_completato_dopo_lo_start_non_e_in_corso(wd):
    wd["start"] = _minutes_ago(10)
    wd["success"] = _minutes_ago(8)      # completato
    throttled, reason = watchdog._is_throttled(None, throttle_minutes=5)
    assert throttled is False


def test_run_bloccato_non_zittisce_per_ore(wd):
    """Un run avviato e mai finito e' morto, non in esecuzione."""
    wd["start"] = _minutes_ago(watchdog.CONCURRENCY_GUARD_MIN + 5)
    wd["success"] = None
    assert watchdog._is_throttled(None)[0] is False
