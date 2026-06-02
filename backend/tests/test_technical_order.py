"""
test_technical_order.py — anti-regressione sul bug "RSI oversold ma prezzo bullish".

Causa: gli indicatori (RSI/MACD/EMA via series.diff()) richiedono la serie in
ordine cronologico ascending (oldest→newest). analyze_ticker lo ASSUMEVA senza
imporlo: se un provider restituiva le barre newest-first (o un reverse saltava),
ogni indicatore si calcolava ALL'INDIETRO → un titolo che SALE veniva letto come
se SCENDESSE (RSI ~0 = oversold) pur essendo bullish.

Fix: ordinamento cronologico difensivo in technical_analysis.analyze_ticker e
in technical._fetch_ticker_indicators (che ordina anche la lista dati, così
current_price = data[-1] e' davvero la barra piu' recente).

Async via asyncio.run() come gli altri test (no pytest-asyncio).
"""
import math

import pandas as pd
import pytest

import technical_analysis as ta


def _bullish_rows(n=60, start="2024-01-01"):
    """Serie chiaramente BULLISH (drift +1/giorno) ma con wobble → l'RSI e'
    ben definito (ci sono sia gain che loss)."""
    from datetime import date, timedelta
    d0 = date.fromisoformat(start)
    rows, prev = [], 100.0
    for i in range(n):
        # drift +0.6/giorno (bullish) + wobble ampio → gain E loss reali
        # (asc → RSI ~73; reversed → RSI ~20). Separazione netta per il test.
        close = 100.0 + i * 0.6 + 2.5 * math.sin(i * 0.6)
        rows.append({
            "date": (d0 + timedelta(days=i)).isoformat(),
            "open": round(prev, 2),
            "high": round(close + 0.6, 2),
            "low": round(close - 0.6, 2),
            "close": round(close, 2),
            "volume": 1_000_000,
        })
        prev = close
    return rows


def _df(rows):
    df = pd.DataFrame(rows)
    df.set_index("date", inplace=True)
    df.columns = [c.capitalize() for c in df.columns]
    return df


def test_rsi_high_on_bullish_ascending():
    rows = _bullish_rows()
    rsi = ta.analyze_ticker(_df(rows))["indicators"]["rsi_14"]
    assert rsi is not None and rsi > 55, f"serie bullish ascending → RSI alto, ottenuto {rsi}"


def test_analyze_ticker_is_order_robust():
    """Stessa serie passata newest-first deve dare lo STESSO RSI (alto), non
    oversold. Prima del fix: l'invertita dava RSI < 40 (il bug)."""
    rows = _bullish_rows()
    rsi_asc = ta.analyze_ticker(_df(rows))["indicators"]["rsi_14"]
    rsi_desc = ta.analyze_ticker(_df(list(reversed(rows))))["indicators"]["rsi_14"]
    assert rsi_asc is not None and rsi_desc is not None
    assert rsi_desc > 55, f"newest-first deve restare bullish dopo il sort, ottenuto {rsi_desc}"
    assert abs(rsi_desc - rsi_asc) < 0.5, (
        f"RSI deve essere order-robust: asc={rsi_asc} desc={rsi_desc}")


def test_raw_rsi_inverts_when_reversed():
    """Documenta il MECCANISMO del bug: la rsi() grezza su una serie invertita
    da' oversold. (E' il motivo per cui analyze_ticker DEVE ordinare.)"""
    closes = [r["close"] for r in _bullish_rows()]
    rsi_up = ta.rsi(pd.Series(closes), 14).iloc[-1]
    rsi_down = ta.rsi(pd.Series(closes[::-1]), 14).iloc[-1]
    assert rsi_up > 55, f"ascending → alto, got {rsi_up}"
    assert rsi_down < 45, f"reversed → oversold (bug se non si ordina), got {rsi_down}"


def test_fetch_ticker_indicators_uses_newest_and_is_order_robust(monkeypatch):
    """Path LIVE: anche se il provider torna newest-first, current_price deve
    essere la barra piu' RECENTE e l'RSI deve riflettere il trend bullish."""
    import asyncio
    import data_fetchers
    import agents.technical as tech
    import agents.technical_advanced as adv

    rows = _bullish_rows()
    desc = list(reversed(rows))  # simula provider che torna newest-first

    monkeypatch.setattr(
        data_fetchers, "fetch_market_data",
        lambda ticker, period_days=90, bypass_cache=False: {
            "data": [dict(r) for r in desc], "source": "test", "error": None},
    )

    async def _noop_adv(*a, **k):
        return {}
    monkeypatch.setattr(adv, "enrich_ticker_advanced", _noop_adv)

    res = asyncio.run(tech._fetch_ticker_indicators("TEST", 90))

    newest_close = rows[-1]["close"]
    assert abs(res["current_price"] - newest_close) < 1e-6, (
        f"current_price deve essere la barra PIU' RECENTE ({newest_close}), "
        f"ottenuto {res['current_price']}")
    assert res["last_bar_date"] == rows[-1]["date"]
    rsi = res["analysis"]["indicators"]["rsi_14"]
    assert rsi is not None and rsi > 55, f"RSI deve riflettere il trend bullish, ottenuto {rsi}"
