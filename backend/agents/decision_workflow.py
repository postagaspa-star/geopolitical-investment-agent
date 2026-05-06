"""
Decision Workflow — state machine per il workflow obbligatorio a 4 fasi
del Decision Agent (standard + crypto).

Le 4 fasi:
  PHASE_NONE                → start
  PHASE_INITIAL_DONE        → dopo commit_initial_assessment
  PHASE_TECH_RECEIVED       → dopo (almeno una) request_(crypto_)technical_analysis
  PHASE_FINAL_THESIS_DONE   → dopo commit_final_thesis (sblocca trading)

Le transizioni sono lineari, ma c'e' uno "shortcut" se l'agente decide
in FASE 1 che non gli servono dati tecnici (technical_questions=[]):
puo' procedere direttamente a FASE 3 saltando FASE 2 (TECH_RECEIVED).

Questo modulo e' condiviso tra decision.py e decision_crypto.py per
mantenere coerenza tra Live equity e Live crypto.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

PHASE_NONE = "none"
PHASE_INITIAL_DONE = "initial_done"
PHASE_TECH_RECEIVED = "tech_received"
PHASE_FINAL_THESIS_DONE = "final_thesis_done"

MAX_TECH_REQUESTS_PER_RUN = 2
MIN_INITIAL_ASSESSMENT_CHARS = 200
MIN_FINAL_THESIS_CHARS = 200
MIN_TRADE_LOGIC_CHAIN_CHARS = 200


@dataclass
class WorkflowState:
    """Stato del workflow per un singolo run del Decision Agent."""
    phase: str = PHASE_NONE
    tech_request_count: int = 0
    initial_assessment: dict | None = None
    final_thesis: dict | None = None
    skip_tech_phase: bool = False  # True se commit_initial_assessment ha questions=[]

    # Tracciamento per logging diagnostico
    rejected_calls: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "tech_request_count": self.tech_request_count,
            "skip_tech_phase": self.skip_tech_phase,
            "rejected_calls": self.rejected_calls[:10],
            "has_initial_assessment": self.initial_assessment is not None,
            "has_final_thesis": self.final_thesis is not None,
        }


def can_call_tool(state: WorkflowState, tool_name: str) -> tuple[bool, str]:
    """
    Verifica se la chiamata al tool e' consentita nello stato corrente.
    Ritorna (allowed, error_message_if_not).

    Tool sempre consentiti (read-only): get_portfolio_state, request_extra_analysis.
    """
    READ_ONLY_TOOLS = {"get_portfolio_state", "request_extra_analysis"}
    if tool_name in READ_ONLY_TOOLS:
        return True, ""

    if tool_name == "commit_initial_assessment":
        if state.phase != PHASE_NONE:
            return False, (
                f"commit_initial_assessment gia' chiamato (fase corrente: "
                f"{state.phase}). Non puoi rifarlo nello stesso run. Procedi "
                f"con request_technical_analysis o commit_final_thesis."
            )
        return True, ""

    if tool_name in ("request_technical_analysis", "request_crypto_technical_analysis"):
        if state.phase == PHASE_NONE:
            return False, (
                "Devi PRIMA chiamare commit_initial_assessment "
                "(FASE 1 del workflow obbligatorio) prima di richiedere dati tecnici."
            )
        if state.phase == PHASE_FINAL_THESIS_DONE:
            return False, (
                "Hai gia' commitato la tesi finale (FASE 3). Non puoi tornare "
                "a richiedere altri dati tecnici. Procedi con execute_trade "
                "o do_nothing."
            )
        if state.tech_request_count >= MAX_TECH_REQUESTS_PER_RUN:
            return False, (
                f"Hai gia' richiesto dati tecnici {state.tech_request_count} volte "
                f"(limite: {MAX_TECH_REQUESTS_PER_RUN}). Procedi con commit_final_thesis."
            )
        return True, ""

    if tool_name == "commit_final_thesis":
        if state.phase == PHASE_NONE:
            return False, (
                "Devi PRIMA chiamare commit_initial_assessment "
                "(FASE 1 del workflow obbligatorio)."
            )
        if state.phase == PHASE_FINAL_THESIS_DONE:
            return False, (
                "Hai gia' commitato la tesi finale. Procedi con execute_trade "
                "o do_nothing."
            )
        # E' OK chiamarlo direttamente da INITIAL_DONE se skip_tech_phase=True
        # oppure da TECH_RECEIVED.
        if state.phase == PHASE_INITIAL_DONE and not state.skip_tech_phase:
            return False, (
                "Devi PRIMA chiamare request_technical_analysis (FASE 2) "
                "se hai dichiarato di volere dati tecnici. Se hai cambiato "
                "idea, dichiara esplicitamente nel thesis che procedi senza "
                "dati tecnici e perche'."
            )
        return True, ""

    if tool_name in ("execute_trade", "do_nothing"):
        if state.phase != PHASE_FINAL_THESIS_DONE:
            return False, (
                f"Devi PRIMA completare il workflow a 4 fasi. Stato corrente: "
                f"{state.phase}. Manca: "
                + _missing_steps(state)
            )
        return True, ""

    if tool_name in ("set_stop_loss", "set_take_profit"):
        # Modifiche a posizioni esistenti: consentite dopo FASE 3 (con tesi)
        if state.phase != PHASE_FINAL_THESIS_DONE:
            return False, (
                "Aggiornare SL/TP richiede prima di completare il workflow a "
                "4 fasi. Stato: " + state.phase
            )
        return True, ""

    # Tool sconosciuto: lascia passare
    return True, ""


def _missing_steps(state: WorkflowState) -> str:
    """Descrive cosa manca in formato human-readable."""
    if state.phase == PHASE_NONE:
        return "commit_initial_assessment + (eventuale) request_technical_analysis + commit_final_thesis"
    if state.phase == PHASE_INITIAL_DONE:
        if state.skip_tech_phase:
            return "commit_final_thesis"
        return "request_technical_analysis + commit_final_thesis"
    if state.phase == PHASE_TECH_RECEIVED:
        return "commit_final_thesis"
    return "(nulla manca, fase: " + state.phase + ")"


def apply_tool_transition(state: WorkflowState, tool_name: str,
                          tool_input: dict, tool_result: str) -> WorkflowState:
    """
    Aggiorna lo stato dopo una chiamata tool *consentita* (gia' validata
    da can_call_tool).
    """
    if tool_name == "commit_initial_assessment":
        questions = tool_input.get("technical_questions") or []
        state.initial_assessment = {
            "situation_overview": tool_input.get("situation_overview", ""),
            "asset_candidates": tool_input.get("asset_candidates", []),
            "technical_questions": questions,
        }
        state.phase = PHASE_INITIAL_DONE
        # Se non ci sono questions tecniche, possiamo saltare FASE 2
        state.skip_tech_phase = (len(questions) == 0)
        return state

    if tool_name in ("request_technical_analysis", "request_crypto_technical_analysis"):
        state.tech_request_count += 1
        # Avanza la fase solo se eravamo in INITIAL_DONE
        if state.phase == PHASE_INITIAL_DONE:
            state.phase = PHASE_TECH_RECEIVED
        return state

    if tool_name == "commit_final_thesis":
        state.final_thesis = {
            "thesis": tool_input.get("thesis", ""),
            "action_plan": tool_input.get("action_plan", ""),
            "primary_risk": tool_input.get("primary_risk", ""),
        }
        state.phase = PHASE_FINAL_THESIS_DONE
        return state

    # Altri tool: nessuna transizione di stato
    return state


def validate_commit_input(tool_name: str, tool_input: dict) -> tuple[bool, str]:
    """
    Validazione di contenuto per i tool di commit (lunghezza minima testi).
    Ritorna (ok, error_msg).
    """
    if tool_name == "commit_initial_assessment":
        ov = (tool_input.get("situation_overview") or "").strip()
        if len(ov) < MIN_INITIAL_ASSESSMENT_CHARS:
            return False, (
                f"situation_overview troppo breve ({len(ov)} char, minimo "
                f"{MIN_INITIAL_ASSESSMENT_CHARS}). Espandi l'analisi della "
                f"situazione corrente: cosa dice il portafoglio, quale tema "
                f"emerge dalle briefing, cosa motiva la scelta dei ticker."
            )
        cands = tool_input.get("asset_candidates")
        if not isinstance(cands, list):
            return False, "asset_candidates deve essere una lista (puo' essere vuota)."
        return True, ""

    if tool_name == "commit_final_thesis":
        th = (tool_input.get("thesis") or "").strip()
        if len(th) < MIN_FINAL_THESIS_CHARS:
            return False, (
                f"thesis troppo breve ({len(th)} char, minimo "
                f"{MIN_FINAL_THESIS_CHARS}). Devi articolare la tesi causale "
                f"integrando i dati tecnici ricevuti: 'se X allora Y perche'..., "
                f"il rischio principale e' Z'."
            )
        ap = (tool_input.get("action_plan") or "").strip()
        if not ap:
            return False, "action_plan non puo' essere vuoto."
        return True, ""

    if tool_name == "execute_trade":
        lc = (tool_input.get("logic_chain") or "").strip()
        if len(lc) < MIN_TRADE_LOGIC_CHAIN_CHARS:
            return False, (
                f"logic_chain troppo corto ({len(lc)} char, minimo "
                f"{MIN_TRADE_LOGIC_CHAIN_CHARS}). Devi spiegare il "
                f"trade citando esplicitamente la tesi commitata in FASE 3."
            )
        return True, ""

    return True, ""


# ─── Tool definitions condivise ─────────────────────────────────────────────

COMMIT_INITIAL_ASSESSMENT_TOOL = {
    "name": "commit_initial_assessment",
    "description": (
        "FASE 1 OBBLIGATORIA. Commit dell'analisi iniziale della situazione "
        "corrente (portfolio, briefing macro, sentiment buffer) PRIMA di "
        "richiedere dati tecnici. situation_overview deve essere >= 200 char. "
        "Se non ti servono dati tecnici, passa technical_questions=[]: "
        "potrai saltare la FASE 2 e andare direttamente a commit_final_thesis."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "situation_overview": {
                "type": "string",
                "description": "Analisi della situazione corrente senza dati tecnici "
                               "(>= 200 caratteri).",
            },
            "asset_candidates": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Ticker su cui vuoi indagare (puo' essere lista vuota se "
                               "il run e' di pure rebalancing/no-trade).",
            },
            "technical_questions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Domande tecniche specifiche da girare al Technical Agent. "
                               "Vuoto = salta FASE 2 e vai direttamente a commit_final_thesis.",
            },
        },
        "required": ["situation_overview", "asset_candidates", "technical_questions"],
    },
}


COMMIT_FINAL_THESIS_TOOL = {
    "name": "commit_final_thesis",
    "description": (
        "FASE 3 OBBLIGATORIA. Commit della tesi finale dopo aver integrato i "
        "dati tecnici (o dopo aver dichiarato di procedere senza). thesis deve "
        "essere >= 200 char. Solo dopo questa chiamata puoi eseguire trade."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "thesis": {
                "type": "string",
                "description": "Tesi causale finale che integra l'analisi iniziale con i "
                               "dati tecnici (>= 200 caratteri).",
            },
            "action_plan": {
                "type": "string",
                "description": "Piano operativo: quale ticker, BUY/SELL, dimensione "
                               "indicativa, livelli SL/TP previsti.",
            },
            "primary_risk": {
                "type": "string",
                "description": "Il rischio principale e cosa invaliderebbe la tesi.",
            },
        },
        "required": ["thesis", "action_plan", "primary_risk"],
    },
}


def make_rejection_result(error_msg: str, current_state: WorkflowState) -> str:
    """Costruisce un tool_result formattato per re-prompt al modello."""
    return json.dumps({
        "error": error_msg,
        "workflow_state": current_state.to_dict(),
        "hint": (
            "Rispetta il workflow a 4 fasi: 1) commit_initial_assessment, "
            "2) request_(crypto_)technical_analysis (max 2x), "
            "3) commit_final_thesis, 4) execute_trade/do_nothing. "
            "Vedi WORKFLOW_PHASES nel system prompt."
        ),
    })
