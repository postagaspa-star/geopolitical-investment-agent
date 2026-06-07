"""
health_check.py — Cruscotto di salute automatico del sistema.

Scopo: trasformare i bug SILENZIOSI in allarmi VISIBILI. Invece di
scoprire un problema vedendo un numero assurdo nella dashboard (a danno
gia' fatto), il sistema gira questi controlli su schedule e via endpoint
e dice lui se qualcosa non torna.

Tutti i check sono READ-ONLY. L'unica scrittura e' un agent_log
HEALTH_DIGEST con l'esito (per avere storico e per la UI).

Stati per ogni check:
  OK        → tutto a posto
  WARNING   → sospetto, da indagare ma non urgente
  CRITICAL  → problema grave (soldi/contabilita')
  ERROR     → il check stesso non e' riuscito a girare

Lo stato complessivo e' il peggiore tra i singoli check.
"""
import json
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

# Fasi di allarme che i vari fix scrivono su agent_logs. Se compaiono nelle
# ultime 24h, il cruscotto le riassume.
_ALERT_PHASES = {
    "NAV_DRIFT_ALERT": "NAV si discosta dalla mediana storica",
    "TRADE_LOG_FAILED": "trade eseguito ma non loggato (gap audit)",
    "TECH_CLONE_DETECTED": "dati tecnici identici tra ticker diversi",
    "DECISION_RISK_REJECTED": "trade equity bloccato dal risk profile",
    "DECISION_CRYPTO_RISK_REJECTED": "trade crypto bloccato dal risk profile",
}

# Soglie
_INVARIANT_TOL_USD = 1.0       # scostamento max cassa+posizioni vs total_value
_CHART_SYNC_MAX_PCT = 5.0      # scostamento max ultimo snapshot vs NAV live
_AGENT_LOGS_SCAN_LIMIT = 150   # quanti log leggere (egress-conscious)
_CASH_AUDIT_SCAN_LIMIT = 100


def _check(name, status, detail, **extra):
    d = {"name": name, "status": status, "detail": detail}
    d.update(extra)
    return d


def _parse_ts(ts):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def run_health_checks() -> dict:
    """Esegue tutti i controlli e ritorna un report. Scrive HEALTH_DIGEST."""
    import database
    import portfolio as _pf

    checks = []
    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)

    # ── 1. Invariante cassa: total_value == cash + Σ posizioni direction-aware ─
    try:
        p = database.get_portfolio() or {}
        cash = float(p.get("cash_balance") or 0)
        saved_total = float(p.get("total_value") or 0)
        positions = database.get_positions() or []
        truth = cash + _pf.compute_positions_value(positions)
        delta = saved_total - truth
        if abs(delta) < _INVARIANT_TOL_USD:
            checks.append(_check(
                "cash_invariant", "OK",
                f"total_value coerente (delta ${delta:.2f})",
                cash=round(cash, 2), saved_total=round(saved_total, 2),
                truth=round(truth, 2)))
        else:
            checks.append(_check(
                "cash_invariant", "CRITICAL",
                f"total_value INCOERENTE: salvato ${saved_total:,.2f} vs vero "
                f"${truth:,.2f} (delta ${delta:,.2f}). Forza un recompute "
                f"(GET /api/portfolio) o controlla NAV_DRIFT_ALERT.",
                cash=round(cash, 2), saved_total=round(saved_total, 2),
                truth=round(truth, 2)))
    except Exception as e:
        checks.append(_check("cash_invariant", "ERROR", f"check fallito: {e}"))

    # ── 2. Sanity portafoglio: cassa/NAV non assurdi ──
    try:
        p = database.get_portfolio() or {}
        cash = float(p.get("cash_balance") or 0)
        total = float(p.get("total_value") or 0)
        problems = []
        if cash < 0:
            problems.append(f"cassa NEGATIVA ${cash:,.2f}")
        if total <= 0:
            problems.append(f"NAV non positivo ${total:,.2f}")
        if problems:
            checks.append(_check("portfolio_sanity", "CRITICAL", "; ".join(problems),
                                 cash=round(cash, 2), total=round(total, 2)))
        else:
            checks.append(_check("portfolio_sanity", "OK",
                                 f"cassa ${cash:,.2f}, NAV ${total:,.2f}"))
    except Exception as e:
        checks.append(_check("portfolio_sanity", "ERROR", f"check fallito: {e}"))

    # ── 3. Sync grafico: ultimo snapshot ≈ NAV live ──
    try:
        p = database.get_portfolio() or {}
        live_total = float(p.get("total_value") or 0)
        hist = database.get_portfolio_history(days=2) or []
        if hist and live_total > 0:
            last_snap = float(hist[-1].get("total_value") or 0)
            drift_pct = abs(last_snap - live_total) / live_total * 100
            if drift_pct <= _CHART_SYNC_MAX_PCT:
                checks.append(_check(
                    "chart_sync", "OK",
                    f"ultimo snapshot ${last_snap:,.2f} ≈ NAV ${live_total:,.2f} "
                    f"({drift_pct:.1f}%)"))
            else:
                checks.append(_check(
                    "chart_sync", "WARNING",
                    f"grafico disallineato: snapshot ${last_snap:,.2f} vs NAV "
                    f"${live_total:,.2f} ({drift_pct:.1f}%)"))
        else:
            checks.append(_check("chart_sync", "WARNING",
                                 "nessuno snapshot recente o NAV non disponibile"))
    except Exception as e:
        checks.append(_check("chart_sync", "ERROR", f"check fallito: {e}"))

    # ── 4. Allarmi recenti negli agent_logs (24h) ──
    try:
        logs = database.get_agent_logs(limit=_AGENT_LOGS_SCAN_LIMIT) or []
        counts = {}
        for log in logs:
            phase = log.get("phase") or ""
            if phase not in _ALERT_PHASES:
                continue
            dt = _parse_ts(log.get("timestamp"))
            if dt is not None and dt < cutoff_24h:
                continue
            counts[phase] = counts.get(phase, 0) + 1
        if counts:
            # TRADE_LOG_FAILED e NAV_DRIFT = critici; il resto warning.
            critical = any(k in counts for k in ("TRADE_LOG_FAILED", "NAV_DRIFT_ALERT"))
            status = "CRITICAL" if critical else "WARNING"
            detail = "; ".join(f"{_ALERT_PHASES[k]}: {v}" for k, v in counts.items())
            checks.append(_check("recent_alerts", status,
                                 f"allarmi nelle ultime 24h — {detail}", counts=counts))
        else:
            checks.append(_check("recent_alerts", "OK",
                                 "nessun allarme nelle ultime 24h"))
    except Exception as e:
        checks.append(_check("recent_alerts", "ERROR", f"check fallito: {e}"))

    # ── 5. Provenienza cassa: mutazioni non attribuite a un trade (24h) ──
    # Richiede la tabella cash_audit_log (migration add_cash_audit_log.sql).
    # Se assente, get_cash_audit_log ritorna [] → check informativo.
    try:
        rows = []
        if hasattr(database, "get_cash_audit_log"):
            rows = database.get_cash_audit_log(limit=_CASH_AUDIT_SCAN_LIMIT) or []
        if not rows:
            checks.append(_check("cash_provenance", "OK",
                                 "nessuna mutazione cassa da tracciare "
                                 "(o cash_audit_log non ancora attivo)"))
        else:
            suspicious = []
            for r in rows:
                dt = _parse_ts(r.get("timestamp"))
                if dt is not None and dt < cutoff_24h:
                    continue
                reason = r.get("reason") or ""
                # 'update_portfolio' generico = mutazione non attribuita a un
                # trade o a un'azione admin esplicita → sospetta.
                if reason in ("update_portfolio", ""):
                    try:
                        if abs(float(r.get("delta") or 0)) >= 1.0:
                            suspicious.append({
                                "delta": r.get("delta"),
                                "timestamp": r.get("timestamp"),
                                "reason": reason or "(vuoto)",
                            })
                    except (TypeError, ValueError):
                        pass
            if suspicious:
                checks.append(_check(
                    "cash_provenance", "WARNING",
                    f"{len(suspicious)} mutazioni di cassa non attribuite a un "
                    f"trade nelle 24h — possibile cassa che cambia 'da sola'",
                    sample=suspicious[:5]))
            else:
                checks.append(_check("cash_provenance", "OK",
                                     "ogni mutazione di cassa ha una causa nota"))
    except Exception as e:
        checks.append(_check("cash_provenance", "ERROR", f"check fallito: {e}"))

    # ── 6. Memoria (RSS) vicino al tetto 512MB di Render ──
    try:
        import memory_utils
        rss = memory_utils.get_rss_mb()
        if rss is None:
            checks.append(_check("memory_rss", "OK", "RSS non determinabile su questa piattaforma"))
        elif rss >= 480:
            checks.append(_check("memory_rss", "CRITICAL",
                                 f"RSS {rss:.0f}MB vicino al tetto 512MB → rischio OOM imminente",
                                 rss_mb=rss))
        elif rss >= 400:
            checks.append(_check("memory_rss", "WARNING",
                                 f"RSS {rss:.0f}MB (tetto 512MB) — margine ridotto",
                                 rss_mb=rss))
        else:
            checks.append(_check("memory_rss", "OK", f"RSS {rss:.0f}MB / 512MB", rss_mb=rss))
    except Exception as e:
        checks.append(_check("memory_rss", "ERROR", f"check fallito: {e}"))

    # ── Verdetto complessivo = peggiore tra i singoli ──
    statuses = [c["status"] for c in checks]
    if "CRITICAL" in statuses:
        overall = "CRITICAL"
    elif "WARNING" in statuses or "ERROR" in statuses:
        overall = "WARNING"
    else:
        overall = "OK"

    # Step 6 — visibilità rollout: modalità universo + ultima diversità copertura.
    try:
        import universe as _u
        universe_mode = "EXPANDED" if _u.expanded_universe_enabled() else "CORE"
    except Exception:
        universe_mode = "CORE"
    latest_diversity = None
    try:
        import diversity as _div
        _m = _div.collect_and_compute(limit=1500)
        latest_diversity = {
            "breadth": _m.get("breadth"),
            "effective_n": _m.get("effective_n"),
            "satellite_share": _m.get("satellite_share"),
        }
    except Exception:
        pass

    report = {
        "status": overall,
        "timestamp": now.isoformat(),
        "checks": checks,
        "universe_mode": universe_mode,
        "latest_diversity": latest_diversity,
        "summary": "; ".join(
            f"{c['name']}={c['status']}" for c in checks
        ),
    }

    # Scrivi il digest su agent_logs (storico + visibile in UI log)
    try:
        import database as _db
        _db.insert_agent_log("health_check", "HEALTH_DIGEST",
                             json.dumps(report, default=str))
    except Exception:
        pass

    if overall != "OK":
        problems = [f"{c['name']}={c['status']}" for c in checks
                    if c["status"] != "OK"]
        logger.warning("[HEALTH] stato=%s — %s", overall, "; ".join(problems))
    else:
        logger.info("[HEALTH] stato=OK (%d check passati)", len(checks))

    return report


def get_latest_digest() -> dict | None:
    """Ritorna l'ultimo HEALTH_DIGEST salvato (senza ri-eseguire i check).
    Utile per la UI: poll a basso costo invece di ricalcolare ogni volta.
    """
    try:
        import database
        logs = database.get_agent_logs(limit=60) or []
        for log in logs:
            if log.get("phase") == "HEALTH_DIGEST":
                content = log.get("content")
                if content:
                    return json.loads(content)
    except Exception as e:
        logger.debug("get_latest_digest fallito: %s", e)
    return None
