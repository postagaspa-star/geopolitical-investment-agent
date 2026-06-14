"""
Round 6 audit — integrità esecuzione.

6.  Doppia esecuzione trade nella chat: il check executed_trade_id e il mark
    NON erano atomici → doppio click/retry eseguiva il trade due volte. Ora
    reserve_decision_chat_message() prenota atomicamente (UPDATE condizionale).
31. scalper: save_state precedeva l'esecuzione → su ordine rifiutato restava
    una posizione fantasma. Ora salva SOLO se l'ordine è confermato.
    [loop run_crypto_scalper → verificato da review + regressione]
"""
import database


def test_reserve_decision_chat_message_atomic():
    conv = database.get_or_create_decision_chat_conversation("standard")
    mid = database.insert_decision_chat_message(conv, "assistant", "trade proposto")

    # 1ª prenotazione riesce; la 2ª (concorrente) viene bloccata
    assert database.reserve_decision_chat_message(mid) is True
    assert database.reserve_decision_chat_message(mid) is False

    # rilascio (esecuzione fallita) → ri-prenotabile
    database.clear_decision_chat_reservation(mid)
    assert database.reserve_decision_chat_message(mid) is True

    # dopo il mark col trade reale, non più prenotabile (già eseguito)
    database.mark_decision_chat_trade_executed(mid, 999)
    assert database.reserve_decision_chat_message(mid) is False


def test_clear_only_releases_reservation_not_real_trade():
    conv = database.get_or_create_decision_chat_conversation("standard")
    mid = database.insert_decision_chat_message(conv, "assistant", "x")
    database.mark_decision_chat_trade_executed(mid, 777)   # trade reale
    # clear NON deve cancellare un trade reale (solo la sentinella -1)
    database.clear_decision_chat_reservation(mid)
    assert database.reserve_decision_chat_message(mid) is False
