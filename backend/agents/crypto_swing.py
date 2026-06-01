"""
Crypto Swing — motore TREND-FOLLOWING bidirezionale su candele 4h/1d.

PERCHE' ESISTE (la lezione dei ~130 backtest sullo scalper):
Nessun approccio INTRADAY batte il buy&hold in uptrend — per catturare un
+28% devi TENERE la posizione attraverso i ritracciamenti, non scalparla. Lo
scalper si fa stoppare a ogni sussulto e rientra piu' in alto, erodendo il
movimento. Lo swing fa l'opposto: entra sul trend confermato e lo CAVALCA per
GIORNI con uno stop largo (Chandelier), uscendo solo quando il trend si rompe
davvero.

FILOSOFIA (trend-following classico, stile Turtle):
  - LONG quando il trend di fondo e' UP confermato (EMA veloce > lenta + prezzo
    sopra il massimo di Donchian N) → cavalca.
  - SHORT quando il trend e' DOWN confermato (specularmente) → cavalca al ribasso.
  - USCITA non su "momentum esausto" (troppo presto), ma su CHANDELIER STOP:
    trailing ad ATR dal massimo/minimo estremo dall'entrata. Largo abbastanza
    da reggere i ritracciamenti, stretto abbastanza da tagliare quando il trend
    finisce. Niente flip frenetici: tra un'uscita e un'entrata opposta puo'
    passare tempo (il trend deve riconfermarsi dall'altro lato).

Bidirezionale ma NON simmetrico forzato: lo short richiede conferma piu' forte
(le crypto hanno bias rialzista — la lezione dell'edge -18 in uptrend).

Modulo PURO/DETERMINISTICO. Opera su candele OHLCV (vecchia→recente), pensato
per 1d o 4h. step() consuma una candela e ritorna l'azione su quella chiusura.
Riusa gli indicatori testati di crypto_scalper.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Optional

from agents.crypto_scalper import _f, ema_series, atr, FLAT, LONG, SHORT


@dataclass
class SwingParams:
    # Trend di fondo
    ema_fast: int = 20         # su 1d: ~3 settimane
    ema_slow: int = 50         # su 1d: ~7 settimane
    # Breakout di Donchian: entra se il prezzo fa il massimo/minimo di N candele
    donchian_entry: int = 20   # ~20 giorni (classico Turtle)
    atr_len: int = 20
    # Chandelier stop: trailing ad ATR dall'estremo favorevole dall'entrata.
    # Largo = cavalca attraverso i ritracciamenti senza farsi stoppare.
    chandelier_k: float = 3.0
    # Asimmetria: lo short richiede un breakout di Donchian piu' lungo (conferma
    # piu' forte) per non shortare i ritracciamenti dentro al bull di fondo.
    short_donchian_mult: float = 1.5
    # Rischio
    k_atr_sl: float = 2.0      # stop iniziale (prima che il chandelier prenda il sopravvento)
    risk_per_trade_pct: float = 1.0
    max_position_pct_nav: float = 25.0
    # Filtro regime di mercato (BTC) opzionale: se fornito al chiamante.
    allow_short: bool = True


@dataclass
class SwingState:
    position: str = FLAT
    entry_price: float = 0.0
    entry_bar: int = -10_000
    stop_loss: float = 0.0
    best_price: float = 0.0    # massimo (long) / minimo (short) dall'entrata
    bar_index: int = -1
    units: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SwingAction:
    action: str                # NONE|OPEN_LONG|OPEN_SHORT|CLOSE|FLIP_TO_LONG|FLIP_TO_SHORT
    reason: str = ""
    price: float = 0.0
    stop_loss: Optional[float] = None
    units: float = 0.0
    trend: str = "?"           # up|down|flat
    atr_pct: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def _donchian(closes, n: int) -> tuple[Optional[float], Optional[float]]:
    """Massimo/minimo delle ULTIME n CHIUSURE ESCLUSA quella corrente.

    Usa i CLOSE (non high/low) per coerenza col confronto: il segnale di
    breakout confronta il close corrente col canale dei close precedenti.
    Mischiare close-vs-high introduceva un artefatto (con spread ampio il
    close corrente restava sotto il max degli high precedenti → breakout mai
    innescato anche in trend pulito)."""
    cs = [c for c in (_f(x) for x in closes) if c is not None]
    if len(cs) < n + 1:
        return None, None
    return max(cs[-n - 1:-1]), min(cs[-n - 1:-1])


def _size_units(nav, entry, sl, p: SwingParams) -> float:
    if entry <= 0 or nav <= 0:
        return 0.0
    d = abs(entry - sl)
    if d <= 0:
        return 0.0
    units = (nav * p.risk_per_trade_pct / 100.0) / d
    cap = (nav * p.max_position_pct_nav / 100.0) / entry
    return max(0.0, min(units, cap))


def step(state: SwingState, bars: list[dict], nav: float,
         params: Optional[SwingParams] = None) -> SwingAction:
    """Una candela (4h/1d). Macchina a stati trend-following con Chandelier stop.
    Muta `state`. Deterministico."""
    p = params or SwingParams()
    state.bar_index += 1

    closes = [b.get("close") for b in bars]
    highs = [b.get("high") for b in bars]
    lows = [b.get("low") for b in bars]
    price = _f(closes[-1]) if closes else None
    none = SwingAction(action="NONE", price=price or 0.0)
    if price is None or price <= 0:
        return none

    ema_f = ema_series(closes, p.ema_fast)[-1]
    ema_s = ema_series(closes, p.ema_slow)[-1]
    a = atr(highs, lows, closes, p.atr_len)
    dc_hi, dc_lo = _donchian(closes, p.donchian_entry)
    dc_hi_s, dc_lo_s = _donchian(closes, int(p.donchian_entry * p.short_donchian_mult))
    if ema_f is None or ema_s is None or a is None or dc_hi is None:
        return none   # warmup

    atr_pct = a / price * 100.0
    trend = "up" if ema_f > ema_s else ("down" if ema_f < ema_s else "flat")
    none.trend, none.atr_pct = trend, atr_pct

    # ── Gestione posizione aperta: Chandelier trailing ──────────────────────
    if state.position == LONG:
        state.best_price = max(state.best_price, price)
        chandelier = state.best_price - p.chandelier_k * a
        stop = max(state.stop_loss, chandelier)   # il trailing puo' solo salire
        state.stop_loss = stop
        if price <= stop:
            return _close(state, price, f"chandelier-stop long ({p.chandelier_k}xATR dal max)", trend, atr_pct)
        return none   # cavalca

    if state.position == SHORT:
        state.best_price = min(state.best_price, price) if state.best_price > 0 else price
        chandelier = state.best_price + p.chandelier_k * a
        stop = min(state.stop_loss, chandelier) if state.stop_loss > 0 else chandelier
        state.stop_loss = stop
        if price >= stop:
            return _close(state, price, f"chandelier-stop short ({p.chandelier_k}xATR dal min)", trend, atr_pct)
        return none

    # ── FLAT: cerca breakout di trend ───────────────────────────────────────
    # LONG: trend up + breakout sopra il canale di Donchian
    long_break = (trend == "up") and (price >= dc_hi)
    # SHORT: trend down + breakout sotto il canale (piu' lungo = conferma forte)
    short_break = (p.allow_short and trend == "down" and dc_lo_s is not None
                   and price <= dc_lo_s)

    if long_break:
        return _open(state, price, LONG, nav, p, a,
                     f"breakout LONG: trend up + max Donchian {p.donchian_entry} rotto", trend, atr_pct)
    if short_break:
        return _open(state, price, SHORT, nav, p, a,
                     f"breakdown SHORT: trend down + min Donchian rotto", trend, atr_pct)

    none.reason = "nessun breakout di trend"
    return none


def _open(state, price, side, nav, p, a, reason, trend, atr_pct) -> SwingAction:
    sl = price - p.k_atr_sl * a if side == LONG else price + p.k_atr_sl * a
    units = _size_units(nav, price, sl, p)
    if units <= 0:
        return SwingAction(action="NONE", reason="size 0", price=price, trend=trend, atr_pct=atr_pct)
    state.position = side
    state.entry_price = price
    state.entry_bar = state.bar_index
    state.stop_loss = round(sl, 8)
    state.best_price = price
    state.units = units
    return SwingAction(action="OPEN_LONG" if side == LONG else "OPEN_SHORT",
                       reason=reason, price=price, stop_loss=state.stop_loss,
                       units=units, trend=trend, atr_pct=atr_pct)


def _close(state, price, reason, trend, atr_pct) -> SwingAction:
    units = state.units
    state.position = FLAT
    state.entry_price = 0.0
    state.stop_loss = 0.0
    state.best_price = 0.0
    state.entry_bar = state.bar_index
    state.units = 0.0
    return SwingAction(action="CLOSE", reason=reason, price=price, units=units,
                       trend=trend, atr_pct=atr_pct)
