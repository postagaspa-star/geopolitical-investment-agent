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


async def retry_pending_mirrors(window_hours: int = 24, limit: int = 20) -> dict:
    """
    Rileva i trade non specchiati (status='failed' o 'pending') e ritenta.
    Chiamato periodicamente dallo scheduler.
    """
    import database

    pending = database.get_pending_mirror_trades(
        window_hours=window_hours, max_attempts=5, limit=limit,
    )
    if not pending:
        return {"checked": 0, "retried": 0, "succeeded": 0, "still_failed": 0}

    succeeded = 0
    still_failed = 0
    skipped = 0

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

        result = await mirror_trade(
            trade_id=trade_id,
            ticker=ticker, action=action, quantity=qty,
            reasoning=reasoning,
            run_id="cs_retry",
        )
        if result.get("mirrored"):
            succeeded += 1
        elif result.get("skipped"):
            skipped += 1
        else:
            still_failed += 1

    logger.info("ClawStreet auto-retry: checked=%d, ok=%d, skipped=%d, still_failed=%d",
                len(pending), succeeded, skipped, still_failed)
    return {
        "checked": len(pending),
        "retried": len(pending),
        "succeeded": succeeded,
        "skipped": skipped,
        "still_failed": still_failed,
    }
