"""
liquidity.py — Guardrail di liquidità / qualità dati (Step 3 del piano universo).

Gli asset meno efficienti pagano PERCHÉ sono più rischiosi: slippage, dati
sporchi, esecuzione difficile. Questi guardrail decidono se un nome (in pratica
un candidato SATELLITE) è abbastanza liquido e pulito da poter essere proposto.

Principio: NON serve una fonte dati nuova — l'ADV (Average Dollar Volume) si
calcola dalle barre OHLCV già recuperate (volume × close). Funzioni PURE,
senza dipendenze dal resto del backend (così niente import circolari e test
senza rete/DB).

Usati da:
  - rotation_scan.py  → calcola adv_usd_20d per riga
  - candidates.py     → ammette un satellite solo se passa il floor + qualità
  - (Step 3) il sizing ridotto del satellite vive in risk_profile.py

Quando il flag EXPANDED_UNIVERSE è OFF nessuno di questi è sul percorso del
core: i candidati restano quelli di sempre.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


# Suffissi di borse non-USA non supportate (coerente con universe.NON_US_SUFFIXES).
_NON_US_SUFFIXES = (".MI", ".PA", ".DE", ".L", ".AS", ".HK", ".TO")


def compute_adv_usd(closes: List[float], volumes: List[float], n: int = 20) -> Optional[float]:
    """
    Average Dollar Volume sugli ultimi n giorni = media( close_i × volume_i ).
    È il proxy standard di liquidità: quanti $ scambiano al giorno.
    Ritorna None se non ci sono abbastanza barre valide.
    """
    if not closes or not volumes:
        return None
    m = min(len(closes), len(volumes))
    if m == 0:
        return None
    c = closes[-n:] if m >= n else closes[-m:]
    v = volumes[-n:] if m >= n else volumes[-m:]
    pairs = [(float(ci), float(vi)) for ci, vi in zip(c, v)
             if ci and vi and ci > 0 and vi > 0]
    if not pairs:
        return None
    return sum(ci * vi for ci, vi in pairs) / len(pairs)


def adv_usd_from_bars(bars: List[Dict[str, Any]], n: int = 20) -> Optional[float]:
    """Come compute_adv_usd ma partendo dalle barre OHLCV (dict con close/volume)."""
    if not bars:
        return None
    closes = [b.get("close") for b in bars]
    volumes = [b.get("volume") for b in bars]
    return compute_adv_usd(
        [c for c in closes if c is not None],
        [v for v in volumes if v is not None],
        n=n,
    )


def passes_liquidity_floor(adv_usd: Optional[float], min_adv_usd: float) -> bool:
    """True se l'ADV in $ è almeno il floor richiesto."""
    return adv_usd is not None and adv_usd >= min_adv_usd


def slippage_bps(
    adv_usd: Optional[float],
    trade_value_usd: float,
    base_bps: float = 5.0,
    impact_coef: float = 50.0,
    max_bps: float = 200.0,
) -> float:
    """
    Slippage ONESTO stimato (bps) = base + impact_coef * (trade_value / ADV).

    Più la size del trade è grande rispetto al volume giornaliero in $, più
    impatto sul prezzo si paga. Per nomi molto liquidi (ADV enorme) il ratio è
    ~0 → ~base; per nomi sottili cresce, fino a max_bps. ADV ignoto/0 → max_bps
    (prudente: se non so quanto è liquido, assumo il peggio).

    Usato dal backtest dell'universo esteso, dove un modello realistico è
    indispensabile: altrimenti i risultati sugli illiquidi mentono per eccesso.
    """
    if not adv_usd or adv_usd <= 0:
        return max_bps
    ratio = max(0.0, float(trade_value_usd) / float(adv_usd))
    return min(max_bps, base_bps + impact_coef * ratio)


def validate_symbol(ticker: str, *, allow_non_us: bool = False) -> Tuple[bool, str]:
    """
    Validazione formato simbolo. Rifiuta vuoti e — salvo override — i suffissi
    di borse non-USA (slippage/dati/esecuzione fuori dal nostro perimetro dati).
    Ritorna (ok, reason).
    """
    t = (ticker or "").strip().upper()
    if not t:
        return False, "ticker vuoto"
    if not allow_non_us and any(t.endswith(s) for s in _NON_US_SUFFIXES):
        return False, f"suffisso non-USA non supportato ({t})"
    if len(t) > 12:
        return False, f"ticker anomalo ({t})"
    return True, "ok"


def _is_corrupted(closes: List[float]) -> bool:
    """
    Stessa logica di data_fetchers._is_ohlcv_corrupted ma su una lista di close
    (reimplementata qui per non importare data_fetchers, che tira dipendenze
    pesanti). ≥90% close identici = feed stantio/corrotto.
    """
    valid = [round(float(c), 2) for c in closes if c and float(c) > 0]
    if len(valid) < 5:
        return False
    from collections import Counter
    _, count = Counter(valid).most_common(1)[0]
    return count >= max(5, int(len(valid) * 0.9))


def ohlcv_quality_ok(
    bars: List[Dict[str, Any]],
    *,
    min_bars: int = 30,
    max_stale_days: int = 5,
    asof_date: Optional[str] = None,
) -> Tuple[bool, str]:
    """
    Gate di qualità dati per ammettere un nome (satellite). BLOCCA (ok=False) se:
      - troppe poche barre (< min_bars) → indicatori inaffidabili;
      - serie corrotta (≥90% close identici);
      - ultima barra troppo vecchia rispetto ad asof_date (se fornita).
    asof_date / le date barre in formato 'YYYY-MM-DD' (confronto lessicografico).
    """
    if not bars or len(bars) < min_bars:
        return False, f"storia insufficiente ({len(bars) if bars else 0} < {min_bars} barre)"
    closes = [b.get("close") for b in bars if b.get("close") is not None]
    if _is_corrupted(closes):
        return False, "serie OHLCV corrotta (>=90% close identici)"
    if asof_date:
        last_date = str(bars[-1].get("date") or "")
        if last_date and last_date < _days_before(asof_date, max_stale_days):
            return False, f"dati stantii (ultima barra {last_date}, asof {asof_date})"
    return True, "ok"


def _days_before(date_str: str, days: int) -> str:
    """Ritorna la stringa 'YYYY-MM-DD' di `days` giorni prima di date_str.
    Difensivo: se il parse fallisce, ritorna una stringa che non blocca."""
    try:
        from datetime import date, timedelta
        y, m, d = (int(x) for x in date_str[:10].split("-"))
        return (date(y, m, d) - timedelta(days=days)).isoformat()
    except Exception:
        return "0000-00-00"
