"""
Crypto Structural Score — selezione basata su STRUTTURA + VOLUME (non RSI/EMA).

PERCHE' (osservazione di Andrea): gli indicatori classici (RSI/EMA/ROC) sono i
segnali piu' banali e "consumati" del mercato — milioni di trader e bot li usano,
quindi qualsiasi edge e' gia' arbitrato via. I trader che guardano la STRUTTURA
(supporti/resistenze, rotture confermate dal volume) sfruttano qualcosa di meno
banale. Questo modulo testa ONESTAMENTE quell'ipotesi.

COSA fa (oggettivo, niente figure soggettive tipo head&shoulders, evidenza
debole + pattern-matching illusorio):
  - DONCHIAN BREAKOUT: il prezzo rompe il massimo delle ultime N candele?
    Una rottura di resistenza e' il segnale strutturale piu' pulito.
  - CONFERMA VOLUME: la rottura avviene con volume > media * k? Un breakout
    SENZA volume e' tipicamente una trappola (false break). QUESTA e' la cosa
    che gli indicatori puri di prezzo NON vedono.
  - POSIZIONE NEL RANGE: vicino al supporto (rischio basso) o gia' esteso?
  - PULLBACK HEALTH: il prezzo ha appena ritracciato a un supporto e rimbalza?

Score 0..1. Su candele 4h cattura i trend di GIORNI (l'ipotesi di Andrea sui
movimenti brevi ben definiti). Modulo PURO/DETERMINISTICO.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from agents.crypto_scalper import _f, atr


@dataclass
class StructuralParams:
    donchian_break: int = 20      # rottura del max delle ultime N candele
    support_lookback: int = 20    # finestra per supporto/resistenza
    vol_ma_len: int = 20          # media volume per la conferma
    vol_break_mult: float = 1.3   # volume del breakout > media * questo
    atr_len: int = 14
    # pesi (somma ~1)
    w_breakout: float = 0.40      # rottura resistenza
    w_volume: float = 0.30        # conferma volume (la chiave anti-falsa-rottura)
    w_position: float = 0.20      # posizione favorevole nel range
    w_pullback: float = 0.10      # rimbalzo da supporto


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def structural_score(closes, highs, lows, volumes,
                     params: Optional[StructuralParams] = None) -> Optional[float]:
    """Score strutturale 0..1. None se warmup insufficiente. No look-ahead."""
    p = params or StructuralParams()
    cs = [c for c in (_f(x) for x in closes) if c is not None]
    hs = [h for h in (_f(x) for x in highs) if h is not None]
    ls = [l for l in (_f(x) for x in lows) if l is not None]
    vs = [v for v in (_f(x) for x in volumes) if v is not None]
    need = max(p.donchian_break, p.support_lookback, p.vol_ma_len, p.atr_len) + 2
    if min(len(cs), len(hs), len(ls), len(vs)) < need:
        return None
    price = cs[-1]
    if price <= 0:
        return None

    # 1) BREAKOUT: prezzo vs massimo delle N candele PRECEDENTI (esclusa corrente)
    prior_high = max(hs[-p.donchian_break - 1:-1])
    prior_low = min(ls[-p.support_lookback - 1:-1])
    if prior_high <= 0 or prior_low <= 0:
        return None
    # quanto sopra la resistenza (in frazione): >0 = breakout
    break_frac = (price - prior_high) / prior_high
    # mappa: appena sopra (0%) → 0.6, +3% sopra → ~1; sotto resistenza → <0.6
    breakout_score = _clamp01(0.6 + break_frac / 0.05)
    if price < prior_high:
        # non ha rotto: score basso proporzionale a quanto e' lontano
        breakout_score = _clamp01(0.5 * (price - prior_low) / max(prior_high - prior_low, 1e-9))

    # 2) VOLUME CONFIRM: volume corrente vs media (la cosa che il prezzo non vede)
    vol_ma = sum(vs[-p.vol_ma_len:]) / p.vol_ma_len
    cur_vol = vs[-1]
    if vol_ma <= 0:
        vol_score = 0.5
    else:
        ratio = cur_vol / vol_ma
        # ratio 1.0 → 0.5 ; ratio >= vol_break_mult → ~1 ; basso volume → <0.5
        vol_score = _clamp01(0.5 + (ratio - 1.0) / (2.0 * (p.vol_break_mult - 1.0)))

    # 3) POSIZIONE NEL RANGE: dove sta il prezzo tra supporto e resistenza
    rng = prior_high - prior_low
    pos_in_range = (price - prior_low) / rng if rng > 0 else 0.5
    # vicino al supporto (entrata a rischio basso) premia; gia' esteso penalizza
    position_score = _clamp01(1.0 - abs(pos_in_range - 0.4) / 0.6)

    # 4) PULLBACK HEALTH: ha toccato il supporto di recente e ora risale?
    recent_low = min(ls[-3:])
    near_support = (recent_low - prior_low) / max(prior_low, 1e-9) < 0.03
    bouncing = price > cs[-2] if len(cs) >= 2 else False
    pullback_score = 1.0 if (near_support and bouncing) else 0.4

    score = (p.w_breakout * breakout_score
             + p.w_volume * vol_score
             + p.w_position * position_score
             + p.w_pullback * pullback_score)
    return _clamp01(score)
