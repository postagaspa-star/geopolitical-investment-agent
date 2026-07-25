"""
Invariante: la finestra di validita' della cache non puo' essere piu' stretta
della cadenza con cui i prezzi vengono prodotti.

Se lo e', per una parte di OGNI ciclo tutti i prezzi risultano scaduti e chi
li consuma non vede nulla. E' successo davvero: polling ogni 1200s contro un
max_age di 700s nel watchdog, cioe' ~500s di finestra cieca per ciclo (41%
del tempo) in cui il cancello di risveglio valutava il mercato senza un solo
prezzo — mentre il suo commento dichiarava di essere "allineato al polling
rate (600s)", una cadenza ormai cambiata.
"""
import price_polling


def test_cache_copre_almeno_un_ciclo_di_polling():
    assert price_polling.POLLING_MAX_AGE_SECONDS >= price_polling.POLLING_INTERVAL_SECONDS, (
        "max_age piu' stretto della cadenza: si riapre una finestra cieca "
        "in cui i consumatori non vedono alcun prezzo")


def test_margine_ragionevole_ma_non_infinito():
    """Il margine copre il giro e i ritardi provider, ma i prezzi devono
    restare recenti: una cache troppo lunga farebbe valutare dati stantii."""
    margin = (price_polling.POLLING_MAX_AGE_SECONDS
              - price_polling.POLLING_INTERVAL_SECONDS)
    assert 60 <= margin <= 900


def test_il_watchdog_usa_la_fonte_unica():
    """Il numero non deve tornare hardcoded nel watchdog."""
    import inspect
    from agents import watchdog
    src = inspect.getsource(watchdog._get_price_snapshot)
    assert "POLLING_MAX_AGE_SECONDS" in src
    assert "get_cached_prices_bulk, tickers, 700" not in src
