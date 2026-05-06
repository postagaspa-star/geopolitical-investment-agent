import { useEffect, useRef, useState } from "react";
import {
  Brain, Send, Save, Loader2, RefreshCw, Trash2, BookOpen, Sparkles, Check,
} from "lucide-react";

const API = window.location.origin;

/**
 * SimAdvisorChat — coach AI per analizzare un run e proporre consigli che
 * vengono salvati in una memoria categorizzata. La memoria viene poi
 * iniettata automaticamente nei run futuri della stessa categoria.
 *
 * Usato come tab "Analizza & Migliora" in SimResult.jsx.
 *
 * Props:
 *   runId  — uuid del run da analizzare
 */
export default function SimAdvisorChat({ runId }) {
  const [state, setState] = useState(null); // { messages, category_key, scenario_tags, existing_advices }
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [savedIds, setSavedIds] = useState({}); // { "msgIdx_advIdx": adviceId }
  const [savingKey, setSavingKey] = useState(null);
  const [showMemoryPanel, setShowMemoryPanel] = useState(false);
  const scrollRef = useRef(null);

  const fetchState = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API}/api/simulator/advisor/${runId}`);
      const data = await res.json();
      if (!res.ok) {
        setError(data.error || `HTTP ${res.status}`);
      } else {
        setState(data);
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (runId) fetchState();
  }, [runId]);

  // Auto-scroll quando arrivano messaggi nuovi
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [state?.messages?.length]);

  const sendMessage = async () => {
    const msg = input.trim();
    if (!msg || sending) return;
    setSending(true);
    setInput("");
    try {
      const res = await fetch(`${API}/api/simulator/advisor/${runId}/message`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: msg }),
      });
      const data = await res.json();
      if (!res.ok) {
        setError(data.error || `HTTP ${res.status}`);
      } else {
        // Append entrambi i messaggi alla state
        setState((prev) => prev ? {
          ...prev,
          messages: [...(prev.messages || []), data.user_message, data.assistant_message],
        } : prev);
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setSending(false);
    }
  };

  const saveAdvice = async (advice, msgIdx, advIdx) => {
    const key = `${msgIdx}_${advIdx}`;
    if (savedIds[key] || savingKey === key) return;
    setSavingKey(key);
    try {
      const res = await fetch(`${API}/api/simulator/advisor/${runId}/save`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: advice.title || "",
          text: advice.text || "",
          rationale: advice.rationale || "",
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        alert(`Errore: ${data.error || res.status}`);
      } else {
        setSavedIds((prev) => ({ ...prev, [key]: data.id }));
        // Refresh existing_advices nel state
        fetchState();
      }
    } catch (e) {
      alert(`Errore: ${e}`);
    } finally {
      setSavingKey(null);
    }
  };

  const resetChat = async () => {
    if (!window.confirm("Reset della chat advisor per questo run? La memoria archiviata resta.")) return;
    try {
      await fetch(`${API}/api/simulator/advisor/${runId}`, { method: "DELETE" });
      setSavedIds({});
      fetchState();
    } catch (e) {
      alert(`Errore reset: ${e}`);
    }
  };

  const deleteAdvice = async (categoryKey, adviceId) => {
    if (!window.confirm("Eliminare questo advice dalla memoria?")) return;
    try {
      await fetch(
        `${API}/api/simulator/advisor/memory/${encodeURIComponent(categoryKey)}/${encodeURIComponent(adviceId)}`,
        { method: "DELETE" },
      );
      fetchState();
    } catch (e) {
      alert(`Errore delete: ${e}`);
    }
  };

  if (loading) {
    return (
      <div style={S.loading}>
        <Loader2 size={16} className="spin" style={{ marginRight: 8 }} />
        Generazione analisi iniziale in corso (DeepSeek-R1, ~30s)...
      </div>
    );
  }
  if (error) {
    return (
      <div style={S.errorBox}>
        <div style={{ color: "#ef4444", marginBottom: 12 }}>⚠ {error}</div>
        <button style={S.btnSecondary} onClick={fetchState}>
          <RefreshCw size={14} /> Riprova
        </button>
      </div>
    );
  }
  if (!state) return null;

  const tags = state.scenario_tags || {};
  const existing = state.existing_advices || [];

  return (
    <div style={S.container}>
      {/* Header con scenario tags */}
      <div style={S.header}>
        <div style={S.headerLeft}>
          <Sparkles size={14} style={{ color: "#a78bfa" }} />
          <span style={S.headerTitle}>Process Coach AI</span>
        </div>
        <div style={S.tagRow}>
          <Tag label="Categoria" value={tags.category} color="#a78bfa" />
          <Tag label="Regime" value={tags.regime} color={regimeColor(tags.regime)} />
          <Tag label="Asset class" value={tags.asset_class} color="#10b981" />
          <button style={S.iconBtn} onClick={() => setShowMemoryPanel(!showMemoryPanel)}
                  title="Memoria archiviata">
            <BookOpen size={14} />
            <span style={{ marginLeft: 4 }}>{existing.length}</span>
          </button>
          <button style={S.iconBtn} onClick={resetChat} title="Reset chat">
            <RefreshCw size={14} />
          </button>
        </div>
      </div>

      {/* Memoria archiviata panel (toggle) */}
      {showMemoryPanel && (
        <div style={S.memoryPanel}>
          <div style={S.memoryPanelTitle}>
            Memoria archiviata per questa categoria — {existing.length} advice
          </div>
          {existing.length === 0 && (
            <div style={S.emptyMem}>
              Nessun advice ancora salvato. Quelli che salvi qui sotto verranno
              iniettati automaticamente nei run futuri della stessa categoria.
            </div>
          )}
          {existing.map((a) => (
            <div key={a.id} style={S.memoryItem}>
              <div style={S.memoryItemHeader}>
                <strong style={{ color: "#e2e8f0", fontSize: 13 }}>{a.title}</strong>
                <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                  <span style={S.applyBadge}>
                    iniettato {a.apply_count || 0}×
                  </span>
                  <button style={S.deleteBtn}
                          onClick={() => deleteAdvice(state.category_key, a.id)}>
                    <Trash2 size={11} />
                  </button>
                </div>
              </div>
              <div style={S.memoryItemText}>{a.text}</div>
              {a.rationale && (
                <div style={S.memoryItemRationale}>
                  <em>Razionale:</em> {a.rationale}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Messaggi */}
      <div ref={scrollRef} style={S.messagesScroll}>
        {(state.messages || []).map((m, idx) => (
          <MessageBubble
            key={idx}
            message={m}
            msgIdx={idx}
            onSaveAdvice={saveAdvice}
            savedIds={savedIds}
            savingKey={savingKey}
          />
        ))}
        {sending && (
          <div style={{ ...S.bubbleAssistant, opacity: 0.7 }}>
            <Loader2 size={14} className="spin" style={{ marginRight: 8 }} />
            Sto pensando...
          </div>
        )}
      </div>

      {/* Input */}
      <div style={S.inputRow}>
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
              e.preventDefault();
              sendMessage();
            }
          }}
          placeholder="Chiedi chiarimenti o proponi modifiche ai consigli (Ctrl+Enter per inviare)..."
          style={S.textarea}
          disabled={sending}
        />
        <button
          style={{ ...S.sendBtn, opacity: sending || !input.trim() ? 0.5 : 1 }}
          onClick={sendMessage}
          disabled={sending || !input.trim()}
        >
          <Send size={14} /> Invia
        </button>
      </div>

      <style>{`
        .spin { animation: spin 1s linear infinite; }
        @keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
      `}</style>
    </div>
  );
}

// ─── Components interni ────────────────────────────────────────────────────

function MessageBubble({ message, msgIdx, onSaveAdvice, savedIds, savingKey }) {
  const isUser = message.role === "user";
  const advices = message.proposed_advices || [];

  // Strippa il blocco ```json ... ``` dal testo per la visualizzazione,
  // tanto i consigli sono renderizzati come card sotto.
  const displayContent = stripJsonBlocks(message.content || "");

  return (
    <div style={isUser ? S.bubbleUser : S.bubbleAssistant}>
      {!isUser && <Brain size={13} style={{ color: "#a78bfa", marginRight: 6, flexShrink: 0 }} />}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={S.bubbleText}>{displayContent}</div>

        {/* Cards con i consigli proposti */}
        {advices.length > 0 && (
          <div style={S.advicesGrid}>
            {advices.map((a, advIdx) => {
              const key = `${msgIdx}_${advIdx}`;
              const saved = !!savedIds[key];
              const saving = savingKey === key;
              return (
                <div key={advIdx} style={S.adviceCard}>
                  <div style={S.adviceTitle}>
                    <Sparkles size={12} style={{ color: "#fbbf24", marginRight: 6 }} />
                    {a.title || "Consiglio senza titolo"}
                  </div>
                  <div style={S.adviceText}>{a.text}</div>
                  {a.rationale && (
                    <div style={S.adviceRationale}>
                      <em>Razionale:</em> {a.rationale}
                    </div>
                  )}
                  <button
                    style={{
                      ...S.saveBtn,
                      background: saved ? "#10b981" : "#a78bfa",
                      cursor: saved || saving ? "default" : "pointer",
                    }}
                    onClick={() => !saved && !saving && onSaveAdvice(a, msgIdx, advIdx)}
                    disabled={saved || saving}
                  >
                    {saved ? (
                      <>
                        <Check size={12} /> Salvato in memoria
                      </>
                    ) : saving ? (
                      <>
                        <Loader2 size={12} className="spin" /> Salvo...
                      </>
                    ) : (
                      <>
                        <Save size={12} /> Salva in memoria
                      </>
                    )}
                  </button>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

function Tag({ label, value, color }) {
  if (!value) return null;
  return (
    <div style={{
      ...S.tag,
      borderColor: color, color,
    }}>
      <span style={{ color: "#64748b", marginRight: 4, textTransform: "uppercase", fontSize: 9 }}>
        {label}
      </span>
      <strong>{value}</strong>
    </div>
  );
}

// ─── Helpers ────────────────────────────────────────────────────────────

function stripJsonBlocks(text) {
  if (!text) return "";
  // Rimuovi ```json ... ```
  return text.replace(/```json[\s\S]*?```/g, "").trim();
}

function regimeColor(regime) {
  const map = {
    bull: "#10b981",
    bear: "#ef4444",
    neutral: "#94a3b8",
    volatile: "#fbbf24",
  };
  return map[regime] || "#94a3b8";
}

const S = {
  container: {
    display: "flex", flexDirection: "column", minHeight: 600,
  },
  header: {
    display: "flex", alignItems: "center", justifyContent: "space-between",
    padding: "8px 12px", background: "#0f172a", borderRadius: 6,
    border: "1px solid #1f2937", marginBottom: 12,
  },
  headerLeft: { display: "flex", alignItems: "center", gap: 8 },
  headerTitle: {
    fontSize: 13, fontWeight: 600, color: "#cbd5e1",
  },
  tagRow: { display: "flex", alignItems: "center", gap: 6 },
  tag: {
    fontSize: 11, padding: "3px 8px", borderRadius: 4,
    border: "1px solid", background: "#0a1018",
  },
  iconBtn: {
    background: "transparent", border: "1px solid #374151",
    color: "#94a3b8", padding: "4px 8px", borderRadius: 4,
    cursor: "pointer", fontSize: 11, display: "inline-flex",
    alignItems: "center", fontFamily: "inherit",
  },
  memoryPanel: {
    background: "#0a1018", border: "1px solid #1f2937",
    borderRadius: 6, padding: 12, marginBottom: 12,
    maxHeight: 300, overflowY: "auto",
  },
  memoryPanelTitle: {
    fontSize: 11, fontWeight: 600, color: "#a78bfa",
    textTransform: "uppercase", letterSpacing: "0.06em",
    marginBottom: 10,
  },
  emptyMem: {
    fontSize: 12, color: "#64748b", lineHeight: 1.6,
    padding: 12, textAlign: "center",
  },
  memoryItem: {
    background: "#111827", padding: 10, borderRadius: 6,
    marginBottom: 8, border: "1px solid #1f2937",
  },
  memoryItemHeader: {
    display: "flex", justifyContent: "space-between",
    alignItems: "center", marginBottom: 6,
  },
  memoryItemText: {
    fontSize: 12, color: "#cbd5e1", lineHeight: 1.5, marginBottom: 4,
  },
  memoryItemRationale: {
    fontSize: 11, color: "#94a3b8", lineHeight: 1.4, marginTop: 4,
  },
  applyBadge: {
    fontSize: 10, color: "#fbbf24", background: "#3f1d0f",
    padding: "2px 6px", borderRadius: 3,
  },
  deleteBtn: {
    background: "transparent", border: "1px solid #374151",
    color: "#ef4444", padding: "2px 6px", borderRadius: 3,
    cursor: "pointer", fontSize: 10, display: "inline-flex",
    alignItems: "center",
  },
  messagesScroll: {
    flex: 1, overflowY: "auto", maxHeight: 600,
    padding: "8px 4px", display: "flex", flexDirection: "column", gap: 12,
  },
  bubbleUser: {
    alignSelf: "flex-end", maxWidth: "85%",
    background: "#1e293b", color: "#e2e8f0",
    padding: "10px 14px", borderRadius: "10px 10px 2px 10px",
    fontSize: 13, lineHeight: 1.6, whiteSpace: "pre-wrap",
    border: "1px solid #334155",
  },
  bubbleAssistant: {
    alignSelf: "flex-start", maxWidth: "100%",
    background: "#0f172a", color: "#cbd5e1",
    padding: "12px 14px", borderRadius: "10px 10px 10px 2px",
    fontSize: 13, lineHeight: 1.6,
    border: "1px solid #1f2937",
    display: "flex", alignItems: "flex-start",
  },
  bubbleText: { whiteSpace: "pre-wrap", color: "#cbd5e1" },
  advicesGrid: {
    display: "grid", gridTemplateColumns: "1fr", gap: 10,
    marginTop: 12,
  },
  adviceCard: {
    background: "#1e1b3a", border: "1px solid #4c3d8a",
    borderLeft: "3px solid #a78bfa",
    padding: 12, borderRadius: 6,
  },
  adviceTitle: {
    fontSize: 13, fontWeight: 700, color: "#e2e8f0",
    display: "flex", alignItems: "center", marginBottom: 8,
  },
  adviceText: {
    fontSize: 12, color: "#cbd5e1", lineHeight: 1.6,
    marginBottom: 8, whiteSpace: "pre-wrap",
  },
  adviceRationale: {
    fontSize: 11, color: "#94a3b8", lineHeight: 1.5,
    marginBottom: 10, paddingTop: 6, borderTop: "1px dashed #374151",
  },
  saveBtn: {
    color: "#0a0e1a", border: 0, padding: "6px 12px",
    borderRadius: 4, fontSize: 12, fontWeight: 600,
    display: "inline-flex", alignItems: "center", gap: 6,
    fontFamily: "inherit",
  },
  inputRow: {
    display: "flex", gap: 8, marginTop: 12,
    padding: "8px 0",
  },
  textarea: {
    flex: 1, background: "#0a1018", border: "1px solid #1f2937",
    color: "#e2e8f0", padding: "10px 12px", borderRadius: 6,
    fontSize: 13, fontFamily: "inherit", resize: "vertical",
    minHeight: 60, maxHeight: 150, outline: "none", lineHeight: 1.5,
  },
  sendBtn: {
    background: "#a78bfa", color: "#0a0e1a", border: 0,
    padding: "10px 18px", borderRadius: 6, fontWeight: 600,
    cursor: "pointer", display: "inline-flex", alignItems: "center", gap: 6,
    fontFamily: "inherit", alignSelf: "flex-end",
  },
  btnSecondary: {
    background: "transparent", color: "#cbd5e1",
    border: "1px solid #374151", padding: "6px 14px", borderRadius: 6,
    cursor: "pointer", display: "inline-flex", alignItems: "center", gap: 6,
    fontSize: 13, fontFamily: "inherit",
  },
  loading: {
    padding: 30, textAlign: "center", color: "#94a3b8",
    fontSize: 13, display: "flex", alignItems: "center", justifyContent: "center",
  },
  errorBox: {
    padding: 24, textAlign: "center",
  },
};
