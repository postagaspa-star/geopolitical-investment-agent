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

# ── Feature flags (default OFF per evitare side-effect non voluti) ──────────
# Lock-in 0.5% e trailing stop dinamico sono OPT-IN: senza esplicita
# attivazione utente, il sistema NON tocca le posizioni vincenti.
# Razionale: il primo deploy ha generato chiusure automatiche di posizioni
# che il Decision Agent voleva mantenere (auto-exit triggherato dallo SL
# stretto del lock-in). Per default questi meccanismi sono spenti — il
# Decision Agent resta l'unica autorita' sui SL/TP delle posizioni che
# ha aperto.
SETTING_LOCK_IN_ENABLED = "risk_state_lock_in_enabled"
SETTING_TRAILING_ENABLED = "risk_state_trailing_enabled"
SETTING_CIRCUIT_BREAKER_ENABLED = "risk_state_circuit_breaker_enabled"

# Marker `auto_exit_set_by` salvati da risk_state per identificare gli SL
# che possiamo aggiornare. Gli SL settati da altri non vengono mai toccati.
SET_BY_LOCK_IN = "risk_state_lock_in"
SET_BY_TRAILING = "risk_state_trailing_stop"
RISK_STATE_SET_BY_MARKERS = (SET_BY_LOCK_IN, SET_BY_TRAILING)


def is_lock_in_enabled() -> bool:
    """Lock-in 0.5% attivo? Default False."""
    try:
        import database
        raw = (database.get_setting(SETTING_LOCK_IN_ENABLED, "false") or "").strip().lower()
        return raw in ("1", "true", "yes", "on")
    except Exception:
        return False


def is_trailing_enabled() -> bool:
    """Trailing stop dinamico attivo? Default False."""
    try:
        import database
        raw = (database.get_setting(SETTING_TRAILING_ENABLED, "false") or "").strip().lower()
        return raw in ("1", "true", "yes", "on")
    except Exception:
        return False


def is_circuit_breaker_enabled() -> bool:
    """
    Circuit breaker 24h attivo? Default False (opt-in).

    DEPRECATO IL DEFAULT TRUE: il primo deploy aveva default True e
    una corruzione di snapshot ha causato un trigger spurio con
    liquidazione totale del portafoglio. Ora default OFF.

    Anche quando ENABLED, il circuit breaker NON liquida piu' automatica-
    mente: alza solo un alert ad alta priorita' (visible in agent_logs).
    La liquidazione richiede chiamata esplicita a
    POST /api/risk-state/manual-liquidate-all dall'utente.
    """
    try:
        import database
        raw = (database.get_setting(SETTING_CIRCUIT_BREAKER_ENABLED, "false") or "").strip().lower()
        return raw in ("1", "true", "yes", "on")
    except Exception:
        return False


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

SETTING_DD_BASELINE_RESET_AT = "risk_dd_baseline_reset_at"


def _find_last_anomaly_event_ts() -> "datetime | None":
    """
    Cerca eventi di "anomalia" che invalidano gli snapshot precedenti per
    il calcolo del drawdown:
      - RECOVERY_BUY (recovery posizioni post-liquidazione)
      - RISK_STATE_LIQUIDATE (circuit breaker che ha liquidato)
      - Manual baseline reset via endpoint admin

    Ritorna il timestamp piu' recente di uno di questi eventi, o None se
    nessun evento di anomalia negli ultimi 7 giorni. Il drawdown 24h verra'
    calcolato usando come baseline il piu' recente tra
    (now - 24h) e (questo timestamp).

    Rationale: se c'e' stata una liquidazione spuria e una recovery, gli
    snapshot pre-evento riflettono uno stato "fantasma" del portfolio
    (valore artificiale) che NON deve influenzare il drawdown calcolato.
    """
    try:
        import database
        # 1. Manual baseline reset (priorita' max)
        manual_iso = database.get_setting(SETTING_DD_BASELINE_RESET_AT, "") or ""
        if manual_iso:
            try:
                manual_ts = datetime.fromisoformat(manual_iso.replace("Z", "+00:00"))
                if manual_ts.tzinfo is None:
                    manual_ts = manual_ts.replace(tzinfo=timezone.utc)
                # Considera solo se nelle ultime 7 giorni
                if (datetime.now(timezone.utc) - manual_ts).total_seconds() < 7 * 24 * 3600:
                    return manual_ts
            except Exception:
                pass

        # 2. Auto-detect RECOVERY_BUY / RISK_STATE_LIQUIDATE in agent_logs
        client = database.get_client() if hasattr(database, "get_client") else None
        if not client:
            return None
        since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        r = (client.table("agent_logs")
             .select("timestamp, phase")
             .in_("phase", ["RECOVERY_BUY", "RISK_STATE_LIQUIDATE"])
             .gte("timestamp", since)
             .order("timestamp", desc=True)
             .limit(1)
             .execute())
        if r.data:
            ts_str = r.data[0].get("timestamp")
            if ts_str:
                ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                return ts
        return None
    except Exception as e:
        logger.debug("_find_last_anomaly_event_ts fail: %s", e)
        return None


def compute_24h_drawdown() -> dict:
    """
    Ritorna {dd_pct, running_max, current_value, snapshots_in_window,
             cutoff_source}.

    dd_pct e' NEGATIVO se il portafoglio e' sotto il running max nella
    finestra di calcolo, 0 se al picco o sopra. Sempre <= 0.

    BUG FIX: la finestra di calcolo non e' piu' "ultime 24h" hard-coded.
    Se c'e' stato un evento di anomalia (RECOVERY_BUY, RISK_STATE_LIQUIDATE,
    o reset manuale) negli ultimi 7 giorni, la baseline parte da DOPO
    quell'evento. Questo evita che snapshot pre-bug del portfolio (con
    valori inflated da corruzione) inflino il running_max e diano un
    drawdown apparente che non riflette la realta'.

    Esempio: portfolio era a $107k pre-bug, dopo bug+recovery e' a $107k
    di nuovo. Senza fix, gli snapshot del crash (a $20k o simili) +
    running_max pre-bug a $107k davano un drawdown -22% spurio. Ora il
    cutoff parte dalla recovery → running_max = current = drawdown 0%.
    """
    try:
        import database
        history = database.get_portfolio_history(days=2)
    except Exception as e:
        logger.warning("compute_24h_drawdown: history read fail: %s", e)
        return {"dd_pct": 0.0, "running_max": 0.0,
                "current_value": 0.0, "snapshots_in_window": 0,
                "cutoff_source": "error"}

    if not history:
        return {"dd_pct": 0.0, "running_max": 0.0,
                "current_value": 0.0, "snapshots_in_window": 0,
                "cutoff_source": "no_history"}

    # Cutoff = max(24h_ago, ultimo_evento_anomalia + 1s).
    # Se evento di anomalia recente, baseline parte da li' (non da 24h fa).
    cutoff_24h = datetime.now(timezone.utc) - timedelta(hours=24)
    anomaly_ts = _find_last_anomaly_event_ts()
    if anomaly_ts and anomaly_ts > cutoff_24h:
        cutoff = anomaly_ts + timedelta(seconds=1)
        cutoff_source = f"anomaly_event_at_{anomaly_ts.isoformat()}"
    else:
        cutoff = cutoff_24h
        cutoff_source = "24h_default"

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
                "current_value": 0.0, "snapshots_in_window": 0,
                "cutoff_source": cutoff_source}

    running_max = max(in_window)
    current = in_window[-1]
    dd_pct = ((current - running_max) / running_max * 100.0) if running_max > 0 else 0.0
    return {
        "dd_pct": round(dd_pct, 3),
        "running_max": round(running_max, 2),
        "current_value": round(current, 2),
        "snapshots_in_window": len(in_window),
        "cutoff_source": cutoff_source,
    }


def reset_drawdown_baseline(reason: str = "manual_admin_reset") -> dict:
    """
    Resetta manualmente la baseline del drawdown 24h al momento attuale.
    Gli snapshot precedenti vengono ignorati nel calcolo del running_max.
    Utile dopo eventi anomali (bug, deposit/withdraw, manutenzione).
    """
    try:
        import database
        now_iso = datetime.now(timezone.utc).isoformat()
        database.set_setting(SETTING_DD_BASELINE_RESET_AT, now_iso)
        return {"ok": True, "baseline_reset_at": now_iso, "reason": reason}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def clear_drawdown_baseline_override() -> dict:
    """Rimuove l'override manuale del baseline (auto-detect riprende)."""
    try:
        import database
        database.set_setting(SETTING_DD_BASELINE_RESET_AT, "")
        return {"ok": True, "cleared": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


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
    NON ha ancora uno SL settato (o ha uno SL precedentemente settato
    dal risk_state stesso che possiamo aggiornare).

    GUARD CRITICO: NON sovrascrive mai SL settati da Decision Agent o
    utente (set_by != risk_state markers). Questo evita che il sistema
    chiuda automaticamente posizioni che l'agente vuole mantenere.
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

        # GUARD: se c'e' uno SL gia' settato da Decision Agent / utente,
        # NON lo tocchiamo. Solo se l'SL e' 0 (mai settato) o e' stato
        # settato da noi (risk_state) possiamo agire.
        if existing_sl > 0:
            set_by = (position.get("auto_exit_set_by") or "").strip()
            if set_by not in RISK_STATE_SET_BY_MARKERS:
                # SL settato da agent/user — non lo tocchiamo MAI.
                return False
            # SL settato da risk_state stesso: aggiorniamo solo se nuovo
            # SL sarebbe piu' alto (proteggi di piu', mai meno).
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
            ticker, stop_loss_price=new_sl, set_by=SET_BY_LOCK_IN,
        )
        return {
            "applied": True,
            "ticker": ticker,
            "new_stop_loss": new_sl,
            "lock_in_pct": DEFAULT_LOCK_IN_SL_PCT,
        }
    except Exception as e:
        return {"applied": False, "error": str(e)}


def clear_risk_state_auto_sls() -> dict:
    """
    Rimuove TUTTI gli SL che sono stati settati automaticamente da
    risk_state (lock-in o trailing). Le posizioni tornano a non avere
    SL automatico — il Decision Agent / utente puo' rimettere il proprio.

    Da chiamare DOPO aver disabilitato lock_in/trailing per liberare
    le posizioni che il sistema aveva preso in gestione.

    Ritorna {cleared: int, tickers: [str]}.
    """
    try:
        import database
        positions = database.get_positions() or []
    except Exception as e:
        return {"cleared": 0, "error": str(e)}

    cleared_tickers: list[str] = []
    for p in positions:
        try:
            set_by = (p.get("auto_exit_set_by") or "").strip()
            if set_by in RISK_STATE_SET_BY_MARKERS:
                ticker = p.get("ticker")
                if not ticker:
                    continue
                # Reset SL a 0 (= no auto-exit). NON tocchiamo il TP.
                database.update_position_auto_exit(
                    ticker, stop_loss_price=0.0,
                    set_by="risk_state_cleared",
                )
                cleared_tickers.append(ticker)
        except Exception as e:
            logger.debug("clear_risk_state_auto_sls per-position fail: %s", e)
            continue
    return {"cleared": len(cleared_tickers), "tickers": cleared_tickers}


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

        # GUARD CRITICO: non sovrascrivere mai SL settato da Decision Agent
        # o utente. Solo SL settati da risk_state (o assenti) possono essere
        # aggiornati.
        if existing_sl > 0:
            set_by = (position.get("auto_exit_set_by") or "").strip()
            if set_by not in RISK_STATE_SET_BY_MARKERS:
                return {"applied": False, "reason": "sl_set_by_agent_or_user",
                        "set_by": set_by, "existing_sl": existing_sl}
            # Il trailing sale, non scende. Se l'SL attuale e' gia' piu' alto,
            # NON sostituirlo (proteggi piu' del trailing standard).
            if existing_sl >= new_sl:
                return {"applied": False, "reason": "existing_sl_higher",
                        "existing_sl": existing_sl, "proposed_trailing_sl": new_sl}

        import database
        database.update_position_auto_exit(
            ticker, stop_loss_price=new_sl, set_by=SET_BY_TRAILING,
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
        # Feature flags correnti (per UI/diagnostica)
        "flags": {
            "lock_in_enabled": is_lock_in_enabled(),
            "trailing_enabled": is_trailing_enabled(),
            "circuit_breaker_enabled": is_circuit_breaker_enabled(),
        },
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
    # Stato dei feature flag (opt-in per default OFF)
    try:
        lock_in_on = is_lock_in_enabled()
    except Exception:
        lock_in_on = False
    try:
        trailing_on = is_trailing_enabled()
    except Exception:
        trailing_on = False
    try:
        cb_on = is_circuit_breaker_enabled()
    except Exception:
        cb_on = True

    lines.extend([
        "",
        "FEATURE FLAGS automatici (governano cosa il sistema fa SENZA chiederti):",
        f"  - Circuit breaker 24h: {'ATTIVO' if cb_on else 'DISATTIVO'} "
        f"(liquida tutto se DD 24h > {abs(DEFAULT_CIRCUIT_BREAKER_24H_PCT):.0f}%)",
        f"  - Lock-in 0.5% automatico: {'ATTIVO' if lock_in_on else 'DISATTIVO (default)'}",
        f"  - Trailing stop dinamico: {'ATTIVO' if trailing_on else 'DISATTIVO (default)'}",
        "",
        "USO NEI PROMPT:",
        f"  - Se win_rate_last_10 > 0.60 + cash > $20k + regime BULL-CYCLE/CRASH-RALLY",
        f"    → puoi usare il CAP PIENO del tuo Risk Profile (no riduzione regime).",
        f"  - Se SL proposto > {DEFAULT_HIGH_RISK_SL_THRESHOLD_PCT:.0f}% di distanza: confidence",
        f"    minima {DEFAULT_HIGH_RISK_CONFIDENCE_FLOOR:.2f} obbligatoria (no eccezioni).",
    ])
    if lock_in_on or trailing_on:
        lines.extend([
            f"  - Lock-in/trailing AUTO sono attivi: il sistema potrebbe aggiornare lo SL",
            f"    di una posizione VINCENTE (>+5% o >+10%) ma SOLO se lo SL non e' gia'",
            f"    settato da te. I tuoi SL espliciti via execute_trade sono SEMPRE",
            f"    rispettati e non vengono sovrascritti.",
        ])
    else:
        lines.append(
            "  - Lock-in/trailing AUTO DISATTIVATI: tu sei l'unica autorita' sui SL"
        )
    lines.extend([
        "═" * 60,
        "",
    ])
    return "\n".join(lines)
