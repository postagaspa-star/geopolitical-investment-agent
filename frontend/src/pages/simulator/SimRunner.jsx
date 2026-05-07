import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Play, ChevronRight, Loader2, TrendingUp, TrendingDown,
  Wallet, Target, AlertCircle, Trophy, ChevronLeft,
} from "lucide-react";

const API = window.location.origin;

const CATEGORIES = [
  { key: "normale", name: "Normale", desc: "Mercato in condizioni standard" },
  { key: "geopolitico", name: "Geopolitico", desc: "Crisi, conflitti, sanzioni, elezioni" },
  { key: "macro", name: "Macro", desc: "FOMC, dati CPI/NFP, banche centrali" },
  { key: "crash_rally", name: "Crash/Rally", desc: "Shock acuti: crash, rally, panic" },
];

/**
 * Simulator V2 — partita a turni con portafoglio multi-asset.
 *
 * Flusso:
 *   1. Selezione categoria + numero turni (3-5) + scenario specifico
 *   2. Loop turni: leggo headline + prezzi reali, AI decide trade,
 *      portafoglio si aggiorna, vado al turno successivo
 *   3. Debrief finale: P&L, vs benchmark SPY, cosa è successo davvero
 *
 * Stateless: il browser tiene tutto (scenario, portfolio, history) e lo
 * rispedisce al server per ogni step. Niente stato lato server.
 */
export default function SimRunner() {
  const nav = useNavigate();

  // ── Setup state ────────────────────────────────────────────────────
  const [phase, setPhase] = useState("config1");
  // config1 → config2 → config3 → playing → finalizing → done
  const [category, setCategory] = useState(null);
  const [numSteps, setNumSteps] = useState(4);
  const [scenarios, setScenarios] = useState([]);
  const [scenarioId, setScenarioId] = useState(null);
  const [scenarioCounts, setScenarioCounts] = useState({});

  // ── Game state (TUTTO LATO CLIENT) ─────────────────────────────────
  const [scenario, setScenario] = useState(null);          // dal /v2/start
  const [portfolio, setPortfolio] = useState(null);
  const [history, setHistory] = useState([]);              // [{step_index, prices, ai_*, applied_trades, ...}]
  const [currentStepIdx, setCurrentStepIdx] = useState(0);
  const [t0Prices, setT0Prices] = useState({});

  // ── Final state ────────────────────────────────────────────────────
  const [finalResult, setFinalResult] = useState(null);

  // ── UI state ───────────────────────────────────────────────────────
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  // Conta scenari per categoria
  useEffect(() => {
    fetch(`${API}/api/simulator/scenarios/counts`)
      .then(r => r.ok ? r.json() : {}).then(setScenarioCounts).catch(() => {});
  }, []);

  // Quando categoria cambia, carica scenari
  useEffect(() => {
    if (!category) return;
    fetch(`${API}/api/simulator/scenarios?category=${category}`)
      .then(r => r.ok ? r.json() : [])
      .then(d => setScenarios(Array.isArray(d) ? d : []))
      .catch(() => setScenarios([]));
  }, [category]);

  // ════════════════════════════════════════════════════════════════════
  // ACTIONS
  // ════════════════════════════════════════════════════════════════════

  async function startGame() {
    setBusy(true); setError(null);
    try {
      const resp = await fetch(`${API}/api/simulator/v2/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          category, num_steps: numSteps,
          scenario_id: scenarioId || null,
          initial_capital: 100000,
        }),
      });
      if (!resp.ok) {
        const errBody = await resp.json().catch(() => ({}));
        throw new Error(errBody.error || `HTTP ${resp.status}`);
      }
      const data = await resp.json();
      setScenario(data.scenario);
      setPortfolio(data.portfolio);
      setT0Prices(data.t0_prices || {});
      setHistory([]);
      setCurrentStepIdx(0);
      setPhase("playing");
    } catch (e) {
      setError(`Errore start: ${e.message}`);
    } finally {
      setBusy(false);
    }
  }

  async function executeNextStep() {
    if (!scenario || !portfolio) return;
    setBusy(true); setError(null);
    try {
      const resp = await fetch(`${API}/api/simulator/v2/step`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          scenario, portfolio, history,
          step_index: currentStepIdx,
        }),
      });
      if (!resp.ok) {
        const errBody = await resp.json().catch(() => ({}));
        throw new Error(errBody.error || `HTTP ${resp.status}`);
      }
      const stepData = await resp.json();
      // Aggiorna stato
      setHistory([...history, stepData]);
      setPortfolio(stepData.new_portfolio);
      const nextIdx = currentStepIdx + 1;
      if (nextIdx >= scenario.num_steps) {
        // Fine partita: chiama finalize
        setPhase("finalizing");
        await finalizeGame(stepData.new_portfolio, [...history, stepData]);
      } else {
        setCurrentStepIdx(nextIdx);
      }
    } catch (e) {
      setError(`Errore step ${currentStepIdx + 1}: ${e.message}`);
    } finally {
      setBusy(false);
    }
  }

  async function finalizeGame(finalPortfolio, finalHistory) {
    try {
      const resp = await fetch(`${API}/api/simulator/v2/finalize`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          scenario,
          portfolio: finalPortfolio,
          history: finalHistory,
          persist: true,
        }),
      });
      if (!resp.ok) {
        const errBody = await resp.json().catch(() => ({}));
        throw new Error(errBody.error || `HTTP ${resp.status}`);
      }
      const data = await resp.json();
      setFinalResult(data);
      setPhase("done");
    } catch (e) {
      setError(`Errore finalize: ${e.message}`);
      setPhase("playing");   // torna a playing per consentire retry
    }
  }

  function resetGame() {
    setPhase("config1");
    setScenario(null); setPortfolio(null); setHistory([]);
    setCurrentStepIdx(0); setFinalResult(null); setError(null);
    setScenarioId(null); setT0Prices({});
  }

  // ════════════════════════════════════════════════════════════════════
  // RENDER
  // ════════════════════════════════════════════════════════════════════

  return (
    <div style={S.root}>
      <header style={S.header}>
        <h1 style={S.h1}>Simulator</h1>
        <span style={S.badge}>Stateless · Prezzi reali Polygon</span>
      </header>

      {error && (
        <div style={S.errorBox}>
          <AlertCircle size={18} />
          <span>{error}</span>
          <button style={S.btnGhost} onClick={() => setError(null)}>×</button>
        </div>
      )}

      {phase === "config1" && (
        <ConfigStep1
          category={category} setCategory={setCategory}
          counts={scenarioCounts}
          onNext={() => setPhase("config2")}
        />
      )}

      {phase === "config2" && (
        <ConfigStep2
          numSteps={numSteps} setNumSteps={setNumSteps}
          onBack={() => setPhase("config1")}
          onNext={() => setPhase("config3")}
        />
      )}

      {phase === "config3" && (
        <ConfigStep3
          scenarios={scenarios} scenarioId={scenarioId}
          setScenarioId={setScenarioId}
          onBack={() => setPhase("config2")}
          onStart={startGame}
          busy={busy}
        />
      )}

      {phase === "playing" && scenario && portfolio && (
        <PlayingView
          scenario={scenario} portfolio={portfolio} history={history}
          currentStepIdx={currentStepIdx}
          t0Prices={t0Prices}
          onNextStep={executeNextStep}
          busy={busy}
        />
      )}

      {phase === "finalizing" && (
        <div style={S.center}>
          <Loader2 size={40} style={{ animation: "spin 1s linear infinite" }} />
          <h2>Calcolo P&L finale e debrief...</h2>
          <p style={{ color: "#94a3b8" }}>
            Sto fetchando prezzi finali Polygon e generando l'analisi
          </p>
        </div>
      )}

      {phase === "done" && finalResult && (
        <DoneView
          finalResult={finalResult} scenario={scenario}
          history={history}
          onRestart={resetGame}
          onViewHistory={() => nav("/simulator/history")}
        />
      )}
    </div>
  );
}

// ════════════════════════════════════════════════════════════════════
// CONFIG STEPS
// ════════════════════════════════════════════════════════════════════

function ConfigStep1({ category, setCategory, counts, onNext }) {
  return (
    <div>
      <h2 style={S.h2}>1 · Categoria scenario</h2>
      <p style={S.subtitle}>
        Tutti gli scenari sono basati su EVENTI STORICI REALI.
        Verranno usati prezzi reali di mercato di quei giorni (Polygon.io).
      </p>
      <div style={S.cardsGrid}>
        {CATEGORIES.map(c => (
          <button key={c.key}
                  style={{ ...S.catCard, ...(category === c.key ? S.catCardSel : {}) }}
                  onClick={() => setCategory(c.key)}>
            <div style={S.catName}>{c.name}</div>
            <div style={S.catDesc}>{c.desc}</div>
            <div style={S.catCount}>{counts[c.key] ?? "?"} scenari disponibili</div>
          </button>
        ))}
      </div>
      <button style={S.btnPrimary} disabled={!category} onClick={onNext}>
        Avanti <ChevronRight size={16} />
      </button>
    </div>
  );
}

function ConfigStep2({ numSteps, setNumSteps, onBack, onNext }) {
  return (
    <div>
      <h2 style={S.h2}>2 · Numero di turni</h2>
      <p style={S.subtitle}>
        Ogni turno = <strong>1 settimana</strong> di tempo simulato. L'AI può
        riconsiderare la posizione solo all'inizio del turno successivo.
      </p>
      <div style={{ display: "flex", gap: 12, marginBottom: 24 }}>
        {[3, 4, 5].map(n => (
          <button key={n}
                  style={{ ...S.numBtn, ...(numSteps === n ? S.numBtnSel : {}) }}
                  onClick={() => setNumSteps(n)}>
            <div style={{ fontSize: 32, fontWeight: 800 }}>{n}</div>
            <div style={{ fontSize: 12, color: "#94a3b8" }}>
              {n} turni · {n} settimane
            </div>
          </button>
        ))}
      </div>
      <div style={{ display: "flex", gap: 8 }}>
        <button style={S.btnGhost} onClick={onBack}>
          <ChevronLeft size={16} /> Indietro
        </button>
        <button style={S.btnPrimary} onClick={onNext}>
          Avanti <ChevronRight size={16} />
        </button>
      </div>
    </div>
  );
}

function ConfigStep3({ scenarios, scenarioId, setScenarioId, onBack, onStart, busy }) {
  return (
    <div>
      <h2 style={S.h2}>3 · Scegli lo scenario specifico</h2>
      <p style={S.subtitle}>
        Selezionane uno o lascia <em>random</em> per scelta automatica.
      </p>
      <div style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 24,
                    maxHeight: 400, overflow: "auto" }}>
        <button style={{ ...S.scenCard, ...(scenarioId === null ? S.scenCardSel : {}) }}
                onClick={() => setScenarioId(null)}>
          <strong>🎲 Random</strong>
          <div style={S.scenMeta}>Scelta casuale tra gli scenari della categoria</div>
        </button>
        {scenarios.map(s => (
          <button key={s.id}
                  style={{ ...S.scenCard, ...(scenarioId === s.id ? S.scenCardSel : {}) }}
                  onClick={() => setScenarioId(s.id)}>
            <strong>{s.title}</strong>
            <div style={S.scenMeta}>
              {s.brief} · Periodo: {s.period_start} → {s.period_end}
            </div>
          </button>
        ))}
      </div>
      <div style={{ display: "flex", gap: 8 }}>
        <button style={S.btnGhost} onClick={onBack} disabled={busy}>
          <ChevronLeft size={16} /> Indietro
        </button>
        <button style={S.btnPrimary} onClick={onStart} disabled={busy}>
          {busy ? <><Loader2 size={16} style={{ animation: "spin 1s linear infinite" }} /> Inizio...</>
                : <><Play size={16} /> Inizia partita</>}
        </button>
      </div>
    </div>
  );
}

// ════════════════════════════════════════════════════════════════════
// PLAYING VIEW
// ════════════════════════════════════════════════════════════════════

function PlayingView({ scenario, portfolio, history, currentStepIdx, t0Prices, onNextStep, busy }) {
  const lastStep = history[history.length - 1] || null;
  const currentDate = scenario.step_dates?.[currentStepIdx] || "?";
  const currentPrices = lastStep ? lastStep.prices : t0Prices;
  const valuation = computeValuation(portfolio, currentPrices);

  return (
    <div>
      {/* Header partita */}
      <div style={S.gameHeader}>
        <div>
          <div style={S.scenTitle}>{scenario.title}</div>
          <div style={S.scenSubtitle}>{scenario.brief}</div>
        </div>
        <div style={S.turnBadge}>
          Turno {currentStepIdx + 1} / {scenario.num_steps}
          <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 2 }}>
            {currentDate}
          </div>
        </div>
      </div>

      {/* Portafoglio attuale */}
      <PortfolioPanel valuation={valuation} prices={currentPrices} />

      {/* Storia precedente */}
      {history.length > 0 && (
        <details style={S.detailsBox} open>
          <summary style={S.summaryHead}>
            📜 Storia turni precedenti ({history.length})
          </summary>
          <div style={{ marginTop: 12 }}>
            {history.map((h, i) => (
              <StepCard key={i} step={h} />
            ))}
          </div>
        </details>
      )}

      {/* Action: prossimo turno */}
      <div style={S.nextActionBox}>
        <div>
          <strong>Pronto per il turno {currentStepIdx + 1}?</strong>
          <div style={{ fontSize: 13, color: "#94a3b8", marginTop: 4 }}>
            L'AI leggerà le headline della settimana {currentDate} e deciderà
            come allocare il portafoglio per i prossimi 7 giorni.
          </div>
        </div>
        <button style={S.btnPrimary} onClick={onNextStep} disabled={busy}>
          {busy ? <><Loader2 size={16} style={{ animation: "spin 1s linear infinite" }} /> AI sta ragionando...</>
                : <><Play size={16} /> Esegui turno {currentStepIdx + 1}</>}
        </button>
      </div>
    </div>
  );
}

function PortfolioPanel({ valuation, prices }) {
  const pnlColor = valuation.total_pnl >= 0 ? "#10b981" : "#ef4444";
  return (
    <div style={S.portfolioBox}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <Wallet size={18} color="#a78bfa" />
          <strong>Il tuo portafoglio</strong>
        </div>
        <div style={{ textAlign: "right" }}>
          <div style={{ fontSize: 22, fontWeight: 700 }}>
            ${valuation.total_value.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}
          </div>
          <div style={{ fontSize: 13, color: pnlColor }}>
            {valuation.total_pnl >= 0 ? "+" : ""}{valuation.total_pnl.toFixed(2)}$
            ({valuation.total_pnl_pct >= 0 ? "+" : ""}{valuation.total_pnl_pct.toFixed(2)}%)
          </div>
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginTop: 12 }}>
        <div style={S.statBox}>
          <div style={S.statLabel}>Cash</div>
          <div style={S.statVal}>${valuation.cash.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}</div>
        </div>
        <div style={S.statBox}>
          <div style={S.statLabel}>Posizioni</div>
          <div style={S.statVal}>${valuation.positions_value.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}</div>
        </div>
      </div>

      {valuation.positions.length > 0 ? (
        <div style={{ marginTop: 12 }}>
          <div style={{ fontSize: 12, color: "#94a3b8", marginBottom: 6 }}>
            Posizioni aperte ({valuation.positions.length})
          </div>
          {valuation.positions.map((p, i) => (
            <div key={i} style={S.posRow}>
              <span style={{ ...S.sideTag, background: p.side === "long" ? "#065f46" : "#7c2d12" }}>
                {p.side === "long" ? "LONG" : "SHORT"}
              </span>
              <span style={{ fontWeight: 600 }}>{p.asset}</span>
              <span style={{ color: "#94a3b8", fontSize: 12 }}>
                {p.quantity.toFixed(2)} @ ${p.avg_entry_price.toFixed(2)}
              </span>
              <span style={{ marginLeft: "auto", color: p.unrealized_pnl >= 0 ? "#10b981" : "#ef4444",
                             fontSize: 13, fontWeight: 600 }}>
                {p.unrealized_pnl >= 0 ? "+" : ""}{p.unrealized_pnl_pct.toFixed(2)}%
              </span>
            </div>
          ))}
        </div>
      ) : (
        <div style={{ marginTop: 12, fontSize: 13, color: "#64748b", fontStyle: "italic" }}>
          Nessuna posizione aperta. Tutto in cash.
        </div>
      )}
    </div>
  );
}

function StepCard({ step }) {
  return (
    <div style={S.stepCard}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6 }}>
        <strong>Turno {step.step_index + 1}</strong>
        <span style={{ color: "#94a3b8", fontSize: 12 }}>{step.step_date}</span>
      </div>
      {step.headlines && step.headlines.length > 0 && (
        <div style={{ fontSize: 12, color: "#cbd5e1", marginBottom: 8 }}>
          📰 {step.headlines.slice(0, 2).map(h => h).join(" · ")}
          {step.headlines.length > 2 && <span style={{ color: "#64748b" }}> +{step.headlines.length - 2} altre</span>}
        </div>
      )}
      {step.ai_reasoning && (
        <details style={{ marginBottom: 8 }}>
          <summary style={{ cursor: "pointer", color: "#a78bfa", fontSize: 13 }}>
            💡 Ragionamento AI
          </summary>
          <div style={{ fontSize: 13, color: "#e2e8f0", lineHeight: 1.5, marginTop: 6,
                        padding: "8px 12px", background: "#0f172a", borderRadius: 6,
                        whiteSpace: "pre-wrap" }}>
            {step.ai_reasoning}
          </div>
        </details>
      )}
      {step.applied_trades && step.applied_trades.length > 0 ? (
        <div>
          {step.applied_trades.map((t, i) => (
            <div key={i} style={S.tradeRow}>
              <span style={{
                ...S.actionTag,
                background: t.action === "BUY" ? "#10b981" : "#ef4444"
              }}>
                {t.action}
              </span>
              <span style={{ fontWeight: 600 }}>{t.asset}</span>
              <span style={{ color: "#94a3b8", fontSize: 12 }}>
                {t.allocation_pct?.toFixed(0)}%
              </span>
              {t.thesis && (
                <span style={{ color: "#cbd5e1", fontSize: 12, fontStyle: "italic",
                               marginLeft: 8, flex: 1 }}>
                  {t.thesis.slice(0, 100)}{t.thesis.length > 100 ? "…" : ""}
                </span>
              )}
            </div>
          ))}
        </div>
      ) : (
        <div style={{ fontSize: 12, color: "#64748b", fontStyle: "italic" }}>
          Nessuna azione, posizioni invariate
        </div>
      )}
    </div>
  );
}

// ════════════════════════════════════════════════════════════════════
// DONE VIEW
// ════════════════════════════════════════════════════════════════════

function DoneView({ finalResult, scenario, history, onRestart, onViewHistory }) {
  const v = finalResult.final_valuation;
  const outcomeColor = {green: "#10b981", yellow: "#f59e0b", red: "#ef4444"}[finalResult.outcome] || "#94a3b8";
  const bench = finalResult.benchmark_spy_pnl_pct;

  return (
    <div>
      <div style={S.gameHeader}>
        <div>
          <div style={S.scenTitle}>🏁 Partita conclusa</div>
          <div style={S.scenSubtitle}>{scenario.title}</div>
        </div>
        <div style={{ ...S.turnBadge, background: outcomeColor + "22", borderColor: outcomeColor }}>
          {finalResult.outcome === "green" ? "✓ Verde" : finalResult.outcome === "red" ? "✗ Rosso" : "Giallo"}
        </div>
      </div>

      <div style={S.finalGrid}>
        <div style={S.finalBox}>
          <div style={S.statLabel}>Valore finale</div>
          <div style={{ fontSize: 28, fontWeight: 800 }}>
            ${v.total_value.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}
          </div>
          <div style={{ fontSize: 14, color: v.total_pnl >= 0 ? "#10b981" : "#ef4444" }}>
            {v.total_pnl >= 0 ? "+" : ""}${v.total_pnl.toFixed(2)} ({v.total_pnl_pct >= 0 ? "+" : ""}{v.total_pnl_pct.toFixed(2)}%)
          </div>
        </div>

        <div style={S.finalBox}>
          <div style={S.statLabel}>Benchmark SPY (buy & hold)</div>
          <div style={{ fontSize: 28, fontWeight: 800 }}>
            {bench !== null && bench !== undefined ? `${bench >= 0 ? "+" : ""}${bench.toFixed(2)}%` : "n/d"}
          </div>
          {bench !== null && bench !== undefined && (
            <div style={{ fontSize: 14, color: (v.total_pnl_pct - bench) >= 0 ? "#10b981" : "#ef4444" }}>
              Delta: {(v.total_pnl_pct - bench) >= 0 ? "+" : ""}{(v.total_pnl_pct - bench).toFixed(2)}%
            </div>
          )}
        </div>
      </div>

      {finalResult.debrief && (
        <div style={S.debriefBox}>
          <h3 style={{ marginTop: 0, color: "#a78bfa" }}>
            <Trophy size={18} style={{ verticalAlign: "middle" }} /> Debrief
          </h3>
          <div style={{ whiteSpace: "pre-wrap", lineHeight: 1.6 }}>
            {finalResult.debrief}
          </div>
        </div>
      )}

      {finalResult.description_reveal && (
        <div style={S.revealBox}>
          <h3 style={{ marginTop: 0 }}>📖 Cosa è successo davvero</h3>
          <div style={{ whiteSpace: "pre-wrap", lineHeight: 1.6 }}>
            {finalResult.description_reveal}
          </div>
        </div>
      )}

      <details style={{ marginTop: 24 }}>
        <summary style={S.summaryHead}>📜 Storia completa dei turni</summary>
        <div style={{ marginTop: 12 }}>
          {history.map((h, i) => <StepCard key={i} step={h} />)}
        </div>
      </details>

      <div style={{ display: "flex", gap: 8, marginTop: 24 }}>
        <button style={S.btnPrimary} onClick={onRestart}>
          <Play size={16} /> Nuova partita
        </button>
        <button style={S.btnGhost} onClick={onViewHistory}>
          Vai allo storico
        </button>
      </div>
    </div>
  );
}

// ════════════════════════════════════════════════════════════════════
// HELPERS — replica logica server-side per visualizzazione live
// ════════════════════════════════════════════════════════════════════

function computeValuation(portfolio, prices) {
  const cash = portfolio.cash || 0;
  let positionsValue = 0;
  const positions = (portfolio.positions || []).map(p => {
    const cur = prices[p.asset] || p.avg_entry_price || 0;
    const qty = p.quantity || 0;
    const entry = p.avg_entry_price || 0;
    let mktValue, pnl;
    if (p.side === "long") {
      mktValue = qty * cur;
      pnl = (cur - entry) * qty;
      positionsValue += mktValue;
    } else {
      mktValue = qty * cur;
      pnl = (entry - cur) * qty;
      positionsValue -= mktValue;
    }
    const pnlPct = (qty && entry) ? (pnl / (qty * entry) * 100) : 0;
    return {
      ...p, current_price: cur, market_value: mktValue,
      unrealized_pnl: pnl, unrealized_pnl_pct: pnlPct,
    };
  });
  const totalValue = cash + positionsValue;
  const initial = portfolio.initial_capital || totalValue;
  const totalPnl = totalValue - initial;
  return {
    total_value: totalValue,
    cash, positions_value: positionsValue,
    total_pnl: totalPnl,
    total_pnl_pct: initial ? (totalPnl / initial * 100) : 0,
    initial_capital: initial,
    positions,
  };
}

// ════════════════════════════════════════════════════════════════════
// STYLES
// ════════════════════════════════════════════════════════════════════

const S = {
  root: { maxWidth: 1100, margin: "0 auto" },
  header: { display: "flex", alignItems: "center", gap: 12, marginBottom: 24 },
  h1: { fontSize: "1.8rem", margin: 0, color: "#e2e8f0" },
  badge: { fontSize: 11, padding: "4px 10px", background: "#1e293b",
           borderRadius: 12, color: "#a78bfa" },
  h2: { color: "#cbd5e1", marginTop: 0 },
  subtitle: { color: "#94a3b8", marginBottom: 16, fontSize: 14 },
  errorBox: { display: "flex", alignItems: "center", gap: 8, padding: "10px 14px",
              background: "#7f1d1d", border: "1px solid #b91c1c", borderRadius: 6,
              marginBottom: 16, color: "#fee2e2" },
  cardsGrid: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 24 },
  catCard: { padding: 16, background: "#1e293b", border: "2px solid transparent",
             borderRadius: 8, cursor: "pointer", textAlign: "left", color: "#e2e8f0" },
  catCardSel: { borderColor: "#a78bfa", background: "#1e1b3a" },
  catName: { fontSize: 16, fontWeight: 700, marginBottom: 4 },
  catDesc: { fontSize: 13, color: "#94a3b8", marginBottom: 6 },
  catCount: { fontSize: 11, color: "#64748b" },
  numBtn: { flex: 1, padding: 24, background: "#1e293b", border: "2px solid transparent",
            borderRadius: 8, cursor: "pointer", color: "#e2e8f0" },
  numBtnSel: { borderColor: "#a78bfa", background: "#1e1b3a" },
  scenCard: { padding: 12, background: "#1e293b", border: "2px solid transparent",
              borderRadius: 8, cursor: "pointer", textAlign: "left", color: "#e2e8f0" },
  scenCardSel: { borderColor: "#a78bfa", background: "#1e1b3a" },
  scenMeta: { fontSize: 12, color: "#94a3b8", marginTop: 4 },
  btnPrimary: { display: "inline-flex", alignItems: "center", gap: 6, padding: "10px 18px",
                background: "#a78bfa", color: "#0a0e1a", border: "none", borderRadius: 6,
                fontWeight: 600, cursor: "pointer", fontSize: 14 },
  btnGhost: { display: "inline-flex", alignItems: "center", gap: 6, padding: "8px 14px",
              background: "transparent", color: "#cbd5e1", border: "1px solid #374151",
              borderRadius: 6, cursor: "pointer", fontSize: 13 },
  center: { display: "flex", flexDirection: "column", alignItems: "center", gap: 12,
            padding: 60, color: "#94a3b8" },
  gameHeader: { display: "flex", justifyContent: "space-between", alignItems: "flex-start",
                marginBottom: 16, padding: "12px 16px", background: "#1e293b", borderRadius: 8 },
  scenTitle: { fontSize: 18, fontWeight: 700, color: "#e2e8f0" },
  scenSubtitle: { fontSize: 13, color: "#94a3b8", marginTop: 4 },
  turnBadge: { padding: "8px 14px", background: "#1e1b3a", border: "1px solid #6d28d9",
               borderRadius: 8, color: "#a78bfa", fontWeight: 700, textAlign: "center" },
  portfolioBox: { padding: 16, background: "#0f172a", border: "1px solid #1e293b",
                  borderRadius: 8, marginBottom: 16 },
  statBox: { padding: 10, background: "#0a0e1a", borderRadius: 6 },
  statLabel: { fontSize: 11, color: "#64748b", marginBottom: 4 },
  statVal: { fontSize: 16, fontWeight: 700, color: "#e2e8f0" },
  posRow: { display: "flex", alignItems: "center", gap: 8, padding: "6px 8px",
            background: "#0a0e1a", borderRadius: 4, marginBottom: 4 },
  sideTag: { padding: "2px 8px", borderRadius: 4, fontSize: 10, fontWeight: 700, color: "#fff" },
  detailsBox: { background: "#0f172a", border: "1px solid #1e293b", borderRadius: 8,
                padding: 12, marginBottom: 16 },
  summaryHead: { cursor: "pointer", fontWeight: 600, color: "#cbd5e1" },
  stepCard: { padding: 12, background: "#1e293b", borderRadius: 6, marginBottom: 8 },
  tradeRow: { display: "flex", alignItems: "center", gap: 8, padding: "4px 0" },
  actionTag: { padding: "2px 8px", borderRadius: 4, fontSize: 10, fontWeight: 700, color: "#fff" },
  nextActionBox: { display: "flex", justifyContent: "space-between", alignItems: "center",
                   gap: 16, padding: 16, background: "#1e1b3a", border: "1px solid #6d28d9",
                   borderRadius: 8, marginTop: 16 },
  finalGrid: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 16 },
  finalBox: { padding: 16, background: "#0f172a", border: "1px solid #1e293b",
              borderRadius: 8 },
  debriefBox: { padding: 16, background: "#1e1b3a", border: "1px solid #6d28d9",
                borderRadius: 8, marginBottom: 16, color: "#e2e8f0" },
  revealBox: { padding: 16, background: "#0f172a", border: "1px solid #1e293b",
               borderRadius: 8, marginBottom: 16, color: "#e2e8f0" },
};
