"""
candidates.py — Layer di assemblaggio candidati (Step 2 del piano universo).

Trasforma la rotation da SIDECAR (oggi solo testo informativo) a DRIVER: estrae
i leader di rotazione per categoria, segnala le coppie anti-correlate e ammette
i candidati SATELLITE liquidi, e li impacchetta in un blocco prompt che il
Decision Agent legge come candidati di prima classe.

Tutto PURO (opera su `rotation_data` già calcolato). È inerte finché il flag
EXPANDED_UNIVERSE è OFF: il chiamante (decision.py) invoca build_candidate_block
solo dentro `if universe.expanded_universe_enabled()`, quindi col flag spento il
prompt resta byte-identico a oggi.

Schema atteso di rotation_data (da rotation_scan.scan_rotation_universe):
  {"rows": [{"ticker","category","rotation_score","rs5d_vs_spy_pct",
             "rsi14","adv_usd_20d", ...}, ...], "data_quality": "ok|degraded"}
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional

import universe
import liquidity


# Coppie macro anti-correlate (categorie del rotation_scan): (forte_atteso_in_riskoff,
# forte_atteso_in_riskon). Lo spread di forza relativa fra le due racconta il regime.
_MACRO_PAIRS = [
    ("defensive", "high_movement_equity", "Difensivi vs Growth/Ciclici (risk-off/on)"),
    ("safe_haven", "sectors", "Safe-haven vs Settori ciclici (flight-to-safety)"),
    ("energy_commodity", "bonds", "Energia/Commodity vs Bond (inflazione vs duration)"),
    ("international", "high_movement_equity", "Internazionale vs Mega-cap USA (rotazione geografica)"),
]


def _rows(rotation_data: Optional[dict]) -> List[dict]:
    if not rotation_data:
        return []
    return [r for r in (rotation_data.get("rows") or []) if isinstance(r, dict) and r.get("ticker")]


def extract_rotation_leaders(
    rotation_data: Optional[dict],
    per_category: Optional[int] = None,
    min_score: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """
    Top-N per categoria per rotation_score. Ritorna una lista di
    {"category", "leaders": [row, ...]} ordinata per forza della categoria.
    """
    per_category = per_category or universe.MAX_ROTATION_LEADERS_PER_CATEGORY
    by_cat: Dict[str, List[dict]] = defaultdict(list)
    for r in _rows(rotation_data):
        if r.get("rotation_score") is None:
            continue
        if min_score is not None and r["rotation_score"] < min_score:
            continue
        by_cat[r.get("category") or "other"].append(r)

    out: List[Dict[str, Any]] = []
    for cat, rows in by_cat.items():
        rows.sort(key=lambda x: x.get("rotation_score") or -1e9, reverse=True)
        out.append({"category": cat, "leaders": rows[:per_category]})
    # categorie con il leader più forte in cima
    out.sort(key=lambda d: (d["leaders"][0].get("rotation_score") if d["leaders"] else -1e9),
             reverse=True)
    return out


def _cat_mean_rs5(rows: List[dict], category: str) -> Optional[float]:
    vals = [r.get("rs5d_vs_spy_pct") for r in rows
            if r.get("category") == category and r.get("rs5d_vs_spy_pct") is not None]
    return (sum(vals) / len(vals)) if vals else None


def extract_rotation_pairs(rotation_data: Optional[dict]) -> List[Dict[str, Any]]:
    """
    Segnali di coppie anti-correlate: per ogni coppia macro calcola lo spread di
    forza relativa media (RS5d) e, se supera la soglia, indica il lato forte e
    il lato debole. È l'edge RELATIVO oltre la direzione assoluta.
    """
    rows = _rows(rotation_data)
    if not rows:
        return []
    out: List[Dict[str, Any]] = []
    for cat_a, cat_b, label in _MACRO_PAIRS:
        rs_a = _cat_mean_rs5(rows, cat_a)
        rs_b = _cat_mean_rs5(rows, cat_b)
        if rs_a is None or rs_b is None:
            continue
        spread = rs_a - rs_b
        if abs(spread) < universe.ROTATION_PAIR_MIN_SPREAD_PCT:
            continue
        strong, weak = (cat_a, cat_b) if spread > 0 else (cat_b, cat_a)
        out.append({
            "label": label,
            "strong_side": strong,
            "weak_side": weak,
            "spread_pct": round(abs(spread), 2),
        })
    out.sort(key=lambda d: d["spread_pct"], reverse=True)
    return out


def extract_satellite_candidates(
    rotation_data: Optional[dict],
    min_adv_usd: Optional[float] = None,
    max_n: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Candidati SATELLITE ammessi: righe dello scan che sono nella cintura
    satellite E superano il floor di liquidità (ADV) E la validazione simbolo.
    Lo scan esclude già le serie corrotte; qui aggiungiamo il filtro liquidità.
    Ordinati per rotation_score desc.
    """
    floor = universe.satellite_min_adv_usd() if min_adv_usd is None else min_adv_usd
    max_n = max_n or universe.MAX_SATELLITE_CANDIDATES
    cands: List[dict] = []
    for r in _rows(rotation_data):
        t = str(r["ticker"]).upper()
        if not universe.is_satellite(t):
            continue
        ok_sym, _ = liquidity.validate_symbol(t)
        if not ok_sym:
            continue
        if not liquidity.passes_liquidity_floor(r.get("adv_usd_20d"), floor):
            continue
        cands.append(r)
    cands.sort(key=lambda x: x.get("rotation_score") or -1e9, reverse=True)
    return cands[:max_n]


def _fmt_pct(v) -> str:
    return "n/d" if v is None else f"{v:+.1f}%"


def _fmt_adv(v) -> str:
    if not v:
        return "n/d"
    return f"${v/1e6:.0f}M/g"


def build_candidate_block(
    rotation_data: Optional[dict],
    focus_tickers: Optional[List[str]] = None,
) -> str:
    """
    Costruisce il blocco prompt 'UNIVERSO ESTESO — CANDIDATI' che rende la
    rotation un DRIVER e ammette i satellite. Ritorna "" se non c'è nulla di
    utile (così il chiamante non appende blocchi vuoti).

    NB: il chiamante invoca questa funzione SOLO col flag ON. Il blocco dichiara
    esplicitamente che i nomi satellite sono tradabili in modalità estesa
    (override mirato del divieto del blocco core), con sizing ridotto.
    """
    rows = _rows(rotation_data)
    if not rows:
        return ""
    if (rotation_data or {}).get("data_quality") not in (None, "ok"):
        # Dati degradati: non promuoviamo candidati su metriche inaffidabili.
        return ""

    leaders = extract_rotation_leaders(rotation_data)
    pairs = extract_rotation_pairs(rotation_data)
    satellites = extract_satellite_candidates(rotation_data)
    if not leaders and not pairs and not satellites:
        return ""

    L: List[str] = []
    L.append("=" * 60)
    L.append("UNIVERSO ESTESO — CANDIDATI DA ROTAZIONE (modalità EXPANDED_UNIVERSE)")
    L.append("=" * 60)
    L.append("Questi candidati derivano dallo scan di rotazione e sono di PRIMA")
    L.append("CLASSE: valutali insieme ai FOCUS TICKERS. Per la cintura SATELLITE")
    L.append("(ETF settoriali/internazionali/tematici/commodity/size) la modalità")
    L.append("estesa AUTORIZZA il trade nonostante il divieto del blocco core,")
    L.append("MA con sizing ridotto (il risk profile applica il moltiplicatore")
    L.append("satellite e un cap di esposizione aggregata).")
    L.append("")

    if pairs:
        L.append("▸ COPPIE ANTI-CORRELATE (edge relativo — long forte / riduci-evita debole):")
        for p in pairs:
            L.append(f"   • {p['label']}: forte={p['strong_side']} vs debole={p['weak_side']} "
                     f"(spread RS5d {p['spread_pct']:.1f}%)")
        L.append("")

    if leaders:
        L.append("▸ LEADER DI ROTAZIONE per categoria (rotation_score = forza relativa):")
        for grp in leaders[:8]:
            names = ", ".join(
                f"{r['ticker']}(score {r.get('rotation_score')}, RS5d {_fmt_pct(r.get('rs5d_vs_spy_pct'))})"
                for r in grp["leaders"]
            )
            if names:
                L.append(f"   • {grp['category']}: {names}")
        L.append("")

    if satellites:
        L.append("▸ CANDIDATI SATELLITE ammessi (liquidità ADV ok — sizing ridotto):")
        for r in satellites:
            L.append(f"   • {r['ticker']} [{universe.satellite_category(r['ticker']) or r.get('category')}] "
                     f"score {r.get('rotation_score')}, RS5d {_fmt_pct(r.get('rs5d_vs_spy_pct'))}, "
                     f"RSI {r.get('rsi14')}, ADV {_fmt_adv(r.get('adv_usd_20d'))}")
        L.append("")

    L.append("ISTRUZIONE: se includi un satellite in asset_candidates, ricorda il")
    L.append("sizing ridotto e motiva con la tesi macro/ciclo + il segnale di rotazione.")
    return "\n".join(L)
