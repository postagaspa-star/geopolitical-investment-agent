import { useState, useEffect, useRef } from "react";
import { MessageCircle, X, Send, Trash2, Plus, History } from "lucide-react";

const API = window.location.origin;

/**
 * ChatWidget — pulsante flottante in basso a destra che apre una chat con
 * l'analista AI (DeepSeek-R1). L'utente seleziona N decisioni del Decision
 * Agent (standard + crypto) come contesto e dialoga sull'analisi.
 *
 * Mini-memoria: il backend conserva solo le ultime 10 conversazioni.
 */
export default function ChatWidget() {
  const [open, setOpen] = useState(false);
  const [view, setView] = useState("chat"); // 'chat' | 'history' | 'select'
  const [conversations, setConversations] = useState([]);
  const [activeConvId, setActiveConvId] = useState(null);
  const [messages, setMessages] = useState([]);
  const [decisions, setDecisions] = useState([]);
  const [selectedIds, setSelectedIds] = useState([]);
  const [inputText, setInputText] = useState("");
  const [sending, setSending] = useState(false);
  const messagesEndRef = useRef(null);

  // Carica conversazioni e decisioni quando il widget si apre
  useEffect(() => {
    if (!open) return;
    loadConversations();
    loadDecisions();
  }, [open]);

  // Auto-scroll on new message
  useEffect(() => {
    if (messagesEndRef.current) {
      messagesEndRef.current.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages, sending]);

  const loadConversations = async () => {
    try {
      const res = await fetch(`${API}/api/chat/conversations?limit=10`);
      if (res.ok) {
        const data = await res.json();
        setConversations(Array.isArray(data) ? data : []);
      }
    } catch (e) { console.error("loadConversations:", e); }
  };

  const loadDecisions = async () => {
    try {
      const res = await fetch(`${API}/api/chat/decisions?limit=30`);
      if (res.ok) {
        const data = await res.json();
        setDecisions(Array.isArray(data) ? data : []);
      }
    } catch (e) { console.error("loadDecisions:", e); }
  };

  const loadConversation = async (convId) => {
    try {
      const res = await fetch(`${API}/api/chat/conversations/${convId}/messages`);
      if (res.ok) {
        const data = await res.json();
        setMessages(data.messages || []);
        setActiveConvId(convId);
        // Recupera selected_decisions dalla lista conversazioni
        const conv = conversations.find(c => c.id === convId);
        if (conv?.selected_decisions) {
          try {
            setSelectedIds(JSON.parse(conv.selected_decisions) || []);
          } catch { setSelectedIds([]); }
        } else {
          setSelectedIds([]);
        }
        setView("chat");
      }
    } catch (e) { console.error("loadConversation:", e); }
  };

  const newConversation = () => {
    setActiveConvId(null);
    setMessages([]);
    setSelectedIds([]);
    setInputText("");
    setView("chat");
  };

  const deleteConversation = async (convId, e) => {
    e.stopPropagation();
    if (!window.confirm("Eliminare questa conversazione?")) return;
    try {
      await fetch(`${API}/api/chat/conversations/${convId}`, { method: "DELETE" });
      if (activeConvId === convId) newConversation();
      loadConversations();
    } catch (e) { console.error("deleteConversation:", e); }
  };

  const toggleDecision = (id) => {
    setSelectedIds(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]);
  };

  const sendMessage = async () => {
    const msg = inputText.trim();
    if (!msg || sending) return;

    setSending(true);
    setInputText("");
    // Optimistic UI: mostra subito il messaggio user
    setMessages(prev => [...prev, { role: "user", content: msg, created_at: new Date().toISOString() }]);

    try {
      const res = await fetch(`${API}/api/chat/send`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          conversation_id: activeConvId,
          message: msg,
          selected_trade_ids: selectedIds.length > 0 ? selectedIds : null,
        }),
      });

      // Leggi sempre il body, anche su errore: il backend ritorna detail strutturato
      let data;
      try {
        data = await res.json();
      } catch {
        data = { error: `HTTP ${res.status} (response non JSON)` };
      }

      if (!res.ok) {
        // Costruisci messaggio di errore leggibile dal detail backend
        const parts = [];
        if (data.error) parts.push(data.error);
        if (data.detail) parts.push(`Dettaglio: ${data.detail}`);
        if (data.hint) parts.push(`Suggerimento: ${data.hint}`);
        if (data.type) parts.push(`Tipo: ${data.type}`);
        const fullErr = parts.length > 0
          ? parts.join("\n")
          : `HTTP ${res.status}`;
        throw new Error(fullErr);
      }

      if (data.is_new && data.conversation_id) {
        setActiveConvId(data.conversation_id);
        loadConversations();
      }
      setMessages(prev => [...prev, {
        role: "assistant",
        content: data.reply || "(nessuna risposta)",
        created_at: new Date().toISOString(),
      }]);
    } catch (e) {
      setMessages(prev => [...prev, {
        role: "assistant",
        content: `⚠ Errore chat:\n${e.message}`,
        created_at: new Date().toISOString(),
      }]);
    } finally {
      setSending(false);
    }
  };

  const onKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  };

  return (
    <>
      {/* Pulsante flottante */}
      {!open && (
        <button onClick={() => setOpen(true)} style={S.fab} title="Apri chat con analista AI">
          <MessageCircle size={24} />
        </button>
      )}

      {/* Pannello chat */}
      {open && (
        <div style={S.panel}>
          {/* Header */}
          <div style={S.header}>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <MessageCircle size={18} style={{ color: "#a78bfa" }} />
              <strong style={{ fontSize: 14 }}>Analista AI</strong>
              <span style={S.badge}>R1</span>
            </div>
            <div style={{ display: "flex", gap: 6 }}>
              <button onClick={newConversation} style={S.iconBtn} title="Nuova conversazione">
                <Plus size={16} />
              </button>
              <button onClick={() => setView(view === "history" ? "chat" : "history")}
                      style={{...S.iconBtn, background: view === "history" ? "#1f2937" : "transparent"}}
                      title="Cronologia">
                <History size={16} />
              </button>
              <button onClick={() => setOpen(false)} style={S.iconBtn} title="Chiudi">
                <X size={16} />
              </button>
            </div>
          </div>

          {/* Body */}
          {view === "history" ? (
            <HistoryView
              conversations={conversations}
              activeId={activeConvId}
              onSelect={loadConversation}
              onDelete={deleteConversation}
            />
          ) : view === "select" ? (
            <SelectView
              decisions={decisions}
              selectedIds={selectedIds}
              onToggle={toggleDecision}
              onClear={() => setSelectedIds([])}
              onDone={() => setView("chat")}
            />
          ) : (
            <ChatView
              messages={messages}
              sending={sending}
              selectedIds={selectedIds}
              decisionsCount={decisions.length}
              onOpenSelect={() => setView("select")}
              messagesEndRef={messagesEndRef}
            />
          )}

          {/* Input bar (solo in chat view) */}
          {view === "chat" && (
            <div style={S.inputBar}>
              <textarea
                value={inputText}
                onChange={(e) => setInputText(e.target.value)}
                onKeyDown={onKeyDown}
                placeholder={selectedIds.length > 0
                  ? `Chiedi qualcosa sui ${selectedIds.length} trade selezionati...`
                  : "Chiedi all'analista AI..."}
                style={S.textarea}
                rows={2}
                disabled={sending}
              />
              <button onClick={sendMessage} disabled={sending || !inputText.trim()} style={S.sendBtn}>
                <Send size={16} />
              </button>
            </div>
          )}
        </div>
      )}
    </>
  );
}

// ─── Sub-views ──────────────────────────────────────────────────────────

function ChatView({ messages, sending, selectedIds, decisionsCount, onOpenSelect, messagesEndRef }) {
  return (
    <div style={S.body}>
      {/* Selezione decisioni */}
      <div style={S.contextBar}>
        <button onClick={onOpenSelect} style={S.contextBtn}>
          📊 Seleziona decisioni ({selectedIds.length}/{decisionsCount})
        </button>
        {selectedIds.length > 0 && (
          <span style={{ fontSize: 11, color: "#10b981" }}>
            {selectedIds.length} trade nel contesto
          </span>
        )}
      </div>

      {/* Messaggi */}
      <div style={S.messagesArea}>
        {messages.length === 0 ? (
          <div style={S.emptyState}>
            <MessageCircle size={32} style={{ color: "#374151", marginBottom: 12 }} />
            <div style={{ fontSize: 13, color: "#94a3b8", marginBottom: 8 }}>
              Chatta con l'analista AI sulle decisioni del Decision Agent.
            </div>
            <div style={{ fontSize: 11, color: "#64748b", lineHeight: 1.6 }}>
              Esempi:<br />
              • "Perché ha comprato NVDA?"<br />
              • "C'è un pattern nei trade falliti?"<br />
              • "Il bot crypto rischia troppo?"
            </div>
          </div>
        ) : (
          messages.map((m, i) => <MessageBubble key={i} msg={m} />)
        )}
        {sending && (
          <div style={{ ...S.bubbleAssistant, fontStyle: "italic", color: "#94a3b8" }}>
            <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
              <div style={S.dot} />
              <div style={{ ...S.dot, animationDelay: "0.2s" }} />
              <div style={{ ...S.dot, animationDelay: "0.4s" }} />
              <span style={{ marginLeft: 8 }}>R1 sta ragionando...</span>
            </div>
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>
    </div>
  );
}

function MessageBubble({ msg }) {
  const isUser = msg.role === "user";
  return (
    <div style={isUser ? S.bubbleUser : S.bubbleAssistant}>
      <div style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
        {msg.content}
      </div>
    </div>
  );
}

function HistoryView({ conversations, activeId, onSelect, onDelete }) {
  return (
    <div style={S.body}>
      <div style={{ padding: "8px 12px", fontSize: 11, color: "#64748b", borderBottom: "1px solid #1f2937" }}>
        Ultime {conversations.length} conversazioni (max 10)
      </div>
      <div style={S.messagesArea}>
        {conversations.length === 0 ? (
          <div style={S.emptyState}>
            <div style={{ fontSize: 13, color: "#94a3b8" }}>Nessuna conversazione precedente.</div>
          </div>
        ) : (
          conversations.map(c => (
            <div
              key={c.id}
              onClick={() => onSelect(c.id)}
              style={{
                ...S.convItem,
                background: c.id === activeId ? "#1e293b" : "#0f172a",
              }}
            >
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 13, color: "#e2e8f0", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {c.title}
                </div>
                <div style={{ fontSize: 10, color: "#64748b", marginTop: 2 }}>
                  {timeAgo(c.updated_at)}
                </div>
              </div>
              <button onClick={(e) => onDelete(c.id, e)} style={S.deleteBtn} title="Elimina">
                <Trash2 size={12} />
              </button>
            </div>
          ))
        )}
      </div>
    </div>
  );
}

function SelectView({ decisions, selectedIds, onToggle, onClear, onDone }) {
  return (
    <div style={S.body}>
      <div style={{ padding: "8px 12px", fontSize: 11, color: "#94a3b8", borderBottom: "1px solid #1f2937", display: "flex", justifyContent: "space-between" }}>
        <span>Seleziona i trade da analizzare ({selectedIds.length} selezionati)</span>
        <div style={{ display: "flex", gap: 8 }}>
          {selectedIds.length > 0 && (
            <button onClick={onClear} style={{ ...S.smallBtn, color: "#ef4444" }}>Pulisci</button>
          )}
          <button onClick={onDone} style={{ ...S.smallBtn, color: "#10b981" }}>OK</button>
        </div>
      </div>
      <div style={S.messagesArea}>
        {decisions.length === 0 ? (
          <div style={S.emptyState}>
            <div style={{ fontSize: 13, color: "#94a3b8" }}>Nessun trade disponibile.</div>
          </div>
        ) : (
          decisions.map(d => (
            <label
              key={d.id}
              style={{
                ...S.decisionItem,
                background: selectedIds.includes(d.id) ? "#1e293b" : "#0f172a",
                borderColor: selectedIds.includes(d.id) ? "#a78bfa" : "#1f2937",
              }}
            >
              <input
                type="checkbox"
                checked={selectedIds.includes(d.id)}
                onChange={() => onToggle(d.id)}
                style={{ marginRight: 8 }}
              />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12 }}>
                  <span style={{
                    color: d.action === "BUY" ? "#10b981" : "#ef4444",
                    fontWeight: 600,
                  }}>{d.action}</span>
                  <span style={{ color: "#e2e8f0", fontFamily: "monospace" }}>{d.ticker}</span>
                  {d.is_crypto && <span style={S.cryptoTag}>crypto</span>}
                  <span style={{ color: "#94a3b8", marginLeft: "auto" }}>
                    {d.quantity} @ ${d.price?.toFixed(2)}
                  </span>
                </div>
                <div style={{ fontSize: 10, color: "#64748b", marginTop: 2 }}>
                  {new Date(d.timestamp).toLocaleString("it-IT")}
                  {d.confidence != null && <span style={{ marginLeft: 8 }}>conf {Math.round(d.confidence)}%</span>}
                </div>
              </div>
            </label>
          ))
        )}
      </div>
    </div>
  );
}

// ─── Helpers ────────────────────────────────────────────────────────────

function timeAgo(iso) {
  if (!iso) return "?";
  const diff = Date.now() - new Date(iso).getTime();
  const m = Math.floor(diff / 60000);
  if (m < 1) return "ora";
  if (m < 60) return `${m}m fa`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h fa`;
  return `${Math.floor(h / 24)}g fa`;
}

// ─── Styles ─────────────────────────────────────────────────────────────

const S = {
  fab: {
    position: "fixed", bottom: 24, right: 24, width: 56, height: 56,
    borderRadius: "50%", border: "none",
    background: "linear-gradient(135deg, #a78bfa 0%, #6366f1 100%)",
    color: "#fff", cursor: "pointer",
    boxShadow: "0 4px 16px rgba(99, 102, 241, 0.5)",
    display: "flex", alignItems: "center", justifyContent: "center",
    zIndex: 9998, transition: "transform 0.15s",
  },
  panel: {
    position: "fixed", bottom: 24, right: 24,
    width: 400, maxWidth: "calc(100vw - 32px)",
    height: 580, maxHeight: "calc(100vh - 48px)",
    background: "#0a0f1c", border: "1px solid #1f2937",
    borderRadius: 12, boxShadow: "0 8px 32px rgba(0,0,0,0.6)",
    display: "flex", flexDirection: "column", zIndex: 9999,
    overflow: "hidden",
  },
  header: {
    display: "flex", justifyContent: "space-between", alignItems: "center",
    padding: "10px 14px", borderBottom: "1px solid #1f2937",
    background: "#111827", color: "#e2e8f0",
  },
  badge: {
    padding: "1px 6px", background: "#831843", color: "#fce7f3",
    borderRadius: 3, fontSize: 9, fontWeight: 600, fontFamily: "monospace",
  },
  iconBtn: {
    background: "transparent", border: "none", color: "#94a3b8",
    cursor: "pointer", padding: 4, borderRadius: 4,
    display: "flex", alignItems: "center", justifyContent: "center",
  },
  body: { flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" },
  contextBar: {
    display: "flex", justifyContent: "space-between", alignItems: "center",
    padding: "8px 12px", borderBottom: "1px solid #1f2937",
    background: "#0f172a",
  },
  contextBtn: {
    background: "#1e293b", color: "#cbd5e1", border: "1px solid #334155",
    padding: "5px 10px", borderRadius: 4, fontSize: 11, cursor: "pointer",
  },
  messagesArea: {
    flex: 1, overflowY: "auto", padding: "12px",
    display: "flex", flexDirection: "column", gap: 8,
  },
  emptyState: {
    flex: 1, display: "flex", flexDirection: "column",
    justifyContent: "center", alignItems: "center", textAlign: "center",
    padding: 20,
  },
  bubbleUser: {
    alignSelf: "flex-end", background: "#6366f1", color: "#fff",
    padding: "8px 12px", borderRadius: "12px 12px 2px 12px",
    maxWidth: "85%", fontSize: 13, lineHeight: 1.5,
  },
  bubbleAssistant: {
    alignSelf: "flex-start", background: "#1e293b", color: "#e2e8f0",
    padding: "8px 12px", borderRadius: "12px 12px 12px 2px",
    maxWidth: "85%", fontSize: 13, lineHeight: 1.5,
  },
  inputBar: {
    display: "flex", gap: 6, padding: 10,
    borderTop: "1px solid #1f2937", background: "#0f172a",
  },
  textarea: {
    flex: 1, background: "#1e293b", border: "1px solid #334155",
    color: "#e2e8f0", padding: "6px 10px", borderRadius: 6, fontSize: 13,
    resize: "none", fontFamily: "inherit", outline: "none",
  },
  sendBtn: {
    background: "#6366f1", color: "#fff", border: "none",
    padding: "0 12px", borderRadius: 6, cursor: "pointer",
    display: "flex", alignItems: "center", justifyContent: "center",
  },
  convItem: {
    display: "flex", alignItems: "center", gap: 8,
    padding: "10px 12px", borderRadius: 6, cursor: "pointer",
    border: "1px solid #1f2937",
  },
  deleteBtn: {
    background: "transparent", border: "none", color: "#94a3b8",
    cursor: "pointer", padding: 4, borderRadius: 4,
    display: "flex", alignItems: "center",
  },
  decisionItem: {
    display: "flex", alignItems: "center", padding: "8px 10px",
    borderRadius: 6, cursor: "pointer", border: "1px solid",
  },
  cryptoTag: {
    padding: "1px 5px", background: "#831843", color: "#fce7f3",
    borderRadius: 3, fontSize: 9, fontWeight: 600,
  },
  smallBtn: {
    background: "transparent", border: "none", fontSize: 11,
    cursor: "pointer", padding: "2px 6px",
  },
  dot: {
    width: 6, height: 6, borderRadius: "50%", background: "#94a3b8",
    animation: "pulse 1.4s infinite",
  },
};
