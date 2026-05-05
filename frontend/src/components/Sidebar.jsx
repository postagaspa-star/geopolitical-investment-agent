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
  RefreshCw,
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
  technical_crypto: { name: 'Technical Crypto', tone: '#0891b2' },
  decision_crypto: { name: 'Decision Crypto', tone: '#f472b6' },
};

// Portafoglio denominato in USD (yfinance/Massive ritornano sempre USD;
// non c'è conversione FX nel codebase). Il simbolo $ riflette la realtà dei dati.
const formatUSD = (value) =>
  new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(value ?? 0);

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
  const lastAttempt = info?.last_attempt;
  const active = !!info?.active;

  // Warning: c'è un tentativo PIÙ RECENTE dell'ultimo successo → fallimenti
  // recenti. Esempio: last_run = 11h fa ma last_attempt = 2m fa → l'agent
  // sta crashando da 11h.
  let attemptWarn = false;
  if (lastAttempt && lastRun) {
    attemptWarn = new Date(lastAttempt) > new Date(lastRun);
  } else if (lastAttempt && !lastRun) {
    attemptWarn = true;
  }

  // Mostra il countdown se c'e' un next_run; altrimenti "ultimo: 5m fa"; altrimenti "in attesa"
  let statusText;
  let statusColor;
  if (nextRun) {
    statusText = `tra ${formatCountdown(nextRun)}`;
    statusColor = '#94a3b8';
  } else if (lastRun) {
    statusText = `ok: ${formatTimeAgo(lastRun)}`;
    statusColor = attemptWarn ? '#ef4444' : '#64748b';
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
        minWidth: '90px',
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
      {attemptWarn && (
        <span title={`Tentativo fallito: ${formatTimeAgo(lastAttempt)} fa`}
              style={{
                fontSize: '0.65rem',
                color: '#ef4444',
                fontWeight: 700,
                marginLeft: 'auto',
                whiteSpace: 'nowrap',
              }}>
          ⚠ FAIL {formatTimeAgo(lastAttempt)}
        </span>
      )}
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
  onDataRefresh,
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

  // Manual price polling
  const [polling, setPolling] = useState(false);
  const [pollFeedback, setPollFeedback] = useState(null);
  const handleRefreshPrices = async () => {
    if (polling) return;
    setPolling(true);
    setPollFeedback(null);
    try {
      const res = await fetch(`${API}/api/prices/trigger-poll`, { method: 'POST' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const ok = (data.quotes_written ?? 0) > 0;
      setPollFeedback({
        ok,
        text: ok
          ? `${data.quotes_written}/${data.tickers} aggiornati (${data.duration_seconds}s)`
          : `Nessun dato (${data.source ?? 'none'})`,
      });
      // Trigger refresh nel parent così l'UI mostra subito i nuovi prezzi
      // (altrimenti aspetterebbe il prossimo tick periodico di App.jsx, 15s)
      if (ok && typeof onDataRefresh === 'function') {
        // Piccolo delay per dare al DB il tempo di committare l'upsert
        setTimeout(() => onDataRefresh(), 400);
      }
    } catch (err) {
      setPollFeedback({ ok: false, text: err.message });
    }
    setPolling(false);
    setTimeout(() => setPollFeedback(null), 5000);
  };

  // Audit + force-fix dei current_price posizioni (chiama yfinance fresh)
  const [auditing, setAuditing] = useState(false);
  const [auditFeedback, setAuditFeedback] = useState(null);
  const handleAuditPrices = async () => {
    if (auditing) return;
    setAuditing(true);
    setAuditFeedback(null);
    try {
      const res = await fetch(`${API}/api/admin/audit-position-prices`, { method: 'POST' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const fixed = data.fixed ?? 0;
      const audited = data.positions_audited ?? 0;
      setAuditFeedback({
        ok: true,
        text: fixed > 0
          ? `Fix ${fixed}/${audited} posizioni (delta >5%)`
          : `OK — ${audited} posizioni allineate`,
      });
      // Refresh dei dati anche qui: se l'audit ha sistemato dei prezzi,
      // l'utente deve vedere subito i nuovi current_price.
      if (typeof onDataRefresh === 'function') {
        setTimeout(() => onDataRefresh(), 400);
      }
    } catch (err) {
      setAuditFeedback({ ok: false, text: err.message });
    }
    setAuditing(false);
    setTimeout(() => setAuditFeedback(null), 8000);
  };

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

  const agentOrder = ['watchdog', 'scout', 'technical', 'decision',
                       'technical_crypto', 'decision_crypto'];

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
          <button className="btn btn-secondary"
            onClick={handleRefreshPrices} disabled={polling}
            style={{ opacity: polling ? 0.6 : 1 }}>
            <RefreshCw style={{
              animation: polling ? 'spin 1s linear infinite' : 'none',
            }} /> {polling ? 'Aggiornando…' : 'Aggiorna prezzi'}
          </button>
          {pollFeedback && (
            <div style={{
              fontSize: '0.7rem',
              padding: '0.3rem 0.5rem',
              borderRadius: '4px',
              background: pollFeedback.ok ? 'rgba(16,185,129,0.12)' : 'rgba(239,68,68,0.12)',
              color: pollFeedback.ok ? 'var(--positive)' : 'var(--negative)',
              border: `1px solid ${pollFeedback.ok ? 'rgba(16,185,129,0.3)' : 'rgba(239,68,68,0.3)'}`,
            }}>
              {pollFeedback.text}
            </div>
          )}
          <button className="btn btn-secondary"
            onClick={handleAuditPrices} disabled={auditing}
            style={{ opacity: auditing ? 0.6 : 1, fontSize: '0.78rem' }}
            title="Confronta i current_price salvati con yfinance live e applica fix se delta > 5%">
            <RefreshCw style={{
              animation: auditing ? 'spin 1s linear infinite' : 'none',
            }} /> {auditing ? 'Verifica…' : 'Verifica prezzi posizioni'}
          </button>
          {auditFeedback && (
            <div style={{
              fontSize: '0.7rem',
              padding: '0.3rem 0.5rem',
              borderRadius: '4px',
              background: auditFeedback.ok ? 'rgba(16,185,129,0.12)' : 'rgba(239,68,68,0.12)',
              color: auditFeedback.ok ? 'var(--positive)' : 'var(--negative)',
              border: `1px solid ${auditFeedback.ok ? 'rgba(16,185,129,0.3)' : 'rgba(239,68,68,0.3)'}`,
            }}>
              {auditFeedback.text}
            </div>
          )}
          <style>{`@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }`}</style>
        </div>

        {/* Portfolio summary */}
        <div className="sidebar-portfolio">
          <div className="portfolio-label">Portafoglio</div>
          <div className="portfolio-value">{formatUSD(totalValue)}</div>
          <div className="portfolio-change"
            style={{ color: pnlPositive ? 'var(--positive)' : 'var(--negative)' }}>
            {pnlPositive ? '+' : ''}{pnlPct.toFixed(2)}%
          </div>
          <div className="status-countdown" style={{ marginTop: '0.35rem' }}>
            Liquidità: <strong>{formatUSD(cashBalance)}</strong>
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
