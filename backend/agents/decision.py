"""
Decision Agent — Claude Sonnet 4.5 (market open) | DeepSeek-R1 (market closed)
Orchestra gerarchica per decisioni di trading ad alto rischio.
Attivato solo quando il Watchdog rileva un segnale significativo (urgency >= 5).
Throttle: max 1 run per ora durante orari mercato, 1 ogni 2h30 fuori orario.

Engine selection (env DECISION_ENGINE):
  - claude       → SEMPRE Claude Sonnet 4.5 (override esplicito)
  - deepseek-r1  → SEMPRE DeepSeek-R1 (override esplicito; usato da GR1 tournament)
  - hybrid       → Sonnet 4.5 quando market OPEN, R1 quando CLOSED  (default GEO)

In modalità hybrid, R1 subentra automaticamente fuori orario per gestire le
crypto 24/7 con costo ridotto (~10× più economico di Sonnet 4.5).

Fasi:
  A: Ingestione contesto a cascata (4D mid-term + 8H short-term +
     buffer L0 ultimi 40 min + Tech Report + Portfolio)
  B: Valutazione strategica
  C: Esecuzione trade o motivazione no-trade
"""

import asyncio
import json
import logging
import os
import re as _re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import aiohttp
from anthropic import Anthropic

logger = logging.getLogger(__name__)

# ── Claude Sonnet 4.5 (production) ──────────────────────────────────────────
# NOTA: verifica che questo ID sia corretto sulla tua dashboard Anthropic
DECISION_MODEL = "claude-sonnet-4-5-20250929"
DECISION_MODEL_FALLBACK = "claude-sonnet-4-20250514"  # Fallback a Sonnet 4 se 4.5 non disponibile

# ── DeepSeek-R1 (tournament, cost-optimized) ────────────────────────────────
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_R1_MODEL = "deepseek-reasoner"   # DeepSeek-R1 (reasoning model)


def _get_decision_engine() -> str:
    """Legge DECISION_ENGINE da env var. Default 'claude' (Sonnet 4.5).

    Valori validi:
      - 'claude'      → Sonnet 4.5 sempre (default Live)
      - 'deepseek-r1' → R1 sempre (GR1 tournament bot via env override)
    L'hybrid mode è stato RIMOSSO (creava confusione con la sidebar e con
    Decision Crypto). Le crypto sono dominio esclusivo del Decision Crypto.
    """
    return os.environ.get("DECISION_ENGINE", "claude").lower()


def _resolve_engine_for_run() -> str:
    """Ritorna l'engine configurato (no più switching per stato mercato)."""
    return _get_decision_engine()


# Setting keys per i timestamp degli ultimi run del Decision Agent.
# Vengono usati per:
#  - cooldown 2h30 R1 overnight (R1_LAST_RUN_KEY)
#  - mostrare "ultimo run" separato Decision (Sonnet) e Decision 24h (R1)
#    nella sidebar del frontend
R1_LAST_RUN_KEY = "last_decision_r1_run_at"
SONNET_LAST_RUN_KEY = "last_decision_sonnet_run_at"
R1_COOLDOWN_SECONDS = 9000   # 2h30


def record_r1_run_timestamp() -> None:
    """Aggiorna il timestamp dell'ultimo run R1 nel DB. Usato per cooldown 2h30."""
    try:
        import database as _db
        ts = datetime.now(timezone.utc).isoformat()
        _db.set_setting(R1_LAST_RUN_KEY, ts)
    except Exception as exc:
        logger.warning("Impossibile salvare timestamp R1 last-run: %s", exc)


def record_sonnet_run_timestamp() -> None:
    """Aggiorna il timestamp dell'ultimo run Sonnet 4.5 (Decision orari mercato)."""
    try:
        import database as _db
        ts = datetime.now(timezone.utc).isoformat()
        _db.set_setting(SONNET_LAST_RUN_KEY, ts)
    except Exception as exc:
        logger.warning("Impossibile salvare timestamp Sonnet last-run: %s", exc)


def is_r1_cooldown_active() -> tuple[bool, int]:
    """
    Ritorna (True, seconds_left) se il cooldown 2h30 è ancora attivo.
    Altrimenti (False, 0). Usato dall'orchestrator per skip overnight.
    """
    try:
        import database as _db
        last_iso = _db.get_setting(R1_LAST_RUN_KEY, "") or ""
        if not last_iso:
            return False, 0
        last_dt = datetime.fromisoformat(last_iso)
        # Compatibilità: se non ha tzinfo, assumi UTC
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
        if elapsed < R1_COOLDOWN_SECONDS:
            return True, int(R1_COOLDOWN_SECONDS - elapsed)
    except Exception as exc:
        logger.debug("Errore lettura cooldown R1 (ignoro, fail-open): %s", exc)
    return False, 0


def _get_deepseek_key() -> str:
    """Recupera DEEPSEEK_API_KEY da env o DB settings."""
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        try:
            import database as _db
            key = _db.get_setting("deepseek_api_key", "") or ""
        except Exception:
            pass
    return key


def _tools_for_openai():
    """Converte DECISION_TOOLS da Anthropic format a OpenAI/DeepSeek format.
    Anthropic usa 'input_schema', OpenAI/DeepSeek usa 'parameters' — stessa struttura JSON."""
    return [
        {"type": "function", "function": {
            "name": t["name"],
            "description": t["description"],
            "parameters": t["input_schema"],
        }}
        for t in DECISION_TOOLS
    ]


def _build_reasoning_text(ia: dict, ft: dict, final_text: str) -> str:
    """
    Compone un testo narrativo del ragionamento dal workflow state per la dashboard.

    Bug precedente: il backend salvava 'situation_overview', 'thesis',
    'final_text' nel log DECISION_REASONING, ma il frontend leggeva
    'reasoning_text' (mai scritto) → Card "Ragionamento" sempre vuota.

    Soluzione: sintetizza un testo unico leggibile dai campi strutturati.
    """
    sections = []
    sit = (ia.get("situation_overview") or "").strip()
    if sit:
        sections.append(f"📍 SITUAZIONE\n{sit}")
    rot = (ia.get("rotation_summary") or "").strip()
    if rot:
        sections.append(f"🔄 ROTATION ANALYSIS\n{rot}")
    candidates = ia.get("asset_candidates") or []
    if candidates:
        if isinstance(candidates, list):
            cand_str = ", ".join(str(c) for c in candidates[:6])
        else:
            cand_str = str(candidates)[:300]
        sections.append(f"🎯 ASSET VALUTATI: {cand_str}")
    thesis = (ft.get("thesis") or "").strip()
    if thesis:
        sections.append(f"💡 TESI\n{thesis}")
    action_plan = (ft.get("action_plan") or "").strip()
    if action_plan:
        sections.append(f"📋 PIANO D'AZIONE\n{action_plan}")
    primary_risk = (ft.get("primary_risk") or "").strip()
    if primary_risk:
        sections.append(f"⚠️ RISCHIO PRINCIPALE\n{primary_risk}")
    final = (final_text or "").strip()
    if final:
        # Cap aumentato 500 → 4000: la conclusione del Decision Agent
        # contiene rotation analysis, trade rationale, risk mitigation —
        # truncare a 500 nascondeva l'intero ragionamento finale nella
        # ReasoningCard del frontend.
        sections.append(f"✅ CONCLUSIONE\n{final[:4000]}")

    if not sections:
        # Fallback: nessuna fase del workflow è stata committed
        return final or "(workflow non completato — nessun ragionamento registrato)"
    return "\n\n".join(sections)

# ============================================================
# System Prompts
# ============================================================

DECISION_SYSTEM_PROMPT_DEFAULT = """Sei il Decision Agent di GeoInvest AI — un sistema di trading autonomo ad alto rischio.

HAI PIENA AUTONOMIA DECISIONALE. Non ci sono restrizioni conservative.
Il tuo obiettivo è MASSIMIZZARE i rendimenti accettando rischi calcolati.

═══════════════════════════════════════════════════════════════════════
FILOSOFIA OPERATIVA — ASSERTIVITÀ MASSIMA, NESSUN VINCOLO DI DIVERSIFICAZIONE
═══════════════════════════════════════════════════════════════════════

1. **OSA. RISCHIA DI PIÙ.**
   - Il "no-trade conservativo" è la peggior decisione possibile.
   - **In dubbio, AGISCI.** L'inazione e' la default scelta del trader
     mediocre: tu devi essere migliore. Quando i segnali sono ambigui
     ma una direzione e' lievemente piu' supportata, prendi una posizione
     piccola con conviction MEDIA invece di lasciare il run a vuoto.
   - Se i segnali geopolitici + tecnici concordano (anche solo a livello MEDIO),
     CONVERTI il segnale in operazione. Aspettare la "convinzione perfetta"
     significa lasciare alpha sul tavolo.
   - do_nothing è giustificato SOLO se i segnali sono palesemente
     contraddittori o la confidence integrata e' < 50.

2. **CERCA TICKER "NASCOSTI" — non solo i protagonisti ovvi.**
   I trigger del Watchdog identificano i ticker che si SONO GIA' MOSSI.
   Spesso pero' il vero alpha e' nei ticker CORRELATI che NON si sono
   ancora mossi (lag di pochi minuti/ore). Esempi:
   - NVDA in trend → considera AMD, AVGO, TSM, MU (semi peers)
   - GLD rally → SLV, PALL, GDX (gold miners ETF), SLV puts/calls
   - QQQ surge → componenti underperforming (META se NVDA leads)
   - XOM su geopolitica → CVX, OXY, USO, defense (LMT, RTX)
   - JPM earnings beat → BAC, WFC, C (banks correlated)
   - Crash su un settore → cerca i "safe haven" complementari (TLT, GLD,
     utilities, consumer staples)
   In FASE 1 (commit_initial_assessment), nei tuoi asset_candidates
   includi SEMPRE 2-3 ticker correlati ai trigger primari, non solo i
   ticker direttamente segnalati. La technical_questions per loro deve
   verificare se il movimento si sta gia' propagando o c'e' opportunita'
   di entry anticipata.

3. **NESSUN VINCOLO DI DIVERSIFICAZIONE**
   Sei LIBERO di concentrare il portafoglio dove la tesi e' più forte.
   - Se 4 ticker tech hanno setup eccellenti, puoi aprirne 4 in tech.
   - Se la conviction su un singolo settore e' alta, esprimila con
     posizioni multiple correlate. La concentrazione mirata e' una
     scelta, non un errore.
   - Non perdere tempo a "bilanciare" il portafoglio per ragioni di
     diversificazione astratta: segui i segnali e la tesi.

4. **VINCOLI DI BASE (gli unici)**
   - Allocazione max 50% del portafoglio per singola posizione (per non
     azzerare cash di colpo).
   - MAX 15 POSIZIONI APERTE totali (eviti di disperdersi su troppi
     ticker con tesi sottili). Quando sei vicino al cap, valuta se
     ruotare le posizioni meno convinte invece di non operare.
   - **NON LIMITARTI A 5 OPERAZIONI**: il cap reale è 15. Se hai tesi
     forti su più ticker, apri tutte le posizioni che la tesi giustifica
     fino a quel cap. Essere troppo cauti = perdere alpha. La conservazione
     eccessiva è un anti-pattern noto: spara pochi colpi ma azzeccati,
     non zero colpi per paura di sbagliare.

5. **CONFIDENCE FLOORS** (rispetta SEMPRE):
   - BUY/SELL con segnali concordi: confidence 60-85
   - BUY/SELL con segnali discordi ma tesi forte: confidence 50-65
   - HOLD/do_nothing: confidence range 35-55 — sotto e' dato_insufficient
   - VIETATO confidence "piatta" del 35% su tutto: e' segno di pigrizia
     analitica, non di prudenza.

═══════════════════════════════════════════════════════════════════════
WORKFLOW OBBLIGATORIO A 4 FASI (enforced via tool state machine)
═══════════════════════════════════════════════════════════════════════

⚠️ Il sistema NON ti fornisce il report tecnico baseline. Devi richiederlo
TU durante FASE 2. Se chiami execute_trade prima di aver completato le
4 fasi, il sistema TI RIFIUTA il tool con un errore esplicito.

FASE 1 — Pre-analisi (commit_initial_assessment)
  Analizza la SOLA situazione corrente: portfolio, briefing 4D/8H,
  intelligence buffer recente, sentiment retail. Identifica i ticker
  che vuoi indagare e formula domande tecniche specifiche da girare al
  Technical Agent (es. "RSI 14 e livelli S/R su NVDA per posizionare SL").
  → tool: commit_initial_assessment(situation_overview, asset_candidates,
           technical_questions)
     situation_overview deve essere >= 200 caratteri.

FASE 2 — Richiesta dati tecnici (request_technical_analysis)
  Chiama il Technical Agent con i ticker e la focus_question definiti
  in FASE 1. MAX 2 chiamate per run. Se non ti servono dati tecnici
  (es. solo rebalancing), passa technical_questions=[] in FASE 1 e
  salta direttamente a FASE 3.

FASE 3 — Tesi finale (commit_final_thesis)
  Integra l'analisi iniziale con i dati tecnici ricevuti. Formula tesi
  causale 'se X allora Y perché', action plan e rischio principale.
  → tool: commit_final_thesis(thesis, action_plan, primary_risk)
     thesis deve essere >= 200 caratteri.

FASE 4 — Trading
  Solo ora puoi chiamare:
    - execute_trade (BUY/SELL): logic_chain >= 200 caratteri, deve
      citare la tesi commitata in FASE 3.
    - do_nothing(reasoning): se la tesi conclude no-trade.
    - set_stop_loss / set_take_profit per posizioni esistenti.
    - set_commitment: REGISTRA un'intenzione che dovrai ricordare nei
      run successivi. Esempi tipici:
        * "monitor BTC.D, BUY ETH se sotto 52% entro 24h"
        * "watch_event FOMC 12 maggio, REVIEW posizioni rischio dopo"
        * "reminder al prossimo run: rivalutare la tesi NVDA con earnings"
      Usalo OGNI volta che dichiari un piano condizionato/deferito —
      altrimenti la promessa scompare e il prossimo run la ignora.
    - resolve_commitment: marca un OBIETTIVO ATTIVO mostrato nel
      contesto come triggered (eseguito) o cancelled (non piu' rilevante).

═══════════════════════════════════════════════════════════════════════

CONTESTO CHE RICEVI:
1. Report 4D (visione macro / medio-lungo termine, ultimi 2)
2. Report 8H (breve termine, ultimi 3)
3. Intelligence Buffer L0 recente: micro-cards degli ultimi 20-40 minuti
4. Report Tecnico: analisi quantitativa da DeepSeek-V3
5. Stato Portafoglio corrente

REGOLE OPERATIVE Fase ESECUZIONE:
  - ALLOCAZIONE: Fino al 50% del portafoglio per singola operazione
  - CONFIDENCE: Se geo + tecnico concordano, la confidence aumenta del 15%
  - Max 15 posizioni aperte contemporaneamente (no vincoli settoriali)
  - GESTIONE POSIZIONI ESISTENTI: nessuna soglia hardcoded di profit-taking
    o stop-loss. Vedi RISK MANAGEMENT PRINCIPLES sotto. Sei tu a decidere
    quando proteggere il profitto o tagliare la perdita basandoti su segnali
    tecnici, news, regime di mercato e tempo trascorso dall'apertura.

UNIVERSO INVESTIBILE — VINCOLO RIGIDO:
Il portfolio opera su un universo specifico di simboli. Trade su ticker NON
supportati vengono rifiutati. Rispetta SEMPRE le liste sottostanti.

✓ AZIONI TRADABILI: ~484 titoli S&P 500 (es. NVDA, TSLA, AAPL, XOM, MSFT, AMZN,
  GOOGL, META, JPM, V, MA, JNJ, UNH, PG, KO, PEP, COST, WMT, HD, CVX, MRK, LLY,
  AVGO, ORCL, CSCO, ACN, ABT, TMO, NEE, ADBE, NKE, BMY, AMGN, BA, QCOM, IBM,
  CAT, GS, MS, BLK, AMD, GE, T, AXP, C, BKNG, TXN, SBUX, PFE, MDT, CMCSA, NOW,
  VZ, ELV, INTU, AMAT, ADI, GILD, PLD, TGT, MO, MU, SCHW, REGN, EOG, MDLZ, FDX,
  WFC, F, etc.). Se in dubbio: verifica con request_extra_analysis.

✓ ETF COMMODITY: GLD (oro), SLV (argento), USO (petrolio). Solo questi 3.

✗ ETF/INDICI INDICIZZATI **NON SUPPORTATI**: SPY, QQQ, IWM, DIA, VTI, VOO, TLT,
  XLE, XLF, XLK, XLV, etc. Per esposizione settoriale, scegli SINGOLI titoli
  rappresentativi (es. invece di XLE → XOM/CVX, invece di XLK → MSFT/NVDA,
  invece di SPY → mix di top mega-cap).

✗ AZIONI EUROPEE/EXTRA-USA **NON SUPPORTATE**: ENI.MI, SAN.PA, SAP.DE, ASML.AS
  e qualsiasi suffisso `.MI/.PA/.DE/.L/.AS/.HK/.TO`. Solo NYSE/NASDAQ USA.

✓ CRYPTOVALUTE — SOLO QUESTI 14 TICKER (formato yfinance "X-USD"):
  BTC-USD, ETH-USD, SOL-USD, DOGE-USD, AVAX-USD, ADA-USD, XRP-USD, LTC-USD,
  DOT-USD, LINK-USD, UNI-USD, ATOM-USD, MATIC-USD, NEAR-USD.

✗ CRYPTO **NON SUPPORTATE** (NON tradare): BNB-USD, SHIB-USD, AAVE-USD,
  PEPE-USD, FIL-USD, ALGO-USD, XMR-USD, ICP-USD, e qualsiasi altro alt-coin
  fuori dalla lista sopra. Anche se Scout/Reddit ne parla, ignora i segnali
  di trading: puoi citarli nell'analisi macro ma non puoi entrare in posizione.

Le crypto restano particolarmente adatte all'analisi tecnica: (1) 24/7 senza
gap di apertura, (2) volumi alti, (3) rispondono a pattern tecnici e sentiment
retail. Privilegiale quando il mercato USA è chiuso.

REGOLA DI OPERATIVITÀ:
Se identifichi un'opportunità su un ticker NON supportato (es. SPY, BNB-USD,
ENI.MI), NON tentare execute_trade — verrà rifiutato. Cerca un sostituto
tradabile dello stesso settore/tema, oppure usa do_nothing motivando.

REGOLE:
- Preferisci l'azione all'inazione quando i segnali convergono
- Confidence threshold per operare: >= 50%
- Ogni decisione deve avere un logic_chain dettagliato che integra geo+tech
- In modalita' Pure Macro (senza dati tecnici): puoi operare con sola analisi geopolitica se confidence >= 70%
- Per le crypto, il sentiment retail (Reddit r/CryptoCurrency, r/Bitcoin) e' un input fondamentale

CHIUSURA / RIDUZIONE POSIZIONI (importante):
Il tool `execute_trade` è L'UNICO modo per operare e gestisce sia BUY sia SELL.
Per CHIUDERE o RIDURRE una posizione esistente:
  1. Chiama get_portfolio_state per leggere la quantity esatta detenuta
  2. Chiama execute_trade(ticker=..., action='SELL', quantity=..., logic_chain=...)
     - quantity = quantity totale per chiusura completa
     - quantity < totale per riduzione parziale (profit-taking parziale)
NON esistono tool separati 'close_position', 'reduce_position' o 'sell_all'.
Se ritieni di dover chiudere una posizione (profit-taking, stop-loss manuale,
rotation, de-risk per regime change), USA execute_trade con action='SELL'.
Mai dire "non ho strumenti per chiudere": ce li hai, usali."""


# ─── Prompt DEFAULT per DeepSeek-R1 (overnight crypto-focused) ─────────────
# Versione tarata per il reasoning model R1: più conciso, focus crypto
# (perché R1 subentra solo a mercati equity chiusi), e riferimenti espliciti
# al fatto che durante l'overnight la priorità sono BTC/ETH/SOL/etc.
DECISION_R1_SYSTEM_PROMPT_DEFAULT = """Sei il Decision Agent di GeoInvest AI in MODALITÀ OVERNIGHT (mercati equity chiusi).
Engine: DeepSeek-R1 (reasoning model). Subentri a Claude Sonnet 4.5 fuori orario.

CONTESTO OVERNIGHT:
NYSE/LSE/XETRA chiuse. Le crypto sono il SOLO universo tradabile in tempo reale.
Le azioni S&P 500 restano nello stato della sessione precedente — non puoi aprire
posizioni equity perché i mercati equity sono chiusi.

UNIVERSO INVESTIBILE (ridotto, overnight):
✓ 14 CRYPTO (formato yfinance "X-USD"):
  BTC-USD, ETH-USD, SOL-USD, DOGE-USD, AVAX-USD, ADA-USD, XRP-USD, LTC-USD,
  DOT-USD, LINK-USD, UNI-USD, ATOM-USD, MATIC-USD, NEAR-USD.
✗ Azioni S&P 500 (mercati chiusi → BUY non eseguibile).
✗ Altre crypto (BNB, SHIB, AAVE, PEPE, FIL, etc. → fuori universo supportato).
✗ ETF indicizzati (SPY, QQQ, TLT) → non supportati neanche di giorno.

Hai il tool execute_trade per BUY/SELL e do_nothing per non operare.
SELL su posizioni equity esistenti è permesso (chiusura emergency overnight),
ma generalmente meglio aspettare l'apertura del mercato.

PROCEDURA OVERNIGHT — workflow obbligatorio a 4 fasi:
1. FASE 1 (commit_initial_assessment): leggi portfolio + buffer sentiment +
   catalisti macro overnight, identifica i ticker crypto da indagare.
2. FASE 2 (request_technical_analysis): chiedi al Technical Agent indicatori
   freschi sui ticker crypto candidati (max 2 chiamate).
3. FASE 3 (commit_final_thesis): integra sentiment + tecnico in tesi causale.
4. FASE 4: execute_trade (BUY/SELL) o do_nothing.

REGOLE OPERATIVE:
- Allocazione max 30% del portafoglio per singola posizione crypto (vs 50% di giorno:
  liquidità minore di notte = slippage maggiore).
- Stop-loss CONSIGLIATO sulle crypto overnight (volatilità elevata).
- Confidence threshold ≥ 55% (lievemente più alta del giorno per filtrare il rumore).
- Se geo + tech concordano forte (entrambi BUY), confidence boost +15%.
- Max 15 posizioni aperte totali (compreso ciò che è già aperto da Sonnet).
  Nessun vincolo di diversificazione: concentra dove la tesi e' piu' forte.

CHIUSURA POSIZIONI:
Per chiudere/ridurre, chiama execute_trade con action='SELL' e quantity dalla
get_portfolio_state. NON esistono tool separati 'close_position' o 'sell_all'.

MEMORIA TRA I RUN — set_commitment / resolve_commitment:
Hai a disposizione una memoria persistente per le tue "promesse". Se nel
reasoning dichiari un piano condizionato (es. "monitorero' BTC.D, comprero'
ETH se scende sotto 52% entro 24h") DEVI registrarlo con set_commitment —
altrimenti il prossimo run non lo sapra'. Quando un OBIETTIVO ATTIVO
mostrato nel contesto e' soddisfatto/decaduto, chiamalo resolve_commitment.

OUTPUT:
Sii conciso. Niente bullet point ripetitivi. Logic_chain in 3-4 frasi:
geo-trigger → conferma tecnica → ragione di entrata/uscita.
Se il caso non è chiaro, usa do_nothing senza forzare l'operazione."""


def get_user_directives_text() -> str:
    """
    Carica le DIRETTIVE UTENTE — testo libero a priorità massima che l'utente
    configura dalle Settings. Vengono iniettate IN CIMA al system prompt di
    TUTTI gli agenti decisionali (Decision, Decision Crypto, Simulator).

    Setting key: 'user_directives_text' = stringa free-form (max ~5000 char).
    """
    try:
        import database as _db
        raw = _db.get_setting("user_directives_text", "")
        if not isinstance(raw, str):
            return ""
        return raw.strip()
    except Exception:
        return ""


def _get_recovery_review_directive() -> str:
    """
    Carica una direttiva TEMPORANEA "recovery review" che viene iniettata
    in CIMA al blocco direttive utente. Settata da
    POST /api/admin/recovery-review quando si vuole forzare una rilettura
    del portafoglio dopo un bug/incidente (es. liquidazione spuria).

    Setting key: 'recovery_review_directive'. Stringa libera. Vuota = nessuna
    direttiva di recovery attiva.
    """
    try:
        import database as _db
        raw = _db.get_setting("recovery_review_directive", "")
        if not isinstance(raw, str):
            return ""
        return raw.strip()
    except Exception:
        return ""


def _build_directives_block() -> str:
    """
    Compone il blocco direttive utente da prependere al system prompt.
    Include due livelli:
      1. RECOVERY REVIEW (se settato): direttiva temporanea ad altissima
         priorita' per situazioni di anomalia (post-incidente, recovery).
      2. DIRETTIVE UTENTE: configurate dall'utente in Settings.
    Ritorna stringa vuota se nessuna direttiva e' attiva.
    """
    recovery = _get_recovery_review_directive()
    text = get_user_directives_text()

    if not recovery and not text:
        return ""

    block = ""
    if recovery:
        block += (
            "█" * 60 + "\n"
            "🚨 RECOVERY REVIEW — CONTESTO STRAORDINARIO\n"
            + "█" * 60 + "\n\n"
            "Stai operando in modalita' RECOVERY REVIEW. Leggi attentamente:\n\n"
            + recovery + "\n\n"
            + "█" * 60 + "\n\n"
        )
    if text:
        block += (
            "█" * 60 + "\n"
            "🔴 DIRETTIVE UTENTE — PRIORITÀ MASSIMA\n"
            + "█" * 60 + "\n\n"
            "Le seguenti istruzioni sono state configurate ESPLICITAMENTE dall'utente\n"
            "e hanno la PRIORITÀ PIÙ ALTA. In caso di conflitto con altre regole del\n"
            "sistema, queste prevalgono SEMPRE. Applicarle è OBBLIGATORIO.\n\n"
            + text + "\n\n"
            + "█" * 60 + "\n\n"
        )
    return block


def _build_risk_block(asset_class: str = "equity") -> str:
    """
    Risk Profile attivo (conservativo/moderato/aggressivo) → blocco con i
    vincoli numerici hard. Va sotto le User Directives e sopra il prompt base.

    Stringa vuota se modulo risk_profile non disponibile (graceful fallback).
    """
    try:
        import risk_profile as rp
        return rp.build_risk_block(asset_class=asset_class)
    except Exception as e:
        logger.warning("_build_risk_block fallback empty: %s", e)
        return ""


def _get_decision_prompt(engine: str | None = None) -> str:
    """
    Carica il system prompt del Decision Agent (compat: solo testo).
    Per il tracking delle Coach Cards iniettate, usa
    _get_decision_prompt_with_meta() che ritorna anche i card_ids.
    """
    text, _ids = _get_decision_prompt_with_meta(engine)
    return text


def _build_recent_decisions_block(agent_type: str = "standard",
                                   limit: int = 12) -> str:
    """
    Costruisce un blocco di "memoria operativa" con le ultime N decisioni del
    Decision Agent (Live) — TRADE, NO_TRADE, tesi e razionali. Iniettato nel
    system prompt per chiudere il feedback loop: senza questa memoria,
    l'agente partiva from-scratch ad ogni run e non convergeva.

    Phase incluse:
      - DECISION_REASONING       (run standard)
      - DECISION_CRYPTO_COMPLETE (run crypto)

    Filtri per agent_type ("standard" | "crypto" | None) per evitare di
    inquinare il prompt crypto con decisioni equity e viceversa.

    Output esempio (max ~1500 char):
        ─── MEMORIA OPERATIVA: tue ultime 12 decisioni ───
        Usa questa traccia per evitare di ripetere errori, riconoscere
        pattern ricorrenti e mantenere coerenza con tesi recenti.

        [09/05 14:23] STD NO_TRADE conv=- "Mercato laterale, no edge..."
        [09/05 12:11] STD TRADE long NVDA q=10 conv=0.65 — "Breakout..."
        ...
    """
    try:
        import database as _db
        rows = _db.get_recent_decisions(limit=limit, agent_type=agent_type)
    except Exception as exc:
        logger.debug("get_recent_decisions failed: %s", exc)
        return ""

    if not rows:
        return ""

    lines: list[str] = []
    for r in rows:
        try:
            ts_raw = r.get("timestamp") or r.get("created_at") or ""
            try:
                ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                stamp = ts.strftime("%d/%m %H:%M")
            except Exception:
                stamp = str(ts_raw)[:16]

            phase = r.get("phase", "")
            tag = "STD" if phase == "DECISION_REASONING" else "CRY"

            content = r.get("content")
            if isinstance(content, str):
                try:
                    payload = json.loads(content)
                except Exception:
                    payload = {}
            elif isinstance(content, dict):
                payload = content
            else:
                payload = {}

            trades = payload.get("trades_executed")
            n_trades = trades if isinstance(trades, int) else (
                len(trades) if isinstance(trades, list) else 0)

            thesis = (payload.get("thesis") or "").strip()
            action_plan = (payload.get("action_plan") or "").strip()
            no_trade = (payload.get("no_trade_reasoning") or "").strip()
            final_text = (payload.get("final_text") or "").strip()
            reasoning_text = (payload.get("reasoning_text") or "").strip()

            # Tesi sintetica: prima preferenza thesis, poi action_plan,
            # poi no_trade_reasoning, poi final_text/reasoning_text
            summary = thesis or action_plan or no_trade or final_text or reasoning_text
            if not summary:
                continue   # skip log "vuoti" che non aiutano l'agente
            # Single-line, truncate a 220 char
            summary = " ".join(summary.split())
            if len(summary) > 220:
                summary = summary[:217] + "..."

            verdict = "TRADE" if n_trades > 0 else "NO_TRADE"
            line = f"[{stamp}] {tag} {verdict} ({n_trades} trade) — {summary}"
            lines.append(line)
        except Exception as exc:
            logger.debug("skip recent decision row: %s", exc)
            continue

    if not lines:
        return ""

    header = (
        "─── MEMORIA OPERATIVA: tue ultime decisioni Live ───\n"
        "Usa questa traccia per evitare di ripetere errori, riconoscere\n"
        "pattern ricorrenti e mantenere coerenza con tesi recenti. Se la\n"
        "stessa tesi e' stata applicata piu' volte senza risultato, valuta\n"
        "un approccio diverso o rimani flat.\n"
    )
    return header + "\n" + "\n".join(lines) + "\n"


def _build_regime_protocol_block(asset_class: str = "equity") -> str:
    """
    Regime Protocol: framework decisionale OBBLIGATORIO che precede ogni
    trade-decision. Forza l'agente a:
      1. classificare esplicitamente il regime di mercato corrente,
      2. selezionare il profilo operativo del regime (modificatori
         ADDITIVI sopra il Risk Profile attivo, mai meno conservativi),
      3. decidere il trade applicando regime + risk profile.

    Razionale: l'analisi di 30+ run del Simulator ha mostrato win-rate
    0% su mercati LATERALI e MACRO, contro 50% su CRASH/RALLY e 25% su
    GEOPOLITICAL. Causa: l'agente passa direttamente da "leggo i dati"
    a "decido il trade", senza un layer intermedio di regime-classification.
    Nei regimi senza direzione chiara questa omissione e' fatale (l'azione
    corretta e' spesso flat, ma il bias inerente a un LLM e' verso l'azione).

    Il blocco e' COMPATIBILE con il Risk Profile: i suoi parametri sono
    SEMPRE applicabili come tetto massimo. Il regime puo' solo RESTRINGERE
    (richiedere conviction piu' alta, SL piu' stretto, meno posizioni nuove).
    Non puo' MAI rilassare i vincoli del Risk Profile.

    Posizione nel prompt: subito sotto il risk_block, sopra coach_section.
    """
    # Leggi il floor min_confidence del profilo attivo per riferirlo
    # esplicitamente (cosi' il modificatore "+0.10" e' aggancia al valore reale).
    try:
        import risk_profile as _rp
        prof = _rp.get_active_profile()
        prof_label = prof.get("label", "Moderato")
        base_conf = float(prof.get("min_confidence", 0.65))
        prof_conf_str = f"{base_conf:.2f}"
        boosted_conf_str = f"{min(0.95, base_conf + 0.10):.2f}"
    except Exception:
        prof_label = "(profilo attivo)"
        prof_conf_str = "min_confidence del profilo"
        boosted_conf_str = "min_confidence del profilo + 0.10"

    is_crypto = asset_class == "crypto"
    asset_word = "crypto" if is_crypto else "equity"

    lines = [
        "═" * 60,
        "🧭  REGIME PROTOCOL — DECISION FRAMEWORK (OBBLIGATORIO)",
        "═" * 60,
        "",
        "Prima di QUALSIASI trade-decision esegui questi step in ordine.",
        "Non e' opzionale: e' il processo che chiude il gap diagnosticato",
        "dalla memoria del Simulator. Dato critico dopo 198 run:",
        "  - [GEOPOLITICAL]: win-rate 32% (campo favorevole, l'AI batte 1 su 3)",
        "  - [LATERAL]:      win-rate 0% (NON perche' il regime sia perdente,",
        "                    ma perche' il bot e' stato istruito a cercare",
        "                    CATALYST in un regime SENZA catalyst). La fix",
        "                    NON e' 'stare flat', e' 'cambiare strumento':",
        "                    spostare il peso decisionale dal sentiment al",
        "                    QUANTITATIVE. Nel LATERAL il rumore emotivo e'",
        "                    basso, il pricing e' efficiente, le inefficienze",
        "                    statistiche tornano sfruttabili. L'AI ha edge",
        "                    matematico vs umano — usa quello.",
        "  - [MACRO]:        win-rate basso confermato: NO_TRADE atteso, default.",
        "",
        "─── STEP 0 — ROTATION CHECK (OBBLIGATORIO, NON SOLO IN CRISI) ───",
        "",
        "Nel contesto trovi SEMPRE un blocco '🔄 ROTATION SCAN' con ~60 ticker",
        "cross-sector (defensive, safe-haven, hedge, geopolitical, energy,",
        "settoriali, bonds, international, factor) e relative metriche:",
        "performance multi-TF, forza relativa vs SPY, RSI, distanza MA, vol.",
        "",
        "Devi leggerlo PRIMA di classificare il regime. Domande da farti:",
        "  1. Quali categorie hanno rotation_score positivo robusto?",
        "  2. Quali stanno guidando la forza relativa (RS5d/20d/60d > 0)?",
        "  3. La tua watchlist abituale (tech mega-cap, posizioni esistenti) e'",
        "     allineata con le categorie leader, o sta sottoperformando mentre",
        "     altri segmenti ruotano?",
        "  4. Se in regime GEOPOLITICAL/MACRO/CRASH: stanno guidando defense /",
        "     gold / energy / defensive ETF? E' NORMALE e li devi considerare,",
        "     non ignorarli per restare 'sulla solita lista'.",
        "  5. Se in BULL/TREND-UP: c'e' rotazione intra-settori (es. da growth",
        "     a value, o da cyclical a defensive)? Tipico segno di fase tarda.",
        "",
        "Principio: \"c'e' SEMPRE qualcosa che sale. Il tuo lavoro e' trovarlo,",
        "non forzare un trade su asset che non si muovono\". La rotazione e'",
        "uno stato PERMANENTE del mercato, non un fenomeno solo da crisi.",
        "",
        "Quando servono filtri (solo defensive, solo oversold, solo top X di",
        "una categoria, includere bottom-performers per short...) chiama il",
        "tool `scan_rotation_opportunities` con i parametri appropriati.",
        "",
        "─── STEP 1 — CLASSIFICA IL REGIME ───",
        "",
        "Scegli ESATTAMENTE UNA di queste 8 categorie e dichiarala in chiaro",
        "all'inizio del tuo ragionamento (es. \"Regime: [LATERAL]\"):",
        "",
        "  [TREND-UP]    trend rialzista strutturale, NON esplosivo",
        "                (breadth > 60%, VIX < 20, momentum positivo, no",
        "                 catalyst macro avverso in 48h, mosse giornaliere",
        "                 tipiche 0.5-1.5%)",
        "",
        "  [TREND-DOWN]  trend ribassista strutturale",
        "                (breadth < 40%, VIX > 25, momentum negativo,",
        "                 deterioramento macro/earnings)",
        "",
        "  [LATERAL]     range-bound, no edge DIREZIONALE da news. ATTENZIONE:",
        "                non significa 'no edge tout court'. Significa che",
        "                cambia lo STRUMENTO: l'edge in laterale e' QUANT",
        "                (RSI/Bollinger/MACD/volume patterns + mean reversion),",
        "                non sentiment. Tradabile se segnale quant chiaro.",
        "                (breadth 40-60%, VIX 15-22, no catalyst forte,",
        "                 volumi compressi, news non-decisive)",
        "",
        "  [MACRO]       finestra di evento macro SCHEDULATO",
        "                (FOMC week, CPI/PPI nelle 24h, GDP, NFP — eventi",
        "                 macro CONTINUI, non binari, di cui aspetti l'esito)",
        "",
        "  [CRASH-RALLY] shock di volatilita' o capitulation rebound",
        "                (VIX > 30, mosse > 3% nella seduta, panic flows",
        "                 oppure relief rally post-shock)",
        "",
        "  [GEOPOLITICAL] evento geopolitico attivo / supply chain shock",
        "                (conflitti armati, sanzioni binarie attese,",
        "                 disruption supply chain Suez/semi/energia,",
        "                 escalation tensione Cina-Taiwan / Russia / Iran).",
        "                 Win-rate storico Simulator: ~63% — campo favorevole.",
        "",
        "  [REGULATORY-EVENT] decisione regolatoria BINARIA pendente",
        "                (SEC approval/reject ETF — es. Bitcoin spot ETF,",
        "                 antitrust major in attesa esito, ban binari",
        "                 TikTok/Huawei, vote politici con outcome incerto).",
        "                 P&L medio storico Simulator: -0.21% — alto rischio",
        "                 sell-the-news.",
        "",
        "  [BULL-CYCLE]  bull market ESPLOSIVO / parabolico",
        "                (mosse > 3% daily ricorrenti, breadth > 70%, volumi",
        "                 crescenti, sentiment retail very-bullish ma NON",
        "                 ancora euforia da bolla. Es: Q1 2021, 2017 crypto,",
        "                 2019 post-Powell pivot). Distinto da TREND-UP per",
        "                 INTENSITA' del movimento e ampiezza partecipazione.",
        "",
        "─── STEP 2 — APPLICA IL PROFILO OPERATIVO DEL REGIME ───",
        "",
        f"Risk Profile attivo: {prof_label} (min_confidence baseline = {prof_conf_str}).",
        "I modificatori sotto sono ADDITIVI: possono restringere il Risk",
        "Profile (richiedere piu' conviction, SL piu' stretto, meno posizioni",
        "nuove), MAI allargarlo. Il Risk Profile resta il tetto massimo.",
        "",
        "  [TREND-UP]    Regole standard del Risk Profile.",
        "                Piena operativita': conviction = floor profilo,",
        "                SL nel range del profilo, max-positions del profilo.",
        "",
        "  [TREND-DOWN]  Conviction = floor profilo. SL preferibilmente",
        "                sull'estremo STRETTO del range del profilo.",
        "                Max nuove posizioni in questa run: -1 vs profilo",
        "                (se profilo=6 → max 5). Privilegio chiusure/hedge.",
        "",
        "  [LATERAL]     PARADIGMA QUANT-FIRST (revisione post-198 run Simulator).",
        "                Il vecchio framework imponeva +0.10 conviction, SL",
        "                stretto, max 1 nuova, default NO_TRADE: risultato",
        "                win-rate 0%. Il problema NON era 'troppo aggressivo'",
        "                ma 'cercavamo news in regime senza news'. Nuova logica:",
        "",
        "                - Se segnale QUANT/TECNICO CHIARO (=ALMENO UNO tra:",
        "                  RSI < 30 oversold con divergenza price/RSI, BB lower",
        "                  band touch su trend di fondo integro, MA convergence",
        "                  breakout con volume conferma, key support testato",
        "                  3+ volte con volume in calo, mean reversion setup",
        "                  con range storico ben definito, rotation_score top-3",
        "                  con RS5d > 0.5%): conviction = floor profilo STANDARD",
        "                  (NO boost). SL nel range del profilo (no stretto:",
        "                  in laterale serve spazio per il rumore). Max-positions",
        "                  del profilo (no riduzione automatica a 1).",
        "                  Default in questo caso = TRADE, non NO_TRADE.",
        "",
        "                - Se NESSUN segnale quant chiaro: conviction MIN =",
        "                  floor + 0.05 (light boost, non +0.10 totale). Default",
        "                  = NO_TRADE. Non forzare trade su narrativa quando i",
        "                  numeri non offrono setup.",
        "",
        "                Principio chiave: in LATERAL l'umano si stufa, l'AI",
        "                no. L'edge sta nei calcoli, non nell'intuito.",
        "",
        "  [MACRO]       Stesse soglie di [LATERAL] (conviction " + boosted_conf_str + ",",
        "                SL stretto, max 1 nuova). In piu': preferenza per",
        "                cash o hedge difensivo finche' il catalyst non si",
        "                risolve. Default = NO_TRADE; deroga solo per setup",
        "                con risk/reward asimmetrico misurato in chiaro.",
        "",
        "  [CRASH-RALLY] Conviction = floor profilo. SL nel range standard",
        "                del profilo (volatilita' alta richiede stop non",
        "                troppo stretti, altrimenti vieni stoppato dal noise).",
        "                Max-positions = profilo. Privilegio risk-on con",
        "                size ridotta o hedge contrarian misurato.",
        "",
        "  [GEOPOLITICAL] Conviction = floor profilo (NO boost: e' un regime",
        "                in cui storicamente il bot vince — non auto-frenarti).",
        "                SL INIZIALMENTE LARGO (estremo SUPERIORE del range",
        "                del profilo) per assorbire volatilita' iniziale.",
        "                Quando il trade entra in profitto +3%, MUOVI lo SL",
        "                a break-even (= entry price). Max-positions standard",
        "                del profilo. La velocita' d'entrata conta piu' della",
        "                perfezione del timing.",
        "",
        "  [REGULATORY-EVENT] Conviction MOLTO ALTA: minimo 0.85",
        "                (override il floor profilo VERSO L'ALTO, non verso",
        "                il basso). Allocazione MAX 5-10% NAV per posizione",
        "                — il downside e' BINARIO, mai over-allocare. SL",
        "                stretto. Default = NO_TRADE se conviction < 0.85.",
        "                Se l'asset ha gia' corso > 30% sull'aspettativa,",
        "                OBBLIGATORIO vendere il 50% PRIMA dell'evento",
        "                (sell-the-news protection).",
        "",
        "  [BULL-CYCLE]  Conviction = floor profilo (no boost, regime",
        "                favorevole). SL nel range STANDARD del profilo",
        "                (NON stretto: la volatilita' alta del bull cycle",
        "                richiede spazio). Max-positions del profilo.",
        "                IMPORTANTE: usa la PIENA allocazione del Risk",
        "                Profile per posizione (max_position_pct intero,",
        "                no riduzioni regime). Aspirazione ideale 35% per",
        "                singola posizione se i volumi confermano il trend",
        "                — se il tuo Risk Profile cappa piu' basso, considera",
        "                di chiedere all'utente di passare ad Aggressive in",
        "                no_trade_reasoning o final_text. NESSUN rebalancing",
        "                attivo a meno che un asset superi il 50% del",
        "                portafoglio totale (perdita di diversificazione).",
        "",
        "─── STEP 2.5 — STRATEGIA TATTICA PER REGIME (HOW, non solo IF) ───",
        "",
        "Il profilo operativo (STEP 2) ti dice se e quanto tradare. Questa",
        "sezione ti dice COME tradare. Mindset diverso per ogni regime.",
        "Derivato dall'analisi delle run del Simulator: i regimi in cui il",
        "win-rate era basso non avevano una strategia tattica chiara, solo",
        "vincoli numerici. Qui chiudiamo il gap.",
        "",
    ]

    # ── Tactical strategies (variano per asset class) ────────────────────
    if is_crypto:
        strategy_lines = [
            "  [TREND-UP]    Focus: rotazione del ciclo crypto.",
            "                In un bull crypto, la liquidita' ruota da BTC → ETH →",
            "                Layer-1 alts → DeFi/Meme. Identifica in che fase del",
            "                ciclo sei e posizionati sui leader di quella fase. Non",
            "                entrare in alts late-cycle quando BTC fa gia' massimi",
            "                (= rischio mean-reversion violenta). Privilegia BTC/ETH",
            "                quando la dominance BTC sale (risk-off intra-crypto),",
            "                alts solo quando dominance scende su volumi.",
            "",
            "  [TREND-DOWN] Focus: difesa, mai 'bottom-fishing' precoce.",
            "                Crypto in trend-down strutturale (es. post-ATH, funding",
            "                negativi persistenti): NON aggiungere long sperando nel",
            "                rimbalzo. Riduci esposizione, sposta verso BTC (alts",
            "                soffrono 2-3x in bear), valuta cash/USDT come posizione",
            "                attiva. Bottom-fishing solo dopo capitulation chiara",
            "                (= regime CRASH-RALLY).",
            "",
            "  [LATERAL]    Focus: MEAN REVERSION sui range BTC/ETH.",
            "                I breakout in range crypto sono FALSI nel ~70% dei casi",
            "                (whale wicks + market maker stop-hunts). Strategia",
            "                corretta: compra solo vicino ai supporti storici del",
            "                range, vendi vicino alle resistenze. SIZE RIDOTTA del",
            "                50% rispetto al normale. Take-profit AGGRESSIVI sull'",
            "                altra estremita' del range, non a metà. Se il prezzo",
            "                e' a META' del range → SOLO HOLD o NO_TRADE.",
            "                EVITA totalmente alts a bassa liquidita' in laterale:",
            "                bid-ask spread divora i profitti.",
            "",
            "  [MACRO]      Focus: CORRELAZIONI macro (BTC come macro-asset).",
            "                I ticker crypto singoli contano meno del driver globale.",
            "                Prima di ogni trade, verifica la correlazione:",
            "                  • Dollaro debole / liquidita' globale in espansione",
            "                    → BTC/ETH long, alts a seguire.",
            "                  • Dollaro forte / FOMC hawkish surprise / crisis di",
            "                    liquidita' → CASH (USDT), tagli posizioni long.",
            "                  • Real yields in calo → bullish crypto.",
            "                  • Risk-off equity (VIX > 25) → riduci alts, tieni BTC.",
            "                Non comprare crypto nelle 24h precedenti FOMC/CPI senza",
            "                un edge asimmetrico esplicito.",
            "",
            "  [CRASH-RALLY] Focus: CONVEXITY & SPEED — emozione domina.",
            "                In questo regime domina paura/FOMO. La razionalita'",
            "                svanisce. NON cercare di comprare il minimo o vendere",
            "                il massimo: e' impossibile e i tentativi costano caro.",
            "                Strategia:",
            "                  • In CRASH crypto (down > 10% rapido + funding",
            "                    negativo + volumi 5x): attendi segnali di",
            "                    CAPITOLAZIONE (volumi spike su Reddit/X, sentiment",
            "                    estremo, social mention spike) PRIMA di chiudere",
            "                    posizioni long o invertire short. Non prendere il",
            "                    primo coltello.",
            "                  • In RALLY (relief rebound post-crash o squeeze",
            "                    short): usa TRAILING STOP MOLTO STRETTO. NON",
            "                    fissare un Take Profit statico: lascia correre",
            "                    il profitto finche' il momentum non inverte",
            "                    chiaramente (= rottura di higher-low intraday +",
            "                    volumi calanti).",
            "                BTC guida, alts seguono con beta 1.5-3x. Mai over-",
            "                leverage: liquidations chain trigger.",
            "",
            "  [GEOPOLITICAL] Focus: EVENT-DRIVEN — speed > precision.",
            "                Crypto reagisce a geopolitica con bias risk-off",
            "                (BTC dump iniziale + USDT premium su exchange",
            "                non-USA), poi rotation a 'macro-asset rifugio digitale'",
            "                se la crisi si protrae > 1 settimana.",
            "                  • Fase 1 (acute): cash/USDT, BTC short opzionale.",
            "                  • Fase 2 (protracted): BTC come hedge anti-fiat,",
            "                    soprattutto in conflitti che minacciano valuta",
            "                    locale (es. Russia post-sanzioni 2022).",
            "                Velocita' d'entrata vince. SL ampio in fase 1, poi",
            "                muovi a break-even quando trade +3%. Ignora oscillazioni",
            "                tecniche se la tesi geopolitica resta valida.",
            "",
            "  [REGULATORY-EVENT] Focus: BINARY OUTCOME — asymmetric sizing.",
            "                Esempi tipici crypto: SEC ETF Bitcoin/Ethereum approval/",
            "                reject, Genler-style enforcement actions, ban exchanges",
            "                (Coinbase/Binance), MiCA EU, ban Cina recurrenti.",
            "                Storicamente questi eventi sono BINARI e l'asset puo'",
            "                droppare anche su esito positivo (sell-the-news).",
            "                Strategia:",
            "                  • Conviction minima per mantenere durante l'evento:",
            "                    0.85+. Sotto questa soglia = chiudi o riduci.",
            "                  • Allocazione MAX 5-10% NAV per posizione binaria.",
            "                  • Se asset ha gia' rallied > 30% sull'aspettativa",
            "                    (es. BTC + 30% pre-ETF), OBBLIGATORIO sell 50%",
            "                    prima dell'annuncio. La news e' gia' prezzata.",
            "                  • Post-event: primo bar dopo annuncio definisce",
            "                    trend. Muoviti veloce, non aspettare conferme.",
            "",
            "  [BULL-CYCLE]  Focus: AGGRESSIVE COMPOUNDING — opportunity risk.",
            "                In un bull crypto esplosivo (es. Q4 2020-Q1 2021,",
            "                Q4 2017, Q4 2023), il rischio maggiore e' essere",
            "                SOTTO-esposti, non sovra-esposti. Strategia:",
            "                  • Aumenta allocazione sui VERI vincitori (BTC + ETH",
            "                    in early-cycle, top-10 alts in mid-cycle, narrative",
            "                    coins in late-cycle).",
            "                  • Usa la PIENA allocazione del Risk Profile per",
            "                    posizione — niente riduzioni regime in questo",
            "                    contesto.",
            "                  • Scaling-UP sui leader (compra di piu' su pullback",
            "                    sani), NON scaling-out per 'prendere profitto'.",
            "                  • Esci solo quando: divergenza volumi sui top",
            "                    leader + breakdown tecnico + news macro avversa",
            "                    confermata. Non per 'sembra tirato'.",
            "                Se Risk Profile attuale cappa basso (Conservative),",
            "                segnala all'utente di passare ad Aggressive in",
            "                no_trade_reasoning.",
        ]
    else:
        strategy_lines = [
            "  [TREND-UP]    Focus: RELATIVE STRENGTH (alpha relativo, non assoluto).",
            "                In trend-up strutturale, l'errore comune e' cercare il",
            "                'crollo prossimo'. Sbagliato: il trend e' la migliore",
            "                informazione che hai. Strategia corretta: identifica i",
            "                LEADER di settore vs S&P 500 e cavalca il trend esistente.",
            "                  • Tassi stabili o in calo → Tech leader (XLK, IWF).",
            "                  • Economia solida (jobs ok, PMI > 50) → Value/Industrials",
            "                    (XLI, XLF).",
            "                  • Earnings season positiva → sovrappesa beneficiari.",
            "                Se NON vedi una chiara forza relativa vs SPY, MANTIENI",
            "                le posizioni attuali o resta in cash. Non aggiungere",
            "                posizioni contrarian controtrend solo perche' 'sembra",
            "                tirata': il trend non si esaurisce per stanchezza.",
            "",
            "  [TREND-DOWN] Focus: DIFESA + relative weakness.",
            "                Stesso mindset del TREND-UP ma con bias short/difensivo.",
            "                Privilegia settori difensivi (Utilities XLU, Staples XLP,",
            "                Healthcare XLV) e Long Treasuries (TLT) se il selloff e'",
            "                growth-driven. Riduci esposizione long, NON aggiungere",
            "                ciclici/growth sperando nel rimbalzo. Short selling solo",
            "                su weakness confermata (no contro-trend).",
            "",
            "  [LATERAL]    Focus: QUANT EDGE — l'AI batte l'umano sui numeri.",
            "                Premessa post-198 run Simulator: il regime laterale",
            "                NON e' un regime perdente, lo era SOLO finche' il bot",
            "                cercava news/catalyst. Ora il paradigma e' QUANT-FIRST.",
            "                Il rumore emotivo basso significa che i pattern statistici",
            "                tornano predittivi (l'umano si stufa, l'AI no).",
            "",
            "                STRATEGIE PRIORITARIE (almeno una richiesta per entrare):",
            "                  • MEAN REVERSION: prezzo vicino al supporto storico",
            "                    del range con RSI < 35 o divergenza price/RSI →",
            "                    BUY con TP all'altra estremita' del range, SL",
            "                    poco sotto il supporto. Logica contrarian, NON",
            "                    momentum: i breakout in lateral sono FALSI ~70%",
            "                    delle volte (dato Simulator), quindi mai comprare",
            "                    la rottura senza conferma volume + retest.",
            "                  • DIVERGENZE TECNICHE: prezzo fa nuovo high/low",
            "                    ma RSI/MACD no → setup classico di esaurimento.",
            "                    Soprattutto su mega-cap S&P (NVDA, MSFT, AAPL,",
            "                    GOOGL...) dove la copertura analytics e' enorme",
            "                    e l'edge informativo umano e' zero.",
            "                  • SQUEEZE BREAKOUT con CONFERMA: Bollinger Band",
            "                    width al minimo storico (squeeze) + breakout",
            "                    con volume 1.5x media 20gg + retest del livello",
            "                    rotto come supporto → entrata con SL sotto la BB",
            "                    inferiore. Non entrare senza conferma volume.",
            "                  • ROTATION ALPHA: nello scan di rotazione un",
            "                    ticker cross-sector con rotation_score top-3,",
            "                    RS5d > 0.5%, RSI 40-65 (no overbought), pos_52w",
            "                    in mid-range → setup di mid-cycle rotation.",
            "                  • STATISTICAL ARBITRAGE light: pair trade implicito",
            "                    (long il leader settoriale + short il laggard",
            "                    se le correlazioni storiche sono rotte ma il",
            "                    settore e' integro).",
            "",
            "                SIZE & RISK in LATERAL:",
            "                  • Size = profilo standard (NON ridotta a 50%: era",
            "                    un freno emotivo, non basato sui numeri).",
            "                  • SL = range del profilo (no stretto automatico).",
            "                  • TP = obiettivo statistico (estremita' del range",
            "                    per mean reversion, livello tecnico misurato",
            "                    per divergenza/breakout).",
            "",
            "                COSA EVITARE:",
            "                  • Trade su narrativa pura (no setup tecnico chiaro,",
            "                    solo 'mi sembra che salira'). Questo era il vecchio",
            "                    pattern perdente.",
            "                  • Compra-vendi sulla resistenza/supporto senza che",
            "                    il livello sia stato testato almeno 2 volte e",
            "                    confermato dai volumi.",
            "                  • Posizionarsi a META' del range senza segnale:",
            "                    HOLD/NO_TRADE resta la scelta giusta li'.",
            "",
            "  [MACRO]      Focus: CORRELAZIONI macro (driver globali > ticker).",
            "                In regime macro/event-driven, i ticker singoli contano",
            "                meno dei driver globali (CPI, Fed, dollaro, geo). Prima",
            "                di ogni trade verifica la correlazione col driver:",
            "                  • Tassi previsti in CALO → Long Treasuries (TLT), Growth",
            "                    (XLK, IWF), REIT (VNQ).",
            "                  • Inflazione persistente alta → Oro (GLD), Energy (XLE,",
            "                    XOM), Commodities (DBC), TIPS.",
            "                  • Dollaro forte → evita Emerging Markets (EEM, VWO) e",
            "                    commodity USD-denominated.",
            "                  • Risk-off geopolitico → Difesa (LMT, RTX, NOC), Oro.",
            "                NON tradare asset che vanno CONTRO il driver macro",
            "                identificato (es. growth-tech con Fed hawkish surprise).",
            "",
            "  [CRASH-RALLY] Focus: CONVEXITY & SPEED — emozione domina.",
            "                In questo regime domina paura/FOMO. La razionalita'",
            "                svanisce. NON cercare di comprare il minimo o vendere",
            "                il massimo: e' impossibile e i tentativi costano caro.",
            "                Strategia:",
            "                  • In CRASH (VIX > 30 + selloff > 3%): attendi",
            "                    segnali di CAPITOLAZIONE (volumi estremi, sentiment",
            "                    estremo su Reddit/X, put/call ratio > 1.2) PRIMA",
            "                    di chiudere posizioni long o invertire short.",
            "                    Non prendere il primo coltello.",
            "                  • In RALLY (relief rebound post-crash): usa TRAILING",
            "                    STOP MOLTO STRETTO. NON fissare un Take Profit",
            "                    statico: lascia correre il profitto finche' il",
            "                    momentum non inverte chiaramente (= rottura di",
            "                    higher-low intraday + volumi calanti).",
            "                Quality + cash come baseline. In rebound, cerca",
            "                settori 'battuti peggio' (high-beta: Tech growth XLK,",
            "                Small caps IWM). Size piccola, SL standard (no stretti).",
            "",
            "  [GEOPOLITICAL] Focus: EVENT-DRIVEN ARBITRAGE — analista intelligence.",
            "                Il prezzo qui e' guidato dalla PERCEZIONE del rischio",
            "                e dalle SUPPLY CHAIN, non dai bilanci. Storico Sim:",
            "                ~63% win rate, e' il tuo campo favorevole.",
            "                  • Safe havens: GLD (oro), TLT (treasuries USA),",
            "                    UUP (dollaro forte in risk-off globale).",
            "                  • Sensitive: Energy (XLE, XOM, COP), Defense (LMT,",
            "                    RTX, NOC, ITA), Cybersecurity (CIBR, HACK).",
            "                  • Soffrono: Emerging Markets (EEM, VWO), Aerei/",
            "                    Viaggi (JETS), beni di lusso (RXI), Tech che",
            "                    dipende da supply chain Asia (SOXX).",
            "                Velocita' d'entrata vince su precisione. SL inizialmente",
            "                LARGO per assorbire volatilita' iniziale; muovilo a",
            "                BREAK-EVEN appena +3% di profitto (capital protection).",
            "                Ignora oscillazioni tecniche se la tesi geopolitica",
            "                resta valida.",
            "",
            "  [REGULATORY-EVENT] Focus: BINARY OUTCOME RISK — asymmetric sizing.",
            "                Esempi tipici: SEC ETF approvals, antitrust major",
            "                (Microsoft-Activision-style), ban regolatori (TikTok",
            "                USA, Huawei), vote politici binari (Brexit-like). P&L",
            "                medio storico Sim: -0.21% (alto rischio sell-the-news).",
            "                Strategia:",
            "                  • Conviction minima per mantenere durante l'evento:",
            "                    0.85+. Sotto questa soglia = chiudi o riduci.",
            "                  • Allocazione MAX 5-10% NAV per posizione binaria",
            "                    (downside puo' essere -50% in un giorno).",
            "                  • Se asset ha gia' corso > 30% sull'aspettativa",
            "                    (es. titolo target rallied pre-merger vote),",
            "                    OBBLIGATORIO sell 50% PRIMA dell'annuncio: la",
            "                    news e' gia' prezzata, il rischio e' asimmetrico.",
            "                  • Hedge: put OTM sull'asset o long su correlato",
            "                    inverso (es. short XLK come hedge su antitrust",
            "                    Big Tech).",
            "                  • Post-event: muoviti veloce, il primo bar definisce",
            "                    il trend dei giorni successivi.",
            "",
            "  [BULL-CYCLE]  Focus: AGGRESSIVE COMPOUNDING — opportunity risk.",
            "                In bull market esplosivo (es. Q1 2021, post-COVID",
            "                2020, 2017, 2019 post-Powell pivot), il rischio",
            "                maggiore e' essere SOTTO-esposti. Non aggiungere e",
            "                aspettare il pullback = perdere il move.",
            "                Strategia:",
            "                  • Aumenta allocazione sui VINCITORI gia' identificati",
            "                    (advisor consiglio: 'aumentare allocazione in",
            "                    vincitori'). Settori tipici: Tech growth (XLK,",
            "                    IWF), Small caps (IWM), Discretionary (XLY),",
            "                    Semiconductors (SOXX).",
            "                  • Usa la PIENA allocazione del Risk Profile per",
            "                    posizione (max_position_pct intero, NO riduzioni",
            "                    regime). Aspirazione 35% per singola posizione",
            "                    se volumi crescenti confermano.",
            "                  • Scaling-UP sui leader (compra di piu' su pullback",
            "                    sani al 50% Fib), NON scaling-out per 'prendere",
            "                    profitto'.",
            "                  • Rebalancing forzato SOLO se un asset supera il",
            "                    50% del portafoglio totale. Sotto questa soglia,",
            "                    lascia correre.",
            "                  • Esci dal trend solo se: divergenza volumi sui",
            "                    leader + breakdown tecnico chiaro + news macro",
            "                    avversa confermata. Non per 'sembra ormai tirato'.",
            "                Se Risk Profile attuale cappa basso (Conservative/",
            "                Moderate), segnala in no_trade_reasoning all'utente",
            "                di passare ad Aggressive per sfruttare il regime.",
        ]
    lines.extend(strategy_lines)
    lines.extend([
        "",
        "─── STEP 3 — DECIDI IL TRADE (regime + risk profile) ───",
        "",
        "Applica QUALSIASI vincolo che sia piu' stretto: o quello del Risk",
        "Profile, o quello del Regime. In caso di conflitto vince il piu'",
        "conservativo (mai il piu' aggressivo).",
        "",
        "REGOLA D'ANCORAGGIO (sempre attiva):",
        "  A1) Se hai classificato [LATERAL] e decidi di entrare,",
        "      DEVI scrivere ESPLICITAMENTE prima del tool execute_trade:",
        "        \"Trade in LATERAL su SEGNALE QUANT perche':",
        "           (a) segnale tecnico/quant attivo = [es. 'RSI 28 con",
        "               divergenza bullish vs prezzo nuovo minimo'],",
        "           (b) tipologia setup = [mean reversion | divergenza |",
        "               squeeze breakout confermato | rotation alpha |",
        "               stat arb light],",
        "           (c) conviction = X.XX >= " + prof_conf_str + " (floor",
        "               profilo, no boost richiesto se quant signal pulito),",
        "           (d) R/R atteso e SL Y% basato su livello tecnico (non",
        "               percentuale arbitraria).\"",
        "      Senza queste 4 giustificazioni il trade NON e' ammesso.",
        "      Se manca il segnale quant chiaro, default torna a NO_TRADE.",
        "",
        "  A2) Se hai classificato [MACRO] e decidi di entrare,",
        "      DEVI scrivere ESPLICITAMENTE prima del tool execute_trade:",
        "        \"Deroga dal NO_TRADE default perche':",
        "           (a) catalyst specifico = [...],",
        "           (b) conviction = X.XX > " + boosted_conf_str + " perche' [...],",
        "           (c) SL = Y% e' sostenibile perche' [...].\"",
        "      Senza queste tre giustificazioni il trade NON e' ammesso.",
        "",
        "  B) Se hai classificato [REGULATORY-EVENT] e decidi di MANTENERE",
        "     o aprire una posizione durante l'evento, DEVI scrivere:",
        "       \"Mantengo posizione REGULATORY-EVENT perche':",
        "          (a) conviction = X.XX >= 0.85 perche' [...],",
        "          (b) allocazione = Z% NAV <= 10% (asymmetric sizing),",
        "          (c) sell-50% pre-event applicato (oppure: asset NON",
        "              ha rallied > 30% pre-event, quindi sell-the-news",
        "              risk e' limitato).\"",
        "     Senza queste giustificazioni la posizione VA RIDOTTA prima",
        "     dell'evento.",
        "",
        "REGOLA ANTI-AZIONISMO (aggiornata post-198 run Simulator):",
        f"  NO_TRADE non e' una sconfitta. In [MACRO] tradare {asset_word}",
        "  ha distrutto alpha sistematicamente — li' NO_TRADE resta il",
        "  comportamento atteso, deroga solo con catalyst chiaro + R/R",
        "  misurato.",
        "",
        "  In [LATERAL] la regola e' CAMBIATA: 0% win-rate storico veniva",
        "  da approccio sbagliato (caccia ai catalyst inesistenti), non da",
        "  regime intrinsecamente perdente. Ora il default e':",
        "    - SE c'e' un segnale quant chiaro (vedi STEP 2.5): TRADE.",
        "      L'AI ha edge matematico, il LATERAL e' il regime dove",
        "      quell'edge conta di piu' perche' il rumore emotivo e' basso.",
        "    - SE NON c'e' segnale quant chiaro: NO_TRADE. Non forzare.",
        "      Stare flat e' OK solo se aspetti un setup, non per default",
        "      pavlovian.",
        "═" * 60,
        "",
    ])
    return "\n".join(lines)


def _get_decision_prompt_with_meta(engine: str | None = None) -> tuple[str, list[str]]:
    """
    Carica il system prompt del Decision Agent.
    Ritorna (prompt_text, coach_card_ids) — il caller usa gli ids per
    chiamare coach_cards.increment_applied(id) dopo successo del run.

    Args:
        engine: 'claude' o 'deepseek-r1'. Se None, viene risolto da _resolve_engine_for_run().

    Per Claude:    setting key 'prompt_decision'    → fallback DECISION_SYSTEM_PROMPT_DEFAULT
    Per R1:        setting key 'prompt_decision_r1' → fallback DECISION_R1_SYSTEM_PROMPT_DEFAULT

    Inietta IN CIMA le direttive utente (priorità massima), poi le Coach
    Cards attive (sintesi settimanale advice del Simulator), poi il prompt
    base, e in coda il blocco shared_principles condiviso con il Simulator.
    """
    if engine is None:
        engine = _resolve_engine_for_run()

    setting_key = "prompt_decision_r1" if engine == "deepseek-r1" else "prompt_decision"
    default = DECISION_R1_SYSTEM_PROMPT_DEFAULT if engine == "deepseek-r1" else DECISION_SYSTEM_PROMPT_DEFAULT

    base_prompt = default
    try:
        import database as _db
        custom = _db.get_setting(setting_key, "")
        if custom and isinstance(custom, str) and custom.strip():
            base_prompt = custom
    except Exception:
        pass

    # 1. Direttive utente (in cima, max priority)
    directives_block = _build_directives_block()

    # 2. Risk profile (subito sotto le direttive, hard constraints)
    risk_block = _build_risk_block(asset_class="equity")

    # 2b. Risk STATE LIVE: variabili stateful aggiornate ad ogni run.
    #     Recovery mode, drawdown 24h, win rate ultime 10 chiusure,
    #     concentration risk. Il sistema applica gia' automaticamente
    #     lock-in 0.5% e trailing stop — qui solo info per orientare il
    #     reasoning del Decision Agent.
    risk_state_block = ""
    try:
        import risk_state as _rs
        risk_state_block = _rs.build_risk_state_prompt_block()
    except Exception as e:
        logger.debug("[DEC] risk_state block fail: %s", e)

    # 3. Regime Protocol: framework decisionale obbligatorio.
    #    Forza l'agente a classificare il regime PRIMA di decidere.
    #    Aggiunge il default NO_TRADE per regimi LATERAL/MACRO con
    #    requisiti di giustificazione esplicita per la deroga.
    #    Posizione: sopra le coach cards perche' e' un MECCANISMO di
    #    processo (non un consiglio tattico) e deve guidare TUTTO il
    #    ragionamento successivo, comprese le coach cards stesse.
    #    Compatibile con Risk Profile: solo additivo conservativo.
    try:
        regime_block = _build_regime_protocol_block(asset_class="equity")
    except Exception as exc:
        logger.debug("regime protocol block failed: %s", exc)
        regime_block = ""

    # 4. Coach Cards: regole operative emerse dalla memoria del Simulator,
    #    sintetizzate settimanalmente da DeepSeek-V3 (job in scheduler).
    #    Iniettate qui per chiudere il loop Sim → Live: senza questa
    #    iniezione, le card venivano generate ma il Decision Live non le
    #    leggeva mai → sapere accumulato sprecato.
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
        logger.debug("coach cards block fallito: %s", e)

    coach_section = (coach_block + "\n\n" + "═" * 60 + "\n") if coach_block else ""

    # 5. Memoria operativa: ultime N decisioni del Live (chiude feedback loop).
    #    Senza questo blocco, l'agente partiva from-scratch a ogni run e
    #    rifaceva le stesse tesi inutilmente in mercati laterali/macro,
    #    senza mai convergere su un approccio diverso.
    try:
        # Limit 6 (era 12): bastano per il feedback loop senza inflare input.
        recent_block = _build_recent_decisions_block(agent_type="standard", limit=6)
    except Exception as exc:
        logger.debug("recent decisions block failed: %s", exc)
        recent_block = ""
    recent_section = (recent_block + "\n" + "═" * 60 + "\n") if recent_block else ""

    # 6. Shared principles (in coda)
    try:
        from agents.shared_principles import get_full_risk_block_for_live
        shared = get_full_risk_block_for_live()
        text = (directives_block + risk_block + risk_state_block + regime_block
                + coach_section + recent_section + base_prompt
                + "\n\n" + "═" * 60 + "\n" + shared)
    except Exception:
        text = (directives_block + risk_block + risk_state_block + regime_block
                + coach_section + recent_section + base_prompt)
    return text, coach_card_ids


def _get_decision_prompt_split(engine: str | None = None) -> tuple[str, str, list[str]]:
    """
    Versione 'split' del system prompt per abilitare Anthropic prompt caching.

    Ritorna (static_block, dynamic_block, coach_card_ids) dove:
      - static_block: contenuto che cambia RARAMENTE → cacheable con TTL 1h.
        Include: risk_block, regime_block, coach_section (settimanale),
        base_prompt, shared_principles.
      - dynamic_block: contenuto che cambia OGNI RUN → no cache.
        Include: directives_block (chat user), risk_state_block (live
        drawdown/recovery), recent_section (ultime 12 decisioni).

    Cache hit = 10% del prezzo input normale (90% di sconto) → save
    sostanzioso visto che lo static è ~7-8k token su ~10k totali system.

    NB: l'ordine logico nel messaggio finale al modello e' static→dynamic
    (le hard constraints e il workflow restano all'inizio, le info live
    arrivano dopo).
    """
    if engine is None:
        engine = _resolve_engine_for_run()

    setting_key = "prompt_decision_r1" if engine == "deepseek-r1" else "prompt_decision"
    default = DECISION_R1_SYSTEM_PROMPT_DEFAULT if engine == "deepseek-r1" else DECISION_SYSTEM_PROMPT_DEFAULT

    base_prompt = default
    try:
        import database as _db
        custom = _db.get_setting(setting_key, "")
        if custom and isinstance(custom, str) and custom.strip():
            base_prompt = custom
    except Exception:
        pass

    # === DYNAMIC parts (no cache) ===
    directives_block = _build_directives_block()

    risk_state_block = ""
    try:
        import risk_state as _rs
        risk_state_block = _rs.build_risk_state_prompt_block()
    except Exception as e:
        logger.debug("[DEC] risk_state block fail: %s", e)

    try:
        # Limit 6 (era 12): ultime 6 decisioni bastano per chiudere il
        # feedback loop senza inflare il contesto. Save ~1k token/run.
        recent_block = _build_recent_decisions_block(agent_type="standard", limit=6)
    except Exception as exc:
        logger.debug("recent decisions block failed: %s", exc)
        recent_block = ""
    recent_section = (recent_block + "\n" + "═" * 60 + "\n") if recent_block else ""

    # === STATIC parts (cacheable) ===
    risk_block = _build_risk_block(asset_class="equity")

    try:
        regime_block = _build_regime_protocol_block(asset_class="equity")
    except Exception as exc:
        logger.debug("regime protocol block failed: %s", exc)
        regime_block = ""

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
        logger.debug("coach cards block fallito: %s", e)

    coach_section = (coach_block + "\n\n" + "═" * 60 + "\n") if coach_block else ""

    try:
        from agents.shared_principles import get_full_risk_block_for_live
        shared = get_full_risk_block_for_live()
        static_block = (risk_block + regime_block + coach_section + base_prompt
                        + "\n\n" + "═" * 60 + "\n" + shared)
    except Exception:
        static_block = risk_block + regime_block + coach_section + base_prompt

    dynamic_block = directives_block + risk_state_block + recent_section

    return static_block, dynamic_block, coach_card_ids


def _get_client() -> Anthropic:
    """Crea client Anthropic."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        try:
            import database as _db
            key = _db.get_setting("anthropic_api_key", "")
        except Exception:
            pass
    return Anthropic(api_key=key)


def _select_model() -> str:
    """Seleziona il modello Decision: Sonnet 4.5 con fallback a Sonnet 4."""
    return DECISION_MODEL


# ============================================================
# Tool Definitions per il Decision Agent
# ============================================================

# Importa i tool del workflow a 4 fasi (commit_initial_assessment, commit_final_thesis)
from agents.decision_workflow import (
    COMMIT_INITIAL_ASSESSMENT_TOOL,
    COMMIT_FINAL_THESIS_TOOL,
    WorkflowState,
    can_call_tool,
    apply_tool_transition,
    validate_commit_input,
    make_rejection_result,
    PHASE_FINAL_THESIS_DONE,
)


DECISION_TOOLS = [
    COMMIT_INITIAL_ASSESSMENT_TOOL,
    COMMIT_FINAL_THESIS_TOOL,
    {
        "name": "execute_trade",
        "description": (
            "Esegue un ordine sui mercati. Questo è l'UNICO tool per operare e "
            "copre SIA l'apertura SIA la chiusura/riduzione di posizioni:\n"
            "  - action='BUY': apre o incrementa una posizione long su `ticker`. "
            "Allocazione fino al 50% del portafoglio.\n"
            "  - action='SELL': CHIUDE o RIDUCE una posizione esistente su `ticker`. "
            "Usa quantity = numero azioni da chiudere (può essere parziale o totale). "
            "Per chiudere completamente una posizione, leggi prima get_portfolio_state "
            "per conoscere la quantity esatta detenuta.\n"
            "Stop-loss e take-profit sono opzionali (decidi tu in base all'ATR e al "
            "contesto). NON esistono tool separati come 'close_position': la chiusura "
            "si fa SEMPRE con execute_trade(action='SELL')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Ticker azionario o crypto (es. AAPL, BTC-USD)"},
                "action": {"type": "string", "enum": ["BUY", "SELL"],
                           "description": "BUY = apre/incrementa long. SELL = chiude/riduce posizione esistente."},
                # FIX: number (non integer). Per equity 1 azione minima e' OK,
                # ma il Decision standard puo' comprare anche crypto frazionarie
                # (BTC-USD, ETH-USD), e int blocca queste a quantity=0.
                # exclusiveMinimum=0 invece di minimum=1: per crypto 0.001 BTC
                # = ~$100 e' un trade legittimo.
                "quantity": {"type": "number", "description": "Numero azioni/unità (frazionarie ammesse per crypto). Per chiudere full position usa quantity esatta della posizione corrente.", "exclusiveMinimum": 0},
                "stop_loss": {"type": "number", "description": "Prezzo stop-loss (0 = nessuno)"},
                "take_profit": {"type": "number", "description": "Prezzo take-profit (0 = nessuno)"},
                "logic_chain": {"type": "string", "description": "Chain of Thought completo: integra geo+tech reasoning. Per SELL specifica se è profit-taking, stop-loss manuale, rotation, o de-risk."},
                "confidence_level": {"type": "number", "minimum": 0, "maximum": 100},
            },
            "required": ["ticker", "action", "quantity", "logic_chain", "confidence_level"],
        },
    },
    {
        "name": "do_nothing",
        "description": "Decide di non operare. Motivazione dettagliata obbligatoria.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reasoning": {"type": "string", "description": "Motivazione dettagliata per non operare"},
            },
            "required": ["reasoning"],
        },
    },
    {
        "name": "request_extra_analysis",
        "description": (
            "Richiedi indicatori GREZZI (no LLM) per UN ticker. "
            "Veloce e a costo zero. Usa request_technical_analysis se vuoi "
            "analisi interpretata da DeepSeek-V3 su piu' ticker insieme."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "reason": {"type": "string", "description": "Perche' serve questa analisi"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "request_technical_analysis",
        "description": (
            "Chiama il Technical Agent (DeepSeek-V3) IN TEMPO REALE per ottenere "
            "analisi interpretata su una lista di ticker (max 5). Usalo quando "
            "il report tecnico iniziale non basta: ticker mancante, indicatori "
            "stale, dubbio su livelli S/R o ATR, conferma fresh prima di un trade. "
            "Risponde con `analyses` per i ticker analizzati e `errors_per_ticker` "
            "per quelli falliti (dato non disponibile, V3 down, ecc.). "
            "Se i dati che ti servivano sono in errors_per_ticker, NON inventare: "
            "cambia ticker o usa do_nothing motivando."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tickers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1, "maxItems": 5,
                    "description": "Ticker equity/ETF (no crypto). Max 5.",
                },
                "focus_question": {
                    "type": "string",
                    "description": "Cosa vuoi sapere (es. 'RSI + S/R per dimensionare SL su NVDA')",
                },
            },
            "required": ["tickers", "focus_question"],
        },
    },
    {
        "name": "scan_rotation_opportunities",
        "description": (
            "Scansiona un universo AMPIO (~60 ticker cross-sector) per identificare "
            "opportunità di rotazione attiva. Da chiamare SEMPRE quando il summary "
            "rotation incluso in contesto non basta o vuoi filtrare per categoria/RSI. "
            "L'universo copre 9 categorie:\n"
            "  - defensive: utilities, staples, healthcare (XLU, XLP, XLV, KO, JNJ, ...)\n"
            "  - safe_haven: oro, argento, treasuries, USD (GLD, SLV, TLT, IEF, UUP)\n"
            "  - hedge: inverse ETF + volatilità (SH, PSQ, RWM, VIXY)\n"
            "  - geopolitical: defense & aerospace (LMT, RTX, NOC, GD, ITA, ...)\n"
            "  - energy_commodity: energy ETF + petroliferi (XLE, USO, OXY, CVX, ...)\n"
            "  - sectors: gli altri sector ETF (XLK, XLF, XLY, XLI, XLB, XLRE, XLC)\n"
            "  - bonds: corporate IG / HY / aggregate (HYG, LQD, AGG)\n"
            "  - international: Europe, Japan, India, Brazil, China (VGK, EWJ, INDA, EWZ, FXI)\n"
            "  - factor: low-vol, quality, momentum, value (USMV, QUAL, MTUM, VLUE)\n\n"
            "Restituisce per ogni ticker filtrato: price, performance multi-TF (1d/5d/20d/60d), "
            "forza relativa vs SPY (RS5d/RS20d/RS60d in %), RSI14, distanza da 50MA/200MA, "
            "posizione nel range 52-week, volume ratio vs 20d-avg, e rotation_score finale.\n\n"
            "USO TIPICO:\n"
            "  - 'voglio vedere SOLO defensive con RS positivo' → categories=[defensive], min_rs_5d_pct=0\n"
            "  - 'top 10 anti-correlati' → categories=[hedge, safe_haven], top_n=10\n"
            "  - 'tutti con RSI oversold' → rsi_max=35\n"
            "  - 'big picture rotazione tra settori' → categories=[sectors], top_n=11\n"
            "Costo: ~0$ (dati cached 30 min). Chiamabile più volte per filtri diversi."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "categories": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [
                            "defensive", "safe_haven", "hedge", "geopolitical",
                            "energy_commodity", "sectors", "bonds",
                            "international", "factor",
                        ],
                    },
                    "description": "Categorie da scansionare. Default: tutte e 9.",
                },
                "min_rs_5d_pct": {
                    "type": "number",
                    "description": "Filtra solo ticker con forza relativa 5d vs SPY ≥ X%. Es: 0.5 = solo chi batte SPY di almeno 0.5% in 5gg.",
                },
                "min_rs_20d_pct": {
                    "type": "number",
                    "description": "Filtra solo ticker con forza relativa 20d vs SPY ≥ X%.",
                },
                "rsi_max": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 100,
                    "description": "Limite RSI superiore (es. 70 per escludere overbought).",
                },
                "rsi_min": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 100,
                    "description": "Limite RSI inferiore (es. 30 per cercare solo oversold).",
                },
                "top_n": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 60,
                    "description": "Max ticker da ritornare ordinati per rotation_score (default 20).",
                },
                "include_bottom": {
                    "type": "boolean",
                    "description": "Se true include anche i 5 peggiori (per short / ticker da evitare). Default false.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "set_stop_loss",
        "description": (
            "Imposta o aggiorna lo stop-loss AUTOMATICO su una posizione "
            "esistente. Quando il prezzo corrente raggiunge stop_price, il "
            "sistema CHIUDE la posizione automaticamente al prossimo update prezzi. "
            "Passa stop_price=0 per rimuovere lo SL. Errore se la posizione non esiste."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "stop_price": {"type": "number", "description": "Prezzo SL assoluto (>0). 0 = rimuovi."},
                "reason": {"type": "string", "description": "Motivazione tecnica (livello chiave invalidato, ATR multiplier, ecc.)"},
            },
            "required": ["ticker", "stop_price", "reason"],
        },
    },
    {
        "name": "set_take_profit",
        "description": (
            "Imposta o aggiorna il take-profit AUTOMATICO su una posizione "
            "esistente. Quando il prezzo corrente raggiunge target_price, il "
            "sistema CHIUDE automaticamente. Passa target_price=0 per rimuovere."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "target_price": {"type": "number", "description": "Prezzo TP assoluto (>0). 0 = rimuovi."},
                "reason": {"type": "string", "description": "Motivazione (resistenza, livello psicologico, ecc.)"},
            },
            "required": ["ticker", "target_price", "reason"],
        },
    },
    {
        "name": "get_portfolio_state",
        "description": "Ottieni lo stato aggiornato del portafoglio (cash, posizioni con SL/TP impostati, P&L).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "set_commitment",
        "description": (
            "Registra un IMPEGNO/INTENZIONE che il prossimo run del Decision Agent "
            "dovra' ricordare e valutare. Usalo quando dichiari nel reasoning una "
            "promessa tipo: 'monitorero' BTC.D, comprero' ETH se scende sotto 52% "
            "entro 24h', oppure 'attendo FOMC del 12 maggio prima di scaricare "
            "rischio'. Senza questo tool, la promessa scompare alla fine del run "
            "e il prossimo run non sa nulla. Tipi:\n"
            "  - monitor: 'tieni d'occhio X finche' Y'\n"
            "  - conditional_buy: 'compra X se Y entro Z'\n"
            "  - conditional_sell: 'vendi X se Y entro Z'\n"
            "  - watch_event: 'monitora evento (es. CPI, FOMC) e fai Y dopo'\n"
            "  - reminder: 'ricordati di Y al prossimo run'\n"
            "Gli impegni attivi appariranno nel contesto OBIETTIVI ATTIVI dei "
            "run successivi finche' non li risolvi (resolve_commitment) o scadono."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "commitment_type": {
                    "type": "string",
                    "enum": ["monitor", "conditional_buy", "conditional_sell",
                             "watch_event", "reminder"],
                },
                "condition_text": {
                    "type": "string",
                    "description": "Descrizione della condizione/obiettivo (max 2000 char).",
                },
                "trigger_action": {
                    "type": "string",
                    "description": "Azione da intraprendere quando la condizione si verifica (testo libero, es. 'BUY ETH 0.5% portfolio', 'REVIEW posizioni', 'ALERT user').",
                },
                "expires_in_hours": {
                    "type": "number",
                    "minimum": 1,
                    "maximum": 720,
                    "description": "Scadenza in ore (1-720). Tipico: 24-168. Oltre quel periodo l'impegno scade auto.",
                },
                "ticker": {
                    "type": "string",
                    "description": "Ticker rilevante (opzionale, es. 'NVDA', 'BTC-USD').",
                },
            },
            "required": ["commitment_type", "condition_text", "expires_in_hours"],
        },
    },
    {
        "name": "resolve_commitment",
        "description": (
            "Marca un impegno attivo come triggered (condizione soddisfatta, "
            "azione eseguita), cancelled (non piu' rilevante), o expired (manuale). "
            "Usalo quando un OBIETTIVO ATTIVO mostrato nel contesto e' stato "
            "raggiunto o non ha piu' senso (es. il setup tecnico e' decaduto, "
            "il prezzo target e' stato superato, l'evento e' avvenuto)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "commitment_id": {"type": "integer"},
                "resolution_status": {
                    "type": "string",
                    "enum": ["triggered", "cancelled"],
                    "description": "triggered = condizione soddisfatta. cancelled = non piu' rilevante.",
                },
                "resolution_reason": {
                    "type": "string",
                    "description": "Motivazione (es. 'BTC.D sceso a 51.5%, ho comprato ETH come da piano').",
                },
            },
            "required": ["commitment_id", "resolution_status", "resolution_reason"],
        },
    },
    {
        "name": "request_capital_from_orchestrator",
        "description": (
            "Chiede al CAPITAL ORCHESTRATOR (DeepSeek-V3) di liberare capitale "
            "vendendo parzialmente posizioni dell'altro agente (in questo caso "
            "il Decision Crypto). Da usare SOLO se hai una conviction >= 0.70 "
            "su un trade equity ma il cash disponibile non basta perché bloccato "
            "in posizioni crypto.\n\n"
            "L'orchestratore vede entrambi i sub-portfolio e decide se conviene "
            "liquidare parzialmente una posizione crypto (preferendo: profit alto, "
            "età matura >7gg, % NAV alto). Guardrail automatici: cooldown 2h, "
            "max 25% NAV per riallocazione, no-realize-loss > -10%.\n\n"
            "Output del tool:\n"
            "  - approved=true: il cash è già stato liberato. Riprova execute_trade.\n"
            "  - approved=false: usa do_nothing o riduci la size del trade.\n\n"
            "Costo: ~$0.0002 (V3 chat). Non chiamare a sproposito: prima verifica "
            "con get_portfolio_state che il gap sia reale e > $500."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string",
                           "description": "Ticker che vuoi comprare (es. 'NVDA')."},
                "amount_needed_usd": {"type": "number", "exclusiveMinimum": 0,
                                       "description": "USD totale necessario per il trade (qty × price)."},
                "amount_available_usd": {"type": "number", "minimum": 0,
                                          "description": "USD attualmente disponibili come cash (da get_portfolio_state)."},
                "conviction": {"type": "number", "minimum": 0, "maximum": 1,
                                "description": "Conviction del trade richiesto (0.0-1.0). Min 0.70."},
                "reasoning": {"type": "string",
                               "description": "Spiegazione breve (max 500 char) del perché serve capitale e perché vale la pena disturbare l'altro agente."},
            },
            "required": ["ticker", "amount_needed_usd", "amount_available_usd",
                          "conviction", "reasoning"],
        },
    },
]


# ============================================================
# Tool Handler
# ============================================================

async def _handle_decision_tool(tool_name: str, tool_input: dict, run_id: str,
                                  workflow_state: "WorkflowState | None" = None) -> str:
    """Gestisce le chiamate tool del Decision Agent.

    workflow_state: se passato, viene usato per ARRICCHIRE il logic_chain
    di execute_trade con il contenuto di commit_final_thesis (thesis,
    action_plan, primary_risk) — cosi' il reasoning del trade nel DB
    contiene tutto il workflow, non solo la stringa scarsa che il modello
    a volte passa direttamente.
    """
    import data_fetchers
    import database
    import portfolio

    timestamp = datetime.now(timezone.utc).isoformat()

    try:
        # ── Tool del workflow a 4 fasi ─────────────────────────────────────
        if tool_name == "commit_initial_assessment":
            payload = {
                "situation_overview": (tool_input.get("situation_overview") or "")[:6000],
                # rotation_summary: campo obbligatorio della FASE 1 che riassume
                # cosa il modello ha visto nel rotation scan iniettato in contesto.
                "rotation_summary": (tool_input.get("rotation_summary") or "")[:2000],
                "asset_candidates": tool_input.get("asset_candidates") or [],
                "technical_questions": tool_input.get("technical_questions") or [],
            }
            database.insert_agent_log(run_id, "DECISION_PHASE1", json.dumps(payload, default=str))
            return json.dumps({
                "phase": "INITIAL_DONE",
                "ack": "Pre-analisi committata. Procedi con request_technical_analysis "
                       "se hai domande tecniche, altrimenti vai a commit_final_thesis.",
                "questions_count": len(payload["technical_questions"]),
            })

        if tool_name == "commit_final_thesis":
            payload = {
                "thesis": (tool_input.get("thesis") or "")[:6000],
                "action_plan": (tool_input.get("action_plan") or "")[:2000],
                "primary_risk": (tool_input.get("primary_risk") or "")[:2000],
            }
            database.insert_agent_log(run_id, "DECISION_PHASE3", json.dumps(payload, default=str))
            return json.dumps({
                "phase": "FINAL_THESIS_DONE",
                "ack": "Tesi finale committata. Procedi con execute_trade o do_nothing.",
            })

        if tool_name == "execute_trade":
            ticker = tool_input["ticker"]
            action = tool_input["action"]
            quantity = tool_input["quantity"]
            logic_chain = tool_input["logic_chain"]
            confidence = tool_input["confidence_level"]
            # NOTA: usiamo controllo esplicito (non `or None`) per non confondere
            # 0 con None. Schema dichiara: 0 = "nessun SL/TP esplicito", None
            # equivalente. Se valore > 0, applica.
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
            # Il modello a volte passa logic_chain corti e generici nel tool
            # (es. "esegui per breakout"). Il VERO reasoning e' in
            # commit_final_thesis. Lo prependiamo cosi' il trade record nel
            # DB contiene il pensiero completo, non solo lo slot del tool.
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

            logger.info("[%s][DECISION] TRADE: %s %d %s (conf: %.0f%%, SL: %s)",
                        run_id, action, quantity, ticker, confidence, stop_loss)

            # ── Veto crypto: il Decision normale opera SOLO su equity/ETF.
            # Le crypto sono dominio esclusivo del Decision Crypto agent.
            t_up = (ticker or "").upper()
            is_crypto = t_up.endswith("-USD") or t_up.startswith("X:")
            if is_crypto:
                error_msg = (
                    f"REJECTED: '{ticker}' è crypto. Il Decision normale opera "
                    f"SOLO su equity/ETF tradizionali. Le crypto sono dominio "
                    f"del Decision Crypto agent."
                )
                database.insert_agent_log(run_id, "DECISION_REJECTED", json.dumps({
                    "ticker": ticker, "action": action,
                    "reason": "crypto_outside_decision_domain",
                }))
                return json.dumps({"error": error_msg, "rejected": True})

            # Ottieni prezzo corrente FRESCO (bypass cache, anti-stale).
            # PRIMA: usava price_quotes cache con TTL 700s che poteva dare
            # prezzi stale fino a 11 min causando entry/exit a prezzi
            # sbagliati → PnL apparente subito dopo il polling. FIX: forza
            # un fetch live al provider primario per ogni execute_trade.
            current_price = None
            try:
                loop = asyncio.get_running_loop()
                current_price = await loop.run_in_executor(
                    None, data_fetchers.fetch_fresh_current_price, ticker
                )
            except Exception as exc:
                logger.warning("[%s][DEC] fetch_fresh %s fail: %s",
                               run_id, ticker, exc)
            if current_price is None:
                # Fallback ultimo: cache price_quotes (per non bloccare il trade
                # se i provider esterni sono down) — accetta fino a 700s di
                # staleness solo come fallback estremo.
                try:
                    from price_polling import get_cached_prices_bulk
                    cached = get_cached_prices_bulk([ticker], max_age_seconds=700)
                    if cached.get(ticker):
                        current_price = float(cached[ticker]["price"])
                except Exception:
                    pass
            if current_price is None:
                # Ultimissimo fallback: cache OHLCV (puo' essere fino a 5 min stale)
                try:
                    loop = asyncio.get_running_loop()
                    price_data = await loop.run_in_executor(
                        None, data_fetchers.fetch_market_data, ticker, 5
                    )
                    if price_data and price_data.get("data"):
                        current_price = float(price_data["data"][-1]["close"])
                except Exception:
                    pass
            if not current_price or current_price <= 0:
                return json.dumps({"error": f"Impossibile ottenere prezzo per {ticker}"})

            # ── Risk Profile validation (HARD CONSTRAINTS) ─────────────────
            # Solo per BUY (SELL = chiusura, non vincolato dal cap allocation).
            # Confidence è 0..100 nel tool schema → normalizza a 0..1.
            if action == "BUY":
                try:
                    import risk_profile as _rp
                    pstate = portfolio.get_portfolio_state()
                    cash = float(pstate.get("cash", 0) or 0)
                    open_count = int(pstate.get("open_positions_count",
                                                len(pstate.get("positions") or [])) or 0)
                    trade_value = float(quantity) * float(current_price)
                    alloc_pct = (trade_value / cash * 100.0) if cash > 0 else 999.0
                    conf_norm = float(confidence) / 100.0 if confidence and confidence > 1 else float(confidence or 0)

                    # Drawdown proxy: pnl_pct negativo dal capitale iniziale.
                    # Non è il peak-to-trough vero, ma è un soft-stop ragionevole
                    # per il portfolio cap: se sei -X% dall'inizio, freezeing
                    # nuovi BUY è prudente.
                    dd_pct = None
                    try:
                        pnl_pct = float(pstate.get("pnl_pct", 0) or 0)
                        if pnl_pct < 0:
                            dd_pct = abs(pnl_pct)
                    except Exception:
                        pass

                    ok, reason = _rp.validate_trade(
                        asset_class="equity",
                        confidence=conf_norm,
                        allocation_pct=alloc_pct,
                        open_positions_count=open_count,
                        portfolio_drawdown_pct=dd_pct,
                    )
                    if not ok:
                        logger.warning("[%s][DECISION] RISK_PROFILE rejected: %s",
                                       run_id, reason)
                        database.insert_agent_log(run_id, "DECISION_RISK_REJECTED",
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
                    pass   # risk_profile non disponibile → degrade graceful
                except Exception as rp_err:
                    logger.warning("[%s][DECISION] risk validation error (non-fatal): %s",
                                   run_id, rp_err)

            # Esegui trade
            geo_part = logic_chain[:500] if logic_chain else ""
            tech_part = logic_chain[500:] if len(logic_chain) > 500 else ""

            if action == "BUY":
                result = portfolio.execute_buy(
                    ticker, quantity, current_price,
                    geo_part, tech_part, confidence
                )
            else:
                result = portfolio.execute_sell(
                    ticker, quantity, current_price,
                    geo_part, tech_part, confidence
                )

            # FIX CRITICO: se l'execute è fallito (cash insufficiente,
            # no-position, qty<=0...) ritorniamo subito un errore strutturato
            # senza propagare la failure al downstream.
            if not (isinstance(result, dict) and result.get("success")):
                fail_reason = (result or {}).get("reason", "unknown") if isinstance(result, dict) else str(result)
                logger.warning("[%s][DECISION] execute_%s fallito su %s qty=%s: %s",
                               run_id, action.lower(), ticker, quantity, fail_reason)
                database.insert_agent_log(run_id, "DECISION_TRADE_FAILED", json.dumps({
                    "ticker": ticker, "action": action, "qty": quantity,
                    "price": current_price, "reason": fail_reason[:300],
                }, default=str))
                return json.dumps({
                    "executed": False, "ticker": ticker, "action": action,
                    "reason": fail_reason, "at": timestamp,
                }, default=str)

            # Se BUY con SL/TP specificati nel tool: registra anche sui livelli
            # automatici della posizione (cosi' price_polling li triggera).
            if action == "BUY" and (stop_loss or take_profit):
                try:
                    if stop_loss and stop_loss > 0:
                        portfolio.set_stop_loss(ticker, float(stop_loss), run_id=run_id)
                    if take_profit and take_profit > 0:
                        portfolio.set_take_profit(ticker, float(take_profit), run_id=run_id)
                except Exception as exc:
                    logger.warning("[%s][DECISION] auto SL/TP set fallito %s: %s",
                                   run_id, ticker, exc)

            # Salva in trades_high_risk
            _save_high_risk_trade(database, run_id, ticker, action, current_price,
                                  quantity, logic_chain, stop_loss, take_profit, confidence)

            # Log decisione
            database.insert_agent_log(run_id, "DECISION_TRADE",
                json.dumps({
                    "ticker": ticker, "action": action, "qty": quantity,
                    "price": current_price, "confidence": confidence,
                    "stop_loss": stop_loss, "take_profit": take_profit,
                }, default=str))

            return json.dumps({
                "executed": True, "ticker": ticker, "action": action,
                "quantity": quantity, "price": current_price,
                "stop_loss": stop_loss, "take_profit": take_profit,
                "result": result, "at": timestamp,
            }, default=str)

        elif tool_name == "do_nothing":
            reasoning = tool_input["reasoning"]
            # Cap aumentato 1000 → 4000: NO_TRADE reasoning include la
            # rotation analysis ("ho scartato XYZ perché defensive guida")
            # + motivazione regime. 1000 char nascondeva metà argomento.
            database.insert_agent_log(run_id, "DECISION_NO_TRADE",
                json.dumps({"reasoning": reasoning[:4000]}, default=str))
            return json.dumps({"action": "no_trade", "reasoning": reasoning, "at": timestamp})

        elif tool_name == "request_extra_analysis":
            ticker = tool_input["ticker"]
            from agents.technical import _fetch_ticker_indicators
            data = await _fetch_ticker_indicators(ticker)
            database.insert_agent_log(run_id, "DECISION_EXTRA_TA", f"Extra TA: {ticker}")
            return json.dumps(data, default=str)

        elif tool_name == "request_technical_analysis":
            tickers = tool_input.get("tickers") or []
            focus = tool_input.get("focus_question", "")
            if not isinstance(tickers, list) or not tickers:
                return json.dumps({"error": "tickers deve essere una lista non vuota"})
            tickers = [str(t).upper().strip() for t in tickers if t][:5]

            # ── Routing automatico: crypto vs equity ────────────────────────
            # Se l'agente ha una crypto in portfolio (es. BTC-USD aperto da
            # un run precedente), DEVE poterne ricevere indicatori anche
            # tramite il tool standard. Splittiamo la richiesta:
            #   - Equity/ETF → run_technical_analysis (DeepSeek-V3 standard)
            #   - Crypto → run_crypto_technical (DeepSeek-V3 crypto-specialized)
            crypto_tickers = [t for t in tickers
                              if t.endswith("-USD") or t.startswith("X:")]
            equity_tickers = [t for t in tickers if t not in crypto_tickers]

            logger.info("[%s][DECISION] request_technical_analysis: %s "
                        "(equity=%d, crypto=%d) — focus: %s",
                        run_id, tickers, len(equity_tickers),
                        len(crypto_tickers), focus[:80])

            analyses_combined: list[dict] = []
            errors_per_ticker: dict[str, str] = {}
            engines_used: list[str] = []
            summaries: list[str] = []

            # Equity branch
            if equity_tickers:
                try:
                    from agents.technical import run_technical_analysis
                    eq_report = await run_technical_analysis(run_id, equity_tickers)
                    analyses_combined += (eq_report.get("analyses") or [])
                    if eq_report.get("engine"):
                        engines_used.append(f"equity={eq_report['engine']}")
                    if eq_report.get("summary"):
                        summaries.append(f"[EQUITY] {eq_report['summary']}")
                    raw_eq = eq_report.get("raw_indicators") or []
                    analyzed_eq = {a.get("ticker") for a in (eq_report.get("analyses") or [])}
                    for t in equity_tickers:
                        if t not in analyzed_eq:
                            err = "non analizzato"
                            if isinstance(raw_eq, list):
                                for r in raw_eq:
                                    if isinstance(r, dict) and r.get("ticker") == t and r.get("error"):
                                        err = r.get("error", "no data")
                                        break
                            errors_per_ticker[t] = err
                except Exception as exc:
                    logger.error("[%s][DECISION] technical equity failed: %s",
                                 run_id, exc, exc_info=True)
                    for t in equity_tickers:
                        errors_per_ticker[t] = f"technical equity error: {exc}"

            # Crypto branch (auto-routing al Technical Crypto).
            # IMPORTANTE: usa log_phase="TECH_CRYPTO_SIDECALL" per distinguere
            # i log di questa chiamata side dal Technical Crypto vero (quello
            # invocato dalla crypto pipeline standalone). Senza questa
            # distinzione, la timeline del run Standard mostrava log
            # "TECH_CRYPTO" che sembravano della crypto pipeline, creando
            # confusione (mix apparente tra le due pipeline).
            if crypto_tickers:
                try:
                    from agents.technical_crypto import run_crypto_technical
                    cr_report = await run_crypto_technical(
                        run_id, crypto_tickers,
                        log_phase="TECH_CRYPTO_SIDECALL",
                    )
                    analyses_combined += (cr_report.get("analyses") or [])
                    if cr_report.get("engine"):
                        engines_used.append(f"crypto={cr_report['engine']}")
                    if cr_report.get("summary"):
                        summaries.append(f"[CRYPTO] {cr_report['summary']}")
                    analyzed_cr = {a.get("ticker") for a in (cr_report.get("analyses") or [])}
                    pte = cr_report.get("per_ticker_errors") or []
                    for t in crypto_tickers:
                        if t not in analyzed_cr:
                            err = "non analizzato (dati yfinance non disponibili o ticker invalido)"
                            for ent in pte:
                                if isinstance(ent, dict) and ent.get("ticker") == t:
                                    err = ent.get("error", err)
                                    break
                            errors_per_ticker[t] = err
                except Exception as exc:
                    logger.error("[%s][DECISION] technical crypto failed: %s",
                                 run_id, exc, exc_info=True)
                    for t in crypto_tickers:
                        errors_per_ticker[t] = f"technical crypto error: {exc}"

            report = {
                "analyses": analyses_combined,
                "engine": " + ".join(engines_used) or "none",
                "summary": " | ".join(summaries),
                "raw_indicators": [],
            }
            analyses = analyses_combined
            analyzed_tickers = {a.get("ticker") for a in analyses if a.get("ticker")}

            database.insert_agent_log(run_id, "DECISION_REALTIME_TA", json.dumps({
                "tickers_requested": tickers,
                "tickers_analyzed": list(analyzed_tickers),
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
                "raw_indicators_available": bool(report.get("raw_indicators")),
            }, default=str)

        elif tool_name == "scan_rotation_opportunities":
            # ── Rotation scan tool ──────────────────────────────────────────
            # Universo ampio cross-sector con metriche dettagliate per
            # individuare opportunità di rotazione attiva (sempre on, non
            # solo in crisi). Cached 30 min — tipicamente cache-hit.
            try:
                from agents.rotation_scan import (
                    scan_rotation_universe, filter_rotation,
                    format_filtered_for_tool, VALID_CATEGORIES,
                )
                # Parametri (tutti opzionali — defaults sensati)
                raw_cats = tool_input.get("categories") or []
                if isinstance(raw_cats, str):
                    raw_cats = [raw_cats]
                cats = [c for c in raw_cats if c in VALID_CATEGORIES] or None

                min_rs_5d = tool_input.get("min_rs_5d_pct")
                min_rs_20d = tool_input.get("min_rs_20d_pct")
                rsi_max = tool_input.get("rsi_max")
                rsi_min = tool_input.get("rsi_min")
                top_n = tool_input.get("top_n", 20)
                include_bottom = bool(tool_input.get("include_bottom", False))

                # Safety caps
                if top_n is not None:
                    try:
                        top_n = max(1, min(60, int(top_n)))
                    except Exception:
                        top_n = 20

                scan_data = await scan_rotation_universe()
                filtered = filter_rotation(
                    scan_data,
                    categories=cats,
                    min_rs_5d_pct=(float(min_rs_5d) if min_rs_5d is not None else None),
                    min_rs_20d_pct=(float(min_rs_20d) if min_rs_20d is not None else None),
                    rsi_max=(float(rsi_max) if rsi_max is not None else None),
                    rsi_min=(float(rsi_min) if rsi_min is not None else None),
                    top_n=top_n,
                    include_bottom=include_bottom,
                )

                database.insert_agent_log(run_id, "DECISION_ROTATION_SCAN", json.dumps({
                    "categories": cats or "all",
                    "filters": {
                        "min_rs_5d_pct": min_rs_5d,
                        "min_rs_20d_pct": min_rs_20d,
                        "rsi_max": rsi_max,
                        "rsi_min": rsi_min,
                        "top_n": top_n,
                        "include_bottom": include_bottom,
                    },
                    "results_count": filtered.get("filter_count", 0),
                    "top_3_tickers": [r["ticker"] for r in filtered.get("rows", [])[:3]],
                }, default=str))

                return format_filtered_for_tool(filtered)
            except Exception as exc:
                logger.warning("[%s] rotation scan tool error: %s", run_id, exc)
                return json.dumps({
                    "error": f"Rotation scan fallito: {exc}",
                    "hint": "Riprova senza filtri, oppure cala il numero di categorie.",
                })

        elif tool_name == "set_stop_loss":
            ticker = (tool_input.get("ticker") or "").upper().strip()
            stop_price = float(tool_input.get("stop_price") or 0)
            reason = tool_input.get("reason", "")
            result = portfolio.set_stop_loss(ticker, stop_price, run_id=run_id)
            # Log: motivazione AI + vero motivo del fallimento (se c'è).
            # Bug precedente: il log mostrava solo `reason` AI (motivazione)
            # quindi se `success: False` non si capiva PERCHÉ → tutti gli SL
            # falliti senza diagnostica visibile in dashboard.
            database.insert_agent_log(run_id, "DECISION_SET_SL", json.dumps({
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
            database.insert_agent_log(run_id, "DECISION_SET_TP", json.dumps({
                "ticker": ticker, "target_price": target_price,
                "success": result.get("success"),
                "ai_motivation": reason[:300],
                "failure_reason": (result.get("reason", "")[:400]
                                   if not result.get("success") else None),
            }))
            return json.dumps(result, default=str)

        elif tool_name == "get_portfolio_state":
            state = portfolio.get_portfolio_state()
            return json.dumps({"portfolio": state, "at": timestamp}, default=str)

        elif tool_name == "set_commitment":
            try:
                commit_id = database.add_agent_commitment(
                    agent_type="standard",
                    commitment_type=tool_input.get("commitment_type", "reminder"),
                    condition_text=tool_input.get("condition_text", ""),
                    trigger_action=tool_input.get("trigger_action"),
                    expires_in_hours=float(tool_input.get("expires_in_hours") or 48),
                    ticker=tool_input.get("ticker"),
                    source_run_id=run_id,
                )
                database.insert_agent_log(run_id, "DECISION_SET_COMMITMENT", json.dumps({
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
                        "message": "Impegno registrato. Apparira' come OBIETTIVO ATTIVO nei run successivi finche' non lo risolvi o scade.",
                    })
                return json.dumps({"success": False, "error": "DB insert ritornato null"})
            except Exception as e:
                logger.warning("[%s] set_commitment error: %s", run_id, e)
                return json.dumps({"success": False, "error": str(e)[:300]})

        elif tool_name == "resolve_commitment":
            try:
                cid = int(tool_input.get("commitment_id") or 0)
                status = tool_input.get("resolution_status") or "triggered"
                reason = tool_input.get("resolution_reason") or ""
                ok = database.resolve_agent_commitment(cid, status=status, resolved_reason=reason)
                database.insert_agent_log(run_id, "DECISION_RESOLVE_COMMITMENT", json.dumps({
                    "commit_id": cid, "status": status, "reason": reason[:300], "ok": ok,
                }, default=str))
                return json.dumps({"success": bool(ok), "commitment_id": cid, "status": status})
            except Exception as e:
                logger.warning("[%s] resolve_commitment error: %s", run_id, e)
                return json.dumps({"success": False, "error": str(e)[:300]})

        elif tool_name == "request_capital_from_orchestrator":
            # Decision Standard chiama il Capital Orchestrator (DeepSeek-V3) per
            # liberare cash bloccato in posizioni crypto. L'orchestratore decide
            # autonomamente se vale la pena disturbare il Decision Crypto e,
            # se sì, esegue le liquidazioni cross-agent direttamente.
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
                    requesting_agent="standard",
                    ticker=ticker,
                    amount_needed=amount_needed,
                    amount_available=amount_available,
                    conviction=conviction,
                    reasoning=reasoning,
                )
                # Logging semplificato per la sidebar
                database.insert_agent_log(run_id, "DECISION_ORCHESTRATOR_REQUEST",
                    json.dumps({
                        "ticker": ticker,
                        "amount_needed": amount_needed,
                        "approved": result.get("approved"),
                        "amount_freed": result.get("amount_freed", 0),
                        "skipped": result.get("skipped"),
                    }, default=str)
                )
                # Restituisce all'AI un sommario + flag actionable
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
                logger.warning("[%s] orchestrator request error: %s", run_id, e)
                return json.dumps({
                    "approved": False, "error": str(e)[:300],
                    "next_step": "Errore orchestratore: usa do_nothing o execute_trade scalato.",
                })

        else:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})

    except Exception as e:
        logger.error("[%s][DECISION] Tool error %s: %s", run_id, tool_name, e, exc_info=True)
        return json.dumps({"error": str(e), "tool": tool_name})


# ============================================================
# Main Decision Loop
# ============================================================

async def run_decision_agent(run_id: str, tech_report: dict,
                              focus_tickers: list[str] | None = None,
                              watchdog_reason: str | None = None) -> dict:
    """
    Esegue il ciclo decisionale completo del Decision Agent.

    Fasi:
      A. Carica contesto globale (Weekly Matrix + Daily + Buffer + Tech Report)
      B. Valutazione strategica con Opus/Sonnet Extended Thinking
      C. Esecuzione trade tramite tool loop

    Args:
        run_id: ID univoco del run
        tech_report: Report tecnico dal Technical Worker

    Returns:
        DecisionResult con trades eseguiti e reasoning
    """
    import database
    import portfolio
    from agents.scout import (
        get_latest_aggregated_reports, get_recent_buffer,
        TIER_8H, TIER_4D,
    )

    logger.info("[%s][DECISION] === Avvio Decision Agent ===", run_id)
    start_time = datetime.now(timezone.utc)

    # ──────────────────────────────────────────────────────────────
    # Engine selection (hybrid: Claude per market-open, R1 per overnight)
    # Il vecchio gate "market_closed → SKIPPED" è stato RIMOSSO: ora
    # quando il mercato è chiuso, l'engine R1 si attiva automaticamente
    # per gestire le crypto 24/7. L'orchestrator filtra le crypto e
    # applica il cooldown 2h30 prima di chiamarci.
    # ──────────────────────────────────────────────────────────────
    resolved_engine = _resolve_engine_for_run()
    logger.info("[%s][DECISION] Engine risolto: %s", run_id, resolved_engine)

    # Checkpoint
    _save_checkpoint(run_id, "decision", "RUNNING", {"phase": "context_loading"})

    # --- ROTATION SCAN (sempre-on, in parallelo all'ingestione contesto) ---
    # Lancia subito il rotation scan: ~10-20s a cache-miss, istantaneo a
    # cache-hit (TTL 30 min). Awaited dopo che gli altri context fetch
    # sono completi, così la latenza si nasconde.
    rotation_task = None
    try:
        from agents.rotation_scan import scan_rotation_universe as _scan_rot
        rotation_task = asyncio.create_task(_scan_rot())
    except Exception as _rot_exc:
        logger.debug("[%s] rotation scan task launch failed: %s", run_id, _rot_exc)

    # --- FASE A: Ingestione Contesto a Cascata (8H + 4D, no più 3W) ---
    context_loaded = {}

    # 4D (visione macro/medio-lungo termine, top tier; ultimi 2)
    rep_4d = get_latest_aggregated_reports(database, TIER_4D, n=2)
    context_loaded["report_4d"] = len(rep_4d)

    # 8H (breve termine, ultimi 3)
    rep_8h = get_latest_aggregated_reports(database, TIER_8H, n=3)
    context_loaded["report_8h"] = len(rep_8h)

    # Buffer L0 recente (ultimi 40 min) - solo micro-cards, esclude i tier aggregati
    recent_buffer = get_recent_buffer(database, minutes=40)
    context_loaded["intelligence_buffer"] = len(recent_buffer)

    # Stato portafoglio
    portfolio_state = portfolio.get_portfolio_state()
    context_loaded["portfolio"] = True

    # Technical report
    context_loaded["technical"] = bool(tech_report and tech_report.get("analyses"))

    # Documenti tecnici caricati — SOLO categoria 'generic'.
    # I documenti 'crypto' sono dominio del Decision Crypto agent.
    docs = []
    try:
        docs = database.get_document_contents(category="generic")
    except TypeError:
        # Fallback se vecchia firma senza parametro
        try:
            docs = database.get_document_contents()
        except Exception:
            pass
    except Exception:
        pass
    context_loaded["documents"] = len(docs) > 0

    # ═══════════════════════════════════════════════════════════════════
    # EARLY-SKIP: salta la chiamata LLM se il contesto e' INVARIATO dal
    # run precedente. Save ~$0.10/run risparmiato (no input/output token,
    # no tool loop). Si applica solo quando NULLA giustifica una nuova
    # valutazione:
    #   - no watchdog trigger
    #   - nessun nuovo report 8H/4D
    #   - buffer L0 invariato
    #   - portfolio invariato (no posizioni nuove/chiuse, cash invariato)
    #   - nessun commitment in scadenza entro 6h
    # ═══════════════════════════════════════════════════════════════════
    current_signature = _compute_context_signature(
        portfolio_state, rep_8h, rep_4d, recent_buffer,
    )
    skip_run, skip_reason = _should_skip_decision_run(
        current_signature=current_signature,
        watchdog_reason=watchdog_reason,
        focus_tickers=focus_tickers,
        agent_type="standard",
    )
    if skip_run:
        duration = (datetime.now(timezone.utc) - start_time).total_seconds()
        try:
            database.insert_agent_log(run_id, "DECISION_SKIPPED", json.dumps({
                "event": "decision_skipped",
                "reason": skip_reason,
                "signature": current_signature[:12],
                "duration_seconds": round(duration, 1),
                "context_loaded": context_loaded,
            }, default=str))
        except Exception:
            pass
        _save_checkpoint(run_id, "decision", "SKIPPED", {"reason": skip_reason})
        # Salva la signature anche su skip (idempotent: continueremo
        # a skippare finche' qualcosa di concreto cambia)
        _save_decision_signature("standard", current_signature)
        logger.info("[%s][DECISION] SKIP — %s (no LLM call, save ~10k tk)",
                    run_id, skip_reason)
        return {
            "run_id": run_id,
            "decision": "NO_TRADE",
            "trades": [],
            "no_trade_reasoning": f"Run skippato: {skip_reason}. Contesto invariato dal run precedente.",
            "context_loaded": context_loaded,
            "duration_seconds": duration,
            "model": "skipped",
            "iterations": 0,
            "final_response": f"Skip: {skip_reason}",
            "skipped": True,
        }

    # --- Await rotation scan (lanciato in parallelo all'inizio) ---
    # Timeout 25s: a cache-hit è istantaneo; a cache-miss prende 10-20s.
    # Se fallisce/scade, continuiamo senza scan (il tool resta comunque
    # invocabile dal modello).
    rotation_data = None
    rotation_status = "not_started"
    if rotation_task is not None:
        try:
            rotation_data = await asyncio.wait_for(rotation_task, timeout=25.0)
            scanned = (rotation_data or {}).get("tickers_scanned", 0)
            context_loaded["rotation_scan"] = scanned
            rotation_status = "ok" if scanned > 0 else "empty"
            logger.info("[%s][DECISION] rotation scan ready: %d tickers", run_id, scanned)
        except asyncio.TimeoutError:
            logger.warning("[%s][DECISION] rotation scan timeout", run_id)
            context_loaded["rotation_scan"] = "timeout"
            rotation_status = "timeout"
        except Exception as exc:
            logger.warning("[%s][DECISION] rotation scan failed: %s", run_id, exc)
            context_loaded["rotation_scan"] = "error"
            rotation_status = "error"

    # ── Log esplicito DECISION_ROTATION (visibile in dashboard) ────────
    # Permette all'utente di verificare che lo scan sia girato realmente e
    # quali siano le top opportunità di rotazione iniettate nel contesto
    # del Decision Agent. Senza questo log, il rotation scan era invisibile
    # → l'utente non poteva sapere se il modello stesse effettivamente
    # vedendo dati di rotazione.
    try:
        rot_log_payload = {
            "event": "rotation_scan_ready",
            "status": rotation_status,
        }
        if rotation_data and rotation_data.get("rows"):
            top_5 = rotation_data["rows"][:5]
            bottom_5 = sorted(
                rotation_data["rows"],
                key=lambda r: r.get("rotation_score", 0),
            )[:5]
            rot_log_payload.update({
                "tickers_total": rotation_data.get("tickers_total", 0),
                "tickers_scanned": rotation_data.get("tickers_scanned", 0),
                "spy_benchmark": rotation_data.get("spy_benchmark", {}),
                "scan_timestamp_utc": rotation_data.get("scan_timestamp_utc"),
                "top_5_by_score": [
                    {
                        "ticker": r["ticker"],
                        "category": r["category"],
                        "rotation_score": r["rotation_score"],
                        "c5d_pct": r.get("c5d_pct"),
                        "rs5d_vs_spy_pct": r.get("rs5d_vs_spy_pct"),
                        "rs20d_vs_spy_pct": r.get("rs20d_vs_spy_pct"),
                        "rsi14": r.get("rsi14"),
                    } for r in top_5
                ],
                "bottom_5_by_score": [
                    {
                        "ticker": r["ticker"],
                        "category": r["category"],
                        "rotation_score": r["rotation_score"],
                        "c5d_pct": r.get("c5d_pct"),
                        "rs5d_vs_spy_pct": r.get("rs5d_vs_spy_pct"),
                    } for r in bottom_5
                ],
            })
        database.insert_agent_log(run_id, "DECISION_ROTATION",
            json.dumps(rot_log_payload, default=str))
    except Exception as _e:
        logger.debug("[%s] DECISION_ROTATION log failed: %s", run_id, _e)

    # --- Costruisci messaggio utente ---
    user_message = _build_context_message(
        rep_4d, rep_8h, recent_buffer, tech_report, portfolio_state, docs,
        focus_tickers=focus_tickers, watchdog_reason=watchdog_reason,
        rotation_data=rotation_data,
    )

    database.insert_agent_log(run_id, "DECISION_CONTEXT",
        json.dumps({
            "context_loaded": context_loaded,
            "buffer_size": len(recent_buffer),
            "watchdog_focus": focus_tickers or [],
            "watchdog_reason": (watchdog_reason or "")[:200],
        }))

    # --- FASE B+C: Valutazione e Esecuzione ---
    _save_checkpoint(run_id, "decision", "RUNNING", {"phase": "evaluation"})

    # ── Branch DeepSeek-R1 (overnight crypto / tournament) ────────────────
    # Attivato quando resolved_engine == 'deepseek-r1':
    #   - DECISION_ENGINE=deepseek-r1   (override esplicito, usato da GR1)
    #   - DECISION_ENGINE=hybrid + market closed (default GEO overnight)
    if resolved_engine == "deepseek-r1":
        logger.info("[%s][DECISION] Engine: DeepSeek-R1 (overnight/tournament)", run_id)
        sys_prompt_r1, coach_card_ids_r1 = _get_decision_prompt_with_meta("deepseek-r1")
        try:
            trades_executed, final_text, iteration = await _run_deepseek_decision_loop(
                run_id, sys_prompt_r1, user_message
            )
            used_model = DEEPSEEK_R1_MODEL
            # Registra timestamp per cooldown 2h30 (solo se il run ha completato senza errore)
            record_r1_run_timestamp()
            # Increment applied_count delle Coach Cards iniettate (track ROI
            # delle card: piu' applied_count = card piu' usata dal Decision)
            try:
                from agents import coach_cards as _cc
                for _cid in coach_card_ids_r1:
                    _cc.increment_applied(_cid)
            except Exception:
                pass
        except Exception as ds_err:
            logger.error("[%s][DECISION] DeepSeek-R1 fallito: %s", run_id, ds_err, exc_info=True)
            # Fail-safe: ritorna no-trade, non crashare l'intero pipeline
            trades_executed, final_text, iteration = [], f"DeepSeek-R1 error: {ds_err}", 0
            used_model = "deepseek-r1-error"

        duration = (datetime.now(timezone.utc) - start_time).total_seconds()
        try:
            p = database.get_portfolio()
            if p:
                database.insert_portfolio_snapshot(p["total_value"], p["cash_balance"])
        except Exception:
            pass
        database.insert_agent_log(run_id, "DECISION_COMPLETE", json.dumps({
            "event": "decision_complete", "model": used_model,
            "iterations": iteration, "trades_executed": len(trades_executed),
            "duration_seconds": round(duration, 1),
            "final_text": final_text[:8000],
        }, default=str))
        _save_checkpoint(run_id, "decision", "COMPLETED", {
            "trades": len(trades_executed), "duration": duration,
        })
        # Salva signature post-run (per early-skip al prossimo run se nulla cambia)
        _save_decision_signature("standard", current_signature)
        logger.info("[%s][DECISION] [R1] Completato in %.1fs, %d trades, %d iterazioni",
                    run_id, duration, len(trades_executed), iteration)
        return {
            "run_id": run_id,
            "decision": "TRADE" if trades_executed else "NO_TRADE",
            "trades": trades_executed,
            "no_trade_reasoning": final_text if not trades_executed else "",
            "context_loaded": context_loaded,
            "duration_seconds": duration,
            "model": used_model,
            "iterations": iteration,
            "final_response": final_text,
        }
    # ── Fine branch DeepSeek-R1 ─────────────────────────────────────────────

    model = _select_model()
    client = _get_client()
    # ═══════════════════════════════════════════════════════════════════
    # PROMPT CACHING (Anthropic): system prompt e tools splittati in
    # blocco STATICO (cacheable, TTL 1h) e blocco DINAMICO (no cache).
    # Cache hit costa il 10% del token normale → risparmio ~25-35% sui
    # costi totali del Decision Agent.
    #
    # Static (~7-8k tk): risk_block + regime_block + coach + base + shared
    # Dynamic (~2k tk):  directives + risk_state + recent_decisions
    # Tools (~2.7k tk):  cachati via cache_control sull'ultimo tool
    # ═══════════════════════════════════════════════════════════════════
    static_block, dynamic_block, coach_card_ids_claude = _get_decision_prompt_split("claude")

    # Costruisci system come lista di blocchi typed per il cache.
    # NB: l'ordine static→dynamic e' importante per il caching (i blocchi
    # cacheable devono venire PRIMA di quelli che cambiano).
    #
    # TTL extended 1h: cache_control con "ttl": "1h" mantiene viva la
    # cache per un'ora intera invece di 5 minuti (default). Write cost
    # +100% (vs +25% del default 5min), hit cost invariato (10% normale).
    # Conviene quando i run sono distanziati di piu' di 5 min ma meno di 1h
    # (caso tipico del nostro bot: pipeline ogni 20-30 min).
    # Richiede beta header 'extended-cache-ttl-2025-04-11' (vedi _create_message).
    system_blocks: list[dict] = [
        {
            "type": "text",
            "text": static_block,
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        },
    ]
    if dynamic_block and dynamic_block.strip():
        system_blocks.append({"type": "text", "text": dynamic_block})

    # Tools: cache_control sull'ultimo tool marca TUTTI i tools come
    # cacheable (regola Anthropic). I 12 tool DECISION_TOOLS sono ~2.7k
    # token statici → ottimo candidato per il cache.
    cached_tools: list[dict] = []
    if DECISION_TOOLS:
        cached_tools = list(DECISION_TOOLS[:-1])
        last_tool = dict(DECISION_TOOLS[-1])
        last_tool["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
        cached_tools.append(last_tool)

    # CRITICO: Anthropic SDK è sincrono → wrap in asyncio.to_thread per non
    # bloccare l'event loop (evita di fermare polling, watchdog, ecc.).
    messages = [{"role": "user", "content": user_message}]

    # Beta header per TTL extended 1h. Senza questo header il backend
    # Anthropic ignora il campo "ttl" e usa il TTL default (5 min).
    _BETA_HEADERS = {"anthropic-beta": "extended-cache-ttl-2025-04-11"}

    def _create_message(model_id: str, max_tokens: int, with_tools: bool = True):
        kwargs = dict(
            model=model_id,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=messages,
            extra_headers=_BETA_HEADERS,
        )
        if with_tools:
            kwargs["tools"] = cached_tools or DECISION_TOOLS
        return client.messages.create(**kwargs)

    # ═══════════════════════════════════════════════════════════════════
    # WORKFLOW A 4 FASI: il modello deve passare attraverso
    # commit_initial_assessment → request_technical_analysis →
    # commit_final_thesis → execute_trade/do_nothing.
    # Lo state machine in decision_workflow.py rifiuta tool out-of-order
    # con un tool_result error che il modello legge e usa per riprovare.
    # ═══════════════════════════════════════════════════════════════════
    workflow_state = WorkflowState()

    try:
        # max_tokens: 5000 (era 8000). Il workflow 4-fasi tipicamente
        # genera 2-4k token totali; 5000 lascia margine senza sprecare
        # budget. Output costa $15/M token → ogni 1000 tk = $0.015/run.
        response = await asyncio.to_thread(_create_message, model, 5000)
        used_model = model
    except Exception as model_err:
        # Circuit breaker: se è un errore di auth/quota, NON tentare il
        # fallback (sprecherebbe altri token). Errori tipici:
        #   - 401 Unauthorized: chiave invalida
        #   - 402 Payment Required / "credit balance is too low"
        #   - 529 Overloaded: anthropic congestionato
        err_str = str(model_err).lower()
        is_auth_quota = any(s in err_str for s in [
            "401", "402", "429", "quota", "credit balance",
            "insufficient", "billing", "rate_limit",
        ])
        if is_auth_quota:
            logger.error("[%s][DECISION] Anthropic auth/quota error (%s) — "
                         "salto fallback model per non sprecare token",
                         run_id, model_err)
            # Re-raise → orchestrator catcha → DECISION_ERROR log → throttle
            raise

        if model == DECISION_MODEL:
            logger.warning("[%s][DECISION] %s non disponibile (%s), fallback a %s",
                           run_id, DECISION_MODEL, model_err, DECISION_MODEL_FALLBACK)
            model = DECISION_MODEL_FALLBACK
            response = await asyncio.to_thread(_create_message, model, 5000)
            used_model = model
        else:
            raise

    # Logga uso token + cache stats. Anthropic ritorna in usage:
    #   input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens
    # Cache hit = cache_read_input_tokens > 0 → save 90% sui token cacheati.
    try:
        usage = getattr(response, "usage", None)
        if usage:
            cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
            cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
            input_tk = getattr(usage, "input_tokens", 0) or 0
            output_tk = getattr(usage, "output_tokens", 0) or 0
            logger.info(
                "[%s][DECISION] tokens: input=%d output=%d cache_read=%d cache_create=%d (cache hit ratio=%.0f%%)",
                run_id, input_tk, output_tk, cache_read, cache_create,
                (cache_read / max(cache_read + input_tk, 1)) * 100,
            )
    except Exception:
        pass

    database.insert_agent_log(run_id, "DECISION_MODEL",
        json.dumps({"model": used_model, "initial_stop_reason": response.stop_reason}))

    # --- Tool Loop con state machine ---
    iteration = 0
    max_iterations = 15
    trades_executed = []

    while response.stop_reason == "tool_use" and iteration < max_iterations:
        iteration += 1
        logger.info("[%s][DECISION] Iter %d phase=%s tech_calls=%d",
                    run_id, iteration, workflow_state.phase,
                    workflow_state.tech_request_count)

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            # Validazione workflow phase
            allowed, err = can_call_tool(workflow_state, block.name)
            if not allowed:
                workflow_state.rejected_calls.append({
                    "tool": block.name, "reason": err[:200],
                })
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": make_rejection_result(err, workflow_state),
                    "is_error": True,
                })
                continue

            # Validazione contenuto (lunghezza minima testi commit + logic_chain)
            content_ok, content_err = validate_commit_input(block.name, block.input)
            if not content_ok:
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": make_rejection_result(content_err, workflow_state),
                    "is_error": True,
                })
                continue

            # Esegui il tool — passiamo workflow_state per arricchire il
            # logic_chain di execute_trade con il commit_final_thesis.
            result = await _handle_decision_tool(
                block.name, block.input, run_id, workflow_state=workflow_state,
            )
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result,
            })

            # Aggiorna state machine post-execution
            workflow_state = apply_tool_transition(
                workflow_state, block.name, block.input, result
            )

            # Track executed trades
            if block.name == "execute_trade":
                try:
                    parsed = json.loads(result)
                    if parsed.get("executed"):
                        trades_executed.append({
                            "ticker": block.input.get("ticker"),
                            "action": block.input.get("action"),
                            "quantity": block.input.get("quantity"),
                            "confidence": block.input.get("confidence_level"),
                        })
                except Exception:
                    pass

        messages.append({"role": "user", "content": tool_results})

        # Wrap in to_thread per non bloccare l'event loop durante il tool loop
        # (15 iterazioni × ~10s = 2-3 min di blocking se non wrappato).
        # max_tokens: 8000 (era 16000) — sufficienti per tool calls + reasoning
        # finale; 16000 raramente saturato, era spreco di budget.
        response = await asyncio.to_thread(_create_message, used_model, 8000)

    # Log finale workflow state per diagnostica
    database.insert_agent_log(run_id, "DECISION_WORKFLOW", json.dumps(
        workflow_state.to_dict(), default=str))

    # --- Estrai risposta finale ---
    final_text = ""
    for block in response.content:
        if hasattr(block, "text"):
            final_text += block.text

    duration = (datetime.now(timezone.utc) - start_time).total_seconds()

    # Portfolio snapshot
    try:
        p = database.get_portfolio()
        if p:
            database.insert_portfolio_snapshot(p["total_value"], p["cash_balance"])
    except Exception:
        pass

    # Log RAGIONAMENTO unificato — la dashboard lo riconosce come summary
    # del processo decisionale, indipendentemente dal workflow phases.
    # Compone: situation_overview + thesis + final_text in un blocco coeso.
    try:
        ws = workflow_state.to_dict() if 'workflow_state' in locals() else {}
        ia = (workflow_state.initial_assessment or {}) if 'workflow_state' in locals() else {}
        ft = (workflow_state.final_thesis or {}) if 'workflow_state' in locals() else {}
        reasoning_text = _build_reasoning_text(ia, ft, final_text)
        # Cap field aumentati: situation_overview 1500→4000, thesis
        # 2000→5000, action_plan 1000→3000, primary_risk 1000→3000.
        # I cap precedenti tagliavano contenuto nelle card dashboard.
        reasoning_summary = {
            "model": used_model,
            "phase_state": ws.get("phase"),
            "reasoning_text": reasoning_text,   # campo letto dal frontend
            "situation_overview": (ia.get("situation_overview") or "")[:4000],
            "rotation_summary": (ia.get("rotation_summary") or "")[:2000],
            "asset_candidates": ia.get("asset_candidates") or [],
            "technical_questions": ia.get("technical_questions") or [],
            "thesis": (ft.get("thesis") or "")[:5000],
            "action_plan": (ft.get("action_plan") or "")[:3000],
            "primary_risk": (ft.get("primary_risk") or "")[:3000],
            "final_text": final_text[:8000],
            "trades": len(trades_executed),
        }
        database.insert_agent_log(run_id, "DECISION_REASONING",
            json.dumps(reasoning_summary, default=str))
    except Exception as _e:
        logger.debug("[%s][DECISION] reasoning log failed: %s", run_id, _e)

    # Log finale
    database.insert_agent_log(run_id, "DECISION_COMPLETE",
        json.dumps({
            "event": "decision_complete",
            "model": used_model,
            "iterations": iteration,
            "trades_executed": len(trades_executed),
            "duration_seconds": round(duration, 1),
            "final_text": final_text[:8000],
        }, default=str))

    # Registra timestamp Sonnet per la sidebar (Decision row)
    record_sonnet_run_timestamp()

    # Increment applied_count delle Coach Cards iniettate nel prompt.
    # Eseguito SOLO dopo che il run e' completato (no fail-fast).
    try:
        from agents import coach_cards as _cc
        for _cid in coach_card_ids_claude:
            _cc.increment_applied(_cid)
    except Exception:
        pass

    _save_checkpoint(run_id, "decision", "COMPLETED", {
        "trades": len(trades_executed),
        "duration": duration,
    })

    # Salva signature post-run per early-skip al prossimo run identico.
    # Salva ANCHE se ci sono stati trade: lo stato portfolio e' cambiato,
    # quindi la signature del prossimo run sara' diversa → no skip
    # accidentale. Il signature attuale resta comunque utile come baseline.
    _save_decision_signature("standard", current_signature)

    logger.info("[%s][DECISION] === Completato in %.1fs, %d trades, %d iterazioni ===",
                run_id, duration, len(trades_executed), iteration)

    return {
        "run_id": run_id,
        "decision": "TRADE" if trades_executed else "NO_TRADE",
        "trades": trades_executed,
        "no_trade_reasoning": final_text if not trades_executed else "",
        "context_loaded": context_loaded,
        "duration_seconds": duration,
        "model": used_model,
        "iterations": iteration,
        "final_response": final_text,
    }


# ============================================================
# DeepSeek-R1 Decision Loop (variante tournament, API OpenAI-compatible)
# ============================================================

async def _run_deepseek_decision_loop(
    run_id: str, system_prompt: str, user_message: str
) -> tuple[list, str, int]:
    """
    Tool loop per DeepSeek-R1 tramite API OpenAI-compatible di DeepSeek.

    Differenze chiave vs Anthropic SDK:
    - finish_reason == "tool_calls"  (Anthropic: "tool_use")
    - tool call in response["choices"][0]["message"]["tool_calls"]
    - tool result: {"role": "tool", "tool_call_id": tc["id"], "content": ...}
    - tc["function"]["arguments"] è una STRINGA JSON → json.loads() obbligatorio
    - R1 genera token di reasoning (<think>...</think>) nella content → da strippare

    Ritorna: (trades_executed_list, final_text, n_iterations)
    """
    api_key = _get_deepseek_key()
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY non configurata — impossibile avviare motore DeepSeek-R1")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    trades_executed: list[dict] = []
    iteration = 0
    max_iterations = 10   # R1 è più lento di Sonnet, riduco da 15 a 10
    final_text = ""

    # ─── State machine workflow a 4 fasi ────────────────────────────────────
    workflow_state = WorkflowState()

    async with aiohttp.ClientSession() as session:
        while iteration < max_iterations:
            payload = {
                "model": DEEPSEEK_R1_MODEL,
                "messages": messages,
                "tools": _tools_for_openai(),
                "tool_choice": "auto",
                "max_tokens": 8000,
            }
            async with session.post(
                DEEPSEEK_API_URL, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise ValueError(f"DeepSeek HTTP {resp.status}: {body[:300]}")
                data = await resp.json()

            choice = data["choices"][0]
            finish_reason = choice.get("finish_reason", "")
            msg = choice["message"]

            # Estrai testo finale (stripping token di reasoning <think>...</think> di R1)
            raw_text = msg.get("content") or ""
            final_text = _re.sub(r"<think>.*?</think>", "", raw_text, flags=_re.DOTALL).strip()

            # Nessuna tool call → ciclo terminato
            if finish_reason != "tool_calls" or not msg.get("tool_calls"):
                break

            iteration += 1
            logger.info("[%s][DECISION-R1] Iter %d phase=%s tech_calls=%d tools=%d",
                        run_id, iteration, workflow_state.phase,
                        workflow_state.tech_request_count, len(msg["tool_calls"]))

            # Aggiungi risposta dell'assistente alla storia della conversazione
            messages.append({
                "role": "assistant",
                "content": msg.get("content"),   # può essere None in R1
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
                result = await _handle_decision_tool(
                    tool_name, tool_input, run_id, workflow_state=workflow_state,
                )

                # Aggiorna state machine
                workflow_state = apply_tool_transition(
                    workflow_state, tool_name, tool_input, result
                )

                # Track trade eseguiti
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

                # Tool result nel formato OpenAI (role=tool, non Anthropic's role=user+type=tool_result)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

    # Log finale workflow state per diagnostica
    try:
        import database
        database.insert_agent_log(run_id, "DECISION_WORKFLOW", json.dumps(
            workflow_state.to_dict(), default=str))
        # Log unificato DECISION_REASONING per dashboard
        ia = workflow_state.initial_assessment or {}
        ft = workflow_state.final_thesis or {}
        database.insert_agent_log(run_id, "DECISION_REASONING", json.dumps({
            "model": DEEPSEEK_R1_MODEL,
            "phase_state": workflow_state.phase,
            "reasoning_text": _build_reasoning_text(ia, ft, final_text),
            # Cap field aumentati per evitare troncamento nelle card UI
            "situation_overview": (ia.get("situation_overview") or "")[:4000],
            "rotation_summary": (ia.get("rotation_summary") or "")[:2000],
            "asset_candidates": ia.get("asset_candidates") or [],
            "technical_questions": ia.get("technical_questions") or [],
            "thesis": (ft.get("thesis") or "")[:5000],
            "action_plan": (ft.get("action_plan") or "")[:3000],
            "primary_risk": (ft.get("primary_risk") or "")[:3000],
            "final_text": final_text[:8000],
            "trades": len(trades_executed),
        }, default=str))
    except Exception:
        pass

    return trades_executed, final_text, iteration


# ============================================================
# Context Builder
# ============================================================

def _build_context_message(rep_4d, rep_8h, buffer, tech_report, portfolio_state, docs,
                           focus_tickers: list[str] | None = None,
                           watchdog_reason: str | None = None,
                           rotation_data: dict | None = None) -> str:
    """
    Costruisce il messaggio di contesto per il Decision Agent.
    Riceve liste di report aggregati (sistema cascata 4D/8H).

    Se il run e' stato triggerato dal Watchdog, focus_tickers contiene i
    ticker che hanno scatenato il trigger (es. NVDA, GLD, QQQ con +5%, +3%,
    +2% in 5 min). Questi DEVONO essere il punto di partenza dell'analisi
    del Decision Agent — non possono essere ignorati senza giustificazione.

    rotation_data: output di scan_rotation_universe() — universo ampio
    cross-sector con metriche dettagliate. Iniettato come blocco visibile
    nel contesto cosi' il modello vede SEMPRE le opportunità di rotazione
    (non solo durante regimi di crisi).
    """
    parts = []

    # === ROTATION SCAN BLOCK (sempre-on) ===
    # In testa al contesto (subito dopo questa nota), così è la PRIMA cosa
    # che il modello legge dopo gli obiettivi attivi/direttive. Mostra forza
    # relativa cross-sector — segnala flow su defensive/safe-haven/energy/
    # defense/etc. che la watchlist standard ignorerebbe.
    if rotation_data and rotation_data.get("rows"):
        try:
            from agents.rotation_scan import format_rotation_for_prompt
            rot_block = format_rotation_for_prompt(rotation_data, top_n=18)
            if rot_block:
                parts.append(rot_block)
        except Exception as _e:
            logger.debug("rotation block format failed: %s", _e)

    # === OBIETTIVI ATTIVI (impegni dei run precedenti) ===
    # Memoria persistente: gli impegni che il Decision Agent stesso ha preso
    # in run passati appaiono qui finche' non sono triggered/cancelled/expired.
    try:
        import database as _db
        if hasattr(_db, "get_active_agent_commitments"):
            commitments = _db.get_active_agent_commitments("standard", limit=15)
            if commitments:
                from datetime import datetime as _dt2, timezone as _tz2
                c_lines = ["=" * 60]
                c_lines.append("OBIETTIVI ATTIVI (impegni presi in run precedenti)")
                c_lines.append("=" * 60)
                c_lines.append(
                    "Sono promesse/intenzioni che TU stesso hai registrato in run "
                    "passati. Per ognuno, valuta se la condizione e' soddisfatta:"
                )
                c_lines.append(
                    "  • se SI: usa execute_trade come previsto e poi resolve_commitment(triggered)\n"
                    "  • se NO ma ancora rilevante: lascialo attivo, non serve azione\n"
                    "  • se NON PIU' RILEVANTE (setup decaduto): resolve_commitment(cancelled, motivazione)"
                )
                c_lines.append("")
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
                                exp_str = " [scaduto, sweep imminente]"
                        except Exception:
                            pass
                    line = f"  • [ID:{cid}] {ctype.upper()}"
                    if tk:
                        line += f" {tk}"
                    line += f"{exp_str}\n     condizione: {cond}"
                    if trig:
                        line += f"\n     trigger: {trig}"
                    c_lines.append(line)
                c_lines.append("")
                parts.append("\n".join(c_lines))
    except Exception as _e:
        logger.debug("Non riesco a caricare commitments: %s", _e)

    # === DIRETTIVE UTENTE RECENTI (chat decision) ===
    # Iniettiamo gli ultimi messaggi dell'utente dalla chat-decision standard
    # come "preferenze morbide" che il Decision Agent rispetta a meno che
    # i segnali tecnici/fondamentali siano forti in senso contrario.
    try:
        import database as _db
        if hasattr(_db, "get_recent_user_directives"):
            directives = _db.get_recent_user_directives("standard", hours=48, limit=5)
            if directives:
                d_lines = ["=" * 60]
                d_lines.append("📋  DIRETTIVE UTENTE RECENTI (da chat, ultime 48h)")
                d_lines.append("=" * 60)
                d_lines.append(
                    "L'utente ha espresso queste preferenze nella chat decision standard. "
                    "Trattale come SOFT GUARDRAILS: rispettale a meno che i segnali "
                    "tecnici/fondamentali siano fortemente contrari. In quel caso, cita "
                    "la direttiva e spiega perche' la contraddici."
                )
                d_lines.append("")
                for d in directives:
                    content = (d.get("content") or "").strip()
                    when = str(d.get("created_at", ""))[:16]
                    if content:
                        d_lines.append(f"  • [{when}] {content[:300]}")
                d_lines.append("")
                parts.append("\n".join(d_lines))
    except Exception as _e:
        logger.debug("Non riesco a caricare direttive utente: %s", _e)

    # === WATCHDOG TRIGGER (in cima, alta priorita') ===
    if focus_tickers or watchdog_reason:
        wd_lines = ["=" * 60]
        # Rilevazione speciale: rebalance trigger (dal Watchdog quando una
        # posizione eccede il 35% del NAV). Riceve istruzioni di SELL
        # parziale invece di BUY/analisi.
        is_rebalance = bool(watchdog_reason and watchdog_reason.upper().startswith("REBALANCE"))
        if is_rebalance:
            wd_lines.append("⚠️  WATCHDOG REBALANCE TRIGGER — RIDUZIONE RISCHIO OBBLIGATORIA ⚠️")
        else:
            wd_lines.append("⚠️  WATCHDOG TRIGGER — INPUT PRIORITARIO ⚠️")
        wd_lines.append("=" * 60)
        if watchdog_reason:
            wd_lines.append(f"Motivo: {watchdog_reason}")
        if focus_tickers:
            label = ("POSIZIONE OVERWEIGHT da ridurre"
                     if is_rebalance else "FOCUS TICKERS (rilevati con movimento anomalo)")
            wd_lines.append(f"{label}: {', '.join(focus_tickers)}")
        wd_lines.append("")
        if is_rebalance:
            # Istruzioni specifiche per il rebalance: il LLM DEVE proporre
            # un SELL parziale per riportare la concentrazione sotto soglia.
            # NON deve fare ulteriori BUY su questo ticker (sarebbe controproducente).
            wd_lines.append("ISTRUZIONI OBBLIGATORIE — REBALANCE (priorita' max):")
            wd_lines.append(
                "  1. La posizione mostrata sopra ha superato il 35% del NAV "
                "    per via della crescita. Concentrazione eccessiva = rischio "
                "    non controllato."
            )
            wd_lines.append(
                "  2. ESEGUI un SELL PARZIALE per riportare la posizione "
                "    sotto il 30% del NAV (trim ~25-40% della quantita' attuale, "
                "    a tua discrezione in base ai segnali tecnici)."
            )
            wd_lines.append(
                "  3. NON aggiungere mai a questa posizione (no BUY) finche' "
                "    rimane sopra-soglia: sarebbe il contrario del rebalance."
            )
            wd_lines.append(
                "  4. Se hai motivi tecnici fortissimi per NON ridurre "
                "    (es. breakout strutturale appena confermato con volumi 3x), "
                "    documentalo in commit_final_thesis e usa do_nothing."
            )
            wd_lines.append(
                "  5. Workflow standard 4 fasi obbligatorio: "
                "    FASE 1 (commit_initial_assessment) → FASE 2 (request_technical) "
                "    → FASE 3 (commit_final_thesis) → FASE 4 (execute_trade SELL)."
            )
        else:
            wd_lines.append("ISTRUZIONI OBBLIGATORIE:")
            wd_lines.append(
                "  1. Includi TUTTI i FOCUS TICKERS in asset_candidates "
                "    di FASE 1 (commit_initial_assessment)."
            )
            wd_lines.append(
                "  2. Le tue technical_questions DEVONO riguardare PRIMA i FOCUS "
                "    TICKERS, poi eventuali altri ticker correlati al tema."
            )
            wd_lines.append(
                "  3. Il run e' stato attivato perche' QUESTI ticker si sono "
                "    mossi: ignorarli per analizzarne altri e' uno spreco "
                "    della trigger window."
            )
            wd_lines.append(
                "  4. Se decidi di NON operare su un focus ticker, devi "
                "    motivarlo esplicitamente nel commit_final_thesis "
                "    (es. 'NVDA gia' overbought, attendo pullback')."
            )
        wd_lines.append("=" * 60)
        parts.append("\n".join(wd_lines))

    # === Report 4D (visione macro/medio termine, ultimi 2) ===
    if rep_4d:
        lines = []
        for r in rep_4d[:2]:
            rep = r["report"]
            lines.append(
                f"\n--- {r.get('timestamp', '?')} | Bias: {rep.get('macro_bias', '?')} ---\n"
                f"{rep.get('summary_text', '')[:500]}\n"
                f"Trend consolidati: {', '.join(rep.get('consolidating_trends', []))[:300]}\n"
                f"Rischi accumulo: {', '.join(rep.get('accumulating_risks', []))[:300]}\n"
                f"Strategia 4d: {rep.get('next_4d_strategy', '')[:300]}"
            )
        parts.append(f"=== REPORT 4D (ultimi {len(rep_4d)}) ===" + "".join(lines))
    else:
        parts.append("=== REPORT 4D === Non ancora disponibili")

    # === Report 8H (breve termine, ultimi 3) ===
    if rep_8h:
        lines = []
        for r in rep_8h[:3]:
            rep = r["report"]
            lines.append(
                f"\n--- {r.get('timestamp', '?')} | Bias: {rep.get('macro_bias', '?')} ---\n"
                f"{rep.get('summary_text', '')[:400]}\n"
                f"Hot tickers: {', '.join(rep.get('hot_tickers', []))[:200]}\n"
                f"Catalisti next 8h: {', '.join(rep.get('next_8h_catalysts', []))[:300]}"
            )
        parts.append(f"=== REPORT 8H (ultimi {len(rep_8h)}) ===" + "".join(lines))
    else:
        parts.append("=== REPORT 8H === Non ancora disponibili")

    # === Buffer L0 recente (micro-cards ultimi 40 min) ===
    if buffer:
        buf_text = ""
        # Limit 8 (era 15): le 8 micro-cards piu' recenti coprono i segnali
        # caldi senza inflare l'input. Le restanti sono gia' sintetizzate
        # nel report 8H. Save ~1.5k token/run.
        for b in buffer[:8]:
            buf_text += f"\n[{b.get('source_type', '?')}] {b.get('micro_summary', b.get('raw_content', '')[:200])}"
        parts.append(f"=== INTELLIGENCE BUFFER L0 (ultimi 40 min, {len(buffer)} micro-cards, top 8) ==={buf_text}")
    else:
        parts.append("=== INTELLIGENCE BUFFER L0 === Vuoto (ultime micro-cards consumate dal report 8H)")

    # Technical Report — RIMOSSO dal contesto iniziale.
    # Il workflow a 4 fasi richiede che sia il Decision Agent stesso a
    # richiederlo via tool (request_technical_analysis) durante FASE 2.
    parts.append(
        "=== TECHNICAL REPORT === NON fornito a priori. Devi richiederlo "
        "tu via tool request_technical_analysis durante FASE 2 del workflow "
        "obbligatorio. Vedi WORKFLOW_PHASES nel system prompt."
    )

    # Portfolio
    parts.append(f"""=== PORTAFOGLIO ===
Cash: {portfolio_state.get('cash', 0):,.2f}
Valore totale: {portfolio_state.get('total_value', 0):,.2f}
P&L: {portfolio_state.get('pnl', 0):,.2f} ({portfolio_state.get('pnl_pct', 0):.2f}%)
Posizioni aperte: {portfolio_state.get('open_positions_count', 0)}
{json.dumps(portfolio_state.get('positions', [])[:5], default=str, ensure_ascii=False)[:1000]}""")

    # Documenti
    if docs:
        doc_text = ""
        for d in docs[:2]:
            doc_text += f"\n### {d.get('filename', '?')}\n{d.get('content', '')[:800]}\n"
        parts.append(f"=== DOCUMENTI STRATEGIA ==={doc_text}")

    parts.append(f"\nTimestamp: {datetime.now(timezone.utc).isoformat()}")
    parts.append("\nAnalizza tutto il contesto e prendi le decisioni di trading appropriate. Usa execute_trade per operare o do_nothing se preferisci attendere.")

    return "\n\n".join(parts)


# ============================================================
# Helpers
# ============================================================

def _save_high_risk_trade(database, run_id, ticker, action, price, qty,
                           logic_chain, stop_loss, take_profit, confidence):
    """Salva trade in trades_high_risk su Supabase."""
    try:
        client = database.get_client()
        if client:
            client.table("trades_high_risk").insert({
                "ticker": ticker,
                "action": action,
                "entry_price": price,
                "quantity": qty,
                "logic_chain": logic_chain,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "confidence_level": confidence,
                "run_id": run_id,
                "status": "OPEN",
            }).execute()
    except Exception as e:
        logger.warning("Errore salvataggio trades_high_risk: %s", e)


def _save_checkpoint(run_id: str, agent_name: str, status: str, data: dict):
    """Salva checkpoint per Render resilience."""
    try:
        import database
        client = database.get_client()
        if client:
            client.table("agent_checkpoints").upsert({
                "run_id": run_id,
                "agent_name": agent_name,
                "status": status,
                "checkpoint_data": data,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
    except Exception:
        pass


# ═════════════════════════════════════════════════════════════════════════
# EARLY-SKIP: identita' di contesto tra run consecutivi
# ═════════════════════════════════════════════════════════════════════════
# Razionale: se NULLA e' cambiato dal run precedente (no nuovi report,
# buffer vuoto, portfolio invariato, nessun trigger esterno), e' inutile
# chiamare il modello — restituisce sempre la stessa NO_TRADE. Skip diretto
# senza spreco di token.
#
# Storage signature: settings table, key='decision_last_signature_<type>'.
# Signature = hash(open_positions, cash, last_8h_ts, last_4d_ts, last_buf_ts,
# buf_count). MD5 e' ok qui (non e' uso crittografico).

def _compute_context_signature(portfolio_state: dict,
                               rep_8h: list,
                               rep_4d: list,
                               recent_buffer: list) -> str:
    """Hash deterministico del contesto attuale per detect cambiamenti."""
    import hashlib
    try:
        positions = portfolio_state.get("positions") or []
        # Tickers ordinati + quantita' per detect chiusura/apertura/SL update
        pos_summary = sorted([
            f"{p.get('ticker','?')}:{round(float(p.get('quantity', 0) or 0), 4)}"
            for p in positions
        ])
        rep_8h_ts = max([str(r.get("timestamp") or "") for r in rep_8h], default="")
        rep_4d_ts = max([str(r.get("timestamp") or "") for r in rep_4d], default="")
        buf_ts = max([str(b.get("created_at") or "") for b in recent_buffer], default="")
        sig_input = {
            "positions": pos_summary,
            "cash": round(float(portfolio_state.get("cash", 0) or 0), 2),
            "rep_8h_ts": rep_8h_ts,
            "rep_4d_ts": rep_4d_ts,
            "buf_ts": buf_ts,
            "buf_count": len(recent_buffer),
        }
        as_str = json.dumps(sig_input, sort_keys=True, default=str)
        return hashlib.md5(as_str.encode("utf-8")).hexdigest()
    except Exception as e:
        logger.debug("signature compute fail: %s", e)
        # Fallback: timestamp → no skip possibile (signature sempre diversa)
        return f"err_{datetime.now(timezone.utc).timestamp()}"


def _should_skip_decision_run(
    *,
    current_signature: str,
    watchdog_reason: str | None,
    focus_tickers: list | None,
    agent_type: str = "standard",
) -> tuple[bool, str]:
    """
    Decide se skippare il run del Decision Agent.

    Ritorna (skip: bool, reason: str). Skip == True solo se TUTTE le
    condizioni di stabilita' sono soddisfatte.

    NON skippa MAI se:
      - Watchdog trigger attivo
      - Focus tickers (movimenti anomali rilevati)
      - Commitment attivo che scade entro 6h
      - Nessuna signature precedente (primo run)
      - Signature precedente diversa (qualcosa e' cambiato)
    """
    # 1. Trigger esterni forzano sempre run
    if watchdog_reason and watchdog_reason.strip():
        return False, f"watchdog: {watchdog_reason[:60]}"
    if focus_tickers:
        return False, f"focus tickers: {','.join(focus_tickers[:5])}"

    # 2. Commitment in scadenza
    try:
        import database
        if hasattr(database, "get_active_agent_commitments"):
            commits = database.get_active_agent_commitments(agent_type, limit=30) or []
            now = datetime.now(timezone.utc)
            for c in commits:
                exp = c.get("expires_at")
                if not exp:
                    continue
                try:
                    exp_dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
                    hrs_left = (exp_dt - now).total_seconds() / 3600.0
                    if 0 < hrs_left < 6:
                        return False, f"commitment {str(c.get('id'))[:8]} scade in {hrs_left:.1f}h"
                except Exception:
                    pass
    except Exception:
        pass

    # 3. Confronta signature
    try:
        import database
        key = f"decision_last_signature_{agent_type}"
        prev_sig = database.get_setting(key, "") or ""
        if not prev_sig:
            return False, "primo run (no signature precedente)"
        if prev_sig.strip() == current_signature.strip():
            return True, "contesto invariato dal run precedente"
        return False, "contesto cambiato"
    except Exception as e:
        logger.debug("signature read fail: %s", e)
        return False, f"signature read error: {e}"


def _save_decision_signature(agent_type: str, signature: str) -> None:
    """Persiste la signature corrente per il prossimo confronto."""
    try:
        import database
        key = f"decision_last_signature_{agent_type}"
        database.set_setting(key, signature)
    except Exception as e:
        logger.debug("signature save fail: %s", e)
