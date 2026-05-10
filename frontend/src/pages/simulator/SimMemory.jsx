import { useEffect, useMemo, useState } from "react";
import { BookOpen, Trash2, Sparkles, Loader2, RefreshCw, AlertCircle,
         Search, X, Bot, User, ArrowUpDown } from "lucide-react";

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

  // ── Filtri ────────────────────────────────────────────────────────────
  // search: testo libero su title/text/rationale
  // categoryFilter: "all" | category_key specifica (es. "macro__bear")
  // sourceFilter: "all" | "auto" (auto-saved da finalize_run) | "manual"
  // sortBy: "recent" | "applied_count_desc" | "title"
  const [search, setSearch] = useState("");
  const [categoryFilter, setCategoryFilter] = useState("all");
  const [sourceFilter, setSourceFilter] = useState("all");
  const [sortBy, setSortBy] = useState("recent");

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

  // Filtraggio + ordinamento applicati ai gruppi.
  // Applico:
  //  - search: case-insensitive su title/text/rationale
  //  - categoryFilter: tiene solo il gruppo selezionato
  //  - sourceFilter: tiene solo advice con scenario_tags.auto == true (Auto)
  //                  o == false (Manual)
  //  - sortBy: ordina i singoli items dentro ogni gruppo
  const filteredGroups = useMemo(() => {
    const q = search.trim().toLowerCase();
    const filterAdvice = (a) => {
      // Source filter (auto vs manual)
      if (sourceFilter !== "all") {
        const isAuto = !!(a.scenario_tags && a.scenario_tags.auto);
        if (sourceFilter === "auto" && !isAuto) return false;
        if (sourceFilter === "manual" && isAuto) return false;
      }
      // Search testo
      if (q) {
        const blob = `${a.title || ""}\n${a.text || ""}\n${a.rationale || ""}`.toLowerCase();
        if (!blob.includes(q)) return false;
      }
      return true;
    };
    const sortAdvice = (a, b) => {
      if (sortBy === "applied_count_desc") {
        return (b.apply_count || 0) - (a.apply_count || 0);
      }
      if (sortBy === "title") {
        return (a.title || "").localeCompare(b.title || "");
      }
      // default: recent
      return (b.created_at || "").localeCompare(a.created_at || "");
    };

    const out = {};
    for (const [key, items] of Object.entries(groups)) {
      if (categoryFilter !== "all" && key !== categoryFilter) continue;
      const filtered = (items || []).filter(filterAdvice).sort(sortAdvice);
      if (filtered.length > 0) out[key] = filtered;
    }
    return out;
  }, [groups, search, categoryFilter, sourceFilter, sortBy]);

  const categoryKeys = Object.keys(filteredGroups).sort();
  const allCategoryKeys = Object.keys(groups).sort();
  const filteredCount = Object.values(filteredGroups)
                              .reduce((s, items) => s + items.length, 0);
  const hasActiveFilters = !!(search || categoryFilter !== "all"
                               || sourceFilter !== "all"
                               || sortBy !== "recent");

  const clearFilters = () => {
    setSearch(""); setCategoryFilter("all");
    setSourceFilter("all"); setSortBy("recent");
  };

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

      {/* ── Barra filtri ────────────────────────────────────────────── */}
      {stats.total_advices > 0 && (
        <div style={S.filterBar}>
          {/* Search */}
          <div style={S.searchWrap}>
            <Search size={14} style={{ color: "#64748b" }} />
            <input
              type="text"
              placeholder="Cerca in titolo, testo, razionale..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              style={S.searchInput}
            />
            {search && (
              <button style={S.searchClear} onClick={() => setSearch("")}
                       aria-label="Pulisci ricerca">
                <X size={12} />
              </button>
            )}
          </div>

          {/* Categoria (dropdown) */}
          <select value={categoryFilter}
                  onChange={(e) => setCategoryFilter(e.target.value)}
                  style={S.select}>
            <option value="all">Tutte le categorie ({allCategoryKeys.length})</option>
            {allCategoryKeys.map(k => (
              <option key={k} value={k}>
                {k} ({(groups[k] || []).length})
              </option>
            ))}
          </select>

          {/* Source filter: Auto vs Manual */}
          <div style={S.toggleGroup}>
            <button
              style={{ ...S.toggleBtn,
                       ...(sourceFilter === "all" ? S.toggleBtnActive : {}) }}
              onClick={() => setSourceFilter("all")}
            >
              Tutti
            </button>
            <button
              style={{ ...S.toggleBtn,
                       ...(sourceFilter === "auto" ? S.toggleBtnActive : {}) }}
              onClick={() => setSourceFilter("auto")}
              title="Solo i consigli salvati automaticamente al termine del run"
            >
              <Bot size={12} style={{ marginRight: 4, verticalAlign: "middle" }} />
              Auto
            </button>
            <button
              style={{ ...S.toggleBtn,
                       ...(sourceFilter === "manual" ? S.toggleBtnActive : {}) }}
              onClick={() => setSourceFilter("manual")}
              title="Solo i consigli salvati manualmente via 'Analizza & Migliora'"
            >
              <User size={12} style={{ marginRight: 4, verticalAlign: "middle" }} />
              Manuale
            </button>
          </div>

          {/* Sort */}
          <select value={sortBy} onChange={(e) => setSortBy(e.target.value)}
                  style={{ ...S.select, paddingLeft: 26 }}
                  title="Ordina i consigli">
            <option value="recent">↓ Più recenti</option>
            <option value="applied_count_desc">↓ Più applicati</option>
            <option value="title">A → Z</option>
          </select>

          {/* Reset */}
          {hasActiveFilters && (
            <button style={S.resetBtn} onClick={clearFilters}>
              <X size={12} /> Reset
            </button>
          )}

          {/* Risultati */}
          <div style={S.filterCount}>
            <strong style={{ color: "#e2e8f0" }}>{filteredCount}</strong>
            <span style={{ color: "#64748b" }}> / {stats.total_advices}</span>
            {hasActiveFilters && (
              <span style={{ color: "#a78bfa", marginLeft: 6, fontSize: 11 }}>
                · filtrati
              </span>
            )}
          </div>
        </div>
      )}

      {stats.total_advices === 0 && (
        <div style={S.emptyState}>
          Nessun consiglio salvato. Per archiviarne uno, completa un run nel
          Simulator (auto-saved automaticamente) o usa la tab
          <strong> Analizza & Migliora</strong> sulla pagina del risultato.
        </div>
      )}

      {stats.total_advices > 0 && categoryKeys.length === 0 && (
        <div style={S.emptyState}>
          Nessun consiglio corrisponde ai filtri selezionati.
          <button style={{ ...S.btnSecondary, marginLeft: 12 }}
                   onClick={clearFilters}>
            <X size={12} /> Pulisci filtri
          </button>
        </div>
      )}

      {/* Lista gruppi (filtrati) */}
      {categoryKeys.map((key) => {
        const items = filteredGroups[key] || [];
        const expanded = expandedKeys[key] !== false;   // default true
        return (
          <div key={key} style={S.group}>
            <div style={S.groupHeader} onClick={() => toggleGroup(key)}>
              <div>
                <span style={S.categoryBadge}>{key}</span>
                <span style={S.groupCount}>
                  {items.length}
                  {items.length !== (groups[key] || []).length && (
                    <span style={{ color: "#64748b" }}>
                      {" / "}{(groups[key] || []).length}
                    </span>
                  )}
                  {" consigli"}
                </span>
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
  const isAuto = !!(advice.scenario_tags && advice.scenario_tags.auto);
  return (
    <div style={S.advice}>
      <div style={S.adviceHeader}>
        <div style={S.adviceTitle}>
          <Sparkles size={13} style={{ color: "#fbbf24", marginRight: 6 }} />
          {advice.title || "(senza titolo)"}
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          {isAuto ? (
            <span style={S.autoBadge} title="Salvato automaticamente al termine del run">
              <Bot size={10} style={{ marginRight: 3, verticalAlign: "middle" }} />
              AUTO
            </span>
          ) : (
            <span style={S.manualBadge} title="Salvato manualmente via Analizza & Migliora">
              <User size={10} style={{ marginRight: 3, verticalAlign: "middle" }} />
              MANUAL
            </span>
          )}
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
  autoBadge: {
    fontSize: 10, color: "#a78bfa", background: "rgba(167,139,250,0.10)",
    border: "1px solid rgba(167,139,250,0.30)",
    padding: "3px 8px", borderRadius: 999, whiteSpace: "nowrap",
    fontWeight: 600, letterSpacing: "0.04em",
  },
  manualBadge: {
    fontSize: 10, color: "#06b6d4", background: "rgba(6,182,212,0.10)",
    border: "1px solid rgba(6,182,212,0.30)",
    padding: "3px 8px", borderRadius: 999, whiteSpace: "nowrap",
    fontWeight: 600, letterSpacing: "0.04em",
  },

  // Barra filtri
  filterBar: {
    display: "flex", flexWrap: "wrap", gap: 10, alignItems: "center",
    background: "#0f172a", border: "1px solid #1f2937",
    padding: "10px 14px", borderRadius: 10, marginBottom: 18,
  },
  searchWrap: {
    position: "relative", display: "inline-flex", alignItems: "center", gap: 6,
    background: "#0a1018", border: "1px solid #1f2937",
    borderRadius: 6, padding: "5px 10px", minWidth: 260, flex: "1 1 260px",
  },
  searchInput: {
    background: "transparent", border: 0, color: "#e2e8f0",
    fontSize: 13, outline: "none", flex: 1, fontFamily: "inherit",
  },
  searchClear: {
    background: "transparent", border: 0, color: "#64748b",
    cursor: "pointer", padding: 2, display: "inline-flex",
  },
  select: {
    background: "#0a1018", border: "1px solid #1f2937", color: "#e2e8f0",
    fontSize: 12, padding: "7px 10px", borderRadius: 6,
    fontFamily: "inherit", cursor: "pointer", minWidth: 180,
  },
  toggleGroup: {
    display: "inline-flex", border: "1px solid #1f2937",
    borderRadius: 6, overflow: "hidden",
  },
  toggleBtn: {
    background: "transparent", border: 0, color: "#94a3b8",
    fontSize: 12, padding: "7px 12px", cursor: "pointer",
    fontFamily: "inherit", fontWeight: 500,
    borderRight: "1px solid #1f2937",
  },
  toggleBtnActive: {
    background: "rgba(167,139,250,0.15)", color: "#c4b5fd",
  },
  resetBtn: {
    background: "transparent", border: "1px solid rgba(239,68,68,0.30)",
    color: "#fca5a5", padding: "6px 10px", borderRadius: 6,
    cursor: "pointer", fontSize: 11.5, fontFamily: "inherit",
    display: "inline-flex", alignItems: "center", gap: 4,
  },
  filterCount: {
    marginLeft: "auto", fontSize: 12, color: "#94a3b8",
    fontFamily: "monospace",
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
