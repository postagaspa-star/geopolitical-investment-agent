"""
Crypto Signal Core — il "cervello tecnico" deterministico del Decision Crypto.

OBIETTIVO (vedi piano "tecnico delle cripto"):
Invertire il controllo. Oggi DeepSeek-R1 decide a ruota libera direzione,
size, stop-loss e conviction, e il codice mette solo dei tetti DOPO. Qui
invece un nucleo PURO e DETERMINISTICO calcola, da dati VERIFICATI:
  - una confluence (conteggio pesato di segnali tecnici realmente presenti),
  - una conviction 0..1,
  - una size basata sulla VOLATILITA' (ATR / rischio-per-trade), mai "a naso",
  - uno STOP-LOSS OBBLIGATORIO ancorato all'ATR e ai vincoli del risk profile,
  - un filtro di REGIME (BTC sopra/sotto EMA200) che blocca i trade contro-trend.

L'LLM (R1) resta a valle come NARRATORE/VETO: puo' spiegare, ridurre la size o
porre un veto, MAI aumentare la size ne' inventare numeri. L'executor prendera'
min(size_core, size_LLM) e rifiutera' aperture senza lo stop-loss del core.

PRINCIPIO FORENSE — FAIL-CLOSED SUI DATI:
ogni fattore e' OPZIONALE. Se un dato non e' presente negli input, NON viene
inventato: contribuisce 0 (neutro) e abbassa il `data_completeness`. Non si
ragiona mai su numeri assenti.

Modulo PURO: nessun I/O, nessun DB, nessuna chiamata di rete, solo stdlib.
Cosi' e' interamente testabile in isolamento e riusabile sia dal Live sia dal
Simulator (stessa logica → backtest fedele).
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional

# ─── Parametri di default (override-abili dal chiamante / risk profile) ───────

# Pesi della confluence: quanto "vale" ciascun segnale tecnico nel conteggio.
W_SIGNAL_COUNT = 2.0     # i conteggi bullish/bearish gia' aggregati dal Technical
W_SIGNAL_LABEL = 1.5     # il campo signal BUY/SELL/HOLD
W_TREND = 1.5            # trend struttura (TRENDING_UP/DOWN)
W_EMA_STACK = 1.5        # allineamento prezzo vs EMA50/EMA200
W_MACD = 1.0             # istogramma MACD
W_RSI = 1.0              # RSI ipercomprato/ipervenduto
W_CANDLE = 1.0           # pattern candlestick (bull/bear nel testo)
W_FUNDING = 1.0          # funding bias (contrarian: overcrowded = rischio squeeze)
W_SR_PROXIMITY = 1.0     # vicinanza a supporto/resistenza
W_FEAR_GREED = 0.5       # Fear&Greed (contrarian, peso lieve)

# Soglie tecniche
RSI_OVERSOLD = 35.0
RSI_OVERBOUGHT = 65.0
SR_NEAR_PCT = 2.0        # entro il 2% da S/R = "vicino"
FG_EXTREME_FEAR = 25.0
FG_EXTREME_GREED = 75.0


@dataclass
class CoreParams:
    """Parametri del motore. Default prudenti; il chiamante puo' iniettare
    i valori dal risk profile attivo (sl_min/sl_max per asset_class=crypto)."""
    risk_per_trade_pct: float = 0.75   # % del NAV rischiata se scatta lo SL
    k_atr_sl: float = 1.5              # distanza SL = k * ATR
    k_atr_tp: float = 3.0             # distanza TP = k * ATR (RR ~2:1)
    min_conviction: float = 0.55       # sotto questa soglia → NO TRADE
    max_position_pct_nav: float = 12.0 # cap size come % NAV (default = moderate)
    sl_min_pct: float = 12.0           # range SL del profilo (crypto moderate)
    sl_max_pct: float = 25.0
    # Gestione regime: in bear i long richiedono conviction alta (e viceversa)
    counter_trend_conviction: float = 0.75
    block_counter_trend: bool = True


@dataclass
class Factor:
    """Un singolo segnale che ha contribuito alla confluence."""
    name: str
    direction: str       # "bull" | "bear"
    weight: float
    detail: str = ""


@dataclass
class CoreDecision:
    """Output deterministico del core per UN ticker."""
    ticker: str
    action: str                      # "BUY" | "SHORT" | "HOLD"
    direction: str                   # "long" | "short" | "none"
    conviction: float                # 0..1
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    size_units: float = 0.0
    size_pct_nav: float = 0.0
    reward_risk: Optional[float] = None
    regime: str = "unknown"          # "bull" | "bear" | "neutral" | "unknown"
    blocked: bool = False
    block_reason: str = ""
    data_completeness: float = 0.0   # 0..1 — quota di fattori realmente presenti
    bull_score: float = 0.0
    bear_score: float = 0.0
    factors: list[Factor] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["factors"] = [asdict(f) for f in self.factors]
        return d


# ─── Utility numeriche pure ───────────────────────────────────────────────────

def _f(x: Any) -> Optional[float]:
    """Float robusto: None/''/NaN/inf → None (mai esplode, mai inventa)."""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return v


def ema(values: list[float], period: int) -> Optional[float]:
    """EMA dell'ultimo valore. None se dati insufficienti."""
    vals = [v for v in (_f(x) for x in (values or [])) if v is not None]
    if len(vals) < period or period <= 0:
        return None
    k = 2.0 / (period + 1.0)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1.0 - k)
    return e


def classify_btc_regime(btc_daily_closes: Optional[list[float]]) -> tuple[str, str]:
    """Regime di mercato da BTC vs EMA200 (daily).

    Ritorna (regime, detail). regime ∈ {bull, bear, neutral, unknown}.
    NB: il 4h non e' disponibile nel feed (solo daily) → usiamo l'EMA200 daily
    come proxy del trend di fondo. Se <200 chiusure, ripieghiamo su EMA piu'
    corta marcando bassa confidenza, senza inventare.
    """
    closes = [v for v in (_f(x) for x in (btc_daily_closes or [])) if v is not None]
    if len(closes) < 50:
        return "unknown", "dati BTC insufficienti per il regime (<50 daily closes)"
    price = closes[-1]
    if len(closes) >= 200:
        ref = ema(closes, 200)
        label = "EMA200"
        low_conf = ""
    else:
        ref = ema(closes, 50)
        label = "EMA50"
        low_conf = " (bassa confidenza: <200 closes)"
    if ref is None:
        return "unknown", "EMA non calcolabile"
    band = ref * 0.01  # banda morta ±1% → neutral
    if price > ref + band:
        return "bull", f"BTC {price:.0f} > {label} {ref:.0f}{low_conf}"
    if price < ref - band:
        return "bear", f"BTC {price:.0f} < {label} {ref:.0f}{low_conf}"
    return "neutral", f"BTC ~{label} (entro ±1%){low_conf}"


# ─── Confluence: conta SOLO i segnali realmente presenti ─────────────────────

# numero massimo di fattori valutabili (per il data_completeness)
_MAX_FACTORS = 10


def score_confluence(details: dict, raw: Optional[dict] = None,
                     market_ctx: Optional[dict] = None) -> dict:
    """Costruisce la confluence pesata dai SOLI dati presenti.

    details: il dict 'details' del Technical Crypto (rsi_14, support, resistance,
             atr, candlestick_setup, signal_count{bullish,bearish}, signal,
             funding_bias, trend, current_price/price ...).
    raw:     indicatori grezzi opzionali (macd_hist, ema50, ema200, price).
    market_ctx: opzionale (fear_greed).

    Ritorna {bull, bear, net, total_weight, factors[], present, completeness}.
    """
    details = details or {}
    raw = raw or {}
    market_ctx = market_ctx or {}
    factors: list[Factor] = []
    present = 0

    price = _f(details.get("current_price")) or _f(details.get("price")) or _f(raw.get("price"))

    def add(name, direction, weight, detail=""):
        factors.append(Factor(name=name, direction=direction, weight=weight, detail=detail))

    # 1) signal_count aggregato (peso alto: e' gia' una sintesi del Technical)
    sc = details.get("signal_count") or {}
    b_cnt, s_cnt = _f(sc.get("bullish")), _f(sc.get("bearish"))
    if b_cnt is not None and s_cnt is not None:
        present += 1
        if b_cnt > s_cnt:
            add("signal_count", "bull", W_SIGNAL_COUNT, f"{b_cnt:.0f} bull vs {s_cnt:.0f} bear")
        elif s_cnt > b_cnt:
            add("signal_count", "bear", W_SIGNAL_COUNT, f"{s_cnt:.0f} bear vs {b_cnt:.0f} bull")

    # 2) signal label BUY/SELL/HOLD
    sig = str(details.get("signal") or "").upper().strip()
    if sig in ("BUY", "SELL", "HOLD"):
        present += 1
        if sig == "BUY":
            add("signal", "bull", W_SIGNAL_LABEL, "signal=BUY")
        elif sig == "SELL":
            add("signal", "bear", W_SIGNAL_LABEL, "signal=SELL")

    # 3) trend struttura
    tr = str(details.get("trend") or "").upper()
    if tr:
        present += 1
        if "UP" in tr or "BULL" in tr:
            add("trend", "bull", W_TREND, tr)
        elif "DOWN" in tr or "BEAR" in tr:
            add("trend", "bear", W_TREND, tr)

    # 4) EMA stack (richiede price + ema50 + ema200)
    ema50 = _f(raw.get("ema50")) or _f(details.get("ema50"))
    ema200 = _f(raw.get("ema200")) or _f(details.get("ema200"))
    if price is not None and ema50 is not None and ema200 is not None:
        present += 1
        if price > ema50 > ema200:
            add("ema_stack", "bull", W_EMA_STACK, "price>EMA50>EMA200")
        elif price < ema50 < ema200:
            add("ema_stack", "bear", W_EMA_STACK, "price<EMA50<EMA200")

    # 5) MACD histogram
    macd_h = _f(raw.get("macd_hist")) or _f(details.get("macd_hist"))
    if macd_h is not None:
        present += 1
        if macd_h > 0:
            add("macd", "bull", W_MACD, f"hist={macd_h:.4g}")
        elif macd_h < 0:
            add("macd", "bear", W_MACD, f"hist={macd_h:.4g}")

    # 6) RSI ipercomprato/ipervenduto
    rsi = _f(details.get("rsi_14")) or _f(raw.get("rsi_14")) or _f(raw.get("rsi"))
    if rsi is not None:
        present += 1
        if rsi < RSI_OVERSOLD:
            add("rsi", "bull", W_RSI, f"RSI {rsi:.1f} oversold")
        elif rsi > RSI_OVERBOUGHT:
            add("rsi", "bear", W_RSI, f"RSI {rsi:.1f} overbought")

    # 7) candlestick setup (testuale)
    cs = str(details.get("candlestick_setup") or "").lower()
    if cs:
        present += 1
        if "bull" in cs or "hammer" in cs or "morning" in cs:
            add("candlestick", "bull", W_CANDLE, cs[:48])
        elif "bear" in cs or "shooting" in cs or "evening" in cs:
            add("candlestick", "bear", W_CANDLE, cs[:48])

    # 8) funding bias — CONTRARIAN: overcrowded long = rischio long-squeeze
    fb = str(details.get("funding_bias") or "").lower()
    if fb and fb != "neutral":
        present += 1
        if "bullish_overcrowded" in fb:
            add("funding", "bear", W_FUNDING, "long affollati: rischio squeeze")
        elif "bearish_overcrowded" in fb:
            add("funding", "bull", W_FUNDING, "short affollati: rischio short-squeeze")

    # 9) prossimita' a supporto/resistenza
    sup, res = _f(details.get("support")), _f(details.get("resistance"))
    if price is not None and (sup is not None or res is not None):
        present += 1
        if sup is not None and sup > 0 and abs(price - sup) / price * 100.0 <= SR_NEAR_PCT:
            add("sr_proximity", "bull", W_SR_PROXIMITY, f"vicino supporto {sup:.4g}")
        elif res is not None and res > 0 and abs(price - res) / price * 100.0 <= SR_NEAR_PCT:
            add("sr_proximity", "bear", W_SR_PROXIMITY, f"vicino resistenza {res:.4g}")

    # 10) Fear & Greed — contrarian lieve
    fg = _f(market_ctx.get("fear_greed")) or _f(market_ctx.get("fear_greed_value"))
    if fg is not None:
        present += 1
        if fg < FG_EXTREME_FEAR:
            add("fear_greed", "bull", W_FEAR_GREED, f"F&G {fg:.0f} extreme fear")
        elif fg > FG_EXTREME_GREED:
            add("fear_greed", "bear", W_FEAR_GREED, f"F&G {fg:.0f} extreme greed")

    bull = sum(f.weight for f in factors if f.direction == "bull")
    bear = sum(f.weight for f in factors if f.direction == "bear")
    total = bull + bear
    return {
        "bull": bull,
        "bear": bear,
        "net": bull - bear,
        "total_weight": total,
        "factors": factors,
        "present": present,
        "completeness": round(present / _MAX_FACTORS, 3),
    }


# ─── Sizing basato su volatilita' (ATR) ──────────────────────────────────────

def position_size_atr(*, nav: float, entry: float, atr: float, side: str,
                      params: CoreParams) -> dict:
    """Size + SL + TP ancorati all'ATR e ai vincoli del profilo.

    Logica risk-based: rischio per trade fisso (% NAV). La size discende dalla
    distanza dello stop, non da quanto "ci si crede". SL clampato nel range del
    profilo. Size cappata dalla max_position_pct_nav.

    Ritorna dict con units, size_pct_nav, stop_loss, take_profit, reward_risk,
    sl_pct, capped(bool), e eventuali note.
    """
    notes: list[str] = []
    entry = _f(entry) or 0.0
    atr = _f(atr) or 0.0
    nav = _f(nav) or 0.0
    if entry <= 0 or nav <= 0:
        return {"units": 0.0, "size_pct_nav": 0.0, "stop_loss": None,
                "take_profit": None, "reward_risk": None, "sl_pct": None,
                "capped": False, "notes": ["entry/nav non validi → size 0"]}

    is_short = side == "short"

    # Distanza SL dall'ATR; se ATR assente, usa il minimo del profilo (fail-safe)
    if atr > 0:
        sl_dist = params.k_atr_sl * atr
        sl_pct = sl_dist / entry * 100.0
    else:
        sl_pct = params.sl_min_pct
        sl_dist = sl_pct / 100.0 * entry
        notes.append("ATR assente: SL impostato al minimo del profilo")

    # Clamp nel range del profilo [sl_min, sl_max]
    clamped = min(max(sl_pct, params.sl_min_pct), params.sl_max_pct)
    if clamped != sl_pct:
        notes.append(f"SL {sl_pct:.1f}% clampato a {clamped:.1f}% (range profilo)")
    sl_pct = clamped
    sl_dist = sl_pct / 100.0 * entry

    # Size dal rischio: rischio_$ = NAV * risk_pct ; units = rischio_$ / sl_dist
    risk_usd = nav * params.risk_per_trade_pct / 100.0
    units = risk_usd / sl_dist if sl_dist > 0 else 0.0

    # Cap sulla size: non oltre max_position_pct_nav del NAV come notional
    max_units = (nav * params.max_position_pct_nav / 100.0) / entry
    capped = False
    if units > max_units:
        units = max_units
        capped = True
        notes.append(f"size cappata a {params.max_position_pct_nav:.0f}% NAV")

    if is_short:
        stop_loss = entry + sl_dist
        take_profit = entry - params.k_atr_tp * atr if atr > 0 else None
    else:
        stop_loss = entry - sl_dist
        take_profit = entry + params.k_atr_tp * atr if atr > 0 else None

    # Reward/Risk
    rr = None
    if take_profit is not None:
        tp_dist = abs(take_profit - entry)
        rr = round(tp_dist / sl_dist, 2) if sl_dist > 0 else None

    size_pct = units * entry / nav * 100.0 if nav > 0 else 0.0
    return {
        "units": units,
        "size_pct_nav": round(size_pct, 3),
        "stop_loss": round(stop_loss, 8) if stop_loss and stop_loss > 0 else None,
        "take_profit": round(take_profit, 8) if take_profit and take_profit > 0 else None,
        "reward_risk": rr,
        "sl_pct": round(sl_pct, 2),
        "capped": capped,
        "notes": notes,
    }


# ─── Conviction ──────────────────────────────────────────────────────────────

def compute_conviction(confl: dict, data_completeness: float) -> float:
    """Conviction 0..1 dalla forza e PUREZZA della confluence, penalizzata se
    i dati sono incompleti. Niente euforia con poca informazione."""
    total = confl.get("total_weight", 0.0)
    if total <= 0:
        return 0.0
    net = abs(confl.get("net", 0.0))
    purity = net / total          # 1.0 = segnali tutti concordi
    strength = min(total / 6.0, 1.0)  # ~6 di peso = confluence "piena"
    base = 0.55 * purity + 0.45 * strength
    # penalita' dati mancanti: con meta' fattori presenti, -~25%
    completeness_factor = 0.5 + 0.5 * max(0.0, min(1.0, data_completeness))
    return round(max(0.0, min(1.0, base * completeness_factor)), 3)


# ─── Decisione completa per un ticker ────────────────────────────────────────

def decide(*, ticker: str, details: dict, nav: float,
           raw: Optional[dict] = None, market_ctx: Optional[dict] = None,
           btc_daily_closes: Optional[list[float]] = None,
           regime: Optional[str] = None,
           params: Optional[CoreParams] = None) -> CoreDecision:
    """Decisione deterministica completa.

    details: 'details' del Technical Crypto per il ticker.
    nav:     valore totale del portafoglio (per il sizing).
    raw/market_ctx: indicatori grezzi e contesto di mercato opzionali.
    btc_daily_closes: chiusure daily di BTC per il filtro di regime.
    regime: regime gia' calcolato ("bull"/"bear"/"neutral"/"unknown"). Se
            fornito, ha priorita' su btc_daily_closes — serve al REPLAY forense
            (ricostruire una decisione passata dal suo snapshot, senza dover
            riconservare l'intera serie BTC). Funzione PURA: stessi input →
            stesso output.
    """
    p = params or CoreParams()
    raw = raw or {}
    market_ctx = market_ctx or {}

    entry = (_f(details.get("current_price")) or _f(details.get("price"))
             or _f(raw.get("price")))
    atr = _f(details.get("atr")) or _f(raw.get("atr")) or 0.0

    confl = score_confluence(details, raw, market_ctx)
    completeness = confl["completeness"]
    if regime is not None:
        regime, regime_detail = regime, f"regime fornito: {regime}"
    else:
        regime, regime_detail = classify_btc_regime(btc_daily_closes)

    dec = CoreDecision(
        ticker=ticker, action="HOLD", direction="none", conviction=0.0,
        entry=entry, regime=regime, data_completeness=completeness,
        bull_score=confl["bull"], bear_score=confl["bear"],
        factors=confl["factors"],
    )
    if regime_detail:
        dec.notes.append(f"regime: {regime_detail}")

    # Direzione dalla confluence netta
    net = confl["net"]
    if net > 0:
        direction = "long"
    elif net < 0:
        direction = "short"
    else:
        dec.block_reason = "confluence neutra (nessun segnale netto)"
        dec.blocked = True
        return dec
    dec.direction = direction

    # Conviction
    dec.conviction = compute_conviction(confl, completeness)

    # Gate 1: conviction minima
    if dec.conviction < p.min_conviction:
        dec.blocked = True
        dec.block_reason = (f"conviction {dec.conviction:.2f} < soglia "
                            f"{p.min_conviction:.2f} → NO TRADE")
        return dec

    # Gate 2: filtro di regime (contro-trend richiede conviction alta)
    counter_trend = (
        (direction == "long" and regime == "bear") or
        (direction == "short" and regime == "bull")
    )
    if counter_trend and p.block_counter_trend:
        if dec.conviction < p.counter_trend_conviction:
            dec.blocked = True
            dec.block_reason = (
                f"trade {direction} contro regime {regime}: conviction "
                f"{dec.conviction:.2f} < {p.counter_trend_conviction:.2f} richiesta "
                f"contro-trend → NO TRADE")
            return dec
        dec.notes.append(
            f"contro-trend ammesso (conviction {dec.conviction:.2f} alta)")

    # Sizing — richiede entry valido
    if entry is None or entry <= 0:
        dec.blocked = True
        dec.block_reason = "entry price non disponibile → impossibile dimensionare"
        return dec

    sizing = position_size_atr(nav=nav, entry=entry, atr=atr,
                               side=direction, params=p)
    dec.stop_loss = sizing["stop_loss"]
    dec.take_profit = sizing["take_profit"]
    dec.size_units = sizing["units"]
    dec.size_pct_nav = sizing["size_pct_nav"]
    dec.reward_risk = sizing["reward_risk"]
    dec.notes.extend(sizing.get("notes", []))

    # Gate 3: SL obbligatorio — se non calcolabile, NIENTE trade (fail-closed)
    if not dec.stop_loss or dec.stop_loss <= 0 or dec.size_units <= 0:
        dec.blocked = True
        dec.block_reason = "stop-loss o size non calcolabili → apertura bloccata"
        dec.action = "HOLD"
        return dec

    dec.action = "BUY" if direction == "long" else "SHORT"
    return dec


# ─── Adapter: indicatori tecnici → input del core ────────────────────────────

def core_inputs_from_analysis(ind: dict) -> tuple[dict, dict, dict]:
    """Mappa un dict di indicatori verso gli input del core: (details, raw,
    market_ctx). Gestisce DUE shape: l'output grezzo di _fetch_ticker_indicators
    (rsi, macd:{histogram}, sma_50/200, bullish_signals/bearish_signals) e
    l'output 'analyses' del Technical Crypto (rsi_14, signal_count, funding_bias).

    Difensivo: cerca le chiavi al top-level e dentro i container annidati
    (details/raw_indicators/advanced/derivatives/multitf). Le chiavi assenti
    restano assenti — il core fa fail-closed, non si inventa nulla.
    """
    ind = ind or {}
    nested: dict = {}
    for k in ("details", "raw_indicators", "indicators", "advanced",
              "derivatives", "multitf"):
        v = ind.get(k)
        if isinstance(v, dict):
            for kk, vv in v.items():
                nested.setdefault(kk, vv)

    def pick(*names):
        for n in names:
            if n in ind and ind[n] is not None:
                return ind[n]
            if n in nested and nested[n] is not None:
                return nested[n]
        return None

    # MACD histogram: spesso annidato in un dict {line, signal, histogram}
    macd_hist = pick("macd_hist", "macd_histogram")
    if macd_hist is None:
        m = ind.get("macd")
        if not isinstance(m, dict):
            m = nested.get("macd")
        if isinstance(m, dict):
            macd_hist = m.get("histogram")

    # signal_count: dict esplicito, oppure costruito da contatori flat
    sc = pick("signal_count", "signals_summary")
    if not isinstance(sc, dict):
        b = pick("bullish_signals", "bullish")
        s = pick("bearish_signals", "bearish")
        if b is not None or s is not None:
            sc = {"bullish": b or 0, "bearish": s or 0}
        else:
            sc = None

    details = {
        "current_price": pick("current_price", "price", "close", "last_price"),
        "rsi_14": pick("rsi_14", "rsi"),
        "atr": pick("atr", "atr_14"),
        "support": pick("support", "nearest_support"),
        "resistance": pick("resistance", "nearest_resistance"),
        "trend": pick("trend", "market_structure", "structure"),
        "signal": pick("signal"),
        "funding_bias": pick("funding_bias", "funding_signal"),
        "candlestick_setup": pick("candlestick_setup", "candlestick", "pattern"),
        "fib_zone": pick("fib_zone"),
    }
    if sc is not None:
        details["signal_count"] = sc

    raw = {
        "price": details["current_price"],
        "ema50": pick("ema50", "ema_50", "sma50", "sma_50"),
        "ema200": pick("ema200", "ema_200", "sma200", "sma_200"),
        "rsi_14": details["rsi_14"],
        "atr": details["atr"],
    }
    if macd_hist is not None:
        raw["macd_hist"] = macd_hist

    market_ctx = {
        "fear_greed": pick("fear_greed", "fear_greed_value", "fng"),
        "btc_dominance": pick("btc_dominance", "btc_d", "dominance"),
    }

    details = {k: v for k, v in details.items() if v is not None}
    raw = {k: v for k, v in raw.items() if v is not None}
    market_ctx = {k: v for k, v in market_ctx.items() if v is not None}
    return details, raw, market_ctx


# ─── Riconciliazione core ↔ proposta LLM (R1 puo' solo ridurre/vetare) ───────

def reconcile(decision: CoreDecision, *, side: str,
              llm_units: Optional[float] = None,
              llm_sl: Optional[float] = None) -> dict:
    """Riconcilia la decisione del core con la proposta di R1.

    REGOLA (inversione del controllo): il core DECIDE; R1 puo' solo RIDURRE la
    size o accettare. Mai aumentare, mai operare contro la direzione del core,
    mai aprire se il core blocca. Lo stop-loss del core e' vincolante.

    Ritorna {allowed, reason, size_units, stop_loss, take_profit}.
    """
    if decision.blocked or decision.action == "HOLD":
        return {"allowed": False,
                "reason": decision.block_reason or "core: nessun setup valido",
                "size_units": 0.0, "stop_loss": None, "take_profit": None}

    core_side = "long" if decision.action == "BUY" else "short"
    if side != core_side:
        return {"allowed": False,
                "reason": f"direzione R1 ({side}) opposta al core ({core_side})",
                "size_units": 0.0, "stop_loss": None, "take_profit": None}

    core_units = decision.size_units or 0.0
    final_units = core_units
    if llm_units is not None and llm_units > 0:
        final_units = min(float(llm_units), core_units)
    return {
        "allowed": True,
        "reason": f"core OK ({decision.action}, conv {decision.conviction:.2f}, "
                  f"regime {decision.regime})",
        "size_units": final_units,
        "stop_loss": decision.stop_loss,
        "take_profit": decision.take_profit,
    }


# ─── Forense: snapshot riproducibile e replay ────────────────────────────────

# Versione dell'algoritmo del core. Va incrementata a OGNI modifica che cambia
# l'output di decide() a parita' di input — cosi' un replay sa con quale logica
# fu presa la decisione (standard evidenziale: una decisione e' verificabile
# solo se sai anche QUALE versione del motore l'ha prodotta).
CORE_VERSION = "1.0.0"


def snapshot_for_replay(*, ticker: str, details: dict, nav: float,
                        raw: Optional[dict] = None,
                        market_ctx: Optional[dict] = None,
                        regime: Optional[str] = None,
                        params: Optional[CoreParams] = None) -> dict:
    """Fotografia COMPLETA e LEGGERA degli input che hanno prodotto una
    decisione del core, sufficiente a ricostruirla in seguito (audit/forense).

    Memorizza il `regime` gia' risolto (non l'intera serie BTC): replay esatto
    a costo di pochi byte. Pensato per essere serializzato in agent_logs.
    """
    p = params or CoreParams()
    return {
        "core_version": CORE_VERSION,
        "ticker": ticker,
        "nav": _f(nav),
        "details": dict(details or {}),
        "raw": dict(raw or {}),
        "market_ctx": dict(market_ctx or {}),
        "regime": regime,
        "params": asdict(p),
    }


def replay(snapshot: dict) -> CoreDecision:
    """Ricostruisce una CoreDecision da uno snapshot prodotto da
    snapshot_for_replay(). Deve restituire ESATTAMENTE la decisione originale
    (decide() e' puro). Tollerante a snapshot parziali (campi assenti → default).
    """
    snap = snapshot or {}
    params_d = snap.get("params") or {}
    try:
        params = CoreParams(**{k: v for k, v in params_d.items()
                               if k in CoreParams.__dataclass_fields__})
    except Exception:
        params = CoreParams()
    return decide(
        ticker=snap.get("ticker", "?"),
        details=snap.get("details") or {},
        nav=_f(snap.get("nav")) or 0.0,
        raw=snap.get("raw") or {},
        market_ctx=snap.get("market_ctx") or {},
        regime=snap.get("regime"),
        params=params,
    )
