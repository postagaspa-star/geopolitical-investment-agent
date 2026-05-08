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
import AgentActivityCards from "../components/AgentActivityCards";

const API = window.location.origin;

// Portafoglio in USD: i prezzi nel DB sono numeri raw da yfinance/Massive
// (entrambi ritornano USD). Non c'è conversione FX nel codebase, quindi il
// simbolo $ è la rappresentazione corretta.
const formatUSD = (val) =>
  new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(val ?? 0);

const PERIOD_OPTIONS = [
  { label: "1H", value: "1h" },
  { label: "4H", value: "4h" },
  { label: "1D", value: "1d" },
  { label: "1W", value: "7d" },
  { label: "1M", value: "30d" },
  { label: "3M", value: "90d" },
  { label: "ALL", value: "all" },
];

// Periodi intraday che usano il filtro client-side sui punti minute-by-minute
const INTRADAY_PERIODS = new Set(["1h", "4h", "1d"]);
const INTRADAY_HOURS = { "1h": 1, "4h": 4, "1d": 24 };

// Filtro outlier MAD-based: rimuove step-jump anomali nell'equity curve
// causati da bad price snapshot persistiti (es. salto +43k → +26k in pochi
// minuti). Strategia: per ogni punto guarda finestra ±3 vicini, se il punto
// devia >MAX_MAD * MAD dalla median locale E lo scostamento è > MIN_PCT, lo
// sostituisce con la median. Robusto anche su jump piatti (più punti consecutivi
// allo stesso valore corrotto): finché la maggioranza della finestra è "sana",
// il punto viene corretto.
function sanitizeOutliers(history) {
  if (!history || history.length < 5) return history || [];
  const arr = history.map((p) => ({ ...p }));
  const N = arr.length;
  const WINDOW = 7;
  const MAX_MAD = 6;
  const MIN_PCT_DEV = 0.10; // 10% di scostamento minimo

  const median = (xs) => {
    if (!xs.length) return 0;
    const s = [...xs].sort((a, b) => a - b);
    const m = Math.floor(s.length / 2);
    return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
  };

  for (let i = 0; i < N; i++) {
    const cur = Number(arr[i].total_value);
    if (!Number.isFinite(cur) || cur <= 0) continue;

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
    }
  }
  return arr;
}

export default function DashboardPage({ portfolio, positions, trades, logs }) {
  const [period, setPeriod] = useState("1d");
  const [showTradeModal, setShowTradeModal] = useState(false);
  const [expandedTradeIdx, setExpandedTradeIdx] = useState(null);
  const [historyData, setHistoryData] = useState([]);
  const [showAllLogs, setShowAllLogs] = useState(false);

  const LOGS_PREVIEW_COUNT = 5;

  const totalValue = portfolio?.total_value ?? 0;
  const initialBalance = portfolio?.initial_balance ?? 100000;
  const pnl = totalValue - initialBalance;
  const pnlPct = initialBalance > 0 ? (pnl / initialBalance) * 100 : 0;
  const openPositions = positions?.length ?? 0;

  useEffect(() => {
    // Per intraday chiediamo "1d" al backend (~1440 punti) e filtriamo lato client
    const apiPeriod = INTRADAY_PERIODS.has(period) ? "1d" : period;
    fetch(`${API}/api/portfolio/history?period=${apiPeriod}`)
      .then((res) => res.json())
      .then((data) => {
        let arr = Array.isArray(data) ? data : [];
        if (INTRADAY_PERIODS.has(period)) {
          const hrs = INTRADAY_HOURS[period];
          const cutoff = Date.now() - hrs * 3600 * 1000;
          arr = arr.filter((p) => new Date(p.timestamp).getTime() >= cutoff);
        }
        // Filtra outlier (step-jump anomali da snapshot corrotti)
        arr = sanitizeOutliers(arr);
        setHistoryData(arr);
      })
      .catch(() => setHistoryData([]));
  }, [period]);

  const lastValue = historyData.length > 0 ? historyData[historyData.length - 1]?.total_value : null;
  const firstValue = historyData.length > 0 ? historyData[0]?.total_value : null;
  // Colore linea: verde se il valore corrente e' >= primo valore visibile (delta del periodo)
  const lineColor = lastValue !== null && firstValue !== null && lastValue >= firstValue ? "#10b981" : "#ef4444";

  // Auto-scale Y-axis sullo span effettivo dei dati visibili (stile Scalable Capital):
  // dataMin/dataMax con padding del 5% del range, cosi' i movimenti piccoli sono visibili.
  const yDomain = (() => {
    if (historyData.length === 0) return ["auto", "auto"];
    const vals = historyData.map((p) => Number(p.total_value)).filter((v) => !isNaN(v));
    if (vals.length === 0) return ["auto", "auto"];
    const min = Math.min(...vals);
    const max = Math.max(...vals);
    const range = max - min;
    // Se la variazione e' minima (< 0.01% del valore medio), forza un padding minimo
    // per evitare la "linea piatta" che non si nota
    const avg = (max + min) / 2;
    const minPadding = avg * 0.001;  // 0.1% del valore medio
    const padding = Math.max(range * 0.15, minPadding);
    return [min - padding, max + padding];
  })();

  // Delta del periodo visibile per il pannello sopra il grafico
  const periodDelta = lastValue !== null && firstValue !== null ? lastValue - firstValue : 0;
  const periodDeltaPct = firstValue ? (periodDelta / firstValue) * 100 : 0;

  const formatXAxis = (tick) => {
    if (!tick) return "";
    const d = new Date(tick);
    if (isNaN(d)) return tick;
    if (INTRADAY_PERIODS.has(period)) {
      return d.toLocaleTimeString("it-IT", { hour: "2-digit", minute: "2-digit" });
    }
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
            {formatUSD(payload[0].value)}
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
          <div className="metric-value">{formatUSD(totalValue)}</div>
        </div>

        <div className="metric-card">
          <div className="metric-label">P&amp;L Totale</div>
          <div
            className="metric-value"
            style={{ color: pnl >= 0 ? "#10b981" : "#ef4444" }}
          >
            {pnl >= 0 ? "+" : ""}
            {formatUSD(pnl)}
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
          <div style={{display:"flex",alignItems:"baseline",gap:"1rem",flexWrap:"wrap"}}>
            <h2 className="card-title" style={{margin:0}}>Equity Curve</h2>
            {historyData.length > 0 && (
              <span style={{
                fontSize:"0.95rem",
                fontWeight:600,
                color: periodDelta >= 0 ? "#10b981" : "#ef4444",
              }}>
                {periodDelta >= 0 ? "+" : ""}{formatUSD(periodDelta)} ({periodDelta >= 0 ? "+" : ""}{periodDeltaPct.toFixed(2)}%)
                <span style={{color:"#6b7280",fontWeight:400,marginLeft:"0.4rem"}}>
                  nel periodo
                </span>
              </span>
            )}
          </div>
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
          <div className="no-data">Nessun dato disponibile in questo periodo</div>
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
                minTickGap={30}
              />
              <YAxis
                tickFormatter={(v) => formatUSD(v)}
                tick={{ fill: "#6b7280", fontSize: 11 }}
                axisLine={{ stroke: "#374151" }}
                tickLine={false}
                width={110}
                domain={yDomain}
                allowDataOverflow={false}
              />
              <Tooltip content={<CustomTooltip />} />
              <Line
                type="monotone"
                dataKey="total_value"
                stroke={lineColor}
                strokeWidth={2}
                dot={false}
                activeDot={{ r: 4, fill: lineColor }}
                isAnimationActive={false}
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
                          <td>{formatUSD(trade.price)}</td>
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
                    <td>{formatUSD(pos.avg_buy_price)}</td>
                    <td>{formatUSD(pos.current_price)}</td>
                    <td style={{ color: pnlColor, fontWeight: 500 }}>
                      {pnlPos >= 0 ? "+" : ""}
                      {formatUSD(pnlPos)}
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

      {/* Attività Agenti (cards strutturate) */}
      <div className="card">
        <h2 className="card-title">Attività Agenti</h2>
        <AgentActivityCards />
      </div>
    </div>
  );
}
