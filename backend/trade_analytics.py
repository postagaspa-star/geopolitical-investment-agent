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


def compute_closed_trades(trades: list) -> list:
    """
    Accoppia BUY→SELL per ticker in ordine temporale (FIFO). Ritorna
    [{pnl_pct, pnl_usd, confidence, ticker, buy_date, sell_date, reason}].

    ESCLUDE qualsiasi gamba (BUY o SELL) con confidence di chiusura
    manuale/forzata (== 100): il BUY non viene messo in coda, il SELL
    non chiude nulla. Cosi' i round-trip toccati da un intervento
    non-AI semplicemente NON entrano nelle statistiche (l'edge misurato
    riflette solo decisioni AI complete entrata+uscita).

    Coerenza con Analytics: pnl_usd = (sell-buy)*qty, IDENTICO al JS,
    cosi' profit factor sui DOLLARI coincide con AnalyticsPage "Tutto".
    """
    buy_queue: dict = {}
    closed: list = []

    srt = sorted(trades or [], key=lambda t: _ts(t.get("timestamp")))
    for t in srt:
        ticker = (t.get("ticker") or "").upper()
        action = (t.get("action") or t.get("side") or "").upper()
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
        # Regola sistemica: le operazioni a confidence 100 (chiusure
        # non-AI) NON contano in nessuna analisi. Saltata la gamba,
        # l'eventuale BUY rimasto in coda resta "aperto" (non chiuso
        # in modo manuale) e non genera un closed-trade fittizio.
        if is_manual_close(conf):
            continue
        if action == "BUY":
            # Estratto del ragionamento all'ENTRATA: e' qui che si forma
            # (o si sbaglia) la confidence. final_decision e' il piu'
            # sintetico; fallback ai due agenti specialisti.
            _reason = (t.get("final_decision") or t.get("technical_reasoning")
                       or t.get("geopolitical_reasoning") or "")
            _reason = " ".join(str(_reason).split())[:240]
            buy_queue.setdefault(ticker, []).append(
                {"price": price, "qty": qty, "conf": conf,
                 "ts": t.get("timestamp"), "reason": _reason})
        elif action == "SELL":
            remaining = qty
            while remaining > 0 and buy_queue.get(ticker):
                buy = buy_queue[ticker][0]
                if buy["price"] <= 0:
                    buy_queue[ticker].pop(0)
                    continue
                matched = min(remaining, buy["qty"])
                pnl_pct = (price - buy["price"]) / buy["price"] * 100.0
                pnl_usd = (price - buy["price"]) * matched
                closed.append({"pnl_pct": round(pnl_pct, 3),
                               "pnl_usd": round(pnl_usd, 2),
                               "confidence": buy["conf"],
                               "ticker": ticker,
                               "buy_date": str(buy.get("ts") or "")[:10],
                               "sell_date": str(t.get("timestamp") or "")[:10],
                               "reason": buy.get("reason") or ""})
                remaining -= matched
                buy["qty"] -= matched
                if buy["qty"] <= 1e-9:
                    buy_queue[ticker].pop(0)
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
