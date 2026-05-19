import { useEffect, useState } from "react";
import {
  ResponsiveContainer, BarChart, Bar, XAxis, YAxis, CartesianGrid,
  Tooltip, LineChart, Line, ScatterChart, Scatter, ZAxis, Cell,
} from "recharts";

const API = window.location.origin;

/**
 * ResearchPanel — strumenti di ricerca (review forward-testing di
 * Michael): distribuzione P&L, P&L cumulato, outlier + metriche senza
 * outlier, e il test chiave: il segnale (confidence) predice l'esito in
 * modo statisticamente significativo? (test di permutazione).
 *
 * Si apre da un tastino in basso a destra su /analytics. Pannello
 * separato per non appesantire la pagina principale.
 */
export default function ResearchPanel({ open, onClose }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open) return;
    let alive = true;
    setLoading(true); setErr(null);
    (async () => {
      try {
        const r = await fetch(`${API}/api/live/research-stats`);
        const t = await r.text();
        if (!alive) return;
        if (!r.ok || !t.trim()) { setErr(`HTTP ${r.status}`); }
        else {
          try { setData(JSON.parse(t)); }
          catch { setErr("Risposta non valida"); }
        }
      } catch (e) { if (alive) setErr(String(e.message || e)); }
      finally { if (alive) setLoading(false); }
    })();
    return () => { alive = false; };
  }, [open]);

  if (!open) return null;

  const sig = data?.significance;
  const sigColor = sig?.testable
    ? (sig.significant_5pct ? "#10b981" : "#ef4444") : "#64748b";

  return (
    <div onClick={onClose} style={{
      position: "fixed", inset: 0, zIndex: 1000,
      background: "rgba(2,6,15,0.78)", display: "flex",
      justifyContent: "center", alignItems: "flex-start",
      overflowY: "auto", padding: "32px 16px",
    }}>
      <div onClick={(e) => e.stopPropagation()} style={{
        maxWidth: 920, width: "100%", background: "#0b1120",
        border: "1px solid #1e293b", borderRadius: 14,
        padding: "22px 24px", color: "#e2e8f0",
      }}>
        <div style={{ display: "flex", justifyContent: "space-between",
                      alignItems: "center", marginBottom: 4 }}>
          <h2 style={{ fontSize: "1.2rem", fontWeight: 700, margin: 0 }}>
            Ricerca avanzata
          </h2>
          <button onClick={onClose} style={{
            background: "transparent", color: "#94a3b8",
            border: "1px solid #334155", borderRadius: 8,
            padding: "5px 12px", cursor: "pointer", fontSize: "0.8rem",
          }}>Chiudi ✕</button>
        </div>
        <p style={{ fontSize: "0.78rem", color: "#64748b",
                    margin: "0 0 16px" }}>
          Suggerimenti di Michael: ispeziona gli outlier, guarda la
          strategia senza, e verifica se il segnale predice l'esito in
          modo statisticamente significativo.
        </p>

        {loading && <div style={{ color: "#94a3b8", padding: 24 }}>
          Calcolo in corso…</div>}
        {err && <div style={{ color: "#ef4444", padding: 16,
          background: "#1f1316", borderRadius: 8 }}>Errore: {err}</div>}
        {data && data.available === false && (
          <div style={{ color: "#94a3b8", padding: 16 }}>{data.reason}</div>
        )}

        {data && data.available && (
          <>
            {/* SIGNIFICATIVITA' — il punto di Michael */}
            <div style={{ background: "#0d1424",
              border: `1px solid ${sigColor}55`,
              borderLeft: `4px solid ${sigColor}`, borderRadius: 10,
              padding: "14px 16px", marginBottom: 18 }}>
              <div style={{ fontSize: "0.95rem", fontWeight: 700,
                            marginBottom: 6 }}>
                Il segnale predice l'esito? (test di significatività)
              </div>
              <div style={{ fontSize: "0.85rem", color: "#cbd5e1",
                            lineHeight: 1.6 }}>
                {data.interpretation}
              </div>
              {sig?.testable && (
                <div style={{ fontSize: "0.74rem", color: "#64748b",
                              marginTop: 8 }}>
                  r={sig.pearson_r} · p={sig.p_value} · n={sig.samples} ·
                  {" "}{sig.method}
                </div>
              )}
            </div>

            {/* METRICHE con/senza outlier */}
            <div style={{ display: "grid",
              gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 18 }}>
              {[["Tutti i trade", data.metrics.all],
                [`Senza outlier (±${data.metrics.outliers_removed_per_side})`,
                 data.metrics.ex_outliers]].map(([lab, m]) => (
                <div key={lab} style={{ background: "#0a1018",
                  borderRadius: 8, padding: "12px 14px" }}>
                  <div style={{ fontSize: "0.78rem", color: "#3b82f6",
                    fontWeight: 700, marginBottom: 6 }}>{lab}</div>
                  <Row k="Trade" v={m.n} />
                  <Row k="Totale $" v={m.total_usd} strong />
                  <Row k="Profit factor" v={m.profit_factor} />
                  <Row k="Win rate" v={m.win_rate_pct != null
                    ? `${m.win_rate_pct}%` : "—"} />
                  <Row k="Media %" v={m.avg_pct != null
                    ? `${m.avg_pct}%` : "—"} />
                </div>
              ))}
            </div>

            <Chart title="Distribuzione del P&L (%)">
              <ResponsiveContainer width="100%" height={200}>
                <BarChart data={data.pnl_distribution}>
                  <CartesianGrid stroke="#1e293b" strokeDasharray="3 3" />
                  <XAxis dataKey="range" tick={{ fill: "#64748b",
                    fontSize: 10 }} />
                  <YAxis tick={{ fill: "#64748b", fontSize: 10 }} />
                  <Tooltip contentStyle={{ background: "#0f172a",
                    border: "1px solid #1e293b", borderRadius: 8,
                    fontSize: 12 }} />
                  <Bar dataKey="count">
                    {data.pnl_distribution.map((d, i) => (
                      <Cell key={i} fill={d.lo < 0 ? "#ef4444"
                        : "#10b981"} fillOpacity={0.8} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </Chart>

            <Chart title="P&L cumulato ($)">
              <ResponsiveContainer width="100%" height={200}>
                <LineChart data={data.cumulative_pnl}>
                  <CartesianGrid stroke="#1e293b" strokeDasharray="3 3" />
                  <XAxis dataKey="date" tick={{ fill: "#64748b",
                    fontSize: 10 }} />
                  <YAxis tick={{ fill: "#64748b", fontSize: 10 }} />
                  <Tooltip contentStyle={{ background: "#0f172a",
                    border: "1px solid #1e293b", borderRadius: 8,
                    fontSize: 12 }} />
                  <Line type="monotone" dataKey="cum_pnl_usd"
                    stroke="#3b82f6" dot={false} strokeWidth={2} />
                </LineChart>
              </ResponsiveContainer>
            </Chart>

            <Chart title="Confidence vs rendimento (solo decisioni AI)">
              <ResponsiveContainer width="100%" height={220}>
                <ScatterChart>
                  <CartesianGrid stroke="#1e293b" strokeDasharray="3 3" />
                  <XAxis type="number" dataKey="confidence"
                    name="Confidence" tick={{ fill: "#64748b",
                    fontSize: 10 }} />
                  <YAxis type="number" dataKey="return_pct"
                    name="Return %" tick={{ fill: "#64748b",
                    fontSize: 10 }} />
                  <ZAxis range={[40, 40]} />
                  <Tooltip contentStyle={{ background: "#0f172a",
                    border: "1px solid #1e293b", borderRadius: 8,
                    fontSize: 12 }} />
                  <Scatter data={data.signal_vs_return}>
                    {data.signal_vs_return.map((d, i) => (
                      <Cell key={i} fill={d.return_pct >= 0
                        ? "#10b981" : "#ef4444"} fillOpacity={0.6} />
                    ))}
                  </Scatter>
                </ScatterChart>
              </ResponsiveContainer>
            </Chart>

            {/* OUTLIER */}
            <div style={{ display: "grid",
              gridTemplateColumns: "1fr 1fr", gap: 12, marginTop: 6 }}>
              <Outliers title="Maggiori guadagni" rows={data.outliers.best}
                good />
              <Outliers title="Maggiori perdite" rows={data.outliers.worst} />
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function Row({ k, v, strong }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between",
      padding: "2px 0", fontSize: "0.78rem" }}>
      <span style={{ color: "#94a3b8" }}>{k}</span>
      <span style={{ color: strong ? "#f1f5f9" : "#cbd5e1",
        fontWeight: strong ? 700 : 500 }}>{v}</span>
    </div>
  );
}

function Chart({ title, children }) {
  return (
    <div style={{ background: "#0a1018", border: "1px solid #1e293b",
      borderRadius: 10, padding: "12px 14px", marginBottom: 14 }}>
      <div style={{ fontSize: "0.82rem", fontWeight: 600,
        color: "#cbd5e1", marginBottom: 8 }}>{title}</div>
      {children}
    </div>
  );
}

function Outliers({ title, rows, good }) {
  return (
    <div>
      <div style={{ fontSize: "0.8rem", fontWeight: 700,
        color: good ? "#10b981" : "#ef4444", marginBottom: 6 }}>
        {title}
      </div>
      {(!rows || rows.length === 0) && (
        <div style={{ fontSize: "0.75rem", color: "#64748b" }}>—</div>
      )}
      {rows && rows.map((t, i) => (
        <div key={i} style={{ background: "#0a1018", borderRadius: 6,
          padding: "8px 10px", marginBottom: 6 }}>
          <div style={{ display: "flex", justifyContent: "space-between",
            fontSize: "0.8rem" }}>
            <span style={{ fontWeight: 700 }}>
              {t.ticker || "—"}
              {t.is_manual && (
                <span style={{ color: "#f59e0b", fontSize: "0.68rem",
                  marginLeft: 6 }}>non-AI</span>
              )}
            </span>
            <span style={{ color: good ? "#10b981" : "#ef4444",
              fontWeight: 700 }}>
              {t.pnl_pct >= 0 ? "+" : ""}{t.pnl_pct}% · ${t.pnl_usd}
            </span>
          </div>
          <div style={{ fontSize: "0.7rem", color: "#64748b",
            marginTop: 2 }}>
            conf {t.confidence ?? "—"} · {t.buy_date || "?"} →{" "}
            {t.sell_date || "?"}
          </div>
        </div>
      ))}
    </div>
  );
}
