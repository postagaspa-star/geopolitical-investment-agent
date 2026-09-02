"""
test_startup_without_db.py — l'app deve partire anche se il database non risponde.

Il 2 settembre 2026 un deploy e' morto con "Application startup failed.
Exiting." (exit 3): database.init_db() era la prima riga del lifespan, senza
protezione, e Supabase non si risolveva ("[Errno -2] Name or service not
known"). Un'interruzione del database diventava un sito completamente giu',
senza nessuna diagnostica per chi guarda la dashboard. Qui si pretende che:
  - l'avvio non esploda;
  - /health risponda 503 "degraded" con il motivo in parole;
  - lo scheduler NON parta (tradare senza database brucia crediti e non
    persiste nulla);
  - quando il database torna, l'app se ne accorga da sola e avvii lo scheduler.
"""
import time

import pytest
from starlette.testclient import TestClient

import main


class _Registro:
    """Conta le chiamate a start_scheduler senza far partire niente."""
    def __init__(self):
        self.avvii = 0

    def start(self):
        self.avvii += 1

    def stop(self, persist=False):
        pass


@pytest.fixture()
def scheduler_finto(monkeypatch):
    reg = _Registro()
    monkeypatch.setattr(main.scheduler, "start_scheduler", reg.start)
    monkeypatch.setattr(main.scheduler, "stop_scheduler", reg.stop)
    return reg


def _dns_rotto(*a, **k):
    raise RuntimeError("[Errno -2] Name or service not known")


def test_avvio_senza_db_non_esplode_e_health_lo_dice(monkeypatch, scheduler_finto):
    monkeypatch.setattr(main.database, "init_db", _dns_rotto)
    monkeypatch.setattr(main, "_DB_RETRY_SECONDS", 3600)   # niente retry nel test

    with TestClient(main.app) as client:            # il context manager esegue il lifespan
        res = client.get("/api/health")
        assert res.status_code == 503
        body = res.json()
        assert body["status"] == "degraded"
        assert body["db"] == "unreachable"
        assert "Name or service not known" in body["db_error"]
        assert "non si risolve" in body["db_error"]          # la spiegazione in parole
        # La dashboard resta servibile: un endpoint di lettura risponde (con
        # errore, ma risponde), non un processo morto.
        assert client.get("/api/agent/status").status_code in (200, 500)

    assert scheduler_finto.avvii == 0, "senza database lo scheduler non deve partire"


def test_quando_il_db_torna_lo_scheduler_parte_da_solo(monkeypatch, scheduler_finto):
    vero_init = main.database.init_db
    tentativi = {"n": 0}

    def init_prima_rotto_poi_ok():
        tentativi["n"] += 1
        if tentativi["n"] == 1:
            raise RuntimeError("[Errno -2] Name or service not known")
        return vero_init()

    monkeypatch.setattr(main.database, "init_db", init_prima_rotto_poi_ok)
    monkeypatch.setattr(main, "_DB_RETRY_SECONDS", 0.05)

    with TestClient(main.app) as client:
        assert client.get("/api/health").status_code == 503
        scadenza = time.time() + 5
        while time.time() < scadenza:
            if client.get("/api/health").status_code == 200:
                break
            time.sleep(0.05)
        assert client.get("/api/health").json() == {"status": "ok"}

    assert tentativi["n"] >= 2
    assert scheduler_finto.avvii == 1, "al ritorno del database lo scheduler deve partire, una volta"


def test_avvio_sano_health_ok_e_scheduler_avviato(scheduler_finto):
    with TestClient(main.app) as client:
        assert client.get("/api/health").json() == {"status": "ok"}
        assert client.get("/health").status_code == 200
    assert scheduler_finto.avvii == 1


def test_descrizione_errore_maschera_l_host(monkeypatch):
    monkeypatch.setattr(main.database, "SUPABASE_URL",
                        "https://abcdefghijkl.supabase.co", raising=False)
    pubblico = main._describe_db_error(RuntimeError("[Errno -2] Name or service not known"))
    assert "abc….supabase.co" in pubblico
    assert "abcdefghijkl" not in pubblico
    completo = main._describe_db_error(RuntimeError("x"), masked=False)
    assert "abcdefghijkl.supabase.co" in completo
