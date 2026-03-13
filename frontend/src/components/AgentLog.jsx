import React, { useEffect, useRef } from "react";

// Mappa fase -> classe CSS del badge
const PHASE_CLASSES = {
  GEOPOLITICAL: "badge-geopolitical",
  TECHNICAL: "badge-technical",
  DECISION: "badge-decision",
  ERROR: "badge-error",
  INFO: "badge-info",
  PORTFOLIO: "badge-info",
  EXECUTION: "badge-decision",
};

// Formattazione orario dal timestamp
const fmtTime = (ts) => {
  if (!ts) return "--:--";
  const d = new Date(ts);
  return d.toLocaleTimeString("it-IT", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
};

// Determina il tipo di run dal contenuto del log
const getRunMode = (log) => {
  try {
    const content = typeof log.content === "string" ? JSON.parse(log.content) : log.content;
    if (content && content.mode) return content.mode;
  } catch {
    // Non e' JSON
  }
  return null;
};

// Badge per tipo di run
const RunModeBadge = ({ mode }) => {
  if (!mode) return null;
  const config = {
    full: { label: "COMPLETO", icon: "\u{1F535}", cls: "badge-run-full" },
    weekend: { label: "WEEKEND", icon: "\u{1F319}", cls: "badge-run-weekend" },
    pre_market: { label: "PRE-MARKET", icon: "\u{1F305}", cls: "badge-run-premarket" },
  };
  const c = config[mode] || config.full;
  return (
    <span className={`badge-phase ${c.cls}`}>
      {c.icon} {c.label}
    </span>
  );
};

function AgentLog({ logs }) {
  const scrollRef = useRef(null);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [logs]);

  if (!logs || logs.length === 0) {
    return (
      <div className="card">
        <div className="card-title">Log Agente</div>
        <div className="empty-state">Nessun log disponibile</div>
      </div>
    );
  }

  const latestRunId = logs[logs.length - 1]?.run_id;
  const filteredLogs = latestRunId
    ? logs.filter((l) => l.run_id === latestRunId)
    : logs.slice(-100);

  // Determina il modo del run corrente
  const runMode = filteredLogs.reduce((mode, log) => {
    if (mode) return mode;
    return getRunMode(log);
  }, null);

  return (
    <div className="card">
      <div className="card-title">
        <span style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
          Log Agente
          <RunModeBadge mode={runMode} />
        </span>
        {latestRunId && (
          <span
            style={{
              float: "right",
              fontSize: "0.7rem",
              color: "#5a5f6a",
              fontWeight: 400,
              textTransform: "none",
            }}
          >
            Run: {latestRunId.substring(0, 8)}...
          </span>
        )}
      </div>
      <div className="scrollable" ref={scrollRef} style={{ maxHeight: 350 }}>
        {filteredLogs.map((log, idx) => {
          const phase = (log.phase || log.level || "INFO").toUpperCase();
          const badgeClass = PHASE_CLASSES[phase] || "badge-info";

          return (
            <div className="log-entry" key={idx}>
              <span className="log-timestamp">{fmtTime(log.timestamp)}</span>
              <span className={`badge-phase ${badgeClass}`}>{phase}</span>
              <span className="log-content">
                {log.message || log.content || JSON.stringify(log)}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default AgentLog;
