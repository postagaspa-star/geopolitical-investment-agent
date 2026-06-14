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
import math
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
        # Crypto-specific position cap (override del generale)
        "max_open_positions_crypto": 2,
        "max_portfolio_drawdown_pct": 6.0,
        # Satellite (universo esteso): sizing piu' stretto, esposizione aggregata bassa.
        "satellite_sizing_mult": 0.30,
        "max_satellite_exposure_pct": 8.0,
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
        "max_open_positions_crypto": 3,
        "max_portfolio_drawdown_pct": 12.0,
        "satellite_sizing_mult": 0.40,
        "max_satellite_exposure_pct": 15.0,
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
        "max_open_positions_crypto": 7,
        "max_portfolio_drawdown_pct": 20.0,
        "satellite_sizing_mult": 0.50,
        "max_satellite_exposure_pct": 25.0,
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

    # Cap posizioni per asset class
    cap_crypto = p.get("max_open_positions_crypto", p["max_open_positions"])

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
        f"  • Posizioni aperte (totale portfolio): max {p['max_open_positions']}",
        f"  • Posizioni CRYPTO aperte (sub-cap dedicato): max {cap_crypto}",
        f"  • Stop portfolio: drawdown max {p['max_portfolio_drawdown_pct']:.1f}% dall'high",
        f"  • Freshness news per usarle come catalyst: max {p['news_freshness_min_hours']}h fa",
        "",
        "Se la conviction è SOTTO la soglia minima → NO TRADE, anche se",
        "il setup tecnico sembra interessante. Aspetta più segnali.",
        "",
        "Se l'allocazione richiesta SUPERA il cap → riduci la size, non saltare",
        "il trade (a meno che non scenda sotto la min_confidence).",
        "",
        f"IMPORTANTE per crypto: il sub-cap di {cap_crypto} posizioni e' uno",
        "stretto, non un suggerimento. Anche se il cap globale e' piu' alto,",
        "le crypto hanno volatilita' tale da giustificare un limite separato.",
        "█" * 60,
        "",
    ]
    return "\n".join(lines)


# ─── Validazione execute_trade ──────────────────────────────────────────────

def _resolve_recovery(recovery: bool | None) -> bool:
    """Recovery mode attivo? Se il chiamante non lo specifica (None), lo legge
    dal governor (risk_state.is_in_recovery_mode), così l'enforcement #13 vale
    per TUTTI i path senza che ogni chiamante debba ricordarsi di passarlo.
    Fail-safe: su errore → False (resta il profilo base, niente over-block)."""
    if recovery is not None:
        return bool(recovery)
    try:
        import risk_state as _rs
        return bool(_rs.is_in_recovery_mode())
    except Exception:
        return False


def effective_sl_bounds(asset_class: str, recovery: bool | None = None) -> tuple[float, float]:
    """Range SL [min, max]% del profilo attivo, GIÀ clampato per recovery mode
    (#13: in recovery lo SL massimo scende a DEFAULT_RECOVERY_SL_MAX_PCT).

    Unica fonte di verità: la usano sia validate_sl_range sia l'auto-set
    d'ufficio dei Decision (equity e crypto), così il midpoint impostato non
    viola mai il range poi verificato. Il clamp NON inverte il range: se
    sl_min > rec_max collassa a [sl_min, sl_min] = lo SL più stretto consentito.
    """
    p = get_active_profile()
    is_crypto = asset_class == "crypto"
    sl_min = float(p["sl_min_pct_crypto"] if is_crypto else p["sl_min_pct_equity"])
    sl_max = float(p["sl_max_pct_crypto"] if is_crypto else p["sl_max_pct_equity"])
    if _resolve_recovery(recovery):
        try:
            import risk_state as _rs
            rec_max = float(getattr(_rs, "DEFAULT_RECOVERY_SL_MAX_PCT", 5.0))
        except Exception:
            rec_max = 5.0
        sl_max = min(sl_max, max(rec_max, sl_min))
    return sl_min, sl_max


def validate_trade(
    *,
    asset_class: str,
    confidence: float | None,
    allocation_pct: float | None,
    open_positions_count: int | None,
    portfolio_drawdown_pct: float | None = None,
    tier: str = "core",
    satellite_exposure_pct: float | None = None,
    recovery: bool | None = None,
) -> tuple[bool, str]:
    """
    Validazione hard del trade contro il profilo attivo.

    recovery: se True (o None + recovery attivo nel governor) il pavimento di
              confidence sale a DEFAULT_RECOVERY_CONFIDENCE_FLOOR (#13).

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

    # Cintura SATELLITE (universo esteso): sizing ridotto. tier='core' (default)
    # = comportamento INVARIATO. Non tocca il crypto.
    if tier == "satellite" and not is_crypto:
        max_pos = max_pos * float(p.get("satellite_sizing_mult", 0.4))

    # 1. Confidence
    # FAIL-CLOSED: un governatore di rischio non deve MAI saltare un controllo
    # su input anomalo (prima `except: pass` lasciava passare il trade; un NaN
    # passava perché ogni confronto con NaN è False).
    # #13: in RECOVERY mode il pavimento di confidence sale al floor recovery
    # (prima il vincolo stava SOLO nel prompt, mai applicato in codice).
    in_recovery = _resolve_recovery(recovery)
    min_conf = float(p["min_confidence"])
    if in_recovery:
        try:
            import risk_state as _rs
            min_conf = max(min_conf,
                           float(getattr(_rs, "DEFAULT_RECOVERY_CONFIDENCE_FLOOR", 0.75)))
        except Exception:
            min_conf = max(min_conf, 0.75)
    if confidence is not None:
        try:
            cf = float(confidence)
        except (TypeError, ValueError):
            return False, f"Confidence non valida ({confidence!r}) — blocco per sicurezza."
        if math.isnan(cf) or math.isinf(cf):
            return False, "Confidence non finita (NaN/Inf) — blocco per sicurezza."
        if cf < min_conf - 1e-6:
            _rec = " (RECOVERY)" if in_recovery else ""
            return False, (
                f"Confidence {cf:.2f} sotto il minimo del profilo "
                f"{p['label']}{_rec} ({min_conf:.2f}). NO TRADE."
            )

    # 2. Allocation
    if allocation_pct is not None:
        try:
            alloc = float(allocation_pct)
        except (TypeError, ValueError):
            return False, f"Allocazione non valida ({allocation_pct!r}) — blocco per sicurezza."
        if math.isnan(alloc) or math.isinf(alloc):
            return False, "Allocazione non finita (NaN/Inf) — blocco per sicurezza."
        if alloc > max_pos + 1e-6:
            return False, (
                f"Allocazione {alloc:.1f}% supera il cap del profilo "
                f"{p['label']} ({max_pos:.1f}% per {asset_class}). "
                f"Riduci la size."
            )

    # 2b. Cap di esposizione SATELLITE aggregata (solo universo esteso).
    if (tier == "satellite" and satellite_exposure_pct is not None
            and allocation_pct is not None):
        try:
            cap = float(p.get("max_satellite_exposure_pct", 100.0))
            projected = float(satellite_exposure_pct) + float(allocation_pct)
        except (TypeError, ValueError):
            return False, "Esposizione satellite non valida — blocco per sicurezza."
        if projected > cap + 1e-6:
            return False, (
                f"Esposizione satellite {projected:.1f}% (attuale "
                f"{float(satellite_exposure_pct):.1f}% + nuovo {float(allocation_pct):.1f}%) "
                f"supera il cap del profilo {p['label']} ({cap:.1f}%). Riduci o salta."
            )

    # 2b. Cap di esposizione SATELLITE aggregata (solo universo esteso).
    if (tier == "satellite" and satellite_exposure_pct is not None
            and allocation_pct is not None):
        try:
            cap = float(p.get("max_satellite_exposure_pct", 100.0))
            projected = float(satellite_exposure_pct) + float(allocation_pct)
            if projected > cap + 1e-6:
                return False, (
                    f"Esposizione satellite {projected:.1f}% (attuale "
                    f"{float(satellite_exposure_pct):.1f}% + nuovo {float(allocation_pct):.1f}%) "
                    f"supera il cap del profilo {p['label']} ({cap:.1f}%). Riduci o salta."
                )
        except (TypeError, ValueError):
            pass

    # 3. Max open positions: per crypto usiamo il cap specifico (piu' stretto),
    # per equity il generale. NB: open_positions_count deve essere il count
    # specifico per la asset_class (vedi orchestrator).
    if open_positions_count is not None:
        try:
            n = int(open_positions_count)
        except (TypeError, ValueError):
            return False, f"Conteggio posizioni non valido ({open_positions_count!r}) — blocco per sicurezza."
        if is_crypto:
            # Crypto cap dedicato (Conservativo 2 / Moderato 3 / Aggressivo 5)
            cap = int(p.get("max_open_positions_crypto", p["max_open_positions"]))
            if n >= cap:
                return False, (
                    f"Già {n} posizioni CRYPTO aperte; il profilo "
                    f"{p['label']} limita a {cap} posizioni crypto. "
                    f"Chiudine una prima di aprirne un'altra."
                )
        else:
            # Equity/ETF: cap generale
            if n >= p["max_open_positions"]:
                return False, (
                    f"Già {n} posizioni aperte; il profilo {p['label']} "
                    f"limita a {p['max_open_positions']}. Chiudi prima."
                )

    # 4. Portfolio drawdown stop
    if portfolio_drawdown_pct is not None:
        try:
            dd = float(portfolio_drawdown_pct)
        except (TypeError, ValueError):
            return False, f"Drawdown non valido ({portfolio_drawdown_pct!r}) — blocco per sicurezza."
        if math.isnan(dd) or math.isinf(dd):
            return False, "Drawdown non finito (NaN/Inf) — blocco per sicurezza."
        if dd > p["max_portfolio_drawdown_pct"]:
            return False, (
                f"Drawdown portfolio {dd:.1f}% supera il limite del "
                f"profilo {p['label']} ({p['max_portfolio_drawdown_pct']:.1f}%). "
                f"Solo chiusure ammesse, NESSUNA nuova posizione."
            )

    return True, "ok"


def validate_sl_range(
    *,
    asset_class: str,
    entry_price: float,
    sl_price: float,
    side: str = "long",
    recovery: bool | None = None,
) -> tuple[bool, str]:
    """
    Verifica che la distanza SL sia nel range [min, max] del profilo.

    side: 'long' o 'short'
    recovery: None = auto (legge il governor). In recovery il max si stringe (#13).
    """
    p = get_active_profile()
    # Range effettivo, già clampato per recovery mode (#13). Unica fonte di
    # verità condivisa con l'auto-set d'ufficio dei Decision (equity + crypto).
    sl_min, sl_max = effective_sl_bounds(asset_class, recovery=recovery)

    if entry_price <= 0 or sl_price <= 0:
        return False, "Prezzo entry o SL non valido"

    if side.lower() == "short":
        # Short: SL sopra l'entry
        dist_pct = ((sl_price - entry_price) / entry_price) * 100.0
    else:
        # Long: SL sotto l'entry
        dist_pct = ((entry_price - sl_price) / entry_price) * 100.0

    # Tolleranza 1e-6 sul bordo (coerente con validate_trade): uno SL esattamente
    # al limite non va rifiutato per rumore float — conta soprattutto quando il
    # range recovery collassa a un punto (sl_min == sl_max) e l'auto-set mette
    # proprio il midpoint = quel bordo.
    if dist_pct < sl_min - 1e-6:
        return False, (
            f"SL troppo stretto ({dist_pct:.1f}%); profilo {p['label']} "
            f"richiede min {sl_min:.1f}% ({asset_class})."
        )
    if dist_pct > sl_max + 1e-6:
        return False, (
            f"SL troppo ampio ({dist_pct:.1f}%); profilo {p['label']} "
            f"limita a max {sl_max:.1f}% ({asset_class})."
        )
    return True, "ok"
