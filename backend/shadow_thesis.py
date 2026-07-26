"""
shadow_thesis — l'agente ombra: previsioni senza soldi, contro un gemello cieco.

LA DOMANDA A CUI RISPONDE
"Il radar del sistema ha davvero fiuto, o e' indistinguibile dal caso?"
Con i trade veri (5-10 al mese) servirebbero anni per saperlo: il campione
cresce troppo piano. Qui il sistema scrive ogni giorno di borsa sei previsioni
FINTE — nessun euro coinvolto — e un gemello cieco ne scrive altrettante a
caso, sugli stessi titoli possibili. Dopo ~50 previsioni chiuse per parte, il
confronto e' un fatto, non un'opinione.

COSA VIENE MISURATO, ONESTAMENTE
Si misura il segnale che DAVVERO guida il sistema oggi: il punteggio di
rotazione (momentum multi-periodo sui ~89 titoli dell'universo equity). Non
si misura "la geopolitica", perche' — come documentato nell'audit — la
geopolitica non entra in nessuna formula: e' prosa nel prompt. Se un giorno
il segnale cambiera', questo stesso banco di prova potra' misurare il nuovo.

LE PREVISIONI
Ogni giorno di borsa: LONG (finto) sui 3 titoli col punteggio piu' alto,
SHORT (finto) sui 3 col punteggio piu' basso. Verifica dopo 5 sedute sulla
chiusura. Il gemello cieco estrae 6 titoli a caso dallo stesso elenco di
titoli validi quel giorno, 3 long e 3 short, con un'estrazione RIPRODUCIBILE
(il seme e' la data: nessuno puo' ripescare finche' non esce bene).

REGOLE DI ONESTA'
- Se i dati del radar quel giorno sono sporchi, non si scrive nulla e lo si
  registra (SHADOW_SKIPPED): un test su dati corrotti non e' un test.
- Se mancano le barre di prezzo per la verifica, la previsione diventa VOID
  (annullata), mai inventata.
- Nessun verdetto sotto le 50 previsioni chiuse per parte: prima di quella
  soglia l'endpoint risponde "campione insufficiente", punto.
- Tutto e' salvato come righe di log (SHADOW_THESIS / SHADOW_RESOLVED):
  niente nuove tabelle, niente migrazioni, interrogabile con cio' che c'e'.

Il modulo e' quasi tutto FUNZIONI PURE (testabili); l'I/O sta in fondo.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from datetime import datetime, date, timedelta, timezone

logger = logging.getLogger(__name__)

# ── Parametri ───────────────────────────────────────────────────────────────

HORIZON_SESSIONS = 5          # verifica dopo 5 sedute di borsa
N_LONG = 3                    # previsioni "compra" al giorno
N_SHORT = 3                   # previsioni "vendi" al giorno
MIN_VALID_ROWS = 20           # sotto questa copertura il radar non e' affidabile
MIN_RESOLVED_FOR_VERDICT = 50 # niente verdetti prima di 50 chiuse per parte
VOID_AFTER_CALENDAR_DAYS = 15 # se dopo 15 giorni mancano ancora le barre: VOID

SETTING_ENABLED = "shadow_thesis_enabled"          # default: on
SETTING_LAST_CREATED = "shadow_last_created_date"

PHASE_THESIS = "SHADOW_THESIS"
PHASE_RESOLVED = "SHADOW_RESOLVED"
PHASE_SKIPPED = "SHADOW_SKIPPED"


# ── Funzioni pure ───────────────────────────────────────────────────────────

def is_us_trading_day(day_iso: str, holidays: set[str] | None = None) -> bool:
    """Sabato/domenica e festivita' NYSE non sono giorni di previsione."""
    try:
        d = date.fromisoformat(day_iso)
    except (TypeError, ValueError):
        return False
    if d.weekday() >= 5:
        return False
    return day_iso not in (holidays or set())


def valid_scan_rows(scan: dict) -> list[dict]:
    """Righe del radar utilizzabili: score numerico e dati non marcati sporchi."""
    rows = []
    for r in (scan or {}).get("rows") or []:
        if r.get("data_quality") not in (None, "ok"):
            continue
        ticker = (r.get("ticker") or "").strip().upper()
        try:
            score = float(r.get("rotation_score"))
        except (TypeError, ValueError):
            continue
        if ticker:
            rows.append({"ticker": ticker, "score": score})
    return rows


def _thesis(day_iso: str, ticker: str, direction: str, kind: str,
            score: float | None) -> dict:
    return {
        "id": f"sh-{day_iso}-{ticker}-{direction[0]}-{kind[0]}",
        "date": day_iso,
        "ticker": ticker,
        "direction": direction,
        "kind": kind,                  # "real" | "random"
        "score": score,
        "horizon_sessions": HORIZON_SESSIONS,
    }


def build_real_theses(rows: list[dict], day_iso: str) -> list[dict]:
    """Top-3 LONG e bottom-3 SHORT per punteggio di rotazione."""
    ordered = sorted(rows, key=lambda r: r["score"], reverse=True)
    out = [_thesis(day_iso, r["ticker"], "LONG", "real", r["score"])
           for r in ordered[:N_LONG]]
    out += [_thesis(day_iso, r["ticker"], "SHORT", "real", r["score"])
            for r in ordered[-N_SHORT:]]
    return out


def build_random_twin(rows: list[dict], day_iso: str) -> list[dict]:
    """
    Il gemello cieco: stessi titoli possibili, stessa composizione (3 long,
    3 short), scelta a caso ma RIPRODUCIBILE — il seme e' la data, quindi
    l'estrazione e' fissata prima di conoscere l'esito e non e' ripetibile
    finche' non "esce bene".
    """
    rng = random.Random(f"shadow-{day_iso}")
    tickers = sorted({r["ticker"] for r in rows})
    picked = rng.sample(tickers, N_LONG + N_SHORT)
    out = [_thesis(day_iso, t, "LONG", "random", None) for t in picked[:N_LONG]]
    out += [_thesis(day_iso, t, "SHORT", "random", None) for t in picked[N_LONG:]]
    return out


def normalize_bars(raw_bars: list[dict]) -> list[dict]:
    """Barre {date, close} pulite: dedup per data, ordinate, chiusure > 0."""
    by_date: dict[str, float] = {}
    for b in raw_bars or []:
        day = str(b.get("date") or "")[:10]
        try:
            close = float(b.get("close") or 0)
        except (TypeError, ValueError):
            continue
        if len(day) == 10 and close > 0:
            by_date[day] = close
    return [{"date": d, "close": by_date[d]} for d in sorted(by_date)]


def resolve_thesis(thesis: dict, bars: list[dict],
                   today_iso: str) -> dict | None:
    """
    Esito di una previsione. Ritorna None se e' ancora presto (OPEN),
    altrimenti un dict con status RESOLVED (ret_pct, hit) oppure VOID.
    """
    day = thesis["date"]
    try:
        age_days = (date.fromisoformat(today_iso) - date.fromisoformat(day)).days
    except (TypeError, ValueError):
        return {"id": thesis["id"], "status": "VOID", "why": "data_illeggibile"}

    idx = next((i for i, b in enumerate(bars) if b["date"] == day), None)
    if idx is None:
        # La barra del giorno di creazione non c'e' (ancora?).
        if age_days > VOID_AFTER_CALENDAR_DAYS:
            return {"id": thesis["id"], "status": "VOID", "why": "manca_barra_ingresso"}
        return None                              # OPEN: si riprova domani

    end = idx + thesis.get("horizon_sessions", HORIZON_SESSIONS)
    if end >= len(bars):
        if age_days > VOID_AFTER_CALENDAR_DAYS:
            return {"id": thesis["id"], "status": "VOID", "why": "barre_insufficienti"}
        return None                              # OPEN: mancano sedute

    start_close = bars[idx]["close"]
    end_close = bars[end]["close"]
    ret_pct = (end_close - start_close) / start_close * 100.0
    hit = ret_pct > 0 if thesis["direction"] == "LONG" else ret_pct < 0
    return {
        "id": thesis["id"], "status": "RESOLVED",
        "ticker": thesis["ticker"], "direction": thesis["direction"],
        "kind": thesis["kind"], "date": day,
        "ret_pct": round(ret_pct, 4), "hit": hit,
    }


def summarize(resolved: list[dict]) -> dict:
    """Pagella di una serie di previsioni chiuse."""
    n = len(resolved)
    if n == 0:
        return {"n": 0, "hit_rate_pct": None, "avg_ret_pct": None}
    hits = sum(1 for r in resolved if r.get("hit"))
    # Rendimento "firmato": per gli SHORT un ribasso e' un guadagno.
    signed = [(-r["ret_pct"] if r["direction"] == "SHORT" else r["ret_pct"])
              for r in resolved]
    return {
        "n": n,
        "hit_rate_pct": round(hits / n * 100.0, 1),
        "avg_ret_pct": round(sum(signed) / n, 3),
    }


def compare(real: list[dict], rnd: list[dict]) -> dict:
    """
    Confronto radar vs caso. Nessun verdetto sotto la soglia minima: la
    tentazione di leggere 10 previsioni come un destino e' esattamente il
    tipo di autoinganno che questo modulo esiste per impedire.
    """
    s_real, s_rnd = summarize(real), summarize(rnd)
    out = {"real": s_real, "random": s_rnd,
           "min_per_verdetto": MIN_RESOLVED_FOR_VERDICT}
    if s_real["n"] < MIN_RESOLVED_FOR_VERDICT or s_rnd["n"] < MIN_RESOLVED_FOR_VERDICT:
        out["verdict"] = "campione_insufficiente"
        out["spiegazione"] = (
            f"servono almeno {MIN_RESOLVED_FOR_VERDICT} previsioni chiuse per "
            f"parte (ora: radar {s_real['n']}, caso {s_rnd['n']})")
        return out

    diff = s_real["hit_rate_pct"] - s_rnd["hit_rate_pct"]
    # Errore standard combinato delle due frequenze (approssimazione normale).
    p1, p2 = s_real["hit_rate_pct"] / 100.0, s_rnd["hit_rate_pct"] / 100.0
    se = ((p1 * (1 - p1) / s_real["n"]) + (p2 * (1 - p2) / s_rnd["n"])) ** 0.5 * 100
    out["diff_hit_rate_pct"] = round(diff, 1)
    out["rumore_2se_pct"] = round(2 * se, 1)
    if abs(diff) <= 2 * se:
        out["verdict"] = "indistinguibile_dal_caso"
        out["spiegazione"] = (
            f"la differenza ({diff:+.1f} punti) sta dentro il rumore "
            f"(±{2*se:.1f}): nessuna prova di fiuto, ne' in bene ne' in male")
    elif diff > 0:
        out["verdict"] = "radar_batte_il_caso"
        out["spiegazione"] = (
            f"+{diff:.1f} punti di accuratezza oltre il rumore (±{2*se:.1f}): "
            f"il segnale ha valore predittivo su questo orizzonte")
    else:
        out["verdict"] = "caso_batte_il_radar"
        out["spiegazione"] = (
            f"{diff:.1f} punti SOTTO il caso, oltre il rumore (±{2*se:.1f}): "
            f"il segnale sta indicando la direzione sbagliata")
    return out


# ── I/O: creazione giornaliera, risoluzione, lettura ───────────────────────

def _enabled(database) -> bool:
    try:
        raw = (database.get_setting(SETTING_ENABLED, "true") or "").strip().lower()
        return raw not in ("0", "false", "no", "off")
    except Exception:
        return True


def _nyse_holidays() -> set[str]:
    try:
        from scheduler import NYSE_HOLIDAYS
        return set(NYSE_HOLIDAYS)
    except Exception:
        return set()


def _log(database, run_id: str, phase: str, payload: dict) -> None:
    try:
        database.insert_agent_log(run_id, phase, json.dumps(payload, default=str))
    except Exception as exc:
        logger.warning("shadow: log %s non scritto (%s)", phase, exc)


def load_shadow_state(logs: list[dict]) -> tuple[list[dict], list[dict]]:
    """(tesi, risoluzioni) gia' presenti nei log. Funzione pura sui log."""
    theses, resolved = [], []
    for row in logs or []:
        phase = row.get("phase")
        if phase not in (PHASE_THESIS, PHASE_RESOLVED):
            continue
        try:
            payload = json.loads(row.get("content") or "{}")
        except (TypeError, ValueError):
            continue
        (theses if phase == PHASE_THESIS else resolved).append(payload)
    return theses, resolved


async def create_daily(database) -> dict:
    """Scrive le previsioni del giorno (radar + gemello cieco), se e' giorno di borsa."""
    today = datetime.now(timezone.utc).date().isoformat()
    run_id = f"shadow_{today}"

    if not _enabled(database):
        return {"created": 0, "reason": "disabilitato"}
    if not is_us_trading_day(today, _nyse_holidays()):
        return {"created": 0, "reason": "borsa_chiusa"}
    try:
        if (database.get_setting(SETTING_LAST_CREATED, "") or "") == today:
            return {"created": 0, "reason": "gia_creato_oggi"}
    except Exception:
        pass

    from agents.rotation_scan import scan_rotation_universe
    scan = await scan_rotation_universe()

    if (scan or {}).get("data_quality") != "ok":
        _log(database, run_id, PHASE_SKIPPED,
             {"date": today, "why": "radar_su_dati_sporchi",
              "data_quality": (scan or {}).get("data_quality")})
        return {"created": 0, "reason": "dati_sporchi"}

    rows = valid_scan_rows(scan)
    if len(rows) < MIN_VALID_ROWS:
        _log(database, run_id, PHASE_SKIPPED,
             {"date": today, "why": "copertura_insufficiente", "rows": len(rows)})
        return {"created": 0, "reason": "poche_righe"}

    theses = build_real_theses(rows, today) + build_random_twin(rows, today)
    for t in theses:
        _log(database, run_id, PHASE_THESIS, t)
    try:
        database.set_setting(SETTING_LAST_CREATED, today)
    except Exception:
        pass
    logger.info("shadow: scritte %d previsioni per il %s", len(theses), today)
    return {"created": len(theses), "reason": "ok"}


def resolve_due(database) -> dict:
    """Chiude le previsioni mature usando le chiusure giornaliere."""
    today = datetime.now(timezone.utc).date().isoformat()
    theses, resolved = load_shadow_state(database.get_agent_logs(limit=5000) or [])
    done_ids = {r.get("id") for r in resolved}
    pending = [t for t in theses if t.get("id") and t["id"] not in done_ids]
    if not pending:
        return {"resolved": 0, "void": 0, "open": 0}

    from data_fetchers import fetch_market_data
    bars_cache: dict[str, list[dict]] = {}
    counts = {"resolved": 0, "void": 0, "open": 0}
    for t in pending:
        ticker = t.get("ticker") or ""
        if ticker not in bars_cache:
            try:
                raw = fetch_market_data(ticker, period_days=40)
                bars_cache[ticker] = normalize_bars((raw or {}).get("data") or [])
            except Exception as exc:
                logger.debug("shadow: barre %s non disponibili (%s)", ticker, exc)
                bars_cache[ticker] = []
            time.sleep(0.3)          # gentilezza verso i provider
        outcome = resolve_thesis(t, bars_cache[ticker], today)
        if outcome is None:
            counts["open"] += 1
            continue
        _log(database, f"shadow_{today}", PHASE_RESOLVED, outcome)
        counts["resolved" if outcome["status"] == "RESOLVED" else "void"] += 1
    logger.info("shadow: risoluzione %s", counts)
    return counts


async def run_shadow_job() -> None:
    """Job giornaliero: prima chiude le previsioni mature, poi scrive le nuove."""
    import database
    if not _enabled(database):
        return
    today = datetime.now(timezone.utc).date().isoformat()
    if not is_us_trading_day(today, _nyse_holidays()):
        return                       # niente barre nuove: inutile lavorare
    try:
        await asyncio.to_thread(resolve_due, database)
    except Exception as exc:
        logger.warning("shadow: risoluzione fallita (%s)", exc)
    try:
        await create_daily(database)
    except Exception as exc:
        logger.warning("shadow: creazione fallita (%s)", exc)
