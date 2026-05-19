import { useEffect, useState, useMemo } from 'react';
import {
  ResponsiveContainer,
  PieChart, Pie, Cell, Tooltip, Legend,
  BarChart, Bar, XAxis, YAxis, CartesianGrid,
  AreaChart, Area, LineChart, Line,
  ComposedChart, ReferenceArea,
  ScatterChart, Scatter, ZAxis, ReferenceLine,
} from 'recharts';
import ResearchPanel from '../components/ResearchPanel';

const API = window.location.origin;

// ============================================================
// Helpers
// ============================================================

// Portafoglio in USD (yfinance/Massive ritornano sempre USD; nessuna conversione FX nel backend)
const fmtUsd = (n) =>
  new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(n ?? 0);

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

    // FIX: skip trade con price/qty non validi o non finiti.
    if (!ticker || !Number.isFinite(price) || price <= 0 ||
        !Number.isFinite(qty) || qty <= 0) continue;

    // NB: le chiusure non-AI (confidence 100: circuit breaker /
    // auto-exit / manuali) NON vengono escluse qui. Il P&L realizzato,
    // l'equity e il win-rate sono un FATTO finanziario: ogni chiusura
    // muove denaro reale e DEVE contare. L'esclusione "solo AI" e' una
    // vista SKILL e si applica SOLO dove serve (la calibrazione della
    // confidence, l'Edge Tracker), non al rendimento del portafoglio.

    if (action === 'BUY') {
      if (!buyQueue[ticker]) buyQueue[ticker] = [];
      buyQueue[ticker].push({ price, qty, ts, confidence });
    } else if (action === 'SELL') {
      let remaining = qty;
      while (remaining > 0 && buyQueue[ticker]?.length) {
        const buy = buyQueue[ticker][0];
        // Guard contro buy.price=0 (avrebbe dato NaN su pnlPct)
        if (!buy.price || buy.price <= 0) {
          buyQueue[ticker].shift();
          continue;
        }
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

// Downsampling: aggrega N record in 1 (riduce rumore visivo dell'equity curve
// quando ci sono molti snapshot al minuto).
function downsampleHistory(history, targetPoints = 100) {
  if (!history?.length || history.length <= targetPoints) return history;
  const step = Math.ceil(history.length / targetPoints);
  const out = [];
  for (let i = 0; i < history.length; i += step) {
    // Prendi l'ultimo punto del bucket (il più recente in ordine ASC)
    out.push(history[Math.min(i + step - 1, history.length - 1)]);
  }
  return out;
}

// Filtra outlier: snapshot con total_value che si discosta troppo dal
// rolling median (MAD-based). Sostituisce il valore corrotto con la mediana
// locale così il grafico non mostra picchi anomali (+47% di colpo, ecc).
//
// Strategia: per ogni punto, calcola median + MAD su una finestra di
// ±WINDOW snapshot. Se |valore - median| > MAX_MAD * MAD → outlier.
// Robusto sia su spike transienti (1 punto) sia su run consecutivi corti
// di dati corrotti (es. 2-3 punti dovuti a un fetch sbagliato di Polygon).
function sanitizeOutliers(history) {
  if (!history || history.length < 5) return history || [];
  const arr = history.map((p) => ({ ...p }));
  const N = arr.length;
  const WINDOW = 7;       // 3 prima + corrente + 3 dopo
  const MAX_MAD = 6;      // punto > 6×MAD dalla median = outlier (soglia conservativa)
  const MIN_PCT_DEV = 0.10;  // serve almeno 10% di scostamento per chiamarlo outlier (evita falsi positivi quando MAD≈0)

  // Mediana di un array
  const median = (xs) => {
    if (!xs.length) return 0;
    const s = [...xs].sort((a, b) => a - b);
    const m = Math.floor(s.length / 2);
    return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
  };

  let fixed = 0;
  for (let i = 0; i < N; i++) {
    const cur = Number(arr[i].total_value);
    if (!Number.isFinite(cur) || cur <= 0) continue;

    // Finestra escludendo il punto corrente
    const lo = Math.max(0, i - Math.floor(WINDOW / 2));
    const hi = Math.min(N, i + Math.floor(WINDOW / 2) + 1);
    const window = [];
    for (let j = lo; j < hi; j++) {
      if (j === i) continue;
      const v = Number(arr[j].total_value);
      if (Number.isFinite(v) && v > 0) window.push(v);
    }
    if (window.length < 3) continue;

    const med = median(window);
    if (med <= 0) continue;
    const mad = median(window.map((v) => Math.abs(v - med))) || 1e-6;

    const dev = Math.abs(cur - med);
    const devPct = dev / med;
    if (dev > MAX_MAD * mad && devPct > MIN_PCT_DEV) {
      arr[i].total_value = med;
      arr[i]._outlier_fixed = true;
      fixed += 1;
    }
  }
  if (fixed > 0 && typeof window !== "undefined" && window.console) {
    console.log(`[AnalyticsPage] sanitizeOutliers: ${fixed} outlier riallineati (MAD-based)`);
  }
  return arr;
}

// Determina se un timestamp ricade nelle "ore di mercato" (NYSE: 14:30–21:00
// UTC, lun-ven). I crypto sono 24/7, quindi se l'utente ha posizioni crypto
// attive il grafico non si "spegne" mai.
function isMarketHours(date) {
  const d = (date instanceof Date) ? date : new Date(date);
  if (Number.isNaN(d.getTime())) return true;
  const dow = d.getUTCDay();   // 0=Sun, 6=Sat
  if (dow === 0 || dow === 6) return false;
  const utcMin = d.getUTCHours() * 60 + d.getUTCMinutes();
  const open = 14 * 60 + 30;   // 14:30 UTC = 9:30 ET
  const close = 21 * 60;       // 21:00 UTC = 16:00 ET (semplificato, ignora DST)
  return utcMin >= open && utcMin < close;
}

// Calcola gli intervalli "off-market" da renderizzare come ReferenceArea
// scure sul chart (stile Scalable Capital: grafico "spento" fuori orario).
// Restituisce array [{x1, x2}].
function computeOffHoursBands(data) {
  if (!data || data.length < 2) return [];
  const bands = [];
  let bandStart = null;
  for (let i = 0; i < data.length; i++) {
    const ts = data[i].timestamp;
    const open = isMarketHours(ts);
    if (!open && bandStart === null) {
      // Inizio banda off-hours: comincia dal punto precedente per coprire la transizione
      bandStart = i > 0 ? data[i - 1].timestamp : ts;
    } else if (open && bandStart !== null) {
      bands.push({ x1: bandStart, x2: ts });
      bandStart = null;
    }
  }
  if (bandStart !== null) {
    bands.push({ x1: bandStart, x2: data[data.length - 1].timestamp });
  }
  return bands;
}

// Equity curve con drawdown
function EquityCurveCard({ history, hasCrypto = false }) {
  if (!history?.length) {
    return <div className="empty-state">Nessuno snapshot di portafoglio nel periodo selezionato</div>;
  }

  // 1. Sanitizza outlier (spike isolati >15% che si autocorreggono = bad data)
  const cleaned = sanitizeOutliers(history);

  // 2. Downsample per leggibilità (max ~100 punti)
  const sampled = downsampleHistory(cleaned, 100);

  // Calcola drawdown rispetto al massimo precedente.
  // FIX: scarta snapshot con value <= 0 dal calcolo del runningMax (altrimenti
  // un singolo zero porta runningMax=0 e drawdown=NaN/Infinity per tutti i
  // punti successivi, rompendo il chart).
  let runningMax = -Infinity;
  const data = sampled.map((p) => {
    const v = Number(p.total_value ?? 0);
    if (Number.isFinite(v) && v > 0) {
      runningMax = Math.max(runningMax, v);
    }
    let drawdown = 0;
    if (Number.isFinite(runningMax) && runningMax > 0
        && Number.isFinite(v) && v > 0) {
      drawdown = ((v - runningMax) / runningMax) * 100;
    }
    return {
      timestamp: p.timestamp,
      value: Number.isFinite(v) ? v : 0,
      drawdown: Number.isFinite(drawdown) ? drawdown : 0,
    };
  });

  const maxDrawdown = data.reduce((min, p) => Math.min(min, p.drawdown), 0);
  const outlierCount = sampled.filter((p) => p._outlier_fixed).length;

  // Off-hours bands: solo se l'utente NON ha posizioni crypto attive
  // (le crypto sono 24/7 → il grafico deve restare "vivo" sempre).
  const offBands = hasCrypto ? [] : computeOffHoursBands(data);

  return (
    <div>
      <div style={{ marginBottom: '0.5rem', fontSize: '0.78rem', color: '#94a3b8',
                    display: 'flex', justifyContent: 'space-between', flexWrap: 'wrap', gap: 8 }}>
        <span>
          Max Drawdown: <strong style={{ color: maxDrawdown < -5 ? '#ef4444' : '#10b981' }}>
            {fmtPct(maxDrawdown, 2)}
          </strong>
        </span>
        <span style={{ display: 'flex', gap: 12 }}>
          {outlierCount > 0 && (
            <span title="Punti anomali (spike isolati che si autocorreggono) sono stati riallineati per leggibilità">
              ⚠ {outlierCount} outlier filtrato{outlierCount > 1 ? 'i' : ''}
            </span>
          )}
          {!hasCrypto && offBands.length > 0 && (
            <span title="Bande grigie: ore in cui i mercati USA sono chiusi (NYSE 9:30–16:00 ET, lun–ven). Non hai posizioni crypto attive 24/7.">
              🌙 {offBands.length} fasce off-market
            </span>
          )}
        </span>
      </div>
      <ResponsiveContainer width="100%" height={240}>
        <ComposedChart data={data} margin={{ top: 5, right: 8, left: -8, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
          {/* Off-market bands (rendering "spento" stile Scalable Capital) */}
          {offBands.map((b, idx) => (
            <ReferenceArea
              key={`off-${idx}`}
              yAxisId="left"
              x1={b.x1}
              x2={b.x2}
              fill="#0b1220"
              fillOpacity={0.55}
              stroke="none"
              ifOverflow="extendDomain"
            />
          ))}
          <XAxis
            dataKey="timestamp"
            tick={{ fill: '#64748b', fontSize: 10 }}
            tickFormatter={(t) => t ? new Date(t).toLocaleDateString('it-IT', { month: 'short', day: 'numeric' }) : ''}
            minTickGap={30}
          />
          <YAxis
            yAxisId="left"
            tick={{ fill: '#64748b', fontSize: 10 }}
            tickFormatter={(v) => `$${(v / 1000).toFixed(0)}k`}
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
              formatter={(v, name) => name === 'Drawdown' ? fmtPct(v) : fmtUsd(v)}
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
          tickFormatter={(v) => `$${v.toFixed(0)}`}
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
              <div>P&L: <strong style={{ color: d.pnl >= 0 ? '#10b981' : '#ef4444' }}>{fmtUsd(d.pnl)}</strong></div>
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
function ExposurePieCard({ positions, portfolio }) {
  // Estrai la liquidità dal portfolio. Supporta cash_balance (schema DB)
  // E cash (chiave restituita da /api/portfolio via get_portfolio_state).
  const cashRaw = portfolio?.cash_balance ?? portfolio?.cash ?? 0;
  const cash = Number(cashRaw);
  const cashValid = Number.isFinite(cash) && cash > 0;

  // Esposizione per posizione aperta
  const positionData = (positions ?? [])
    .map((p) => ({
      name: p.ticker,
      value: Math.abs(Number(p.current_price ?? 0) * Number(p.quantity ?? 0)),
      pnl: Number(p.unrealized_pnl ?? 0),
      isCash: false,
    }))
    .filter((d) => d.value > 0)
    .sort((a, b) => b.value - a.value);

  // Slice Liquidità in fondo (color grigio per distinguerla dalle posizioni).
  // Threshold abbassato a > 0 (qualsiasi cash positivo) — prima era > 0.5.
  const data = [...positionData];
  if (cashValid) {
    data.push({
      name: "Liquidità",
      value: cash,
      pnl: 0,
      isCash: true,
    });
  }

  const positionsTotal = positionData.reduce((s, d) => s + d.value, 0);
  const portfolioTotal = positionsTotal + (cashValid ? cash : 0);
  const pctInvestito = portfolioTotal > 0
    ? (positionsTotal / portfolioTotal) * 100
    : 0;
  const pctCash = portfolioTotal > 0
    ? ((cashValid ? cash : 0) / portfolioTotal) * 100
    : 0;

  if (!data.length) {
    return (
      <div className="empty-state">
        Nessuna posizione e nessun cash rilevato dal portafoglio.
      </div>
    );
  }

  return (
    <div>
      <ResponsiveContainer width="100%" height={220}>
        <PieChart>
          <Pie
            data={data}
            cx="50%"
            cy="50%"
            innerRadius={50}
            outerRadius={88}
            paddingAngle={2}
            dataKey="value"
            label={({ name, percent }) => `${name} ${(percent * 100).toFixed(0)}%`}
            labelLine={false}
            fontSize={10}
          >
            {data.map((entry, i) => (
              <Cell
                key={entry.name}
                // Cash sempre grigio scuro per non confonderlo con asset
                fill={entry.isCash ? "#64748b" : PALETTE[i % PALETTE.length]}
                stroke={entry.isCash ? "#94a3b8" : undefined}
                strokeWidth={entry.isCash ? 1 : 0}
              />
            ))}
          </Pie>
          <Tooltip content={({ active, payload }) => {
            if (!active || !payload?.length) return null;
            const d = payload[0]?.payload;
            return (
              <div style={{ background: '#0f172a', border: '1px solid #1e293b', borderRadius: 8, padding: '0.5rem 0.85rem', fontSize: '0.78rem', color: '#f1f5f9' }}>
                <div style={{ color: '#94a3b8' }}>{d.name}</div>
                <div>{d.isCash ? "Cash disponibile" : "Esposizione"}: <strong>{fmtUsd(d.value)}</strong></div>
                {!d.isCash && (
                  <div>P&L non realizzato: <strong style={{ color: d.pnl >= 0 ? '#10b981' : '#ef4444' }}>{fmtUsd(d.pnl)}</strong></div>
                )}
              </div>
            );
          }} />
        </PieChart>
      </ResponsiveContainer>

      {/* Riepilogo testuale sotto il pie — sempre visibile anche se la slice */}
      {/* fosse troppo piccola per essere notata. Garantisce che l'utente vede */}
      {/* sempre cash + esposizione + totale, indipendentemente dal rendering pie. */}
      <div style={{
        marginTop: '0.6rem',
        fontSize: '0.74rem',
        color: '#94a3b8',
        textAlign: 'center',
        lineHeight: 1.55,
        background: '#0f172a',
        border: '1px solid #1e293b',
        borderRadius: 6,
        padding: '0.5rem 0.65rem',
      }}>
        <div>
          Investito: <strong style={{ color: '#10b981' }}>{fmtUsd(positionsTotal)}</strong>
          {' '}({pctInvestito.toFixed(0)}%)
          {' · '}
          Cash: <strong style={{ color: '#cbd5e1' }}>{fmtUsd(cashValid ? cash : 0)}</strong>
          {' '}({pctCash.toFixed(0)}%)
        </div>
        <div style={{ marginTop: 2 }}>
          Totale portafoglio: <strong style={{ color: '#f1f5f9' }}>{fmtUsd(portfolioTotal)}</strong>
        </div>
      </div>
    </div>
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
// NUOVE METRICHE INTROSPETTIVE
// ============================================================

// 1) Confidence vs Outcome — scatter del confidence_score (X) vs P&L% (Y).
// Risponde alla domanda: "il confidence dell'AI è calibrato?".
// Dot grandi se trade > $5k, piccoli sotto.
function ConfidenceVsOutcomeCard({ closedTrades }) {
  if (!closedTrades?.length) {
    return <div className="empty-state">Nessun trade chiuso</div>;
  }

  // Vista SKILL: questo grafico misura se la confidence dell'AI e'
  // calibrata, quindi qui (e SOLO qui, non nel P&L) si escludono le
  // chiusure non-AI a confidence 100 — non sono confidence reali ma
  // marcatori di circuit breaker / auto-exit / chiusura manuale.
  const data = closedTrades
    .filter((c) => Number.isFinite(c.confidence) && c.confidence > 0
                   && c.confidence < 100)
    .map((c) => ({
      // Normalizza confidence: alcuni sono 0-1, altri 0-100. Forziamo 0-100.
      confidence: c.confidence > 1 ? c.confidence : c.confidence * 100,
      pnlPct: c.pnlPct,
      ticker: c.ticker,
      pnl: c.pnl,
      size: Math.min(120, Math.max(20, Math.abs(c.pnl) / 50)),
    }));

  if (!data.length) {
    return <div className="empty-state">Nessun trade chiuso ha confidence_score registrato</div>;
  }

  // Punti vincenti vs perdenti separati per dare colori diversi
  const winners = data.filter((d) => d.pnlPct >= 0);
  const losers = data.filter((d) => d.pnlPct < 0);

  return (
    <ResponsiveContainer width="100%" height={260}>
      <ScatterChart margin={{ top: 5, right: 12, left: -8, bottom: 5 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
        <XAxis
          type="number"
          dataKey="confidence"
          name="Confidence"
          domain={[0, 100]}
          tick={{ fill: '#64748b', fontSize: 10 }}
          tickFormatter={(v) => `${v}%`}
          label={{ value: 'Confidence dell\'AI al BUY', position: 'insideBottom',
                   offset: -2, fill: '#64748b', fontSize: 10 }}
        />
        <YAxis
          type="number"
          dataKey="pnlPct"
          name="P&L %"
          tick={{ fill: '#64748b', fontSize: 10 }}
          tickFormatter={(v) => `${v.toFixed(0)}%`}
        />
        <ZAxis type="number" dataKey="size" range={[20, 120]} />
        <ReferenceLine y={0} stroke="#64748b" strokeDasharray="3 3" />
        <Tooltip content={({ active, payload }) => {
          if (!active || !payload?.length) return null;
          const d = payload[0]?.payload;
          return (
            <div style={{ background: '#0f172a', border: '1px solid #1e293b', borderRadius: 8, padding: '0.5rem 0.85rem', fontSize: '0.78rem', color: '#f1f5f9' }}>
              <div style={{ color: '#94a3b8', marginBottom: 2 }}>{d?.ticker}</div>
              <div>Confidence: <strong>{d?.confidence?.toFixed(0)}%</strong></div>
              <div>Outcome: <strong style={{ color: d?.pnlPct >= 0 ? '#10b981' : '#ef4444' }}>{fmtPct(d?.pnlPct)}</strong></div>
              <div>P&L: <strong>{fmtUsd(d?.pnl)}</strong></div>
            </div>
          );
        }} />
        <Scatter name="Vincenti" data={winners} fill="#10b981" fillOpacity={0.7} />
        <Scatter name="Perdenti" data={losers} fill="#ef4444" fillOpacity={0.7} />
      </ScatterChart>
    </ResponsiveContainer>
  );
}

// 2) Rolling win rate (finestra mobile su N trade) — mostra se la
// performance dell'AI sta migliorando, peggiorando, o oscillando.
function RollingWinRateCard({ closedTrades, windowSize = 10 }) {
  if (!closedTrades?.length || closedTrades.length < windowSize) {
    return <div className="empty-state">
      Servono almeno {windowSize} trade chiusi (attuali: {closedTrades?.length ?? 0})
    </div>;
  }

  // closedTrades è già ordinato per sellTimestamp ASC (computeClosedTrades)
  const data = [];
  for (let i = windowSize - 1; i < closedTrades.length; i++) {
    const window = closedTrades.slice(i - windowSize + 1, i + 1);
    const wins = window.filter((c) => c.pnl > 0).length;
    const winRate = (wins / window.length) * 100;
    data.push({
      idx: i + 1,
      winRate,
      timestamp: closedTrades[i].sellTimestamp,
      ticker: closedTrades[i].ticker,
    });
  }

  return (
    <ResponsiveContainer width="100%" height={240}>
      <LineChart data={data} margin={{ top: 5, right: 12, left: -8, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
        <XAxis
          dataKey="idx"
          tick={{ fill: '#64748b', fontSize: 10 }}
          label={{ value: `Trade # (rolling ${windowSize})`, position: 'insideBottom',
                   offset: -2, fill: '#64748b', fontSize: 10 }}
        />
        <YAxis
          domain={[0, 100]}
          tick={{ fill: '#64748b', fontSize: 10 }}
          tickFormatter={(v) => `${v}%`}
        />
        <ReferenceLine y={50} stroke="#64748b" strokeDasharray="3 3"
                       label={{ value: '50%', position: 'right', fill: '#64748b', fontSize: 10 }} />
        <Tooltip content={({ active, payload }) => {
          if (!active || !payload?.length) return null;
          const d = payload[0]?.payload;
          return (
            <div style={{ background: '#0f172a', border: '1px solid #1e293b', borderRadius: 8, padding: '0.5rem 0.85rem', fontSize: '0.78rem', color: '#f1f5f9' }}>
              <div style={{ color: '#94a3b8' }}>Trade #{d?.idx}</div>
              <div>Win rate (rolling {windowSize}): <strong>{d?.winRate.toFixed(1)}%</strong></div>
              {d?.timestamp && <div style={{ color: '#64748b', fontSize: '0.7rem' }}>{new Date(d.timestamp).toLocaleDateString('it-IT')}</div>}
            </div>
          );
        }} />
        <Line
          type="monotone"
          dataKey="winRate"
          stroke="#3b82f6"
          strokeWidth={2}
          dot={false}
          isAnimationActive={false}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}

// 3) Equity vs Crypto P&L separati — mostra quale "gamba" del bot regge.
// Aggregato per giorno: somma P&L realizzato per asset class.
function EquityVsCryptoPnlCard({ closedTrades }) {
  if (!closedTrades?.length) {
    return <div className="empty-state">Nessun trade chiuso</div>;
  }

  const isCrypto = (t) => t.endsWith('-USD') || t.startsWith('X:') || t.endsWith('USD');

  // Group by giorno + asset class
  const byDay = {};
  for (const c of closedTrades) {
    if (!c.sellTimestamp) continue;
    const day = new Date(c.sellTimestamp).toISOString().slice(0, 10);
    const ac = isCrypto(c.ticker) ? 'crypto' : 'equity';
    if (!byDay[day]) byDay[day] = { day, equity: 0, crypto: 0 };
    byDay[day][ac] += c.pnl;
  }

  // Sort + cumulative
  const sortedDays = Object.values(byDay).sort((a, b) => a.day.localeCompare(b.day));
  let eqCum = 0, crCum = 0;
  const data = sortedDays.map((d) => {
    eqCum += d.equity;
    crCum += d.crypto;
    return { day: d.day, equityCum: eqCum, cryptoCum: crCum };
  });

  return (
    <ResponsiveContainer width="100%" height={240}>
      <LineChart data={data} margin={{ top: 5, right: 12, left: -8, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
        <XAxis
          dataKey="day"
          tick={{ fill: '#64748b', fontSize: 10 }}
          tickFormatter={(t) => {
            const d = new Date(t);
            return isNaN(d) ? t : d.toLocaleDateString('it-IT', { month: 'short', day: 'numeric' });
          }}
          minTickGap={30}
        />
        <YAxis
          tick={{ fill: '#64748b', fontSize: 10 }}
          tickFormatter={(v) => `$${v.toFixed(0)}`}
        />
        <ReferenceLine y={0} stroke="#64748b" strokeDasharray="3 3" />
        <Tooltip content={({ active, payload, label }) => {
          if (!active || !payload?.length) return null;
          return (
            <div style={{ background: '#0f172a', border: '1px solid #1e293b', borderRadius: 8, padding: '0.5rem 0.85rem', fontSize: '0.78rem', color: '#f1f5f9' }}>
              <div style={{ color: '#94a3b8', marginBottom: 2 }}>{label}</div>
              {payload.map((e, i) => (
                <div key={i} style={{ color: e.color }}>
                  {e.name}: <strong>{fmtUsd(e.value)}</strong>
                </div>
              ))}
            </div>
          );
        }} />
        <Legend wrapperStyle={{ fontSize: '0.75rem', color: '#94a3b8' }} iconType="circle" iconSize={8} />
        <Line type="monotone" dataKey="equityCum" name="Equity (cumulato)"
              stroke="#3b82f6" strokeWidth={2} dot={false} isAnimationActive={false} />
        <Line type="monotone" dataKey="cryptoCum" name="Crypto (cumulato)"
              stroke="#f59e0b" strokeWidth={2} dot={false} isAnimationActive={false} />
      </LineChart>
    </ResponsiveContainer>
  );
}

// 4) Underwater chart — % distanza dal precedente high (sempre <= 0).
// Mostra DURATA del drawdown, non solo profondità.
function UnderwaterChart({ history }) {
  if (!history?.length) {
    return <div className="empty-state">Nessuno snapshot di portafoglio</div>;
  }

  const cleaned = sanitizeOutliers(history);
  const sampled = downsampleHistory(cleaned, 150);

  let runningMax = -Infinity;
  const data = sampled.map((p) => {
    const v = Number(p.total_value ?? 0);
    if (Number.isFinite(v) && v > 0) {
      runningMax = Math.max(runningMax, v);
    }
    let underwater = 0;
    if (Number.isFinite(runningMax) && runningMax > 0 && Number.isFinite(v) && v > 0) {
      underwater = ((v - runningMax) / runningMax) * 100;
    }
    return { timestamp: p.timestamp, underwater };
  });

  // Calcola % di tempo speso underwater
  const underwaterCount = data.filter((d) => d.underwater < -0.5).length;
  const pctTimeUnder = data.length > 0 ? (underwaterCount / data.length) * 100 : 0;

  return (
    <div>
      <div style={{ marginBottom: '0.5rem', fontSize: '0.78rem', color: '#94a3b8' }}>
        Tempo sotto il massimo: <strong style={{ color: pctTimeUnder > 50 ? '#ef4444' : '#10b981' }}>
          {pctTimeUnder.toFixed(0)}%
        </strong> del periodo
      </div>
      <ResponsiveContainer width="100%" height={210}>
        <AreaChart data={data} margin={{ top: 5, right: 12, left: -8, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
          <XAxis
            dataKey="timestamp"
            tick={{ fill: '#64748b', fontSize: 10 }}
            tickFormatter={(t) => t ? new Date(t).toLocaleDateString('it-IT', { month: 'short', day: 'numeric' }) : ''}
            minTickGap={30}
          />
          <YAxis
            tick={{ fill: '#64748b', fontSize: 10 }}
            tickFormatter={(v) => `${v.toFixed(0)}%`}
            domain={['dataMin', 0]}
          />
          <Tooltip content={({ active, payload, label }) => {
            if (!active || !payload?.length) return null;
            const v = payload[0]?.value;
            return (
              <div style={{ background: '#0f172a', border: '1px solid #1e293b', borderRadius: 8, padding: '0.5rem 0.85rem', fontSize: '0.78rem', color: '#f1f5f9' }}>
                <div style={{ color: '#94a3b8' }}>{label ? new Date(label).toLocaleString('it-IT') : ''}</div>
                <div>Underwater: <strong style={{ color: v < -1 ? '#ef4444' : '#10b981' }}>{fmtPct(v)}</strong></div>
              </div>
            );
          }} />
          <Area
            type="monotone"
            dataKey="underwater"
            stroke="#ef4444"
            fill="#ef4444"
            fillOpacity={0.25}
            strokeWidth={1.5}
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

// 5) P&L medio per ora del giorno (UTC) — rivela bias temporali.
function PnlByHourCard({ closedTrades }) {
  if (!closedTrades?.length) {
    return <div className="empty-state">Nessun trade chiuso</div>;
  }

  // Aggrega P&L per ora del BUY (entry timing).
  const byHour = Array.from({ length: 24 }, (_, h) => ({
    hour: h, pnl: 0, count: 0,
  }));

  for (const c of closedTrades) {
    const ts = c.buyTimestamp ? new Date(c.buyTimestamp) : null;
    if (!ts || isNaN(ts)) continue;
    const h = ts.getUTCHours();
    byHour[h].pnl += c.pnl;
    byHour[h].count += 1;
  }

  const data = byHour.map((b) => ({
    hour: `${String(b.hour).padStart(2, '0')}:00`,
    avgPnl: b.count > 0 ? b.pnl / b.count : 0,
    count: b.count,
    totalPnl: b.pnl,
  }));

  return (
    <ResponsiveContainer width="100%" height={240}>
      <BarChart data={data} margin={{ top: 5, right: 12, left: -8, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
        <XAxis
          dataKey="hour"
          tick={{ fill: '#64748b', fontSize: 9 }}
          interval={1}
          label={{ value: 'Ora UTC del BUY', position: 'insideBottom', offset: -2,
                   fill: '#64748b', fontSize: 10 }}
        />
        <YAxis
          tick={{ fill: '#64748b', fontSize: 10 }}
          tickFormatter={(v) => `$${v.toFixed(0)}`}
        />
        <ReferenceLine y={0} stroke="#64748b" strokeDasharray="3 3" />
        <Tooltip content={({ active, payload }) => {
          if (!active || !payload?.length) return null;
          const d = payload[0]?.payload;
          return (
            <div style={{ background: '#0f172a', border: '1px solid #1e293b', borderRadius: 8, padding: '0.5rem 0.85rem', fontSize: '0.78rem', color: '#f1f5f9' }}>
              <div style={{ color: '#94a3b8' }}>Ora: {d.hour} UTC</div>
              <div>P&L medio per trade: <strong style={{ color: d.avgPnl >= 0 ? '#10b981' : '#ef4444' }}>{fmtUsd(d.avgPnl)}</strong></div>
              <div>P&L totale: <strong>{fmtUsd(d.totalPnl)}</strong></div>
              <div>Trade chiusi: <strong>{d.count}</strong></div>
            </div>
          );
        }} />
        <Bar dataKey="avgPnl" radius={[3, 3, 0, 0]}>
          {data.map((d, i) => (
            <Cell key={i} fill={d.count === 0 ? '#1f2937' : (d.avgPnl >= 0 ? '#10b981' : '#ef4444')}
                  fillOpacity={0.85} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

// Card riusabile con titolo + descrizione esplicativa + subtitle
function ChartCard({ title, description, subtitle, children }) {
  return (
    <div className="card" style={{
      background: '#111827',
      border: '1px solid #1f2937',
      borderRadius: 10,
      padding: '1rem 1.1rem',
    }}>
      <div className="card-header" style={{
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'flex-start',
        marginBottom: 6,
        gap: 12,
      }}>
        <span className="card-title" style={{
          fontSize: '0.95rem',
          fontWeight: 700,
          color: '#f1f5f9',
        }}>{title}</span>
        {subtitle && (
          <span style={{ fontSize: '0.7rem', color: '#64748b',
                         flexShrink: 0, paddingTop: 3 }}>
            {subtitle}
          </span>
        )}
      </div>
      {description && (
        <p style={{
          fontSize: '0.72rem',
          color: '#64748b',
          margin: '0 0 12px 0',
          lineHeight: 1.45,
        }}>{description}</p>
      )}
      {children}
    </div>
  );
}

// ============================================================
// Main component
// ============================================================

const PERIOD_OPTIONS = [
  { key: "1d", label: "1 giorno" },
  { key: "7d", label: "7 giorni" },
  { key: "30d", label: "30 giorni" },
  { key: "90d", label: "90 giorni" },
  { key: "all", label: "Tutto" },
];

export default function AnalyticsPage() {
  const [positions, setPositions] = useState([]);
  const [trades, setTrades] = useState([]);
  const [history, setHistory] = useState([]);
  const [portfolio, setPortfolio] = useState(null);
  const [benchmark, setBenchmark] = useState(null);
  const [period, setPeriod] = useState("30d");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [researchOpen, setResearchOpen] = useState(false);

  useEffect(() => {
    const fetchData = async () => {
      setLoading(true);
      setError(null);
      try {
        const [posRes, tradeRes, histRes, portRes, benchRes] = await Promise.all([
          fetch(`${API}/api/positions`),
          // FIX: limit alto per non troncare le statistiche. Bug precedente:
          // default 50 → 80+ trade reali → KPI calcolati solo sugli ultimi 50.
          fetch(`${API}/api/trades?limit=1000`),
          fetch(`${API}/api/portfolio/history?period=${period}`),
          // Portfolio per estrarre cash_balance e mostrare la Liquidità
          // nel pie chart Esposizione Attuale.
          fetch(`${API}/api/portfolio`),
          // Confronto vs S&P 500: isola alpha (bravura) da beta (mercato).
          fetch(`${API}/api/portfolio/benchmark?period=${period}`),
        ]);

        const [posData, tradeData, histData, portData, benchData] = await Promise.all([
          posRes.ok ? posRes.json() : Promise.resolve([]),
          tradeRes.ok ? tradeRes.json() : Promise.resolve([]),
          histRes.ok ? histRes.json() : Promise.resolve([]),
          portRes.ok ? portRes.json() : Promise.resolve(null),
          benchRes.ok ? benchRes.json() : Promise.resolve(null),
        ]);

        setPositions(Array.isArray(posData) ? posData : posData?.positions ?? []);
        setTrades(Array.isArray(tradeData) ? tradeData : tradeData?.trades ?? []);
        setHistory(Array.isArray(histData) ? histData : []);
        setPortfolio(portData);
        setBenchmark(benchData && benchData.available ? benchData : null);
      } catch (err) {
        setError(err.message ?? 'Errore nel caricamento dei dati');
      } finally {
        setLoading(false);
      }
    };

    fetchData();
  }, [period]);

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
      <div style={{ marginBottom: '1.5rem', display: 'flex',
                    justifyContent: 'space-between', alignItems: 'flex-end', flexWrap: 'wrap', gap: 12 }}>
        <div>
          <h2 style={{ fontSize: '1.4rem', fontWeight: 700, color: '#f1f5f9', marginBottom: '0.25rem' }}>
            Analytics
          </h2>
          <p style={{ fontSize: '0.85rem', color: '#94a3b8' }}>
            {kpis.totalPositions} posizioni aperte · {kpis.totalTrades} trade totali · {closedTrades.length} chiusi
          </p>
        </div>
        <div style={{ display: 'flex', gap: 4, background: '#0f172a',
                      border: '1px solid #1e293b', borderRadius: 8, padding: 4 }}>
          {PERIOD_OPTIONS.map((p) => (
            <button key={p.key}
                    onClick={() => setPeriod(p.key)}
                    style={{
                      padding: '6px 12px',
                      background: period === p.key ? '#3b82f6' : 'transparent',
                      color: period === p.key ? '#fff' : '#94a3b8',
                      border: 'none', borderRadius: 6,
                      fontSize: '0.75rem', fontWeight: 600,
                      cursor: 'pointer',
                    }}>
              {p.label}
            </button>
          ))}
        </div>
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
          value={fmtUsd(kpis.realizedPnl)}
          subValue={`${kpis.winnersCount} win · ${kpis.losersCount} loss`}
          color={realizedColor}
        />
        <KpiCard
          label="P&L Non Realizzato"
          value={fmtUsd(kpis.unrealizedPnl)}
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
          subValue={kpis.bestTrade ? `${kpis.bestTrade.ticker} · ${fmtUsd(kpis.bestTrade.pnl)}` : ''}
          color="#10b981"
        />
        <KpiCard
          label="Worst Trade"
          value={kpis.worstTrade ? fmtPct(kpis.worstTrade.pnlPct, 2) : '—'}
          subValue={kpis.worstTrade ? `${kpis.worstTrade.ticker} · ${fmtUsd(kpis.worstTrade.pnl)}` : ''}
          color="#ef4444"
        />
        {benchmark && benchmark.alpha_pct != null && (
          <KpiCard
            label="Alpha vs S&P 500"
            value={`${benchmark.alpha_pct >= 0 ? '+' : ''}${benchmark.alpha_pct}pp`}
            subValue={`Tu ${benchmark.portfolio_return_pct >= 0 ? '+' : ''}${benchmark.portfolio_return_pct}% · S&P ${benchmark.sp500_return_pct >= 0 ? '+' : ''}${benchmark.sp500_return_pct}%`}
            color={benchmark.alpha_pct > 0 ? '#10b981' : '#ef4444'}
            hint={benchmark.portfolio_sharpe_est != null
              ? `Sharpe~${benchmark.portfolio_sharpe_est} · maxDD ${benchmark.portfolio_max_drawdown_pct}% vs ${benchmark.sp500_max_drawdown_pct}%`
              : 'alpha GREZZO — vedi verdetto sotto'}
          />
        )}
      </div>

      {/* Charts grid */}
      <div className="analytics-grid" style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(420px, 1fr))',
        gap: '1rem',
      }}>
        {benchmark && (() => {
          // Resample lineare a 60 punti comuni: asse X = % periodo
          // trascorso, cosi' le due serie (snapshot portfolio vs daily
          // SPY, lunghezze diverse) sono visivamente comparabili.
          const K = 60;
          const rs = (arr) => {
            if (!arr || arr.length === 0) return [];
            if (arr.length === 1) return Array(K).fill(arr[0]);
            const out = [];
            for (let i = 0; i < K; i++) {
              const pos = (i / (K - 1)) * (arr.length - 1);
              const lo = Math.floor(pos), hi = Math.ceil(pos);
              const frac = pos - lo;
              out.push(arr[lo] * (1 - frac) + arr[hi] * frac);
            }
            return out;
          };
          const pN = rs(benchmark.portfolio_series_norm);
          const sN = rs(benchmark.sp500_series_norm);
          const data = pN.map((v, i) => ({
            t: Math.round((i / (K - 1)) * 100),
            Portafoglio: Number(v.toFixed(2)),
            'S&P 500': sN[i] != null ? Number(sN[i].toFixed(2)) : null,
          }));
          const vMap = {
            alpha_plausibile: { c: '#10b981', t: '✅ Segnale di alpha' },
            beta_travestito: { c: '#f59e0b', t: '⚠️ Beta travestito da alpha' },
            difensivo_sotto: { c: '#38bdf8', t: '🛡️ Difensivo (no edge di rendimento)' },
            sotto_benchmark: { c: '#ef4444', t: '❌ Sotto il benchmark' },
            insufficiente: { c: '#64748b', t: 'Dati insufficienti' },
          };
          const vd = vMap[benchmark.verdict] || vMap.insufficiente;
          return (
            <ChartCard
              title="Performance vs S&P 500 (base 100)"
              description="Confronto a parità di punto di partenza. Battere l'S&P in rendimento NON basta: conta il rendimento aggiustato per il rischio (drawdown). Un mese non distingue edge da fortuna di regime."
              subtitle={`Periodo: ${PERIOD_OPTIONS.find(o => o.key === period)?.label}`}>
              <ResponsiveContainer width="100%" height={240}>
                <LineChart data={data} margin={{ top: 5, right: 12, left: -8, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                  <XAxis dataKey="t" stroke="#64748b" fontSize={11}
                         tickFormatter={(v) => `${v}%`} />
                  <YAxis stroke="#64748b" fontSize={11}
                         domain={['auto', 'auto']} />
                  <Tooltip
                    contentStyle={{ background: '#0f172a', border: '1px solid #1e293b',
                                    borderRadius: 8, fontSize: 12 }}
                    formatter={(v) => (v != null ? v.toFixed(2) : '—')}
                    labelFormatter={(l) => `${l}% del periodo`} />
                  <Legend wrapperStyle={{ fontSize: 12 }} />
                  <Line type="monotone" dataKey="Portafoglio" stroke="#3b82f6"
                        strokeWidth={2} dot={false} />
                  <Line type="monotone" dataKey="S&P 500" stroke="#94a3b8"
                        strokeWidth={2} strokeDasharray="5 4" dot={false} />
                </LineChart>
              </ResponsiveContainer>
              {benchmark.data_anomaly && (
                <div style={{
                  marginTop: 10, padding: '10px 12px', borderRadius: 8,
                  background: '#7c2d1222', border: '1px solid #f59e0b66',
                }}>
                  <div style={{ fontSize: '0.8rem', fontWeight: 700, color: '#f59e0b' }}>
                    ⚠️ Periodo con {benchmark.n_anomalies} anomalia/e tecnica/e — dati sanitizzati
                  </div>
                  <div style={{ fontSize: '0.74rem', color: '#cbd5e1', marginTop: 4, lineHeight: 1.5 }}>
                    Rilevati salti &gt;±12% tra snapshot consecutivi: glitch di
                    valutazione, NON decisioni di trading. Le metriche sotto sono
                    ricostruite escludendo quei salti.
                    <br />
                    <span style={{ color: '#64748b' }}>
                      Grezzo (NON affidabile): return {benchmark.portfolio_return_pct_raw >= 0 ? '+' : ''}{benchmark.portfolio_return_pct_raw}%
                      · maxDD {benchmark.portfolio_max_drawdown_pct_raw}%
                    </span>
                  </div>
                </div>
              )}
              <div style={{
                marginTop: 10, padding: '10px 12px', borderRadius: 8,
                background: `${vd.c}1a`, border: `1px solid ${vd.c}55`,
              }}>
                <div style={{ fontSize: '0.82rem', fontWeight: 700, color: vd.c }}>
                  {vd.t}{benchmark.data_anomaly ? ' (su dati puliti)' : ''}
                </div>
                <div style={{ fontSize: '0.78rem', color: '#cbd5e1', marginTop: 4, lineHeight: 1.5 }}>
                  {benchmark.verdict_detail}
                </div>
                <div style={{ fontSize: '0.72rem', color: '#64748b', marginTop: 6 }}>
                  Tu {benchmark.portfolio_return_pct >= 0 ? '+' : ''}{benchmark.portfolio_return_pct}%
                  (maxDD {benchmark.portfolio_max_drawdown_pct}%)
                  {benchmark.data_anomaly ? ' [pulito]' : ''}
                  {' · '}S&P {benchmark.sp500_return_pct >= 0 ? '+' : ''}{benchmark.sp500_return_pct}%
                  (maxDD {benchmark.sp500_max_drawdown_pct}%)
                  {benchmark.portfolio_sharpe_est != null
                    ? ` · Sharpe~${benchmark.portfolio_sharpe_est}` : ''}
                </div>
              </div>
            </ChartCard>
          );
        })()}
        <ChartCard title="Equity Curve & Drawdown"
                   description="Valore del portafoglio nel tempo (linea blu, asse sx) e perdita rispetto al massimo storico (rosso, asse dx). Un drawdown -10% significa che ora vali il 10% in meno del tuo picco."
                   subtitle={`Periodo: ${PERIOD_OPTIONS.find(o => o.key === period)?.label}`}>
          <EquityCurveCard
            history={history}
            hasCrypto={positions.some((p) => {
              const t = (p.ticker || p.symbol || '').toUpperCase();
              return t.endsWith('-USD') || t.startsWith('X:');
            })}
          />
        </ChartCard>

        <ChartCard title="Confidence vs Outcome"
                   description="Per ogni trade chiuso: la confidence dichiarata dall'AI al momento del BUY (asse X) vs il rendimento effettivo % alla chiusura (asse Y). Se i punti ad alta confidence sono prevalentemente sopra lo zero, il modello è ben calibrato. Se non c'è correlazione, la confidence è rumore."
                   subtitle={`${closedTrades.length} trade`}>
          <ConfidenceVsOutcomeCard closedTrades={closedTrades} />
        </ChartCard>

        <ChartCard title="Rolling Win Rate"
                   description="Win rate calcolato su una finestra mobile degli ultimi 10 trade. Risponde a 'la performance sta migliorando o peggiorando?'. Una media globale (60%) può nascondere un trend che parte da 80% e cala al 30%."
                   subtitle="Window 10">
          <RollingWinRateCard closedTrades={closedTrades} windowSize={10} />
        </ChartCard>

        <ChartCard title="Equity vs Crypto (P&L cumulato)"
                   description="P&L realizzato cumulato separato per asset class. Il bot trada due universi diversi (mercato regolamentato vs 24/7) — è importante vedere quale gamba regge il sistema."
                   subtitle="Per giorno">
          <EquityVsCryptoPnlCard closedTrades={closedTrades} />
        </ChartCard>

        <ChartCard title="Tempo in Drawdown (Underwater)"
                   description="Distanza in % dal MASSIMO STORICO del portafoglio (high-water mark), sempre ≤ 0. NB: anche se sei in profitto rispetto al capitale iniziale, sei 'underwater' finché non SUPERI il tuo massimo precedente. Esempio: parti da 100k, sali a 120k, scendi a 115k → underwater = -4.2% (non in perdita assoluta, ma sotto il picco). Mostra non solo la profondità del drawdown ma QUANTO TEMPO il bot ci sta dentro: un bot che passa il 70% del tempo sotto il picco è da rivedere anche se la profondità è contenuta."
                   subtitle={`Periodo: ${PERIOD_OPTIONS.find(o => o.key === period)?.label}`}>
          <UnderwaterChart history={history} />
        </ChartCard>

        <ChartCard title="P&L Medio per Ora del Giorno (UTC)"
                   description="Per ciascuna ora UTC in cui il bot ha aperto un BUY, P&L medio dei trade chiusi. Rivela bias temporali: es. trade aperti alle 14:30 UTC (apertura NYSE) sono mediamente peggiori per via dell'alta volatilità → utile per aggiustare il cron schedule."
                   subtitle="Entry hour">
          <PnlByHourCard closedTrades={closedTrades} />
        </ChartCard>

        <ChartCard title="P&L per Ticker"
                   description="Quale asset ti ha fatto guadagnare o perdere di più (somma dei P&L sui trade chiusi). Verde = vincente, rosso = perdente."
                   subtitle="Top 12">
          <PnlByTickerCard closedTrades={closedTrades} />
        </ChartCard>

        <ChartCard title="Win Rate per Ticker"
                   description="Per ogni asset: % di trade chiusi in profitto. Verde >= 50% (più vincenti che perdenti), rosso < 50%."
                   subtitle="Top 10">
          <WinRateByTickerCard closedTrades={closedTrades} />
        </ChartCard>

        <ChartCard title="Esposizione Attuale"
                   description="Composizione del portafoglio: % di ogni posizione + slice grigio Liquidità (cash non investito). Aiuta a vedere se sei concentrato/diversificato e quanto cash hai da redeployare."
                   subtitle={`${positions.length} posizion${positions.length === 1 ? "e" : "i"} + cash`}>
          <ExposurePieCard positions={positions} portfolio={portfolio} />
        </ChartCard>
      </div>

      {/* Tastino discreto in basso a destra → pannello ricerca avanzata
          (analisi suggerite da Michael). Tenuto fuori dal flusso della
          pagina per non appesantirla. */}
      <button onClick={() => setResearchOpen(true)} title="Ricerca avanzata"
        style={{
          position: "fixed", bottom: 20, right: 20, zIndex: 90,
          background: "#1e293b", color: "#93c5fd",
          border: "1px solid #334155", borderRadius: 999,
          padding: "9px 16px", fontSize: "0.78rem", fontWeight: 600,
          cursor: "pointer", boxShadow: "0 2px 10px rgba(0,0,0,0.35)",
        }}>
        🔬 Ricerca
      </button>
      <ResearchPanel open={researchOpen}
        onClose={() => setResearchOpen(false)} />
    </div>
  );
}
