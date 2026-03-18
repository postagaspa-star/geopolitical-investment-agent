import { useEffect, useState } from 'react';
import {
  ResponsiveContainer,
  PieChart, Pie, Cell, Tooltip, Legend,
  BarChart, Bar, XAxis, YAxis, CartesianGrid,
  ScatterChart, Scatter,
} from 'recharts';

const API = window.location.origin;

// --- Sector mapping ---
const SECTOR_MAP = {
  XOM: 'Energy', CVX: 'Energy', SHEL: 'Energy', TTE: 'Energy', ENI: 'Energy',
  LMT: 'Defense', RTX: 'Defense', NOC: 'Defense', BA: 'Defense', LDOS: 'Defense',
  GLD: 'Commodities', SLV: 'Commodities', USO: 'Commodities', UNG: 'Commodities',
  SPY: 'ETF', QQQ: 'ETF', EEM: 'ETF', VEA: 'ETF',
  EWG: 'Europe', EWI: 'Europe', EWQ: 'Europe', EWP: 'Europe',
};

function getSector(ticker) {
  return SECTOR_MAP[ticker?.toUpperCase()] ?? 'Altro';
}

const PIE_COLORS = ['#3b82f6', '#10b981', '#f59e0b', '#a78bfa', '#06b6d4', '#ef4444'];

const fmtEur = (n) =>
  new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR' }).format(n ?? 0);

const fmtPct = (n, decimals = 1) =>
  `${Number(n ?? 0).toFixed(decimals)}%`;

// --- Custom tooltip for dark theme ---
const DarkTooltip = ({ active, payload, label, formatter }) => {
  if (!active || !payload?.length) return null;
  return (
    <div style={{
      background: '#111827',
      border: '1px solid #1f2937',
      borderRadius: 8,
      padding: '0.5rem 0.85rem',
      fontSize: '0.78rem',
      color: '#f9fafb',
    }}>
      {label && <p style={{ color: '#9ca3af', marginBottom: '0.3rem' }}>{label}</p>}
      {payload.map((entry, i) => (
        <p key={i} style={{ color: entry.color ?? '#f9fafb', margin: '0.1rem 0' }}>
          {entry.name}: {formatter ? formatter(entry.value, entry.name) : entry.value}
        </p>
      ))}
    </div>
  );
};

// --- Section 1: Portfolio distribution by sector (Pie) ---
function SectorPieChart({ positions }) {
  if (!positions.length) {
    return <div className="empty-state">Nessuna posizione</div>;
  }

  const grouped = {};
  for (const pos of positions) {
    const sector = getSector(pos.ticker);
    const value = Math.abs(Number(pos.current_price ?? 0) * Number(pos.quantity ?? 0));
    grouped[sector] = (grouped[sector] ?? 0) + value;
  }

  const data = Object.entries(grouped).map(([name, value]) => ({ name, value }));

  return (
    <ResponsiveContainer width="100%" height={260}>
      <PieChart>
        <Pie
          data={data}
          cx="50%"
          cy="50%"
          innerRadius={55}
          outerRadius={90}
          paddingAngle={3}
          dataKey="value"
        >
          {data.map((entry, i) => (
            <Cell key={entry.name} fill={PIE_COLORS[i % PIE_COLORS.length]} />
          ))}
        </Pie>
        <Tooltip
          content={({ active, payload }) => (
            <DarkTooltip
              active={active}
              payload={payload}
              formatter={(v) => fmtEur(v)}
            />
          )}
        />
        <Legend
          iconType="circle"
          iconSize={8}
          wrapperStyle={{ fontSize: '0.75rem', color: '#9ca3af' }}
        />
      </PieChart>
    </ResponsiveContainer>
  );
}

// --- Section 2: Trades per asset (Bar) ---
function TradesByAssetChart({ trades }) {
  if (!trades.length) {
    return <div className="empty-state">Nessun trade</div>;
  }

  const grouped = {};
  for (const trade of trades) {
    const ticker = trade.ticker?.toUpperCase() ?? 'N/A';
    if (!grouped[ticker]) grouped[ticker] = { ticker, Buy: 0, Sell: 0 };
    const side = (trade.side ?? trade.action ?? '').toUpperCase();
    if (side === 'BUY') grouped[ticker].Buy += 1;
    else if (side === 'SELL') grouped[ticker].Sell += 1;
  }

  const data = Object.values(grouped).sort((a, b) => (b.Buy + b.Sell) - (a.Buy + a.Sell));

  return (
    <ResponsiveContainer width="100%" height={260}>
      <BarChart data={data} margin={{ top: 4, right: 8, left: -16, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
        <XAxis dataKey="ticker" tick={{ fill: '#6b7280', fontSize: 11 }} />
        <YAxis tick={{ fill: '#6b7280', fontSize: 11 }} allowDecimals={false} />
        <Tooltip
          content={({ active, payload, label }) => (
            <DarkTooltip active={active} payload={payload} label={label} />
          )}
        />
        <Legend wrapperStyle={{ fontSize: '0.75rem', color: '#9ca3af' }} />
        <Bar dataKey="Buy" name="Buy" fill="#10b981" radius={[3, 3, 0, 0]} />
        <Bar dataKey="Sell" name="Sell" fill="#ef4444" radius={[3, 3, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}

// --- Section 3: Performance per sector (Table) ---
function SectorPerformanceTable({ trades }) {
  if (!trades.length) {
    return <div className="empty-state">Nessun trade</div>;
  }

  // Match BUY/SELL pairs by ticker to compute real returns
  const buyMap = {};
  const sectorResults = {};

  for (const trade of trades) {
    const ticker = trade.ticker?.toUpperCase() ?? '';
    const side = (trade.action ?? '').toUpperCase();
    const sector = getSector(ticker);

    if (!sectorResults[sector]) sectorResults[sector] = { total: 0, wins: 0, returns: [] };
    sectorResults[sector].total += 1;

    if (side === 'BUY') {
      if (!buyMap[ticker]) buyMap[ticker] = [];
      buyMap[ticker].push(Number(trade.price ?? 0));
    } else if (side === 'SELL' && buyMap[ticker]?.length) {
      const buyPrice = buyMap[ticker].shift();
      const sellPrice = Number(trade.price ?? 0);
      if (buyPrice > 0) {
        const ret = ((sellPrice - buyPrice) / buyPrice) * 100;
        sectorResults[sector].returns.push(ret);
        if (ret > 0) sectorResults[sector].wins += 1;
      }
    }
  }

  const rows = Object.entries(sectorResults).map(([sector, stats]) => {
    const winRate = stats.returns.length > 0 ? (stats.wins / stats.returns.length) * 100 : 0;
    const avgReturn = stats.returns.length
      ? stats.returns.reduce((a, b) => a + b, 0) / stats.returns.length
      : 0;
    return { sector, total: stats.total, winRate, avgReturn };
  }).sort((a, b) => b.total - a.total);

  return (
    <div style={{ overflowX: 'auto' }}>
      <table className="data-table">
        <thead>
          <tr>
            <th>Settore</th>
            <th>N. Trade</th>
            <th>Win Rate %</th>
            <th>Rendimento Medio</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.sector}>
              <td style={{ color: '#f9fafb' }}>{row.sector}</td>
              <td>{row.total}</td>
              <td>
                <span className={row.winRate >= 50 ? 'positive' : 'negative'}>
                  {fmtPct(row.winRate)}
                </span>
              </td>
              <td>
                <span className={row.avgReturn >= 0 ? 'positive' : 'negative'}>
                  {fmtPct(row.avgReturn)}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// --- Section 4: Confidence vs Return (Scatter) ---
function ConfidenceScatterChart({ trades }) {
  // Match buys with sells by ticker to compute return
  const buyMap = {};
  const scatterData = [];

  for (const trade of trades) {
    const side = (trade.side ?? trade.action ?? '').toUpperCase();
    const ticker = trade.ticker?.toUpperCase() ?? '';
    if (side === 'BUY') {
      if (!buyMap[ticker]) buyMap[ticker] = [];
      buyMap[ticker].push(trade);
    }
  }

  for (const trade of trades) {
    const side = (trade.side ?? trade.action ?? '').toUpperCase();
    const ticker = trade.ticker?.toUpperCase() ?? '';

    // If trade already has pnl/return and a confidence score, use it directly
    const confidence = Number(trade.confidence ?? trade.confidence_score ?? null);
    const directReturn = Number(trade.return_pct ?? trade.pnl_pct ?? null);

    if (!isNaN(confidence) && !isNaN(directReturn) && trade.return_pct != null) {
      scatterData.push({
        x: confidence,
        y: directReturn,
        ticker,
        positive: directReturn >= 0,
      });
      continue;
    }

    // Match SELL with BUY
    if (side === 'SELL' && !isNaN(confidence) && buyMap[ticker]?.length) {
      const buy = buyMap[ticker].shift();
      const buyPrice = Number(buy.price ?? buy.entry_price ?? 0);
      const sellPrice = Number(trade.price ?? trade.exit_price ?? 0);
      if (buyPrice > 0) {
        const ret = ((sellPrice - buyPrice) / buyPrice) * 100;
        scatterData.push({
          x: confidence,
          y: parseFloat(ret.toFixed(2)),
          ticker,
          positive: ret >= 0,
        });
      }
    }
  }

  if (!scatterData.length) {
    return <div className="empty-state">Dati insufficienti</div>;
  }

  const positiveData = scatterData.filter((d) => d.positive);
  const negativeData = scatterData.filter((d) => !d.positive);

  return (
    <ResponsiveContainer width="100%" height={260}>
      <ScatterChart margin={{ top: 8, right: 8, left: -16, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
        <XAxis
          type="number"
          dataKey="x"
          name="Confidence"
          domain={[0, 100]}
          tick={{ fill: '#6b7280', fontSize: 11 }}
          label={{ value: 'Confidence', position: 'insideBottom', offset: -2, fill: '#6b7280', fontSize: 11 }}
        />
        <YAxis
          type="number"
          dataKey="y"
          name="Rendimento %"
          tick={{ fill: '#6b7280', fontSize: 11 }}
        />
        <Tooltip
          cursor={{ strokeDasharray: '3 3', stroke: '#374151' }}
          content={({ active, payload }) => {
            if (!active || !payload?.length) return null;
            const d = payload[0]?.payload;
            return (
              <div style={{
                background: '#111827', border: '1px solid #1f2937',
                borderRadius: 8, padding: '0.5rem 0.85rem', fontSize: '0.78rem', color: '#f9fafb',
              }}>
                <p style={{ color: '#9ca3af', marginBottom: '0.25rem' }}>{d?.ticker}</p>
                <p>Confidence: <strong>{d?.x}</strong></p>
                <p>Rendimento: <strong className={d?.positive ? 'positive' : 'negative'}>{fmtPct(d?.y)}</strong></p>
              </div>
            );
          }}
        />
        {positiveData.length > 0 && (
          <Scatter name="Positivo" data={positiveData} fill="#10b981" opacity={0.8} />
        )}
        {negativeData.length > 0 && (
          <Scatter name="Negativo" data={negativeData} fill="#ef4444" opacity={0.8} />
        )}
        <Legend wrapperStyle={{ fontSize: '0.75rem', color: '#9ca3af' }} />
      </ScatterChart>
    </ResponsiveContainer>
  );
}

// --- Main component ---
export default function AnalyticsPage() {
  const [positions, setPositions] = useState([]);
  const [trades, setTrades] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    const fetchData = async () => {
      setLoading(true);
      setError(null);
      try {
        const [posRes, tradeRes] = await Promise.all([
          fetch(`${API}/api/positions`),
          fetch(`${API}/api/trades`),
        ]);

        const [posData, tradeData] = await Promise.all([
          posRes.ok ? posRes.json() : Promise.resolve([]),
          tradeRes.ok ? tradeRes.json() : Promise.resolve([]),
        ]);

        setPositions(Array.isArray(posData) ? posData : posData?.positions ?? []);
        setTrades(Array.isArray(tradeData) ? tradeData : tradeData?.trades ?? []);
      } catch (err) {
        setError(err.message ?? 'Errore nel caricamento dei dati');
      } finally {
        setLoading(false);
      }
    };

    fetchData();
  }, []);

  if (loading) {
    return (
      <div className="main-content">
        <div className="loading-state">Caricamento analytics...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="main-content">
        <div className="error-state">Errore: {error}</div>
      </div>
    );
  }

  return (
    <div className="main-content">
      <div style={{ marginBottom: '1.5rem' }}>
        <h2 style={{ fontSize: '1.2rem', fontWeight: 700, color: 'var(--text-primary)', marginBottom: '0.25rem' }}>
          Analytics
        </h2>
        <p style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>
          {positions.length} posizioni &middot; {trades.length} trade
        </p>
      </div>

      <div className="analytics-grid">
        {/* --- Card 1: Distribuzione Portafoglio per Settore --- */}
        <div className="card">
          <div className="card-header">
            <span className="card-title">Distribuzione Portafoglio per Settore</span>
          </div>
          <SectorPieChart positions={positions} />
        </div>

        {/* --- Card 2: Trade per Asset --- */}
        <div className="card">
          <div className="card-header">
            <span className="card-title">Trade per Asset</span>
          </div>
          <TradesByAssetChart trades={trades} />
        </div>

        {/* --- Card 3: Performance per Settore --- */}
        <div className="card">
          <div className="card-header">
            <span className="card-title">Performance per Settore</span>
          </div>
          <SectorPerformanceTable trades={trades} />
        </div>

        {/* --- Card 4: Confidence vs Rendimento --- */}
        <div className="card">
          <div className="card-header">
            <span className="card-title">Confidence vs Rendimento</span>
          </div>
          <ConfidenceScatterChart trades={trades} />
        </div>
      </div>
    </div>
  );
}
