import { NavLink } from 'react-router-dom';
import { useState, useEffect } from 'react';
import {
  LayoutDashboard,
  Brain,
  BarChart3,
  Settings,
  Play,
  Square,
  Zap,
  Trophy,
  ExternalLink,
} from 'lucide-react';

const MODE_LABELS = {
  full: 'Mercati Aperti',
  pre_market: 'Mercati Chiusi - Pre Market',
  weekend: 'Weekend - Solo Lettura',
  idle: 'Inattivo',
};

const AGENT_LABELS = {
  watchdog: { name: 'Watchdog', tone: '#fbbf24' },
  scout: { name: 'Scout', tone: '#a78bfa' },
  technical: { name: 'Technical', tone: '#06b6d4' },
  decision: { name: 'Decision', tone: '#10b981' },
};

const formatEUR = (value) =>
  new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR' }).format(value ?? 0);

// Calcola tempo restante in formato umano (1h 5m, 4m 12s, ...)
function formatCountdown(isoString) {
  if (!isoString) return null;
  const diff = new Date(isoString) - new Date();
  if (diff <= 0) return 'a breve';
  const totalSec = Math.floor(diff / 1000);
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

// Formatta timestamp ultimo run come "5m fa", "2h fa"
function formatTimeAgo(isoString) {
  if (!isoString) return null;
  const diff = new Date() - new Date(isoString);
  if (diff < 0) return null;
  const totalSec = Math.floor(diff / 1000);
  if (totalSec < 60) return `${totalSec}s fa`;
  const m = Math.floor(totalSec / 60);
  if (m < 60) return `${m}m fa`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h fa`;
  const d = Math.floor(h / 24);
  return `${d}g fa`;
}

function AgentRow({ agentKey, info, tick }) {
  const meta = AGENT_LABELS[agentKey] || { name: agentKey, tone: '#9ca3af' };
  const nextRun = info?.next_run;
  const lastRun = info?.last_run;
  const active = !!info?.active;

  // Mostra il countdown se c'e' un next_run; altrimenti "ultimo: 5m fa"; altrimenti "in attesa"
  let statusText;
  let statusColor;
  if (nextRun) {
    statusText = `tra ${formatCountdown(nextRun)}`;
    statusColor = '#94a3b8';
  } else if (lastRun) {
    statusText = `ultimo: ${formatTimeAgo(lastRun)}`;
    statusColor = '#64748b';
  } else {
    statusText = active ? 'in attesa di trigger' : 'inattivo';
    statusColor = '#64748b';
  }

  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      gap: '0.5rem',
      padding: '0.35rem 0',
      fontSize: '0.75rem',
    }}>
      <span style={{
        width: '6px',
        height: '6px',
        borderRadius: '50%',
        background: active ? meta.tone : '#475569',
        flexShrink: 0,
      }} />
      <span style={{
        color: active ? '#e2e8f0' : '#94a3b8',
        fontWeight: 500,
        minWidth: '70px',
      }}>
        {meta.name}
      </span>
      <span style={{
        color: statusColor,
        fontSize: '0.7rem',
        flexShrink: 1,
        overflow: 'hidden',
        textOverflow: 'ellipsis',
      }}>
        {statusText}
      </span>
    </div>
  );
}

export default function Sidebar({
  schedulerRunning,
  currentMode,
  marketCountdown,
  marketOpen,
  portfolio,
  agentStatus,
  agentsInfo,
  onStartMonitoring,
  onStopMonitoring,
  onRunOnce,
  sidebarOpen,
  onCloseSidebar,
}) {
  const modeLabel = MODE_LABELS[currentMode] ?? currentMode ?? 'Inattivo';
  const isRunning = agentStatus === 'running';

  // tick state per forzare il refresh dei countdown ogni secondo
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  // ClawStreet status
  const [csStatus, setCsStatus] = useState(null);
  const [csRegistering, setCsRegistering] = useState(false);
  const API = window.location.origin;

  useEffect(() => {
    fetch(`${API}/api/clawstreet/status`)
      .then(r => r.json())
      .then(d => setCsStatus(d))
      .catch(() => {});
  }, [API]);

  const handleRegisterCS = async () => {
    setCsRegistering(true);
    try {
      const res = await fetch(`${API}/api/clawstreet/register`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}'
      });
      if (res.ok) {
        const updated = await fetch(`${API}/api/clawstreet/status`).then(r => r.json());
        setCsStatus(updated);
      }
    } catch {}
    setCsRegistering(false);
  };

  const totalValue = portfolio?.total_value ?? 0;
  const cashBalance = portfolio?.cash ?? portfolio?.cash_balance ?? 0;
  const initialCapital = portfolio?.initial_balance ?? 100000;
  const pnlPct = initialCapital > 0 ? ((totalValue - initialCapital) / initialCapital) * 100 : 0;
  const pnlPositive = pnlPct >= 0;

  const agentOrder = ['watchdog', 'scout', 'technical', 'decision'];

  return (
    <>
      {/* Mobile overlay */}
      <div
        className={`sidebar-overlay${sidebarOpen ? ' open' : ''}`}
        onClick={onCloseSidebar}
        aria-hidden="true"
      />

      <aside className={`sidebar${sidebarOpen ? ' open' : ''}`}>
        {/* Logo */}
        <div className="sidebar-logo">
          <span className={`logo-dot${schedulerRunning ? ' active' : ' inactive'}`} />
          <h1>GeoInvest AI</h1>
        </div>

        {/* Navigation */}
        <nav className="sidebar-nav">
          <NavLink to="/" end
            className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}
            onClick={onCloseSidebar}>
            <LayoutDashboard /> Dashboard
          </NavLink>
          <NavLink to="/intelligence"
            className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}
            onClick={onCloseSidebar}>
            <Brain /> Intelligence
          </NavLink>
          <NavLink to="/analytics"
            className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}
            onClick={onCloseSidebar}>
            <BarChart3 /> Analytics
          </NavLink>
          <NavLink to="/settings"
            className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}
            onClick={onCloseSidebar}>
            <Settings /> Impostazioni
          </NavLink>
        </nav>

        {/* Status section */}
        <div className="sidebar-status">
          <div className="status-mode">Modalità</div>
          <div className="status-mode-value">{modeLabel}</div>

          <div className="status-countdown" style={{ marginTop: '0.25rem' }}>
            Arch:{' '}
            <strong style={{ color: currentMode === 'full' ? '#a78bfa' : '#94a3b8' }}>
              {currentMode === 'full' ? 'Multi-Agent (Scout→Tech→Decision)' : 'Single-Agent'}
            </strong>
          </div>

          {marketCountdown && !marketOpen && (
            <div className="status-countdown" style={{ marginTop: '0.25rem' }}>
              Apertura mercati: <strong>{marketCountdown}</strong>
            </div>
          )}

          {marketOpen && (
            <div className="status-countdown" style={{ marginTop: '0.25rem' }}>
              Mercati: <strong style={{ color: 'var(--positive)' }}>Aperti</strong>
            </div>
          )}
        </div>

        {/* Agent pipeline status — sostituisce "Attività Recente" */}
        <div className="sidebar-activity">
          <div className="activity-title">Pipeline Agenti</div>
          {schedulerRunning ? (
            agentOrder.map((key) => (
              <AgentRow key={key} agentKey={key} info={agentsInfo?.[key]} />
            ))
          ) : (
            <div style={{ color: 'var(--text-muted)', fontSize: '0.75rem', padding: '0.35rem 0' }}>
              Scheduler fermo
            </div>
          )}
        </div>

        {/* Controls */}
        <div className="sidebar-controls">
          {schedulerRunning ? (
            <button className="btn btn-danger" onClick={onStopMonitoring}>
              <Square /> Ferma Monitoraggio
            </button>
          ) : (
            <button className="btn btn-primary" onClick={onStartMonitoring}>
              <Play /> Riavvia Monitoraggio
            </button>
          )}
          <button className="btn btn-secondary"
            onClick={onRunOnce} disabled={isRunning}>
            <Zap /> Esegui Manuale
          </button>
        </div>

        {/* Portfolio summary */}
        <div className="sidebar-portfolio">
          <div className="portfolio-label">Portafoglio</div>
          <div className="portfolio-value">{formatEUR(totalValue)}</div>
          <div className="portfolio-change"
            style={{ color: pnlPositive ? 'var(--positive)' : 'var(--negative)' }}>
            {pnlPositive ? '+' : ''}{pnlPct.toFixed(2)}%
          </div>
          <div className="status-countdown" style={{ marginTop: '0.35rem' }}>
            Liquidità: <strong>{formatEUR(cashBalance)}</strong>
          </div>
        </div>

        {/* ClawStreet */}
        <div className="sidebar-portfolio" style={{
          borderTop: '1px solid var(--border)', paddingTop: '0.75rem', marginTop: '0.5rem',
        }}>
          <div className="portfolio-label" style={{
            display: 'flex', alignItems: 'center', gap: '0.35rem',
          }}>
            <Trophy size={14} /> ClawStreet
          </div>
          {csStatus?.registered ? (
            <>
              <div className="status-countdown" style={{ marginTop: '0.25rem' }}>
                Bot: <strong>{csStatus.bot_name || 'GeoInvest AI'}</strong>
              </div>
              <a href="https://www.clawstreet.io/agents/geoinvest-ai"
                target="_blank" rel="noopener noreferrer"
                className="status-countdown"
                style={{
                  display: 'flex', alignItems: 'center', gap: '0.25rem',
                  marginTop: '0.25rem', color: 'var(--primary)',
                  textDecoration: 'none', fontSize: '0.75rem',
                }}>
                <ExternalLink size={12} /> Pagina pubblica
              </a>
            </>
          ) : (
            <button className="btn btn-secondary"
              style={{ marginTop: '0.35rem', fontSize: '0.75rem', padding: '0.3rem 0.6rem' }}
              onClick={handleRegisterCS} disabled={csRegistering}>
              {csRegistering ? 'Registrando...' : 'Registra bot pubblico'}
            </button>
          )}
        </div>
      </aside>
    </>
  );
}
