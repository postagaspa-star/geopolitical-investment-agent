"""
test_read_endpoints_fail_loud.py — un errore di lettura non e' mai un HTTP 200.

Il bug in produzione: /api/logs falliva (Supabase in timeout) e rispondeva
HTTP 200 con {"error": "..."}. Il frontend vedeva res.ok, passava l'oggetto a
codice che si aspettava una lista, `for (const log of logs)` esplodeva con
"e is not iterable" e l'intera Dashboard Live finiva nell'ErrorBoundary.

Qui si rompe la lettura dal DB di proposito e si pretende un 500 con il
corpo {"error": ...}: cosi' chi legge gestisce il fallimento con un solo
`if (!res.ok)` e nessun oggetto-errore puo' piu' travestirsi da dato.
"""
import pytest
from starlette.testclient import TestClient

import main


def _boom(*args, **kwargs):
    raise RuntimeError("Supabase in timeout (simulato)")


@pytest.fixture()
def client():
    # /api/logs ha una cache in memoria di 20s: va svuotata, altrimenti un
    # test potrebbe ricevere la risposta buona di quello prima.
    main._RESP_CACHE.clear()
    return TestClient(main.app)


# (percorso, oggetto su cui sta la funzione, nome della funzione da far esplodere)
CASI = [
    ("/api/logs?limit=77", "database", "get_agent_logs"),
    ("/api/trades?limit=7", "database", "get_trades"),
    ("/api/positions", "database", "get_positions"),
    ("/api/portfolio", "portfolio", "get_portfolio_state"),
    ("/api/intelligence?limit=5", "database", "get_weekend_intelligence"),
    ("/api/briefings?limit=5", "database", "get_pre_market_briefings"),
    ("/api/agent/status", "scheduler", "get_scheduler_info"),
]


@pytest.mark.parametrize("path,modulo,funzione", CASI)
def test_lettura_fallita_risponde_500_con_errore(client, monkeypatch,
                                                 path, modulo, funzione):
    monkeypatch.setattr(getattr(main, modulo), funzione, _boom)
    res = client.get(path)
    assert res.status_code == 500, f"{path}: un errore non puo' essere un {res.status_code}"
    body = res.json()
    assert isinstance(body, dict) and "error" in body
    assert "timeout" in body["error"]


def test_lettura_sana_resta_una_lista(client):
    """Il percorso felice non deve cambiare: /api/logs e /api/trades danno
    liste (anche vuote) con HTTP 200."""
    for path in ("/api/logs?limit=78", "/api/trades?limit=8", "/api/positions"):
        res = client.get(path)
        assert res.status_code == 200, path
        assert isinstance(res.json(), list), path
