import { useState } from "react";
import {
  Activity, TrendingDown, BarChart3, Target,
  Zap, Clock, DollarSign, Sigma,
} from "lucide-react";
import ChartHelpButton, {
  explainKPIBlock, explainEquityVsBenchmark, explainUnderwater,
  explainReturnsHistogram, explainConfidenceScatter, explainSlippageSensitivity,
} from "./ChartHelpButton";

const API = window.location.origin;

/**
 * SimMetricsPanel — KPI quantitativi + grafici statistici di una partita V2.
 *
 * Riceve:
 *   quantMetrics:           da run.full_data.quant_metrics  (dict)
 *   portfolioValueSeries:   equity curve del portafoglio    (list)
 *   benchmarkValueSeries:   equity curve SPY/BTC            (list)
 *   benchmarkTicker:        "SPY" | "BTC-USD"
 *   commissionBps:          bps applicati (es. 10 = 0.10%)
 *   totalCommissions:       totale fees in $
 *   runId:                  per il rerun slippage sensitivity
 *
 * Render condizionale: se quantMetrics e' assente (run V1 legacy), il
 * componente ritorna null. Tutti i grafici sono SVG inline (no librerie
 * pesanti, coerente col resto della UI).
 */
export default function SimMetricsPanel({
  quantMetrics, portfolioValueSeries, benchmarkValueSeries,
  benchmarkTicker, commissionBps, totalCommissions, runId,
}) {
  if (!quantMetrics) return null;

  return (
    <>
      {/* KPI scalari */}
      <KPIBlock
        quantMetrics={quantMetrics}
        commissionBps={commissionBps}
        totalCommissions={totalCommissions}
      />

      {/* Equity vs Benchmark */}
      {portfolioValueSeries && portfolioValueSeries.length > 1 && (
        <EquityVsBenchmarkChart
          portfolio={portfolioValueSeries}
          benchmark={benchmarkValueSeries}
          benchmarkTicker={benchmarkTicker}
        />
      )}

      {/* Underwater */}
      {quantMetrics.underwater_curve && quantMetrics.underwater_curve.length > 1 && (
        <UnderwaterChart data={quantMetrics.underwater_curve}
                         maxDD={quantMetrics.max_drawdown} />
      )}

      {/* Histogram returns */}
      {quantMetrics.returns_histogram && quantMetrics.returns_histogram.bins.length > 0 && (
        <ReturnsHistogram data={quantMetrics.returns_histogram} />
      )}

      {/* Confidence vs Outcome */}
      {quantMetrics.confidence_outcome_scatter
        && quantMetrics.confidence_outcome_scatter.length > 0 && (
        <ConfidenceOutcomeScatter data={quantMetrics.confidence_outcome_scatter} />
      )}

      {/* Slippage Sensitivity (on-demand) */}
      {runId && <SlippageSensitivity runId={runId} commissionBps={commissionBps} />}
    </>
  );
}


// ─── KPI Block (Sharpe, MDD, Profit Factor, Expectancy, Hold time, Fees) ──

function KPIBlock({ quantMetrics, commissionBps, totalCommissions }) {
  const sharpe = quantMetrics.sharpe_ratio;
  const mdd = quantMetrics.max_drawdown?.max_drawdown_pct;
  const pf = quantMetrics.profit_factor;
  const pfInf = quantMetrics.profit_factor_infinite;
  const exp = quantMetrics.expectancy || {};
  const hold = quantMetrics.avg_hold_time || {};

  const sharpeColor = sharpe == null ? "#94a3b8"
    : sharpe >= 2 ? "#10b981"
    : sharpe >= 1 ? "#84cc16"
    : sharpe >= 0 ? "#fbbf24"
    : "#ef4444";

  const sharpeLabel = sharpe == null ? "n/d"
    : sharpe >= 2 ? "Eccellente"
    : sharpe >= 1 ? "Buono"
    : sharpe >= 0 ? "Mediocre"
    : "Negativo";

  const pfDisplay = pfInf ? "∞" : pf == null ? "—" : pf.toFixed(2);
  const pfColor = pfInf ? "#10b981"
    : pf == null ? "#94a3b8"
    : pf >= 2 ? "#10b981"
    : pf >= 1 ? "#84cc16"
    : "#ef4444";

  const help = explainKPIBlock(quantMetrics, commissionBps, totalCommissions);
  return (
    <div style={S.card}>
      <div style={{ ...S.cardTitle, justifyContent: "space-between" }}>
        <span style={{ display: "inline-flex", alignItems: "center" }}>
          <Sigma size={14} style={{ display: "inline", marginRight: 6, color: "#a78bfa" }} />
          Metriche di Performance Quantitative
        </span>
        <ChartHelpButton title="Metriche Quantitative"
                          general={help.general} specific={help.specific} />
      </div>
      <div style={S.subtitle}>
        Calcoli numerici sulla qualità della strategia, non solo sul P&L finale.
      </div>
      <div style={S.kpiGrid}>
        <KPICard
          label="Sharpe Ratio"
          value={sharpe != null ? sharpe.toFixed(2) : "—"}
          sub={sharpeLabel}
          tone={sharpeColor}
          icon={<Activity size={14} />}
          tooltip="Rendimento risk-adjusted annualizzato. >2 eccellente, 1-2 buono, <0 peggio del cash."
        />
        <KPICard
          label="Max Drawdown"
          value={mdd != null ? `${mdd.toFixed(2)}%` : "—"}
          sub={`Peak ${quantMetrics.max_drawdown?.peak_value || 0} → ${quantMetrics.max_drawdown?.trough_value || 0}`}
          tone={mdd != null && mdd <= -10 ? "#ef4444" : "#fbbf24"}
          icon={<TrendingDown size={14} />}
          tooltip="Massima perdita dal picco più alto. Quanta sofferenza prima del recupero."
        />
        <KPICard
          label="Profit Factor"
          value={pfDisplay}
          sub={`${exp.n_winners || 0}W vs ${exp.n_losers || 0}L`}
          tone={pfColor}
          icon={<Target size={14} />}
          tooltip="Σ winners / |Σ losers|. >2 = eccellente, <1 = sistema in perdita strutturale."
        />
        <KPICard
          label="Expectancy / trade"
          value={exp.expectancy_dollars != null
            ? `$${exp.expectancy_dollars >= 0 ? "+" : ""}${exp.expectancy_dollars.toFixed(0)}`
            : "—"}
          sub={`Win rate ${((exp.win_rate || 0) * 100).toFixed(0)}%`}
          tone={exp.expectancy_dollars >= 0 ? "#10b981" : "#ef4444"}
          icon={<DollarSign size={14} />}
          tooltip="Quanto ti aspetti di guadagnare in media per ogni trade."
        />
        <KPICard
          label="Avg Hold Time"
          value={hold.avg_hold_days != null
            ? `${hold.avg_hold_days.toFixed(1)}g`
            : "—"}
          sub={`${hold.avg_hold_steps || 0} step (${hold.n_open_close_pairs || 0} chiusi, ${hold.still_open_at_end || 0} aperti)`}
          tone="#06b6d4"
          icon={<Clock size={14} />}
          tooltip="Quanto in media tieni una posizione. Troppo corto = fees ti mangiano. Troppo lungo = pigro."
        />
        <KPICard
          label="Commissioni totali"
          value={totalCommissions != null
            ? `$${(totalCommissions || 0).toFixed(2)}`
            : "—"}
          sub={`${commissionBps != null ? commissionBps.toFixed(1) : "?"} bps per trade`}
          tone="#94a3b8"
          icon={<Zap size={14} />}
          tooltip={`Pagate ${commissionBps != null ? commissionBps.toFixed(1) : "?"} bps su ogni BUY/SELL. Slippage simulato.`}
        />
      </div>
    </div>
  );
}


// ─── Equity Curve vs Benchmark (overlay SPY o BTC) ────────────────────────

function EquityVsBenchmarkChart({ portfolio, benchmark, benchmarkTicker }) {
  const w = 800, h = 280, padL = 65, padR = 60, padT = 20, padB = 30;
  const innerW = w - padL - padR, innerH = h - padT - padB;

  const allValues = [
    ...portfolio.map(p => p.value).filter(v => v != null),
    ...(benchmark || []).map(p => p.value).filter(v => v != null),
  ];
  if (allValues.length === 0) {
    return (
      <div style={S.card}>
        <div style={S.cardTitle}>Equity Curve vs Benchmark</div>
        <div style={{ color: "#64748b", padding: 20, textAlign: "center" }}>
          Dati insufficienti per il grafico
        </div>
      </div>
    );
  }
  let minV = Math.min(...allValues), maxV = Math.max(...allValues);
  const range = maxV - minV || 1;
  const pad = range * 0.05;
  minV -= pad; maxV += pad;
  const finalRange = maxV - minV;

  const n = portfolio.length;
  const xToPx = (i) => padL + (i / Math.max(1, n - 1)) * innerW;
  const yToPx = (v) => padT + innerH - ((v - minV) / finalRange) * innerH;

  const portfolioPoints = portfolio.map((p, i) =>
    `${xToPx(i)},${yToPx(p.value)}`).join(" ");

  // Allinea il benchmark sugli stessi index del portfolio (se hanno la
  // stessa cadenza degli step; in caso contrario, fallback su index naive)
  const benchPoints = benchmark && benchmark.length
    ? benchmark.map((p, i) => `${xToPx(Math.min(i, n - 1))},${yToPx(p.value)}`).join(" ")
    : null;

  const finalPort = portfolio[n - 1]?.value;
  const finalBench = benchmark && benchmark.length ? benchmark[benchmark.length - 1].value : null;
  const portReturn = portfolio[0] && finalPort != null
    ? (finalPort - portfolio[0].value) / portfolio[0].value * 100 : null;
  const benchReturn = benchmark && benchmark[0] && finalBench != null
    ? (finalBench - benchmark[0].value) / benchmark[0].value * 100 : null;
  const alpha = portReturn != null && benchReturn != null ? portReturn - benchReturn : null;

  const help = explainEquityVsBenchmark(portfolio, benchmark, benchmarkTicker);
  return (
    <div style={S.card}>
      <div style={{ ...S.cardTitle, justifyContent: "space-between" }}>
        <span style={{ display: "inline-flex", alignItems: "center" }}>
          <BarChart3 size={14} style={{ display: "inline", marginRight: 6, color: "#a78bfa" }} />
          Equity Curve vs {benchmarkTicker || "Benchmark"}
        </span>
        <ChartHelpButton title={`Equity Curve vs ${benchmarkTicker || "Benchmark"}`}
                          general={help.general} specific={help.specific} />
      </div>
      <div style={S.subtitle}>
        La tua linea sovrapposta al benchmark buy-and-hold. Se la tua sale meno
        del benchmark, l'AI ha sottoperformato il "comprare e tenere".
      </div>

      {/* Mini summary sopra il chart */}
      <div style={{ display: "flex", gap: 16, marginBottom: 14, flexWrap: "wrap" }}>
        <Pill label="Portafoglio" value={fmtPctSigned(portReturn)}
              color={pctColor(portReturn)} />
        <Pill label={benchmarkTicker || "Benchmark"} value={fmtPctSigned(benchReturn)}
              color={pctColor(benchReturn)} />
        <Pill label="Alpha" value={fmtPctSigned(alpha)}
              color={pctColor(alpha)} bold />
      </div>

      <svg width="100%" height={h} viewBox={`0 0 ${w} ${h}`} style={{ display: "block" }}>
        {/* Griglia */}
        {[0, 0.25, 0.5, 0.75, 1].map((t, i) => {
          const v = minV + t * finalRange;
          const y = yToPx(v);
          return (
            <g key={i}>
              <line x1={padL} y1={y} x2={w - padR} y2={y}
                    stroke="#1f2937" strokeWidth={1} strokeDasharray="3 3" opacity={0.5} />
              <text x={padL - 6} y={y + 4} fill="#64748b" fontSize={10}
                    textAnchor="end" fontFamily="monospace">
                ${(v / 1000).toFixed(1)}k
              </text>
            </g>
          );
        })}

        {/* Linea benchmark */}
        {benchPoints && (
          <polyline points={benchPoints} fill="none" stroke="#06b6d4"
                    strokeWidth={2} strokeDasharray="4 4" opacity={0.85} />
        )}

        {/* Linea portfolio (sopra) */}
        <polyline points={portfolioPoints} fill="none" stroke="#a78bfa" strokeWidth={2.5} />

        {/* Punti agli step */}
        {portfolio.map((p, i) => (
          <circle key={i} cx={xToPx(i)} cy={yToPx(p.value)} r={3}
                  fill="#a78bfa" stroke="#0a0e1a" strokeWidth={1} />
        ))}

        {/* X-axis labels: prima e ultima data */}
        <text x={padL} y={h - 8} fill="#64748b" fontSize={10}>
          {portfolio[0]?.step_date || "T0"}
        </text>
        <text x={w - padR} y={h - 8} fill="#64748b" fontSize={10} textAnchor="end">
          {portfolio[n - 1]?.step_date || "Tn"}
        </text>

        {/* Legenda */}
        <g transform={`translate(${w - padR - 130}, ${padT + 5})`}>
          <rect x={0} y={0} width={130} height={36} rx={4}
                fill="#0a1018" stroke="#1f2937" />
          <line x1={8} y1={12} x2={28} y2={12} stroke="#a78bfa" strokeWidth={2.5} />
          <text x={32} y={15} fill="#cbd5e1" fontSize={10}>Portafoglio</text>
          <line x1={8} y1={26} x2={28} y2={26} stroke="#06b6d4" strokeWidth={2}
                strokeDasharray="4 4" />
          <text x={32} y={29} fill="#cbd5e1" fontSize={10}>{benchmarkTicker || "Benchmark"}</text>
        </g>
      </svg>
    </div>
  );
}


// ─── Underwater Chart (drawdowns running) ─────────────────────────────────

function UnderwaterChart({ data, maxDD }) {
  const w = 800, h = 200, padL = 65, padR = 30, padT = 20, padB = 30;
  const innerW = w - padL - padR, innerH = h - padT - padB;

  const dds = data.map(d => d.drawdown_pct);
  const minDD = Math.min(0, ...dds);   // <= 0
  const range = -minDD || 1;
  const n = data.length;

  const xToPx = (i) => padL + (i / Math.max(1, n - 1)) * innerW;
  // y: 0 in alto, minDD in basso
  const yToPx = (dd) => padT + (-dd / range) * innerH;

  // Polygon "area sotto zero"
  const points = data.map((d, i) => `${xToPx(i)},${yToPx(d.drawdown_pct)}`).join(" ");
  const areaPoints = `${padL},${padT} ${points} ${xToPx(n - 1)},${padT}`;

  const help = explainUnderwater(data, maxDD);
  return (
    <div style={S.card}>
      <div style={{ ...S.cardTitle, justifyContent: "space-between" }}>
        <span style={{ display: "inline-flex", alignItems: "center" }}>
          <TrendingDown size={14} style={{ display: "inline", marginRight: 6, color: "#ef4444" }} />
          Underwater Drawdown
        </span>
        <ChartHelpButton title="Underwater Drawdown"
                          general={help.general} specific={help.specific} />
      </div>
      <div style={S.subtitle}>
        Quanto tempo il portafoglio passa sotto zero rispetto al picco più alto.
        Più la "valle" è profonda e larga, più la strategia richiede
        sopportazione del dolore.
      </div>
      {maxDD && maxDD.duration_steps > 0 && (
        <div style={{ marginBottom: 10, fontSize: 12, color: "#94a3b8" }}>
          Worst run: <strong style={{ color: "#fca5a5" }}>{maxDD.max_drawdown_pct}%</strong>
          {" "}durato <strong style={{ color: "#fca5a5" }}>{maxDD.duration_steps} step</strong>
          {" "}(da T{maxDD.peak_index} a T{maxDD.trough_index})
        </div>
      )}
      <svg width="100%" height={h} viewBox={`0 0 ${w} ${h}`} style={{ display: "block" }}>
        {/* Linea zero */}
        <line x1={padL} y1={padT} x2={w - padR} y2={padT}
              stroke="#1f2937" strokeWidth={1} />
        {/* Area drawdown */}
        <polygon points={areaPoints}
                 fill="rgba(239,68,68,0.18)" stroke="#ef4444" strokeWidth={1.5} />
        {/* Y axis labels */}
        {[0, 0.5, 1].map((t, i) => {
          const dd = minDD * t;
          const y = yToPx(dd);
          return (
            <text key={i} x={padL - 6} y={y + 4} fill="#64748b" fontSize={10}
                  textAnchor="end" fontFamily="monospace">
              {dd.toFixed(1)}%
            </text>
          );
        })}
        {/* X axis labels */}
        <text x={padL} y={h - 8} fill="#64748b" fontSize={10}>
          {data[0]?.step_date || "T0"}
        </text>
        <text x={w - padR} y={h - 8} fill="#64748b" fontSize={10} textAnchor="end">
          {data[n - 1]?.step_date || "Tn"}
        </text>
      </svg>
    </div>
  );
}


// ─── Histogram of Returns ─────────────────────────────────────────────────

function ReturnsHistogram({ data }) {
  const bins = data.bins || [];
  if (bins.length === 0) return null;
  const w = 800, h = 220, padL = 50, padR = 30, padT = 20, padB = 35;
  const innerW = w - padL - padR, innerH = h - padT - padB;
  const maxCount = Math.max(...bins.map(b => b.count), 1);
  const binW = innerW / bins.length;

  const help = explainReturnsHistogram(data);
  return (
    <div style={S.card}>
      <div style={{ ...S.cardTitle, justifyContent: "space-between" }}>
        <span style={{ display: "inline-flex", alignItems: "center" }}>
          <BarChart3 size={14} style={{ display: "inline", marginRight: 6, color: "#fbbf24" }} />
          Distribuzione dei Ritorni per Step
        </span>
        <ChartHelpButton title="Distribuzione Ritorni"
                          general={help.general} specific={help.specific} />
      </div>
      <div style={S.subtitle}>
        Frequenza dei ritorni tra step. Una distribuzione "a campana" suggerisce
        consistenza; concentrazione su un lato indica pochi grossi colpi vs
        molti piccoli.
      </div>
      <div style={{ marginBottom: 10, fontSize: 11, color: "#94a3b8" }}>
        Range osservato:{" "}
        <strong style={{ color: data.min_return_pct >= 0 ? "#86efac" : "#fca5a5" }}>
          {data.min_return_pct.toFixed(2)}%
        </strong>
        {" "}→{" "}
        <strong style={{ color: data.max_return_pct >= 0 ? "#86efac" : "#fca5a5" }}>
          {data.max_return_pct.toFixed(2)}%
        </strong>
        {" "}su {data.n_samples} step
      </div>
      <svg width="100%" height={h} viewBox={`0 0 ${w} ${h}`} style={{ display: "block" }}>
        {/* Y axis labels */}
        {[0, 0.5, 1].map((t, i) => {
          const c = Math.round(maxCount * t);
          const y = padT + innerH - innerH * t;
          return (
            <g key={i}>
              <line x1={padL} y1={y} x2={w - padR} y2={y}
                    stroke="#1f2937" strokeWidth={1} strokeDasharray="3 3" opacity={0.4} />
              <text x={padL - 6} y={y + 4} fill="#64748b" fontSize={10} textAnchor="end">
                {c}
              </text>
            </g>
          );
        })}
        {/* Linea zero return (verticale) — se c'e' la transizione */}
        {data.min_return_pct < 0 && data.max_return_pct > 0 && (() => {
          const range = data.max_return_pct - data.min_return_pct;
          const zeroPx = padL + ((-data.min_return_pct) / range) * innerW;
          return (
            <line x1={zeroPx} y1={padT} x2={zeroPx} y2={padT + innerH}
                  stroke="#475569" strokeWidth={1} strokeDasharray="2 2" />
          );
        })()}
        {/* Barre */}
        {bins.map((b, i) => {
          const barH = (b.count / maxCount) * innerH;
          const x = padL + i * binW;
          const y = padT + innerH - barH;
          const midpoint = (b.lo + b.hi) / 2;
          const fill = midpoint >= 0 ? "#10b981" : "#ef4444";
          return (
            <g key={i}>
              <rect x={x + 1} y={y} width={Math.max(2, binW - 2)} height={barH}
                    fill={fill} opacity={0.75} />
              {b.count > 0 && (
                <text x={x + binW / 2} y={y - 4} fill="#cbd5e1" fontSize={9}
                      textAnchor="middle">{b.count}</text>
              )}
            </g>
          );
        })}
        {/* Asse X labels: prima, mezzo, ultima */}
        <text x={padL} y={h - 8} fill="#64748b" fontSize={10}>
          {bins[0]?.lo.toFixed(1)}%
        </text>
        <text x={w / 2} y={h - 8} fill="#64748b" fontSize={10} textAnchor="middle">
          Ritorno per step (%)
        </text>
        <text x={w - padR} y={h - 8} fill="#64748b" fontSize={10} textAnchor="end">
          {bins[bins.length - 1]?.hi.toFixed(1)}%
        </text>
      </svg>
    </div>
  );
}


// ─── Confidence vs Outcome Scatter ────────────────────────────────────────

function ConfidenceOutcomeScatter({ data }) {
  if (!data || data.length === 0) return null;
  const w = 800, h = 280, padL = 60, padR = 30, padT = 20, padB = 35;
  const innerW = w - padL - padR, innerH = h - padT - padB;

  const xs = data.map(d => d.conviction_score);
  const ys = data.map(d => d.outcome_pct);
  const xMin = 0.4, xMax = 1.05;   // conviction range tipico (0.5-1.0 + padding)
  let yMin = Math.min(0, ...ys);
  let yMax = Math.max(0, ...ys);
  const yRange = (yMax - yMin) || 1;
  yMin -= yRange * 0.1; yMax += yRange * 0.1;

  const xToPx = (x) => padL + ((x - xMin) / (xMax - xMin)) * innerW;
  const yToPx = (y) => padT + innerH - ((y - yMin) / (yMax - yMin)) * innerH;

  // Diagnostico: le ad-alta-conviction sono in profitto?
  const highConv = data.filter(d => d.conviction_score >= 0.85);
  const highConvWinners = highConv.filter(d => d.outcome_pct > 0).length;
  const highConvWinRate = highConv.length ? highConvWinners / highConv.length : null;
  const overconfident = highConvWinRate != null && highConvWinRate < 0.5 && highConv.length >= 3;

  const help = explainConfidenceScatter(data);
  return (
    <div style={S.card}>
      <div style={{ ...S.cardTitle, justifyContent: "space-between" }}>
        <span style={{ display: "inline-flex", alignItems: "center" }}>
          <Target size={14} style={{ display: "inline", marginRight: 6, color: "#06b6d4" }} />
          Confidenza vs Esito (overconfidence detector)
        </span>
        <ChartHelpButton title="Confidenza vs Esito"
                          general={help.general} specific={help.specific} />
      </div>
      <div style={S.subtitle}>
        Ogni punto è un trade. Asse X: conviction dichiarata dall'AI. Asse Y: P&L
        % effettivo. Se i punti ALTA conviction stanno in basso, il modello è
        overconfident e va ricalibrato.
      </div>
      {overconfident && (
        <div style={S.warnBanner}>
          ⚠️ Possibile overconfidence: dei {highConv.length} trade ad ALTA conviction,
          solo {highConvWinners} sono profittevoli ({(highConvWinRate * 100).toFixed(0)}% win rate).
          Considera di rivedere il prompt del Decision Agent.
        </div>
      )}
      {!overconfident && highConvWinRate != null && (
        <div style={{ marginBottom: 10, fontSize: 12, color: "#86efac" }}>
          Calibrazione conviction OK: {highConvWinners}/{highConv.length} trade ALTA
          conviction profittevoli ({(highConvWinRate * 100).toFixed(0)}%).
        </div>
      )}
      <svg width="100%" height={h} viewBox={`0 0 ${w} ${h}`} style={{ display: "block" }}>
        {/* Asse Y zero */}
        <line x1={padL} y1={yToPx(0)} x2={w - padR} y2={yToPx(0)}
              stroke="#475569" strokeWidth={1} strokeDasharray="3 3" />
        {/* Griglia X (BASSA, MEDIA, ALTA) */}
        {[
          { x: 0.5, label: "BASSA" },
          { x: 0.75, label: "MEDIA" },
          { x: 1.0, label: "ALTA" },
        ].map((tk, i) => (
          <g key={i}>
            <line x1={xToPx(tk.x)} y1={padT} x2={xToPx(tk.x)} y2={padT + innerH}
                  stroke="#1f2937" strokeWidth={1} strokeDasharray="2 2" />
            <text x={xToPx(tk.x)} y={h - 12} fill="#64748b" fontSize={11}
                  textAnchor="middle">{tk.label}</text>
          </g>
        ))}
        {/* Asse Y labels */}
        {[0, 0.5, 1].map((t, i) => {
          const y = yMax - t * (yMax - yMin);
          return (
            <text key={i} x={padL - 6} y={yToPx(y) + 4} fill="#64748b" fontSize={10}
                  textAnchor="end" fontFamily="monospace">
              {y >= 0 ? "+" : ""}{y.toFixed(1)}%
            </text>
          );
        })}
        {/* Punti */}
        {data.map((d, i) => {
          const cx = xToPx(d.conviction_score);
          const cy = yToPx(d.outcome_pct);
          const fill = d.outcome_pct >= 0 ? "#10b981" : "#ef4444";
          const r = d.status === "open" ? 5 : 6;
          const stroke = d.status === "open" ? "#fbbf24" : "#0a0e1a";
          return (
            <g key={i}>
              <circle cx={cx} cy={cy} r={r} fill={fill} fillOpacity={0.75}
                      stroke={stroke} strokeWidth={d.status === "open" ? 1.5 : 1.5}>
                <title>
                  {d.action} {d.asset} {d.conviction} → {d.outcome_pct >= 0 ? "+" : ""}
                  {d.outcome_pct.toFixed(2)}% (${d.outcome_dollars >= 0 ? "+" : ""}
                  {d.outcome_dollars.toFixed(0)}) [{d.status}]
                </title>
              </circle>
            </g>
          );
        })}
        {/* Etichette */}
        <text x={w / 2} y={h - 2} fill="#64748b" fontSize={10} textAnchor="middle">
          Conviction (X) vs Outcome % (Y) — bordo giallo = posizione ancora aperta
        </text>
      </svg>
    </div>
  );
}


// ─── Slippage Sensitivity (rerun on demand) ───────────────────────────────

function SlippageSensitivity({ runId, commissionBps }) {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const runRerun = async () => {
    setBusy(true); setError(null);
    try {
      const res = await fetch(`${API}/api/simulator/slippage-rerun`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          run_id: runId,
          commission_bps_list: [0, 10, 25, 50, 100],
        }),
      });
      const j = await res.json();
      if (!res.ok) {
        setError(j.error || `HTTP ${res.status}`);
      } else {
        setData(j);
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const help = explainSlippageSensitivity(commissionBps);
  return (
    <div style={S.card}>
      <div style={{ ...S.cardTitle, justifyContent: "space-between" }}>
        <span style={{ display: "inline-flex", alignItems: "center" }}>
          <Zap size={14} style={{ display: "inline", marginRight: 6, color: "#fbbf24" }} />
          Slippage Sensitivity Test
        </span>
        <ChartHelpButton title="Slippage Sensitivity"
                          general={help.general} specific={help.specific} />
      </div>
      <div style={S.subtitle}>
        Replay del run con diverse rate di commissione (0% / 0.1% / 0.25% / 0.5% / 1%).
        Se il profitto sparisce a 0.5%, la strategia non è robusta per il mondo reale.
      </div>
      <div style={{ marginBottom: 12, fontSize: 12, color: "#94a3b8" }}>
        Run originale: <strong style={{ color: "#cbd5e1" }}>
          {commissionBps != null ? `${commissionBps.toFixed(1)} bps` : "default"}
        </strong>
      </div>
      {!data && (
        <button style={S.btnAction} onClick={runRerun} disabled={busy}>
          {busy ? "Rerun in corso..." : "Esegui Rerun a 0% / 0.10% / 0.25% / 0.50% / 1%"}
        </button>
      )}
      {error && <div style={S.errorBox}>⚠️ {error}</div>}
      {data && data.scenarios && (
        <table style={S.slippageTable}>
          <thead>
            <tr>
              <th>Commission</th>
              <th>P&L finale</th>
              <th>Sharpe</th>
              <th>Max DD</th>
              <th>Profit Factor</th>
              <th>Fees totali</th>
            </tr>
          </thead>
          <tbody>
            {data.scenarios.map((s, i) => (
              <tr key={i}>
                <td style={{ fontFamily: "monospace" }}>
                  {s.commission_bps.toFixed(0)} bps
                  {commissionBps != null && Math.abs(s.commission_bps - commissionBps) < 0.5 && (
                    <span style={{ marginLeft: 6, fontSize: 10, color: "#a78bfa" }}>
                      (originale)
                    </span>
                  )}
                </td>
                <td style={{ color: pctColor(s.final_pnl_pct / 100), fontWeight: 600 }}>
                  {s.final_pnl_pct >= 0 ? "+" : ""}{s.final_pnl_pct.toFixed(2)}%
                </td>
                <td>{s.sharpe_ratio != null ? s.sharpe_ratio.toFixed(2) : "—"}</td>
                <td style={{ color: "#fca5a5" }}>
                  {s.max_drawdown != null ? `${s.max_drawdown.toFixed(2)}%` : "—"}
                </td>
                <td>{s.profit_factor != null ? s.profit_factor.toFixed(2) : "—"}</td>
                <td style={{ color: "#94a3b8" }}>
                  ${(s.total_commissions_paid || 0).toFixed(2)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}


// ─── Helpers visivi ───────────────────────────────────────────────────────

function KPICard({ label, value, sub, tone, icon, tooltip }) {
  return (
    <div style={S.kpiCard} title={tooltip || ""}>
      <div style={{ display: "flex", justifyContent: "space-between",
                    alignItems: "center", marginBottom: 6 }}>
        <div style={S.kpiLabel}>{label}</div>
        {icon && <div style={{ color: tone, opacity: 0.7 }}>{icon}</div>}
      </div>
      <div style={{ ...S.kpiValue, color: tone }}>{value}</div>
      <div style={S.kpiSub}>{sub}</div>
    </div>
  );
}

function Pill({ label, value, color, bold }) {
  return (
    <div style={{
      background: "#0f172a", padding: "8px 12px", borderRadius: 6,
      border: "1px solid #1f2937", display: "inline-flex", alignItems: "baseline", gap: 8,
    }}>
      <span style={{ fontSize: 11, color: "#94a3b8" }}>{label}</span>
      <span style={{ fontSize: 14, color, fontWeight: bold ? 700 : 600 }}>{value}</span>
    </div>
  );
}

function fmtPctSigned(v) {
  if (v == null) return "—";
  return `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`;
}

function pctColor(v) {
  if (v == null) return "#94a3b8";
  return v >= 0 ? "#10b981" : "#ef4444";
}


const S = {
  card: {
    background: "#111827", border: "1px solid #1f2937", padding: 18,
    borderRadius: 10, marginBottom: 16,
  },
  cardTitle: {
    fontSize: 14, fontWeight: 600, color: "#cbd5e1",
    marginBottom: 6, display: "flex", alignItems: "center",
  },
  subtitle: {
    fontSize: 12, color: "#94a3b8", marginBottom: 14, lineHeight: 1.55,
  },
  kpiGrid: {
    display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 10,
  },
  kpiCard: {
    background: "#0f172a", padding: "12px 14px", borderRadius: 8,
    border: "1px solid #1a2030", cursor: "default",
  },
  kpiLabel: {
    fontSize: 10, fontWeight: 600, textTransform: "uppercase",
    letterSpacing: "0.06em", color: "#64748b",
  },
  kpiValue: { fontSize: 22, fontWeight: 700, marginBottom: 2 },
  kpiSub: { fontSize: 11, color: "#94a3b8" },

  warnBanner: {
    background: "rgba(251,191,36,0.08)",
    border: "1px solid rgba(251,191,36,0.3)",
    color: "#fcd34d", padding: "10px 14px", borderRadius: 8,
    fontSize: 12.5, lineHeight: 1.5, marginBottom: 12,
  },

  btnAction: {
    background: "#a78bfa", color: "#0a0e1a", border: 0,
    padding: "10px 16px", borderRadius: 6, fontWeight: 600,
    cursor: "pointer", fontSize: 13, fontFamily: "inherit",
  },
  errorBox: {
    background: "rgba(239,68,68,0.08)",
    border: "1px solid rgba(239,68,68,0.3)",
    color: "#fca5a5", padding: "10px 14px", borderRadius: 8,
    fontSize: 12.5, marginTop: 12,
  },
  slippageTable: {
    width: "100%", borderCollapse: "collapse", fontSize: 13, marginTop: 8,
  },
};
