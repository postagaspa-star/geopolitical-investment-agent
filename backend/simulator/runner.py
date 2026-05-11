"""
Runner del Simulator: esegue uno scenario chiamando un LLM con il prompt
strutturato (lettura contesto / ragionamento / decisione).

Engine: DeepSeek-R1 reasoning (no Anthropic dependency, ridotti costi).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from uuid import uuid4
from typing import Optional

import aiohttp

from simulator import scenarios, db as sim_db

logger = logging.getLogger(__name__)

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_R1 = "deepseek-reasoner"

# In-memory state dei run attivi (single-step + multi-step in corso).
# Su Render i pod si riavviano (deploy, scaling, idle), il dict si svuota
# → multi-step si rompeva tra step 0 e step 1 con HTTP 500 "Run not found".
# Soluzione: SHADOW PERSIST su sim_settings come JSON, key
#   _sim_active_run::{run_id}
# Caricato lazy in execute_step() se manca dal dict in-memory.
#
# THREAD SAFETY: il dict e' acceduto da multiple coroutines (start_run,
# execute_step, _finalize_run, rebuild_run_from_memory). asyncio non garantisce
# atomicita' su read-modify-write tra due await. Lock per gli accessi mutativi.
import threading as _threading
_active_runs: dict[str, dict] = {}
_active_runs_lock = _threading.Lock()
_ACTIVE_RUN_KEY_PREFIX = "_sim_active_run::"
_ACTIVE_RUN_TTL_HOURS = 24   # cleanup degli abbandonati dopo 24h


def _persist_active_run(run_id: str, state: dict) -> None:
    """Shadow-persiste lo state corrente su sim_settings (cross-restart)."""
    try:
        from simulator import db as sim_db
        # NOTA: lo scenario completo include market_data + headlines già
        # serializzabili. Lo step list contiene oggetti string-only.
        payload = json.dumps(state, default=str, ensure_ascii=False)
        sim_db.set_setting(f"{_ACTIVE_RUN_KEY_PREFIX}{run_id}", payload)
    except Exception as e:
        logger.warning("[SIM] persist_active_run %s fallita: %s", run_id, e)


def _load_active_run(run_id: str) -> dict | None:
    """Carica lo state shadow-persistito. None se assente o corrotto."""
    try:
        from simulator import db as sim_db
        raw = sim_db.get_setting(f"{_ACTIVE_RUN_KEY_PREFIX}{run_id}", "")
        if not raw:
            return None
        state = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(state, dict):
            return None
        # Verifica TTL: se started_at > 24h fa, lo trattiamo come scaduto
        started = state.get("started_at")
        if started:
            try:
                started_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
                age_h = (datetime.now(timezone.utc) - started_dt).total_seconds() / 3600
                if age_h > _ACTIVE_RUN_TTL_HOURS:
                    logger.info("[SIM] active_run %s scaduto (%.1fh > %dh), skip",
                                 run_id, age_h, _ACTIVE_RUN_TTL_HOURS)
                    return None
            except Exception:
                pass
        return state
    except Exception as e:
        logger.warning("[SIM] load_active_run %s fallita: %s", run_id, e)
        return None


def _clear_active_run(run_id: str) -> None:
    """Rimuove lo state shadow-persistito (chiamato a fine run)."""
    try:
        from simulator import db as sim_db
        sim_db.set_setting(f"{_ACTIVE_RUN_KEY_PREFIX}{run_id}", "")
    except Exception:
        pass


def _get_or_load_active_run(run_id: str) -> dict | None:
    """
    Ritorna lo state da memoria o lo recupera dal shadow store.
    Se ricarica, popola anche `_active_runs` per i call successivi nello stesso pod.
    Thread-safe via _active_runs_lock per evitare race su read-modify-write.
    """
    with _active_runs_lock:
        state = _active_runs.get(run_id)
        if state is not None:
            return state
    # Load shadow fuori dal lock (potrebbe essere I/O lento)
    state = _load_active_run(run_id)
    if state is not None:
        with _active_runs_lock:
            # Double-check: un altro thread potrebbe averlo gia' caricato
            existing = _active_runs.get(run_id)
            if existing is None:
                _active_runs[run_id] = state
                logger.info("[SIM] state %s ricaricato da shadow persistence", run_id)
            else:
                state = existing
    return state


SIM_PROMPT_DEFAULT = """Sei un Investment Analyst AI in modalità SIMULATOR.
Stai analizzando uno scenario di mercato realistico per testare il tuo ragionamento.

⚠️ NON ricevi date né periodo storico. Il setup è "snapshot": ti vengono dati
prezzi correnti, headline e dati. Devi decidere come operare ORA, basandoti
solo su quello che vedi.

Il messaggio user ti dira' se sei in modalita' SINGLE-STEP (decisione una-tantum,
orizzonte breve max 1 mese) o MULTI-STEP (turni successivi, decisioni adattive).

═══════════════════════════════════════════════════════════════════════
PROCEDURA OBBLIGATORIA (4 sezioni nominate, in ordine, NESSUNA OMISSIONE):
═══════════════════════════════════════════════════════════════════════

[0] CLASSIFICAZIONE DEL REGIME (OBBLIGATORIA, una riga)
    Scegli ESATTAMENTE UNA categoria e dichiarala in chiaro
    (es. "Regime: [LATERAL]"):

      [TREND-UP]    trend rialzista (breadth ampia, momentum positivo,
                    risk-on diffuso, no catalyst macro avverso imminente)
      [TREND-DOWN]  trend ribassista (breadth stretta, momentum negativo,
                    risk-off, deterioramento macro/earnings)
      [LATERAL]     range-bound, low conviction, no edge direzionale
                    (volumi compressi, news non decisive, no catalyst forte)
      [MACRO]       finestra di evento macro / event-driven binario
                    (FOMC, CPI, NFP, evento geopolitico in attesa di esito)
      [CRASH-RALLY] shock di volatilita' (crash) o rebound da capitulation
                    (mosse > 3% in un giorno, panic flows o relief rally)

    PROFILO OPERATIVO PER REGIME (vincoli di processo, non opzionali):
      - [TREND-UP] / [TREND-DOWN] / [CRASH-RALLY]:
          puoi tradare normalmente, conviction da BASSA ad ALTA,
          sizing standard.
      - [LATERAL] / [MACRO]:
          il default e' action=HOLD. Per derogare DEVI giustificare
          esplicitamente in [2]: catalyst specifico + perche'
          il risk/reward e' asimmetrico. Inoltre la conviction puo'
          essere SOLO "ALTA" (no MEDIA/BASSA) se decidi di entrare,
          e lo stop_loss_target deve essere obbligatoriamente popolato.

    STRATEGIA TATTICA PER REGIME (il "COME" tradare, non solo "SE"):
    Derivata dall'analisi dei run storici. Mindset diverso per regime —
    se ignori questa parte, ricadi negli stessi pattern di perdita.

      [TREND-UP] Focus: RELATIVE STRENGTH (alpha relativo, non assoluto).
          Non cercare il crollo prossimo: il trend e' la migliore
          informazione che hai. Identifica i LEADER di settore vs SPY
          e cavalca il trend. Tassi stabili/in calo → Tech (XLK, IWF).
          Economia solida (PMI > 50, jobs ok) → Value/Industrials
          (XLI, XLF). Earnings season positiva → sovrappesa beneficiari.
          Se NON vedi forza relativa chiara vs SPY, HOLD: non aggiungere
          posizioni contrarian sperando 'ormai e' tirato'.

      [TREND-DOWN] Focus: DIFESA + relative weakness.
          Privilegia difensivi (XLU Utilities, XLP Staples, XLV
          Healthcare) e TLT se selloff growth-driven. NON aggiungere
          ciclici/growth sperando nel rimbalzo. Short selling solo su
          weakness confermata, mai contro-trend.

      [LATERAL] Focus: MEAN REVERSION (NON breakout).
          DATO CRITICO dai run: i breakout in regime laterale falliscono
          nel ~70% dei casi. Hai perso sistematicamente comprando rotture
          che rientrano. Strategia corretta:
            • Compra SOLO vicino ai supporti del range.
            • Vendi SOLO vicino alle resistenze.
            • Logica contrarian, non momentum.
            • Take-profit AGGRESSIVI sull'altra estremita' del range
              (non aspettare il 'breakout vero').
            • Stop-loss STRETTI: se il supporto cede, esci subito.
            • Size piu' piccola del normale (conviction MEDIA non basta:
              o ALTA con setup chiaro, o HOLD).
          Se il prezzo e' a META' range, UNICA azione = HOLD.

      [MACRO] Focus: CORRELAZIONI macro (driver > ticker).
          I ticker singoli contano meno dei driver globali (CPI, Fed,
          dollaro, geo). Prima di ogni trade, verifica la correlazione:
            • Tassi previsti in CALO → TLT, Growth (XLK, IWF), VNQ.
            • Inflazione persistente → GLD, Energy (XLE, XOM), DBC, TIPS.
            • Dollaro forte → evita EM (EEM, VWO) e commodity USD.
            • Risk-off geopolitico → Difesa (LMT, RTX, NOC), Oro.
          NON tradare asset CONTRO il driver macro identificato (es. non
          comprare growth-tech con Fed hawkish surprise nelle headline).

      [CRASH-RALLY] Focus: quality + capitulation reversal.
          Crash (VIX shock, mosse > 3%): quality + cash, evita ciclici
          e leverage. NON prendere il primo coltello — attendi un bar di
          stabilizzazione. Capitulation rebound: cerca settori 'battuti
          peggio' che mostrano primi segnali reversal (high-beta: Tech
          growth, Small caps IWM). Size piccola, stop_loss_target SEMPRE
          popolato, exit_strategy = trailing_atr o level_target.

    Questa classificazione chiude il gap diagnosticato dai run storici
    del Simulator: 0% win-rate in regimi LATERAL/MACRO. Non saltare
    questo step e non inventare ibridi. Una sola etichetta.

[1] LETTURA DEL CONTESTO
    Sintesi di cosa stai vedendo:
    - Tema/regime suggerito dalle headline (3-5 righe, coerente con [0])
    - Asset più mossi nei prezzi e direzione
    - Eventuali campanelli d'allarme o opportunità immediate

[2] RAGIONAMENTO CAUSALE
    - Tesi principale: catena causale ('se X allora Y perché...')
    - Cosa nei dati supporta la tesi
    - Cosa potrebbe invalidarla
    - Rischio principale di una decisione operativa
    - SE regime [LATERAL] o [MACRO] e action != HOLD:
        scrivi "Deroga dal HOLD-default perche': (a) catalyst = ...,
        (b) conviction ALTA giustificata da ..., (c) SL sostenibile a ...".

[3] DECISIONE
    Output STRUTTURATO sotto forma di JSON. Puoi prendere UNA o PIÙ azioni
    nello stesso step (max 5). Schema esatto (NIENTE testo extra):
    {
      "decisions": [
        {
          "action": "BUY" | "SELL" | "HOLD",
          "asset": "TICKER",
          "conviction": "BASSA" | "MEDIA" | "ALTA",
          "horizon": "1g" | "1settimana" | "1mese" | "3mesi",
          "risk": "frase breve sul rischio (1-2 righe)",
          "stop_loss_target": null | <prezzo assoluto>,
          "take_profit_target": null | <prezzo assoluto>,
          "exit_strategy": "trailing_atr" | "level_target" | "time_based" | "discretionary"
        }
      ]
    }

    Per back-compat, se hai una sola decisione puoi anche usare il formato
    flat senza "decisions": [...] (campi al primo livello).

REGOLE:
- Le sezioni [1] e [2] DEVONO essere scritte in plain text
- La sezione [3] DEVE essere un JSON valido (lista decisions o flat singola)
- Ogni asset deve essere uno dei ticker dell'asset_universe fornito
- Niente testo dopo il JSON di [3]
- I campi stop_loss_target e take_profit_target sono PREZZI ASSOLUTI (es. 150.50)
- exit_strategy descrive il TUO approccio: 'level_target' se hai mirato livelli
  tecnici precisi, 'trailing_atr' se prevedi adattamento via ATR, 'time_based'
  se chiuderesti dopo X giorni indipendentemente dal prezzo, 'discretionary'
  se preferisci decidere step-by-step.

QUANDO USARE PIÙ DECISIONI:
- Pair trade: BUY un settore + SELL un correlato (es. BUY GLD + SELL TLT)
- Diversificazione tematica: BUY 2-3 ticker dello stesso tema
- Hedge: BUY del long + posizione difensiva
- HOLD: usa una sola decision con action=HOLD; non ha senso fare HOLD multipli
"""

SIM_PROMPT_MULTI_UPDATE = """\
═══════════════════════════════════════════════════════════════════════
[UPDATE T+{step} di {total_steps}] Stai continuando uno scenario MULTI-STEP.
═══════════════════════════════════════════════════════════════════════

ECCO COSA È CAMBIATO DAL TURNO PRECEDENTE (T+{prev_step}):

📰 HEADLINE NUOVE / AGGIORNATE:
{headline_changes}

📊 MOVIMENTO PREZZI dal precedente snapshot:
{price_changes}

🎯 LA TUA DECISIONE A T+{prev_step} ERA:
  Action: {prev_action}
  Asset: {prev_asset}
  Conviction: {prev_conviction}
  Orizzonte: {prev_horizon}
  Tesi: {prev_thesis}

📈 PERFORMANCE DELL'ASSET CHE HAI SCELTO da T+{prev_step} a T+{step}:
{asset_performance}

═══════════════════════════════════════════════════════════════════════
COSA DEVI FARE ORA:

Decidi se la tua tesi precedente è CONFERMATA, MODIFICATA o INVALIDATA dai
nuovi dati. La tua nuova decisione T+{step} può essere:
  • CONFERMA: stessa action sullo stesso asset (mantieni la posizione)
  • RAFFORZA: aumenta conviction o size se la tesi si sta realizzando bene
  • RUOTA: cambia asset/action se i nuovi dati mostrano opportunità migliori
  • CHIUDI: action=HOLD se la tesi è invalidata o profitto raggiunto

Nel ragionamento [2], dichiara ESPLICITAMENTE:
  "Tesi precedente: [conferma/modifica/invalida] perche' [motivo concreto
   basato sui dati nuovi]."

Stessa procedura: [1] Lettura, [2] Ragionamento, [3] Decisione JSON.
═══════════════════════════════════════════════════════════════════════
"""


def _get_deepseek_key() -> str:
    return os.environ.get("DEEPSEEK_API_KEY", "")


def _load_memory_summary(category: str, n_same: int = 5, n_recent: int = 3) -> str:
    """
    Costruisce un riassunto sintetico degli ultimi N run da iniettare nel context.
    - Ultimi 5 della stessa categoria
    - Ultimi 3 in assoluto
    Formulato in modo neutro, senza "hai sbagliato/fatto bene".
    """
    same_cat = sim_db.list_runs(category=category, limit=n_same)
    recent = sim_db.list_runs(limit=n_recent)
    seen = set()
    items = []
    for r in same_cat + recent:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        items.append({
            "category": r.get("category"),
            "asset": r.get("asset_chosen"),
            "action": r.get("action_chosen"),
            "conviction": r.get("conviction"),
            "outcome_neutral": r.get("what_happened", "")[:200],
        })
        if len(items) >= n_same + n_recent:
            break
    if not items:
        return ""
    lines = ["MEMORIA — run recenti (descrittiva, non valutativa):"]
    for it in items:
        lines.append(
            f"  • [{it.get('category','?')}] action={it.get('action','?')} "
            f"asset={it.get('asset','?')} conv={it.get('conviction','?')} — "
            f"{(it.get('outcome_neutral') or '')[:120]}"
        )
    return "\n".join(lines)


def _perturb_market_data(scenario: dict, step_index: int) -> list:
    """
    Genera la versione "T+step" dei market_data applicando perturbazioni
    deterministiche (stessa seed → stessi prezzi sempre, riproducibile).
    """
    import random
    base = list(scenario.get("market_data", []))
    if step_index <= 0:
        return base
    random.seed(hash(scenario.get("id", "")) + step_index)
    out = []
    for m in base:
        drift = random.uniform(-0.04, 0.06) * step_index
        out.append({
            **m,
            "price_t0": round(m["price_t0"] * (1 + drift), 2),
            "change_24h": round(random.uniform(-3, 3), 2),
            "change_7d": round(random.uniform(-8, 8), 2),
        })
    return out


def _perturb_headlines(scenario: dict, step_index: int) -> list:
    """
    Per multi-step: simula evoluzione delle headlines. Per il primo step
    ritorna la lista originale. Per step successivi, ne ruota alcune e
    aggiunge "follow-up" generici basati sulla categoria.
    """
    base = list(scenario.get("headlines", []))
    if step_index <= 0:
        return base

    import random
    random.seed(hash("hl::" + scenario.get("id", "")) + step_index)

    # Headline "follow-up" per categoria — simula nuove news che escono
    category = scenario.get("category", "")
    followup_pool = {
        "geopolitico": [
            "Reazioni dei mercati alla situazione geopolitica continuano a impattare i settori risk-on",
            "Analisti rivedono le proiezioni per le prossime settimane",
            "Diplomazia attiva su piu' tavoli negoziali",
        ],
        "macro": [
            "Dichiarazioni Fed/BCE muovono i tassi sui Treasury",
            "Dati macro contrastanti: occupazione vs inflazione",
            "Forward guidance delle banche centrali aggiornata",
        ],
        "crash_rally": [
            "Volatilita' rimane elevata: VIX in espansione",
            "Trader retail aumentano l'attivita' su small cap",
            "Hedge funds modificano posizionamento netto",
        ],
        "normale": [
            "Earnings stagionali confermano la traiettoria attesa",
            "Volume sopra la media negli ETF settoriali",
            "Sentiment retail stabile",
        ],
    }
    pool = followup_pool.get(category, followup_pool["normale"])
    new_headlines = list(base)
    for _ in range(min(2, len(pool))):
        h = random.choice(pool)
        if h not in new_headlines:
            new_headlines.append(h)
    return new_headlines


def _format_price_changes(prev_md: list, curr_md: list, max_lines: int = 8) -> str:
    """Formatta le variazioni di prezzo tra due snapshot multi-step."""
    if not prev_md or not curr_md:
        return "  (snapshot iniziale, no comparativi)"
    by_ticker_prev = {m["ticker"]: m for m in prev_md}
    lines = []
    for m in curr_md[:max_lines]:
        t = m["ticker"]
        prev = by_ticker_prev.get(t)
        if not prev:
            continue
        delta_pct = ((m["price_t0"] - prev["price_t0"]) / prev["price_t0"] * 100) if prev["price_t0"] else 0
        arrow = "↗" if delta_pct > 0.5 else "↘" if delta_pct < -0.5 else "→"
        lines.append(
            f"  {arrow} {t:8s} ${prev['price_t0']:>8.2f} → ${m['price_t0']:>8.2f} "
            f"({delta_pct:+.2f}%)"
        )
    return "\n".join(lines) if lines else "  (nessun cambiamento significativo)"


def _format_headline_changes(prev_hl: list, curr_hl: list, max_lines: int = 5) -> str:
    """Formatta le headline nuove (presenti in curr ma non in prev)."""
    prev_set = set(prev_hl or [])
    new_only = [h for h in (curr_hl or []) if h not in prev_set][:max_lines]
    if not new_only:
        return "  (nessuna headline nuova: contesto stabile)"
    return "\n".join(f"  • [NUOVA] {h}" for h in new_only)


def _format_asset_performance(asset: str, prev_md: list, curr_md: list) -> str:
    """Mostra come si e' mosso l'asset scelto al turno precedente."""
    if not asset or not prev_md or not curr_md:
        return "  (asset non determinato o snapshot mancante)"
    prev = next((m for m in prev_md if m["ticker"] == asset), None)
    curr = next((m for m in curr_md if m["ticker"] == asset), None)
    if not prev or not curr:
        return f"  (asset {asset} non in market_data)"
    delta_pct = ((curr["price_t0"] - prev["price_t0"]) / prev["price_t0"] * 100) if prev["price_t0"] else 0
    icon = "📈" if delta_pct >= 0 else "📉"
    verdict = (
        "tesi si sta realizzando" if delta_pct >= 1
        else "tesi neutrale" if delta_pct >= -1
        else "tesi sotto pressione"
    )
    return (
        f"  {icon} {asset}: ${prev['price_t0']:.2f} → ${curr['price_t0']:.2f} "
        f"({delta_pct:+.2f}%)  [{verdict}]"
    )


def _build_context_prompt(scenario: dict, step_index: int = 0,
                           prev_steps: list[dict] | None = None,
                           scenario_type: str = "single",
                           total_steps: int = 1) -> tuple[str, dict]:
    """
    Costruisce il messaggio user per il modello + ritorna il context (per UI).

    Aggiunge:
    - Mode banner: SINGLE-STEP (max 1 mese) vs MULTI-STEP (T+X di Y)
    - Multi-step: cambiamenti specifici di prezzi/headline + perf dell'asset
    """
    asset_universe = list(scenario.get("asset_universe", []))
    headlines = _perturb_headlines(scenario, step_index)
    market_data = _perturb_market_data(scenario, step_index)

    parts = []

    # ── Mode banner (sempre in cima)
    if scenario_type == "single":
        parts.append("═" * 60)
        parts.append("MODE: SINGLE-STEP — decisione una-tantum, orizzonte breve.")
        parts.append("VINCOLO: il campo 'horizon' deve essere uno tra '1g' / '1settimana' / '1mese'.")
        parts.append("NON usare '3mesi' (riservato al multi-step). Hai una sola finestra di azione.")
        parts.append("═" * 60)
        parts.append("")
    else:
        parts.append("═" * 60)
        parts.append(f"MODE: MULTI-STEP — turno T+{step_index} di {total_steps}.")
        parts.append("Hai turni successivi per adattare la decisione: usa orizzonti coerenti")
        parts.append("col numero di step rimanenti.")
        parts.append("═" * 60)
        parts.append("")

    parts.append(f"Asset universe disponibile: {', '.join(asset_universe)}")
    parts.append("")
    parts.append("HEADLINE (ultime 24h):")
    for h in headlines:
        parts.append(f"  • {h}")
    parts.append("")
    parts.append("MARKET DATA SNAPSHOT:")
    for m in market_data:
        parts.append(
            f"  {m['ticker']:8s} ${m['price_t0']:>9.2f}  "
            f"24h: {m['change_24h']:+.2f}%  7d: {m['change_7d']:+.2f}%"
        )

    context_for_ui = {
        "headlines": headlines,
        "market_data": market_data,
        "asset_universe": asset_universe,
        "scenario_type": scenario_type,
        "total_steps": total_steps,
        "step_index": step_index,
    }

    # ── Multi-step: blocco update con cambiamenti specifici
    if step_index > 0 and prev_steps:
        prev = prev_steps[-1]
        prev_decision = prev.get("decision") or {}
        # Calcola gli snapshot del turno precedente per fare il diff
        prev_md = _perturb_market_data(scenario, step_index - 1)
        prev_hl = _perturb_headlines(scenario, step_index - 1)

        price_changes = _format_price_changes(prev_md, market_data)
        headline_changes = _format_headline_changes(prev_hl, headlines)
        asset_perf = _format_asset_performance(
            prev_decision.get("asset"), prev_md, market_data
        )

        update_text = SIM_PROMPT_MULTI_UPDATE.format(
            step=step_index,
            prev_step=step_index - 1,
            total_steps=total_steps,
            prev_action=prev_decision.get("action", "?"),
            prev_asset=prev_decision.get("asset", "—"),
            prev_conviction=prev_decision.get("conviction", "?"),
            prev_horizon=prev_decision.get("horizon", "?"),
            prev_thesis=(prev.get("reasoning", "") or "")[:400],
            headline_changes=headline_changes,
            price_changes=price_changes,
            asset_performance=asset_perf,
        )
        parts.insert(0, update_text)

        # Estendi il context_for_ui con il diff per la UI
        context_for_ui["update"] = (
            f"T+{step_index} di {total_steps}. Decisione precedente: "
            f"{prev_decision.get('action')} {prev_decision.get('asset','')} "
            f"({prev_decision.get('conviction','?')})"
        )
        context_for_ui["update_details"] = {
            "prev_decision": prev_decision,
            "prev_thesis": (prev.get("reasoning", "") or "")[:400],
            "headline_changes": [
                h for h in headlines if h not in (prev_hl or [])
            ],
            "price_changes": [
                {
                    "ticker": m["ticker"],
                    "prev_price": next(
                        (p["price_t0"] for p in prev_md if p["ticker"] == m["ticker"]),
                        None,
                    ),
                    "curr_price": m["price_t0"],
                    "delta_pct": (
                        ((m["price_t0"] - p["price_t0"]) / p["price_t0"] * 100)
                        if (p := next((x for x in prev_md if x["ticker"] == m["ticker"]), None))
                           and p["price_t0"] else None
                    ),
                }
                for m in market_data
            ],
            "asset_performance_text": asset_perf.strip(),
        }

    # Inietta memoria descrittiva (run recenti, neutra)
    memory = _load_memory_summary(scenario.get("category", ""))
    if memory:
        parts.insert(0, memory + "\n\n" + "=" * 60)

    # Inietta advice categorizzati (lezioni operative dall'advisor)
    try:
        from agents import sim_advisor
        advice_block, category_key, advice_ids = (
            sim_advisor.get_advice_block_for_runner(scenario, max_items=5)
        )
        if advice_block:
            parts.insert(0, advice_block + "\n")
            # Track apply_count: solo al primo step (T0) per non gonfiare
            if step_index == 0 and advice_ids:
                try:
                    sim_advisor.increment_apply_count(advice_ids, category_key)
                except Exception:
                    pass
            context_for_ui["advice_applied"] = {
                "category_key": category_key,
                "advice_ids": advice_ids,
                "count": len(advice_ids),
            }
    except Exception as exc:
        logger.debug("[SIM] advice injection skipped: %s", exc)

    return "\n".join(parts), context_for_ui


async def _call_r1(system_prompt: str, user_message: str,
                   max_retries: int = 3) -> str:
    """Chiama DeepSeek-R1. Ritorna il response_text.

    Retry automatico su 429 (rate limit), 5xx (server error), e timeout.
    Backoff esponenziale: 2s, 4s, 8s.

    Bug precedente: nessun retry → un singolo 429/timeout faceva HTTP 500
    sul single-step (visto che c'e' una sola chiamata _call_r1).

    Inietta automaticamente i shared_principles (SL/TP autonomy) cosi' il
    Simulator e il bot Live ragionano con la stessa filosofia di gestione
    del rischio.
    """
    api_key = _get_deepseek_key()
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY non configurata")

    # Inietta shared principles in coda al system_prompt (idempotente: se gia'
    # presenti per via di un prompt custom, e' solo ridondanza inerte).
    try:
        from agents.shared_principles import get_full_risk_block_for_simulator
        system_prompt = system_prompt + "\n\n" + ("═" * 60) + "\n" + get_full_risk_block_for_simulator()
    except Exception:
        pass

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": DEEPSEEK_R1,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "max_tokens": 4500,
    }

    last_error: str = ""
    for attempt in range(max_retries):
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(
                    DEEPSEEK_API_URL, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=180)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data["choices"][0]["message"]["content"] or ""
                    body = await resp.text()
                    last_error = f"HTTP {resp.status}: {body[:200]}"
                    # Retry solo su errori transitori (429, 5xx)
                    if resp.status == 429 or 500 <= resp.status < 600:
                        if attempt < max_retries - 1:
                            wait_s = 2 ** (attempt + 1)
                            logger.warning(
                                "[SIM] DeepSeek-R1 %d (attempt %d/%d), retry in %ds: %s",
                                resp.status, attempt + 1, max_retries, wait_s,
                                body[:120]
                            )
                            await asyncio.sleep(wait_s)
                            continue
                    # Errore non transitorio (4xx eccetto 429): fail immediato
                    raise ValueError(f"DeepSeek-R1 {last_error}")
        except asyncio.TimeoutError:
            last_error = "timeout"
            if attempt < max_retries - 1:
                wait_s = 2 ** (attempt + 1)
                logger.warning(
                    "[SIM] DeepSeek-R1 timeout (attempt %d/%d), retry in %ds",
                    attempt + 1, max_retries, wait_s
                )
                await asyncio.sleep(wait_s)
                continue
            raise ValueError(f"DeepSeek-R1 timeout dopo {max_retries} tentativi")
        except aiohttp.ClientError as e:
            last_error = f"network: {e}"
            if attempt < max_retries - 1:
                wait_s = 2 ** (attempt + 1)
                logger.warning(
                    "[SIM] DeepSeek-R1 network err (attempt %d/%d), retry in %ds: %s",
                    attempt + 1, max_retries, wait_s, str(e)[:120]
                )
                await asyncio.sleep(wait_s)
                continue
            raise ValueError(f"DeepSeek-R1 network error: {e}")

    raise ValueError(f"DeepSeek-R1 fallito dopo {max_retries} tentativi: {last_error}")


def _parse_response(raw: str) -> dict:
    """
    Estrae le 3 sezioni dal response del modello.
    Stripping <think>...</think>.
    """
    txt = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

    def grab(label: str, until: list[str]) -> str:
        # Pattern flessibili: [1]/[2]/[3], "1.", "1 .", "**[1]**" ecc.
        patterns = [
            rf"\[{label[0]}\]\s*{re.escape(label[2:])}",
            rf"\*\*\[{label[0]}\][^\*]*\*\*",
            rf"^{label[0]}[\.\)]\s*{re.escape(label[2:])}",
        ]
        for p in patterns:
            m = re.search(p, txt, re.IGNORECASE | re.MULTILINE)
            if m:
                start = m.end()
                end = len(txt)
                for u in until:
                    pat = re.escape(u) if not u.startswith(r"\[") else u
                    um = re.search(rf"\[{pat[2]}\]|\*\*\[{pat[2]}\]", txt[start:])
                    if um:
                        end = min(end, start + um.start())
                return txt[start:end].strip()
        return ""

    reading = grab("1 LETTURA", ["[2]", "[3]"])
    reasoning = grab("2 RAGIONAMENTO", ["[3]"])

    # Decision: cerca primo JSON valido nel testo dopo [3]
    decision_section = grab("3 DECISIONE", [])
    decision_json = {}
    if decision_section:
        m = re.search(r"\{[\s\S]*\}", decision_section)
        if m:
            try:
                decision_json = json.loads(m.group(0))
            except Exception:
                pass
    # Fallback: cerca JSON nell'intero testo
    if not decision_json:
        # Prova prima JSON con "decisions": [...]
        m = re.search(r'\{[\s\S]*?"decisions"[\s\S]*?\]\s*\}', txt)
        if m:
            try:
                decision_json = json.loads(m.group(0))
            except Exception:
                pass
    if not decision_json:
        m = re.search(r'\{[^{}]*"action"[\s\S]*?\}', txt)
        if m:
            try:
                decision_json = json.loads(m.group(0))
            except Exception:
                pass

    # ─── Parse decisions: supporta sia "decisions": [...] sia formato flat ───
    decisions_list: list[dict] = []
    if isinstance(decision_json, dict):
        if isinstance(decision_json.get("decisions"), list) and decision_json["decisions"]:
            for d in decision_json["decisions"][:5]:   # cap a 5 azioni per step
                if not isinstance(d, dict):
                    continue
                decisions_list.append(_normalize_decision(d))
        elif decision_json.get("action"):
            decisions_list.append(_normalize_decision(decision_json))

    if not decisions_list:
        decisions_list.append({
            "action": "HOLD", "asset": None, "conviction": "BASSA",
            "horizon": "1settimana", "risk": "", "stop_loss_target": None,
            "take_profit_target": None, "exit_strategy": "discretionary",
        })

    # Per back-compat, "decision" = primo elemento (entry decision)
    primary = decisions_list[0]

    return {
        "reading": reading or txt[:800],
        "reasoning": reasoning or "",
        "decision": primary,
        "decisions": decisions_list,   # NUOVO: lista completa
        "raw": txt,
    }


def _normalize_decision(d: dict) -> dict:
    """Normalizza un singolo dict decisione applicando default + clipping."""
    return {
        "action": (d.get("action") or "HOLD").upper(),
        "asset": d.get("asset"),
        "conviction": (d.get("conviction") or "BASSA").upper(),
        "horizon": d.get("horizon", "1settimana"),
        "risk": (d.get("risk") or "")[:500],
        "stop_loss_target": d.get("stop_loss_target"),
        "take_profit_target": d.get("take_profit_target"),
        "exit_strategy": d.get("exit_strategy", "discretionary"),
    }


# ─── Public API ─────────────────────────────────────────────────────────────

async def start_run(category: str, scenario_type: str, num_steps: int,
                     scenario_id: Optional[str], mode: str = "manual") -> dict:
    """Inizia un run. Ritorna {run_id, scenario_id, total_steps}."""
    if scenario_id:
        scenario = scenarios.get_scenario_by_id(scenario_id)
    else:
        scenario = scenarios.get_random_scenario(category)
    if not scenario:
        raise ValueError(f"Nessuno scenario disponibile per categoria={category}")

    run_id = str(uuid4())
    total = num_steps if scenario_type == "multi" else 1
    state = {
        "scenario": scenario,
        "scenario_type": scenario_type,
        "category": category,
        "total_steps": total,
        "steps": [],   # lista per ogni step: {context, reading, reasoning, decision, ...}
        "mode": mode,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    with _active_runs_lock:
        _active_runs[run_id] = state
    # Shadow persist subito: cosi' anche se il pod si riavvia tra start_run e
    # il primo execute_step, lo state e' recuperabile.
    _persist_active_run(run_id, state)

    logger.info("[SIM] Avviato run %s scenario=%s type=%s steps=%d",
                run_id, scenario["id"], scenario_type, total)
    return {"run_id": run_id, "scenario_id": scenario["id"], "total_steps": total}


def rebuild_run_from_memory(run_id: str) -> dict | None:
    """
    Ricostruisce i dati di un run gia' eseguito ma non ancora salvato nel DB
    (es. l'INSERT su Supabase e' fallito ma lo state e' ancora disponibile).
    Cerca prima in memory, poi nel shadow store (sim_settings).
    """
    state = _get_or_load_active_run(run_id)
    if not state:
        logger.warning("[SIM] rebuild_run_from_memory: %s NON trovato (mem keys: %s)",
                       run_id, list(_active_runs.keys())[:5])
        return None
    if not state.get("steps"):
        logger.warning("[SIM] rebuild_run_from_memory: %s ha steps vuoti", run_id)
        return None
    try:
        result = _build_run_data(run_id, state)
        result["_source"] = "in_memory_fallback"
        logger.info("[SIM] rebuild_run_from_memory ok: %s", run_id)
        return result
    except Exception as exc:
        logger.error("[SIM] rebuild_run_from_memory %s failed: %s", run_id, exc, exc_info=True)
        return None


async def execute_step(run_id: str, step_index: int) -> dict:
    """Esegue uno step del run. Ritorna context + output del modello.

    Recupera lo state da memoria O dal shadow store (sim_settings) — cosi'
    se il pod Render si riavvia tra step 0 e step 1, il multi-step continua
    a funzionare invece di crashare con HTTP 500 "Run not found".
    """
    state = _get_or_load_active_run(run_id)
    if state is None:
        raise ValueError(
            f"Run {run_id} non trovato. Possibili cause: run scaduto (>24h "
            f"da start_run), errore di persistenza, oppure id non mai esistito."
        )
    scenario = state["scenario"]
    scenario_type = state.get("scenario_type", "single")
    total_steps = state.get("total_steps", 1)

    user_msg, context_for_ui = _build_context_prompt(
        scenario, step_index=step_index, prev_steps=state["steps"],
        scenario_type=scenario_type, total_steps=total_steps,
    )
    raw = await _call_r1(SIM_PROMPT_DEFAULT, user_msg)
    parsed = _parse_response(raw)

    step_data = {
        "step_index": step_index,
        "context": context_for_ui,
        "reading": parsed["reading"],
        "reasoning": parsed["reasoning"],
        "decision": parsed["decision"],          # primary (back-compat)
        "decisions": parsed.get("decisions", [parsed["decision"]]),  # lista completa
        "raw": parsed["raw"][:3000],
        "is_last_step": (step_index + 1) >= state["total_steps"],
    }
    state["steps"].append(step_data)

    # Aggiorna shadow persistence DOPO ogni step (cosi' il prossimo step,
    # anche su pod diverso, vede gli step precedenti).
    _persist_active_run(run_id, state)

    # Se è l'ultimo step, FINALIZE IN BACKGROUND.
    # Prima il _finalize era awaited inline → la request HTTP impiegava
    # 30-60s perche' faceva _build_run_data + sim_db.insert_run sincronamente.
    # Render kill connections > 60s → il frontend vedeva "ultimo turno mai
    # portato a termine". Ora ritorniamo SUBITO il step_data e il finalize
    # gira asincrono. Lo state e' gia' nel shadow persistence, quindi anche
    # se il pod muore, get_run() ricostruisce dal shadow.
    if step_data["is_last_step"]:
        try:
            asyncio.create_task(_finalize_run_safe(run_id))
            step_data["finalize_pending"] = True
        except Exception as exc:
            logger.error("[SIM] schedule _finalize_run fallito su %s: %s",
                         run_id, exc, exc_info=True)
            step_data["finalize_error"] = (
                f"Schedule finalize fallito: {type(exc).__name__}: {str(exc)[:200]}"
            )

    return step_data


async def _finalize_run_safe(run_id: str):
    """Wrapper di _finalize_run che cattura ogni eccezione (no crash background)."""
    try:
        await _finalize_run(run_id)
    except Exception as exc:
        logger.error("[SIM] _finalize_run background crash %s: %s",
                     run_id, exc, exc_info=True)


def _build_run_data(run_id: str, state: dict) -> dict:
    """
    Costruisce il dict run_data finale dal state in-memory.
    Condiviso tra _finalize_run (persistence) e rebuild_run_from_memory
    (fallback). Calcola metriche estese per la pagina di risultato:
    prezzo entry, drawdown massimo, volatilita', P&L su $10k notional,
    SL/TP target proposti dal modello, ecc.
    """
    scenario = state.get("scenario") or {}
    steps = state.get("steps") or []
    last_step = steps[-1] if steps else {}
    decision = last_step.get("decision") or {}
    market_data = scenario.get("market_data") or []

    # ── PERFORMANCE: aggregazione per-asset across all steps.
    #
    # Bug precedente: prendevamo la PRIMA BUY/SELL trovata. Ma se l'AI fa
    # T0=SELL ZION → T1=BUY ZION (chiude la SHORT) → T2=HOLD ZION,
    # il calcolo era SHORT P&L ignorando il reversal a T1.
    #
    # Logica corretta:
    #   1. Per ogni asset, raccogli TUTTE le decisions (BUY/SELL/HOLD) in
    #      ordine cronologico.
    #   2. Identifica il "main asset": quello con piu' decisions. In caso di
    #      tie, l'asset con la prima entry chronologica.
    #   3. Per il main asset, prendi l'ULTIMA azione non-HOLD: quella e' la
    #      posizione netta che l'utente terrebbe alla fine della sequenza.
    #      (Un SELL seguito da BUY sullo stesso asset = posizione FLAT/LONG,
    #       non SHORT come prima.)
    #   4. Se tutte le decisions sull'asset sono HOLD → tratta come HOLD.
    asset_actions: dict[str, list[tuple[int, str, dict]]] = {}
    for s in steps:
        step_idx = s.get("step_index", 0)
        candidates = s.get("decisions") or [s.get("decision") or {}]
        for d in candidates:
            if not d:
                continue
            asset_d = d.get("asset")
            action_d = d.get("action")
            if not asset_d or not action_d:
                continue
            asset_actions.setdefault(asset_d, []).append((step_idx, action_d, d))

    main_asset = None
    if asset_actions:
        # Score per-asset: (n_decisions, -first_step_idx). Piu' decisioni vince;
        # in caso di tie, l'asset entrato prima vince.
        def _asset_score(actions_list):
            return (len(actions_list), -actions_list[0][0])
        main_asset = max(asset_actions.keys(),
                         key=lambda a: _asset_score(asset_actions[a]))

    entry_decision = None
    if main_asset:
        actions = asset_actions[main_asset]
        # Ultima azione non-HOLD = posizione netta finale
        for step_idx, action, d in reversed(actions):
            if action in ("BUY", "SELL"):
                entry_decision = d
                break
        # Se tutte HOLD → tratta la prima come "no-entry"
        if entry_decision is None and actions:
            entry_decision = actions[0][2]

    if entry_decision is None:
        entry_decision = decision  # nessuna decisione valida → ultima del run

    asset = entry_decision.get("asset") or decision.get("asset")
    entry_action = entry_decision.get("action") or decision.get("action")

    # Diagnostic log per debug del fix aggregation
    if asset_actions and main_asset:
        n_decs = len(asset_actions[main_asset])
        all_actions_main = [a for _, a, _ in asset_actions[main_asset]]
        logger.info(
            "[SIM] aggregation run=%s main_asset=%s decisions=%d actions=%s "
            "→ net=%s",
            run_id, main_asset, n_decs, all_actions_main, entry_action
        )

    logger.info("[SIM] _build_run_data run=%s entry: %s %s (steps=%d, last_action=%s)",
                run_id, entry_action, asset, len(steps), decision.get("action"))

    # ── DUE METRICHE SEPARATE ──────────────────────────────────────────
    # asset_*  : return dell'asset (long buy-and-hold). Usato per il
    #            price chart (movimento reale dell'asset).
    # perf_*   : P&L della POSIZIONE presa (BUY=asset_return, SELL=-asset_return).
    #            Usato per outcome, "tesi confermata", display "Performance posizione".
    asset_1w, asset_1m, asset_3m = _simulate_asset_return(scenario, asset)
    perf_1w = _position_pnl(asset_1w, entry_action)
    perf_1m = _position_pnl(asset_1m, entry_action)
    perf_3m = _position_pnl(asset_3m, entry_action)
    perf_sp_1m = 0.02
    perf_sector_1m = 0.015
    perf_monkey_1m = 0.005
    delta_sp = perf_1m - perf_sp_1m if perf_1m is not None else None
    delta_sector = perf_1m - perf_sector_1m if perf_1m is not None else None
    delta_monkey = perf_1m - perf_monkey_1m if perf_1m is not None else None

    if perf_1m is None:
        outcome = "yellow"
    elif delta_sp and delta_sp > 0.005 and delta_sector and delta_sector > 0:
        outcome = "green"
    elif delta_sp and delta_sp < -0.01:
        outcome = "red"
    else:
        outcome = "yellow"

    period_start = scenario.get("period_start", "?")
    period_end = scenario.get("period_end", "?")
    historical_period = f"{period_start} → {period_end}"

    # ── Prezzi: entry, exit a 1S/1M/3M, max/min nel periodo
    entry_price = None
    if asset:
        anchor = next((m for m in market_data if m.get("ticker") == asset), None)
        if anchor:
            entry_price = float(anchor.get("price_t0") or 0)

    if asset and entry_price:
        import random
        random.seed(hash(str(scenario.get("id", "")) + (asset or "")))
        # Usa ASSET return (non P&L posizione) per il price chart: il chart
        # mostra il movimento REALE del prezzo, indipendente dal long/short.
        target = entry_price * (1 + (asset_3m or 0))
        price_chart = []
        for i in range(90):
            t = i / 89
            base = entry_price + (target - entry_price) * t
            noise = random.uniform(-0.02, 0.02) * entry_price
            price_chart.append({"day": i, "price": round(base + noise, 2)})
    else:
        price_chart = []

    # Prezzi a milestone (interpolati dal price_chart)
    def _price_at_day(day: int) -> float | None:
        if not price_chart or day < 0 or day >= len(price_chart):
            return None
        return price_chart[day]["price"]

    price_at_1w = _price_at_day(7)
    price_at_1m = _price_at_day(30)
    price_at_3m = _price_at_day(89) if price_chart else None

    # Stats sul price_chart: max, min, drawdown, volatilita'
    chart_stats = {}
    if price_chart and entry_price:
        prices = [p["price"] for p in price_chart]
        max_price = max(prices)
        min_price = min(prices)
        # Max drawdown (peak-to-trough running)
        peak = prices[0]
        max_dd = 0
        for p in prices:
            if p > peak:
                peak = p
            dd = (p - peak) / peak if peak > 0 else 0
            if dd < max_dd:
                max_dd = dd
        # Volatilita' annualizzata (std dei daily returns × sqrt(252))
        daily_returns = []
        for i in range(1, len(prices)):
            if prices[i - 1] > 0:
                daily_returns.append((prices[i] - prices[i - 1]) / prices[i - 1])
        if daily_returns:
            mean = sum(daily_returns) / len(daily_returns)
            var = sum((r - mean) ** 2 for r in daily_returns) / len(daily_returns)
            vol = (var ** 0.5) * (252 ** 0.5)
        else:
            vol = 0.0
        chart_stats = {
            "max_price_period": round(max_price, 2),
            "min_price_period": round(min_price, 2),
            "max_runup_pct": round(((max_price - entry_price) / entry_price) * 100, 2),
            "max_drawdown_pct": round(max_dd * 100, 2),
            "annualized_volatility_pct": round(vol * 100, 2),
        }

    # ── P&L assoluto su $10k notional (per dare un riferimento concreto)
    notional = 10000.0
    pnl_dollars_1m = None
    if perf_1m is not None:
        pnl_dollars_1m = round(notional * perf_1m, 2)
    pnl_dollars_3m = None
    if perf_3m is not None:
        pnl_dollars_3m = round(notional * perf_3m, 2)

    # ── Step breakdown (ora anche per single-step, non solo multi)
    # Ogni step puo' contenere multiple decisions (action multiple per step).
    steps_data = []
    for i, s in enumerate(steps):
        primary = s.get("decision") or {}
        all_decisions = s.get("decisions") or [primary]
        # Costruisci dict per ogni decisione
        per_step_decisions = []
        for d in all_decisions:
            if not d:
                continue
            asset_step = d.get("asset")
            price = None
            if asset_step:
                anchor_step = next(
                    (m for m in market_data if m.get("ticker") == asset_step), None
                )
                if anchor_step:
                    price = anchor_step.get("price_t0")
            per_step_decisions.append({
                "action": d.get("action"),
                "asset": asset_step,
                "conviction": d.get("conviction"),
                "horizon": d.get("horizon"),
                "price": price,
                "stop_loss_target": d.get("stop_loss_target"),
                "take_profit_target": d.get("take_profit_target"),
                "exit_strategy": d.get("exit_strategy"),
                "risk": (d.get("risk") or "")[:300],
            })
        # Manteniamo i campi flat (back-compat) della primary decision
        primary_asset = primary.get("asset")
        primary_price = None
        if primary_asset:
            anchor = next(
                (m for m in market_data if m.get("ticker") == primary_asset), None
            )
            if anchor:
                primary_price = anchor.get("price_t0")
        steps_data.append({
            "step_index": i,
            "action": primary.get("action"),
            "asset": primary_asset,
            "conviction": primary.get("conviction"),
            "horizon": primary.get("horizon"),
            "price": primary_price,
            "stop_loss_target": primary.get("stop_loss_target"),
            "take_profit_target": primary.get("take_profit_target"),
            "exit_strategy": primary.get("exit_strategy"),
            "risk": (primary.get("risk") or "")[:300],
            "perf_from_here": None,
            "decisions": per_step_decisions,         # NUOVO: lista per UI
            "decisions_count": len(per_step_decisions),
        })

    # ── Verifica TP/SL: i target proposti sarebbero stati toccati nel periodo?
    # LONG (BUY):  SL sotto entry (chiudo se cade), TP sopra entry (chiudo se sale)
    # SHORT (SELL): SL sopra entry (chiudo se sale contro), TP sotto entry (chiudo se scende come previsto)
    sl_target = decision.get("stop_loss_target")
    tp_target = decision.get("take_profit_target")
    is_short = (entry_action == "SELL")
    sl_hit = None
    tp_hit = None
    sl_hit_day = None
    tp_hit_day = None
    if price_chart:
        for day_obj in price_chart:
            p = day_obj["price"]
            if sl_target and sl_hit is None:
                hit = (p >= sl_target) if is_short else (p <= sl_target)
                if hit:
                    sl_hit = True
                    sl_hit_day = day_obj["day"]
            if tp_target and tp_hit is None:
                hit = (p <= tp_target) if is_short else (p >= tp_target)
                if hit:
                    tp_hit = True
                    tp_hit_day = day_obj["day"]
            if sl_hit and tp_hit:
                break
        if sl_target and sl_hit is None:
            sl_hit = False
        if tp_target and tp_hit is None:
            tp_hit = False

    return {
        "id": run_id,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "mode": state.get("mode", "manual"),
        "category": scenario.get("category", "unknown"),
        "scenario_type": state.get("scenario_type", "single"),
        "steps": state.get("total_steps", 1),
        "scenario_id": scenario.get("id", "unknown"),
        "historical_period": historical_period,

        # Decision summary — usa la decisione di ENTRY (primo BUY/SELL)
        # cosi' le metriche sono coerenti con il P&L calcolato.
        "asset_chosen": asset,
        "action_chosen": entry_action,
        "conviction": entry_decision.get("conviction") or decision.get("conviction"),
        "horizon": entry_decision.get("horizon") or decision.get("horizon"),

        # Performance core (P&L della POSIZIONE: già invertita per SHORT)
        "perf_1w": perf_1w,
        "perf_1m": perf_1m,
        "perf_3m": perf_3m,
        # Return ASSET (movimento prezzo, indipendente da long/short).
        # Utile per il frontend: mostra "Asset SPY: -8% / La tua SHORT: +8%".
        "asset_return_1w": asset_1w,
        "asset_return_1m": asset_1m,
        "asset_return_3m": asset_3m,
        "is_short": is_short,
        "perf_sp_1m": perf_sp_1m,
        "perf_sector_1m": perf_sector_1m,
        "perf_monkey_1m": perf_monkey_1m,
        "delta_sp": delta_sp,
        "delta_sector": delta_sector,
        "delta_monkey": delta_monkey,
        "outcome": outcome,

        # Reasoning
        "original_thesis": (last_step.get("reasoning") or "")[:1500],
        "what_happened": scenario.get("description_reveal", ""),
        "thesis_evaluation": _evaluate_thesis(decision, scenario, perf_1m),

        # full_data: dati estesi non flatable, JSON nel DB
        "full_data": {
            "steps": steps,
            "price_chart": price_chart,
            "steps_data": steps_data,

            # Prezzi e milestone
            "entry_price": entry_price,
            "price_at_1w": price_at_1w,
            "price_at_1m": price_at_1m,
            "price_at_3m": price_at_3m,

            # Statistiche periodo
            "chart_stats": chart_stats,

            # P&L assoluto su $10k
            "pnl_on_10k": {
                "notional": notional,
                "pnl_1m": pnl_dollars_1m,
                "pnl_3m": pnl_dollars_3m,
            },

            # Reasoning completo (sezioni del prompt) — non solo troncato
            "reading_text": last_step.get("reading", ""),
            "reasoning_full": last_step.get("reasoning", ""),
            "decision_full": decision,  # incluso stop_loss_target/take_profit_target

            # SL/TP analysis: il target sarebbe stato toccato?
            "sl_tp_analysis": {
                "stop_loss_target": sl_target,
                "take_profit_target": tp_target,
                "exit_strategy": decision.get("exit_strategy"),
                "stop_loss_would_hit": sl_hit,
                "stop_loss_hit_day": sl_hit_day,
                "take_profit_would_hit": tp_hit,
                "take_profit_hit_day": tp_hit_day,
            },

            # Risk identificato dall'agente
            "risk_identified": decision.get("risk", ""),
        },
    }


async def _finalize_run(run_id: str):
    """
    Salva il run completo nel DB con benchmark + outcome calcolati.
    Wrappato in try/except: NESSUN errore deve propagare a execute_step.
    """
    state = _active_runs.get(run_id) or _load_active_run(run_id)
    if not state:
        return
    try:
        run_data = _build_run_data(run_id, state)
    except Exception as exc:
        logger.error("[SIM] _build_run_data crash su run %s: %s",
                     run_id, exc, exc_info=True)
        return

    try:
        sim_db.insert_run(run_data)
        logger.info("[SIM] Run %s salvato (outcome=%s)", run_id, run_data.get("outcome"))
        # Cleanup dello shadow state (run terminato, persistito in sim_runs)
        _clear_active_run(run_id)
    except Exception as exc:
        logger.error("[SIM] Errore salvataggio run %s: %s", run_id, exc, exc_info=True)


def _simulate_asset_return(scenario: dict, asset: Optional[str]
                            ) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Simula il ritorno dell'ASSET nel periodo (sempre dal punto di vista
    long buy-and-hold). NON applica inversione per SELL — questa funzione
    ritorna il movimento del prezzo, non il P&L dell'utente.

    Pipeline:
      1. Pattern ASSET[+-]X% nel description_reveal
      2. Pattern verbale (rose/fell/gained/lost X%)
      3. Heuristic per categoria scenario con magnitude realistica
    """
    if not asset:
        return 0.0, 0.0, 0.0

    desc_lower = (scenario.get("description_reveal") or "").lower()
    asset_lower = asset.lower()
    base: float | None = None

    import re as _re
    # Pattern 1
    m = _re.search(rf"\b{_re.escape(asset_lower)}\b[^a-z]{{0,40}}([+\-]?\d+(?:[.,]\d+)?)\s*%",
                   desc_lower)
    if m:
        try:
            num_str = m.group(1).replace(",", ".")
            base = float(num_str) / 100.0
        except Exception:
            pass

    # Pattern 2
    if base is None:
        verb_pos = r"(?:gain|gained|rose|rallied|surged|salì|cresciuto|guadagnato|recovered|recupera)"
        verb_neg = r"(?:fell|fall|drop|dropped|lost|crash|crashed|plunged|sceso|perso|crolla|tonfo)"
        m_pos = _re.search(rf"\b{_re.escape(asset_lower)}\b[^.]{{0,80}}{verb_pos}[^0-9]{{0,30}}(\d+(?:[.,]\d+)?)\s*%",
                           desc_lower)
        m_neg = _re.search(rf"\b{_re.escape(asset_lower)}\b[^.]{{0,80}}{verb_neg}[^0-9]{{0,30}}(\d+(?:[.,]\d+)?)\s*%",
                           desc_lower)
        if m_pos:
            base = float(m_pos.group(1).replace(",", ".")) / 100.0
        elif m_neg:
            base = -float(m_neg.group(1).replace(",", ".")) / 100.0

    # Pattern 3: heuristic
    if base is None:
        category = (scenario.get("category") or "").lower()
        ranges_by_cat = {
            "crash_rally":   (-0.12, +0.08),
            "geopolitico":   (-0.08, +0.06),
            "macro":         (-0.05, +0.07),
            "normale":       (-0.04, +0.06),
        }
        lo, hi = ranges_by_cat.get(category, (-0.05, +0.07))
        import random
        random.seed(hash(str(scenario.get("id", "")) + asset) & 0xFFFFFFFF)
        v1 = random.uniform(lo, hi)
        v2 = random.uniform(lo, hi)
        base = (v1 + v2) / 2.0
        if abs(base) < 0.015:
            base = 0.015 if base >= 0 else -0.015

    asset_1w = round(base * 0.3, 4)
    asset_1m = round(base, 4)
    asset_3m = round(base * 1.4, 4)
    logger.debug(
        "[SIM] _simulate_asset_return asset=%s scenario=%s → 1w=%.4f 1m=%.4f 3m=%.4f",
        asset, scenario.get("id"), asset_1w, asset_1m, asset_3m,
    )
    return asset_1w, asset_1m, asset_3m


def _position_pnl(asset_return: Optional[float], action: Optional[str]) -> Optional[float]:
    """
    Converte il return ASSET in P&L della POSIZIONE basata sull'azione.
      - BUY:  P&L = asset_return  (lungo, profit se sale)
      - SELL: P&L = -asset_return (corto, profit se scende)
      - HOLD: P&L = 0
    """
    if asset_return is None or action is None:
        return None
    if action == "HOLD":
        return 0.0
    if action == "SELL":
        return round(-asset_return, 4)
    return round(asset_return, 4)


# Wrapper legacy per back-compat (chiamato da altre parti?). Ora ritorna
# sempre il P&L della posizione (con SELL inversion già applicata).
def _simulate_outcome(scenario: dict, asset: Optional[str], action: Optional[str]
                       ) -> tuple[Optional[float], Optional[float], Optional[float]]:
    if not asset or action == "HOLD":
        return 0.0, 0.0, 0.0
    a1w, a1m, a3m = _simulate_asset_return(scenario, asset)
    return (
        _position_pnl(a1w, action),
        _position_pnl(a1m, action),
        _position_pnl(a3m, action),
    )


def _evaluate_thesis(decision: dict, scenario: dict, perf_1m: Optional[float]) -> str:
    """Frase descrittiva della valutazione tesi."""
    if perf_1m is None:
        return "Non valutabile (azione = HOLD)"
    if perf_1m > 0.03:
        return "La tesi si è realizzata: l'asset scelto ha sovraperformato."
    if perf_1m > 0:
        return "La tesi si è parzialmente realizzata."
    if perf_1m > -0.03:
        return "Esito neutro: poca differenza vs benchmark."
    return "La tesi non si è realizzata: l'asset ha sottoperformato."
