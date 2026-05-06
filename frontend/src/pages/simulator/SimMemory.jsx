import { useEffect, useState } from "react";
import { BookOpen, Trash2, Sparkles, Loader2, RefreshCw, AlertCircle } from "lucide-react";

const API = window.location.origin;

/**
 * Memoria Consigli — pagina dedicata che mostra TUTTI gli advice salvati,
 * raggruppati per scenario_category (es. macro__bear, crypto_volatile).
 *
 * Mostra: titolo, testo, razionale, apply_count (quante volte e' stato
 * iniettato in run successivi), pulsante elimina.
 *
 * Endpoint: GET /api/simulator/advisor/memory/all
 *           DELETE /api/simulator/advisor/memory/{key}/{advice_id}
 */
export default function SimMemory() {
  const [groups, setGroups] = useState({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [stats, setStats] = useState({ category_count: 0, total_advices: 0 });
  const [expandedKeys, setExpandedKeys] = useState({});

  const fetchMemory = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API}/api/simulator/advisor/memory/all`);
      const data = await res.json();
      if (!res.ok) {
        setError(data.error || `HTTP ${res.status}`);
      } else {
        setGroups(data.groups || {});
        setStats({
          category_count: data.category_count || 0,
          total_advices: data.total_advices || 0,
        });
        // Auto-espandi tutti i gruppi al primo load
        const expand = {};
        Object.keys(data.groups || {}).forEach(k => { expand[k] = true; });
        setExpandedKeys(expand);
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchMemory(); }, []);

  const deleteAdvice = async (categoryKey, adviceId) => {
    if (!window.confirm("Eliminare questo consiglio dalla memoria?")) return;
    try {
      const res = await fetch(
        `${API}/api/simulator/advisor/memory/${encodeURIComponent(categoryKey)}/${encodeURIComponent(adviceId)}`,
        { method: "DELETE" },
      );
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        alert(`Errore: ${data.error || res.status}`);
        return;
      }
      // Update locale: rimuovi dall'oggetto groups
      setGroups(prev => {
        const newGroups = { ...prev };
        if (newGroups[categoryKey]) {
          newGroups[categoryKey] = newGroups[categoryKey].filter(a => a.id !== adviceId);
          if (newGroups[categoryKey].length === 0) {
            delete newGroups[categoryKey];
          }
        }
        return newGroups;
      });
      setStats(prev => ({
        ...prev,
        total_advices: Math.max(0, prev.total_advices - 1),
        category_count: Object.keys(groups).length,
      }));
    } catch (e) {
      alert(`Errore delete: ${e}`);
    }
  };

  const toggleGroup = (key) => {
    setExpandedKeys(prev => ({ ...prev, [key]: !prev[key] }));
  };

  if (loading) {
    return (
      <div style={S.loading}>
        <Loader2 size={16} className="spin" style={{ marginRight: 8 }} />
        Caricamento memoria consigli...
        <style>{`.spin { animation: spin 1s linear infinite; } @keyframes spin { to { transform: rotate(360deg); } }`}</style>
      </div>
    );
  }
  if (error) {
    return (
      <div style={S.error}>
        <AlertCircle size={16} style={{ marginRight: 8 }} />
        {error}
        <button style={S.btnSecondary} onClick={fetchMemory}>
          <RefreshCw size={14} /> Riprova
        </button>
      </div>
    );
  }

  const categoryKeys = Object.keys(groups).sort();

  return (
    <div>
      {/* Header */}
      <div style={S.headerRow}>
        <div>
          <h1 style={S.h1}>
            <BookOpen size={20} style={{ marginRight: 8, verticalAlign: "middle", color: "#a78bfa" }} />
            Memoria Consigli
          </h1>
          <div style={S.subtitle}>
            {stats.total_advices} consigli archiviati in {stats.category_count} categorie scenario.
            Vengono iniettati automaticamente nei run futuri della stessa categoria.
          </div>
        </div>
        <button style={S.btnSecondary} onClick={fetchMemory}>
          <RefreshCw size={14} /> Aggiorna
        </button>
      </div>

      {categoryKeys.length === 0 && (
        <div style={S.emptyState}>
          Nessun consiglio salvato. Per archiviarne uno, completa un run nel
          Simulator e usa la tab <strong>Analizza & Migliora</strong> sulla
          pagina del risultato.
        </div>
      )}

      {/* Lista gruppi */}
      {categoryKeys.map((key) => {
        const items = groups[key] || [];
        const expanded = expandedKeys[key];
        return (
          <div key={key} style={S.group}>
            <div style={S.groupHeader} onClick={() => toggleGroup(key)}>
              <div>
                <span style={S.categoryBadge}>{key}</span>
                <span style={S.groupCount}>{items.length} consigli</span>
              </div>
              <span style={S.toggleIcon}>{expanded ? "−" : "+"}</span>
            </div>
            {expanded && (
              <div style={S.groupBody}>
                {items.map((a) => (
                  <AdviceCard
                    key={a.id}
                    advice={a}
                    onDelete={() => deleteAdvice(key, a.id)}
                  />
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function AdviceCard({ advice, onDelete }) {
  return (
    <div style={S.advice}>
      <div style={S.adviceHeader}>
        <div style={S.adviceTitle}>
          <Sparkles size={13} style={{ color: "#fbbf24", marginRight: 6 }} />
          {advice.title || "(senza titolo)"}
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <span style={S.applyBadge}>iniettato {advice.apply_count || 0}×</span>
          <button style={S.deleteBtn} onClick={onDelete} title="Elimina">
            <Trash2 size={12} />
          </button>
        </div>
      </div>
      <div style={S.adviceText}>{advice.text}</div>
      {advice.rationale && (
        <div style={S.adviceRationale}>
          <em>Razionale:</em> {advice.rationale}
        </div>
      )}
      <div style={S.adviceMeta}>
        Creato: {new Date(advice.created_at).toLocaleString("it-IT", {
          day: "2-digit", month: "2-digit", year: "numeric",
          hour: "2-digit", minute: "2-digit",
        })}
      </div>
    </div>
  );
}

const S = {
  headerRow: {
    display: "flex", justifyContent: "space-between", alignItems: "flex-start",
    marginBottom: 24, gap: 16, flexWrap: "wrap",
  },
  h1: { fontSize: "1.5rem", margin: 0, fontWeight: 600 },
  subtitle: {
    fontSize: 13, color: "#94a3b8", marginTop: 6, lineHeight: 1.5,
    maxWidth: 720,
  },
  emptyState: {
    padding: "40px 24px", textAlign: "center", color: "#94a3b8",
    background: "#0f172a", border: "1px dashed #475569", borderRadius: 12,
    fontSize: 13, lineHeight: 1.6,
  },
  group: {
    marginBottom: 16, background: "linear-gradient(180deg, #111827 0%, rgba(17,24,39,0.92) 100%)",
    border: "1px solid rgba(255,255,255,0.05)", borderRadius: 14,
    overflow: "hidden",
    boxShadow: "0 1px 3px rgba(0,0,0,0.18), 0 1px 2px rgba(0,0,0,0.12)",
  },
  groupHeader: {
    padding: "14px 18px", display: "flex", justifyContent: "space-between",
    alignItems: "center", cursor: "pointer", userSelect: "none",
    transition: "background 0.15s ease",
  },
  groupCount: {
    marginLeft: 12, color: "#94a3b8", fontSize: 12,
  },
  categoryBadge: {
    background: "rgba(167,139,250,0.14)",
    color: "#c4b5fd",
    border: "1px solid rgba(167,139,250,0.35)",
    padding: "0.28rem 0.7rem",
    borderRadius: 999,
    fontSize: 11,
    fontFamily: "monospace",
    fontWeight: 600,
    letterSpacing: "0.04em",
  },
  toggleIcon: {
    fontSize: 22, color: "#64748b", fontWeight: 300, lineHeight: 1,
    width: 24, textAlign: "center",
  },
  groupBody: {
    padding: "0 18px 14px", display: "flex", flexDirection: "column", gap: 10,
  },
  advice: {
    background: "#0a1018", border: "1px solid rgba(255,255,255,0.04)",
    borderLeft: "3px solid #a78bfa",
    borderRadius: 8, padding: "12px 14px",
  },
  adviceHeader: {
    display: "flex", justifyContent: "space-between", alignItems: "flex-start",
    marginBottom: 8, gap: 12,
  },
  adviceTitle: {
    fontSize: 13, fontWeight: 600, color: "#e2e8f0",
    display: "flex", alignItems: "center", flex: 1,
  },
  applyBadge: {
    fontSize: 10, color: "#fbbf24", background: "rgba(251,191,36,0.12)",
    border: "1px solid rgba(251,191,36,0.3)",
    padding: "3px 8px", borderRadius: 999, whiteSpace: "nowrap",
  },
  deleteBtn: {
    background: "transparent", border: "1px solid rgba(239,68,68,0.3)",
    color: "#fca5a5", padding: "4px 8px", borderRadius: 6,
    cursor: "pointer", display: "inline-flex", alignItems: "center",
  },
  adviceText: {
    fontSize: 12.5, color: "#cbd5e1", lineHeight: 1.6, marginBottom: 6,
    whiteSpace: "pre-wrap",
  },
  adviceRationale: {
    fontSize: 11.5, color: "#94a3b8", lineHeight: 1.5,
    paddingTop: 6, borderTop: "1px dashed rgba(255,255,255,0.06)",
    marginTop: 6,
  },
  adviceMeta: {
    fontSize: 10, color: "#64748b", marginTop: 8,
    fontFamily: "monospace",
  },
  loading: {
    padding: 40, textAlign: "center", color: "#94a3b8",
    display: "flex", alignItems: "center", justifyContent: "center", fontSize: 13,
  },
  error: {
    padding: 24, color: "#ef4444",
    display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap",
  },
  btnSecondary: {
    background: "transparent", color: "#cbd5e1",
    border: "1px solid rgba(255,255,255,0.1)",
    padding: "8px 14px", borderRadius: 8,
    cursor: "pointer", display: "inline-flex", alignItems: "center", gap: 6,
    fontSize: 13, fontFamily: "inherit",
  },
};
