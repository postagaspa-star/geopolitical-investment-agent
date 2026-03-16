import React, { useState, useEffect, useCallback, useRef } from "react";
import { BrowserRouter, Routes, Route } from "react-router-dom";
import { Menu } from "lucide-react";

import Sidebar from "./components/Sidebar";
import DashboardPage from "./pages/DashboardPage";
import IntelligencePage from "./pages/IntelligencePage";
import AnalyticsPage from "./pages/AnalyticsPage";
import Settings from "./components/Settings";

const API = window.location.origin;

function AppContent() {
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
  const [sidebarOpen, setSidebarOpen] = useState(false);

  const countdownRef = useRef(null);

  const fetchData = useCallback(async (endpoint) => {
    try {
      const res = await fetch(`${API}${endpoint}`);
      if (!res.ok) return null;
      return await res.json();
    } catch { return null; }
  }, []);

  const fetchAll = useCallback(async () => {
    const [pData, posData, tData, statusData, logsData] = await Promise.all([
      fetchData("/api/portfolio"),
      fetchData("/api/positions"),
      fetchData("/api/trades"),
      fetchData("/api/agent/status"),
      fetchData("/api/logs"),
    ]);
    if (pData) setPortfolio(pData);
    if (posData) setPositions(Array.isArray(posData) ? posData : []);
    if (tData) setTrades(Array.isArray(tData) ? tData : []);
    if (logsData) setLogs(Array.isArray(logsData) ? logsData : []);
    if (statusData) {
      setAgentStatus(statusData.status || "idle");
      setSchedulerRunning(!!statusData.scheduler_running);
      setCurrentMode(statusData.mode || "idle");
      setMarketOpen(!!statusData.market_open);
      setNextRun(statusData.next_run || null);
      setNextMarketOpen(statusData.next_market_open || null);
    }
  }, [fetchData]);

  useEffect(() => {
    fetchAll();
    const interval = setInterval(fetchAll, 15000);
    return () => clearInterval(interval);
  }, [fetchAll]);

  // Countdown timers
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
    try {
      await fetch(`${API}/api/agent/start`, { method: "POST" });
      setSchedulerRunning(true);
      fetchAll();
    } catch (err) { console.error(err); }
  };

  const handleStopMonitoring = async () => {
    try {
      await fetch(`${API}/api/agent/stop`, { method: "POST" });
      setSchedulerRunning(false);
      setCountdown("");
      fetchAll();
    } catch (err) { console.error(err); }
  };

  const handleRunOnce = async () => {
    setAgentStatus("running");
    try {
      await fetch(`${API}/api/agent/run`, { method: "POST" });
      setTimeout(fetchAll, 2000);
    } catch { setAgentStatus("error"); }
  };

  return (
    <div className="app-layout">
      {/* Mobile header */}
      <div className="mobile-header">
        <button className="hamburger" onClick={() => setSidebarOpen(true)}>
          <Menu />
        </button>
        <span style={{fontWeight:700, fontSize:"1rem"}}>GeoInvest AI</span>
        <div style={{width:24}} />
      </div>

      <Sidebar
        schedulerRunning={schedulerRunning}
        currentMode={currentMode}
        countdown={countdown}
        marketCountdown={marketCountdown}
        marketOpen={marketOpen}
        logs={logs}
        portfolio={portfolio}
        agentStatus={agentStatus}
        onStartMonitoring={handleStartMonitoring}
        onStopMonitoring={handleStopMonitoring}
        onRunOnce={handleRunOnce}
        sidebarOpen={sidebarOpen}
        onCloseSidebar={() => setSidebarOpen(false)}
      />

      <main className="main-content">
        <Routes>
          <Route path="/" element={
            <DashboardPage portfolio={portfolio} positions={positions} trades={trades} logs={logs} />
          } />
          <Route path="/intelligence" element={<IntelligencePage />} />
          <Route path="/analytics" element={<AnalyticsPage />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </main>
    </div>
  );
}

function App() {
  return (
    <BrowserRouter>
      <AppContent />
    </BrowserRouter>
  );
}

export default App;
