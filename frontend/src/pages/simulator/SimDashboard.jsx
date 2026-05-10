import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Play, Zap, RefreshCw, Activity, BookOpen, Cpu, Bot,
         Bitcoin, TrendingUp, AlertCircle, CheckCircle2, Loader2,
         Download } from "lucide-react";

const API = window.location.origin;

/**
 * Home Simulator: KPI aggregati + ultimi run + toggle modalità automatica
 * + Win Rate per Scenario + Sentiment Drift (top advice & Sharpe rolling).
 */
export default function SimDashboard() {
  const nav = useNavigate();
  const [kpi, setKpi] = useState(null);
  const [recent, setRecent] = useState([]);
  const [autoMode, setAutoMode] = useState({ enabled: false, daily_cap: 5, runs_today: 0 });
  const [winByScenario, setWinByScenario] = useState(null);
  const [drift, setDrift] = useState(null);
  const [activeRuns, setActiveRuns] = useState([]);
  const [loading, setLoading] = useState(true);

  // ── PDF export ─────────────────────────────────────────────────
  const [pdfBusy, setPdfBusy] = useState(false);
  // pdfAllRuns viene popolato solo durante la generazione PDF e contiene
  // TUTTI i run (non solo gli ultimi 10) per la tabella riassuntiva
  // off-screen che entra nel report.
  const [pdfAllRuns, setPdfAllRuns] = useState(null);

  const handleDownloadGlobalPDF = async () => {
    if (pdfBusy) return;
    setPdfBusy(true);
    try {
      // Fetch TUTTI i dati aggregati + l'elenco completo dei run
      const [kFresh, allRuns, wFresh, dFresh] = await Promise.all([
        fetch(`${API}/api/simulator/kpi`).then(r => r.ok ? r.json() : null),
        fetch(`${API}/api/simulator/runs?limit=500`).then(r => r.ok ? r.json() : []),
        fetch(`${API}/api/simulator/win-by-scenario`).then(r => r.ok ? r.json() : null),
        fetch(`${API}/api/simulator/sentiment-drift`).then(r => r.ok ? r.json() : null),
      ]);

      // Aggiorna anche le card visibili con i dati appena fetchati
      // (l'utente vede la dashboard "fresca" dopo la generazione)
      if (kFresh) setKpi(kFresh);
      if (wFresh) setWinByScenario(wFresh);
      if (dFresh) setDrift(dFresh);

      // Renderizza la tabella full-runs off-screen
      const runs = Array.isArray(allRuns) ? allRuns : [];
      setPdfAllRuns(runs);

      // Aspetta 2 frame perché React renderizzi la tabella off-screen
      await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
      await new Promise(r => setTimeout(r, 60));

      const { generatePDFFromSections } = await import("../../utils/pdfExport");
      const dates = runs
        .map(r => r.completed_at)
        .filter(Boolean)
        .sort();
      const periodStr = dates.length >= 2
        ? `${new Date(dates[0]).toLocaleDateString("it-IT")} → ${new Date(dates[dates.length - 1]).toLocaleDateString("it-IT")}`
        : dates.length === 1
          ? new Date(dates[0]).toLocaleDateString("it-IT")
          : "—";

      const meta = {
        title: "Simulator — Report Globale",
        subtitle: `Riepilogo aggregato di tutte le ${runs.length} simulazioni completate`,
        runId: "global-summary",
        category: "Tutte le categorie",
        scenarioType: `${kFresh?.single_step ?? 0} single-step · ${kFresh?.multi_step ?? 0} multi-step`,
        action: kFresh
          ? `Win rate ${(kFresh.win_rate * 100).toFixed(0)}% · Score ${(kFresh.score ?? 0).toFixed(1)}`
          : "—",
        asset: "—",
        outcome: kFresh?.score_label ?? "—",
        period: periodStr,
        generatedAt: new Date().toLocaleString("it-IT", {
          dateStyle: "medium", timeStyle: "short",
        }),
      };
      const filename = `simulator-global-report-${new Date().toISOString().slice(0, 10)}.pdf`;

      await generatePDFFromSections({
        rootId: "sim-dashboard-pdf",
        filename,
        meta,
      });
    } catch (e) {
      console.error("[PDF] global report failed:", e);
      alert("Errore generazione PDF: " + (e.message || e));
    } finally {
      setPdfAllRuns(null);
      setPdfBusy(false);
    }
  };

  const reload = async () => {
    setLoading(true);
    try {
      const [k, r, a, w, d] = await Promise.all([
        fetch(`${API}/api/simulator/kpi`).then(r => r.ok ? r.json() : null),
        fetch(`${API}/api/simulator/runs?limit=10`).then(r => r.ok ? r.json() : []),
        fetch(`${API}/api/simulator/auto-mode`).then(r => r.ok ? r.json() : null),
        fetch(`${API}/api/simulator/win-by-scenario`).then(r => r.ok ? r.json() : null),
        fetch(`${API}/api/simulator/sentiment-drift`).then(r => r.ok ? r.json() : null),
      ]);
      if (k) setKpi(k);
      if (Array.isArray(r)) setRecent(r);
      if (a) setAutoMode(a);
      if (w) setWinByScenario(w);
      if (d) setDrift(d);
    } catch {}
    setLoading(false);
  };

  useEffect(() => { reload(); }, []);

  // Polling adattivo per i run in corso: 4s quando ce ne sono di attivi,
  // 15s quando idle. Implementato con setTimeout ricorsivo (no setInterval)
  // cosi' il period si auto-aggiusta dinamicamente in base al return value
  // di pollOnce, senza dover ricreare l'effect ad ogni cambio di stato.
  // Evita anche di dover dichiarare deps che farebbero scattare il warning
  // "exhaustive-deps" che su Render con CI=true diventa error.
  useEffect(() => {
    let alive = true;
    let timeoutId = null;

    const pollOnce = async () => {
      try {
        const res = await fetch(`${API}/api/simulator/active-runs?include_recent=true`);
        if (!res.ok) return false;
        const data = await res.json();
        const runs = data.runs || [];
        setActiveRuns(runs);
        // Se uno dei run e' appena passato a completed (entro gli ultimi
        // 8s), ricarica KPI/recent/winBy in background senza loading flag.
        const justCompleted = runs.some(r => r.status === "completed"
                                              && r.seconds_since_update < 8);
        if (justCompleted) {
          try {
            const [k, r, w] = await Promise.all([
              fetch(`${API}/api/simulator/kpi`).then(r => r.ok ? r.json() : null),
              fetch(`${API}/api/simulator/runs?limit=10`).then(r => r.ok ? r.json() : []),
              fetch(`${API}/api/simulator/win-by-scenario`).then(r => r.ok ? r.json() : null),
            ]);
            if (k) setKpi(k);
            if (Array.isArray(r)) setRecent(r);
            if (w) setWinByScenario(w);
          } catch {}
        }
        return runs.some(x => x.status !== "completed" && x.status !== "error");
      } catch {
        return false;
      }
    };

    const tick = async () => {
      if (!alive) return;
      const hasRunning = await pollOnce();
      if (!alive) return;
      timeoutId = setTimeout(tick, hasRunning ? 4000 : 15000);
    };

    tick();   // immediate first call
    return () => {
      alive = false;
      if (timeoutId) clearTimeout(timeoutId);
    };
  }, []);

  const toggleAuto = async () => {
    try {
      await fetch(`${API}/api/simulator/auto-mode`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: !autoMode.enabled, daily_cap: autoMode.daily_cap }),
      });
      reload();
    } catch {}
  };

  const setCap = async (v) => {
    const cap = parseInt(v, 10) || 1;
    try {
      await fetch(`${API}/api/simulator/auto-mode`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: autoMode.enabled, daily_cap: cap }),
      });
      setAutoMode({ ...autoMode, daily_cap: cap });
    } catch {}
  };

  return (
    <div>
      {/* Keyframes globali per il polling LIVE indicator */}
      <style>{`
        @keyframes ar-pulse {
          0% { transform: scale(1); opacity: 1; }
          50% { transform: scale(1.5); opacity: 0.45; }
          100% { transform: scale(1); opacity: 1; }
        }
        @keyframes spin { to { transform: rotate(360deg); } }
      `}</style>
      <div style={S.header}>
        <h1 style={S.h1}>GeoInvest AI Simulator</h1>
        <div style={{ display: "flex", gap: 8 }}>
          <button
            style={{
              ...S.refreshBtn,
              opacity: (pdfBusy || !kpi || (kpi.total ?? 0) === 0) ? 0.5 : 1,
              cursor: (pdfBusy || !kpi || (kpi.total ?? 0) === 0) ? "not-allowed" : "pointer",
            }}
            onClick={handleDownloadGlobalPDF}
            disabled={pdfBusy || !kpi || (kpi.total ?? 0) === 0}
            title={
              !kpi || (kpi.total ?? 0) === 0
                ? "Avvia almeno un run prima di scaricare il report"
                : "Scarica un PDF con il riepilogo globale di tutte le simulazioni"
            }
          >
            {pdfBusy ? (
              <>
                <Loader2 size={14} style={{ animation: "spin 1s linear infinite" }} />
                Generazione…
              </>
            ) : (
              <>
                <Download size={14} /> Scarica PDF
              </>
            )}
          </button>
          <button style={S.refreshBtn} onClick={reload} disabled={loading}>
            <RefreshCw size={14} style={loading ? { animation: "spin 1s linear infinite" } : {}} />
            {loading ? "..." : "Aggiorna"}
          </button>
        </div>
      </div>
      <p style={S.sub}>
        Stato complessivo sui run completati. Clicca su uno scenario per vedere il dettaglio.
      </p>

      {/* RUN IN CORSO — sezione live (polling 4s quando attivi, 15s idle) */}
      {/* NON dentro il container PDF: e' UI operativa, non riepilogo. */}
      <ActiveRunsSection runs={activeRuns} nav={nav} />

      {/* Container PDF: tutte le sezioni con data-pdf-section qui dentro
          finiranno nel report globale. */}
      <div id="sim-dashboard-pdf">

      {/* KPI ROW */}
      <div style={S.kpiRow} data-pdf-section="true">
        <KPI label="Score composito" value={kpi?.score?.toFixed(1) ?? "—"}
             sub={kpi?.score_label ?? "Nessun dato"} tone="#a78bfa" />
        <KPI label="Win rate globale" value={kpi ? `${(kpi.win_rate * 100).toFixed(0)}%` : "—"}
             sub={`${kpi?.wins ?? 0}/${kpi?.total ?? 0} run`} tone="#10b981" />
        <KPI label="Run completati" value={kpi?.total ?? 0}
             sub={`${kpi?.single_step ?? 0} single · ${kpi?.multi_step ?? 0} multi`} tone="#06b6d4" />
        <KPI label="Δ vs S&P (medio)" value={kpi ? `${(kpi.avg_delta_sp * 100).toFixed(2)}%` : "—"}
             sub="Performance media a 1M" tone="#f472b6" />
      </div>

      {/* Win rate per categoria */}
      <div style={S.card} data-pdf-section="true">
        <div style={S.cardTitle}>Win rate per categoria</div>
        <CategoryBars data={kpi?.win_by_category} />
      </div>

      {/* Win rate per Scenario specifico (NON solo categoria) */}
      <div style={S.card} data-pdf-section="true">
        <div style={S.cardTitle}>
          <Activity size={14} style={{ display: "inline", marginRight: 6, color: "#a78bfa" }} />
          Win Rate per Scenario
        </div>
        <div style={S.subText}>
          In quali scenari l'AI è più "intelligente". Solo scenari con almeno 1 run.
          Cliccare su una riga per vedere il primo run di quello scenario.
        </div>
        <ScenarioWinTable
          data={winByScenario}
          onPickScenario={(sid) => {
            // Naviga al runner pre-selezionando questo scenario
            nav(`/simulator/runner?scenario=${sid}`);
          }}
        />
      </div>

      {/* Sentiment Drift: Top advice + Sharpe rolling */}
      <div style={S.card} data-pdf-section="true">
        <div style={S.cardTitle}>
          <BookOpen size={14} style={{ display: "inline", marginRight: 6, color: "#06b6d4" }} />
          Sentiment Drift (evoluzione strategia)
        </div>
        <div style={S.subText}>
          I 3 advice più applicati dall'Advisor + come è evoluto lo Sharpe rolling
          nei run V2. Aiuta a capire se il "sapere accumulato" sta producendo
          risultati migliori.
        </div>
        <SentimentDriftView data={drift} />
      </div>

      {/* Pulsanti azione */}
      <div style={S.actionsRow}>
        <button style={S.btnPrimary} onClick={() => nav("/simulator/runner")}>
          <Play size={16} /> Avvia Scenario Manuale
        </button>

        <div style={S.autoBox}>
          <label style={S.autoLabel}>
            <input type="checkbox" checked={autoMode.enabled} onChange={toggleAuto}
                   style={{ marginRight: 8 }} />
            Modalità Automatica
          </label>
          <label style={S.autoCap}>
            Cap giornaliero:
            <input type="number" min={1} max={50} value={autoMode.daily_cap}
                   onChange={(e) => setCap(e.target.value)} style={S.capInput} />
          </label>
          <span style={S.autoStat}>
            <Zap size={12} /> Oggi: {autoMode.runs_today}/{autoMode.daily_cap}
          </span>
        </div>
      </div>

      {/* Ultimi run */}
      <div style={S.card}>
        <div style={S.cardTitle}>Ultimi 10 run</div>
        {recent.length === 0 ? (
          <div style={S.empty}>Nessuno scenario completato. Avvia il primo scenario manuale.</div>
        ) : (
          <table style={S.table}>
            <thead>
              <tr>
                <th>Data</th><th>Categoria</th><th>Tipo</th><th>Asset</th>
                <th>Perf 1M</th><th>Δ S&P</th><th>Esito</th>
              </tr>
            </thead>
            <tbody>
              {recent.map(r => (
                <tr key={r.id} onClick={() => nav(`/simulator/result/${r.id}`)} style={S.row}>
                  <td>{new Date(r.completed_at).toLocaleDateString()}</td>
                  <td>{r.category}</td>
                  <td>{r.scenario_type === "multi" ? `${r.steps}-step` : "single"}</td>
                  <td>{r.asset_chosen || "—"}</td>
                  <td style={{ color: r.perf_1m >= 0 ? "#10b981" : "#ef4444" }}>
                    {r.perf_1m != null ? `${(r.perf_1m * 100).toFixed(2)}%` : "—"}
                  </td>
                  <td style={{ color: r.delta_sp >= 0 ? "#10b981" : "#ef4444" }}>
                    {r.delta_sp != null ? `${(r.delta_sp * 100).toFixed(2)}%` : "—"}
                  </td>
                  <td>{ <OutcomeDot outcome={r.outcome} /> }</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* OFF-SCREEN: tabella completa di tutti i run, renderizzata SOLO
          durante la generazione PDF. Non visibile all'utente, ma html2canvas
          la cattura perche' ha dimensioni reali (position:fixed con offset
          fuori viewport invece di display:none). */}
      {pdfAllRuns && pdfAllRuns.length > 0 && (
        <div data-pdf-section="true"
             style={{
               position: "fixed", left: -10000, top: 0,
               width: 1100, background: "#0a0e1a",
               padding: 20, borderRadius: 8,
               border: "1px solid #1f2937",
               fontFamily: "inherit",
             }}>
          <FullRunsTable runs={pdfAllRuns} kpi={kpi} />
        </div>
      )}

      </div>{/* /sim-dashboard-pdf */}
    </div>
  );
}

// ─── Tabella completa di tutti i run (per il PDF globale) ─────────────────
function FullRunsTable({ runs, kpi }) {
  if (!runs || runs.length === 0) return null;

  // Conta esiti per riepilogo
  const greenN = runs.filter(r => r.outcome === "green").length;
  const yellowN = runs.filter(r => r.outcome === "yellow").length;
  const redN = runs.filter(r => r.outcome === "red").length;

  return (
    <div>
      <div style={{ fontSize: 18, fontWeight: 700, color: "#e2e8f0",
                     marginBottom: 4 }}>
        Tutte le simulazioni ({runs.length})
      </div>
      <div style={{ fontSize: 12, color: "#94a3b8", marginBottom: 14,
                     lineHeight: 1.5 }}>
        Storico completo dei run completati, ordinato dal più recente.
        {" "}Esiti: <span style={{ color: "#10b981" }}>{greenN} verdi</span>
        {" · "}<span style={{ color: "#fbbf24" }}>{yellowN} gialli</span>
        {" · "}<span style={{ color: "#ef4444" }}>{redN} rossi</span>
        {kpi?.win_rate != null && (
          <> {" · "}Win rate: <strong>{(kpi.win_rate * 100).toFixed(1)}%</strong></>
        )}
      </div>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11 }}>
        <thead>
          <tr style={{ borderBottom: "2px solid #334155", color: "#94a3b8" }}>
            <th style={{ textAlign: "left", padding: "6px 4px" }}>#</th>
            <th style={{ textAlign: "left", padding: "6px 4px" }}>Data</th>
            <th style={{ textAlign: "left", padding: "6px 4px" }}>Categoria</th>
            <th style={{ textAlign: "left", padding: "6px 4px" }}>Tipo</th>
            <th style={{ textAlign: "left", padding: "6px 4px" }}>Asset</th>
            <th style={{ textAlign: "left", padding: "6px 4px" }}>Azione</th>
            <th style={{ textAlign: "right", padding: "6px 4px" }}>Conv.</th>
            <th style={{ textAlign: "right", padding: "6px 4px" }}>Perf 1S</th>
            <th style={{ textAlign: "right", padding: "6px 4px" }}>Perf 1M</th>
            <th style={{ textAlign: "right", padding: "6px 4px" }}>Perf 3M</th>
            <th style={{ textAlign: "right", padding: "6px 4px" }}>Δ S&P</th>
            <th style={{ textAlign: "center", padding: "6px 4px" }}>Esito</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r, i) => {
            const fmtPct = (v) => v == null ? "—"
              : `${v >= 0 ? "+" : ""}${(v * 100).toFixed(2)}%`;
            const colorPct = (v) => v == null ? "#94a3b8"
              : v >= 0 ? "#10b981" : "#ef4444";
            const outcomeColor = r.outcome === "green" ? "#10b981"
                               : r.outcome === "yellow" ? "#fbbf24"
                               : r.outcome === "red" ? "#ef4444" : "#475569";
            return (
              <tr key={r.id || i} style={{
                borderBottom: "1px solid #1f2937",
                color: "#cbd5e1",
              }}>
                <td style={{ padding: "5px 4px", color: "#64748b" }}>{i + 1}</td>
                <td style={{ padding: "5px 4px" }}>
                  {r.completed_at
                    ? new Date(r.completed_at).toLocaleDateString("it-IT")
                    : "—"}
                </td>
                <td style={{ padding: "5px 4px", color: "#94a3b8" }}>
                  {r.category || "—"}
                </td>
                <td style={{ padding: "5px 4px", color: "#94a3b8" }}>
                  {r.scenario_type === "multi" ? `${r.steps}-step` : "single"}
                </td>
                <td style={{ padding: "5px 4px", fontFamily: "monospace" }}>
                  {r.asset_chosen || "—"}
                </td>
                <td style={{ padding: "5px 4px",
                              color: r.action_chosen === "BUY" ? "#10b981"
                                    : r.action_chosen === "SELL" ? "#ef4444"
                                    : "#94a3b8",
                              fontWeight: 600 }}>
                  {r.action_chosen || "—"}
                </td>
                <td style={{ padding: "5px 4px", textAlign: "right",
                              color: "#94a3b8" }}>
                  {r.conviction || "—"}
                </td>
                <td style={{ padding: "5px 4px", textAlign: "right",
                              color: colorPct(r.perf_1w) }}>
                  {fmtPct(r.perf_1w)}
                </td>
                <td style={{ padding: "5px 4px", textAlign: "right",
                              color: colorPct(r.perf_1m), fontWeight: 600 }}>
                  {fmtPct(r.perf_1m)}
                </td>
                <td style={{ padding: "5px 4px", textAlign: "right",
                              color: colorPct(r.perf_3m) }}>
                  {fmtPct(r.perf_3m)}
                </td>
                <td style={{ padding: "5px 4px", textAlign: "right",
                              color: colorPct(r.delta_sp) }}>
                  {fmtPct(r.delta_sp)}
                </td>
                <td style={{ padding: "5px 4px", textAlign: "center" }}>
                  <span style={{
                    display: "inline-block", width: 10, height: 10,
                    borderRadius: "50%", background: outcomeColor,
                  }} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function KPI({ label, value, sub, tone }) {
  return (
    <div style={{ ...S.kpi, borderLeftColor: tone }}>
      <div style={S.kpiLabel}>{label}</div>
      <div style={S.kpiValue}>{value}</div>
      <div style={S.kpiSub}>{sub}</div>
    </div>
  );
}

function CategoryBars({ data }) {
  const cats = [
    { key: "normale", name: "Normale", tone: "#06b6d4" },
    { key: "geopolitico", name: "Geopolitico", tone: "#f472b6" },
    { key: "macro", name: "Macro", tone: "#fbbf24" },
    { key: "crash_rally", name: "Crash/Rally", tone: "#ef4444" },
  ];
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {cats.map(c => {
        const pct = data?.[c.key] ?? 0;
        const total = data?.[c.key + "_total"] ?? 0;
        return (
          <div key={c.key} style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <span style={{ width: 110, fontSize: 13, color: "#cbd5e1" }}>{c.name}</span>
            <div style={{ flex: 1, height: 18, background: "#1f2937", borderRadius: 4, overflow: "hidden" }}>
              <div style={{ width: `${pct * 100}%`, height: "100%", background: c.tone,
                            transition: "width 0.4s" }} />
            </div>
            <span style={{ width: 90, fontSize: 12, color: "#94a3b8", textAlign: "right" }}>
              {(pct * 100).toFixed(0)}% ({total})
            </span>
          </div>
        );
      })}
    </div>
  );
}

function OutcomeDot({ outcome }) {
  const colors = { green: "#10b981", yellow: "#fbbf24", red: "#ef4444" };
  return <span style={{ display: "inline-block", width: 10, height: 10, borderRadius: "50%",
                        background: colors[outcome] || "#475569" }} />;
}

// ─── Active Runs Section (real-time progress) ─────────────────────────────

function ActiveRunsSection({ runs, nav }) {
  // Mostra sempre la sezione, anche vuota: l'utente sa dove guardare
  // quando lancia un run. Niente layout shift ad ogni nuovo run.
  const hasRunning = runs.some(r => r.status !== "completed" && r.status !== "error");

  return (
    <div style={S.activeCard}>
      <div style={S.activeHeader}>
        <div style={S.cardTitle}>
          <Activity size={14} style={{ display: "inline", marginRight: 6,
                                         color: hasRunning ? "#10b981" : "#64748b" }} />
          Run in corso
          {hasRunning && (
            <span style={S.liveDot}>
              <span style={S.liveDotInner} />
              LIVE
            </span>
          )}
        </div>
        <div style={S.activeSub}>
          {runs.length === 0 ? "Nessun run attivo"
            : `${runs.length} ${runs.length === 1 ? "run" : "run"} in elenco`}
        </div>
      </div>

      {runs.length === 0 ? (
        <div style={S.activeEmpty}>
          Quando lanci uno scenario manuale o quando l'auto-mode esegue un run,
          lo vedrai qui in tempo reale.
        </div>
      ) : (
        <div style={S.activeList}>
          {runs.map((r) => (
            <ActiveRunCard key={r.run_id} run={r} nav={nav} />
          ))}
        </div>
      )}
    </div>
  );
}


function ActiveRunCard({ run, nav }) {
  const total = run.total_steps || 1;
  const cur = Math.max(0, Math.min(total, run.current_step + 1));
  const pct = run.status === "completed" ? 100
            : run.status === "error" ? Math.max(2, (cur / total) * 100)
            : Math.max(2, (cur / total) * 100);

  const isCrypto = run.engine === "v2_crypto";
  const isAuto = run.mode === "auto";
  const EngineIcon = isCrypto ? Bitcoin : TrendingUp;

  // Status display
  const statusInfo = STATUS_MAP[run.status] || STATUS_MAP.starting;
  const StatusIcon = statusInfo.icon;
  const stepUnit = isCrypto ? "2 giorni" : "1 settimana";

  // Click → naviga al risultato se completed e abbiamo persisted_run_id
  const onClick = () => {
    if (run.status === "completed" && run.persisted_run_id) {
      nav(`/simulator/result/${run.persisted_run_id}`);
    }
  };
  const clickable = run.status === "completed" && run.persisted_run_id;

  return (
    <div style={{ ...S.runCard, ...(clickable ? S.runCardClickable : {}),
                   borderLeftColor: statusInfo.color }}
         onClick={onClick}>
      {/* Top row: titolo scenario + badge mode */}
      <div style={{ display: "flex", justifyContent: "space-between",
                     alignItems: "flex-start", gap: 10, marginBottom: 6 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={S.runTitle}>
            <EngineIcon size={13} style={{ marginRight: 5,
                                            color: isCrypto ? "#fbbf24" : "#06b6d4",
                                            verticalAlign: "text-top" }} />
            {run.scenario_title || run.scenario_id || "Scenario"}
          </div>
          <div style={S.runMeta}>
            <span style={S.runMetaTag}>{run.category}</span>
            <span style={S.runMetaSep}>·</span>
            <span style={{ ...S.runMetaTag,
                            color: isAuto ? "#fbbf24" : "#a78bfa",
                            background: isAuto ? "rgba(251,191,36,0.10)" : "rgba(167,139,250,0.10)",
                            border: `1px solid ${isAuto ? "rgba(251,191,36,0.3)" : "rgba(167,139,250,0.3)"}`,
                          }}>
              {isAuto ? <><Bot size={9} style={{ marginRight: 3, verticalAlign: "middle" }} /> AUTO</>
                       : <><Cpu size={9} style={{ marginRight: 3, verticalAlign: "middle" }} /> MANUAL</>}
            </span>
            <span style={S.runMetaSep}>·</span>
            <span style={S.runMetaTag}>
              {total} step × {stepUnit}
            </span>
          </div>
        </div>
        {/* Status badge */}
        <div style={{ ...S.statusBadge, color: statusInfo.color,
                       background: statusInfo.bg, borderColor: statusInfo.border }}>
          <StatusIcon size={11} className={statusInfo.spin ? "ar-spin" : ""}
                      style={{ marginRight: 4 }} />
          {statusInfo.label}
        </div>
      </div>

      {/* Progress bar */}
      <div style={S.progressWrap}>
        <div style={{ ...S.progressBar, width: `${pct}%`,
                       background: statusInfo.color,
                       opacity: run.status === "completed" ? 1 : 0.85 }} />
        {/* Shimmer overlay for running */}
        {run.status !== "completed" && run.status !== "error" && (
          <div style={S.shimmer} />
        )}
      </div>

      {/* Bottom row: step counter + elapsed + outcome (se completato) */}
      <div style={S.runBottom}>
        <span style={S.runStep}>
          Step{" "}
          <strong style={{ color: "#e2e8f0" }}>
            {run.status === "completed" ? total : cur}
          </strong>
          {" / "}
          <span style={{ color: "#94a3b8" }}>{total}</span>
        </span>
        <span style={S.runStep}>
          <span style={{ color: "#64748b" }}>Tempo:</span>{" "}
          <strong style={{ color: "#cbd5e1", fontVariantNumeric: "tabular-nums" }}>
            {formatElapsed(run.elapsed_seconds || 0)}
          </strong>
        </span>
        {run.status === "completed" && run.pnl_pct != null && (
          <span style={{ ...S.runStep,
                          color: run.pnl_pct >= 0 ? "#10b981" : "#ef4444" }}>
            P&L: <strong>{run.pnl_pct >= 0 ? "+" : ""}{run.pnl_pct.toFixed(2)}%</strong>
            {run.outcome && (
              <span style={{ marginLeft: 6, fontSize: 11 }}>
                ({outcomeBadge(run.outcome)})
              </span>
            )}
          </span>
        )}
        {run.status === "error" && (
          <span style={{ ...S.runStep, color: "#fca5a5",
                          maxWidth: 360, overflow: "hidden",
                          textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {run.error_msg || "errore"}
          </span>
        )}
        {clickable && (
          <span style={{ marginLeft: "auto", fontSize: 11, color: "#a78bfa" }}>
            Apri risultato →
          </span>
        )}
      </div>

      {/* CSS shimmer/spin (scoped) */}
      <style>{`
        @keyframes ar-shimmer { 0% { transform: translateX(-100%); } 100% { transform: translateX(100%); } }
        @keyframes ar-spin { from { transform: rotate(0); } to { transform: rotate(360deg); } }
        .ar-spin { animation: ar-spin 1.1s linear infinite; }
      `}</style>
    </div>
  );
}


// Map status → label + colore + icona
const STATUS_MAP = {
  starting:        { label: "Avvio",            color: "#94a3b8", bg: "rgba(148,163,184,0.10)", border: "rgba(148,163,184,0.3)", icon: Loader2, spin: true },
  stepping:        { label: "Step in corso",    color: "#06b6d4", bg: "rgba(6,182,212,0.10)",   border: "rgba(6,182,212,0.3)",   icon: Loader2, spin: true },
  calling_ai:      { label: "Decision Agent",   color: "#a78bfa", bg: "rgba(167,139,250,0.10)", border: "rgba(167,139,250,0.3)", icon: Loader2, spin: true },
  applying_trades: { label: "Applico trade",    color: "#06b6d4", bg: "rgba(6,182,212,0.10)",   border: "rgba(6,182,212,0.3)",   icon: Loader2, spin: true },
  finalizing:      { label: "Finalizing",       color: "#fbbf24", bg: "rgba(251,191,36,0.10)",  border: "rgba(251,191,36,0.3)",  icon: Loader2, spin: true },
  completed:       { label: "Completato",       color: "#10b981", bg: "rgba(16,185,129,0.10)",  border: "rgba(16,185,129,0.3)",  icon: CheckCircle2, spin: false },
  error:           { label: "Errore",           color: "#ef4444", bg: "rgba(239,68,68,0.10)",   border: "rgba(239,68,68,0.3)",   icon: AlertCircle, spin: false },
};


function formatElapsed(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rs = s % 60;
  if (m < 60) return `${m}m ${String(rs).padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return `${h}h ${String(rm).padStart(2, "0")}m`;
}

function outcomeBadge(outcome) {
  if (outcome === "green") return "verde";
  if (outcome === "red") return "rosso";
  return "neutro";
}

// ─── Scenario Win Rate Table ──────────────────────────────────────────────

function ScenarioWinTable({ data, onPickScenario }) {
  if (!data || !data.scenarios || data.scenarios.length === 0) {
    return (
      <div style={S.empty}>
        Nessuno scenario giocato finora. Avvia almeno un Scenario Manuale.
      </div>
    );
  }
  // Cap a 12 righe top per non gonfiare la UI
  const rows = data.scenarios.slice(0, 12);
  return (
    <table style={S.table}>
      <thead>
        <tr>
          <th style={{ textAlign: "left" }}>Scenario</th>
          <th>Categoria</th>
          <th>Run</th>
          <th>Win rate</th>
          <th>P&L 1M</th>
          <th>Sharpe</th>
        </tr>
      </thead>
      <tbody>
        {rows.map(s => {
          const winColor = s.win_rate >= 0.7 ? "#10b981"
                         : s.win_rate >= 0.4 ? "#fbbf24"
                         : "#ef4444";
          return (
            <tr key={s.scenario_id} onClick={() => onPickScenario && onPickScenario(s.scenario_id)}
                style={S.row}>
              <td style={{ maxWidth: 320, overflow: "hidden", textOverflow: "ellipsis",
                            whiteSpace: "nowrap" }}>
                {s.title || s.scenario_id}
              </td>
              <td style={{ fontSize: 11, color: "#94a3b8" }}>{s.category}</td>
              <td>{s.n_runs}</td>
              <td>
                <span style={{ color: winColor, fontWeight: 600 }}>
                  {(s.win_rate * 100).toFixed(0)}%
                </span>
                <span style={{ fontSize: 10, color: "#64748b", marginLeft: 4 }}>
                  ({s.n_green}/{s.n_runs})
                </span>
              </td>
              <td style={{ color: s.avg_pnl_1m == null ? "#94a3b8"
                            : s.avg_pnl_1m >= 0 ? "#10b981" : "#ef4444" }}>
                {s.avg_pnl_1m == null ? "—"
                  : `${s.avg_pnl_1m >= 0 ? "+" : ""}${(s.avg_pnl_1m * 100).toFixed(2)}%`}
              </td>
              <td style={{ color: s.avg_sharpe == null ? "#94a3b8"
                            : s.avg_sharpe >= 1 ? "#10b981"
                            : s.avg_sharpe >= 0 ? "#fbbf24" : "#ef4444" }}>
                {s.avg_sharpe == null ? "—" : s.avg_sharpe.toFixed(2)}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}


// ─── Sentiment Drift View ─────────────────────────────────────────────────

function SentimentDriftView({ data }) {
  if (!data) {
    return <div style={S.empty}>Caricamento sentiment drift...</div>;
  }
  const top = data.top_advice || [];
  const series = data.sharpe_evolution || [];

  return (
    <div>
      {/* Top 3 advice */}
      {top.length === 0 ? (
        <div style={S.subText}>
          Nessun advice salvato finora. Apri il "Risultato" di un run e usa
          "Analizza & Migliora" per estrarre le prime lezioni dall'AI.
        </div>
      ) : (
        <div style={{ marginBottom: 18 }}>
          <div style={{ fontSize: 12, color: "#94a3b8", marginBottom: 8,
                         textTransform: "uppercase", letterSpacing: "0.06em" }}>
            Top 3 Advice (per applicazioni)
          </div>
          {top.map((a, i) => (
            <div key={a.id || i} style={{ background: "#0f172a",
                                            padding: "10px 14px", borderRadius: 6,
                                            marginBottom: 6, border: "1px solid #1a2030" }}>
              <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
                <span style={{ fontSize: 11, color: "#a78bfa", fontWeight: 600 }}>
                  #{i + 1}
                </span>
                <span style={{ flex: 1, fontSize: 13, color: "#e2e8f0", fontWeight: 600 }}>
                  {a.title || "(senza titolo)"}
                </span>
                <span style={{ fontSize: 11, color: "#fbbf24", fontWeight: 600 }}>
                  ×{a.apply_count} apply
                </span>
              </div>
              <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 4 }}>
                {a.scenario_category}
              </div>
              <div style={{ fontSize: 12, color: "#cbd5e1", marginTop: 6,
                              lineHeight: 1.5 }}>
                {a.text}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Sharpe rolling chart */}
      {series.length === 0 ? (
        <div style={S.subText}>
          Servono almeno alcuni run V2 (Simulator multi-step con metriche)
          per visualizzare l'evoluzione dello Sharpe.
        </div>
      ) : (
        <SharpeRollingChart data={series} window={data.rolling_window} />
      )}
    </div>
  );
}


function SharpeRollingChart({ data, window: rollWindow }) {
  const w = 800, h = 200, padL = 50, padR = 30, padT = 20, padB = 35;
  const innerW = w - padL - padR, innerH = h - padT - padB;

  const sharpes = data.map(d => d.sharpe);
  const rollings = data.map(d => d.rolling_mean_sharpe);
  const all = [...sharpes, ...rollings, 0];
  let minV = Math.min(...all), maxV = Math.max(...all);
  const pad = Math.max(0.3, (maxV - minV) * 0.1);
  minV -= pad; maxV += pad;
  const range = maxV - minV || 1;
  const n = data.length;

  const xToPx = (i) => padL + (i / Math.max(1, n - 1)) * innerW;
  const yToPx = (v) => padT + innerH - ((v - minV) / range) * innerH;

  const sharpePts = data.map((d, i) => `${xToPx(i)},${yToPx(d.sharpe)}`).join(" ");
  const rollPts = data.map((d, i) => `${xToPx(i)},${yToPx(d.rolling_mean_sharpe)}`).join(" ");

  return (
    <div>
      <div style={{ display: "flex", gap: 14, marginBottom: 8, fontSize: 11 }}>
        <span style={{ color: "#475569" }}>
          <span style={{ display: "inline-block", width: 10, height: 2, background: "#475569",
                          marginRight: 4, verticalAlign: "middle" }} />
          Sharpe per run
        </span>
        <span style={{ color: "#a78bfa" }}>
          <span style={{ display: "inline-block", width: 10, height: 2, background: "#a78bfa",
                          marginRight: 4, verticalAlign: "middle" }} />
          Rolling {rollWindow}-run mean
        </span>
      </div>
      <svg width="100%" height={h} viewBox={`0 0 ${w} ${h}`} style={{ display: "block" }}>
        {/* Linea zero */}
        <line x1={padL} y1={yToPx(0)} x2={w - padR} y2={yToPx(0)}
              stroke="#475569" strokeWidth={1} strokeDasharray="3 3" opacity={0.6} />
        {/* Linea Sharpe=1 e Sharpe=2 (riferimenti "buono" e "eccellente") */}
        {[1, 2].map(t => {
          if (t > maxV || t < minV) return null;
          return (
            <g key={t}>
              <line x1={padL} y1={yToPx(t)} x2={w - padR} y2={yToPx(t)}
                    stroke={t === 2 ? "#10b981" : "#84cc16"}
                    strokeWidth={1} strokeDasharray="2 4" opacity={0.4} />
              <text x={w - padR - 24} y={yToPx(t) - 2} fill={t === 2 ? "#10b981" : "#84cc16"}
                    fontSize={9}>{t === 2 ? "ECCELLENTE" : "BUONO"}</text>
            </g>
          );
        })}
        {/* Y axis labels */}
        {[0, 0.5, 1].map((t, i) => {
          const v = minV + t * range;
          return (
            <text key={i} x={padL - 6} y={yToPx(v) + 4} fill="#64748b" fontSize={10}
                  textAnchor="end" fontFamily="monospace">{v.toFixed(1)}</text>
          );
        })}
        {/* Linea Sharpe per-run (sottile, grigia) */}
        <polyline points={sharpePts} fill="none" stroke="#475569" strokeWidth={1.2} />
        {/* Linea rolling mean (spessa, viola) */}
        <polyline points={rollPts} fill="none" stroke="#a78bfa" strokeWidth={2.5} />
        {/* Punti */}
        {data.map((d, i) => (
          <circle key={i} cx={xToPx(i)} cy={yToPx(d.sharpe)} r={2.5}
                  fill="#475569" />
        ))}
        {/* Asse X */}
        <text x={padL} y={h - 8} fill="#64748b" fontSize={10}>run #1</text>
        <text x={w - padR} y={h - 8} fill="#64748b" fontSize={10} textAnchor="end">
          run #{n}
        </text>
      </svg>
    </div>
  );
}

const S = {
  header: { display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 },
  h1: { margin: 0, fontSize: "1.8rem", fontWeight: 600 },
  sub: { color: "#94a3b8", fontSize: 14, marginTop: 0, marginBottom: 24 },
  refreshBtn: { background: "#1e293b", color: "#cbd5e1", border: "1px solid #334155",
                padding: "6px 12px", borderRadius: 6, cursor: "pointer",
                display: "inline-flex", alignItems: "center", gap: 6, fontSize: 13 },
  kpiRow: { display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12, marginBottom: 24 },
  kpi: { background: "#111827", border: "1px solid #1f2937", borderLeft: "3px solid",
         padding: "16px", borderRadius: 8 },
  kpiLabel: { color: "#94a3b8", fontSize: 11, textTransform: "uppercase", letterSpacing: 0.5 },
  kpiValue: { fontSize: "1.6rem", fontWeight: 600, margin: "6px 0", color: "#f1f5f9" },
  kpiSub: { color: "#64748b", fontSize: 12 },
  card: { background: "#111827", border: "1px solid #1f2937", padding: 20,
          borderRadius: 8, marginBottom: 20 },
  cardTitle: { fontSize: 14, fontWeight: 600, color: "#e2e8f0", marginBottom: 14 },
  subText: { fontSize: 12, color: "#94a3b8", marginBottom: 14, lineHeight: 1.55 },
  empty: { padding: 24, textAlign: "center", color: "#64748b", fontSize: 13 },
  actionsRow: { display: "flex", gap: 16, alignItems: "center", marginBottom: 24, flexWrap: "wrap" },
  btnPrimary: { background: "#a78bfa", color: "#0a0e1a", border: 0, padding: "10px 18px",
                borderRadius: 6, fontWeight: 600, cursor: "pointer",
                display: "inline-flex", alignItems: "center", gap: 8, fontSize: 14 },
  autoBox: { display: "flex", gap: 16, alignItems: "center", padding: "8px 14px",
             background: "#111827", border: "1px solid #1f2937", borderRadius: 6 },
  autoLabel: { color: "#cbd5e1", fontSize: 13, cursor: "pointer", display: "flex", alignItems: "center" },
  autoCap: { color: "#94a3b8", fontSize: 12, display: "flex", alignItems: "center", gap: 6 },
  capInput: { width: 50, padding: "4px 6px", background: "#0a0e1a", color: "#f1f5f9",
              border: "1px solid #334155", borderRadius: 4, fontSize: 12 },
  autoStat: { color: "#a78bfa", fontSize: 12, display: "inline-flex", alignItems: "center", gap: 4 },
  table: { width: "100%", borderCollapse: "collapse", fontSize: 13 },
  row: { cursor: "pointer", borderBottom: "1px solid #1f2937" },

  // ── Active Runs section ──
  activeCard: {
    background: "linear-gradient(180deg, rgba(17,24,39,0.95) 0%, rgba(17,24,39,1) 100%)",
    border: "1px solid #1f2937",
    borderRadius: 12, padding: 18, marginBottom: 24,
  },
  activeHeader: {
    display: "flex", justifyContent: "space-between",
    alignItems: "center", marginBottom: 12, gap: 12, flexWrap: "wrap",
  },
  activeSub: { fontSize: 12, color: "#64748b" },
  activeEmpty: {
    background: "#0a1018", border: "1px dashed rgba(255,255,255,0.06)",
    color: "#64748b", fontSize: 12.5, padding: "16px 20px",
    borderRadius: 8, lineHeight: 1.55,
  },
  activeList: { display: "flex", flexDirection: "column", gap: 10 },

  liveDot: {
    display: "inline-flex", alignItems: "center", gap: 5,
    fontSize: 9, color: "#10b981", letterSpacing: "0.12em",
    background: "rgba(16,185,129,0.10)", border: "1px solid rgba(16,185,129,0.3)",
    padding: "2px 8px", borderRadius: 999, marginLeft: 10,
    fontWeight: 700,
  },
  liveDotInner: {
    display: "inline-block", width: 6, height: 6, borderRadius: "50%",
    background: "#10b981", boxShadow: "0 0 6px #10b981",
    animation: "ar-pulse 1.6s ease-in-out infinite",
  },

  runCard: {
    background: "#0f172a",
    border: "1px solid rgba(255,255,255,0.05)",
    borderLeft: "3px solid #06b6d4",
    padding: "12px 14px", borderRadius: 8,
    transition: "background 0.15s ease",
    position: "relative",
  },
  runCardClickable: {
    cursor: "pointer",
  },
  runTitle: {
    fontSize: 13.5, color: "#e2e8f0", fontWeight: 600,
    overflow: "hidden", textOverflow: "ellipsis",
    whiteSpace: "nowrap", marginBottom: 4,
  },
  runMeta: {
    display: "flex", alignItems: "center", gap: 6, fontSize: 11,
    color: "#94a3b8", flexWrap: "wrap",
  },
  runMetaTag: {
    background: "rgba(255,255,255,0.04)", padding: "2px 7px",
    borderRadius: 999, fontFamily: "monospace", fontSize: 10,
    border: "1px solid rgba(255,255,255,0.06)", whiteSpace: "nowrap",
  },
  runMetaSep: { color: "#475569" },

  statusBadge: {
    display: "inline-flex", alignItems: "center",
    fontSize: 10.5, fontWeight: 600, letterSpacing: "0.04em",
    padding: "4px 9px", borderRadius: 999,
    border: "1px solid", whiteSpace: "nowrap",
    textTransform: "uppercase",
  },

  progressWrap: {
    height: 6, background: "rgba(255,255,255,0.05)",
    borderRadius: 999, overflow: "hidden", margin: "10px 0 8px",
    position: "relative",
  },
  progressBar: {
    height: "100%", borderRadius: 999,
    transition: "width 0.5s ease",
  },
  shimmer: {
    position: "absolute", top: 0, bottom: 0, width: "30%",
    background: "linear-gradient(90deg, rgba(255,255,255,0) 0%, rgba(255,255,255,0.15) 50%, rgba(255,255,255,0) 100%)",
    animation: "ar-shimmer 1.6s ease-in-out infinite",
  },

  runBottom: {
    display: "flex", alignItems: "center", gap: 14,
    fontSize: 11.5, color: "#94a3b8", flexWrap: "wrap",
  },
  runStep: {
    fontVariantNumeric: "tabular-nums",
  },
};
