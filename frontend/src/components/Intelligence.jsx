import React, { useState, useEffect, useCallback } from "react";

const API = window.location.origin;

const fmtDate = (ts) => {
  if (!ts) return "--";
  const d = new Date(ts);
  return d.toLocaleString("it-IT", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
};

function Intelligence() {
  const [intelligence, setIntelligence] = useState([]);
  const [briefings, setBriefings] = useState([]);
  const [activeSection, setActiveSection] = useState("intelligence");
  const [expandedId, setExpandedId] = useState(null);

  const fetchIntelligence = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/intelligence?limit=20`);
      if (res.ok) {
        const data = await res.json();
        setIntelligence(Array.isArray(data) ? data : []);
      }
    } catch (err) {
      console.error("Errore fetch intelligence:", err);
    }
  }, []);

  const fetchBriefings = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/briefings?limit=20`);
      if (res.ok) {
        const data = await res.json();
        setBriefings(Array.isArray(data) ? data : []);
      }
    } catch (err) {
      console.error("Errore fetch briefings:", err);
    }
  }, []);

  useEffect(() => {
    fetchIntelligence();
    fetchBriefings();
  }, [fetchIntelligence, fetchBriefings]);

  const parseJsonSafe = (str) => {
    if (!str) return [];
    try {
      return JSON.parse(str);
    } catch {
      return [];
    }
  };

  return (
    <div className="intelligence-page">
      <h2>Intelligence &amp; Briefings</h2>

      <div className="intel-tabs">
        <button
          className={`intel-tab ${activeSection === "intelligence" ? "active" : ""}`}
          onClick={() => setActiveSection("intelligence")}
        >
          Weekend Intelligence ({intelligence.length})
        </button>
        <button
          className={`intel-tab ${activeSection === "briefings" ? "active" : ""}`}
          onClick={() => setActiveSection("briefings")}
        >
          Pre-Market Briefings ({briefings.length})
        </button>
      </div>

      {activeSection === "intelligence" && (
        <div className="intel-list">
          {intelligence.length === 0 ? (
            <div className="empty-state">Nessuna intelligence weekend disponibile</div>
          ) : (
            intelligence.map((item) => {
              const events = parseJsonSafe(item.key_events);
              const isExpanded = expandedId === `intel-${item.id}`;
              return (
                <div
                  key={item.id}
                  className={`intel-card ${isExpanded ? "expanded" : ""}`}
                  onClick={() =>
                    setExpandedId(isExpanded ? null : `intel-${item.id}`)
                  }
                >
                  <div className="intel-card-header">
                    <span className="intel-badge weekend">WEEKEND</span>
                    <span className="intel-date">{fmtDate(item.saved_at)}</span>
                    <span className="intel-run">
                      Run: {(item.run_id || "").substring(0, 8)}
                    </span>
                  </div>

                  {events.length > 0 && (
                    <div className="intel-events">
                      <strong>Eventi chiave:</strong>
                      <ul>
                        {events.slice(0, isExpanded ? events.length : 3).map(
                          (evt, i) => (
                            <li key={i}>{evt}</li>
                          )
                        )}
                        {!isExpanded && events.length > 3 && (
                          <li className="more">
                            +{events.length - 3} altri eventi...
                          </li>
                        )}
                      </ul>
                    </div>
                  )}

                  {item.market_implications && (
                    <div className="intel-implications">
                      <strong>Implicazioni mercato:</strong>
                      <p>
                        {isExpanded
                          ? item.market_implications
                          : item.market_implications.substring(0, 200) +
                            (item.market_implications.length > 200
                              ? "..."
                              : "")}
                      </p>
                    </div>
                  )}

                  {isExpanded && item.content && (
                    <div className="intel-full">
                      <strong>Analisi completa:</strong>
                      <p>{item.content}</p>
                    </div>
                  )}
                </div>
              );
            })
          )}
        </div>
      )}

      {activeSection === "briefings" && (
        <div className="intel-list">
          {briefings.length === 0 ? (
            <div className="empty-state">
              Nessun briefing pre-market disponibile
            </div>
          ) : (
            briefings.map((item) => {
              const assets = parseJsonSafe(item.priority_assets);
              const isExpanded = expandedId === `brief-${item.id}`;
              return (
                <div
                  key={item.id}
                  className={`intel-card ${isExpanded ? "expanded" : ""}`}
                  onClick={() =>
                    setExpandedId(isExpanded ? null : `brief-${item.id}`)
                  }
                >
                  <div className="intel-card-header">
                    <span className="intel-badge premarket">PRE-MARKET</span>
                    {item.market_session && (
                      <span className="intel-badge session">
                        {item.market_session}
                      </span>
                    )}
                    <span className="intel-date">{fmtDate(item.saved_at)}</span>
                    <span className="intel-run">
                      Run: {(item.run_id || "").substring(0, 8)}
                    </span>
                  </div>

                  {assets.length > 0 && (
                    <div className="intel-assets">
                      <strong>Asset prioritari:</strong>
                      <div className="asset-tags">
                        {assets.map((a, i) => (
                          <span key={i} className="ticker-tag">
                            {a}
                          </span>
                        ))}
                      </div>
                    </div>
                  )}

                  <div className="intel-content">
                    <p>
                      {isExpanded
                        ? item.content
                        : (item.content || "").substring(0, 300) +
                          ((item.content || "").length > 300 ? "..." : "")}
                    </p>
                  </div>
                </div>
              );
            })
          )}
        </div>
      )}
    </div>
  );
}

export default Intelligence;
