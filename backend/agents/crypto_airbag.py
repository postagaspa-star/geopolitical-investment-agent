"""
Crypto Airbag — scudo deterministico anti-crash SOPRA l'LLM.

CONTESTO (la sintesi di ~250 backtest + misura reale dell'LLM in prod):
- L'LLM di GeoInvest trada in modo decente nei tempi normali (+5.5% in 2.5 mesi,
  mai sotto il capitale iniziale). NON ha un drawdown serio nei periodi calmi.
- MA non e' mai stato testato da un vero BEAR. Li' e' il rischio.
- Lo scudo deterministico (regime settimanale) e' l'UNICA cosa robusta su 2
  cicli indipendenti (2017-20 e 2021-24): protegge nei crash. NON genera alpha.

ARCHITETTURA (decisa con Andrea): l'LLM resta il cervello che trada; l'airbag
e' l'AIRBAG che si attiva SOLO quando il regime di mercato (BTC, settimanale)
entra in DOWNTREND macro confermato. Quando scatta: mette in CASH tutte le
posizioni crypto (versione 'cash', la piu' prudente e validata), finche' il
regime non torna UP. Nei tempi normali (UP/SIDE) NON interviene: zero
interferenza con l'LLM.

SICUREZZA:
- Flag-gated: no-op se settings['crypto_airbag_enabled'] != 'true' (default OFF).
- Deterministico: la decisione (bear si/no) dipende solo dai prezzi BTC, niente LLM.
- Fail-safe: ogni errore → non fa nulla (non vende per sbaglio).
- Idempotente: se e' gia' intervenuto e siamo ancora bear, non rivende (niente
  da vendere). Se il regime torna UP, NON ricompra (lascia fare all'LLM).
- Governatori di rischio (risk_profile/risk_state) NON toccati: l'airbag sta
  SOPRA, e' un layer di protezione aggiuntivo.

Pensato per girare nello scheduler (es. una volta al giorno: il regime e'
settimanale, non serve piu' spesso).
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

SETTING_ENABLED = "crypto_airbag_enabled"        # default OFF
SETTING_STATE = "_airbag::state"                 # "armed" | "deployed" (namespaced → non in UI)
BTC_SYMBOL = "BTC-USD"

# Parametri del regime settimanale (gli stessi validati nei backtest)
_WEEKLY_REGIME = dict(ema_fast=8, ema_slow=20, atr_len=10, short_confirm_bars=2,
                      shield="cash")


def _is_enabled() -> bool:
    try:
        import database
        return (database.get_setting(SETTING_ENABLED, "false") or "false").strip().lower() == "true"
    except Exception:
        return False


def _is_crypto(ticker: str) -> bool:
    t = (ticker or "").upper().strip()
    return t.endswith("-USD") or t.startswith("X:")


def _fetch_btc_weekly() -> Optional[list]:
    """Scarica BTC daily e lo ricampiona in candele SETTIMANALI (7 giorni).
    Ritorna lista di bar OHLCV (vecchia→recente), o None su errore."""
    try:
        import data_fetchers
        # ~400 giorni daily → ~57 settimane, abbastanza per EMA20 settimanale
        md = data_fetchers.fetch_market_data(BTC_SYMBOL, 400)
        daily = (md or {}).get("data") or []
        if len(daily) < 140:
            return None
        weekly = []
        for i in range(0, len(daily), 7):
            chunk = daily[i:i+7]
            if not chunk:
                continue
            weekly.append({
                "open": chunk[0].get("open"),
                "high": max(b.get("high", 0) for b in chunk),
                "low": min(b.get("low", 1e18) for b in chunk),
                "close": chunk[-1].get("close"),
                "volume": sum(b.get("volume", 0) for b in chunk),
            })
        return weekly if len(weekly) >= 25 else None
    except Exception as e:
        logger.warning("[AIRBAG] fetch BTC weekly fail: %s", e)
        return None


def detect_market_regime() -> Optional[str]:
    """Regime di mercato dal BTC settimanale: 'UPTREND'|'DOWNTREND'|'SIDEWAYS'
    oppure None se dati insufficienti. Deterministico, no LLM.

    Fa avanzare la macchina a stati del regime su TUTTE le candele settimanali
    (l'isteresi richiede continuita'), poi ritorna lo stato finale."""
    weekly = _fetch_btc_weekly()
    if not weekly:
        return None
    try:
        from agents.crypto_regime import RegimeParams, RegimeState, step
        p = RegimeParams(**_WEEKLY_REGIME)
        st = RegimeState()
        warm = p.ema_slow + 6
        last_regime = None
        for k in range(warm, len(weekly) + 1):
            act = step(st, weekly[:k], nav=100_000.0, params=p)
            last_regime = act.regime
        return last_regime
    except Exception as e:
        logger.warning("[AIRBAG] regime detect fail: %s", e)
        return None


def run_airbag(run_id: str | None = None) -> dict:
    """Un tick dell'airbag. Se abilitato e il mercato e' in DOWNTREND macro
    confermato, mette in CASH tutte le posizioni crypto. Altrimenti no-op.

    Ritorna un report. Best-effort: ogni errore → non vende (fail-safe)."""
    from uuid import uuid4
    rid = run_id or str(uuid4())[:8]

    if not _is_enabled():
        return {"skipped": True, "reason": "airbag disabilitato (flag OFF)"}

    import database
    regime = detect_market_regime()
    if regime is None:
        return {"skipped": True, "reason": "regime non determinabile (dati BTC insuff.)"}

    prev_state = database.get_setting(SETTING_STATE, "armed") or "armed"

    # Mercato sano (UP/SIDE): l'airbag NON interviene. Se era 'deployed', si
    # ri-arma (il bear e' finito) ma NON ricompra: lascia fare all'LLM.
    if regime != "DOWNTREND":
        if prev_state == "deployed":
            database.set_setting(SETTING_STATE, "armed")
            database.insert_agent_log(rid, "CRYPTO_AIRBAG_REARMED", _j({
                "regime": regime, "note": "bear finito: airbag ri-armato, LLM libero di operare",
            }))
            return {"skipped": False, "action": "rearmed", "regime": regime}
        return {"skipped": True, "reason": f"mercato {regime}: airbag in attesa (nessun intervento)"}

    # ── DOWNTREND macro confermato: METTI IN CASH le crypto ──────────────────
    import portfolio
    try:
        positions = database.get_positions() or []
    except Exception as e:
        return {"skipped": True, "reason": f"lettura posizioni fallita: {e}"}

    crypto_pos = [p for p in positions
                  if _is_crypto(p.get("ticker")) and float(p.get("quantity") or 0) > 0]

    if not crypto_pos:
        # gia' in cash (o airbag gia' scattato): segna deployed, niente da fare
        if prev_state != "deployed":
            database.set_setting(SETTING_STATE, "deployed")
        return {"skipped": False, "action": "already_cash", "regime": regime,
                "sold": []}

    sold = []
    for p in crypto_pos:
        ticker = (p.get("ticker") or "").upper()
        qty = float(p.get("quantity") or 0)
        try:
            price = _current_price(ticker)
            if not price or price <= 0:
                logger.warning("[AIRBAG %s] prezzo non disp. per %s: skip", rid, ticker)
                continue
            res = portfolio.execute_sell(
                ticker, qty, price,
                geo_reasoning="(airbag deterministico)",
                tech_reasoning=f"AIRBAG: mercato in DOWNTREND macro confermato "
                               f"(BTC settimanale) → metto in cash per protezione.",
                confidence=None,
                execution_type="airbag",
            )
            ok = isinstance(res, dict) and res.get("success")
            sold.append({"ticker": ticker, "qty": qty, "price": price, "ok": bool(ok)})
        except Exception as e:
            logger.error("[AIRBAG %s] vendita %s fallita: %s", rid, ticker, e)
            sold.append({"ticker": ticker, "qty": qty, "ok": False, "error": str(e)[:200]})

    database.set_setting(SETTING_STATE, "deployed")
    database.insert_agent_log(rid, "CRYPTO_AIRBAG_DEPLOYED", _j({
        "regime": regime,
        "positions_closed": len([s for s in sold if s.get("ok")]),
        "detail": sold,
        "note": "Scudo anti-crash scattato: posizioni crypto messe in cash.",
    }))
    return {"skipped": False, "action": "deployed", "regime": regime, "sold": sold}


def _current_price(ticker: str) -> Optional[float]:
    """Prezzo fresco per la vendita. Bypassa cache se possibile."""
    try:
        import data_fetchers
        px = data_fetchers.fetch_fresh_current_price(ticker)
        if px and px > 0:
            return float(px)
    except Exception:
        pass
    try:
        import data_fetchers
        md = data_fetchers.fetch_market_data(ticker, 5)
        if md and md.get("data"):
            return float(md["data"][-1]["close"])
    except Exception:
        pass
    return None


def _j(d: dict) -> str:
    import json
    return json.dumps(d, default=str)
