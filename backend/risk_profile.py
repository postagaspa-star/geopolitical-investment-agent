"""
Risk Profile — vincoli numerici hard per il Decision Agent (equity + crypto).

Tre preset ("conservative", "moderate", "aggressive") definiscono parametri
quantitativi che il Decision Agent NON può violare:
  - Max % per posizione
  - Min confidence per eseguire
  - SL distance min/max
  - Max posizioni aperte
  - Max drawdown prima di stop totale
  - Freshness minima delle news per usarle come catalyst

Storage: settings table, key='user_risk_profile' (default='moderate').

Iniettato come blocco in cima al system prompt (sotto le User Directives).
Validato in execute_trade prima di eseguire (rifiuta se viola hard caps).
"""
from __future__ import annotations

import logging
from typing import Literal

logger = logging.getLogger(__name__)

ProfileKey = Literal["conservative", "moderate", "aggressive"]
DEFAULT_PROFILE: ProfileKey = "moderate"
SETTING_KEY = "user_risk_profile"


# ─── Definizione profili ────────────────────────────────────────────────────

PROFILES: dict[str, dict] = {
    "conservative": {
        "label": "Conservativo",
        "icon": "🛡️",
        "summary": "Capital preservation. Pochi trade, alta conviction, stop stretti.",
        # Equity / ETF
        "max_position_pct_equity": 8.0,
        "sl_min_pct_equity": 4.0,
        "sl_max_pct_equity": 8.0,
        # Crypto
        "max_position_pct_crypto": 6.0,
        "sl_min_pct_crypto": 8.0,
        "sl_max_pct_crypto": 15.0,
        # Common
        "min_confidence": 0.80,
        "max_open_positions": 3,
        "max_portfolio_drawdown_pct": 6.0,
        "news_freshness_min_hours": 1,
    },
    "moderate": {
        "label": "Moderato",
        "icon": "⚖️",
        "summary": "Equilibrio rischio/rendimento. Default consigliato.",
        "max_position_pct_equity": 15.0,
        "sl_min_pct_equity": 7.0,
        "sl_max_pct_equity": 15.0,
        "max_position_pct_crypto": 12.0,
        "sl_min_pct_crypto": 12.0,
        "sl_max_pct_crypto": 25.0,
        "min_confidence": 0.65,
        "max_open_positions": 6,
        "max_portfolio_drawdown_pct": 12.0,
        "news_freshness_min_hours": 3,
    },
    "aggressive": {
        "label": "Aggressivo",
        "icon": "🔥",
        "summary": "Massima esposizione. Più trade, soglie basse, stop ampi.",
        "max_position_pct_equity": 25.0,
        "sl_min_pct_equity": 12.0,
        "sl_max_pct_equity": 25.0,
        "max_position_pct_crypto": 20.0,
        "sl_min_pct_crypto": 20.0,
        "sl_max_pct_crypto": 40.0,
        "min_confidence": 0.50,
        "max_open_positions": 10,
        "max_portfolio_drawdown_pct": 20.0,
        "news_freshness_min_hours": 6,
    },
}


# ─── Storage ────────────────────────────────────────────────────────────────

def get_active_profile_key() -> str:
    """Legge la chiave profilo corrente. Default 'moderate' se non valida."""
    try:
        import database as _db
        raw = (_db.get_setting(SETTING_KEY, DEFAULT_PROFILE) or "").strip().lower()
        if raw in PROFILES:
            return raw
        return DEFAULT_PROFILE
    except Exception as e:
        logger.warning("get_active_profile_key fallback to default: %s", e)
        return DEFAULT_PROFILE


def set_active_profile_key(key: str) -> str:
    """Setta il profilo. Ritorna la chiave effettivamente salvata (validata)."""
    k = (key or "").strip().lower()
    if k not in PROFILES:
        k = DEFAULT_PROFILE
    try:
        import database as _db
        _db.set_setting(SETTING_KEY, k)
    except Exception as e:
        logger.error("set_active_profile_key error: %s", e)
        raise
    return k


def get_active_profile() -> dict:
    """Ritorna il profilo attivo completo (dict con tutti i parametri)."""
    key = get_active_profile_key()
    profile = dict(PROFILES[key])
    profile["key"] = key
    return profile


def list_profiles() -> dict:
    """Per UI: ritorna {active_key, profiles: {...}}."""
    return {
        "active_key": get_active_profile_key(),
        "profiles": PROFILES,
    }


# ─── Prompt block ──────────────────────────────────────────────────────────

def build_risk_block(asset_class: str = "equity") -> str:
    """
    Compone il blocco testo iniettato nel system prompt.

    asset_class: 'equity' o 'crypto' — sceglie quali soglie mostrare in primo
    piano (entrambe sono comunque mostrate per completezza).
    """
    p = get_active_profile()
    is_crypto = asset_class == "crypto"

    primary_pos = p["max_position_pct_crypto"] if is_crypto else p["max_position_pct_equity"]
    primary_sl_min = p["sl_min_pct_crypto"] if is_crypto else p["sl_min_pct_equity"]
    primary_sl_max = p["sl_max_pct_crypto"] if is_crypto else p["sl_max_pct_equity"]

    lines = [
        "█" * 60,
        f"⚖️  RISK PROFILE — {p['label'].upper()}  (HARD CONSTRAINTS)",
        "█" * 60,
        "",
        f"{p['summary']}",
        "",
        "Questi sono VINCOLI NUMERICI obbligatori. Possono essere applicati",
        "in modo PIÙ conservativo, MAI più aggressivo. Il sistema rifiuterà",
        "qualsiasi trade che li viola.",
        "",
        f"  • Max allocazione singolo trade: {primary_pos:.1f}% del cash disponibile",
        f"  • Confidence minima per eseguire: {p['min_confidence']:.2f}",
        f"  • Stop Loss obbligatorio nel range: {primary_sl_min:.1f}%–{primary_sl_max:.1f}% dall'entry",
        f"  • Posizioni aperte simultanee (totale portfolio): max {p['max_open_positions']}",
        f"  • Stop portfolio: drawdown max {p['max_portfolio_drawdown_pct']:.1f}% dall'high",
        f"  • Freshness news per usarle come catalyst: max {p['news_freshness_min_hours']}h fa",
        "",
        "Se la conviction è SOTTO la soglia minima → NO TRADE, anche se",
        "il setup tecnico sembra interessante. Aspetta più segnali.",
        "",
        "Se l'allocazione richiesta SUPERA il cap → riduci la size, non saltare",
        "il trade (a meno che non scenda sotto la min_confidence).",
        "█" * 60,
        "",
    ]
    return "\n".join(lines)


# ─── Validazione execute_trade ──────────────────────────────────────────────

def validate_trade(
    *,
    asset_class: str,
    confidence: float | None,
    allocation_pct: float | None,
    open_positions_count: int | None,
    portfolio_drawdown_pct: float | None = None,
) -> tuple[bool, str]:
    """
    Validazione hard del trade contro il profilo attivo.

    Ritorna (ok: bool, reason: str). Se ok=False, reason spiega la violazione.

    asset_class: 'equity' o 'crypto'
    confidence:  0..1
    allocation_pct: % del cash disponibile (0..100)
    open_positions_count: numero posizioni APERTE prima di questo trade
    portfolio_drawdown_pct: drawdown corrente dal portfolio high (0..100,
                            valore positivo). None = non controllare.
    """
    p = get_active_profile()
    is_crypto = asset_class == "crypto"
    max_pos = p["max_position_pct_crypto"] if is_crypto else p["max_position_pct_equity"]

    # 1. Confidence
    if confidence is not None:
        try:
            cf = float(confidence)
            if cf < p["min_confidence"] - 1e-6:
                return False, (
                    f"Confidence {cf:.2f} sotto il minimo del profilo "
                    f"{p['label']} ({p['min_confidence']:.2f}). NO TRADE."
                )
        except (TypeError, ValueError):
            pass

    # 2. Allocation
    if allocation_pct is not None:
        try:
            alloc = float(allocation_pct)
            if alloc > max_pos + 1e-6:
                return False, (
                    f"Allocazione {alloc:.1f}% supera il cap del profilo "
                    f"{p['label']} ({max_pos:.1f}% per {asset_class}). "
                    f"Riduci la size."
                )
        except (TypeError, ValueError):
            pass

    # 3. Max open positions
    if open_positions_count is not None:
        try:
            n = int(open_positions_count)
            if n >= p["max_open_positions"]:
                return False, (
                    f"Già {n} posizioni aperte; il profilo {p['label']} "
                    f"limita a {p['max_open_positions']}. Chiudi prima."
                )
        except (TypeError, ValueError):
            pass

    # 4. Portfolio drawdown stop
    if portfolio_drawdown_pct is not None:
        try:
            dd = float(portfolio_drawdown_pct)
            if dd > p["max_portfolio_drawdown_pct"]:
                return False, (
                    f"Drawdown portfolio {dd:.1f}% supera il limite del "
                    f"profilo {p['label']} ({p['max_portfolio_drawdown_pct']:.1f}%). "
                    f"Solo chiusure ammesse, NESSUNA nuova posizione."
                )
        except (TypeError, ValueError):
            pass

    return True, "ok"


def validate_sl_range(
    *,
    asset_class: str,
    entry_price: float,
    sl_price: float,
    side: str = "long",
) -> tuple[bool, str]:
    """
    Verifica che la distanza SL sia nel range [min, max] del profilo.

    side: 'long' o 'short'
    """
    p = get_active_profile()
    is_crypto = asset_class == "crypto"
    sl_min = p["sl_min_pct_crypto"] if is_crypto else p["sl_min_pct_equity"]
    sl_max = p["sl_max_pct_crypto"] if is_crypto else p["sl_max_pct_equity"]

    if entry_price <= 0 or sl_price <= 0:
        return False, "Prezzo entry o SL non valido"

    if side.lower() == "short":
        # Short: SL sopra l'entry
        dist_pct = ((sl_price - entry_price) / entry_price) * 100.0
    else:
        # Long: SL sotto l'entry
        dist_pct = ((entry_price - sl_price) / entry_price) * 100.0

    if dist_pct < sl_min:
        return False, (
            f"SL troppo stretto ({dist_pct:.1f}%); profilo {p['label']} "
            f"richiede min {sl_min:.1f}% ({asset_class})."
        )
    if dist_pct > sl_max:
        return False, (
            f"SL troppo ampio ({dist_pct:.1f}%); profilo {p['label']} "
            f"limita a max {sl_max:.1f}% ({asset_class})."
        )
    return True, "ok"
