"""
Rotation Scan — universo ampio cross-sector per individuare opportunità di
rotazione attiva. SEMPRE ON, non solo durante regimi di crisi.

Razionale: in ogni regime di mercato (BULL, GEOPOLITICAL, MACRO, CRASH, ...)
c'è quasi sempre QUALCHE settore/asset che ruota positivamente. Il bug
storico del Decision Agent era restare focalizzato su una watchlist ristretta
(tipicamente tech mega-cap + qualche posizione esistente), perdendo flow
verso defensive/safe-haven/energy/defense/etc.

Il modulo:
  1. Definisce un universo categorizzato di ~60 ticker.
  2. Calcola metriche dettagliate per ogni ticker (performance multi-TF,
     forza relativa vs SPY, RSI, distanza MA, posizione 52w, volume).
  3. Pondera in un "rotation score" e ordina.
  4. Fornisce un formatter per iniezione nel contesto del Decision Agent
     E una funzione di filtro per il tool `scan_rotation_opportunities`.

Cache 30 min — chiamato in parallelo all'ingestione contesto in
run_decision_agent. La latenza per un cache-miss è ~10-20s (60 tickers
in parallelo via thread pool su Polygon/yfinance). I cache-hit sono istantanei.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# UNIVERSO DI ROTAZIONE (~60 ticker liquidi cross-sector)
# ════════════════════════════════════════════════════════════════════════════
ROTATION_UNIVERSE: dict[str, list[str]] = {
    # Settori difensivi puri (anti-ciclici, dividend payers, demand inelastica)
    "defensive": [
        "XLU", "XLP", "XLV",
        "NEE", "DUK", "SO", "AEP",          # utilities
        "KO", "PG", "WMT", "COST", "PEP", "MO",  # consumer staples
        "JNJ", "UNH", "LLY",                 # healthcare mega-cap
    ],

    # Safe-haven monetari/reali (oro, treasuries, USD)
    "safe_haven": [
        "GLD", "IAU", "GDX",     # oro
        "SLV",                    # argento
        "TLT", "IEF", "SHY", "TIP",  # treasuries (long/mid/short/inflation-linked)
        "UUP",                    # USD index
    ],

    # Hedge diretti contro il mercato (inverse / volatilità)
    "hedge": [
        "SH", "PSQ", "RWM",   # inverse SPY/QQQ/IWM
        "VIXY",                # volatilità
    ],

    # Vincitori geopolitici (defense + aerospace)
    "geopolitical": [
        "LMT", "RTX", "NOC", "GD", "LHX",
        "ITA",   # aerospace & defense ETF
    ],

    # Energy + Commodity (beneficiari di shock supply/inflazione)
    # NB: rimosso BNO (Brent Oil Fund) — frequentemente segnalato come
    # delisted da yfinance, causa errori "Expecting value: line 1 column 1"
    # ad ogni cache-miss. Sostituito da USL (US 12-Month Oil Fund) che è
    # più liquido. SLB (Schlumberger) per esposizione oilfield services.
    "energy_commodity": [
        "XLE", "USO", "USL", "DBC",
        "OXY", "CVX", "XOM", "COP", "SLB",
    ],

    # Sector ETF rimanenti (per misurare rotazione settoriale completa)
    "sectors": [
        "XLK",   # tech
        "XLF",   # financials
        "XLY",   # consumer discretionary
        "XLI",   # industrials
        "XLB",   # materials
        "XLRE",  # real estate
        "XLC",   # communication
    ],

    # Bond corporate (segnali di stress credit / risk-on)
    "bonds": [
        "HYG",   # high yield (junk) — sale in risk-on, scende in stress
        "LQD",   # investment grade
        "AGG",   # aggregate
    ],

    # International / decoupling (rotazione geografica)
    "international": [
        "VGK",   # Europe
        "EWJ",   # Japan
        "INDA",  # India
        "EWZ",   # Brazil
        "FXI",   # China large cap
    ],

    # Factor (momentum / value / low-vol / quality)
    "factor": [
        "USMV",  # low volatility
        "QUAL",  # quality
        "MTUM",  # momentum
        "VLUE",  # value
    ],
}

# Reverse lookup ticker → categoria
ROTATION_TICKER_TO_CATEGORY: dict[str, str] = {
    t: cat for cat, ts in ROTATION_UNIVERSE.items() for t in ts
}

ALL_ROTATION_TICKERS: list[str] = sorted({
    t for ts in ROTATION_UNIVERSE.values() for t in ts
})

VALID_CATEGORIES: list[str] = list(ROTATION_UNIVERSE.keys())


# ════════════════════════════════════════════════════════════════════════════
# CACHE (30 min) — evita di rifare scansione completa ad ogni run del Decision
# ════════════════════════════════════════════════════════════════════════════
_cache: dict[str, Any] = {"timestamp": None, "data": None}
CACHE_TTL_SEC = 1800  # 30 minuti


def invalidate_cache() -> None:
    """Forza il prossimo scan a rifare il fetch (utile per test/manual refresh)."""
    _cache["timestamp"] = None
    _cache["data"] = None


# ════════════════════════════════════════════════════════════════════════════
# HELPER MATH
# ════════════════════════════════════════════════════════════════════════════
def _pct_change_n_days(closes: list[float], n: int) -> float | None:
    """% change su n trading days (closes[-1] vs closes[-(n+1)])."""
    if not closes or len(closes) < n + 1:
        return None
    base = closes[-(n + 1)]
    if base == 0:
        return None
    return closes[-1] / base - 1.0


def _rsi14(closes: list[float]) -> float | None:
    """RSI 14 standard."""
    if len(closes) < 15:
        return None
    deltas = [closes[i + 1] - closes[i] for i in range(len(closes) - 1)]
    last_14 = deltas[-14:]
    gains = [d for d in last_14 if d > 0]
    losses = [-d for d in last_14 if d < 0]
    avg_g = sum(gains) / 14.0
    avg_l = sum(losses) / 14.0
    if avg_l == 0:
        return 100.0 if avg_g > 0 else 50.0
    rs = avg_g / avg_l
    return 100.0 - 100.0 / (1.0 + rs)


def _ma_distance(closes: list[float], period: int) -> float | None:
    """% distance from N-day moving average (positivo = sopra MA)."""
    if len(closes) < period:
        return None
    ma = sum(closes[-period:]) / period
    if ma == 0:
        return None
    return closes[-1] / ma - 1.0


def _pos_52w(highs: list[float], lows: list[float], current: float) -> float | None:
    """Posizione nel range 52-week (0 = al low, 1 = al high)."""
    if not highs or not lows:
        return None
    window = 252
    hs = highs[-window:] if len(highs) >= window else highs
    ls = lows[-window:] if len(lows) >= window else lows
    hi = max(hs)
    lo = min(ls)
    if hi == lo:
        return 0.5
    return (current - lo) / (hi - lo)


def _extract_series(result: dict) -> tuple[list[float], list[float], list[float], list[float]]:
    """Estrae (closes, highs, lows, volumes) dal payload di fetch_market_data."""
    if not result:
        return [], [], [], []
    rows = result.get("data") or []
    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    volumes: list[float] = []
    for r in rows:
        c = r.get("close")
        h = r.get("high")
        lo = r.get("low")
        v = r.get("volume")
        if c is None or c <= 0:
            continue
        closes.append(float(c))
        highs.append(float(h) if h else float(c))
        lows.append(float(lo) if lo else float(c))
        volumes.append(float(v) if v else 0.0)
    return closes, highs, lows, volumes


async def _fetch_ohlcv_async(ticker: str, period_days: int = 280) -> dict | None:
    """Wrapper async per data_fetchers.fetch_market_data (sync sotto)."""
    try:
        import data_fetchers
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, data_fetchers.fetch_market_data, ticker, period_days
        )
    except Exception as e:
        logger.debug("rotation_scan: fetch fail %s: %s", ticker, e)
        return None


# ════════════════════════════════════════════════════════════════════════════
# SCAN CORE
# ════════════════════════════════════════════════════════════════════════════
async def scan_rotation_universe(force_refresh: bool = False) -> dict:
    """
    Esegue una scansione completa dell'universo rotazione.

    Restituisce un dict:
      {
        "scan_timestamp_utc": ISO datetime,
        "tickers_total": <int>,
        "tickers_scanned": <int>,
        "spy_benchmark": {c1d_pct, c5d_pct, c20d_pct, c60d_pct},
        "rows": [ <dict per ticker, ordinato per rotation_score desc> ],
      }

    Ogni row ha:
      ticker, category, price,
      c1d_pct, c5d_pct, c20d_pct, c60d_pct,
      rs5d_vs_spy_pct, rs20d_vs_spy_pct, rs60d_vs_spy_pct,
      rsi14, dist_50ma_pct, dist_200ma_pct, pos_52w, vol_ratio_20d,
      rotation_score (somma ponderata RS multi-TF + volume bonus).
    """
    now_ts = datetime.now(timezone.utc).timestamp()
    if (not force_refresh
            and _cache["timestamp"] is not None
            and (now_ts - _cache["timestamp"]) < CACHE_TTL_SEC
            and _cache["data"] is not None):
        return _cache["data"]

    # Fetch SPY + tutti i ticker in parallelo
    tickers_to_fetch = list(ALL_ROTATION_TICKERS)
    if "SPY" not in tickers_to_fetch:
        tickers_to_fetch.append("SPY")

    logger.info("rotation_scan: fetching %d tickers (cache miss)...",
                len(tickers_to_fetch))
    fetch_start = datetime.now(timezone.utc).timestamp()
    results = await asyncio.gather(
        *[_fetch_ohlcv_async(t) for t in tickers_to_fetch],
        return_exceptions=False,
    )
    by_ticker: dict[str, dict | None] = dict(zip(tickers_to_fetch, results))
    fetch_dur = datetime.now(timezone.utc).timestamp() - fetch_start
    logger.info("rotation_scan: fetch completed in %.1fs", fetch_dur)

    # SPY benchmark
    spy_closes, _, _, _ = _extract_series(by_ticker.get("SPY") or {})
    spy_1d = _pct_change_n_days(spy_closes, 1)
    spy_5d = _pct_change_n_days(spy_closes, 5)
    spy_20d = _pct_change_n_days(spy_closes, 20)
    spy_60d = _pct_change_n_days(spy_closes, 60)

    rows: list[dict] = []
    for t in ALL_ROTATION_TICKERS:
        result = by_ticker.get(t)
        closes, highs, lows, volumes = _extract_series(result or {})
        if not closes:
            continue
        last = closes[-1]
        c1 = _pct_change_n_days(closes, 1)
        c5 = _pct_change_n_days(closes, 5)
        c20 = _pct_change_n_days(closes, 20)
        c60 = _pct_change_n_days(closes, 60)

        rs1 = (c1 - spy_1d) if (c1 is not None and spy_1d is not None) else None
        rs5 = (c5 - spy_5d) if (c5 is not None and spy_5d is not None) else None
        rs20 = (c20 - spy_20d) if (c20 is not None and spy_20d is not None) else None
        rs60 = (c60 - spy_60d) if (c60 is not None and spy_60d is not None) else None

        rsi = _rsi14(closes)
        d50 = _ma_distance(closes, 50)
        d200 = _ma_distance(closes, 200)
        pos = _pos_52w(highs, lows, last)

        vol_20 = (sum(volumes[-20:]) / 20.0) if len(volumes) >= 20 else None
        vol_ratio = (volumes[-1] / vol_20) if (vol_20 and vol_20 > 0 and volumes) else None

        # Rotation score (in scala %; alto = più forza relativa)
        #  - 5d RS pesa di più (rotazione di breve)
        #  - 20d e 60d confermano la persistenza
        #  - volume_ratio dà bonus a movimenti partecipativi
        score = 0.0
        if rs5 is not None:
            score += 0.40 * rs5 * 100.0
        if rs20 is not None:
            score += 0.30 * rs20 * 100.0
        if rs60 is not None:
            score += 0.20 * rs60 * 100.0
        if vol_ratio is not None:
            score += 0.10 * max(0.0, min(3.0, vol_ratio - 1.0))

        rows.append({
            "ticker": t,
            "category": ROTATION_TICKER_TO_CATEGORY.get(t, "other"),
            "price": round(last, 2),
            "c1d_pct": round(c1 * 100, 2) if c1 is not None else None,
            "c5d_pct": round(c5 * 100, 2) if c5 is not None else None,
            "c20d_pct": round(c20 * 100, 2) if c20 is not None else None,
            "c60d_pct": round(c60 * 100, 2) if c60 is not None else None,
            "rs1d_vs_spy_pct": round(rs1 * 100, 2) if rs1 is not None else None,
            "rs5d_vs_spy_pct": round(rs5 * 100, 2) if rs5 is not None else None,
            "rs20d_vs_spy_pct": round(rs20 * 100, 2) if rs20 is not None else None,
            "rs60d_vs_spy_pct": round(rs60 * 100, 2) if rs60 is not None else None,
            "rsi14": round(rsi, 1) if rsi is not None else None,
            "dist_50ma_pct": round(d50 * 100, 2) if d50 is not None else None,
            "dist_200ma_pct": round(d200 * 100, 2) if d200 is not None else None,
            "pos_52w": round(pos, 2) if pos is not None else None,
            "vol_ratio_20d": round(vol_ratio, 2) if vol_ratio is not None else None,
            "rotation_score": round(score, 2),
        })

    # Ordina per rotation_score desc
    rows.sort(key=lambda r: r["rotation_score"], reverse=True)

    data = {
        "scan_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "tickers_total": len(ALL_ROTATION_TICKERS),
        "tickers_scanned": len(rows),
        "spy_benchmark": {
            "c1d_pct": round(spy_1d * 100, 2) if spy_1d is not None else None,
            "c5d_pct": round(spy_5d * 100, 2) if spy_5d is not None else None,
            "c20d_pct": round(spy_20d * 100, 2) if spy_20d is not None else None,
            "c60d_pct": round(spy_60d * 100, 2) if spy_60d is not None else None,
        },
        "rows": rows,
    }
    _cache["timestamp"] = now_ts
    _cache["data"] = data
    return data


# ════════════════════════════════════════════════════════════════════════════
# FILTRO (per uso del tool)
# ════════════════════════════════════════════════════════════════════════════
def filter_rotation(
    data: dict,
    categories: list[str] | None = None,
    min_rs_5d_pct: float | None = None,
    min_rs_20d_pct: float | None = None,
    top_n: int | None = None,
    include_bottom: bool = False,
    rsi_max: float | None = None,
    rsi_min: float | None = None,
) -> dict:
    """
    Filtra i risultati del scan per uso del tool scan_rotation_opportunities.

    Parametri:
      categories: limita alle categorie indicate
      min_rs_5d_pct / min_rs_20d_pct: filtra ticker con forza relativa minima
      top_n: max ticker da ritornare (post-filtri, default 20)
      include_bottom: se True aggiunge anche i 5 ticker peggiori (per short/avoid)
      rsi_max / rsi_min: filtra per RSI

    Restituisce un dict con i ticker filtrati ordinati per rotation_score.
    """
    if not data or not data.get("rows"):
        return {"rows": [], "by_category": {}, "filter_count": 0}

    rows = data["rows"]

    if categories:
        valid_cats = set(categories) & set(VALID_CATEGORIES)
        if valid_cats:
            rows = [r for r in rows if r["category"] in valid_cats]

    if min_rs_5d_pct is not None:
        rows = [
            r for r in rows
            if r.get("rs5d_vs_spy_pct") is not None
            and r["rs5d_vs_spy_pct"] >= min_rs_5d_pct
        ]

    if min_rs_20d_pct is not None:
        rows = [
            r for r in rows
            if r.get("rs20d_vs_spy_pct") is not None
            and r["rs20d_vs_spy_pct"] >= min_rs_20d_pct
        ]

    if rsi_max is not None:
        rows = [
            r for r in rows
            if r.get("rsi14") is not None and r["rsi14"] <= rsi_max
        ]

    if rsi_min is not None:
        rows = [
            r for r in rows
            if r.get("rsi14") is not None and r["rsi14"] >= rsi_min
        ]

    top = rows[:top_n] if top_n else rows

    by_category: dict[str, list[dict]] = {}
    for r in top:
        by_category.setdefault(r["category"], []).append(r)

    result = {
        "rows": top,
        "by_category": by_category,
        "filter_count": len(top),
        "total_after_category_filter": len(rows),
        "spy_benchmark": data.get("spy_benchmark", {}),
        "scan_timestamp_utc": data.get("scan_timestamp_utc"),
    }

    if include_bottom and data.get("rows"):
        # Bottom 5 dell'INTERO universo (no filtri) per visibilità sui peggiori
        all_rows = data["rows"]
        result["bottom_5_universe"] = sorted(
            all_rows, key=lambda r: r.get("rotation_score", 0)
        )[:5]

    return result


# ════════════════════════════════════════════════════════════════════════════
# FORMATTER per iniezione nel system prompt dinamico / user message
# ════════════════════════════════════════════════════════════════════════════
def _fmt_pct(v: float | None, decimals: int = 2, with_sign: bool = True,
             width: int = 6) -> str:
    """Format percentage con allineamento. None → ' n/a '"""
    if v is None:
        return f"{'n/a':>{width}}"
    fmt = f"{{:{'+' if with_sign else ''}{width}.{decimals}f}}"
    return fmt.format(v)


def _fmt_num(v: float | None, decimals: int = 1, width: int = 5) -> str:
    if v is None:
        return f"{'n/a':>{width}}"
    fmt = f"{{:{width}.{decimals}f}}"
    return fmt.format(v)


def format_rotation_for_prompt(data: dict, top_n: int = 18) -> str:
    """
    Formatta un blocco testuale denso per iniezione nel contesto del
    Decision Agent. Mostra:
      - SPY benchmark multi-timeframe
      - TOP N per rotation_score (forza relativa vs SPY)
      - BOTTOM 5 (sottoperformance — possibili short / da evitare)
      - Breakdown sintetico per categoria
      - Istruzioni d'uso
    """
    if not data or not data.get("rows"):
        return ""

    rows = data["rows"]
    spy = data.get("spy_benchmark", {}) or {}

    lines: list[str] = []
    lines.append("═" * 76)
    lines.append("🔄  ROTATION SCAN — UNIVERSO ANTI-CICLICO / SETTORIALE (sempre-on)")
    lines.append("═" * 76)
    lines.append("")
    lines.append(
        f"Scansione di {data.get('tickers_total', 0)} ticker cross-sector: "
        "defensive, safe-haven, hedge, geopolitical, energy/commodity, "
        "sector ETF, bonds, international, factor."
    )
    lines.append(
        f"Aggiornata ogni 30 min — {data.get('scan_timestamp_utc', 'n/a')}"
    )
    lines.append("")

    # SPY benchmark
    spy_line = "📊 SPY benchmark: "
    spy_line += (
        f"1d {_fmt_pct(spy.get('c1d_pct'), 2, True, 5)}%  "
        f"5d {_fmt_pct(spy.get('c5d_pct'), 2, True, 6)}%  "
        f"20d {_fmt_pct(spy.get('c20d_pct'), 2, True, 6)}%  "
        f"60d {_fmt_pct(spy.get('c60d_pct'), 2, True, 6)}%"
    )
    lines.append(spy_line)
    lines.append("")

    # Top N
    n_show = min(top_n, len(rows))
    lines.append(f"🏆 TOP {n_show} per ROTATION SCORE (forza relativa vs SPY, peso 5d/20d/60d + vol):")
    lines.append("")
    lines.append(
        "  Ticker | Categoria      | "
        "1d%    | 5d%    | 20d%   | 60d%   | "
        "RS5d   | RS20d  | RS60d  | RSI  | 52w | Vol× | Score"
    )
    lines.append("  " + "-" * 130)
    for r in rows[:n_show]:
        cat = r.get("category", "n/a")
        pos52 = r.get("pos_52w")
        pos52_str = f"{int(pos52 * 100):3d}%" if pos52 is not None else "n/a "
        lines.append(
            f"  {r['ticker']:<6} | {cat:<14} | "
            f"{_fmt_pct(r.get('c1d_pct'), 2, True, 6)} | "
            f"{_fmt_pct(r.get('c5d_pct'), 2, True, 6)} | "
            f"{_fmt_pct(r.get('c20d_pct'), 2, True, 6)} | "
            f"{_fmt_pct(r.get('c60d_pct'), 2, True, 6)} | "
            f"{_fmt_pct(r.get('rs5d_vs_spy_pct'), 2, True, 6)} | "
            f"{_fmt_pct(r.get('rs20d_vs_spy_pct'), 2, True, 6)} | "
            f"{_fmt_pct(r.get('rs60d_vs_spy_pct'), 2, True, 6)} | "
            f"{_fmt_num(r.get('rsi14'), 1, 4)} | "
            f"{pos52_str} | "
            f"{_fmt_num(r.get('vol_ratio_20d'), 2, 4)} | "
            f"{_fmt_num(r.get('rotation_score'), 2, 6)}"
        )
    lines.append("")

    # Bottom 5
    bottom = sorted(rows, key=lambda r: r.get("rotation_score", 0))[:5]
    if bottom:
        lines.append("📉 BOTTOM 5 (sottoperformance vs SPY — candidati SHORT o DA EVITARE):")
        lines.append("")
        for r in bottom:
            lines.append(
                f"  {r['ticker']:<6} ({r.get('category', 'n/a')}): "
                f"5d={_fmt_pct(r.get('c5d_pct'), 2, True, 6)}%  "
                f"RS5d={_fmt_pct(r.get('rs5d_vs_spy_pct'), 2, True, 6)}%  "
                f"RS20d={_fmt_pct(r.get('rs20d_vs_spy_pct'), 2, True, 6)}%  "
                f"RSI={_fmt_num(r.get('rsi14'), 1, 5)}  "
                f"dist50MA={_fmt_pct(r.get('dist_50ma_pct'), 2, True, 6)}%"
            )
        lines.append("")

    # Breakdown per categoria — leader di ogni gruppo
    lines.append("📦 LEADER PER CATEGORIA (top ticker per ogni segmento):")
    lines.append("")
    by_cat: dict[str, list[dict]] = {}
    for r in rows:
        by_cat.setdefault(r.get("category", "other"), []).append(r)
    for cat in VALID_CATEGORIES:
        cat_rows = by_cat.get(cat, [])
        if not cat_rows:
            continue
        leader = cat_rows[0]  # già ordinati per score desc globalmente
        lines.append(
            f"  {cat:<18} leader: {leader['ticker']:<6}  "
            f"score={_fmt_num(leader.get('rotation_score'), 2, 6)}  "
            f"5d={_fmt_pct(leader.get('c5d_pct'), 2, True, 6)}%  "
            f"RS5d={_fmt_pct(leader.get('rs5d_vs_spy_pct'), 2, True, 6)}%  "
            f"RSI={_fmt_num(leader.get('rsi14'), 1, 5)}"
        )
    lines.append("")

    # Istruzioni d'uso
    lines.append("📚 COME LEGGERE:")
    lines.append("  • RS5d/20d/60d = (return ticker - return SPY) sul timeframe. Positivo = batte SPY.")
    lines.append("  • rotation_score = 0.40·RS5d + 0.30·RS20d + 0.20·RS60d + 0.10·(vol_ratio−1).")
    lines.append("  • RSI > 70 = overbought (entry tardiva), RSI < 30 = oversold (rimbalzo possibile).")
    lines.append("  • pos_52w 80%+ = vicino ai massimi (momentum forte ma rischio esaurimento).")
    lines.append("  • pos_52w < 20% = vicino ai minimi (capitulation o trend strutturale rotto).")
    lines.append("  • Vol× > 1.5 = volumi accelerati → flow istituzionale netto.")
    lines.append("")
    lines.append("📋 USO OBBLIGATORIO:")
    lines.append("  • Consulta SEMPRE questa tabella prima di decidere, in OGNI regime.")
    lines.append("  • Se la tua watchlist abituale (es. tech mega-cap) è ferma o negativa")
    lines.append("    ma il rotation_score di un'altra categoria è chiaramente alto → VALUTA")
    lines.append("    una posizione lì invece di forzare un trade su asset che non si muove.")
    lines.append("  • In regimi GEOPOLITICAL/MACRO/CRASH controlla se DEFENSE/GOLD/ENERGY/")
    lines.append("    DEFENSIVE stanno guidando — è normale, non ignorarli.")
    lines.append("  • Per dati raw/filtrati per categoria/RSI/etc. chiama il tool")
    lines.append("    `scan_rotation_opportunities` (parametri custom + universo intero).")
    lines.append("")
    lines.append("═" * 76)
    lines.append("")
    return "\n".join(lines)


def format_filtered_for_tool(filtered: dict) -> str:
    """
    Output dettagliato JSON-friendly per il tool `scan_rotation_opportunities`.
    A differenza del prompt-format, qui includiamo TUTTI i campi raw.
    """
    import json as _json
    payload = {
        "scan_timestamp_utc": filtered.get("scan_timestamp_utc"),
        "spy_benchmark": filtered.get("spy_benchmark"),
        "total_after_filter": filtered.get("filter_count", 0),
        "results": filtered.get("rows", []),
        "results_by_category": filtered.get("by_category", {}),
    }
    if "bottom_5_universe" in filtered:
        payload["bottom_5_universe"] = filtered["bottom_5_universe"]
    return _json.dumps(payload, default=str, ensure_ascii=False)
