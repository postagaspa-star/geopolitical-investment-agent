"""
Technical Advanced — Helpers analitici per Technical Crypto (e potenzialmente
Technical standard).

Tutte funzioni PURE: niente LLM, niente DB, niente side effects.
Input: pandas DataFrame OHLCV (colonne: Open, High, Low, Close, Volume).
Output: dict serializzabili JSON, pronti per essere inclusi nel context LLM.

Moduli:
  - detect_candlestick_patterns(df, lookback=5)
  - calculate_fibonacci_levels(df, window=60)
  - identify_volume_profile(df, bins=20)
  - detect_market_structure(df, swing_lookback=5)
  - fetch_binance_derivatives(symbol)  [async, aiohttp]
  - fetch_multitimeframe_summary(ticker, intervals)  [async]

Tutto safe rispetto a NaN/Inf — i risultati passano da _scrub() prima di
ritornare al chiamante.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


# ─── Utility ──────────────────────────────────────────────────────────────────

def _is_finite(x) -> bool:
    if x is None:
        return False
    try:
        v = float(x)
        return not (math.isnan(v) or math.isinf(v))
    except (TypeError, ValueError):
        return False


def _scrub(obj):
    """Sostituisce NaN/Inf con None ricorsivamente."""
    if obj is None:
        return None
    if isinstance(obj, float):
        return obj if _is_finite(obj) else None
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(v) for v in obj]
    if isinstance(obj, (int, str, bool)):
        return obj
    try:
        v = float(obj)
        return v if _is_finite(v) else None
    except (TypeError, ValueError):
        return obj


def _round(x, decimals=2):
    if not _is_finite(x):
        return None
    return round(float(x), decimals)


# ─── 1. Candlestick patterns ──────────────────────────────────────────────────

def _candle_body(o: float, c: float) -> float:
    return abs(c - o)


def _candle_range(h: float, l: float) -> float:
    return max(h - l, 1e-9)


def _upper_wick(o: float, h: float, c: float) -> float:
    return h - max(o, c)


def _lower_wick(o: float, l: float, c: float) -> float:
    return min(o, c) - l


def _is_bullish(o: float, c: float) -> bool:
    return c > o


def _is_doji(o: float, h: float, c: float, l: float, threshold: float = 0.1) -> bool:
    """Body < threshold% del range totale → indecisione."""
    rng = _candle_range(h, l)
    return _candle_body(o, c) <= threshold * rng


def _is_hammer(o: float, h: float, c: float, l: float) -> bool:
    """
    Hammer: corpo piccolo in alto, lunga ombra inferiore (>2x corpo),
    ombra superiore minima. Bullish se appare in downtrend.
    """
    body = _candle_body(o, c)
    rng = _candle_range(h, l)
    if body == 0 or rng == 0:
        return False
    lower_w = _lower_wick(o, l, c)
    upper_w = _upper_wick(o, h, c)
    return (
        lower_w >= 2 * body
        and upper_w <= 0.3 * body
        and body / rng <= 0.4
    )


def _is_shooting_star(o: float, h: float, c: float, l: float) -> bool:
    """Inverso dell'hammer: lunga ombra superiore. Bearish in uptrend."""
    body = _candle_body(o, c)
    rng = _candle_range(h, l)
    if body == 0 or rng == 0:
        return False
    upper_w = _upper_wick(o, h, c)
    lower_w = _lower_wick(o, l, c)
    return (
        upper_w >= 2 * body
        and lower_w <= 0.3 * body
        and body / rng <= 0.4
    )


def _is_bullish_engulfing(prev_o, prev_c, curr_o, curr_c) -> bool:
    """Candela verde corrente copre interamente il corpo della precedente rossa."""
    prev_red = prev_c < prev_o
    curr_green = curr_c > curr_o
    engulfs = curr_o <= prev_c and curr_c >= prev_o
    return prev_red and curr_green and engulfs


def _is_bearish_engulfing(prev_o, prev_c, curr_o, curr_c) -> bool:
    prev_green = prev_c > prev_o
    curr_red = curr_c < curr_o
    engulfs = curr_o >= prev_c and curr_c <= prev_o
    return prev_green and curr_red and engulfs


def detect_candlestick_patterns(df, lookback: int = 5) -> list[dict]:
    """
    Scansiona le ultime N candele e ritorna i pattern rilevati.

    Args:
        df: DataFrame con colonne Open, High, Low, Close (date index)
        lookback: quante candele analizzare partendo da -1

    Returns:
        Lista di dict: [{"candle_offset": -1, "pattern": "bullish_hammer",
                         "bias": "bullish|bearish|neutral", "strength": "high|med|low"}]
    """
    if df is None or len(df) < 2:
        return []

    patterns: list[dict] = []
    n = len(df)
    start = max(1, n - lookback)  # serve sempre prev candle

    for i in range(start, n):
        try:
            o = float(df["Open"].iloc[i])
            h = float(df["High"].iloc[i])
            l = float(df["Low"].iloc[i])
            c = float(df["Close"].iloc[i])
            offset = i - n  # -1, -2, -3...

            # Single-candle patterns
            if _is_doji(o, h, c, l):
                patterns.append({
                    "candle_offset": offset,
                    "pattern": "doji",
                    "bias": "neutral",
                    "strength": "low",
                    "note": "indecisione mercato",
                })
                continue  # un doji esclude hammer/shooting

            if _is_hammer(o, h, c, l):
                # Bullish se in downtrend recente (5 candele prima)
                trend_window = df["Close"].iloc[max(0, i-5):i]
                downtrend = (
                    len(trend_window) > 0
                    and float(trend_window.iloc[-1]) < float(trend_window.iloc[0])
                )
                patterns.append({
                    "candle_offset": offset,
                    "pattern": "bullish_hammer" if downtrend else "hammer",
                    "bias": "bullish" if downtrend else "neutral",
                    "strength": "high" if downtrend else "med",
                    "note": "lunga ombra inferiore — possibile reversal",
                })
                continue

            if _is_shooting_star(o, h, c, l):
                trend_window = df["Close"].iloc[max(0, i-5):i]
                uptrend = (
                    len(trend_window) > 0
                    and float(trend_window.iloc[-1]) > float(trend_window.iloc[0])
                )
                patterns.append({
                    "candle_offset": offset,
                    "pattern": "shooting_star" if uptrend else "inverted_hammer",
                    "bias": "bearish" if uptrend else "neutral",
                    "strength": "high" if uptrend else "med",
                    "note": "lunga ombra superiore — pressione rifiuto",
                })
                continue

            # Multi-candle patterns (richiedono prev)
            prev_o = float(df["Open"].iloc[i - 1])
            prev_c = float(df["Close"].iloc[i - 1])

            if _is_bullish_engulfing(prev_o, prev_c, o, c):
                patterns.append({
                    "candle_offset": offset,
                    "pattern": "bullish_engulfing",
                    "bias": "bullish",
                    "strength": "high",
                    "note": "candela verde inghiotte rossa precedente",
                })
                continue

            if _is_bearish_engulfing(prev_o, prev_c, o, c):
                patterns.append({
                    "candle_offset": offset,
                    "pattern": "bearish_engulfing",
                    "bias": "bearish",
                    "strength": "high",
                    "note": "candela rossa inghiotte verde precedente",
                })
                continue
        except (KeyError, IndexError, ValueError) as e:
            logger.debug("[TECH-ADV] candle %d skip: %s", i, e)
            continue

    return patterns


# ─── 2. Fibonacci retracement ─────────────────────────────────────────────────

def calculate_fibonacci_levels(df, window: int = 60) -> dict:
    """
    Calcola livelli Fibonacci dall'ultimo swing high/low della finestra.

    Args:
        df: DataFrame con colonne High, Low, Close
        window: numero candele da considerare per swing high/low

    Returns:
        {
          "swing_high": 109000, "swing_low": 76000,
          "trend_direction": "up|down",  # high before low → down, viceversa → up
          "levels": {"0.0": 76000, "0.236": ..., "1.618": ...},
          "current_zone": "between_0.618_and_0.786",
          "nearest_support": {"level": "0.618", "price": 95260, "distance_pct": 2.1},
          "nearest_resistance": {"level": "0.786", "price": 101580, "distance_pct": 4.5}
        }
    """
    if df is None or len(df) < 10:
        return {"error": "data insufficiente per fib (< 10 candele)"}

    # Limita alla finestra
    sub = df.tail(window) if len(df) > window else df
    try:
        swing_high = float(sub["High"].max())
        swing_low = float(sub["Low"].min())
        # Posizione del max e min: definisce direzione trend
        idx_high = sub["High"].idxmax()
        idx_low = sub["Low"].idxmin()
        trend_direction = "up" if idx_low < idx_high else "down"
        current_price = float(df["Close"].iloc[-1])
    except (KeyError, ValueError) as e:
        return {"error": f"errore lettura OHLC: {e}"}

    if not (_is_finite(swing_high) and _is_finite(swing_low)) or swing_high <= swing_low:
        return {"error": "swing high/low invalidi"}

    rng = swing_high - swing_low
    # Livelli classici (incluse extension)
    fib_ratios = {
        "0.0": 0.0,
        "0.236": 0.236,
        "0.382": 0.382,
        "0.5": 0.5,
        "0.618": 0.618,
        "0.786": 0.786,
        "1.0": 1.0,
        "1.272": 1.272,
        "1.618": 1.618,
    }
    levels = {k: _round(swing_low + r * rng, 2) for k, r in fib_ratios.items()}

    # Determina la zona corrente e il livello più vicino sopra/sotto
    sorted_lvls = sorted(levels.items(), key=lambda kv: kv[1])
    current_zone = "below_0.0" if current_price < levels["0.0"] else "above_1.618"
    nearest_support = None
    nearest_resistance = None

    for i, (label, price) in enumerate(sorted_lvls):
        if price >= current_price:
            nearest_resistance = {
                "level": label, "price": price,
                "distance_pct": _round(((price - current_price) / current_price) * 100, 2),
            }
            if i > 0:
                prev_label, prev_price = sorted_lvls[i - 1]
                nearest_support = {
                    "level": prev_label, "price": prev_price,
                    "distance_pct": _round(((current_price - prev_price) / current_price) * 100, 2),
                }
                current_zone = f"between_{prev_label}_and_{label}"
            else:
                current_zone = f"below_{label}"
            break

    if nearest_resistance is None:  # prezzo sopra tutti i livelli
        last_label, last_price = sorted_lvls[-1]
        nearest_support = {
            "level": last_label, "price": last_price,
            "distance_pct": _round(((current_price - last_price) / current_price) * 100, 2),
        }
        current_zone = f"above_{last_label}"

    return _scrub({
        "swing_high": _round(swing_high, 2),
        "swing_low": _round(swing_low, 2),
        "trend_direction": trend_direction,
        "current_price": _round(current_price, 2),
        "levels": levels,
        "current_zone": current_zone,
        "nearest_support": nearest_support,
        "nearest_resistance": nearest_resistance,
    })


# ─── 3. Volume Profile (HVN/LVN) ──────────────────────────────────────────────

def identify_volume_profile(df, bins: int = 20, lookback: int = 60) -> dict:
    """
    Costruisce un volume profile (volume per fascia di prezzo) e identifica
    HVN (High Volume Nodes — supporto/resistenza forte) e LVN (Low Volume
    Nodes — zone che il prezzo attraversa velocemente).

    Args:
        df: DataFrame con colonne High, Low, Close, Volume
        bins: numero fasce di prezzo
        lookback: candele da usare

    Returns:
        {"poc": float (price con max volume),
         "hvn": [{"price_low": ..., "price_high": ..., "volume": ...}, ...],
         "lvn": [...],
         "current_position_vs_poc": "above|below|at"}
    """
    if df is None or len(df) < 10:
        return {"error": "data insufficiente"}

    sub = df.tail(lookback) if len(df) > lookback else df
    try:
        price_min = float(sub["Low"].min())
        price_max = float(sub["High"].max())
        if price_max <= price_min:
            return {"error": "range prezzo nullo"}

        bin_width = (price_max - price_min) / bins
        bin_volumes = [0.0] * bins
        bin_centers = [price_min + (i + 0.5) * bin_width for i in range(bins)]

        for i in range(len(sub)):
            h = float(sub["High"].iloc[i])
            l = float(sub["Low"].iloc[i])
            v = float(sub["Volume"].iloc[i])
            if not (_is_finite(h) and _is_finite(l) and _is_finite(v)):
                continue
            # Distribuisci il volume linearmente sulle fasce attraversate
            mid = (h + l) / 2
            bin_idx = min(bins - 1, max(0, int((mid - price_min) / bin_width)))
            bin_volumes[bin_idx] += v

        if sum(bin_volumes) == 0:
            return {"error": "volume totale zero"}

        # POC = fascia col volume max
        poc_idx = bin_volumes.index(max(bin_volumes))
        poc_price = bin_centers[poc_idx]

        # HVN: top 3 fasce per volume; LVN: bottom 3 (escludendo zero)
        avg_vol = sum(bin_volumes) / bins
        sorted_by_vol = sorted(enumerate(bin_volumes), key=lambda x: -x[1])

        hvn = []
        for idx, vol in sorted_by_vol[:3]:
            if vol < avg_vol * 1.3:  # solo fasce davvero "high"
                break
            hvn.append({
                "price_low": _round(price_min + idx * bin_width, 2),
                "price_high": _round(price_min + (idx + 1) * bin_width, 2),
                "volume_pct_of_total": _round(100 * vol / sum(bin_volumes), 2),
            })

        # LVN: fasce con volume < 30% media (e non zero)
        lvn = []
        for idx, vol in enumerate(bin_volumes):
            if 0 < vol < avg_vol * 0.3:
                lvn.append({
                    "price_low": _round(price_min + idx * bin_width, 2),
                    "price_high": _round(price_min + (idx + 1) * bin_width, 2),
                    "volume_pct_of_total": _round(100 * vol / sum(bin_volumes), 2),
                })
        lvn = lvn[:3]  # max 3

        current_price = float(df["Close"].iloc[-1])
        if abs(current_price - poc_price) < bin_width * 0.5:
            position = "at_poc"
        elif current_price > poc_price:
            position = "above_poc"
        else:
            position = "below_poc"

        return _scrub({
            "poc": _round(poc_price, 2),
            "current_price": _round(current_price, 2),
            "current_position_vs_poc": position,
            "hvn": hvn,
            "lvn": lvn,
            "interpretation": (
                f"POC a {_round(poc_price, 2)} è la zona di accettazione massima — "
                f"il prezzo è {position.replace('_', ' ')}. HVN agiscono come S/R, "
                f"LVN come transit zones (rotture rapide attese)."
            ),
        })
    except Exception as e:
        return {"error": f"volume profile failed: {e}"}


# ─── 4. Market Structure (HH/HL/BoS/CHoCH) ────────────────────────────────────

def _find_swings(df, lookback: int = 5) -> tuple[list[dict], list[dict]]:
    """
    Identifica swing high/low usando il metodo "fractal" classico:
    una candela è swing high se ha High > delle N candele attorno (sopra e sotto).
    """
    highs = []
    lows = []
    n = len(df)
    for i in range(lookback, n - lookback):
        try:
            curr_h = float(df["High"].iloc[i])
            curr_l = float(df["Low"].iloc[i])
            window_h = df["High"].iloc[i - lookback:i + lookback + 1]
            window_l = df["Low"].iloc[i - lookback:i + lookback + 1]
            if curr_h == float(window_h.max()):
                highs.append({"index": i, "price": curr_h, "offset_from_end": i - n})
            if curr_l == float(window_l.min()):
                lows.append({"index": i, "price": curr_l, "offset_from_end": i - n})
        except Exception:
            continue
    return highs, lows


def detect_market_structure(df, swing_lookback: int = 5) -> dict:
    """
    Rileva struttura di mercato:
      - HH/HL = uptrend strutturale (Higher Highs + Higher Lows)
      - LH/LL = downtrend
      - BoS (Break of Structure) = rottura ultimo swing high/low → trend continua
      - CHoCH (Change of Character) = rottura nella direzione opposta → potenziale inversione
    """
    if df is None or len(df) < swing_lookback * 4:
        return {"error": "data insufficiente per market structure"}

    highs, lows = _find_swings(df, swing_lookback)
    if len(highs) < 2 or len(lows) < 2:
        return {"structure": "INDETERMINATE", "note": "swing insufficienti"}

    # Ultimi 2 swing per categoria
    last_highs = sorted(highs, key=lambda x: x["index"])[-2:]
    last_lows = sorted(lows, key=lambda x: x["index"])[-2:]

    higher_high = last_highs[1]["price"] > last_highs[0]["price"]
    higher_low = last_lows[1]["price"] > last_lows[0]["price"]
    lower_high = last_highs[1]["price"] < last_highs[0]["price"]
    lower_low = last_lows[1]["price"] < last_lows[0]["price"]

    if higher_high and higher_low:
        structure = "UPTREND"
    elif lower_high and lower_low:
        structure = "DOWNTREND"
    elif higher_high and lower_low:
        structure = "EXPANDING"  # volatilità in aumento
    elif lower_high and higher_low:
        structure = "CONTRACTING"  # triangolo / consolidamento
    else:
        structure = "MIXED"

    # BoS / CHoCH detection: prezzo corrente vs ultimo swing
    current_price = float(df["Close"].iloc[-1])
    last_swing_high = last_highs[-1]["price"]
    last_swing_low = last_lows[-1]["price"]

    bos_choch = None
    if structure == "UPTREND" and current_price > last_swing_high:
        bos_choch = {
            "type": "BoS_bullish",
            "broken_level": _round(last_swing_high, 2),
            "note": "trend up confermato, rotto ultimo high",
        }
    elif structure == "DOWNTREND" and current_price < last_swing_low:
        bos_choch = {
            "type": "BoS_bearish",
            "broken_level": _round(last_swing_low, 2),
            "note": "trend down confermato, rotto ultimo low",
        }
    elif structure == "DOWNTREND" and current_price > last_swing_high:
        bos_choch = {
            "type": "CHoCH_bullish",
            "broken_level": _round(last_swing_high, 2),
            "note": "potenziale inversione: rotto high in downtrend",
        }
    elif structure == "UPTREND" and current_price < last_swing_low:
        bos_choch = {
            "type": "CHoCH_bearish",
            "broken_level": _round(last_swing_low, 2),
            "note": "potenziale inversione: rotto low in uptrend",
        }

    return _scrub({
        "structure": structure,
        "last_swing_high": _round(last_swing_high, 2),
        "last_swing_low": _round(last_swing_low, 2),
        "bos_choch": bos_choch,
        "current_price": _round(current_price, 2),
    })


# ─── 5. Binance derivatives (funding rate + open interest) ────────────────────

# Mapping crypto-USD ticker → simbolo Binance perpetual (USDT-margined)
_BINANCE_SYMBOL_MAP = {
    "BTC-USD": "BTCUSDT",
    "ETH-USD": "ETHUSDT",
    "SOL-USD": "SOLUSDT",
    "DOGE-USD": "DOGEUSDT",
    "AVAX-USD": "AVAXUSDT",
    "ADA-USD": "ADAUSDT",
    "XRP-USD": "XRPUSDT",
    "LTC-USD": "LTCUSDT",
    "DOT-USD": "DOTUSDT",
    "LINK-USD": "LINKUSDT",
    "UNI-USD": "UNIUSDT",
    "ATOM-USD": "ATOMUSDT",
    "MATIC-USD": "MATICUSDT",
    "NEAR-USD": "NEARUSDT",
}


async def fetch_binance_derivatives(ticker: str, timeout: float = 8.0) -> dict:
    """
    Recupera funding rate corrente + open interest dal Binance Futures API
    (endpoint pubblici, no auth richiesta).

    Args:
        ticker: ticker formato yfinance (es. "BTC-USD")
        timeout: secondi totali per le 2 chiamate

    Returns:
        {
          "binance_symbol": "BTCUSDT",
          "funding_rate_pct": 0.0123,  # percentuale (8h funding)
          "funding_rate_annualized_pct": 13.5,  # *3*365 / 100 (approx)
          "funding_signal": "bullish_overcrowded|bearish_overcrowded|neutral",
          "open_interest_usd": 12500000000,
          "fetched_at": "2026-05-09T10:00:00Z",
        }
        oppure {"error": ...} se ticker non mappato o API fallisce.
    """
    sym = _BINANCE_SYMBOL_MAP.get(ticker.upper())
    if not sym:
        return {"error": f"no Binance mapping for {ticker}"}

    url_funding = f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={sym}"
    url_oi = f"https://fapi.binance.com/fapi/v1/openInterest?symbol={sym}"

    out: dict[str, Any] = {"binance_symbol": sym}

    try:
        async with aiohttp.ClientSession() as sess:
            # Funding rate via premiumIndex (lastFundingRate)
            try:
                async with sess.get(url_funding, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                    if r.status == 200:
                        data = await r.json()
                        fr = float(data.get("lastFundingRate", 0))  # decimal (es. 0.0001 = 0.01%)
                        mark_price = float(data.get("markPrice", 0))
                        out["funding_rate_pct"] = _round(fr * 100, 4)
                        # Annualized: funding ogni 8h → 3 volte al giorno → *3*365
                        out["funding_rate_annualized_pct"] = _round(fr * 3 * 365 * 100, 2)
                        out["mark_price"] = _round(mark_price, 2)
                        # Interpretazione
                        if fr > 0.0003:  # > 0.03% per 8h → annualizzato ~30%+
                            out["funding_signal"] = "bullish_overcrowded"
                            out["funding_note"] = (
                                "long crowded, rischio long squeeze se trend rallenta"
                            )
                        elif fr < -0.0003:
                            out["funding_signal"] = "bearish_overcrowded"
                            out["funding_note"] = (
                                "short crowded, rischio short squeeze al rialzo"
                            )
                        else:
                            out["funding_signal"] = "neutral"
                            out["funding_note"] = "funding bilanciato, nessuna direzione crowded"
                    else:
                        out["funding_error"] = f"HTTP {r.status}"
            except Exception as e:
                out["funding_error"] = str(e)[:80]

            # Open Interest
            try:
                async with sess.get(url_oi, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                    if r.status == 200:
                        data = await r.json()
                        oi_contracts = float(data.get("openInterest", 0))
                        # OI è in numero di contratti (1 contratto = 1 unit di asset)
                        # Per convertire in USD serve mark price
                        mark_price = out.get("mark_price")
                        if mark_price:
                            out["open_interest_usd"] = _round(oi_contracts * mark_price, 0)
                        out["open_interest_contracts"] = _round(oi_contracts, 2)
                    else:
                        out["oi_error"] = f"HTTP {r.status}"
            except Exception as e:
                out["oi_error"] = str(e)[:80]

        from datetime import datetime, timezone
        out["fetched_at"] = datetime.now(timezone.utc).isoformat()
    except Exception as e:
        return {"error": f"binance fetch failed: {e}", "binance_symbol": sym}

    return _scrub(out)


# ─── 6. Multi-timeframe summary ───────────────────────────────────────────────

def _compact_indicators(df) -> dict:
    """
    Estrae indicatori compatti (RSI 14, MACD signal, trend SMA) da un DataFrame.
    Usato per riassumere ogni timeframe senza saturare il context.
    """
    if df is None or len(df) < 20:
        return {"error": "data insufficiente"}
    try:
        closes = df["Close"]
        # SMA 20 / 50 per trend
        sma20 = float(closes.tail(20).mean())
        sma50 = float(closes.tail(min(50, len(closes))).mean())
        current = float(closes.iloc[-1])

        # RSI 14
        deltas = closes.diff().dropna()
        gains = deltas.where(deltas > 0, 0)
        losses = -deltas.where(deltas < 0, 0)
        avg_gain = gains.tail(14).mean()
        avg_loss = losses.tail(14).mean()
        if avg_loss > 0:
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))
        else:
            rsi = 100.0 if avg_gain > 0 else 50.0

        if current > sma20 > sma50:
            trend = "BULLISH"
        elif current < sma20 < sma50:
            trend = "BEARISH"
        else:
            trend = "MIXED"

        # Variazione % ultime 24 candele
        if len(closes) >= 24:
            change_pct = ((current - float(closes.iloc[-24])) / float(closes.iloc[-24])) * 100
        else:
            change_pct = ((current - float(closes.iloc[0])) / float(closes.iloc[0])) * 100

        return _scrub({
            "current_price": _round(current, 2),
            "sma20": _round(sma20, 2),
            "sma50": _round(sma50, 2),
            "rsi_14": _round(rsi, 1),
            "trend": trend,
            "change_pct_last_24_candles": _round(change_pct, 2),
            "candles": len(df),
        })
    except Exception as e:
        return {"error": f"compact indicators failed: {e}"}


async def fetch_multitimeframe_summary(ticker: str) -> dict:
    """
    Recupera 3 timeframe (1h / 4h / 1d) per il ticker e ritorna un summary
    compatto per ognuno. Per crypto yfinance supporta interval='1h' e '1d';
    il 4h viene risampled da 1h.

    Args:
        ticker: yfinance ticker (es. "BTC-USD")

    Returns:
        {
          "1h": {"current_price": ..., "rsi_14": ..., "trend": ...},
          "4h": {...},
          "1d": {...},
          "confluence": "BULLISH|BEARISH|MIXED",  # 2/3 timeframe d'accordo
        }
    """
    import pandas as pd

    def _sync_yf(t: str, period: str, interval: str):
        try:
            import yfinance as yf
            data = yf.download(
                t, period=period, interval=interval,
                progress=False, auto_adjust=False, prepost=False,
                threads=False,
            )
            if data is None or len(data) == 0:
                return None
            # Drop multi-level columns se presenti
            if isinstance(data.columns, pd.MultiIndex):
                data.columns = [c[0] for c in data.columns]
            return data
        except Exception as e:
            logger.debug("[TECH-ADV] yf %s %s/%s fail: %s", t, period, interval, e)
            return None

    loop = asyncio.get_running_loop()
    # 1h (max 60d), 1d (90d). 4h derivato da 1h.
    try:
        df_1h = await loop.run_in_executor(None, _sync_yf, ticker, "30d", "1h")
        df_1d = await loop.run_in_executor(None, _sync_yf, ticker, "90d", "1d")
    except Exception as e:
        return {"error": f"fetch failed: {e}"}

    out: dict[str, Any] = {}

    if df_1h is not None and len(df_1h) > 20:
        out["1h"] = _compact_indicators(df_1h)
        # Resample a 4h
        try:
            df_4h = df_1h.resample("4h").agg({
                "Open": "first", "High": "max", "Low": "min",
                "Close": "last", "Volume": "sum",
            }).dropna()
            if len(df_4h) > 20:
                out["4h"] = _compact_indicators(df_4h)
        except Exception as e:
            logger.debug("[TECH-ADV] 4h resample failed: %s", e)

    if df_1d is not None and len(df_1d) > 20:
        out["1d"] = _compact_indicators(df_1d)

    # Confluence: maggioranza dei trend
    trends = []
    for tf in ("1h", "4h", "1d"):
        t = (out.get(tf) or {}).get("trend")
        if t in ("BULLISH", "BEARISH", "MIXED"):
            trends.append(t)
    if trends:
        bull = trends.count("BULLISH")
        bear = trends.count("BEARISH")
        if bull >= 2:
            out["confluence"] = "BULLISH"
        elif bear >= 2:
            out["confluence"] = "BEARISH"
        else:
            out["confluence"] = "MIXED"
        out["confluence_note"] = (
            f"{bull} bullish / {bear} bearish / {len(trends)-bull-bear} mixed "
            f"sui {len(trends)} timeframe analizzati"
        )

    return _scrub(out)


# ─── 7. Orchestrator: enrich_ticker(ticker, df) ───────────────────────────────

async def enrich_ticker_advanced(ticker: str, df, include_multitf: bool = True,
                                 include_derivatives: bool = True) -> dict:
    """
    Pipeline completa di arricchimento per un ticker. Chiama tutti i moduli
    e ritorna un blocco unico pronto per essere allegato al ticker_data.

    Args:
        ticker: simbolo (es. "BTC-USD")
        df: DataFrame OHLCV principale (da _fetch_ticker_indicators, daily)
        include_multitf: scarica anche 1h/4h da yfinance (costa ~2s)
        include_derivatives: chiama Binance funding/OI (costa ~1s, solo crypto)

    Returns:
        dict con keys: candlestick_patterns, fibonacci, volume_profile,
                       market_structure, multitimeframe, derivatives
    """
    out: dict[str, Any] = {}

    # 1. Candlestick patterns (sync, veloce)
    try:
        out["candlestick_patterns"] = detect_candlestick_patterns(df, lookback=5)
    except Exception as e:
        out["candlestick_patterns"] = {"error": str(e)[:80]}

    # 2. Fibonacci (sync)
    try:
        out["fibonacci"] = calculate_fibonacci_levels(df, window=60)
    except Exception as e:
        out["fibonacci"] = {"error": str(e)[:80]}

    # 3. Volume profile (sync)
    try:
        out["volume_profile"] = identify_volume_profile(df, bins=20, lookback=60)
    except Exception as e:
        out["volume_profile"] = {"error": str(e)[:80]}

    # 4. Market structure (sync)
    try:
        out["market_structure"] = detect_market_structure(df, swing_lookback=5)
    except Exception as e:
        out["market_structure"] = {"error": str(e)[:80]}

    # 5. Multi-timeframe (async — yfinance)
    if include_multitf:
        try:
            out["multitimeframe"] = await fetch_multitimeframe_summary(ticker)
        except Exception as e:
            out["multitimeframe"] = {"error": str(e)[:80]}

    # 6. Binance derivatives (async — solo crypto)
    if include_derivatives:
        try:
            out["derivatives"] = await fetch_binance_derivatives(ticker)
        except Exception as e:
            out["derivatives"] = {"error": str(e)[:80]}

    return out
