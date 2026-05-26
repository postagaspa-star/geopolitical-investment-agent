"""
CryptoMonitor — sorveglianza intelligente delle posizioni crypto aperte.

Filosofia: il Decision Crypto Agent decide cosa comprare/vendere su base
tesi macro + tecnica, ma gira solo ogni ora. Le crypto si muovono in pochi
minuti, quindi serve un agente leggero che monitora costantemente lo stato
del trend di OGNI posizione aperta e capisce in autonomia quando il trend
si sta esaurendo o invertendo, senza regole hard-coded.

Cosa fa:
  1. Ogni 15 minuti, legge le posizioni crypto aperte (ticker.endswith("-USD"))
  2. Per ognuna fetcha ~25 candele da 15 min (last ~6h) via yfinance
  3. Chiama DeepSeek-V3 con prompt strutturato che chiede di valutare,
     per ogni asset, se il trend di entrata e' ancora sano oppure mostra
     segnali di esaurimento/inversione (volume divergence, candle pattern,
     RSI divergence, momentum decay).
  4. Parsa la risposta JSON: per ogni asset → verdict HEALTHY / WARNING /
     REVERSAL_CONFIRMED + reasoning + suggested_action.
  5. Per REVERSAL_CONFIRMED:
     - logga un agent_log critico (visibile in dashboard)
     - se l'utente ha abilitato auto_tighten_sl, restringe lo stop-loss
       attorno al prezzo corrente (-1% delta) per proteggere il profitto
     - schedula una run urgente del Decision Crypto col contesto del segnale
  6. Per WARNING: solo log, l'utente lo vede.

NON esegue trade autonomi. Si limita a:
  - alzare alert
  - opzionalmente restringere SL (proteggere capitale, non chiudere)
  - triggerare un run del Decision Crypto che ha il contesto completo

Costo: DeepSeek-V3 (~$0.0001 per chiamata, 24/7 = 96 run/giorno =
~$0.01/giorno totale).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone, timedelta
from typing import Any
from uuid import uuid4

import aiohttp

logger = logging.getLogger(__name__)

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_V3_MODEL = "deepseek-chat"   # V3: ~10x piu' economico di R1, fast

# Setting key per disabilitare totalmente il monitor (default: enabled)
SETTING_ENABLED = "crypto_monitor_enabled"
# Setting key per abilitare auto-tighten-SL (default: disabled).
# Quando enabled, su REVERSAL_CONFIRMED con confidence >= 0.75 il monitor
# restringe lo stop-loss attorno al prezzo corrente per proteggere il profitto.
SETTING_AUTO_TIGHTEN = "crypto_monitor_auto_tighten_sl"
# Cooldown per ticker (sec): non rilanciare alert sullo stesso ticker piu'
# spesso di questo intervallo (default 60 min).
SETTING_PER_TICKER_COOLDOWN_SEC = "crypto_monitor_ticker_cooldown_sec"
DEFAULT_TICKER_COOLDOWN_SEC = 60 * 60   # 1h


CRYPTO_MONITOR_PROMPT = """Sei un Trend Health Monitor specializzato in crypto.
Il tuo lavoro: analizzare le candele 15-min di ogni posizione crypto del
portafoglio e dire, per ognuna, se il trend di entrata e' ancora sano oppure
mostra segnali tecnici di esaurimento / inversione.

Sei un osservatore tecnico, NON un consulente strategico. Non proponi nuovi
ingressi. Per ogni asset rispondi solo se il trend e' HEALTHY, in WARNING di
esaurimento, o REVERSAL_CONFIRMED. La decisione finale di exit la prende il
Decision Agent — tu ALZI L'ALLARME.

═══════════════════════════════════════════════════════════════════════
COSA OSSERVARE (esempi, non checklist rigida)
═══════════════════════════════════════════════════════════════════════

Segnali di esaurimento (WARNING):
  - Volume in divergenza: il prezzo fa nuovi massimi/minimi locali ma il
    volume cala vistosamente.
  - Range compression dopo trend: candele sempre piu' piccole, indecisione.
  - RSI implicito in divergenza (calcolalo mentalmente dalle chiusure):
    prezzo nuovo massimo ma momentum delle ultime N candele in calo.
  - Wick lunghi nella direzione del trend non confermati dalla chiusura.

Segnali di inversione confermata (REVERSAL_CONFIRMED):
  - Engulfing pattern AVERSO al trend + volume sopra la media.
  - Breakout chiaro di una struttura di supporto / resistenza locale,
    confermato da una candela di follow-through.
  - 2-3 candele consecutive contro-trend di ampiezza > media recente.
  - Volume spike (>= 2.5x media) con candela bearish dopo un long trend up.

Trend sano (HEALTHY):
  - Candele coerenti col trend, pullback shallow con volume basso,
    ripresa rapida con volume sopra la media.
  - Nessuno dei segnali sopra.

═══════════════════════════════════════════════════════════════════════
INPUT
═══════════════════════════════════════════════════════════════════════

Ti fornisco un JSON con un array `positions`. Ogni posizione contiene:
  - ticker
  - avg_buy_price: prezzo medio di carico
  - current_price: prezzo corrente
  - unrealized_pnl_pct: P&L percentuale corrente
  - stop_loss_price, take_profit_price (se settati)
  - bars_15m: ARRAY di candele 15-min dalla piu' VECCHIA alla piu' RECENTE.
    Ogni candela = [timestamp_iso, open, high, low, close, volume].

═══════════════════════════════════════════════════════════════════════
OUTPUT — JSON RIGOROSAMENTE QUESTO FORMATO
═══════════════════════════════════════════════════════════════════════

{
  "analyses": [
    {
      "ticker": "BTC-USD",
      "verdict": "HEALTHY" | "WARNING" | "REVERSAL_CONFIRMED",
      "confidence": 0.0-1.0,
      "trend_so_far": "UP" | "DOWN" | "RANGE",
      "signals_seen": ["volume_divergence", "bearish_engulfing", "..."],
      "reasoning": "1-3 frasi su cosa hai notato",
      "suggested_action": "HOLD" | "TIGHTEN_SL" | "ALERT_DECISION_AGENT"
    },
    ...
  ]
}

REGOLE OUTPUT:
- UN oggetto per ogni ticker in input — niente di piu', niente di meno.
- "confidence" >= 0.7 solo se ci sono almeno 2 segnali concordi.
- "suggested_action":
    HOLD = trend sano o segnali deboli
    TIGHTEN_SL = warning forte ma non ancora confermato (proteggi profitto)
    ALERT_DECISION_AGENT = reversal confermata → il Decision Agent deve
      rivalutare se chiudere (tesi originale potrebbe essere invalidata).
- Niente testo fuori dal JSON. Niente blocchi ```. JSON puro."""


# ─── Helpers ────────────────────────────────────────────────────────────────

def _get_deepseek_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    try:
        import database
        return (database.get_setting("deepseek_api_key", "") or "").strip()
    except Exception:
        return ""


def _is_crypto_ticker(ticker: str) -> bool:
    if not ticker:
        return False
    t = ticker.upper().strip()
    return t.endswith("-USD") or t.startswith("X:")


def _get_crypto_positions() -> list[dict]:
    """Ritorna SOLO posizioni crypto aperte (qty > 0)."""
    try:
        import database
        positions = database.get_positions() or []
    except Exception as e:
        logger.warning("[CRYPTO-MONITOR] read positions failed: %s", e)
        return []
    out = []
    for p in positions:
        try:
            t = (p.get("ticker") or "").upper().strip()
            qty = float(p.get("quantity") or 0)
            if not t or qty <= 0:
                continue
            if not _is_crypto_ticker(t):
                continue
            out.append(p)
        except Exception:
            continue
    return out


def _fetch_15m_bars_for_ticker(ticker: str, bars: int = 25) -> list[list]:
    """
    Scarica le ultime N candele a 15 minuti via yfinance.
    Crypto su yfinance accetta `interval=15m` con `period=7d` (limite ~60g).
    Ritorna lista di [ts_iso, open, high, low, close, volume].
    """
    try:
        import yfinance
        # Period 2g e' suff. per 96 candele 15m (24h * 4 = 96/giorno).
        # Usiamo 5d per resilienza ai weekend low-volume (crypto 24/7 ok,
        # ma il rate-limit di yfinance puo' ritornare meno dati).
        df = yfinance.download(
            ticker,
            period="5d",
            interval="15m",
            progress=False,
            auto_adjust=False,
            threads=False,
        )
        if df is None or df.empty:
            return []
        # Se multi-ticker (download accetta lista, qui no), df e' single-level.
        # Prendi ultime N righe.
        tail = df.tail(bars)
        out = []
        for idx, row in tail.iterrows():
            try:
                ts = idx.to_pydatetime().replace(tzinfo=timezone.utc).isoformat()
                # Handle MultiIndex columns (rare per single ticker, ma defensive)
                if hasattr(row, "to_dict"):
                    d = row.to_dict()
                else:
                    continue
                def _g(key):
                    v = d.get(key)
                    if v is None and isinstance(d, dict):
                        # MultiIndex case: keys are tuples
                        for k, val in d.items():
                            if (isinstance(k, tuple) and k[0] == key) or k == key:
                                v = val
                                break
                    return float(v) if v is not None else 0.0
                o = _g("Open")
                h = _g("High")
                l = _g("Low")
                c = _g("Close")
                v = _g("Volume")
                if c <= 0:
                    continue
                out.append([ts, round(o, 4), round(h, 4),
                             round(l, 4), round(c, 4), int(v)])
            except Exception:
                continue
        return out
    except Exception as e:
        logger.warning("[CRYPTO-MONITOR] fetch 15m bars %s failed: %s", ticker, e)
        return []


async def _build_payload(positions: list[dict]) -> dict:
    """Costruisce il dict da serializzare nel prompt user."""
    loop = asyncio.get_running_loop()

    async def _one(pos):
        t = pos.get("ticker")
        bars = await loop.run_in_executor(None, _fetch_15m_bars_for_ticker, t, 25)
        avg = float(pos.get("avg_buy_price") or 0)
        cur = float(pos.get("current_price") or avg)
        direction = (pos.get("direction") or "LONG").upper()
        # DIRECTION-AWARE: SHORT guadagna se prezzo scende
        if direction == "SHORT":
            pnl_pct = ((avg - cur) / avg * 100.0) if avg > 0 else 0.0
        else:
            pnl_pct = ((cur - avg) / avg * 100.0) if avg > 0 else 0.0
        return {
            "ticker": t,
            "direction": direction,
            "avg_buy_price": round(avg, 6),
            "current_price": round(cur, 6),
            "unrealized_pnl_pct": round(pnl_pct, 2),
            "quantity": float(pos.get("quantity") or 0),
            "stop_loss_price": float(pos.get("stop_loss_price") or 0),
            "take_profit_price": float(pos.get("take_profit_price") or 0),
            "bars_15m": bars,
        }

    results = await asyncio.gather(*[_one(p) for p in positions], return_exceptions=True)
    out = []
    for r in results:
        if isinstance(r, dict) and r.get("bars_15m"):
            out.append(r)
    return {"positions": out}


async def _call_deepseek_v3(system_prompt: str, user_content: str) -> str:
    """Chiama DeepSeek-V3 (deepseek-chat) con retry esponenziale."""
    api_key = _get_deepseek_key()
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY non configurata")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": DEEPSEEK_V3_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": 3000,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }

    last_err = None
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    DEEPSEEK_API_URL, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data["choices"][0]["message"]["content"]
                    elif resp.status == 429:
                        await asyncio.sleep(2 ** (attempt + 1))
                        continue
                    else:
                        body = await resp.text()
                        last_err = f"HTTP {resp.status}: {body[:200]}"
        except Exception as e:
            last_err = str(e)
            if attempt < 2:
                await asyncio.sleep(2 ** (attempt + 1))

    raise ValueError(f"DeepSeek V3 failed after 3 retries: {last_err}")


def _parse_response(raw: str) -> list[dict]:
    """Estrae l'array `analyses` dal raw text del modello."""
    if not raw:
        return []
    text = raw.strip()
    # Strip optional ```json fences
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    # Find first { and last }
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(obj, dict):
        return []
    analyses = obj.get("analyses") or []
    if not isinstance(analyses, list):
        return []

    # Normalize each analysis
    valid_verdicts = ("HEALTHY", "WARNING", "REVERSAL_CONFIRMED")
    valid_actions = ("HOLD", "TIGHTEN_SL", "ALERT_DECISION_AGENT")
    out = []
    for a in analyses:
        if not isinstance(a, dict):
            continue
        t = str(a.get("ticker", "")).upper().strip()
        v = str(a.get("verdict", "")).upper().strip()
        if not t or v not in valid_verdicts:
            continue
        try:
            conf = max(0.0, min(1.0, float(a.get("confidence", 0))))
        except (TypeError, ValueError):
            conf = 0.0
        act = str(a.get("suggested_action", "")).upper().strip()
        if act not in valid_actions:
            act = "HOLD"
        out.append({
            "ticker": t,
            "verdict": v,
            "confidence": round(conf, 2),
            "trend_so_far": str(a.get("trend_so_far", "")).upper()[:6],
            "signals_seen": [str(s)[:40] for s in (a.get("signals_seen") or [])[:6]],
            "reasoning": str(a.get("reasoning", ""))[:600],
            "suggested_action": act,
        })
    return out


# ─── Cooldown management ───────────────────────────────────────────────────

def _get_ticker_cooldown_sec() -> int:
    try:
        import database
        raw = database.get_setting(SETTING_PER_TICKER_COOLDOWN_SEC, "")
        if raw:
            return max(60, int(float(raw)))
    except Exception:
        pass
    return DEFAULT_TICKER_COOLDOWN_SEC


# Cache in-memory: ticker → ultimo alert ts (sec epoch). Si resetta a riavvio.
_last_alert_ts: dict[str, float] = {}


def _is_cooldown_active(ticker: str) -> bool:
    import time as _t
    last = _last_alert_ts.get(ticker.upper(), 0.0)
    return (_t.time() - last) < _get_ticker_cooldown_sec()


def _mark_alert_sent(ticker: str) -> None:
    import time as _t
    _last_alert_ts[ticker.upper()] = _t.time()


# ─── Action handlers ────────────────────────────────────────────────────────

def _tighten_stop_loss(ticker: str, current_price: float, avg: float,
                        direction: str = "LONG") -> dict:
    """
    Restringe lo stop-loss attorno al prezzo corrente per proteggere
    profitto (se in profitto) o limitare perdita (se in leggera perdita).

    DIRECTION-AWARE: per le SHORT i segni sono invertiti.
      - LONG:  SL sotto il prezzo (esce se scende). P&L positivo se prezzo > avg.
      - SHORT: SL sopra il prezzo (esce se sale). P&L positivo se prezzo < avg.
    """
    if avg <= 0 or current_price <= 0:
        return {"ok": False, "error": "prezzi invalidi"}
    is_short = str(direction or "LONG").upper() == "SHORT"

    if is_short:
        # SHORT: profitto se cur < avg → P&L pct = (avg - cur)/avg
        pnl_pct = (avg - current_price) / avg * 100.0
    else:
        pnl_pct = (current_price - avg) / avg * 100.0

    if pnl_pct <= -3:
        return {"ok": False, "skipped": True, "reason": "pnl_too_negative_for_tighten"}

    if is_short:
        # SHORT: SL SOPRA il prezzo. Se in profitto, lo stringo a +1% sopra il
        # current (lock-in del gain). Se in leggera perdita, +1.5% sopra.
        if pnl_pct >= 0:
            new_sl = round(current_price * 1.01, 6)
        else:
            new_sl = round(current_price * 1.015, 6)
    else:
        # LONG: SL SOTTO il prezzo (logica originale).
        if pnl_pct >= 0:
            new_sl = round(current_price * 0.99, 6)
        else:
            new_sl = round(current_price * 0.985, 6)
    try:
        import database
        database.update_position_auto_exit(
            ticker, stop_loss_price=new_sl, set_by="crypto_monitor_auto_tighten",
        )
        return {"ok": True, "new_stop_loss": new_sl, "pnl_pct_at_tighten": round(pnl_pct, 2),
                "direction": "SHORT" if is_short else "LONG"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _is_auto_tighten_enabled() -> bool:
    try:
        import database
        raw = (database.get_setting(SETTING_AUTO_TIGHTEN, "false") or "").strip().lower()
        return raw in ("1", "true", "yes", "on")
    except Exception:
        return False


def _record_commitment_for_decision(ticker: str, analysis: dict, run_id: str) -> None:
    """
    Registra un agent_commitment "crypto monitor alert" che verra' iniettato
    automaticamente nel prossimo run del Decision Crypto, cosi' l'agente
    sa che c'e' un alert pendente da rivalutare.
    """
    try:
        import database
        text = (
            f"[CryptoMonitor] {ticker}: verdict={analysis['verdict']} "
            f"conf={analysis['confidence']:.2f} — {analysis['reasoning'][:250]}"
        )
        # Usa add_agent_commitment se disponibile, altrimenti agent_log puro.
        if hasattr(database, "add_agent_commitment"):
            try:
                database.add_agent_commitment(
                    agent_type="crypto",
                    commitment_type="monitor_alert",
                    commitment_text=text[:500],
                    related_ticker=ticker,
                    expires_in_hours=4,
                )
                return
            except Exception as e:
                logger.debug("[CRYPTO-MONITOR] add_agent_commitment fail: %s", e)
        # Fallback: insert agent_log come signal persistente
        database.insert_agent_log(run_id, "CRYPTO_MONITOR_ALERT", json.dumps({
            "event": "monitor_alert",
            "ticker": ticker,
            "verdict": analysis["verdict"],
            "confidence": analysis["confidence"],
            "suggested_action": analysis["suggested_action"],
            "signals": analysis.get("signals_seen", []),
            "reasoning": analysis.get("reasoning", ""),
        }, default=str))
    except Exception as e:
        logger.warning("[CRYPTO-MONITOR] record_commitment fail: %s", e)


# ─── Public API ─────────────────────────────────────────────────────────────

async def run_crypto_monitor(run_id: str | None = None) -> dict:
    """
    Entry point del monitor: gira UNA volta, analizza tutte le posizioni
    crypto aperte, logga alert e (opzionalmente) restringe SL.

    Ritorna un dict con:
      run_id, positions_analyzed, alerts (lista), tightened (lista), errors.
    """
    if not run_id:
        run_id = str(uuid4())[:8]

    started = datetime.now(timezone.utc)

    # 0. Gate: monitor abilitato?
    try:
        import database
        raw_enabled = (database.get_setting(SETTING_ENABLED, "true") or "").strip().lower()
        if raw_enabled in ("0", "false", "no", "off"):
            return {"run_id": run_id, "skipped": True, "reason": "disabled_setting"}
    except Exception:
        pass

    # 1. Carica posizioni crypto
    positions = _get_crypto_positions()
    if not positions:
        return {"run_id": run_id, "skipped": True, "reason": "no_crypto_positions",
                "positions_analyzed": 0}

    logger.info("[%s][CRYPTO-MONITOR] %d posizioni crypto da analizzare",
                run_id, len(positions))

    # 2. Costruisci payload (fetch OHLCV 15m per ogni asset, parallelo)
    payload = await _build_payload(positions)
    valid_positions = payload.get("positions", [])
    if not valid_positions:
        return {"run_id": run_id, "skipped": True, "reason": "no_bars_fetched",
                "positions_analyzed": 0}

    user_content = (
        f"timestamp UTC: {started.isoformat()}\n"
        f"posizioni da analizzare: {len(valid_positions)}\n\n"
        f"INPUT:\n```json\n"
        f"{json.dumps(payload, indent=2, ensure_ascii=False, default=str)[:20000]}\n```"
    )

    # 3. Chiama DeepSeek V3
    try:
        raw = await _call_deepseek_v3(CRYPTO_MONITOR_PROMPT, user_content)
    except Exception as e:
        logger.error("[%s][CRYPTO-MONITOR] LLM call failed: %s", run_id, e)
        try:
            import database
            database.insert_agent_log(run_id, "CRYPTO_MONITOR_ERROR", json.dumps({
                "event": "llm_error", "error": str(e)[:300],
            }))
        except Exception:
            pass
        return {"run_id": run_id, "error": str(e), "positions_analyzed": len(valid_positions)}

    analyses = _parse_response(raw)
    if not analyses:
        try:
            import database
            database.insert_agent_log(run_id, "CRYPTO_MONITOR_PARSE_FAIL", json.dumps({
                "event": "parse_failed", "raw_excerpt": raw[:1200],
            }))
        except Exception:
            pass
        return {"run_id": run_id, "error": "parse_failed",
                "positions_analyzed": len(valid_positions),
                "raw_excerpt": raw[:600]}

    # 4. Agisci per ogni analisi
    alerts = []
    tightened = []
    healthy_count = 0
    auto_tighten = _is_auto_tighten_enabled()

    # Mappa ticker → posizione per accessi veloci
    pos_by_ticker = {p["ticker"]: p for p in valid_positions}

    for a in analyses:
        ticker = a["ticker"]
        verdict = a["verdict"]
        conf = a["confidence"]

        if verdict == "HEALTHY":
            healthy_count += 1
            continue

        # Cooldown per ticker: evita alert ripetuti sullo stesso ticker
        if _is_cooldown_active(ticker):
            logger.debug("[%s][CRYPTO-MONITOR] %s: cooldown attivo, skip alert",
                         run_id, ticker)
            continue

        alerts.append(a)
        _mark_alert_sent(ticker)

        # 4a. Logga alert visibile in dashboard
        try:
            import database
            database.insert_agent_log(run_id, "CRYPTO_MONITOR_ALERT", json.dumps({
                "event": "monitor_alert",
                "ticker": ticker,
                "verdict": verdict,
                "confidence": conf,
                "trend": a.get("trend_so_far"),
                "signals": a.get("signals_seen", []),
                "suggested_action": a.get("suggested_action"),
                "reasoning": a.get("reasoning", ""),
            }, default=str))
        except Exception as e:
            logger.warning("[CRYPTO-MONITOR] log alert fail: %s", e)

        # 4b. Inietta come commitment per il prossimo Decision Crypto
        _record_commitment_for_decision(ticker, a, run_id)

        # 4c. Se REVERSAL_CONFIRMED + auto_tighten abilitato, restringi SL
        if (verdict == "REVERSAL_CONFIRMED" and conf >= 0.75
                and a.get("suggested_action") == "TIGHTEN_SL"
                and auto_tighten):
            p = pos_by_ticker.get(ticker, {})
            res = _tighten_stop_loss(
                ticker,
                current_price=float(p.get("current_price") or 0),
                avg=float(p.get("avg_buy_price") or 0),
                direction=(p.get("direction") or "LONG"),
            )
            if res.get("ok"):
                tightened.append({"ticker": ticker, **res})

    duration = (datetime.now(timezone.utc) - started).total_seconds()

    # 5. Log riassuntivo
    try:
        import database
        database.insert_agent_log(run_id, "CRYPTO_MONITOR", json.dumps({
            "event": "monitor_complete",
            "positions_analyzed": len(valid_positions),
            "healthy": healthy_count,
            "alerts": len(alerts),
            "tightened_sl": len(tightened),
            "auto_tighten_enabled": auto_tighten,
            "duration_seconds": round(duration, 1),
            "model": DEEPSEEK_V3_MODEL,
        }, default=str))
    except Exception:
        pass

    logger.info(
        "[%s][CRYPTO-MONITOR] complete: %d pos analizzate, %d healthy, "
        "%d alert, %d SL tightened (%.1fs)",
        run_id, len(valid_positions), healthy_count,
        len(alerts), len(tightened), duration,
    )

    return {
        "run_id": run_id,
        "positions_analyzed": len(valid_positions),
        "healthy": healthy_count,
        "alerts": alerts,
        "tightened_sl": tightened,
        "duration_seconds": round(duration, 1),
        "model": DEEPSEEK_V3_MODEL,
    }
