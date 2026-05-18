import { useEffect, useState } from "react";

const API = window.location.origin;

/**
 * Edge Tracker — "il sistema ha un vantaggio reale, o no, o non si sa ancora?"
 *
 * Mette insieme i dati che gia' esistono (trade chiusi, alpha vs S&P,
 * calibrazione del segnale) e applica criteri DECISI PRIMA e mostrati in
 * chiaro — cosi' non sono spostabili a posteriori. Risponde alla domanda
 * vera: bravura o fortuna? Con campioni statisticamente sufficienti, non
 * sul "buon mese".
 */
const VERDICT = {
  insufficient_data: { color: "#64748b", label: "DATI INSUFFICIENTI",
    sub: "Troppo presto per giudicare" },
  edge_confirmed: { color: "#10b981", label: "EDGE DIMOSTRATO",
    sub: "Tutti i criteri soddisfatti su questo campione" },
  no_edge: { color: "#ef4444", label: "NESSUN EDGE",
    sub: "Ipotesi falsificata su questo periodo" },
  edge_emerging: { color: "#f59e0b", label: "IN COSTRUZIONE",
    sub: "Segnale parziale, serve piu' tempo" },
};

function fmtNum(v) {
  if (v === null || v === undefined) return "—";
  if (v === "∞") return "∞";
  return typeof v === "number" ? v.toFixed(2) : String(v);
}

function Criterion({ label, ok, detail }) {
  const c = ok === true ? "#10b981" : ok === false ? "#ef4444" : "#64748b";
  const icon = ok === true ? "✓" : ok === false ? "✗" : "•";
  return (
    <div style={{ display: "flex", alignItems: "flex-start", gap: 10,
                  padding: "8px 0", borderBottom: "1px solid #1a2030" }}>
      <span style={{ color: c, fontWeight: 700, fontSize: "1rem",
                     minWidth: 16 }}>{icon}</span>
      <div>
        <div style={{ fontSize: "0.86rem", color: "#e2e8f0", fontWeight: 600 }}>
          {label}
        </div>
        {detail && (
          <div style={{ fontSize: "0.76rem", color: "#94a3b8", marginTop: 2,
                        lineHeight: 1.5 }}>{detail}</div>
        )}
      </div>
    </div>
  );
}

export default function EdgeTrackerPage() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await fetch(`${API}/api/live/edge-tracker`);
        const txt = await res.text();
        if (!alive) return;
        if (!res.ok || !txt.trim()) {
          setError(`Errore caricamento (HTTP ${res.status})`);
        } else {
          try { setData(JSON.parse(txt)); }
          catch { setError("Risposta non valida dal server"); }
        }
      } catch (e) {
        if (alive) setError(String(e.message || e));
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => { alive = false; };
  }, []);

  if (loading) {
    return <div className="main-content"><div className="loading-state">
      Analisi in corso…</div></div>;
  }
  if (error || !data) {
    return <div className="main-content"><div className="error-state">
      {error || "Nessun dato"}</div></div>;
  }

  const v = VERDICT[data.verdict] || VERDICT.insufficient_data;
  const samplesPct = Math.min(100,
    Math.round((data.samples / data.min_samples) * 100));

  // Stato di ogni criterio (per i check visivi)
  const pfNum = data.profit_factor === "∞" ? 999
    : Number(data.profit_factor);
  const asymNum = data.asymmetry === "∞" ? 999 : Number(data.asymmetry);
  const enough = data.samples >= data.min_samples;
  const beatsSp = data.alpha_vs_sp_pct !== null
    && data.alpha_vs_sp_pct > 0;
  const pfOk = Number.isFinite(pfNum) && pfNum >= data.criteria.pf_promote;
  const asymOk = Number.isFinite(asymNum)
    && asymNum >= data.criteria.asymmetry_promote;

  return (
    <div className="main-content">
      <div style={{ marginBottom: "1.5rem" }}>
        <h2 style={{ fontSize: "1.4rem", fontWeight: 700, color: "#f1f5f9",
                     marginBottom: 4 }}>Edge Tracker</h2>
        <p style={{ fontSize: "0.85rem", color: "#94a3b8" }}>
          Il sistema ha un vantaggio reale sul mercato, o è fortuna?
          Verdetto onesto con criteri decisi <strong>prima</strong> (qui
          sotto, non modificabili a posteriori).
        </p>
      </div>

      {/* VERDETTO */}
      <div style={{ background: "#111827", border: `1px solid ${v.color}55`,
                    borderLeft: `4px solid ${v.color}`, borderRadius: 12,
                    padding: "20px 24px", marginBottom: 20 }}>
        <div style={{ fontSize: "1.5rem", fontWeight: 800, color: v.color,
                      letterSpacing: "0.02em" }}>{v.label}</div>
        <div style={{ fontSize: "0.82rem", color: "#94a3b8", marginTop: 2 }}>
          {v.sub}
        </div>
        <div style={{ fontSize: "0.9rem", color: "#e2e8f0", marginTop: 12,
                      lineHeight: 1.6 }}>{data.verdict_msg}</div>
      </div>

      {/* CAMPIONI — barra */}
      <div style={{ background: "#111827", border: "1px solid #1f2937",
                    borderRadius: 10, padding: "16px 18px", marginBottom: 16 }}>
        <div style={{ display: "flex", justifyContent: "space-between",
                      marginBottom: 8 }}>
          <span style={{ fontSize: "0.85rem", color: "#cbd5e1",
                         fontWeight: 600 }}>
            Campioni (trade chiusi)
          </span>
          <span style={{ fontSize: "0.85rem", color: enough ? "#10b981"
                          : "#f59e0b", fontWeight: 700 }}>
            {data.samples} / {data.min_samples} minimo
          </span>
        </div>
        <div style={{ height: 8, background: "#0a1018", borderRadius: 999,
                      overflow: "hidden" }}>
          <div style={{ width: `${samplesPct}%`, height: "100%",
                        background: enough ? "#10b981" : "#f59e0b" }} />
        </div>
        <div style={{ fontSize: "0.74rem", color: "#64748b", marginTop: 6 }}>
          {enough ? "Campione sufficiente per un giudizio statistico."
            : `Servono ancora ${data.min_samples - data.samples} trade `
              + "chiusi prima di poter giudicare con onestà."}
        </div>
      </div>

      {/* METRICHE vs CRITERI */}
      <div style={{ background: "#111827", border: "1px solid #1f2937",
                    borderRadius: 10, padding: "16px 18px", marginBottom: 16 }}>
        <div style={{ fontSize: "0.95rem", fontWeight: 700, color: "#e2e8f0",
                      marginBottom: 10 }}>I criteri, uno per uno</div>
        <Criterion label={`Batte l'S&P 500 (alpha > 0)`} ok={beatsSp}
          detail={data.alpha_vs_sp_pct !== null
            ? `Alpha: ${data.alpha_vs_sp_pct >= 0 ? "+" : ""}`
              + `${data.alpha_vs_sp_pct} punti${data.benchmark_verdict
              ? ` · ${data.benchmark_verdict}` : ""}`
            : "Confronto col mercato non disponibile."} />
        <Criterion
          label={`Profit factor ≥ ${data.criteria.pf_promote}`} ok={pfOk}
          detail={`Attuale: ${fmtNum(data.profit_factor)} `
            + `(per ogni € perso, quanti se ne recuperano)`} />
        <Criterion
          label={`Asimmetria guadagni/perdite ≥ ${data.criteria.asymmetry_promote}`}
          ok={asymOk}
          detail={`Attuale: ${fmtNum(data.asymmetry)} · `
            + `vinci medio +${data.avg_win_pct}% / perdi medio `
            + `${data.avg_loss_pct}% · win rate ${data.win_rate_pct}%`} />
        <Criterion label="La confidence dell'AI discrimina"
          ok={data.calibration_ok} detail={data.calibration_detail} />
      </div>

      {/* CRITERI DECISI PRIMA — trasparenza anti-bias */}
      <div style={{ background: "#0d1424", border: "1px dashed #334155",
                    borderRadius: 10, padding: "14px 18px" }}>
        <div style={{ fontSize: "0.8rem", fontWeight: 700, color: "#94a3b8",
                      marginBottom: 6, textTransform: "uppercase",
                      letterSpacing: "0.05em" }}>
          Regola del giudizio (fissata in anticipo)
        </div>
        <div style={{ fontSize: "0.84rem", color: "#cbd5e1",
                      lineHeight: 1.6 }}>
          {data.criteria.explain}
        </div>
        <div style={{ fontSize: "0.74rem", color: "#64748b", marginTop: 8 }}>
          Questi numeri sono scritti nel codice e mostrati qui apposta:
          servono a non "spostare l'asticella" dopo aver visto i risultati.
          È la differenza tra misurare e illudersi.
        </div>
      </div>
    </div>
  );
}
