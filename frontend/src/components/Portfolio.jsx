import React, { useMemo } from "react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
} from "recharts";

// Formattazione valuta EUR
const fmtEur = (val) => {
  if (val == null) return "-";
  return new Intl.NumberFormat("it-IT", {
    style: "currency",
    currency: "EUR",
    minimumFractionDigits: 2,
  }).format(val);
};

// Formattazione percentuale
const fmtPct = (val) => {
  if (val == null) return "-";
  return `${val >= 0 ? "+" : ""}${val.toFixed(2)}%`;
};

// Tooltip personalizzato per il grafico equity
const CustomTooltip = ({ active, payload }) => {
  if (!active || !payload || !payload.length) return null;
  const d = payload[0].payload;
  return (
    <div
      style={{
        background: "#1e2028",
        border: "1px solid #2a2d35",
        borderRadius: 6,
        padding: "0.5rem 0.75rem",
        fontSize: "0.75rem",
      }}
    >
      <div style={{ color: "#8a8f98", marginBottom: 2 }}>{d.label}</div>
      <div style={{ color: "#e0e0e0", fontWeight: 700 }}>
        {fmtEur(d.total_value)}
      </div>
    </div>
  );
};

function Portfolio({ portfolio, trades }) {
  // Calcola la curva equity dai dati delle operazioni
  const equityData = useMemo(() => {
    if (!trades || trades.length === 0) return [];

    // Costruisce punti della curva basati sul valore cumulativo
    let runningValue = portfolio?.initial_capital || 100000;
    const points = [];

    // Ordina per data
    const sorted = [...trades].sort(
      (a, b) => new Date(a.timestamp) - new Date(b.timestamp)
    );

    sorted.forEach((t) => {
      const total = (t.price || 0) * (t.quantity || 0);
      if (t.action === "BUY") {
        runningValue -= total;
      } else if (t.action === "SELL") {
        runningValue += total;
      }
      points.push({
        label: new Date(t.timestamp).toLocaleDateString("it-IT", {
          day: "2-digit",
          month: "short",
        }),
        total_value: runningValue,
      });
    });

    return points;
  }, [trades, portfolio]);

  // Valori dal portafoglio
  const cash = portfolio?.cash ?? 0;
  const totalValue = portfolio?.total_value ?? cash;
  const initialCapital = portfolio?.initial_capital ?? 100000;
  const pnl = totalValue - initialCapital;
  const pnlPct = initialCapital > 0 ? (pnl / initialCapital) * 100 : 0;
  const pnlClass = pnl >= 0 ? "positive" : "negative";

  return (
    <div className="card">
      <div className="card-title">Panoramica Portafoglio</div>

      {/* Metriche principali */}
      <div className="portfolio-metrics">
        <div className="metric-box">
          <div className="metric-label">Liquidita</div>
          <div className="metric-value">{fmtEur(cash)}</div>
        </div>
        <div className="metric-box">
          <div className="metric-label">Valore Totale</div>
          <div className="metric-value">{fmtEur(totalValue)}</div>
        </div>
        <div className="metric-box">
          <div className="metric-label">P&L</div>
          <div className={`metric-value ${pnlClass}`}>{fmtEur(pnl)}</div>
        </div>
        <div className="metric-box">
          <div className="metric-label">P&L %</div>
          <div className={`metric-value ${pnlClass}`}>{fmtPct(pnlPct)}</div>
        </div>
      </div>

      {/* Curva equity */}
      {equityData.length > 1 ? (
        <div style={{ width: "100%", height: 200 }}>
          <ResponsiveContainer>
            <LineChart data={equityData}>
              <CartesianGrid strokeDasharray="3 3" stroke="#2a2d35" />
              <XAxis
                dataKey="label"
                tick={{ fill: "#5a5f6a", fontSize: 11 }}
                axisLine={{ stroke: "#2a2d35" }}
                tickLine={false}
              />
              <YAxis
                tick={{ fill: "#5a5f6a", fontSize: 11 }}
                axisLine={{ stroke: "#2a2d35" }}
                tickLine={false}
                tickFormatter={(v) => `${(v / 1000).toFixed(0)}k`}
                width={50}
              />
              <Tooltip content={<CustomTooltip />} />
              <Line
                type="monotone"
                dataKey="total_value"
                stroke={pnl >= 0 ? "#00c853" : "#ff1744"}
                strokeWidth={2}
                dot={false}
                activeDot={{ r: 4, fill: "#448aff" }}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      ) : (
        <div className="empty-state">
          Nessun dato sufficiente per la curva equity
        </div>
      )}
    </div>
  );
}

export default Portfolio;
