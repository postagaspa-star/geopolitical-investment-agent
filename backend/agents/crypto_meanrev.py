"""
Crypto Mean-Reversion — il robot "da LATERALE" (conservativo).

IDEA: nel laterale il prezzo oscilla attorno a una media. Mean-reversion compra
quando il prezzo e' ANOMALMENTE basso (sotto-banda) e vende quando torna alla
media. NON segue lo slancio: scommette che l'eccesso rientri.

REGOLA D'ORO DI ANDREA: "meglio non far nulla che sbagliare". Ogni scelta di
design e' improntata alla PRUDENZA, perche' il mean-reversion ha un nemico
mortale: quando il laterale FINISCE e parte un trend vero, continuerebbe a
"comprare il fondo" mentre il prezzo crolla (falling knife). Difese:

  1. OPERA SOLO IN LATERALE. Riceve il regime dall'esterno (crypto_regime):
     se non e' SIDEWAYS, NON apre nulla. Questo da solo evita il falling-knife
     dei downtrend.
  2. SOGLIA ALTA. Compra solo se z-score <= -2.0 (evento raro, ~2 dev.std sotto
     la media). Niente ingressi su rumore.
  3. HARD-STOP anti-trend. Se dopo l'acquisto il prezzo continua a scendere
     oltre z <= -3.5, NON sta rimbalzando: sta TRENDANDO giu'. Esci subito,
     taglia la perdita. (E' il "mi sbagliavo, era un trend").
  4. SIZE PICCOLA. Posizione contenuta: il laterale rende poco, non vale
     rischiare grosso.
  5. ASTIENE DI DEFAULT. Senza un segnale forte, l'azione e' NONE.
  6. Solo LONG (compra il minimo). Niente short: shortare un "massimo" in un
     mercato con bias rialzista e' il modo classico per farsi male.

Modulo PURO/DETERMINISTICO su candele giornaliere.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Optional

from agents.crypto_scalper import _f, ema_series

FLAT, LONG = "FLAT", "LONG"


@dataclass
class MeanRevParams:
    lookback: int = 20            # finestra per media + deviazione standard
    z_entry: float = -2.0         # compra se z <= questo (2 dev.std sotto)
    z_exit: float = 0.0           # vendi quando torna alla media (z >= 0)
    z_hardstop: float = -3.5      # NON entrare se z e' gia' sotto questo (troppo esteso)
    # HARD-STOP DI PREZZO: stop-loss % sotto l'entry. Robusto a prescindere
    # dalla media mobile (che, scendendo, fa "risalire" lo z anche mentre il
    # prezzo crolla → lo z da solo NON cattura il falling knife). Questo si'.
    hardstop_pct: float = 5.0     # esci se il prezzo scende > 5% sotto l'entry
    max_position_pct_nav: float = 8.0   # size piccola (laterale rende poco)
    max_hold_bars: int = 15       # se non rimbalza entro N candele, esci (capitale fermo)
    require_sideways: bool = True # opera SOLO se il regime e' SIDEWAYS
    # COOLDOWN dopo un hard-stop: NON ricomprare per N candele. Senza questo, in
    # un falling knife il robot ricompra a ogni gradino piu' basso (ogni prezzo
    # sembra "oversold" vs la media che scende) e prende -6% ripetuti. Il
    # cooldown spezza questa emorragia: "mi sono sbagliato, mi fermo e aspetto".
    reentry_cooldown_bars: int = 10


@dataclass
class MeanRevState:
    position: str = FLAT
    entry_price: float = 0.0
    entry_bar: int = -10_000
    units: float = 0.0
    bar_index: int = -1
    last_hardstop_bar: int = -10_000   # per il cooldown anti falling-knife

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MeanRevAction:
    action: str          # NONE | OPEN_LONG | CLOSE
    reason: str = ""
    price: float = 0.0
    z: float = 0.0
    units: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def _mean_std(values: list[float]) -> tuple[Optional[float], Optional[float]]:
    vs = [v for v in (_f(x) for x in values) if v is not None]
    if len(vs) < 2:
        return None, None
    m = sum(vs) / len(vs)
    var = sum((v - m) ** 2 for v in vs) / len(vs)
    return m, var ** 0.5


def zscore(closes: list[float], lookback: int) -> Optional[float]:
    """Quanto l'ultimo prezzo e' lontano dalla media, in deviazioni standard."""
    if len(closes) < lookback:
        return None
    window = closes[-lookback:]
    m, sd = _mean_std(window)
    last = _f(closes[-1])
    if m is None or sd is None or sd <= 0 or last is None:
        return None
    return (last - m) / sd


def step(state: MeanRevState, bars: list[dict], nav: float,
         regime: str = "SIDEWAYS", params: Optional[MeanRevParams] = None) -> MeanRevAction:
    """Una candela. `regime` arriva dal meta-cervello: se non e' SIDEWAYS (e
    require_sideways=True) il robot NON apre nuove posizioni (evita il falling
    knife). Muta state. Deterministico."""
    p = params or MeanRevParams()
    state.bar_index += 1
    closes = [b.get("close") for b in bars]
    price = _f(closes[-1]) if closes else None
    none = MeanRevAction(action="NONE", price=price or 0.0)
    if price is None or price <= 0:
        return none

    z = zscore(closes, p.lookback)
    if z is None:
        return none   # warmup
    none.z = z

    # ── Gestione posizione aperta ────────────────────────────────────────────
    if state.position == LONG:
        # 1) HARD-STOP DI PREZZO: il prezzo e' sceso troppo sotto l'entry → era
        #    un trend giu', non una reversion. Esci e taglia (robusto: non
        #    dipende dalla media che scivola). Questo cattura il falling knife.
        if price <= state.entry_price * (1 - p.hardstop_pct / 100.0):
            loss = (price / state.entry_price - 1) * 100.0
            state.last_hardstop_bar = state.bar_index   # avvia il cooldown
            return _close(state, price, f"hard-stop prezzo: {loss:.1f}% sotto entry (trend giu')", z)
        # 2) TARGET: tornato alla media → incassa
        if z >= p.z_exit:
            return _close(state, price, f"target raggiunto: z={z:.2f} (rientro alla media)", z)
        # 3) TIMEOUT: capitale fermo troppo a lungo senza rimbalzo → libera
        if state.bar_index - state.entry_bar >= p.max_hold_bars:
            return _close(state, price, f"timeout {p.max_hold_bars} candele: nessun rimbalzo", z)
        return none   # aspetta il rientro

    # ── FLAT: valuta acquisto (solo condizioni forti + laterale) ─────────────
    if p.require_sideways and regime != "SIDEWAYS":
        none.reason = f"regime {regime} != SIDEWAYS: mi astengo (no falling knife)"
        return none
    # COOLDOWN dopo un hard-stop: non ricomprare subito (anti falling-knife)
    if state.bar_index - state.last_hardstop_bar < p.reentry_cooldown_bars:
        none.reason = "cooldown post hard-stop: mi astengo (evito di ricomprare il knife)"
        return none
    # Non entrare se gia' TROPPO esteso (z sotto l'hard-stop di z): un eccesso
    # cosi' estremo e' spesso l'inizio di un crollo, non una reversion.
    if z <= p.z_hardstop:
        none.reason = f"z={z:.2f} troppo esteso (<= {p.z_hardstop}): mi astengo, non e' reversion"
        return none
    if p.z_entry >= z > p.z_hardstop:
        units = (nav * p.max_position_pct_nav / 100.0) / price
        if units <= 0:
            return none
        state.position = LONG
        state.entry_price = price
        state.entry_bar = state.bar_index
        state.units = units
        return MeanRevAction(action="OPEN_LONG",
                             reason=f"oversold forte: z={z:.2f} <= {p.z_entry} (compro il minimo)",
                             price=price, z=z, units=units)
    none.reason = f"z={z:.2f}: nessun eccesso, mi astengo"
    return none


def _close(state, price, reason, z) -> MeanRevAction:
    units = state.units
    state.position = FLAT
    state.entry_price = 0.0
    state.entry_bar = state.bar_index
    state.units = 0.0
    return MeanRevAction(action="CLOSE", reason=reason, price=price, z=z, units=units)
