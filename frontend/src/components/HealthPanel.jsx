import { useEffect, useState, useCallback } from "react";

const API = window.location.origin;

/**
 * HealthPanel — stato di salute dell'app (cruscotto).
 * Legge GET /api/admin/health?fresh=true e mostra lo stato complessivo +
 * ogni singolo controllo (coerenza cassa, sync grafico, allarmi, memoria…).
 * Si apre da un bottone in alto a destra SOLO nella pagina Impostazioni.
 */

const STATUS_COLOR = {
  OK: "#10b981",
  WARNING: "#f59e0b",
  CRITICAL: "#ef4444",
  ERROR: "#6b7280",
};

const STATUS_LABEL = {
  OK: "Tutto a posto",
  WARNING: "Da tenere d'occhio",
  CRITICAL: "Problema grave",
  ERROR: "Controllo fallito",
};

// Etichette leggibili per i nomi tecnici dei check
const CHECK_LABEL = {
  cash_invariant: "Coerenza cassa",
  portfolio_sanity: "Sanità portafoglio",
  chart_sync: "Sincronia grafico ↔ valore",
  recent_alerts: "Allarmi recenti (24h)",
  cash_provenance: "Provenienza della cassa",
  memory_rss: "Memoria (RAM)",
};

export default function HealthPanel({ open, onClose }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const r = await fetch(`${API}/api/admin/health?fresh=true`);
      const t = await r.text();
      if (!r.ok || !t.trim()) {
        setErr(`HTTP ${r.status}`);
      } else {
        try { setData(JSON.parse(t)); }
        catch { setErr("Risposta non valida"); }
      }
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) load();
  }, [open, load]);

  if (!open) return null;

  const overall = data?.status;
  const overallColor = STATUS_COLOR[overall] || "#6b7280";

  return (
    <div onClick={onClose} style={{
      position: "fixed", inset: 0, zIndex: 1000,
      background: "rgba(17,24,39,0.55)", display: "flex",
      justifyContent: "center", alignItems: "flex-start",
      overflowY: "auto", padding: "40px 16px",
      fontFamily: "'Inter', -apple-system, sans-serif",
    }}>
      <div onClick={(e) => e.stopPropagation()} style={{
        maxWidth: 640, width: "100%", background: "#ffffff",
        border: "1px solid #e5e7eb", borderRadius: 14,
        padding: "22px 24px", color: "#111827",
        boxShadow: "0 20px 60px rgba(0,0,0,0.25)",
      }}>
        {/* Header */}
        <div style={{ display: "flex", justifyContent: "space-between",
                      alignItems: "center", marginBottom: 14 }}>
          <h2 style={{ fontSize: "1.2rem", fontWeight: 700, margin: 0 }}>
            Stato dell'app
          </h2>
          <div style={{ display: "flex", gap: 8 }}>
            <button onClick={load} disabled={loading} style={{
              background: "#f3f4f6", color: "#374151",
              border: "1px solid #d1d5db", borderRadius: 8,
              padding: "5px 12px", cursor: "pointer", fontSize: "0.8rem",
            }}>{loading ? "..." : "Ricontrolla"}</button>
            <button onClick={onClose} style={{
              background: "transparent", color: "#6b7280",
              border: "1px solid #d1d5db", borderRadius: 8,
              padding: "5px 12px", cursor: "pointer", fontSize: "0.8rem",
            }}>Chiudi ✕</button>
          </div>
        </div>

        {/* Stato complessivo */}
        {data && (
          <div style={{
            display: "flex", alignItems: "center", gap: 12,
            background: `${overallColor}14`,
            border: `1px solid ${overallColor}55`,
            borderLeft: `4px solid ${overallColor}`,
            borderRadius: 10, padding: "12px 16px", marginBottom: 16,
          }}>
            <span style={{ width: 12, height: 12, borderRadius: "50%",
              background: overallColor, flexShrink: 0 }} />
            <div>
              <div style={{ fontWeight: 700, color: overallColor }}>
                {STATUS_LABEL[overall] || overall}
              </div>
              <div style={{ fontSize: "0.74rem", color: "#6b7280" }}>
                {data.timestamp ? new Date(data.timestamp).toLocaleString("it-IT") : ""}
              </div>
            </div>
          </div>
        )}

        {loading && !data && (
          <div style={{ color: "#6b7280", padding: 24 }}>Controllo in corso…</div>
        )}
        {err && (
          <div style={{ color: "#991b1b", padding: 14, background: "#fef2f2",
            border: "1px solid #fecaca", borderRadius: 8 }}>
            Errore: {err}
            <div style={{ fontSize: "0.72rem", color: "#6b7280", marginTop: 6 }}>
              Se l'app è in riavvio (OOM) o offline, il controllo non risponde.
            </div>
          </div>
        )}

        {/* Lista dei check */}
        {data?.checks && data.checks.map((c) => {
          const color = STATUS_COLOR[c.status] || "#6b7280";
          return (
            <div key={c.name} style={{
              display: "flex", alignItems: "flex-start", gap: 10,
              padding: "10px 12px", marginBottom: 8,
              background: "#f9fafb", border: "1px solid #f3f4f6",
              borderRadius: 8,
            }}>
              <span style={{ width: 10, height: 10, borderRadius: "50%",
                background: color, flexShrink: 0, marginTop: 5 }} />
              <div style={{ flex: 1 }}>
                <div style={{ display: "flex", justifyContent: "space-between",
                              alignItems: "center" }}>
                  <span style={{ fontWeight: 600, fontSize: "0.88rem" }}>
                    {CHECK_LABEL[c.name] || c.name}
                  </span>
                  <span style={{ fontSize: "0.7rem", fontWeight: 700,
                    color, textTransform: "uppercase" }}>{c.status}</span>
                </div>
                <div style={{ fontSize: "0.78rem", color: "#4b5563",
                  marginTop: 2, lineHeight: 1.4 }}>{c.detail}</div>
              </div>
            </div>
          );
        })}

        <div style={{ fontSize: "0.72rem", color: "#9ca3af", marginTop: 10 }}>
          I controlli girano anche da soli ogni 6 ore. Qui forzi un controllo
          fresco al volo.
        </div>
      </div>
    </div>
  );
}
