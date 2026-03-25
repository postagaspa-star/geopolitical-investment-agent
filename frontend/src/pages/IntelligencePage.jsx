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
  GEO_WORKER: "#059669",
  TECH_WORKER: "#2563eb",
  MANAGER: "#dc2626",
  MANAGER_DECISION: "#f59e0b",
  MARKET_INTELLIGENCE: "#0891b2",
  GEOPOLITICAL: "#059669",
  TECHNICAL: "#6366f1",
  DECISION: "#f59e0b",
  CLAWSTREET_MIRROR: "#8b5cf6",
  INFO: "#64748b",
  ERROR: "#ef4444",
};

const PHASE_LABELS = {
  ORCHESTRATOR: "Orchestratore",
  GEO_WORKER: "Worker Geopolitico",
  TECH_WORKER: "Worker Tecnico",
  MANAGER: "Manager Agent",
  MANAGER_DECISION: "Decisione Manager",
  MANAGER_TOOL: "Manager Tool",
  MARKET_INTELLIGENCE: "Market Intel",
  GEOPOLITICAL: "Geopolitica",
  TECHNICAL: "Analisi Tecnica",
  DECISION: "Decisione",
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
  if (parsed) {
    if (parsed.event) {
      summary = parsed.event.replace(/_/g, " ").toUpperCase();
      if (parsed.mode) summary += ` (${parsed.mode})`;
      if (parsed.duration_seconds) summary += ` — ${parsed.duration_seconds.toFixed(1)}s`;
      if (parsed.architecture) summary += ` [${parsed.architecture}]`;
      if (parsed.geo_risk) summary += ` | Risk: ${parsed.geo_risk}`;
      if (parsed.tech_engine) summary += ` | Engine: ${parsed.tech_engine}`;
    } else if (parsed.tool_name) {
      summary = `Tool: ${parsed.tool_name}`;
      if (parsed.tool_input?.ticker) summary += ` (${parsed.tool_input.ticker})`;
    } else if (parsed.error) {
      summary = `ERRORE: ${parsed.error.substring(0, 150)}`;
    }
  }
  if (summary.length > 200) summary = summary.substring(0, 200) + "...";

  return (
    <div className="log-entry" onClick={() => setExpanded(!expanded)}>
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

  useEffect(() => {
    logsEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [logs]);

  if (loading) {
    return <div className="empty-state">Caricamento log agente...</div>;
  }

  if (!logs || logs.length === 0) {
    return <div className="empty-state">Nessun log disponibile. L'agente non ha ancora eseguito un ciclo.</div>;
  }

  // Group by run_id
  const grouped = {};
  for (const log of logs) {
    const rid = log.run_id || "unknown";
    if (!grouped[rid]) grouped[rid] = [];
    grouped[rid].push(log);
  }

  const runIds = Object.keys(grouped);

  return (
    <div className="thinking-process">
      <div className="thinking-header">
        <span className="thinking-count">{logs.length} log entries — {runIds.length} runs</span>
        <button
          className={`btn btn-sm ${groupByRun ? "btn-primary" : "btn-secondary"}`}
          onClick={() => setGroupByRun(!groupByRun)}
          style={{ fontSize: "0.7rem", padding: "0.2rem 0.5rem" }}
        >
          {groupByRun ? "Raggruppati" : "Cronologico"}
        </button>
      </div>

      {groupByRun ? (
        runIds.map((rid) => {
          const runLogs = grouped[rid];
          const firstLog = runLogs[0];
          const firstParsed = parseLogContent(firstLog?.content);
          const arch = firstParsed?.architecture || "single-agent";
          const isMulti = arch === "multi-agent" || arch === "manager-workers";

          return (
            <div key={rid} className="run-group">
              <div className="run-group-header">
                <span className="run-id">Run: {rid.substring(0, 8)}</span>
                {isMulti && <span className="badge badge-multi">MULTI-AGENT</span>}
                {!isMulti && <span className="badge badge-single">SINGLE-AGENT</span>}
                <span className="run-time">{fmtDate(firstLog?.timestamp)}</span>
              </div>
              {runLogs.map((log, i) => <LogEntry key={i} log={log} />)}
            </div>
          );
        })
      ) : (
        logs.map((log, i) => <LogEntry key={i} log={log} />)
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

  return (
    <div>
      <h2 style={{fontSize:"1.3rem",marginBottom:"1.5rem",color:"var(--text-primary)"}}>Intelligence &amp; Thinking Process</h2>

      <div className="intel-tabs">
        <button className={`intel-tab ${activeSection === "thinking" ? "active" : ""}`} onClick={() => setActiveSection("thinking")}>
          Thinking Process ({agentLogs.length})
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
