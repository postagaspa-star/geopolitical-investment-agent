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
_active_runs: dict[str, dict] = {}
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
    """
    state = _active_runs.get(run_id)
    if state is not None:
        return state
    state = _load_active_run(run_id)
    if state is not None:
        _active_runs[run_id] = state
        logger.info("[SIM] state %s ricaricato da shadow persistence", run_id)
    return state


SIM_PROMPT_DEFAULT = """Sei un Investment Analyst AI in modalità SIMULATOR.
Stai analizzando uno scenario di mercato realistico per testare il tuo ragionamento.

⚠️ NON ricevi date né periodo storico. Il setup è "snapshot": ti vengono dati
prezzi correnti, headline e dati. Devi decidere come operare ORA, basandoti
solo su quello che vedi.

Il messaggio user ti dira' se sei in modalita' SINGLE-STEP (decisione una-tantum,
orizzonte breve max 1 mese) o MULTI-STEP (turni successivi, decisioni adattive).

═══════════════════════════════════════════════════════════════════════
PROCEDURA OBBLIGATORIA (3 sezioni nominate, in ordine, NESSUNA OMISSIONE):
═══════════════════════════════════════════════════════════════════════

[1] LETTURA DEL CONTESTO
    Sintesi di cosa stai vedendo:
    - Tema/regime suggerito dalle headline (3-5 righe)
    - Asset più mossi nei prezzi e direzione
    - Eventuali campanelli d'allarme o opportunità immediate

[2] RAGIONAMENTO CAUSALE
    - Tesi principale: catena causale ('se X allora Y perché...')
    - Cosa nei dati supporta la tesi
    - Cosa potrebbe invalidarla
    - Rischio principale di una decisione operativa

[3] DECISIONE
    Output STRUTTURATO sotto forma di JSON, formato esatto (NIENTE testo extra):
    {
      "action": "BUY" | "SELL" | "HOLD",
      "asset": "TICKER",  // se action=HOLD, può essere null
      "conviction": "BASSA" | "MEDIA" | "ALTA",
      "horizon": "1g" | "1settimana" | "1mese" | "3mesi",
      "risk": "frase breve sul rischio principale (1-2 righe)",
      "stop_loss_target": null | <prezzo assoluto>,
      "take_profit_target": null | <prezzo assoluto>,
      "exit_strategy": "trailing_atr" | "level_target" | "time_based" | "discretionary"
    }

REGOLE:
- Le sezioni [1] e [2] DEVONO essere scritte in plain text
- La sezione [3] DEVE essere un JSON valido nel formato specificato
- Asset deve essere uno dei ticker dell'asset_universe fornito
- Niente testo dopo il JSON di [3]
- I campi stop_loss_target e take_profit_target sono PREZZI ASSOLUTI (es. 150.50),
  non percentuali. Se action='HOLD' o non riesci a fissarli, usa null.
- exit_strategy descrive il TUO approccio: 'level_target' se hai mirato livelli
  tecnici precisi, 'trailing_atr' se prevedi adattamento via ATR, 'time_based'
  se chiuderesti dopo X giorni indipendentemente dal prezzo, 'discretionary'
  se preferisci decidere step-by-step.
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


async def _call_r1(system_prompt: str, user_message: str) -> str:
    """Chiama DeepSeek-R1. Ritorna il response_text.

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
    async with aiohttp.ClientSession() as sess:
        async with sess.post(DEEPSEEK_API_URL, json=payload, headers=headers,
                              timeout=aiohttp.ClientTimeout(total=180)) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise ValueError(f"DeepSeek-R1 HTTP {resp.status}: {body[:300]}")
            data = await resp.json()
    return data["choices"][0]["message"]["content"] or ""


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
        m = re.search(r'\{[^{}]*"action"[\s\S]*?\}', txt)
        if m:
            try:
                decision_json = json.loads(m.group(0))
            except Exception:
                pass

    return {
        "reading": reading or txt[:800],
        "reasoning": reasoning or "",
        "decision": {
            "action": decision_json.get("action", "HOLD"),
            "asset": decision_json.get("asset"),
            "conviction": decision_json.get("conviction", "BASSA"),
            "horizon": decision_json.get("horizon", "1settimana"),
            "risk": decision_json.get("risk", ""),
            # Nuovi campi SL/TP: opzionali, possono essere None
            "stop_loss_target": decision_json.get("stop_loss_target"),
            "take_profit_target": decision_json.get("take_profit_target"),
            "exit_strategy": decision_json.get("exit_strategy", "discretionary"),
        },
        "raw": txt,
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
        "decision": parsed["decision"],
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

    # ── PERFORMANCE: usa la decisione di ENTRY (primo step), non l'ultima.
    # In multi-step l'AI puo' fare HOLD/RUOTA negli step successivi, ma il
    # P&L deve essere calcolato sull'azione iniziale altrimenti vedi sempre
    # 0% se l'ultimo step era HOLD.
    # Se primo step e' HOLD, prova lo step successivo che ha BUY/SELL.
    entry_decision = None
    for s in steps:
        d = (s.get("decision") or {})
        if d.get("action") in ("BUY", "SELL") and d.get("asset"):
            entry_decision = d
            break
    if entry_decision is None:
        # Tutti HOLD → outcome = 0
        entry_decision = decision

    asset = entry_decision.get("asset") or decision.get("asset")
    entry_action = entry_decision.get("action") or decision.get("action")

    perf_1w, perf_1m, perf_3m = _simulate_outcome(scenario, asset, entry_action)
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
        target = entry_price * (1 + (perf_3m or 0))
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
    steps_data = []
    for i, s in enumerate(steps):
        d = s.get("decision") or {}
        asset_step = d.get("asset")
        price = None
        if asset_step:
            anchor_step = next(
                (m for m in market_data if m.get("ticker") == asset_step), None
            )
            if anchor_step:
                price = anchor_step.get("price_t0")
        steps_data.append({
            "step_index": i,
            "action": d.get("action"),
            "asset": asset_step,
            "conviction": d.get("conviction"),
            "horizon": d.get("horizon"),
            "price": price,
            "stop_loss_target": d.get("stop_loss_target"),
            "take_profit_target": d.get("take_profit_target"),
            "exit_strategy": d.get("exit_strategy"),
            "risk": d.get("risk", "")[:300],
            "perf_from_here": None,
        })

    # ── Verifica TP/SL: i target proposti sarebbero stati toccati nel periodo?
    sl_target = decision.get("stop_loss_target")
    tp_target = decision.get("take_profit_target")
    sl_hit = None
    tp_hit = None
    sl_hit_day = None
    tp_hit_day = None
    if price_chart:
        for day_obj in price_chart:
            p = day_obj["price"]
            if sl_target and sl_hit is None and p <= sl_target:
                sl_hit = True
                sl_hit_day = day_obj["day"]
            if tp_target and tp_hit is None and p >= tp_target:
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

        # Performance core
        "perf_1w": perf_1w,
        "perf_1m": perf_1m,
        "perf_3m": perf_3m,
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


def _simulate_outcome(scenario: dict, asset: Optional[str], action: Optional[str]
                       ) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Simulazione semplice della performance dell'asset nel periodo.
    Per ora basata sul description_reveal (semplificato — in v2 usare serie reali).
    """
    if not asset or action == "HOLD":
        return 0.0, 0.0, 0.0
    desc = (scenario.get("description_reveal") or "").lower()
    base = 0.0
    # Heuristic semplice: se asset compare nel reveal con +X%, usa quello
    import re as _re
    m = _re.search(rf"{asset.lower()}[^a-z]*([+\-]?\d+)%", desc)
    if m:
        base = int(m.group(1)) / 100.0
    else:
        # default: piccolo drift positivo casuale
        import random
        random.seed(hash(scenario["id"] + asset))
        base = random.uniform(-0.1, 0.15)

    # Inverti se action=SELL
    if action == "SELL":
        base = -base

    return round(base * 0.3, 4), round(base, 4), round(base * 1.4, 4)


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
