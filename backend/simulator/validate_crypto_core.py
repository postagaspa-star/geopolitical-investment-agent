"""
Validazione Fase 4 — harness DETERMINISTICO del Crypto Signal Core sugli
scenari crypto reali (Luna, FTX, China-ban, ecc.), usando il modulo metrics.py
del progetto.

COSA DIMOSTRA (e cosa NO):
- DIMOSTRA, senza alcuna chiamata LLM e a costo zero, la proprieta' centrale
  del redesign: il core ANCORA la perdita per-trade al rischio fisso del profilo
  (risk_per_trade_pct via SL obbligatorio ATR-based), quindi il drawdown di
  portafoglio resta limitato anche quando OGNI trade va contro.
- Confronta due curve equity sugli stessi scenari:
    A) "core_bound"  → size risk-based, SL sempre presente → perdita ~ risk%/trade
    B) "r1_legacy_naive" → size ~max_position% NAV, SL assente → cavalca il crash
  e calcola max_drawdown / Sharpe con simulator/metrics.py (gli stessi KPI del
  Simulator vero).
- NON e' un backtest con candele storiche reali ne' un A/B con DeepSeek-R1 vivo:
  quello richiede API key a pagamento ed e' lo step gated sul "live". Questa e'
  una validazione STRUTTURALE delle garanzie di rischio del core.

Uso:  python -m simulator.validate_crypto_core   (dalla cartella backend/)
"""
from __future__ import annotations

import os
import sys

# backend/ sul path quando eseguito come script diretto
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from agents import crypto_signal_core as core
from simulator import metrics
from simulator.crypto_scenarios import CRYPTO_SCENARIOS

INITIAL_NAV = 100_000.0


def _terminal_move_pct(scenario: dict) -> float:
    """Stima della variazione di prezzo dell'asset principale nel periodo.

    Usa change_7d come proxy del movimento near-term documentato. Per gli
    scenari 'crash' applichiamo l'aggravamento documentato (i reveal citano
    -26% BTC su Luna, -75% SOL su FTX, ecc.): prendiamo il piu' severo tra
    change_7d e una stima prudente di crash, così la curva legacy riflette il
    rischio reale di cavalcare l'evento senza stop.
    """
    md = scenario.get("market_data") or []
    if not md:
        return 0.0
    main = md[0]
    chg7 = float(main.get("change_7d") or 0.0)
    if scenario.get("category") == "crash":
        # peggiora il near-term verso il drawdown documentato dell'evento
        return min(chg7, -25.0)
    return chg7


def _core_decision_for(scenario: dict, move: float, signal_mode: str):
    """Costruisce indicatori per il core. signal_mode controlla la RELAZIONE coi
    fatti (il movimento reale `move`), cosi' l'harness non testa solo i casi
    facili concordi (#34):
      - 'concordant': segnali nella direzione del movimento → il core ci prende;
      - 'discordant': segnali OPPOSTI al movimento → il core sbaglia, ma lo SL
        gli limita la perdita (e' la proprieta' che il gate deve dimostrare);
      - 'neutral'  : segnali misti → il core dovrebbe astenersi (HOLD).
    """
    md = (scenario.get("market_data") or [{}])[0]
    price = float(md.get("price_t0") or 100.0)
    atr = price * 0.04  # 4% ATR tipico crypto
    ticker = md.get("ticker", "BTC-USD")

    if signal_mode == "neutral":
        details = {
            "current_price": price, "atr": atr,
            "signal_count": {"bullish": 2, "bearish": 2},
            "signal": "HOLD", "trend": "RANGING",
            "rsi_14": 50, "candlestick_setup": "doji",
        }
        return core.decide(ticker=ticker, details=details, nav=INITIAL_NAV,
                           btc_daily_closes=[100.0 for _ in range(200)])

    # Direzione INDOTTA nel core: concordant = come il movimento; discordant =
    # opposta (il core sbaglia di proposito, per testare il taglio dello SL).
    induce_up = (move > 0) if signal_mode == "concordant" else (move <= 0)
    if induce_up:
        details = {
            "current_price": price, "atr": atr,
            "signal_count": {"bullish": 4, "bearish": 0},
            "signal": "BUY", "trend": "TRENDING_UP",
            "rsi_14": 34, "candlestick_setup": "bullish hammer",
        }
        btc = [100 + i for i in range(200)]  # regime bull
    else:
        details = {
            "current_price": price, "atr": atr,
            "signal_count": {"bullish": 0, "bearish": 4},
            "signal": "SELL", "trend": "TRENDING_DOWN",
            "rsi_14": 72, "candlestick_setup": "bearish engulfing",
        }
        btc = [200 - i * 0.5 for i in range(200)]  # regime bear
    return core.decide(ticker=ticker, details=details, nav=INITIAL_NAV,
                       btc_daily_closes=btc)


def build_equity_curves():
    """Due curve equity step-by-step sugli scenari, una per motore."""
    params = core.CoreParams()  # default = profilo moderate
    nav_core = INITIAL_NAV
    nav_legacy = INITIAL_NAV
    curve_core = [{"value": nav_core}]
    curve_legacy = [{"value": nav_legacy}]
    hist_core: list[dict] = []
    hist_legacy: list[dict] = []
    rows = []

    # Modi a rotazione: il core affronta scenari concordi, DISCORDI e neutri
    # (prima solo concordi → il core non poteva mai sbagliare = gate vacuo, #34).
    modes = ["concordant", "discordant", "neutral"]
    for i, sc in enumerate(CRYPTO_SCENARIOS):
        move = _terminal_move_pct(sc) / 100.0  # frazione (es. -0.26)
        mode = modes[i % len(modes)]
        dec = _core_decision_for(sc, move, mode)

        # ── Motore A: core_bound ──
        #  - flat/blocked: 0
        #  - direzione concorde col movimento: +|move| sulla size (cap)
        #  - direzione discorde: perdita LIMITATA dallo SL (risk_per_trade)
        if dec.blocked or dec.action == "HOLD":
            pnl_core = 0.0
            core_outcome = "flat"
        else:
            core_long = dec.action == "BUY"
            move_up = move > 0
            notional = dec.size_pct_nav / 100.0 * nav_core
            if core_long == move_up:
                pnl_core = notional * abs(move)          # direzione giusta
                core_outcome = "win"
            else:
                # perdita tagliata allo SL: rischio = risk_per_trade del NAV
                pnl_core = -(params.risk_per_trade_pct / 100.0 * nav_core)
                core_outcome = "stopped"
        nav_core += pnl_core
        curve_core.append({"value": nav_core})
        hist_core.append({"action": "CLOSE", "realized_pnl": pnl_core,
                          "confidence": "high"})

        # ── Motore B: r1_legacy_naive ──
        # CONFRONTO EQUO (#34): STESSA direzione del core, ma size ~max_position%
        # NAV e SENZA stop → subisce l'INTERO movimento (anche avverso). Prima
        # era hardcoded sempre LONG: uno strawman che perdeva solo nei crash.
        if dec.blocked or dec.action == "HOLD":
            pnl_legacy = 0.0          # senza direzione dal core, niente trade
        else:
            legacy_long = dec.action == "BUY"
            legacy_notional = params.max_position_pct_nav / 100.0 * nav_legacy
            pnl_legacy = legacy_notional * (move if legacy_long else -move)
        nav_legacy += pnl_legacy
        curve_legacy.append({"value": nav_legacy})
        hist_legacy.append({"action": "CLOSE", "realized_pnl": pnl_legacy,
                            "confidence": "high"})

        rows.append({
            "scenario": sc["id"], "category": sc.get("category", "?"),
            "mode": mode,
            "move_pct": round(move * 100, 1),
            "core_action": dec.action, "core_outcome": core_outcome,
            "core_conv": dec.conviction, "core_regime": dec.regime,
            # #34: SOLO le vere aperture (gli HOLD NON contano come SL=PASS).
            "core_has_sl": bool(dec.stop_loss),
            "pnl_core": round(pnl_core, 0), "pnl_legacy": round(pnl_legacy, 0),
        })

    return curve_core, curve_legacy, hist_core, hist_legacy, rows


def main():
    curve_core, curve_legacy, hist_core, hist_legacy, rows = build_equity_curves()
    ppy = metrics.ANNUALIZATION_CRYPTO

    # final_valuation vuoto: tutte le posizioni sono chiuse a ogni step nel
    # nostro modello (realized_pnl per-step), quindi non ci sono open positions.
    m_core = metrics.compute_all_metrics(curve_core, hist_core, {},
                                         periods_per_year=ppy)
    m_legacy = metrics.compute_all_metrics(curve_legacy, hist_legacy, {},
                                           periods_per_year=ppy)

    print("=" * 74)
    print("VALIDAZIONE FASE 4 — Crypto Signal Core vs R1-legacy (naive) "
          "su scenari reali")
    print("=" * 74)
    print(f"{'scenario':<28}{'cat':<12}{'move%':>7}  "
          f"{'core':>6} {'esito':>8} {'SL':>3}")
    for r in rows:
        print(f"{r['scenario']:<28}{r['category']:<12}{r['move_pct']:>7}  "
              f"{r['core_action']:>6} {r['core_outcome']:>8} "
              f"{'Y' if r['core_has_sl'] else 'N':>3}")

    print("-" * 74)
    cc = m_core["max_drawdown"]["max_drawdown_pct"]
    cl = m_legacy["max_drawdown"]["max_drawdown_pct"]
    print(f"NAV finale     core_bound={curve_core[-1]['value']:>12,.0f}   "
          f"r1_legacy={curve_legacy[-1]['value']:>12,.0f}")
    print(f"Max Drawdown   core_bound={cc:>11}%   r1_legacy={cl:>11}%")
    print(f"Sharpe         core_bound={str(m_core['sharpe_ratio']):>12}   "
          f"r1_legacy={str(m_legacy['sharpe_ratio']):>12}")

    # Verdetto strutturale (gate del piano: il core deve ridurre il drawdown).
    # #34: lo SL si verifica SOLO sulle vere aperture (BUY/SHORT); gli HOLD non
    # contano come PASS, e serve almeno un'apertura (altrimenti il gate e' vacuo).
    openings = [r for r in rows if r["core_action"] != "HOLD"]
    all_sl = bool(openings) and all(r["core_has_sl"] for r in openings)
    dd_ok = abs(cc) <= abs(cl)
    print("-" * 74)
    print(f"[{'PASS' if all_sl else 'FAIL'}] Ogni apertura del core ha uno stop-loss "
          f"({len(openings)} aperture, {len(rows) - len(openings)} HOLD)")
    print(f"[{'PASS' if dd_ok else 'FAIL'}] Drawdown core <= drawdown legacy "
          f"({abs(cc):.1f}% vs {abs(cl):.1f}%)")
    print("=" * 74)
    print("NOTA: confronto STRUTTURALE (no LLM). L'A/B con DeepSeek-R1 vivo "
          "richiede\nAPI key ed e' lo step gated sul live (flag "
          "crypto_decision_engine=core_bound).")
    return 0 if (all_sl and dd_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
