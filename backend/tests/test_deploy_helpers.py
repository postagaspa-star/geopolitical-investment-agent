"""
Test helper di deploy (fase 2 analisi 13/07): auto-migrazione
execution_type e core riclassificazione condiviso script/endpoint.
"""
import db_sqlite


def test_ensure_trades_execution_type_sqlite_stub():
    ok, msg = db_sqlite.ensure_trades_execution_type()
    assert ok is True
    assert "init_db" in msg


def test_reclassify_core_condiviso_tra_script_ed_endpoint():
    """Lo script tools/ e l'endpoint devono usare LO STESSO core
    (simulator.reclassify): una divergenza qui significherebbe criteri
    diversi tra locale e server."""
    import sys, os
    sys.path.insert(0, os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "tools")))
    from simulator.reclassify import reclassify as core
    from reclassify_outcomes import reclassify as script_reexport
    assert script_reexport is core
