import React, { useState, useEffect, useCallback } from "react";

const API = window.location.origin;

const fmtDate = (ts) => {
  if (!ts) return "--";
  const d = new Date(ts);
  return d.toLocaleString("it-IT", {
    day: "2-digit", month: "2-digit", year: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
};

function IntelligencePage() {
  const [intelligence, setIntelligence] = useState([]);
  const [briefings, setBriefings] = useState([]);
  const [activeSection, setActiveSection] = useState("intelligence");
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

  useEffect(() => { fetchIntelligence(); fetchBriefings(); }, [fetchIntelligence, fetchBriefings]);

  const parseJson = (str) => { try { return JSON.parse(str); } catch { return []; } };

  return (
    <div>
      <h2 style={{fontSize:"1.3rem",marginBottom:"1.5rem",color:"var(--text-primary)"}}>Intelligence &amp; Briefings</h2>

      <div className="intel-tabs">
        <button className={`intel-tab ${activeSection === "intelligence" ? "active" : ""}`} onClick={() => setActiveSection("intelligence")}>
          Weekend Intelligence ({Array.isArray(intelligence) ? intelligence.length : 0})
        </button>
        <button className={`intel-tab ${activeSection === "briefings" ? "active" : ""}`} onClick={() => setActiveSection("briefings")}>
          Pre-Market Briefings ({Array.isArray(briefings) ? briefings.length : 0})
        </button>
      </div>

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
