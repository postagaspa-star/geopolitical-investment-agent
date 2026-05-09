import { useEffect, useRef, useState } from "react";
import {
  MessageSquare, Send, Loader2, Trash2, AlertCircle, Check,
  TrendingUp, TrendingDown, Bot, User, Sparkles, RefreshCw, Bitcoin, BarChart3,
} from "lucide-react";

const API = window.location.origin;

const AGENT_TYPES = [
  {
    id: "standard",
    label: "Decision Standard",
    sublabel: "Equity / ETF · Claude Sonnet 4.5",
    icon: BarChart3,
    color: "#10b981",
    placeholder:
      "Chiedi una spiegazione, suggerisci una mossa, o discuti la strategia equity. Esempi:\n" +
      "• Perché hai comprato NVDA il 5 maggio?\n" +
      "• Considera di ridurre il tech del 20% questa settimana\n" +
      "• Compra 5 azioni QQQ se vedi setup buono",
  },
  {
    id: "crypto",
    label: "Decision Crypto",
    sublabel: "BTC / ETH / altcoin · DeepSeek-R1",
    icon: Bitcoin,
    color: "#f472b6",
    placeholder:
      "Chiedi della tua esposizione crypto, suggerisci una rotazione, o discuti del setup attuale. Esempi:\n" +
      "• Come valuti la posizione BTC ai livelli attuali?\n" +
      "• Voglio entrare su ETH se vede 0.618 fib\n" +
      "• Spiega perché il funding rate è bullish",
  },
];

function formatTime(iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    return d.toLocaleString("it-IT", {
      day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
    });
  } catch {
    return "";
  }
}

// Renderer markdown molto leggero — supporta **bold**, *italic*, `code`, line breaks, liste.
function renderMessage(text) {
  if (!text) return null;
  // Split per linee — ogni linea è un blocco
  return text.split("\n").map((line, i) => {
    // Headers ## / ###
    if (line.startsWith("### ")) {
      return (
        <h4 key={i} style={{ fontSize: "0.95rem", margin: "0.6rem 0 0.3rem", color: "#cbd5e1" }}>
          {line.slice(4)}
        </h4>
      );
    }
    if (line.startsWith("## ")) {
      return (
        <h3 key={i} style={{ fontSize: "1.05rem", margin: "0.7rem 0 0.4rem", color: "#e2e8f0" }}>
          {line.slice(3)}
        </h3>
      );
    }
    // Liste
    if (line.match(/^[-*]\s/)) {
      const content = applyInline(line.replace(/^[-*]\s/, ""));
      return (
        <div key={i} style={{ marginLeft: "1rem", lineHeight: 1.5 }}>
          • {content}
        </div>
      );
    }
    if (line.trim() === "") {
      return <div key={i} style={{ height: "0.4rem" }} />;
    }
    return (
      <div key={i} style={{ lineHeight: 1.5, marginBottom: "0.2rem" }}>
        {applyInline(line)}
      </div>
    );
  });
}

function applyInline(text) {
  // **bold**, *italic*, `code` — single-pass split
  const parts = [];
  let remaining = text;
  let key = 0;
  while (remaining.length > 0) {
    const boldMatch = remaining.match(/\*\*([^*]+)\*\*/);
    const codeMatch = remaining.match(/`([^`]+)`/);
    const italicMatch = remaining.match(/\*([^*]+)\*/);
    const matches = [boldMatch, codeMatch, italicMatch].filter(Boolean);
    if (matches.length === 0) {
      parts.push(remaining);
      break;
    }
    matches.sort((a, b) => a.index - b.index);
    const m = matches[0];
    if (m.index > 0) parts.push(remaining.slice(0, m.index));
    if (m === boldMatch) {
      parts.push(<strong key={key++}>{m[1]}</strong>);
    } else if (m === codeMatch) {
      parts.push(
        <code key={key++} style={{
          background: "#1e293b", padding: "1px 5px", borderRadius: "3px",
          fontSize: "0.85em", fontFamily: "monospace",
        }}>{m[1]}</code>
      );
    } else {
      parts.push(<em key={key++}>{m[1]}</em>);
    }
    remaining = remaining.slice(m.index + m[0].length);
  }
  return parts;
}

function ProposedTradeCard({ message, onExecute, executing, executedTradeId }) {
  const pt = message.proposed_trade;
  if (!pt) return null;
  const isBuy = (pt.action || "").toUpperCase() === "BUY";
  const Icon = isBuy ? TrendingUp : TrendingDown;
  const accentColor = isBuy ? "#10b981" : "#f87171";
  const isExecuted = !!message.executed_trade_id || !!executedTradeId;

  return (
    <div style={{
      marginTop: "0.6rem",
      background: "#0f172a",
      border: `1.5px solid ${accentColor}`,
      borderRadius: "10px",
      padding: "0.85rem 1rem",
      display: "flex",
      flexDirection: "column",
      gap: "0.65rem",
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
        <Icon size={18} color={accentColor} />
        <strong style={{ color: accentColor, fontSize: "0.95rem" }}>
          Proposta: {pt.action} {pt.quantity} {pt.ticker}
        </strong>
        {pt.confidence_level && (
          <span style={{
            marginLeft: "auto",
            fontSize: "0.7rem",
            background: "#1e293b",
            color: "#94a3b8",
            padding: "2px 8px",
            borderRadius: "10px",
            border: "1px solid #334155",
          }}>
            confidence: {pt.confidence_level}
          </span>
        )}
      </div>
      {pt.reasoning && (
        <div style={{ fontSize: "0.8rem", color: "#cbd5e1", lineHeight: 1.4 }}>
          {pt.reasoning}
        </div>
      )}
      {isExecuted ? (
        <div style={{
          display: "flex", alignItems: "center", gap: "0.4rem",
          color: "#10b981", fontSize: "0.85rem", fontWeight: 600,
          padding: "0.4rem 0.6rem",
          background: "rgba(16, 185, 129, 0.1)",
          borderRadius: "6px",
        }}>
          <Check size={14} /> Trade eseguito (ID #{message.executed_trade_id || executedTradeId})
        </div>
      ) : (
        <button
          onClick={() => onExecute(message.id)}
          disabled={executing}
          style={{
            background: accentColor,
            color: "#0f172a",
            border: "none",
            padding: "0.55rem 1rem",
            borderRadius: "6px",
            fontWeight: 600,
            fontSize: "0.9rem",
            cursor: executing ? "not-allowed" : "pointer",
            opacity: executing ? 0.6 : 1,
            display: "flex", alignItems: "center", justifyContent: "center", gap: "0.4rem",
          }}
        >
          {executing ? (
            <>
              <Loader2 size={14} className="spin" /> Esecuzione...
            </>
          ) : (
            <>
              <Check size={14} /> Conferma ed esegui
            </>
          )}
        </button>
      )}
    </div>
  );
}

function MessageBubble({ msg, agent, onExecuteTrade, executingMsgId, lastExecutedId }) {
  const isUser = msg.role === "user";
  const isErr = msg.role === "assistant" && (msg.content || "").startsWith("Errore tecnico:");

  return (
    <div style={{
      display: "flex",
      justifyContent: isUser ? "flex-end" : "flex-start",
      marginBottom: "0.85rem",
    }}>
      <div style={{
        maxWidth: "78%",
        background: isUser ? "#1e3a5f" : (isErr ? "#3a1e1e" : "#1e293b"),
        border: `1px solid ${isUser ? "#334155" : (isErr ? "#7f1d1d" : "#334155")}`,
        borderRadius: "12px",
        padding: "0.75rem 1rem",
        color: "#e2e8f0",
      }}>
        <div style={{
          display: "flex", alignItems: "center", gap: "0.4rem",
          fontSize: "0.7rem", color: "#94a3b8", marginBottom: "0.3rem",
        }}>
          {isUser ? <User size={12} /> : <Bot size={12} color={agent.color} />}
          <span>{isUser ? "Tu" : agent.label}</span>
          <span style={{ marginLeft: "auto" }}>{formatTime(msg.created_at)}</span>
        </div>
        <div style={{ fontSize: "0.88rem" }}>
          {renderMessage(msg.content)}
        </div>
        {!isUser && msg.proposed_trade && (
          <ProposedTradeCard
            message={msg}
            onExecute={onExecuteTrade}
            executing={executingMsgId === msg.id}
            executedTradeId={msg.id === executingMsgId ? lastExecutedId : null}
          />
        )}
      </div>
    </div>
  );
}

export default function ChatDecisionPage() {
  const [agentType, setAgentType] = useState("standard");
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [executingMsgId, setExecutingMsgId] = useState(null);
  const [lastExecutedId, setLastExecutedId] = useState(null);
  const scrollRef = useRef(null);

  const agent = AGENT_TYPES.find((a) => a.id === agentType) || AGENT_TYPES[0];

  // Load history on agent type change
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setMessages([]);
    (async () => {
      try {
        const res = await fetch(`${API}/api/chat-decision/history/${agentType}`);
        const data = await res.json();
        if (cancelled) return;
        if (!res.ok) {
          setError(data.error || `HTTP ${res.status}`);
          setMessages([]);
        } else {
          setMessages(data.messages || []);
        }
      } catch (e) {
        if (!cancelled) setError(String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [agentType]);

  // Auto-scroll to bottom on new message
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, sending]);

  const sendMessage = async () => {
    const msg = input.trim();
    if (!msg || sending) return;
    setSending(true);
    setError(null);
    // Optimistic update — aggiungi il msg utente subito
    const tempUserMsg = {
      id: `temp-${Date.now()}`,
      role: "user",
      content: msg,
      created_at: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, tempUserMsg]);
    setInput("");
    try {
      const res = await fetch(`${API}/api/chat-decision/send`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent_type: agentType, message: msg }),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || `HTTP ${res.status}`);
      }
      // Refresh full history (per avere ID definitivi e proposed_trade serializzati)
      const histRes = await fetch(`${API}/api/chat-decision/history/${agentType}`);
      const hist = await histRes.json();
      if (histRes.ok) {
        setMessages(hist.messages || []);
      }
    } catch (e) {
      setError(String(e.message || e));
      // Rimuovi il msg ottimistico se fallisce, ma lascia un placeholder errore
    } finally {
      setSending(false);
    }
  };

  const executeTrade = async (messageId) => {
    if (!window.confirm(
      "Confermi l'esecuzione di questo trade? L'operazione e' immediata e non puo' essere annullata."
    )) return;
    setExecutingMsgId(messageId);
    setError(null);
    try {
      const res = await fetch(`${API}/api/chat-decision/execute-trade`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message_id: messageId }),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || data.reason || `HTTP ${res.status}`);
      }
      setLastExecutedId(data.trade_id);
      // Refresh history per vedere lo stato "eseguito"
      const histRes = await fetch(`${API}/api/chat-decision/history/${agentType}`);
      const hist = await histRes.json();
      if (histRes.ok) {
        setMessages(hist.messages || []);
      }
    } catch (e) {
      setError(`Esecuzione fallita: ${e.message || e}`);
    } finally {
      setExecutingMsgId(null);
    }
  };

  const clearChat = async () => {
    if (!window.confirm(
      `Cancellare tutta la conversazione con ${agent.label}? L'operazione non puo' essere annullata.`
    )) return;
    try {
      await fetch(`${API}/api/chat-decision/clear/${agentType}`, { method: "DELETE" });
      setMessages([]);
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div style={{
      display: "flex",
      flexDirection: "column",
      height: "calc(100vh - 6rem)",
      maxWidth: "950px",
      margin: "0 auto",
    }}>
      <style>{`.spin { animation: spin 1s linear infinite; } @keyframes spin { to { transform: rotate(360deg); } }`}</style>
      {/* Header */}
      <div style={{
        display: "flex",
        alignItems: "center",
        gap: "0.75rem",
        marginBottom: "1rem",
        flexWrap: "wrap",
      }}>
        <MessageSquare size={26} color={agent.color} />
        <div style={{ flex: 1 }}>
          <h1 style={{ fontSize: "1.4rem", margin: 0, color: "#e2e8f0" }}>
            Chat con il Decision Agent
          </h1>
          <div style={{ fontSize: "0.78rem", color: "#94a3b8" }}>
            Conversa con l'agente che opera il portafoglio. Le tue direttive vengono ricordate
            e influenzano le decisioni autonome dei prossimi run.
          </div>
        </div>
        <button
          onClick={clearChat}
          title="Cancella conversazione"
          style={{
            background: "transparent",
            color: "#ef4444",
            border: "1px solid #7f1d1d",
            padding: "0.4rem 0.7rem",
            borderRadius: "6px",
            cursor: "pointer",
            display: "flex",
            alignItems: "center",
            gap: "0.3rem",
            fontSize: "0.78rem",
          }}>
          <Trash2 size={13} /> Cancella
        </button>
      </div>

      {/* Tabs */}
      <div style={{
        display: "flex",
        gap: "0.5rem",
        marginBottom: "1rem",
      }}>
        {AGENT_TYPES.map((a) => {
          const Icon = a.icon;
          const isActive = agentType === a.id;
          return (
            <button
              key={a.id}
              onClick={() => setAgentType(a.id)}
              style={{
                flex: 1,
                background: isActive ? a.color : "#1e293b",
                color: isActive ? "#0f172a" : "#cbd5e1",
                border: `1px solid ${isActive ? a.color : "#334155"}`,
                padding: "0.7rem 1rem",
                borderRadius: "8px",
                cursor: "pointer",
                display: "flex",
                alignItems: "center",
                gap: "0.6rem",
                fontWeight: isActive ? 700 : 500,
                fontSize: "0.9rem",
              }}>
              <Icon size={16} />
              <div style={{ textAlign: "left", flex: 1 }}>
                <div>{a.label}</div>
                <div style={{
                  fontSize: "0.68rem",
                  fontWeight: 400,
                  opacity: isActive ? 0.85 : 0.6,
                }}>
                  {a.sublabel}
                </div>
              </div>
            </button>
          );
        })}
      </div>

      {/* Messages thread */}
      <div ref={scrollRef} style={{
        flex: 1,
        overflowY: "auto",
        background: "#0f172a",
        border: "1px solid #1e293b",
        borderRadius: "10px",
        padding: "1rem",
        marginBottom: "0.75rem",
        minHeight: "300px",
      }}>
        {loading ? (
          <div style={{ display: "flex", justifyContent: "center", padding: "3rem", color: "#64748b" }}>
            <Loader2 size={20} className="spin" />
          </div>
        ) : error ? (
          <div style={{
            background: "#3a1e1e",
            border: "1px solid #7f1d1d",
            color: "#fca5a5",
            padding: "0.7rem 1rem",
            borderRadius: "8px",
            display: "flex",
            alignItems: "flex-start",
            gap: "0.5rem",
            fontSize: "0.85rem",
          }}>
            <AlertCircle size={16} style={{ flexShrink: 0, marginTop: "2px" }} />
            <div>{error}</div>
          </div>
        ) : messages.length === 0 ? (
          <div style={{
            color: "#64748b",
            textAlign: "center",
            padding: "3rem 1rem",
            fontSize: "0.9rem",
          }}>
            <Sparkles size={32} style={{ opacity: 0.4, marginBottom: "0.7rem" }} />
            <div style={{ marginBottom: "0.5rem", color: "#94a3b8", fontWeight: 500 }}>
              Nessuna conversazione ancora con {agent.label}
            </div>
            <div style={{ whiteSpace: "pre-line", fontSize: "0.78rem", maxWidth: "500px", margin: "0 auto" }}>
              {agent.placeholder}
            </div>
          </div>
        ) : (
          messages.map((m) => (
            <MessageBubble
              key={m.id}
              msg={m}
              agent={agent}
              onExecuteTrade={executeTrade}
              executingMsgId={executingMsgId}
              lastExecutedId={lastExecutedId}
            />
          ))
        )}
        {sending && (
          <div style={{
            display: "flex",
            justifyContent: "flex-start",
            marginBottom: "0.85rem",
          }}>
            <div style={{
              background: "#1e293b",
              border: "1px solid #334155",
              borderRadius: "12px",
              padding: "0.75rem 1rem",
              color: "#94a3b8",
              fontSize: "0.85rem",
              display: "flex",
              alignItems: "center",
              gap: "0.5rem",
            }}>
              <Loader2 size={14} className="spin" /> {agent.label} sta pensando...
            </div>
          </div>
        )}
      </div>

      {/* Input */}
      <div style={{ display: "flex", gap: "0.5rem", alignItems: "flex-end" }}>
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              sendMessage();
            }
          }}
          placeholder={`Scrivi a ${agent.label}... (Enter per inviare, Shift+Enter per a capo)`}
          disabled={sending}
          rows={2}
          style={{
            flex: 1,
            background: "#0f172a",
            color: "#e2e8f0",
            border: `1px solid ${input.trim() ? agent.color : "#334155"}`,
            borderRadius: "8px",
            padding: "0.7rem 0.9rem",
            fontSize: "0.9rem",
            fontFamily: "inherit",
            resize: "vertical",
            outline: "none",
            transition: "border-color 0.15s",
          }}
        />
        <button
          onClick={sendMessage}
          disabled={!input.trim() || sending}
          style={{
            background: input.trim() && !sending ? agent.color : "#334155",
            color: input.trim() && !sending ? "#0f172a" : "#64748b",
            border: "none",
            padding: "0.7rem 1.1rem",
            borderRadius: "8px",
            cursor: input.trim() && !sending ? "pointer" : "not-allowed",
            display: "flex",
            alignItems: "center",
            gap: "0.4rem",
            fontWeight: 600,
            fontSize: "0.9rem",
          }}>
          {sending ? <Loader2 size={14} className="spin" /> : <Send size={14} />}
          Invia
        </button>
      </div>
    </div>
  );
}
