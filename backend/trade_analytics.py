"""
trade_analytics.py — analisi dei trade chiusi (vista Live/Edge).

La matematica canonica (FIFO long/short, fee, regola manual-close) vive ora
in `accounting.py`: UN solo posto, testato. Qui restano:
  1. il wrapper che inietta la commissione LIVE (dalle settings) e delega ad
     accounting.compute_closed_trades — firma invariata per i chiamanti;
  2. confidence_calibration_summary, diagnosi specifica per il prompt del
     Decision Agent.

Consumatori: main.py (edge-tracker, diagnosi confidence) e
agents/decision.py (blocco di auto-calibrazione nel system prompt).

Re-export per retro-compatibilita': MANUAL_CLOSE_CONFIDENCE, is_manual_close.
"""
from accounting import (  # noqa: F401  (re-export: i chiamanti li importano da qui)
    MANUAL_CLOSE_CONFIDENCE,
    is_manual_close,
    compute_closed_trades as _acc_compute_closed_trades,
)


def _live_commission_bps() -> float:
    """Legge la commissione configurata dalle settings (default 10 bps)."""
    try:
        import database
        return float(database.get_setting("commission_bps", "10") or 10)
    except Exception:
        return 10.0


def compute_closed_trades(trades: list,
                          exclude_manual_closes: bool = False,
                          include_fees: bool = True) -> list:
    """Accoppia aperture→chiusure (FIFO long/short separate) e ritorna i
    round-trip con P&L NETTO delle commissioni.

    Firma invariata: inietta la commissione LIVE e delega al modulo canonico
    accounting.compute_closed_trades (vedi la' per la logica completa).

    exclude_manual_closes=True → vista SKILL (esclude chiusure non-AI conf>=100).
    include_fees=False → P&L lordo.
    """
    return _acc_compute_closed_trades(
        trades,
        exclude_manual_closes=exclude_manual_closes,
        include_fees=include_fees,
        commission_bps=_live_commission_bps(),
    )


def confidence_calibration_summary(closed: list) -> dict | None:
    """
    Sintesi COMPATTA della calibrazione confidence↔esito, per iniettarla
    nel system prompt del Decision Agent (auto-feedback). NON e' la
    diagnosi completa (quella resta in main.py): qui solo i numeri che
    servono all'AI per ritarare la propria confidence.

    Ritorna None se i campioni con confidence sono troppo pochi (<20).
    """
    wc = [c for c in (closed or []) if c.get("confidence") is not None]
    n = len(wc)
    if n < 20:
        return None
    confs = [float(c["confidence"]) for c in wc]
    rets = [float(c["pnl_pct"]) for c in wc]
    cmean = sum(confs) / n
    rmean = sum(rets) / n
    cstd = (sum((x - cmean) ** 2 for x in confs) / n) ** 0.5
    rstd = (sum((y - rmean) ** 2 for y in rets) / n) ** 0.5
    if cstd < 1e-9:
        return None
    corr = None
    if rstd > 1e-9:
        cov = sum((confs[i] - cmean) * (rets[i] - rmean)
                  for i in range(n)) / n
        corr = round(cov / (cstd * rstd), 3)
    order = sorted(range(n), key=lambda i: confs[i])
    t = max(1, n // 3)
    low_idx = order[:t]
    high_idx = order[-t:]

    def _avg(idxs):
        return round(sum(rets[i] for i in idxs) / len(idxs), 2)

    low_avg = _avg(low_idx)
    high_avg = _avg(high_idx)
    return {
        "samples": n,
        "correlation": corr,
        "low_conf_avg_return_pct": low_avg,
        "high_conf_avg_return_pct": high_avg,
        "inverted": (corr is not None and corr < 0) or (high_avg < low_avg),
    }
