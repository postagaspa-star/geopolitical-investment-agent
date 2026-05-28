"""
accounting.py — modulo CANONICO dei conti del portafoglio.

UNICA fonte di verita' per la matematica dei soldi. Tutto il resto del
backend (portfolio.py, trade_analytics.py, main.py audit, simulator, gli
agenti) DEVE chiamare queste funzioni invece di reimplementare i calcoli.

Razionale: gli stessi bug (NAV non direction-aware, P&L senza fee, FIFO che
mischia long e short) erano comparsi in ~10 posti diversi perche' la logica
era duplicata. Centralizzandola, un fix vale per tutti e un test la copre
una volta sola.

REGOLA: questo modulo e' PURO — niente import di database/portfolio, nessuno
stato globale, nessun side effect. Opera solo sui dati che riceve. Cosi' e'
testabile e non crea cicli di import.

Convenzioni:
  - LONG  → contribuisce +qty*prezzo al NAV; P&L = (prezzo - avg) * qty
  - SHORT → contribuisce -qty*prezzo al NAV (passivita'); P&L = (avg - prezzo) * qty
  - commissione = |valore_lordo| * bps / 10_000  (default 10 bps = 0.10%)
"""
from datetime import datetime as _dt

DEFAULT_COMMISSION_BPS = 10.0
# Confidence con cui le chiusure NON-AI (circuit breaker, auto-exit, manuali)
# vengono marcate. L'AI per prompt non supera mai ~97; 100 = marcatore non-AI.
MANUAL_CLOSE_CONFIDENCE = 100.0


# ── Primitive di valutazione ────────────────────────────────────────────────

def commission(gross_value, bps: float = DEFAULT_COMMISSION_BPS) -> float:
    """Commissione in $ da un valore lordo (quantity * price)."""
    try:
        return abs(float(gross_value)) * (float(bps) / 10_000.0)
    except (TypeError, ValueError):
        return 0.0


def is_short(direction) -> bool:
    return str(direction or "LONG").upper() == "SHORT"


def signed_position_value(qty, price, direction) -> float:
    """Contributo FIRMATO di una posizione al NAV.
    +qty*price per LONG, -qty*price per SHORT (passivita')."""
    try:
        v = float(qty) * float(price)
    except (TypeError, ValueError):
        return 0.0
    return -v if is_short(direction) else v


def positions_value(positions, price_key: str = "current_price") -> float:
    """Somma DIRECTION-AWARE del valore di una lista di posizioni.

    positions: lista di dict con almeno {quantity, <price_key>, direction}.
    Salta quantita'/prezzi non positivi o non numerici.
    """
    total = 0.0
    for p in positions or []:
        try:
            qty = float(p.get("quantity") or 0)
            price = float(p.get(price_key) or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0 or price <= 0:
            continue
        total += signed_position_value(qty, price, p.get("direction"))
    return total


def unrealized_pnl(qty, avg, current, direction) -> float:
    """P&L non realizzato DIRECTION-AWARE.
    LONG: (current - avg) * qty ; SHORT: (avg - current) * qty."""
    try:
        qty = float(qty); avg = float(avg); current = float(current)
    except (TypeError, ValueError):
        return 0.0
    if is_short(direction):
        return (avg - current) * qty
    return (current - avg) * qty


def unrealized_pnl_pct(avg, current, direction) -> float:
    """P&L % direction-aware rispetto al prezzo medio. 0 se avg<=0."""
    try:
        avg = float(avg); current = float(current)
    except (TypeError, ValueError):
        return 0.0
    if avg <= 0:
        return 0.0
    if is_short(direction):
        return (avg - current) / avg * 100.0
    return (current - avg) / avg * 100.0


# ── Closed-trades FIFO (con fee) ─────────────────────────────────────────────

def is_manual_close(conf) -> bool:
    """True se la confidence indica una chiusura NON decisa dall'AI."""
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
                          include_fees: bool = True,
                          commission_bps: float = DEFAULT_COMMISSION_BPS) -> list:
    """Accoppia aperture→chiusure per ticker in ordine temporale (FIFO),
    con code separate long/short (non si mischiano mai).

    Classificazione gamba per (action, direction):
      BUY +LONG  → apre long      SELL+LONG  → chiude long
      SELL+SHORT → apre short     BUY +SHORT → chiude short (cover)

    include_fees=True (default): pnl_usd e pnl_pct sono NETTI (commissione
    apertura + chiusura sottratte, a `commission_bps`). Ogni closed-trade
    porta anche gross_pnl_usd e fees_paid.

    exclude_manual_closes=True: vista SKILL — esclude i round-trip toccati da
    una chiusura non-AI (confidence>=100). La coda FIFO resta comunque
    costruita per intero (no closed-trade "fantasma").
    """
    fee_of = (lambda v: commission(v, commission_bps)) if include_fees else (lambda v: 0.0)

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
        short = (direction == "SHORT")
        opens = (action == "SELL") if short else (action == "BUY")

        if opens:
            _reason = (t.get("final_decision") or t.get("technical_reasoning")
                       or t.get("geopolitical_reasoning") or "")
            _reason = " ".join(str(_reason).split())[:240]
            leg = {"price": price, "qty": qty, "conf": conf,
                   "ts": t.get("timestamp"), "reason": _reason,
                   "manual": leg_manual}
            (short_queue if short else long_queue).setdefault(ticker, []).append(leg)
        else:
            q = (short_queue if short else long_queue).get(ticker) or []
            remaining = qty
            while remaining > 0 and q:
                opn = q[0]
                if opn["price"] <= 0:
                    q.pop(0)
                    continue
                matched = min(remaining, opn["qty"])
                if short:
                    gross_pnl_usd = (opn["price"] - price) * matched
                else:
                    gross_pnl_usd = (price - opn["price"]) * matched
                fees_round_trip = fee_of(opn["price"] * matched) + fee_of(price * matched)
                net_pnl_usd = gross_pnl_usd - fees_round_trip
                net_pnl_pct = (net_pnl_usd / (opn["price"] * matched) * 100.0
                               if opn["price"] > 0 else 0.0)
                closed.append({
                    "pnl_pct": round(net_pnl_pct, 3),
                    "pnl_usd": round(net_pnl_usd, 2),
                    "fees_paid": round(fees_round_trip, 2),
                    "gross_pnl_usd": round(gross_pnl_usd, 2),
                    "confidence": opn["conf"],
                    "ticker": ticker,
                    "direction": "SHORT" if short else "LONG",
                    "buy_date": str(opn.get("ts") or "")[:10],
                    "sell_date": str(t.get("timestamp") or "")[:10],
                    "reason": opn.get("reason") or "",
                    "is_manual": bool(opn.get("manual")) or leg_manual,
                })
                remaining -= matched
                opn["qty"] -= matched
                if opn["qty"] <= 1e-9:
                    q.pop(0)

    if exclude_manual_closes:
        return [c for c in closed if not c.get("is_manual")]
    return closed
