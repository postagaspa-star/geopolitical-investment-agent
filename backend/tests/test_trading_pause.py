"""
Il tasto STOP deve fermare davvero — e fermare la cosa giusta.

Bug riportato dall'utente (02/08/2026): "se cliccato riparte subito".
Causa: lo stop spegneva l'intero scheduler, ma l'avvio dell'app lo
riaccendeva SEMPRE — e il servizio si riavvia spesso (OOM). In piu',
spegnere tutto avrebbe fermato anche gli stop di protezione.

Ora: pausa PERSISTENTE dei soli job decisionali; protezione sempre viva.
"""
import asyncio

import pytest

import database
import scheduler


@pytest.fixture(autouse=True)
def _clean_flag():
    database.set_setting(scheduler.SETTING_TRADING_PAUSED, "")
    yield
    database.set_setting(scheduler.SETTING_TRADING_PAUSED, "")


# ── Il flag ─────────────────────────────────────────────────────────────────

def test_default_non_in_pausa():
    assert scheduler.is_trading_paused() is False


def test_stop_persistente():
    database.set_setting(scheduler.SETTING_TRADING_PAUSED, "true")
    assert scheduler.is_trading_paused() is True


def test_valore_sporco_non_ferma():
    database.set_setting(scheduler.SETTING_TRADING_PAUSED, "boh")
    assert scheduler.is_trading_paused() is False


def test_db_rotto_non_ferma(monkeypatch):
    monkeypatch.setattr(scheduler.database, "get_setting",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    assert scheduler.is_trading_paused() is False


# ── Il lucchetto sui job ────────────────────────────────────────────────────

def test_job_decisionale_saltato_in_pausa():
    chiamato = {"si": False}

    async def finto_job():
        chiamato["si"] = True

    gated = scheduler._gated(finto_job, "finto")
    database.set_setting(scheduler.SETTING_TRADING_PAUSED, "true")
    asyncio.run(gated())
    assert chiamato["si"] is False, "in pausa il job decisionale NON deve girare"

    database.set_setting(scheduler.SETTING_TRADING_PAUSED, "false")
    asyncio.run(gated())
    assert chiamato["si"] is True, "tolta la pausa, il job torna a girare"


def test_quali_job_sono_dietro_il_lucchetto():
    """
    Ispezione del codice di registrazione: i job che DECIDONO devono essere
    avvolti, quelli che PROTEGGONO non devono esserlo mai. Se qualcuno sposta
    un job dalla parte sbagliata, questo test lo dice.
    """
    import inspect
    src = inspect.getsource(scheduler)
    decisionali = ["_watchdog_job", "_crypto_pipeline_job", "_crypto_monitor_job",
                   "_standard_pipeline_job", "_simulator_auto_run_job",
                   "_scout_hourly_job"]
    protezione = ["_price_polling_job", "_risk_safety_job", "_health_check_job"]
    for job in decisionali:
        assert f"_gated({job}," in src, f"{job} deve stare dietro il lucchetto"
    for job in protezione:
        assert f"_gated({job}," not in src, (
            f"{job} e' PROTEZIONE: non deve mai essere messo in pausa")


# ── Gli endpoint ────────────────────────────────────────────────────────────

def test_stop_ferma_tutto(monkeypatch):
    """Semantica chiarita da Andrea: il tasto ferma TUTTO, Simulator incluso."""
    import main
    spento = {"si": False}
    monkeypatch.setattr(scheduler, "stop_scheduler",
                        lambda *a, **k: spento.update(si=True))
    out = asyncio.run(main.stop_continuous_monitoring())
    assert out["status"] == "stopped"
    assert scheduler.is_trading_paused() is True, "la scelta deve persistere"
    assert spento["si"] is True, "lo scheduler va spento DAVVERO, tutto incluso"
    # e il messaggio deve dire la verita' scomoda sulle posizioni non gestite
    assert "stop automatici" in out["message"]


def test_l_avvio_dell_app_rispetta_lo_stop():
    """IL BUG ORIGINARIO: il lifespan riavviava SEMPRE lo scheduler,
    vanificando il tasto al primo riavvio (frequente, con l'OOM)."""
    import inspect
    import main
    src = inspect.getsource(main)
    assert "SISTEMA FERMATO DALL'UTENTE" in src, (
        "il lifespan deve controllare is_trading_paused() prima di avviare")
    assert "sempre attivo al deploy" not in src, (
        "il vecchio avvio incondizionato non deve tornare")


def test_start_toglie_la_pausa(monkeypatch):
    import main
    database.set_setting(scheduler.SETTING_TRADING_PAUSED, "true")
    monkeypatch.setattr(scheduler, "is_scheduler_running", lambda: True)
    out = asyncio.run(main.start_continuous_monitoring())
    assert out["status"] == "started"
    assert scheduler.is_trading_paused() is False


def test_status_mostra_fermo_quando_in_pausa(monkeypatch):
    import main
    database.set_setting(scheduler.SETTING_TRADING_PAUSED, "true")
    monkeypatch.setattr(scheduler, "get_scheduler_info", lambda: {
        "running": False, "mode": "idle", "market_open": True,
        "is_weekend": False, "agents": {}})
    out = asyncio.run(main.get_agent_status())
    assert out["trading_paused"] is True
    assert out["running"] is False, "la UI deve vedere FERMO"
    assert out["scheduler_running"] is False
    assert out["protection_running"] is False, "fermo vuol dire fermo: tutto"
