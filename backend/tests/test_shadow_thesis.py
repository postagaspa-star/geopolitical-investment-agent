"""
L'agente ombra: previsioni senza soldi, contro un gemello cieco.

Cosa deve essere vero perche' il banco di prova sia onesto:
  - il gemello casuale e' riproducibile (seme = data) e ha la stessa
    composizione delle previsioni vere: niente ripescaggi fortunati;
  - i dati sporchi vengono esclusi, mai usati;
  - una previsione senza barre di prezzo si annulla (VOID), non si inventa;
  - nessun verdetto sotto la soglia minima di previsioni chiuse.
"""
from datetime import date, timedelta

import pytest

import shadow_thesis as sh


# ── Giorni di borsa ─────────────────────────────────────────────────────────

def test_weekend_non_e_giorno_di_borsa():
    assert sh.is_us_trading_day("2026-07-25") is False   # sabato
    assert sh.is_us_trading_day("2026-07-26") is False   # domenica
    assert sh.is_us_trading_day("2026-07-27") is True    # lunedi


def test_festivita_esclusa():
    assert sh.is_us_trading_day("2026-12-25", {"2026-12-25"}) is False


def test_data_illeggibile_non_esplode():
    assert sh.is_us_trading_day("boh") is False


# ── Selezione delle righe del radar ─────────────────────────────────────────

def _rows(n=30):
    return [{"ticker": f"T{i:02d}", "rotation_score": float(n - i)}
            for i in range(n)]


def test_righe_sporche_escluse():
    rows = _rows(5) + [{"ticker": "BAD", "rotation_score": 99.0,
                        "data_quality": "corrupted_clone"},
                       {"ticker": "NAN", "rotation_score": "boh"}]
    valid = sh.valid_scan_rows({"rows": rows})
    tickers = {r["ticker"] for r in valid}
    assert "BAD" not in tickers and "NAN" not in tickers
    assert len(valid) == 5


# ── Previsioni vere e gemello cieco ─────────────────────────────────────────

def test_previsioni_vere_top_e_bottom():
    rows = sh.valid_scan_rows({"rows": _rows(30)})
    out = sh.build_real_theses(rows, "2026-07-27")
    longs = [t for t in out if t["direction"] == "LONG"]
    shorts = [t for t in out if t["direction"] == "SHORT"]
    assert [t["ticker"] for t in longs] == ["T00", "T01", "T02"]      # i piu' forti
    assert [t["ticker"] for t in shorts] == ["T27", "T28", "T29"]     # i piu' deboli
    assert all(t["kind"] == "real" for t in out)


def test_gemello_riproducibile_e_bilanciato():
    rows = sh.valid_scan_rows({"rows": _rows(30)})
    a = sh.build_random_twin(rows, "2026-07-27")
    b = sh.build_random_twin(rows, "2026-07-27")
    assert a == b, "stesso giorno = stessa estrazione: niente ripescaggi"
    c = sh.build_random_twin(rows, "2026-07-28")
    assert c != a, "giorno diverso = estrazione diversa"
    assert sum(1 for t in a if t["direction"] == "LONG") == sh.N_LONG
    assert sum(1 for t in a if t["direction"] == "SHORT") == sh.N_SHORT
    universe = {r["ticker"] for r in rows}
    assert all(t["ticker"] in universe for t in a), "solo titoli validi quel giorno"
    assert len({t["ticker"] for t in a}) == sh.N_LONG + sh.N_SHORT


# ── Risoluzione ─────────────────────────────────────────────────────────────

def _bars(start="2026-07-27", closes=(100, 101, 102, 103, 104, 106, 107)):
    d = date.fromisoformat(start)
    out = []
    for i, c in enumerate(closes):
        # solo giorni feriali, per realismo
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append({"date": d.isoformat(), "close": float(c)})
        d += timedelta(days=1)
    return out


def _thesis(direction="LONG", day="2026-07-27", kind="real"):
    return {"id": f"x-{direction}", "date": day, "ticker": "T00",
            "direction": direction, "kind": kind,
            "horizon_sessions": sh.HORIZON_SESSIONS}


def test_long_azzeccato():
    out = sh.resolve_thesis(_thesis("LONG"), _bars(), "2026-08-20")
    assert out["status"] == "RESOLVED"
    assert out["hit"] is True
    assert out["ret_pct"] == pytest.approx(6.0)      # 100 -> 106 in 5 sedute


def test_short_sbagliato_se_il_prezzo_sale():
    out = sh.resolve_thesis(_thesis("SHORT"), _bars(), "2026-08-20")
    assert out["status"] == "RESOLVED" and out["hit"] is False


def test_short_azzeccato_se_il_prezzo_scende():
    bars = _bars(closes=(100, 99, 98, 97, 96, 95, 94))
    out = sh.resolve_thesis(_thesis("SHORT"), bars, "2026-08-20")
    assert out["hit"] is True


def test_ancora_presto_resta_aperta():
    bars = _bars(closes=(100, 101, 102))             # solo 2 sedute dopo
    assert sh.resolve_thesis(_thesis(), bars, "2026-07-30") is None


def test_senza_barre_da_troppo_tempo_si_annulla():
    out = sh.resolve_thesis(_thesis(day="2026-07-01"), [], "2026-07-26")
    assert out["status"] == "VOID"


def test_barre_sporche_normalizzate():
    raw = [{"date": "2026-07-27", "close": 100},
           {"date": "2026-07-27", "close": 100.5},   # doppione: vince l'ultimo
           {"date": "2026-07-28T00:00:00", "close": 101},
           {"date": "2026-07-29", "close": 0},        # chiusura nulla: fuori
           {"date": "", "close": 102}]
    bars = sh.normalize_bars(raw)
    assert [b["date"] for b in bars] == ["2026-07-27", "2026-07-28"]
    assert bars[0]["close"] == 100.5


# ── Pagella e confronto ─────────────────────────────────────────────────────

def _resolved(n, hit_rate, kind="real", direction="LONG"):
    out = []
    for i in range(n):
        hit = i < int(n * hit_rate)
        ret = 2.0 if hit else -2.0
        if direction == "SHORT":
            ret = -ret
        out.append({"id": f"r{i}", "status": "RESOLVED", "kind": kind,
                    "direction": direction, "ret_pct": ret, "hit": hit,
                    "date": "2026-07-27", "ticker": "T"})
    return out


def test_pagella_short_col_segno_giusto():
    s = sh.summarize(_resolved(10, 1.0, direction="SHORT"))
    assert s["hit_rate_pct"] == 100.0
    assert s["avg_ret_pct"] > 0, "uno short azzeccato e' un guadagno"


def test_nessun_verdetto_sotto_soglia():
    out = sh.compare(_resolved(10, 0.9), _resolved(10, 0.1, kind="random"))
    assert out["verdict"] == "campione_insufficiente"


def test_differenza_piccola_e_indistinguibile():
    out = sh.compare(_resolved(60, 0.52), _resolved(60, 0.48, kind="random"))
    assert out["verdict"] == "indistinguibile_dal_caso"


def test_differenza_grande_da_verdetto():
    out = sh.compare(_resolved(100, 0.75), _resolved(100, 0.45, kind="random"))
    assert out["verdict"] == "radar_batte_il_caso"


def test_radar_peggio_del_caso_viene_detto():
    out = sh.compare(_resolved(100, 0.30), _resolved(100, 0.55, kind="random"))
    assert out["verdict"] == "caso_batte_il_radar"


# ── Lettura dello stato dai log ─────────────────────────────────────────────

def test_lettura_stato_dai_log():
    import json
    logs = [
        {"phase": "SHADOW_THESIS", "content": json.dumps(_thesis())},
        {"phase": "SHADOW_RESOLVED",
         "content": json.dumps({"id": "x-LONG", "status": "RESOLVED"})},
        {"phase": "WATCHDOG", "content": "{}"},
        {"phase": "SHADOW_THESIS", "content": "non-json"},
    ]
    theses, resolved = sh.load_shadow_state(logs)
    assert len(theses) == 1 and len(resolved) == 1
