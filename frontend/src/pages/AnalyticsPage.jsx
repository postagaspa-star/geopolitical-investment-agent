import { useEffect, useState, useMemo } from 'react';
import {
  ResponsiveContainer,
  PieChart, Pie, Cell, Tooltip, Legend,
  BarChart, Bar, XAxis, YAxis, CartesianGrid,
  AreaChart, Area, LineChart, Line,
  ComposedChart,
} from 'recharts';

const API = window.location.origin;

// ============================================================
// Helpers
// ============================================================

const fmtEur = (n) =>
  new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR' }).format(n ?? 0);

const fmtPct = (n, decimals = 2) =>
  `${(Number(n ?? 0) >= 0 ? '+' : '')}${Number(n ?? 0).toFixed(decimals)}%`;

const fmtNum = (n) =>
  new Intl.NumberFormat('it-IT').format(n ?? 0);

const PALETTE = ['#3b82f6', '#10b981', '#f59e0b', '#a78bfa', '#06b6d4', '#ef4444', '#ec4899', '#84cc16'];

// Tooltip dark theme riutilizzato
const DarkTooltip = ({ active, payload, label, formatter, labelFormatter }) => {
  if (!active || !payload?.length) return null;
  return (
    <div style={{
      background: '#0f172a',
      border: '1px solid #1e293b',
      borderRadius: 8,
      padding: '0.6rem 0.9rem',
      fontSize: '0.78rem',
      color: '#f1f5f9',
      boxShadow: '0 4px 6px -1px rgba(0,0,0,0.3)',
    }}>
      {label != null && (
        <div style={{ color: '#94a3b8', marginBottom: '0.3rem', fontWeight: 500 }}>
          {labelFormatter ? labelFormatter(label) : label}
        </div>
      )}
      {payload.map((entry, i) => (
        <div key={i} style={{ color: entry.color ?? '#f1f5f9', margin: '0.15rem 0' }}>
          {entry.name}: <strong>{formatter ? formatter(entry.value, entry.name) : entry.value}</strong>
        </div>
      ))}
    </div>
  );
};

// ============================================================
// KPI Cards
// ============================================================

function KpiCard({ label, value, subValue, color, hint }) {
  return (
    <div style={{
      background: '#111827',
      border: '1px solid #1f2937',
      borderRadius: 10,
      padding: '1rem 1.1rem',
      display: 'flex',
      flexDirection: 'column',
      gap: '0.3rem',
      minHeight: '92px',
    }}>
      <div style={{ fontSize: '0.72rem', color: '#94a3b8', fontWeight: 500, textTransform: 'uppercase', letterSpacing: '0.04em' }}>
        {label}
      </div>
      <div style={{ fontSize: '1.4rem', fontWeight: 700, color: color ?? '#f1f5f9', lineHeight: 1.1 }}>
        {value}
      </div>
      {subValue && (
        <div style={{ fontSize: '0.78rem', color: '#94a3b8' }}>
          {subValue}
        </div>
      )}
      {hint && (
        <div style={{ fontSize: '0.7rem', color: '#64748b' }}>
          {hint}
        </div>
      )}
    </div>
  );
}

// ============================================================
// Computation utilities
// ============================================================

// Calcola P&L realizzato per coppie BUY-SELL (FIFO).
// Restituisce array di { ticker, buyPrice, sellPrice, qty, pnl, pnlPct, sellTimestamp, confidence, holdHours }
function computeClosedTrades(trades) {
  const buyQueue = {}; // ticker -> [{ price, qty, ts, confidence }]
  const closed = [];

  // Ordino i trade per timestamp crescente
  const sorted = [...(trades ?? [])].sort((a, b) => {
    const ta = new Date(a.timestamp ?? 0).getTime();
    const tb = new Date(b.timestamp ?? 0).getTime();
    return ta - tb;
  });

  for (const t of sorted) {
    const ticker = (t.ticker ?? '').toUpperCase();
    const action = (t.action ?? t.side ?? '').toUpperCase();
    const price = Number(t.price ?? 0);
    const qty = Number(t.quantity ?? 0);
    const ts = t.timestamp;
    const confidence = Number(t.confidence_score ?? t.confidence ?? NaN);

    if (!ticker || !price || !qty) continue;

    if (action === 'BUY') {
      if (!buyQueue[ticker]) buyQueue[ticker] = [];
      buyQueue[ticker].push({ price, qty, ts, confidence });
    } else if (action === 'SELL') {
      let remaining = qty;
      while (remaining > 0 && buyQueue[ticker]?.length) {
        const buy = buyQueue[ticker][0];
        const matched = Math.min(remaining, buy.qty);
        const pnl = (price - buy.price) * matched;
        const pnlPct = ((price - buy.price) / buy.price) * 100;
        const holdHours = ts && buy.ts
          ? (new Date(ts).getTime() - new Date(buy.ts).getTime()) / 3600000
          : null;
        closed.push({
          ticker,
          buyPrice: buy.price,
          sellPrice: price,
          qty: matched,
          pnl,
          pnlPct,
          buyTimestamp: buy.ts,
          sellTimestamp: ts,
          confidence: buy.confidence,
          holdHours,
        });
        buy.qty -= matched;
        remaining -= matched;
        if (buy.qty <= 0) buyQueue[ticker].shift();
      }
    }
  }

  return closed;
}

function computeKpis(positions, trades, closedTrades) {
  const totalPositions = positions?.length ?? 0;
  const realizedPnl = closedTrades.reduce((s, c) => s + c.pnl, 0);
  const winners = closedTrades.filter((c) => c.pnl > 0);
  const losers = closedTrades.filter((c) => c.pnl < 0);
  const winRate = closedTrades.length ? (winners.length / closedTrades.length) * 100 : 0;
  const avgWin = winners.length ? winners.reduce((s, c) => s + c.pnlPct, 0) / winners.length : 0;
  const avgLoss = losers.length ? losers.reduce((s, c) => s + c.pnlPct, 0) / losers.length : 0;
  const bestTrade = closedTrades.reduce((b, c) => (b == null || c.pnlPct > b.pnlPct ? c : b), null);
  const worstTrade = closedTrades.reduce((w, c) => (w == null || c.pnlPct < w.pnlPct ? c : w), null);
  const unrealizedPnl = (positions ?? []).reduce((s, p) => s + Number(p.unrealized_pnl ?? 0), 0);
  const totalTrades = trades?.length ?? 0;
  const avgHoldHours = closedTrades.length
    ? closedTrades.filter((c) => c.holdHours != null).reduce((s, c) => s + c.holdHours, 0) / closedTrades.length
    : 0;

  // Profit factor = sum(wins) / sum(|losses|)
  const sumWins = winners.reduce((s, c) => s + c.pnl, 0);
  const sumLosses = Math.abs(losers.reduce((s, c) => s + c.pnl, 0));
  const profitFactor = sumLosses > 0 ? sumWins / sumLosses : (sumWins > 0 ? Infinity : 0);

  return {
    totalPositions, totalTrades, realizedPnl, unrealizedPnl,
    winRate, avgWin, avgLoss, bestTrade, worstTrade, avgHoldHours, profitFactor,
    winnersCount: winners.length, losersCount: losers.length,
  };
}

// ============================================================
// Section components
// ============================================================

// Equity curve con drawdown
function EquityCurveCard({ history }) {
  if (!history?.length) {
    return <div className="empty-state">Equity history non disponibile</div>;
  }

  // Calcola drawdown rispetto al massimo precedente
  let runningMax = -Infinity;
  const data = history.map((p) => {
    const v = Number(p.total_value ?? 0);
    runningMax = Math.max(runningMax, v);
    const drawdown = runningMax > 0 ? ((v - runningMax) / runningMax) * 100 : 0;
    return {
      timestamp: p.timestamp,
      value: v,
      drawdown,
    };
  });

  const maxDrawdown = data.reduce((min, p) => Math.min(min, p.drawdown), 0);

  return (
    <div>
      <div style={{ marginBottom: '0.5rem', fontSize: '0.78rem', color: '#94a3b8' }}>
        Max Drawdown: <strong style={{ color: maxDrawdown < -5 ? '#ef4444' : '#10b981' }}>
          {fmtPct(maxDrawdown, 2)}
        </strong>
      </div>
      <ResponsiveContainer width="100%" height={240}>
        <ComposedChart data={data} margin={{ top: 5, right: 8, left: -8, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
          <XAxis
            dataKey="timestamp"
            tick={{ fill: '#64748b', fontSize: 10 }}
            tickFormatter={(t) => t ? new Date(t).toLocaleDateString('it-IT', { month: 'short', day: 'numeric' }) : ''}
            minTickGap={30}
          />
          <YAxis
            yAxisId="left"
            tick={{ fill: '#64748b', fontSize: 10 }}
            tickFormatter={(v) => `€${(v / 1000).toFixed(0)}k`}
          />
          <YAxis
            yAxisId="right"
            orientation="right"
            tick={{ fill: '#64748b', fontSize: 10 }}
            tickFormatter={(v) => `${v.toFixed(0)}%`}
            domain={['dataMin', 0]}
          />
          <Tooltip content={({ active, payload, label }) => (
            <DarkTooltip
              active={active}
              payload={payload}
              label={label}
              labelFormatter={(l) => l ? new Date(l).toLocaleString('it-IT') : ''}
              formatter={(v, name) => name === 'Drawdown' ? fmtPct(v) : fmtEur(v)}
            />
          )} />
          <Area
            yAxisId="left"
            type="monotone"
            dataKey="value"
            name="Equity"
            stroke="#3b82f6"
            fill="#3b82f6"
            fillOpacity={0.15}
            strokeWidth={2}
            isAnimationActive={false}
          />
          <Area
            yAxisId="right"
            type="monotone"
            dataKey="drawdown"
            name="Drawdown"
            stroke="#ef4444"
            fill="#ef4444"
            fillOpacity={0.2}
            strokeWidth={1}
            isAnimationActive={false}
          />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}

// Cumulative P&L sui trade chiusi
function CumulativePnlCard({ closedTrades }) {
  if (!closedTrades?.length) {
    return <div className="empty-state">Nessun trade chiuso</div>;
  }

  let cum = 0;
  const data = closedTrades.map((c, i) => {
    cum += c.pnl;
    return {
      idx: i + 1,
      cum,
      ticker: c.ticker,
      pnl: c.pnl,
    };
  });

  const finalCum = data[data.length - 1].cum;
  const lineColor = finalCum >= 0 ? '#10b981' : '#ef4444';

  return (
    <ResponsiveContainer width="100%" height={240}>
      <AreaChart data={data} margin={{ top: 5, right: 8, left: -8, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
        <XAxis
          dataKey="idx"
          tick={{ fill: '#64748b', fontSize: 10 }}
          label={{ value: 'Trade #', position: 'insideBottom', offset: -2, fill: '#64748b', fontSize: 10 }}
        />
        <YAxis
          tick={{ fill: '#64748b', fontSize: 10 }}
          tickFormatter={(v) => `€${v.toFixed(0)}`}
        />
        <Tooltip content={({ active, payload }) => {
          if (!active || !payload?.length) return null;
          const d = payload[0]?.payload;
          return (
            <div style={{ background: '#0f172a', border: '1px solid #1e293b', borderRadius: 8, padding: '0.5rem 0.85rem', fontSize: '0.78rem', color: '#f1f5f9' }}>
              <div style={{ color: '#94a3b8' }}>Trade #{d?.idx} · {d?.ticker}</div>
              <div>Cumulativo: <strong>{fmtEur(d?.cum)}</strong></div>
              <div>P&L: <strong style={{ color: d?.pnl >= 0 ? '#10b981' : '#ef4444' }}>{fmtEur(d?.pnl)}</strong></div>
            </div>
          );
        }} />
        <Area
          type="monotone"
          dataKey="cum"
          stroke={lineColor}
          fill={lineColor}
          fillOpacity={0.15}
          strokeWidth={2}
          isAnimationActive={false}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}

// Distribuzione P&L (istogramma)
function PnlDistributionCard({ closedTrades }) {
  if (!closedTrades?.length) {
    return <div className="empty-state">Nessun trade chiuso</div>;
  }

  // Crea bucket dinamici
  const returns = closedTrades.map((c) => c.pnlPct);
  const min = Math.min(...returns);
  const max = Math.max(...returns);
  const bucketCount = 10;
  const range = max - min || 1;
  const step = range / bucketCount;

  const buckets = Array.from({ length: bucketCount }, (_, i) => ({
    label: `${(min + i * step).toFixed(1)}%`,
    range: [min + i * step, min + (i + 1) * step],
    count: 0,
    isPositive: (min + (i + 0.5) * step) >= 0,
  }));

  for (const r of returns) {
    let idx = Math.min(bucketCount - 1, Math.max(0, Math.floor((r - min) / step)));
    buckets[idx].count += 1;
  }

  return (
    <ResponsiveContainer width="100%" height={240}>
      <BarChart data={buckets} margin={{ top: 5, right: 8, left: -16, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
        <XAxis dataKey="label" tick={{ fill: '#64748b', fontSize: 9 }} angle={-30} textAnchor="end" height={50} />
        <YAxis tick={{ fill: '#64748b', fontSize: 10 }} allowDecimals={false} />
        <Tooltip content={({ active, payload }) => {
          if (!active || !payload?.length) return null;
          const d = payload[0]?.payload;
          return (
            <div style={{ background: '#0f172a', border: '1px solid #1e293b', borderRadius: 8, padding: '0.5rem 0.85rem', fontSize: '0.78rem', color: '#f1f5f9' }}>
              <div style={{ color: '#94a3b8' }}>Range: {d.range[0].toFixed(1)}% – {d.range[1].toFixed(1)}%</div>
              <div>Trade: <strong>{d.count}</strong></div>
            </div>
          );
        }} />
        <Bar dataKey="count" radius={[3, 3, 0, 0]}>
          {buckets.map((b, i) => (
            <Cell key={i} fill={b.isPositive ? '#10b981' : '#ef4444'} fillOpacity={0.85} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

// Top performers / Worst performers
function PnlByTickerCard({ closedTrades }) {
  if (!closedTrades?.length) {
    return <div className="empty-state">Nessun trade chiuso</div>;
  }

  const byTicker = {};
  for (const c of closedTrades) {
    if (!byTicker[c.ticker]) byTicker[c.ticker] = { ticker: c.ticker, pnl: 0, count: 0 };
    byTicker[c.ticker].pnl += c.pnl;
    byTicker[c.ticker].count += 1;
  }

  const data = Object.values(byTicker)
    .sort((a, b) => b.pnl - a.pnl)
    .slice(0, 12);

  return (
    <ResponsiveContainer width="100%" height={Math.max(200, data.length * 28)}>
      <BarChart data={data} layout="vertical" margin={{ top: 5, right: 16, left: 8, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
        <XAxis
          type="number"
          tick={{ fill: '#64748b', fontSize: 10 }}
          tickFormatter={(v) => `€${v.toFixed(0)}`}
        />
        <YAxis
          type="category"
          dataKey="ticker"
          tick={{ fill: '#cbd5e1', fontSize: 11, fontWeight: 600 }}
          width={56}
        />
        <Tooltip content={({ active, payload }) => {
          if (!active || !payload?.length) return null;
          const d = payload[0]?.payload;
          return (
            <div style={{ background: '#0f172a', border: '1px solid #1e293b', borderRadius: 8, padding: '0.5rem 0.85rem', fontSize: '0.78rem', color: '#f1f5f9' }}>
              <div style={{ color: '#94a3b8' }}>{d.ticker}</div>
              <div>P&L: <strong style={{ color: d.pnl >= 0 ? '#10b981' : '#ef4444' }}>{fmtEur(d.pnl)}</strong></div>
              <div>Trade chiusi: <strong>{d.count}</strong></div>
            </div>
          );
        }} />
        <Bar dataKey="pnl" radius={[0, 3, 3, 0]}>
          {data.map((d, i) => (
            <Cell key={i} fill={d.pnl >= 0 ? '#10b981' : '#ef4444'} fillOpacity={0.85} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

// Esposizione attuale per ticker (concentrazione del portafoglio)
function ExposurePieCard({ positions }) {
  if (!positions?.length) {
    return <div className="empty-state">Nessuna posizione aperta</div>;
  }

  const data = positions
    .map((p) => ({
      name: p.ticker,
      value: Math.abs(Number(p.current_price ?? 0) * Number(p.quantity ?? 0)),
      pnl: Number(p.unrealized_pnl ?? 0),
    }))
    .sort((a, b) => b.value - a.value);

  return (
    <ResponsiveContainer width="100%" height={260}>
      <PieChart>
        <Pie
          data={data}
          cx="50%"
          cy="50%"
          innerRadius={55}
          outerRadius={95}
          paddingAngle={2}
          dataKey="value"
          label={({ name, percent }) => `${name} ${(percent * 100).toFixed(0)}%`}
          labelLine={false}
          fontSize={10}
        >
          {data.map((entry, i) => (
            <Cell key={entry.name} fill={PALETTE[i % PALETTE.length]} />
          ))}
        </Pie>
        <Tooltip content={({ active, payload }) => {
          if (!active || !payload?.length) return null;
          const d = payload[0]?.payload;
          return (
            <div style={{ background: '#0f172a', border: '1px solid #1e293b', borderRadius: 8, padding: '0.5rem 0.85rem', fontSize: '0.78rem', color: '#f1f5f9' }}>
              <div style={{ color: '#94a3b8' }}>{d.name}</div>
              <div>Esposizione: <strong>{fmtEur(d.value)}</strong></div>
              <div>P&L non realizzato: <strong style={{ color: d.pnl >= 0 ? '#10b981' : '#ef4444' }}>{fmtEur(d.pnl)}</strong></div>
            </div>
          );
        }} />
      </PieChart>
    </ResponsiveContainer>
  );
}

// Activity heatmap: trade per giorno della settimana
function TradesPerWeekdayCard({ trades }) {
  if (!trades?.length) {
    return <div className="empty-state">Nessun trade</div>;
  }

  const days = ['Lun', 'Mar', 'Mer', 'Gio', 'Ven', 'Sab', 'Dom'];
  const counts = days.map((d) => ({ day: d, Buy: 0, Sell: 0 }));

  for (const t of trades) {
    const ts = t.timestamp ? new Date(t.timestamp) : null;
    if (!ts || isNaN(ts)) continue;
    // getDay: 0=Sun, 1=Mon, ..., 6=Sat. Converto a 0=Mon...6=Sun
    const idx = (ts.getDay() + 6) % 7;
    const action = (t.action ?? '').toUpperCase();
    if (action === 'BUY') counts[idx].Buy += 1;
    else if (action === 'SELL') counts[idx].Sell += 1;
  }

  return (
    <ResponsiveContainer width="100%" height={240}>
      <BarChart data={counts} margin={{ top: 5, right: 8, left: -16, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
        <XAxis dataKey="day" tick={{ fill: '#64748b', fontSize: 11 }} />
        <YAxis tick={{ fill: '#64748b', fontSize: 10 }} allowDecimals={false} />
        <Tooltip content={({ active, payload, label }) => (
          <DarkTooltip active={active} payload={payload} label={label} />
        )} />
        <Legend wrapperStyle={{ fontSize: '0.75rem', color: '#94a3b8' }} iconType="circle" iconSize={8} />
        <Bar dataKey="Buy" stackId="a" fill="#10b981" radius={[0, 0, 0, 0]} />
        <Bar dataKey="Sell" stackId="a" fill="#ef4444" radius={[3, 3, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}

// Win rate per ticker (almeno 3 trade chiusi)
function WinRateByTickerCard({ closedTrades }) {
  if (!closedTrades?.length) {
    return <div className="empty-state">Nessun trade chiuso</div>;
  }

  const stats = {};
  for (const c of closedTrades) {
    if (!stats[c.ticker]) stats[c.ticker] = { ticker: c.ticker, total: 0, wins: 0 };
    stats[c.ticker].total += 1;
    if (c.pnl > 0) stats[c.ticker].wins += 1;
  }

  const data = Object.values(stats)
    .filter((s) => s.total >= 1)
    .map((s) => ({ ...s, winRate: (s.wins / s.total) * 100 }))
    .sort((a, b) => b.winRate - a.winRate)
    .slice(0, 10);

  if (!data.length) {
    return <div className="empty-state">Dati insufficienti</div>;
  }

  return (
    <ResponsiveContainer width="100%" height={Math.max(200, data.length * 28)}>
      <BarChart data={data} layout="vertical" margin={{ top: 5, right: 16, left: 8, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
        <XAxis
          type="number"
          domain={[0, 100]}
          tick={{ fill: '#64748b', fontSize: 10 }}
          tickFormatter={(v) => `${v.toFixed(0)}%`}
        />
        <YAxis
          type="category"
          dataKey="ticker"
          tick={{ fill: '#cbd5e1', fontSize: 11, fontWeight: 600 }}
          width={56}
        />
        <Tooltip content={({ active, payload }) => {
          if (!active || !payload?.length) return null;
          const d = payload[0]?.payload;
          return (
            <div style={{ background: '#0f172a', border: '1px solid #1e293b', borderRadius: 8, padding: '0.5rem 0.85rem', fontSize: '0.78rem', color: '#f1f5f9' }}>
              <div style={{ color: '#94a3b8' }}>{d.ticker}</div>
              <div>Win rate: <strong>{d.winRate.toFixed(1)}%</strong></div>
              <div>{d.wins}/{d.total} trade vincenti</div>
            </div>
          );
        }} />
        <Bar dataKey="winRate" radius={[0, 3, 3, 0]}>
          {data.map((d, i) => (
            <Cell key={i} fill={d.winRate >= 50 ? '#10b981' : '#ef4444'} fillOpacity={0.85} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

// ============================================================
// Main component
// ============================================================

export default function AnalyticsPage() {
  const [positions, setPositions] = useState([]);
  const [trades, setTrades] = useState([]);
  const [history, setHistory] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    const fetchData = async () => {
      setLoading(true);
      setError(null);
      try {
        const [posRes, tradeRes, histRes] = await Promise.all([
          fetch(`${API}/api/positions`),
          fetch(`${API}/api/trades`),
          fetch(`${API}/api/portfolio/history?period=30d`),
        ]);

        const [posData, tradeData, histData] = await Promise.all([
          posRes.ok ? posRes.json() : Promise.resolve([]),
          tradeRes.ok ? tradeRes.json() : Promise.resolve([]),
          histRes.ok ? histRes.json() : Promise.resolve([]),
        ]);

        setPositions(Array.isArray(posData) ? posData : posData?.positions ?? []);
        setTrades(Array.isArray(tradeData) ? tradeData : tradeData?.trades ?? []);
        setHistory(Array.isArray(histData) ? histData : []);
      } catch (err) {
        setError(err.message ?? 'Errore nel caricamento dei dati');
      } finally {
        setLoading(false);
      }
    };

    fetchData();
  }, []);

  const closedTrades = useMemo(() => computeClosedTrades(trades), [trades]);
  const kpis = useMemo(() => computeKpis(positions, trades, closedTrades), [positions, trades, closedTrades]);

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

  const realizedColor = kpis.realizedPnl >= 0 ? '#10b981' : '#ef4444';
  const unrealizedColor = kpis.unrealizedPnl >= 0 ? '#10b981' : '#ef4444';
  const winRateColor = kpis.winRate >= 50 ? '#10b981' : '#f59e0b';

  return (
    <div className="main-content">
      <div style={{ marginBottom: '1.5rem' }}>
        <h2 style={{ fontSize: '1.4rem', fontWeight: 700, color: '#f1f5f9', marginBottom: '0.25rem' }}>
          Analytics
        </h2>
        <p style={{ fontSize: '0.85rem', color: '#94a3b8' }}>
          {kpis.totalPositions} posizioni aperte · {kpis.totalTrades} trade totali · {closedTrades.length} chiusi
        </p>
      </div>

      {/* KPI Cards */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))',
        gap: '1rem',
        marginBottom: '1.5rem',
      }}>
        <KpiCard
          label="P&L Realizzato"
          value={fmtEur(kpis.realizedPnl)}
          subValue={`${kpis.winnersCount} win · ${kpis.losersCount} loss`}
          color={realizedColor}
        />
        <KpiCard
          label="P&L Non Realizzato"
          value={fmtEur(kpis.unrealizedPnl)}
          subValue={`${kpis.totalPositions} posizioni aperte`}
          color={unrealizedColor}
        />
        <KpiCard
          label="Win Rate"
          value={`${kpis.winRate.toFixed(1)}%`}
          subValue={`Profit factor: ${isFinite(kpis.profitFactor) ? kpis.profitFactor.toFixed(2) : '∞'}`}
          color={winRateColor}
        />
        <KpiCard
          label="Avg Win / Avg Loss"
          value={`${fmtPct(kpis.avgWin, 1)} / ${fmtPct(kpis.avgLoss, 1)}`}
          subValue={`Hold medio: ${kpis.avgHoldHours.toFixed(1)}h`}
        />
        <KpiCard
          label="Best Trade"
          value={kpis.bestTrade ? fmtPct(kpis.bestTrade.pnlPct, 2) : '—'}
          subValue={kpis.bestTrade ? `${kpis.bestTrade.ticker} · ${fmtEur(kpis.bestTrade.pnl)}` : ''}
          color="#10b981"
        />
        <KpiCard
          label="Worst Trade"
          value={kpis.worstTrade ? fmtPct(kpis.worstTrade.pnlPct, 2) : '—'}
          subValue={kpis.worstTrade ? `${kpis.worstTrade.ticker} · ${fmtEur(kpis.worstTrade.pnl)}` : ''}
          color="#ef4444"
        />
      </div>

      {/* Charts grid */}
      <div className="analytics-grid" style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(420px, 1fr))',
        gap: '1rem',
      }}>
        <div className="card">
          <div className="card-header">
            <span className="card-title">Equity Curve & Drawdown</span>
            <span style={{ fontSize: '0.7rem', color: '#64748b' }}>Ultimi 30 giorni</span>
          </div>
          <EquityCurveCard history={history} />
        </div>

        <div className="card">
          <div className="card-header">
            <span className="card-title">P&L Cumulativo</span>
            <span style={{ fontSize: '0.7rem', color: '#64748b' }}>Sui trade chiusi</span>
          </div>
          <CumulativePnlCard closedTrades={closedTrades} />
        </div>

        <div className="card">
          <div className="card-header">
            <span className="card-title">Distribuzione Rendimenti</span>
            <span style={{ fontSize: '0.7rem', color: '#64748b' }}>% per trade chiuso</span>
          </div>
          <PnlDistributionCard closedTrades={closedTrades} />
        </div>

        <div className="card">
          <div className="card-header">
            <span className="card-title">P&L per Ticker</span>
            <span style={{ fontSize: '0.7rem', color: '#64748b' }}>Top 12</span>
          </div>
          <PnlByTickerCard closedTrades={closedTrades} />
        </div>

        <div className="card">
          <div className="card-header">
            <span className="card-title">Esposizione Attuale</span>
            <span style={{ fontSize: '0.7rem', color: '#64748b' }}>Posizioni aperte</span>
          </div>
          <ExposurePieCard positions={positions} />
        </div>

        <div className="card">
          <div className="card-header">
            <span className="card-title">Win Rate per Ticker</span>
            <span style={{ fontSize: '0.7rem', color: '#64748b' }}>Top 10</span>
          </div>
          <WinRateByTickerCard closedTrades={closedTrades} />
        </div>

        <div className="card">
          <div className="card-header">
            <span className="card-title">Attività per Giorno</span>
            <span style={{ fontSize: '0.7rem', color: '#64748b' }}>Buy / Sell</span>
          </div>
          <TradesPerWeekdayCard trades={trades} />
        </div>
      </div>
    </div>
  );
}
