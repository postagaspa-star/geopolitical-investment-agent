import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Play, ChevronRight, Loader2 } from "lucide-react";

const API = window.location.origin;

const CATEGORIES = [
  { key: "normale", name: "Normale", desc: "Mercato in condizioni standard, volatilità contenuta, niente eventi strutturali" },
  { key: "geopolitico", name: "Geopolitico", desc: "Crisi geopolitiche, conflitti, sanzioni, elezioni che muovono i mercati" },
  { key: "macro", name: "Macro", desc: "Eventi macro: FOMC, dati CPI/NFP, banche centrali, recessioni" },
  { key: "crash_rally", name: "Crash/Rally", desc: "Shock acuti: crash >5%, rally inattesi, panic selling, blowoff tops" },
];

/**
 * Scenario Runner — costruzione test in 4 step:
 *  1. categoria
 *  2. tipo (single/multi)
 *  3. selezione scenario (specifico o random)
 *  4. esecuzione live con context + reasoning streaming
 */
export default function SimRunner() {
  const nav = useNavigate();
  const [step, setStep] = useState(1);  // 1..4 (config) | "running" | "done"
  const [category, setCategory] = useState(null);
  const [scenarioType, setScenarioType] = useState("single");
  const [numSteps, setNumSteps] = useState(3);
  const [scenarios, setScenarios] = useState([]);
  const [scenarioId, setScenarioId] = useState(null);
  const [scenarioCounts, setScenarioCounts] = useState({});

  // Run state
  const [runId, setRunId] = useState(null);
  const [stepIdx, setStepIdx] = useState(0);
  const [stepData, setStepData] = useState([]);   // array di {context, reasoning, decision} per ogni step
  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);

  // Conta scenari per ogni categoria
  useEffect(() => {
    fetch(`${API}/api/simulator/scenarios/counts`).then(r => r.ok ? r.json() : {}).then(setScenarioCounts).catch(() => {});
  }, []);

  // Quando categoria cambia, carica gli scenari di quella categoria
  useEffect(() => {
    if (!category) return;
    fetch(`${API}/api/simulator/scenarios?category=${category}`)
      .then(r => r.ok ? r.json() : [])
      .then(d => setScenarios(Array.isArray(d) ? d : []))
      .catch(() => setScenarios([]));
  }, [category]);

  // === Step 1: categoria ===
  const renderStep1 = () => (
    <div>
      <h2 style={S.h2}>1 · Scegli categoria scenario</h2>
      <div style={S.cardsGrid}>
        {CATEGORIES.map(c => (
          <button key={c.key} style={{ ...S.catCard, ...(category === c.key ? S.catCardSel : {}) }}
                  onClick={() => setCategory(c.key)}>
            <div style={S.catName}>{c.name}</div>
            <div style={S.catDesc}>{c.desc}</div>
            <div style={S.catCount}>{scenarioCounts[c.key] ?? "?"} scenari disponibili</div>
          </button>
        ))}
      </div>
      <button style={S.btnPrimary} disabled={!category} onClick={() => setStep(2)}>
        Avanti <ChevronRight size={16} />
      </button>
    </div>
  );

  // === Step 2: tipo ===
  const renderStep2 = () => (
    <div>
      <h2 style={S.h2}>2 · Tipo di scenario</h2>
      <div style={{ display: "flex", gap: 12, marginBottom: 20 }}>
        {[
          { v: "single", l: "Single-step", d: "Una sola decisione su uno snapshot" },
          { v: "multi", l: "Multi-step", d: "Decisioni a T0, T1, T2... con update tra uno e l'altro" },
        ].map(t => (
          <button key={t.v}
                  style={{ ...S.typeCard, ...(scenarioType === t.v ? S.typeCardSel : {}) }}
                  onClick={() => setScenarioType(t.v)}>
            <strong>{t.l}</strong>
            <div style={{ fontSize: 12, color: "#94a3b8", marginTop: 4 }}>{t.d}</div>
          </button>
        ))}
      </div>
      {scenarioType === "multi" && (
        <div style={{ marginBottom: 20 }}>
          <label style={{ fontSize: 13, color: "#cbd5e1" }}>
            Numero step:{" "}
            <select value={numSteps} onChange={(e) => setNumSteps(parseInt(e.target.value, 10))}
                    style={S.select}>
              <option value={3}>3</option>
              <option value={4}>4</option>
              <option value={5}>5</option>
            </select>
          </label>
        </div>
      )}
      <div style={{ display: "flex", gap: 8 }}>
        <button style={S.btnSec} onClick={() => setStep(1)}>Indietro</button>
        <button style={S.btnPrimary} onClick={() => setStep(3)}>Avanti <ChevronRight size={16} /></button>
      </div>
    </div>
  );

  // === Step 3: selezione scenario specifico (o random) ===
  const renderStep3 = () => (
    <div>
      <h2 style={S.h2}>3 · Scenario specifico</h2>
      <p style={S.note}>
        Lascia "Random" per pescare uno scenario casuale dalla categoria.
        Le descrizioni non rivelano il periodo storico.
      </p>
      <div style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 20 }}>
        <button style={{ ...S.scenarioRow, ...(scenarioId === null ? S.scenarioRowSel : {}) }}
                onClick={() => setScenarioId(null)}>
          🎲 Random — il sistema sceglie per te
        </button>
        {scenarios.map(s => (
          <button key={s.id}
                  style={{ ...S.scenarioRow, ...(scenarioId === s.id ? S.scenarioRowSel : {}) }}
                  onClick={() => setScenarioId(s.id)}>
            <strong>{s.title}</strong>
            <div style={{ fontSize: 12, color: "#94a3b8", marginTop: 2 }}>{s.brief}</div>
          </button>
        ))}
        {scenarios.length === 0 && (
          <div style={{ color: "#64748b", fontSize: 13, padding: 12 }}>
            Nessuno scenario in libreria per questa categoria. (Random pescherà comunque
            uno scenario disponibile.)
          </div>
        )}
      </div>
      <div style={{ display: "flex", gap: 8 }}>
        <button style={S.btnSec} onClick={() => setStep(2)}>Indietro</button>
        <button style={S.btnPrimary} onClick={() => setStep(4)}>Avanti <ChevronRight size={16} /></button>
      </div>
    </div>
  );

  // === Step 4: conferma + avvio ===
  const renderStep4 = () => (
    <div>
      <h2 style={S.h2}>4 · Conferma e avvia</h2>
      <div style={S.summaryBox}>
        <div><strong>Categoria:</strong> {CATEGORIES.find(c => c.key === category)?.name}</div>
        <div><strong>Tipo:</strong> {scenarioType === "multi" ? `Multi-step (${numSteps} step)` : "Single-step"}</div>
        <div><strong>Scenario:</strong> {scenarioId ? scenarios.find(s => s.id === scenarioId)?.title : "Random"}</div>
      </div>
      <div style={{ display: "flex", gap: 8 }}>
        <button style={S.btnSec} onClick={() => setStep(3)}>Indietro</button>
        <button style={S.btnPrimary} onClick={startRun}>
          <Play size={16} /> Avvia Test
        </button>
      </div>
    </div>
  );

  // === Avvio scenario ===
  const startRun = async () => {
    setError(null);
    setRunning(true);
    setStep("running");
    try {
      const res = await fetch(`${API}/api/simulator/run/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          category,
          scenario_type: scenarioType,
          num_steps: scenarioType === "multi" ? numSteps : 1,
          scenario_id: scenarioId,
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setRunId(data.run_id);
      // Esegui primo step subito
      await runStep(data.run_id, 0);
    } catch (e) {
      setError(e.message);
      setRunning(false);
      setStep(4);
    }
  };

  const runStep = async (rid, idx) => {
    setRunning(true);
    try {
      const res = await fetch(`${API}/api/simulator/run/${rid}/step`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ step_index: idx }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setStepData(prev => [...prev, data]);
      setStepIdx(idx);
      setRunning(false);
      // Auto-completion: se è l'ultimo step
      if (data.is_last_step) {
        setStep("done");
      }
    } catch (e) {
      setError(e.message);
      setRunning(false);
    }
  };

  const renderRunning = () => {
    const current = stepData[stepIdx];
    const totalSteps = scenarioType === "multi" ? numSteps : 1;
    const updateDetails = current?.context?.update_details;
    const isMulti = scenarioType === "multi" && stepIdx > 0;

    return (
      <div>
        <div style={S.runHeader}>
          <h2 style={S.h2}>
            Step {stepIdx + 1} di {totalSteps}
            {scenarioType === "single" && (
              <span style={S.modeChip}>SINGLE-STEP · max 1 mese</span>
            )}
            {scenarioType === "multi" && (
              <span style={S.modeChip}>MULTI-STEP</span>
            )}
          </h2>
          {running && <span style={S.runStatus}><Loader2 size={14} className="spin" /> Agente in elaborazione…</span>}
        </div>

        {/* MULTI-STEP: banner cambiamenti dal turno precedente (sopra a tutto) */}
        {isMulti && updateDetails && (
          <UpdateBanner stepIdx={stepIdx} totalSteps={totalSteps} details={updateDetails} />
        )}

        <div style={S.runColumns}>
          {/* SINISTRA: contesto */}
          <div style={S.runCol}>
            <h3 style={S.colTitle}>Contesto fornito all'agente</h3>
            {current?.context && (
              <>
                <div style={S.contextBlock}>
                  <strong>📰 Headline:</strong>
                  <ul style={S.headlineList}>
                    {(current.context.headlines || []).map((h, i) => {
                      const isNew = isMulti && updateDetails?.headline_changes?.includes(h);
                      return (
                        <li key={i} style={isNew ? { color: "#fbbf24", fontWeight: 600 } : null}>
                          {isNew && <span style={S.newTag}>NUOVA</span>}
                          {h}
                        </li>
                      );
                    })}
                  </ul>
                </div>
                <div style={S.contextBlock}>
                  <strong>📊 Mercato:</strong>
                  <table style={S.priceTable}>
                    <thead>
                      <tr>
                        <th>Asset</th><th>Prezzo</th>
                        {isMulti && <th>Δ vs T-1</th>}
                        <th>24h</th><th>7g</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(current.context.market_data || []).map((m, i) => {
                        const change = updateDetails?.price_changes?.find(c => c.ticker === m.ticker);
                        return (
                          <tr key={i}>
                            <td style={{ fontFamily: "monospace" }}>{m.ticker}</td>
                            <td>${m.price_t0?.toFixed(2)}</td>
                            {isMulti && (
                              <td style={{
                                color: change?.delta_pct >= 0 ? "#10b981" : "#ef4444",
                                fontWeight: 600,
                              }}>
                                {change?.delta_pct != null
                                  ? `${change.delta_pct >= 0 ? "+" : ""}${change.delta_pct.toFixed(2)}%`
                                  : "—"}
                              </td>
                            )}
                            <td style={{color: m.change_24h >= 0 ? "#10b981" : "#ef4444"}}>
                              {m.change_24h >= 0 ? "+" : ""}{m.change_24h?.toFixed(2)}%
                            </td>
                            <td style={{color: m.change_7d >= 0 ? "#10b981" : "#ef4444"}}>
                              {m.change_7d >= 0 ? "+" : ""}{m.change_7d?.toFixed(2)}%
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </div>

          {/* DESTRA: output agente */}
          <div style={S.runCol}>
            <h3 style={S.colTitle}>Output dell'agente</h3>
            {current ? (
              <>
                <Section label="🔍 Lettura del contesto" body={current.reading} />
                <Section label="🧠 Ragionamento causale" body={current.reasoning} />
                <Section label="🎯 Decisione" decision={current.decision} />
              </>
            ) : (
              <div style={{ color: "#64748b", fontSize: 13 }}>In attesa…</div>
            )}
          </div>
        </div>

        {/* MULTI-STEP: storico decisioni precedenti collassabile */}
        {isMulti && stepData.length > 1 && (
          <PrevDecisionsHistory steps={stepData.slice(0, stepIdx)} />
        )}

        {/* Error from finalize (l'ultimo step puo' avere finalize_error) */}
        {current?.finalize_error && (
          <div style={S.warnBox}>
            ⚠ {current.finalize_error}
          </div>
        )}

        {error && <div style={S.errorBox}>Errore: {error}</div>}

        <div style={{ marginTop: 20, display: "flex", justifyContent: "flex-end", gap: 8 }}>
          {!running && stepIdx + 1 < totalSteps && (
            <button style={S.btnPrimary} onClick={() => runStep(runId, stepIdx + 1)}>
              Procedi al prossimo step <ChevronRight size={16} />
            </button>
          )}
        </div>
      </div>
    );
  };

  const renderDone = () => (
    <div style={{ textAlign: "center", padding: 40 }}>
      <h2 style={{ color: "#10b981" }}>✅ Scenario completato</h2>
      <p style={{ color: "#94a3b8" }}>Tutti gli step sono stati eseguiti.</p>
      <button style={S.btnPrimary} onClick={() => nav(`/simulator/result/${runId}`)}>
        Visualizza Risultato
      </button>
    </div>
  );

  return (
    <div>
      <h1 style={S.h1}>Scenario Runner</h1>
      {step === 1 && renderStep1()}
      {step === 2 && renderStep2()}
      {step === 3 && renderStep3()}
      {step === 4 && renderStep4()}
      {step === "running" && renderRunning()}
      {step === "done" && renderDone()}
      <style>{`@keyframes spin {from {transform: rotate(0)} to {transform: rotate(360deg)}}
              .spin {animation: spin 1s linear infinite;}`}</style>
    </div>
  );
}

function Section({ label, body, decision }) {
  return (
    <div style={S.outputSection}>
      <div style={S.outputLabel}>{label}</div>
      {decision ? (
        <div style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "4px 12px", fontSize: 13 }}>
          <strong>Azione:</strong><span style={{
            color: decision.action === "BUY" ? "#10b981"
                 : decision.action === "SELL" ? "#ef4444" : "#94a3b8",
            fontWeight: 700,
          }}>{decision.action || "—"}</span>
          <strong>Asset:</strong><span style={{ fontFamily: "monospace" }}>{decision.asset || "—"}</span>
          <strong>Conviction:</strong><span>{decision.conviction || "—"}</span>
          <strong>Orizzonte:</strong><span>{decision.horizon || "—"}</span>
          <strong>Rischio:</strong><span>{decision.risk || "—"}</span>
          {decision.stop_loss_target != null && (
            <>
              <strong>Stop-loss:</strong>
              <span style={{ color: "#fca5a5" }}>${decision.stop_loss_target}</span>
            </>
          )}
          {decision.take_profit_target != null && (
            <>
              <strong>Take-profit:</strong>
              <span style={{ color: "#86efac" }}>${decision.take_profit_target}</span>
            </>
          )}
        </div>
      ) : (
        <div style={S.outputBody}>{body || <em style={{color: "#64748b"}}>—</em>}</div>
      )}
    </div>
  );
}

/**
 * Banner che mostra cosa è cambiato dal turno precedente nel multi-step.
 * Usato sopra le 2 colonne per dare contesto chiaro all'utente.
 */
function UpdateBanner({ stepIdx, totalSteps, details }) {
  const prev = details?.prev_decision || {};
  const headlineChanges = details?.headline_changes || [];
  const priceChanges = (details?.price_changes || []).filter(p => p.delta_pct != null);
  const significantChanges = priceChanges.filter(p => Math.abs(p.delta_pct) >= 1);
  return (
    <div style={S.updateBanner}>
      <div style={S.updateBannerHeader}>
        🔄 Cambiamenti dal turno T+{stepIdx - 1} → T+{stepIdx}
        <span style={S.updateBannerStep}>{stepIdx + 1}/{totalSteps}</span>
      </div>

      {/* Decisione precedente */}
      <div style={S.updateSection}>
        <div style={S.updateSectionLabel}>Tua decisione precedente</div>
        <div style={S.prevDecisionRow}>
          <span style={{
            ...S.prevTag,
            background: prev.action === "BUY" ? "#064e3b"
                      : prev.action === "SELL" ? "#7f1d1d" : "#334155",
            color: prev.action === "BUY" ? "#86efac"
                 : prev.action === "SELL" ? "#fca5a5" : "#cbd5e1",
          }}>
            {prev.action || "—"}
          </span>
          <span style={{ fontFamily: "monospace", fontWeight: 700, color: "#e2e8f0" }}>
            {prev.asset || "—"}
          </span>
          <span style={{ fontSize: 11, color: "#94a3b8" }}>
            conv: <strong>{prev.conviction || "—"}</strong>
            {" · "}orizzonte: <strong>{prev.horizon || "—"}</strong>
          </span>
        </div>
        {details.prev_thesis && (
          <div style={S.prevThesis}>"{details.prev_thesis}"</div>
        )}
      </div>

      {/* Performance asset scelto */}
      {details.asset_performance_text && (
        <div style={S.updateSection}>
          <div style={S.updateSectionLabel}>Performance del tuo asset</div>
          <div style={S.perfText}>{details.asset_performance_text}</div>
        </div>
      )}

      {/* Headline nuove */}
      {headlineChanges.length > 0 && (
        <div style={S.updateSection}>
          <div style={S.updateSectionLabel}>Nuove headline ({headlineChanges.length})</div>
          <ul style={{ ...S.headlineList, color: "#fbbf24" }}>
            {headlineChanges.map((h, i) => <li key={i}>{h}</li>)}
          </ul>
        </div>
      )}

      {/* Movimenti significativi */}
      {significantChanges.length > 0 && (
        <div style={S.updateSection}>
          <div style={S.updateSectionLabel}>Movimenti prezzo significativi (≥1%)</div>
          <div style={S.movementsRow}>
            {significantChanges.map((p, i) => (
              <span key={i} style={{
                ...S.movementChip,
                color: p.delta_pct >= 0 ? "#10b981" : "#ef4444",
                background: p.delta_pct >= 0 ? "#064e3b30" : "#7f1d1d30",
                borderColor: p.delta_pct >= 0 ? "#065f46" : "#991b1b",
              }}>
                {p.ticker} {p.delta_pct >= 0 ? "+" : ""}{p.delta_pct.toFixed(2)}%
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * Storico decisioni precedenti per multi-step. Collassato di default.
 */
function PrevDecisionsHistory({ steps }) {
  const [expanded, setExpanded] = useState(false);
  if (!steps || steps.length === 0) return null;
  return (
    <div style={S.histBox}>
      <button onClick={() => setExpanded(!expanded)} style={S.histToggle}>
        {expanded ? "▼" : "▶"} Storico decisioni precedenti ({steps.length})
      </button>
      {expanded && (
        <div style={S.histContent}>
          {steps.map((s, i) => {
            const d = s.decision || {};
            return (
              <div key={i} style={S.histItem}>
                <div style={S.histItemHeader}>
                  <strong>T+{i}</strong>
                  <span style={{
                    color: d.action === "BUY" ? "#10b981"
                         : d.action === "SELL" ? "#ef4444" : "#94a3b8",
                    fontWeight: 700,
                  }}>{d.action || "—"}</span>
                  <span style={{ fontFamily: "monospace" }}>{d.asset || "—"}</span>
                  <span style={{ fontSize: 11, color: "#94a3b8" }}>
                    conv {d.conviction || "—"} · {d.horizon || "—"}
                  </span>
                </div>
                {s.reasoning && (
                  <div style={S.histReasoning}>{s.reasoning.slice(0, 250)}{s.reasoning.length > 250 ? "..." : ""}</div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

const S = {
  h1: { fontSize: "1.6rem", margin: "0 0 24px 0" },
  h2: { fontSize: "1.1rem", margin: "0 0 16px 0", color: "#cbd5e1" },
  note: { color: "#94a3b8", fontSize: 13, marginBottom: 16 },
  cardsGrid: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 20 },
  catCard: { background: "#111827", border: "2px solid #1f2937", padding: 16,
             borderRadius: 8, cursor: "pointer", textAlign: "left", color: "#e2e8f0",
             fontFamily: "inherit" },
  catCardSel: { borderColor: "#a78bfa", background: "#1e1b4b" },
  catName: { fontSize: 16, fontWeight: 600, marginBottom: 6 },
  catDesc: { fontSize: 12, color: "#94a3b8", lineHeight: 1.4 },
  catCount: { fontSize: 11, color: "#64748b", marginTop: 8 },
  typeCard: { flex: 1, background: "#111827", border: "2px solid #1f2937", padding: 14,
              borderRadius: 8, cursor: "pointer", textAlign: "left", color: "#e2e8f0", fontFamily: "inherit" },
  typeCardSel: { borderColor: "#a78bfa", background: "#1e1b4b" },
  select: { background: "#0a0e1a", color: "#e2e8f0", border: "1px solid #334155",
            padding: "4px 8px", borderRadius: 4, fontSize: 13, marginLeft: 8 },
  scenarioRow: { background: "#111827", border: "2px solid #1f2937", padding: 12,
                 borderRadius: 6, cursor: "pointer", textAlign: "left", color: "#e2e8f0",
                 fontFamily: "inherit" },
  scenarioRowSel: { borderColor: "#a78bfa", background: "#1e1b4b" },
  summaryBox: { background: "#111827", border: "1px solid #1f2937", padding: 16,
                borderRadius: 8, marginBottom: 16, lineHeight: 1.7, fontSize: 14 },
  btnPrimary: { background: "#a78bfa", color: "#0a0e1a", border: 0, padding: "10px 18px",
                borderRadius: 6, fontWeight: 600, cursor: "pointer",
                display: "inline-flex", alignItems: "center", gap: 6, fontSize: 14 },
  btnSec: { background: "transparent", color: "#cbd5e1", border: "1px solid #334155",
            padding: "10px 16px", borderRadius: 6, cursor: "pointer", fontSize: 14 },
  runHeader: { display: "flex", justifyContent: "space-between", alignItems: "center" },
  runStatus: { color: "#a78bfa", fontSize: 13, display: "inline-flex", alignItems: "center", gap: 6 },
  runColumns: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 },
  runCol: { background: "#111827", border: "1px solid #1f2937", padding: 16, borderRadius: 8 },
  colTitle: { fontSize: 14, fontWeight: 600, color: "#cbd5e1", marginTop: 0, marginBottom: 12 },
  contextBlock: { marginBottom: 14, background: "#0f172a", padding: 12, borderRadius: 6 },
  headlineList: { margin: "8px 0", paddingLeft: 20, fontSize: 13, color: "#cbd5e1", lineHeight: 1.5 },
  priceTable: { width: "100%", marginTop: 8, fontSize: 12, borderCollapse: "collapse" },
  outputSection: { marginBottom: 14, paddingBottom: 12, borderBottom: "1px solid #1f2937" },
  outputLabel: { fontSize: 12, color: "#a78bfa", fontWeight: 600, marginBottom: 6 },
  outputBody: { fontSize: 13, color: "#cbd5e1", lineHeight: 1.5, whiteSpace: "pre-wrap" },
  errorBox: { background: "#7f1d1d", color: "#fef2f2", padding: 12, borderRadius: 6,
              marginTop: 12, fontSize: 13 },
  warnBox: { background: "#3f1d0f", color: "#fed7aa", padding: 12, borderRadius: 6,
             marginTop: 12, fontSize: 13, border: "1px solid #7c2d12" },

  // Mode chip (single-step / multi-step)
  modeChip: {
    marginLeft: 12, padding: "3px 10px", borderRadius: 12,
    background: "#1e1b4b", color: "#a78bfa",
    fontSize: 10, fontWeight: 700, letterSpacing: "0.05em",
    textTransform: "uppercase", verticalAlign: "middle",
  },

  // Update banner (multi-step transitions)
  updateBanner: {
    background: "linear-gradient(135deg, #0f1729 0%, #1e1b4b 100%)",
    border: "1px solid #4c1d95", padding: 16, borderRadius: 10,
    marginBottom: 16,
  },
  updateBannerHeader: {
    fontSize: 14, fontWeight: 700, color: "#e9d5ff",
    display: "flex", justifyContent: "space-between", alignItems: "center",
    paddingBottom: 12, borderBottom: "1px solid #4c1d95", marginBottom: 12,
  },
  updateBannerStep: {
    fontSize: 11, fontWeight: 600, color: "#a78bfa",
    background: "#1e1b4b", padding: "3px 8px", borderRadius: 4,
  },
  updateSection: { marginBottom: 12 },
  updateSectionLabel: {
    fontSize: 10, fontWeight: 600, color: "#a78bfa",
    textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 6,
  },
  prevDecisionRow: {
    display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap",
    marginBottom: 4,
  },
  prevTag: {
    padding: "3px 10px", borderRadius: 4,
    fontSize: 11, fontWeight: 700, letterSpacing: "0.05em",
  },
  prevThesis: {
    fontSize: 12, color: "#cbd5e1", lineHeight: 1.5,
    background: "#0a0e1a", padding: "8px 10px", borderRadius: 4,
    fontStyle: "italic", border: "1px solid #1f2937", marginTop: 6,
  },
  perfText: {
    fontSize: 13, color: "#cbd5e1",
    background: "#0a0e1a", padding: "8px 10px", borderRadius: 4,
    border: "1px solid #1f2937",
  },
  movementsRow: { display: "flex", gap: 6, flexWrap: "wrap" },
  movementChip: {
    padding: "3px 9px", borderRadius: 4,
    fontSize: 11, fontWeight: 600, fontFamily: "monospace",
    border: "1px solid",
  },
  newTag: {
    display: "inline-block", padding: "1px 5px",
    background: "#7c2d12", color: "#fed7aa",
    borderRadius: 3, fontSize: 9, fontWeight: 700,
    marginRight: 6, verticalAlign: "middle",
  },

  // Storico decisioni precedenti (collassabile)
  histBox: {
    marginTop: 16, background: "#0f172a", border: "1px solid #1f2937",
    borderRadius: 6,
  },
  histToggle: {
    width: "100%", textAlign: "left", padding: "10px 14px",
    background: "transparent", border: 0, color: "#cbd5e1",
    cursor: "pointer", fontSize: 13, fontFamily: "inherit", fontWeight: 600,
  },
  histContent: { padding: "0 14px 14px 14px" },
  histItem: {
    background: "#111827", padding: 10, borderRadius: 6,
    marginBottom: 8, border: "1px solid #1f2937",
  },
  histItemHeader: {
    display: "flex", gap: 10, alignItems: "center",
    marginBottom: 4, fontSize: 13,
  },
  histReasoning: {
    fontSize: 12, color: "#94a3b8", lineHeight: 1.5,
    fontStyle: "italic",
  },
};
