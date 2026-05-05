import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Filter, RefreshCw } from "lucide-react";

const API = window.location.origin;

/**
 * Storico & Analytics: tabella completa + grafici aggregati + pattern testuali.
 */
export default function SimHistory() {
  const nav = useNavigate();
  const [runs, setRuns] = useState([]);
  const [analytics, setAnalytics] = useState(null);
  const [filterCat, setFilterCat] = useState("");
  const [filterType, setFilterType] = useState("");
  const [filterOutcome, setFilterOutcome] = useState("");
  const [patterns, setPatterns] = useState({ text: "", loading: false });

  const reload = async () => {
    const qs = new URLSearchParams();
    if (filterCat) qs.set("category", filterCat);
    if (filterType) qs.set("scenario_type", filterType);
    if (filterOutcome) qs.set("outcome", filterOutcome);
    qs.set("limit", "200");
    try {
      const [runsData, anaData] = await Promise.all([
        fetch(`${API}/api/simulator/runs?${qs.toString()}`).then(r => r.ok ? r.json() : []),
        fetch(`${API}/api/simulator/analytics`).then(r => r.ok ? r.json() : null),
      ]);
      setRuns(Array.isArray(runsData) ? runsData : []);
      setAnalytics(anaData);
    } catch {}
  };

  useEffect(() => { reload(); }, [filterCat, filterType, filterOutcome]);

  const generatePatterns = async () => {
    setPatterns({ text: "Generazione in corso...", loading: true });
    try {
      const res = await fetch(`${API}/api/simulator/patterns`, { method: "POST" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setPatterns({ text: data.analysis || "Nessun pattern identificato", loading: false });
    } catch (e) {
      setPatterns({ text: `Errore: ${e.message}`, loading: false });
    }
  };

  return (
    <div>
      <div style={S.headerRow}>
        <h1 style={S.h1}>Storico & Analytics</h1>
        <button style={S.btn} onClick={reload}><RefreshCw size={14} /> Aggiorna</button>
      </div>

      {/* === ANALYTICS GRAFICI === */}
      <div style={S.gridTwo}>
        <div style={S.card}>
          <div style={S.cardTitle}>Win rate per categoria</div>
          {analytics?.win_by_category ? (
            <BarChart data={[
              { label: "Normale", value: analytics.win_by_category.normale ?? 0 },
              { label: "Geopolitico", value: analytics.win_by_category.geopolitico ?? 0 },
              { label: "Macro", value: analytics.win_by_category.macro ?? 0 },
              { label: "Crash/Rally", value: analytics.win_by_category.crash_rally ?? 0 },
            ]} />
          ) : <Empty />}
        </div>
        <div style={S.card}>
          <div style={S.cardTitle}>Performance per orizzonte</div>
          {analytics?.perf_by_horizon ? (
            <BarChart data={[
              { label: "1 settimana", value: analytics.perf_by_horizon.h_1w ?? 0 },
              { label: "1 mese", value: analytics.perf_by_horizon.h_1m ?? 0 },
              { label: "3 mesi", value: analytics.perf_by_horizon.h_3m ?? 0 },
            ]} />
          ) : <Empty />}
        </div>
        <div style={S.card}>
          <div style={S.cardTitle}>Distribuzione esiti</div>
          {analytics?.outcome_distribution ? (
            <DonutLite data={analytics.outcome_distribution} />
          ) : <Empty />}
        </div>
        <div style={S.card}>
          <div style={S.cardTitle}>Curva di apprendimento (perf media nei run)</div>
          {analytics?.learning_curve ? <LineChart data={analytics.learning_curve} /> : <Empty />}
        </div>
      </div>

      {/* === PATTERN RICORRENTI === */}
      <div style={S.card}>
        <div style={{...S.cardTitle, display: "flex", justifyContent: "space-between"}}>
          <span>Pattern ricorrenti (analisi automatica)</span>
          <button style={S.btnSmall} onClick={generatePatterns} disabled={patterns.loading}>
            {patterns.loading ? "Generando..." : "Rigenera"}
          </button>
        </div>
        <div style={S.patternsBox}>
          {patterns.text || analytics?.patterns_text ||
            "Nessun pattern generato. Clicca 'Rigenera' per produrre l'analisi."}
        </div>
      </div>

      {/* === FILTRI === */}
      <div style={S.filters}>
        <Filter size={14} style={{ color: "#94a3b8" }} />
        <select value={filterCat} onChange={e => setFilterCat(e.target.value)} style={S.select}>
          <option value="">Tutte le categorie</option>
          <option value="normale">Normale</option>
          <option value="geopolitico">Geopolitico</option>
          <option value="macro">Macro</option>
          <option value="crash_rally">Crash/Rally</option>
        </select>
        <select value={filterType} onChange={e => setFilterType(e.target.value)} style={S.select}>
          <option value="">Tutti i tipi</option>
          <option value="single">Single-step</option>
          <option value="multi">Multi-step</option>
        </select>
        <select value={filterOutcome} onChange={e => setFilterOutcome(e.target.value)} style={S.select}>
          <option value="">Tutti gli esiti</option>
          <option value="green">Verde (battuto benchmark)</option>
          <option value="yellow">Giallo (parziale)</option>
          <option value="red">Rosso (sotto benchmark)</option>
        </select>
        <span style={{ color: "#94a3b8", fontSize: 12, marginLeft: "auto" }}>
          {runs.length} run
        </span>
      </div>

      {/* === TABELLA STORICO === */}
      <div style={S.card}>
        {runs.length === 0 ? (
          <div style={{ padding: 30, textAlign: "center", color: "#64748b" }}>
            Nessun run con i filtri selezionati
          </div>
        ) : (
          <table style={S.table}>
            <thead>
              <tr>
                <th>Data</th><th>Categoria</th><th>Tipo</th>
                <th>Asset</th><th>1S</th><th>1M</th><th>3M</th>
                <th>Δ S&P</th><th>Conviction</th><th>Esito</th>
              </tr>
            </thead>
            <tbody>
              {runs.map(r => (
                <tr key={r.id} onClick={() => nav(`/simulator/result/${r.id}`)} style={S.row}>
                  <td>{new Date(r.completed_at).toLocaleDateString()}</td>
                  <td>{r.category}</td>
                  <td>{r.scenario_type === "multi" ? `${r.steps}-step` : "single"}</td>
                  <td>{r.asset_chosen || "—"}</td>
                  <td style={col(r.perf_1w)}>{pct(r.perf_1w)}</td>
                  <td style={col(r.perf_1m)}>{pct(r.perf_1m)}</td>
                  <td style={col(r.perf_3m)}>{pct(r.perf_3m)}</td>
                  <td style={col(r.delta_sp)}>{pct(r.delta_sp)}</td>
                  <td>{r.conviction || "—"}</td>
                  <td><Dot c={r.outcome} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

const pct = (v) => v != null ? `${(v * 100).toFixed(1)}%` : "—";
const col = (v) => v == null ? {} : { color: v >= 0 ? "#10b981" : "#ef4444" };

function Dot({ c }) {
  const colors = { green: "#10b981", yellow: "#fbbf24", red: "#ef4444" };
  return <span style={{ display: "inline-block", width: 10, height: 10,
                        borderRadius: "50%", background: colors[c] || "#475569" }} />;
}
function Empty() {
  return <div style={{ padding: 24, textAlign: "center", color: "#64748b", fontSize: 13 }}>
    Dati insufficienti</div>;
}
function BarChart({ data }) {
  const max = Math.max(0.01, ...data.map(d => Math.abs(d.value)));
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {data.map((d, i) => (
        <div key={i} style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ width: 100, fontSize: 12, color: "#cbd5e1" }}>{d.label}</span>
          <div style={{ flex: 1, height: 16, background: "#1f2937", borderRadius: 3 }}>
            <div style={{ width: `${(Math.abs(d.value) / max) * 100}%`, height: "100%",
                          background: d.value >= 0 ? "#10b981" : "#ef4444", borderRadius: 3 }}/>
          </div>
          <span style={{ width: 70, textAlign: "right", fontSize: 11, color: "#94a3b8" }}>
            {(d.value * 100).toFixed(1)}%
          </span>
        </div>
      ))}
    </div>
  );
}
function DonutLite({ data }) {
  const total = (data.green ?? 0) + (data.yellow ?? 0) + (data.red ?? 0);
  if (!total) return <Empty />;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {[
        { l: "Verde", k: "green", c: "#10b981" },
        { l: "Giallo", k: "yellow", c: "#fbbf24" },
        { l: "Rosso", k: "red", c: "#ef4444" },
      ].map(s => (
        <div key={s.k} style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ width: 70, fontSize: 12 }}>{s.l}</span>
          <div style={{ flex: 1, height: 14, background: "#1f2937" }}>
            <div style={{ width: `${((data[s.k] ?? 0) / total) * 100}%`,
                          height: "100%", background: s.c }} />
          </div>
          <span style={{ width: 50, textAlign: "right", fontSize: 11, color: "#94a3b8" }}>
            {data[s.k] ?? 0}
          </span>
        </div>
      ))}
    </div>
  );
}
function LineChart({ data }) {
  if (!data || data.length === 0) return <Empty />;
  const w = 500, h = 120, pad = 20;
  const ys = data.map(d => d.value);
  const min = Math.min(0, ...ys), max = Math.max(0, ...ys);
  const range = max - min || 1;
  const points = data.map((d, i) => {
    const x = pad + (i / (data.length - 1)) * (w - 2 * pad);
    const y = h - pad - ((d.value - min) / range) * (h - 2 * pad);
    return `${x},${y}`;
  }).join(" ");
  return (
    <svg width="100%" height={h} viewBox={`0 0 ${w} ${h}`}>
      <polyline points={points} fill="none" stroke="#a78bfa" strokeWidth={2} />
    </svg>
  );
}

const S = {
  h1: { fontSize: "1.5rem", margin: 0 },
  headerRow: { display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 18 },
  btn: { background: "#1e293b", color: "#cbd5e1", border: "1px solid #334155",
         padding: "6px 12px", borderRadius: 6, cursor: "pointer",
         display: "inline-flex", alignItems: "center", gap: 6, fontSize: 13 },
  btnSmall: { background: "#a78bfa", color: "#0a0e1a", border: 0, padding: "4px 10px",
              borderRadius: 4, cursor: "pointer", fontSize: 11, fontWeight: 600 },
  gridTwo: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14, marginBottom: 16 },
  card: { background: "#111827", border: "1px solid #1f2937", padding: 16,
          borderRadius: 8, marginBottom: 14 },
  cardTitle: { fontSize: 13, fontWeight: 600, color: "#cbd5e1", marginBottom: 12 },
  filters: { display: "flex", gap: 8, alignItems: "center", padding: "10px 14px",
             background: "#0f172a", borderRadius: 6, marginBottom: 14, flexWrap: "wrap" },
  select: { background: "#0a0e1a", color: "#e2e8f0", border: "1px solid #334155",
            padding: "5px 8px", borderRadius: 4, fontSize: 12 },
  table: { width: "100%", borderCollapse: "collapse", fontSize: 12 },
  row: { cursor: "pointer", borderBottom: "1px solid #1f2937" },
  patternsBox: { padding: 14, background: "#0f172a", borderRadius: 6, color: "#cbd5e1",
                 fontSize: 13, lineHeight: 1.6, whiteSpace: "pre-wrap", minHeight: 60 },
};
