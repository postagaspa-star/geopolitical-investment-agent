"""
Crypto Regime — il META-CERVELLO a 3 stati (la sintesi dei ~140 backtest).

LA LEZIONE, in una frase: nessun motore tecnico batte il "compra e tieni" in
salita, ma gli scudi annullano i crash, e nel laterale TUTTI perdono per
commissioni + falsi segnali. Quindi non si cerca un motore furbo: si sceglie
il COMPORTAMENTO giusto per il regime, e nel dubbio si sta FERMI.

STATI (decisi da Andrea):
  - UPTREND   → TIENI/LONG: compra e cavalca (come buy&hold, che in salita
                vince). Quasi tutto il capitale investito.
  - DOWNTREND → SCUDO: proteggi. Variante 'cash' (esci e basta) o 'short'
                (scommetti al ribasso). Default cash = piu' prudente.
  - SIDEWAYS  → FLAT TOTALE: chiudi TUTTO, anche il long. Cash puro. Regola di
                Andrea: nel laterale si bruciano solo soldi → zero esposizione.

ANTI-WHIPSAW: il regime non si decide su una candela. Serve ISTERESI: per
ENTRARE in un trend serve conferma forte; per USCIRNE serve che si rompa
davvero. Cosi' non si rimbalza UP<->SIDEWAYS ogni giorno (= commissioni che
uccidono, la malattia gia' vista).

Modulo PURO/DETERMINISTICO su candele giornaliere. Riusa indicatori testati.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Optional

from agents.crypto_scalper import _f, ema_series, atr

# Stati del meta-cervello
UP, DOWN, SIDE = "UPTREND", "DOWNTREND", "SIDEWAYS"
# Posizioni
FLAT, LONG, SHORT = "FLAT", "LONG", "SHORT"


@dataclass
class RegimeParams:
    ema_fast: int = 20
    ema_slow: int = 50
    atr_len: int = 20
    # Soglia di "pendenza" del trend (slope EMA lenta normalizzata, % per candela)
    # per distinguere un trend VERO da un laterale che oscilla.
    slope_min_pct: float = 0.10      # EMA slow deve salire/scendere >= 0.10%/candela
    # Distanza minima prezzo<->EMA per confermare il trend (filtra il rumore
    # quando il prezzo balla attorno alla media = laterale).
    sep_min_pct: float = 1.5         # prezzo almeno 1.5% sopra/sotto EMA slow
    # Range-detector del laterale: se il prezzo, nelle ultime range_lookback
    # candele, e' rimasto in una banda piu' stretta di range_max_pct, e' LATERALE
    # a prescindere dall'EMA (che dopo un pump resta indietro e darebbe falso UP).
    range_lookback: int = 14
    range_max_pct: float = 8.0       # banda < 8% in 14 candele = laterale
    # ISTERESI: candele di conferma per CAMBIARE stato (anti-whipsaw).
    confirm_bars: int = 2
    # Scudo in downtrend: 'cash' (esci) o 'short' (scommetti giu')
    shield: str = "cash"
    # Capitale investito quando LONG in uptrend (% NAV). "Tieni" = quasi tutto.
    long_pct_nav: float = 95.0
    short_pct_nav: float = 25.0      # se shield='short', size piu' prudente
    k_atr_sl: float = 3.0            # stop di sicurezza (anche in hold)


@dataclass
class RegimeState:
    regime: str = SIDE               # parte prudente: laterale = flat
    position: str = FLAT
    candidate: str = SIDE            # regime candidato in attesa di conferma
    candidate_count: int = 0
    entry_price: float = 0.0
    stop_loss: float = 0.0
    units: float = 0.0
    bar_index: int = -1

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RegimeAction:
    action: str          # NONE | OPEN_LONG | OPEN_SHORT | CLOSE_ALL
    reason: str = ""
    price: float = 0.0
    regime: str = SIDE
    units: float = 0.0
    stop_loss: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


def classify_regime(closes, p: RegimeParams) -> Optional[str]:
    """Classifica il regime ISTANTANEO (pre-isteresi) da prezzo + EMA + slope.
    Ritorna UP/DOWN/SIDE, oppure None se warmup insufficiente."""
    cs = [c for c in (_f(x) for x in closes) if c is not None]
    if len(cs) < p.ema_slow + 5:
        return None
    es = ema_series(cs, p.ema_slow)
    ef = ema_series(cs, p.ema_fast)[-1]
    es_now = es[-1]
    es_prev = es[-5]                 # slope su 4 candele
    if es_now is None or es_prev is None or ef is None or es_now <= 0:
        return None
    price = cs[-1]
    slope_pct = (es_now - es_prev) / es_prev / 4.0 * 100.0   # %/candela
    sep_pct = (price - es_now) / es_now * 100.0

    # RANGE-DETECTOR: se negli ultimi N giorni il prezzo e' rimasto in una
    # banda stretta, e' LATERALE — anche se l'EMA (in ritardo dopo un pump)
    # direbbe ancora trend. Questo cattura il "flat dopo la corsa".
    if len(cs) >= p.range_lookback:
        window = cs[-p.range_lookback:]
        lo, hi = min(window), max(window)
        if lo > 0 and (hi - lo) / lo * 100.0 <= p.range_max_pct:
            return SIDE

    up = (ef > es_now) and (slope_pct >= p.slope_min_pct) and (sep_pct >= p.sep_min_pct)
    down = (ef < es_now) and (slope_pct <= -p.slope_min_pct) and (sep_pct <= -p.sep_min_pct)
    if up:
        return UP
    if down:
        return DOWN
    return SIDE


def step(state: RegimeState, bars: list[dict], nav: float,
         params: Optional[RegimeParams] = None) -> RegimeAction:
    """Una candela giornaliera. Decide regime (con isteresi) e adegua la
    posizione: UP→long, DOWN→scudo, SIDE→flat totale. Muta state."""
    p = params or RegimeParams()
    state.bar_index += 1
    closes = [b.get("close") for b in bars]
    highs = [b.get("high") for b in bars]
    lows = [b.get("low") for b in bars]
    price = _f(closes[-1]) if closes else None
    none = RegimeAction(action="NONE", price=price or 0.0, regime=state.regime)
    if price is None or price <= 0:
        return none

    inst = classify_regime(closes, p)
    if inst is None:
        return none   # warmup

    # ── ISTERESI: cambia regime solo dopo confirm_bars candele concordi ──────
    if inst == state.candidate:
        state.candidate_count += 1
    else:
        state.candidate = inst
        state.candidate_count = 1
    new_regime = state.regime
    if state.candidate != state.regime and state.candidate_count >= p.confirm_bars:
        new_regime = state.candidate

    a = atr(highs, lows, closes, p.atr_len) or (price * 0.02)

    # ── Stop di sicurezza sempre attivo (anche in hold long) ─────────────────
    if state.position == LONG and state.stop_loss > 0 and price <= state.stop_loss:
        _set_flat(state)
        state.regime = new_regime
        return RegimeAction(action="CLOSE_ALL", price=price, regime=new_regime,
                            reason="stop di sicurezza colpito (hold long)")
    if state.position == SHORT and state.stop_loss > 0 and price >= state.stop_loss:
        _set_flat(state)
        state.regime = new_regime
        return RegimeAction(action="CLOSE_ALL", price=price, regime=new_regime,
                            reason="stop di sicurezza colpito (short)")

    # Se il regime non cambia, non fare nulla (mantieni la posizione corrente)
    if new_regime == state.regime:
        none.regime = new_regime
        return none

    # ── CAMBIO DI REGIME: adegua la posizione ────────────────────────────────
    old = state.regime
    state.regime = new_regime

    # Qualunque cambio parte chiudendo cio' che non e' coerente col nuovo stato.
    if new_regime == SIDE:
        # REGOLA DI ANDREA: laterale = chiudi TUTTO, cash puro.
        if state.position != FLAT:
            _set_flat(state)
            return RegimeAction(action="CLOSE_ALL", price=price, regime=SIDE,
                                reason=f"{old}→SIDEWAYS: chiudo TUTTO (cash puro, no esposizione)")
        none.regime = SIDE
        none.reason = "laterale: resto flat"
        return none

    if new_regime == UP:
        # tieni/long: se non sono gia' long, apri long quasi all-in
        if state.position == SHORT:
            _set_flat(state)
        if state.position != LONG:
            sl = price - p.k_atr_sl * a
            units = (nav * p.long_pct_nav / 100.0) / price
            _set_pos(state, LONG, price, sl, units)
            return RegimeAction(action="OPEN_LONG", price=price, regime=UP, units=units,
                                stop_loss=round(sl, 8),
                                reason=f"{old}→UPTREND: TIENI/LONG (cavalca il rialzo)")
        none.regime = UP
        return none

    if new_regime == DOWN:
        # scudo: chiudi il long; se shield=short apri uno short prudente
        if state.position == LONG:
            _set_flat(state)
        if p.shield == "short" and state.position != SHORT:
            sl = price + p.k_atr_sl * a
            units = (nav * p.short_pct_nav / 100.0) / price
            _set_pos(state, SHORT, price, sl, units)
            return RegimeAction(action="OPEN_SHORT", price=price, regime=DOWN, units=units,
                                stop_loss=round(sl, 8),
                                reason=f"{old}→DOWNTREND: SCUDO short (proteggi + scommetti giu')")
        # shield=cash: solo chiusura
        return RegimeAction(action="CLOSE_ALL", price=price, regime=DOWN,
                            reason=f"{old}→DOWNTREND: SCUDO cash (esco e aspetto)")

    none.regime = new_regime
    return none


def _set_flat(state: RegimeState):
    state.position = FLAT
    state.entry_price = 0.0
    state.stop_loss = 0.0
    state.units = 0.0


def _set_pos(state: RegimeState, side: str, price: float, sl: float, units: float):
    state.position = side
    state.entry_price = price
    state.stop_loss = round(sl, 8)
    state.units = units
