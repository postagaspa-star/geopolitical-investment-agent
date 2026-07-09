"""
Test del magazzino OHLCV (Approach 1).
  - Flag OFF → modulo inerte (nessuna lettura/scrittura, comportamento invariato)
  - Flag ON  → get_bars usa il magazzino, filtra i dati stantii
  - warm_universe_tickers → solo equity, niente crypto
  - fetch_market_data legge dal magazzino quando fresco (niente provider)
"""
from datetime import datetime, timezone, timedelta

import pytest


# ─── Fake Supabase client (fluent) ──────────────────────────────────────

class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def range(self, *a, **k): return self
    def upsert(self, rows, **k):
        self._captured = rows
        return self
    def execute(self):
        class _R: pass
        r = _R()
        r.data = self._rows
        return r


class _FakeClient:
    def __init__(self, rows):
        self._rows = rows
        self.upserted = None
    def table(self, name):
        q = _FakeQuery(self._rows)
        # cattura upsert
        orig_upsert = q.upsert
        def _cap(rows, **k):
            self.upserted = rows
            return orig_upsert(rows, **k)
        q.upsert = _cap
        return q


def _iso(dt):
    return dt.isoformat()


def _bars_rows(n=30, age_hours=1.0, close=100.0):
    """Righe finte come le tornerebbe Supabase (date DESC), fresche di age_hours."""
    upd = _iso(datetime.now(timezone.utc) - timedelta(hours=age_hours))
    rows = []
    base = datetime.now(timezone.utc).date()
    for i in range(n):
        d = (base - timedelta(days=i)).isoformat()
        rows.append({"date": d, "open": close, "high": close + 1,
                     "low": close - 1, "close": close + i * 0.1,
                     "volume": 1000 + i, "source": "twelvedata",
                     "updated_at": upd})
    return rows


# ─── Flag OFF: inerte ───────────────────────────────────────────────────

def test_disabled_get_bars_returns_none(monkeypatch):
    import ohlcv_store
    monkeypatch.delenv("OHLCV_STORE_ENABLED", raising=False)
    assert ohlcv_store.get_bars("AAPL", 90) is None
    assert ohlcv_store.upsert_bars("AAPL", [{"date": "2026-01-01", "close": 1}]) is False


# ─── Flag ON: lettura dal magazzino ─────────────────────────────────────

def test_get_bars_fresh(monkeypatch):
    import ohlcv_store
    monkeypatch.setenv("OHLCV_STORE_ENABLED", "true")
    monkeypatch.setattr(ohlcv_store, "_get_client",
                        lambda: _FakeClient(_bars_rows(30, age_hours=1.0)))
    out = ohlcv_store.get_bars("AAPL", 90)
    assert out is not None
    assert out["from_store"] is True
    assert len(out["data"]) == 30
    # ordinate ASC (oldest→newest)
    dates = [r["date"] for r in out["data"]]
    assert dates == sorted(dates)


def test_get_bars_stale_rejected(monkeypatch):
    import ohlcv_store
    monkeypatch.setenv("OHLCV_STORE_ENABLED", "true")
    # updated_at di 48h fa, max_age default 6h → stantio → None
    monkeypatch.setattr(ohlcv_store, "_get_client",
                        lambda: _FakeClient(_bars_rows(30, age_hours=48.0)))
    assert ohlcv_store.get_bars("AAPL", 90) is None


def test_get_bars_too_few_rows(monkeypatch):
    import ohlcv_store
    monkeypatch.setenv("OHLCV_STORE_ENABLED", "true")
    monkeypatch.setattr(ohlcv_store, "_get_client",
                        lambda: _FakeClient(_bars_rows(3, age_hours=1.0)))
    assert ohlcv_store.get_bars("AAPL", 90) is None   # <5 barre


def test_upsert_bars_writes(monkeypatch):
    import ohlcv_store
    monkeypatch.setenv("OHLCV_STORE_ENABLED", "true")
    fake = _FakeClient([])
    monkeypatch.setattr(ohlcv_store, "_get_client", lambda: fake)
    recs = [{"date": "2026-07-01", "open": 10, "high": 11, "low": 9,
             "close": 10.5, "volume": 500}]
    assert ohlcv_store.upsert_bars("aapl", recs, "stooq") is True
    assert fake.upserted and fake.upserted[0]["ticker"] == "AAPL"
    assert fake.upserted[0]["source"] == "stooq"


# ─── warm_universe_tickers: solo equity ─────────────────────────────────

def test_warm_universe_only_equity():
    import ohlcv_store
    tickers = ohlcv_store.warm_universe_tickers()
    assert len(tickers) > 20            # core equity + ETF
    assert "AAPL" in tickers
    assert not any(t.endswith("-USD") or t.startswith("X:") for t in tickers)
    assert len(tickers) == len(set(tickers))   # dedup


# ─── fetch_market_data legge dal magazzino quando fresco ────────────────

def test_fetch_market_data_uses_store(monkeypatch):
    import ohlcv_store
    import data_fetchers
    monkeypatch.setenv("OHLCV_STORE_ENABLED", "true")
    monkeypatch.setattr(ohlcv_store, "_get_client",
                        lambda: _FakeClient(_bars_rows(60, age_hours=1.0)))
    # Se leggesse dai provider, questo test sarebbe lento/instabile: verifichiamo
    # che ritorni dal magazzino (source con prefisso "store:").
    data_fetchers._yfinance_cache.clear()
    out = data_fetchers.fetch_market_data("AAPL", 90)
    assert out.get("from_store") is True
    assert out["source"].startswith("store:")
