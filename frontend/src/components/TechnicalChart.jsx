import React, { useMemo } from "react";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
  Cell,
} from "recharts";

// Formattazione data compatta
const fmtDate = (ts) => {
  if (!ts) return "-";
  return new Date(ts).toLocaleDateString("it-IT", {
    day: "2-digit",
    month: "short",
  });
};

// Tooltip personalizzato per il grafico confidenza
const ConfidenceTooltip = ({ active, payload }) => {
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
      <div style={{ color: "#8a8f98" }}>
        {d.ticker} - {d.action}
      </div>
      <div style={{ color: "#e0e0e0", fontWeight: 700 }}>
        Confidenza: {(d.confidence * 100).toFixed(0)}%
      </div>
      <div style={{ color: "#5a5f6a", fontSize: "0.7rem" }}>{d.date}</div>
    </div>
  );
};

function TechnicalChart({ positions, trades }) {
  // Prepara dati confidenza dalle operazioni
  const confidenceData = useMemo(() => {
    if (!trades || trades.length === 0) return [];

    return trades
      .filter((t) => t.confidence != null)
      .sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp))
      .slice(-20) // Ultime 20 operazioni
      .map((t) => ({
        date: fmtDate(t.timestamp),
        ticker: t.ticker || "?",
        action: t.action || "?",
        confidence: t.confidence,
        fill: t.action === "BUY" ? "#00c853" : "#ff1744",
      }));
  }, [trades]);

  return (
    <div className="card">
      <div className="card-title">Analisi Tecnica</div>

      {/* Tabella posizioni aperte con stato indicatori */}
      {positions && positions.length > 0 && (
        <>
          <div
            style={{
              fontSize: "0.75rem",
              color: "#8a8f98",
              marginBottom: "0.5rem",
              fontWeight: 600,
            }}
          >
            POSIZIONI APERTE
          </div>
          <div className="scrollable" style={{ maxHeight: 180, marginBottom: "1rem" }}>
            <table className="data-table">
              <thead>
                <tr>
                  <th>Ticker</th>
                  <th>Qty</th>
                  <th>Prezzo Medio</th>
                  <th>Valore</th>
                  <th>Segnale</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((pos, idx) => {
                  // Determina il segnale tecnico in base ai dati disponibili
                  const signal = pos.signal || pos.technical_signal || "-";
                  const signalClass =
                    signal === "BUY" || signal === "BULLISH"
                      ? "positive"
                      : signal === "SELL" || signal === "BEARISH"
                      ? "negative"
                      : "";

                  return (
                    <tr key={idx}>
                      <td style={{ fontWeight: 600 }}>{pos.ticker || "-"}</td>
                      <td>{pos.quantity ?? "-"}</td>
                      <td>
                        {pos.avg_price != null
                          ? `${pos.avg_price.toFixed(2)}`
                          : "-"}
                      </td>
                      <td>
                        {pos.market_value != null
                          ? `${pos.market_value.toFixed(2)}`
                          : pos.avg_price && pos.quantity
                          ? `${(pos.avg_price * pos.quantity).toFixed(2)}`
                          : "-"}
                      </td>
                      <td className={signalClass} style={{ fontWeight: 600 }}>
                        {signal}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}

      {/* Grafico confidenza operazioni */}
      {confidenceData.length > 0 ? (
        <>
          <div
            style={{
              fontSize: "0.75rem",
              color: "#8a8f98",
              marginBottom: "0.5rem",
              fontWeight: 600,
            }}
          >
            CONFIDENZA OPERAZIONI
          </div>
          <div style={{ width: "100%", height: 180 }}>
            <ResponsiveContainer>
              <BarChart data={confidenceData}>
                <CartesianGrid strokeDasharray="3 3" stroke="#2a2d35" />
                <XAxis
                  dataKey="date"
                  tick={{ fill: "#5a5f6a", fontSize: 10 }}
                  axisLine={{ stroke: "#2a2d35" }}
                  tickLine={false}
                />
                <YAxis
                  domain={[0, 1]}
                  tick={{ fill: "#5a5f6a", fontSize: 10 }}
                  axisLine={{ stroke: "#2a2d35" }}
                  tickLine={false}
                  tickFormatter={(v) => `${(v * 100).toFixed(0)}%`}
                  width={40}
                />
                <Tooltip content={<ConfidenceTooltip />} />
                <Bar dataKey="confidence" radius={[3, 3, 0, 0]}>
                  {confidenceData.map((entry, idx) => (
                    <Cell key={idx} fill={entry.fill} fillOpacity={0.7} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </>
      ) : (
        !positions ||
        (positions.length === 0 && (
          <div className="empty-state">
            Nessun dato tecnico disponibile
          </div>
        ))
      )}
    </div>
  );
}

export default TechnicalChart;
