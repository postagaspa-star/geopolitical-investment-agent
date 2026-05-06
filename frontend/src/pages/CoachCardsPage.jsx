import { useEffect, useState } from "react";
import {
  BookOpen, Loader2, RefreshCw, Trash2, Sparkles, AlertCircle, Zap,
  Check, X,
} from "lucide-react";

const API = window.location.origin;

/**
 * Coach Cards — pagina Live che mostra le raccomandazioni operative
 * sintetizzate settimanalmente dalla memoria del Sim Advisor.
 *
 * Ogni card ha: titolo, DO/DON'T, rationale, scenarios_signature.
 * L'AI le rigenera ogni Lunedì alle 06:00 UTC dal cron weekly.
 * Trigger manuale disponibile via "Rigenera ora" (~30s).
 */
export default function CoachCardsPage() {
  const [cards, setCards] = useState([]);
  const [status, setStatus] = useState({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [synthesizing, setSynthesizing] = useState(false);

  const fetchCards = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API}/api/coach-cards`);
      const data = await res.json();
      if (!res.ok) {
        setError(data.error || `HTTP ${res.status}`);
      } else {
        setCards(data.cards || []);
        setStatus(data.status || {});
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchCards(); }, []);

  const triggerSynthesis = async () => {
    if (synthesizing) return;
    if (!window.confirm(
      "Rigenerare ora le Coach Cards? L'operazione dura 30-60s e legge tutta la memoria del Sim Advisor."
    )) return;
    setSynthesizing(true);
    try {
      await fetch(`${API}/api/coach-cards/synthesize`, { method: "POST" });
      // Polling: ricarica ogni 8s per i prossimi 90s
      let attempts = 0;
      const poll = setInterval(async () => {
        attempts++;
        await fetchCards();
        if (attempts >= 12) {
          clearInterval(poll);
          setSynthesizing(false);
        }
      }, 8000);
    } catch (e) {
      alert(`Errore: ${e}`);
      setSynthesizing(false);
    }
  };

  const deleteCard = async (cardId) => {
    if (!window.confirm("Eliminare questa Coach Card?")) return;
    try {
      const res = await fetch(`${API}/api/coach-cards/${cardId}`, { method: "DELETE" });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        alert(`Errore: ${data.error || res.status}`);
        return;
      }
      setCards(prev => prev.filter(c => c.id !== cardId));
    } catch (e) {
      alert(`Errore: ${e}`);
    }
  };

  if (loading) {
    return (
      <div className="card" style={S.center}>
        <Loader2 size={16} className="spin" style={{ marginRight: 8 }} />
        Caricamento Coach Cards...
        <style>{`.spin { animation: spin 1s linear infinite; } @keyframes spin { to { transform: rotate(360deg); } }`}</style>
      </div>
    );
  }

  return (
    <div>
      {/* Header */}
      <div style={S.header}>
        <div>
          <h1 style={S.h1}>
            <BookOpen size={20} style={{ marginRight: 8, verticalAlign: "middle", color: "#a78bfa" }} />
            Coach Cards
          </h1>
          <div style={S.subtitle}>
            Raccomandazioni operative high-level, sintetizzate settimanalmente
            dall'AI dalla memoria del Sim Advisor. Il Decision Agent Live le
            considera come reminder durante i suoi run.
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button style={S.btnSecondary} onClick={fetchCards}>
            <RefreshCw size={14} /> Aggiorna
          </button>
          <button
            style={{ ...S.btnPrimary, opacity: synthesizing ? 0.6 : 1 }}
            onClick={triggerSynthesis}
            disabled={synthesizing}
          >
            {synthesizing ? (
              <>
                <Loader2 size={14} className="spin" /> Generazione...
              </>
            ) : (
              <>
                <Zap size={14} /> Rigenera ora
              </>
            )}
          </button>
        </div>
      </div>

      {/* Status info */}
      {status.last_synth_at && (
        <div style={S.statusBar}>
          Ultima synthesis: <strong>{formatDate(status.last_synth_at)}</strong>
          {" · "}
          Stato: <span style={{
            color: status.synth_status === "ok" ? "#10b981"
                 : status.synth_status === "running" ? "#fbbf24"
                 : "#ef4444",
          }}>
            {status.synth_status || "—"}
          </span>
          {" · "}
          Cards attive: <strong>{status.active_cards ?? 0}</strong>
          {" · "}
          <span style={{ color: "#94a3b8" }}>Auto-rigenerate ogni Lunedì 06:00 UTC</span>
        </div>
      )}

      {error && (
        <div style={S.error}>
          <AlertCircle size={16} style={{ marginRight: 8 }} /> {error}
        </div>
      )}

      {cards.length === 0 && !error && (
        <div style={S.emptyState}>
          <BookOpen size={32} style={{ color: "#475569", marginBottom: 12 }} />
          <div style={{ fontSize: 14, color: "#94a3b8", marginBottom: 8 }}>
            Nessuna Coach Card attiva.
          </div>
          <div style={{ fontSize: 12, color: "#64748b", maxWidth: 460, lineHeight: 1.6 }}>
            Le card vengono generate ogni Lunedì alle 06:00 UTC analizzando
            i consigli archiviati nel Sim Advisor. Puoi anche triggerare una
            synthesis manuale dal pulsante "Rigenera ora".
          </div>
        </div>
      )}

      <div style={S.grid}>
        {cards.map((card) => (
          <CoachCard key={card.id} card={card} onDelete={() => deleteCard(card.id)} />
        ))}
      </div>
    </div>
  );
}

function CoachCard({ card, onDelete }) {
  return (
    <div style={S.card}>
      <div style={S.cardHeader}>
        <div style={S.cardTitleRow}>
          <Sparkles size={14} style={{ color: "#fbbf24", flexShrink: 0 }} />
          <h3 style={S.cardTitle}>{card.title}</h3>
        </div>
        <button style={S.deleteBtn} onClick={onDelete} title="Elimina card">
          <Trash2 size={12} />
        </button>
      </div>

      <div style={S.tagRow}>
        {card.scenarios_signature && (
          <span style={S.signatureTag}>{card.scenarios_signature}</span>
        )}
        {card.category_focus && (
          <span style={S.categoryTag}>{card.category_focus}</span>
        )}
        <span style={S.weekTag}>Sett. {card.week_of || "—"}</span>
      </div>

      <div style={S.section}>
        <div style={{ ...S.sectionLabel, color: "#10b981" }}>
          <Check size={12} style={{ marginRight: 4 }} /> DO
        </div>
        <div style={S.sectionText}>{card.do}</div>
      </div>

      <div style={S.section}>
        <div style={{ ...S.sectionLabel, color: "#ef4444" }}>
          <X size={12} style={{ marginRight: 4 }} /> DON'T
        </div>
        <div style={S.sectionText}>{card.dont}</div>
      </div>

      {card.rationale && (
        <div style={S.rationale}>
          <em>Razionale:</em> {card.rationale}
        </div>
      )}

      <div style={S.footer}>
        <span>Da {card.source_advice_count || 0} advice del Simulator</span>
        <span>·</span>
        <span>Iniettata {card.applied_count || 0}× nei run Live</span>
      </div>
    </div>
  );
}

function formatDate(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("it-IT", {
      day: "2-digit", month: "2-digit", year: "numeric",
      hour: "2-digit", minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

const S = {
  header: {
    display: "flex", justifyContent: "space-between", alignItems: "flex-start",
    marginBottom: 20, gap: 16, flexWrap: "wrap",
  },
  h1: { fontSize: "1.5rem", margin: 0, fontWeight: 600 },
  subtitle: {
    fontSize: 13, color: "#94a3b8", marginTop: 6, lineHeight: 1.5,
    maxWidth: 720,
  },
  statusBar: {
    background: "#0f172a", border: "1px solid rgba(255,255,255,0.05)",
    borderRadius: 10, padding: "10px 14px", marginBottom: 18,
    fontSize: 12, color: "#cbd5e1",
  },
  emptyState: {
    padding: "48px 24px", textAlign: "center",
    background: "linear-gradient(180deg, #111827 0%, rgba(17,24,39,0.92) 100%)",
    border: "1px solid rgba(255,255,255,0.05)", borderRadius: 14,
    display: "flex", flexDirection: "column", alignItems: "center",
  },
  error: {
    padding: 14, background: "rgba(239,68,68,0.1)",
    border: "1px solid rgba(239,68,68,0.3)", borderRadius: 8,
    color: "#fca5a5", fontSize: 13,
    display: "flex", alignItems: "center", marginBottom: 16,
  },
  grid: {
    display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(340px, 1fr))",
    gap: 14,
  },
  card: {
    background: "linear-gradient(180deg, #111827 0%, rgba(17,24,39,0.92) 100%)",
    border: "1px solid rgba(167,139,250,0.18)",
    borderRadius: 14, padding: 18,
    boxShadow: "0 1px 3px rgba(0,0,0,0.18), 0 1px 2px rgba(0,0,0,0.12)",
    transition: "border-color 0.2s ease, transform 0.2s ease",
  },
  cardHeader: {
    display: "flex", justifyContent: "space-between", alignItems: "flex-start",
    gap: 8, marginBottom: 10,
  },
  cardTitleRow: {
    display: "flex", gap: 6, alignItems: "flex-start", flex: 1,
  },
  cardTitle: {
    fontSize: 14, fontWeight: 700, color: "#e2e8f0", margin: 0,
    lineHeight: 1.4,
  },
  deleteBtn: {
    background: "transparent", border: "1px solid rgba(239,68,68,0.25)",
    color: "#fca5a5", padding: "4px 6px", borderRadius: 6,
    cursor: "pointer", display: "inline-flex", alignItems: "center",
    flexShrink: 0,
  },
  tagRow: {
    display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 14,
  },
  signatureTag: {
    background: "rgba(167,139,250,0.14)", color: "#c4b5fd",
    border: "1px solid rgba(167,139,250,0.3)",
    padding: "3px 9px", borderRadius: 999, fontSize: 10,
    fontFamily: "monospace", fontWeight: 600,
  },
  categoryTag: {
    background: "rgba(6,182,212,0.12)", color: "#67e8f9",
    border: "1px solid rgba(6,182,212,0.25)",
    padding: "3px 9px", borderRadius: 999, fontSize: 10,
    fontWeight: 600,
  },
  weekTag: {
    background: "rgba(148,163,184,0.1)", color: "#94a3b8",
    padding: "3px 9px", borderRadius: 999, fontSize: 10,
    fontFamily: "monospace",
  },
  section: { marginBottom: 12 },
  sectionLabel: {
    fontSize: 10, fontWeight: 700, textTransform: "uppercase",
    letterSpacing: "0.08em", marginBottom: 4,
    display: "flex", alignItems: "center",
  },
  sectionText: {
    fontSize: 13, color: "#cbd5e1", lineHeight: 1.55,
    whiteSpace: "pre-wrap",
  },
  rationale: {
    fontSize: 11.5, color: "#94a3b8", lineHeight: 1.5,
    paddingTop: 10, borderTop: "1px dashed rgba(255,255,255,0.06)",
    marginTop: 4,
  },
  footer: {
    fontSize: 10, color: "#64748b", marginTop: 12,
    paddingTop: 10, borderTop: "1px dashed rgba(255,255,255,0.06)",
    display: "flex", gap: 8,
    fontFamily: "monospace",
  },
  btnSecondary: {
    background: "transparent", color: "#cbd5e1",
    border: "1px solid rgba(255,255,255,0.1)",
    padding: "8px 14px", borderRadius: 8,
    cursor: "pointer", display: "inline-flex", alignItems: "center", gap: 6,
    fontSize: 13, fontFamily: "inherit",
  },
  btnPrimary: {
    background: "#a78bfa", color: "#0a0e1a",
    border: 0, padding: "8px 16px", borderRadius: 8, fontWeight: 600,
    cursor: "pointer", display: "inline-flex", alignItems: "center", gap: 6,
    fontSize: 13, fontFamily: "inherit",
  },
  center: {
    padding: 30, display: "flex", alignItems: "center", justifyContent: "center",
    color: "#94a3b8", fontSize: 13,
  },
};
