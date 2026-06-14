import { useEffect, useRef, useState } from "react";
import {
  MessageSquare, Send, Loader2, Trash2, AlertCircle, Check,
  TrendingUp, TrendingDown, Bot, User, Sparkles, RefreshCw, Bitcoin, BarChart3,
  Target, X as XIcon, Clock, Shield, Award, FileText,
} from "lucide-react";

const API = window.location.origin;

/**
 * Safe fetch+parse JSON: gestisce response vuote, errori proxy (502/504 con
 * body HTML), e response truncate da timeout backend.
 *
 * Bug originale: res.json() crash con "Unexpected end of JSON input" quando
 * il backend taglia la response (timeout Cloudflare/Render dopo 30-60s) o
 * il proxy ritorna pagina di errore HTML invece di JSON. Tipico con il
 * second-round technical agent della chat decision che puo' prendere 20-30s.
 *
 * Ritorna { ok: bool, status: number, data: object | null, error?: string }.
 */
async function safeFetchJson(url, opts = {}, timeoutMs = 120000) {
  const ctrl = new AbortController();
  // Combina abort signal esterno con il timer interno
  const externalSignal = opts.signal;
  if (externalSignal) {
    if (externalSignal.aborted) ctrl.abort();
    else externalSignal.addEventListener("abort", () => ctrl.abort(), { once: true });
  }
  const timer = setTimeout(() => ctrl.abort(new Error("client timeout")), timeoutMs);
  try {
    const res = await fetch(url, { ...opts, signal: ctrl.signal });
    const text = await res.text();
    if (!text || !text.trim()) {
      return {
        ok: false, status: res.status, data: null,
        error: `Risposta vuota dal server (HTTP ${res.status}). Probabile timeout backend o errore proxy.`,
      };
    }
    let data;
    try {
      data = JSON.parse(text);
    } catch (e) {
      // Body non-JSON (es. pagina HTML di errore proxy)
      return {
        ok: false, status: res.status, data: null,
        error: `Risposta non valida (HTTP ${res.status}): ${text.slice(0, 100)}`,
      };
    }
    return { ok: res.ok, status: res.status, data };
  } catch (e) {
    if (e.name === "AbortError") {
      return { ok: false, status: 0, data: null, error: "Richiesta annullata (timeout o abort)" };
    }
    return { ok: false, status: 0, data: null, error: String(e.message || e) };
  } finally {
    clearTimeout(timer);
  }
}

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

// ────────────────────────────────────────────────────────────────────────────
// PROPOSED ACTIONS — nuova UI multi-action (trade / stop-loss / take-profit / direttive)
// ────────────────────────────────────────────────────────────────────────────
function actionMeta(action) {
  const t = (action.type || "").toLowerCase();
  if (t === "execute_trade") {
    const isBuy = (action.action || "").toUpperCase() === "BUY";
    return {
      icon: isBuy ? TrendingUp : TrendingDown,
      color: isBuy ? "#10b981" : "#f87171",
      label: `${action.action} ${action.quantity} ${action.ticker}`,
      kind: "Trade",
    };
  }
  if (t === "set_stop_loss") {
    const target = action.stop_loss_price != null
      ? `prezzo $${action.stop_loss_price}`
      : `${action.stop_loss_pct}%`;
    return {
      icon: Shield,
      color: "#fb923c",
      label: `Stop-loss ${action.ticker} → ${target}`,
      kind: "Stop-Loss",
    };
  }
  if (t === "set_take_profit") {
    const target = action.take_profit_price != null
      ? `prezzo $${action.take_profit_price}`
      : `+${action.take_profit_pct}%`;
    return {
      icon: Award,
      color: "#34d399",
      label: `Take-profit ${action.ticker} → ${target}`,
      kind: "Take-Profit",
    };
  }
  if (t === "add_directive") {
    return {
      icon: FileText,
      color: "#a78bfa",
      label: "Nuova direttiva utente",
      kind: "Direttiva",
    };
  }
  return {
    icon: AlertCircle, color: "#64748b",
    label: action.type || "azione sconosciuta", kind: "Sconosciuto",
  };
}

function ProposedActionCard({
  message, action, index, onExecute, executing, executedResult,
}) {
  const meta = actionMeta(action);
  const Icon = meta.icon;
  const ok = !!(executedResult && executedResult.ok);
  const failed = !!(executedResult && executedResult.ok === false);

  return (
    <div style={{
      marginTop: "0.6rem",
      background: "#0f172a",
      border: `1.5px solid ${meta.color}`,
      borderRadius: "10px",
      padding: "0.85rem 1rem",
      display: "flex",
      flexDirection: "column",
      gap: "0.65rem",
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
        <Icon size={18} color={meta.color} />
        <strong style={{ color: meta.color, fontSize: "0.95rem" }}>
          {meta.kind}: {meta.label}
        </strong>
        {action.confidence_level && (
          <span style={{
            marginLeft: "auto", fontSize: "0.7rem",
            background: "#1e293b", color: "#94a3b8",
            padding: "2px 8px", borderRadius: "10px", border: "1px solid #334155",
          }}>
            conf: {action.confidence_level}
          </span>
        )}
      </div>

      {action.type === "add_directive" && action.text && (
        <div style={{
          fontSize: "0.85rem", color: "#e2e8f0",
          background: "#1e293b", border: "1px solid #334155",
          padding: "0.5rem 0.7rem", borderRadius: "6px",
          fontStyle: "italic",
        }}>
          "{action.text}"
        </div>
      )}

      {action.reasoning && (
        <div style={{ fontSize: "0.8rem", color: "#cbd5e1", lineHeight: 1.4 }}>
          {action.reasoning}
        </div>
      )}

      {ok ? (
        <div style={{
          display: "flex", alignItems: "center", gap: "0.4rem",
          color: "#10b981", fontSize: "0.85rem", fontWeight: 600,
          padding: "0.4rem 0.6rem",
          background: "rgba(16, 185, 129, 0.1)", borderRadius: "6px",
        }}>
          <Check size={14} /> Eseguita
          {executedResult.trade_id && ` (trade #${executedResult.trade_id})`}
          {executedResult.stop_loss_price && ` — stop a $${executedResult.stop_loss_price}`}
          {executedResult.take_profit_price && ` — target $${executedResult.take_profit_price}`}
        </div>
      ) : failed ? (
        <div style={{
          display: "flex", alignItems: "flex-start", gap: "0.4rem",
          color: "#f87171", fontSize: "0.82rem",
          padding: "0.4rem 0.6rem",
          background: "rgba(248, 113, 113, 0.1)", borderRadius: "6px",
        }}>
          <AlertCircle size={14} style={{ flexShrink: 0, marginTop: 1 }} />
          <span>Fallita: {executedResult.error || "errore sconosciuto"}</span>
        </div>
      ) : (
        <button
          onClick={() => onExecute(message.id, index)}
          disabled={executing}
          style={{
            background: meta.color, color: "#0f172a", border: "none",
            padding: "0.55rem 1rem", borderRadius: "6px",
            fontWeight: 600, fontSize: "0.9rem",
            cursor: executing ? "not-allowed" : "pointer",
            opacity: executing ? 0.6 : 1,
            display: "flex", alignItems: "center", justifyContent: "center", gap: "0.4rem",
          }}>
          {executing ? (
            <><Loader2 size={14} className="spin" /> Esecuzione...</>
          ) : (
            <><Check size={14} /> Conferma ed esegui</>
          )}
        </button>
      )}
    </div>
  );
}


function MessageBubble({ msg, agent, onExecuteTrade, onExecuteAction, executingMsgId, executingActionIndex, lastExecutedId }) {
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

        {/* NUOVA UI: proposed_actions[] — supporta trade + stop-loss + take-profit + direttive */}
        {!isUser && Array.isArray(msg.proposed_actions) && msg.proposed_actions.length > 0 && (
          msg.proposed_actions.map((a, idx) => (
            <ProposedActionCard
              key={idx}
              message={msg}
              action={a}
              index={idx}
              onExecute={onExecuteAction}
              executing={executingMsgId === msg.id && executingActionIndex === idx}
              executedResult={(msg.executed_action_results || {})[String(idx)]}
            />
          ))
        )}

        {/* LEGACY UI: solo se nessuna proposed_actions ma c'e' proposed_trade (msg vecchi nel DB) */}
        {!isUser && (!Array.isArray(msg.proposed_actions) || msg.proposed_actions.length === 0)
          && msg.proposed_trade && (
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

// ────────────────────────────────────────────────────────────────────────────
// Pannello "OBIETTIVI ATTIVI" — mostra impegni del Decision Agent (memoria persistente)
// ────────────────────────────────────────────────────────────────────────────
function CommitmentsPanel({ agentType, accentColor }) {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [collapsed, setCollapsed] = useState(false);
  const [cancellingId, setCancellingId] = useState(null);

  const loadItems = async () => {
    setLoading(true);
    try {
      const r = await safeFetchJson(`${API}/api/commitments/${agentType}?status=active`);
      if (r.ok && r.data) setItems(r.data.items || []);
    } catch {
      /* ignore */
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadItems();
    // refresh ogni 60s in background.
    // NOTA: loadItems usa agentType via closure; ricreare l'effect ad ogni
    // cambio di agentType e' il comportamento corretto (cambia categoria →
    // ricarica). Non includiamo loadItems nelle deps perche' verrebbe
    // ricreata ad ogni render dei sibling state, causando reset del setInterval.
    const id = setInterval(loadItems, 60000);
    return () => clearInterval(id);
  }, [agentType]);

  const cancelItem = async (id) => {
    if (!window.confirm("Cancellare questo obiettivo? L'agente non lo vedra' piu' nei prossimi run.")) return;
    setCancellingId(id);
    try {
      await fetch(`${API}/api/commitments/${id}/cancel`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason: "cancellato dalla UI" }),
      });
      await loadItems();
    } finally {
      setCancellingId(null);
    }
  };

  const formatExpiry = (iso) => {
    if (!iso) return "no scadenza";
    try {
      const d = new Date(iso);
      const diffMs = d - new Date();
      if (diffMs <= 0) return "scaduto";
      const hrs = diffMs / 3600000;
      if (hrs < 1) return `${Math.round(hrs * 60)}m`;
      if (hrs < 48) return `${hrs.toFixed(0)}h`;
      return `${Math.round(hrs / 24)}g`;
    } catch {
      return "?";
    }
  };

  const typeLabels = {
    monitor: "Monitor",
    conditional_buy: "BUY se",
    conditional_sell: "SELL se",
    watch_event: "Evento",
    reminder: "Reminder",
  };

  if (!loading && items.length === 0) {
    return null; // niente da mostrare → nascondi del tutto
  }

  return (
    <div style={{
      background: "#0b1424",
      border: "1px solid #1e293b",
      borderRadius: "10px",
      padding: "0.7rem 1rem",
      marginBottom: "0.75rem",
    }}>
      <div
        onClick={() => setCollapsed((c) => !c)}
        style={{
          display: "flex", alignItems: "center", gap: "0.5rem", cursor: "pointer",
          userSelect: "none",
        }}>
        <Target size={15} color={accentColor} />
        <div style={{ fontWeight: 600, fontSize: "0.85rem", color: "#cbd5e1" }}>
          Obiettivi attivi
          {items.length > 0 && (
            <span style={{
              marginLeft: "0.4rem", fontSize: "0.72rem",
              background: accentColor, color: "#0f172a",
              padding: "1px 7px", borderRadius: "10px", fontWeight: 700,
            }}>
              {items.length}
            </span>
          )}
        </div>
        <div style={{ flex: 1, fontSize: "0.7rem", color: "#64748b", fontStyle: "italic" }}>
          impegni che l'agente ricorda tra un run e l'altro
        </div>
        <div style={{ fontSize: "0.7rem", color: "#64748b" }}>
          {collapsed ? "espandi ▾" : "comprimi ▴"}
        </div>
      </div>

      {!collapsed && (
        <div style={{ marginTop: "0.6rem", display: "flex", flexDirection: "column", gap: "0.4rem" }}>
          {loading && items.length === 0 && (
            <div style={{ color: "#64748b", fontSize: "0.78rem" }}>caricamento...</div>
          )}
          {items.map((c) => (
            <div key={c.id} style={{
              background: "#0f172a",
              border: "1px solid #1e293b",
              borderRadius: "7px",
              padding: "0.55rem 0.7rem",
              display: "flex",
              gap: "0.6rem",
              alignItems: "flex-start",
            }}>
              <div style={{
                background: "#1e293b",
                color: accentColor,
                fontSize: "0.65rem",
                fontWeight: 700,
                padding: "2px 7px",
                borderRadius: "4px",
                whiteSpace: "nowrap",
                flexShrink: 0,
                marginTop: "1px",
              }}>
                {typeLabels[c.commitment_type] || c.commitment_type}
                {c.ticker && (
                  <span style={{ marginLeft: "0.3rem", color: "#cbd5e1" }}>{c.ticker}</span>
                )}
              </div>
              <div style={{ flex: 1, fontSize: "0.8rem", color: "#cbd5e1", lineHeight: 1.4 }}>
                {c.condition_text}
                {c.trigger_action && (
                  <div style={{ marginTop: "0.2rem", color: "#94a3b8", fontSize: "0.74rem" }}>
                    → {c.trigger_action}
                  </div>
                )}
              </div>
              <div style={{
                display: "flex", alignItems: "center", gap: "0.3rem",
                fontSize: "0.7rem", color: "#94a3b8", flexShrink: 0,
              }}>
                <Clock size={11} />
                {formatExpiry(c.expires_at)}
              </div>
              <button
                onClick={() => cancelItem(c.id)}
                disabled={cancellingId === c.id}
                title="Cancella obiettivo"
                style={{
                  background: "transparent",
                  border: "1px solid #334155",
                  color: "#94a3b8",
                  width: 22, height: 22,
                  borderRadius: "4px",
                  cursor: "pointer",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  flexShrink: 0,
                  padding: 0,
                }}>
                {cancellingId === c.id ? (
                  <Loader2 size={11} className="spin" />
                ) : (
                  <XIcon size={11} />
                )}
              </button>
            </div>
          ))}
        </div>
      )}
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
  const [executingActionIndex, setExecutingActionIndex] = useState(null);
  const [lastExecutedId, setLastExecutedId] = useState(null);
  const scrollRef = useRef(null);

  const agent = AGENT_TYPES.find((a) => a.id === agentType) || AGENT_TYPES[0];

  // Load history on agent type change.
  // NON svuotiamo i messaggi prima del fetch — lascia visibili i precedenti
  // finche' non arriva la nuova lista. Evita il flash "chat vuota" durante
  // lo switch standard <-> crypto.
  useEffect(() => {
    const ac = new AbortController();
    setLoading(true);
    setError(null);
    (async () => {
      const r = await safeFetchJson(
        `${API}/api/chat-decision/history/${agentType}`,
        { signal: ac.signal },
        60000,  // history: 60s timeout
      );
      if (ac.signal.aborted) return;
      if (!r.ok) {
        setError((r.data && r.data.error) || r.error || `HTTP ${r.status}`);
        // NON cancellare i messaggi precedenti: l'errore e' visibile sopra
      } else {
        setMessages((r.data && r.data.messages) || []);
      }
      if (!ac.signal.aborted) setLoading(false);
    })();
    return () => { ac.abort(); };
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
    const tempId = `temp-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const tempUserMsg = {
      id: tempId,
      role: "user",
      content: msg,
      created_at: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, tempUserMsg]);
    setInput("");
    try {
      // Timeout esteso a 180s: il second-round con technical agent + LLM
      // re-call puo' impiegare 30-60s, e il default 30s del browser
      // chiudeva la connessione prima → "Unexpected end of JSON input".
      const r = await safeFetchJson(
        `${API}/api/chat-decision/send`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ agent_type: agentType, message: msg }),
        },
        180000,
      );
      if (!r.ok) {
        throw new Error(
          (r.data && r.data.error) || r.error || `HTTP ${r.status}`
        );
      }
      const data = r.data || {};
      // FIX RACE CONDITION: invece di GET history (che puo' tornare vuoto
      // se l'INSERT in DB e' fallito ma il fallback non e' ancora popolato),
      // costruiamo i nuovi messaggi DIRETTAMENTE dalla response del send.
      // Cosi' anche se la history-fetch dovesse arrivare in ritardo o
      // restituire un set incompleto, la chat resta consistente.
      setMessages((prev) => {
        // 1) Rimuovi TUTTI i temp- (potrebbero essercene altri orfani)
        const cleaned = prev.filter((m) => !String(m.id || "").startsWith("temp-"));
        // 2) Aggiungi il msg utente con ID reale del backend (se disponibile)
        const userMsg = {
          id: data.user_message_id || tempId,
          role: "user",
          content: msg,
          created_at: tempUserMsg.created_at,
        };
        // 3) Aggiungi il msg assistant dalla response
        const assistantMsg = {
          id: data.assistant_message_id,
          role: "assistant",
          content: data.text,
          proposed_trade: data.proposed_trade,
          proposed_actions: data.proposed_actions || [],
          created_at: new Date().toISOString(),
        };
        // 4) Evita duplicati per ID (es. user_message_id gia' presente)
        const ids = new Set(cleaned.map((m) => m.id));
        const out = [...cleaned];
        if (!ids.has(userMsg.id)) out.push(userMsg);
        if (!ids.has(assistantMsg.id)) out.push(assistantMsg);
        return out;
      });
    } catch (e) {
      // Rimuovi solo il msg ottimistico fallito (NON gli altri)
      setMessages((prev) => prev.filter((m) => m.id !== tempId));
      setError(String(e.message || e));
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
      const r = await safeFetchJson(
        `${API}/api/chat-decision/execute-trade`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message_id: messageId }),
        },
        90000,
      );
      if (!r.ok) {
        const d = r.data || {};
        throw new Error(d.error || d.reason || r.error || `HTTP ${r.status}`);
      }
      const data = r.data || {};
      setLastExecutedId(data.trade_id);
      // FIX: invece di GET history, aggiorna direttamente il msg in place
      // per riflettere lo stato eseguito.
      setMessages((prev) => prev.map((m) => {
        if (m.id !== messageId) return m;
        return { ...m, executed_trade_id: data.trade_id };
      }));
    } catch (e) {
      setError(`Esecuzione fallita: ${e.message || e}`);
    } finally {
      setExecutingMsgId(null);
    }
  };

  // Esegue una singola proposed_action (trade/stop-loss/take-profit/direttiva)
  // indicizzata dal suo posto nell'array proposed_actions del messaggio.
  const executeAction = async (messageId, actionIndex) => {
    if (!window.confirm(
      "Confermi l'esecuzione di questa azione? L'operazione e' immediata."
    )) return;
    setExecutingMsgId(messageId);
    setExecutingActionIndex(actionIndex);
    setError(null);
    try {
      const r = await safeFetchJson(
        `${API}/api/chat-decision/execute-action`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message_id: messageId, action_index: actionIndex }),
        },
        90000,
      );
      const data = r.data || {};
      // FIX: guardia con OR (prima `&&` non scattava MAI su HTTP 200) → marca
      // l'azione "eseguita" SOLO se il backend ha restituito davvero un result.
      if (!r.ok || !data.result) {
        throw new Error(data.error || r.error || `HTTP ${r.status}`);
      }
      // FIX: aggiorna direttamente lo state del msg con il nuovo
      // executed_action_results per indice — niente piu' GET history
      // che potrebbe sovrascrivere altri messaggi.
      setMessages((prev) => prev.map((m) => {
        if (m.id !== messageId) return m;
        const ear = (m.executed_action_results && typeof m.executed_action_results === "object")
                     ? { ...m.executed_action_results }
                     : {};
        ear[String(actionIndex)] = data.result;   // result garantito dalla guardia — niente {ok:true} finto
        return { ...m, executed_action_results: ear };
      }));
      if (data.result && data.result.ok === false) {
        setError(`Azione fallita: ${data.result.error || "errore sconosciuto"}`);
      }
    } catch (e) {
      setError(`Esecuzione fallita: ${e.message || e}`);
    } finally {
      setExecutingMsgId(null);
      setExecutingActionIndex(null);
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

      {/* Pannello Obiettivi Attivi (memoria persistente del Decision Agent) */}
      <CommitmentsPanel agentType={agentType} accentColor={agent.color} />

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
              onExecuteAction={executeAction}
              executingMsgId={executingMsgId}
              executingActionIndex={executingActionIndex}
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
