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
  SCOUT: "Scout (DeepSeek-V3)",
  SCOUT_8H: "Scout Report 8H",
  SCOUT_4D: "Scout Report 4D",
  SCOUT_DAILY: "Daily Recap (legacy)",
  SCOUT_WEEKLY: "Weekly Matrix (legacy)",
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

function ScoutInfoModal({ open, onClose }) {
  if (!open) return null;
  return (
    <div className="modal-overlay" onClick={onClose} style={{
      position:"fixed",inset:0,background:"rgba(0,0,0,0.75)",zIndex:9999,
      display:"flex",alignItems:"center",justifyContent:"center",padding:"2rem"
    }}>
      <div onClick={(e)=>e.stopPropagation()} style={{
        background:"#0f172a",
        border:"1px solid #1f2937",
        borderRadius:"12px",
        width:"95vw", maxWidth:"900px",
        height:"90vh",
        overflowY:"auto",
        padding:"2rem",
        color:"#e2e8f0",
        position:"relative"
      }}>
        <button onClick={onClose} style={{
          position:"absolute", top:"1rem", right:"1rem",
          background:"transparent", border:"1px solid #374151", color:"#94a3b8",
          width:"32px", height:"32px", borderRadius:"50%",
          cursor:"pointer", fontSize:"1.2rem"
        }}>×</button>
        <h2 style={{marginTop:0, color:"#f1f5f9"}}>Scout Findings — guida</h2>
        <p style={{color:"#94a3b8", marginBottom:"2rem"}}>
          Lo Scout e' l'agente AI (DeepSeek-V3) che ogni 20 minuti, 24/7,
          raccoglie notizie da 9 fonti diverse e le sintetizza in micro-schede
          azionabili. Ogni scheda ti dice in 2-3 frasi cosa e' successo, su quali
          ticker impatta, e se il sentiment e' positivo o negativo. Inoltre produce
          report aggregati ogni 8 ore e ogni 4 giorni per sintetizzare il contesto
          macro letto dal Decision Agent.
        </p>

        <h3 style={{color:"#f1f5f9"}}>Le fonti</h3>
        <div style={{display:"grid", gap:"0.8rem", marginBottom:"2rem"}}>
          <div style={{padding:"0.8rem", background:"#1e293b", borderRadius:"8px"}}>
            <div style={{display:"flex",alignItems:"center",gap:"0.5rem",marginBottom:"0.4rem"}}>
              <span className="badge" style={{background:"#0891b2"}}>GDELT</span>
              <strong>Eventi geopolitici globali</strong>
            </div>
            <div style={{fontSize:"0.85rem", color:"#cbd5e1"}}>
              GDELT Project monitora la copertura mediatica mondiale per individuare
              guerre, tensioni, sanzioni, crisi energetiche. E' la fonte piu' "macro" —
              ti dice se sta succedendo qualcosa di grosso a livello geopolitico.
            </div>
          </div>

          <div style={{padding:"0.8rem", background:"#1e293b", borderRadius:"8px"}}>
            <div style={{display:"flex",alignItems:"center",gap:"0.5rem",marginBottom:"0.4rem"}}>
              <span className="badge" style={{background:"#10b981"}}>NEWSAPI</span>
              <strong>News mainstream finanziarie</strong>
            </div>
            <div style={{fontSize:"0.85rem", color:"#cbd5e1"}}>
              Aggregatore di news dei principali quotidiani finanziari (Reuters,
              Bloomberg, FT, ecc.). Fonte istituzionale — copre quello che leggi
              sui giornali di settore.
            </div>
          </div>

          <div style={{padding:"0.8rem", background:"#1e293b", borderRadius:"8px"}}>
            <div style={{display:"flex",alignItems:"center",gap:"0.5rem",marginBottom:"0.4rem"}}>
              <span className="badge" style={{background:"#16a34a"}}>YFINANCE_NEWS</span>
              <strong>News specifiche per ticker</strong>
            </div>
            <div style={{fontSize:"0.85rem", color:"#cbd5e1"}}>
              News pertinenti SOLO ai ticker che il bot detiene o monitora
              (es. earnings di XOM, downgrade di LMT). Molto piu' mirato di
              GDELT/NewsAPI.
            </div>
          </div>

          <div style={{padding:"0.8rem", background:"#1e293b", borderRadius:"8px"}}>
            <div style={{display:"flex",alignItems:"center",gap:"0.5rem",marginBottom:"0.4rem"}}>
              <span className="badge" style={{background:"#f97316"}}>REDDIT</span>
              <span className="badge" style={{background:"#f97316"}}>RETAIL</span>
              <strong>Sentiment dei retail trader</strong>
            </div>
            <div style={{fontSize:"0.85rem", color:"#cbd5e1"}}>
              Top post da r/wallstreetbets, r/stocks, r/investing, r/options.
              Ti dice cosa pensa "la massa" — utile per leggere FOMO, panic,
              short squeeze e meme-stock movements. Spesso anticipa volatilita'
              su singoli titoli.
            </div>
          </div>

          <div style={{padding:"0.8rem", background:"#1e293b", borderRadius:"8px"}}>
            <div style={{display:"flex",alignItems:"center",gap:"0.5rem",marginBottom:"0.4rem"}}>
              <span className="badge" style={{background:"#f97316"}}>X</span>
              <span className="badge" style={{background:"#f97316"}}>RETAIL</span>
              <strong>Sentiment Twitter/X finance</strong>
            </div>
            <div style={{fontSize:"0.85rem", color:"#cbd5e1"}}>
              Tweet finance ad alto engagement. Richiede X_API_BEARER nelle
              impostazioni — se non configurato non produce schede.
            </div>
          </div>

          <div style={{padding:"0.8rem", background:"#1e293b", borderRadius:"8px"}}>
            <div style={{display:"flex",alignItems:"center",gap:"0.5rem",marginBottom:"0.4rem"}}>
              <span className="badge" style={{background:"#8b5cf6"}}>CLAWSTREET</span>
              <strong>Contesto mercati real-time</strong>
            </div>
            <div style={{fontSize:"0.85rem", color:"#cbd5e1"}}>
              Snapshot quotato dei principali indici (SPY, NASDAQ, DOW, BTC) +
              sentiment aggregato + performance settoriale dell'ultima sessione.
            </div>
          </div>

          <div style={{padding:"0.8rem", background:"#1e293b", borderRadius:"8px"}}>
            <div style={{display:"flex",alignItems:"center",gap:"0.5rem",marginBottom:"0.4rem"}}>
              <span className="badge" style={{background:"#dc2626"}}>CONGRESSIONAL</span>
              <strong>Insider del Congresso USA</strong>
            </div>
            <div style={{fontSize:"0.85rem", color:"#cbd5e1"}}>
              Trade dichiarati dai membri del Congresso americano (Pelosi,
              Burr, ecc.). Sono "insider strong signal" perche' siedono nelle
              commissioni che regolano i settori in cui investono. Filtrato a
              trade &gt; $50.000 negli ultimi 30 giorni.
            </div>
          </div>
        </div>

        <h3 style={{color:"#f1f5f9"}}>Sentiment score</h3>
        <p style={{color:"#cbd5e1", fontSize:"0.9rem"}}>
          Va da <span style={{color:"#ef4444",fontWeight:600}}>-1.0</span> (molto
          ribassista) a <span style={{color:"#10b981",fontWeight:600}}>+1.0</span>
          (molto rialzista). La barra colorata accanto a ogni scheda lo visualizza:
          larghezza = intensita', colore = direzione.
        </p>

        <h3 style={{color:"#f1f5f9"}}>Sentiment_type</h3>
        <ul style={{color:"#cbd5e1", fontSize:"0.9rem"}}>
          <li><strong>INSTITUTIONAL</strong>: viene da news ufficiali (GDELT,
            NewsAPI, yFinance, ClawStreet, Congressional). Riflette la posizione
            di analisti, fondi, governi.</li>
          <li><strong>RETAIL</strong>: viene da social (Reddit, X). Riflette
            cosa pensa la "folla" dei piccoli investitori. Spesso piu' rumoroso
            ma utile per anticipare squeeze e meme moves.</li>
        </ul>

        <h3 style={{color:"#f1f5f9"}}>Come usa lo Scout queste schede</h3>
        <p style={{color:"#cbd5e1", fontSize:"0.9rem"}}>
          Le micro-schede vengono salvate in un buffer condiviso. Il Watchdog
          (DeepSeek-V3, ogni 1 min) le legge insieme ai prezzi e decide se
          c'e' urgenza sufficiente per attivare il Decision Agent (Claude
          Sonnet 4.5, max 1 volta/ora). Se l'urgenza supera la soglia 5/10,
          parte la pipeline completa che potrebbe culminare in un trade.
        </p>

        <h3 style={{color:"#f1f5f9"}}>Risk keywords</h3>
        <p style={{color:"#cbd5e1", fontSize:"0.9rem"}}>
          Parole-chiave di rischio estratte dallo Scout (es. "conflict",
          "sanctions", "earnings_miss", "short_squeeze"). Le vedi in arancione
          sotto la scheda quando presenti.
        </p>

        <h3 style={{color:"#f1f5f9"}}>Filtri</h3>
        <p style={{color:"#cbd5e1", fontSize:"0.9rem"}}>
          I due dropdown in alto ti permettono di filtrare per fonte
          (es. solo Reddit per leggere il sentiment retail) o per tipo
          (Istituzionale vs Retail).
        </p>

        <div style={{marginTop:"2rem", paddingTop:"1rem", borderTop:"1px solid #1f2937", textAlign:"center"}}>
          <button onClick={onClose} className="btn btn-primary" style={{padding:"0.6rem 1.5rem"}}>
            Ho capito
          </button>
        </div>
      </div>
    </div>
  );
}

function ScoutBufferView({ items, loading }) {
  const [filterSource, setFilterSource] = useState("ALL");
  const [filterSentiment, setFilterSentiment] = useState("ALL");
  const [infoOpen, setInfoOpen] = useState(false);

  if (loading) return <div className="empty-state">Caricamento Scout findings...</div>;
  if (!items || items.length === 0) {
    return (
      <>
        <ScoutInfoModal open={infoOpen} onClose={()=>setInfoOpen(false)} />
        <div style={{display:"flex",justifyContent:"flex-end",marginBottom:"0.5rem"}}>
          <button onClick={()=>setInfoOpen(true)} title="Cosa significa tutto questo?" style={{
            width:"28px",height:"28px",borderRadius:"50%",
            background:"#1e293b",color:"#60a5fa",
            border:"1px solid #334155",cursor:"pointer",
            fontSize:"0.85rem",fontWeight:600
          }}>?</button>
        </div>
        <div className="empty-state">Nessuna scoperta dello Scout nelle ultime 24 ore. Lo Scout gira ogni 20 minuti — controlla di nuovo a breve.</div>
      </>
    );
  }

  const allSources = [...new Set(items.map(i => i.source_type || "UNKNOWN"))].sort();

  let filtered = items;
  if (filterSource !== "ALL") filtered = filtered.filter(i => (i.source_type || "UNKNOWN") === filterSource);
  if (filterSentiment !== "ALL") filtered = filtered.filter(i => (i.sentiment_type || "INSTITUTIONAL") === filterSentiment);

  const sourceColor = (s) => {
    if (!s) return "#64748b";
    if (s.includes("REDDIT") || s.includes("X")) return "#f97316";
    if (s.includes("GDELT")) return "#0891b2";
    if (s.includes("YFINANCE")) return "#16a34a";
    if (s.includes("NEWSAPI") || s.includes("NEWS")) return "#10b981";
    if (s.includes("CONGRESS")) return "#dc2626";
    if (s.includes("CLAW")) return "#8b5cf6";
    return "#64748b";
  };

  const sentimentBar = (score) => {
    const s = Number(score) || 0;
    const pct = Math.min(100, Math.abs(s) * 100);
    const color = s > 0.2 ? "#10b981" : s < -0.2 ? "#ef4444" : "#94a3b8";
    return (
      <div style={{display:"flex",alignItems:"center",gap:"0.4rem",fontSize:"0.7rem"}}>
        <span style={{color, fontWeight:600, minWidth:"3rem"}}>{s >= 0 ? "+" : ""}{s.toFixed(2)}</span>
        <div style={{width:"60px", height:"6px", background:"#1e293b", borderRadius:"3px", overflow:"hidden"}}>
          <div style={{width:`${pct}%`, height:"100%", background:color}} />
        </div>
      </div>
    );
  };

  return (
    <div>
      <ScoutInfoModal open={infoOpen} onClose={()=>setInfoOpen(false)} />
      <div className="thinking-header" style={{marginBottom:"1rem"}}>
        <span className="thinking-count">{filtered.length} di {items.length} micro-schede</span>
        <div style={{display:"flex",gap:"0.4rem",flexWrap:"wrap",alignItems:"center"}}>
          <select className="phase-filter" value={filterSource} onChange={(e)=>setFilterSource(e.target.value)}>
            <option value="ALL">Tutte le fonti ({items.length})</option>
            {allSources.map(s => (
              <option key={s} value={s}>{s} ({items.filter(i => (i.source_type||"UNKNOWN")===s).length})</option>
            ))}
          </select>
          <select className="phase-filter" value={filterSentiment} onChange={(e)=>setFilterSentiment(e.target.value)}>
            <option value="ALL">Tutti i sentiment</option>
            <option value="INSTITUTIONAL">Istituzionale</option>
            <option value="RETAIL">Retail (Reddit/X)</option>
          </select>
          <button onClick={()=>setInfoOpen(true)} title="Cosa significa tutto questo?" style={{
            width:"28px",height:"28px",borderRadius:"50%",
            background:"#1e293b",color:"#60a5fa",
            border:"1px solid #334155",cursor:"pointer",
            fontSize:"0.85rem",fontWeight:600
          }}>?</button>
        </div>
      </div>
      {filtered.map((item) => (
        <div key={item.id} className="intel-card" style={{padding:"0.8rem", marginBottom:"0.5rem"}}>
          <div className="intel-card-header" style={{marginBottom:"0.4rem"}}>
            <span className="badge" style={{background:sourceColor(item.source_type)}}>{item.source_type || "UNKNOWN"}</span>
            {item.sentiment_type === "RETAIL" && <span className="badge" style={{background:"#f97316"}}>RETAIL</span>}
            {sentimentBar(item.sentiment_score)}
            <span className="intel-date" style={{marginLeft:"auto"}}>{fmtDate(item.timestamp)}</span>
          </div>
          <div style={{fontSize:"0.85rem", color:"var(--text-primary)", lineHeight:1.4}}>
            {item.micro_summary || "(nessun summary)"}
          </div>
          {item.key_tickers && item.key_tickers.length > 0 && (
            <div className="asset-tags" style={{marginTop:"0.4rem"}}>
              {item.key_tickers.map((t,i) => <span key={i} className="ticker-tag">{t}</span>)}
            </div>
          )}
          {item.risk_keywords && item.risk_keywords.length > 0 && (
            <div style={{marginTop:"0.3rem", fontSize:"0.7rem", color:"#f87171"}}>
              ⚠ {item.risk_keywords.join(" · ")}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function IntelligencePage() {
  const [intelligence, setIntelligence] = useState([]);
  const [briefings, setBriefings] = useState([]);
  const [agentLogs, setAgentLogs] = useState([]);
  const [scoutBuffer, setScoutBuffer] = useState([]);
  const [scoutLoading, setScoutLoading] = useState(false);
  const [logsLoading, setLogsLoading] = useState(false);
  const [activeSection, setActiveSection] = useState("scout");
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

  const fetchScoutBuffer = useCallback(async () => {
    setScoutLoading(true);
    try {
      const res = await fetch(`${API}/api/scout-buffer?limit=150&hours=24`);
      if (res.ok) {
        const data = await res.json();
        setScoutBuffer(Array.isArray(data) ? data : []);
      }
    } catch (err) { console.error(err); }
    setScoutLoading(false);
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
    fetchScoutBuffer();
  }, [fetchIntelligence, fetchBriefings, fetchLogs, fetchScoutBuffer]);

  // Auto-refresh dello Scout buffer ogni 30s
  useEffect(() => {
    if (activeSection !== "scout") return;
    const interval = setInterval(fetchScoutBuffer, 30000);
    return () => clearInterval(interval);
  }, [activeSection, fetchScoutBuffer]);

  // Auto-refresh logs every 15s when on thinking tab
  useEffect(() => {
    if (activeSection !== "thinking") return;
    const interval = setInterval(fetchLogs, 15000);
    return () => clearInterval(interval);
  }, [activeSection, fetchLogs]);

  // parseJson tollera sia stringhe (SQLite) sia array gia' decodificati (Supabase JSON)
  const parseJson = (val) => {
    if (val == null) return [];
    if (Array.isArray(val)) return val;
    if (typeof val === "object") return [val];
    try { return JSON.parse(val); } catch { return []; }
  };

  // Count trades in logs
  const tradeCount = agentLogs.filter(l => l.phase === "DECISION_TRADE").length;

  return (
    <div>
      <h2 style={{fontSize:"1.3rem",marginBottom:"1.5rem",color:"var(--text-primary)"}}>Intelligence &amp; Thinking Process</h2>

      <div className="intel-tabs">
        <button className={`intel-tab ${activeSection === "scout" ? "active" : ""}`} onClick={() => setActiveSection("scout")}>
          Scout Findings ({scoutBuffer.length})
        </button>
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

      {activeSection === "scout" && (
        <ScoutBufferView items={scoutBuffer} loading={scoutLoading} />
      )}

      {activeSection === "thinking" && (
        <ThinkingProcess logs={agentLogs} loading={logsLoading} />
      )}

      {activeSection === "intelligence" && (
        <div>
          {(!Array.isArray(intelligence) || intelligence.length === 0) ? (
            <div className="empty-state">Nessuna intelligence weekend disponibile. La Daily/Weekly Recap dello Scout viene generata alle 23:59 CET ogni giorno (e domenica per la Weekly).</div>
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
                      {events.slice(0, isExp ? events.length : 3).map((e, i) => {
                        // Supporta sia stringhe sia oggetti {event, impact, tickers}
                        if (typeof e === "string") return <li key={i}>{e}</li>;
                        if (e && typeof e === "object") {
                          return (
                            <li key={i}>
                              <strong>{e.event || "(senza titolo)"}</strong>
                              {e.impact && <div style={{fontSize:"0.85em",color:"#94a3b8",marginTop:"0.2rem"}}>{e.impact}</div>}
                              {Array.isArray(e.tickers) && e.tickers.length > 0 && (
                                <div className="asset-tags" style={{marginTop:"0.3rem"}}>
                                  {e.tickers.map((t,j) => <span key={j} className="ticker-tag">{t}</span>)}
                                </div>
                              )}
                            </li>
                          );
                        }
                        return <li key={i}>{String(e)}</li>;
                      })}
                      {!isExp && events.length > 3 && <li className="more">+{events.length-3} altri...</li>}
                    </ul>
                  </div>
                )}
                {item.market_implications && (
                  <div className="intel-implications">
                    <strong>Implicazioni mercato:</strong>
                    <p>{isExp ? item.market_implications : (item.market_implications || "").substring(0,200) + ((item.market_implications || "").length > 200 ? "..." : "")}</p>
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
                    <div className="asset-tags">
                      {assets.map((a, i) => (
                        <span key={i} className="ticker-tag">
                          {typeof a === "string" ? a : (a && a.ticker) || JSON.stringify(a).substring(0, 20)}
                        </span>
                      ))}
                    </div>
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
