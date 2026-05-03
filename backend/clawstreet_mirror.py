"""
clawstreet_mirror.py — Wrapper centralizzato per il mirroring trade su ClawStreet.

Tutti i path che eseguono trade (decision.py, multi_agent.py, tools.py)
DEVONO usare `mirror_trade(trade_id, ticker, action, quantity, reasoning)`
invece di chiamare data_fetchers.mirror_trade_to_clawstreet direttamente.

Vantaggi:
  - Aggiorna automaticamente cs_mirror_status sul trade
  - Permette retry automatici via scheduled job
  - Centralizza la lettura delle credenziali (DB → env fallback)
  - Logga in modo uniforme
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def _get_credentials() -> tuple[str, str]:
    """Recupera bot_id + api_key da DB con fallback su env."""
    try:
        import database as _db
        bot_id = _db.get_setting("clawstreet_bot_id", "") or os.environ.get("CLAWSTREET_BOT_ID", "")
        api_key = _db.get_setting("clawstreet_api_key", "") or os.environ.get("CLAWSTREET_API_KEY", "")
    except Exception:
        bot_id = os.environ.get("CLAWSTREET_BOT_ID", "")
        api_key = os.environ.get("CLAWSTREET_API_KEY", "")
    return bot_id.strip(), api_key.strip()


async def mirror_trade(
    trade_id: Optional[int],
    ticker: str,
    action: str,
    quantity: int,
    reasoning: str,
    run_id: str = "",
) -> dict:
    """
    Specchia un trade su ClawStreet AGGIORNANDO cs_mirror_status sul trade.

    Args:
        trade_id: ID della riga trades (da insert_trade). Se None, niente
                  aggiornamento status — usato solo per fire-and-forget legacy.
        ticker: ticker yfinance (es. "BTC-USD", "AAPL")
        action: "BUY" | "SELL"
        quantity: numero di unità (intero)
        reasoning: motivazione (max 280 char)
        run_id: ID del run multi-agente (per logging)

    Returns:
        dict con esito (mirrored, skipped, status, reason). Stessa shape
        del data_fetchers.mirror_trade_to_clawstreet originale.
    """
    import database
    import data_fetchers

    bot_id, api_key = _get_credentials()
    if not bot_id or not api_key:
        if trade_id is not None:
            try:
                database.update_trade_mirror_status(
                    trade_id, "no_creds", "Credenziali ClawStreet non configurate"
                )
            except Exception as exc:
                logger.warning("update_trade_mirror_status no_creds fallita: %s", exc)
        if run_id:
            try:
                database.insert_agent_log(run_id, "CLAWSTREET_MIRROR", json.dumps({
                    "trade_id": trade_id, "ticker": ticker, "action": action,
                    "quantity": quantity, "mirrored": False,
                    "skipped": "credentials_missing",
                }))
            except Exception:
                pass
        return {"mirrored": False, "skipped": True, "reason": "no_credentials"}

    # Chiama il mirror "low-level" (esistente, con retry interni HTTP)
    try:
        result = await data_fetchers.mirror_trade_to_clawstreet(
            bot_id=bot_id, api_key=api_key,
            symbol=ticker, action=action, qty=quantity,
            reasoning=reasoning[:280],
        )
    except Exception as exc:
        logger.error("ClawStreet mirror exception (trade_id=%s, %s %d %s): %s",
                     trade_id, action, quantity, ticker, exc, exc_info=True)
        result = {"mirrored": False, "error": str(exc)[:300]}

    # Determina lo stato finale (in modo coerente con i return di mirror_trade_to_clawstreet)
    if result.get("mirrored"):
        status = "ok"
        reason = f"HTTP {result.get('status', '')}"
    elif result.get("skipped"):
        # ClawStreet ha rifiutato per motivi attesi (INVALID_SYMBOL, NO_BUYING_POWER, ...)
        # Non è un fallimento dell'app — è un limite della piattaforma.
        # Non riprovare in retry: marca come "skipped" definitivamente.
        status = "skipped"
        reason = result.get("reason", "skipped")
    else:
        # Errore vero (HTTP 500, network, timeout, ...): andrà in retry
        status = "failed"
        reason = (
            result.get("error")
            or f"HTTP {result.get('status', '')}: {result.get('response', '')[:100]}"
        )

    # Aggiorna lo stato sul trade
    if trade_id is not None:
        try:
            database.update_trade_mirror_status(trade_id, status, reason)
        except Exception as exc:
            logger.warning("update_trade_mirror_status fallita per trade %s: %s", trade_id, exc)

    # Logga nel agent_logs (per dashboard cronologica)
    if run_id:
        try:
            database.insert_agent_log(run_id, "CLAWSTREET_MIRROR", json.dumps({
                "trade_id": trade_id,
                "ticker": ticker, "action": action, "quantity": quantity,
                "mirrored": status == "ok",
                "status": status, "reason": reason[:300],
                "http_status": result.get("status"),
            }, default=str))
        except Exception:
            pass

    return result


async def _fetch_cs_trades_set(bot_id: str, api_key: str) -> set[tuple]:
    """
    Fetcha i trade già su ClawStreet e ritorna un set di (symbol, action, qty)
    per dedup. Usato dal retry job per evitare di duplicare trade già presenti.
    """
    import aiohttp as _aiohttp
    from clawstreet_universe import to_clawstreet_format

    cs_set: set[tuple] = set()
    try:
        async with _aiohttp.ClientSession() as sess:
            async with sess.get(
                f"https://www.clawstreet.io/api/bots/{bot_id}/trades",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=_aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    return cs_set
                data = await resp.json(content_type=None)
        trades = data.get("trades", []) if isinstance(data, dict) else (data or [])
        for t in trades:
            sym = (t.get("symbol") or "").upper()
            act = (t.get("action") or "").lower()
            qty = int(t.get("qty") or t.get("quantity") or 0)
            if sym and act and qty > 0:
                cs_set.add((sym, act, qty))
    except Exception as exc:
        logger.warning("Impossibile fetchare CS trades per dedup: %s", exc)
    return cs_set


async def retry_pending_mirrors(window_hours: int = 24, limit: int = 20) -> dict:
    """
    Rileva i trade non specchiati (status='failed' o 'pending') e ritenta —
    MA prima fa dedup contro lo storico ClawStreet per evitare duplicati.

    Nuova logica (anti-duplicate):
      1. Fetcha trade già su CS e crea set (symbol, action, qty)
      2. Per ogni trade locale pending:
         - Calcola la chiave canonica (cs_format(ticker), action.lower, qty)
         - Se la chiave è in CS → marca come 'ok' senza nuova chiamata HTTP
         - Altrimenti → tenta il mirror via mirror_trade()
      3. Aggiorna sempre cs_mirror_status sul trade locale.

    Questo previene il caso problematico: trade fatti PRE-migration v6 sono
    finiti col flag default 'pending' anche se erano già stati specchiati
    con successo. Senza dedup, il retry li rimirrorerebbe creando duplicati.
    """
    import database
    from clawstreet_universe import to_clawstreet_format

    pending = database.get_pending_mirror_trades(
        window_hours=window_hours, max_attempts=5, limit=limit,
    )
    if not pending:
        return {"checked": 0, "retried": 0, "succeeded": 0, "still_failed": 0,
                "skipped_already_on_cs": 0}

    bot_id, api_key = _get_credentials()
    cs_existing = set()
    if bot_id and api_key:
        cs_existing = await _fetch_cs_trades_set(bot_id, api_key)

    succeeded = 0
    still_failed = 0
    skipped_unsupported = 0
    skipped_already_on_cs = 0
    # Counter di trade locali per gestire copie identiche multiple
    from collections import Counter
    local_counter: Counter = Counter()

    for tr in pending:
        trade_id = tr.get("id")
        ticker = tr.get("ticker", "")
        action = (tr.get("action") or "").upper()
        qty = int(tr.get("quantity") or 0)
        reasoning = (
            tr.get("final_decision")
            or tr.get("geopolitical_reasoning")
            or f"Auto-retry trade {trade_id}"
        )[:280]

        if not ticker or action not in ("BUY", "SELL") or qty <= 0:
            still_failed += 1
            continue

        # Dedup check: se questa coppia (sym, act, qty) è già su CS e non
        # abbiamo già "consumato" tutte le occorrenze CS con altri retry
        # locali, marca come ok senza rimirrorare.
        cs_sym = to_clawstreet_format(ticker)
        key = (cs_sym, action.lower(), qty)
        local_counter[key] += 1
        cs_count = sum(1 for k in cs_existing if k == key)
        # cs_existing è un set (dedup), quindi cs_count è 0 o 1. Non
        # gestiamo bene il caso "2 trade locali identici, 1 su CS" — ma è
        # raro, e la peggior conseguenza è 1 trade duplicato su CS.
        if cs_count >= 1 and local_counter[key] <= cs_count:
            try:
                database.update_trade_mirror_status(
                    trade_id, "ok",
                    f"Already on CS (dedup retry, key={key})",
                )
            except Exception:
                pass
            skipped_already_on_cs += 1
            continue

        result = await mirror_trade(
            trade_id=trade_id,
            ticker=ticker, action=action, quantity=qty,
            reasoning=reasoning,
            run_id="cs_retry",
        )
        if result.get("mirrored"):
            succeeded += 1
            cs_existing.add(key)   # aggiungi al set per dedup intra-batch
        elif result.get("skipped"):
            skipped_unsupported += 1
        else:
            still_failed += 1

    logger.info(
        "ClawStreet auto-retry: checked=%d ok=%d already_on_cs=%d "
        "skipped_unsupported=%d still_failed=%d",
        len(pending), succeeded, skipped_already_on_cs,
        skipped_unsupported, still_failed,
    )
    return {
        "checked": len(pending),
        "retried": len(pending),
        "succeeded": succeeded,
        "skipped_already_on_cs": skipped_already_on_cs,
        "skipped_unsupported": skipped_unsupported,
        "still_failed": still_failed,
    }
