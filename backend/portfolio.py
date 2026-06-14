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

# Modulo CANONICO dei conti (puro, nessuna dipendenza dall'app → no cicli).
import accounting

from database import (
    get_portfolio, update_portfolio, update_portfolio_total_value,
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


# ═══════════════════════════════════════════════════════════════════════
# COMMISSIONI / SLIPPAGE per il bot Live
# ═══════════════════════════════════════════════════════════════════════
#
# Il bot Live ora simula un costo di transazione per ogni BUY/SELL: senza
# questo i backtest e il P&L apparente erano sempre leggermente "troppo
# ottimistici" rispetto a quello che otterresti su un broker retail vero.
#
# Formula:
#   fee = gross_value * (commission_bps / 10_000)
#   - BUY:  total_cost = (quantity*price) + fee   → dedotti dal cash
#   - SELL: net_proceeds = (quantity*price) - fee → accreditati al cash
#
# La rate e' configurabile dalla tabella settings ("commission_bps") con
# default 10 bps (= 0.10%, livello tipico broker retail US azionario).
# Settando 0 si simula esecuzione "perfect fill" per A/B testing.
#
# Tracciamento cumulativo: ogni trade aggiorna "total_commissions_paid"
# nelle settings. Esposto via /api/portfolio per la UI.

DEFAULT_COMMISSION_BPS = 10.0
MAX_COMMISSION_BPS = 100.0   # sanity cap (1% per trade = piu' di qualunque broker)

# Step 4: surcharge di slippage per i nomi SATELLITE (meno liquidi) in modalità
# EXPANDED_UNIVERSE. Si AGGIUNGE alla commissione, solo per il tier satellite e
# solo col flag ON. Col flag OFF o sul core/crypto: 0 → esecuzione invariata.
SATELLITE_SLIPPAGE_BPS = 15.0


def _get_commission_bps() -> float:
    """
    Legge la rate di commissione configurata. Default 10 bps (0.10%).
    Clamp [0, MAX] per safety: una rate negativa o assurda romperebbe la math.
    """
    try:
        raw = get_setting("commission_bps", str(DEFAULT_COMMISSION_BPS))
        bps = float(raw) if raw else DEFAULT_COMMISSION_BPS
    except (TypeError, ValueError):
        bps = DEFAULT_COMMISSION_BPS
    if bps < 0:
        return 0.0
    if bps > MAX_COMMISSION_BPS:
        return MAX_COMMISSION_BPS
    return bps


def _commission_amount(gross_value: float, bps: float | None = None) -> float:
    """
    Calcola la commissione in $ da un valore lordo (quantity * price).
    bps None → usa la rate configurata. Delega ad accounting (canonico).
    """
    if bps is None:
        bps = _get_commission_bps()
    return accounting.commission(gross_value, bps)


def _satellite_slippage_fee(gross_value: float, ticker: str) -> float:
    """
    Step 4: costo di slippage extra per i nomi SATELLITE in modalità
    EXPANDED_UNIVERSE. Ritorna 0 col flag OFF o per core/crypto: in quei casi
    l'esecuzione resta byte-identica a prima.
    """
    try:
        import universe as _u
        if not _u.expanded_universe_enabled():
            return 0.0
        if _u.classify_ticker(ticker).get("tier") != "satellite":
            return 0.0
        return accounting.commission(gross_value, SATELLITE_SLIPPAGE_BPS)
    except Exception:
        return 0.0


def _accrue_commission(amount: float) -> None:
    """
    Aggiunge l'importo al totale cumulativo "total_commissions_paid" nelle
    settings. Best-effort: errori di DB non bloccano il trade.
    """
    if not amount or amount <= 0:
        return
    try:
        from database import set_setting
        prev = float(get_setting("total_commissions_paid", "0") or 0)
        set_setting("total_commissions_paid", str(round(prev + amount, 2)))
    except Exception as ex:
        logger.debug("commissioni: accrue fallito (non critico): %s", ex)


def _alert_trade_not_logged(action: str, ticker, quantity, price, ex) -> None:
    """Allarme CRITICAL: il trade e' avvenuto (cash + posizione gia' scritti)
    ma insert_trade e' fallito. NON si deve ritornare success=False (il
    chiamante ritenterebbe e RADDOPPIEREBBE il trade): si logga qui per
    l'audit e si prosegue come successo con trade_id=None.
    """
    logger.critical(
        "execute_%s: insert_trade FALLITO ma cash+posizione GIA' committati "
        "(%s qty=%s @ %s): %s. Trade NON loggato — gap di audit.",
        action, ticker, quantity, price, ex, exc_info=True,
    )
    try:
        from database import insert_agent_log as _ial
        import json as _json
        _ial("portfolio", "TRADE_LOG_FAILED", _json.dumps({
            "event": "trade_executed_but_not_logged",
            "action": action, "ticker": ticker,
            "quantity": quantity, "price": price,
            "error": str(ex)[:300],
        }, default=str))
    except Exception:
        pass


def _alert_rollback_failed(action: str, ticker, ex) -> None:
    """Allarme CRITICAL: il rollback del cash dopo un upsert_position fallito
    e' a sua volta fallito → cash potenzialmente detratto senza posizione
    (incoerenza contabile). Prima era 'except: pass' muto; ora si logga e si
    scrive un agent_log CASH_ROLLBACK_FAILED visibile nel cruscotto/health.
    """
    logger.critical(
        "execute_%s: ROLLBACK del cash FALLITO per %s: %s. Possibile cash "
        "detratto senza posizione — incoerenza contabile da verificare.",
        action, ticker, ex, exc_info=True,
    )
    try:
        from database import insert_agent_log as _ial
        import json as _json
        _ial("portfolio", "CASH_ROLLBACK_FAILED", _json.dumps({
            "event": "cash_rollback_failed",
            "action": action, "ticker": ticker, "error": str(ex)[:300],
        }, default=str))
    except Exception:
        pass


def calculate_total_value():
    """
    Ricalcola il valore totale del portafoglio (liquidita' + posizioni aperte).

    Difese a tre livelli (anti +67% spike / -25% drawdown bug):

    1. Per-position cap: ogni posizione contribuisce con qty * SAFE_PRICE
       dove SAFE_PRICE = current_price se entro [0.10x, 10x] dell'avg_buy_price
       (cost basis), altrimenti avg_buy_price stesso. Cattura prezzi corrotti.

    2. Median sanity (legacy): se raw_total devia >25% dalla mediana degli
       ultimi 50 snapshot reali, ritorna la mediana invece del raw. Soglia
       abbassata da 30% → 25% per essere piu' rigorosa.

    3. Trimmed mean: la "mediana" usa IQR-based filter (Q1-Q3) per non
       essere contaminata da snapshot estremi pre-esistenti.

    Questo evita il caso "portfolio segna $169k mentre il chart
    correttamente mostra $103k" senza richiedere intervento manuale.
    """
    portfolio = get_portfolio()
    if portfolio is None:
        return 0.0

    cash = portfolio["cash_balance"]
    positions = get_positions()

    # ── 1. Per-position safe valuation ─────────────────────────────────
    # Per ogni posizione, cap il prezzo di valutazione tra [0.1x, 10x]
    # dell'avg_buy_price. Cattura corruzioni grossolane di current_price
    # (es. decimal-shifted, ticker mismatch, NaN sneaking through).
    positions_value = 0.0
    for pos in positions:
        try:
            cp = float(pos.get("current_price") or 0)
            avg = float(pos.get("avg_buy_price") or 0)
            qty = float(pos.get("quantity") or 0)
            if qty <= 0:
                continue
            ticker = pos.get("ticker", "")
            is_crypto = (ticker.upper().startswith("X:") or
                         (ticker.upper().endswith("-USD") and len(ticker) > 4))
            outer_min = 0.05 if is_crypto else 0.10
            outer_max = 20.0 if is_crypto else 10.0

            if cp <= 0:
                # No price available: usa cost basis per non perdere il valore
                safe_price = avg
            elif avg > 0:
                ratio = cp / avg
                if ratio < outer_min or ratio > outer_max:
                    logger.warning(
                        "calculate_total_value: %s current_price %.2f fuori range "
                        "[%.1f×, %.1f×] vs avg %.2f. Uso cost basis per safety.",
                        ticker, cp, outer_min, outer_max, avg,
                    )
                    safe_price = avg
                else:
                    safe_price = cp
            else:
                safe_price = cp

            # SHORT vs LONG: una posizione LONG vale +qty*prezzo; una
            # SHORT e' una PASSIVITA' (devi ricomprare le azioni) → vale
            # -qty*prezzo. La cassa contiene gia' i proventi incassati
            # all'apertura dello short, quindi:
            #   NAV = cash + Σ_long(qty*prezzo) - Σ_short(qty*prezzo)
            # Aprire uno short e' NAV-neutro (incassi = passivita'); il
            # NAV sale solo quando il prezzo scende. Questo evita il bug
            # della "cassa fantasma" / leva infinita.
            # Contributo firmato al NAV (canonico): SHORT sottrae, LONG aggiunge.
            positions_value += accounting.signed_position_value(
                qty, safe_price, pos.get("direction"))
        except (TypeError, ValueError) as ex:
            logger.warning("calculate_total_value: error sulla posizione %s: %s",
                           pos.get("ticker"), ex)
            continue

    raw_total = cash + positions_value

    # ── 2. Drift ALERT (no clamp) ──────────────────────────────────────
    # PRIMA: se raw_total deviava >25% dalla mediana storica, sostituivo
    # il valore con la mediana ("safe_total = median_val") e salvavo
    # quello. Era una toppa che NASCONDEVA i bug invece di mostrarli:
    # se cash si gonfiava per errore, il dashboard restava al valore
    # vecchio (mediana) mentre la verita' (cash + positions) era diversa
    # — il chart usava la verita', il dashboard la versione clampata, e
    # i due divergevano in silenzio.
    #
    # ORA: il valore salvato e' SEMPRE raw_total = cash + Σ posizioni
    # direction-aware. La verita' della matematica vince. Se rileva un
    # drift sospetto, si limita a LOGGARE (anche su agent_logs visibile
    # in dashboard) ma NON modifica il valore. Cosi' i bug si vedono.
    try:
        from database import get_portfolio_history, insert_agent_log
        recent = get_portfolio_history(days=2) or []
        recent_vals = sorted(
            float(r["total_value"]) for r in recent
            if r.get("total_value") and float(r["total_value"]) > 0
        )[-50:]
        if len(recent_vals) >= 8:
            n = len(recent_vals)
            q1_idx = n // 4
            q3_idx = (3 * n) // 4
            trimmed = recent_vals[q1_idx:q3_idx + 1]
            if trimmed:
                mid = len(trimmed) // 2
                median_val = (
                    trimmed[mid] if len(trimmed) % 2 == 1
                    else (trimmed[mid - 1] + trimmed[mid]) / 2.0
                )
                if median_val > 0:
                    drift = abs(raw_total - median_val) / median_val
                    if drift > 0.25:
                        logger.error(
                            "NAV_DRIFT_ALERT: raw=%.2f devia %.1f%% dalla mediana "
                            "trimmed %.2f. cash=%.2f, positions_value=%.2f. "
                            "Possibile bug cash o posizione corrotta.",
                            raw_total, drift * 100, median_val, cash, positions_value,
                        )
                        try:
                            import json as _json
                            insert_agent_log("portfolio", "NAV_DRIFT_ALERT", _json.dumps({
                                "event": "nav_drift_alert",
                                "raw_total": round(raw_total, 2),
                                "median_trimmed": round(median_val, 2),
                                "drift_pct": round(drift * 100, 2),
                                "cash": round(cash, 2),
                                "positions_value": round(positions_value, 2),
                                "open_positions": len(positions),
                            }, default=str))
                        except Exception:
                            pass
    except Exception as ex:
        logger.debug("calculate_total_value drift check skipped: %s", ex)

    # Persist SOLO total_value (NON il cash): calculate_total_value e' una
    # valutazione. Riscrivere il cash qui (letto a inizio funzione) poteva
    # sovrascrivere un trade concorrente → perdita silenziosa di denaro.
    update_portfolio_total_value(raw_total)
    return raw_total


def compute_positions_value(positions: list) -> float:
    """Somma DIRECTION-AWARE del valore delle posizioni.

    SHORT è una passività nel NAV → sottrae. LONG aggiunge.
    Formula:  Σ_long(qty*current_price) - Σ_short(qty*current_price)

    Delega al modulo canonico accounting.positions_value (UN solo posto per
    la logica direction-aware → niente piu' copie divergenti).
    """
    return accounting.positions_value(positions, price_key="current_price")


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

    # Commissioni cumulative (best-effort: se le settings non hanno il valore,
    # lo trattiamo come 0 — lo show in UI scompare semplicemente).
    try:
        total_fees = float(get_setting("total_commissions_paid", "0") or 0)
    except (TypeError, ValueError):
        total_fees = 0.0

    return {
        "cash": p["cash_balance"],
        "positions": positions,
        "total_value": total_value,
        "initial_balance": initial_balance,
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 2),
        "open_positions_count": len(positions),
        "commission_bps": _get_commission_bps(),
        "total_commissions_paid": round(total_fees, 2),
    }


def can_buy(ticker, quantity, price):
    """
    Verifica se un acquisto e' possibile (controllo liquidita' + commissioni).
    L'AI decide autonomamente dimensione e allocazione delle posizioni.
    Restituisce (consentito: bool, motivo: str).

    NOTA: il check considera la commissione (default 0.10%): se cash copre
    quantity*price ma non quantity*price*(1+commission_bps/10000), il trade
    viene rifiutato. L'agente puo' ridurre quantity e ritentare.
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

    gross_cost = q_float * p_float
    fee = _commission_amount(gross_cost)
    total_cost = gross_cost + fee

    if total_cost > p["cash_balance"]:
        return False, (
            f"Liquidita' insufficiente: necessari {total_cost:.2f} "
            f"(prezzo {gross_cost:.2f} + commissione {fee:.2f}), "
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

    Commissioni: applicate al cash flow. cash -= (quantity*price + fee).
    Il P&L "true" della posizione include la commissione: la posizione viene
    aperta a "price" (non a "price + fee_per_unit") per semplicita' del cost
    basis tracking; il fee diventa una perdita realizzata immediata.
    """
    # Controllo liquidita' (include validation quantity/price > 0 + commissione)
    allowed, reason = can_buy(ticker, quantity, price)
    if not allowed:
        return {"success": False, "reason": reason}

    # GUARD direzione: una posizione SHORT su questo ticker va chiusa con
    # COVER, non con un BUY long (una sola direzione per ticker — la
    # tabella positions ha UNIQUE su ticker).
    _exist_dir = get_position(ticker)
    if (_exist_dir is not None
            and str(_exist_dir.get("direction") or "LONG").upper() == "SHORT"):
        return {"success": False, "reason": (
            f"Esiste una posizione SHORT su {ticker}: chiudila con COVER "
            "prima di aprire un long. Una sola direzione per ticker.")}

    portfolio = get_portfolio()
    gross_cost = quantity * price
    fee = _commission_amount(gross_cost) + _satellite_slippage_fee(gross_cost, ticker)
    total_cost = gross_cost + fee

    # Aggiorna la liquidita' — log error reale se il DB rifiuta
    new_cash = portfolio["cash_balance"] - total_cost
    try:
        update_portfolio(new_cash, portfolio["total_value"])
    except Exception as ex:
        logger.error("execute_buy: update_portfolio FAILED for %s qty=%s price=%s: %s",
                     ticker, quantity, price, ex, exc_info=True)
        return {"success": False, "reason": f"DB write fail (portfolio): {str(ex)[:200]}",
                "db_error": str(ex)[:500]}

    # Aggiorna o crea la posizione — log error reale se il DB rifiuta.
    # Cost basis: usiamo SOLO gross_cost (quantity * price), senza la fee.
    # Cosi' avg_buy_price riflette il "vero" prezzo di ingresso per la media
    # ponderata; la fee e' contabilizzata come perdita realizzata immediata
    # nel cash, non spalmata sulla posizione (piu' chiaro per la UI).
    existing = get_position(ticker)
    try:
        if existing is not None:
            # Media ponderata del prezzo di acquisto
            old_total = existing["avg_buy_price"] * existing["quantity"]
            new_quantity = existing["quantity"] + quantity
            new_avg_price = (old_total + gross_cost) / new_quantity
            upsert_position(ticker, new_quantity, new_avg_price, price)
        else:
            upsert_position(ticker, quantity, price, price)
    except Exception as ex:
        logger.error("execute_buy: upsert_position FAILED for %s qty=%s: %s",
                     ticker, quantity, ex, exc_info=True)
        # Rollback cash — se fallisce, lo segnaliamo (non piu' 'except: pass' muto)
        try:
            update_portfolio(portfolio["cash_balance"], portfolio["total_value"])
        except Exception as rb_ex:
            _alert_rollback_failed("buy", ticker, rb_ex)
        return {"success": False, "reason": f"DB write fail (position): {str(ex)[:200]}",
                "db_error": str(ex)[:500]}

    # Ricalcola il valore totale del portafoglio
    new_total = calculate_total_value()
    # Accrual cumulativo commissioni
    _accrue_commission(fee)

    # Registra l'operazione nel log delle transazioni — log error reale se DB rifiuta
    # Append diagnostica fee al tech_reasoning per tracciabilita' senza
    # cambiare schema DB.
    fee_note = f" [fee=${fee:.2f} bps={_get_commission_bps():.1f}]"
    decision_text = f"BUY {quantity} {ticker} @ {price:.2f}"
    try:
        trade_id = insert_trade(
            ticker, "BUY", quantity, price,
            geo_reasoning,
            (tech_reasoning or "") + fee_note,
            decision_text, confidence,
        )
    except Exception as ex:
        _alert_trade_not_logged("buy", ticker, quantity, price, ex)
        trade_id = None   # prosegue come successo: il trade E' avvenuto

    return {
        "success": True,
        "action": "BUY",
        "ticker": ticker,
        "quantity": quantity,
        "price": price,
        "gross_cost": round(gross_cost, 2),
        "commission": round(fee, 2),
        "total_cost": round(total_cost, 2),
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

    # GUARD direzione: SELL chiude un LONG. Una posizione SHORT si chiude
    # con COVER (execute_cover), non con SELL.
    if str(existing.get("direction") or "LONG").upper() == "SHORT":
        return {"success": False, "reason": (
            f"{ticker} e' una posizione SHORT: per chiuderla usa COVER "
            "(execute_cover), non SELL.")}

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
    gross_proceeds = quantity * price
    fee = _commission_amount(gross_proceeds) + _satellite_slippage_fee(gross_proceeds, ticker)
    net_proceeds = gross_proceeds - fee

    # Aggiorna la liquidita' — log error reale se il DB rifiuta
    new_cash = portfolio["cash_balance"] + net_proceeds
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
        # Rollback cash — se fallisce, lo segnaliamo (non piu' 'except: pass' muto)
        try:
            update_portfolio(portfolio["cash_balance"], portfolio["total_value"])
        except Exception as rb_ex:
            _alert_rollback_failed("sell", ticker, rb_ex)
        return {"success": False, "reason": f"DB write fail (position): {str(ex)[:200]}",
                "db_error": str(ex)[:500]}

    # Ricalcola il valore totale del portafoglio
    new_total = calculate_total_value()
    _accrue_commission(fee)

    # Calcolo P&L realizzato su questa vendita (al netto della fee)
    realized_pnl = (price - existing["avg_buy_price"]) * quantity - fee

    # Registra l'operazione nel log delle transazioni (fee diagnostica nel
    # tech_reasoning per back-compat schema DB)
    fee_note = f" [fee=${fee:.2f} bps={_get_commission_bps():.1f}]"
    decision_text = f"SELL {quantity} {ticker} @ {price:.2f}"
    try:
        trade_id = insert_trade(
            ticker, "SELL", quantity, price,
            geo_reasoning,
            (tech_reasoning or "") + fee_note,
            decision_text, confidence,
        )
    except Exception as ex:
        _alert_trade_not_logged("sell", ticker, quantity, price, ex)
        trade_id = None   # prosegue come successo: il trade E' avvenuto

    return {
        "success": True,
        "action": "SELL",
        "ticker": ticker,
        "quantity": quantity,
        "price": price,
        "gross_proceeds": round(gross_proceeds, 2),
        "commission": round(fee, 2),
        "total_proceeds": round(net_proceeds, 2),
        "realized_pnl": round(realized_pnl, 2),
        "remaining_cash": round(new_cash, 2),
        "portfolio_total_value": round(new_total, 2),
        "trade_id": trade_id,   # ID del trade per linkare al mirror status
    }


# ═══════════════════════════════════════════════════════════════════════
# SHORT SELLING — apertura/chiusura posizioni allo scoperto
# ═══════════════════════════════════════════════════════════════════════

def execute_short(ticker, quantity, price, geo_reasoning, tech_reasoning,
                  confidence):
    """
    Apre (o incrementa) una posizione SHORT: 'vende' allo scoperto N azioni
    a prezzo P incassandone i proventi. Si guadagna se il prezzo SCENDE.

    Contabilita' paper:
      - cash += proventi netti (gross - commissione).
      - posizione con direction='SHORT', avg_buy_price = prezzo medio
        ponderato di SHORT (il prezzo a cui hai 'venduto').
      - NAV invariato all'apertura (proventi incassati = passivita' assunta),
        vedi calculate_total_value. Il NAV sale solo se il prezzo scende.

    Guard anti-leva: l'esposizione short lorda totale non puo' superare il
    NAV corrente (short max 1x, completamente collateralizzato — evita il
    bug della 'cassa fantasma'/leva infinita).
    """
    try:
        quantity = float(quantity or 0)
        price = float(price or 0)
    except (TypeError, ValueError):
        return {"success": False, "reason": "quantity/price non numerici"}
    if quantity <= 0:
        return {"success": False, "reason": f"quantity deve essere > 0 (ricevuto {quantity})"}
    if price <= 0:
        return {"success": False, "reason": f"price deve essere > 0 (ricevuto {price})"}

    # Guard direzione: non puoi shortare un ticker su cui sei gia' LONG.
    existing = get_position(ticker)
    if (existing is not None
            and str(existing.get("direction") or "LONG").upper() != "SHORT"):
        return {"success": False, "reason": (
            f"Esiste una posizione LONG su {ticker}: chiudila con SELL "
            "prima di aprire uno short.")}

    portfolio = get_portfolio()
    if portfolio is None:
        return {"success": False, "reason": "Portafoglio non inizializzato"}

    gross = quantity * price
    fee = _commission_amount(gross)
    net_proceeds = gross - fee

    # Guard anti-leva: esposizione short lorda totale <= NAV.
    try:
        nav = calculate_total_value()
        existing_short_notional = 0.0
        for p in get_positions():
            if str(p.get("direction") or "LONG").upper() == "SHORT":
                existing_short_notional += (
                    float(p.get("quantity") or 0)
                    * float(p.get("current_price") or p.get("avg_buy_price") or 0))
        if nav > 0 and existing_short_notional + gross > nav:
            return {"success": False, "reason": (
                f"Esposizione short troppo alta: short totali "
                f"${existing_short_notional + gross:,.0f} supererebbero il "
                f"NAV ${nav:,.0f}. Lo short e' limitato a 1x il NAV.")}
    except Exception as ex:
        logger.warning("execute_short: leverage guard skipped: %s", ex)

    new_cash = portfolio["cash_balance"] + net_proceeds
    try:
        update_portfolio(new_cash, portfolio["total_value"])
    except Exception as ex:
        logger.error("execute_short: update_portfolio FAILED %s: %s",
                     ticker, ex, exc_info=True)
        return {"success": False, "reason": f"DB write fail (portfolio): {str(ex)[:200]}"}

    try:
        if existing is not None:
            old_qty = float(existing.get("quantity") or 0)
            old_avg = float(existing.get("avg_buy_price") or 0)
            new_qty = old_qty + quantity
            new_avg = ((old_avg * old_qty + gross) / new_qty
                       if new_qty > 0 else price)
            upsert_position(ticker, new_qty, new_avg, price, direction="SHORT")
        else:
            upsert_position(ticker, quantity, price, price, direction="SHORT")
    except Exception as ex:
        try:
            update_portfolio(portfolio["cash_balance"], portfolio["total_value"])
        except Exception as rb_ex:
            _alert_rollback_failed("short", ticker, rb_ex)
        logger.error("execute_short: upsert_position FAILED %s: %s",
                     ticker, ex, exc_info=True)
        return {"success": False, "reason": f"DB write fail (position): {str(ex)[:200]}"}

    new_total = calculate_total_value()
    _accrue_commission(fee)

    fee_note = f" [fee=${fee:.2f} bps={_get_commission_bps():.1f}]"
    decision_text = f"SHORT {quantity} {ticker} @ {price:.2f}"
    try:
        trade_id = insert_trade(
            ticker, "SELL", quantity, price, geo_reasoning,
            (tech_reasoning or "") + fee_note, decision_text, confidence,
            direction="SHORT",
        )
    except Exception as ex:
        _alert_trade_not_logged("short", ticker, quantity, price, ex)
        trade_id = None   # prosegue come successo: il trade E' avvenuto

    return {
        "success": True,
        "action": "SHORT",
        "direction": "SHORT",
        "ticker": ticker,
        "quantity": quantity,
        "price": price,
        "gross_proceeds": round(gross, 2),
        "commission": round(fee, 2),
        "net_proceeds": round(net_proceeds, 2),
        "remaining_cash": round(new_cash, 2),
        "portfolio_total_value": round(new_total, 2),
        "trade_id": trade_id,
    }


def execute_cover(ticker, quantity, price, geo_reasoning, tech_reasoning,
                  confidence):
    """
    Chiude (o riduce) una posizione SHORT: 'ricompra' N azioni a prezzo P
    per restituirle. Realized P&L = (prezzo_short - prezzo_cover)*qty - fee
    → POSITIVO se il prezzo e' SCESO dopo lo short.
    """
    try:
        quantity = float(quantity or 0)
        price = float(price or 0)
    except (TypeError, ValueError):
        return {"success": False, "reason": "quantity/price non numerici"}
    if quantity <= 0:
        return {"success": False, "reason": f"quantity deve essere > 0 (ricevuto {quantity})"}
    if price <= 0:
        return {"success": False, "reason": f"price deve essere > 0 (ricevuto {price})"}

    existing = get_position(ticker)
    if existing is None:
        return {"success": False, "reason": f"Nessuna posizione aperta per {ticker}"}
    if str(existing.get("direction") or "LONG").upper() != "SHORT":
        return {"success": False, "reason": (
            f"{ticker} e' una posizione LONG: per chiuderla usa SELL, non COVER.")}

    held = float(existing.get("quantity") or 0)
    # Tolleranza 1e-6 (non 1e-9): allineata al round(...,6) di sell_qty negli
    # auto-exit, cosi' un qty=10.000001 da rounding non viene falso-rifiutato.
    if quantity > held + 1e-6:
        return {"success": False, "reason": (
            f"Quantita' insufficiente: short aperto di {held} {ticker}, "
            f"richieste {quantity} da coprire.")}

    portfolio = get_portfolio()
    short_entry = float(existing.get("avg_buy_price") or 0)
    gross = quantity * price
    fee = _commission_amount(gross)
    total_cost = gross + fee

    new_cash = portfolio["cash_balance"] - total_cost
    try:
        update_portfolio(new_cash, portfolio["total_value"])
    except Exception as ex:
        logger.error("execute_cover: update_portfolio FAILED %s: %s",
                     ticker, ex, exc_info=True)
        return {"success": False, "reason": f"DB write fail (portfolio): {str(ex)[:200]}"}

    remaining = held - quantity
    try:
        if remaining <= 1e-9:
            delete_position(ticker)
        else:
            upsert_position(ticker, remaining, short_entry, price, direction="SHORT")
    except Exception as ex:
        try:
            update_portfolio(portfolio["cash_balance"], portfolio["total_value"])
        except Exception as rb_ex:
            _alert_rollback_failed("cover", ticker, rb_ex)
        logger.error("execute_cover: position update FAILED %s: %s",
                     ticker, ex, exc_info=True)
        return {"success": False, "reason": f"DB write fail (position): {str(ex)[:200]}"}

    new_total = calculate_total_value()
    _accrue_commission(fee)

    # P&L realizzato SHORT: guadagni se ricompri piu' BASSO del prezzo short.
    realized_pnl = (short_entry - price) * quantity - fee

    fee_note = f" [fee=${fee:.2f} bps={_get_commission_bps():.1f}]"
    decision_text = f"COVER {quantity} {ticker} @ {price:.2f}"
    try:
        trade_id = insert_trade(
            ticker, "BUY", quantity, price, geo_reasoning,
            (tech_reasoning or "") + fee_note, decision_text, confidence,
            direction="SHORT",
        )
    except Exception as ex:
        _alert_trade_not_logged("cover", ticker, quantity, price, ex)
        trade_id = None   # prosegue come successo: il trade E' avvenuto

    return {
        "success": True,
        "action": "COVER",
        "direction": "SHORT",
        "ticker": ticker,
        "quantity": quantity,
        "price": price,
        "gross_cost": round(gross, 2),
        "commission": round(fee, 2),
        "total_cost": round(total_cost, 2),
        "realized_pnl": round(realized_pnl, 2),
        "remaining_cash": round(new_cash, 2),
        "portfolio_total_value": round(new_total, 2),
        "trade_id": trade_id,
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
        # Direction-aware: lo SL di uno SHORT sta SOPRA il prezzo, quello di un
        # LONG sotto. Prima era hardcoded is_long=True → ogni SL di SHORT veniva
        # rifiutato e la posizione restava naked. La direzione si legge dalla
        # posizione (default LONG).
        is_long = str(pos.get("direction") or "LONG").upper() != "SHORT"
        ok, reason = _validate_sltp_level(
            float(stop_price), float(cur), kind="SL", is_long=is_long,
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
        # Direction-aware (vedi set_stop_loss): il TP di uno SHORT sta SOTTO il
        # prezzo corrente, quello di un LONG sopra.
        is_long = str(pos.get("direction") or "LONG").upper() != "SHORT"
        ok, reason = _validate_sltp_level(
            float(target_price), float(cur), kind="TP", is_long=is_long,
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


def close_position_market(ticker, quantity, price, geo_reasoning,
                          tech_reasoning, confidence=100):
    """Chiude (a mercato) una posizione instradando per DIREZIONE:
    SHORT → execute_cover, LONG → execute_sell. Prima close_position e
    liquidate_all chiamavano sempre execute_sell, che su una SHORT torna
    success=False senza chiudere nulla (posizione lasciata aperta ma
    contata come chiusa → in emergenza l'utente si credeva flat)."""
    existing = get_position(ticker)
    if existing is None:
        return {"success": False, "reason": f"Nessuna posizione aperta per {ticker}"}
    if str(existing.get("direction") or "LONG").upper() == "SHORT":
        return execute_cover(ticker, quantity, price, geo_reasoning,
                             tech_reasoning, confidence)
    return execute_sell(ticker, quantity, price, geo_reasoning,
                        tech_reasoning, confidence)


def liquidate_all_positions(reason: str = "circuit_breaker",
                              prices: dict | None = None) -> list:
    """
    Vende TUTTE le posizioni aperte al prezzo corrente. Utilizzato dal
    circuit breaker 24h (risk_state.is_circuit_breaker_triggered).

    Args:
        reason: motivo descrittivo per audit (geo_reasoning del trade).
        prices: dict {ticker: current_price} fresh dal price polling.
                Se None, usa current_price salvato sulla posizione.

    Ritorna lista di trade eseguiti (uno per ticker).
    """
    import database
    executed: list[dict] = []
    try:
        positions = get_positions() or []
    except Exception as e:
        logger.error("liquidate_all_positions read fail: %s", e)
        return executed

    for p in positions:
        ticker = p.get("ticker")
        qty = float(p.get("quantity") or 0)
        if not ticker or qty <= 0:
            continue

        # Prezzo: prima dal dict fresh, poi dalla posizione (salvato)
        cur = None
        if prices and ticker in prices:
            try:
                cur = float(prices[ticker])
            except (TypeError, ValueError):
                cur = None
        if cur is None or cur <= 0:
            try:
                cur = float(p.get("current_price") or p.get("avg_buy_price") or 0)
            except (TypeError, ValueError):
                cur = 0
        if cur <= 0:
            logger.warning("liquidate_all_positions skip %s: no valid price",
                           ticker)
            continue

        try:
            result = close_position_market(
                ticker, qty, cur,
                geo_reasoning=f"CIRCUIT_BREAKER_LIQUIDATE: {reason}",
                tech_reasoning=f"(forced close, qty={qty}, price={cur:.4f})",
                confidence=100,
            )
            executed.append({
                "ticker": ticker, "qty": qty, "price": cur,
                "result": result,
            })
        except Exception as e:
            logger.error("liquidate %s failed: %s", ticker, e)
            executed.append({"ticker": ticker, "error": str(e)})

    # Log evento per audit
    try:
        import json as _json
        database.insert_agent_log(
            "circuit_breaker", "RISK_STATE_LIQUIDATE",
            _json.dumps({
                "event": "liquidate_all",
                "reason": reason,
                "positions_liquidated": len(executed),
                "tickers": [e.get("ticker") for e in executed],
            }, default=str),
        )
    except Exception:
        pass

    return executed


# ─── Auto-exits configuration (default safe) ──────────────────────────────
SETTING_AUTO_EXITS_ENABLED = "auto_exits_enabled"   # default False (post-bug)
SETTING_AUTO_EXITS_SELL_PCT = "auto_exits_sell_pct"  # % della qty da vendere
SETTING_AUTO_EXITS_MARGIN_PCT = "auto_exits_margin_pct"  # margine sicurezza
SETTING_AUTO_EXITS_ALERT_ONLY = "auto_exits_alert_only"  # se True, solo log

DEFAULT_AUTO_EXITS_SELL_PCT = 50.0     # default: vendi 50%, non TUTTO
DEFAULT_AUTO_EXITS_MARGIN_PCT = 1.0    # 1% margine sicurezza vs noise


def _get_auto_exits_config() -> dict:
    """Legge la config corrente. Default safe: disabilitato."""
    try:
        import database as _db
        raw_enabled = (_db.get_setting(SETTING_AUTO_EXITS_ENABLED, "false") or "").strip().lower()
        enabled = raw_enabled in ("1", "true", "yes", "on")
        raw_alert = (_db.get_setting(SETTING_AUTO_EXITS_ALERT_ONLY, "true") or "").strip().lower()
        alert_only = raw_alert in ("1", "true", "yes", "on")
        try:
            sell_pct = float(_db.get_setting(SETTING_AUTO_EXITS_SELL_PCT, "") or DEFAULT_AUTO_EXITS_SELL_PCT)
        except (ValueError, TypeError):
            sell_pct = DEFAULT_AUTO_EXITS_SELL_PCT
        try:
            margin_pct = float(_db.get_setting(SETTING_AUTO_EXITS_MARGIN_PCT, "") or DEFAULT_AUTO_EXITS_MARGIN_PCT)
        except (ValueError, TypeError):
            margin_pct = DEFAULT_AUTO_EXITS_MARGIN_PCT
        return {
            "enabled": enabled,
            "alert_only": alert_only,
            "sell_pct": max(1.0, min(100.0, sell_pct)),
            "margin_pct": max(0.0, margin_pct),
        }
    except Exception:
        return {
            "enabled": False,
            "alert_only": True,
            "sell_pct": DEFAULT_AUTO_EXITS_SELL_PCT,
            "margin_pct": DEFAULT_AUTO_EXITS_MARGIN_PCT,
        }


def check_and_execute_auto_exits(prices: dict | None = None) -> list:
    """
    Per ogni posizione con SL/TP impostato, controlla se il prezzo corrente
    ha attivato l'exit automatico.

    POST-BUG SAFETY HARDENING:
    - Default DISABILITATO (auto_exits_enabled = "false"). Da quando il
      sistema ha venduto autonomamente NVDA (auto_stop_loss + auto_take_profit
      su tutta la posizione), il default e' off. L'utente o il Decision
      Agent gestiscono le chiusure esplicitamente.
    - Quando ENABLED:
        * Margine sicurezza: il prezzo deve superare il target di
          `auto_exits_margin_pct`% (default 1%) per triggerare, evitando
          scatti su noise / wick.
        * Vendita parziale: solo `auto_exits_sell_pct`% della quantita'
          viene venduta (default 50%, non TUTTA). Il resto resta aperto.
        * Alert-only mode: se `auto_exits_alert_only=true`, scrive solo
          un log RISK_AUTO_EXIT_ALERT senza eseguire SELL.

    Args:
        prices: dict {ticker: current_price} aggiornato da price_polling.

    Ritorna lista di dict con i trade eseguiti o gli alert generati.
    """
    cfg = _get_auto_exits_config()

    executed: list = []
    try:
        positions = get_positions_with_auto_exits()
    except Exception:
        positions = []
    if not positions:
        return executed

    if not cfg["enabled"]:
        # Silenzioso: niente alert anche se SL/TP hit, l'utente ha
        # esplicitamente disabilitato il sistema.
        return executed

    for p in positions:
        ticker = p.get("ticker")
        if not ticker:
            continue
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
        qty = float(p.get("quantity") or 0)
        avg = float(p.get("avg_buy_price") or 0)
        if qty <= 0:
            continue

        # Margine sicurezza: il prezzo deve superare il target di margin_pct%.
        margin = cfg["margin_pct"] / 100.0
        direction = str(p.get("direction") or "LONG").upper()
        is_short = (direction == "SHORT")

        trigger = None
        target_price = None
        if is_short:
            # SHORT: lo SL sta SOPRA l'entry → si esce in PERDITA se il
            # prezzo SALE oltre lo SL. Il TP sta SOTTO → si esce in
            # PROFITTO se il prezzo SCENDE sotto il TP. Logica invertita
            # rispetto al long.
            sl_trigger_price = sl * (1.0 + margin) if sl > 0 else 0
            tp_trigger_price = tp * (1.0 - margin) if tp > 0 else 0
            if tp > 0 and cur_price <= tp_trigger_price:
                trigger = "take_profit"
                target_price = tp
            elif sl > 0 and cur_price >= sl_trigger_price:
                trigger = "stop_loss"
                target_price = sl
        else:
            # LONG: SL sotto l'entry, TP sopra.
            sl_trigger_price = sl * (1.0 - margin) if sl > 0 else 0
            tp_trigger_price = tp * (1.0 + margin) if tp > 0 else 0
            if tp > 0 and cur_price >= tp_trigger_price:
                trigger = "take_profit"
                target_price = tp
            elif sl > 0 and cur_price <= sl_trigger_price:
                trigger = "stop_loss"
                target_price = sl

        if not trigger:
            continue

        # Vendita parziale: % della qty.
        sell_qty = round(qty * cfg["sell_pct"] / 100.0, 6)
        if sell_qty <= 0:
            sell_qty = qty   # fallback safety

        reason = (
            f"AUTO {trigger.upper()}: prezzo {cur_price:.4f} ha superato"
            f" target {target_price:.4f} (+margin {cfg['margin_pct']:.1f}%)."
            f" Sell qty {sell_qty} su {qty} ({cfg['sell_pct']:.0f}%, carico {avg:.4f})."
        )

        # Alert-only mode: non eseguire trade, solo log
        if cfg["alert_only"]:
            try:
                from database import insert_agent_log
                import json as _json
                insert_agent_log(
                    "auto_exit", "RISK_AUTO_EXIT_ALERT",
                    _json.dumps({
                        "event": f"auto_{trigger}_alert_only",
                        "ticker": ticker, "qty": qty,
                        "trigger_price": target_price,
                        "current_price": cur_price,
                        "would_sell_qty": sell_qty,
                        "avg_buy_price": avg,
                        "note": "auto_exits_alert_only=true: no SELL eseguito",
                    }, default=str),
                )
            except Exception:
                pass
            executed.append({
                "ticker": ticker, "trigger": trigger,
                "alert_only": True,
                "trigger_price": target_price,
                "current_price": cur_price,
                "would_sell_qty": sell_qty,
            })
            continue

        # Esegui la chiusura parziale (o totale se sell_pct=100).
        # SHORT → COVER (ricompra); LONG → SELL. confidence=100 marca
        # l'operazione come chiusura non-AI (auto-exit).
        try:
            if is_short:
                result = execute_cover(
                    ticker, sell_qty, cur_price,
                    geo_reasoning=f"auto_{trigger}",
                    tech_reasoning=reason,
                    confidence=100,
                )
            else:
                result = execute_sell(
                    ticker, sell_qty, cur_price,
                    geo_reasoning=f"auto_{trigger}",
                    tech_reasoning=reason,
                    confidence=100,
                )
            executed.append({
                "ticker": ticker, "trigger": trigger,
                "trigger_price": target_price,
                "executed_price": cur_price,
                "quantity_sold": sell_qty, "quantity_remaining": qty - sell_qty,
                "sell_pct": cfg["sell_pct"],
                "result": result,
            })
            try:
                from database import insert_agent_log
                import json as _json
                insert_agent_log(
                    "auto_exit", "DECISION_AUTO_EXIT",
                    _json.dumps({
                        "event": f"auto_{trigger}",
                        "ticker": ticker,
                        "qty_sold": sell_qty,
                        "qty_remaining": qty - sell_qty,
                        "trigger_price": target_price,
                        "executed_price": cur_price,
                        "avg_buy_price": avg,
                        "sell_pct": cfg["sell_pct"],
                        "margin_pct": cfg["margin_pct"],
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

    HARDENING: oltre al check NaN/zero, valida ogni prezzo contro
    l'avg_buy_price (cost basis): rifiuta extreme outliers (>10x equity,
    >20x crypto) per evitare contaminazione di current_price.
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

            # Validazione vs cost basis (avg_buy_price): cattura extreme outliers
            avg = float(pos.get("avg_buy_price") or 0)
            qty = float(pos.get("quantity") or 0)
            if avg > 0:
                is_crypto = (ticker.upper().startswith("X:") or
                             (ticker.upper().endswith("-USD") and len(ticker) > 4))
                outer_min = 0.05 if is_crypto else 0.10
                outer_max = 20.0 if is_crypto else 10.0
                ratio = new_price_f / avg
                if ratio < outer_min or ratio > outer_max:
                    logger.warning(
                        "update_prices REJECT %s: %.2f fuori [%.1f×, %.1f×] avg=%.2f "
                        "(probabile bad data, current_price NON aggiornato)",
                        ticker, new_price_f, outer_min, outer_max, avg,
                    )
                    continue

            update_position_price(ticker, new_price_f)
            # Calcola il P&L aggiornato (guard contro avg_buy_price=0)
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


