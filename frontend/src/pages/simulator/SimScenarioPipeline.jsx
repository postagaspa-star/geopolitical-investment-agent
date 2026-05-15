import { useEffect, useState, useCallback } from "react";
import {
  RefreshCw, CheckCircle2, AlertTriangle, XCircle, HelpCircle,
  Clock, Package, TrendingUp, Server, ShieldAlert, Inbox,
} from "lucide-react";

const API = window.location.origin;

/**
 * SimScenarioPipeline — diagnostica del generator scenari (GitHub Actions
 * 3x/giorno -> POST /api/simulator/scenarios/dynamic).
 *
 * Mostra:
 *  - Health badge (ok/stale/error/never) derivato dall'ultimo upload OK
 *  - Summary: scenari attivi, aggiunti 24h/7d, breakdown categorie,
 *    ultimo upload riuscito, ultimo errore
 *  - Pipeline log: ultimi eventi (upload/auth_failed/server_misconfig/...)
 *    con dettaglio errore espandibile
 *  - Scenari attivi: id/categoria/titolo/quando creato/scadenza
 *
 * Serve a capire SE il sistema funziona e, se no, PERCHE' (es. token
 * GitHub != token Render -> auth_failed; o nessun upload mai -> il
 * workflow GitHub non parte affatto).
 */
async function safeFetchJson(url, timeoutMs = 30000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, { signal: ctrl.signal });
    const text = await res.text();
    if (!text || !text.trim()) {
      return { ok: false, error: `Risposta vuota (HTTP ${res.status})` };
    }
    let data;
    try {
      data = JSON.parse(text);
    } catch {
      return { ok: false, error: `Risposta non JSON (HTTP ${res.status})` };
    }
    return { ok: res.ok, status: res.status, data };
  } catch (e) {
    return { ok: false, error: e.name === "AbortError" ? "Timeout richiesta" : String(e.message || e) };
  } finally {
    clearTimeout(timer);
  }
}

function fmtDateTime(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleString("it-IT", {
      day: "2-digit", month: "2-digit", year: "numeric",
      hour: "2-digit", minute: "2-digit",
    });
  } catch {
    return String(iso);
  }
}

function timeAgo(iso) {
  if (!iso) return "mai";
  const diff = Date.now() - new Date(iso).getTime();
  if (Number.isNaN(diff)) return "—";
  const m = Math.floor(diff / 60000);
  if (m < 1) return "ora";
  if (m < 60) return `${m} min fa`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h fa`;
  return `${Math.floor(h / 24)}g fa`;
}

const HEALTH_CONFIG = {
  ok: {
    color: "#10b981", icon: CheckCircle2, label: "OPERATIVA",
    desc: "Upload riuscito nelle ultime 26h. La pipeline sta funzionando.",
  },
  stale: {
    color: "#f59e0b", icon: AlertTriangle, label: "IN RITARDO",
    desc: "Ultimo upload riuscito 26-50h fa. Uno o piu' run schedulati sono saltati.",
  },
  error: {
    color: "#ef4444", icon: XCircle, label: "ERRORE",
    desc: "Nessun upload riuscito da oltre 50h, ma ci sono tentativi falliti. Vedi log sotto.",
  },
  never: {
    color: "#64748b", icon: HelpCircle, label: "MAI ESEGUITA",
    desc: "Il backend non ha MAI ricevuto un upload. Probabile: workflow GitHub non parte (secrets mancanti) o non ha mai raggiunto il backend.",
  },
};

const EVENT_CONFIG = {
  upload: { icon: Package, color: "#10b981", label: "Upload" },
  auth_failed: { icon: ShieldAlert, color: "#ef4444", label: "Auth fallita" },
  server_misconfig: { icon: Server, color: "#ef4444", label: "Config server" },
  empty_batch: { icon: Inbox, color: "#f59e0b", label: "Batch vuoto" },
};

export default function SimScenarioPipeline() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [expandedIdx, setExpandedIdx] = useState(null);
  const [lastRefresh, setLastRefresh] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    const r = await safeFetchJson(`${API}/api/simulator/scenarios/pipeline-status`);
    if (!r.ok) {
      setError(r.error || (r.data && r.data.error) || "Errore caricamento");
    } else {
      setError(null);
      setData(r.data);
      setLastRefresh(new Date());
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000); // auto-refresh 30s
    return () => clearInterval(id);
  }, [load]);

  const health = data?.health || "never";
  const hc = HEALTH_CONFIG[health] || HEALTH_CONFIG.never;
  const HIcon = hc.icon;
  const summary = data?.summary || {};
  const log = data?.pipeline_log || [];
  const scenarios = data?.active_scenarios || [];

  return (
    <div style={S.page}>
      <div style={S.header}>
        <div>
          <h1 style={S.title}>Pipeline Scenari</h1>
          <p style={S.subtitle}>
            Diagnostica del generator automatico (GitHub Actions 3×/giorno → backend)
          </p>
        </div>
        <button style={S.refreshBtn} onClick={load} disabled={loading}>
          <RefreshCw size={14} style={loading ? { animation: "spin 1s linear infinite" } : {}} />
          {loading ? "Aggiorno…" : "Aggiorna"}
        </button>
      </div>
      <style>{`@keyframes spin { from {transform:rotate(0)} to {transform:rotate(360deg)} }`}</style>

      {error && (
        <div style={S.errorBox}>
          <AlertTriangle size={16} /> {error}
        </div>
      )}

      {/* HEALTH BADGE */}
      <div style={{ ...S.healthCard, borderColor: `${hc.color}55` }}>
        <div style={{ ...S.healthIconWrap, background: `${hc.color}1a` }}>
          <HIcon size={32} style={{ color: hc.color }} />
        </div>
        <div style={{ flex: 1 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <span style={{ ...S.healthLabel, color: hc.color }}>{hc.label}</span>
            {lastRefresh && (
              <span style={S.refreshNote}>
                aggiornato {lastRefresh.toLocaleTimeString("it-IT")}
              </span>
            )}
          </div>
          <div style={S.healthDesc}>{hc.desc}</div>
        </div>
      </div>

      {/* SUMMARY GRID */}
      <div style={S.statsGrid}>
        <StatCard
          icon={Package} color="#a78bfa"
          value={summary.active_total ?? "—"}
          label="Scenari attivi"
          sub="dinamici non scaduti"
        />
        <StatCard
          icon={TrendingUp} color="#10b981"
          value={summary.added_last_24h ?? "—"}
          label="Aggiunti 24h"
          sub={`target ~${summary.expected_per_day ?? 15}/giorno`}
          warn={(summary.added_last_24h ?? 0) < 2}
        />
        <StatCard
          icon={TrendingUp} color="#38bdf8"
          value={summary.added_last_7d ?? "—"}
          label="Aggiunti 7gg"
          sub="ultima settimana"
        />
        <StatCard
          icon={Clock} color="#f59e0b"
          value={timeAgo(summary.last_successful_upload_at)}
          label="Ultimo upload OK"
          sub={fmtDateTime(summary.last_successful_upload_at)}
        />
      </div>

      {/* ULTIMO ERRORE (se presente) */}
      {summary.last_error && (
        <div style={S.lastErrorBox}>
          <div style={S.lastErrorHead}>
            <XCircle size={16} style={{ color: "#ef4444" }} />
            <strong>Ultimo errore registrato</strong>
            <span style={S.lastErrorTs}>{fmtDateTime(summary.last_error.ts)}</span>
          </div>
          <div style={S.lastErrorDetail}>
            <code style={S.eventTag}>{summary.last_error.event}</code>
            {summary.last_error.detail}
          </div>
        </div>
      )}

      {/* CATEGORIE BREAKDOWN */}
      {summary.categories && Object.keys(summary.categories).length > 0 && (
        <div style={S.section}>
          <h2 style={S.sectionTitle}>Scenari attivi per categoria</h2>
          <div style={S.catRow}>
            {Object.entries(summary.categories).map(([cat, n]) => (
              <div key={cat} style={S.catPill}>
                <span style={S.catName}>{cat}</span>
                <span style={S.catCount}>{n}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* PIPELINE LOG */}
      <div style={S.section}>
        <h2 style={S.sectionTitle}>
          Log eventi pipeline <span style={S.dim}>({log.length})</span>
        </h2>
        {log.length === 0 ? (
          <div style={S.empty}>
            Nessun evento registrato. Il backend non ha mai ricevuto una
            chiamata dal generator. Verifica che il workflow GitHub Actions
            "Scenario Generator (3x/day)" sia attivo e che i secrets
            (SCENARIO_UPLOAD_TOKEN, GEOINVEST_API_BASE_URL) siano configurati.
          </div>
        ) : (
          <div style={S.logList}>
            {log.map((ev, i) => {
              const ec = EVENT_CONFIG[ev.event] || { icon: HelpCircle, color: "#64748b", label: ev.event };
              const EIcon = ec.icon;
              const isErr = ev.status === "error";
              const isWarn = ev.status === "warning";
              const tone = isErr ? "#ef4444" : isWarn ? "#f59e0b" : "#10b981";
              const hasDetail = ev.detail || (ev.rejected_reasons && ev.rejected_reasons.length);
              const expanded = expandedIdx === i;
              return (
                <div key={i} style={{ ...S.logItem, borderLeftColor: tone }}>
                  <div
                    style={S.logMain}
                    onClick={() => hasDetail && setExpandedIdx(expanded ? null : i)}
                  >
                    <EIcon size={15} style={{ color: ec.color, flexShrink: 0 }} />
                    <span style={{ ...S.logEvent, color: ec.color }}>{ec.label}</span>
                    <span style={{ ...S.logStatus, background: `${tone}22`, color: tone }}>
                      {ev.status}
                    </span>
                    {ev.event === "upload" && (
                      <span style={S.logCounts}>
                        ✓ {ev.accepted ?? 0} accettati
                        {ev.rejected ? ` · ✗ ${ev.rejected} rifiutati` : ""}
                      </span>
                    )}
                    <span style={S.logTs}>{fmtDateTime(ev.ts)}</span>
                  </div>
                  {ev.detail && (
                    <div style={S.logDetail}>{ev.detail}</div>
                  )}
                  {expanded && ev.rejected_reasons && ev.rejected_reasons.length > 0 && (
                    <div style={S.rejectedBox}>
                      <strong style={{ fontSize: 11, color: "#94a3b8" }}>
                        Scenari rifiutati:
                      </strong>
                      {ev.rejected_reasons.map((r, ri) => (
                        <div key={ri} style={S.rejectedRow}>
                          <code style={S.rejectedId}>{r.id || "?"}</code>
                          <span>{r.reason}</span>
                        </div>
                      ))}
                    </div>
                  )}
                  {expanded && ev.accepted_ids && ev.accepted_ids.length > 0 && (
                    <div style={S.acceptedBox}>
                      <strong style={{ fontSize: 11, color: "#64748b" }}>
                        ID accettati:
                      </strong>{" "}
                      {ev.accepted_ids.join(", ")}
                    </div>
                  )}
                  {hasDetail && ev.rejected_reasons && ev.rejected_reasons.length > 0 && (
                    <button
                      style={S.expandBtn}
                      onClick={() => setExpandedIdx(expanded ? null : i)}
                    >
                      {expanded ? "− nascondi dettaglio" : "+ mostra dettaglio"}
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* SCENARI ATTIVI */}
      <div style={S.section}>
        <h2 style={S.sectionTitle}>
          Scenari dinamici attivi <span style={S.dim}>({scenarios.length})</span>
        </h2>
        {scenarios.length === 0 ? (
          <div style={S.empty}>
            Nessuno scenario dinamico attivo. Il Simulator sta usando solo
            gli scenari statici hardcoded.
          </div>
        ) : (
          <div style={S.tableWrap}>
            <table style={S.table}>
              <thead>
                <tr>
                  <th style={S.th}>ID</th>
                  <th style={S.th}>Categoria</th>
                  <th style={S.th}>Titolo</th>
                  <th style={S.th}>Creato</th>
                  <th style={S.th}>Scade</th>
                </tr>
              </thead>
              <tbody>
                {scenarios.map((s, i) => (
                  <tr key={s.id || i} style={i % 2 ? S.trOdd : S.trEven}>
                    <td style={{ ...S.td, fontFamily: "ui-monospace, monospace", fontSize: 11 }}>
                      {s.id}
                    </td>
                    <td style={S.td}>
                      <span style={S.catBadge}>{s.category}</span>
                    </td>
                    <td style={{ ...S.td, color: "#cbd5e1" }}>{s.title}</td>
                    <td style={S.td}>
                      <span title={fmtDateTime(s.uploaded_at)}>
                        {timeAgo(s.uploaded_at)}
                      </span>
                    </td>
                    <td style={{ ...S.td, color: "#64748b" }}>
                      {fmtDateTime(s.expires_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

function StatCard({ icon: Icon, color, value, label, sub, warn }) {
  return (
    <div style={{ ...S.statCard, ...(warn ? { borderColor: "#f59e0b66" } : {}) }}>
      <div style={{ ...S.statIcon, background: `${color}1a` }}>
        <Icon size={18} style={{ color }} />
      </div>
      <div style={S.statValue}>{value}</div>
      <div style={S.statLabel}>{label}</div>
      {sub && <div style={S.statSub}>{sub}</div>}
    </div>
  );
}

const S = {
  page: { maxWidth: 1100, margin: "0 auto" },
  header: { display: "flex", justifyContent: "space-between", alignItems: "flex-start",
            marginBottom: 24, gap: 16, flexWrap: "wrap" },
  title: { fontSize: "1.6rem", fontWeight: 700, margin: 0, color: "#f1f5f9" },
  subtitle: { fontSize: 13, color: "#64748b", margin: "4px 0 0" },
  refreshBtn: { display: "inline-flex", alignItems: "center", gap: 6,
                background: "#1e293b", border: "1px solid #334155", color: "#cbd5e1",
                padding: "8px 14px", borderRadius: 7, cursor: "pointer", fontSize: 13 },
  errorBox: { display: "flex", alignItems: "center", gap: 8, background: "#1f0e0e",
              border: "1px solid #ef444455", color: "#fca5a5", padding: "12px 16px",
              borderRadius: 8, marginBottom: 16, fontSize: 13 },

  healthCard: { display: "flex", alignItems: "center", gap: 18, background: "#111827",
                border: "1px solid", padding: "20px 24px", borderRadius: 12,
                marginBottom: 20 },
  healthIconWrap: { width: 60, height: 60, borderRadius: 12, display: "flex",
                    alignItems: "center", justifyContent: "center", flexShrink: 0 },
  healthLabel: { fontSize: "1.3rem", fontWeight: 800, letterSpacing: "0.04em" },
  healthDesc: { fontSize: 13, color: "#94a3b8", marginTop: 6, lineHeight: 1.5 },
  refreshNote: { fontSize: 11, color: "#64748b" },

  statsGrid: { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))",
               gap: 14, marginBottom: 20 },
  statCard: { background: "#111827", border: "1px solid #1f2937", borderRadius: 10,
              padding: "16px 18px" },
  statIcon: { width: 34, height: 34, borderRadius: 8, display: "flex",
              alignItems: "center", justifyContent: "center", marginBottom: 10 },
  statValue: { fontSize: "1.5rem", fontWeight: 700, color: "#f1f5f9", lineHeight: 1.1 },
  statLabel: { fontSize: 13, color: "#cbd5e1", marginTop: 4, fontWeight: 600 },
  statSub: { fontSize: 11, color: "#64748b", marginTop: 2 },

  lastErrorBox: { background: "#1a0f0f", border: "1px solid #ef444444",
                  borderRadius: 10, padding: "14px 18px", marginBottom: 20 },
  lastErrorHead: { display: "flex", alignItems: "center", gap: 8, fontSize: 13,
                   color: "#fca5a5" },
  lastErrorTs: { marginLeft: "auto", fontSize: 11, color: "#64748b" },
  lastErrorDetail: { fontSize: 13, color: "#e2e8f0", marginTop: 8, lineHeight: 1.5 },

  section: { marginBottom: 24 },
  sectionTitle: { fontSize: "1.05rem", fontWeight: 700, color: "#e2e8f0",
                  marginBottom: 12 },
  dim: { color: "#64748b", fontWeight: 400, fontSize: "0.85em" },

  catRow: { display: "flex", flexWrap: "wrap", gap: 8 },
  catPill: { display: "flex", alignItems: "center", gap: 8, background: "#1e293b",
             border: "1px solid #334155", borderRadius: 999, padding: "5px 12px" },
  catName: { fontSize: 12, color: "#cbd5e1" },
  catCount: { fontSize: 12, fontWeight: 700, color: "#a78bfa",
              background: "#0f172a", borderRadius: 999, padding: "1px 8px" },

  logList: { display: "flex", flexDirection: "column", gap: 8 },
  logItem: { background: "#111827", border: "1px solid #1f2937",
             borderLeft: "3px solid", borderRadius: 8, padding: "10px 14px" },
  logMain: { display: "flex", alignItems: "center", gap: 10, cursor: "default",
             flexWrap: "wrap" },
  logEvent: { fontSize: 13, fontWeight: 600 },
  logStatus: { fontSize: 10, fontWeight: 700, textTransform: "uppercase",
               padding: "2px 8px", borderRadius: 4, letterSpacing: "0.04em" },
  logCounts: { fontSize: 12, color: "#94a3b8" },
  logTs: { marginLeft: "auto", fontSize: 11, color: "#64748b",
           fontFamily: "ui-monospace, monospace" },
  logDetail: { fontSize: 12, color: "#94a3b8", marginTop: 6, lineHeight: 1.5 },
  rejectedBox: { marginTop: 8, padding: "8px 10px", background: "#0f172a",
                 borderRadius: 6, display: "flex", flexDirection: "column", gap: 4 },
  rejectedRow: { display: "flex", gap: 8, fontSize: 12, color: "#cbd5e1" },
  rejectedId: { fontFamily: "ui-monospace, monospace", fontSize: 11,
                color: "#f59e0b" },
  acceptedBox: { marginTop: 8, padding: "8px 10px", background: "#0f172a",
                 borderRadius: 6, fontSize: 11, color: "#94a3b8",
                 fontFamily: "ui-monospace, monospace", wordBreak: "break-all" },
  expandBtn: { background: "transparent", border: "none", color: "#64748b",
               fontSize: 11, cursor: "pointer", marginTop: 6, padding: 0 },
  eventTag: { fontFamily: "ui-monospace, monospace", fontSize: 11,
              background: "#0f172a", color: "#f59e0b", padding: "1px 6px",
              borderRadius: 4, marginRight: 8 },

  tableWrap: { border: "1px solid #1f2937", borderRadius: 8, overflow: "hidden" },
  table: { width: "100%", borderCollapse: "collapse", fontSize: 13 },
  th: { padding: "8px 12px", textAlign: "left", fontSize: 10, fontWeight: 600,
        textTransform: "uppercase", letterSpacing: "0.06em", color: "#64748b",
        background: "#0f172a", borderBottom: "1px solid #1f2937" },
  td: { padding: "8px 12px", color: "#e2e8f0", borderBottom: "1px solid #1a2030" },
  trEven: { background: "transparent" },
  trOdd: { background: "rgba(15,23,42,0.4)" },
  catBadge: { fontSize: 11, fontWeight: 600, color: "#a78bfa",
              background: "#1e1b3a", border: "1px solid #4c1d95", borderRadius: 4,
              padding: "2px 8px" },

  empty: { color: "#64748b", padding: "20px 24px", textAlign: "center", fontSize: 13,
           background: "#0f172a", borderRadius: 8, border: "1px dashed #1f2937",
           lineHeight: 1.6 },
};
