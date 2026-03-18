import React, { useState, useEffect } from "react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
} from "recharts";

const API = window.location.origin;

const formatEUR = (val) =>
  new Intl.NumberFormat("it-IT", { style: "currency", currency: "EUR" }).format(val);

const PERIOD_OPTIONS = [
  { label: "1W", value: "7d" },
  { label: "1M", value: "30d" },
  { label: "3M", value: "90d" },
  { label: "ALL", value: "all" },
];

export default function DashboardPage({ portfolio, positions, trades, logs }) {
  const [period, setPeriod] = useState("30d");
  const [showTradeModal, setShowTradeModal] = useState(false);
  const [expandedTradeIdx, setExpandedTradeIdx] = useState(null);
  const [historyData, setHistoryData] = useState([]);

  const totalValue = portfolio?.total_value ?? 0;
  const initialBalance = portfolio?.initial_balance ?? 100000;
  const pnl = totalValue - initialBalance;
  const pnlPct = initialBalance > 0 ? (pnl / initialBalance) * 100 : 0;
  const openPositions = positions?.length ?? 0;

  useEffect(() => {
    fetch(`${API}/api/portfolio/history?period=${period}`)
      .then((res) => res.json())
      .then((data) => setHistoryData(Array.isArray(data) ? data : []))
      .catch(() => setHistoryData([]));
  }, [period]);

  const lastValue = historyData.length > 0 ? historyData[historyData.length - 1]?.total_value : null;
  const lineColor = lastValue !== null && lastValue >= initialBalance ? "#10b981" : "#ef4444";

  const formatXAxis = (tick) => {
    if (!tick) return "";
    const d = new Date(tick);
    if (isNaN(d)) return tick;
    return d.toLocaleDateString("it-IT", { month: "short", day: "numeric" });
  };

  const CustomTooltip = ({ active, payload, label }) => {
    if (active && payload && payload.length) {
      return (
        <div
          style={{
            background: "#111827",
            border: "1px solid #374151",
            borderRadius: 6,
            padding: "8px 12px",
            fontSize: 13,
          }}
        >
          <div style={{ color: "#9ca3af", marginBottom: 2 }}>{formatXAxis(label)}</div>
          <div style={{ color: lineColor, fontWeight: 600 }}>
            {formatEUR(payload[0].value)}
          </div>
        </div>
      );
    }
    return null;
  };

  const toggleTrade = (idx) => {
    setExpandedTradeIdx(expandedTradeIdx === idx ? null : idx);
  };

  return (
    <div className="dashboard-page">
      {/* Metrics Bar */}
      <div className="metrics-bar">
        <div className="metric-card">
          <div className="metric-label">Patrimonio Totale</div>
          <div className="metric-value">{formatEUR(totalValue)}</div>
        </div>

        <div className="metric-card">
          <div className="metric-label">P&amp;L Totale</div>
          <div
            className="metric-value"
            style={{ color: pnl >= 0 ? "#10b981" : "#ef4444" }}
          >
            {pnl >= 0 ? "+" : ""}
            {formatEUR(pnl)}
          </div>
        </div>

        <div className="metric-card">
          <div className="metric-label">P&amp;L %</div>
          <div
            className="metric-value"
            style={{ color: pnlPct >= 0 ? "#10b981" : "#ef4444" }}
          >
            {pnlPct >= 0 ? "+" : ""}
            {pnlPct.toFixed(2)}%
          </div>
        </div>

        <div className="metric-card">
          <div className="metric-label">Posizioni Aperte</div>
          <div className="metric-value">{openPositions}</div>
        </div>
      </div>

      {/* Equity Chart */}
      <div className="chart-container card">
        <div className="chart-header">
          <h2 className="card-title">Equity Curve</h2>
          <div className="period-selector">
            {PERIOD_OPTIONS.map((opt) => (
              <button
                key={opt.value}
                className={`period-btn${period === opt.value ? " active" : ""}`}
                onClick={() => setPeriod(opt.value)}
              >
                {opt.label}
              </button>
            ))}
          </div>
        </div>

        {historyData.length === 0 ? (
          <div className="no-data">Nessun dato disponibile</div>
        ) : (
          <ResponsiveContainer width="100%" height={300}>
            <LineChart data={historyData} margin={{ top: 10, right: 20, left: 10, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
              <XAxis
                dataKey="timestamp"
                tickFormatter={formatXAxis}
                tick={{ fill: "#6b7280", fontSize: 12 }}
                axisLine={{ stroke: "#374151" }}
                tickLine={false}
              />
              <YAxis
                tickFormatter={(v) => formatEUR(v)}
                tick={{ fill: "#6b7280", fontSize: 11 }}
                axisLine={{ stroke: "#374151" }}
                tickLine={false}
                width={110}
              />
              <Tooltip content={<CustomTooltip />} />
              <Line
                type="monotone"
                dataKey="total_value"
                stroke={lineColor}
                strokeWidth={2}
                dot={false}
                activeDot={{ r: 4, fill: lineColor }}
              />
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>

      {/* Toggle Trade Modal Button */}
      <div style={{ marginBottom: "1.5rem" }}>
        <button className="btn-primary" onClick={() => setShowTradeModal(true)}>
          Vedi tutte le transazioni
        </button>
      </div>

      {/* Trade Modal */}
      {showTradeModal && (
        <div className="modal-overlay" onClick={() => setShowTradeModal(false)}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>Tutte le Transazioni</h2>
              <button
                className="modal-close"
                onClick={() => setShowTradeModal(false)}
              >
                &times;
              </button>
            </div>

            <div className="modal-body">
              {!trades || trades.length === 0 ? (
                <div className="no-data">Nessuna transazione disponibile</div>
              ) : (
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Data</th>
                      <th>Ticker</th>
                      <th>Azione</th>
                      <th>Quantita</th>
                      <th>Prezzo</th>
                      <th>Confidence</th>
                    </tr>
                  </thead>
                  <tbody>
                    {trades.map((trade, idx) => (
                      <React.Fragment key={idx}>
                        <tr
                          className="trade-row"
                          onClick={() => toggleTrade(idx)}
                          style={{ cursor: "pointer" }}
                        >
                          <td>
                            {trade.timestamp
                              ? new Date(trade.timestamp).toLocaleString("it-IT")
                              : "—"}
                          </td>
                          <td style={{ fontWeight: 600 }}>{trade.ticker}</td>
                          <td>
                            <span
                              className={`badge ${
                                trade.action === "BUY" ? "badge-buy" : "badge-sell"
                              }`}
                            >
                              {trade.action}
                            </span>
                          </td>
                          <td>{trade.quantity}</td>
                          <td>{formatEUR(trade.price)}</td>
                          <td>
                            {trade.confidence_score != null
                              ? `${Number(trade.confidence_score).toFixed(0)}%`
                              : "—"}
                          </td>
                        </tr>
                        {expandedTradeIdx === idx && (
                          <tr className="trade-reasoning-row">
                            <td colSpan={6}>
                              <div className="trade-reasoning">
                                <strong>Reasoning:</strong>{" "}
                                {trade.final_decision || trade.geopolitical_reasoning || trade.technical_reasoning || "Nessun dettaglio disponibile."}
                              </div>
                            </td>
                          </tr>
                        )}
                      </React.Fragment>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Positions Table */}
      <div className="card" style={{ marginBottom: "1.5rem" }}>
        <h2 className="card-title">Posizioni Aperte</h2>
        {!positions || positions.length === 0 ? (
          <div className="no-data">Nessuna posizione aperta</div>
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th>Ticker</th>
                <th>Quantita</th>
                <th>Prezzo Medio</th>
                <th>Prezzo Attuale</th>
                <th>P&amp;L</th>
                <th>P&amp;L %</th>
                <th>Azione</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((pos, idx) => {
                const pnlPos = pos.unrealized_pnl ?? 0;
                const costBasis = pos.avg_buy_price * pos.quantity;
                const pnlPctPos = costBasis !== 0 ? (pnlPos / costBasis) * 100 : 0;
                const rowClass = pnlPos >= 0 ? "row-positive" : "row-negative";
                const pnlColor = pnlPos >= 0 ? "#10b981" : "#ef4444";

                return (
                  <tr key={idx} className={rowClass}>
                    <td style={{ fontWeight: 600 }}>{pos.ticker}</td>
                    <td>{pos.quantity}</td>
                    <td>{formatEUR(pos.avg_buy_price)}</td>
                    <td>{formatEUR(pos.current_price)}</td>
                    <td style={{ color: pnlColor, fontWeight: 500 }}>
                      {pnlPos >= 0 ? "+" : ""}
                      {formatEUR(pnlPos)}
                    </td>
                    <td style={{ color: pnlColor, fontWeight: 500 }}>
                      {pnlPctPos >= 0 ? "+" : ""}
                      {pnlPctPos.toFixed(2)}%
                    </td>
                    <td>
                      <button
                      className="btn-secondary btn-sm"
                      onClick={async (e) => {
                        e.stopPropagation();
                        if (!window.confirm(`Chiudere la posizione ${pos.ticker}?`)) return;
                        try {
                          const res = await fetch(`${API}/api/positions/close`, {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ ticker: pos.ticker }),
                          });
                          if (!res.ok) throw new Error(`HTTP ${res.status}`);
                        } catch (err) {
                          console.error("Errore chiusura posizione:", err);
                        }
                      }}
                    >Chiudi</button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      {/* Agent Logs */}
      <div className="card">
        <h2 className="card-title">Agent Logs</h2>
        {!logs || logs.length === 0 ? (
          <div className="no-data">Nessun log disponibile</div>
        ) : (
          <div className="logs-container">
            {logs.map((log, idx) => (
              <div key={idx} className="log-entry">
                <span className={`log-badge phase-${(log.phase || "info").toLowerCase()}`}>
                  {log.phase || "INFO"}
                </span>
                <span className="log-timestamp">
                  {log.timestamp
                    ? new Date(log.timestamp).toLocaleString("it-IT")
                    : ""}
                </span>
                <span className="log-message">{log.content || log.message}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
