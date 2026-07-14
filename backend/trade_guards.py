"""
Guard pre-esecuzione per i Decision (Step 7-8 analisi 13/07).

Due controlli, applicati negli handler execute_trade di decision.py e
decision_crypto.py PRIMA di eseguire il trade:

  1. check_reentry_cooldown (ATTIVO di default, minuti configurabili):
     dopo una chiusura MECCANICA (fail_closed / stop_enforced) su un
     ticker, blocca la ri-apertura nella STESSA direzione per N minuti.
     Caso reale: SOL 29/06 — BUY → fail-closed SELL → re-BUY 30s dopo →
     fail-closed di nuovo (4 trade in 34 secondi, doppie commissioni).
     Indipendente dal cooldown globale 50min (che il watchdog REBALANCE
     bypassa). Chiusure sempre permesse; direzione opposta permessa.

  2. check_anti_inversion (flag OFF di default): inversione di direzione
     su un ticker tradato dall'AI nelle ultime N ore SOLO con
     giustificazione esplicita (thesis_invalidation) o confidence alta.
     Motivazione: 13 inversioni AI-vs-AI <72h su 51 trade AI nelle 3
     settimane analizzate (churn che erode capitale in commissioni).

Entrambi sono FAIL-OPEN sugli errori DB: un guard che non riesce a
leggere lo storico non deve bloccare l'operativita'.
"""
import logging

import database

logger = logging.getLogger(__name__)

# ── Settings ─────────────────────────────────────────────────────────────
SETTING_REENTRY_COOLDOWN_MIN = "reentry_cooldown_minutes"   # default 60, "0" disattiva
SETTING_ANTI_INVERSION = "anti_inversion_enabled"           # default false
SETTING_ANTI_INVERSION_HOURS = "anti_inversion_hours"       # default 12

_MECHANICAL_CLOSE_TYPES = ["fail_closed", "stop_enforced"]
# Soglie anti-inversione: passa con giustificazione scritta (>= 30 char)
# oppure confidence >= 80 (10 punti sopra il gate standard 70).
_MIN_INVALIDATION_CHARS = 30
_INVERSION_CONFIDENCE_OVERRIDE = 80


def _truthy(val: str | None) -> bool:
    return (val or "").strip().lower() in ("1", "true", "yes", "on")


def _cooldown_minutes() -> int:
    try:
        return int(float(database.get_setting(SETTING_REENTRY_COOLDOWN_MIN, "60") or 60))
    except (TypeError, ValueError):
        return 60


def check_reentry_cooldown(ticker: str, action: str) -> tuple[bool, str]:
    """
    (allowed, reason). Blocca solo ri-APERTURE stessa direzione:
    BUY bloccato se una chiusura meccanica di un LONG sul ticker e' piu'
    recente di N minuti; SHORT idem con direction SHORT.
    SELL / COVER (chiusure) sempre permessi.
    """
    act = str(action or "").upper()
    if act not in ("BUY", "SHORT"):
        return True, ""
    minutes = _cooldown_minutes()
    if minutes <= 0:
        return True, ""
    try:
        rows = database.get_recent_trades_by_ticker(
            ticker, hours=minutes / 60.0,
            execution_types=_MECHANICAL_CLOSE_TYPES)
    except Exception as exc:
        logger.warning("[GUARD] cooldown check fallito per %s: %s", ticker, exc)
        return True, ""
    want_dir = "LONG" if act == "BUY" else "SHORT"
    for r in rows:
        if str(r.get("direction") or "LONG").upper() == want_dir:
            return False, (
                f"cooldown post-chiusura meccanica su {ticker}: "
                f"{r.get('execution_type')} alle {r.get('timestamp')} — "
                f"ri-apertura {want_dir} bloccata per {minutes} min. "
                f"Se la tesi resta valida, rientra al prossimo ciclo.")
    return True, ""


def _anti_inversion_hours() -> float:
    try:
        return float(database.get_setting(SETTING_ANTI_INVERSION_HOURS, "12") or 12)
    except (TypeError, ValueError):
        return 12.0


def check_anti_inversion(ticker: str, action: str,
                         confidence: float | None,
                         thesis_invalidation: str | None) -> tuple[bool, str]:
    """
    (allowed, reason). Flag OFF -> sempre permesso.

    Inversione = apertura nella direzione OPPOSTA all'ultimo trade AI sul
    ticker entro la finestra (BUY dopo un'apertura SHORT, SHORT dopo
    un'apertura LONG). Le chiusure (SELL del long, COVER) non contano mai,
    ne' come trigger ne' come inversione.

    Passa se thesis_invalidation e' compilata (>= 30 char: quale fatto ha
    invalidato la tesi precedente) o confidence >= 80.
    """
    if not _truthy(database.get_setting(SETTING_ANTI_INVERSION, "false")):
        return True, ""
    act = str(action or "").upper()
    if act not in ("BUY", "SHORT"):
        return True, ""
    hours = _anti_inversion_hours()
    if hours <= 0:
        return True, ""
    try:
        rows = database.get_recent_trades_by_ticker(
            ticker, hours=hours, execution_types=["ai"])
    except Exception as exc:
        logger.warning("[GUARD] anti-inversion check fallito per %s: %s",
                       ticker, exc)
        return True, ""

    # Ultimo trade AI di APERTURA sul ticker (le chiusure non contano):
    #   apertura long  = action BUY,  direction LONG
    #   apertura short = action SELL, direction SHORT
    last_open_dir = None
    last_row = None
    for r in rows:  # gia' ordinati dal piu' recente
        r_act = str(r.get("action") or "").upper()
        r_dir = str(r.get("direction") or "LONG").upper()
        if r_act == "BUY" and r_dir == "LONG":
            last_open_dir = "LONG"
        elif r_act == "SELL" and r_dir == "SHORT":
            last_open_dir = "SHORT"
        else:
            continue   # chiusura (SELL long / BUY short): non e' un'apertura
        last_row = r
        break

    if last_open_dir is None:
        return True, ""
    new_dir = "LONG" if act == "BUY" else "SHORT"
    if new_dir == last_open_dir:
        return True, ""   # ADD stessa direzione: legittimo

    # E' un'inversione: serve giustificazione o confidence alta.
    justification = (thesis_invalidation or "").strip()
    if len(justification) >= _MIN_INVALIDATION_CHARS:
        return True, ""
    try:
        conf = float(confidence) if confidence is not None else 0.0
    except (TypeError, ValueError):
        conf = 0.0
    if conf >= _INVERSION_CONFIDENCE_OVERRIDE:
        return True, ""
    return False, (
        f"inversione {last_open_dir}->{new_dir} su {ticker} entro {hours:.0f}h "
        f"dall'ultimo trade AI ({last_row.get('timestamp')}) senza "
        f"giustificazione. Compila thesis_invalidation (>= {_MIN_INVALIDATION_CHARS} "
        f"caratteri: quale FATTO ha invalidato la tesi precedente) "
        f"oppure dichiara confidence >= {_INVERSION_CONFIDENCE_OVERRIDE}.")
