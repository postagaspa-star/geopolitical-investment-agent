"""
Modulo di gestione del portafoglio virtuale con regole di risk management
applicate direttamente nel codice (NON nel prompt dell'agente).

Regole di risk management:
- Nessuna posizione singola puo' superare il 10% del valore totale del portafoglio
- Stop-loss automatico al -15% per posizione (eseguito autonomamente ad ogni run)
- Massimo 8 posizioni aperte contemporaneamente
- Non eseguire operazioni se confidence_score < 40
"""

from database import (
    get_portfolio, update_portfolio,
    get_positions, get_position,
    upsert_position, delete_position,
    update_position_price, count_positions,
    insert_trade, get_setting,
)

# --- Valori di default per il risk management ---
_DEFAULT_MAX_POSITION_PCT = 0.10
_DEFAULT_STOP_LOSS = -0.15
_DEFAULT_MAX_POSITIONS = 8
_DEFAULT_MIN_CONFIDENCE = 40


def _get_risk_params():
    """Legge i parametri di risk management dal database, con fallback ai default."""
    try:
        max_pct = float(get_setting("max_position_pct", str(_DEFAULT_MAX_POSITION_PCT)))
        stop_loss = float(get_setting("stop_loss_threshold", str(_DEFAULT_STOP_LOSS)))
        max_pos = int(float(get_setting("max_open_positions", str(_DEFAULT_MAX_POSITIONS))))
        min_conf = float(get_setting("min_confidence", str(_DEFAULT_MIN_CONFIDENCE)))
    except Exception:
        max_pct = _DEFAULT_MAX_POSITION_PCT
        stop_loss = _DEFAULT_STOP_LOSS
        max_pos = _DEFAULT_MAX_POSITIONS
        min_conf = _DEFAULT_MIN_CONFIDENCE
    return max_pct, stop_loss, max_pos, min_conf


def calculate_total_value():
    """Ricalcola il valore totale del portafoglio (liquidita' + posizioni aperte)."""
    portfolio = get_portfolio()
    if portfolio is None:
        return 0.0

    cash = portfolio["cash_balance"]
    positions = get_positions()

    # Somma il valore di mercato di ogni posizione
    positions_value = sum(
        pos["current_price"] * pos["quantity"]
        for pos in positions
        if pos["current_price"] > 0
    )

    total = cash + positions_value
    # Aggiorna il valore totale nel database
    update_portfolio(cash, total)
    return total


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
    Verifica se un acquisto e' consentito dalle regole di risk management.
    Restituisce (consentito: bool, motivo: str).
    """
    MAX_POSITION_PCT, _, MAX_OPEN_POSITIONS, _ = _get_risk_params()

    p = get_portfolio()
    if p is None:
        return False, "Portafoglio non inizializzato"

    total_value = calculate_total_value()
    cost = quantity * price

    if cost > p["cash_balance"]:
        return False, (
            f"Liquidita' insufficiente: necessari {cost:.2f}, "
            f"disponibili {p['cash_balance']:.2f}"
        )

    existing = get_position(ticker)
    if existing is None and count_positions() >= MAX_OPEN_POSITIONS:
        return False, (
            f"Numero massimo di posizioni aperte raggiunto ({MAX_OPEN_POSITIONS})"
        )

    existing_value = 0.0
    if existing is not None:
        existing_value = existing["current_price"] * existing["quantity"]
    new_position_value = existing_value + cost

    max_allowed = total_value * MAX_POSITION_PCT
    if new_position_value > max_allowed:
        return False, (
            f"Limite di concentrazione superato: la posizione varrebbe "
            f"{new_position_value:.2f} (max consentito: {max_allowed:.2f}, "
            f"cioe' {MAX_POSITION_PCT*100:.0f}% del portafoglio)"
        )

    return True, "Acquisto consentito"


def execute_buy(ticker, quantity, price, geo_reasoning, tech_reasoning, confidence):
    """
    Esegue un ordine di acquisto applicando tutti i controlli di risk management.
    Restituisce un dizionario con l'esito dell'operazione.
    """
    # Controllo punteggio di confidenza minimo (letto dal DB)
    _, _, _, MIN_CONFIDENCE = _get_risk_params()
    if confidence < MIN_CONFIDENCE:
        return {
            "success": False,
            "reason": (
                f"Confidenza troppo bassa: {confidence} "
                f"(minimo richiesto: {MIN_CONFIDENCE})"
            ),
        }

    # Controlli di risk management
    allowed, reason = can_buy(ticker, quantity, price)
    if not allowed:
        return {"success": False, "reason": reason}

    portfolio = get_portfolio()
    cost = quantity * price

    # Aggiorna la liquidita'
    new_cash = portfolio["cash_balance"] - cost
    update_portfolio(new_cash, portfolio["total_value"])

    # Aggiorna o crea la posizione
    existing = get_position(ticker)
    if existing is not None:
        # Media ponderata del prezzo di acquisto
        old_total = existing["avg_buy_price"] * existing["quantity"]
        new_quantity = existing["quantity"] + quantity
        new_avg_price = (old_total + cost) / new_quantity
        upsert_position(ticker, new_quantity, new_avg_price, price)
    else:
        upsert_position(ticker, quantity, price, price)

    # Ricalcola il valore totale del portafoglio
    new_total = calculate_total_value()

    # Registra l'operazione nel log delle transazioni
    decision_text = f"BUY {quantity} {ticker} @ {price:.2f}"
    insert_trade(
        ticker, "BUY", quantity, price,
        geo_reasoning, tech_reasoning,
        decision_text, confidence,
    )

    return {
        "success": True,
        "action": "BUY",
        "ticker": ticker,
        "quantity": quantity,
        "price": price,
        "total_cost": round(cost, 2),
        "remaining_cash": round(new_cash, 2),
        "portfolio_total_value": round(new_total, 2),
    }


def execute_sell(ticker, quantity, price, geo_reasoning, tech_reasoning, confidence):
    """
    Esegue un ordine di vendita con i relativi controlli.
    Restituisce un dizionario con l'esito dell'operazione.
    """
    # Controllo punteggio di confidenza minimo (letto dal DB)
    _, _, _, MIN_CONFIDENCE = _get_risk_params()
    if confidence < MIN_CONFIDENCE:
        return {
            "success": False,
            "reason": (
                f"Confidenza troppo bassa: {confidence} "
                f"(minimo richiesto: {MIN_CONFIDENCE})"
            ),
        }

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

    # Aggiorna la liquidita'
    new_cash = portfolio["cash_balance"] + proceeds
    update_portfolio(new_cash, portfolio["total_value"])

    # Aggiorna o rimuovi la posizione
    remaining = existing["quantity"] - quantity
    if remaining == 0:
        # Posizione completamente chiusa
        delete_position(ticker)
    else:
        # Posizione parzialmente ridotta (il prezzo medio resta invariato)
        upsert_position(ticker, remaining, existing["avg_buy_price"], price)

    # Ricalcola il valore totale del portafoglio
    new_total = calculate_total_value()

    # Calcolo P&L realizzato su questa vendita
    realized_pnl = (price - existing["avg_buy_price"]) * quantity

    # Registra l'operazione nel log delle transazioni
    decision_text = f"SELL {quantity} {ticker} @ {price:.2f}"
    insert_trade(
        ticker, "SELL", quantity, price,
        geo_reasoning, tech_reasoning,
        decision_text, confidence,
    )

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
    }


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
            update_position_price(ticker, new_price)
            # Calcola il P&L aggiornato
            pnl = (new_price - pos["avg_buy_price"]) * pos["quantity"]
            pnl_pct = ((new_price - pos["avg_buy_price"]) / pos["avg_buy_price"]) * 100
            updated.append({
                "ticker": ticker,
                "old_price": pos["current_price"],
                "new_price": new_price,
                "unrealized_pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2),
            })

    # Ricalcola il valore totale dopo l'aggiornamento dei prezzi
    total_value = calculate_total_value()

    return {
        "updated_positions": updated,
        "portfolio_total_value": round(total_value, 2),
    }


def check_stop_losses(current_prices: dict):
    """
    Controlla tutte le posizioni aperte e esegue automaticamente lo stop-loss
    per quelle che hanno perso oltre il 15% rispetto al prezzo medio di acquisto.
    Viene eseguito autonomamente ad ogni run del sistema.
    """
    # Prima aggiorna i prezzi con quelli forniti
    if current_prices:
        update_prices(current_prices)

    positions = get_positions()
    triggered = []

    for pos in positions:
        ticker = pos["ticker"]
        current_price = current_prices.get(ticker, pos["current_price"])

        # Salta posizioni senza prezzo corrente valido
        if current_price <= 0 or pos["avg_buy_price"] <= 0:
            continue

        # Calcola la variazione percentuale rispetto al prezzo medio di acquisto
        change_pct = (current_price - pos["avg_buy_price"]) / pos["avg_buy_price"]

        _, STOP_LOSS_THRESHOLD, _, _ = _get_risk_params()
        if change_pct <= STOP_LOSS_THRESHOLD:
            # Stop-loss attivato: vendita automatica dell'intera posizione
            result = execute_sell(
                ticker=ticker,
                quantity=pos["quantity"],
                price=current_price,
                geo_reasoning="Stop-loss automatico attivato dal sistema",
                tech_reasoning=(
                    f"Perdita del {change_pct*100:.1f}% "
                    f"(soglia: {STOP_LOSS_THRESHOLD*100:.0f}%)"
                ),
                confidence=100,  # Esecuzione automatica, confidenza massima
            )
            triggered.append({
                "ticker": ticker,
                "avg_buy_price": pos["avg_buy_price"],
                "trigger_price": current_price,
                "loss_pct": round(change_pct * 100, 2),
                "quantity_sold": pos["quantity"],
                "sell_result": result,
            })

    return {
        "stop_losses_checked": len(positions),
        "stop_losses_triggered": len(triggered),
        "details": triggered,
    }
