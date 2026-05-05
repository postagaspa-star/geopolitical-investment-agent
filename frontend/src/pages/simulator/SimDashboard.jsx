import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Play, Zap, RefreshCw } from "lucide-react";

const API = window.location.origin;

/**
 * Home Simulator: KPI aggregati + ultimi run + toggle modalità automatica.
 */
export default function SimDashboard() {
  const nav = useNavigate();
  const [kpi, setKpi] = useState(null);
  const [recent, setRecent] = useState([]);
  const [autoMode, setAutoMode] = useState({ enabled: false, daily_cap: 5, runs_today: 0 });
  const [loading, setLoading] = useState(true);

  const reload = async () => {
    setLoading(true);
    try {
      const [k, r, a] = await Promise.all([
        fetch(`${API}/api/simulator/kpi`).then(r => r.ok ? r.json() : null),
        fetch(`${API}/api/simulator/runs?limit=10`).then(r => r.ok ? r.json() : []),
        fetch(`${API}/api/simulator/auto-mode`).then(r => r.ok ? r.json() : null),
      ]);
      if (k) setKpi(k);
      if (Array.isArray(r)) setRecent(r);
      if (a) setAutoMode(a);
    } catch {}
    setLoading(false);
  };

  useEffect(() => { reload(); }, []);

  const toggleAuto = async () => {
    try {
      await fetch(`${API}/api/simulator/auto-mode`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: !autoMode.enabled, daily_cap: autoMode.daily_cap }),
      });
      reload();
    } catch {}
  };

  const setCap = async (v) => {
    const cap = parseInt(v, 10) || 1;
    try {
      await fetch(`${API}/api/simulator/auto-mode`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: autoMode.enabled, daily_cap: cap }),
      });
      setAutoMode({ ...autoMode, daily_cap: cap });
    } catch {}
  };

  return (
    <div>
      <div style={S.header}>
        <h1 style={S.h1}>GeoInvest AI Simulator</h1>
        <button style={S.refreshBtn} onClick={reload} disabled={loading}>
          <RefreshCw size={14} style={loading ? { animation: "spin 1s linear infinite" } : {}} />
          {loading ? "..." : "Aggiorna"}
        </button>
      </div>
      <p style={S.sub}>
        Stato complessivo sui run completati. Clicca su uno scenario per vedere il dettaglio.
      </p>

      {/* KPI ROW */}
      <div style={S.kpiRow}>
        <KPI label="Score composito" value={kpi?.score?.toFixed(1) ?? "—"}
             sub={kpi?.score_label ?? "Nessun dato"} tone="#a78bfa" />
        <KPI label="Win rate globale" value={kpi ? `${(kpi.win_rate * 100).toFixed(0)}%` : "—"}
             sub={`${kpi?.wins ?? 0}/${kpi?.total ?? 0} run`} tone="#10b981" />
        <KPI label="Run completati" value={kpi?.total ?? 0}
             sub={`${kpi?.single_step ?? 0} single · ${kpi?.multi_step ?? 0} multi`} tone="#06b6d4" />
        <KPI label="Δ vs S&P (medio)" value={kpi ? `${(kpi.avg_delta_sp * 100).toFixed(2)}%` : "—"}
             sub="Performance media a 1M" tone="#f472b6" />
      </div>

      {/* Win rate per categoria */}
      <div style={S.card}>
        <div style={S.cardTitle}>Win rate per categoria</div>
        <CategoryBars data={kpi?.win_by_category} />
      </div>

      {/* Pulsanti azione */}
      <div style={S.actionsRow}>
        <button style={S.btnPrimary} onClick={() => nav("/simulator/runner")}>
          <Play size={16} /> Avvia Scenario Manuale
        </button>

        <div style={S.autoBox}>
          <label style={S.autoLabel}>
            <input type="checkbox" checked={autoMode.enabled} onChange={toggleAuto}
                   style={{ marginRight: 8 }} />
            Modalità Automatica
          </label>
          <label style={S.autoCap}>
            Cap giornaliero:
            <input type="number" min={1} max={50} value={autoMode.daily_cap}
                   onChange={(e) => setCap(e.target.value)} style={S.capInput} />
          </label>
          <span style={S.autoStat}>
            <Zap size={12} /> Oggi: {autoMode.runs_today}/{autoMode.daily_cap}
          </span>
        </div>
      </div>

      {/* Ultimi run */}
      <div style={S.card}>
        <div style={S.cardTitle}>Ultimi 10 run</div>
        {recent.length === 0 ? (
          <div style={S.empty}>Nessuno scenario completato. Avvia il primo scenario manuale.</div>
        ) : (
          <table style={S.table}>
            <thead>
              <tr>
                <th>Data</th><th>Categoria</th><th>Tipo</th><th>Asset</th>
                <th>Perf 1M</th><th>Δ S&P</th><th>Esito</th>
              </tr>
            </thead>
            <tbody>
              {recent.map(r => (
                <tr key={r.id} onClick={() => nav(`/simulator/result/${r.id}`)} style={S.row}>
                  <td>{new Date(r.completed_at).toLocaleDateString()}</td>
                  <td>{r.category}</td>
                  <td>{r.scenario_type === "multi" ? `${r.steps}-step` : "single"}</td>
                  <td>{r.asset_chosen || "—"}</td>
                  <td style={{ color: r.perf_1m >= 0 ? "#10b981" : "#ef4444" }}>
                    {r.perf_1m != null ? `${(r.perf_1m * 100).toFixed(2)}%` : "—"}
                  </td>
                  <td style={{ color: r.delta_sp >= 0 ? "#10b981" : "#ef4444" }}>
                    {r.delta_sp != null ? `${(r.delta_sp * 100).toFixed(2)}%` : "—"}
                  </td>
                  <td>{ <OutcomeDot outcome={r.outcome} /> }</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

function KPI({ label, value, sub, tone }) {
  return (
    <div style={{ ...S.kpi, borderLeftColor: tone }}>
      <div style={S.kpiLabel}>{label}</div>
      <div style={S.kpiValue}>{value}</div>
      <div style={S.kpiSub}>{sub}</div>
    </div>
  );
}

function CategoryBars({ data }) {
  const cats = [
    { key: "normale", name: "Normale", tone: "#06b6d4" },
    { key: "geopolitico", name: "Geopolitico", tone: "#f472b6" },
    { key: "macro", name: "Macro", tone: "#fbbf24" },
    { key: "crash_rally", name: "Crash/Rally", tone: "#ef4444" },
  ];
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {cats.map(c => {
        const pct = data?.[c.key] ?? 0;
        const total = data?.[c.key + "_total"] ?? 0;
        return (
          <div key={c.key} style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <span style={{ width: 110, fontSize: 13, color: "#cbd5e1" }}>{c.name}</span>
            <div style={{ flex: 1, height: 18, background: "#1f2937", borderRadius: 4, overflow: "hidden" }}>
              <div style={{ width: `${pct * 100}%`, height: "100%", background: c.tone,
                            transition: "width 0.4s" }} />
            </div>
            <span style={{ width: 90, fontSize: 12, color: "#94a3b8", textAlign: "right" }}>
              {(pct * 100).toFixed(0)}% ({total})
            </span>
          </div>
        );
      })}
    </div>
  );
}

function OutcomeDot({ outcome }) {
  const colors = { green: "#10b981", yellow: "#fbbf24", red: "#ef4444" };
  return <span style={{ display: "inline-block", width: 10, height: 10, borderRadius: "50%",
                        background: colors[outcome] || "#475569" }} />;
}

const S = {
  header: { display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 },
  h1: { margin: 0, fontSize: "1.8rem", fontWeight: 600 },
  sub: { color: "#94a3b8", fontSize: 14, marginTop: 0, marginBottom: 24 },
  refreshBtn: { background: "#1e293b", color: "#cbd5e1", border: "1px solid #334155",
                padding: "6px 12px", borderRadius: 6, cursor: "pointer",
                display: "inline-flex", alignItems: "center", gap: 6, fontSize: 13 },
  kpiRow: { display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12, marginBottom: 24 },
  kpi: { background: "#111827", border: "1px solid #1f2937", borderLeft: "3px solid",
         padding: "16px", borderRadius: 8 },
  kpiLabel: { color: "#94a3b8", fontSize: 11, textTransform: "uppercase", letterSpacing: 0.5 },
  kpiValue: { fontSize: "1.6rem", fontWeight: 600, margin: "6px 0", color: "#f1f5f9" },
  kpiSub: { color: "#64748b", fontSize: 12 },
  card: { background: "#111827", border: "1px solid #1f2937", padding: 20,
          borderRadius: 8, marginBottom: 20 },
  cardTitle: { fontSize: 14, fontWeight: 600, color: "#e2e8f0", marginBottom: 14 },
  empty: { padding: 24, textAlign: "center", color: "#64748b", fontSize: 13 },
  actionsRow: { display: "flex", gap: 16, alignItems: "center", marginBottom: 24, flexWrap: "wrap" },
  btnPrimary: { background: "#a78bfa", color: "#0a0e1a", border: 0, padding: "10px 18px",
                borderRadius: 6, fontWeight: 600, cursor: "pointer",
                display: "inline-flex", alignItems: "center", gap: 8, fontSize: 14 },
  autoBox: { display: "flex", gap: 16, alignItems: "center", padding: "8px 14px",
             background: "#111827", border: "1px solid #1f2937", borderRadius: 6 },
  autoLabel: { color: "#cbd5e1", fontSize: 13, cursor: "pointer", display: "flex", alignItems: "center" },
  autoCap: { color: "#94a3b8", fontSize: 12, display: "flex", alignItems: "center", gap: 6 },
  capInput: { width: 50, padding: "4px 6px", background: "#0a0e1a", color: "#f1f5f9",
              border: "1px solid #334155", borderRadius: 4, fontSize: 12 },
  autoStat: { color: "#a78bfa", fontSize: 12, display: "inline-flex", alignItems: "center", gap: 4 },
  table: { width: "100%", borderCollapse: "collapse", fontSize: 13 },
  row: { cursor: "pointer", borderBottom: "1px solid #1f2937" },
};
