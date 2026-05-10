import { useEffect, useRef, useState } from "react";
import { HelpCircle, X } from "lucide-react";

/**
 * ChartHelpButton — piccolo pulsante "?" che mostra un popover con due sezioni:
 *  1. Cosa rappresenta (spiegazione generale del grafico/metrica)
 *  2. Nella tua simulazione (frase data-driven con i numeri reali del run)
 *
 * Props:
 *   title:        nome del grafico (mostrato nell'header del popover)
 *   general:      stringa o JSX con la spiegazione generale
 *   specific:     stringa o JSX con la spiegazione applicata al run corrente
 *   align:        "right" (default) o "left", in che lato del bottone si apre
 *   size:         dimensione bottone (default 16)
 *
 * Click outside / ESC per chiudere.
 */
export default function ChartHelpButton({ title, general, specific,
                                           align = "right", size = 16 }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    const onKey = (e) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <span ref={ref} style={S.wrap}>
      <button
        type="button"
        aria-label="Help"
        title="Spiega questo grafico"
        style={{ ...S.btn, ...(open ? S.btnOpen : {}) }}
        onClick={(e) => { e.stopPropagation(); setOpen((v) => !v); }}
      >
        <HelpCircle size={size} />
      </button>

      {open && (
        <div
          style={{
            ...S.popover,
            ...(align === "left" ? { right: "auto", left: 0 } : {}),
          }}
          onClick={(e) => e.stopPropagation()}
        >
          <div style={S.popHeader}>
            <span style={S.popTitle}>{title || "Aiuto"}</span>
            <button type="button" style={S.popClose}
                    onClick={() => setOpen(false)} aria-label="Chiudi">
              <X size={13} />
            </button>
          </div>

          <div style={S.section}>
            <div style={S.sectionLabel}>Cosa rappresenta</div>
            <div style={S.sectionBody}>{general}</div>
          </div>

          {specific && (
            <div style={S.section}>
              <div style={{ ...S.sectionLabel, color: "#a78bfa" }}>
                Nella tua simulazione
              </div>
              <div style={S.sectionBody}>{specific}</div>
            </div>
          )}
        </div>
      )}
    </span>
  );
}


// ═══════════════════════════════════════════════════════════════════════
// Helpers di formattazione (riusati dalle funzioni buildExplain)
// ═══════════════════════════════════════════════════════════════════════

export function fmtPct(v, sign = true) {
  if (v == null || isNaN(v)) return "—";
  const s = sign && v >= 0 ? "+" : "";
  return `${s}${v.toFixed(2)}%`;
}

export function fmtUsd(v) {
  if (v == null || isNaN(v)) return "—";
  const sign = v < 0 ? "-" : "";
  const abs = Math.abs(v);
  if (abs >= 1000) return `${sign}$${(abs / 1000).toFixed(2)}k`;
  return `${sign}$${abs.toFixed(2)}`;
}

export function fmtNum(v, dec = 2) {
  if (v == null || isNaN(v)) return "—";
  return Number(v).toFixed(dec);
}


// ═══════════════════════════════════════════════════════════════════════
// Funzioni buildExplain — una per ogni grafico/sezione
// Tutte accettano lo stesso payload e ritornano stringhe/JSX di spiegazione.
// ═══════════════════════════════════════════════════════════════════════

/**
 * KPI block (Sharpe, MDD, Profit Factor, Expectancy, Hold time, Commissioni).
 * Spiega l'insieme dei 6 KPI.
 */
export function explainKPIBlock(quantMetrics, commissionBps, totalCommissions) {
  const general = (
    <>
      Sei numeri quantitativi che riassumono la qualità della strategia oltre
      al P&L finale:
      <ul style={S.list}>
        <li><b>Sharpe</b>: rendimento risk-adjusted annualizzato. &gt;2 eccellente.</li>
        <li><b>Max Drawdown</b>: massima perdita dal picco. Quanto "dolore" prima del recupero.</li>
        <li><b>Profit Factor</b>: Σwinners / |Σlosers|. &gt;2 sistema solido, &lt;1 sistema in perdita.</li>
        <li><b>Expectancy</b>: P&L medio per trade in $. Cosa ti aspetti per ogni operazione.</li>
        <li><b>Hold time</b>: durata media delle posizioni.</li>
        <li><b>Commissioni</b>: costo totale di transazione applicato a tutti i trade.</li>
      </ul>
    </>
  );

  if (!quantMetrics) {
    return { general, specific: "Metriche non disponibili per questo run." };
  }

  const sh = quantMetrics.sharpe_ratio;
  const mdd = quantMetrics.max_drawdown?.max_drawdown_pct;
  const exp = quantMetrics.expectancy || {};
  const pf = quantMetrics.profit_factor;
  const pfInf = quantMetrics.profit_factor_infinite;
  const hold = quantMetrics.avg_hold_time || {};

  const sharpeJudge = sh == null ? "n/d"
    : sh >= 2 ? "eccellente (la strategia compensa bene il rischio)"
    : sh >= 1 ? "buono"
    : sh >= 0 ? "mediocre (il rischio assunto non è ben ripagato)"
    : "negativo (peggio del cash)";

  const pfJudge = pfInf ? "infinito (nessun trade in perdita)"
    : pf == null ? "non calcolabile (no trade chiusi)"
    : pf >= 2 ? "ottimo"
    : pf >= 1 ? "marginale"
    : "in perdita strutturale";

  const specific = (
    <>
      <b>Sharpe</b> {fmtNum(sh)} → {sharpeJudge}.
      {" "}<b>MDD</b> {fmtPct(mdd, false)} (peak ${fmtNum(quantMetrics.max_drawdown?.peak_value, 0)}
      {" → "}${fmtNum(quantMetrics.max_drawdown?.trough_value, 0)},
      durata {quantMetrics.max_drawdown?.duration_steps || 0} step).
      {" "}<b>Profit Factor</b> {pfInf ? "∞" : fmtNum(pf)} → {pfJudge}.
      {" "}<b>Expectancy</b> {fmtUsd(exp.expectancy_dollars)}/trade
      con win rate {((exp.win_rate || 0) * 100).toFixed(0)}%
      ({exp.n_winners || 0}W / {exp.n_losers || 0}L su {exp.n_total || 0} trade).
      {" "}<b>Hold time</b> medio {fmtNum(hold.avg_hold_days, 1)}g
      ({hold.avg_hold_steps || 0} step).
      {" "}<b>Commissioni</b> totali {fmtUsd(totalCommissions)}
      a {fmtNum(commissionBps, 1)} bps/trade.
    </>
  );
  return { general, specific };
}


/**
 * Equity Curve vs Benchmark (sovrapposizione portfolio vs SPY/BTC).
 */
export function explainEquityVsBenchmark(portfolio, benchmark, benchmarkTicker) {
  const general = (
    <>
      La <b>linea viola</b> è il valore del tuo portafoglio nel tempo.
      La <b>linea ciano tratteggiata</b> è il benchmark <b>{benchmarkTicker || "SPY"}</b>
      buy-and-hold scalato sullo stesso capitale iniziale. Se la viola sale meno
      della ciano, l'AI ha sottoperformato il "comprare e tenere" del benchmark
      → conviene tenere il benchmark invece di gestire attivamente. La differenza
      finale è chiamata <b>Alpha</b>: positivo = valore aggiunto dall'AI.
    </>
  );

  if (!portfolio || portfolio.length < 2) {
    return { general, specific: "Equity curve non disponibile." };
  }
  const first = portfolio[0]?.value;
  const last = portfolio[portfolio.length - 1]?.value;
  const portReturn = first ? ((last - first) / first) * 100 : null;
  const benchFirst = benchmark?.[0]?.value;
  const benchLast = benchmark?.[benchmark.length - 1]?.value;
  const benchReturn = benchFirst ? ((benchLast - benchFirst) / benchFirst) * 100 : null;
  const alpha = portReturn != null && benchReturn != null ? portReturn - benchReturn : null;

  const verdict = alpha == null ? "non calcolabile (manca benchmark)"
    : alpha > 1 ? "il portafoglio ha BATTUTO il benchmark"
    : alpha < -1 ? "il portafoglio ha SOTTOPERFORMATO il benchmark"
    : "il portafoglio è sostanzialmente IN LINEA con il benchmark (delta < ±1%)";

  return {
    general,
    specific: (
      <>
        Il tuo portfolio è andato da <b>{fmtUsd(first)}</b> a <b>{fmtUsd(last)}</b>
        ({fmtPct(portReturn)}). {benchmarkTicker || "Benchmark"}: {fmtPct(benchReturn)}.
        {" "}<b>Alpha = {fmtPct(alpha)}</b> → {verdict}.
      </>
    ),
  };
}


/**
 * Underwater Drawdown chart (drawdown running %).
 */
export function explainUnderwater(data, maxDD) {
  const general = (
    <>
      Mostra <b>quanto in basso</b> il portafoglio è andato rispetto al picco
      più alto raggiunto fino a quel momento. Quando la linea è a 0 sei al
      massimo storico del run; quando è negativa sei sotto il picco. Più
      profonda e larga è la "valle", più tempo passi in perdita prima di
      recuperare. Una strategia robusta ha drawdown contenuti e brevi.
    </>
  );

  if (!data || data.length === 0) {
    return { general, specific: "Dati underwater non disponibili." };
  }
  const dds = data.map(d => d.drawdown_pct);
  const stepsBelowZero = dds.filter(d => d < -0.5).length;
  const totalSteps = dds.length;
  const pctBelowZero = totalSteps ? (stepsBelowZero / totalSteps) * 100 : 0;

  const dur = maxDD?.duration_steps || 0;
  const peakIdx = maxDD?.peak_index ?? 0;
  const troughIdx = maxDD?.trough_index ?? 0;
  const dolore = (maxDD?.max_drawdown_pct ?? 0) <= -10
    ? "dolore significativo"
    : (maxDD?.max_drawdown_pct ?? 0) <= -5 ? "drawdown moderato"
    : "drawdown contenuto";

  return {
    general,
    specific: (
      <>
        Il drawdown peggiore è stato <b>{fmtPct(maxDD?.max_drawdown_pct, false)}</b>
        ({dolore}): peak <b>{fmtUsd(maxDD?.peak_value)}</b> al passo T{peakIdx},
        trough <b>{fmtUsd(maxDD?.trough_value)}</b> al passo T{troughIdx},
        durato <b>{dur} {dur === 1 ? "step" : "step"}</b>.
        {" "}Il portafoglio è stato sotto il picco per <b>{stepsBelowZero}/{totalSteps} step</b>
        ({pctBelowZero.toFixed(0)}% del run).
      </>
    ),
  };
}


/**
 * Histogram of Returns per step.
 */
export function explainReturnsHistogram(data) {
  const general = (
    <>
      Frequenza dei <b>ritorni step-on-step</b>: ogni barra dice quanti step hai
      avuto in quel range di rendimento. Una distribuzione "a campana"
      simmetrica suggerisce <b>consistenza</b>; concentrazione su un lato indica
      <b> pochi grossi colpi</b> (positivi o negativi) che dominano il P&L. Se
      vedi una sola barra positiva enorme, il guadagno dipende da un singolo
      colpo di fortuna.
    </>
  );

  if (!data || !data.bins || data.bins.length === 0) {
    return { general, specific: "Istogramma non disponibile (servono almeno 2 step)." };
  }
  const bins = data.bins;
  const positiveBins = bins.filter(b => (b.lo + b.hi) / 2 >= 0);
  const negativeBins = bins.filter(b => (b.lo + b.hi) / 2 < 0);
  const totalPos = positiveBins.reduce((s, b) => s + b.count, 0);
  const totalNeg = negativeBins.reduce((s, b) => s + b.count, 0);
  const total = data.n_samples || (totalPos + totalNeg);
  const skew = totalPos > totalNeg * 1.5 ? "fortemente positiva (più step di guadagno)"
              : totalNeg > totalPos * 1.5 ? "fortemente negativa (più step di perdita)"
              : "bilanciata";

  return {
    general,
    specific: (
      <>
        <b>{total}</b> step osservati. Range: da <b>{fmtPct(data.min_return_pct)}</b> a
        {" "}<b>{fmtPct(data.max_return_pct)}</b>. {totalPos} step positivi vs {totalNeg}
        {" "}negativi → distribuzione <b>{skew}</b>.
      </>
    ),
  };
}


/**
 * Confidence vs Outcome scatter (overconfidence detector).
 */
export function explainConfidenceScatter(data) {
  const general = (
    <>
      Ogni punto è un trade chiuso o aperto. Asse <b>X</b>: conviction
      dichiarata dall'AI (BASSA / MEDIA / ALTA). Asse <b>Y</b>: P&L %
      effettivo. <b>Punti verdi</b> = profittevoli, <b>rossi</b> = in perdita,
      <b>bordo giallo</b> = posizione ancora aperta.
      {" "}Se i punti ALTA conviction stanno in basso (Y negativo), il modello è
      <b> overconfident</b> e il prompt va ricalibrato.
    </>
  );

  if (!data || data.length === 0) {
    return { general, specific: "Nessun trade da analizzare." };
  }
  const high = data.filter(d => d.conviction_score >= 0.85);
  const med = data.filter(d => d.conviction_score >= 0.65 && d.conviction_score < 0.85);
  const low = data.filter(d => d.conviction_score < 0.65);
  const winRate = (arr) => arr.length ? arr.filter(d => d.outcome_pct > 0).length / arr.length : null;
  const wrHigh = winRate(high);
  const wrMed = winRate(med);
  const wrLow = winRate(low);
  const verdict = wrHigh != null && wrHigh < 0.5 && high.length >= 3
    ? "overconfident (i trade ALTA conviction perdono più della metà delle volte)"
    : wrHigh != null && wrHigh > wrLow
    ? "calibrazione corretta (più conviction → più win rate)"
    : "calibrazione neutra";

  const fmtWR = (v, n) => v == null ? "n/d" : `${(v * 100).toFixed(0)}% (${n} trade)`;

  return {
    general,
    specific: (
      <>
        Hai <b>{data.length} trade</b> totali. Win rate per livello di conviction:
        {" "}ALTA <b>{fmtWR(wrHigh, high.length)}</b>,
        {" "}MEDIA <b>{fmtWR(wrMed, med.length)}</b>,
        {" "}BASSA <b>{fmtWR(wrLow, low.length)}</b>.
        {" "}Verdetto: <b>{verdict}</b>.
      </>
    ),
  };
}


/**
 * Slippage Sensitivity (replay con commission_bps diverse).
 */
export function explainSlippageSensitivity(currentBps) {
  return {
    general: (
      <>
        Replay del run a diversi livelli di commissione: <b>0%</b> (no fees),
        <b> 0.10%</b>, <b>0.25%</b>, <b>0.50%</b>, <b>1%</b>. Risponde alla
        domanda: "se le fees fossero state più alte, la strategia sarebbe
        ancora profittevole?". Se il P&L diventa negativo a 0.5%, la
        strategia non è robusta nel mondo reale (broker retail crypto/equity
        applicano spesso 25-50 bps).
      </>
    ),
    specific: (
      <>
        Il run originale è stato eseguito con <b>{fmtNum(currentBps, 1)} bps</b>
        di commissione. Clicca "Esegui Rerun" per vedere come cambierebbero
        Sharpe, P&L e Profit Factor agli altri livelli — è un <b>test di
        robustezza</b> della tua strategia.
      </>
    ),
  };
}


// ═══════════════════════════════════════════════════════════════════════
// Styles
// ═══════════════════════════════════════════════════════════════════════

const S = {
  wrap: { position: "relative", display: "inline-flex", alignItems: "center" },
  btn: {
    background: "transparent",
    border: "1px solid rgba(167,139,250,0.25)",
    color: "#a78bfa",
    width: 22, height: 22, borderRadius: "50%",
    display: "inline-flex", alignItems: "center", justifyContent: "center",
    cursor: "pointer", padding: 0, marginLeft: 8,
    transition: "all 0.15s ease",
  },
  btnOpen: {
    background: "rgba(167,139,250,0.15)",
    borderColor: "#a78bfa",
  },
  popover: {
    position: "absolute",
    top: "calc(100% + 8px)",
    right: 0,
    minWidth: 320, maxWidth: 440,
    background: "#0f172a",
    border: "1px solid rgba(167,139,250,0.35)",
    borderRadius: 10,
    boxShadow: "0 8px 24px rgba(0,0,0,0.4), 0 0 0 1px rgba(167,139,250,0.08)",
    padding: "12px 14px",
    zIndex: 100,
    color: "#cbd5e1",
    fontSize: 12.5,
    lineHeight: 1.55,
    fontWeight: 400,
  },
  popHeader: {
    display: "flex", justifyContent: "space-between", alignItems: "center",
    paddingBottom: 8,
    marginBottom: 10,
    borderBottom: "1px solid rgba(167,139,250,0.15)",
  },
  popTitle: {
    color: "#e2e8f0",
    fontWeight: 600,
    fontSize: 13,
  },
  popClose: {
    background: "transparent",
    border: 0,
    color: "#64748b",
    cursor: "pointer",
    padding: 2,
    display: "inline-flex",
  },
  section: { marginBottom: 12 },
  sectionLabel: {
    fontSize: 10,
    fontWeight: 700,
    color: "#94a3b8",
    textTransform: "uppercase",
    letterSpacing: "0.06em",
    marginBottom: 4,
  },
  sectionBody: {
    color: "#cbd5e1",
    fontSize: 12.5,
    lineHeight: 1.6,
  },
  list: {
    margin: "6px 0 0 0",
    paddingLeft: 18,
    color: "#cbd5e1",
  },
};
