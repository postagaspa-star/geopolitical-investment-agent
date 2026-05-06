import { useEffect, useState, useMemo } from "react";
import { Eye, BookOpen, BarChart3, Brain, Target, AlertCircle } from "lucide-react";

const API = window.location.origin;

/**
 * AgentActivityCards — sostituisce la lista log raw nella Dashboard Live.
 *
 * Mostra card strutturate per:
 *  - Watchdog: SOLO trigger positivi (= ha svegliato Decision/Tech) con motivazione
 *  - Scout: solo "creato N schede" + breve riassunto
 *  - Technical: tutte le analisi sui ticker
 *  - Decision: due card → ragionamento + decisione presa
 */
export default function AgentActivityCards() {
  const [logs, setLogs] = useState([]);
  const [loading, setLoading] = useState(true);

  const reload = async () => {
    try {
      const res = await fetch(`${API}/api/logs?limit=200`);
      if (res.ok) setLogs(await res.json());
    } catch {}
    setLoading(false);
  };

  useEffect(() => {
    reload();
    const id = setInterval(reload, 15000);
    return () => clearInterval(id);
  }, []);

  const cards = useMemo(() => buildCards(logs), [logs]);

  if (loading) return <div style={S.empty}>Caricamento…</div>;
  if (!cards.length) return <div style={S.empty}>Nessuna attività recente</div>;

  return (
    <div style={S.list}>
      {cards.slice(0, 30).map((c, i) => <CardRenderer key={c.key || i} card={c} />)}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════
// PARSING: trasforma log raw in card
// ═══════════════════════════════════════════════════════════════════════

function buildCards(logs) {
  const cards = [];
  const skipNoise = new Set(["watchdog_throttled", "watchdog_complete"]);

  for (const log of logs) {
    const phase = (log.phase || "").toUpperCase();
    let content;
    try { content = JSON.parse(log.content || "{}"); }
    catch { content = { raw: log.content }; }
    const ts = log.timestamp;

    // ─── WATCHDOG: solo trigger positivi (skip throttled/no-trigger) ───
    if (phase === "WATCHDOG") {
      if (skipNoise.has(content.event) && !content.trigger) continue;
      if (content.event === "watchdog_complete" && !content.trigger) continue;
      if (content.event === "watchdog_throttled") continue;
      // Solo eventi con trigger=true
      if (content.trigger) {
        cards.push({
          key: `wd_${log.id || ts}`,
          type: "watchdog",
          ts,
          urgency: content.urgency,
          reason: content.reason,
          focus_tickers: content.focus_tickers || [],
          deep_check: content.deep_check,
          portfolio_tickers: content.portfolio_tickers,
        });
      }
      continue;
    }

    // ORCHESTRATOR: skip — duplicato del Watchdog/Pipeline complete
    if (phase === "ORCHESTRATOR") continue;

    // ─── SCOUT: solo scout_20min_complete con riassunto ───
    if (phase === "SCOUT" && content.event === "scout_20min_complete") {
      const sources = content.sources_summary || {};
      const okSources = Object.entries(sources)
        .filter(([_, v]) => v.status === "ok" && v.count > 0)
        .map(([k]) => k);
      cards.push({
        key: `sc_${log.id || ts}`,
        type: "scout",
        ts,
        cards_created: content.micro_cards || 0,
        written_to_buffer: content.written_to_buffer || 0,
        skipped_duplicates: content.skipped_duplicates || 0,
        sources_active: okSources,
        sources_total: Object.keys(sources).length,
      });
      continue;
    }
    // Skip altri scout events
    if (phase.startsWith("SCOUT")) continue;

    // ─── TECHNICAL ───
    if (phase === "TECH_WORKER" && content.event === "technical_analysis_complete") {
      cards.push({
        key: `tw_${log.id || ts}`,
        type: "technical",
        ts,
        engine: content.engine,
        analyses: content.analyses_summary || [],
        summary: content.summary_text,
        tickers_analyzed: content.tickers_analyzed,
        tickers_requested: content.tickers_requested,
      });
      continue;
    }
    if (phase === "TECH_CRYPTO" && content.event === "tech_crypto_complete") {
      cards.push({
        key: `tc_${log.id || ts}`,
        type: "technical_crypto",
        ts,
        engine: content.engine,
        analyses: content.analyses_summary || [],
        summary: content.summary_text,
        tickers_analyzed: content.tickers_analyzed,
        tickers_with_data: content.tickers_with_data || [],
      });
      continue;
    }

    // ─── DECISION ───
    if (phase === "DECISION_REASONING") {
      cards.push({
        key: `dr_${log.id || ts}`,
        type: "decision_reasoning",
        ts,
        model: content.model,
        reasoning_text: content.reasoning_text,
      });
      continue;
    }
    if (phase === "DECISION_TRADE") {
      cards.push({
        key: `dt_${log.id || ts}`,
        type: "decision_decision",
        ts,
        action: content.action, ticker: content.ticker, qty: content.qty,
        price: content.price, confidence: content.confidence,
        stop_loss: content.stop_loss, take_profit: content.take_profit,
        decision_label: "TRADE",
      });
      continue;
    }
    if (phase === "DECISION_NO_TRADE") {
      cards.push({
        key: `dn_${log.id || ts}`,
        type: "decision_decision",
        ts,
        decision_label: "NO TRADE",
        reasoning: content.reasoning,
      });
      continue;
    }
    if (phase === "DECISION_ERROR") {
      cards.push({
        key: `de_${log.id || ts}`,
        type: "decision_error",
        ts,
        error_type: content.error_type,
        error_message: content.error_message,
      });
      continue;
    }

    // ─── DECISION CRYPTO (R1) ───
    if (phase === "DECISION_CRYPTO_COMPLETE") {
      cards.push({
        key: `dcc_${log.id || ts}`,
        type: "decision_crypto",
        ts, model: content.model,
        trades_executed: content.trades_executed || 0,
        final_text: content.final_text,
      });
      continue;
    }
    if (phase === "DECISION_CRYPTO_TRADE") {
      cards.push({
        key: `dct_${log.id || ts}`,
        type: "decision_decision",
        ts,
        action: content.action, ticker: content.ticker, qty: content.qty,
        price: content.price, confidence: content.confidence,
        decision_label: "CRYPTO TRADE",
      });
      continue;
    }
  }

  // Ordina cronologicamente decrescente
  return cards.sort((a, b) => new Date(b.ts) - new Date(a.ts));
}

// ═══════════════════════════════════════════════════════════════════════
// RENDER
// ═══════════════════════════════════════════════════════════════════════

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

function CardRenderer({ card }) {
  switch (card.type) {
    case "watchdog": return <WatchdogCard c={card} />;
    case "scout": return <ScoutCard c={card} />;
    case "technical": return <TechCard c={card} />;
    case "technical_crypto": return <TechCard c={card} crypto />;
    case "decision_reasoning": return <ReasoningCard c={card} />;
    case "decision_decision": return <DecisionCard c={card} />;
    case "decision_crypto": return <DecisionCryptoCard c={card} />;
    case "decision_error": return <ErrorCard c={card} />;
    default: return null;
  }
}

function WatchdogCard({ c }) {
  return (
    <div style={{ ...S.card, borderLeftColor: "#fbbf24" }}>
      <div style={S.head}>
        <Eye size={14} style={{ color: "#fbbf24" }} />
        <strong>Watchdog → trigger</strong>
        <span style={S.urgency}>urgency {c.urgency}/10</span>
        <span style={S.timestamp}>{timeAgo(c.ts)}</span>
      </div>
      <div style={S.body}>
        <strong>Motivazione:</strong> {c.reason}
      </div>
      {c.focus_tickers?.length > 0 && (
        <div style={S.tags}>
          {c.focus_tickers.map(t => <span key={t} style={S.tag}>{t}</span>)}
        </div>
      )}
    </div>
  );
}

function ScoutCard({ c }) {
  return (
    <div style={{ ...S.card, borderLeftColor: "#a78bfa" }}>
      <div style={S.head}>
        <BookOpen size={14} style={{ color: "#a78bfa" }} />
        <strong>Scout</strong>
        <span style={S.timestamp}>{timeAgo(c.ts)}</span>
      </div>
      <div style={S.body}>
        Create <strong>{c.cards_created}</strong> schede
        {" "}({c.written_to_buffer} scritte, {c.skipped_duplicates} duplicate skippate)
        — {c.sources_active.length}/{c.sources_total} fonti attive
      </div>
      {c.sources_active.length > 0 && (
        <div style={S.tags}>
          {c.sources_active.map(s => <span key={s} style={{...S.tag, fontSize: 10}}>{s}</span>)}
        </div>
      )}
    </div>
  );
}

function TechCard({ c, crypto }) {
  const tone = crypto ? "#0891b2" : "#06b6d4";
  return (
    <div style={{ ...S.card, borderLeftColor: tone }}>
      <div style={S.head}>
        <BarChart3 size={14} style={{ color: tone }} />
        <strong>Technical {crypto ? "Crypto" : ""}</strong>
        <span style={{...S.tag, fontSize: 10}}>{c.engine}</span>
        <span style={S.timestamp}>{timeAgo(c.ts)}</span>
      </div>
      {c.summary && <div style={S.body}>{c.summary}</div>}
      {c.analyses.length > 0 && (
        <table style={S.table}>
          <thead>
            <tr><th>Ticker</th><th>Signal</th><th>Trend</th><th>Conf</th></tr>
          </thead>
          <tbody>
            {c.analyses.map((a, i) => (
              <tr key={i}>
                <td style={{ fontFamily: "monospace" }}>{a.ticker}</td>
                <td style={{ color: signalColor(a.signal) }}>{a.signal || "—"}</td>
                <td>{a.trend || "—"}</td>
                <td>{a.confidence != null ? `${a.confidence}%` : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function ReasoningCard({ c }) {
  return (
    <div style={{ ...S.card, borderLeftColor: "#10b981" }}>
      <div style={S.head}>
        <Brain size={14} style={{ color: "#10b981" }} />
        <strong>Decision — Ragionamento</strong>
        <span style={{...S.tag, fontSize: 10}}>{c.model}</span>
        <span style={S.timestamp}>{timeAgo(c.ts)}</span>
      </div>
      <div style={{ ...S.body, whiteSpace: "pre-wrap", maxHeight: 240, overflowY: "auto" }}>
        {c.reasoning_text || "(testo vuoto)"}
      </div>
    </div>
  );
}

function DecisionCard({ c }) {
  const isTrade = c.decision_label !== "NO TRADE";
  const isBuy = c.action === "BUY";
  const tone = !isTrade ? "#64748b" : isBuy ? "#10b981" : "#ef4444";
  return (
    <div style={{ ...S.card, borderLeftColor: tone, background: "#0f172a" }}>
      <div style={S.head}>
        <Target size={14} style={{ color: tone }} />
        <strong>Decision — {c.decision_label}</strong>
        <span style={S.timestamp}>{timeAgo(c.ts)}</span>
      </div>
      {isTrade ? (
        <div style={S.body}>
          <div style={S.tradeRow}>
            <strong style={{ color: tone, fontSize: 16 }}>
              {c.action} {c.qty} {c.ticker}
            </strong>
            <span>@ ${c.price?.toFixed(2)}</span>
            <span style={{ color: "#94a3b8" }}>conf {c.confidence}%</span>
          </div>
          {(c.stop_loss || c.take_profit) && (
            <div style={{ fontSize: 12, color: "#94a3b8" }}>
              {c.stop_loss && <span>SL ${c.stop_loss} </span>}
              {c.take_profit && <span>· TP ${c.take_profit}</span>}
            </div>
          )}
        </div>
      ) : (
        <div style={S.body}>{c.reasoning?.slice(0, 400)}</div>
      )}
    </div>
  );
}

function DecisionCryptoCard({ c }) {
  return (
    <div style={{ ...S.card, borderLeftColor: "#f472b6", background: "#1a0e1a" }}>
      <div style={S.head}>
        <Target size={14} style={{ color: "#f472b6" }} />
        <strong>Decision Crypto</strong>
        <span style={{...S.tag, fontSize: 10, background: "#831843", color: "#fce7f3"}}>R1</span>
        <span style={S.tag}>{c.trades_executed} trade</span>
        <span style={S.timestamp}>{timeAgo(c.ts)}</span>
      </div>
      <div style={{ ...S.body, whiteSpace: "pre-wrap", maxHeight: 200, overflowY: "auto" }}>
        {c.final_text}
      </div>
    </div>
  );
}

function ErrorCard({ c }) {
  return (
    <div style={{ ...S.card, borderLeftColor: "#ef4444" }}>
      <div style={S.head}>
        <AlertCircle size={14} style={{ color: "#ef4444" }} />
        <strong style={{ color: "#ef4444" }}>Decision ERROR</strong>
        <span style={S.timestamp}>{timeAgo(c.ts)}</span>
      </div>
      <div style={S.body}>
        <strong>{c.error_type}</strong>: {c.error_message}
      </div>
    </div>
  );
}

const signalColor = (s) => {
  if (s === "BUY") return "#10b981";
  if (s === "SELL") return "#ef4444";
  if (s === "HOLD") return "#94a3b8";
  return "#cbd5e1";
};

const S = {
  list: { display: "flex", flexDirection: "column", gap: 10 },
  card: { background: "#111827", border: "1px solid #1f2937",
          borderLeft: "4px solid", padding: "10px 14px", borderRadius: 6 },
  head: { display: "flex", alignItems: "center", gap: 8, marginBottom: 6,
          fontSize: 13, color: "#cbd5e1" },
  body: { fontSize: 13, color: "#e2e8f0", lineHeight: 1.5 },
  urgency: { padding: "2px 6px", borderRadius: 3, background: "#7c2d12",
             color: "#fed7aa", fontSize: 10, fontWeight: 600 },
  timestamp: { marginLeft: "auto", color: "#64748b", fontSize: 11 },
  tag: { padding: "2px 6px", borderRadius: 3, background: "#1e293b",
         color: "#94a3b8", fontSize: 11, fontFamily: "monospace" },
  tags: { display: "flex", gap: 4, flexWrap: "wrap", marginTop: 6 },
  table: { width: "100%", borderCollapse: "collapse", fontSize: 12, marginTop: 8 },
  tradeRow: { display: "flex", gap: 12, alignItems: "center", marginBottom: 4 },
  empty: { color: "#64748b", padding: 24, textAlign: "center", fontSize: 13 },
};
