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

# In-memory state dei run attivi (single-step + multi-step in corso)
_active_runs: dict[str, dict] = {}


SIM_PROMPT_DEFAULT = """Sei un Investment Analyst AI in modalità SIMULATOR.
Stai analizzando uno scenario di mercato realistico per testare il tuo ragionamento.

⚠️ NON ricevi date né periodo storico. Il setup è "snapshot": ti vengono dati
prezzi correnti, headline e dati. Devi decidere come operare ORA, basandoti
solo su quello che vedi.

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
[UPDATE T+{step}] Stai continuando lo scenario.

Vediamo come sono evolute le cose. Headline e prezzi sono cambiati.
La tua decisione precedente è stata ricordata sotto.

DECISIONE PRECEDENTE T+{prev_step}:
  Action: {prev_action}
  Asset: {prev_asset}
  Conviction: {prev_conviction}
  Tesi: {prev_thesis}

PERFORMANCE DA T+{prev_step} A T+{step}:
{prev_perf}

Ora devi prendere una nuova decisione (può confermare, modificare o chiudere
la posizione precedente). Stessa procedura: [1] Lettura, [2] Ragionamento, [3] Decisione JSON.
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


def _build_context_prompt(scenario: dict, step_index: int = 0,
                           prev_steps: list[dict] | None = None) -> tuple[str, dict]:
    """
    Costruisce il messaggio user per il modello + ritorna il context (per UI).
    """
    headlines = list(scenario.get("headlines", []))
    market_data = list(scenario.get("market_data", []))
    asset_universe = list(scenario.get("asset_universe", []))

    # Per multi-step: ad ogni step variamo leggermente i dati per simulare il movimento
    # (in production sostituibile con vere serie storiche del periodo). Per ora,
    # applichiamo piccole perturbazioni casuali progressive.
    if step_index > 0:
        import random
        random.seed(hash(scenario["id"]) + step_index)
        market_data = [
            {**m, "price_t0": round(m["price_t0"] * (1 + random.uniform(-0.04, 0.06) * step_index), 2),
                  "change_24h": round(random.uniform(-3, 3), 2),
                  "change_7d": round(random.uniform(-8, 8), 2)}
            for m in market_data
        ]

    parts = []
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
    }

    # Multi-step: aggiungi update + decisione precedente
    if step_index > 0 and prev_steps:
        prev = prev_steps[-1]
        update_text = SIM_PROMPT_MULTI_UPDATE.format(
            step=step_index,
            prev_step=step_index - 1,
            prev_action=prev["decision"].get("action", "?"),
            prev_asset=prev["decision"].get("asset", "—"),
            prev_conviction=prev["decision"].get("conviction", "?"),
            prev_thesis=(prev.get("reasoning", "") or "")[:300],
            prev_perf="(simulazione: range -5/+5%)",
        )
        parts.insert(0, update_text)
        context_for_ui["update"] = (
            f"Headline e prezzi aggiornati al T+{step_index}. "
            f"Tua precedente decisione: {prev['decision'].get('action')} {prev['decision'].get('asset','')}."
        )

    # Inietta memoria
    memory = _load_memory_summary(scenario["category"])
    if memory:
        parts.insert(0, memory + "\n\n" + "=" * 60)

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
    _active_runs[run_id] = {
        "scenario": scenario,
        "scenario_type": scenario_type,
        "category": category,
        "total_steps": total,
        "steps": [],   # lista per ogni step: {context, reading, reasoning, decision, ...}
        "mode": mode,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.info("[SIM] Avviato run %s scenario=%s type=%s steps=%d",
                run_id, scenario["id"], scenario_type, total)
    return {"run_id": run_id, "scenario_id": scenario["id"], "total_steps": total}


def rebuild_run_from_memory(run_id: str) -> dict | None:
    """
    Ricostruisce i dati di un run gia' eseguito ma non ancora salvato nel DB
    (es. l'INSERT su Supabase e' fallito ma _active_runs ce l'ha).

    Robusto: ogni accesso a campi dello scenario usa .get() con fallback,
    cosi' anche se uno scenario ha schema parziale, il rebuild non crasha.
    """
    state = _active_runs.get(run_id)
    if not state:
        logger.warning("[SIM] rebuild_run_from_memory: %s NON in _active_runs (keys: %s)",
                       run_id, list(_active_runs.keys())[:5])
        return None
    if not state.get("steps"):
        logger.warning("[SIM] rebuild_run_from_memory: %s ha steps vuoti", run_id)
        return None
    try:
        scenario = state.get("scenario") or {}
        last_step = state["steps"][-1] if state.get("steps") else {}
        decision = last_step.get("decision") or {}
        asset = decision.get("asset")

        perf_1w, perf_1m, perf_3m = _simulate_outcome(
            scenario, asset, decision.get("action")
        )
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

        # Robust: usa .get() con fallback su tutti i campi scenario
        period_start = scenario.get("period_start", "?")
        period_end = scenario.get("period_end", "?")
        historical_period = f"{period_start} → {period_end}"

        market_data = scenario.get("market_data") or []
        if asset and market_data:
            anchor = next((m for m in market_data if m.get("ticker") == asset), None)
            anchor_price = anchor.get("price_t0", 100.0) if anchor else 100.0
            import random
            random.seed(hash(str(scenario.get("id", "")) + (asset or "")))
            target = anchor_price * (1 + (perf_3m or 0))
            price_chart = []
            for i in range(90):
                t = i / 89
                base = anchor_price + (target - anchor_price) * t
                noise = random.uniform(-0.02, 0.02) * anchor_price
                price_chart.append({"day": i, "price": round(base + noise, 2)})
        else:
            price_chart = []

        steps_data = []
        for s in state.get("steps", []):
            d = s.get("decision") or {}
            asset_step = d.get("asset")
            price = None
            if asset_step and market_data:
                price = next(
                    (m.get("price_t0") for m in market_data if m.get("ticker") == asset_step),
                    None,
                )
            steps_data.append({
                "action": d.get("action"),
                "asset": asset_step,
                "price": price,
                "perf_from_here": None,
            })

        result = {
            "id": run_id,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "mode": state.get("mode", "manual"),
            "category": scenario.get("category", "unknown"),
            "scenario_type": state.get("scenario_type", "single"),
            "steps": state.get("total_steps", 1),
            "scenario_id": scenario.get("id", "unknown"),
            "historical_period": historical_period,
            "asset_chosen": asset,
            "action_chosen": decision.get("action"),
            "conviction": decision.get("conviction"),
            "horizon": decision.get("horizon"),
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
            "original_thesis": (last_step.get("reasoning") or "")[:1500],
            "what_happened": scenario.get("description_reveal", ""),
            "thesis_evaluation": _evaluate_thesis(decision, scenario, perf_1m),
            "full_data": {
                "steps": state.get("steps", []),
                "price_chart": price_chart,
                "steps_data": steps_data,
            },
            "_source": "in_memory_fallback",
        }
        logger.info("[SIM] rebuild_run_from_memory ok: %s", run_id)
        return result
    except Exception as exc:
        logger.error("[SIM] rebuild_run_from_memory %s failed: %s", run_id, exc, exc_info=True)
        return None


async def execute_step(run_id: str, step_index: int) -> dict:
    """Esegue uno step del run. Ritorna context + output del modello."""
    if run_id not in _active_runs:
        raise ValueError(f"Run {run_id} non trovato (forse scaduto)")
    state = _active_runs[run_id]
    scenario = state["scenario"]

    user_msg, context_for_ui = _build_context_prompt(
        scenario, step_index=step_index, prev_steps=state["steps"]
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

    # Se è l'ultimo step, persisti il run e calcola gli outcome
    if step_data["is_last_step"]:
        await _finalize_run(run_id)

    return step_data


async def _finalize_run(run_id: str):
    """Salva il run completo nel DB con benchmark + outcome calcolati."""
    state = _active_runs.get(run_id)
    if not state:
        return
    scenario = state["scenario"]
    last_step = state["steps"][-1]
    decision = last_step["decision"]
    asset = decision.get("asset")

    # Benchmark calculation: per la fase 1, usiamo i campi description_reveal
    # come base + simulazione realistica. In v2 si può collegare a serie
    # storiche reali con yfinance.
    perf_1w, perf_1m, perf_3m = _simulate_outcome(scenario, asset, decision.get("action"))
    perf_sp_1m = 0.02   # benchmark statico, in v2 serie reali
    perf_sector_1m = 0.015
    perf_monkey_1m = 0.005
    delta_sp = perf_1m - perf_sp_1m if perf_1m is not None else None
    delta_sector = perf_1m - perf_sector_1m if perf_1m is not None else None
    delta_monkey = perf_1m - perf_monkey_1m if perf_1m is not None else None

    # Outcome semaforo
    if perf_1m is None:
        outcome = "yellow"
    elif delta_sp and delta_sp > 0.005 and delta_sector and delta_sector > 0:
        outcome = "green"
    elif delta_sp and delta_sp < -0.01:
        outcome = "red"
    else:
        outcome = "yellow"

    historical_period = f"{scenario['period_start']} → {scenario['period_end']}"

    # Costruisci price_chart simulato per UI (90 punti tra T0 e fine periodo)
    if asset:
        anchor = next((m for m in scenario["market_data"] if m["ticker"] == asset), None)
        anchor_price = anchor["price_t0"] if anchor else 100.0
        # genera curva semplice con drift verso il perf_3m
        import random
        random.seed(hash(scenario["id"] + (asset or "")))
        target = anchor_price * (1 + (perf_3m or 0))
        price_chart = []
        for i in range(90):
            t = i / 89
            base = anchor_price + (target - anchor_price) * t
            noise = random.uniform(-0.02, 0.02) * anchor_price
            price_chart.append({"day": i, "price": round(base + noise, 2)})
    else:
        price_chart = []

    # Steps_data per multi-step UI
    steps_data = [{
        "action": s["decision"].get("action"),
        "asset": s["decision"].get("asset"),
        "price": next((m["price_t0"] for m in scenario["market_data"]
                        if m["ticker"] == s["decision"].get("asset")), None),
        "perf_from_here": None,   # calcolabile in v2
    } for s in state["steps"]]

    run_data = {
        "id": run_id,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "mode": state["mode"],
        "category": scenario["category"],
        "scenario_type": state["scenario_type"],
        "steps": state["total_steps"],
        "scenario_id": scenario["id"],
        "historical_period": historical_period,
        "asset_chosen": asset,
        "action_chosen": decision.get("action"),
        "conviction": decision.get("conviction"),
        "horizon": decision.get("horizon"),
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
        "original_thesis": (last_step.get("reasoning") or "")[:1500],
        "what_happened": scenario.get("description_reveal", ""),
        "thesis_evaluation": _evaluate_thesis(decision, scenario, perf_1m),
        "full_data": {
            "steps": state["steps"],
            "price_chart": price_chart,
            "steps_data": steps_data,
        },
    }
    try:
        sim_db.insert_run(run_data)
        logger.info("[SIM] Run %s salvato (outcome=%s)", run_id, outcome)
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
