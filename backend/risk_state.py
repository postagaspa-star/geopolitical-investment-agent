"""
risk_state.py — regole stateful di risk-management che gli LLM da soli non
possono applicare (richiedono persistenza, dati storici, computation).

Implementa:
  - 24h drawdown circuit breaker (vendi tutto se DD > 5% in 24h)
  - Recovery mode (post-circuit-breaker, SL ≤ 5%, confidence ≥ 0.75)
  - Lock-in 0.5% (a +5% di profitto, SL auto → entry * 1.005)
  - Trailing stop dinamico (3-4% dal picco per posizioni in trend intatto)
  - Win rate ultime N chiusure
  - Concentration risk (% di NAV per posizione, trigger rebalance)

Tutte le funzioni sono stateless w.r.t. il chiamante: lo stato e' in DB
(settings + portfolio_snapshots + trades + price_history).

Settings keys utilizzate:
  - risk_recovery_mode               "true"|"false" (default false)
  - risk_recovery_mode_entered_at    ISO8601 timestamp
  - risk_recovery_initial_balance    float (snapshot del valore portafoglio
                                     all'ingresso recovery, per uscita)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Iterable

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# Constants — soglie configurabili da settings, fallback a default
# ═══════════════════════════════════════════════════════════════════════

DEFAULT_CIRCUIT_BREAKER_24H_PCT = -5.0       # se DD 24h ≤ questo → circuit breaker
DEFAULT_RECOVERY_SL_MAX_PCT = 5.0            # SL massimo durante recovery
DEFAULT_RECOVERY_CONFIDENCE_FLOOR = 0.75     # conviction floor recovery
DEFAULT_LOCK_IN_TRIGGER_PCT = 5.0            # +5% di profitto → applica lock-in
DEFAULT_LOCK_IN_SL_PCT = 0.5                 # SL = entry * (1 + 0.5/100)
DEFAULT_TRAILING_STOP_PCT_FROM_PEAK = 4.0    # 4% dal picco
DEFAULT_CONCENTRATION_TRIGGER_PCT = 35.0     # > 35% del NAV → rebalance forzato
DEFAULT_HIGH_RISK_SL_THRESHOLD_PCT = 10.0    # SL > 10% = high risk
DEFAULT_HIGH_RISK_CONFIDENCE_FLOOR = 0.75    # conviction floor per high risk

SETTING_RECOVERY_MODE = "risk_recovery_mode"
SETTING_RECOVERY_ENTERED_AT = "risk_recovery_mode_entered_at"
SETTING_RECOVERY_INITIAL_BALANCE = "risk_recovery_initial_balance"
SETTING_LAST_CB_TRIGGER_AT = "risk_last_cb_trigger_at"


# ═══════════════════════════════════════════════════════════════════════
# 1. Win rate ultime N chiusure (FIFO matching BUY → SELL)
# ═══════════════════════════════════════════════════════════════════════

def _compute_closed_trades_fifo(trades: list[dict]) -> list[dict]:
    """
    FIFO matching BUY → SELL per ticker. Port del computeClosedTrades di
    frontend/src/pages/AnalyticsPage.jsx. Ritorna lista di chiusure con
    pnl, pnl_pct, hold_hours per ognuna.
    """
    buy_queue: dict[str, list[dict]] = {}
    closed: list[dict] = []

    # Ordina ASC per timestamp
    def _ts(t):
        try:
            ts = t.get("timestamp") or t.get("created_at") or ""
            if not ts:
                return 0
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            return 0

    sorted_trades = sorted(trades or [], key=_ts)

    for t in sorted_trades:
        ticker = (t.get("ticker") or "").upper().strip()
        action = (t.get("action") or t.get("side") or "").upper().strip()
        try:
            price = float(t.get("price") or 0)
            qty = float(t.get("quantity") or 0)
        except (TypeError, ValueError):
            continue
        if not ticker or price <= 0 or qty <= 0:
            continue
        ts = t.get("timestamp") or t.get("created_at")

        if action == "BUY":
            buy_queue.setdefault(ticker, []).append({
                "price": price, "qty": qty, "ts": ts,
            })
        elif action == "SELL":
            remaining = qty
            queue = buy_queue.get(ticker) or []
            while remaining > 0 and queue:
                buy = queue[0]
                if buy["price"] <= 0:
                    queue.pop(0)
                    continue
                matched = min(remaining, buy["qty"])
                pnl = (price - buy["price"]) * matched
                pnl_pct = ((price - buy["price"]) / buy["price"]) * 100.0
                closed.append({
                    "ticker": ticker,
                    "buy_price": buy["price"],
                    "sell_price": price,
                    "qty": matched,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "buy_timestamp": buy["ts"],
                    "sell_timestamp": ts,
                })
                buy["qty"] -= matched
                remaining -= matched
                if buy["qty"] <= 0.000001:
                    queue.pop(0)
            buy_queue[ticker] = queue
    return closed


def get_recent_win_rate(limit: int = 10) -> dict:
    """
    Calcola il win rate delle ultime `limit` chiusure (FIFO BUY→SELL match).
    Ritorna {win_rate: float 0-1, wins: int, losses: int, sample_size: int}.

    Se `sample_size < limit`, ritorna comunque il WR computato sui trade
    disponibili (best-effort), perche' un WR su 5 trade e' meglio di niente.
    """
    try:
        import database
        # Fetch ampio per coprire eventuali parziali / unmatched (FIFO usa
        # piu' BUY per ogni SELL totale possibile).
        trades = database.get_trades(limit=max(200, limit * 10))
    except Exception as e:
        logger.warning("get_recent_win_rate: read trades failed: %s", e)
        return {"win_rate": 0.0, "wins": 0, "losses": 0, "sample_size": 0}

    closed = _compute_closed_trades_fifo(trades)
    if not closed:
        return {"win_rate": 0.0, "wins": 0, "losses": 0, "sample_size": 0}

    # Ultime N chiusure
    recent = closed[-limit:]
    wins = sum(1 for c in recent if c["pnl"] > 0)
    losses = sum(1 for c in recent if c["pnl"] <= 0)
    n = len(recent)
    wr = (wins / n) if n > 0 else 0.0
    return {
        "win_rate": round(wr, 3),
        "wins": wins,
        "losses": losses,
        "sample_size": n,
    }


# ═══════════════════════════════════════════════════════════════════════
# 2. 24h drawdown + circuit breaker
# ═══════════════════════════════════════════════════════════════════════

def compute_24h_drawdown() -> dict:
    """
    Ritorna {dd_pct: float, running_max: float, current_value: float,
             snapshots_in_window: int}.

    dd_pct e' NEGATIVO se il portafoglio e' sotto il running max delle
    ultime 24h, 0 se al picco o sopra. Sempre <= 0.

    Se non ci sono snapshot, ritorna dd_pct=0 (niente da misurare).
    """
    try:
        import database
        history = database.get_portfolio_history(days=2)
    except Exception as e:
        logger.warning("compute_24h_drawdown: history read fail: %s", e)
        return {"dd_pct": 0.0, "running_max": 0.0,
                "current_value": 0.0, "snapshots_in_window": 0}

    if not history:
        return {"dd_pct": 0.0, "running_max": 0.0,
                "current_value": 0.0, "snapshots_in_window": 0}

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    in_window: list[float] = []
    for h in history:
        ts_str = h.get("timestamp")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if ts < cutoff:
            continue
        try:
            v = float(h.get("total_value") or 0)
            if v > 0:
                in_window.append(v)
        except (TypeError, ValueError):
            continue

    if not in_window:
        return {"dd_pct": 0.0, "running_max": 0.0,
                "current_value": 0.0, "snapshots_in_window": 0}

    running_max = max(in_window)
    current = in_window[-1]
    dd_pct = ((current - running_max) / running_max * 100.0) if running_max > 0 else 0.0
    return {
        "dd_pct": round(dd_pct, 3),
        "running_max": round(running_max, 2),
        "current_value": round(current, 2),
        "snapshots_in_window": len(in_window),
    }


def is_circuit_breaker_triggered(threshold_pct: float | None = None) -> tuple[bool, float]:
    """Ritorna (triggered, dd_pct). Threshold di default -5%."""
    threshold = threshold_pct if threshold_pct is not None else DEFAULT_CIRCUIT_BREAKER_24H_PCT
    info = compute_24h_drawdown()
    dd = info.get("dd_pct", 0.0)
    triggered = dd <= threshold
    return triggered, dd


# ═══════════════════════════════════════════════════════════════════════
# 3. Recovery mode (post circuit breaker)
# ═══════════════════════════════════════════════════════════════════════

def is_in_recovery_mode() -> bool:
    try:
        import database
        raw = (database.get_setting(SETTING_RECOVERY_MODE, "false") or "").strip().lower()
        return raw in ("1", "true", "yes", "on")
    except Exception:
        return False


def get_recovery_initial_balance() -> float | None:
    """Balance snapshot all'ingresso recovery — soglia per l'uscita auto."""
    try:
        import database
        raw = database.get_setting(SETTING_RECOVERY_INITIAL_BALANCE, "") or ""
        if not raw:
            return None
        return float(raw)
    except Exception:
        return None


def enter_recovery_mode(reason: str = "circuit_breaker_24h") -> dict:
    """
    Entra in recovery mode: imposta i flag + salva il valore portafoglio
    iniziale (per l'uscita automatica). Ritorna info dell'azione.
    """
    try:
        import database
        port = database.get_portfolio()
        if not port:
            return {"ok": False, "error": "no_portfolio"}

        initial_balance = float(port.get("cash_balance") or 0)
        # Aggiungi valore posizioni (per il totale corrente in chiusura
        # del circuit breaker, post-liquidate sara' tutto cash)
        try:
            import portfolio as _portfolio
            initial_balance = float(_portfolio.calculate_total_value())
        except Exception:
            pass

        # Se la initial_balance e' bassa (post-liquidate sara' solo cash),
        # usa quel valore. NB: il punto di uscita dal recovery non e' il
        # valore PRE-liquidazione ma il valore iniziale del portafoglio
        # del bot (da settings 'initial_balance' o env).
        try:
            initial_setting = database.get_setting("initial_balance", None)
            if initial_setting is not None:
                target_for_exit = float(initial_setting)
            else:
                import os as _os
                target_for_exit = float(
                    _os.environ.get("INITIAL_PORTFOLIO_BALANCE", 100000)
                )
        except (ValueError, TypeError):
            target_for_exit = 100000.0

        now_iso = datetime.now(timezone.utc).isoformat()
        database.set_setting(SETTING_RECOVERY_MODE, "true")
        database.set_setting(SETTING_RECOVERY_ENTERED_AT, now_iso)
        database.set_setting(SETTING_RECOVERY_INITIAL_BALANCE, str(target_for_exit))

        return {
            "ok": True,
            "reason": reason,
            "entered_at": now_iso,
            "exit_target_balance": target_for_exit,
            "portfolio_value_at_entry": round(initial_balance, 2),
        }
    except Exception as e:
        logger.error("enter_recovery_mode failed: %s", e, exc_info=True)
        return {"ok": False, "error": str(e)}


def exit_recovery_mode(reason: str = "portfolio_recovered") -> dict:
    """Esce dal recovery mode (auto quando portafoglio torna >= initial)."""
    try:
        import database
        was_active = is_in_recovery_mode()
        database.set_setting(SETTING_RECOVERY_MODE, "false")
        database.set_setting(SETTING_RECOVERY_ENTERED_AT, "")
        database.set_setting(SETTING_RECOVERY_INITIAL_BALANCE, "")
        return {"ok": True, "was_active": was_active, "reason": reason}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def check_recovery_exit_condition() -> bool:
    """
    Verifica se il portafoglio e' tornato >= initial_balance. Se si',
    esce automaticamente dal recovery mode. Da chiamare periodicamente.
    """
    if not is_in_recovery_mode():
        return False
    try:
        import portfolio as _portfolio
        current = float(_portfolio.calculate_total_value())
        target = get_recovery_initial_balance() or 100000.0
        if current >= target:
            exit_recovery_mode(reason=f"portfolio_recovered({current:.2f}>={target:.2f})")
            return True
        return False
    except Exception as e:
        logger.warning("check_recovery_exit_condition: %s", e)
        return False


# ═══════════════════════════════════════════════════════════════════════
# 4. Lock-in 0.5% (a +5% di profitto)
# ═══════════════════════════════════════════════════════════════════════

def should_apply_lock_in(position: dict, current_price: float | None = None,
                         trigger_pct: float | None = None) -> bool:
    """
    True se la posizione ha raggiunto +trigger_pct% di unrealized e
    NON ha ancora uno SL >= entry (= lock-in non ancora applicato).
    """
    threshold = trigger_pct if trigger_pct is not None else DEFAULT_LOCK_IN_TRIGGER_PCT
    try:
        avg = float(position.get("avg_buy_price") or 0)
        if avg <= 0:
            return False
        cp = float(current_price or position.get("current_price") or 0)
        if cp <= 0:
            return False
        pnl_pct = (cp - avg) / avg * 100.0
        if pnl_pct < threshold:
            return False
        existing_sl = float(position.get("stop_loss_price") or 0)
        # Se gia' c'e' uno SL >= entry, il lock-in e' gia' attivo
        if existing_sl >= avg * (1 + (DEFAULT_LOCK_IN_SL_PCT / 100.0)):
            return False
        return True
    except (TypeError, ValueError):
        return False


def compute_lock_in_sl_price(position: dict,
                              sl_pct: float | None = None) -> float | None:
    """SL price = entry * (1 + sl_pct/100), default 0.5%."""
    pct = sl_pct if sl_pct is not None else DEFAULT_LOCK_IN_SL_PCT
    try:
        avg = float(position.get("avg_buy_price") or 0)
        if avg <= 0:
            return None
        return round(avg * (1 + pct / 100.0), 6)
    except (TypeError, ValueError):
        return None


def apply_lock_in_protection(position: dict, current_price: float | None = None) -> dict:
    """
    Applica lock-in se necessario. Ritorna risultato action.
    """
    if not should_apply_lock_in(position, current_price):
        return {"applied": False, "reason": "not_eligible"}
    new_sl = compute_lock_in_sl_price(position)
    if new_sl is None:
        return {"applied": False, "reason": "compute_failed"}
    ticker = position.get("ticker")
    try:
        import database
        database.update_position_auto_exit(
            ticker, stop_loss_price=new_sl, set_by="risk_state_lock_in",
        )
        return {
            "applied": True,
            "ticker": ticker,
            "new_stop_loss": new_sl,
            "lock_in_pct": DEFAULT_LOCK_IN_SL_PCT,
        }
    except Exception as e:
        return {"applied": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════
# 5. Trailing stop dinamico (3-4% dal picco intraday)
# ═══════════════════════════════════════════════════════════════════════

def get_position_peak_price(ticker: str, lookback_days: int = 7) -> float | None:
    """
    Ritorna il prezzo MAX su price_history per il ticker nelle ultime
    `lookback_days` giorni. Best-effort: se price_history non ha dati,
    fallback a current_price della posizione.
    """
    try:
        import database
        client = database.get_client() if hasattr(database, "get_client") else None
        if client:
            since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
            r = (client.table("price_history")
                 .select("price")
                 .eq("ticker", ticker.upper())
                 .gte("timestamp", since)
                 .order("price", desc=True)
                 .limit(1)
                 .execute())
            if r.data:
                return float(r.data[0].get("price") or 0)
    except Exception as e:
        logger.debug("get_position_peak_price %s failed: %s", ticker, e)
    # Fallback: prezzo corrente dalla posizione
    try:
        import database
        positions = database.get_positions() or []
        for p in positions:
            if (p.get("ticker") or "").upper() == ticker.upper():
                return float(p.get("current_price") or 0)
    except Exception:
        pass
    return None


def compute_trailing_stop_price(peak_price: float,
                                trailing_pct: float | None = None) -> float | None:
    """SL trailing = peak * (1 - trailing_pct/100)."""
    pct = trailing_pct if trailing_pct is not None else DEFAULT_TRAILING_STOP_PCT_FROM_PEAK
    if peak_price is None or peak_price <= 0:
        return None
    return round(peak_price * (1 - pct / 100.0), 6)


def apply_trailing_stop_if_needed(position: dict,
                                    trailing_pct: float | None = None,
                                    min_profit_pct: float = 5.0) -> dict:
    """
    Applica trailing stop su una posizione vincente (PnL > min_profit_pct).
    Il nuovo SL e' max(SL_corrente, peak * (1 - trailing_pct/100)) —
    il trailing si MUOVE SU, mai giu'.
    """
    try:
        avg = float(position.get("avg_buy_price") or 0)
        if avg <= 0:
            return {"applied": False, "reason": "no_avg"}
        cur = float(position.get("current_price") or 0)
        if cur <= 0:
            return {"applied": False, "reason": "no_current"}
        pnl_pct = (cur - avg) / avg * 100.0
        if pnl_pct < min_profit_pct:
            return {"applied": False, "reason": "not_profitable_enough"}

        ticker = (position.get("ticker") or "").upper()
        if not ticker:
            return {"applied": False, "reason": "no_ticker"}

        peak = get_position_peak_price(ticker)
        if not peak or peak <= 0:
            return {"applied": False, "reason": "no_peak"}

        new_sl = compute_trailing_stop_price(peak, trailing_pct)
        if not new_sl:
            return {"applied": False, "reason": "compute_failed"}

        existing_sl = float(position.get("stop_loss_price") or 0)
        # Il trailing sale, non scende. Se l'SL attuale e' gia' piu' alto,
        # NON sostituirlo (proteggi piu' del trailing standard).
        if existing_sl > 0 and existing_sl >= new_sl:
            return {"applied": False, "reason": "existing_sl_higher",
                    "existing_sl": existing_sl, "proposed_trailing_sl": new_sl}

        import database
        database.update_position_auto_exit(
            ticker, stop_loss_price=new_sl, set_by="risk_state_trailing_stop",
        )
        return {
            "applied": True,
            "ticker": ticker,
            "new_stop_loss": new_sl,
            "peak_price": peak,
            "trailing_pct": trailing_pct or DEFAULT_TRAILING_STOP_PCT_FROM_PEAK,
            "previous_sl": existing_sl,
        }
    except Exception as e:
        return {"applied": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════
# 6. Concentration risk
# ═══════════════════════════════════════════════════════════════════════

def compute_position_concentration() -> dict:
    """
    Per ogni posizione aperta calcola % del NAV totale. Ritorna:
      max_concentration_pct: float, max_ticker: str, breakdown: list[dict]
    """
    try:
        import database
        positions = database.get_positions() or []
        port = database.get_portfolio()
        cash = float((port or {}).get("cash_balance") or 0)
    except Exception:
        return {"max_concentration_pct": 0.0, "max_ticker": "",
                "breakdown": [], "total_nav": 0.0}

    breakdown = []
    pos_values: dict[str, float] = {}
    total_position_value = 0.0
    for p in positions:
        try:
            qty = float(p.get("quantity") or 0)
            cp = float(p.get("current_price") or p.get("avg_buy_price") or 0)
            if qty <= 0 or cp <= 0:
                continue
            value = qty * cp
            ticker = (p.get("ticker") or "").upper()
            pos_values[ticker] = value
            total_position_value += value
        except (TypeError, ValueError):
            continue

    total_nav = total_position_value + max(cash, 0.0)
    if total_nav <= 0:
        return {"max_concentration_pct": 0.0, "max_ticker": "",
                "breakdown": [], "total_nav": 0.0}

    max_pct = 0.0
    max_ticker = ""
    for ticker, value in pos_values.items():
        pct = value / total_nav * 100.0
        breakdown.append({"ticker": ticker, "value": round(value, 2),
                          "pct_of_nav": round(pct, 2)})
        if pct > max_pct:
            max_pct = pct
            max_ticker = ticker

    breakdown.sort(key=lambda d: d["pct_of_nav"], reverse=True)
    return {
        "max_concentration_pct": round(max_pct, 2),
        "max_ticker": max_ticker,
        "breakdown": breakdown[:10],
        "total_nav": round(total_nav, 2),
    }


def is_concentration_trigger_active(threshold_pct: float | None = None) -> tuple[bool, str, float]:
    """Ritorna (triggered, max_ticker, max_pct)."""
    thr = threshold_pct if threshold_pct is not None else DEFAULT_CONCENTRATION_TRIGGER_PCT
    info = compute_position_concentration()
    return info["max_concentration_pct"] > thr, info["max_ticker"], info["max_concentration_pct"]


# ═══════════════════════════════════════════════════════════════════════
# 7. Snapshot operativo: stato di rischio completo (per UI + prompt)
# ═══════════════════════════════════════════════════════════════════════

def build_risk_state_snapshot() -> dict:
    """
    Snapshot completo dello stato di rischio, da iniettare nel contesto
    del Decision Agent e/o esposto via /api/risk-state.
    """
    wr = get_recent_win_rate(limit=10)
    dd = compute_24h_drawdown()
    conc = compute_position_concentration()
    in_recovery = is_in_recovery_mode()
    return {
        "recovery_mode": in_recovery,
        "recovery_target_balance": get_recovery_initial_balance() if in_recovery else None,
        "drawdown_24h_pct": dd["dd_pct"],
        "running_max_24h": dd["running_max"],
        "win_rate_last_10": wr["win_rate"],
        "wr_wins": wr["wins"],
        "wr_losses": wr["losses"],
        "wr_sample_size": wr["sample_size"],
        "max_position_concentration_pct": conc["max_concentration_pct"],
        "max_concentration_ticker": conc["max_ticker"],
        "concentration_trigger_active": conc["max_concentration_pct"] > DEFAULT_CONCENTRATION_TRIGGER_PCT,
        "total_nav": conc["total_nav"],
        # Soglie correnti (per riferimento prompt)
        "thresholds": {
            "circuit_breaker_24h_pct": DEFAULT_CIRCUIT_BREAKER_24H_PCT,
            "recovery_sl_max_pct": DEFAULT_RECOVERY_SL_MAX_PCT,
            "recovery_confidence_floor": DEFAULT_RECOVERY_CONFIDENCE_FLOOR,
            "lock_in_trigger_pct": DEFAULT_LOCK_IN_TRIGGER_PCT,
            "lock_in_sl_pct": DEFAULT_LOCK_IN_SL_PCT,
            "trailing_stop_pct_from_peak": DEFAULT_TRAILING_STOP_PCT_FROM_PEAK,
            "concentration_trigger_pct": DEFAULT_CONCENTRATION_TRIGGER_PCT,
            "high_risk_sl_threshold_pct": DEFAULT_HIGH_RISK_SL_THRESHOLD_PCT,
            "high_risk_confidence_floor": DEFAULT_HIGH_RISK_CONFIDENCE_FLOOR,
        },
    }


def build_risk_state_prompt_block() -> str:
    """
    Costruisce un blocco di testo compatto da iniettare nel system prompt
    del Decision Agent. Variabile, basato sullo stato corrente.
    """
    state = build_risk_state_snapshot()
    lines = [
        "═" * 60,
        "📊  RISK STATE LIVE — variabili stateful aggiornate",
        "═" * 60,
        "",
    ]
    if state["recovery_mode"]:
        lines.extend([
            "⚠️  RECOVERY MODE ATTIVO — il portafoglio ha attivato il circuit",
            "    breaker 24h. Vincoli stringenti:",
            f"      - Stop-loss MAX: {DEFAULT_RECOVERY_SL_MAX_PCT:.1f}% (sotto questo soltanto)",
            f"      - Confidence floor: {DEFAULT_RECOVERY_CONFIDENCE_FLOOR:.2f}",
            f"      - Target uscita: portafoglio >= ${state['recovery_target_balance']:.2f}",
            "    Riduci drasticamente la frequenza di trade. Solo setup chiari.",
            "",
        ])
    else:
        lines.append("✅ Stato operativo: normale (no circuit breaker attivo)")
        lines.append("")

    lines.extend([
        f"Drawdown 24h: {state['drawdown_24h_pct']:.2f}% "
        f"(soglia circuit breaker: {DEFAULT_CIRCUIT_BREAKER_24H_PCT:.1f}%)",
        f"Win rate ultime {state['wr_sample_size']} chiusure: "
        f"{state['win_rate_last_10']*100:.1f}% "
        f"({state['wr_wins']}W / {state['wr_losses']}L)",
        f"Max concentrazione singola posizione: "
        f"{state['max_position_concentration_pct']:.1f}% "
        f"({state['max_concentration_ticker'] or '-'})",
    ])
    if state["concentration_trigger_active"]:
        lines.append(
            f"⚠️  TRIGGER CONCENTRAZIONE: una posizione supera "
            f"{DEFAULT_CONCENTRATION_TRIGGER_PCT:.0f}% del NAV — valuta rebalance."
        )
    lines.extend([
        "",
        "USO NEI PROMPT:",
        f"  - Se win_rate_last_10 > 0.60 + cash > $20k + regime BULL-CYCLE/CRASH-RALLY",
        f"    → puoi usare il CAP PIENO del tuo Risk Profile (no riduzione regime).",
        f"  - Se SL proposto > {DEFAULT_HIGH_RISK_SL_THRESHOLD_PCT:.0f}% di distanza: confidence",
        f"    minima {DEFAULT_HIGH_RISK_CONFIDENCE_FLOOR:.2f} obbligatoria (no eccezioni).",
        f"  - Lock-in 0.5% e trailing stop sono APPLICATI AUTOMATICAMENTE dal",
        f"    sistema sulle posizioni vincenti — NON sovrascriverli con SL piu'",
        f"    larghi tramite execute_trade (verrebbero rimossi al prossimo poll).",
        "═" * 60,
        "",
    ])
    return "\n".join(lines)
