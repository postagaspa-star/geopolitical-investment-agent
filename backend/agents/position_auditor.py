"""
Position Auditor — DeepSeek-V3, revisore TECNICO e IMPARZIALE delle posizioni
aperte.

Perche' esiste: il Decision Agent che ha APERTO una posizione e' in conflitto
d'interessi (vuole avere ragione) e razionalizza i cali come "e' solo un
rintracciamento". L'Auditor e' un secondo paio d'occhi SENZA ego: guarda SOLO i
dati tecnici (struttura HH/HL, BoS/CHoCH, livelli, volume, % dal picco, R), NON
le news e NON sa che le posizioni sono "tue". Per ogni posizione da' un verdetto
freddo HOLD / TRIM / EXIT col motivo TECNICO, distinguendo:
  - rintracciamento SANO (struttura intatta)  -> si tiene/cavalca;
  - INVERSIONE (struttura rotta, CHoCH)        -> si esce;
  - TOPPING / distribuzione (climax + volume)  -> si prende il profitto.

NON e' un blocco meccanico: produce un GIUDIZIO tecnico che il Decision deve
confutare COI DATI (non a parole) per tenere. Le "teeth" misurate (stringere lo
SL su EXIT ad alta conviction + struttura rotta) vivono in enforce_auditor_verdicts.

Modello: DeepSeek-V3 (deepseek-chat), specializzato via PLAYBOOK + RAG sui
documenti crypto + few-shot (NON fine-tuning). Costo ~$0.0006/posizione.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import aiohttp

logger = logging.getLogger(__name__)

DEEPSEEK_MODEL = "deepseek-chat"   # V3
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"


# ════════════════════════════════════════════════════════════════════════════
# PLAYBOOK — la "specializzazione" dell'Auditor (conoscenza di mercato in prompt)
# ════════════════════════════════════════════════════════════════════════════
AUDITOR_PLAYBOOK = """Sei un AUDITOR di posizioni di trading: un tecnico esperto,
freddo e SPIETATAMENTE OGGETTIVO. NON hai aperto tu queste posizioni e NON ti
importa di "aver ragione": il tuo unico compito e' leggere i DATI TECNICI e dire,
posizione per posizione, se va tenuta o chiusa. NON ricevi news ne' narrazioni —
e' voluto: un'uscita e' un evento TECNICO, non di sentiment.

═══════════════════════════════════════════════════════════════════════
COME RAGIONA UN ESPERTO (playbook)
═══════════════════════════════════════════════════════════════════════

1. RINTRACCIAMENTO SANO vs INVERSIONE — e' la distinzione centrale.
   - SANO (si TIENE): il prezzo scende ma la STRUTTURA regge — market_structure
     resta UPTREND, l'ultimo higher-low NON e' rotto, il pullback arriva su un
     supporto/livello Fibonacci (0.382-0.618) con VOLUME in CALO (chi vende si
     esaurisce). Un trend e' fatto di storni: NON si scappa al primo calo.
   - INVERSIONE (si ESCE): la struttura si ROMPE — CHoCH nella direzione opposta,
     lower-low confermato, rottura del supporto chiave con VOLUME in AUMENTO.
     Qui "risale" e' speranza, non analisi: il movimento e' cambiato.

2. TOPPING / DISTRIBUZIONE (si PRENDE il profitto): mossa quasi PARABOLICA +
   climax di VOLUME (spike enorme) + candele di distribuzione (lunghe ombre
   superiori, chiusure deboli sui massimi) + RSI in forte ipercomprato che inizia
   a divergere. Il movimento e' probabilmente ESAURITO: meglio incassare.
   - Caso LOW-CAP / MEMECOIN: low-cap + pump verticale + volume che si esaurisce
     dopo il climax = probabilita' di crollo ALTA. Ma NON e' un dogma ("le meme
     crollano sempre"): leggi i SEGNI (volume in esaurimento, distribuzione,
     divergenza). Se i segni ci sono, alza la probabilita' di EXIT.

3. GIVEBACK del profitto: se la posizione era molto in profitto e ora ha
   restituito gran parte del guadagno dal picco MENTRE la struttura si indebolisce
   -> almeno TRIM. Far correre i vincenti e' giusto, regalare il profitto no.

4. R-multiple e invalidazione: se il prezzo e' oltre il livello che invaliderebbe
   la tesi tecnica (rotto il supporto/struttura), il trade NON e' piu' valido a
   prescindere da quanto "manca poco al recupero".

REGOLE DI GIUDIZIO:
- TIENI: struttura intatta + pullback sano (anche se in perdita momentanea).
- TRIM: profitto in giveback o segnali misti / topping iniziale.
- EXIT: struttura rotta (CHoCH/lower-low) OPPURE topping confermato OPPURE
  invalidazione tecnica.
- In dubbio tra TIENI ed EXIT con struttura AMBIGUA -> TRIM (riduci il rischio).
- confidence alta (>=75) SOLO con segnali tecnici concordi e chiari.

Per le posizioni SHORT, inverti: "scende" e' a tuo favore; l'inversione e' una
rottura di struttura al RIALZO (CHoCH bullish, higher-high).

═══════════════════════════════════════════════════════════════════════
ESEMPI (few-shot)
═══════════════════════════════════════════════════════════════════════
- LONG, +12% poi -4% dal picco, market_structure=UPTREND, higher-low intatto,
  pullback su fib 0.5, volume in calo -> {"verdict":"HOLD","classification":
  "healthy_pullback","technical_reason":"uptrend intatto, HL non rotto, pullback
  su 0.5 fib con volume calante","confidence":78}
- LONG, era +20%, ora CHoCH_bearish + lower-low confermato, volume in aumento sul
  ribasso -> {"verdict":"EXIT","classification":"reversal","technical_reason":
  "CHoCH bearish + lower-low, supporto chiave rotto con volume: trend invertito",
  "confidence":85}
- LONG low-cap, +300% parabolico, volume climax poi in esaurimento, candele di
  distribuzione, RSI 88 in divergenza -> {"verdict":"EXIT","classification":
  "topping","technical_reason":"pump parabolico + climax volume + distribuzione +
  divergenza RSI: top probabile, incassa","confidence":82}

OUTPUT: SOLO JSON, nessun preambolo:
{
  "audits": [
    {"ticker":"BTC-USD","verdict":"HOLD|TRIM|EXIT",
     "classification":"healthy_pullback|reversal|topping|giveback|unclear",
     "technical_reason":"max 1-2 frasi, SOLO tecnica","confidence":0-100}
  ]
}"""


def _get_deepseek_key() -> str:
    return os.environ.get("DEEPSEEK_API_KEY", "")


def _get_auditor_prompt() -> str:
    """Playbook custom da settings, fallback al default."""
    try:
        import database as _db
        custom = _db.get_setting("prompt_position_auditor", "")
        if custom and isinstance(custom, str) and custom.strip():
            return custom
    except Exception:
        pass
    return AUDITOR_PLAYBOOK


def _build_df_from_market_data(market_data: dict):
    import pandas as pd
    if not market_data or not market_data.get("data"):
        return None
    df = pd.DataFrame(market_data["data"])
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        df.set_index("date", inplace=True)
    df.columns = [c.capitalize() for c in df.columns]
    if not {"Open", "High", "Low", "Close", "Volume"}.issubset(set(df.columns)):
        return None
    return df


async def _fetch_technicals(ticker: str) -> dict:
    """Indicatori base + enrichment avanzato (struttura/BoS/CHoCH/fib/volume) per
    UN ticker. Stessa pipeline dei Technical Agent, ma senza chiamata LLM: e' la
    materia prima che l'Auditor legge."""
    try:
        from agents.technical import _fetch_ticker_indicators
        data = await _fetch_ticker_indicators(ticker, period_days=90)
        if not (data and isinstance(data, dict) and data.get("current_price")):
            return {"ticker": ticker, "error": (data or {}).get("error", "no data")}
        try:
            import data_fetchers as _df_mod
            from agents.technical_advanced import enrich_ticker_advanced
            md = await asyncio.get_running_loop().run_in_executor(
                None, _df_mod.fetch_market_data, ticker, 90)
            df = _build_df_from_market_data(md)
            if df is not None and len(df) >= 20:
                data["advanced"] = await enrich_ticker_advanced(
                    ticker, df, include_multitf=True, include_derivatives=True,
                    intervals=["1h", "4h", "1d"])
        except Exception as e:
            data["advanced"] = {"error": str(e)[:120]}
        return data
    except Exception as exc:
        return {"ticker": ticker, "error": str(exc)[:200]}


def _load_crypto_docs(max_chars: int = 6000) -> str:
    """RAG: documenti tecnici crypto come riferimento per l'Auditor."""
    try:
        import database
        docs = database.get_document_contents(category="crypto") or []
        if not docs:
            return ""
        lines = ["DOCUMENTI TECNICI DI RIFERIMENTO (estratti):"]
        used = 0
        for d in docs:
            snippet = f"--- {d.get('filename','?')} ---\n{(d.get('content') or '')[:1400]}"
            if used + len(snippet) > max_chars:
                break
            lines.append(snippet)
            used += len(snippet)
        return "\n\n".join(lines)
    except Exception:
        return ""


def _load_experience_block(max_chars: int = 2500) -> str:
    """FASE 2 — feedback loop: l'Auditor impara dagli ESITI del sistema, non solo
    dal playbook astratto. Inietta (a) la BASE RATE (win-rate sulle chiusure reali
    recenti) e (b) le LEZIONI APPRESE (coach cards). Best-effort: vuoto se mancano.
    Cosi' i verdetti sono calibrati sul track record reale, non su teoria."""
    parts = []
    try:
        import risk_state as _rs
        wr = _rs.get_recent_win_rate(limit=10)
        if wr and wr.get("sample_size"):
            parts.append(
                f"BASE RATE (ultime {wr['sample_size']} chiusure reali): win "
                f"{wr['win_rate'] * 100:.0f}% ({wr['wins']}W/{wr['losses']}L). "
                "Win-rate basso = il sistema tende a TENERE troppo i perdenti: "
                "sii piu' severo sugli EXIT e sul giveback del profitto.")
    except Exception:
        pass
    try:
        from agents import coach_cards as _cc
        block = (_cc.get_active_cards_block_for_decision() or "").strip()
        if block:
            parts.append("LEZIONI APPRESE DAL SISTEMA:\n" + block[:max_chars])
    except Exception:
        pass
    if not parts:
        return ""
    return "ESPERIENZA DEL SISTEMA (impara dai TUOI esiti):\n" + "\n\n".join(parts)


async def _call_v3(context: str, max_retries: int = 2) -> str:
    api_key = _get_deepseek_key()
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY non configurata")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": _get_auditor_prompt()},
            {"role": "user", "content": context},
        ],
        "temperature": 0.2,   # auditor = freddo e consistente
        "max_tokens": 3000,
    }
    last_error = None
    for attempt in range(max_retries):
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(DEEPSEEK_API_URL, json=payload, headers=headers,
                                     timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data["choices"][0]["message"]["content"]
                    last_error = f"HTTP {resp.status}: {(await resp.text())[:200]}"
        except Exception as e:
            last_error = str(e)
        if attempt < max_retries - 1:
            await asyncio.sleep(2 ** (attempt + 1))
    raise ValueError(f"DeepSeek-V3 (auditor) fallito: {last_error}")


def _position_fact_block(p: dict) -> dict:
    """Fatti OGGETTIVI della posizione (no ego: e' "una posizione", non "la tua")."""
    avg = float(p.get("avg_buy_price") or 0)
    cur = float(p.get("current_price") or 0)
    direction = str(p.get("direction") or "LONG").upper()
    if avg > 0 and cur > 0:
        pnl_pct = ((cur - avg) / avg * 100.0) if direction != "SHORT" else ((avg - cur) / avg * 100.0)
    else:
        pnl_pct = 0.0
    return {
        "ticker": p.get("ticker"), "direction": direction,
        "entry": round(avg, 6), "current": round(cur, 6),
        "unrealized_pnl_pct": round(pnl_pct, 2),
        "stop_loss": float(p.get("stop_loss_price") or 0),
    }


async def audit_positions(run_id: str, positions: list | None = None) -> dict:
    """Verdetto tecnico per ogni posizione aperta. Ritorna {ticker: verdict_dict}.
    Best-effort: senza API key / senza posizioni ritorna {}."""
    import database
    if positions is None:
        try:
            positions = database.get_positions() or []
        except Exception:
            positions = []
    positions = [p for p in positions if p.get("ticker")
                 and float(p.get("quantity") or 0) > 0]
    if not positions:
        return {}
    if not _get_deepseek_key():
        logger.info("[%s][AUDITOR] DEEPSEEK_API_KEY assente — audit saltato", run_id)
        return {}

    # Materia prima tecnica per ogni posizione (bounded).
    sem = asyncio.Semaphore(4)

    async def _one(p):
        async with sem:
            tech = await _fetch_technicals(p.get("ticker"))
            return _position_fact_block(p), tech

    pairs = await asyncio.gather(*[_one(p) for p in positions])
    items = [{"position": fact, "technicals": tech}
             for fact, tech in pairs if tech and not tech.get("error")]
    if not items:
        return {}

    try:
        from agents.technical import _clean_for_json
        payload = _clean_for_json({"positions_to_audit": items,
                                   "timestamp": datetime.now(timezone.utc).isoformat()})
    except Exception:
        payload = {"positions_to_audit": items}
    context = json.dumps(payload, default=str, ensure_ascii=False)
    # Fase 2: esperienza (base-rate + lezioni) + Fase 1: documenti tecnici (RAG).
    prefix = "\n\n".join(b for b in (_load_experience_block(), _load_crypto_docs()) if b)
    if prefix:
        context = prefix + "\n\n" + "=" * 60 + "\n\n" + context

    try:
        raw = await _call_v3(context[:32000])
    except Exception as exc:
        logger.warning("[%s][AUDITOR] V3 fallito: %s", run_id, exc)
        return {}

    verdicts: dict = {}
    try:
        s, e = raw.find("{"), raw.rfind("}") + 1
        report = json.loads(raw[s:e]) if s >= 0 and e > s else {}
    except json.JSONDecodeError:
        report = {}
    for a in (report.get("audits") or []):
        tk = (a.get("ticker") or "").upper().strip()
        if not tk:
            continue
        verdicts[tk] = {
            "verdict": str(a.get("verdict") or "HOLD").upper(),
            "classification": a.get("classification") or "unclear",
            "technical_reason": str(a.get("technical_reason") or "")[:400],
            "confidence": a.get("confidence"),
        }
    try:
        database.insert_agent_log(run_id, "POSITION_AUDIT", json.dumps({
            "verdicts": verdicts, "audited": len(items),
        }, default=str))
    except Exception:
        pass
    return verdicts


def format_auditor_block(verdicts: dict) -> str:
    """Blocco testo da iniettare nel contesto del Decision: sfida tecnica
    obbligatoria. Vuoto se nessun verdetto rilevante."""
    if not verdicts:
        return ""
    flagged = {t: v for t, v in verdicts.items()
               if str(v.get("verdict")).upper() in ("EXIT", "TRIM")}
    if not flagged:
        return ""
    lines = [
        "═" * 60,
        "POSITION AUDITOR — analisi TECNICA indipendente e SENZA bias delle tue",
        "posizioni aperte (guarda solo i dati tecnici, non le news). REGOLA: per",
        "TENERE una posizione segnalata EXIT/TRIM devi CONFUTARLA coi DATI TECNICI",
        "(il livello/struttura precisa che la salva). 'E' un rintracciamento' NON",
        "basta: serve il dato. I TECNICI pesano piu' delle news, e un'uscita e' un",
        "evento tecnico.",
        "CONFUTAZIONE VALIDA = SOLO struttura/livelli/pattern (supporto che tiene,",
        "fib/HVN, higher-low intatto, divergenza confermata). INAMMISSIBILE basarsi",
        "su P&L, prezzo d'ingresso, 'break-even', 'sono in pari', 'perderei poco':",
        "e' il BIAS che eliminiamo — se la tua UNICA difesa e' quella, ESCI. E in",
        "ogni caso metti lo SL al livello tecnico che invalida la tesi.",
    ]
    for t, v in flagged.items():
        c = v.get("confidence")
        lines.append(f"  • {t}: {v['verdict']} [{v.get('classification')}] "
                     f"(conf {c}) — {v.get('technical_reason')}")
    lines.append("═" * 60)
    return "\n".join(lines)


def enforce_auditor_verdicts(run_id: str, verdicts: dict, positions: list | None = None) -> list:
    """Teeth MISURATE: su EXIT ad ALTA conviction con struttura ROTTA
    (classification reversal/topping), stringe lo SL appena sotto (long) / sopra
    (short) il prezzo corrente, cosi' enforce_stops chiude in fretta se il calo
    continua, ma un eventuale recupero (storno sano) sopravvive. NON market-dumpa.
    Ritorna i ticker su cui ha agito."""
    import database
    acted: list = []
    if not verdicts:
        return acted
    try:
        if positions is None:
            positions = database.get_positions() or []
        by_ticker = {p.get("ticker"): p for p in positions if p.get("ticker")}
    except Exception:
        by_ticker = {}
    for tk, v in verdicts.items():
        try:
            if str(v.get("verdict")).upper() != "EXIT":
                continue
            if str(v.get("classification")) not in ("reversal", "topping"):
                continue
            conf = float(v.get("confidence") or 0)
            if conf < 75:
                continue
            p = by_ticker.get(tk)
            if not p:
                continue
            cur = float(p.get("current_price") or 0)
            if cur <= 0:
                continue
            is_short = str(p.get("direction") or "LONG").upper() == "SHORT"
            # SL stretto appena oltre il prezzo (0.5%): se prosegue, enforce_stops esce.
            new_sl = round(cur * (1.005 if is_short else 0.995), 6)
            database.update_position_auto_exit(tk, stop_loss_price=new_sl,
                                               set_by="position_auditor")
            database.insert_agent_log(run_id, "POSITION_AUDIT_TEETH", json.dumps({
                "ticker": tk, "tightened_sl": new_sl, "price": cur,
                "classification": v.get("classification"), "confidence": conf,
            }, default=str))
            acted.append(tk)
        except Exception as e:
            logger.debug("[%s][AUDITOR] teeth %s fail: %s", run_id, tk, e)
    return acted


# Termini che rendono una confutazione INAMMISSIBILE (bias contabile / speranza)
# e termini TECNICI ammessi. Usati dal validatore leggero post-decisione.
_BIAS_TERMS = ("break-even", "breakeven", "break even", "in pari", "sono in pari",
               "quasi in pari", "p&l", "pnl", " p/l", "perderei", "perdo poco",
               "perdo solo", "poco sotto", "prezzo d'ingresso", "prezzo di ingresso",
               "entry price", "carico medio", "non perdo molto")
_TECH_TERMS = ("struttura", "supporto", "resistenza", "fib", "hvn", "lvn", "bos",
               "choch", "engulfing", "rsi", "macd", "ema", "livello", "volume",
               "higher-low", "lower-low", "higher high", "lower high", "swing",
               "divergenza", "trend", "breakout", "ipervenduto", "ipercomprato",
               "hammer", "doji")


def flag_bias_holds(decision_text: str, exit_tickers: list) -> list:
    """Validatore LEGGERO della confutazione. Per ogni ticker che l'Auditor aveva
    segnalato EXIT, guarda la finestra di testo attorno alla sua menzione nel
    ragionamento del Decision: se la difesa si regge su termini di BIAS (P&L,
    break-even, prezzo d'ingresso) e NON cita struttura/livelli/pattern, e' una
    confutazione INAMMISSIBILE -> l'EXIT deve prevalere. Ritorna quei ticker."""
    if not decision_text or not exit_tickers:
        return []
    low = str(decision_text).lower()
    flagged = []
    for tk in exit_tickers:
        base = str(tk or "").upper().replace("-USD", "").replace("X:", "").strip()
        if not base:
            continue
        idx = low.find(base.lower())
        if idx < 0:
            continue
        window = low[max(0, idx - 80): idx + 280]
        has_bias = any(b in window for b in _BIAS_TERMS)
        has_tech = any(t in window for t in _TECH_TERMS)
        if has_bias and not has_tech:
            flagged.append(tk)
    return flagged


def enforce_bias_holds(run_id: str, tickers: list, positions: list | None = None) -> list:
    """Per i ticker la cui confutazione e' risultata BIAS-based (flag_bias_holds),
    stringe lo SL appena oltre il prezzo (come le teeth): l'EXIT dell'Auditor
    prevale di fatto se il prezzo prosegue. Ritorna i ticker su cui ha agito."""
    import database
    acted = []
    if not tickers:
        return acted
    try:
        if positions is None:
            positions = database.get_positions() or []
        by_ticker = {p.get("ticker"): p for p in positions if p.get("ticker")}
    except Exception:
        by_ticker = {}
    for tk in tickers:
        try:
            p = by_ticker.get(tk)
            if not p:
                continue
            cur = float(p.get("current_price") or 0)
            if cur <= 0:
                continue
            is_short = str(p.get("direction") or "LONG").upper() == "SHORT"
            new_sl = round(cur * (1.005 if is_short else 0.995), 6)
            database.update_position_auto_exit(tk, stop_loss_price=new_sl,
                                               set_by="auditor_bias_reject")
            database.insert_agent_log(run_id, "POSITION_AUDIT_BIAS_REJECT", json.dumps({
                "ticker": tk, "tightened_sl": new_sl, "price": cur,
                "reason": "confutazione bias-based (P&L/entry): EXIT prevale",
            }, default=str))
            acted.append(tk)
        except Exception as e:
            logger.debug("[%s][AUDITOR] bias-reject %s fail: %s", run_id, tk, e)
    return acted
