"""
Crypto Scalper — motore intraday momentum-reversal con AUTO-FLIP long<->short.

QUESTO E' CIO' CHE ANDREA VOLEVA DAVVERO:
  "alta volatilita', c'e' un momentum di qualche minuto/ora bullish, lo sfrutto,
   poi capisce il picco, chiude la posizione long e ne apre una short, finche'
   il trend non si riassesta."

Quindi NON e' il core swing/posizionale (crypto_signal_core, candele giornaliere,
1 run/ora). Questo legge candele intraday (5-15m), valuta lo SLANCIO in tempo
quasi-reale, e gestisce una macchina a stati:

    FLAT ──(momentum bullish + vol alta)──> LONG
    FLAT ──(momentum bearish + vol alta)──> SHORT
    LONG ──(momentum esausto / inversione)──> CLOSE → (se inversione forte) FLIP a SHORT
    SHORT ──(momentum esausto / inversione)──> CLOSE → (se inversione forte) FLIP a LONG
    LONG/SHORT ──(volatilita' crollata = trend "riassestato")──> CLOSE → FLAT
    LONG/SHORT ──(stop-loss ATR colpito)──> CLOSE → FLAT

PRINCIPI ONESTI (niente magia):
  - NON prende il picco esatto: rileva il DECADIMENTO del momentum + una CONFERMA
    di inversione. Per costruzione entra/esce in lieve ritardo. E' fisica del
    mercato, non un difetto risolvibile.
  - Il nemico e' il LATERALE: flippare senza trend = morte per mille tagli
    (falsi segnali + 10bps/lato di commissioni). Per questo c'e' un VOL-GATE:
    si opera SOLO quando la volatilita'/spinta supera una soglia; sotto soglia
    si va FLAT e si aspetta ("finche' il trend non si riassesta").
  - Stop-loss ATR SEMPRE presente: e' uno scalper, le perdite vanno tagliate corte.

Modulo PURO e DETERMINISTICO: nessun I/O, nessun LLM, nessuna rete. Stessa
funzione su stessi input → stesso output. Backtestabile bar-per-bar su klines
storici reali (vedi simulator/backtest_scalper.py).

Unita' di misura: lavora su una lista di candele OHLCV (la piu' vecchia per
prima), tipicamente 5m o 15m. step() consuma UNA candela alla volta e ritorna
l'azione da prendere su quella chiusura.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


# ─── Parametri (tunabili; default tarati per 5-15m crypto) ───────────────────

@dataclass
class ScalperParams:
    # Finestre indicatori (in numero di candele)
    ema_fast: int = 9
    ema_slow: int = 21
    rsi_len: int = 14
    roc_len: int = 6           # momentum: rate-of-change su N candele
    atr_len: int = 14

    # Vol-gate: si OPERA solo se la volatilita' normalizzata (ATR/prezzo) e'
    # sopra questa soglia. Sotto → FLAT ("trend riassestato"). Tipico crypto 5m.
    vol_gate_atr_pct: float = 0.35   # 0.35% ATR/price per candela

    # Ingresso: forza minima dello slancio per aprire
    entry_roc_pct: float = 0.40      # |ROC| minimo (%) per considerare "spinta"
    entry_rsi_long: float = 52.0     # RSI sopra → bias long
    entry_rsi_short: float = 48.0    # RSI sotto → bias short

    # Uscita / inversione: il momentum e' "esausto" se ROC rientra sotto questa
    # frazione del livello d'ingresso, o se l'EMA veloce ri-incrocia la lenta.
    exhaust_roc_frac: float = 0.30   # ROC sceso sotto 30% del picco osservato
    # Inversione "forte" che giustifica il FLIP immediato (non solo CLOSE):
    flip_roc_pct: float = 0.35       # ROC opposto >= questa soglia
    flip_needs_ema_cross: bool = True  # il flip richiede anche EMA cross opposto

    # Rischio
    k_atr_sl: float = 1.2            # stop-loss = k*ATR dall'entry
    k_atr_trail: float = 1.5         # trailing stop in ATR (protezione profitto)
    risk_per_trade_pct: float = 0.5  # % NAV rischiata per trade
    max_position_pct_nav: float = 10.0

    # Anti-overtrading: minimo numero di candele tra due aperture sullo stesso
    # ticker (evita il flip-flop frenetico in micro-rumore).
    min_bars_between_entries: int = 2

    # ── MIGLIORIE v2 (default = comportamento baseline preservato) ───────────

    # (M2) Filtro di trend SUPERIORE: EMA lunga sulla stessa serie come proxy
    # del trend di fondo (su 5m, EMA200 ~ trend a ~16h). Usato come BIAS, non
    # come veto duro. 0 = disattivato (baseline).
    trend_bias_ema: int = 0
    # (M1) Asimmetria long/short: la soglia d'ingresso SHORT e' moltiplicata
    # per questo fattore (>1 = piu' difficile shortare). Le crypto hanno bias
    # rialzista di fondo: shortare ogni ritracciamento e' la causa del -15 edge
    # in uptrend. 1.0 = simmetrico (baseline).
    short_roc_mult: float = 1.0
    # Contro-bias: se il trend di fondo e' UP, lo short richiede un extra di
    # momentum (e viceversa). 0 = nessun contro-bias (baseline).
    counter_trend_roc_extra: float = 0.0

    # (M3) Conviction sizing: la size scala con la forza dello slancio (ROC vs
    # soglia) e con la volatilita'. False = size fissa risk-based (baseline).
    conviction_sizing: bool = False
    conviction_max_mult: float = 1.0   # moltiplicatore max della size (1 = off)

    # (M1-aggr) Pyramiding: aggiungi alla posizione vincente se lo slancio
    # accelera. False = una gamba sola (baseline).
    allow_pyramiding: bool = False
    max_pyramid_adds: int = 0
    pyramid_roc_step: float = 0.50     # ROC extra richiesto per ogni add

    # (M-aggr) Flip "forte" multi-segnale: oltre a ROC+EMA, richiede anche RSI
    # oltre soglia opposta. Rende il flip piu' affidabile (meno falsi). False
    # = solo ROC+EMA (baseline).
    flip_needs_rsi: bool = False
    flip_rsi_long: float = 55.0
    flip_rsi_short: float = 45.0

    # (M-aggr) Lascia correre i profitti: in modalita' aggressiva alziamo
    # exhaust_roc_frac (esce piu' tardi) e allarghiamo il trailing.

    @classmethod
    def aggressive(cls) -> "ScalperParams":
        """Profilo AGGRESSIVO richiesto da Andrea: entra deciso sugli sbalzi
        brevi, carica capitale con conviction, lascia correre, flippa su
        inversione FORTE multi-segnale, sempre attivo su entrambe le direzioni.
        L'asimmetria + filtro trend evitano di shortare dentro ai rialzi."""
        return cls(
            # entrata piu' rapida su finestre brevi (sbalzi di pochi minuti)
            roc_len=3,
            ema_fast=5, ema_slow=13,
            vol_gate_atr_pct=0.25,        # piu' permissivo: piu' finestre
            entry_roc_pct=0.30,           # entra prima
            # asimmetria: short piu' selettivo (bias rialzista crypto)
            short_roc_mult=1.6,
            counter_trend_roc_extra=0.40,
            trend_bias_ema=200,
            # carica capitale con conviction
            conviction_sizing=True,
            conviction_max_mult=2.5,
            risk_per_trade_pct=0.8,
            max_position_pct_nav=40.0,    # "molto capitale" (cap esplorato nel sweep)
            # lascia correre i profitti
            exhaust_roc_frac=0.12,        # esce molto piu' tardi
            k_atr_trail=2.5,              # trailing largo: cavalca il movimento
            k_atr_sl=1.5,
            # flip deciso ma confermato (multi-segnale = "segnale forte")
            flip_roc_pct=0.30,
            flip_needs_ema_cross=True,
            flip_needs_rsi=True,
            # pyramiding: aggiunge al vincente
            allow_pyramiding=True,
            max_pyramid_adds=2,
            pyramid_roc_step=0.50,
            # reattivo: meno attesa tra i trade
            min_bars_between_entries=1,
        )


# ─── Stato della macchina (un'istanza per ticker) ────────────────────────────

FLAT, LONG, SHORT = "FLAT", "LONG", "SHORT"


@dataclass
class ScalperState:
    position: str = FLAT
    entry_price: float = 0.0
    entry_bar: int = -10_000
    stop_loss: float = 0.0
    peak_roc: float = 0.0            # massimo |ROC| visto da quando si e' aperto
    best_price: float = 0.0          # estremo favorevole (per trailing)
    bar_index: int = -1
    units: float = 0.0
    pyramid_adds: int = 0            # quante volte si e' aggiunto al vincente

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScalperAction:
    """Cosa fare alla chiusura della candela corrente."""
    action: str                      # "NONE"|"OPEN_LONG"|"OPEN_SHORT"|"CLOSE"|"FLIP_TO_LONG"|"FLIP_TO_SHORT"
    reason: str = ""
    price: float = 0.0
    stop_loss: Optional[float] = None
    units: float = 0.0
    # diagnostica
    roc: float = 0.0
    atr_pct: float = 0.0
    rsi: float = 0.0
    regime: str = "?"                # "trending" | "quiet"

    def to_dict(self) -> dict:
        return asdict(self)


# ─── Indicatori puri ─────────────────────────────────────────────────────────

def _f(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def ema_series(values: list[float], period: int) -> list[Optional[float]]:
    out: list[Optional[float]] = []
    k = 2.0 / (period + 1.0)
    e: Optional[float] = None
    for v in values:
        fv = _f(v)
        if fv is None:
            out.append(e)
            continue
        e = fv if e is None else (fv * k + e * (1.0 - k))
        out.append(e)
    return out


def rsi(closes: list[float], period: int) -> Optional[float]:
    cs = [c for c in (_f(x) for x in closes) if c is not None]
    if len(cs) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    # Wilder smoothing semplificato sull'ultima finestra
    for i in range(len(cs) - period, len(cs)):
        ch = cs[i] - cs[i - 1]
        if ch >= 0:
            gains += ch
        else:
            losses -= ch
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return 100.0 - (100.0 / (1.0 + rs))


def atr(highs: list[float], lows: list[float], closes: list[float],
        period: int) -> Optional[float]:
    n = len(closes)
    if n < period + 1:
        return None
    trs = []
    for i in range(1, n):
        h, l, pc = _f(highs[i]), _f(lows[i]), _f(closes[i - 1])
        if h is None or l is None or pc is None:
            continue
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def roc(closes: list[float], period: int) -> Optional[float]:
    """Rate of change % su `period` candele = momentum."""
    cs = [c for c in (_f(x) for x in closes) if c is not None]
    if len(cs) < period + 1:
        return None
    past = cs[-period - 1]
    if past == 0:
        return None
    return (cs[-1] - past) / past * 100.0


# ─── Sizing ──────────────────────────────────────────────────────────────────

def _size_units(nav: float, entry: float, sl: float, p: ScalperParams,
                conviction: float = 1.0) -> float:
    """Size risk-based. Se conviction_sizing e' attivo, scala la size col
    `conviction` (1.0 = base; fino a conviction_max_mult). Sempre cappata dal
    max_position_pct_nav (il "molto capitale" ha comunque un tetto)."""
    if entry <= 0 or nav <= 0:
        return 0.0
    sl_dist = abs(entry - sl)
    if sl_dist <= 0:
        return 0.0
    risk_usd = nav * p.risk_per_trade_pct / 100.0
    if p.conviction_sizing:
        mult = max(1.0, min(conviction, p.conviction_max_mult))
        risk_usd *= mult
    units = risk_usd / sl_dist
    max_units = (nav * p.max_position_pct_nav / 100.0) / entry
    return max(0.0, min(units, max_units))


def _conviction_from_roc(r: float, entry_roc: float, max_mult: float) -> float:
    """Conviction = quanto lo slancio supera la soglia d'ingresso. ROC al
    doppio della soglia → conviction ~2. Cappata a max_mult."""
    if entry_roc <= 0:
        return 1.0
    raw = abs(r) / entry_roc
    return max(1.0, min(raw, max_mult))


# ─── Core: valuta UNA candela e decide l'azione ──────────────────────────────

def step(state: ScalperState, bars: list[dict], nav: float,
         params: Optional[ScalperParams] = None) -> ScalperAction:
    """Consuma la storia `bars` (OHLCV, vecchia→recente) fino alla candela
    corrente (l'ultima della lista) e decide l'azione su quella chiusura.

    `state` viene MUTATO per riflettere la nuova posizione (cosi' il backtest
    e il live condividono identica logica). Ritorna ScalperAction.
    """
    p = params or ScalperParams()
    state.bar_index += 1
    i = state.bar_index

    closes = [b.get("close") for b in bars]
    highs = [b.get("high") for b in bars]
    lows = [b.get("low") for b in bars]
    price = _f(closes[-1]) if closes else None

    none = ScalperAction(action="NONE", price=price or 0.0)
    if price is None or price <= 0:
        return none

    ema_f = ema_series(closes, p.ema_fast)[-1]
    ema_s = ema_series(closes, p.ema_slow)[-1]
    r = roc(closes, p.roc_len)
    a = atr(highs, lows, closes, p.atr_len)
    rs = rsi(closes, p.rsi_len)
    if ema_f is None or ema_s is None or r is None or a is None or rs is None:
        return none   # warmup: dati insufficienti, non operare

    atr_pct = a / price * 100.0
    trending = atr_pct >= p.vol_gate_atr_pct
    regime = "trending" if trending else "quiet"
    none.roc, none.atr_pct, none.rsi, none.regime = r, atr_pct, rs, regime

    ema_bull = ema_f > ema_s
    ema_bear = ema_f < ema_s

    # (M2) Trend di fondo: prezzo vs EMA lunga. Bias, non veto.
    trend_up = trend_down = False
    if p.trend_bias_ema and len(closes) >= p.trend_bias_ema:
        ema_trend = ema_series(closes, p.trend_bias_ema)[-1]
        if ema_trend is not None:
            trend_up = price > ema_trend
            trend_down = price < ema_trend

    # (M1) Soglia short asimmetrica + contro-bias di trend. Le crypto hanno
    # bias rialzista: per shortare serve piu' slancio, e ANCORA di piu' se il
    # trend di fondo e' UP (non shortare i ritracciamenti dentro un rialzo).
    short_thr = p.entry_roc_pct * p.short_roc_mult
    long_thr = p.entry_roc_pct
    if trend_up:
        short_thr += p.counter_trend_roc_extra
    if trend_down:
        long_thr += p.counter_trend_roc_extra

    # ── Gestione posizione aperta ────────────────────────────────────────────
    if state.position in (LONG, SHORT):
        is_long = state.position == LONG
        # aggiorna estremo favorevole + peak momentum
        if is_long:
            state.best_price = max(state.best_price, price)
        else:
            state.best_price = min(state.best_price, price) if state.best_price > 0 else price
        state.peak_roc = max(state.peak_roc, abs(r))

        # 1) STOP-LOSS ATR colpito
        if is_long and price <= state.stop_loss:
            return _close(state, price, "stop-loss long colpito", r, atr_pct, rs, regime)
        if (not is_long) and price >= state.stop_loss:
            return _close(state, price, "stop-loss short colpito", r, atr_pct, rs, regime)

        # 2) TRAILING stop (protegge il profitto in ATR)
        if is_long:
            trail = state.best_price - p.k_atr_trail * a
            if price <= trail and price > state.entry_price:
                return _close(state, price, "trailing-stop long (profitto protetto)", r, atr_pct, rs, regime)
        else:
            trail = state.best_price + p.k_atr_trail * a
            if price >= trail and price < state.entry_price:
                return _close(state, price, "trailing-stop short (profitto protetto)", r, atr_pct, rs, regime)

        # 3) Volatilita' crollata → il trend "si e' riassestato": esci e aspetta
        if not trending:
            return _close(state, price, "volatilita' rientrata: trend riassestato → FLAT", r, atr_pct, rs, regime)

        # 4) INVERSIONE FORTE in direzione opposta → FLIP ("toglie tutto e
        #    mette short/long"). "Forte" = multi-segnale: ROC oltre soglia +
        #    EMA cross + (opz.) RSI oltre soglia opposta. Cosi' scatta sulle
        #    VERE inversioni, non a ogni sussulto.
        if is_long:
            strong_rev = (r <= -p.flip_roc_pct)
            if p.flip_needs_ema_cross:
                strong_rev = strong_rev and ema_bear
            if p.flip_needs_rsi:
                strong_rev = strong_rev and (rs <= p.flip_rsi_short)
            if strong_rev:
                return _flip(state, price, SHORT, nav, p,
                             f"inversione bearish FORTE (ROC {r:.2f}%, RSI {rs:.0f}) → FLIP a SHORT",
                             r, atr_pct, rs, regime)
        else:
            strong_rev = (r >= p.flip_roc_pct)
            if p.flip_needs_ema_cross:
                strong_rev = strong_rev and ema_bull
            if p.flip_needs_rsi:
                strong_rev = strong_rev and (rs >= p.flip_rsi_long)
            if strong_rev:
                return _flip(state, price, LONG, nav, p,
                             f"inversione bullish FORTE (ROC {r:.2f}%, RSI {rs:.0f}) → FLIP a LONG",
                             r, atr_pct, rs, regime)

        # 5) MOMENTUM ESAUSTO (il "picco"): ROC sceso sotto frazione del picco,
        #    oppure EMA veloce ha ri-incrociato contro la posizione → CLOSE
        exhausted = abs(r) <= p.exhaust_roc_frac * max(state.peak_roc, 1e-9)
        ema_against = (is_long and ema_bear) or ((not is_long) and ema_bull)
        if exhausted or ema_against:
            why = "momentum esaurito (picco)" if exhausted else "EMA cross contrario"
            return _close(state, price, f"{why} → CLOSE", r, atr_pct, rs, regime)

        # 6) PYRAMIDING: lo slancio ACCELERA nella direzione della posizione →
        #    aggiungi capitale al vincente (sfrutta al massimo il movimento).
        if p.allow_pyramiding and state.pyramid_adds < p.max_pyramid_adds:
            accel = abs(r) >= state.peak_roc + p.pyramid_roc_step
            aligned = (is_long and r > 0 and ema_bull) or ((not is_long) and r < 0 and ema_bear)
            if accel and aligned and (i - state.entry_bar) >= 1:
                conv = _conviction_from_roc(r, p.entry_roc_pct, p.conviction_max_mult)
                add_units = _size_units(nav, price, state.stop_loss, p, conviction=conv) * 0.5
                if add_units > 0:
                    state.units += add_units
                    state.pyramid_adds += 1
                    state.peak_roc = abs(r)
                    return ScalperAction(
                        action="ADD_LONG" if is_long else "ADD_SHORT",
                        reason=f"pyramiding #{state.pyramid_adds}: slancio accelera (ROC {r:.2f}%)",
                        price=price, stop_loss=state.stop_loss, units=add_units,
                        roc=r, atr_pct=atr_pct, rsi=rs, regime=regime)

        return none   # posizione sana, mantieni

    # ── FLAT: valuta apertura ────────────────────────────────────────────────
    # Vol-gate: in mercato quieto si resta fermi
    if not trending:
        none.reason = "quiet: vol sotto soglia, attendo spinta"
        return none
    # Anti-overtrading
    if i - state.entry_bar < p.min_bars_between_entries:
        none.reason = "cooldown anti-overtrading"
        return none

    # Soglie asimmetriche (long_thr/short_thr calcolate sopra col trend-bias)
    long_ok = (r >= long_thr) and ema_bull and (rs >= p.entry_rsi_long)
    short_ok = (r <= -short_thr) and ema_bear and (rs <= p.entry_rsi_short)

    if long_ok:
        conv = _conviction_from_roc(r, long_thr, p.conviction_max_mult)
        return _open(state, price, LONG, nav, p,
                     f"momentum bullish (ROC {r:.2f}%, RSI {rs:.0f}, vol {atr_pct:.2f}%, conv {conv:.1f}x)",
                     r, atr_pct, rs, regime, a, conviction=conv)
    if short_ok:
        conv = _conviction_from_roc(r, short_thr, p.conviction_max_mult)
        return _open(state, price, SHORT, nav, p,
                     f"momentum bearish (ROC {r:.2f}%, RSI {rs:.0f}, vol {atr_pct:.2f}%, conv {conv:.1f}x)",
                     r, atr_pct, rs, regime, a, conviction=conv)

    none.reason = "spinta presente ma direzione non confermata"
    return none


# ─── Helper di transizione (mutano lo stato) ─────────────────────────────────

def _atr_now(state: ScalperState, price: float, p: ScalperParams) -> float:
    # distanza SL gia' nota dal momento dell'apertura: ricaviamo l'ATR implicito
    return abs(price - state.stop_loss) / p.k_atr_sl if state.stop_loss else 0.0


def _open(state, price, side, nav, p, reason, r, atr_pct, rs, regime, a,
          conviction: float = 1.0) -> ScalperAction:
    sl = price - p.k_atr_sl * a if side == LONG else price + p.k_atr_sl * a
    units = _size_units(nav, price, sl, p, conviction=conviction)
    if units <= 0:
        return ScalperAction(action="NONE", reason="size 0 (NAV/SL invalido)",
                             price=price, roc=r, atr_pct=atr_pct, rsi=rs, regime=regime)
    state.position = side
    state.entry_price = price
    state.entry_bar = state.bar_index
    state.stop_loss = round(sl, 8)
    state.peak_roc = abs(r)
    state.best_price = price
    state.units = units
    state.pyramid_adds = 0
    return ScalperAction(
        action="OPEN_LONG" if side == LONG else "OPEN_SHORT",
        reason=reason, price=price, stop_loss=state.stop_loss, units=units,
        roc=r, atr_pct=atr_pct, rsi=rs, regime=regime)


def _close(state, price, reason, r, atr_pct, rs, regime) -> ScalperAction:
    units = state.units
    state.position = FLAT
    state.entry_price = 0.0
    state.stop_loss = 0.0
    state.peak_roc = 0.0
    state.best_price = 0.0
    state.entry_bar = state.bar_index
    state.units = 0.0
    state.pyramid_adds = 0
    return ScalperAction(action="CLOSE", reason=reason, price=price, units=units,
                         roc=r, atr_pct=atr_pct, rsi=rs, regime=regime)


def _flip(state, price, new_side, nav, p, reason, r, atr_pct, rs, regime) -> ScalperAction:
    # chiudi la vecchia, apri la nuova nella stessa candela
    closed_units = state.units
    # ricostruisci ATR dall'SL corrente per dimensionare la nuova gamba
    a = _atr_now(state, state.entry_price or price, p) or (abs(price) * p.vol_gate_atr_pct / 100.0)
    sl = price - p.k_atr_sl * a if new_side == LONG else price + p.k_atr_sl * a
    conv = _conviction_from_roc(r, p.entry_roc_pct, p.conviction_max_mult)
    units = _size_units(nav, price, sl, p, conviction=conv)
    state.position = new_side
    state.entry_price = price
    state.entry_bar = state.bar_index
    state.stop_loss = round(sl, 8)
    state.peak_roc = abs(r)
    state.best_price = price
    state.units = units
    state.pyramid_adds = 0
    return ScalperAction(
        action="FLIP_TO_LONG" if new_side == LONG else "FLIP_TO_SHORT",
        reason=reason, price=price, stop_loss=state.stop_loss, units=units,
        roc=r, atr_pct=atr_pct, rsi=rs, regime=regime)


# ─── Persistenza stato per ticker (cross-run nel live) ───────────────────────
# Il job gira ogni ~5 min e ogni volta ricostruisce lo ScalperState dal DB:
# la macchina a stati deve "ricordare" se e' LONG/SHORT/FLAT tra un tick e
# l'altro. Salviamo come setting JSON namespaced "_scalper::state::{ticker}".

_STATE_KEY = "_scalper::state::{ticker}"


def load_state(ticker: str) -> ScalperState:
    try:
        import database, json as _j
        raw = database.get_setting(_STATE_KEY.format(ticker=ticker.upper()), "")
        if raw:
            d = _j.loads(raw) if isinstance(raw, str) else raw
            st = ScalperState()
            for k, v in (d or {}).items():
                if hasattr(st, k):
                    setattr(st, k, v)
            return st
    except Exception:
        pass
    return ScalperState()


def save_state(ticker: str, state: ScalperState) -> None:
    try:
        import database, json as _j
        database.set_setting(_STATE_KEY.format(ticker=ticker.upper()),
                             _j.dumps(state.to_dict(), default=str))
    except Exception:
        pass


# ─── Entry point LIVE (flag-gated) ───────────────────────────────────────────
# Watchlist di default: i major piu' liquidi (book profondo → slippage minimo,
# essenziale per uno scalper). Override via setting 'scalper_watchlist' (CSV).
_DEFAULT_WATCHLIST = ["BTC-USD", "ETH-USD", "SOL-USD"]
SETTING_ENABLED = "crypto_scalper_enabled"     # default OFF
SETTING_WATCHLIST = "scalper_watchlist"
SETTING_INTERVAL = "scalper_interval"          # "5m" | "15m"


def _is_enabled() -> bool:
    try:
        import database
        return (database.get_setting(SETTING_ENABLED, "false") or "false").strip().lower() == "true"
    except Exception:
        return False


def _watchlist() -> list[str]:
    try:
        import database
        raw = (database.get_setting(SETTING_WATCHLIST, "") or "").strip()
        if raw:
            return [t.strip().upper() for t in raw.split(",") if t.strip()]
    except Exception:
        pass
    return list(_DEFAULT_WATCHLIST)


async def run_crypto_scalper(run_id: str | None = None) -> dict:
    """Un tick dello scalper: per ogni ticker in watchlist, scarica le candele
    intraday recenti, fa avanzare la macchina a stati di UNA candela, ed esegue
    l'azione (apri/chiudi/flip) in autonomia tramite portfolio.execute_*.

    Flag-gated: no-op se settings['crypto_scalper_enabled'] != 'true'.
    Best-effort per ticker: un errore su uno non blocca gli altri.
    Ritorna un report con le azioni intraprese.
    """
    from uuid import uuid4
    rid = run_id or str(uuid4())[:8]
    if not _is_enabled():
        return {"skipped": True, "reason": "scalper disabilitato (flag OFF)"}

    import database
    import portfolio
    try:
        import data_fetchers
    except Exception as e:
        return {"skipped": True, "reason": f"data_fetchers non disponibile: {e}"}

    interval = (database.get_setting(SETTING_INTERVAL, "5m") or "5m").strip()
    params = ScalperParams()
    actions_taken = []

    # NAV corrente (per il sizing)
    try:
        ps = portfolio.get_portfolio_state()
        nav = float(ps.get("total_value") or ps.get("cash") or 0)
    except Exception:
        nav = 0.0

    for ticker in _watchlist():
        try:
            bars = _fetch_intraday_bars(data_fetchers, ticker, interval, limit=120)
            if not bars or len(bars) < 40:
                continue
            st = load_state(ticker)
            # allinea bar_index al numero di candele viste (la macchina conta
            # internamente; per il cooldown anti-overtrading basta la coerenza
            # relativa tra tick, quindi ripartiamo dall'indice salvato).
            act = step(st, bars, nav=nav, params=params)
            save_state(ticker, st)

            if act.action == "NONE":
                continue

            price = act.price
            res = await _execute_scalper_action(portfolio, database, rid, ticker, act)
            actions_taken.append({"ticker": ticker, "action": act.action,
                                   "price": price, "reason": act.reason,
                                   "ok": res})
            database.insert_agent_log(rid, "CRYPTO_SCALPER_ACTION", _json_dump({
                "ticker": ticker, "action": act.action, "price": price,
                "stop_loss": act.stop_loss, "units": act.units,
                "roc": act.roc, "atr_pct": act.atr_pct, "rsi": act.rsi,
                "regime": act.regime, "reason": act.reason,
            }))
        except Exception as e:
            logger_warn(f"[SCALPER {rid}] {ticker} errore: {e}")
            continue

    return {"skipped": False, "run_id": rid, "interval": interval,
            "actions": actions_taken, "n_actions": len(actions_taken)}


def _fetch_intraday_bars(data_fetchers, ticker: str, interval: str,
                          limit: int = 120) -> list[dict]:
    """Candele intraday recenti via Binance klines (5m/15m). Riusa l'helper
    sincrono se presente; altrimenti fa una fetch diretta."""
    sym = ticker.upper().replace("X:", "").replace("-", "")
    if sym.endswith("USD") and not sym.endswith("USDT"):
        sym = sym[:-3] + "USDT"
    import urllib.parse, urllib.request, json as _j
    params = urllib.parse.urlencode({"symbol": sym, "interval": interval,
                                     "limit": min(max(limit, 50), 1000)})
    url = f"https://api.binance.com/api/v3/klines?{params}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "geoinvest-scalper"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            arr = _j.loads(resp.read().decode())
        out = []
        for k in arr:
            out.append({"open": float(k[1]), "high": float(k[2]),
                        "low": float(k[3]), "close": float(k[4]),
                        "volume": float(k[5])})
        return out
    except Exception:
        return []


async def _execute_scalper_action(portfolio, database, run_id, ticker, act) -> bool:
    """Traduce l'azione della macchina in execute_* del portfolio, entro i
    governatori di rischio esistenti. Ritorna True se eseguito."""
    import asyncio
    price = act.price
    units = act.units
    note = f"[SCALPER] {act.reason}"

    def _buy():
        return portfolio.execute_buy(ticker, units, price, "(scalper)", note, 70)

    def _sell_all():
        pos = _current_qty(database, ticker)
        return portfolio.execute_sell(ticker, pos, price, "(scalper)", note, 70) if pos > 0 else None

    def _short():
        return portfolio.execute_short(ticker, units, price, "(scalper)", note, 70)

    def _cover_all():
        pos = _current_qty(database, ticker)
        return portfolio.execute_cover(ticker, abs(pos), price, "(scalper)", note, 70) if pos else None

    loop = asyncio.get_running_loop()
    try:
        if act.action == "OPEN_LONG":
            await loop.run_in_executor(None, _buy)
            if act.stop_loss:
                await loop.run_in_executor(None, lambda: portfolio.set_stop_loss(ticker, float(act.stop_loss), run_id=run_id))
        elif act.action == "OPEN_SHORT":
            await loop.run_in_executor(None, _short)
            if act.stop_loss:
                await loop.run_in_executor(None, lambda: portfolio.set_stop_loss(ticker, float(act.stop_loss), run_id=run_id))
        elif act.action == "CLOSE":
            # chiudi qualunque lato aperto
            await loop.run_in_executor(None, _sell_all)
            await loop.run_in_executor(None, _cover_all)
        elif act.action == "FLIP_TO_SHORT":
            await loop.run_in_executor(None, _sell_all)
            await loop.run_in_executor(None, _short)
            if act.stop_loss:
                await loop.run_in_executor(None, lambda: portfolio.set_stop_loss(ticker, float(act.stop_loss), run_id=run_id))
        elif act.action == "FLIP_TO_LONG":
            await loop.run_in_executor(None, _cover_all)
            await loop.run_in_executor(None, _buy)
            if act.stop_loss:
                await loop.run_in_executor(None, lambda: portfolio.set_stop_loss(ticker, float(act.stop_loss), run_id=run_id))
        return True
    except Exception as e:
        logger_warn(f"[SCALPER {run_id}] execute {act.action} {ticker} fallito: {e}")
        return False


def _current_qty(database, ticker: str) -> float:
    try:
        for p in (database.get_positions() or []):
            if (p.get("ticker") or "").upper() == ticker.upper():
                return float(p.get("quantity") or 0)
    except Exception:
        pass
    return 0.0


def _json_dump(d: dict) -> str:
    import json as _j
    return _j.dumps(d, default=str)


def logger_warn(msg: str) -> None:
    import logging
    logging.getLogger(__name__).warning(msg)

