"""
Calendario macro — eventi binari ad alto impatto (FOMC, CPI) con orario REALE.

Da' al Decision STANDARD un dato AFFIDABILE su quanto manca al prossimo evento,
invece di farglielo indovinare. Bug risolto: l'agente diceva "FOMC oggi 18:00"
quando la riunione era il giorno PRIMA (decisione 17/06) e il vero prossimo evento
era settimane dopo -> congelava i trade per giorni. Ora la sospensione dei nuovi
short scatta SOLO nelle ultime ore di un evento DAVVERO imminente.

Date VERIFICATE alla fonte (giugno 2026):
  - FOMC 2026: federalreserve.gov/monetarypolicy/fomccalendars.htm
    (decisione il 2o giorno della riunione, ~14:00 ET)
  - CPI 2026: bls.gov/schedule/news_release/cpi.htm (~8:30 ET)
AGGIORNARE a ogni nuovo anno (Fed/BLS pubblicano il calendario in anticipo).
Conversione ET->UTC: 14:00 ET = 18:00 (EDT mar-nov) / 19:00 (EST); 8:30 ET =
12:30 (EDT) / 13:30 (EST).
"""
from datetime import datetime, timezone

# (ISO UTC, nome). Solo eventi BINARI ad alto impatto. Ordine non rilevante
# (viene riordinato). La lista CPI e' parziale (H1 2026 + luglio): la FOMC copre
# comunque gli eventi binari del resto dell'anno. Estendere con le date H2 CPI.
MACRO_EVENTS_UTC: list[tuple[str, str]] = [
    # --- FOMC 2026 (decisione tassi, 2o giorno della riunione) ---
    ("2026-01-28T19:00:00+00:00", "FOMC rate decision"),   # EST
    ("2026-03-18T18:00:00+00:00", "FOMC rate decision"),   # EDT
    ("2026-04-29T18:00:00+00:00", "FOMC rate decision"),
    ("2026-06-17T18:00:00+00:00", "FOMC rate decision"),
    ("2026-07-29T18:00:00+00:00", "FOMC rate decision"),
    ("2026-09-16T18:00:00+00:00", "FOMC rate decision"),
    ("2026-10-28T18:00:00+00:00", "FOMC rate decision"),
    ("2026-12-09T19:00:00+00:00", "FOMC rate decision"),   # EST
    # --- CPI 2026 (8:30 ET) ---
    ("2026-02-11T13:30:00+00:00", "CPI release"),          # EST
    ("2026-03-11T12:30:00+00:00", "CPI release"),          # EDT
    ("2026-04-10T12:30:00+00:00", "CPI release"),
    ("2026-05-12T12:30:00+00:00", "CPI release"),
    ("2026-06-10T12:30:00+00:00", "CPI release"),
    ("2026-07-14T12:30:00+00:00", "CPI release"),
]


def _events() -> list[tuple[datetime, str]]:
    out = []
    for iso, name in MACRO_EVENTS_UTC:
        try:
            out.append((datetime.fromisoformat(iso), name))
        except Exception:
            continue
    return sorted(out, key=lambda x: x[0])


def next_macro_event(now: datetime | None = None) -> dict | None:
    """Prossimo evento binario FUTURO + ore mancanti. None se la tabella e'
    esaurita (oltre l'ultima data nota): il chiamante NON e' in finestra."""
    now = now or datetime.now(timezone.utc)
    for dt, name in _events():
        if dt > now:
            return {"name": name, "at_utc": dt.isoformat(),
                    "hours_until": round((dt - now).total_seconds() / 3600.0, 1)}
    return None


def format_macro_event_block(now: datetime | None = None, window_h: float = 3.0) -> str:
    """Blocco per il contesto del Decision: ore REALI al prossimo evento binario
    (l'agente non le indovina). Dice ESPLICITO se sei in finestra pre-evento."""
    ev = next_macro_event(now)
    head = ("═" * 60 + "\nPROSSIMO EVENTO MACRO (calendario reale, non stimato)\n"
            + "═" * 60)
    if not ev:
        return (head + "\nNessun evento binario noto nei prossimi giorni "
                "(calendario esaurito). NON sei in finestra pre-evento: opera "
                "normalmente sui tecnici.\n" + "═" * 60)
    h = ev["hours_until"]
    if 0 < h <= window_h:
        state = (f"⚠️ SEI IN FINESTRA PRE-EVENTO ({h:.1f}h, < {window_h:.0f}h): sospendi "
                 "i NUOVI short, aspetta la reazione post-dato.")
    else:
        state = (f"NON sei in finestra: mancano {h:.1f}h (~{h / 24.0:.1f} giorni). "
                 "Opera normalmente sui tecnici — NON congelare 'in attesa' "
                 "dell'evento, e' lontano.")
    return f"{head}\n{ev['name']} fra {h:.1f}h ({ev['at_utc']}).\n{state}\n" + "═" * 60
