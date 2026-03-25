"""
Database module — routing automatico tra Supabase (PostgreSQL) e SQLite.

Se SUPABASE_URL e SUPABASE_KEY sono configurati, usa Supabase.
Altrimenti usa SQLite come fallback per sviluppo locale.
"""
import logging
import os

logger = logging.getLogger(__name__)

# --- Scegli il backend in base alle variabili d'ambiente ---
_USE_SUPABASE = bool(os.environ.get("SUPABASE_URL")) and bool(os.environ.get("SUPABASE_KEY"))

if _USE_SUPABASE:
    logger.info("Database backend: SUPABASE (PostgreSQL)")
    from db_supabase import *  # noqa: F401, F403
else:
    logger.info("Database backend: SQLite (locale)")
    from db_sqlite import *  # noqa: F401, F403
