"""
conftest.py — setup per i test anti-regressione sui path dei soldi.

Forza il backend SQLite (niente Supabase) su un DB temporaneo, e resetta
il portafoglio a 100.000 prima di OGNI test. Commissione fissata a 10 bps
(0.10%) per avere math deterministica nei test sulle fee.

IMPORTANTE: il DB_PATH va impostato PRIMA di importare `database`, perche'
db_sqlite legge DB_PATH a import-time.
"""
import os
import sys
import tempfile

# 1. Forza SQLite + DB temporaneo PRIMA di qualunque import del backend
os.environ.pop("SUPABASE_URL", None)
os.environ.pop("SUPABASE_KEY", None)
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "geoinvest_pytest.db")

# 2. backend/ sul path (questo file e' in backend/tests/)
_BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

import pytest  # noqa: E402
import database  # noqa: E402

COMMISSION_BPS = 10.0
INITIAL_BALANCE = 100000.0


@pytest.fixture(autouse=True)
def fresh_portfolio():
    """Portafoglio pulito a 100k su SQLite temporaneo, prima di ogni test."""
    database.init_db()
    database.reset_portfolio_data(INITIAL_BALANCE)
    database.set_setting("commission_bps", str(COMMISSION_BPS))
    yield
