"""
performance_report.py — il report completo delle performance del portafoglio LIVE.

MODULO PURO: non importa database/portfolio, non tiene stato globale, non ha
side effect. Riceve i dati grezzi (trade, posizioni, portafoglio, storico del
valore, movimenti di cassa manuali) e restituisce dizionari pronti da
serializzare. Stessa regola di accounting.py: cosi' e' testabile senza DB e non
crea cicli di import.

La matematica dei soldi NON viene riscritta qui. Il FIFO long/short, le
commissioni e il P&L netto arrivano da accounting.compute_closed_trades, che
resta l'unica fonte di verita' (vedi accounting.py).

Cosa produce:
  1. transaction_ledger()  — OGNI transazione con la sua commissione e il flusso
     di cassa netto. E' la base del CSV "tutte le transazioni".
  2. round_trip_ledger()   — le operazioni CHIUSE (apertura -> chiusura) con P&L
     netto, commissioni pagate e durata.
  3. nav_metrics()         — rendimento, drawdown, volatilita', Sharpe, Sortino,
     Calmar sulla curva del valore.
  4. monthly_breakdown()   — rendimento mese per mese.
  5. ticker_breakdown()    — P&L, commissioni e win rate per singolo strumento.
  6. fee_summary()         — quanto sono costate le commissioni, anche in
     rapporto al profitto lordo (il "peso delle commissioni").
  7. build_report()        — assembla tutto in un unico dizionario.
  8. to_csv()              — serializza una lista di dizionari in CSV.

NOTA SUL CAMPIONAMENTO — perche' Sharpe e volatilita' qui non coincidono con
quelli di /api/portfolio/benchmark. Gli snapshot del valore arrivano dal price
polling, quindi possono essere anche uno al minuto. Annualizzare la deviazione
standard di rendimenti al minuto moltiplicando per radice di 252 e' sbagliato:
252 sono i giorni di borsa in un anno, non i minuti. Qui la serie viene prima
ridotta a UN punto per giorno (l'ultimo del giorno) e solo dopo annualizzata.
Il numero che esce e' confrontabile con lo Sharpe che pubblicano i fondi.
"""
from __future__ import annotations

import calendar
import csv
import io
import math
from datetime import datetime, timezone

from accounting import (
    commission,
    DEFAULT_COMMISSION_BPS,
    is_manual_close,
    compute_closed_trades,
    positions_value,
    unrealized_pnl,
)

# Giorni di borsa in un anno: costante standard per annualizzare.
TRADING_DAYS_PER_YEAR = 252

# Soglia sotto la quale i numeri annualizzati non hanno senso statistico.
# Con meno di 10 giorni di storico, volatilita' e Sharpe sono rumore: si
# restituisce None invece di un numero che sembra una misura ma non lo e'.
MIN_DAYS_FOR_ANNUALIZED = 10


# ── Utility di parsing ──────────────────────────────────────────────────────

def parse_ts(value) -> datetime | None:
    """Converte un timestamp ISO (con o senza Z) in datetime consapevole del
    fuso. Ritorna None se non e' interpretabile: nessuna eccezione, nessuna
    data inventata."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    txt = str(value).strip()
    if not txt:
        return None
    try:
        dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
    except ValueError:
        # SQLite scrive "YYYY-MM-DD HH:MM:SS" senza fuso.
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(txt[:19], fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _f(value, default: float = 0.0) -> float:
    """float() che non esplode mai: valori sporchi diventano `default`."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(out) or math.isinf(out):
        return default
    return out


def _r(value, digits: int = 2):
    """Arrotonda lasciando passare None (un valore mancante resta mancante,
    non diventa 0: sono due cose diverse e vanno mostrate diverse)."""
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _leg_role(action: str, direction: str) -> str:
    """Che cosa fa questa gamba: apre o chiude una posizione.

    BUY  + LONG  -> apertura      SELL + LONG  -> chiusura
    SELL + SHORT -> apertura      BUY  + SHORT -> chiusura
    """
    is_short = direction == "SHORT"
    opens = (action == "SELL") if is_short else (action == "BUY")
    return "apertura" if opens else "chiusura"


# ── 1. Registro di TUTTE le transazioni ─────────────────────────────────────

def transaction_ledger(trades: list,
                       commission_bps: float = DEFAULT_COMMISSION_BPS) -> list:
    """Una riga per ogni transazione eseguita, in ordine cronologico.

    Ogni riga porta il valore lordo, la commissione applicata a quella singola
    gamba e il flusso di cassa netto, con la convenzione gia' usata nel resto
    del backend (portfolio.execute_buy / execute_sell):
        BUY  -> esce cassa:  -(valore + commissione)
        SELL -> entra cassa: +(valore - commissione)
    Vale anche per gli short: aprire uno short e' un SELL (incassi), chiuderlo
    e' un BUY (paghi).

    Le righe malformate (ticker vuoto, quantita' o prezzo non positivi) sono
    marcate `valida=False` invece di essere buttate via in silenzio: se un
    trade e' sporco nel database, il CSV lo deve dire.
    """
    rows = []
    ordered = sorted(trades or [], key=lambda t: (parse_ts(t.get("timestamp"))
                                                  or datetime.min.replace(tzinfo=timezone.utc)))
    for t in ordered:
        ticker = str(t.get("ticker") or "").upper()
        action = str(t.get("action") or t.get("side") or "").upper()
        direction = str(t.get("direction") or "LONG").upper()
        if direction not in ("LONG", "SHORT"):
            direction = "LONG"
        qty = _f(t.get("quantity"))
        price = _f(t.get("price"))
        dt = parse_ts(t.get("timestamp"))

        valid = bool(ticker) and qty > 0 and price > 0 and action in ("BUY", "SELL")
        gross = qty * price
        fee = commission(gross, commission_bps) if valid else 0.0
        cash_flow = (-(gross + fee) if action == "BUY" else (gross - fee)) if valid else 0.0

        conf = t.get("confidence_score", t.get("confidence"))
        try:
            conf_val = float(conf) if conf is not None else None
        except (TypeError, ValueError):
            conf_val = None

        reason = (t.get("final_decision") or t.get("technical_reasoning")
                  or t.get("geopolitical_reasoning") or "")
        reason = " ".join(str(reason).split())[:300]

        rows.append({
            "id": t.get("id") or t.get("trade_id"),
            "data": dt.strftime("%Y-%m-%d") if dt else "",
            "ora_utc": dt.strftime("%H:%M:%S") if dt else "",
            "timestamp": dt.isoformat() if dt else str(t.get("timestamp") or ""),
            "ticker": ticker,
            "operazione": action,
            "direzione": direction,
            "tipo": _leg_role(action, direction) if valid else "",
            "quantita": _r(qty, 6),
            "prezzo_usd": _r(price, 4),
            "valore_lordo_usd": _r(gross),
            "commissione_usd": _r(fee),
            "commissione_bps": _r(commission_bps, 2),
            "flusso_cassa_usd": _r(cash_flow),
            "eseguito_da": str(t.get("execution_type") or "ai"),
            "decisore": ("sistema/manuale" if is_manual_close(conf_val) else "AI"),
            "confidence": _r(conf_val, 1),
            "motivazione": reason,
            "valida": valid,
        })
    return rows


# ── 2. Registro delle operazioni chiuse (round-trip) ────────────────────────

def round_trip_ledger(trades: list,
                      commission_bps: float = DEFAULT_COMMISSION_BPS) -> list:
    """Le operazioni CHIUSE: apertura accoppiata alla sua chiusura, con P&L
    netto e commissioni. Il calcolo e' interamente delegato ad
    accounting.compute_closed_trades; qui si aggiungono solo i giorni di
    detenzione e le etichette in italiano per il CSV.
    """
    closed = compute_closed_trades(trades, include_fees=True,
                                   commission_bps=commission_bps)
    rows = []
    for c in closed:
        d_open = parse_ts(c.get("buy_date"))
        d_close = parse_ts(c.get("sell_date"))
        holding_days = None
        if d_open and d_close:
            holding_days = max(0, (d_close - d_open).days)
        pnl = _f(c.get("pnl_usd"))
        rows.append({
            "ticker": c.get("ticker"),
            "direzione": c.get("direction"),
            "data_apertura": c.get("buy_date"),
            "data_chiusura": c.get("sell_date"),
            "giorni_detenzione": holding_days,
            "pnl_netto_usd": _r(pnl),
            "pnl_netto_pct": _r(c.get("pnl_pct"), 3),
            "pnl_lordo_usd": _r(c.get("gross_pnl_usd")),
            "commissioni_usd": _r(c.get("fees_paid")),
            "esito": ("vincente" if pnl > 0 else "perdente" if pnl < 0 else "pari"),
            "confidence": _r(c.get("confidence"), 1),
            "chiusura_manuale": bool(c.get("is_manual")),
            "motivazione_apertura": c.get("reason") or "",
        })
    return rows


# ── 3. Metriche sulla curva del valore (NAV) ────────────────────────────────

def daily_nav(history: list) -> list:
    """Riduce lo storico del NAV a UN punto per giorno (l'ultimo del giorno).

    Serve per annualizzare correttamente volatilita' e Sharpe: con snapshot al
    minuto, i rendimenti "per punto" non sono rendimenti giornalieri e
    moltiplicarli per radice di 252 gonfia il numero senza motivo.

    Ritorna [{"date": "YYYY-MM-DD", "value": float}, ...] in ordine crescente.
    """
    per_day: dict[str, tuple[datetime, float]] = {}
    for h in history or []:
        dt = parse_ts(h.get("timestamp") or h.get("created_at"))
        val = _f(h.get("total_value"))
        if not dt or val <= 0:
            continue
        key = dt.strftime("%Y-%m-%d")
        prev = per_day.get(key)
        if prev is None or dt >= prev[0]:
            per_day[key] = (dt, val)
    return [{"date": k, "value": v[1]} for k, v in sorted(per_day.items())]


def max_drawdown(values: list) -> tuple[float, int | None]:
    """Perdita massima dal picco precedente, in percentuale (numero <= 0), e
    indice del punto peggiore. Serie vuota o piatta -> (0.0, None)."""
    if not values:
        return 0.0, None
    peak = values[0]
    worst = 0.0
    worst_idx = None
    for i, v in enumerate(values):
        if v > peak:
            peak = v
        if peak > 0:
            dd = (v / peak - 1.0) * 100.0
            if dd < worst:
                worst = dd
                worst_idx = i
    return round(worst, 2), worst_idx


def drawdown_series(daily: list) -> list:
    """Curva "sott'acqua": per ogni giorno, quanto sei sotto il massimo storico.
    Zero quando sei su un nuovo massimo."""
    out = []
    peak = None
    for point in daily:
        v = point["value"]
        peak = v if peak is None or v > peak else peak
        dd = (v / peak - 1.0) * 100.0 if peak and peak > 0 else 0.0
        out.append({"date": point["date"], "value": round(v, 2),
                    "drawdown_pct": round(dd, 3)})
    return out


def nav_metrics(daily: list) -> dict:
    """Metriche di rendimento e rischio sulla serie giornaliera del NAV.

    Restituisce None (non 0) su tutto cio' che non e' misurabile con i dati
    disponibili: meno di 2 punti, o meno di MIN_DAYS_FOR_ANNUALIZED giorni per
    le metriche annualizzate. Un None si mostra come "dati insufficienti"; uno
    zero si legge come "misurato, vale zero", che sarebbe una bugia.
    """
    n = len(daily)
    empty = {
        "punti_giornalieri": n,
        "primo_giorno": daily[0]["date"] if n else None,
        "ultimo_giorno": daily[-1]["date"] if n else None,
        "valore_iniziale_usd": _r(daily[0]["value"]) if n else None,
        "valore_finale_usd": _r(daily[-1]["value"]) if n else None,
        "rendimento_totale_pct": None,
        "rendimento_annualizzato_pct": None,
        "volatilita_annua_pct": None,
        "sharpe": None,
        "sortino": None,
        "calmar": None,
        "max_drawdown_pct": None,
        "max_drawdown_data": None,
        "giorni_positivi_pct": None,
        "miglior_giorno_pct": None,
        "peggior_giorno_pct": None,
        "giorni_calendario": None,
    }
    if n < 2:
        return empty

    values = [p["value"] for p in daily]
    first, last = values[0], values[-1]
    total_ret = (last / first - 1.0) * 100.0 if first > 0 else None

    d0 = parse_ts(daily[0]["date"])
    d1 = parse_ts(daily[-1]["date"])
    cal_days = max(1, (d1 - d0).days) if d0 and d1 else n

    rets = [(values[i] / values[i - 1] - 1.0)
            for i in range(1, n) if values[i - 1] > 0]

    mdd, mdd_idx = max_drawdown(values)

    out = dict(empty)
    out["rendimento_totale_pct"] = _r(total_ret)
    out["max_drawdown_pct"] = mdd
    out["max_drawdown_data"] = daily[mdd_idx]["date"] if mdd_idx is not None else None
    out["giorni_calendario"] = cal_days

    if rets:
        pos = sum(1 for r in rets if r > 0)
        out["giorni_positivi_pct"] = round(pos / len(rets) * 100.0, 1)
        out["miglior_giorno_pct"] = round(max(rets) * 100.0, 2)
        out["peggior_giorno_pct"] = round(min(rets) * 100.0, 2)

    # Annualizzate solo con abbastanza storico.
    if len(rets) >= MIN_DAYS_FOR_ANNUALIZED and first > 0:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
        std = math.sqrt(var)
        if std > 0:
            out["volatilita_annua_pct"] = round(
                std * math.sqrt(TRADING_DAYS_PER_YEAR) * 100.0, 2)
            out["sharpe"] = round(mean / std * math.sqrt(TRADING_DAYS_PER_YEAR), 2)
        # Sortino: come lo Sharpe ma la volatilita' conta solo i giorni in
        # perdita (l'oscillazione al rialzo non e' un rischio da punire).
        downside = [r for r in rets if r < 0]
        if downside:
            dvar = sum(r ** 2 for r in downside) / len(downside)
            dstd = math.sqrt(dvar)
            if dstd > 0:
                out["sortino"] = round(mean / dstd * math.sqrt(TRADING_DAYS_PER_YEAR), 2)
        # CAGR + Calmar (rendimento annualizzato diviso il drawdown massimo).
        years = cal_days / 365.25
        if years > 0 and last > 0:
            cagr = ((last / first) ** (1.0 / years) - 1.0) * 100.0
            out["rendimento_annualizzato_pct"] = round(cagr, 2)
            if mdd < 0:
                out["calmar"] = round(cagr / abs(mdd), 2)
    return out


# ── 4. Rendimento mese per mese ─────────────────────────────────────────────

def monthly_breakdown(daily: list) -> list:
    """Rendimento di ogni mese di calendario, calcolato da chiusura a chiusura.

    Il primo mese parte dal primo valore disponibile (non da una chiusura del
    mese precedente che non esiste), quindi e' un mese parziale: viene marcato
    `parziale=True` invece di essere presentato come un mese pieno.
    """
    if len(daily) < 2:
        return []
    by_month: dict[str, list] = {}
    for p in daily:
        by_month.setdefault(p["date"][:7], []).append(p)

    out = []
    prev_close = None
    months = sorted(by_month)
    for i, month in enumerate(months):
        points = by_month[month]
        start = prev_close if prev_close is not None else points[0]["value"]
        end = points[-1]["value"]
        ret = (end / start - 1.0) * 100.0 if start > 0 else None
        # Parziale = non copre il mese intero: il primo se i dati non partono
        # dal giorno 1, l'ultimo se non arrivano all'ultimo giorno del mese
        # (tipicamente il mese in corso). Un mese in corso presentato come
        # pieno falsa il confronto con quelli prima.
        year, mon = int(month[:4]), int(month[5:7])
        month_end = f"{year:04d}-{mon:02d}-{calendar.monthrange(year, mon)[1]:02d}"
        first_partial = (i == 0 and points[0]["date"] != f"{month}-01")
        last_partial = (i == len(months) - 1 and points[-1]["date"] < month_end)
        out.append({
            "mese": month,
            "valore_iniziale_usd": _r(start),
            "valore_finale_usd": _r(end),
            "rendimento_pct": _r(ret),
            "variazione_usd": _r(end - start),
            "giorni_osservati": len(points),
            "parziale": bool(first_partial or last_partial),
        })
        prev_close = end
    return out


# ── 5. Scomposizione per strumento ──────────────────────────────────────────

def ticker_breakdown(round_trips: list, open_positions: list | None = None) -> list:
    """P&L realizzato, commissioni e win rate per singolo ticker, piu' il P&L
    ancora aperto se lo strumento e' tuttora in portafoglio. Ordinati dal
    contributo maggiore al minore."""
    agg: dict[str, dict] = {}
    for rt in round_trips or []:
        tk = rt.get("ticker") or "?"
        a = agg.setdefault(tk, {
            "ticker": tk, "operazioni_chiuse": 0, "vincenti": 0, "perdenti": 0,
            "pnl_netto_usd": 0.0, "pnl_lordo_usd": 0.0, "commissioni_usd": 0.0,
            "pnl_aperto_usd": 0.0, "posizione_aperta": False,
        })
        pnl = _f(rt.get("pnl_netto_usd"))
        a["operazioni_chiuse"] += 1
        a["vincenti"] += 1 if pnl > 0 else 0
        a["perdenti"] += 1 if pnl < 0 else 0
        a["pnl_netto_usd"] += pnl
        a["pnl_lordo_usd"] += _f(rt.get("pnl_lordo_usd"))
        a["commissioni_usd"] += _f(rt.get("commissioni_usd"))

    for p in open_positions or []:
        tk = str(p.get("ticker") or "?").upper()
        a = agg.setdefault(tk, {
            "ticker": tk, "operazioni_chiuse": 0, "vincenti": 0, "perdenti": 0,
            "pnl_netto_usd": 0.0, "pnl_lordo_usd": 0.0, "commissioni_usd": 0.0,
            "pnl_aperto_usd": 0.0, "posizione_aperta": False,
        })
        a["posizione_aperta"] = True
        a["pnl_aperto_usd"] += unrealized_pnl(
            p.get("quantity"), p.get("avg_buy_price"),
            p.get("current_price"), p.get("direction"))

    rows = []
    for a in agg.values():
        n = a["operazioni_chiuse"]
        rows.append({
            "ticker": a["ticker"],
            "operazioni_chiuse": n,
            "vincenti": a["vincenti"],
            "perdenti": a["perdenti"],
            "win_rate_pct": round(a["vincenti"] / n * 100.0, 1) if n else None,
            "pnl_netto_usd": _r(a["pnl_netto_usd"]),
            "pnl_lordo_usd": _r(a["pnl_lordo_usd"]),
            "commissioni_usd": _r(a["commissioni_usd"]),
            "pnl_aperto_usd": _r(a["pnl_aperto_usd"]),
            "posizione_aperta": a["posizione_aperta"],
        })
    rows.sort(key=lambda r: (_f(r["pnl_netto_usd"]) + _f(r["pnl_aperto_usd"])),
              reverse=True)
    return rows


# ── 6. Statistiche di trading e commissioni ─────────────────────────────────

def trade_stats(round_trips: list) -> dict:
    """Win rate, profit factor, guadagno medio, perdita media, aspettativa per
    operazione, serie consecutive. Tutto sui round-trip NETTI di commissioni."""
    n = len(round_trips or [])
    base = {
        "operazioni_chiuse": n,
        "vincenti": 0, "perdenti": 0, "pari": 0,
        "win_rate_pct": None, "profit_factor": None,
        "guadagno_medio_usd": None, "perdita_media_usd": None,
        "rapporto_guadagno_perdita": None,
        "aspettativa_per_operazione_usd": None,
        "miglior_operazione_usd": None, "peggior_operazione_usd": None,
        "durata_media_giorni": None,
        "serie_vincente_max": 0, "serie_perdente_max": 0,
        "pnl_realizzato_netto_usd": 0.0, "pnl_realizzato_lordo_usd": 0.0,
        "commissioni_totali_usd": 0.0,
        "chiusure_manuali": 0,
    }
    if n == 0:
        return base

    pnls = [_f(rt.get("pnl_netto_usd")) for rt in round_trips]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    base["vincenti"] = len(wins)
    base["perdenti"] = len(losses)
    base["pari"] = n - len(wins) - len(losses)
    base["win_rate_pct"] = round(len(wins) / n * 100.0, 1)
    base["pnl_realizzato_netto_usd"] = _r(sum(pnls))
    base["pnl_realizzato_lordo_usd"] = _r(sum(_f(rt.get("pnl_lordo_usd"))
                                              for rt in round_trips))
    base["commissioni_totali_usd"] = _r(sum(_f(rt.get("commissioni_usd"))
                                            for rt in round_trips))
    base["chiusure_manuali"] = sum(1 for rt in round_trips
                                   if rt.get("chiusura_manuale"))
    base["miglior_operazione_usd"] = _r(max(pnls))
    base["peggior_operazione_usd"] = _r(min(pnls))

    sum_w = sum(wins)
    sum_l = abs(sum(losses))
    if sum_l > 0:
        base["profit_factor"] = round(sum_w / sum_l, 2)
    elif sum_w > 0:
        # Nessuna perdita: il rapporto e' infinito. Si lascia None e lo si
        # dichiara a parole, invece di stampare "Infinity" in un CSV.
        base["profit_factor"] = None
    if wins:
        base["guadagno_medio_usd"] = _r(sum_w / len(wins))
    if losses:
        base["perdita_media_usd"] = _r(sum(losses) / len(losses))
    if wins and losses:
        avg_l = abs(sum(losses) / len(losses))
        if avg_l > 0:
            base["rapporto_guadagno_perdita"] = round((sum_w / len(wins)) / avg_l, 2)
    base["aspettativa_per_operazione_usd"] = _r(sum(pnls) / n)

    durations = [rt.get("giorni_detenzione") for rt in round_trips
                 if rt.get("giorni_detenzione") is not None]
    if durations:
        base["durata_media_giorni"] = round(sum(durations) / len(durations), 1)

    # Serie consecutive: quante vittorie/sconfitte di fila, al massimo.
    streak_w = streak_l = best_w = best_l = 0
    for p in pnls:
        if p > 0:
            streak_w += 1
            streak_l = 0
        elif p < 0:
            streak_l += 1
            streak_w = 0
        else:
            streak_w = streak_l = 0
        best_w = max(best_w, streak_w)
        best_l = max(best_l, streak_l)
    base["serie_vincente_max"] = best_w
    base["serie_perdente_max"] = best_l
    return base


def fee_summary(ledger: list, round_trips: list,
                commission_bps: float = DEFAULT_COMMISSION_BPS) -> dict:
    """Quanto sono costate le commissioni e quanto pesano davvero.

    "peso_su_profitto_lordo_pct" e' la domanda vera: di ogni dollaro guadagnato
    lordo, quanto se ne e' andato in commissioni. Il rapporto usa SOLO le
    operazioni chiuse, da entrambe le parti della divisione: mettere al
    numeratore anche le commissioni delle posizioni ancora aperte (il cui
    guadagno non e' ancora nel denominatore) gonfierebbe il peso. Se il
    profitto lordo e' negativo o zero il rapporto non ha senso e resta None.
    """
    total_fees = sum(_f(r.get("commissione_usd")) for r in ledger or [])
    gross_traded = sum(_f(r.get("valore_lordo_usd")) for r in ledger or [])
    gross_pnl = sum(_f(rt.get("pnl_lordo_usd")) for rt in round_trips or [])
    net_pnl = sum(_f(rt.get("pnl_netto_usd")) for rt in round_trips or [])

    by_month: dict[str, float] = {}
    for r in ledger or []:
        month = str(r.get("data") or "")[:7]
        if month:
            by_month[month] = by_month.get(month, 0.0) + _f(r.get("commissione_usd"))

    closed_fees = sum(_f(rt.get("commissioni_usd")) for rt in round_trips or [])
    weight = None
    if gross_pnl > 0:
        weight = round(closed_fees / gross_pnl * 100.0, 1)

    return {
        "commissione_bps": _r(commission_bps, 2),
        "commissioni_operazioni_chiuse_usd": _r(closed_fees),
        "commissione_pct_per_operazione": _r(commission_bps / 100.0, 4),
        "commissioni_totali_usd": _r(total_fees),
        "numero_transazioni": len(ledger or []),
        "commissione_media_usd": (_r(total_fees / len(ledger)) if ledger else None),
        "volume_lordo_negoziato_usd": _r(gross_traded),
        "pnl_lordo_usd": _r(gross_pnl),
        "pnl_netto_usd": _r(net_pnl),
        "peso_su_profitto_lordo_pct": weight,
        "commissioni_per_mese": [
            {"mese": m, "commissioni_usd": _r(v)} for m, v in sorted(by_month.items())
        ],
    }


# ── 7. Report completo ──────────────────────────────────────────────────────

def build_report(trades: list,
                 positions: list,
                 portfolio_state: dict | None,
                 history: list,
                 commission_bps: float = DEFAULT_COMMISSION_BPS,
                 initial_balance: float | None = None,
                 cash_adjustments: list | None = None,
                 benchmark: dict | None = None,
                 period: str = "all",
                 period_start: datetime | None = None) -> dict:
    """Assembla l'intero report. Nessuna chiamata di rete, nessun accesso al DB:
    tutto arriva dai parametri, cosi' il test lo puo' ricostruire a mano.

    `period_start`: se valorizzato, transazioni e operazioni vengono filtrate
    da quella data in poi (le transazioni per data di esecuzione, le
    operazioni per data di chiusura). Il FIFO viene comunque costruito su
    TUTTA la storia, perche' una chiusura di oggi puo' avere l'apertura di sei
    mesi fa: il filtro si applica dopo, sulle righe. Senza, "ultimi 7 giorni"
    mostrava il rendimento di una settimana accanto alle commissioni di tutta
    la vita del portafoglio.
    """
    ledger = transaction_ledger(trades, commission_bps)
    round_trips = round_trip_ledger(trades, commission_bps)
    if period_start is not None:
        floor = datetime.min.replace(tzinfo=timezone.utc)
        ledger = [r for r in ledger
                  if (parse_ts(r.get("timestamp")) or floor) >= period_start]
        round_trips = [r for r in round_trips
                       if (parse_ts(r.get("data_chiusura")) or floor) >= period_start]
    daily = daily_nav(history)
    nav = nav_metrics(daily)
    stats = trade_stats(round_trips)
    fees = fee_summary(ledger, round_trips, commission_bps)
    months = monthly_breakdown(daily)
    tickers = ticker_breakdown(round_trips, positions)

    pstate = portfolio_state or {}
    total_value = _f(pstate.get("total_value"))
    cash = _f(pstate.get("cash_balance"))
    invested = positions_value(positions or [], price_key="current_price")
    open_pnl = sum(unrealized_pnl(p.get("quantity"), p.get("avg_buy_price"),
                                  p.get("current_price"), p.get("direction"))
                   for p in positions or [])

    start_capital = _f(initial_balance) if initial_balance else None
    total_pnl = None
    total_pnl_pct = None
    if start_capital and start_capital > 0 and total_value > 0:
        total_pnl = total_value - start_capital
        total_pnl_pct = total_pnl / start_capital * 100.0

    # Versamenti/prelievi manuali: distorcono il rendimento calcolato come
    # semplice differenza di valore. Se ce ne sono, il report lo dichiara
    # invece di lasciar credere che quel +X% sia stato guadagnato.
    adjustments = []
    for a in cash_adjustments or []:
        adjustments.append({
            "timestamp": str(a.get("timestamp") or ""),
            "delta_usd": _r(a.get("delta")),
            "motivo": str(a.get("reason") or ""),
            "origine": str(a.get("source") or ""),
        })
    adj_total = sum(_f(a.get("delta_usd")) for a in adjustments)

    warnings = []
    if period_start is not None:
        warnings.append(
            f"Transazioni, operazioni, commissioni e dettaglio per titolo sono "
            f"filtrati dal {period_start.strftime('%Y-%m-%d')} in poi. Capitale "
            f"iniziale, valore attuale e guadagno totale restano riferiti "
            f"all'intera vita del portafoglio.")
    if adjustments:
        warnings.append(
            f"Rilevati {len(adjustments)} movimenti di cassa manuali per "
            f"{adj_total:+,.2f}$ complessivi. Il rendimento calcolato come "
            f"differenza di valore include anche questi versamenti/prelievi: "
            f"non e' tutto guadagno di trading.")
    invalid = sum(1 for r in ledger if not r.get("valida"))
    if invalid:
        warnings.append(
            f"{invalid} transazioni nel database sono malformate (quantita' o "
            f"prezzo non validi): compaiono nel CSV marcate valida=False e non "
            f"entrano nei calcoli.")
    if nav["punti_giornalieri"] <= MIN_DAYS_FOR_ANNUALIZED:
        warnings.append(
            f"Solo {nav['punti_giornalieri']} giorni di storico: volatilita', "
            f"Sharpe, Sortino e Calmar non vengono calcolati perche' sotto i "
            f"{MIN_DAYS_FOR_ANNUALIZED} giorni sarebbero rumore, non misure.")
    if stats["operazioni_chiuse"] < 30:
        warnings.append(
            f"Solo {stats['operazioni_chiuse']} operazioni chiuse: win rate e "
            f"profit factor su questo campione non distinguono bravura da "
            f"fortuna.")

    return {
        "generato_il": datetime.now(timezone.utc).isoformat(),
        "periodo": period,
        "periodo_inizio": period_start.strftime("%Y-%m-%d") if period_start else None,
        "valuta": "USD",
        "sintesi": {
            "capitale_iniziale_usd": _r(start_capital),
            "valore_attuale_usd": _r(total_value),
            "liquidita_usd": _r(cash),
            "investito_usd": _r(invested),
            "pnl_totale_usd": _r(total_pnl),
            "pnl_totale_pct": _r(total_pnl_pct),
            "pnl_realizzato_netto_usd": stats["pnl_realizzato_netto_usd"],
            "pnl_non_realizzato_usd": _r(open_pnl),
            "commissioni_totali_usd": fees["commissioni_totali_usd"],
            "transazioni_totali": len(ledger),
            "operazioni_chiuse": stats["operazioni_chiuse"],
            "posizioni_aperte": len(positions or []),
            "movimenti_cassa_manuali_usd": _r(adj_total) if adjustments else None,
        },
        "metriche_nav": nav,
        "statistiche_operazioni": stats,
        "commissioni": fees,
        "mensile": months,
        "per_strumento": tickers,
        "curva_valore": drawdown_series(daily),
        "movimenti_cassa": adjustments,
        "benchmark": benchmark,
        "avvertenze": warnings,
        "transazioni": ledger,
        "operazioni": round_trips,
    }


# ── 8. Serializzazione CSV ──────────────────────────────────────────────────

# Colonne dei CSV scaricabili. L'ordine e' quello con cui si aprono nel foglio
# di calcolo, quindi conta: prima quando/cosa, poi quanto, poi il perche'.
CSV_COLUMNS = {
    "transazioni": [
        "id", "data", "ora_utc", "ticker", "operazione", "direzione", "tipo",
        "quantita", "prezzo_usd", "valore_lordo_usd", "commissione_usd",
        "commissione_bps", "flusso_cassa_usd", "eseguito_da", "decisore",
        "confidence", "valida", "motivazione", "timestamp",
    ],
    "operazioni": [
        "ticker", "direzione", "data_apertura", "data_chiusura",
        "giorni_detenzione", "pnl_netto_usd", "pnl_netto_pct", "pnl_lordo_usd",
        "commissioni_usd", "esito", "confidence", "chiusura_manuale",
        "motivazione_apertura",
    ],
    "curva_valore": ["date", "value", "drawdown_pct"],
    "mensile": [
        "mese", "valore_iniziale_usd", "valore_finale_usd", "rendimento_pct",
        "variazione_usd", "giorni_osservati", "parziale",
    ],
    "per_strumento": [
        "ticker", "operazioni_chiuse", "vincenti", "perdenti", "win_rate_pct",
        "pnl_netto_usd", "pnl_lordo_usd", "commissioni_usd", "pnl_aperto_usd",
        "posizione_aperta",
    ],
    "sintesi": ["metrica", "valore"],
}


def to_csv(rows: list, columns: list | None = None, separator: str = ",") -> str:
    """Serializza una lista di dizionari in CSV.

    - `columns` fissa l'ordine delle colonne; le chiavi non elencate seguono in
      coda ordinate, cosi' non si perde mai un dato per dimenticanza.
    - I None diventano stringa vuota (una cella vuota si legge come "non
      disponibile"; uno 0 si leggerebbe come una misura).
    - `separator` esiste per Excel in italiano, che si aspetta il punto e
      virgola. In quel caso anche i decimali usano la virgola (12,5 e non
      12.5): con il punto, Excel italiano legge i numeri come testo e la
      somma della colonna fa zero. Il valore standard resta la virgola come
      separatore e il punto come decimale.
    """
    if not rows:
        header = columns or []
        return separator.join(header) + "\r\n" if header else ""

    keys = list(columns or [])
    for row in rows:
        for k in row:
            if k not in keys:
                keys.append(k)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=keys, delimiter=separator,
                            extrasaction="ignore", lineterminator="\r\n")
    writer.writeheader()

    def _cell(v):
        if v is None:
            return ""
        if separator == ";" and isinstance(v, float):
            return str(v).replace(".", ",")
        return v

    for row in rows:
        writer.writerow({k: _cell(row.get(k)) for k in keys})
    return buf.getvalue()


def summary_rows(report: dict) -> list:
    """Trasforma il report in righe metrica/valore, per il CSV di sintesi.
    Un foglio a due colonne si legge e si incolla ovunque."""
    rows: list = []

    def add(label: str, value):
        rows.append({"metrica": label, "valore": "" if value is None else value})

    s = report.get("sintesi", {})
    nav = report.get("metriche_nav", {})
    st = report.get("statistiche_operazioni", {})
    fees = report.get("commissioni", {})
    bench = report.get("benchmark") or {}

    add("Report generato il (UTC)", report.get("generato_il"))
    add("Periodo", report.get("periodo"))
    add("Valuta", report.get("valuta"))
    add("--- PORTAFOGLIO ---", "")
    add("Capitale iniziale (USD)", s.get("capitale_iniziale_usd"))
    add("Valore attuale (USD)", s.get("valore_attuale_usd"))
    add("Liquidita' (USD)", s.get("liquidita_usd"))
    add("Investito (USD)", s.get("investito_usd"))
    add("P&L totale (USD)", s.get("pnl_totale_usd"))
    add("P&L totale (%)", s.get("pnl_totale_pct"))
    add("P&L realizzato netto (USD)", s.get("pnl_realizzato_netto_usd"))
    add("P&L non realizzato (USD)", s.get("pnl_non_realizzato_usd"))
    add("Movimenti di cassa manuali (USD)", s.get("movimenti_cassa_manuali_usd"))
    add("--- RENDIMENTO E RISCHIO ---", "")
    add("Primo giorno", nav.get("primo_giorno"))
    add("Ultimo giorno", nav.get("ultimo_giorno"))
    add("Giorni di calendario", nav.get("giorni_calendario"))
    add("Rendimento totale (%)", nav.get("rendimento_totale_pct"))
    add("Rendimento annualizzato CAGR (%)", nav.get("rendimento_annualizzato_pct"))
    add("Volatilita' annua (%)", nav.get("volatilita_annua_pct"))
    add("Sharpe", nav.get("sharpe"))
    add("Sortino", nav.get("sortino"))
    add("Calmar", nav.get("calmar"))
    add("Max drawdown (%)", nav.get("max_drawdown_pct"))
    add("Max drawdown in data", nav.get("max_drawdown_data"))
    add("Giorni positivi (%)", nav.get("giorni_positivi_pct"))
    add("Miglior giorno (%)", nav.get("miglior_giorno_pct"))
    add("Peggior giorno (%)", nav.get("peggior_giorno_pct"))
    add("--- OPERAZIONI ---", "")
    add("Transazioni totali", s.get("transazioni_totali"))
    add("Operazioni chiuse", st.get("operazioni_chiuse"))
    add("Posizioni aperte", s.get("posizioni_aperte"))
    add("Vincenti", st.get("vincenti"))
    add("Perdenti", st.get("perdenti"))
    add("Win rate (%)", st.get("win_rate_pct"))
    add("Profit factor", st.get("profit_factor"))
    add("Guadagno medio (USD)", st.get("guadagno_medio_usd"))
    add("Perdita media (USD)", st.get("perdita_media_usd"))
    add("Rapporto guadagno/perdita", st.get("rapporto_guadagno_perdita"))
    add("Aspettativa per operazione (USD)", st.get("aspettativa_per_operazione_usd"))
    add("Miglior operazione (USD)", st.get("miglior_operazione_usd"))
    add("Peggior operazione (USD)", st.get("peggior_operazione_usd"))
    add("Durata media (giorni)", st.get("durata_media_giorni"))
    add("Serie vincente piu' lunga", st.get("serie_vincente_max"))
    add("Serie perdente piu' lunga", st.get("serie_perdente_max"))
    add("Chiusure non decise dall'AI", st.get("chiusure_manuali"))
    add("--- COMMISSIONI ---", "")
    add("Commissione applicata (bps)", fees.get("commissione_bps"))
    add("Commissione per operazione (%)", fees.get("commissione_pct_per_operazione"))
    add("Commissioni totali (USD)", fees.get("commissioni_totali_usd"))
    add("di cui su operazioni chiuse (USD)",
        fees.get("commissioni_operazioni_chiuse_usd"))
    add("Commissione media per transazione (USD)", fees.get("commissione_media_usd"))
    add("Volume lordo negoziato (USD)", fees.get("volume_lordo_negoziato_usd"))
    add("P&L lordo prima delle commissioni (USD)", fees.get("pnl_lordo_usd"))
    add("P&L netto dopo le commissioni (USD)", fees.get("pnl_netto_usd"))
    add("Peso commissioni sul profitto lordo (%)", fees.get("peso_su_profitto_lordo_pct"))
    if bench and bench.get("available"):
        add("--- CONFRONTO CON IL BENCHMARK ---", "")
        add("Benchmark", "S&P 500 (SPY)")
        add("Rendimento portafoglio nel periodo (%)", bench.get("portfolio_return_pct"))
        add("Rendimento S&P 500 nel periodo (%)", bench.get("sp500_return_pct"))
        add("Differenza alpha (punti percentuali)", bench.get("alpha_pct"))
        add("Max drawdown portafoglio (%)", bench.get("portfolio_max_drawdown_pct"))
        add("Max drawdown S&P 500 (%)", bench.get("sp500_max_drawdown_pct"))
        add("Verdetto", bench.get("verdict"))
        add("Dettaglio verdetto", bench.get("verdict_detail"))
    for i, w in enumerate(report.get("avvertenze") or [], start=1):
        add(f"Avvertenza {i}", w)
    return rows


def benchmark_series_rows(benchmark: dict | None) -> list:
    """Le due curve base 100 (portafoglio e S&P 500) affiancate, per il CSV.

    Le due serie hanno lunghezze diverse (snapshot del portafoglio contro
    chiusure giornaliere dell'indice): l'asse comune e' la percentuale di
    periodo trascorso, cosi' le righe si confrontano una a una.
    """
    if not benchmark or not benchmark.get("available"):
        return []
    port = benchmark.get("portfolio_series_norm") or []
    spy = benchmark.get("sp500_series_norm") or []
    if not port and not spy:
        return []

    def at(series, frac):
        if not series:
            return None
        if len(series) == 1:
            return series[0]
        pos = frac * (len(series) - 1)
        lo, hi = math.floor(pos), math.ceil(pos)
        w = pos - lo
        return round(series[lo] * (1 - w) + series[hi] * w, 3)

    steps = 100
    rows = []
    for i in range(steps + 1):
        frac = i / steps
        rows.append({
            "pct_periodo_trascorso": i,
            "portafoglio_base100": at(port, frac),
            "sp500_base100": at(spy, frac),
        })
    return rows
