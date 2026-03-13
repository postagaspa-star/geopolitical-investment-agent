import React, { useState, useEffect, useCallback, useRef } from "react";
import Portfolio from "./components/Portfolio";
import TradeHistory from "./components/TradeHistory";
import AgentLog from "./components/AgentLog";
import GeopoliticalFeed from "./components/GeopoliticalFeed";
import TechnicalChart from "./components/TechnicalChart";
import Settings from "./components/Settings";
import Intelligence from "./components/Intelligence";

// Base URL per le chiamate API (stesso server)
const API = window.location.origin;

// Etichette modalita'
const MODE_LABELS = {
  full: "\u{1F7E2} Mercati Aperti",
  pre_market: "\u{1F305} Mercati Chiusi",
  weekend: "\u{1F4C5} Weekend - Solo Lettura",
  idle: "\u26AA Inattivo",
};

function App() {
  const [portfolio, setPortfolio] = useState(null);
  const [positions, setPositions] = useState([]);
  const [trades, setTrades] = useState([]);
  const [logs, setLogs] = useState([]);
  const [agentStatus, setAgentStatus] = useState("idle");
  const [schedulerRunning, setSchedulerRunning] = useState(false);
  const [currentMode, setCurrentMode] = useState("idle");
  const [marketOpen, setMarketOpen] = useState(false);
  const [nextRun, setNextRun] = useState(null);
  const [nextMarketOpen, setNextMarketOpen] = useState(null);
  const [countdown, setCountdown] = useState("");
  const [marketCountdown, setMarketCountdown] = useState("");
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState("dashboard");

  const countdownRef = useRef(null);

  const fetchData = useCallback(async (endpoint) => {
    try {
      const res = await fetch(`${API}${endpoint}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return await res.json();
    } catch (err) {
      console.error(`Errore fetch ${endpoint}:`, err);
      return null;
    }
  }, []);

  const fetchPortfolioData = useCallback(async () => {
    const [pData, posData, tData, statusData] = await Promise.all([
      fetchData("/api/portfolio"),
      fetchData("/api/positions"),
      fetchData("/api/trades"),
      fetchData("/api/agent/status"),
    ]);

    if (pData) setPortfolio(pData);
    if (posData) setPositions(Array.isArray(posData) ? posData : []);
    if (tData) setTrades(Array.isArray(tData) ? tData : []);
    if (statusData) {
      setAgentStatus(statusData.status || "idle");
      setSchedulerRunning(!!statusData.scheduler_running);
      setCurrentMode(statusData.mode || "idle");
      setMarketOpen(!!statusData.market_open);
      setNextRun(statusData.next_run || null);
      setNextMarketOpen(statusData.next_market_open || null);
    }

    setLoading(false);
  }, [fetchData]);

  const fetchLogs = useCallback(async () => {
    const data = await fetchData("/api/logs");
    if (data) setLogs(Array.isArray(data) ? data : []);
  }, [fetchData]);

  // Polling
  useEffect(() => {
    fetchPortfolioData();
    fetchLogs();
    const portfolioInterval = setInterval(fetchPortfolioData, 15000);
    const logsInterval = setInterval(fetchLogs, 10000);
    return () => {
      clearInterval(portfolioInterval);
      clearInterval(logsInterval);
    };
  }, [fetchPortfolioData, fetchLogs]);

  // Countdown timer
  useEffect(() => {
    if (countdownRef.current) clearInterval(countdownRef.current);
    countdownRef.current = setInterval(() => {
      if (nextRun && schedulerRunning) {
        const diff = new Date(nextRun) - new Date();
        if (diff > 0) {
          const m = Math.floor(diff / 60000);
          const s = Math.floor((diff % 60000) / 1000);
          setCountdown(`${m}m ${s}s`);
        } else {
          setCountdown("in corso...");
        }
      } else {
        setCountdown("");
      }

      if (nextMarketOpen && !marketOpen) {
        const diff = new Date(nextMarketOpen) - new Date();
        if (diff > 0) {
          const h = Math.floor(diff / 3600000);
          const m = Math.floor((diff % 3600000) / 60000);
          setMarketCountdown(`${h}h ${m}m`);
        } else {
          setMarketCountdown("");
        }
      } else {
        setMarketCountdown("");
      }
    }, 1000);
    return () => clearInterval(countdownRef.current);
  }, [nextRun, nextMarketOpen, schedulerRunning, marketOpen]);

  const handleStartMonitoring = async () => {
    setError(null);
    try {
      const res = await fetch(`${API}/api/agent/start`, { method: "POST" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setSchedulerRunning(true);
      fetchPortfolioData();
    } catch (err) {
      setError(err.message);
    }
  };

  const handleStopMonitoring = async () => {
    setError(null);
    try {
      const res = await fetch(`${API}/api/agent/stop`, { method: "POST" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setSchedulerRunning(false);
      setCountdown("");
      fetchPortfolioData();
    } catch (err) {
      setError(err.message);
    }
  };

  const handleRunOnce = async () => {
    setAgentStatus("running");
    setError(null);
    try {
      const res = await fetch(`${API}/api/agent/run`, { method: "POST" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setTimeout(() => {
        fetchPortfolioData();
        fetchLogs();
      }, 2000);
    } catch (err) {
      setError(err.message);
      setAgentStatus("error");
    }
  };

  return (
    <div className="app">
      <header className="header">
        <div className="header-title">
          <span>Geopolitical Investment Agent</span>
        </div>

        <div className="header-center">
          <span className="mode-label">{MODE_LABELS[currentMode] || currentMode}</span>
          {schedulerRunning && countdown && (
            <span className="countdown-label">
              Prossimo run: <strong>{countdown}</strong>
            </span>
          )}
          {!marketOpen && marketCountdown && (
            <span className="countdown-label market-countdown">
              Apertura: <strong>{marketCountdown}</strong>
            </span>
          )}
        </div>

        <div className="header-controls">
          <span className={`status-dot ${schedulerRunning ? "monitoring" : agentStatus}`} />
          <span className="status-label">
            {schedulerRunning ? "MONITORAGGIO" : agentStatus.toUpperCase()}
          </span>

          {schedulerRunning ? (
            <button className="btn-stop" onClick={handleStopMonitoring}>
              Ferma Monitoraggio
            </button>
          ) : (
            <button
              className="btn-run"
              onClick={handleStartMonitoring}
              disabled={agentStatus === "running"}
            >
              Avvia Monitoraggio Continuo
            </button>
          )}

          {!schedulerRunning && (
            <button
              className="btn-run-once"
              onClick={handleRunOnce}
              disabled={agentStatus === "running"}
              title="Esegui un singolo ciclo"
            >
              &#9654;
            </button>
          )}

          <div className="tab-nav">
            <button
              className={`tab-btn ${activeTab === "dashboard" ? "active" : ""}`}
              onClick={() => setActiveTab("dashboard")}
            >
              Dashboard
            </button>
            <button
              className={`tab-btn ${activeTab === "intelligence" ? "active" : ""}`}
              onClick={() => setActiveTab("intelligence")}
            >
              Intelligence
            </button>
          </div>

          <button
            className="btn-settings"
            onClick={() => setActiveTab(activeTab === "settings" ? "dashboard" : "settings")}
            title="Impostazioni"
          >
            {activeTab === "settings" ? "\u2715" : "\u2699"}
          </button>
        </div>
      </header>

      {error && (
        <div style={{ padding: "0.5rem 2rem" }}>
          <div className="error-state">{error}</div>
        </div>
      )}

      <div className="dashboard-container">
        {activeTab === "settings" ? (
          <Settings onBack={() => setActiveTab("dashboard")} />
        ) : activeTab === "intelligence" ? (
          <Intelligence />
        ) : loading ? (
          <div className="loading-state">Caricamento dashboard...</div>
        ) : (
          <div className="dashboard-grid">
            <div className="full-width">
              <Portfolio portfolio={portfolio} trades={trades} />
            </div>
            <GeopoliticalFeed logs={logs} />
            <TechnicalChart positions={positions} trades={trades} />
            <div className="full-width">
              <TradeHistory trades={trades} />
            </div>
            <div className="full-width">
              <AgentLog logs={logs} />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default App;
