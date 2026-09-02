"""
test_performance_endpoints.py — smoke test degli endpoint del report.

Il modulo di calcolo e' gia' coperto da test_performance_report.py. Qui si
verifica il pezzo che quello non puo' vedere: che gli endpoint HTTP leggano
davvero dal database, che il CSV esca con le intestazioni giuste e con
l'header che fa partire il download, e che i numeri mostrati a schermo e
quelli scaricati vengano dallo stesso calcolo.

Il benchmark viene sostituito con un valore finto: chiamare l'S&P 500 vero
significherebbe una richiesta di rete dentro un test.
"""
import pytest
from starlette.testclient import TestClient

import database
import main


@pytest.fixture()
def client():
    return TestClient(main.app)


@pytest.fixture()
def portafoglio_con_trade():
    """Due round-trip chiusi + una posizione ancora aperta, piu' qualche
    snapshot del valore: abbastanza per far girare tutte le sezioni."""
    database.set_setting("commission_bps", "10")
    database.insert_trade("AAPL", "BUY", 10, 100.0, "geo", "tech",
                          "compro AAPL", 80.0)
    database.insert_trade("AAPL", "SELL", 10, 110.0, "geo", "tech",
                          "vendo AAPL", 82.0)
    database.insert_trade("MSFT", "BUY", 5, 300.0, "geo", "tech",
                          "compro MSFT", 70.0)
    database.upsert_position("MSFT", 5, 300.0, current_price=310.0)
    for value in (100000.0, 100200.0, 100450.0):
        database.insert_portfolio_snapshot(value, value / 2)
    yield


@pytest.fixture(autouse=True)
def benchmark_finto(monkeypatch):
    """Niente rete nei test: l'S&P 500 e' un valore fisso."""
    async def _fake(period="all"):
        return {
            "available": True,
            "period": period,
            "portfolio_return_pct": 4.5,
            "sp500_return_pct": 2.0,
            "alpha_pct": 2.5,
            "portfolio_max_drawdown_pct": -3.0,
            "sp500_max_drawdown_pct": -2.0,
            "verdict": "alpha_plausibile",
            "verdict_detail": "dettaglio di prova",
            "portfolio_series_norm": [100.0, 102.0, 104.5],
            "sp500_series_norm": [100.0, 101.0, 102.0],
        }
    monkeypatch.setattr(main, "get_portfolio_benchmark", _fake)


# ── /api/live/performance ───────────────────────────────────────────────────

def test_performance_risponde_con_tutte_le_sezioni(client, portafoglio_con_trade):
    res = client.get("/api/live/performance?period=all")
    assert res.status_code == 200
    body = res.json()
    for section in ("sintesi", "metriche_nav", "statistiche_operazioni",
                    "commissioni", "mensile", "per_strumento", "curva_valore",
                    "transazioni", "operazioni", "benchmark", "avvertenze"):
        assert section in body, f"sezione mancante: {section}"
    assert body["sintesi"]["transazioni_totali"] == 3
    assert body["sintesi"]["operazioni_chiuse"] == 1
    assert body["benchmark"]["alpha_pct"] == 2.5


def test_performance_su_database_vuoto_non_esplode(client):
    res = client.get("/api/live/performance?period=all")
    assert res.status_code == 200
    assert res.json()["sintesi"]["transazioni_totali"] == 0


def test_performance_dichiara_quando_taglia_le_righe(client, portafoglio_con_trade):
    """Con max_rows basso la pagina deve sapere che sta vedendo una parte, non
    il totale: altrimenti mostra "3 transazioni" quando sono di piu'."""
    res = client.get("/api/live/performance?period=all&max_rows=50")
    trunc = res.json()["troncato"]
    assert trunc["transazioni_totali"] == 3
    assert trunc["transazioni_mostrate"] == 3
    assert trunc["limite"] == 50


def test_performance_usa_la_commissione_delle_impostazioni(client,
                                                           portafoglio_con_trade):
    """Cambiare la commissione in Impostazioni deve cambiare il report. E' il
    motivo per cui il valore non sta scritto a mano da nessuna parte."""
    base = client.get("/api/live/performance?period=all").json()
    database.set_setting("commission_bps", "100")     # 1% per gamba
    alzata = client.get("/api/live/performance?period=all").json()

    assert alzata["commissioni"]["commissione_bps"] == 100.0
    assert (alzata["commissioni"]["commissioni_totali_usd"]
            > base["commissioni"]["commissioni_totali_usd"])
    assert (alzata["sintesi"]["pnl_realizzato_netto_usd"]
            < base["sintesi"]["pnl_realizzato_netto_usd"])


def test_commissione_esposta_al_frontend(client):
    database.set_setting("commission_bps", "25")
    body = client.get("/api/live/commission").json()
    assert body["commission_bps"] == 25.0
    assert body["commission_pct"] == 0.25


# ── /api/live/performance/export ────────────────────────────────────────────

def _csv_lines(res):
    text = res.text
    if text.startswith("﻿"):
        text = text[1:]
    return [ln for ln in text.split("\r\n") if ln]


def test_export_transazioni_scarica_un_csv(client, portafoglio_con_trade):
    res = client.get("/api/live/performance/export?dataset=transazioni&period=all")
    assert res.status_code == 200
    assert "text/csv" in res.headers["content-type"]
    assert "attachment" in res.headers["content-disposition"]
    assert ".csv" in res.headers["content-disposition"]
    # BOM: senza, Excel storpia gli accenti.
    assert res.text.startswith("﻿")

    lines = _csv_lines(res)
    header = lines[0].split(",")
    assert "commissione_usd" in header
    assert "flusso_cassa_usd" in header
    assert len(lines) == 4          # intestazione + 3 transazioni


def test_export_copre_tutti_i_blocchi(client, portafoglio_con_trade):
    for dataset in ("transazioni", "operazioni", "curva_valore", "mensile",
                    "per_strumento", "sintesi", "benchmark"):
        res = client.get(
            f"/api/live/performance/export?dataset={dataset}&period=all")
        assert res.status_code == 200, dataset
        assert _csv_lines(res), f"{dataset}: file vuoto"


def test_export_separatore_punto_e_virgola(client, portafoglio_con_trade):
    res = client.get(
        "/api/live/performance/export?dataset=transazioni&period=all&sep=;")
    assert ";" in _csv_lines(res)[0]


def test_export_rifiuta_un_dataset_inventato(client):
    res = client.get("/api/live/performance/export?dataset=pippo")
    assert res.status_code == 400
    assert "disponibili" in res.json()


def test_csv_e_pagina_raccontano_gli_stessi_numeri(client, portafoglio_con_trade):
    """Il rischio vero di avere due strade per gli stessi dati e' che
    divergano. Il totale delle commissioni nel CSV deve coincidere con quello
    mostrato a schermo."""
    body = client.get("/api/live/performance?period=all").json()
    res = client.get("/api/live/performance/export?dataset=transazioni&period=all")

    header, *rows = _csv_lines(res)
    idx = header.split(",").index("commissione_usd")
    totale_csv = sum(float(r.split(",")[idx] or 0) for r in rows)

    assert round(totale_csv, 2) == body["commissioni"]["commissioni_totali_usd"]
