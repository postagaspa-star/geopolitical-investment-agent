"""
Decision Crypto Agent — DeepSeek-R1 reasoning, focus ESCLUSIVO crypto 24/7.

Riceve l'output di technical_crypto.py + buffer intelligence + report aggregati
+ documenti specifici crypto. Decide se eseguire trade su crypto.

Differenze rispetto a decision.py:
  - SOLO crypto: opera esclusivamente su ticker in formato -USD o X:
    (qualsiasi crypto liquida via yfinance/Polygon). NESSUNA whitelist
    hardcoded: l'agente sceglie i ticker su base liquidita', tesi e
    disponibilita' dati.
  - Documenti separati: carica solo doc con category='crypto' (non i doc
    generici dei mercati equity)
  - Prompt specializzato su rischio crypto: leverage, liquidation, depeg,
    regulatory, hack, exchange risk
  - Engine fisso DeepSeek-R1 (no fallback Sonnet — no Anthropic dependency)

Costo per run: ~$0.006 (R1 reasoning).
Schedule: ogni 1 ora, 24/7.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from uuid import uuid4

import aiohttp

logger = logging.getLogger(__name__)

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_R1_MODEL = "deepseek-reasoner"

# Riusiamo l'helper del decision.py per coerenza dashboard.
# Import lazy per evitare circular dependency a load-time.
def _build_reasoning_text(ia: dict, ft: dict, final_text: str) -> str:
    try:
        from agents.decision import _build_reasoning_text as _impl
        return _impl(ia, ft, final_text)
    except Exception:
        # Fallback inline se l'import fallisce
        # NB: cap 500 → 4000 per allinearsi al path principale (decision.py).
        # Il fallback è raro (solo se circular import a load time), ma
        # quando scatta il cap 500 troncava la CONCLUSIONE a metà frase.
        parts = []
        if ia.get("situation_overview"):
            parts.append(f"SITUAZIONE: {ia['situation_overview']}")
        if ia.get("rotation_summary"):
            parts.append(f"ROTATION: {ia['rotation_summary']}")
        if ft.get("thesis"):
            parts.append(f"TESI: {ft['thesis']}")
        if final_text:
            parts.append(f"CONCLUSIONE: {final_text[:4000]}")
        return "\n\n".join(parts) or final_text or "(no reasoning)"


# Cooldown per evitare doppi run nello stesso ora (es. test manuali)
CRYPTO_COOLDOWN_KEY = "last_decision_crypto_run_at"
CRYPTO_COOLDOWN_SECONDS = 50 * 60   # 50 min: lascia 10 min di margine vs schedule 1h


CRYPTO_DECISION_PROMPT_DEFAULT = """Sei il Decision Agent CRYPTO di GeoInvest AI — sistema autonomo focalizzato ESCLUSIVAMENTE sui mercati crypto (BTC, ETH, SOL, ecc.).

UNIVERSO INVESTIBILE — APERTO:
Qualsiasi crypto liquida in formato yfinance (suffisso -USD) o Polygon
(prefisso X:). NESSUNA whitelist hardcoded: hai liberta' di scegliere il
ticker su base liquidita', tesi, e disponibilita' dati.

Esempi di ticker comunemente liquidi e ben coperti dai feed:
  BTC-USD, ETH-USD, SOL-USD, DOGE-USD, AVAX-USD, ADA-USD, XRP-USD,
  LTC-USD, DOT-USD, LINK-USD, UNI-USD, ATOM-USD, MATIC-USD, NEAR-USD,
  BNB-USD, SHIB-USD, AAVE-USD, ARB-USD, OP-USD, INJ-USD, ...

REGOLE per la scelta del ticker:
  - Verifica liquidita': preferisci market cap > $500M per posizioni
    significative (slippage contenuto).
  - Conferma disponibilita' dati: se request_crypto_technical_analysis
    ritorna error sul ticker, e' probabile che yfinance non lo copra in
    modo affidabile — riprova con un alternativo.
  - Per memecoin / asset low-cap, riduci dimensione e tieni SL piu' stretti.

NON tradare equity (azioni/ETF) — quelli sono dominio del Decision normale.

CONTESTO CHE RICEVI:
1. Report tecnico crypto-specifico (tech_report) da Technical Crypto Agent
2. Documenti tecnici dedicati al crypto (analisi on-chain, framework di
   rischio, strategie di entry/exit)
3. Buffer intelligence ultime 60 min (sentiment retail Reddit/X, news
   regolamentari, hack, depeg, whale alerts)
4. Stato portafoglio corrente (cash + posizioni crypto già aperte)

REGOLE OPERATIVE:
- Allocazione max 30% del cash per singola posizione crypto (volatilità alta)
- Stop-loss tecnico OBBLIGATORIO sulle crypto: usa set_stop_loss o passa
  stop_loss in execute_trade. Niente posizioni naked overnight.
- Confidence threshold per BUY: >= 55%  (osa di piu')
- Confidence threshold per SELL/profit-taking discrezionale: >= 50%
- Max 15 posizioni crypto aperte contemporaneamente (NESSUN vincolo di
  categoria: sei libero di concentrarti su PoW, PoS o DeFi se la tesi
  e' forte, anche tutte e 15 nello stesso filone).
- GESTIONE POSIZIONI: nessuna soglia hardcoded di profit-taking o stop-loss.
  Vedi RISK MANAGEMENT PRINCIPLES sotto. Sei tu a decidere i livelli SL/TP
  basandoti su S/R tecnici, ATR, regime di mercato e narrazione corrente.

FILOSOFIA: OSA, non aspettare la convinzione perfetta. Se sentiment +
tecnico concordano (anche solo a livello MEDIO), opera. do_nothing va
usato solo se i dati sono palesemente contraddittori o sei al cap di 15
posizioni totali. La concentrazione mirata e' una scelta legittima,
non un errore da evitare.

IN DUBBIO, AGISCI: l'inazione e' la scelta del trader mediocre. Quando
i segnali sono ambigui ma una direzione e' lievemente piu' supportata,
prendi una posizione piccola con conviction MEDIA invece di lasciare
il run a vuoto.

TICKER CORRELATI ("nascosti"): non focalizzarti solo sui crypto che si
SONO GIA' MOSSI nel buffer/news. Spesso il movimento si propaga ai peers
in pochi minuti. Esempi:
- BTC rally → considera ETH (lag 5-15 min tipico), poi LTC/BCH (PoW peers)
- ETH breakout → SOL/AVAX (smart contract competitor), MATIC (L2)
- DeFi sentiment → LINK (oracle), UNI (DEX leader)
- Crash su uno major → cerca i "safe" relativi (BTC tende a tenere meglio
  in flight-to-quality crypto-to-crypto)
In FASE 1 (commit_initial_assessment), includi 2-3 ticker correlati
oltre ai trigger primari. Spesso l'entry anticipata su un peer in lag
e' il trade migliore.

RISCHI CRYPTO-SPECIFIC da valutare prima di operare:
- Liquidità: per altcoin minori (DOT, ATOM, NEAR) il book può svuotarsi
- Regolamentazione: SEC/MiCA possono cambiare regime di un singolo asset
- Sentiment regime: bull market → bias BUY su breakouts, bear → bias SELL su rallies
- Funding rate squeezes: se rilevati nel buffer, attesa fino a stabilizzazione

WORKFLOW OBBLIGATORIO A 4 FASI (state machine enforced):

FASE 1 — Pre-analisi (commit_initial_assessment):
  Analizza la SOLA situazione corrente: portfolio crypto, buffer sentiment
  retail, news regulatorie/macro overnight, catalisti potenziali. Identifica
  i ticker crypto da indagare e le domande tecniche specifiche.
  → tool: commit_initial_assessment(situation_overview, asset_candidates,
           technical_questions). situation_overview >= 200 caratteri.

FASE 2 — Richiesta dati tecnici (request_crypto_technical_analysis):
  Chiama il Technical Crypto Agent con i ticker e le domande di FASE 1.
  MAX 2 chiamate per run. Se non servono dati tecnici (es. solo SL update),
  passa technical_questions=[] in FASE 1 e salta a FASE 3.

FASE 3 — Tesi finale (commit_final_thesis):
  Integra pre-analisi + dati tecnici in tesi causale ('se X allora Y perché').
  → tool: commit_final_thesis(thesis, action_plan, primary_risk).
     thesis >= 200 caratteri.

FASE 4 — Trading:
  execute_trade (BUY/SELL, logic_chain >= 200 char che cita la tesi),
  do_nothing (con reasoning), o set_stop_loss / set_take_profit.

Per CHIUDERE una posizione: get_portfolio_state per leggere quantity,
poi execute_trade(action='SELL', quantity=...) — parziale o totale.

Se chiami execute_trade prima di aver completato le 4 fasi, il sistema
TI RIFIUTA il tool con un errore esplicito e dovrai riprovare.

MEMORIA TRA I RUN — set_commitment / resolve_commitment:
Hai a disposizione una memoria persistente per le tue intenzioni. Se
nel reasoning dichiari un piano condizionato (es. "monitoro BTC.D, "
"compro ETH se sotto 52% entro 24h", "attendo unlock SOL del 12 maggio
prima di operare"), DEVI registrarlo con set_commitment — altrimenti
il prossimo run non lo sa. Quando un OBIETTIVO ATTIVO mostrato nel
contesto e' soddisfatto/decaduto, chiamalo resolve_commitment(triggered/cancelled)."""


def _get_deepseek_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        try:
            import database as _db
            key = _db.get_setting("deepseek_api_key", "") or ""
        except Exception:
            pass
    return key


def _get_crypto_decision_prompt() -> str:
    """Versione legacy compat: ritorna solo testo (no card_ids tracking)."""
    text, _ids = _get_crypto_decision_prompt_with_meta()
    return text


def _get_crypto_decision_prompt_with_meta() -> tuple[str, list[str]]:
    """Carica prompt custom + Direttive + Coach Cards + Shared.

    Ritorna (prompt_text, coach_card_ids). Il caller deve invocare
    coach_cards.increment_applied(id) DOPO il successo del run.

    Ordine di priorità nel prompt finale:
      1. Direttive Utente (in cima, max priority)
      2. Risk profile (crypto-specific)
      3. Regime Protocol (framework decisionale obbligatorio)
      4. Coach Cards (sintesi settimanale memoria Simulator)
      5. Memoria operativa (ultime decisioni crypto Live)
      6. Prompt base (custom o default)
      7. Shared principles (in coda)
    """
    base = CRYPTO_DECISION_PROMPT_DEFAULT
    try:
        import database as _db
        custom = _db.get_setting("prompt_decision_crypto", "")
        if custom and isinstance(custom, str) and custom.strip():
            base = custom
    except Exception:
        pass

    # 1. Direttive utente in cima
    try:
        from agents.decision import _build_directives_block
        directives_block = _build_directives_block()
    except Exception:
        directives_block = ""

    # 2. Risk profile (asset_class=crypto, soglie diverse rispetto equity)
    try:
        from agents.decision import _build_risk_block
        risk_block = _build_risk_block(asset_class="crypto")
    except Exception:
        risk_block = ""

    # 2b. Risk state live (recovery mode, dd 24h, WR, concentration)
    risk_state_block = ""
    try:
        import risk_state as _rs
        risk_state_block = _rs.build_risk_state_prompt_block()
    except Exception as e:
        logger.debug("[DEC-CRYPTO] risk_state block fail: %s", e)

    # 3. Regime Protocol (asset_class=crypto): forza la classificazione
    #    del regime e aggiunge default NO_TRADE per LATERAL/MACRO.
    #    Per crypto i regimi LATERAL e MACRO sono particolarmente
    #    insidiosi (range trading senza catalyst macro forte erode
    #    capitale via funding/spread), quindi il default flat e' ancora
    #    piu' importante che su equity.
    regime_block = ""
    try:
        from agents.decision import _build_regime_protocol_block
        regime_block = _build_regime_protocol_block(asset_class="crypto")
    except Exception as exc:
        logger.debug("[DEC-CRYPTO] regime protocol block failed: %s", exc)

    # 4. Coach Cards: stesse del Decision standard (la sintesi settimanale
    #    legge tutti gli advice del Simulator, equity + crypto). Le card hanno
    #    `category_focus` e `scenarios_signature` cosi' R1 sa quali sono
    #    crypto-specific. Iniettare TUTTE non confonde: il Decision Crypto
    #    filtra per rilevanza nel context del prompt.
    coach_block = ""
    coach_card_ids: list[str] = []
    try:
        from agents import coach_cards as _cc
        coach_block = _cc.get_active_cards_block_for_decision() or ""
        if coach_block:
            for c in _cc.list_cards(active_only=True)[:10]:
                cid = c.get("id")
                if cid:
                    coach_card_ids.append(cid)
    except Exception as e:
        logger.debug("[DEC-CRYPTO] coach cards block fallito: %s", e)
    coach_section = (coach_block + "\n\n" + "═" * 60 + "\n") if coach_block else ""

    # 5. Memoria operativa: ultime N decisioni crypto del Live (feedback loop).
    #    Iniettata anche qui per evitare che R1 ripeta tesi gia' applicate
    #    senza esito su crypto laterali / regime range-bound.
    recent_section = ""
    try:
        from agents.decision import _build_recent_decisions_block
        recent_block = _build_recent_decisions_block(agent_type="crypto", limit=12)
        if recent_block:
            recent_section = recent_block + "\n" + "═" * 60 + "\n"
    except Exception as exc:
        logger.debug("[DEC-CRYPTO] recent decisions block failed: %s", exc)

    # 6. Shared principles in coda
    try:
        from agents.shared_principles import get_full_risk_block_for_live
        shared = get_full_risk_block_for_live()
        text = (directives_block + risk_block + risk_state_block + regime_block
                + coach_section + recent_section + base
                + "\n\n" + "═" * 60 + "\n" + shared)
    except Exception:
        text = (directives_block + risk_block + risk_state_block + regime_block
                + coach_section + recent_section + base)
    return text, coach_card_ids




def is_cooldown_active() -> tuple[bool, int]:
    """Ritorna (True, seconds_left) se cooldown 50min ancora attivo."""
    try:
        import database as _db
        last_iso = _db.get_setting(CRYPTO_COOLDOWN_KEY, "") or ""
        if not last_iso:
            return False, 0
        last_dt = datetime.fromisoformat(last_iso)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
        if elapsed < CRYPTO_COOLDOWN_SECONDS:
            return True, int(CRYPTO_COOLDOWN_SECONDS - elapsed)
        return False, 0
    except Exception:
        return False, 0


def _record_run_timestamp():
    try:
        import database as _db
        _db.set_setting(CRYPTO_COOLDOWN_KEY, datetime.now(timezone.utc).isoformat())
    except Exception:
        pass


# ─── Tools ──────────────────────────────────────────────────────────────────

# Importa workflow shared
from agents.decision_workflow import (
    COMMIT_INITIAL_ASSESSMENT_TOOL,
    COMMIT_FINAL_THESIS_TOOL,
    WorkflowState,
    can_call_tool,
    apply_tool_transition,
    validate_commit_input,
    make_rejection_result,
)


def _wrap_anthropic_tool_for_openai(t: dict) -> dict:
    """Converte tool format Anthropic → OpenAI/DeepSeek (per i tool del workflow)."""
    return {
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t["description"],
            "parameters": t["input_schema"],
        },
    }


CRYPTO_DECISION_TOOLS = [
    _wrap_anthropic_tool_for_openai(COMMIT_INITIAL_ASSESSMENT_TOOL),
    _wrap_anthropic_tool_for_openai(COMMIT_FINAL_THESIS_TOOL),
    {
        "type": "function",
        "function": {
            "name": "execute_trade",
            "description": (
                "Esegue un trade crypto. action='BUY' apre/incrementa posizione, "
                "action='SELL' chiude/riduce. quantity = numero unità (intero)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string",
                               "description": "Crypto in formato yfinance (BTC-USD, ETH-USD, ...)"},
                    "action": {"type": "string", "enum": ["BUY", "SELL"]},
                    # number (NON integer): crypto frazionarie obbligatorie.
                    # BTC@$100k: 1 BTC = $100k che eccede risk-cap 30% → l'AI
                    # DEVE poter emettere 0.5 BTC. Lo schema integer bloccava
                    # silenziosamente ogni decisione su BTC/ETH.
                    "quantity": {"type": "number", "exclusiveMinimum": 0},
                    "stop_loss": {"type": "number"},
                    "take_profit": {"type": "number"},
                    "logic_chain": {"type": "string", "description": "Reasoning tecnico+sentiment"},
                    "confidence_level": {"type": "number", "minimum": 0, "maximum": 100},
                },
                "required": ["ticker", "action", "quantity", "logic_chain", "confidence_level"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "do_nothing",
            "description": "Non operare; motivazione obbligatoria.",
            "parameters": {
                "type": "object",
                "properties": {"reasoning": {"type": "string"}},
                "required": ["reasoning"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_crypto_technical_analysis",
            "description": (
                "Chiama il Technical Crypto Agent (DeepSeek-V3) IN TEMPO REALE per "
                "ottenere analisi fresh su una lista di crypto (max 5). Risponde "
                "con 'analyses' per i ticker analizzati e 'errors_per_ticker' per "
                "quelli falliti. Se i dati che ti servivano sono in errors_per_ticker, "
                "NON inventare: cambia ticker o usa do_nothing motivando."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tickers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1, "maxItems": 5,
                        "description": "Crypto in formato yfinance (BTC-USD, ETH-USD, ...)",
                    },
                    "focus_question": {
                        "type": "string",
                        "description": "Cosa vuoi sapere (es. 'breakout 100k BTC?', 'RSI ETH 4H')",
                    },
                },
                "required": ["tickers", "focus_question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_stop_loss",
            "description": (
                "Imposta o aggiorna lo stop-loss AUTOMATICO su una crypto in "
                "portafoglio. Quando il prezzo scende a stop_price, il sistema "
                "chiude automaticamente. Passa stop_price=0 per rimuovere."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "stop_price": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["ticker", "stop_price", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_take_profit",
            "description": (
                "Imposta o aggiorna il take-profit AUTOMATICO. Quando il prezzo "
                "raggiunge target_price, il sistema chiude automaticamente."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "target_price": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["ticker", "target_price", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_portfolio_state",
            "description": "Stato corrente cash + posizioni con SL/TP impostati.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_commitment",
            "description": (
                "Registra un IMPEGNO/INTENZIONE che il prossimo run del Decision Crypto "
                "dovra' ricordare. Esempi: 'monitorare BTC dominance e comprare ETH "
                "se scende sotto 52% entro 24h', 'attendere il merge ETH o l'unlock "
                "SOL prima di operare'. Senza questo tool la promessa scompare alla "
                "fine del run. Tipi: monitor, conditional_buy, conditional_sell, "
                "watch_event, reminder. Gli impegni attivi appariranno come OBIETTIVI "
                "ATTIVI nei prossimi run."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "commitment_type": {
                        "type": "string",
                        "enum": ["monitor", "conditional_buy", "conditional_sell",
                                 "watch_event", "reminder"],
                    },
                    "condition_text": {
                        "type": "string",
                        "description": "Descrizione condizione/obiettivo (max 2000 char).",
                    },
                    "trigger_action": {
                        "type": "string",
                        "description": "Azione quando si verifica (es. 'BUY ETH 0.5%', 'CLOSE BTC long').",
                    },
                    "expires_in_hours": {
                        "type": "number",
                        "minimum": 1,
                        "maximum": 720,
                        "description": "Scadenza in ore (1-720). Tipico crypto: 12-72.",
                    },
                    "ticker": {
                        "type": "string",
                        "description": "Crypto ticker (es. BTC-USD, ETH-USD).",
                    },
                },
                "required": ["commitment_type", "condition_text", "expires_in_hours"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resolve_commitment",
            "description": (
                "Marca un impegno attivo come triggered (condizione soddisfatta + "
                "azione eseguita) o cancelled (non piu' rilevante). Usalo quando un "
                "OBIETTIVO ATTIVO mostrato nel contesto e' stato raggiunto o decaduto."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "commitment_id": {"type": "integer"},
                    "resolution_status": {
                        "type": "string",
                        "enum": ["triggered", "cancelled"],
                    },
                    "resolution_reason": {"type": "string"},
                },
                "required": ["commitment_id", "resolution_status", "resolution_reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_capital_from_orchestrator",
            "description": (
                "Chiede al CAPITAL ORCHESTRATOR (DeepSeek-V3) di liberare capitale "
                "vendendo parzialmente posizioni dell'altro agente (in questo caso "
                "il Decision Standard, equity/ETF). Da usare SOLO se hai conviction "
                ">= 0.70 su un trade crypto ma il cash disponibile non basta perché "
                "bloccato in posizioni equity.\n\n"
                "L'orchestratore vede entrambi i sub-portfolio e decide se conviene "
                "liquidare parzialmente una posizione equity (preferendo: profit alto, "
                "età matura >7gg, % NAV alto). Guardrail automatici: cooldown 2h, "
                "max 25% NAV per riallocazione, no-realize-loss > -10%.\n\n"
                "Output: approved=true significa che il cash è già stato liberato → "
                "riprova execute_trade. approved=false → usa do_nothing o scala la size.\n\n"
                "Costo: ~$0.0002 (V3 chat). Non chiamare se gap < $500."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string",
                               "description": "Ticker crypto che vuoi comprare (es. 'BTC-USD')."},
                    "amount_needed_usd": {"type": "number", "exclusiveMinimum": 0,
                                           "description": "USD totale necessario per il trade."},
                    "amount_available_usd": {"type": "number", "minimum": 0,
                                              "description": "USD disponibili come cash (da get_portfolio_state)."},
                    "conviction": {"type": "number", "minimum": 0, "maximum": 1,
                                    "description": "Conviction 0.0-1.0. Min 0.70."},
                    "reasoning": {"type": "string",
                                   "description": "Spiegazione (max 500 char) del perché serve capitale."},
                },
                "required": ["ticker", "amount_needed_usd", "amount_available_usd",
                              "conviction", "reasoning"],
            },
        },
    },
]


# ─── Tool handler ───────────────────────────────────────────────────────────

async def _handle_tool(tool_name: str, tool_input: dict, run_id: str,
                        workflow_state=None) -> str:
    import data_fetchers
    import database
    import portfolio

    timestamp = datetime.now(timezone.utc).isoformat()

    try:
        # ── Tool del workflow a 4 fasi ─────────────────────────────────────
        if tool_name == "commit_initial_assessment":
            payload = {
                "situation_overview": (tool_input.get("situation_overview") or "")[:6000],
                "asset_candidates": tool_input.get("asset_candidates") or [],
                "technical_questions": tool_input.get("technical_questions") or [],
            }
            database.insert_agent_log(run_id, "DECISION_CRYPTO_PHASE1", json.dumps(payload, default=str))
            return json.dumps({
                "phase": "INITIAL_DONE",
                "ack": "Pre-analisi crypto committata. Procedi con request_crypto_technical_analysis "
                       "se hai domande tecniche, altrimenti vai a commit_final_thesis.",
                "questions_count": len(payload["technical_questions"]),
            })

        if tool_name == "commit_final_thesis":
            payload = {
                "thesis": (tool_input.get("thesis") or "")[:6000],
                "action_plan": (tool_input.get("action_plan") or "")[:2000],
                "primary_risk": (tool_input.get("primary_risk") or "")[:2000],
            }
            database.insert_agent_log(run_id, "DECISION_CRYPTO_PHASE3", json.dumps(payload, default=str))
            return json.dumps({
                "phase": "FINAL_THESIS_DONE",
                "ack": "Tesi finale crypto committata. Procedi con execute_trade o do_nothing.",
            })

        if tool_name == "execute_trade":
            ticker = tool_input["ticker"]
            action = tool_input["action"]
            # FIX: float (non int). Crypto frazionarie (0.5 BTC) sono lecite.
            # int() troncava 0.5 BTC a 0 → trade rifiutato silenziosamente.
            quantity = float(tool_input["quantity"])
            logic_chain = tool_input["logic_chain"]
            confidence = tool_input["confidence_level"]
            # Stop_loss/take_profit: parse esplicito per evitare il bug
            # `or None` che convertiva 0.0 a None silenziosamente.
            sl_raw = tool_input.get("stop_loss")
            tp_raw = tool_input.get("take_profit")
            try:
                stop_loss = float(sl_raw) if sl_raw is not None and float(sl_raw) > 0 else None
            except (TypeError, ValueError):
                stop_loss = None
            try:
                take_profit = float(tp_raw) if tp_raw is not None and float(tp_raw) > 0 else None
            except (TypeError, ValueError):
                take_profit = None

            # ── ARRICCHIMENTO logic_chain con thesis dal workflow_state ──
            if workflow_state is not None and getattr(workflow_state, "final_thesis", None):
                ft = workflow_state.final_thesis or {}
                thesis = (ft.get("thesis") or "").strip()
                plan = (ft.get("action_plan") or "").strip()
                risk = (ft.get("primary_risk") or "").strip()
                enriched_parts = []
                if thesis:
                    enriched_parts.append(f"[TESI] {thesis[:1500]}")
                if plan:
                    enriched_parts.append(f"[PIANO] {plan[:600]}")
                if risk:
                    enriched_parts.append(f"[RISCHIO] {risk[:400]}")
                if enriched_parts and (logic_chain or "").strip():
                    enriched_parts.append(f"[ESECUZIONE] {logic_chain.strip()[:1000]}")
                if enriched_parts:
                    logic_chain = "\n\n".join(enriched_parts)

            # Pre-validation: solo ticker crypto. Decision Crypto opera SOLO su crypto.
            t_up = (ticker or "").upper()
            is_crypto = t_up.endswith("-USD") or t_up.startswith("X:")
            if not is_crypto:
                return json.dumps({
                    "error": f"REJECTED: '{ticker}' non è crypto. Decision Crypto opera SOLO su crypto.",
                    "rejected": True,
                })

            # Ottieni current_price FRESCO (bypass cache, anti-stale).
            # PRIMA: cache price_quotes 120s + fallback fetch_market_data
            # CACHED 5min → su crypto 24/7 a volatilita' alta un prezzo
            # vecchio anche di 1-2 min puo' essere off di 0.5-2%. Caso
            # LINK-USD documentato: BUY a prezzo cached, polling subito
            # dopo aggiorna current_price, P&L apparente fittizio.
            # FIX: fetch live via fetch_fresh_current_price (bypass_cache=True).
            current_price = None
            try:
                loop = asyncio.get_running_loop()
                current_price = await loop.run_in_executor(
                    None, data_fetchers.fetch_fresh_current_price, ticker
                )
            except Exception as exc:
                logger.warning("[%s][DEC-CRYPTO] fetch_fresh %s fail: %s",
                               run_id, ticker, exc)
            if current_price is None:
                # Fallback 1: cache price_quotes (TTL 120s, accetta solo
                # se i provider esterni sono down)
                try:
                    from price_polling import get_cached_prices_bulk
                    cached = get_cached_prices_bulk([ticker], max_age_seconds=120)
                    if cached.get(ticker):
                        current_price = float(cached[ticker]["price"])
                except Exception:
                    pass
            if current_price is None:
                # Fallback 2: cache OHLCV (5 min stale al massimo)
                try:
                    loop = asyncio.get_running_loop()
                    price_data = await loop.run_in_executor(
                        None, data_fetchers.fetch_market_data, ticker, 5
                    )
                    if price_data and price_data.get("data"):
                        current_price = float(price_data["data"][-1]["close"])
                except Exception as exc:
                    logger.warning("[%s][DEC-CRYPTO] price fetch failed for %s: %s",
                                   run_id, ticker, exc)

            if not current_price or current_price <= 0:
                return json.dumps({"error": f"Prezzo non disponibile per {ticker}"})

            # ── Risk Profile validation (HARD CONSTRAINTS, asset_class=crypto) ─
            if action == "BUY":
                try:
                    import risk_profile as _rp
                    pstate = portfolio.get_portfolio_state()
                    cash = float(pstate.get("cash", 0) or 0)
                    # FIX: per il cap crypto-specifico (max_open_positions_crypto)
                    # serve contare SOLO le posizioni crypto in essere, non il
                    # totale generale (che include equity).
                    positions_all = pstate.get("positions") or []
                    crypto_count = 0
                    for _pos in positions_all:
                        _t = (_pos.get("ticker") or "").upper()
                        if _t.endswith("-USD") or _t.startswith("X:"):
                            crypto_count += 1
                    open_count = crypto_count
                    trade_value = float(quantity) * float(current_price)
                    alloc_pct = (trade_value / cash * 100.0) if cash > 0 else 999.0
                    conf_norm = float(confidence) / 100.0 if confidence and confidence > 1 else float(confidence or 0)

                    dd_pct = None
                    try:
                        pnl_pct = float(pstate.get("pnl_pct", 0) or 0)
                        if pnl_pct < 0:
                            dd_pct = abs(pnl_pct)
                    except Exception:
                        pass

                    ok, reason = _rp.validate_trade(
                        asset_class="crypto",
                        confidence=conf_norm,
                        allocation_pct=alloc_pct,
                        open_positions_count=open_count,
                        portfolio_drawdown_pct=dd_pct,
                    )
                    if not ok:
                        logger.warning("[%s][DEC-CRYPTO] RISK_PROFILE rejected: %s",
                                       run_id, reason)
                        database.insert_agent_log(run_id, "DECISION_CRYPTO_RISK_REJECTED",
                            json.dumps({
                                "ticker": ticker, "action": action,
                                "qty": quantity, "price": current_price,
                                "alloc_pct": round(alloc_pct, 2),
                                "confidence": conf_norm,
                                "open_positions": open_count,
                                "drawdown_pct": dd_pct,
                                "reason": reason,
                            }, default=str))
                        return json.dumps({
                            "executed": False, "rejected": True,
                            "ticker": ticker, "action": action,
                            "reason": f"RISK_PROFILE: {reason}",
                            "at": timestamp,
                        })
                except ImportError:
                    pass
                except Exception as rp_err:
                    logger.warning("[%s][DEC-CRYPTO] risk validation error (non-fatal): %s",
                                   run_id, rp_err)

            if action == "BUY":
                result = portfolio.execute_buy(
                    ticker, quantity, current_price,
                    geo_reasoning="(crypto-only run)", tech_reasoning=logic_chain,
                    confidence=confidence,
                )
            else:
                result = portfolio.execute_sell(
                    ticker, quantity, current_price,
                    geo_reasoning="(crypto-only run)", tech_reasoning=logic_chain,
                    confidence=confidence,
                )

            # FIX CRITICO: skip mirror se l'execute è fallito (no-position,
            # cash insufficiente, qty<=0). Stesso bug del Decision standard.
            if not (isinstance(result, dict) and result.get("success")):
                fail_reason = (result or {}).get("reason", "unknown") if isinstance(result, dict) else str(result)
                logger.warning("[%s][DEC-CRYPTO] execute_%s fallito su %s qty=%s: %s — skip mirror",
                               run_id, action.lower(), ticker, quantity, fail_reason)
                database.insert_agent_log(run_id, "DECISION_CRYPTO_TRADE_FAILED", json.dumps({
                    "ticker": ticker, "action": action, "qty": quantity,
                    "price": current_price, "reason": fail_reason[:300],
                }, default=str))
                return json.dumps({
                    "executed": False, "ticker": ticker, "action": action,
                    "reason": fail_reason, "at": timestamp,
                }, default=str)

            # Salva SL/TP automatici sulla posizione se BUY con livelli
            if action == "BUY" and (stop_loss or take_profit):
                try:
                    if stop_loss and stop_loss > 0:
                        portfolio.set_stop_loss(ticker, float(stop_loss), run_id=run_id)
                    if take_profit and take_profit > 0:
                        portfolio.set_take_profit(ticker, float(take_profit), run_id=run_id)
                except Exception as exc:
                    logger.warning("[%s][DEC-CRYPTO] auto SL/TP set fallito %s: %s",
                                   run_id, ticker, exc)

            database.insert_agent_log(run_id, "DECISION_CRYPTO_TRADE", json.dumps({
                "ticker": ticker, "action": action, "qty": quantity,
                "price": current_price, "confidence": confidence,
                "stop_loss": stop_loss, "take_profit": take_profit,
            }, default=str))

            return json.dumps({
                "executed": True, "ticker": ticker, "action": action,
                "quantity": quantity, "price": current_price,
                "result": result, "at": timestamp,
            }, default=str)

        elif tool_name == "do_nothing":
            reasoning = tool_input["reasoning"]
            database.insert_agent_log(run_id, "DECISION_CRYPTO_NO_TRADE",
                json.dumps({"reasoning": reasoning[:1000]}))
            return json.dumps({"action": "no_trade", "reasoning": reasoning})

        elif tool_name == "request_crypto_technical_analysis":
            tickers = tool_input.get("tickers") or []
            focus = tool_input.get("focus_question", "")
            if not isinstance(tickers, list) or not tickers:
                return json.dumps({"error": "tickers deve essere una lista non vuota"})
            tickers = [str(t).upper().strip() for t in tickers if t][:5]

            logger.info("[%s][DEC-CRYPTO] request_crypto_technical_analysis: %s — focus: %s",
                        run_id, tickers, focus[:80])
            try:
                from agents.technical_crypto import run_crypto_technical
                report = await run_crypto_technical(run_id, tickers)
            except Exception as exc:
                logger.error("[%s][DEC-CRYPTO] crypto technical realtime failed: %s",
                             run_id, exc, exc_info=True)
                return json.dumps({
                    "error": f"Technical Crypto fallito: {exc}",
                    "tickers_requested": tickers,
                })

            analyses = report.get("analyses") or []
            analyzed = {a.get("ticker") for a in analyses if a.get("ticker")}
            errors_per_ticker = {}
            for t in tickers:
                if t not in analyzed:
                    raw_list = report.get("raw_indicators") or []
                    if isinstance(raw_list, list):
                        for r in raw_list:
                            if isinstance(r, dict) and r.get("ticker") == t and r.get("error"):
                                errors_per_ticker[t] = r.get("error", "no data")
                                break
                    if t not in errors_per_ticker:
                        errors_per_ticker[t] = "non analizzato (out of universe / no data)"

            database.insert_agent_log(run_id, "DECISION_CRYPTO_REALTIME_TA", json.dumps({
                "tickers_requested": tickers,
                "tickers_analyzed": list(analyzed),
                "errors_per_ticker": errors_per_ticker,
                "engine": report.get("engine"),
                "focus": focus[:200],
            }, default=str))

            return json.dumps({
                "focus_question": focus,
                "analyses": analyses,
                "summary": report.get("summary", ""),
                "engine": report.get("engine", ""),
                "errors_per_ticker": errors_per_ticker,
            }, default=str)

        elif tool_name == "set_stop_loss":
            ticker = (tool_input.get("ticker") or "").upper().strip()
            stop_price = float(tool_input.get("stop_price") or 0)
            reason = tool_input.get("reason", "")
            result = portfolio.set_stop_loss(ticker, stop_price, run_id=run_id)
            database.insert_agent_log(run_id, "DECISION_CRYPTO_SET_SL", json.dumps({
                "ticker": ticker, "stop_price": stop_price,
                "success": result.get("success"),
                "ai_motivation": reason[:300],
                "failure_reason": (result.get("reason", "")[:400]
                                   if not result.get("success") else None),
            }))
            return json.dumps(result, default=str)

        elif tool_name == "set_take_profit":
            ticker = (tool_input.get("ticker") or "").upper().strip()
            target_price = float(tool_input.get("target_price") or 0)
            reason = tool_input.get("reason", "")
            result = portfolio.set_take_profit(ticker, target_price, run_id=run_id)
            database.insert_agent_log(run_id, "DECISION_CRYPTO_SET_TP", json.dumps({
                "ticker": ticker, "target_price": target_price,
                "success": result.get("success"),
                "ai_motivation": reason[:300],
                "failure_reason": (result.get("reason", "")[:400]
                                   if not result.get("success") else None),
            }))
            return json.dumps(result, default=str)

        elif tool_name == "get_portfolio_state":
            state = portfolio.get_portfolio_state()
            # FIX: il Decision Crypto opera SOLO su crypto. Esponendo le
            # equity nel state["positions"], il modello R1 si confondeva e
            # poteva proporre SELL su NVDA o GLD (poi rifiutati dal pre-validator
            # ma con costo di reasoning). Ora rimpiazziamo positions con il
            # filtro crypto-only e ricomputiamo open_positions_count coerente.
            all_positions = state.get("positions", [])
            crypto_positions = [
                p for p in all_positions
                if p.get("ticker", "").upper().endswith("-USD")
                or p.get("ticker", "").upper().startswith("X:")
            ]
            state["positions"] = crypto_positions
            state["positions_crypto"] = crypto_positions   # alias backward-compat
            state["open_positions_count"] = len(crypto_positions)
            # equity_positions_count e' info diagnostica (non per il modello)
            state["equity_positions_count_info"] = (
                len(all_positions) - len(crypto_positions)
            )
            return json.dumps({"portfolio": state, "at": timestamp}, default=str)

        elif tool_name == "set_commitment":
            try:
                commit_id = database.add_agent_commitment(
                    agent_type="crypto",
                    commitment_type=tool_input.get("commitment_type", "reminder"),
                    condition_text=tool_input.get("condition_text", ""),
                    trigger_action=tool_input.get("trigger_action"),
                    expires_in_hours=float(tool_input.get("expires_in_hours") or 48),
                    ticker=tool_input.get("ticker"),
                    source_run_id=run_id,
                )
                database.insert_agent_log(run_id, "DECISION_CRYPTO_SET_COMMITMENT", json.dumps({
                    "commit_id": commit_id,
                    "type": tool_input.get("commitment_type"),
                    "condition": (tool_input.get("condition_text") or "")[:500],
                    "expires_in_hours": tool_input.get("expires_in_hours"),
                    "ticker": tool_input.get("ticker"),
                }, default=str))
                if commit_id:
                    return json.dumps({
                        "success": True,
                        "commitment_id": commit_id,
                        "message": "Impegno crypto registrato. Apparira' come OBIETTIVO ATTIVO nei run successivi.",
                    })
                return json.dumps({"success": False, "error": "DB insert ritornato null"})
            except Exception as e:
                logger.warning("[%s] crypto set_commitment error: %s", run_id, e)
                return json.dumps({"success": False, "error": str(e)[:300]})

        elif tool_name == "resolve_commitment":
            try:
                cid = int(tool_input.get("commitment_id") or 0)
                status = tool_input.get("resolution_status") or "triggered"
                reason = tool_input.get("resolution_reason") or ""
                ok = database.resolve_agent_commitment(cid, status=status, resolved_reason=reason)
                database.insert_agent_log(run_id, "DECISION_CRYPTO_RESOLVE_COMMITMENT", json.dumps({
                    "commit_id": cid, "status": status, "reason": reason[:300], "ok": ok,
                }, default=str))
                return json.dumps({"success": bool(ok), "commitment_id": cid, "status": status})
            except Exception as e:
                logger.warning("[%s] crypto resolve_commitment error: %s", run_id, e)
                return json.dumps({"success": False, "error": str(e)[:300]})

        elif tool_name == "request_capital_from_orchestrator":
            # Decision Crypto chiama il Capital Orchestrator (DeepSeek-V3) per
            # liberare cash bloccato in posizioni equity gestite dal Decision
            # Standard. L'orchestratore decide se vale la pena disturbare l'altro
            # agente e, se sì, esegue le liquidazioni cross-agent direttamente.
            try:
                from agents.capital_orchestrator import request_capital
                ticker = str(tool_input.get("ticker", "")).upper().strip()
                amount_needed = float(tool_input.get("amount_needed_usd") or 0)
                amount_available = float(tool_input.get("amount_available_usd") or 0)
                conviction = float(tool_input.get("conviction") or 0)
                reasoning = str(tool_input.get("reasoning") or "")[:500]

                if not ticker or amount_needed <= 0:
                    return json.dumps({
                        "approved": False,
                        "error": "ticker o amount_needed_usd non validi",
                    })

                result = await request_capital(
                    run_id=run_id,
                    requesting_agent="crypto",
                    ticker=ticker,
                    amount_needed=amount_needed,
                    amount_available=amount_available,
                    conviction=conviction,
                    reasoning=reasoning,
                )
                database.insert_agent_log(
                    run_id, "DECISION_CRYPTO_ORCHESTRATOR_REQUEST",
                    json.dumps({
                        "ticker": ticker,
                        "amount_needed": amount_needed,
                        "approved": result.get("approved"),
                        "amount_freed": result.get("amount_freed", 0),
                        "skipped": result.get("skipped"),
                    }, default=str)
                )
                return json.dumps({
                    "approved": result.get("approved", False),
                    "amount_freed_usd": result.get("amount_freed", 0),
                    "actions_executed_count": len(result.get("actions_executed") or []),
                    "actions_executed": result.get("actions_executed") or [],
                    "reasoning": result.get("reasoning", ""),
                    "skipped": result.get("skipped"),
                    "next_step": (
                        "Cash liberato. Riprova execute_trade ora."
                        if result.get("approved") else
                        "Riallocazione negata. Usa do_nothing o riduci la size."
                    ),
                }, default=str)
            except Exception as e:
                logger.warning("[%s] crypto orchestrator request error: %s", run_id, e)
                return json.dumps({
                    "approved": False, "error": str(e)[:300],
                    "next_step": "Errore orchestratore: usa do_nothing o execute_trade scalato.",
                })

        else:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})

    except Exception as e:
        logger.error("[%s][DECISION-CRYPTO] Tool error %s: %s", run_id, tool_name, e, exc_info=True)
        return json.dumps({"error": str(e), "tool": tool_name})


# ─── Context builder ────────────────────────────────────────────────────────

def _load_crypto_documents(database) -> list[dict]:
    """
    Carica solo i documenti con category='crypto'.
    Se la colonna `category` non esiste ancora (migrazione non applicata),
    ritorna lista vuota (degrade graceful).
    """
    try:
        return database.get_document_contents(category="crypto") or []
    except Exception:
        return []


def _build_context(tech_report: dict, recent_buffer: list, portfolio_state: dict,
                   crypto_docs: list,
                   focus_tickers: list[str] | None = None,
                   watchdog_reason: str | None = None) -> str:
    parts = []
    parts.append(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")

    # === WATCHDOG TRIGGER (in cima, alta priorita') — incluso REBALANCE ===
    if focus_tickers or watchdog_reason:
        is_rebalance = bool(watchdog_reason and watchdog_reason.upper().startswith("REBALANCE"))
        wd_lines = ["=" * 60]
        if is_rebalance:
            wd_lines.append("⚠️  WATCHDOG REBALANCE TRIGGER — RIDUZIONE RISCHIO OBBLIGATORIA ⚠️")
        else:
            wd_lines.append("⚠️  WATCHDOG TRIGGER — INPUT PRIORITARIO ⚠️")
        wd_lines.append("=" * 60)
        if watchdog_reason:
            wd_lines.append(f"Motivo: {watchdog_reason}")
        if focus_tickers:
            label = ("POSIZIONE OVERWEIGHT da ridurre"
                     if is_rebalance else "FOCUS TICKERS")
            wd_lines.append(f"{label}: {', '.join(focus_tickers)}")
        wd_lines.append("")
        if is_rebalance:
            wd_lines.append("ISTRUZIONI OBBLIGATORIE — REBALANCE crypto:")
            wd_lines.append(
                "  1. La posizione mostrata sopra ha superato il 35% del NAV. "
                "    Concentrazione eccessiva = rischio non controllato."
            )
            wd_lines.append(
                "  2. ESEGUI un SELL PARZIALE per riportare la posizione "
                "    sotto il 25% del NAV (le crypto sono piu' volatili: "
                "    riduci a target piu' basso vs equity)."
            )
            wd_lines.append(
                "  3. NON aggiungere a questa posizione (no BUY) finche' "
                "    rimane sopra-soglia."
            )
            wd_lines.append(
                "  4. Workflow standard 4 fasi obbligatorio: "
                "    commit_initial_assessment → request_crypto_technical_analysis "
                "    → commit_final_thesis → execute_trade SELL."
            )
        wd_lines.append("=" * 60)
        parts.append("\n".join(wd_lines))

    # === OBIETTIVI ATTIVI (impegni dei run precedenti) ===
    try:
        import database as _db
        if hasattr(_db, "get_active_agent_commitments"):
            commitments = _db.get_active_agent_commitments("crypto", limit=15)
            if commitments:
                from datetime import datetime as _dt2, timezone as _tz2
                parts.append("=" * 60)
                parts.append("OBIETTIVI ATTIVI (impegni presi in run crypto precedenti)")
                parts.append("=" * 60)
                parts.append(
                    "Sono promesse/intenzioni che TU stesso hai registrato in run "
                    "passati. Per ognuno valuta: condizione soddisfatta? -> esegui + "
                    "resolve_commitment(triggered). Decaduta? -> resolve_commitment(cancelled). "
                    "Ancora rilevante ma non ancora? -> lascialo attivo."
                )
                now_utc = _dt2.now(_tz2.utc)
                for c in commitments:
                    cid = c.get("id")
                    ctype = c.get("commitment_type", "?")
                    cond = (c.get("condition_text") or "")[:300]
                    trig = (c.get("trigger_action") or "")[:200]
                    tk = c.get("ticker") or ""
                    exp = c.get("expires_at")
                    exp_str = ""
                    if exp:
                        try:
                            exp_dt = _dt2.fromisoformat(str(exp).replace("Z", "+00:00"))
                            hrs_left = (exp_dt - now_utc).total_seconds() / 3600.0
                            if hrs_left > 0:
                                exp_str = f" [scade in {hrs_left:.0f}h]"
                            else:
                                exp_str = " [scaduto]"
                        except Exception:
                            pass
                    line = f"  • [ID:{cid}] {ctype.upper()}"
                    if tk:
                        line += f" {tk}"
                    line += f"{exp_str}\n     condizione: {cond}"
                    if trig:
                        line += f"\n     trigger: {trig}"
                    parts.append(line)
                parts.append("")
    except Exception as _e:
        logger.debug("decision_crypto: commitments non caricati: %s", _e)

    # === DIRETTIVE UTENTE RECENTI (chat decision crypto) ===
    try:
        import database as _db
        if hasattr(_db, "get_recent_user_directives"):
            directives = _db.get_recent_user_directives("crypto", hours=48, limit=5)
            if directives:
                parts.append("=" * 60)
                parts.append("📋  DIRETTIVE UTENTE RECENTI (da chat crypto, ultime 48h)")
                parts.append("=" * 60)
                parts.append(
                    "L'utente ha espresso queste preferenze nella chat decision crypto. "
                    "Trattale come SOFT GUARDRAILS: rispettale salvo segnali tecnici "
                    "fortemente contrari (in quel caso cita la direttiva e spiega "
                    "perche' la stai contraddicendo)."
                )
                for d in directives:
                    content = (d.get("content") or "").strip()
                    when = str(d.get("created_at", ""))[:16]
                    if content:
                        parts.append(f"  • [{when}] {content[:300]}")
    except Exception as _e:
        logger.debug("decision_crypto: direttive utente non caricate: %s", _e)

    parts.append("=" * 60)
    # Workflow a 4 fasi: il tech_report NON e' iniettato a priori. Il
    # Decision Crypto Agent deve PRIMA fare commit_initial_assessment, poi
    # request_crypto_technical_analysis (FASE 2), poi commit_final_thesis.
    parts.append(
        "REPORT TECNICO CRYPTO: NON fornito a priori. Devi richiederlo TU "
        "tramite request_crypto_technical_analysis durante FASE 2 del workflow "
        "obbligatorio (vedi WORKFLOW_PHASES nel system prompt)."
    )
    parts.append("=" * 60)
    parts.append(f"PORTAFOGLIO CORRENTE:")
    parts.append(json.dumps(portfolio_state, default=str, ensure_ascii=False)[:3000])
    parts.append("=" * 60)
    parts.append(f"INTELLIGENCE BUFFER (ultimi 60 min, {len(recent_buffer)} card):")
    for c in recent_buffer[:20]:
        s = c.get("micro_summary", "")[:200]
        st = c.get("source_type", "?")
        parts.append(f"  [{st}] {s}")
    parts.append("=" * 60)
    if crypto_docs:
        parts.append(f"DOCUMENTI CRYPTO ({len(crypto_docs)} caricati):")
        for d in crypto_docs[:5]:
            name = d.get("filename") or d.get("name", "?")
            content = (d.get("content") or "")[:1500]
            parts.append(f"--- {name} ---\n{content}")
    else:
        parts.append("DOCUMENTI CRYPTO: nessuno caricato (vai in Impostazioni → Documenti Crypto)")
    return "\n".join(parts)


# ─── Tool loop ──────────────────────────────────────────────────────────────

async def _run_r1_loop(run_id: str, system_prompt: str, user_message: str
                        ) -> tuple[list, str, int, dict, dict]:
    api_key = _get_deepseek_key()
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY non configurata")

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    trades_executed: list[dict] = []
    iteration = 0
    max_iterations = 8
    final_text = ""

    # ─── State machine workflow a 4 fasi ────────────────────────────────────
    workflow_state = WorkflowState()

    async with aiohttp.ClientSession() as session:
        while iteration < max_iterations:
            payload = {
                "model": DEEPSEEK_R1_MODEL,
                "messages": messages,
                "tools": CRYPTO_DECISION_TOOLS,
                "tool_choice": "auto",
                "max_tokens": 6000,
            }
            # Timeout 180s + 1 retry: DeepSeek-R1 in fase final_thesis puo'
            # impiegare 60-90s di reasoning. 120s lasciava pochissimo buffer
            # per spike di latenza. Il retry e' fondamentale per non abortire
            # la pipeline crypto su un singolo timeout transient.
            data = None
            last_err = None
            for _retry_idx in range(2):
                try:
                    async with session.post(
                        DEEPSEEK_API_URL, json=payload, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=180),
                    ) as resp:
                        if resp.status != 200:
                            body = await resp.text()
                            # 429/5xx = transient → retry una volta
                            if resp.status == 429 or 500 <= resp.status < 600:
                                last_err = f"HTTP {resp.status}: {body[:150]}"
                                if _retry_idx == 0:
                                    logger.warning("[%s][DEC-CRYPTO] R1 transient %s, retry",
                                                    run_id, last_err)
                                    await asyncio.sleep(3)
                                    continue
                            raise ValueError(
                                f"DeepSeek-R1 HTTP {resp.status}: {body[:300]}"
                            )
                        data = await resp.json()
                        break
                except asyncio.TimeoutError:
                    last_err = "timeout 180s"
                    if _retry_idx == 0:
                        logger.warning("[%s][DEC-CRYPTO] R1 timeout, retry once",
                                        run_id)
                        await asyncio.sleep(2)
                        continue
                    raise ValueError(f"DeepSeek-R1 {last_err} (post retry)")
            if data is None:
                raise ValueError(f"DeepSeek-R1 failed: {last_err}")

            choice = data["choices"][0]
            finish_reason = choice.get("finish_reason", "")
            msg = choice["message"]

            import re as _re
            raw_text = msg.get("content") or ""
            final_text = _re.sub(r"<think>.*?</think>", "", raw_text,
                                 flags=_re.DOTALL).strip()

            if finish_reason != "tool_calls" or not msg.get("tool_calls"):
                break

            iteration += 1
            logger.info("[%s][DEC-CRYPTO] Iter %d phase=%s tech_calls=%d tools=%d",
                        run_id, iteration, workflow_state.phase,
                        workflow_state.tech_request_count, len(msg["tool_calls"]))

            messages.append({
                "role": "assistant",
                "content": msg.get("content"),
                "tool_calls": msg["tool_calls"],
            })

            for tc in msg["tool_calls"]:
                tool_name = tc["function"]["name"]
                try:
                    tool_input = json.loads(tc["function"]["arguments"])
                except Exception:
                    tool_input = {}

                # Validazione workflow phase
                allowed, err = can_call_tool(workflow_state, tool_name)
                if not allowed:
                    workflow_state.rejected_calls.append({
                        "tool": tool_name, "reason": err[:200],
                    })
                    messages.append({
                        "role": "tool", "tool_call_id": tc["id"],
                        "content": make_rejection_result(err, workflow_state),
                    })
                    continue

                # Validazione contenuto (lunghezza minima testi commit + logic_chain)
                content_ok, content_err = validate_commit_input(tool_name, tool_input)
                if not content_ok:
                    messages.append({
                        "role": "tool", "tool_call_id": tc["id"],
                        "content": make_rejection_result(content_err, workflow_state),
                    })
                    continue

                # Esegui tool — workflow_state per arricchimento logic_chain
                result = await _handle_tool(
                    tool_name, tool_input, run_id, workflow_state=workflow_state,
                )

                # Aggiorna state machine
                workflow_state = apply_tool_transition(
                    workflow_state, tool_name, tool_input, result
                )

                if tool_name == "execute_trade":
                    try:
                        parsed = json.loads(result)
                        if parsed.get("executed"):
                            trades_executed.append({
                                "ticker": tool_input.get("ticker"),
                                "action": tool_input.get("action"),
                                "quantity": tool_input.get("quantity"),
                                "confidence": tool_input.get("confidence_level"),
                            })
                    except Exception:
                        pass

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

    # Log finale workflow state. Il reasoning ricco (thesis/action_plan/
    # primary_risk + final_text) viene incluso direttamente in
    # DECISION_CRYPTO_COMPLETE dal chiamante, evitando la doppia card
    # "Decision — Ragionamento" + "Decision Crypto" che il frontend
    # produceva quando entrambi i log esistevano.
    try:
        import database
        database.insert_agent_log(run_id, "DECISION_CRYPTO_WORKFLOW", json.dumps(
            workflow_state.to_dict(), default=str))
    except Exception:
        pass

    ia = workflow_state.initial_assessment or {}
    ft = workflow_state.final_thesis or {}
    return trades_executed, final_text, iteration, ia, ft


# ─── Main entry ─────────────────────────────────────────────────────────────

async def run_crypto_decision(run_id: str, tech_report: dict | None,
                               focus_tickers: list[str] | None = None,
                               watchdog_reason: str | None = None) -> dict:
    """Esegue il Decision Crypto Agent con DeepSeek-R1 reasoning.

    NOTA: tech_report e' Optional. Nel workflow a 4 fasi (default ora),
    l'orchestrator passa None: il Decision Crypto deve richiedere il
    tech_report via tool durante la FASE 2. Per back-compat con chiamate
    legacy che lo passano come dict, accettiamo entrambi i casi.

    Args:
        focus_tickers: ticker prioritari indicati dal Watchdog (es. rebalance)
        watchdog_reason: motivo del trigger (se inizia con "REBALANCE:" il
                         Decision riceve istruzioni di SELL parziale).
    """
    import database
    from agents.scout import get_recent_buffer
    import portfolio

    logger.info("[%s][DECISION-CRYPTO] === Avvio ===", run_id)
    start_time = datetime.now(timezone.utc)

    # Normalizza tech_report (None → {} per evitare AttributeError)
    tech_report = tech_report or {}

    # Carica contesto specifico crypto
    recent_buffer = get_recent_buffer(database, minutes=60)
    portfolio_state = portfolio.get_portfolio_state()
    crypto_docs = _load_crypto_documents(database)

    user_message = _build_context(
        tech_report, recent_buffer, portfolio_state, crypto_docs,
        focus_tickers=focus_tickers, watchdog_reason=watchdog_reason,
    )

    database.insert_agent_log(run_id, "DECISION_CRYPTO_CONTEXT", json.dumps({
        "tech_engine": tech_report.get("engine", "deferred_to_decision"),
        "buffer_size": len(recent_buffer),
        "crypto_docs": len(crypto_docs),
        "portfolio_total": portfolio_state.get("total_value", 0),
    }, default=str))

    sys_prompt_crypto, coach_card_ids_crypto = _get_crypto_decision_prompt_with_meta()
    try:
        trades, final_text, iterations, ia, ft = await _run_r1_loop(
            run_id, sys_prompt_crypto, user_message,
        )
        used_model = DEEPSEEK_R1_MODEL
        _record_run_timestamp()
        # Increment Coach Cards applied_count: track ROI delle card
        # (piu' applied = card piu' usata dal Decision)
        try:
            from agents import coach_cards as _cc
            for _cid in coach_card_ids_crypto:
                _cc.increment_applied(_cid)
        except Exception:
            pass
    except Exception as exc:
        logger.error("[%s][DECISION-CRYPTO] R1 fallito: %s", run_id, exc, exc_info=True)
        try:
            import traceback as _tb
            database.insert_agent_log(run_id, "DECISION_CRYPTO_ERROR", json.dumps({
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:500],
                "traceback": _tb.format_exc()[:2000],
            }, default=str))
        except Exception:
            pass
        return {"decision": "ERROR", "trades": [], "error": str(exc),
                "model": "deepseek-r1-error"}

    duration = (datetime.now(timezone.utc) - start_time).total_seconds()

    # DECISION_CRYPTO_COMPLETE include il reasoning ricco completo (thesis,
    # action_plan, primary_risk + final_text). E' l'UNICO log "ricco" del
    # crypto run: il vecchio DECISION_REASONING e' stato rimosso per evitare
    # doppia card nella UI ("Decision — Ragionamento" + "Decision Crypto"
    # mostravano lo stesso contenuto sovrapposto, incluso il riepilogo trade
    # markdown ripetuto).
    database.insert_agent_log(run_id, "DECISION_CRYPTO_COMPLETE", json.dumps({
        "model": used_model,
        "iterations": iterations,
        "trades_executed": len(trades),
        "duration_seconds": round(duration, 1),
        "final_text": final_text[:8000],
        # reasoning_text e' fallback se la card UI lo richiede (es. quando
        # ft e' vuoto perche' R1 ha terminato senza completare la final_thesis).
        "reasoning_text": final_text[:8000] if final_text else "",
        # Reasoning strutturato (estratto da workflow_state.final_thesis):
        "thesis": (ft.get("thesis") or "")[:3500],
        "action_plan": (ft.get("action_plan") or "")[:2000],
        "primary_risk": (ft.get("primary_risk") or "")[:2000],
        # Initial assessment (utile per debugging / coach cards):
        "situation_overview": (ia.get("situation_overview") or "")[:2500],
        "asset_candidates": ia.get("asset_candidates") or [],
        "technical_questions": ia.get("technical_questions") or [],
    }, default=str))

    return {
        "run_id": run_id,
        "decision": "TRADE" if trades else "NO_TRADE",
        "trades": trades,
        "no_trade_reasoning": final_text if not trades else "",
        "duration_seconds": duration,
        "model": used_model,
        "iterations": iterations,
    }
