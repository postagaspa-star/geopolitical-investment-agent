"""
churn_guard — attrito minimo contro il ribaltamento rapido di posizione.

IL PROBLEMA MISURATO
In una finestra di osservazione: 13 inversioni di direzione sullo STESSO
titolo entro 72 ore, su 51 operazioni decise dall'AI. Una operazione su
quattro era un dietrofront.

PERCHE' SUCCEDE
Nel percorso che esegue davvero i trade non esiste alcun attrito temporale:
nessun periodo minimo di detenzione, nessun cooldown dopo uno stop, nessun
controllo di coerenza con la direzione precedente. `validate_trade` non
riceve nemmeno il parametro `ticker`. Ogni run ricalcola la direzione da zero
e il costo di cambiare idea non e' modellato da nessuna parte — quindi
cambiare idea e' sempre localmente ottimale. Il nucleo deterministico crypto
lo rende evidente: la direzione e' il SEGNO di una somma pesata, con banda
morta zero, e la funzione non riceve mai la posizione corrente.

La logica giusta era gia' scritta nel repo (crypto_regime: conferma su piu'
barre; crypto_scalper: minimo di barre fra ingressi, "il nemico e' il
laterale, flippare senza trend = morte per mille tagli"; crypto_meanrev:
cooldown di rientro dopo lo stop) — ma vive tutta dietro flag spenti e fuori
dallo scheduler, cioe' nel ramo che NON esegue.

COSA FA QUESTO MODULO, E COSA NON FA
Non impedisce di operare: impedisce di CONTRADDIRSI troppo in fretta. La
distinzione conta, perche' il sistema soffre gia' di eccesso di veti
sull'entrata. Qui non si aggiunge un altro cancello all'apertura in generale:
si mette attrito su due gesti specifici e patologici su un orizzonte swing
(giorni-settimane):

  1. rientrare su un titolo che ti ha appena stoppato;
  2. aprire nella direzione OPPOSTA a quella che hai appena chiuso.

Le CHIUSURE non sono mai bloccate: uscire da una posizione deve restare
sempre possibile, in qualunque momento. Un guard che potesse impedire
un'uscita sarebbe un rischio, non una disciplina.

Modulo puro: riceve la lista dei trade e decide. Nessun I/O, testabile.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

SETTING_ENABLED = "churn_guard_enabled"
SETTING_POST_STOP_HOURS = "churn_post_stop_cooldown_hours"
SETTING_FLIP_HOURS = "churn_flip_cooldown_hours"

# Dopo che uno stop ha chiuso una posizione, rientrare sullo stesso nome
# entro poche ore significa quasi sempre ripagare lo spread per rifare la
# stessa scommessa appena invalidata dal mercato.
DEFAULT_POST_STOP_COOLDOWN_HOURS = 12.0

# Ribaltare la direzione sullo stesso nome: e' il gesto che ha prodotto le 13
# inversioni in 72h. Su un orizzonte swing, una tesi che si capovolge in meno
# di un giorno non era una tesi.
DEFAULT_FLIP_COOLDOWN_HOURS = 24.0

# Azioni che APRONO o aumentano esposizione. Solo queste sono soggette al
# guard; SELL e COVER passano sempre.
OPENING_ACTIONS = {"BUY", "SHORT"}

# Direzione implicata da un'azione di apertura.
_ACTION_DIRECTION = {"BUY": "LONG", "SHORT": "SHORT"}

# Chiusure: l'azione che termina una posizione di quella direzione.
_CLOSING_ACTION_DIRECTION = {"SELL": "LONG", "COVER": "SHORT"}


def _parse_ts(value) -> datetime | None:
    """Timestamp dei trade: ISO con o senza timezone. Mai solleva."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        text = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def is_enabled(database=None) -> bool:
    try:
        if database is None:
            import database as _db
        else:
            _db = database
        raw = (_db.get_setting(SETTING_ENABLED, "true") or "").strip().lower()
        return raw not in ("0", "false", "no", "off")
    except Exception:
        return True   # in dubbio, la disciplina resta attiva


def _hours(database, key: str, default: float) -> float:
    try:
        if database is None:
            import database as _db
        else:
            _db = database
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
                flip_hours: float = DEFAULT_FLIP_COOLDOWN_HOURS) -> tuple[bool, str]:
    """
    (consentito, motivo). Funzione PURA.

    `trades` e' lo storico piu' recente (qualunque ordine): vengono considerati
    solo quelli sul `ticker` indicato.
    """
    action = (action or "").upper().strip()
    if action not in OPENING_ACTIONS:
        return True, ""          # le uscite non si bloccano mai

    now = now or datetime.now(timezone.utc)
    wanted_direction = _ACTION_DIRECTION[action]

    # Chiusure recenti sullo stesso nome, dalla piu' recente.
    closings: list[tuple[datetime, str, str]] = []
    for trade in trades or []:
        if (trade.get("ticker") or "").upper() != (ticker or "").upper():
            continue
        act = (trade.get("action") or "").upper().strip()
        closed_direction = _CLOSING_ACTION_DIRECTION.get(act)
        if closed_direction is None:
            continue
        stamp = _parse_ts(trade.get("timestamp"))
        if stamp is None:
            continue
        closings.append((stamp, closed_direction,
                         (trade.get("execution_type") or "").lower()))
    if not closings:
        return True, ""
    closings.sort(key=lambda item: item[0], reverse=True)

    for stamp, closed_direction, exec_type in closings:
        age_h = (now - stamp).total_seconds() / 3600.0
        if age_h < 0:
            continue   # timestamp nel futuro: dato sporco, si ignora

        # 1. Rientro dopo uno stop sullo stesso nome.
        if "stop" in exec_type and age_h < post_stop_hours:
            return False, (
                f"churn_guard: {ticker} e' stato chiuso da uno stop "
                f"{age_h:.1f}h fa; rientro consentito dopo {post_stop_hours:.0f}h. "
                f"Rientrare subito ripaga i costi per rifare la scommessa che il "
                f"mercato ha appena invalidato.")

        # 2. Ribaltamento della direzione sullo stesso nome.
        if closed_direction != wanted_direction and age_h < flip_hours:
            return False, (
                f"churn_guard: {ticker} chiuso {age_h:.1f}h fa come "
                f"{closed_direction}; aprire {wanted_direction} ora e' "
                f"un'inversione entro {flip_hours:.0f}h. Su un orizzonte swing "
                f"una tesi che si capovolge in meno di un giorno non era una tesi.")

        # La chiusura piu' recente non blocca: le precedenti sono piu' vecchie.
        break

    return True, ""


def check_trade_live(ticker: str, action: str, database=None,
                     limit: int = 200) -> tuple[bool, str]:
    """
    Variante con I/O per i call-site. Fail-open: se lo storico non e'
    leggibile NON si blocca il trade — questo modulo impone disciplina, non
    e' un governatore di sicurezza, e non deve poter congelare l'operativita'
    per un guasto del DB.
    """
    try:
        if database is None:
            import database as _db
        else:
            _db = database
        if not is_enabled(_db):
            return True, ""
        trades = _db.get_trades(limit=limit) or []
        return check_trade(
            ticker, action, trades,
            post_stop_hours=_hours(_db, SETTING_POST_STOP_HOURS,
                                   DEFAULT_POST_STOP_COOLDOWN_HOURS),
            flip_hours=_hours(_db, SETTING_FLIP_HOURS,
                              DEFAULT_FLIP_COOLDOWN_HOURS),
        )
    except Exception as exc:
        logger.debug("churn_guard non valutabile (%s): passo", exc)
        return True, ""
