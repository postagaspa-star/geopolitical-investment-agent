"""
trade_analytics.py — logica CANONICA di analisi dei trade chiusi.

Unico posto in cui vive (lato backend):
  1. la regola "operazione manuale/forzata" (confidence == 100): sono
     chiusure NON decise dall'AI (circuit breaker, auto-exit SL/TP,
     chiusura manuale dell'utente). NON devono entrare in NESSUNA
     analisi/grafico/ragionamento dell'AI: falserebbero la misura
     dell'edge e la calibrazione della confidence.
  2. l'accoppiamento FIFO BUY→SELL per ticker (stessa identica logica
     di AnalyticsPage.computeClosedTrades lato frontend).

Consumatori: main.py (edge-tracker, diagnosi confidence) e
agents/decision.py (blocco di auto-calibrazione nel system prompt).
Tenere la regola in UN posto evita derive tra le varie superfici.
"""
from datetime import datetime as _dt

# Confidence con cui portfolio.py marca le chiusure NON-AI
# (liquidate_all_positions, process_auto_exits): 0-100, esattamente 100.
MANUAL_CLOSE_CONFIDENCE = 100.0


def is_manual_close(conf) -> bool:
    """
    True se la confidence indica un'operazione NON decisa dall'AI
    (forzata/automatica/manuale). L'AI per istruzione di prompt non
    supera mai ~85-97: 100 e' il marcatore riservato alle chiusure
    non-AI. >= 100 per robustezza (mai legittimo per una decisione AI).
    """
    if conf is None:
        return False
    try:
        return float(conf) >= MANUAL_CLOSE_CONFIDENCE
    except (TypeError, ValueError):
        return False


def _ts(v) -> float:
    try:
        return _dt.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def compute_closed_trades(trades: list,
                          exclude_manual_closes: bool = False,
                          include_fees: bool = True) -> list:
    """
    Accoppia BUY→SELL per ticker in ordine temporale (FIFO). Ritorna
    [{pnl_pct, pnl_usd, confidence, ticker, buy_date, sell_date, reason,
      is_manual, fees_paid}].

    exclude_manual_closes (default False):
      - False → INCLUDE TUTTO. Il P&L / la performance del portafoglio
        sono un FATTO finanziario: ogni chiusura sposta denaro reale e
        DEVE contare, anche le chiusure non-AI.
      - True → vista SKILL/EDGE: salta le gambe a confidence manuale.

    include_fees (default True):
      - True  → pnl_usd e pnl_pct sono NETTI (commissione open + close
        sottratte). Ogni gamba paga commission_bps × gross_value (default
        10 bps = 0.10%); su un round-trip BUY+SELL si pagano DUE fee.
        Senza questa correzione il P&L mostrato era 'grezzo' e
        sovrastimava il vero rendimento (e il rebuild dai trade
        sovrastimava la cassa).
      - False → P&L grezzo (cosi' com'era prima del fix). Utile per
        confronti storici o A/B testing senza commissioni.

    Ogni closed-trade porta `is_manual` (True se la gamba BUY o SELL era
    una chiusura non-AI) e `fees_paid` (commissione totale del round-trip,
    0 se include_fees=False).
    """
    # Lazy import per evitare cicli (portfolio importa database)
    if include_fees:
        try:
            from portfolio import _commission_amount as _fee_of
        except Exception:
            _fee_of = lambda v: 0.0
    else:
        _fee_of = lambda v: 0.0
    # Due code FIFO separate per ticker: long e short non si matchano
    # mai tra loro. Classificazione gamba per (action, direction):
    #   BUY +LONG  → apre long      SELL+LONG  → chiude long
    #   SELL+SHORT → apre short     BUY +SHORT → chiude short (cover)
    # I trade storici senza 'direction' → 'LONG' (retro-compatibile).
    long_queue: dict = {}
    short_queue: dict = {}
    closed: list = []

    srt = sorted(trades or [], key=lambda t: _ts(t.get("timestamp")))
    for t in srt:
        ticker = (t.get("ticker") or "").upper()
        action = (t.get("action") or t.get("side") or "").upper()
        direction = (t.get("direction") or "LONG").upper()
        try:
            price = float(t.get("price") or 0)
            qty = float(t.get("quantity") or 0)
        except (TypeError, ValueError):
            continue
        conf = t.get("confidence_score", t.get("confidence"))
        try:
            conf = float(conf) if conf is not None else None
        except (TypeError, ValueError):
            conf = None
        if not ticker or price <= 0 or qty <= 0:
            continue
        leg_manual = is_manual_close(conf)
        is_short = (direction == "SHORT")
        # Una gamba APRE se: BUY su book long, oppure SELL su book short.
        opens = (action == "SELL") if is_short else (action == "BUY")

        # NB: NON si salta MAI una gamba — nemmeno in vista SKILL: la coda
        # FIFO deve restare fisicamente coerente (saltare una gamba forzata
        # lascerebbe l'apertura corrispondente "appesa" → closed-trade
        # fantasma). Si TAGGA is_manual e, per la vista SKILL, si FILTRA
        # l'output a fine funzione.
        if opens:
            # Ragionamento all'ENTRATA (apertura): qui si forma la confidence.
            _reason = (t.get("final_decision") or t.get("technical_reasoning")
                       or t.get("geopolitical_reasoning") or "")
            _reason = " ".join(str(_reason).split())[:240]
            leg = {"price": price, "qty": qty, "conf": conf,
                   "ts": t.get("timestamp"), "reason": _reason,
                   "manual": leg_manual}
            (short_queue if is_short else long_queue).setdefault(
                ticker, []).append(leg)
        else:
            q = (short_queue if is_short else long_queue).get(ticker) or []
            remaining = qty
            while remaining > 0 and q:
                opn = q[0]
                if opn["price"] <= 0:
                    q.pop(0)
                    continue
                matched = min(remaining, opn["qty"])
                # Gross P&L (prima delle fee)
                if is_short:
                    gross_pnl_usd = (opn["price"] - price) * matched
                else:
                    gross_pnl_usd = (price - opn["price"]) * matched

                # Fee del round-trip: una su apertura (opn["price"] * matched),
                # una su chiusura (price * matched). Le due gambe condividono
                # `matched`, non l'intera qty della gamba originaria — quindi
                # le fee si splittano pro-quota tra match successivi.
                fee_open = _fee_of(opn["price"] * matched)
                fee_close = _fee_of(price * matched)
                fees_round_trip = fee_open + fee_close
                net_pnl_usd = gross_pnl_usd - fees_round_trip

                # pct calcolato sul cost basis dell'open (per coerenza con
                # le viste precedenti); se include_fees=True, e' netto.
                net_pnl_pct = (net_pnl_usd / (opn["price"] * matched) * 100.0
                               if opn["price"] > 0 else 0.0)

                closed.append({"pnl_pct": round(net_pnl_pct, 3),
                               "pnl_usd": round(net_pnl_usd, 2),
                               "fees_paid": round(fees_round_trip, 2),
                               "gross_pnl_usd": round(gross_pnl_usd, 2),
                               "confidence": opn["conf"],
                               "ticker": ticker,
                               "direction": "SHORT" if is_short else "LONG",
                               "buy_date": str(opn.get("ts") or "")[:10],
                               "sell_date": str(t.get("timestamp") or "")[:10],
                               "reason": opn.get("reason") or "",
                               "is_manual": bool(opn.get("manual")) or leg_manual})
                remaining -= matched
                opn["qty"] -= matched
                if opn["qty"] <= 1e-9:
                    q.pop(0)
    if exclude_manual_closes:
        # Vista SKILL: la coda e' stata costruita interamente (corretta),
        # ora si escludono SOLO i round-trip toccati da una gamba non-AI.
        return [c for c in closed if not c.get("is_manual")]
    return closed


def confidence_calibration_summary(closed: list) -> dict | None:
    """
    Sintesi COMPATTA della calibrazione confidence↔esito, per iniettarla
    nel system prompt del Decision Agent (auto-feedback). NON e' la
    diagnosi completa (quella resta in main.py): qui solo i numeri che
    servono all'AI per ritarare la propria confidence.

    Ritorna None se i campioni con confidence sono troppo pochi (<20).
    """
    wc = [c for c in (closed or []) if c.get("confidence") is not None]
    n = len(wc)
    if n < 20:
        return None
    confs = [float(c["confidence"]) for c in wc]
    rets = [float(c["pnl_pct"]) for c in wc]
    cmean = sum(confs) / n
    rmean = sum(rets) / n
    cstd = (sum((x - cmean) ** 2 for x in confs) / n) ** 0.5
    rstd = (sum((y - rmean) ** 2 for y in rets) / n) ** 0.5
    if cstd < 1e-9:
        return None
    corr = None
    if rstd > 1e-9:
        cov = sum((confs[i] - cmean) * (rets[i] - rmean)
                  for i in range(n)) / n
        corr = round(cov / (cstd * rstd), 3)
    order = sorted(range(n), key=lambda i: confs[i])
    t = max(1, n // 3)
    low_idx = order[:t]
    high_idx = order[-t:]

    def _avg(idxs):
        return round(sum(rets[i] for i in idxs) / len(idxs), 2)

    low_avg = _avg(low_idx)
    high_avg = _avg(high_idx)
    return {
        "samples": n,
        "correlation": corr,
        "low_conf_avg_return_pct": low_avg,
        "high_conf_avg_return_pct": high_avg,
        "inverted": (corr is not None and corr < 0) or (high_avg < low_avg),
    }
