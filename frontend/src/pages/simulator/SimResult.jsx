import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, ArrowRight, Eye, Brain, Target, AlertCircle, TrendingUp, TrendingDown, Sparkles, BarChart3 } from "lucide-react";
import SimAdvisorChat from "../../components/SimAdvisorChat";

const API = window.location.origin;

const fmtPct = (v) => v == null ? "—" : `${v >= 0 ? "+" : ""}${(v * 100).toFixed(2)}%`;
const fmtUsd = (v) => v == null ? "—" :
  new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(v);
const colorFor = (v) => v == null ? "#64748b" : v >= 0 ? "#10b981" : "#ef4444";

/**
 * Risultato di uno scenario: verdetto + grafico + analisi del ragionamento +
 * SL/TP analysis + step breakdown + statistiche di prezzo.
 */
export default function SimResult() {
  const { runId } = useParams();
  const nav = useNavigate();
  const [run, setRun] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [activeTab, setActiveTab] = useState("result"); // "result" | "advisor"

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`${API}/api/simulator/result/${runId}`);
        let data;
        try { data = await res.json(); } catch { data = { error: `HTTP ${res.status}` }; }
        if (cancelled) return;
        if (!res.ok) {
          const parts = [];
          if (data.error) parts.push(data.error);
          if (data.hint) parts.push(`Suggerimento: ${data.hint}`);
          if (data.in_active_memory != null) {
            parts.push(`Run in memoria: ${data.in_active_memory ? "sì" : "no"}`);
          }
          setError(parts.join("\n") || `HTTP ${res.status}`);
        } else {
          setRun(data);
        }
      } catch (e) {
        if (!cancelled) setError(String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [runId]);

  if (loading) return <div style={S.empty}>Caricamento risultato...</div>;
  if (error) {
    return (
      <div style={S.empty}>
        <div style={{ color: "#ef4444", fontSize: 16, marginBottom: 12 }}>
          ⚠ Errore caricamento risultato
        </div>
        <div style={{ whiteSpace: "pre-line", fontSize: 13, color: "#cbd5e1", lineHeight: 1.6 }}>
          {error}
        </div>
        <div style={{ marginTop: 20, display: "flex", gap: 8, justifyContent: "center" }}>
          <button style={S.backBtn} onClick={() => nav("/simulator")}>
            <ArrowLeft size={16} /> Dashboard
          </button>
          <button style={S.btnPrimary} onClick={() => nav("/simulator/runner")}>
            Avvia nuovo scenario <ArrowRight size={16} />
          </button>
        </div>
      </div>
    );
  }
  if (!run) return <div style={S.empty}>Risultato non trovato</div>;

  return (
    <div>
      {/* HEADER */}
      <div style={S.headerRow}>
        <button style={S.backBtn} onClick={() => nav("/simulator")}>
          <ArrowLeft size={16} /> Dashboard
        </button>
        <h1 style={S.h1}>Risultato Scenario</h1>
        <button style={S.btnPrimary} onClick={() => nav("/simulator/runner")}>
          Avvia nuovo <ArrowRight size={16} />
        </button>
      </div>

      {/* TAB SWITCHER */}
      <div style={S.tabBar}>
        <button
          style={{ ...S.tabBtn, ...(activeTab === "result" ? S.tabBtnActive : {}) }}
          onClick={() => setActiveTab("result")}
        >
          <BarChart3 size={14} style={{ marginRight: 6 }} />
          Risultato & Analisi
        </button>
        <button
          style={{ ...S.tabBtn, ...(activeTab === "advisor" ? S.tabBtnActive : {}) }}
          onClick={() => setActiveTab("advisor")}
        >
          <Sparkles size={14} style={{ marginRight: 6 }} />
          Analizza & Migliora
        </button>
      </div>

      {activeTab === "advisor" ? (
        <div style={S.card}>
          <div style={S.advisorIntro}>
            La AI analizza questo run e propone consigli operativi per migliorare
            il Decision Agent in scenari simili. I consigli che salvi vengono
            iniettati automaticamente nei run futuri della stessa categoria.
          </div>
          <SimAdvisorChat runId={runId} />
        </div>
      ) : (
        <ResultView run={run} runId={runId} nav={nav} />
      )}
    </div>
  );
}

function ResultView({ run, runId, nav }) {
  const full = run.full_data || {};
  const stats = full.chart_stats || {};
  const slTp = full.sl_tp_analysis || {};
  const pnl10k = full.pnl_on_10k || {};
  const decisionFull = full.decision_full || {};
  const stepsData = full.steps_data || [];

  const outcomeColor = run.outcome === "green" ? "#10b981"
                     : run.outcome === "yellow" ? "#fbbf24" : "#ef4444";
  const outcomeLabel = run.outcome === "green" ? "✅ Tesi confermata"
                     : run.outcome === "yellow" ? "⚠️ Esito misto"
                     : "❌ Tesi smentita";

  const isBuy = run.action_chosen === "BUY";
  const isHold = run.action_chosen === "HOLD";
  const actionColor = isHold ? "#94a3b8" : isBuy ? "#10b981" : "#ef4444";

  return (
    <>
      {/* META INFO esteso (8 box) */}
      <div style={S.metaGrid}>
        <Meta label="Categoria" value={run.category} mono />
        <Meta label="Tipo" value={run.scenario_type === "multi" ? `${run.steps}-step` : "Single-step"} />
        <Meta label="Azione" value={
          <span style={{ color: actionColor, fontWeight: 700 }}>{run.action_chosen || "—"}</span>
        } />
        <Meta label="Asset scelto" value={<span style={{ fontFamily: "monospace" }}>{run.asset_chosen || "—"}</span>} />
        <Meta label="Conviction" value={run.conviction || "—"} />
        <Meta label="Orizzonte" value={run.horizon || "—"} />
        <Meta label="Strategia exit" value={
          <span style={{ fontSize: 12, color: "#cbd5e1" }}>
            {decisionFull.exit_strategy || "discretionary"}
          </span>
        } />
        <Meta label="Periodo" value={
          <span style={{ fontSize: 11, fontFamily: "monospace", color: "#cbd5e1" }}>
            {run.historical_period?.slice(0, 25) || "—"}
          </span>
        } />
      </div>

      {/* OUTCOME + PERFORMANCE GRID */}
      <div style={{ ...S.card, borderLeft: `4px solid ${outcomeColor}` }}>
        <div style={S.outcomeLabel}>{outcomeLabel}</div>

        {/* Riga 1: Performance asset (1S, 1M, 3M) */}
        <div style={S.perfSectionTitle}>Performance asset selezionato</div>
        <div style={S.perfGrid3}>
          <PerfCol label="1 Settimana" v={run.perf_1w} />
          <PerfCol label="1 Mese" v={run.perf_1m} big />
          <PerfCol label="3 Mesi" v={run.perf_3m} />
        </div>

        {/* Riga 2: Δ vs benchmarks (1M) */}
        <div style={S.perfSectionTitle}>Alpha vs benchmark (1 mese)</div>
        <div style={S.perfGrid3}>
          <PerfCol label="Δ vs S&P 500" v={run.delta_sp} />
          <PerfCol label="Δ vs settore" v={run.delta_sector} />
          <PerfCol label="Δ vs random" v={run.delta_monkey} />
        </div>
      </div>

      {/* PREZZI ENTRY/EXIT + P&L $$ */}
      {(full.entry_price != null) && (
        <div style={S.card}>
          <div style={S.cardTitle}>Prezzi & P&L</div>
          <div style={S.priceTimeline}>
            <PriceMilestone label="Entry T0" price={full.entry_price} />
            <PriceMilestone label="+1S" price={full.price_at_1w} entry={full.entry_price} />
            <PriceMilestone label="+1M" price={full.price_at_1m} entry={full.entry_price} />
            <PriceMilestone label="+3M" price={full.price_at_3m} entry={full.entry_price} />
          </div>
          <div style={S.divider} />
          <div style={{ fontSize: 12, color: "#94a3b8", marginBottom: 8 }}>
            Notional di riferimento: {fmtUsd(pnl10k.notional || 10000)}
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            <div style={S.pnlBox}>
              <div style={S.pnlLabel}>P&L stimato a 1M</div>
              <div style={{ ...S.pnlValue, color: colorFor(pnl10k.pnl_1m) }}>
                {pnl10k.pnl_1m != null ? `${pnl10k.pnl_1m >= 0 ? "+" : ""}${fmtUsd(pnl10k.pnl_1m)}` : "—"}
              </div>
            </div>
            <div style={S.pnlBox}>
              <div style={S.pnlLabel}>P&L stimato a 3M</div>
              <div style={{ ...S.pnlValue, color: colorFor(pnl10k.pnl_3m) }}>
                {pnl10k.pnl_3m != null ? `${pnl10k.pnl_3m >= 0 ? "+" : ""}${fmtUsd(pnl10k.pnl_3m)}` : "—"}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* STATISTICHE PERIODO */}
      {Object.keys(stats).length > 0 && (
        <div style={S.card}>
          <div style={S.cardTitle}>Statistiche del periodo</div>
          <div style={S.statsGrid}>
            <StatBox label="Max prezzo" value={fmtUsd(stats.max_price_period)} />
            <StatBox label="Min prezzo" value={fmtUsd(stats.min_price_period)} />
            <StatBox label="Max run-up" value={fmtPct((stats.max_runup_pct || 0) / 100)}
                     color={colorFor(stats.max_runup_pct)} />
            <StatBox label="Max drawdown" value={fmtPct((stats.max_drawdown_pct || 0) / 100)}
                     color={colorFor(stats.max_drawdown_pct)} />
            <StatBox label="Volatilità ann." value={`${(stats.annualized_volatility_pct || 0).toFixed(1)}%`} />
          </div>
        </div>
      )}

      {/* SL/TP ANALYSIS — nuova sezione critica */}
      {(slTp.stop_loss_target != null || slTp.take_profit_target != null) && (
        <div style={S.card}>
          <div style={S.cardTitle}>
            <Target size={14} style={{ display: "inline", marginRight: 6, color: "#a78bfa" }} />
            Analisi Stop-Loss / Take-Profit proposti
          </div>
          <div style={{ fontSize: 12, color: "#94a3b8", marginBottom: 12, lineHeight: 1.5 }}>
            Strategia dichiarata: <strong style={{ color: "#cbd5e1" }}>{slTp.exit_strategy || "discretionary"}</strong>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            {slTp.stop_loss_target != null && (
              <SLTPBox
                kind="SL" target={slTp.stop_loss_target}
                hit={slTp.stop_loss_would_hit} hitDay={slTp.stop_loss_hit_day}
              />
            )}
            {slTp.take_profit_target != null && (
              <SLTPBox
                kind="TP" target={slTp.take_profit_target}
                hit={slTp.take_profit_would_hit} hitDay={slTp.take_profit_hit_day}
              />
            )}
          </div>
        </div>
      )}

      {/* GRAFICO con SL/TP overlay */}
      <div style={S.card}>
        <div style={S.cardTitle}>Evoluzione prezzo asset (3 mesi)</div>
        <PriceChart data={full.price_chart || []} stepsData={stepsData}
                    slTarget={slTp.stop_loss_target} tpTarget={slTp.take_profit_target} />
      </div>

      {/* RAGIONAMENTO COMPLETO (3 sezioni: lettura/ragionamento/decisione) */}
      <div style={S.card}>
        <div style={S.cardTitle}>
          <Brain size={14} style={{ display: "inline", marginRight: 6, color: "#10b981" }} />
          Analisi del ragionamento dell'agente
        </div>

        {full.reading_text && (
          <ReasoningBlock label="📖 Lettura del contesto" body={full.reading_text} />
        )}

        <ReasoningBlock label="🧠 Tesi originale (ragionamento causale)"
                        body={full.reasoning_full || run.original_thesis} />

        {full.risk_identified && (
          <div style={S.riskBox}>
            <div style={S.label}>⚠️ Rischio identificato dall'agente</div>
            <div style={{ fontSize: 13, color: "#fed7aa", marginTop: 4 }}>{full.risk_identified}</div>
          </div>
        )}

        <div style={S.divider} />

        <div style={{ marginTop: 8 }}>
          <div style={S.label}>🕰️ Cosa è successo realmente nel periodo</div>
          <div style={S.realityBox}>{run.what_happened || "—"}</div>
        </div>

        <div style={{ marginTop: 12 }}>
          <div style={S.label}>⚖️ Valutazione tesi</div>
          <div style={{ ...S.thesisEval, color: outcomeColor }}>
            {run.thesis_evaluation || outcomeLabel}
          </div>
        </div>
      </div>

      {/* STEP BREAKDOWN (anche per single-step ora, mostra dettaglio decisione) */}
      {stepsData.length > 0 && (
        <div style={S.card}>
          <div style={S.cardTitle}>
            Decisioni step-by-step ({stepsData.length} step)
          </div>
          <table style={S.stepsTable}>
            <thead>
              <tr>
                <th>Step</th><th>Azione</th><th>Asset</th><th>Conv.</th>
                <th>Orizzonte</th><th>Prezzo</th><th>SL</th><th>TP</th>
              </tr>
            </thead>
            <tbody>
              {stepsData.map((s, i) => (
                <tr key={i}>
                  <td style={{ fontFamily: "monospace" }}>T{i}</td>
                  <td style={{
                    color: s.action === "BUY" ? "#10b981"
                         : s.action === "SELL" ? "#ef4444" : "#94a3b8",
                    fontWeight: 600,
                  }}>{s.action || "—"}</td>
                  <td style={{ fontFamily: "monospace" }}>{s.asset || "—"}</td>
                  <td>{s.conviction || "—"}</td>
                  <td style={{ fontSize: 11, color: "#94a3b8" }}>{s.horizon || "—"}</td>
                  <td>{s.price != null ? fmtUsd(s.price) : "—"}</td>
                  <td style={{ fontSize: 11, color: "#fca5a5" }}>
                    {s.stop_loss_target ? fmtUsd(s.stop_loss_target) : "—"}
                  </td>
                  <td style={{ fontSize: 11, color: "#86efac" }}>
                    {s.take_profit_target ? fmtUsd(s.take_profit_target) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* PERIODO STORICO (reveal) */}
      {run.historical_period && (
        <div style={S.revealBox}>
          🕰️ Periodo storico: <strong>{run.historical_period}</strong>
          {run._source && (
            <span style={{ fontSize: 11, marginLeft: 12, color: "#64748b" }}>
              · sorgente dati: {run._source}
            </span>
          )}
        </div>
      )}
    </>
  );
}

// ─── Components ─────────────────────────────────────────────────────────

function Meta({ label, value, mono }) {
  return (
    <div style={S.meta}>
      <div style={S.metaLabel}>{label}</div>
      <div style={{ ...S.metaValue, fontFamily: mono ? "monospace" : "inherit" }}>
        {value}
      </div>
    </div>
  );
}

function PerfCol({ label, v, big }) {
  const pct = v != null ? (v * 100).toFixed(2) + "%" : "—";
  const color = colorFor(v);
  const Icon = v != null && v >= 0 ? TrendingUp : TrendingDown;
  return (
    <div style={S.perfCol}>
      <div style={S.perfLabel}>{label}</div>
      <div style={{
        fontSize: big ? 24 : 18, fontWeight: 700, color,
        marginTop: 4, display: "flex", alignItems: "center", gap: 6,
      }}>
        {v != null && <Icon size={big ? 18 : 14} />}
        {v != null && v >= 0 ? "+" : ""}{pct}
      </div>
    </div>
  );
}

function PriceMilestone({ label, price, entry }) {
  const delta = entry && price ? (price - entry) / entry : null;
  return (
    <div style={S.milestoneBox}>
      <div style={S.metaLabel}>{label}</div>
      <div style={{ fontSize: 16, fontWeight: 600, color: "#e2e8f0" }}>
        {price != null ? fmtUsd(price) : "—"}
      </div>
      {delta != null && (
        <div style={{ fontSize: 11, color: colorFor(delta), marginTop: 2 }}>
          {fmtPct(delta)}
        </div>
      )}
    </div>
  );
}

function StatBox({ label, value, color }) {
  return (
    <div style={S.statBox}>
      <div style={S.metaLabel}>{label}</div>
      <div style={{ fontSize: 15, fontWeight: 600, color: color || "#e2e8f0", marginTop: 2 }}>
        {value}
      </div>
    </div>
  );
}

function SLTPBox({ kind, target, hit, hitDay }) {
  const isSL = kind === "SL";
  const tone = isSL ? "#ef4444" : "#10b981";
  const hitColor = hit ? "#fbbf24" : "#10b981";
  return (
    <div style={{ ...S.slTpBox, borderLeftColor: tone }}>
      <div style={{ fontSize: 11, color: "#94a3b8", textTransform: "uppercase", letterSpacing: "0.06em" }}>
        {isSL ? "Stop-Loss target" : "Take-Profit target"}
      </div>
      <div style={{ fontSize: 18, fontWeight: 700, color: tone, marginTop: 4 }}>
        {fmtUsd(target)}
      </div>
      {hit != null && (
        <div style={{ fontSize: 12, color: hitColor, marginTop: 6 }}>
          {hit
            ? `⚠️ Toccato al giorno T+${hitDay} → posizione sarebbe stata chiusa`
            : `✅ Mai toccato nei 90 giorni`}
        </div>
      )}
    </div>
  );
}

function ReasoningBlock({ label, body }) {
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={S.label}>{label}</div>
      <div style={S.thesisBox}>{body || "—"}</div>
    </div>
  );
}

function PriceChart({ data, stepsData, slTarget, tpTarget }) {
  if (!data || data.length === 0) {
    return <div style={{ color: "#64748b", padding: 20, textAlign: "center" }}>
      Grafico non disponibile</div>;
  }
  const w = 800, h = 260, pad = 50;
  const ys = data.map(d => d.price);
  let min = Math.min(...ys), max = Math.max(...ys);
  // Includi SL/TP nei limiti del grafico
  if (slTarget && slTarget < min) min = slTarget;
  if (tpTarget && tpTarget > max) max = tpTarget;
  const range = max - min || 1;
  const padY = range * 0.05;
  min -= padY; max += padY;
  const finalRange = max - min;

  const yToPx = (price) => h - pad - ((price - min) / finalRange) * (h - 2 * pad);
  const xToPx = (i) => pad + (i / (data.length - 1)) * (w - 2 * pad);

  const points = data.map((d, i) => `${xToPx(i)},${yToPx(d.price)}`).join(" ");

  return (
    <svg width="100%" height={h} viewBox={`0 0 ${w} ${h}`} style={{ display: "block" }}>
      {/* Linee SL/TP orizzontali */}
      {slTarget && (
        <g>
          <line x1={pad} y1={yToPx(slTarget)} x2={w - pad} y2={yToPx(slTarget)}
                stroke="#ef4444" strokeWidth={1} strokeDasharray="6 4" opacity={0.7} />
          <text x={w - pad + 4} y={yToPx(slTarget) + 4} fill="#ef4444" fontSize={10}>
            SL ${slTarget}
          </text>
        </g>
      )}
      {tpTarget && (
        <g>
          <line x1={pad} y1={yToPx(tpTarget)} x2={w - pad} y2={yToPx(tpTarget)}
                stroke="#10b981" strokeWidth={1} strokeDasharray="6 4" opacity={0.7} />
          <text x={w - pad + 4} y={yToPx(tpTarget) + 4} fill="#10b981" fontSize={10}>
            TP ${tpTarget}
          </text>
        </g>
      )}

      {/* Asse Y */}
      <line x1={pad} y1={pad} x2={pad} y2={h - pad} stroke="#1f2937" strokeWidth={1} />
      <line x1={pad} y1={h - pad} x2={w - pad} y2={h - pad} stroke="#1f2937" strokeWidth={1} />

      {/* Tick Y (3) */}
      {[0, 0.5, 1].map((t, i) => {
        const v = min + t * finalRange;
        const y = yToPx(v);
        return (
          <g key={i}>
            <text x={pad - 6} y={y + 4} fill="#64748b" fontSize={10} textAnchor="end">
              ${v.toFixed(0)}
            </text>
            <line x1={pad} y1={y} x2={w - pad} y2={y} stroke="#0f172a" strokeWidth={1} />
          </g>
        );
      })}

      {/* Linea prezzo */}
      <polyline points={points} fill="none" stroke="#a78bfa" strokeWidth={2} />

      {/* Step markers */}
      {(stepsData || []).map((s, i) => {
        if (!s.asset) return null;
        const dataIdx = Math.floor((i / Math.max(1, stepsData.length - 1)) * (data.length - 1));
        const d = data[dataIdx];
        if (!d) return null;
        const x = xToPx(dataIdx);
        const y = yToPx(d.price);
        return (
          <g key={i}>
            <circle cx={x} cy={y} r={5} fill="#fbbf24" stroke="#0a0e1a" strokeWidth={2} />
            <text x={x} y={y - 10} fill="#fbbf24" fontSize={11} textAnchor="middle">T{i}</text>
          </g>
        );
      })}
    </svg>
  );
}

const S = {
  h1: { fontSize: "1.5rem", margin: 0 },
  headerRow: {
    display: "flex", justifyContent: "space-between", alignItems: "center",
    marginBottom: 16,
  },
  backBtn: {
    background: "transparent", color: "#94a3b8", border: "1px solid #374151",
    padding: "6px 12px", borderRadius: 6, cursor: "pointer",
    display: "inline-flex", alignItems: "center", gap: 6, fontSize: 13,
    fontFamily: "inherit",
  },
  btnPrimary: {
    background: "#a78bfa", color: "#0a0e1a", border: 0, padding: "8px 14px",
    borderRadius: 6, fontWeight: 600, cursor: "pointer",
    display: "inline-flex", alignItems: "center", gap: 6, fontSize: 13,
    fontFamily: "inherit",
  },

  // Meta info grid
  metaGrid: {
    display: "grid",
    gridTemplateColumns: "repeat(4, 1fr)",
    gap: 10, marginBottom: 16,
  },
  meta: {
    background: "#111827", border: "1px solid #1f2937",
    padding: "10px 14px", borderRadius: 6,
  },
  metaLabel: {
    fontSize: 10, fontWeight: 600, textTransform: "uppercase",
    letterSpacing: "0.06em", color: "#64748b", marginBottom: 4,
  },
  metaValue: { fontSize: 14, color: "#e2e8f0", fontWeight: 600 },

  // Cards
  card: {
    background: "#111827", border: "1px solid #1f2937", padding: 18,
    borderRadius: 10, marginBottom: 16,
  },
  cardTitle: {
    fontSize: 14, fontWeight: 600, color: "#cbd5e1",
    marginBottom: 14, display: "flex", alignItems: "center",
  },
  outcomeLabel: { fontSize: 18, fontWeight: 700, marginBottom: 16 },

  // Performance grids (3 col)
  perfSectionTitle: {
    fontSize: 11, fontWeight: 600, color: "#64748b",
    textTransform: "uppercase", letterSpacing: "0.06em",
    marginTop: 12, marginBottom: 8,
  },
  perfGrid3: { display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 10 },
  perfCol: { background: "#0f172a", padding: "12px 14px", borderRadius: 8 },
  perfLabel: { fontSize: 11, color: "#94a3b8", letterSpacing: "0.04em" },

  // Prezzi timeline
  priceTimeline: {
    display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 10,
    marginBottom: 12,
  },
  milestoneBox: { background: "#0f172a", padding: 12, borderRadius: 6 },
  divider: { height: 1, background: "#1f2937", margin: "12px 0" },
  pnlBox: { background: "#0a1018", padding: 12, borderRadius: 6,
            border: "1px solid #1a2030" },
  pnlLabel: { fontSize: 11, color: "#94a3b8", textTransform: "uppercase",
              letterSpacing: "0.06em" },
  pnlValue: { fontSize: 22, fontWeight: 700, marginTop: 4 },

  // Stats grid
  statsGrid: {
    display: "grid", gridTemplateColumns: "repeat(5, 1fr)", gap: 8,
  },
  statBox: { background: "#0f172a", padding: 10, borderRadius: 6 },

  // SL/TP analysis
  slTpBox: {
    background: "#0f172a", padding: 12, borderRadius: 6,
    borderLeft: "3px solid",
  },

  // Reasoning blocks
  label: {
    fontSize: 11, fontWeight: 600, color: "#a78bfa",
    textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 6,
  },
  thesisBox: {
    background: "#0a1018", padding: 12, borderRadius: 6,
    fontSize: 13, lineHeight: 1.6, color: "#cbd5e1",
    whiteSpace: "pre-wrap", border: "1px solid #1a2030",
  },
  realityBox: {
    background: "#1e293b", padding: 12, borderRadius: 6,
    fontSize: 13, lineHeight: 1.6, color: "#cbd5e1", whiteSpace: "pre-wrap",
  },
  riskBox: {
    background: "#3f1d0f", padding: 12, borderRadius: 6,
    border: "1px solid #7c2d12", marginTop: 8, marginBottom: 8,
  },
  thesisEval: { fontSize: 14, fontWeight: 600 },

  // Steps table
  stepsTable: { width: "100%", borderCollapse: "collapse", fontSize: 13 },

  // Reveal
  revealBox: {
    background: "#0f172a", border: "1px dashed #475569",
    padding: 14, borderRadius: 6, color: "#94a3b8", fontSize: 13,
  },

  empty: { padding: 40, textAlign: "center", color: "#94a3b8" },

  // Tab switcher
  tabBar: {
    display: "flex", gap: 4, marginBottom: 16,
    borderBottom: "1px solid #1f2937", paddingBottom: 0,
  },
  tabBtn: {
    background: "transparent", color: "#94a3b8", border: 0,
    padding: "10px 18px", cursor: "pointer", fontSize: 13,
    fontWeight: 600, fontFamily: "inherit",
    display: "inline-flex", alignItems: "center",
    borderBottom: "2px solid transparent",
    transition: "all 0.15s ease",
    marginBottom: -1, // sovrappone il border del tabBar
  },
  tabBtnActive: {
    color: "#a78bfa", borderBottom: "2px solid #a78bfa",
  },

  // Advisor intro (descrittivo, sopra la chat)
  advisorIntro: {
    fontSize: 12, color: "#94a3b8", lineHeight: 1.6,
    padding: "10px 12px", background: "#0a1018", borderRadius: 6,
    border: "1px solid #1f2937", marginBottom: 14,
  },
};
