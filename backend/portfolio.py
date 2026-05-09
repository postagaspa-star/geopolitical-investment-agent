"""
Modulo di gestione del portafoglio virtuale.

L'agente AI ha piena autonomia decisionale su:
- Quante posizioni aprire e di quale dimensione
- Quando vendere (stop-loss, take-profit, ribilanciamento)
- Livello di confidenza minimo per operare
- Allocazione del portafoglio per settore

Il codice si limita a eseguire gli ordini e a verificare che ci sia
liquidita' sufficiente e che le posizioni esistano prima di venderle.
"""

import json
import logging

logger = logging.getLogger(__name__)

from database import (
    get_portfolio, update_portfolio,
    get_positions, get_position,
    upsert_position, delete_position,
    update_position_price, count_positions,
    insert_trade, get_setting,
)

# Helpers per SL/TP automatici impostati dall'agente.
# Import lazy con try/except per non rompere se le colonne non esistono ancora
# (es. migration non ancora applicata su un Supabase legacy).
try:
    from database import update_position_auto_exit, get_positions_with_auto_exits
except ImportError:
    def update_position_auto_exit(*a, **kw):  # type: ignore
        return False
    def get_positions_with_auto_exits():  # type: ignore
        return []


def calculate_total_value():
    """
    Ricalcola il valore totale del portafoglio (liquidita' + posizioni aperte).

    Sanity check anti-bug: se il valore raw (cash + sum(qty*current_price))
    devia di oltre il 30% dalla mediana degli ultimi 50 snapshot reali del
    portafoglio_snapshots, c'e' quasi certamente un current_price gonfiato
    in una position. In quel caso usiamo la mediana dello storico come
    valore di display (allineato al grafico Equity Curve) e LOGGHIAMO
    rumorosamente per investigare.

    Questo evita il caso "portfolio segna $169k mentre il chart
    correttamente mostra $103k" senza richiedere intervento manuale.
    """
    portfolio = get_portfolio()
    if portfolio is None:
        return 0.0

    cash = portfolio["cash_balance"]
    positions = get_positions()

    # Somma il valore di mercato di ogni posizione (raw)
    positions_value = sum(
        pos["current_price"] * pos["quantity"]
        for pos in positions
        if pos["current_price"] > 0
    )
    raw_total = cash + positions_value

    # Sanity check vs mediana storica degli snapshot recenti
    safe_total = raw_total
    try:
        from database import get_portfolio_history
        recent = get_portfolio_history(days=2) or []
        # Tieni solo gli ultimi 50 snapshot validi (>0)
        recent_vals = sorted(
            float(r["total_value"]) for r in recent
            if r.get("total_value") and float(r["total_value"]) > 0
        )[-50:]
        if len(recent_vals) >= 5:
            mid = len(recent_vals) // 2
            median_val = (
                recent_vals[mid] if len(recent_vals) % 2 == 1
                else (recent_vals[mid - 1] + recent_vals[mid]) / 2.0
            )
            if median_val > 0:
                drift = abs(raw_total - median_val) / median_val
                if drift > 0.30:
                    logger.warning(
                        "calculate_total_value: raw=%.2f devia %.0f%% dalla mediana "
                        "recente %.2f (probabile current_price gonfiato in una posizione). "
                        "Uso mediana storica come display value. Esegui audit per dettagli.",
                        raw_total, drift * 100, median_val,
                    )
                    safe_total = median_val
    except Exception as ex:
        logger.debug("calculate_total_value sanity check skipped: %s", ex)

    # Persist sempre il safe_total in DB
    update_portfolio(cash, safe_total)
    return safe_total


def get_portfolio_state():
    """
    Restituisce lo stato completo del portafoglio:
    liquidita', posizioni, valore totale e P&L complessivo.
    Il P&L e' calcolato rispetto al bilancio iniziale (total_value - initial_balance).
    """
    p = get_portfolio()
    if p is None:
        return {
            "cash": 0.0,
            "positions": [],
            "total_value": 0.0,
            "initial_balance": 100000.0,
            "pnl": 0.0,
            "pnl_pct": 0.0,
            "open_positions_count": 0,
        }

    positions = get_positions()
    total_value = calculate_total_value()

    # Legge il bilancio iniziale dalle impostazioni (fallback: env o 100000)
    try:
        ib = get_setting("initial_balance", None)
        if ib is not None:
            initial_balance = float(ib)
        else:
            initial_balance = float(
                __import__("os").environ.get("INITIAL_PORTFOLIO_BALANCE", 100000)
            )
    except (ValueError, TypeError):
        initial_balance = 100000.0

    # P&L calcolato rispetto al capitale iniziale
    pnl = total_value - initial_balance
    pnl_pct = (pnl / initial_balance * 100) if initial_balance > 0 else 0.0

    return {
        "cash": p["cash_balance"],
        "positions": positions,
        "total_value": total_value,
        "initial_balance": initial_balance,
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 2),
        "open_positions_count": len(positions),
    }


def can_buy(ticker, quantity, price):
    """
    Verifica se un acquisto e' possibile (solo controllo liquidita').
    L'AI decide autonomamente dimensione e allocazione delle posizioni.
    Restituisce (consentito: bool, motivo: str).
    """
    p = get_portfolio()
    if p is None:
        return False, "Portafoglio non inizializzato"

    # Sanity check: quantity > 0 e price > 0.
    # FIX CRITICO: float (non int) per supportare crypto frazionarie.
    # Bug precedente: int(quantity) troncava 0.5 BTC a 0 e rifiutava il trade,
    # OPPURE costringeva l'AI a comprare interi BTC ($100k+ posizione).
    try:
        q_float = float(quantity)
    except (TypeError, ValueError):
        return False, f"Quantity non numerica: {quantity}"
    if q_float <= 0:
        return False, f"Quantity deve essere > 0 (ricevuto {quantity})"
    try:
        p_float = float(price)
    except (TypeError, ValueError):
        return False, f"Price non numerico: {price}"
    if p_float <= 0:
        return False, f"Price deve essere > 0 (ricevuto {price})"

    cost = q_float * p_float

    if cost > p["cash_balance"]:
        return False, (
            f"Liquidita' insufficiente: necessari {cost:.2f}, "
            f"disponibili {p['cash_balance']:.2f}"
        )

    return True, "Acquisto consentito"


def execute_buy(ticker, quantity, price, geo_reasoning, tech_reasoning, confidence):
    """
    Esegue un ordine di acquisto verificando solo la disponibilita' di liquidita'.
    L'AI decide autonomamente quando e quanto comprare.
    Restituisce un dizionario con l'esito dell'operazione.

    Sanity checks: rifiuta quantity<=0, price<=0 (BUG: prima il bot poteva
    creare ordini fantasma con quantity=0 che inquinavano i log).
    """
    # Controllo liquidita' (include validation quantity/price > 0)
    allowed, reason = can_buy(ticker, quantity, price)
    if not allowed:
        return {"success": False, "reason": reason}

    portfolio = get_portfolio()
    cost = quantity * price

    # Aggiorna la liquidita' — log error reale se il DB rifiuta
    new_cash = portfolio["cash_balance"] - cost
    try:
        update_portfolio(new_cash, portfolio["total_value"])
    except Exception as ex:
        logger.error("execute_buy: update_portfolio FAILED for %s qty=%s price=%s: %s",
                     ticker, quantity, price, ex, exc_info=True)
        return {"success": False, "reason": f"DB write fail (portfolio): {str(ex)[:200]}",
                "db_error": str(ex)[:500]}

    # Aggiorna o crea la posizione — log error reale se il DB rifiuta
    existing = get_position(ticker)
    try:
        if existing is not None:
            # Media ponderata del prezzo di acquisto
            old_total = existing["avg_buy_price"] * existing["quantity"]
            new_quantity = existing["quantity"] + quantity
            new_avg_price = (old_total + cost) / new_quantity
            upsert_position(ticker, new_quantity, new_avg_price, price)
        else:
            upsert_position(ticker, quantity, price, price)
    except Exception as ex:
        logger.error("execute_buy: upsert_position FAILED for %s qty=%s: %s",
                     ticker, quantity, ex, exc_info=True)
        # Rollback cash (best effort)
        try:
            update_portfolio(portfolio["cash_balance"], portfolio["total_value"])
        except Exception:
            pass
        return {"success": False, "reason": f"DB write fail (position): {str(ex)[:200]}",
                "db_error": str(ex)[:500]}

    # Ricalcola il valore totale del portafoglio
    new_total = calculate_total_value()

    # Registra l'operazione nel log delle transazioni — log error reale se DB rifiuta
    decision_text = f"BUY {quantity} {ticker} @ {price:.2f}"
    try:
        trade_id = insert_trade(
            ticker, "BUY", quantity, price,
            geo_reasoning, tech_reasoning,
            decision_text, confidence,
        )
    except Exception as ex:
        logger.error("execute_buy: insert_trade FAILED for %s qty=%s: %s",
                     ticker, quantity, ex, exc_info=True)
        return {"success": False, "reason": f"DB write fail (trade log): {str(ex)[:200]}",
                "db_error": str(ex)[:500]}

    return {
        "success": True,
        "action": "BUY",
        "ticker": ticker,
        "quantity": quantity,
        "price": price,
        "total_cost": round(cost, 2),
        "remaining_cash": round(new_cash, 2),
        "portfolio_total_value": round(new_total, 2),
        "trade_id": trade_id,   # ID del trade per linkare al mirror status
    }


def execute_sell(ticker, quantity, price, geo_reasoning, tech_reasoning, confidence):
    """
    Esegue un ordine di vendita verificando solo che la posizione esista.
    L'AI decide autonomamente quando e quanto vendere.
    Restituisce un dizionario con l'esito dell'operazione.
    """
    # Validazione quantity > 0 (asimmetria con execute_buy che validava già):
    # bug precedente accettava quantity=0 silenziosamente, scriveva un trade
    # fantasma a $0 e lasciava la position invariata.
    try:
        quantity_f = float(quantity or 0)
    except (TypeError, ValueError):
        return {"success": False, "reason": f"quantity non valida: {quantity!r}"}
    if quantity_f <= 0:
        return {
            "success": False,
            "reason": f"quantity deve essere > 0 (ricevuto: {quantity_f})",
        }
    quantity = quantity_f

    # Validazione price > 0 (stessa logica)
    try:
        price_f = float(price or 0)
    except (TypeError, ValueError):
        return {"success": False, "reason": f"price non valido: {price!r}"}
    if price_f <= 0:
        return {"success": False, "reason": f"price deve essere > 0 (ricevuto: {price_f})"}
    price = price_f

    # Verifica che la posizione esista
    existing = get_position(ticker)
    if existing is None:
        return {
            "success": False,
            "reason": f"Nessuna posizione aperta per {ticker}",
        }

    # Verifica che la quantita' da vendere non superi quella posseduta
    if quantity > existing["quantity"]:
        return {
            "success": False,
            "reason": (
                f"Quantita' insufficiente: si possiedono {existing['quantity']} "
                f"azioni di {ticker}, richieste {quantity}"
            ),
        }

    portfolio = get_portfolio()
    proceeds = quantity * price

    # Aggiorna la liquidita' — log error reale se il DB rifiuta
    new_cash = portfolio["cash_balance"] + proceeds
    try:
        update_portfolio(new_cash, portfolio["total_value"])
    except Exception as ex:
        logger.error("execute_sell: update_portfolio FAILED for %s qty=%s price=%s: %s",
                     ticker, quantity, price, ex, exc_info=True)
        return {"success": False, "reason": f"DB write fail (portfolio): {str(ex)[:200]}",
                "db_error": str(ex)[:500]}

    # Aggiorna o rimuovi la posizione — log error reale se DB rifiuta
    remaining = existing["quantity"] - quantity
    try:
        if remaining == 0:
            delete_position(ticker)
        else:
            upsert_position(ticker, remaining, existing["avg_buy_price"], price)
    except Exception as ex:
        logger.error("execute_sell: position update FAILED for %s remaining=%s: %s",
                     ticker, remaining, ex, exc_info=True)
        # Rollback cash
        try:
            update_portfolio(portfolio["cash_balance"], portfolio["total_value"])
        except Exception:
            pass
        return {"success": False, "reason": f"DB write fail (position): {str(ex)[:200]}",
                "db_error": str(ex)[:500]}

    # Ricalcola il valore totale del portafoglio
    new_total = calculate_total_value()

    # Calcolo P&L realizzato su questa vendita
    realized_pnl = (price - existing["avg_buy_price"]) * quantity

    # Registra l'operazione nel log delle transazioni
    decision_text = f"SELL {quantity} {ticker} @ {price:.2f}"
    try:
        trade_id = insert_trade(
            ticker, "SELL", quantity, price,
            geo_reasoning, tech_reasoning,
            decision_text, confidence,
        )
    except Exception as ex:
        logger.error("execute_sell: insert_trade FAILED for %s qty=%s: %s",
                     ticker, quantity, ex, exc_info=True)
        return {"success": False, "reason": f"DB write fail (trade log): {str(ex)[:200]}",
                "db_error": str(ex)[:500]}

    return {
        "success": True,
        "action": "SELL",
        "ticker": ticker,
        "quantity": quantity,
        "price": price,
        "total_proceeds": round(proceeds, 2),
        "realized_pnl": round(realized_pnl, 2),
        "remaining_cash": round(new_cash, 2),
        "portfolio_total_value": round(new_total, 2),
        "trade_id": trade_id,   # ID del trade per linkare al mirror status
    }


# ═══════════════════════════════════════════════════════════════════════
# SL/TP SANITY VALIDATION
# Bug noto: il modello a volte confondeva la quantity (es. 150) con il
# prezzo del SL, impostando SL=$145 su GLD a $420. Risultato: chiusura
# istantanea della posizione a prezzo assurdo. Queste funzioni rifiutano
# tutti i SL/TP che si discostano > 50% dal prezzo corrente.
# ═══════════════════════════════════════════════════════════════════════

# Soglie di sanita' per i livelli SL/TP. Ratio = level / current_price.
# Equity: range ±50% (un livello "ragionevole" è entro 0.5x – 1.5x del current).
# Crypto: range ±70% (più volatile: SL larghi su asset come SOL/AVAX a -55% sono
#   tecnicamente legittimi su selloff). Bug precedente: range stretto rifiutava
#   tutti i SL su crypto durante drawdown reali.
_SLTP_MIN_RATIO_EQUITY = 0.50
_SLTP_MAX_RATIO_EQUITY = 1.50
_SLTP_MIN_RATIO_CRYPTO = 0.30
_SLTP_MAX_RATIO_CRYPTO = 2.00


def _is_crypto_ticker_for_sltp(ticker: str) -> bool:
    """True se il ticker è crypto."""
    if not ticker:
        return False
    t = ticker.upper().strip()
    return t.startswith("X:") or (t.endswith("-USD") and len(t) > 4)


def _refresh_current_price(ticker: str, fallback: float) -> float:
    """
    Tenta di leggere il prezzo corrente dalla cache price_polling
    (aggiornata ogni 60s). Fallback al prezzo passato se cache vuota.
    Bug precedente: validate_sltp usava current_price dalla position
    che poteva essere None o stale → rifiutava tutti i SL.
    """
    try:
        from price_polling import get_cached_prices_bulk
        cached = get_cached_prices_bulk([ticker], max_age_seconds=600)
        if ticker in cached and cached[ticker].get("price"):
            return float(cached[ticker]["price"])
    except Exception:
        pass
    return float(fallback) if fallback else 0.0


def _validate_sltp_level(level_price: float, current_price: float,
                          kind: str, is_long: bool = True,
                          ticker: str = ""
                          ) -> tuple[bool, str]:
    """
    Verifica che un livello SL/TP sia plausibile rispetto al prezzo corrente.

    Args:
      level_price:  prezzo proposto del SL/TP (deve essere > 0)
      current_price: prezzo corrente dell'asset
      kind:  'SL' o 'TP' per messaggi di errore
      is_long: True se LONG (BUY), False se SHORT (SELL)
      ticker: opzionale, usato per scegliere il range crypto vs equity

    Ritorna (ok, reason).
    """
    if not isinstance(level_price, (int, float)) or level_price <= 0:
        return False, f"{kind} non valido (deve essere > 0)"
    if not isinstance(current_price, (int, float)) or current_price <= 0:
        return False, f"current_price non valido per validare {kind} (forse cache prezzi vuota?)"

    is_crypto = _is_crypto_ticker_for_sltp(ticker)
    min_ratio = _SLTP_MIN_RATIO_CRYPTO if is_crypto else _SLTP_MIN_RATIO_EQUITY
    max_ratio = _SLTP_MAX_RATIO_CRYPTO if is_crypto else _SLTP_MAX_RATIO_EQUITY

    ratio = float(level_price) / float(current_price)
    if ratio < min_ratio or ratio > max_ratio:
        asset_class = "crypto" if is_crypto else "equity"
        return False, (
            f"{kind} ({level_price}) si discosta {abs(1 - ratio) * 100:.0f}% "
            f"dal prezzo corrente ({current_price}) — fuori dal range {asset_class} "
            f"({min_ratio:.2f}x – {max_ratio:.2f}x = "
            f"{current_price * min_ratio:.2f} – {current_price * max_ratio:.2f}). "
            f"Probabile errore (hai confuso quantità con prezzo?)."
        )

    # Verifica direzione corretta vs current_price
    if kind == "SL":
        if is_long and level_price >= current_price:
            return False, (
                f"SL ({level_price}) DEVE essere SOTTO current ({current_price}) "
                f"per un LONG."
            )
        if (not is_long) and level_price <= current_price:
            return False, (
                f"SL ({level_price}) DEVE essere SOPRA current ({current_price}) "
                f"per uno SHORT."
            )
    elif kind == "TP":
        if is_long and level_price <= current_price:
            return False, (
                f"TP ({level_price}) DEVE essere SOPRA current ({current_price}) "
                f"per un LONG."
            )
        if (not is_long) and level_price >= current_price:
            return False, (
                f"TP ({level_price}) DEVE essere SOTTO current ({current_price}) "
                f"per uno SHORT."
            )

    return True, ""


def set_stop_loss(ticker: str, stop_price: float, run_id: str = "") -> dict:
    """
    Imposta o rimuove (stop_price=0) lo stop-loss automatico su una posizione.
    Sanity check: rifiuta SL che si discostano > 50% dal prezzo corrente
    (preveniva il bug "SL=145 su GLD@420" che chiudeva la posizione subito).
    """
    pos = get_position(ticker)
    if pos is None:
        return {"success": False, "reason": f"Nessuna posizione su {ticker}"}
    if stop_price is None or stop_price < 0:
        return {"success": False, "reason": "stop_price deve essere >= 0"}

    if float(stop_price) > 0:
        # Refresh current_price dalla cache price_polling (aggiornata ogni 60s)
        # invece di affidarsi al campo nella position record che può essere
        # stale o None per posizioni appena aperte.
        pos_cur = pos.get("current_price") or pos.get("avg_buy_price") or 0
        cur = _refresh_current_price(ticker, pos_cur)
        # Per il sistema attuale tutte le posizioni sono LONG (BUY); il
        # validatore copre comunque il caso SHORT futuro.
        ok, reason = _validate_sltp_level(
            float(stop_price), float(cur), kind="SL", is_long=True,
            ticker=ticker,
        )
        if not ok:
            try:
                import database as _db
                _db.insert_agent_log(run_id or "", "SL_REJECTED", json.dumps({
                    "ticker": ticker,
                    "stop_price_proposed": stop_price,
                    "current_price": cur,
                    "reason": reason,
                }, default=str))
            except Exception:
                pass
            return {"success": False, "reason": reason}

    ok = update_position_auto_exit(ticker, stop_loss_price=stop_price, set_by=run_id)
    if not ok:
        return {"success": False, "reason": "Aggiornamento DB fallito"}
    return {
        "success": True, "ticker": ticker,
        "stop_loss_price": stop_price,
        "current_price": pos.get("current_price"),
        "avg_buy_price": pos.get("avg_buy_price"),
        "note": "Lo stop-loss verra' eseguito automaticamente al raggiungimento.",
    }


def set_take_profit(ticker: str, target_price: float, run_id: str = "") -> dict:
    """
    Imposta o rimuove (target_price=0) il take-profit automatico.
    Sanity check: stesso pattern di set_stop_loss (rifiuta level > ±50%).
    """
    pos = get_position(ticker)
    if pos is None:
        return {"success": False, "reason": f"Nessuna posizione su {ticker}"}
    if target_price is None or target_price < 0:
        return {"success": False, "reason": "target_price deve essere >= 0"}

    if float(target_price) > 0:
        pos_cur = pos.get("current_price") or pos.get("avg_buy_price") or 0
        cur = _refresh_current_price(ticker, pos_cur)
        ok, reason = _validate_sltp_level(
            float(target_price), float(cur), kind="TP", is_long=True,
            ticker=ticker,
        )
        if not ok:
            try:
                import database as _db
                _db.insert_agent_log(run_id or "", "TP_REJECTED", json.dumps({
                    "ticker": ticker,
                    "target_price_proposed": target_price,
                    "current_price": cur,
                    "reason": reason,
                }, default=str))
            except Exception:
                pass
            return {"success": False, "reason": reason}

    ok = update_position_auto_exit(ticker, take_profit_price=target_price, set_by=run_id)
    if not ok:
        return {"success": False, "reason": "Aggiornamento DB fallito"}
    return {
        "success": True, "ticker": ticker,
        "take_profit_price": target_price,
        "current_price": pos.get("current_price"),
        "avg_buy_price": pos.get("avg_buy_price"),
        "note": "Il take-profit verra' eseguito automaticamente al raggiungimento.",
    }


def check_and_execute_auto_exits(prices: dict | None = None) -> list:
    """
    Per ogni posizione con SL/TP impostato, controlla se il prezzo corrente
    ha attivato l'exit automatico e in tal caso esegue execute_sell.

    Args:
        prices: dict {ticker: current_price} aggiornato da price_polling.
                Se None, usa il current_price gia' salvato sulla posizione.

    Ritorna lista di dict con i trade eseguiti automaticamente.
    """
    executed = []
    try:
        positions = get_positions_with_auto_exits()
    except Exception:
        positions = []

    for p in positions:
        ticker = p.get("ticker")
        if not ticker:
            continue
        # Prezzo corrente: prima dal dict prices (fresh), poi dal DB
        cur_price = None
        if prices and ticker in prices:
            try:
                cur_price = float(prices[ticker])
            except Exception:
                cur_price = None
        if cur_price is None or cur_price <= 0:
            cur_price = p.get("current_price") or 0
        if not cur_price or cur_price <= 0:
            continue

        sl = float(p.get("stop_loss_price") or 0)
        tp = float(p.get("take_profit_price") or 0)
        # FIX: float (non int) per supportare crypto frazionarie (BTC 0.5 unità).
        qty = float(p.get("quantity") or 0)
        avg = float(p.get("avg_buy_price") or 0)
        if qty <= 0:
            continue

        trigger = None
        if tp > 0 and cur_price >= tp:
            trigger = "take_profit"
        elif sl > 0 and cur_price <= sl:
            trigger = "stop_loss"

        if not trigger:
            continue

        # Esegue la vendita auto. Reasoning include il trigger per audit.
        reason = (
            f"AUTO {trigger.upper()}: prezzo {cur_price:.4f} ha "
            f"{'superato TP' if trigger == 'take_profit' else 'rotto SL'} "
            f"a {(tp if trigger == 'take_profit' else sl):.4f} "
            f"(carico {avg:.4f}, qty {qty})."
        )
        try:
            result = execute_sell(
                ticker, qty, cur_price,
                geo_reasoning=f"auto_{trigger}",
                tech_reasoning=reason,
                confidence=100,
            )
            executed.append({
                "ticker": ticker,
                "trigger": trigger,
                "trigger_price": tp if trigger == "take_profit" else sl,
                "executed_price": cur_price,
                "quantity": qty,
                "result": result,
            })
            # Log nel DB cosi' appare nelle dashboard
            try:
                from database import insert_agent_log
                import json as _json
                insert_agent_log(
                    "auto_exit", "DECISION_AUTO_EXIT",
                    _json.dumps({
                        "event": f"auto_{trigger}",
                        "ticker": ticker, "qty": qty,
                        "trigger_price": tp if trigger == "take_profit" else sl,
                        "executed_price": cur_price,
                        "avg_buy_price": avg,
                    }, default=str),
                )
            except Exception:
                pass
        except Exception as e:
            executed.append({
                "ticker": ticker, "trigger": trigger, "error": str(e),
            })

    return executed


def update_prices(prices: dict):
    """
    Aggiorna i prezzi correnti di tutte le posizioni e ricalcola il P&L.
    Parametro prices: dizionario {ticker: prezzo_corrente}.
    """
    positions = get_positions()
    updated = []

    for pos in positions:
        ticker = pos["ticker"]
        if ticker in prices:
            new_price = prices[ticker]
            # Skip se il prezzo nuovo non e' valido (NaN, 0, negativo)
            try:
                new_price_f = float(new_price)
                if new_price_f <= 0 or new_price_f != new_price_f:  # NaN check
                    logger.warning("update_prices skip %s: prezzo invalido %s",
                                   ticker, new_price)
                    continue
            except (TypeError, ValueError):
                logger.warning("update_prices skip %s: prezzo non numerico %s",
                               ticker, new_price)
                continue
            update_position_price(ticker, new_price_f)
            # Calcola il P&L aggiornato (guard contro avg_buy_price=0)
            avg = float(pos.get("avg_buy_price") or 0)
            qty = float(pos.get("quantity") or 0)
            pnl = (new_price_f - avg) * qty if avg > 0 else 0
            pnl_pct = ((new_price_f - avg) / avg) * 100 if avg > 0 else 0
            updated.append({
                "ticker": ticker,
                "old_price": pos["current_price"],
                "new_price": new_price,
                "unrealized_pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2),
            })

    # Ricalcola il valore totale dopo l'aggiornamento dei prezzi
    total_value = calculate_total_value()

    # Controlla auto-exits SL/TP e chiude le posizioni che hanno raggiunto i livelli
    auto_exits = []
    try:
        auto_exits = check_and_execute_auto_exits(prices)
        if auto_exits:
            # Ricalcola dopo le chiusure automatiche
            total_value = calculate_total_value()
    except Exception:
        pass

    return {
        "updated_positions": updated,
        "portfolio_total_value": round(total_value, 2),
        "auto_exits_executed": auto_exits,
    }


