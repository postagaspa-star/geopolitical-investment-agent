import React, { useState } from "react";

// Formattazione valuta EUR
const fmtEur = (val) => {
  if (val == null) return "-";
  return new Intl.NumberFormat("it-IT", {
    style: "currency",
    currency: "EUR",
    minimumFractionDigits: 2,
  }).format(val);
};

// Formattazione data compatta
const fmtDate = (ts) => {
  if (!ts) return "-";
  return new Date(ts).toLocaleDateString("it-IT", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
};

function TradeHistory({ trades }) {
  // Riga espansa per mostrare il reasoning
  const [expandedRow, setExpandedRow] = useState(null);

  const toggleRow = (idx) => {
    setExpandedRow(expandedRow === idx ? null : idx);
  };

  if (!trades || trades.length === 0) {
    return (
      <div className="card">
        <div className="card-title">Storico Operazioni</div>
        <div className="empty-state">Nessuna operazione registrata</div>
      </div>
    );
  }

  // Ordina per data decrescente (operazioni piu recenti in alto)
  const sorted = [...trades].sort(
    (a, b) => new Date(b.timestamp) - new Date(a.timestamp)
  );

  return (
    <div className="card">
      <div className="card-title">Storico Operazioni</div>
      <div className="scrollable">
        <table className="data-table">
          <thead>
            <tr>
              <th>Data</th>
              <th>Ticker</th>
              <th>Azione</th>
              <th>Qty</th>
              <th>Prezzo</th>
              <th>Totale</th>
              <th>Confidenza</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((trade, idx) => {
              const total = (trade.price || 0) * (trade.quantity || 0);
              const actionClass =
                trade.action === "BUY"
                  ? "badge-buy"
                  : trade.action === "SELL"
                  ? "badge-sell"
                  : "badge-hold";
              const isExpanded = expandedRow === idx;
              const hasReasoning =
                trade.geopolitical_reasoning || trade.technical_reasoning;

              return (
                <React.Fragment key={idx}>
                  <tr
                    className={hasReasoning ? "expandable" : ""}
                    onClick={() => hasReasoning && toggleRow(idx)}
                    title={hasReasoning ? "Clicca per espandere il reasoning" : ""}
                  >
                    <td>{fmtDate(trade.timestamp)}</td>
                    <td style={{ fontWeight: 600 }}>{trade.ticker || "-"}</td>
                    <td>
                      <span className={`badge ${actionClass}`}>
                        {trade.action || "-"}
                      </span>
                    </td>
                    <td>{trade.quantity ?? "-"}</td>
                    <td>{fmtEur(trade.price)}</td>
                    <td>{fmtEur(total)}</td>
                    <td>
                      {trade.confidence != null
                        ? `${(trade.confidence * 100).toFixed(0)}%`
                        : "-"}
                    </td>
                  </tr>
                  {/* Riga espansa con le motivazioni */}
                  {isExpanded && (
                    <tr className="expanded-row">
                      <td colSpan={7}>
                        {trade.geopolitical_reasoning && (
                          <div style={{ marginBottom: "0.5rem" }}>
                            <strong style={{ color: "#448aff" }}>
                              Analisi Geopolitica:
                            </strong>{" "}
                            {trade.geopolitical_reasoning}
                          </div>
                        )}
                        {trade.technical_reasoning && (
                          <div>
                            <strong style={{ color: "#b388ff" }}>
                              Analisi Tecnica:
                            </strong>{" "}
                            {trade.technical_reasoning}
                          </div>
                        )}
                      </td>
                    </tr>
                  )}
                </React.Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default TradeHistory;
