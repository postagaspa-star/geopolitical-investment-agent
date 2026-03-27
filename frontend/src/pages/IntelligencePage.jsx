import React, { useState, useEffect, useCallback, useRef } from "react";

const API = window.location.origin;

const fmtDate = (ts) => {
  if (!ts) return "--";
  const d = new Date(ts);
  return d.toLocaleString("it-IT", {
    day: "2-digit", month: "2-digit", year: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
};

const PHASE_COLORS = {
  ORCHESTRATOR: "#7c3aed",
  SCOUT: "#10b981",
  SCOUT_DAILY: "#059669",
  SCOUT_WEEKLY: "#047857",
  SCOUT_BUFFER_GDELT: "#10b981",
  SCOUT_BUFFER_NEWSAPI: "#10b981",
  SCOUT_BUFFER_CLAWSTREET: "#10b981",
  SCOUT_BUFFER_CONGRESSIONAL: "#10b981",
  TECH_WORKER: "#2563eb",
  DECISION: "#f59e0b",
  DECISION_CONTEXT: "#d97706",
  DECISION_MODEL: "#b45309",
  DECISION_TRADE: "#dc2626",
  DECISION_NO_TRADE: "#6b7280",
  DECISION_EXTRA_TA: "#8b5cf6",
  DECISION_COMPLETE: "#f59e0b",
  GEO_WORKER: "#059669",
  MANAGER: "#dc2626",
  MANAGER_DECISION: "#f59e0b",
  MANAGER_TOOL: "#9333ea",
  MARKET_INTELLIGENCE: "#0891b2",
  GEOPOLITICAL: "#059669",
  TECHNICAL: "#6366f1",
  CLAWSTREET_MIRROR: "#8b5cf6",
  INFO: "#64748b",
  ERROR: "#ef4444",
};

const PHASE_LABELS = {
  ORCHESTRATOR: "Orchestratore",
  SCOUT: "Scout (Sonnet)",
  SCOUT_DAILY: "Daily Recap",
  SCOUT_WEEKLY: "Weekly Matrix",
  SCOUT_BUFFER_GDELT: "Scout GDELT",
  SCOUT_BUFFER_NEWSAPI: "Scout NewsAPI",
  SCOUT_BUFFER_CLAWSTREET: "Scout ClawStreet",
  SCOUT_BUFFER_CONGRESSIONAL: "Scout Congressional",
  TECH_WORKER: "Technical (DeepSeek)",
  DECISION: "Decision (Opus)",
  DECISION_CONTEXT: "Decision Context",
  DECISION_MODEL: "Decision Model",
  DECISION_TRADE: "TRADE ESEGUITO",
  DECISION_NO_TRADE: "No Trade",
  DECISION_EXTRA_TA: "Extra Analysis",
  DECISION_COMPLETE: "Decision Complete",
  GEO_WORKER: "Worker Geopolitico",
  MANAGER: "Manager Agent",
  MANAGER_DECISION: "Decisione Manager",
  MANAGER_TOOL: "Manager Tool",
  MARKET_INTELLIGENCE: "Market Intel",
  GEOPOLITICAL: "Geopolitica",
  TECHNICAL: "Analisi Tecnica",
  CLAWSTREET_MIRROR: "ClawStreet",
  INFO: "Info",
  ERROR: "Errore",
};

function parseLogContent(content) {
  try {
    return JSON.parse(content);
  } catch {
    return null;
  }
}

function LogEntry({ log }) {
  const [expanded, setExpanded] = useState(false);
  const phase = log.phase || "INFO";
  const color = PHASE_COLORS[phase] || "#64748b";
  const label = PHASE_LABELS[phase] || phase;
  const parsed = parseLogContent(log.content);

  let summary = log.content;
  let isTradeEntry = false;

  if (parsed) {
    if (parsed.ticker && parsed.action && (phase === "DECISION_TRADE" || phase.includes("TRADE"))) {
      isTradeEntry = true;
      summary = `${parsed.action} ${parsed.qty || parsed.quantity || "?"} ${parsed.ticker}`;
      if (parsed.price) summary += ` @ $${Number(parsed.price).toFixed(2)}`;
      if (parsed.confidence) summary += ` | Conf: ${Number(parsed.confidence).toFixed(0)}%`;
      if (parsed.stop_loss) summary += ` | SL: $${Number(parsed.stop_loss).toFixed(2)}`;
    } else if (parsed.event) {
      summary = parsed.event.replace(/_/g, " ").toUpperCase();
      if (parsed.mode) summary += ` (${parsed.mode})`;
      if (parsed.duration_seconds) summary += ` — ${Number(parsed.duration_seconds).toFixed(1)}s`;
      if (parsed.architecture) summary += ` [${parsed.architecture}]`;
      if (parsed.engine) summary += ` | Engine: ${parsed.engine}`;
      if (parsed.tickers_analyzed != null) summary += ` | ${parsed.tickers_analyzed} tickers`;
      if (parsed.micro_cards != null) summary += ` | ${parsed.micro_cards} cards`;
      if (parsed.trades_executed != null) summary += ` | ${parsed.trades_executed} trades`;
      if (parsed.model) summary += ` | Model: ${parsed.model}`;
      if (parsed.geo_risk) summary += ` | Risk: ${parsed.geo_risk}`;
      if (parsed.tech_engine) summary += ` | Engine: ${parsed.tech_engine}`;
      if (parsed.macro_bias) summary += ` | Bias: ${parsed.macro_bias}`;
    } else if (parsed.context_loaded) {
      const loaded = Object.entries(parsed.context_loaded)
        .filter(([, v]) => v)
        .map(([k]) => k);
      summary = `Context loaded: ${loaded.join(", ")}`;
    } else if (parsed.reasoning) {
      summary = parsed.reasoning.substring(0, 180);
    } else if (parsed.tool_name) {
      summary = `Tool: ${parsed.tool_name}`;
      if (parsed.tool_input?.ticker) summary += ` (${parsed.tool_input.ticker})`;
    } else if (parsed.tickers) {
      summary = `Tickers: ${parsed.tickers.join(", ")}`;
    } else if (parsed.error) {
      summary = `ERRORE: ${parsed.error.substring(0, 150)}`;
    }
  }
  if (summary && summary.length > 220) summary = summary.substring(0, 220) + "...";

  return (
    <div
      className={`log-entry ${isTradeEntry ? "log-entry-trade" : ""}`}
      onClick={() => setExpanded(!expanded)}
    >
      <div className="log-entry-header">
        <span className="log-phase-badge" style={{ background: color }}>{label}</span>
        <span className="log-time">{fmtDate(log.timestamp)}</span>
      </div>
      <div className="log-summary">{summary}</div>
      {expanded && parsed && (
        <pre className="log-detail">{JSON.stringify(parsed, null, 2)}</pre>
      )}
    </div>
  );
}

function ThinkingProcess({ logs, loading }) {
  const logsEndRef = useRef(null);
  const [groupByRun, setGroupByRun] = useState(true);
  const [filterPhase, setFilterPhase] = useState("ALL");

  useEffect(() => {
    logsEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [logs]);

  if (loading) {
    return <div className="empty-state">Caricamento log agente...</div>;
  }

  if (!logs || logs.length === 0) {
    return <div className="empty-state">Nessun log disponibile. L'agente non ha ancora eseguito un ciclo.</div>;
  }

  // Collect unique phases for filter
  const allPhases = [...new Set(logs.map(l => l.phase || "INFO"))].sort();

  // Apply filter
  const filtered = filterPhase === "ALL" ? logs : logs.filter(l => (l.phase || "INFO") === filterPhase);

  // Group by run_id
  const grouped = {};
  for (const log of filtered) {
    const rid = log.run_id || "unknown";
    if (!grouped[rid]) grouped[rid] = [];
    grouped[rid].push(log);
  }

  const runIds = Object.keys(grouped);

  // Count phases in current view
  const phaseCounts = {};
  for (const log of filtered) {
    const p = log.phase || "INFO";
    phaseCounts[p] = (phaseCounts[p] || 0) + 1;
  }

  return (
    <div className="thinking-process">
      <div className="thinking-header">
        <span className="thinking-count">{filtered.length} log entries — {runIds.length} runs</span>
        <div style={{ display: "flex", gap: "0.4rem", flexWrap: "wrap" }}>
          <select
            className="phase-filter"
            value={filterPhase}
            onChange={(e) => setFilterPhase(e.target.value)}
          >
            <option value="ALL">Tutti ({logs.length})</option>
            {allPhases.map(p => (
              <option key={p} value={p}>{PHASE_LABELS[p] || p} ({phaseCounts[p] || 0})</option>
            ))}
          </select>
          <button
            className={`btn btn-sm ${groupByRun ? "btn-primary" : "btn-secondary"}`}
            onClick={() => setGroupByRun(!groupByRun)}
            style={{ fontSize: "0.7rem", padding: "0.2rem 0.5rem" }}
          >
            {groupByRun ? "Raggruppati" : "Cronologico"}
          </button>
        </div>
      </div>

      {groupByRun ? (
        runIds.map((rid) => {
          const runLogs = grouped[rid];
          const firstLog = runLogs[0];
          const firstParsed = parseLogContent(firstLog?.content);
          const arch = firstParsed?.architecture || "single-agent";
          const isMulti = arch === "multi-agent" || arch === "manager-workers";

          // Detect pipeline phases present in this run
          const runPhases = [...new Set(runLogs.map(l => l.phase))];
          const hasScout = runPhases.some(p => p?.startsWith("SCOUT"));
          const hasTech = runPhases.some(p => p?.includes("TECH"));
          const hasDecision = runPhases.some(p => p?.startsWith("DECISION"));
          const hasTrade = runPhases.includes("DECISION_TRADE");

          return (
            <div key={rid} className="run-group">
              <div className="run-group-header">
                <span className="run-id">Run: {rid.substring(0, 8)}</span>
                {isMulti && <span className="badge badge-multi">MULTI-AGENT</span>}
                {!isMulti && <span className="badge badge-single">SINGLE-AGENT</span>}
                {hasTrade && <span className="badge badge-trade">TRADE</span>}
                <span className="run-time">{fmtDate(firstLog?.timestamp)}</span>
              </div>
              {/* Pipeline progress bar */}
              {isMulti && (
                <div className="pipeline-progress">
                  <span className={`pipeline-step ${hasScout ? "done" : ""}`}>Scout</span>
                  <span className="pipeline-arrow">&rarr;</span>
                  <span className={`pipeline-step ${hasTech ? "done" : ""}`}>Technical</span>
                  <span className="pipeline-arrow">&rarr;</span>
                  <span className={`pipeline-step ${hasDecision ? "done" : ""}`}>Decision</span>
                </div>
              )}
              {runLogs.map((log, i) => <LogEntry key={i} log={log} />)}
            </div>
          );
        })
      ) : (
        filtered.map((log, i) => <LogEntry key={i} log={log} />)
      )}
      <div ref={logsEndRef} />
    </div>
  );
}

function IntelligencePage() {
  const [intelligence, setIntelligence] = useState([]);
  const [briefings, setBriefings] = useState([]);
  const [agentLogs, setAgentLogs] = useState([]);
  const [logsLoading, setLogsLoading] = useState(false);
  const [activeSection, setActiveSection] = useState("thinking");
  const [expandedId, setExpandedId] = useState(null);

  const fetchIntelligence = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/intelligence?limit=20`);
      if (res.ok) setIntelligence(await res.json());
    } catch (err) { console.error(err); }
  }, []);

  const fetchBriefings = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/briefings?limit=20`);
      if (res.ok) setBriefings(await res.json());
    } catch (err) { console.error(err); }
  }, []);

  const fetchLogs = useCallback(async () => {
    setLogsLoading(true);
    try {
      const res = await fetch(`${API}/api/logs?limit=200`);
      if (res.ok) {
        const data = await res.json();
        setAgentLogs(Array.isArray(data) ? data.reverse() : []);
      }
    } catch (err) { console.error(err); }
    setLogsLoading(false);
  }, []);

  useEffect(() => {
    fetchIntelligence();
    fetchBriefings();
    fetchLogs();
  }, [fetchIntelligence, fetchBriefings, fetchLogs]);

  // Auto-refresh logs every 15s when on thinking tab
  useEffect(() => {
    if (activeSection !== "thinking") return;
    const interval = setInterval(fetchLogs, 15000);
    return () => clearInterval(interval);
  }, [activeSection, fetchLogs]);

  const parseJson = (str) => { try { return JSON.parse(str); } catch { return []; } };

  // Count trades in logs
  const tradeCount = agentLogs.filter(l => l.phase === "DECISION_TRADE").length;

  return (
    <div>
      <h2 style={{fontSize:"1.3rem",marginBottom:"1.5rem",color:"var(--text-primary)"}}>Intelligence &amp; Thinking Process</h2>

      <div className="intel-tabs">
        <button className={`intel-tab ${activeSection === "thinking" ? "active" : ""}`} onClick={() => setActiveSection("thinking")}>
          Thinking Process ({agentLogs.length})
          {tradeCount > 0 && <span className="tab-badge-trade">{tradeCount}</span>}
        </button>
        <button className={`intel-tab ${activeSection === "intelligence" ? "active" : ""}`} onClick={() => setActiveSection("intelligence")}>
          Weekend Intelligence ({Array.isArray(intelligence) ? intelligence.length : 0})
        </button>
        <button className={`intel-tab ${activeSection === "briefings" ? "active" : ""}`} onClick={() => setActiveSection("briefings")}>
          Pre-Market Briefings ({Array.isArray(briefings) ? briefings.length : 0})
        </button>
      </div>

      {activeSection === "thinking" && (
        <ThinkingProcess logs={agentLogs} loading={logsLoading} />
      )}

      {activeSection === "intelligence" && (
        <div>
          {(!intelligence || intelligence.length === 0) ? (
            <div className="empty-state">Nessuna intelligence weekend disponibile</div>
          ) : intelligence.map((item) => {
            const events = parseJson(item.key_events);
            const isExp = expandedId === `i-${item.id}`;
            return (
              <div key={item.id} className={`intel-card ${isExp ? "expanded" : ""}`} onClick={() => setExpandedId(isExp ? null : `i-${item.id}`)}>
                <div className="intel-card-header">
                  <span className="badge badge-weekend">WEEKEND</span>
                  <span className="intel-date">{fmtDate(item.saved_at)}</span>
                  <span className="intel-run">Run: {(item.run_id||"").substring(0,8)}</span>
                </div>
                {events.length > 0 && (
                  <div className="intel-events">
                    <strong>Eventi chiave:</strong>
                    <ul>
                      {events.slice(0, isExp ? events.length : 3).map((e,i) => <li key={i}>{e}</li>)}
                      {!isExp && events.length > 3 && <li className="more">+{events.length-3} altri...</li>}
                    </ul>
                  </div>
                )}
                {item.market_implications && (
                  <div className="intel-implications">
                    <strong>Implicazioni mercato:</strong>
                    <p>{isExp ? item.market_implications : item.market_implications.substring(0,200) + (item.market_implications.length > 200 ? "..." : "")}</p>
                  </div>
                )}
                {isExp && item.content && (
                  <div className="intel-full"><strong>Analisi completa:</strong><p>{item.content}</p></div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {activeSection === "briefings" && (
        <div>
          {(!briefings || briefings.length === 0) ? (
            <div className="empty-state">Nessun briefing pre-market disponibile</div>
          ) : briefings.map((item) => {
            const assets = parseJson(item.priority_assets);
            const isExp = expandedId === `b-${item.id}`;
            return (
              <div key={item.id} className={`intel-card ${isExp ? "expanded" : ""}`} onClick={() => setExpandedId(isExp ? null : `b-${item.id}`)}>
                <div className="intel-card-header">
                  <span className="badge badge-premarket">PRE-MARKET</span>
                  {item.market_session && <span className="badge badge-full">{item.market_session}</span>}
                  <span className="intel-date">{fmtDate(item.saved_at)}</span>
                  <span className="intel-run">Run: {(item.run_id||"").substring(0,8)}</span>
                </div>
                {assets.length > 0 && (
                  <div className="intel-assets">
                    <strong>Asset prioritari:</strong>
                    <div className="asset-tags">{assets.map((a,i) => <span key={i} className="ticker-tag">{a}</span>)}</div>
                  </div>
                )}
                <div className="intel-content">
                  <p>{isExp ? item.content : (item.content||"").substring(0,300) + ((item.content||"").length > 300 ? "..." : "")}</p>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default IntelligencePage;
