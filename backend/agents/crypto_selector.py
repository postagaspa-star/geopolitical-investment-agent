"""
Crypto Selector — motore di SELEZIONE deterministico sull'universo delle major.

PROBLEMA che risolve (osservazione di Andrea, giustissima): finora ho testato
su BTC/ETH/SOL scelti a mano = cherry-picking. Nella realta' il sistema vede
TUTTE le ~14 major e deve, a ogni run, valutarle TUTTE allo stesso modo,
assegnare a ognuna un punteggio di OPPORTUNITA', e operare solo le migliori.

Questo e' "cross-sectional momentum" / rotazione per forza relativa: tieni le
piu' forti, eviti le deboli, ruoti quando il quadro cambia. E' una delle poche
tecniche che storicamente aggiunge valore, perche' NON prevede il futuro: si
limita a stare dove la forza c'e' gia'.

opportunity_score() → 0..1 (pseudo-probabilita' che l'asset sia un'opportunita'
LONG ADESSO). Combina, da SOLI dati tecnici verificati:
  - trend-health: EMA stack (fast>slow) + slope positiva → trend sano
  - momentum risk-adjusted: ROC / volatilita' (premia chi sale in modo stabile,
    non chi sbalza) — il "Sharpe" del momentum
  - posizione nel trend: leggero bonus se non e' ipercomprato (entrata migliore)
Penalizza la volatilita' eccessiva (rischio). Niente look-ahead: usa solo il
passato fino alla candela corrente.

Modulo PURO/DETERMINISTICO. Riusa gli indicatori testati.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from agents.crypto_scalper import _f, ema_series, atr, roc, rsi

# Le 14 major (vive oggi). NOTA: esclude quelle morte (LUNA, FTT) → i backtest
# storici avranno un filo di survivorship bias (ottimistico). Dichiarato.
UNIVERSE_14 = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT",
    "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "MATICUSDT", "LTCUSDT",
    "TRXUSDT", "ATOMUSDT",
]


@dataclass
class SelectorParams:
    ema_fast: int = 20
    ema_slow: int = 50
    roc_len: int = 20          # momentum su ~20 candele
    atr_len: int = 20
    rsi_len: int = 14
    # pesi della combinazione (somma ~1)
    w_trend: float = 0.40      # salute del trend
    w_momentum: float = 0.45   # momentum risk-adjusted
    w_entry: float = 0.15      # qualita' del punto d'entrata
    overbought_rsi: float = 80.0


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def opportunity_score(closes, highs, lows, params: Optional[SelectorParams] = None) -> Optional[float]:
    """Pseudo-probabilita' 0..1 che l'asset sia un'opportunita' LONG adesso.
    None se warmup insufficiente. Deterministico, no look-ahead."""
    p = params or SelectorParams()
    cs = [c for c in (_f(x) for x in closes) if c is not None]
    if len(cs) < p.ema_slow + 5:
        return None
    ef = ema_series(cs, p.ema_fast)[-1]
    es_list = ema_series(cs, p.ema_slow)
    es = es_list[-1]
    es_prev = es_list[-5]
    a = atr(highs, lows, cs, p.atr_len)
    r = roc(cs, p.roc_len)
    rs = rsi(cs, p.rsi_len)
    price = cs[-1]
    if None in (ef, es, es_prev, a, r, rs) or es <= 0 or price <= 0:
        return None

    # 1) TREND-HEALTH 0..1: EMA stack + slope
    slope_pct = (es - es_prev) / es_prev / 4.0 * 100.0
    stack = 1.0 if ef > es else 0.0
    # slope normalizzata: 0%/candela→0.5, +0.3%/candela→~1, -0.3%→~0
    slope_score = _clamp01(0.5 + slope_pct / 0.6)
    trend_health = 0.5 * stack + 0.5 * slope_score

    # 2) MOMENTUM RISK-ADJUSTED 0..1: ROC diviso volatilita' (ATR%)
    atr_pct = a / price * 100.0
    if atr_pct <= 0:
        return None
    rar = r / atr_pct          # momentum per unita' di rischio
    # rar tipico in trend sano ~ +0.5..+2; mappiamo: 0→0.5, +1.5→~1, -1.5→~0
    momentum_score = _clamp01(0.5 + rar / 3.0)

    # 3) ENTRY-QUALITY 0..1: penalizza l'ipercomprato (entrata tardiva)
    if rs >= p.overbought_rsi:
        entry_score = 0.2
    else:
        entry_score = _clamp01(1.0 - (rs - 50.0) / 60.0)  # piu' basso RSI = entrata migliore

    score = (p.w_trend * trend_health
             + p.w_momentum * momentum_score
             + p.w_entry * entry_score)
    return _clamp01(score)


@dataclass
class Ranked:
    symbol: str
    score: float


def rank_universe(data_by_symbol: dict, params: Optional[SelectorParams] = None) -> list[Ranked]:
    """data_by_symbol: {symbol: list[bar]} (bar = dict OHLCV, vecchia→recente).
    Ritorna la lista ordinata per opportunity_score DECRESCENTE. Gli asset con
    score None (warmup/dati mancanti) sono esclusi (non si opera al buio)."""
    out: list[Ranked] = []
    for sym, bars in (data_by_symbol or {}).items():
        if not bars:
            continue
        closes = [b.get("close") for b in bars]
        highs = [b.get("high") for b in bars]
        lows = [b.get("low") for b in bars]
        s = opportunity_score(closes, highs, lows, params)
        if s is not None:
            out.append(Ranked(symbol=sym, score=round(s, 4)))
    out.sort(key=lambda r: r.score, reverse=True)
    return out


def select_top(data_by_symbol: dict, k: int, min_score: float = 0.55,
               params: Optional[SelectorParams] = None) -> list[Ranked]:
    """Top-K per opportunita', MA solo quelli sopra min_score (se nessuno
    supera la soglia, ritorna lista vuota = stai in cash, non forzare trade).
    'Meglio non far nulla che sbagliare' applicato anche alla selezione."""
    ranked = rank_universe(data_by_symbol, params)
    good = [r for r in ranked if r.score >= min_score]
    return good[:k]
