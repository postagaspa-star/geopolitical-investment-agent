"""
Technical Crypto — copertura dell'INTERO universo tradeable (richiesta Andrea).

Il Technical Crypto deve analizzare ogni run TUTTE le crypto su cui si può
operare (universe.CORE_CRYPTO), non un sottoinsieme. Poiché ~20 ticker non
entrano in un solo contesto DeepSeek (32K → troncamento), run_crypto_technical
spezza l'analisi in CHUNK paralleli e unisce i risultati. Questi test bloccano
le due regressioni pericolose:
  - un ticker che "sparisce" perché tagliato dal troncamento;
  - un singolo chunk fallito che azzera l'intero report (deve restare best-effort).
"""
import asyncio
import json

from agents import technical_crypto


def _make_fakes(tickers, fail_on=None):
    """Crea fetch+deepseek finti. fake_deepseek ritorna un'analisi per ogni
    ticker PRESENTE nel context del chunk (così verifichiamo la copertura
    reale chunk-per-chunk). Se fail_on interseca i ticker del chunk, solleva."""
    fail_on = set(fail_on or [])

    async def fake_fetch(t):
        return {"ticker": t, "current_price": 100.0, "rsi": 55.0,
                "data_quality": "ok"}

    async def fake_deepseek(context, max_retries=2):
        present = [t for t in tickers if f'"{t}"' in context]
        if fail_on and any(t in fail_on for t in present):
            raise RuntimeError("deepseek boom")
        analyses = [{"ticker": t, "signal": "HOLD", "trend": "NEUTRAL",
                     "confidence": 50} for t in present]
        return json.dumps({"analyses": analyses, "summary": "ok"}), "deepseek-v3"

    return fake_fetch, fake_deepseek


def test_chunks_cover_all_tickers(monkeypatch):
    # 12 ticker → 3 chunk (5/5/2): con la vecchia single-call a 32K i ticker
    # oltre il taglio sparivano. Ora devono esserci TUTTI.
    tickers = [f"C{i:02d}-USD" for i in range(12)]
    fake_fetch, fake_deepseek = _make_fakes(tickers)
    monkeypatch.setattr(technical_crypto, "_fetch_crypto_indicators", fake_fetch)
    monkeypatch.setattr(technical_crypto, "_call_deepseek", fake_deepseek)

    report = asyncio.run(technical_crypto.run_crypto_technical("run-test", tickers))
    analyzed = {a["ticker"] for a in report["analyses"]}
    assert analyzed == set(tickers)            # nessun ticker perso
    assert len(report["analyses"]) == 12
    assert report["engine"] == "deepseek-v3"


def test_chunk_failure_is_best_effort(monkeypatch):
    # Il primo chunk (C00..C04) fallisce; gli altri due devono comunque
    # produrre analisi. Il report NON deve degradare a engine='error'.
    tickers = [f"C{i:02d}-USD" for i in range(12)]
    fake_fetch, fake_deepseek = _make_fakes(tickers, fail_on={"C00-USD"})
    monkeypatch.setattr(technical_crypto, "_fetch_crypto_indicators", fake_fetch)
    monkeypatch.setattr(technical_crypto, "_call_deepseek", fake_deepseek)

    report = asyncio.run(technical_crypto.run_crypto_technical("run-test", tickers))
    analyzed = {a["ticker"] for a in report["analyses"]}
    assert "C00-USD" not in analyzed                       # chunk saltato
    assert {"C05-USD", "C11-USD"}.issubset(analyzed)       # gli altri ci sono
    assert report["engine"] == "deepseek-v3"               # non tutti falliti


def test_all_chunks_failed_returns_error_report(monkeypatch):
    # Se TUTTI i chunk falliscono → report d'errore con raw_indicators, così il
    # Decision sa di dover degradare a do_nothing (contratto preservato).
    tickers = [f"C{i:02d}-USD" for i in range(8)]
    fake_fetch, fake_deepseek = _make_fakes(
        tickers, fail_on={t for t in tickers})   # tutti
    monkeypatch.setattr(technical_crypto, "_fetch_crypto_indicators", fake_fetch)
    monkeypatch.setattr(technical_crypto, "_call_deepseek", fake_deepseek)

    report = asyncio.run(technical_crypto.run_crypto_technical("run-test", tickers))
    assert report["engine"] == "error"
    assert report["analyses"] == []
    assert "raw_indicators" in report and "data_warning" in report


def test_default_tickers_is_full_core_universe(monkeypatch):
    # tickers=None → l'INTERO universo tradeable (non più 6 hardcoded).
    import universe
    requested = []

    async def fake_fetch(t):
        requested.append(t)
        return {"ticker": t, "current_price": 1.0, "data_quality": "ok"}

    async def fake_deepseek(context, max_retries=2):
        return json.dumps({"analyses": [], "summary": ""}), "deepseek-v3"

    monkeypatch.setattr(technical_crypto, "_fetch_crypto_indicators", fake_fetch)
    monkeypatch.setattr(technical_crypto, "_call_deepseek", fake_deepseek)

    asyncio.run(technical_crypto.run_crypto_technical("run-x", None))
    assert set(requested) == set(universe.CORE_CRYPTO)
    assert len(requested) == len(universe.CORE_CRYPTO)
