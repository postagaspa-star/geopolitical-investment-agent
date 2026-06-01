"""
test_price_freshness.py — verifica ensure_fresh_prices() (prezzi freschi
OBBLIGATORI prima di ogni run decisionale).

Casi coperti:
  - prezzi freschi (ultimo polling recente)  → NESSUN refresh (skip)
  - prezzi vecchi                             → refresh OBBLIGATORIO
  - mai pollati (dopo restart processo)       → refresh OBBLIGATORIO
  - provider giu' (update_price_cache solleva)→ best-effort, NON solleva
  - due decisional concorrenti (crypto :00 +  → UN solo update_price_cache
    standard :02)                                (lock + double-check)

Async via asyncio.run() come gli altri test (no dipendenza pytest-asyncio).
"""
import asyncio
import time

import pytest

import price_polling


@pytest.fixture(autouse=True)
def _reset_freshness_state():
    """
    Resetta lo stato globale prima di ogni test. In particolare azzera il
    lock: ogni asyncio.run() crea un NUOVO event loop e un asyncio.Lock
    creato in un loop precedente solleverebbe 'attached to a different loop'.
    In produzione il loop e' unico, quindi il problema non si pone.
    """
    price_polling._LAST_POLL_TS = 0.0
    price_polling._PRERUN_POLL_LOCK = None
    yield
    price_polling._LAST_POLL_TS = 0.0
    price_polling._PRERUN_POLL_LOCK = None


def _make_fake_update(counter):
    async def _fake_update_price_cache():
        counter["calls"] += 1
        # Riproduce il side-effect reale: marca il timestamp di completamento.
        price_polling._LAST_POLL_TS = time.time()
        return {"quotes_written": 7, "source": "fake"}
    return _fake_update_price_cache


def test_skip_when_fresh(monkeypatch):
    counter = {"calls": 0}
    monkeypatch.setattr(price_polling, "update_price_cache", _make_fake_update(counter))
    price_polling._LAST_POLL_TS = time.time()  # appena pollato → fresco
    res = asyncio.run(price_polling.ensure_fresh_prices(max_age_sec=180, reason="t"))
    assert res["refreshed"] is False
    assert res["reason"] == "fresh"
    assert counter["calls"] == 0


def test_refresh_when_stale(monkeypatch):
    counter = {"calls": 0}
    monkeypatch.setattr(price_polling, "update_price_cache", _make_fake_update(counter))
    price_polling._LAST_POLL_TS = time.time() - 1000  # 1000s fa → vecchio
    res = asyncio.run(price_polling.ensure_fresh_prices(max_age_sec=180, reason="t"))
    assert res["refreshed"] is True
    assert res["reason"] == "stale"
    assert counter["calls"] == 1


def test_refresh_when_never_polled(monkeypatch):
    counter = {"calls": 0}
    monkeypatch.setattr(price_polling, "update_price_cache", _make_fake_update(counter))
    price_polling._LAST_POLL_TS = 0.0  # mai (es. subito dopo un restart)
    res = asyncio.run(price_polling.ensure_fresh_prices(max_age_sec=180, reason="t"))
    assert res["refreshed"] is True
    assert counter["calls"] == 1


def test_best_effort_on_provider_error(monkeypatch):
    async def _boom():
        raise RuntimeError("provider down")
    monkeypatch.setattr(price_polling, "update_price_cache", _boom)
    price_polling._LAST_POLL_TS = 0.0  # forza il tentativo
    # NON deve sollevare: il decisional deve poter proseguire comunque.
    res = asyncio.run(price_polling.ensure_fresh_prices(max_age_sec=180, reason="t"))
    assert res["refreshed"] is False
    assert res["reason"] == "error"


def test_floor_30s(monkeypatch):
    # max_age_sec < 30 viene alzato a 30 (anti-hammering): con ultimo polling
    # 20s fa e richiesta max_age=5, deve comunque considerarlo fresco (20<=30).
    counter = {"calls": 0}
    monkeypatch.setattr(price_polling, "update_price_cache", _make_fake_update(counter))
    price_polling._LAST_POLL_TS = time.time() - 20
    res = asyncio.run(price_polling.ensure_fresh_prices(max_age_sec=5, reason="t"))
    assert res["refreshed"] is False
    assert counter["calls"] == 0


def test_concurrent_dedup(monkeypatch):
    # Due decisional che partono insieme con prezzi vecchi: il lock + il
    # double-check fanno girare update_price_cache UNA volta sola; il secondo
    # riusa i prezzi appena rinfrescati dal primo.
    counter = {"calls": 0}

    async def _slow_update():
        counter["calls"] += 1
        await asyncio.sleep(0.05)
        price_polling._LAST_POLL_TS = time.time()
        return {"quotes_written": 7, "source": "fake"}

    monkeypatch.setattr(price_polling, "update_price_cache", _slow_update)
    price_polling._LAST_POLL_TS = time.time() - 1000  # vecchio

    async def _run_two():
        return await asyncio.gather(
            price_polling.ensure_fresh_prices(max_age_sec=180, reason="crypto"),
            price_polling.ensure_fresh_prices(max_age_sec=180, reason="standard"),
        )

    results = asyncio.run(_run_two())
    assert counter["calls"] == 1
    # Uno rinfresca (True), l'altro riusa (False).
    assert sorted(r["refreshed"] for r in results) == [False, True]
