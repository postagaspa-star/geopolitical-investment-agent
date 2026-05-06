import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, ArrowRight } from "lucide-react";

const API = window.location.origin;

/**
 * Risultato di uno scenario: verdetto + grafico + analisi del ragionamento.
 */
export default function SimResult() {
  const { runId } = useParams();
  const nav = useNavigate();
  const [run, setRun] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

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

  const outcomeColor = run.outcome === "green" ? "#10b981"
                     : run.outcome === "yellow" ? "#fbbf24" : "#ef4444";
  const outcomeLabel = run.outcome === "green" ? "✅ Tesi confermata" :
                       run.outcome === "yellow" ? "⚠️ Esito misto" : "❌ Tesi smentita";

  return (
    <div>
      <div style={S.headerRow}>
        <button style={S.backBtn} onClick={() => nav("/simulator")}>
          <ArrowLeft size={16} /> Dashboard
        </button>
        <h1 style={S.h1}>Risultato Scenario</h1>
        <button style={S.btnPrimary} onClick={() => nav("/simulator/runner")}>
          Avvia nuovo <ArrowRight size={16} />
        </button>
      </div>

      <div style={S.metaRow}>
        <Meta label="Categoria" value={run.category} />
        <Meta label="Tipo" value={run.scenario_type === "multi" ? `${run.steps}-step` : "Single-step"} />
        <Meta label="Asset scelto" value={run.asset_chosen || "—"} />
        <Meta label="Conviction" value={run.conviction || "—"} />
      </div>

      {/* SEMAFORO + PERFORMANCE */}
      <div style={{ ...S.card, borderLeft: `4px solid ${outcomeColor}` }}>
        <div style={S.outcomeLabel}>{outcomeLabel}</div>
        <div style={S.perfGrid}>
          <PerfCol label="Asset (1S)" v={run.perf_1w} />
          <PerfCol label="Asset (1M)" v={run.perf_1m} />
          <PerfCol label="Asset (3M)" v={run.perf_3m} />
          <PerfCol label="Δ vs S&P 1M" v={run.delta_sp} />
          <PerfCol label="Δ vs settore 1M" v={run.delta_sector} />
          <PerfCol label="Δ vs random 1M" v={run.delta_monkey} />
        </div>
      </div>

      {/* GRAFICO (semplice ASCII art per ora — sostituibile con Recharts in v2) */}
      <div style={S.card}>
        <div style={S.cardTitle}>Evoluzione prezzo asset (3 mesi)</div>
        <PriceChart data={run.price_chart || []} steps={run.steps_data || []} />
      </div>

      {/* ANALISI RAGIONAMENTO */}
      <div style={S.card}>
        <div style={S.cardTitle}>Analisi del ragionamento</div>
        <div style={{ marginBottom: 14 }}>
          <strong>Tesi originale:</strong>
          <p style={S.thesisBox}>{run.original_thesis || "—"}</p>
        </div>
        <div style={{ marginBottom: 14 }}>
          <strong>Cosa è successo realmente nel periodo:</strong>
          <p style={S.realityBox}>{run.what_happened || "—"}</p>
        </div>
        <div>
          <strong>Valutazione tesi:</strong>{" "}
          <span style={{ color: outcomeColor, fontWeight: 600 }}>
            {run.thesis_evaluation || outcomeLabel}
          </span>
        </div>
      </div>

      {/* MULTI-STEP TABELLA */}
      {run.scenario_type === "multi" && run.steps_data && (
        <div style={S.card}>
          <div style={S.cardTitle}>Decisioni step per step</div>
          <table style={S.stepsTable}>
            <thead>
              <tr><th>Step</th><th>Azione</th><th>Asset</th><th>Prezzo</th><th>Perf da qui</th></tr>
            </thead>
            <tbody>
              {run.steps_data.map((s, i) => (
                <tr key={i}>
                  <td>T{i}</td>
                  <td>{s.action || "—"}</td>
                  <td>{s.asset || "—"}</td>
                  <td>${s.price?.toFixed(2) ?? "—"}</td>
                  <td style={{ color: (s.perf_from_here ?? 0) >= 0 ? "#10b981" : "#ef4444" }}>
                    {s.perf_from_here != null ? `${(s.perf_from_here * 100).toFixed(2)}%` : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* PERIODO STORICO RIVELATO */}
      {run.historical_period && (
        <div style={S.revealBox}>
          🕰️ Periodo storico: <strong>{run.historical_period}</strong>
          {run.historical_note && <div style={{ fontSize: 12, marginTop: 6 }}>{run.historical_note}</div>}
        </div>
      )}
    </div>
  );
}

function Meta({ label, value }) {
  return (
    <div style={S.meta}>
      <div style={{ fontSize: 11, color: "#94a3b8", textTransform: "uppercase" }}>{label}</div>
      <div style={{ fontSize: 14, color: "#e2e8f0", fontWeight: 600 }}>{value}</div>
    </div>
  );
}

function PerfCol({ label, v }) {
  const pct = v != null ? (v * 100).toFixed(2) + "%" : "—";
  const color = v == null ? "#64748b" : v >= 0 ? "#10b981" : "#ef4444";
  return (
    <div style={S.perfCol}>
      <div style={{ fontSize: 11, color: "#94a3b8" }}>{label}</div>
      <div style={{ fontSize: 18, fontWeight: 600, color, marginTop: 4 }}>
        {v >= 0 && v != null ? "+" : ""}{pct}
      </div>
    </div>
  );
}

function PriceChart({ data, steps }) {
  if (!data || data.length === 0) {
    return <div style={{ color: "#64748b", padding: 20, textAlign: "center" }}>
      Grafico non disponibile</div>;
  }
  // Mini sparkline SVG per evitare di aggiungere recharts solo per questo
  const w = 800, h = 220, pad = 40;
  const ys = data.map(d => d.price);
  const min = Math.min(...ys), max = Math.max(...ys);
  const range = max - min || 1;
  const points = data.map((d, i) => {
    const x = pad + (i / (data.length - 1)) * (w - 2 * pad);
    const y = h - pad - ((d.price - min) / range) * (h - 2 * pad);
    return `${x},${y}`;
  }).join(" ");
  return (
    <svg width="100%" height={h} viewBox={`0 0 ${w} ${h}`}>
      <polyline points={points} fill="none" stroke="#a78bfa" strokeWidth={2} />
      {(steps || []).map((s, i) => {
        const dataIdx = Math.floor((i / Math.max(1, steps.length - 1)) * (data.length - 1));
        const x = pad + (dataIdx / (data.length - 1)) * (w - 2 * pad);
        const y = h - pad - ((data[dataIdx]?.price - min) / range) * (h - 2 * pad);
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
  headerRow: { display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 },
  backBtn: { background: "transparent", color: "#94a3b8", border: "1px solid #374151",
             padding: "6px 12px", borderRadius: 6, cursor: "pointer",
             display: "inline-flex", alignItems: "center", gap: 6, fontSize: 13 },
  btnPrimary: { background: "#a78bfa", color: "#0a0e1a", border: 0, padding: "8px 14px",
                borderRadius: 6, fontWeight: 600, cursor: "pointer",
                display: "inline-flex", alignItems: "center", gap: 6, fontSize: 13 },
  metaRow: { display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12, marginBottom: 16 },
  meta: { background: "#111827", border: "1px solid #1f2937", padding: 12, borderRadius: 6 },
  card: { background: "#111827", border: "1px solid #1f2937", padding: 18,
          borderRadius: 8, marginBottom: 16 },
  cardTitle: { fontSize: 14, fontWeight: 600, color: "#cbd5e1", marginBottom: 12 },
  outcomeLabel: { fontSize: 18, fontWeight: 600, marginBottom: 14 },
  perfGrid: { display: "grid", gridTemplateColumns: "repeat(6, 1fr)", gap: 8 },
  perfCol: { background: "#0f172a", padding: 12, borderRadius: 6 },
  thesisBox: { background: "#0f172a", padding: 12, borderRadius: 6,
               fontSize: 13, lineHeight: 1.5, color: "#cbd5e1", whiteSpace: "pre-wrap" },
  realityBox: { background: "#1e293b", padding: 12, borderRadius: 6,
                fontSize: 13, lineHeight: 1.5, color: "#cbd5e1", whiteSpace: "pre-wrap" },
  stepsTable: { width: "100%", borderCollapse: "collapse", fontSize: 13 },
  empty: { padding: 40, textAlign: "center", color: "#94a3b8" },
  revealBox: { background: "#0f172a", border: "1px dashed #475569", padding: 14,
               borderRadius: 6, color: "#94a3b8", fontSize: 13 },
};
