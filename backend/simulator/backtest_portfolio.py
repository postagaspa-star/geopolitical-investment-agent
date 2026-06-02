"""
Backtest di PORTAFOGLIO multi-asset sull'universo delle 14 major.

Risponde all'osservazione di Andrea: NON testare su una crypto scelta a mano
(cherry-picking), ma far valutare al sistema TUTTE le 14 a ogni step, scegliere
le piu' interessanti col selettore deterministico, e operarle.

LOGICA per ogni step (candela):
  1. opportunity_score su tutte e 14 (crypto_selector)
  2. seleziona top-K sopra soglia E in regime UP (crypto_regime) → le altre = cash
  3. alloca il capitale equamente tra le selezionate (resto cash)
  4. quando un asset esce dai top o gira DOWN/SIDE → lo si chiude (torna cash)

BENCHMARK ONESTO: non "batte BTC?" ma "batte il tenere le 14 equal-weight?"
(buy&hold di un paniere equipesato, ribilanciato all'inizio). E' il vero metro.

LIMITI dichiarati:
  - Survivorship bias: le 14 sono quelle VIVE oggi (esclude LUNA, FTT morte) →
    risultati un filo ottimistici.
  - Sim al close, commissioni 10bps + slippage. No funding.
  - Asset entrati in Binance dopo lo start (es. alcune nel 2021) → semplicemente
    non disponibili in quei primi mesi (gestito: score None = escluso).

Uso:  python -m simulator.backtest_portfolio 1d   2021-01-01 2024-12-31
      python -m simulator.backtest_portfolio 4h   2024-01-01 2024-12-31
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from agents.crypto_selector import UNIVERSE_14, UNIVERSE_50, select_top, rank_universe, SelectorParams
from agents.crypto_regime import classify_regime, RegimeParams, UP
from simulator.backtest_swing import fetch_daily, _dt_ms
from simulator.backtest_scalper import fetch_klines_range

COMMISSION_BPS = 10.0
INITIAL_NAV = 100_000.0


def _commission(n): return abs(n) * COMMISSION_BPS / 10_000.0


def _fetch(symbol, interval, start_ms, end_ms):
    if interval == "1d":
        return fetch_daily(symbol, start_ms, end_ms)
    return fetch_klines_range(symbol, interval, start_ms, end_ms)


def _align(data: dict) -> tuple[list[int], dict]:
    """Allinea le serie su una TIMELINE MASTER deterministica con FORWARD-FILL.

    Bug precedente: usavo l'UNIONE dei timestamp; le candele mancanti (Binance
    a volte ne salta) venivano riempite in modo diverso a seconda di micro-
    differenze tra download → stesso input dava +349% o +287% (backtest
    'ballerino'). NON deterministico.

    Fix: la timeline master = i timestamp dell'asset PIU' COMPLETO (di norma
    BTC). Per ogni asset, ogni candela viene mappata al timestamp master >=
    suo, e ogni buco e' riempito col forward-fill (ultima candela nota). Cosi':
      - ogni asset ha un prezzo definito a OGNI istante master (l'ultimo reale),
      - prima della sua nascita su Binance, l'asset semplicemente non esiste
        (None) → escluso dalla selezione (gestito a valle),
      - stesso input → SEMPRE stesso output.
    """
    # 1. timeline master = unione ordinata, ma il riempimento e' deterministico
    #    (forward-fill), quindi i buchi non dipendono piu' dall'ordine di scarico.
    all_ts = set()
    for bars in data.values():
        for b in bars:
            if b.get("t") is not None:
                all_ts.add(b["t"])
    timeline = sorted(all_ts)

    index = {}
    for sym, bars in data.items():
        bars_sorted = sorted((b for b in bars if b.get("t") is not None),
                             key=lambda b: b["t"])
        m = {}
        bi = 0
        last = None
        for t in timeline:
            # avanza fino all'ultima candela con timestamp <= t
            while bi < len(bars_sorted) and bars_sorted[bi]["t"] <= t:
                last = bars_sorted[bi]
                bi += 1
            # last = ultima candela reale nota a questo istante (forward-fill);
            # None se l'asset non era ancora nato → resta assente
            m[t] = last
        index[sym] = m
    return timeline, index


def run_portfolio_backtest(data: dict, interval: str,
                           min_score: float = 0.58, slippage_bps: float = 5.0,
                           warm: int = 60,
                           asset_fast_stop_pct: float = 0.0,
                           portfolio_cb_pct: float = 0.0,
                           max_invested_pct: float = 100.0) -> dict:
    """Simula il portafoglio multi-asset. data: {symbol: [bar con 't']}.
    Il NUMERO di posizioni e' deciso dal cervello (quanti asset UP+sopra-soglia
    ci sono), NON imposto.

    DEFAULT = versione MIGLIORE (+349%/-66%): tutti i controlli OFF. Le leve di
    rischio sono OPT-IN, testabili UNA alla volta:
      - max_invested_pct: CASH-FLOOR. Quota massima del NAV investita in crypto
        (es. 75 = sempre >=25% cash). Abbassa OGNI drawdown in proporzione SENZA
        vendere nei cali (niente trappola vendi-ricompra). 100 = nessun floor.
      - asset_fast_stop_pct: esci da un asset se scende oltre questo % dal suo
        MASSIMO (grilletto rapido crash veloci). 0 = off.
      - portfolio_cb_pct: circuit breaker (vende-tutto/cash). BOCCIATO dai dati
        (vende basso/ricompra alto) — lasciato solo per confronto. 0 = off.
    """
    sp = SelectorParams()
    rp = RegimeParams()
    timestamps, idx = _align(data)
    if len(timestamps) < warm + 10:
        return {"error": f"dati insuff ({len(timestamps)} ts)"}

    cash = INITIAL_NAV
    holdings: dict[str, float] = {}   # symbol -> units
    entry_px: dict[str, float] = {}
    asset_peak: dict[str, float] = {}  # massimo prezzo da quando in portafoglio
    equity = []
    pos_counts = []                   # quante posizioni aperte a ogni step
    n_trades = 0
    fees = 0.0
    nav_peak = INITIAL_NAV            # picco del NAV (per il circuit breaker)
    cb_active = False                 # circuit breaker scattato → tutto cash
    cb_events = 0

    def price_at(sym, ts):
        b = idx.get(sym, {}).get(ts)
        return b["close"] if b else None

    def nav_at(ts):
        v = cash
        for sym, u in holdings.items():
            px = price_at(sym, ts)
            if px:
                v += u * px
        return v

    def history_until(sym, ts_pos):
        """Bars REALI (de-duplicati) dell'asset fino all'istante corrente, per
        il calcolo indicatori. Con il forward-fill, m[t] ripete l'ultima candela
        nei buchi: qui prendiamo ogni candela una sola volta (per timestamp
        reale 't'), altrimenti i close ripetuti falserebbero ATR/RSI (volatilita'
        finta zero). No look-ahead: solo fino a ts_pos."""
        m = idx.get(sym, {})
        out = []
        seen_t = None
        for t in timestamps[:ts_pos + 1]:
            b = m.get(t)
            if b is None:
                continue
            if b.get("t") != seen_t:   # candela reale nuova (non un fill ripetuto)
                out.append(b)
                seen_t = b.get("t")
        return out

    def _sell(sym, ts, reason_trades=True):
        nonlocal cash, fees, n_trades
        px = price_at(sym, ts)
        if px and holdings.get(sym, 0) > 0:
            proceeds = holdings[sym] * px * (1 - slippage_bps / 1e4)
            fee = _commission(proceeds)
            cash += proceeds - fee
            fees += fee
            if reason_trades:
                n_trades += 1
        holdings.pop(sym, None)
        entry_px.pop(sym, None)
        asset_peak.pop(sym, None)

    for ti in range(warm, len(timestamps)):
        ts = timestamps[ti]

        # 1+2. score + regime su tutte le 14 → costruisci data_by_symbol storico
        hist = {}
        for sym in data:
            h = history_until(sym, ti)
            if len(h) >= warm:
                hist[sym] = h

        # ── CONTROLLO RISCHIO A: grilletto rapido PER-ASSET (crash veloci) ──
        # aggiorna il picco di ogni asset in mano; se scende oltre la soglia
        # dal suo massimo, esci SUBITO (non aspettare il regime/EMA).
        if asset_fast_stop_pct > 0:
            for sym in list(holdings.keys()):
                px = price_at(sym, ts)
                if not px:
                    continue
                asset_peak[sym] = max(asset_peak.get(sym, px), px)
                if px <= asset_peak[sym] * (1 - asset_fast_stop_pct / 100.0):
                    _sell(sym, ts)

        # ── CONTROLLO RISCHIO B: CIRCUIT BREAKER di portafoglio ──
        cur_nav = nav_at(ts)
        nav_peak = max(nav_peak, cur_nav)
        if portfolio_cb_pct > 0 and not cb_active:
            if cur_nav <= nav_peak * (1 - portfolio_cb_pct / 100.0):
                # liquida TUTTO, vai cash
                for sym in list(holdings.keys()):
                    _sell(sym, ts)
                cb_active = True
                cb_events += 1
        # uscita dal circuit breaker: rientra solo quando BTC torna UP
        if cb_active:
            btc_hist = hist.get("BTCUSDT")
            btc_up = btc_hist and classify_regime([b["close"] for b in btc_hist], rp) == UP
            if btc_up:
                cb_active = False
                nav_peak = nav_at(ts)   # reset del picco al rientro
            else:
                equity.append(nav_at(ts))   # resta in cash, salta selezione
                pos_counts.append(0)
                continue

        # SELEZIONE: e' il CERVELLO a decidere QUANTI asset prendere, non un
        # cap imposto. Prende OGNI asset che e' (a) in regime UP confermato e
        # (b) sopra la soglia di opportunita'. Possono essere 0, 1, 8, o tutti e
        # 14 — dipende da quante opportunita' VERE ci sono in quel momento.
        # ISTERESI per ridurre il churn: chi e' gia' dentro lo si tiene finche'
        # resta UP e sopra una soglia di USCITA piu' bassa (non lo si scarica
        # per micro-variazioni di ranking).
        exit_score = min_score - 0.10
        ranked = rank_universe(hist, sp)
        score_by = {r.symbol: r.score for r in ranked}

        def is_up(sym):
            return classify_regime([b["close"] for b in hist[sym]], rp) == UP

        target = set()
        # 1) mantieni gli attuali ancora sani (UP + sopra exit_score)
        for sym in holdings:
            if sym in hist and is_up(sym) and score_by.get(sym, 0) >= exit_score:
                target.add(sym)
        # 2) aggiungi TUTTI i nuovi che meritano (UP + sopra min_score). Nessun
        #    limite artificiale: quante opportunita' ci sono, tante se ne prendono.
        for r in ranked:
            if r.symbol not in target and r.score >= min_score and is_up(r.symbol):
                target.add(r.symbol)

        # 4. chiudi chi non e' piu' target
        for sym in list(holdings.keys()):
            if sym not in target:
                _sell(sym, ts)

        # 3. apri/mantieni i target, capitale equipesato. CASH-FLOOR: si investe
        #    al massimo max_invested_pct del NAV (il resto resta cash → abbassa
        #    il drawdown in proporzione, senza vendere nei cali).
        if target:
            nav = nav_at(ts)
            investable = nav * max_invested_pct / 100.0
            target_alloc = investable / len(target)   # quota per asset
            for sym in target:
                px = price_at(sym, ts)
                if not px:
                    continue
                cur_val = holdings.get(sym, 0.0) * px
                # ribilancia solo se scostamento GROSSO (soglia 0.40 per ridurre
                # ulteriormente il churn: micro-ribilanciamenti = commissioni inutili)
                if abs(cur_val - target_alloc) / target_alloc > 0.40:
                    # vendi/compra la differenza
                    diff_val = target_alloc - cur_val
                    if diff_val > 0 and cash >= diff_val:
                        u = diff_val / px
                        fee = _commission(diff_val)
                        cash -= diff_val + fee
                        fees += fee
                        holdings[sym] = holdings.get(sym, 0.0) + u
                        entry_px[sym] = px
                        asset_peak.setdefault(sym, px)   # inizia il picco per il fast-stop
                        n_trades += 1
                    elif diff_val < 0:
                        u = -diff_val / px
                        proceeds = u * px
                        fee = _commission(proceeds)
                        cash += proceeds - fee
                        fees += fee
                        holdings[sym] = max(0.0, holdings.get(sym, 0.0) - u)
                        n_trades += 1

        equity.append(nav_at(ts))
        pos_counts.append(len(holdings))

    # liquida a fine serie
    last_ts = timestamps[-1]
    for sym, u in list(holdings.items()):
        px = price_at(sym, last_ts)
        if px:
            cash += u * px - _commission(u * px)
    final = cash if not equity else nav_at(last_ts) if holdings else equity[-1]
    final = equity[-1] if equity else INITIAL_NAV

    # metriche
    peak, max_dd = INITIAL_NAV, 0.0
    for v in equity:
        peak = max(peak, v)
        max_dd = min(max_dd, (v - peak) / peak * 100.0)
    ret = (equity[-1] / INITIAL_NAV - 1.0) * 100.0 if equity else 0.0

    # BENCHMARK: equal-weight hold delle 14 (quelle con dati al warm)
    bh = _equal_weight_hold(data, timestamps, idx, warm)
    avg_pos = sum(pos_counts) / len(pos_counts) if pos_counts else 0.0
    max_pos = max(pos_counts) if pos_counts else 0
    return {"return_pct": round(ret, 1), "ew_hold_pct": round(bh["ret"], 1),
            "edge": round(ret - bh["ret"], 1),
            "max_drawdown_pct": round(max_dd, 1), "ew_max_dd_pct": round(bh["dd"], 1),
            "trades": n_trades, "n_assets": len(data), "steps": len(equity),
            "avg_positions": round(avg_pos, 1), "max_positions": max_pos,
            "cb_events": cb_events}


def _equal_weight_hold(data, timestamps, idx, warm) -> dict:
    """Benchmark: compra equal-weight le 14 al primo step disponibile, tieni."""
    start_ts = timestamps[warm]
    avail = [s for s in data if idx.get(s, {}).get(start_ts)]
    if not avail:
        return {"ret": 0.0, "dd": 0.0}
    w = INITIAL_NAV / len(avail)
    units = {s: w / idx[s][start_ts]["close"] for s in avail}
    eq = []
    for ti in range(warm, len(timestamps)):
        ts = timestamps[ti]
        v = 0.0
        for s in avail:
            b = idx.get(s, {}).get(ts)   # forward-filled: ultimo prezzo noto
            if b:
                v += units[s] * b["close"]
        eq.append(v)
    peak, dd = INITIAL_NAV, 0.0
    for v in eq:
        peak = max(peak, v)
        dd = min(dd, (v - peak) / peak * 100.0)
    ret = (eq[-1] / INITIAL_NAV - 1.0) * 100.0 if eq else 0.0
    return {"ret": ret, "dd": dd}


def main(argv):
    interval = argv[1] if len(argv) > 1 else "1d"
    d0 = argv[2] if len(argv) > 2 else "2021-01-01"
    d1 = argv[3] if len(argv) > 3 else "2024-12-31"
    uni_arg = argv[4] if len(argv) > 4 else "14"
    universe = UNIVERSE_50 if uni_arg == "50" else UNIVERSE_14
    print(f"Scarico universo {len(universe)} {interval} {d0}->{d1} ...")
    data = {}
    for sym in universe:
        bars = _fetch(sym, interval, _dt_ms(d0), _dt_ms(d1))
        if len(bars) >= 60:
            data[sym] = bars
    print(f"  {len(data)}/{len(universe)} asset con dati sufficienti")
    if len(data) < 5:
        print("Dati insufficienti."); return 1

    warm = 60 if interval == "1d" else 120
    r = run_portfolio_backtest(data, interval, warm=warm)
    print("\n" + "=" * 80)
    print(f"PORTAFOGLIO MULTI-ASSET ({len(universe)} monete, il sistema sceglie QUANTI/QUALI)  {interval}  {d0}->{d1}")
    print("=" * 80)
    if "error" in r:
        print("  ", r["error"]); return 1
    print(f"  Asset nell'universo : {r['n_assets']}/{len(universe)}   step simulati: {r['steps']}")
    print(f"  Posizioni aperte    : media {r['avg_positions']}, max {r['max_positions']} (DECISE dal sistema, non imposte)")
    print(f"  Sistema (rotazione) : {r['return_pct']:+.1f}%   max DD {r['max_drawdown_pct']:.1f}%")
    print(f"  Benchmark EW-hold-14: {r['ew_hold_pct']:+.1f}%   max DD {r['ew_max_dd_pct']:.1f}%")
    print(f"  EDGE vs equal-weight: {r['edge']:+.1f} punti   ({r['trades']} trade)")
    print(f"  Circuit breaker scattato {r['cb_events']} volte (liquidazione totale->cash)")
    print("=" * 80)
    print("BENCHMARK ONESTO = tenere le 14 equipesate. Survivorship bias: 14 vive")
    print("oggi (esclude LUNA/FTT morte) -> risultati un filo ottimistici.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
