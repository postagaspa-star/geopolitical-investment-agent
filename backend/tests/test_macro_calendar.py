"""
Calendario macro: ore REALI al prossimo evento binario (FOMC/CPI), date verificate
alla fonte. Risolve il freeze multi-giorno (l'agente prima indovinava la data e
trattava un evento lontano come 'imminente').
"""
from datetime import datetime, timezone
import macro_calendar as mc


def _utc(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def test_next_event_is_future_and_picks_nearest():
    # il 19/06/2026 la FOMC di giugno (17) e' gia' passata: prossimo binario = CPI 14/07
    ev = mc.next_macro_event(_utc(2026, 6, 19, 12))
    assert ev is not None
    assert ev["name"] == "CPI release"
    assert ev["at_utc"].startswith("2026-07-14")
    assert ev["hours_until"] > 24 * 20          # settimane, non ore


def test_in_window_only_within_3h():
    # 2h prima della FOMC del 29/07 (18:00 UTC) -> in finestra
    assert "SEI IN FINESTRA" in mc.format_macro_event_block(_utc(2026, 7, 29, 16, 0))
    # 1 giorno prima -> NON in finestra
    assert "NON sei in finestra" in mc.format_macro_event_block(_utc(2026, 7, 28, 16, 0))


def test_far_event_says_trade_normally():
    blk = mc.format_macro_event_block(_utc(2026, 6, 19, 12))
    assert "NON sei in finestra" in blk
    assert "giorni" in blk.lower()


def test_table_exhausted_is_safe():
    assert mc.next_macro_event(_utc(2027, 6, 1)) is None
    blk = mc.format_macro_event_block(_utc(2027, 6, 1))
    assert "esaurito" in blk.lower()
    assert "NON sei in finestra" in blk
