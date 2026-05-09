import React, { useState, useEffect, useCallback, useRef } from "react";
import { BrowserRouter, Routes, Route, Navigate, NavLink } from "react-router-dom";
import { Menu, BookOpen } from "lucide-react";

import Sidebar from "./components/Sidebar";
import DashboardPage from "./pages/DashboardPage";
import IntelligencePage from "./pages/IntelligencePage";
import AnalyticsPage from "./pages/AnalyticsPage";
import CoachCardsPage from "./pages/CoachCardsPage";
import ChatDecisionPage from "./pages/ChatDecisionPage";
import Settings from "./components/Settings";
import ChatWidget from "./components/ChatWidget";

import EntryPage from "./pages/EntryPage";
import SimulatorLayout from "./pages/simulator/SimulatorLayout";
import SimDashboard from "./pages/simulator/SimDashboard";
import SimRunner from "./pages/simulator/SimRunner";
import SimResult from "./pages/simulator/SimResult";
import SimHistory from "./pages/simulator/SimHistory";
import SimMemory from "./pages/simulator/SimMemory";

const API = window.location.origin;

/**
 * LiveApp — la dashboard "Live" (bot operativo). Router interno con prefix /live.
 */
function LiveApp() {
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
  const [agentsInfo, setAgentsInfo] = useState({});

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
      setAgentsInfo(statusData.agents || {});
    }
  }, [fetchData]);

  useEffect(() => {
    fetchAll();
    const interval = setInterval(fetchAll, 15000);
    return () => clearInterval(interval);
  }, [fetchAll]);

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
      <div className="mobile-header">
        <button className="hamburger" onClick={() => setSidebarOpen(true)}>
          <Menu />
        </button>
        <span style={{fontWeight:700, fontSize:"1rem"}}>GeoInvest AI Live</span>
        <div style={{width:24}} />
      </div>

      {/* Floating Coach Cards button — sempre visibile in alto a destra,
          sostituisce il vecchio link Coach Cards nella sidebar */}
      <NavLink
        to="/live/coach-cards"
        title="Coach Cards (raccomandazioni operative)"
        style={({ isActive }) => ({
          position: "fixed",
          top: "1rem",
          right: "1rem",
          zIndex: 100,
          background: isActive ? "#fbbf24" : "#1e293b",
          color: isActive ? "#0f172a" : "#fbbf24",
          border: `1px solid ${isActive ? "#fbbf24" : "#334155"}`,
          borderRadius: "50%",
          width: "40px",
          height: "40px",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          boxShadow: "0 2px 8px rgba(0,0,0,0.3)",
          textDecoration: "none",
          cursor: "pointer",
        })}>
        <BookOpen size={18} />
      </NavLink>

      <Sidebar
        schedulerRunning={schedulerRunning}
        currentMode={currentMode}
        countdown={countdown}
        marketCountdown={marketCountdown}
        marketOpen={marketOpen}
        portfolio={portfolio}
        agentStatus={agentStatus}
        agentsInfo={agentsInfo}
        onStartMonitoring={handleStartMonitoring}
        onStopMonitoring={handleStopMonitoring}
        onRunOnce={handleRunOnce}
        onDataRefresh={fetchAll}
        sidebarOpen={sidebarOpen}
        onCloseSidebar={() => setSidebarOpen(false)}
      />

      <main className="main-content">
        <Routes>
          <Route index element={
            <DashboardPage portfolio={portfolio} positions={positions} trades={trades} logs={logs} />
          } />
          <Route path="intelligence" element={<IntelligencePage />} />
          <Route path="analytics" element={<AnalyticsPage />} />
          <Route path="coach-cards" element={<CoachCardsPage />} />
          <Route path="chat" element={<ChatDecisionPage />} />
          <Route path="settings" element={<Settings onDataRefresh={fetchAll} />} />
        </Routes>
      </main>

      {/* Chat widget flottante: solo nella Live (analizza decisioni reali) */}
      <ChatWidget />
    </div>
  );
}

function App() {
  return (
    <BrowserRouter>
      <Routes>
        {/* Entry page con scelta Live/Simulator */}
        <Route path="/" element={<EntryPage />} />

        {/* Live: dashboard bot operativo (route esistente "/" interna) */}
        <Route path="/live/*" element={<LiveApp />} />

        {/* Simulator: ambiente test scenari storici */}
        <Route path="/simulator" element={<SimulatorLayout />}>
          <Route index element={<SimDashboard />} />
          <Route path="runner" element={<SimRunner />} />
          <Route path="result/:runId" element={<SimResult />} />
          <Route path="history" element={<SimHistory />} />
          <Route path="memory" element={<SimMemory />} />
        </Route>

        {/* Fallback: redirect a entry page */}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}

export default App;
