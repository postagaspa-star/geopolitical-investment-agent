"""
backtest_base_delta — prova storica della "posizione normale" (Fase 3, su carta).

LA DOMANDA
Se il portafoglio partisse SEMPRE investito in una base fissa, diversificata e
dimensionata da una formula (non dal modello), come sarebbe andata negli ultimi
10 anni rispetto a (a) il comportamento attuale — pochi soldi investiti, tutti
concentrati — e (b) il restare investiti e basta?

LA BASE (deterministica, nessun LLM nel giro)
- 4 ingredienti diversificati: azioni USA (SPY), obbligazioni lunghe (TLT),
  oro (GLD), crypto (BTC) con TETTO al 10% del portafoglio. Scelta di Andrea
  (fine luglio 2026, corretta il giorno dopo): le cripto NON vanno tolte,
  restano nel paniere ma come fetta COMPLEMENTARE — non piu' il centro della
  strategia. Il tetto 10% e' gia' cosi' dalla prima stesura del motore; qui
  si conferma la scelta e si ricalibra il termometro sulla regola di rischio
  di Andrea con le cripto incluse (vedi sotto).
- Pesi "al contrario del nervosismo" (inverse-vol sui 60 giorni precedenti):
  chi balla di piu' pesa di meno.
- Quanto investire in totale lo decide un TERMOMETRO: si stima quanto
  ballerebbe il paniere e si scala l'esposizione perche' il ballo atteso resti
  al bersaglio (TARGET_VOL). Il bersaglio deriva dal limite di perdita del
  profilo (max drawdown 12%): storicamente il drawdown di un portafoglio
  diversificato e' ~1.5-2.5x la sua volatilita' annua, quindi 7% di target
  tiene il drawdown atteso sotto il limite. Il resto sta in liquidita' — ma
  come RISULTATO della formula, non come stato di riposo gratuito.

ONESTA' / LIMITI (leggere prima dei numeri)
- Esecuzione alla chiusura mensile, commissione 10 bps per lato sul ribilanciato
  (come il sistema); NIENTE slippage oltre a questo, niente tasse.
- Pesi tenuti costanti dentro il mese (approssimazione da ribilanciamento
  giornaliero senza costi infra-mese).
- BTC scelto col senno di poi: e' la crypto sopravvissuta e vincente del
  decennio. Il tetto al 10% limita ma non elimina questo bias — vale
  SOPRATTUTTO per la strategia "OGGI" (25% crypto), che ne beneficia in pieno.
- 10 anni contengono ~2-3 regimi indipendenti: i numeri di drawdown sono
  stime con POCHE osservazioni di coda, non garanzie.
- SPY/TLT oggi NON sono nell'universo investibile del sistema (il flag
  EXPANDED_UNIVERSE con la cintura ETF e' spento): costruire la base davvero
  richiede o accendere quel flag o replicare con titoli singoli. Decisione
  di Andrea, segnalata nel progetto.
- Nessun contributo dell'LLM qui: questa e' SOLO la base. Gli scostamenti
  tattici (il lavoro del modello) si aggiungerebbero sopra — in meglio o in
  peggio: e' esattamente cio' che l'agente ombra sta misurando.

Uso:
    python backtest_base_delta.py <cartella-con-hist_*.json>
    (file hist_SPY.json ecc. in formato Yahoo v8 chart)

Il core (simulate e funzioni ausiliarie) e' PURO e testato in
tests/test_backtest_base_delta.py.
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone

TRADING_DAYS = 252
WINDOW = 60                 # giorni di storia per stimare il nervosismo
COST_BPS_SIDE = 10.0        # commissione per lato, come il sistema
BTC_CAP = 0.10              # tetto crypto nella base
TARGET_VOL_DEFAULT = 0.07   # 7% annuo, derivato dal limite di drawdown 12%


# ── funzioni pure ───────────────────────────────────────────────────────────

def daily_returns(closes: list[float]) -> list[float]:
    return [(b - a) / a for a, b in zip(closes, closes[1:])]


def ann_vol(rets: list[float]) -> float:
    n = len(rets)
    if n < 2:
        return 0.0
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    return math.sqrt(var) * math.sqrt(TRADING_DAYS)


def inverse_vol_weights(vols: dict[str, float],
                        caps: dict[str, float] | None = None) -> dict[str, float]:
    """Pesi proporzionali a 1/vol, con eventuali tetti (rinormalizzando gli altri)."""
    inv = {t: (1.0 / v if v > 1e-9 else 0.0) for t, v in vols.items()}
    tot = sum(inv.values())
    if tot <= 0:
        return {t: 0.0 for t in vols}
    w = {t: x / tot for t, x in inv.items()}
    for t, cap in (caps or {}).items():
        if t in w and w[t] > cap:
            excess_pool = sum(x for k, x in w.items() if k != t)
            w[t] = cap
            if excess_pool > 0:
                scale = (1.0 - cap) / excess_pool
                for k in w:
                    if k != t:
                        w[k] *= scale
    return w


def month_key(date_iso: str) -> str:
    return date_iso[:7]


def base_weights(vols: dict[str, float],
                 btc_share: float | None = None,
                 caps: dict[str, float] | None = None) -> dict[str, float]:
    """
    Pesi del paniere investito (somma = 1), in due modalita':

    - btc_share=None (storica): inverse-vol su TUTTI gli ingredienti, con
      eventuali tetti. Qui la cripto esce strutturalmente PICCOLA: ballando
      3-4 volte piu' degli altri, l'inverse-vol le assegna 3-4 volte meno —
      il tetto al 10% quasi non serve, la formula da sola sta sotto.

    - btc_share=X (quota fissa, richiesta di Andrea per alzare la cripto):
      la cripto riceve ESATTAMENTE la quota X della parte investita, decisa
      a monte e non dalla formula; gli altri ingredienti si spartiscono il
      resto in inverse-vol. Attenzione: e' una scelta strategica, non una
      misura — piu' quota cripto = piu' rendimento NEL DECENNIO IN CUI BTC
      ha fatto ~100x, il che e' senno di poi, non una legge.
    """
    if btc_share is None:
        return inverse_vol_weights(vols, caps)
    others = {t: v for t, v in vols.items() if t != "BTC-USD"}
    w = inverse_vol_weights(others)
    out = {t: x * (1.0 - btc_share) for t, x in w.items()}
    if "BTC-USD" in vols:
        out["BTC-USD"] = btc_share
    return out


def simulate(dates: list[str], closes: dict[str, list[float]],
             mode: str, target_vol: float = TARGET_VOL_DEFAULT,
             fixed_weights: dict[str, float] | None = None,
             cost_bps: float = COST_BPS_SIDE,
             window: int = WINDOW,
             cash_rate: float = 0.0,
             btc_share: float | None = None) -> dict:
    """
    Simulazione a pesi mensili. mode:
      "base"  -> inverse-vol + tetto BTC + termometro (scala a target_vol)
      "fixed" -> pesi costanti (fixed_weights; il resto e' liquidita')
    Nessuno sguardo al futuro: i pesi decisi al giorno i usano solo dati < i.

    cash_rate: rendimento ANNUO della liquidita' non investita. Il default 0
    e' prudente ma penalizza le strategie poco esposte: nel 2023-26 la
    liquidita' in dollari ha reso ~4-5%. Una media piatta resta un'
    approssimazione (i tassi 2016-21 erano ~0-2%): dichiarata come tale.
    window: giorni di storia per stimare il nervosismo (sensibilita').
    """
    tickers = sorted(closes)
    rets = {t: daily_returns(closes[t]) for t in tickers}
    n_days = len(dates) - 1                      # rendimenti: 1..n
    equity = [1.0]
    weights = {t: 0.0 for t in tickers}
    curve_dates = [dates[0]]
    daily_cash = cash_rate / TRADING_DAYS

    for i in range(n_days):
        day = dates[i + 1]
        # ribilancio al primo giorno del mese (usando SOLO storia passata)
        if i == 0 or month_key(day) != month_key(dates[i]):
            if mode == "fixed":
                new_w = dict(fixed_weights or {})
            else:
                if i >= window:
                    vols = {t: ann_vol(rets[t][i - window:i]) for t in tickers}
                    w = base_weights(vols, btc_share=btc_share,
                                     caps={"BTC-USD": BTC_CAP})
                    port_window = [sum(w[t] * rets[t][j] for t in tickers)
                                   for j in range(i - window, i)]
                    pv = ann_vol(port_window)
                    scale = min(1.0, target_vol / pv) if pv > 1e-9 else 0.0
                    new_w = {t: w[t] * scale for t in tickers}
                else:
                    new_w = {t: 0.0 for t in tickers}   # warm-up: liquidi
            turnover = sum(abs(new_w.get(t, 0.0) - weights.get(t, 0.0))
                           for t in tickers)
            equity[-1] *= (1.0 - turnover * cost_bps / 10_000.0)
            weights = new_w
        invested = sum(weights.values())
        day_ret = sum(weights.get(t, 0.0) * rets[t][i] for t in tickers)
        day_ret += max(0.0, 1.0 - invested) * daily_cash
        equity.append(equity[-1] * (1.0 + day_ret))
        curve_dates.append(day)

    return {"dates": curve_dates, "equity": equity,
            "metrics": compute_metrics(equity)}


def compute_metrics(equity: list[float]) -> dict:
    n = len(equity) - 1
    if n <= 0:
        return {}
    total = equity[-1] / equity[0]
    years = n / TRADING_DAYS
    cagr = total ** (1 / years) - 1 if years > 0 else 0.0
    rets = daily_returns(equity)
    vol = ann_vol(rets)
    peak, maxdd = equity[0], 0.0
    for v in equity:
        peak = max(peak, v)
        maxdd = min(maxdd, v / peak - 1.0)
    worst_12m = min((equity[i + TRADING_DAYS] / equity[i] - 1.0
                     for i in range(n - TRADING_DAYS)), default=0.0)
    return {
        "final_x": round(equity[-1], 4),
        "cagr_pct": round(cagr * 100, 2),
        "vol_pct": round(vol * 100, 2),
        "max_dd_pct": round(maxdd * 100, 2),
        "worst_12m_pct": round(worst_12m * 100, 2),
    }


# ── caricamento dati (I/O) ──────────────────────────────────────────────────

def load_yahoo(path: str) -> dict[str, float]:
    """Da file Yahoo v8 chart a {data_iso: close}."""
    raw = json.load(open(path, encoding="utf-8"))
    q = raw["chart"]["result"][0]
    out = {}
    for ts, close in zip(q["timestamp"], q["indicators"]["quote"][0]["close"]):
        if close:
            day = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
            out[day] = float(close)
    return out


def align(series: dict[str, dict[str, float]],
          calendar_of: tuple[str, ...] = ("SPY", "TLT", "GLD")) -> tuple[list[str], dict]:
    """Calendario = giorni presenti in TUTTI gli strumenti di borsa; le serie
    24/7 (BTC) vengono campionate su quei giorni (ultimo valore noto)."""
    cal = None
    for t in calendar_of:
        days = set(series[t])
        cal = days if cal is None else (cal & days)
    dates = sorted(cal)
    closes: dict[str, list[float]] = {}
    for t, s in series.items():
        vals, last = [], None
        for d in dates:
            last = s.get(d, last)
            vals.append(last)
        # scarta la testa senza dati
        first_ok = next((i for i, v in enumerate(vals) if v), 0)
        closes[t] = vals
        if first_ok:
            dates = dates[first_ok:]
            closes = {k: v[first_ok:] for k, v in closes.items()}
    return dates, closes


def main(folder: str) -> None:
    series = {t: load_yahoo(f"{folder}/hist_{t}.json")
              for t in ["SPY", "TLT", "GLD", "BTC-USD"]}
    dates, closes = align(series)
    print(f"periodo: {dates[0]} -> {dates[-1]}  ({len(dates)} sedute)\n")

    # REGOLA DI RISCHIO DI ANDREA (aggiornata ancora a fine luglio 2026):
    # limite di perdita massima alzato CONSAPEVOLMENTE a -20% (prima: quasi
    # mai sotto -10%). Le cripto restano COMPLEMENTARI (tetto 10%).
    #
    # Scelta raccomandata: termometro 8% -> 9,2%/anno, max perdita storica
    # -18,4% (margine ~8% relativo sotto il limite, stessa disciplina usata
    # per la regola precedente: il backtest e' ottimista per costruzione).
    # L'8,5% arriva a -19,5%: troppo a filo. NOTA su cosa compra il rischio:
    # raddoppiare la tolleranza (da -10% a -20%) NON raddoppia la resa
    # (6,6% -> 9,2%); e sopra il termometro ~9% la formula smette di mordere
    # perche' l'esposizione e' gia' al 100% in meta' dei mesi (niente leva):
    # questa base ha un tetto naturale intorno al 10%/anno.
    runs = [
        ("BASE (cripto 10%), term. 7%", closes, dict(mode="base",
         target_vol=0.07, cash_rate=0.03)),
        ("BASE (cripto 10%), term. 8% -- SCELTA", closes, dict(mode="base",
         target_vol=0.08, cash_rate=0.03)),
        ("BASE (cripto 10%), term. 8.5% (a filo)", closes,
         dict(mode="base", target_vol=0.085, cash_rate=0.03)),
        ("BASE, term. 8%, finestra 90g", closes, dict(mode="base",
         target_vol=0.08, cash_rate=0.03, window=90)),
        ("BASE prudente term. 4% (vecchia regola -10%)", closes,
         dict(mode="base", target_vol=0.04, cash_rate=0.03)),
        ("OGGI: 25% BTC + 75% fermo (scartata)", closes,
         dict(mode="fixed", fixed_weights={"BTC-USD": 0.25}, cash_rate=0.03)),
        ("Tutto azioni (SPY 100%)", closes,
         dict(mode="fixed", fixed_weights={"SPY": 1.0})),
        ("Classico 60/40", closes,
         dict(mode="fixed", fixed_weights={"SPY": 0.6, "TLT": 0.4})),
    ]
    header = f"{'strategia':28s} {'100k ->':>10s} {'annuo':>7s} {'ballo':>7s} {'max perdita':>12s} {'peggior anno':>13s}"
    print(header)
    print("-" * len(header))
    for name, data, cfg in runs:
        m = simulate(dates, data, **cfg)["metrics"]
        print(f"{name:28s} {100000*m['final_x']:>10,.0f} {m['cagr_pct']:>6.1f}% "
              f"{m['vol_pct']:>6.1f}% {m['max_dd_pct']:>11.1f}% {m['worst_12m_pct']:>12.1f}%")


def frontier(folder: str) -> None:
    """
    Frontiera rischio/rendimento richiesta da Andrea: per ogni livello di
    perdita massima tollerata (-2% ... -20%, a passi di 2), trova la
    combinazione termometro x quota-cripto col rendimento migliore che resta
    DENTRO quel livello sull'intero decennio.

    AVVERTENZA DI ONESTA' (stampata anche in coda): scegliere la quota cripto
    guardando questo decennio significa ottimizzare sul miglior decennio
    della storia di BTC (~100x). I vincitori di ogni riga vanno letti come
    "cosa avrebbe pagato", non "cosa paghera'".
    """
    series = {t: load_yahoo(f"{folder}/hist_{t}.json")
              for t in ["SPY", "TLT", "GLD", "BTC-USD"]}
    dates, closes = align(series)
    print(f"periodo: {dates[0]} -> {dates[-1]}  ({len(dates)} sedute)")

    modes = [(None, "formula (inv-vol, tetto 10%)"), (0.10, "cripto fissa 10%"),
             (0.15, "cripto fissa 15%"), (0.20, "cripto fissa 20%"),
             (0.25, "cripto fissa 25%"), (0.30, "cripto fissa 30%")]
    vols_grid = [x / 200.0 for x in range(4, 29)]     # 2.0% ... 14.0% passo 0.5

    all_runs = []
    for share, label in modes:
        for tv in vols_grid:
            m = simulate(dates, closes, mode="base", target_vol=tv,
                         cash_rate=0.03, btc_share=share)["metrics"]
            all_runs.append({"share": share, "label": label, "tv": tv, **m})

    print(f"\ncombinazioni provate: {len(all_runs)} "
          f"({len(modes)} modalita' cripto x {len(vols_grid)} termometri)\n")
    header = (f"{'limite':>7s} | {'migliore combinazione':32s} "
              f"{'annuo':>6s} {'max perdita':>12s} {'100k ->':>9s}")
    print(header)
    print("-" * len(header))
    for bucket in range(2, 21, 2):
        ok = [r for r in all_runs if r["max_dd_pct"] >= -bucket]
        if not ok:
            print(f"{-bucket:>6d}% | (nessuna combinazione resta nel limite)")
            continue
        best = max(ok, key=lambda r: r["cagr_pct"])
        combo = f"{best['label']}, term. {best['tv']*100:.1f}%"
        print(f"{-bucket:>6d}% | {combo:32s} {best['cagr_pct']:>5.1f}% "
              f"{best['max_dd_pct']:>11.1f}% {100000*best['final_x']:>9,.0f}")

    print("\nAVVERTENZA: le righe con quota cripto alta vincono PERCHE' questo")
    print("e' stato il miglior decennio della storia di BTC. E' senno di poi:")
    print("una quota fissa alta e' una scommessa strategica, non una misura.")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[2] == "frontier":
        frontier(sys.argv[1])
    else:
        main(sys.argv[1] if len(sys.argv) > 1 else ".")
