"""
inaction_metrics — dare un numero all'omissione.

PERCHE' ESISTE
Tutta la strumentazione del sistema misura le operazioni FATTE: win rate,
profit factor, calibrazione della confidence, edge tracker. Il denominatore e'
sempre il numero di trade, mai il numero di occasioni. Conseguenza: se il
sistema smette di operare non produce metriche cattive — non produce metriche
affatto, e l'edge tracker resta educatamente su "insufficient_data,
continua a far girare il sistema".

Sbagliare AGENDO lascia una traccia: una commissione, uno stop, un log di
rifiuto. Sbagliare NON agendo non lascia nulla. Un sistema che misura solo una
delle due code impara asimmetricamente e converge sull'inazione: e' l'unica
strategia che non genera mai un errore visibile e attribuibile.

Questo modulo non cambia nessuna decisione. Rende leggibile cio' che finora
era invisibile, ed e' il presupposto di qualunque discorso sulla funzione
obiettivo: senza questi numeri, "l'agente e' troppo prudente" resta
un'impressione.

I DATI CI SONO GIA'
Nessuna nuova raccolta: il watchdog logga OGNI ciclo anche quando non sveglia
(phase WATCHDOG), i commitment hanno stato e scadenza, i trade hanno
timestamp. Mancava solo chi li leggesse. Il primo di questi conteggi, fatto a
mano sui log di produzione, ha risolto in cinque minuti un caso aperto da
giorni: dei 512 cicli esaminati, 250 erano un'API rotta e 229 un throttle —
il 94% dei "non svegliare" non era una valutazione del mercato.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Motivi di non-risveglio raggruppati per NATURA, non per stringa: e' la
# distinzione che conta per capire se il sistema sta valutando o solo tacendo.
_REASON_KIND = {
    "guasto": ("http_", "json_parse_error", "no_deepseek_key", "timeout",
               "llm ko", "decision_failing_backoff"),
    "throttle": ("decision_ran_recently", "decision_possibly_running",
                 "all_agents_busy", "global_trigger_cooldown", "throttled"),
    "risparmio": ("market_closed", "no_recent_news", "cost_guard", "dedup"),
}


def classify_reason(reason: str, event: str = "") -> str:
    """
    "guasto" | "throttle" | "risparmio" | "giudizio".

    Solo l'ultima categoria e' una vera valutazione del mercato. Le prime tre
    sono il sistema che non ha nemmeno posto la domanda — e prima d'ora
    finivano tutte indistintamente in "il watchdog non ha triggerato".
    """
    text = f"{event} {reason}".lower()
    if "llm_failed" in text:
        return "guasto"
    for kind, needles in _REASON_KIND.items():
        if any(needle in text for needle in needles):
            return kind
    return "giudizio"


def watchdog_breakdown(logs: list[dict], hours: float = 24.0,
                       now: datetime | None = None) -> dict:
    """
    Perche' il cancello di risveglio non ha svegliato. Funzione PURA.

    Risponde alla domanda che i log, per come erano scritti, non permettevano:
    quante volte il sistema ha VALUTATO il mercato e ha concluso "niente", e
    quante volte non ha valutato affatto?
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)

    kinds, reasons = Counter(), Counter()
    triggered = considered = total = 0

    for row in logs or []:
        if (row.get("phase") or "") != "WATCHDOG":
            continue
        stamp = _parse_ts(row.get("timestamp"))
        if stamp is None or stamp < cutoff:
            continue
        try:
            payload = json.loads(row.get("content") or "{}")
        except (TypeError, ValueError):
            continue

        total += 1
        event = str(payload.get("event") or "")
        reason = str(payload.get("reason") or "")
        if payload.get("trigger"):
            triggered += 1
            continue

        kind = classify_reason(reason, event)
        kinds[kind] += 1
        reasons[reason[:80] or "(vuoto)"] += 1
        if kind == "giudizio":
            considered += 1

    evaluated = considered + triggered
    return {
        "window_hours": hours,
        "cycles": total,
        "triggered": triggered,
        "not_triggered": total - triggered,
        # Quota di cicli in cui il mercato e' stato davvero guardato.
        "evaluated_pct": round(evaluated / total * 100, 1) if total else 0.0,
        "by_kind": dict(kinds),
        "top_reasons": reasons.most_common(8),
        "health": _gate_health(total, evaluated, kinds),
    }


def _gate_health(total: int, evaluated: int, kinds: Counter) -> str:
    if total == 0:
        return "nessun dato"
    share = evaluated / total
    if kinds.get("guasto", 0) / total > 0.10:
        return "GUASTO: una quota rilevante dei cicli non ha potuto valutare"
    if share < 0.25:
        return ("SILENZIOSO: il cancello quasi non valuta il mercato "
                "(prevalgono throttle e risparmio)")
    if share < 0.60:
        return "PARZIALE: buona parte dei cicli non arriva alla valutazione"
    return "ok"


def inaction_summary(trades: list[dict], portfolio_state: dict,
                     commitments: list[dict] | None = None,
                     now: datetime | None = None) -> dict:
    """
    Il costo dell'omissione in grandezze osservabili. Funzione PURA.

    `commitments`: i livelli d'ingresso che l'agente si autoimpone. Il loro
    tasso di scadenza-senza-esecuzione misura direttamente il comportamento
    "mi pongo un livello e aspetto indefinitamente": una promessa scaduta e
    mai onorata e' un'occasione mancata che finora non costava nulla a
    nessuno, mentre un trade sbagliato veniva contato fino al centesimo.
    """
    now = now or datetime.now(timezone.utc)

    stamps = [s for s in (_parse_ts(t.get("timestamp")) for t in trades or []) if s]
    last_trade = max(stamps) if stamps else None
    days_idle = round((now - last_trade).total_seconds() / 86400, 2) if last_trade else None

    state = portfolio_state or {}
    cash = _as_float(state.get("cash"))
    nav = _as_float(state.get("total_value")) or cash
    cash_pct = round(cash / nav * 100, 1) if nav > 0 else None

    counts = Counter()
    for row in commitments or []:
        counts[str(row.get("status") or "?").lower()] += 1
    resolved = counts["triggered"] + counts["expired"] + counts["cancelled"]
    lapse_pct = (round(counts["expired"] / resolved * 100, 1)
                 if resolved else None)

    return {
        "days_since_last_trade": days_idle,
        "trades_last_7d": sum(1 for s in stamps if (now - s).days < 7),
        "cash_pct": cash_pct,
        "open_positions": len(state.get("positions") or []),
        "commitments": {
            "active": counts["active"],
            "triggered": counts["triggered"],
            "expired_unfulfilled": counts["expired"],
            "cancelled": counts["cancelled"],
            # Quota di promesse scadute senza essere onorate: il numero che
            # descrive "si pone livelli e aspetta".
            "lapse_pct": lapse_pct,
        },
    }


def opportunity_cost(cash: float, benchmark_return_pct: float | None) -> dict:
    """
    Quanto e' costato tenere fermo `cash` mentre il mercato si muoveva.

    Non e' un dettaglio contabile: e' l'unico termine che rende il non-agire
    confrontabile con l'agire. Senza, il sistema riceve numeri veri solo sul
    rischio di muoversi (drawdown, win rate, concentrazione) e zero numeri sul
    rischio di stare fermo — e ottimizza di conseguenza.

    Il segno e' informativo, non un giudizio: in un mercato che scende, stare
    liquidi ha reso. Va letto su una serie, non su un giorno.
    """
    if benchmark_return_pct is None:
        return {"available": False,
                "note": "rendimento del benchmark non disponibile nel periodo"}
    cash = _as_float(cash)
    return {
        "available": True,
        "idle_cash": round(cash, 2),
        "benchmark_return_pct": round(benchmark_return_pct, 3),
        "opportunity_cost_usd": round(cash * benchmark_return_pct / 100.0, 2),
    }


# ── utility ─────────────────────────────────────────────────────────────────

def _parse_ts(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _as_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
