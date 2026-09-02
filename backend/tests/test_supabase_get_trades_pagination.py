"""
test_supabase_get_trades_pagination.py — il tetto delle 1000 righe di Supabase.

PostgREST tronca ogni risposta a 1000 righe qualunque sia il .limit(). Qui un
client finto riproduce quel comportamento e si verifica che get_trades le
recuperi TUTTE paginando, in ordine e senza duplicati. Sopra le 1000 gambe
questo e' cio' che separa un P&L vero da uno inventato.
"""
from types import SimpleNamespace

import pytest

import db_supabase

POSTGREST_MAX_ROWS = 1000


class _FakeQuery:
    def __init__(self, data, log):
        self._data = data
        self._log = log
        self._lo = 0
        self._hi = None

    def select(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def limit(self, n):
        self._hi = self._lo + int(n) - 1
        return self

    def range(self, lo, hi):
        self._lo, self._hi = int(lo), int(hi)
        return self

    def execute(self):
        hi = self._hi if self._hi is not None else self._lo + POSTGREST_MAX_ROWS - 1
        hi = min(hi, self._lo + POSTGREST_MAX_ROWS - 1)   # il tetto di PostgREST
        self._log.append((self._lo, hi))
        return SimpleNamespace(data=self._data[self._lo:hi + 1])


class _FakeClient:
    def __init__(self, n_rows):
        self.rows = [{"id": n_rows - i, "ticker": "AAPL"} for i in range(n_rows)]
        self.calls = []

    def table(self, name):
        assert name == "trades"
        return _FakeQuery(self.rows, self.calls)


@pytest.fixture()
def finto(monkeypatch):
    holder = {}

    def _mk(n):
        holder["c"] = _FakeClient(n)
        monkeypatch.setattr(db_supabase, "_get_client", lambda: holder["c"])
        return holder["c"]
    return _mk


def test_sotto_il_tetto_una_sola_richiesta(finto):
    c = finto(300)
    out = db_supabase.get_trades(limit=50)
    assert len(out) == 50
    assert len(c.calls) == 1


def test_sopra_il_tetto_recupera_tutte_le_righe(finto):
    """2.500 trade, limit 5000: prima ne arrivavano 1.000 in silenzio."""
    c = finto(2500)
    out = db_supabase.get_trades(limit=5000)
    assert len(out) == 2500
    ids = [r["id"] for r in out]
    assert ids == sorted(ids, reverse=True)          # dal piu' recente
    assert len(set(ids)) == 2500                      # nessun duplicato
    assert len(c.calls) == 3                          # 1000 + 1000 + 500


def test_rispetta_il_limite_richiesto(finto):
    finto(5000)
    assert len(db_supabase.get_trades(limit=1500)) == 1500


def test_tabella_vuota(finto):
    finto(0)
    assert db_supabase.get_trades(limit=5000) == []
