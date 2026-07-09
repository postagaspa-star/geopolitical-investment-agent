"""
ohlcv_store.py — magazzino OHLCV pre-caricato (Approach 1 anti-dati-corrotti).

PROBLEMA RISOLTO
  Il technical agent scarica 90 giorni di OHLCV per MOLTI ticker IN PARALLELO,
  sul momento, durante il run del Decision. I provider equity (Twelve Data
  free, ecc.) vanno in rate-limit silenzioso sotto quella raffica e
  restituiscono la stessa barra ripetuta → il corruption guard la scarta →
  il ticker esce dall'universo investibile. Con tanti ticker, il campione
  crolla (soprattutto sul Decision STANDARD/equity).

SOLUZIONE
  Un magazzino (tabella ohlcv_daily) tenuto fresco da un job in BACKGROUND
  che scarica l'universo UN TICKER ALLA VOLTA, con pause (mai in parallelo)
  → nessuna raffica → nessun rate-limit. `data_fetchers.fetch_market_data`
  legge PRIMA dal magazzino: se il dato è fresco lo usa senza toccare i
  provider. Durante il run del Decision le richieste vanno tutte al
  magazzino veloce, non ai provider → niente corruzione, campione pieno.

SICUREZZA
  - Dietro FLAG `OHLCV_STORE_ENABLED` (default OFF): quando OFF questo modulo
    è inerte e fetch_market_data si comporta ESATTAMENTE come prima.
  - Nel magazzino finiscono SOLO dati che hanno già passato il corruption
    guard (upsert = il risultato validato del cascade): il magazzino non
    contiene mai barre corrotte.
  - Ogni funzione è best-effort: su qualsiasi errore ritorna None/False e il
    chiamante ripiega sul fetch live. Non può peggiorare lo stato attuale.

Tabella (vedi migrations/create_ohlcv_store.sql):
  ohlcv_daily(ticker, date, open, high, low, close, volume, source, updated_at)
  PRIMARY KEY (ticker, date)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_TABLE = "ohlcv_daily"

# Quanti giorni di storico teniamo per ticker (>= 90 servono al technical;
# 250 danno margine per SMA200 e simili senza gonfiare la tabella).
STORE_KEEP_BARS = 250

# Un ticker nel magazzino è "fresco" (usabile senza fetch live) se è stato
# aggiornato entro questo tempo. Le barre sono DAILY (EOD): qualche ora di
# età è irrilevante per gli indicatori tecnici. Override via env.
DEFAULT_MAX_AGE_SEC = int(os.environ.get("OHLCV_STORE_MAX_AGE_SEC", "21600") or 21600)  # 6h


def ohlcv_store_enabled() -> bool:
    """True se il magazzino OHLCV è attivo (env OHLCV_STORE_ENABLED)."""
    return os.environ.get("OHLCV_STORE_ENABLED", "").strip().lower() in {
        "1", "true", "on", "yes"}


def _get_client():
    try:
        import database
        return database.get_client()
    except Exception:
        return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_seconds(iso_ts: str) -> float:
    try:
        dt = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
        return (_now() - dt).total_seconds()
    except Exception:
        return 1e12   # trattato come "vecchissimo" → non fresco


def get_bars(ticker: str, period_days: int = 90,
             max_age_sec: int | None = None) -> dict | None:
    """
    Legge le barre di un ticker dal magazzino, SOLO se fresche.

    Ritorna un dict nello STESSO formato di data_fetchers.fetch_market_data
    ({ticker, data, error, source, fetched_at, from_store, store_age_sec})
    oppure None se: flag OFF, nessun client, ticker assente, dati stantii,
    o qualsiasi errore. In tutti i casi None → il chiamante fa fetch live.
    """
    if not ohlcv_store_enabled():
        return None
    client = _get_client()
    if client is None:
        return None
    max_age = DEFAULT_MAX_AGE_SEC if max_age_sec is None else max_age_sec
    t = (ticker or "").upper().strip()
    if not t:
        return None
    try:
        # Prendi le ultime N barre (date DESC), con updated_at per la freschezza.
        n = min(max(int(period_days) + 20, 30), STORE_KEEP_BARS)
        res = (client.table(_TABLE)
               .select("date, open, high, low, close, volume, source, updated_at")
               .eq("ticker", t)
               .order("date", desc=True)
               .limit(n)
               .execute())
        rows = res.data or []
        if len(rows) < 5:
            return None
        # Freschezza: il ticker è usabile se l'ultimo aggiornamento è recente.
        newest_upd = max((r.get("updated_at") or "") for r in rows)
        age = _age_seconds(newest_upd)
        if age > max_age:
            return None
        # Riordina ASC (oldest→newest) come si aspettano gli indicatori.
        rows.sort(key=lambda r: str(r.get("date")))
        data = [{
            "date": str(r.get("date"))[:10],
            "open": float(r.get("open") or 0),
            "high": float(r.get("high") or 0),
            "low": float(r.get("low") or 0),
            "close": float(r.get("close") or 0),
            "volume": int(r.get("volume") or 0),
        } for r in rows]
        src = rows[-1].get("source") or "store"
        return {
            "ticker": t,
            "data": data,
            "fetched_at": newest_upd,
            "error": None,
            "source": f"store:{src}",
            "from_store": True,
            "store_age_sec": int(age),
        }
    except Exception as exc:
        logger.debug("[OHLCV-STORE] get_bars %s fallita: %s", t, str(exc)[:150])
        return None


def upsert_bars(ticker: str, records: list, source: str | None = None) -> bool:
    """
    Scrive/aggiorna le barre di un ticker nel magazzino. `records` è la lista
    già validata (post corruption guard) da fetch_market_data. Best-effort.
    """
    if not ohlcv_store_enabled():
        return False
    client = _get_client()
    if client is None or not records:
        return False
    t = (ticker or "").upper().strip()
    if not t:
        return False
    try:
        now_iso = _now().isoformat()
        # Tieni solo le ultime STORE_KEEP_BARS barre (le più recenti).
        recs = records[-STORE_KEEP_BARS:]
        rows = []
        for r in recs:
            d = str(r.get("date") or "")[:10]
            if not d:
                continue
            rows.append({
                "ticker": t,
                "date": d,
                "open": float(r.get("open") or 0),
                "high": float(r.get("high") or 0),
                "low": float(r.get("low") or 0),
                "close": float(r.get("close") or 0),
                "volume": int(r.get("volume") or 0),
                "source": (source or "?")[:40],
                "updated_at": now_iso,
            })
        if not rows:
            return False
        client.table(_TABLE).upsert(rows, on_conflict="ticker,date").execute()
        return True
    except Exception as exc:
        logger.debug("[OHLCV-STORE] upsert_bars %s fallita: %s", t, str(exc)[:150])
        return False


def store_stats() -> dict:
    """Diagnostica per un endpoint admin: quanti ticker freschi/stantii."""
    info = {"enabled": ohlcv_store_enabled(), "tickers": 0, "fresh": 0,
            "stale": 0, "max_age_sec": DEFAULT_MAX_AGE_SEC}
    client = _get_client()
    if client is None:
        info["backend"] = "no_client"
        return info
    try:
        # Un punto per ticker: max(updated_at). PostgREST non aggrega bene,
        # quindi campioniamo le righe recenti (cap) e deduciamo per ticker.
        res = (client.table(_TABLE)
               .select("ticker, updated_at")
               .order("updated_at", desc=True)
               .limit(20000)
               .execute())
        rows = res.data or []
        last_by_ticker: dict[str, str] = {}
        for r in rows:
            tk = r.get("ticker")
            if tk and tk not in last_by_ticker:
                last_by_ticker[tk] = r.get("updated_at") or ""
        info["tickers"] = len(last_by_ticker)
        for upd in last_by_ticker.values():
            if _age_seconds(upd) <= DEFAULT_MAX_AGE_SEC:
                info["fresh"] += 1
            else:
                info["stale"] += 1
    except Exception as exc:
        info["error"] = str(exc)[:150]
    return info


def warm_universe_tickers() -> list[str]:
    """
    Lista dei ticker equity/ETF da tenere caldi nel magazzino: core equity +
    ETF settoriali/satellite + universo rotation-scan. Solo equity (le crypto
    usano Binance, che non ha il problema rate-limit). Dedup preservando
    l'ordine (i core per primi = priorità se il ciclo viene troncato).
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(seq):
        for x in seq or []:
            u = (x or "").upper().strip()
            if u and u not in seen and not (u.endswith("-USD") or u.startswith("X:")):
                seen.add(u)
                out.append(u)

    try:
        import universe
        _add(getattr(universe, "CORE_EQUITY_EXAMPLES", []))
        try:
            _add(universe.satellite_universe_flat())
        except Exception:
            for vals in getattr(universe, "SATELLITE", {}).values():
                _add(vals)
    except Exception as exc:
        logger.debug("[OHLCV-STORE] universe import fallito: %s", exc)
    # Universo rotation-scan (ETF cross-sector) se disponibile.
    try:
        from agents import rotation_scan
        for attr in ("ROTATION_UNIVERSE", "UNIVERSE", "ROTATION_TICKERS"):
            u = getattr(rotation_scan, attr, None)
            if isinstance(u, dict):
                for vals in u.values():
                    _add(vals if isinstance(vals, (list, tuple)) else [])
            elif isinstance(u, (list, tuple)):
                _add(u)
    except Exception:
        pass
    return out
