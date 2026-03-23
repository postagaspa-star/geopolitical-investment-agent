import { NavLink } from 'react-router-dom';
import { useState, useEffect } from 'react';
import {
  LayoutDashboard,
  Brain,
  BarChart3,
  Settings,
  Globe,
  TrendingUp,
  Activity,
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

const formatEUR = (value) =>
  new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR' }).format(value ?? 0);

function getActivityIcon(log) {
  const text = (log?.content ?? log?.message ?? log ?? '').toString().toUpperCase();
  if (text.includes('GEOPOLIT')) {
    return <Globe className="activity-icon geo" />;
  }
  if (text.includes('DECISION') || text.includes('EXECUTION') || text.includes('TRADE')) {
    return <TrendingUp className="activity-icon trade" />;
  }
  return <Activity className="activity-icon monitor" />;
}

function getLogText(log) {
  if (typeof log === 'string') return log;
  if (log && typeof log === 'object') {
    return log.content ?? log.message ?? log.text ?? log.event ?? JSON.stringify(log);
  }
  return String(log);
}

export default function Sidebar({
  schedulerRunning,
  currentMode,
  countdown,
  marketCountdown,
  marketOpen,
  logs,
  portfolio,
  agentStatus,
  onStartMonitoring,
  onStopMonitoring,
  onRunOnce,
  sidebarOpen,
  onCloseSidebar,
}) {
  const modeLabel = MODE_LABELS[currentMode] ?? currentMode ?? 'Inattivo';
  const isRunning = agentStatus === 'running';
  const recentLogs = Array.isArray(logs) ? logs.slice(-5).reverse() : [];

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
      const res = await fetch(`${API}/api/clawstreet/register`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
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
          <NavLink
            to="/"
            end
            className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}
            onClick={onCloseSidebar}
          >
            <LayoutDashboard />
            Dashboard
          </NavLink>
          <NavLink
            to="/intelligence"
            className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}
            onClick={onCloseSidebar}
          >
            <Brain />
            Intelligence
          </NavLink>
          <NavLink
            to="/analytics"
            className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}
            onClick={onCloseSidebar}
          >
            <BarChart3 />
            Analytics
          </NavLink>
          <NavLink
            to="/settings"
            className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}
            onClick={onCloseSidebar}
          >
            <Settings />
            Impostazioni
          </NavLink>
        </nav>

        {/* Status section */}
        <div className="sidebar-status">
          <div className="status-mode">Modalità</div>
          <div className="status-mode-value">{modeLabel}</div>

          {countdown && (
            <div className="status-countdown">
              Prossima esecuzione:{' '}
              <strong>{countdown}</strong>
            </div>
          )}

          {marketCountdown && !marketOpen && (
            <div className="status-countdown" style={{ marginTop: '0.25rem' }}>
              Apertura mercati:{' '}
              <strong>{marketCountdown}</strong>
            </div>
          )}

          {marketOpen && (
            <div className="status-countdown" style={{ marginTop: '0.25rem' }}>
              Mercati:{' '}
              <strong style={{ color: 'var(--positive)' }}>Aperti</strong>
            </div>
          )}
        </div>

        {/* Activity feed */}
        <div className="sidebar-activity">
          <div className="activity-title">Attività Recente</div>
          {recentLogs.length === 0 ? (
            <div className="activity-item" style={{ color: 'var(--text-muted)' }}>
              <Activity className="activity-icon monitor" />
              <span>Nessuna attività</span>
            </div>
          ) : (
            recentLogs.map((log, idx) => (
              <div className="activity-item" key={idx}>
                {getActivityIcon(log)}
                <span>{getLogText(log)}</span>
              </div>
            ))
          )}
        </div>

        {/* Controls */}
        <div className="sidebar-controls">
          {schedulerRunning ? (
            <button
              className="btn btn-danger"
              onClick={onStopMonitoring}
              disabled={false}
            >
              <Square />
              Ferma Monitoraggio
            </button>
          ) : (
            <button
              className="btn btn-primary"
              onClick={onStartMonitoring}
              disabled={false}
            >
              <Play />
              Avvia Monitoraggio
            </button>
          )}
          <button
            className="btn btn-secondary"
            onClick={onRunOnce}
            disabled={isRunning}
          >
            <Zap />
            Esegui Manuale
          </button>
        </div>

        {/* Portfolio summary */}
        <div className="sidebar-portfolio">
          <div className="portfolio-label">Portafoglio</div>
          <div className="portfolio-value">{formatEUR(totalValue)}</div>
          <div
            className="portfolio-change"
            style={{ color: pnlPositive ? 'var(--positive)' : 'var(--negative)' }}
          >
            {pnlPositive ? '+' : ''}
            {pnlPct.toFixed(2)}%
          </div>
          <div
            className="status-countdown"
            style={{ marginTop: '0.35rem' }}
          >
            Liquidità: <strong>{formatEUR(cashBalance)}</strong>
          </div>
        </div>

        {/* ClawStreet */}
        <div className="sidebar-portfolio" style={{ borderTop: '1px solid var(--border)', paddingTop: '0.75rem', marginTop: '0.5rem' }}>
          <div className="portfolio-label" style={{ display: 'flex', alignItems: 'center', gap: '0.35rem' }}>
            <Trophy size={14} /> ClawStreet
          </div>
          {csStatus?.registered ? (
            <>
              <div className="status-countdown" style={{ marginTop: '0.25rem' }}>
                Bot: <strong>{csStatus.bot_name || 'GeoInvest AI'}</strong>
              </div>
              {csStatus.public_url && (
                <a
                  href={csStatus.public_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="status-countdown"
                  style={{ display: 'flex', alignItems: 'center', gap: '0.25rem', marginTop: '0.25rem', color: 'var(--primary)', textDecoration: 'none', fontSize: '0.75rem' }}
                >
                  <ExternalLink size={12} /> Pagina pubblica
                </a>
              )}
            </>
          ) : (
            <button
              className="btn btn-secondary"
              style={{ marginTop: '0.35rem', fontSize: '0.75rem', padding: '0.3rem 0.6rem' }}
              onClick={handleRegisterCS}
              disabled={csRegistering}
            >
              {csRegistering ? 'Registrando...' : 'Registra bot pubblico'}
            </button>
          )}
        </div>
      </aside>
    </>
  );
}
