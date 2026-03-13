import React, { useMemo } from "react";

// Formattazione data/ora per il feed
const fmtTime = (ts) => {
  if (!ts) return "";
  const d = new Date(ts);
  return d.toLocaleString("it-IT", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
};

function GeopoliticalFeed({ logs }) {
  // Estrae gli eventi geopolitici dai log
  // Cerca log con fase GEOPOLITICAL o che contengono dati di snapshot
  const feedItems = useMemo(() => {
    if (!logs || logs.length === 0) return [];

    return logs
      .filter((l) => {
        const phase = (l.phase || l.level || "").toUpperCase();
        return (
          phase === "GEOPOLITICAL" ||
          l.source === "GDELT" ||
          l.source === "NEWSAPI" ||
          l.type === "geopolitical_event"
        );
      })
      .map((l, idx) => ({
        id: idx,
        source: (l.source || "GDELT").toUpperCase(),
        timestamp: l.timestamp,
        summary:
          l.summary ||
          l.message ||
          l.content ||
          l.title ||
          JSON.stringify(l),
        impact: l.impact || null,
        regions: l.regions || [],
      }))
      .reverse() // Piu recenti in alto
      .slice(0, 50);
  }, [logs]);

  return (
    <div className="card">
      <div className="card-title">
        Feed Geopolitico
        <span
          style={{
            float: "right",
            fontSize: "0.7rem",
            color: "#5a5f6a",
            fontWeight: 400,
            textTransform: "none",
          }}
        >
          {feedItems.length} eventi
        </span>
      </div>
      <div className="scrollable" style={{ maxHeight: 380 }}>
        {feedItems.length === 0 ? (
          <div className="empty-state">
            Nessun evento geopolitico recente
          </div>
        ) : (
          feedItems.map((item) => {
            const sourceClass =
              item.source === "GDELT" ? "badge-gdelt" : "badge-newsapi";

            return (
              <div className="feed-item" key={item.id}>
                <div className="feed-meta">
                  <span className={`badge-source ${sourceClass}`}>
                    {item.source}
                  </span>
                  <span className="feed-time">{fmtTime(item.timestamp)}</span>
                  {item.impact && (
                    <span
                      className={`badge ${
                        item.impact === "high"
                          ? "badge-sell"
                          : item.impact === "medium"
                          ? "badge-hold"
                          : "badge-buy"
                      }`}
                    >
                      {item.impact}
                    </span>
                  )}
                </div>
                <div className="feed-text">{item.summary}</div>
                {item.regions && item.regions.length > 0 && (
                  <div
                    style={{
                      marginTop: "0.25rem",
                      fontSize: "0.7rem",
                      color: "#5a5f6a",
                    }}
                  >
                    Regioni: {item.regions.join(", ")}
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

export default GeopoliticalFeed;
