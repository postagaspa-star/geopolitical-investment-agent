"""
churn_guard — attrito minimo contro il ribaltamento e il ricompro compulsivo.

STORIA, IN DUE INCIDENTI
1) Prima versione (misurata sul passato): 13 inversioni sullo stesso titolo
   in 72h. Il guard copriva ribaltamenti e rientri post-stop.
2) Produzione 26-30/07/2026, a sistema sbloccato: 80 operazioni in 4 giorni.
   Il sanguinamento e' passato dai DUE buchi rimasti:
   a. il "vendo e ricompro NELLA STESSA direzione" era permesso per scelta
      (pensavo alla presa di profitto legittima): ETH venduto e ricomprato in
      5 minuti, TRX in 54, XRP in 14, DOT ricostruito in 4 ore pagando di
      piu'. Ogni giro = commissioni doppie + scarto avverso.
   b. il guard leggeva solo `action` dallo storico, ma execute_short registra
      l'apertura come action="SELL" (direction="SHORT") ed execute_cover la
      chiusura come action="BUY" (direction="SHORT"): meta' delle operazioni
      veniva classificata al contrario, e la protezione sui ribaltamenti era
      cieca proprio sui flip verso short — quelli del 29/07 sera, richiusi il
      30/07 in perdita (~1.100 EUR realizzati in un giorno).

COSA FA ORA
- Classifica ogni riga dello storico con action+direction (la sola action
  mente sugli short).
- TRE regole, tutte solo sull'APRIRE (le chiusure non sono MAI bloccate:
  un guard che puo' impedire un'uscita e' un rischio, non una disciplina):
  1. RIENTRO POST-STOP (12h, per ticker): il mercato ti ha appena dato torto.
  2. RIBALTAMENTO (24h, per ticker): chiuso LONG -> niente SHORT subito, e
     viceversa. Ora vede anche i flip verso short.
  3. RICOMPRO A FREDDO (4h, per ticker, QUALUNQUE direzione): dopo una
     chiusura — incluse le prese di profitto parziali — non si riapre lo
     stesso nome per qualche ora. Chiude anche il litigio "il take-profit
     vende e il Decision ricompra cinque minuti dopo".
- Fail-open: se lo storico non e' leggibile NON blocca (disciplina, non
  governatore di sicurezza). Tutto configurabile, 0 = spento.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

SETTING_ENABLED = "churn_guard_enabled"
SETTING_POST_STOP_HOURS = "churn_post_stop_cooldown_hours"
SETTING_FLIP_HOURS = "churn_flip_cooldown_hours"
SETTING_REOPEN_HOURS = "churn_reopen_cooldown_hours"

DEFAULT_POST_STOP_COOLDOWN_HOURS = 12.0
DEFAULT_FLIP_COOLDOWN_HOURS = 24.0
# Il piu' importante dei tre dopo l'incidente del 26-30/07: vieta il
# "vendo e ricompro" ravvicinato in QUALUNQUE direzione.
DEFAULT_REOPEN_COOLDOWN_HOURS = 4.0

# Azioni che il Decision puo' chiedere e che APRONO esposizione.
OPENING_ACTIONS = {"BUY", "SHORT"}
_ACTION_DIRECTION = {"BUY": "LONG", "SHORT": "SHORT"}


def classify_history_row(trade: dict) -> tuple[str, str] | None:
    """
    (tipo, direzione) di una riga dello storico: ("open"|"close", "LONG"|"SHORT").

    La sola `action` NON basta: execute_short scrive l'apertura short come
    action="SELL" (direction="SHORT") ed execute_cover la chiusura come
    action="BUY" (direction="SHORT"). Verificato in portfolio.py e confermato
    dai dati di produzione del 29-30/07/2026.
    """
    action = (trade.get("action") or "").upper().strip()
    direction = (trade.get("direction") or "LONG").upper().strip()
    if action == "BUY":
        return ("close", "SHORT") if direction == "SHORT" else ("open", "LONG")
    if action == "SELL":
        return ("open", "SHORT") if direction == "SHORT" else ("close", "LONG")
    if action == "COVER":
        return ("close", "SHORT")
    if action == "SHORT":
        return ("open", "SHORT")
    return None


def _parse_ts(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        text = str(value).strip().replace("Z", "+00:00")
        # lo storico usa anche "YYYY-MM-DD HH:MM:SS" senza timezone
        if "T" not in text and " " in text:
            text = text.replace(" ", "T", 1)
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def is_enabled(database=None) -> bool:
    try:
        _db = database if database is not None else __import__("database")
        raw = (_db.get_setting(SETTING_ENABLED, "true") or "").strip().lower()
        return raw not in ("0", "false", "no", "off")
    except Exception:
        return True


def _hours(database, key: str, default: float) -> float:
    try:
        _db = database if database is not None else __import__("database")
        raw = (_db.get_setting(key, "") or "").strip()
        if raw == "":
            return default
        value = float(raw)
        return value if value >= 0 else default
    except Exception:
        return default


def check_trade(ticker: str, action: str, trades: list[dict],
                now: datetime | None = None,
                post_stop_hours: float = DEFAULT_POST_STOP_COOLDOWN_HOURS,
                flip_hours: float = DEFAULT_FLIP_COOLDOWN_HOURS,
                reopen_hours: float = DEFAULT_REOPEN_COOLDOWN_HOURS) -> tuple[bool, str]:
    """(consentito, motivo). Funzione PURA. Le chiusure passano sempre."""
    action = (action or "").upper().strip()
    if action not in OPENING_ACTIONS:
        return True, ""

    now = now or datetime.now(timezone.utc)
    wanted = _ACTION_DIRECTION[action]

    closings: list[tuple[datetime, str, str]] = []
    for trade in trades or []:
        if (trade.get("ticker") or "").upper() != (ticker or "").upper():
            continue
        kind = classify_history_row(trade)
        if kind is None or kind[0] != "close":
            continue
        stamp = _parse_ts(trade.get("timestamp"))
        if stamp is None:
            continue
        closings.append((stamp, kind[1],
                         (trade.get("execution_type") or "").lower()))
    if not closings:
        return True, ""
    closings.sort(key=lambda item: item[0], reverse=True)

    stamp, closed_direction, exec_type = closings[0]
    age_h = (now - stamp).total_seconds() / 3600.0
    if age_h < 0:
        return True, ""          # timestamp nel futuro: dato sporco

    # 1. Rientro dopo uno stop (ora vede anche gli stop sugli short).
    if "stop" in exec_type and age_h < post_stop_hours:
        return False, (
            f"churn_guard: {ticker} chiuso da uno stop {age_h:.1f}h fa; "
            f"rientro dopo {post_stop_hours:.0f}h. Il mercato ha appena "
            f"invalidato la scommessa: ripagarla subito e' churn.")

    # 2. Ribaltamento di direzione.
    if closed_direction != wanted and age_h < flip_hours:
        return False, (
            f"churn_guard: {ticker} chiuso {age_h:.1f}h fa come "
            f"{closed_direction}; aprire {wanted} ora e' un'inversione entro "
            f"{flip_hours:.0f}h. Una tesi che si capovolge in ore non era "
            f"una tesi.")

    # 3. Ricompro a freddo, qualunque direzione (il buco del 26-30/07:
    #    ETH venduto e ricomprato in 5 minuti, 80 operazioni in 4 giorni).
    if age_h < reopen_hours:
        return False, (
            f"churn_guard: {ticker} chiuso {age_h:.1f}h fa; riaprirlo prima "
            f"di {reopen_hours:.0f}h paga due commissioni per la stessa idea. "
            f"Se la tesi e' buona tra qualche ora ci sara' ancora.")

    return True, ""


def check_trade_live(ticker: str, action: str, database=None,
                     limit: int = 200) -> tuple[bool, str]:
    """Variante con I/O. Fail-open: un guasto del DB non blocca mai il trade."""
    try:
        _db = database if database is not None else __import__("database")
        if not is_enabled(_db):
            return True, ""
        trades = _db.get_trades(limit=limit) or []
        return check_trade(
            ticker, action, trades,
            post_stop_hours=_hours(_db, SETTING_POST_STOP_HOURS,
                                   DEFAULT_POST_STOP_COOLDOWN_HOURS),
            flip_hours=_hours(_db, SETTING_FLIP_HOURS,
                              DEFAULT_FLIP_COOLDOWN_HOURS),
            reopen_hours=_hours(_db, SETTING_REOPEN_HOURS,
                                DEFAULT_REOPEN_COOLDOWN_HOURS),
        )
    except Exception as exc:
        logger.debug("churn_guard non valutabile (%s): passo", exc)
        return True, ""
