"""
capital_orchestrator.py — Capital Orchestrator (DeepSeek-V3)

Risolve il "silo problem" tra Decision Standard ed il Decision Crypto:
quando uno dei due agenti ha bisogno di liquidità per aprire una posizione
ma il cash è bloccato in posizioni dell'altro agente, l'orchestratore
ragiona se chiudere parzialmente una posizione cross-agente per liberare
capitale.

Architettura advisory + executive:
  1. Decision Standard (o Crypto) chiama `request_capital(...)` quando
     `available_cash < required_amount * 0.8`.
  2. L'orchestratore (DeepSeek-V3) vede entrambi i sub-portfolio + i
     metadata di tutte le posizioni (P&L, età, conviction storica).
  3. Ragiona se conviene chiudere parzialmente una posizione dell'altro
     agente per finanziare il trade richiesto.
  4. Se approvato: esegue la liquidazione via `portfolio.execute_sell`,
     restituisce il cash freed all'agente richiedente, setta cooldown 2h.
  5. Se negato: ritorna motivazione strutturata, l'agente richiedente
     deve scalare il trade o rinunciare.

Modello: deepseek-chat (V3, non R1). La decisione è strutturata e relativamente
breve, non serve catena di ragionamento R1.

Guardrail:
  - Cooldown 2h post-riallocazione (orchestrator_last_run_at)
  - Max transfer cap: 25% del NAV totale per riallocazione
  - Min conviction richiesta: 0.70
  - Min gap utilizzabile: $500 (sotto questa soglia l'orchestratore rifiuta)
  - Non liquida posizioni < 24h dall'apertura
  - Mai realizza loss > -10% per finanziare cross-agent (a meno di conviction
    > 0.85 sul trade richiesto)

Costo: ~$0.0002 per chiamata (V3 input ~$0.27/M, output ~$1.10/M, ~3-4k token).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_V3_MODEL = "deepseek-v4-flash"   # V3, non R1

ORCHESTRATOR_COOLDOWN_HOURS = 2
MAX_TRANSFER_PCT_NAV = 25.0           # Max % del NAV per singola riallocazione
MIN_CONVICTION = 0.70                 # Sotto questo valore, rifiuta a priori
MIN_GAP_USD = 500.0                   # Gap minimo utilizzabile
MIN_POSITION_AGE_HOURS = 24           # Non liquidare posizioni troppo fresche
MAX_LOSS_TOLERATED_PCT = -10.0        # Max loss % accettato per liquidare


def _is_crypto_ticker(ticker: str) -> bool:
    """True se il ticker è crypto (formato yfinance BTC-USD o esteso X:BTCUSD)."""
    if not ticker:
        return False
    t = ticker.upper().strip()
    if t.startswith("X:"):
        return True
    if t.endswith("-USD") and len(t) > 4:
        return True
    return False


def _get_deepseek_key() -> str:
    """Recupera la chiave DeepSeek da env, con fallback al DB settings."""
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        try:
            import database as _db
            key = _db.get_setting("deepseek_api_key", "") or ""
        except Exception:
            pass
    return key


# ── Cooldown gestione ─────────────────────────────────────────────────────

def _is_orchestrator_cooldown_active() -> tuple[bool, int]:
    """
    True se l'orchestratore ha eseguito una riallocazione negli ultimi
    ORCHESTRATOR_COOLDOWN_HOURS. Ritorna anche secondi rimanenti.

    Storage: settings table, key = "orchestrator_last_run_at" (ISO timestamp).
    """
    try:
        import database
        last_iso = database.get_setting("orchestrator_last_run_at", "") or ""
        if not last_iso:
            return False, 0
        last_dt = datetime.fromisoformat(last_iso.replace("Z", "+00:00"))
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        elapsed = datetime.now(timezone.utc) - last_dt
        cooldown = timedelta(hours=ORCHESTRATOR_COOLDOWN_HOURS)
        if elapsed < cooldown:
            seconds_left = int((cooldown - elapsed).total_seconds())
            return True, seconds_left
        return False, 0
    except Exception as e:
        logger.warning("[ORCHESTRATOR] Cooldown check failed (fail-open): %s", e)
        return False, 0


def _set_orchestrator_cooldown() -> None:
    """Registra il timestamp dell'ultima riallocazione per il cooldown 2h."""
    try:
        import database
        now_iso = datetime.now(timezone.utc).isoformat()
        database.set_setting("orchestrator_last_run_at", now_iso)
    except Exception as e:
        logger.warning("[ORCHESTRATOR] Cooldown set failed: %s", e)


# ── Portfolio snapshot ────────────────────────────────────────────────────

def _build_full_portfolio_snapshot() -> dict:
    """
    Ritorna lo snapshot completo del portafoglio (equity + crypto) con
    metadata utili per l'orchestratore: P&L, età posizione, valore corrente.

    Schema:
    {
      "total_nav_usd": float,
      "cash_usd": float,
      "positions": [
        {
          "ticker": "NVDA",
          "is_crypto": false,
          "quantity": 50,
          "avg_buy_price": 480.0,
          "current_price": 540.0,
          "current_value": 27000.0,
          "pnl_abs": 3000.0,
          "pnl_pct": 12.5,
          "pct_of_nav": 18.5,
          "age_hours": 240,
          "agent_owner": "standard"   # o "crypto"
        }
      ]
    }
    """
    try:
        import database
        portfolio = database.get_portfolio() or {}
        positions_raw = database.get_positions() or []
    except Exception as e:
        logger.error("[ORCHESTRATOR] Failed to load portfolio: %s", e)
        return {"total_nav_usd": 0.0, "cash_usd": 0.0, "positions": []}

    cash = float(portfolio.get("cash_balance", 0) or 0)
    total_nav = cash
    positions_data = []

    now_utc = datetime.now(timezone.utc)
    for p in positions_raw:
        ticker = p.get("ticker", "")
        qty = float(p.get("quantity", 0) or 0)
        if qty <= 0:
            continue
        avg_buy = float(p.get("avg_buy_price", 0) or 0)
        # current price: usa current_price se valido, fallback a avg_buy
        current_price = float(p.get("current_price", 0) or 0)
        if current_price <= 0:
            current_price = avg_buy
        # DIRECTION-AWARE (canonico accounting): SHORT = passività nel NAV.
        direction = (p.get("direction") or "LONG").upper()
        import accounting as _acc
        current_value = _acc.signed_position_value(qty, current_price, direction)
        pnl_abs = _acc.unrealized_pnl(qty, avg_buy, current_price, direction)
        pnl_pct = _acc.unrealized_pnl_pct(avg_buy, current_price, direction)

        # Età posizione
        age_hours = 0
        ts_str = p.get("opened_at") or p.get("created_at") or ""
        if ts_str:
            try:
                ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age_hours = int((now_utc - ts).total_seconds() / 3600)
            except Exception:
                age_hours = 0

        is_crypto = _is_crypto_ticker(ticker)
        positions_data.append({
            "ticker": ticker,
            "direction": direction,
            "is_crypto": is_crypto,
            "quantity": round(qty, 6),
            "avg_buy_price": round(avg_buy, 4),
            "current_price": round(current_price, 4),
            "current_value": round(current_value, 2),   # firmato (- per SHORT)
            "pnl_abs": round(pnl_abs, 2),
            "pnl_pct": round(pnl_pct, 2),
            "age_hours": age_hours,
            "agent_owner": "crypto" if is_crypto else "standard",
        })
        total_nav += current_value   # gia' firmato direction-aware

    # Aggiorna pct_of_nav ora che conosciamo il totale (con valore assoluto:
    # il "peso" di una SHORT nel NAV e' la sua esposizione lorda, non il segno).
    for pos in positions_data:
        pos["pct_of_nav"] = round(
            (abs(pos["current_value"]) / total_nav * 100)
            if total_nav > 0 else 0.0,
            2,
        )

    return {
        "total_nav_usd": round(total_nav, 2),
        "cash_usd": round(cash, 2),
        "positions": positions_data,
    }


# ── Prompt ────────────────────────────────────────────────────────────────

ORCHESTRATOR_SYSTEM_PROMPT = """Sei il CAPITAL ORCHESTRATOR di un sistema di
investimento multi-agente. Due agenti gestiscono in parallelo:
  - DECISION STANDARD: equity + ETF (azioni USA, settoriali, indici, bond)
  - DECISION CRYPTO: crypto (BTC, ETH, SOL, ecc.)

Entrambi attingono dallo STESSO pool di cash, ma operano in silo: nessuno dei
due può vendere posizioni dell'altro. Il tuo ruolo è quello di un "fund of fund
allocator": quando uno dei due agenti ha bisogno di capitale ma il cash è
bloccato in posizioni dell'altro, ragioni se conviene liquidare parzialmente
una posizione cross-agente per finanziare il trade richiesto.

═══════════════════════════════════════════════════════════════════════
COSA RICEVI
═══════════════════════════════════════════════════════════════════════
1. CAPITAL_REQUEST: {requesting_agent, ticker, amount_needed, amount_available,
   gap, conviction, reasoning}
2. PORTFOLIO_SNAPSHOT: posizioni complete di entrambi i sub-portfolio con
   P&L, età e percentuale del NAV per ognuna.

═══════════════════════════════════════════════════════════════════════
REGOLE DECISIONALI
═══════════════════════════════════════════════════════════════════════
1. APPROVA solo se la conviction del trade richiesto >= 0.70.
2. PREFERISCI liquidare posizioni con:
   - PROFITTO ALTO (>= +15% unrealized): realizzare gain è positivo
   - ETÀ MATURA (>= 7 giorni): meno disturbo all'idea originale dell'altro agente
   - PCT_OF_NAV ALTO (>= 15%): riduzione del rischio di concentrazione
3. EVITA di liquidare:
   - Posizioni in PERDITA (-10% o peggio): cristalizza loss inutilmente
   - Posizioni TROPPO FRESCHE (< 24h): conviction tesi originale ancora attuale
   - Posizioni piccole (< $500): impatto trascurabile
4. CAP MAX: non spostare più del 25% del NAV totale in una singola riallocazione.
5. CHIUSURA PARZIALE preferita a chiusura totale: tipicamente liquida solo
   l'amount necessario + 5-10% margine per slippage.

═══════════════════════════════════════════════════════════════════════
OUTPUT (SOLO JSON, niente altro)
═══════════════════════════════════════════════════════════════════════
{
  "decision": "approve" | "deny",
  "reasoning": "spiegazione breve del ragionamento (max 300 char)",
  "actions": [
    {
      "ticker": "ETH-USD",
      "from_agent": "crypto",
      "action": "PARTIAL_SELL",
      "quantity_to_sell": 0.85,
      "estimated_proceeds_usd": 3500,
      "rationale": "ETH +42% gain, posizione 14 giorni mature, no urgenza tesi"
    }
  ]
}

Se decision == "deny": "actions" è array vuoto, "reasoning" deve spiegare
chiaramente perché la riallocazione non conviene (es. "Tutte le posizioni
crypto in loss <-5%; realizzare loss per finanziare un equity trade con
conviction 0.72 non è efficiente")."""


# ── DeepSeek-V3 call ──────────────────────────────────────────────────────

async def _call_v3(system_prompt: str, user_message: str,
                    max_retries: int = 2,
                    timeout_seconds: int = 60) -> str:
    """
    Chiama DeepSeek-V3 via OpenAI-compatible API.
    V3 è il modello chat veloce (non R1), ideale per decisioni strutturate.
    """
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
            {"role": "user", "content": user_message},
        ],
        "temperature": 0.3,        # Decisione strutturata, deterministico
        "max_tokens": 1500,
        "response_format": {"type": "json_object"},   # Force JSON
    }

    last_err: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    DEEPSEEK_API_URL,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        raise ValueError(
                            f"DeepSeek-V3 HTTP {resp.status}: {body[:300]}"
                        )
                    data = await resp.json()
                    content = data["choices"][0]["message"]["content"] or ""
                    return content.strip()
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                wait = 1.5 * (attempt + 1)
                logger.warning("[ORCHESTRATOR] V3 call retry %d/%d in %.1fs: %s",
                                attempt + 1, max_retries, wait, e)
                await asyncio.sleep(wait)
            else:
                break

    raise RuntimeError(f"DeepSeek-V3 failed after {max_retries + 1} attempts: {last_err}")


# ── Output parsing ────────────────────────────────────────────────────────

def _parse_orchestrator_response(raw: str) -> Optional[dict]:
    """
    Parse output JSON dell'orchestratore. Restituisce None se malformato.
    Tollera codeblock markdown e testo extra (estrae il primo JSON object).
    """
    if not raw:
        return None
    text = raw.strip()
    # Rimuovi eventuali code fences
    if "```" in text:
        try:
            chunk = text.split("```")[1].replace("json", "", 1).strip()
            text = chunk
        except Exception:
            pass

    # Tentativo diretto
    try:
        return json.loads(text)
    except Exception:
        pass

    # Fallback: estrai il primo blocco {...}
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


# ── Validation guardrail ──────────────────────────────────────────────────

def _validate_directive(directive: dict, snapshot: dict,
                          requesting_agent: str) -> tuple[bool, str]:
    """
    Verifica che la directive rispetti i guardrail. Ritorna (ok, motivo_se_no_ok).
    """
    decision = directive.get("decision", "")
    if decision not in ("approve", "deny"):
        return False, f"decision non valida: '{decision}' (atteso approve/deny)"

    if decision == "deny":
        return True, ""   # Deny è sempre valido

    actions = directive.get("actions") or []
    if not isinstance(actions, list) or not actions:
        return False, "approve senza actions"

    total_nav = snapshot.get("total_nav_usd", 0)
    if total_nav <= 0:
        return False, "NAV non valido"

    total_proceeds = 0.0
    for a in actions:
        if not isinstance(a, dict):
            return False, "action non è dict"
        ticker = a.get("ticker", "")
        from_agent = a.get("from_agent", "")
        qty = float(a.get("quantity_to_sell", 0) or 0)

        if not ticker:
            return False, "ticker vuoto in action"
        if from_agent == requesting_agent:
            return False, (
                f"from_agent '{from_agent}' uguale al requesting_agent: "
                f"non è una riallocazione cross-agent"
            )
        if qty <= 0:
            return False, f"quantity_to_sell deve essere > 0 (ricevuto {qty})"

        # Verifica posizione esista nel snapshot e abbia abbastanza qty
        pos = next((p for p in snapshot["positions"] if p["ticker"] == ticker), None)
        if pos is None:
            return False, f"posizione {ticker} non esiste nel portafoglio"
        # Proprietario REALE (deterministico dallo snapshot), non il from_agent
        # asserito dal modello: non si può "liberare" capitale liquidando una
        # propria posizione spacciandola per riallocazione cross-agent.
        if str(pos.get("agent_owner") or "").lower() == str(requesting_agent or "").lower():
            return False, (
                f"posizione {ticker} è di proprietà del richiedente "
                f"'{requesting_agent}': non è una riallocazione cross-agent"
            )
        if qty > pos["quantity"]:
            return False, (
                f"quantity_to_sell {qty} > qty disponibile {pos['quantity']} "
                f"per {ticker}"
            )

        # Età posizione
        if pos.get("age_hours", 0) < MIN_POSITION_AGE_HOURS:
            return False, (
                f"posizione {ticker} troppo fresca ({pos['age_hours']}h < "
                f"{MIN_POSITION_AGE_HOURS}h): conviction tesi originale ancora attuale"
            )

        # Loss tolerance
        if pos.get("pnl_pct", 0) < MAX_LOSS_TOLERATED_PCT:
            return False, (
                f"{ticker} ha loss {pos['pnl_pct']:.1f}% < "
                f"{MAX_LOSS_TOLERATED_PCT}%: realizzare loss non efficiente"
            )

        # Cap calcolato sul valore REALE qty*prezzo (non su estimated_proceeds_usd
        # fornito dal modello, che poteva dichiararlo basso per superare il cap
        # e vendere molto più del 25% del NAV).
        real_price = float(pos.get("current_price") or pos.get("avg_buy_price") or 0)
        total_proceeds += qty * real_price

    # Cap massimo trasferimento
    transfer_pct = (total_proceeds / total_nav) * 100 if total_nav > 0 else 0
    if transfer_pct > MAX_TRANSFER_PCT_NAV:
        return False, (
            f"trasferimento totale {transfer_pct:.1f}% NAV > "
            f"max {MAX_TRANSFER_PCT_NAV}%"
        )

    return True, ""


# ── Esecuzione liquidazione ───────────────────────────────────────────────

def _execute_liquidation(action: dict, run_id: str) -> dict:
    """
    Esegue una singola action di liquidazione via portfolio.execute_sell.
    Ritorna {success, ticker, qty, proceeds, error?}.
    """
    ticker = action.get("ticker", "")
    qty = float(action.get("quantity_to_sell", 0) or 0)

    try:
        import portfolio as _pf
        import database
        pos = database.get_position(ticker)
        if pos is None:
            return {"success": False, "ticker": ticker,
                    "error": "position not found"}
        current_price = float(pos.get("current_price", 0) or pos.get("avg_buy_price", 0))
        if current_price <= 0:
            return {"success": False, "ticker": ticker,
                    "error": "current_price not available"}

        # DIRECTION-AWARE: una SHORT si chiude con COVER, non con SELL.
        # Prima usava sempre execute_sell → su una SHORT veniva rifiutato
        # ("usa COVER") e la liquidazione falliva silenziosamente, quindi
        # l'orchestrator non riusciva MAI a ridurre le posizioni short.
        direction = str(pos.get("direction") or "LONG").upper()
        common = dict(
            ticker=ticker,
            quantity=qty,
            price=current_price,
            geo_reasoning="[ORCHESTRATOR] Cross-agent capital reallocation",
            tech_reasoning=action.get("rationale", "Capital orchestrator directive"),
            confidence=85,
            execution_type="orchestrator",
        )
        if direction == "SHORT":
            result = _pf.execute_cover(**common)
        else:
            result = _pf.execute_sell(**common)

        if not result.get("success"):
            _op = "execute_cover" if direction == "SHORT" else "execute_sell"
            return {"success": False, "ticker": ticker,
                    "error": result.get("reason", f"{_op} failed")}

        # Una SHORT si chiude con COVER, che SPENDE cash: non è una fonte di
        # liquidità per la riallocazione. proceeds=0 così il chiamante non
        # gonfia total_freed (prima sommava qty*price come capitale liberato
        # mentre il cash DIMINUIVA).
        proceeds = 0.0 if direction == "SHORT" else qty * current_price
        return {
            "success": True,
            "ticker": ticker,
            "qty_sold": qty,
            "price": current_price,
            "proceeds": round(proceeds, 2),
            "direction": direction,
        }
    except Exception as e:
        logger.error("[ORCHESTRATOR][%s] Liquidation failed for %s: %s",
                     run_id, ticker, e, exc_info=True)
        return {"success": False, "ticker": ticker, "error": str(e)[:200]}


# ── Entry point principale ────────────────────────────────────────────────

async def request_capital(
    run_id: str,
    requesting_agent: str,
    ticker: str,
    amount_needed: float,
    amount_available: float,
    conviction: float,
    reasoning: str,
) -> dict:
    """
    Chiama l'orchestratore quando un Decision agent ha bisogno di capitale
    bloccato in posizioni dell'altro agente.

    Args:
        run_id: ID del run dell'agente richiedente (per logging)
        requesting_agent: "standard" | "crypto"
        ticker: ticker che l'agente vuole comprare (es. "NVDA", "BTC-USD")
        amount_needed: USD totale necessario per il trade
        amount_available: USD attualmente disponibili come cash
        conviction: 0.0-1.0, conviction dell'agente sul trade
        reasoning: spiegazione breve del perché serve capitale

    Returns:
        {
          "approved": bool,
          "amount_freed": float,        # USD effettivamente liberati (0 se denied)
          "actions_executed": [...],    # liste delle liquidazioni effettuate
          "directive": {...},           # output raw dell'orchestratore
          "reasoning": str,             # motivazione finale (success o deny)
          "skipped": str | None,        # se skip (cooldown, validation, ...)
          "duration_seconds": float,
        }
    """
    start = time.time()
    requesting_agent = (requesting_agent or "").lower()
    if requesting_agent not in ("standard", "crypto"):
        return {
            "approved": False, "amount_freed": 0.0, "actions_executed": [],
            "directive": None, "reasoning": "",
            "skipped": f"requesting_agent invalido: '{requesting_agent}'",
            "duration_seconds": round(time.time() - start, 2),
        }

    # ── Pre-check 0: feature flag enabled? Default OFF post-bug ──────────
    try:
        import database as _db
        raw_enabled = (_db.get_setting("capital_orchestrator_enabled", "false") or "").strip().lower()
        if raw_enabled not in ("1", "true", "yes", "on"):
            msg = ("Capital Orchestrator DISABILITATO via setting. "
                   "Il Decision Agent deve scalare il trade o aspettare cash.")
            _log_orchestrator_event(run_id, "request_skipped_disabled", {
                "requesting_agent": requesting_agent, "ticker": ticker,
            })
            return {
                "approved": False, "amount_freed": 0.0, "actions_executed": [],
                "directive": None, "reasoning": msg,
                "skipped": "feature_disabled",
                "duration_seconds": round(time.time() - start, 2),
            }
    except Exception:
        # Se non posso leggere il flag, fallback safe: skip
        return {
            "approved": False, "amount_freed": 0.0, "actions_executed": [],
            "directive": None, "reasoning": "Capital Orchestrator check failed",
            "skipped": "flag_read_error",
            "duration_seconds": round(time.time() - start, 2),
        }

    gap = max(0.0, float(amount_needed) - float(amount_available))

    # ── Pre-check 1: conviction sufficiente ──────────────────────────────
    if conviction < MIN_CONVICTION:
        msg = (
            f"Conviction {conviction:.2f} < {MIN_CONVICTION:.2f}: non disturbo "
            f"posizioni cross-agent per un'idea con bassa convinzione"
        )
        _log_orchestrator_event(run_id, "request_skipped_low_conviction", {
            "requesting_agent": requesting_agent, "ticker": ticker,
            "conviction": conviction, "min_required": MIN_CONVICTION,
        })
        return {
            "approved": False, "amount_freed": 0.0, "actions_executed": [],
            "directive": None, "reasoning": msg,
            "skipped": "low_conviction",
            "duration_seconds": round(time.time() - start, 2),
        }

    # ── Pre-check 2: gap minimo ──────────────────────────────────────────
    if gap < MIN_GAP_USD:
        msg = (
            f"Gap ${gap:.2f} < ${MIN_GAP_USD}: troppo piccolo per giustificare "
            f"una riallocazione cross-agent"
        )
        return {
            "approved": False, "amount_freed": 0.0, "actions_executed": [],
            "directive": None, "reasoning": msg,
            "skipped": "gap_too_small",
            "duration_seconds": round(time.time() - start, 2),
        }

    # ── Pre-check 3: cooldown 2h ─────────────────────────────────────────
    cooldown_active, seconds_left = _is_orchestrator_cooldown_active()
    if cooldown_active:
        msg = (
            f"Cooldown orchestratore attivo: prossima riallocazione possibile "
            f"tra {seconds_left // 60} minuti"
        )
        _log_orchestrator_event(run_id, "request_skipped_cooldown", {
            "requesting_agent": requesting_agent, "ticker": ticker,
            "seconds_left": seconds_left,
        })
        return {
            "approved": False, "amount_freed": 0.0, "actions_executed": [],
            "directive": None, "reasoning": msg,
            "skipped": "cooldown_active",
            "duration_seconds": round(time.time() - start, 2),
        }

    # ── Snapshot portafoglio ─────────────────────────────────────────────
    snapshot = await asyncio.to_thread(_build_full_portfolio_snapshot)
    if snapshot["total_nav_usd"] <= 0:
        return {
            "approved": False, "amount_freed": 0.0, "actions_executed": [],
            "directive": None, "reasoning": "Portafoglio vuoto o non valido",
            "skipped": "empty_portfolio",
            "duration_seconds": round(time.time() - start, 2),
        }

    # ── Costruisci user message ──────────────────────────────────────────
    user_msg = json.dumps({
        "capital_request": {
            "requesting_agent": requesting_agent,
            "ticker": ticker,
            "amount_needed_usd": round(amount_needed, 2),
            "amount_available_usd": round(amount_available, 2),
            "gap_usd": round(gap, 2),
            "conviction": conviction,
            "reasoning": reasoning[:500],
        },
        "portfolio_snapshot": snapshot,
    }, indent=2)

    _log_orchestrator_event(run_id, "request_received", {
        "requesting_agent": requesting_agent, "ticker": ticker,
        "amount_needed": amount_needed, "gap": gap, "conviction": conviction,
        "total_nav": snapshot["total_nav_usd"],
        "positions_count": len(snapshot["positions"]),
    })

    # ── Chiamata DeepSeek-V3 ─────────────────────────────────────────────
    try:
        raw = await _call_v3(ORCHESTRATOR_SYSTEM_PROMPT, user_msg)
    except Exception as e:
        logger.error("[ORCHESTRATOR][%s] V3 call failed: %s", run_id, e)
        _log_orchestrator_event(run_id, "ai_call_failed", {"error": str(e)[:300]})
        return {
            "approved": False, "amount_freed": 0.0, "actions_executed": [],
            "directive": None, "reasoning": f"AI call failed: {e}",
            "skipped": "ai_error",
            "duration_seconds": round(time.time() - start, 2),
        }

    directive = _parse_orchestrator_response(raw)
    if directive is None:
        logger.warning("[ORCHESTRATOR][%s] JSON parse failed. Raw: %s",
                        run_id, raw[:300])
        _log_orchestrator_event(run_id, "parse_failed", {"raw": raw[:500]})
        return {
            "approved": False, "amount_freed": 0.0, "actions_executed": [],
            "directive": None, "reasoning": "Output AI non parsabile",
            "skipped": "parse_error",
            "duration_seconds": round(time.time() - start, 2),
        }

    # ── Validation guardrail ─────────────────────────────────────────────
    valid, validation_err = _validate_directive(
        directive, snapshot, requesting_agent
    )
    if not valid:
        msg = f"Directive AI non rispetta i guardrail: {validation_err}"
        logger.warning("[ORCHESTRATOR][%s] %s", run_id, msg)
        _log_orchestrator_event(run_id, "validation_failed", {
            "directive": directive, "error": validation_err,
        })
        return {
            "approved": False, "amount_freed": 0.0, "actions_executed": [],
            "directive": directive, "reasoning": msg,
            "skipped": "validation_failed",
            "duration_seconds": round(time.time() - start, 2),
        }

    # ── Decision: deny ───────────────────────────────────────────────────
    if directive.get("decision") == "deny":
        reason = str(directive.get("reasoning", "Riallocazione non conveniente"))
        _log_orchestrator_event(run_id, "denied", {
            "requesting_agent": requesting_agent, "ticker": ticker,
            "reasoning": reason,
        })
        return {
            "approved": False, "amount_freed": 0.0, "actions_executed": [],
            "directive": directive, "reasoning": reason,
            "skipped": None,
            "duration_seconds": round(time.time() - start, 2),
        }

    # ── Decision: approve → esegui liquidazioni ──────────────────────────
    actions = directive.get("actions", [])
    executed: list[dict] = []
    total_freed = 0.0

    for action in actions:
        result = await asyncio.to_thread(_execute_liquidation, action, run_id)
        executed.append(result)
        if result.get("success"):
            total_freed += float(result.get("proceeds", 0))
        else:
            logger.warning(
                "[ORCHESTRATOR][%s] Liquidation failed for %s: %s",
                run_id, result.get("ticker"), result.get("error")
            )

    # Setta cooldown SOLO se almeno una liquidazione è riuscita
    if total_freed > 0:
        _set_orchestrator_cooldown()

    success_count = sum(1 for e in executed if e.get("success"))
    final_reasoning = (
        f"{directive.get('reasoning', '')} | "
        f"Eseguite {success_count}/{len(executed)} liquidazioni, "
        f"liberati ${total_freed:.2f}"
    )

    _log_orchestrator_event(run_id, "approved_executed", {
        "requesting_agent": requesting_agent, "ticker": ticker,
        "actions_count": len(executed),
        "actions_succeeded": success_count,
        "total_freed_usd": round(total_freed, 2),
        "directive_reasoning": directive.get("reasoning", ""),
    })

    return {
        "approved": True,
        "amount_freed": round(total_freed, 2),
        "actions_executed": executed,
        "directive": directive,
        "reasoning": final_reasoning,
        "skipped": None,
        "duration_seconds": round(time.time() - start, 2),
    }


# ── Logging helper ────────────────────────────────────────────────────────

def _log_orchestrator_event(run_id: str, event: str, data: dict) -> None:
    """Logga un evento dell'orchestratore nel DB (agent_logs)."""
    try:
        import database
        payload = {"event": event, **data}
        database.insert_agent_log(
            run_id, "CAPITAL_ORCHESTRATOR",
            json.dumps(payload, default=str)
        )
    except Exception as e:
        logger.debug("[ORCHESTRATOR] log insert failed: %s", e)
