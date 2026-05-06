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
    Usa la stessa _build_run_data di _finalize_run per consistency.
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
        result = _build_run_data(run_id, state)
        result["_source"] = "in_memory_fallback"
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
    asset = decision.get("asset")
    market_data = scenario.get("market_data") or []

    # ── Performance base
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

        # Decision summary
        "asset_chosen": asset,
        "action_chosen": decision.get("action"),
        "conviction": decision.get("conviction"),
        "horizon": decision.get("horizon"),

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
    """Salva il run completo nel DB con benchmark + outcome calcolati."""
    state = _active_runs.get(run_id)
    if not state:
        return
    run_data = _build_run_data(run_id, state)
    try:
        sim_db.insert_run(run_data)
        logger.info("[SIM] Run %s salvato (outcome=%s)", run_id, run_data.get("outcome"))
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
