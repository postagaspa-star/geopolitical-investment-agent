import { useEffect, useState, useRef } from "react";
import { useNavigate } from "react-router-dom";
import {
  Play, ChevronRight, Loader2, Wallet, AlertCircle, Trophy,
  ChevronLeft, MessageCircle, Send, TrendingUp, TrendingDown,
} from "lucide-react";

const API = window.location.origin;

const CATEGORIES = [
  { key: "normale", name: "Normale", desc: "Mercato in condizioni standard" },
  { key: "geopolitico", name: "Geopolitico", desc: "Crisi, conflitti, sanzioni, elezioni" },
  { key: "macro", name: "Macro", desc: "FOMC, dati CPI/NFP, banche centrali" },
  { key: "crash_rally", name: "Crash/Rally", desc: "Shock acuti: crash, rally, panic" },
];

export default function SimRunner() {
  const nav = useNavigate();

  // ── Setup state ────────────────────────────────────────────────
  const [phase, setPhase] = useState("config1");
  const [category, setCategory] = useState(null);
  const [numSteps, setNumSteps] = useState(4);
  const [scenarios, setScenarios] = useState([]);
  const [scenarioId, setScenarioId] = useState(null);
  const [scenarioCounts, setScenarioCounts] = useState({});

  // ── Game state ─────────────────────────────────────────────────
  const [scenario, setScenario] = useState(null);
  const [portfolio, setPortfolio] = useState(null);
  const [history, setHistory] = useState([]);
  const [currentStepIdx, setCurrentStepIdx] = useState(0);
  const [t0Prices, setT0Prices] = useState({});

  const [finalResult, setFinalResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    fetch(`${API}/api/simulator/scenarios/counts`)
      .then(r => r.ok ? r.json() : {}).then(setScenarioCounts).catch(() => {});
  }, []);

  useEffect(() => {
    if (!category) return;
    fetch(`${API}/api/simulator/scenarios?category=${category}`)
      .then(r => r.ok ? r.json() : [])
      .then(d => setScenarios(Array.isArray(d) ? d : []))
      .catch(() => setScenarios([]));
  }, [category]);

  async function startGame() {
    setBusy(true); setError(null);
    try {
      const resp = await fetch(`${API}/api/simulator/v2/start`, {
        method: "POST", headers: { "Content-Type": "application/json" },
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
      setHistory([]); setCurrentStepIdx(0);
      setPhase("playing");
    } catch (e) { setError(`Errore start: ${e.message}`); }
    finally { setBusy(false); }
  }

  async function executeNextStep() {
    if (!scenario || !portfolio) return;
    setBusy(true); setError(null);
    try {
      const resp = await fetch(`${API}/api/simulator/v2/step`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          scenario, portfolio, history, step_index: currentStepIdx,
        }),
      });
      if (!resp.ok) {
        const errBody = await resp.json().catch(() => ({}));
        throw new Error(errBody.error || `HTTP ${resp.status}`);
      }
      const stepData = await resp.json();
      const newHistory = [...history, stepData];
      setHistory(newHistory); setPortfolio(stepData.new_portfolio);
      const nextIdx = currentStepIdx + 1;
      if (nextIdx >= scenario.num_steps) {
        setPhase("finalizing");
        await finalizeGame(stepData.new_portfolio, newHistory);
      } else {
        setCurrentStepIdx(nextIdx);
      }
    } catch (e) { setError(`Errore step ${currentStepIdx + 1}: ${e.message}`); }
    finally { setBusy(false); }
  }

  async function finalizeGame(finalPortfolio, finalHistory) {
    try {
      const resp = await fetch(`${API}/api/simulator/v2/finalize`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          scenario, portfolio: finalPortfolio,
          history: finalHistory, persist: true,
        }),
      });
      if (!resp.ok) {
        const errBody = await resp.json().catch(() => ({}));
        throw new Error(errBody.error || `HTTP ${resp.status}`);
      }
      setFinalResult(await resp.json()); setPhase("done");
    } catch (e) {
      setError(`Errore finalize: ${e.message}`); setPhase("playing");
    }
  }

  function resetGame() {
    setPhase("config1"); setScenario(null); setPortfolio(null);
    setHistory([]); setCurrentStepIdx(0); setFinalResult(null);
    setError(null); setScenarioId(null); setT0Prices({});
  }

  return (
    <div style={S.root}>
      <header style={S.header}>
        <h1 style={S.h1}>Simulator</h1>
        <span style={S.badge}>Stateless · Prezzi reali Polygon</span>
      </header>

      {error && (
        <div style={S.errorBox}>
          <AlertCircle size={18} /><span>{error}</span>
          <button style={S.btnGhost} onClick={() => setError(null)}>×</button>
        </div>
      )}

      {phase === "config1" && (
        <ConfigStep1 category={category} setCategory={setCategory}
                     counts={scenarioCounts} onNext={() => setPhase("config2")} />
      )}
      {phase === "config2" && (
        <ConfigStep2 numSteps={numSteps} setNumSteps={setNumSteps}
                     onBack={() => setPhase("config1")} onNext={() => setPhase("config3")} />
      )}
      {phase === "config3" && (
        <ConfigStep3 scenarios={scenarios} scenarioId={scenarioId}
                     setScenarioId={setScenarioId}
                     onBack={() => setPhase("config2")}
                     onStart={startGame} busy={busy} />
      )}
      {phase === "playing" && scenario && portfolio && (
        <PlayingView scenario={scenario} portfolio={portfolio} history={history}
                     currentStepIdx={currentStepIdx} t0Prices={t0Prices}
                     onNextStep={executeNextStep} busy={busy} />
      )}
      {phase === "finalizing" && (
        <div style={S.center}>
          <Loader2 size={40} style={{ animation: "spin 1s linear infinite" }} />
          <h2>Calcolo P&L finale, prezzi reali e debrief...</h2>
        </div>
      )}
      {phase === "done" && finalResult && (
        <DoneView finalResult={finalResult} scenario={scenario}
                  history={history} onRestart={resetGame}
                  onViewHistory={() => nav("/simulator/history")} />
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
        Tutti gli scenari sono basati su EVENTI STORICI REALI con prezzi
        di mercato di quei giorni (Polygon.io).
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
        Ogni turno = <strong>1 settimana</strong> di tempo simulato.
        L'AI può riconsiderare la posizione solo all'inizio del turno successivo.
      </p>
      <div style={{ display: "flex", gap: 12, marginBottom: 24 }}>
        {[3, 4, 5].map(n => (
          <button key={n} style={{ ...S.numBtn, ...(numSteps === n ? S.numBtnSel : {}) }}
                  onClick={() => setNumSteps(n)}>
            <div style={{ fontSize: 32, fontWeight: 800 }}>{n}</div>
            <div style={{ fontSize: 12, color: "#94a3b8" }}>{n} turni · {n} settimane</div>
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
      <div style={{ display: "flex", flexDirection: "column", gap: 8,
                    marginBottom: 24, maxHeight: 400, overflow: "auto" }}>
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
      <div style={S.gameHeader}>
        <div>
          <div style={S.scenTitle}>{scenario.title}</div>
          <div style={S.scenSubtitle}>{scenario.brief}</div>
        </div>
        <div style={S.turnBadge}>
          Turno {currentStepIdx + 1} / {scenario.num_steps}
          <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 2 }}>{currentDate}</div>
        </div>
      </div>

      <PortfolioPanel valuation={valuation} prices={currentPrices} />

      {/* Asset universe con prezzi attuali e delta */}
      {lastStep && lastStep.price_changes && (
        <PriceTablePanel priceChanges={lastStep.price_changes}
                         universe={scenario.asset_universe} />
      )}

      {history.length > 0 && (
        <details style={S.detailsBox} open>
          <summary style={S.summaryHead}>
            📜 Storia turni precedenti ({history.length})
          </summary>
          <div style={{ marginTop: 12 }}>
            {history.map((h, i) => <StepCard key={i} step={h} />)}
          </div>
        </details>
      )}

      <div style={S.nextActionBox}>
        <div>
          <strong>Pronto per il turno {currentStepIdx + 1}?</strong>
          <div style={{ fontSize: 13, color: "#94a3b8", marginTop: 4 }}>
            L'AI leggerà le headline del {currentDate} e deciderà come allocare il
            portafoglio per i prossimi 7 giorni.
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

function PriceTablePanel({ priceChanges, universe }) {
  return (
    <div style={S.portfolioBox}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
        <TrendingUp size={18} color="#a78bfa" />
        <strong>Prezzi correnti</strong>
        <span style={{ fontSize: 11, color: "#64748b" }}>(asset disponibili)</span>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1.2fr 1fr 1fr 1fr",
                    gap: 8, fontSize: 12, color: "#94a3b8", padding: "4px 8px" }}>
        <span>Ticker</span>
        <span style={{ textAlign: "right" }}>Prezzo</span>
        <span style={{ textAlign: "right" }}>1 sett.</span>
        <span style={{ textAlign: "right" }}>Dal T0</span>
      </div>
      {(universe || []).map(t => {
        const pc = priceChanges[t];
        if (!pc) return (
          <div key={t} style={{ ...S.priceRow, color: "#64748b" }}>
            <span>{t}</span><span>—</span><span>—</span><span>—</span>
          </div>
        );
        return (
          <div key={t} style={S.priceRow}>
            <span style={{ fontWeight: 600 }}>{t}</span>
            <span style={{ textAlign: "right", fontFamily: "monospace" }}>${pc.current?.toFixed(2)}</span>
            <span style={{ textAlign: "right",
                           color: pc.chg_1w_pct == null ? "#64748b"
                                  : pc.chg_1w_pct >= 0 ? "#10b981" : "#ef4444" }}>
              {pc.chg_1w_pct != null ? `${pc.chg_1w_pct >= 0 ? "+" : ""}${pc.chg_1w_pct.toFixed(2)}%` : "—"}
            </span>
            <span style={{ textAlign: "right",
                           color: pc.chg_total_pct == null ? "#64748b"
                                  : pc.chg_total_pct >= 0 ? "#10b981" : "#ef4444" }}>
              {pc.chg_total_pct != null ? `${pc.chg_total_pct >= 0 ? "+" : ""}${pc.chg_total_pct.toFixed(2)}%` : "—"}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function PortfolioPanel({ valuation, prices }) {
  const pnlColor = valuation.total_pnl >= 0 ? "#10b981" : "#ef4444";
  return (
    <div style={S.portfolioBox}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <Wallet size={18} color="#a78bfa" /><strong>Il tuo portafoglio</strong>
        </div>
        <div style={{ textAlign: "right" }}>
          <div style={{ fontSize: 22, fontWeight: 700 }}>
            ${valuation.total_value.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}
          </div>
          <div style={{ fontSize: 13, color: pnlColor }}>
            {valuation.total_pnl >= 0 ? "+" : ""}${valuation.total_pnl.toFixed(2)} ({valuation.total_pnl_pct >= 0 ? "+" : ""}{valuation.total_pnl_pct.toFixed(2)}%)
          </div>
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginTop: 12 }}>
        <div style={S.statBox}>
          <div style={S.statLabel}>Cash</div>
          <div style={S.statVal}>${valuation.cash.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}</div>
        </div>
        <div style={S.statBox}>
          <div style={S.statLabel}>Valore posizioni</div>
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
              <span style={{ color: "#cbd5e1", fontSize: 12 }}>
                {p.quantity.toFixed(2)} unità @ ${p.avg_entry_price.toFixed(2)}
              </span>
              <span style={{ color: "#94a3b8", fontSize: 12 }}>
                ora ${p.current_price.toFixed(2)}
              </span>
              <span style={{ marginLeft: "auto", textAlign: "right" }}>
                <div style={{ color: p.unrealized_pnl >= 0 ? "#10b981" : "#ef4444",
                              fontSize: 13, fontWeight: 600 }}>
                  {p.unrealized_pnl >= 0 ? "+" : ""}${p.unrealized_pnl.toFixed(2)}
                </div>
                <div style={{ color: p.unrealized_pnl >= 0 ? "#10b981" : "#ef4444",
                              fontSize: 11 }}>
                  {p.unrealized_pnl_pct >= 0 ? "+" : ""}{p.unrealized_pnl_pct.toFixed(2)}%
                </div>
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
          📰 {step.headlines.slice(0, 2).join(" · ")}
          {step.headlines.length > 2 && (
            <span style={{ color: "#64748b" }}> +{step.headlines.length - 2} altre</span>
          )}
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
          {step.applied_trades.map((t, i) => {
            const isExecuted = t.status && t.status.startsWith("executed");
            const isSkipped = t.status === "skipped";
            const isShort = t.status === "executed_open_short";
            const isCloseLong = t.status === "executed_close_long";
            const tone = isShort ? "#f97316"
                       : isCloseLong ? "#fbbf24"
                       : t.action === "BUY" ? "#10b981" : "#ef4444";
            return (
              <div key={i} style={S.tradeRowDetail}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
                  <span style={{ ...S.actionTag, background: tone }}>
                    {isShort ? "OPEN SHORT" : isCloseLong ? "CLOSE LONG" : t.action}
                  </span>
                  <strong style={{ fontSize: 14 }}>{t.asset}</strong>
                  {isSkipped && <span style={{ color: "#ef4444", fontSize: 11 }}>SKIPPED</span>}
                </div>
                {isExecuted && t.executed_qty && (
                  <div style={{ fontSize: 13, color: "#cbd5e1", marginLeft: 8 }}>
                    <strong>{t.executed_qty.toFixed(2)} unità</strong> @ ${t.executed_price?.toFixed(2)} =
                    <strong style={{ marginLeft: 4 }}>
                      ${(t.executed_value || (t.executed_qty * t.executed_price))?.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}
                    </strong>
                    <span style={{ color: "#64748b", marginLeft: 8 }}>
                      ({t.allocation_pct?.toFixed(0)}% del portafoglio)
                    </span>
                  </div>
                )}
                {isSkipped && (
                  <div style={{ fontSize: 12, color: "#ef4444", marginLeft: 8 }}>
                    Motivo: {t.reason}
                  </div>
                )}
                {t.thesis && (
                  <div style={{ fontSize: 12, color: "#94a3b8", marginLeft: 8,
                                fontStyle: "italic", marginTop: 4 }}>
                    💭 {t.thesis}
                  </div>
                )}
              </div>
            );
          })}
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
  const portfolioSeries = finalResult.portfolio_value_series || [];
  const priceSeries = finalResult.price_series || {};
  const stepDates = finalResult.step_dates || scenario.step_dates || [];

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
          <div style={S.statLabel}>Benchmark SPY (buy &amp; hold)</div>
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

      {/* Chart valore portafoglio nel tempo */}
      {portfolioSeries.length >= 2 && (
        <div style={S.chartBox}>
          <h3 style={{ marginTop: 0, color: "#a78bfa" }}>📈 Valore portafoglio per turno</h3>
          <PortfolioChart series={portfolioSeries} initialCapital={v.initial_capital} />
        </div>
      )}

      {/* Chart prezzi multi-asset */}
      {Object.keys(priceSeries).length > 0 && stepDates.length >= 2 && (
        <div style={S.chartBox}>
          <h3 style={{ marginTop: 0, color: "#a78bfa" }}>📊 Movimenti prezzi durante lo scenario</h3>
          <PriceMultiChart series={priceSeries} stepDates={stepDates}
                           assetBreakdown={finalResult.asset_breakdown || []} />
        </div>
      )}

      {/* Breakdown per asset */}
      {finalResult.asset_breakdown && finalResult.asset_breakdown.length > 0 && (
        <div style={S.chartBox}>
          <h3 style={{ marginTop: 0, color: "#a78bfa" }}>💼 Breakdown posizioni finali</h3>
          {finalResult.asset_breakdown.map((p, i) => (
            <div key={i} style={S.posRow}>
              <span style={{ ...S.sideTag, background: p.side === "long" ? "#065f46" : "#7c2d12" }}>
                {p.side === "long" ? "LONG" : "SHORT"}
              </span>
              <span style={{ fontWeight: 600 }}>{p.asset}</span>
              <span style={{ color: "#cbd5e1", fontSize: 12 }}>
                {p.quantity.toFixed(2)} @ ${p.avg_entry_price.toFixed(2)} → ${p.final_price.toFixed(2)}
              </span>
              <span style={{ marginLeft: "auto", color: p.unrealized_pnl >= 0 ? "#10b981" : "#ef4444",
                             fontSize: 13, fontWeight: 600 }}>
                {p.unrealized_pnl >= 0 ? "+" : ""}${p.unrealized_pnl.toFixed(2)} ({p.unrealized_pnl_pct >= 0 ? "+" : ""}{p.unrealized_pnl_pct.toFixed(2)}%)
              </span>
            </div>
          ))}
        </div>
      )}

      {finalResult.debrief && (
        <div style={S.debriefBox}>
          <h3 style={{ marginTop: 0, color: "#a78bfa" }}>
            <Trophy size={18} style={{ verticalAlign: "middle" }} /> Debrief AI
          </h3>
          <div style={{ whiteSpace: "pre-wrap", lineHeight: 1.6 }}>{finalResult.debrief}</div>
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

      {/* AI Advisor Chat */}
      <AdvisorChat scenario={scenario} history={history} finalResult={finalResult} />

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
        <button style={S.btnGhost} onClick={onViewHistory}>Vai allo storico</button>
      </div>
    </div>
  );
}

// ════════════════════════════════════════════════════════════════════
// CHARTS (SVG)
// ════════════════════════════════════════════════════════════════════

function PortfolioChart({ series, initialCapital }) {
  const W = 600, H = 200, P = 30;
  const values = series.map(s => s.value).filter(v => v != null);
  if (values.length < 2) return <div style={{ color: "#64748b" }}>Dati insufficienti</div>;

  const minV = Math.min(initialCapital, ...values) * 0.98;
  const maxV = Math.max(initialCapital, ...values) * 1.02;
  const xStep = (W - 2 * P) / (series.length - 1);
  const yScale = (v) => H - P - ((v - minV) / (maxV - minV)) * (H - 2 * P);

  const points = series.map((s, i) => `${P + i * xStep},${yScale(s.value || initialCapital)}`).join(" ");
  const initialY = yScale(initialCapital);
  const lastValue = series[series.length - 1].value;
  const trendUp = lastValue >= initialCapital;
  const lineColor = trendUp ? "#10b981" : "#ef4444";

  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" height="200" style={{ background: "#0f172a", borderRadius: 6 }}>
      {/* Linea baseline (capitale iniziale) */}
      <line x1={P} x2={W - P} y1={initialY} y2={initialY}
            stroke="#475569" strokeDasharray="4 4" strokeWidth="1" />
      <text x={W - P + 4} y={initialY + 4} fill="#94a3b8" fontSize="10">${initialCapital.toLocaleString()}</text>

      {/* Linea valore portafoglio */}
      <polyline points={points} fill="none" stroke={lineColor} strokeWidth="2" />

      {/* Punti per turno */}
      {series.map((s, i) => (
        <g key={i}>
          <circle cx={P + i * xStep} cy={yScale(s.value || initialCapital)} r="4" fill={lineColor} />
          <text x={P + i * xStep} y={H - 8} textAnchor="middle" fill="#94a3b8" fontSize="10">
            T{i}
          </text>
          <text x={P + i * xStep} y={yScale(s.value || initialCapital) - 8} textAnchor="middle"
                fill="#e2e8f0" fontSize="10">
            ${(s.value || 0).toFixed(0)}
          </text>
        </g>
      ))}
    </svg>
  );
}

function PriceMultiChart({ series, stepDates, assetBreakdown }) {
  // Mostra fino a 6 asset: priorità a quelli del breakdown finale, poi major
  const assetSet = new Set();
  (assetBreakdown || []).forEach(a => assetSet.add(a.asset));
  // Priorità al breakdown, poi tickers SPY/major
  ["SPY", "QQQ", "GLD", "BTC-USD", "TLT", "XOM"].forEach(t => {
    if (assetSet.size < 6 && series[t]) assetSet.add(t);
  });
  // Riempire fino a 6 con altri ticker disponibili
  Object.keys(series).forEach(t => {
    if (assetSet.size < 6) assetSet.add(t);
  });

  const tickers = Array.from(assetSet).slice(0, 6);
  const colors = ["#a78bfa", "#10b981", "#f59e0b", "#06b6d4", "#ef4444", "#ec4899"];

  // Normalizza ogni serie a 100 al T0 per confronto pulito
  const normalized = tickers.map((t, idx) => {
    const prices = series[t] || {};
    const t0Price = prices[stepDates[0]];
    if (!t0Price) return null;
    const points = stepDates
      .map(d => prices[d] != null ? (prices[d] / t0Price * 100) : null)
      .filter(p => p != null);
    return points.length >= 2 ? { ticker: t, color: colors[idx % colors.length], points } : null;
  }).filter(Boolean);

  if (normalized.length === 0) {
    return <div style={{ color: "#64748b" }}>Dati prezzi insufficienti</div>;
  }

  const W = 600, H = 220, P = 35;
  const allPts = normalized.flatMap(n => n.points);
  const minV = Math.min(95, ...allPts);
  const maxV = Math.max(105, ...allPts);
  const xStep = (W - 2 * P) / (stepDates.length - 1);
  const yScale = (v) => H - P - ((v - minV) / (maxV - minV)) * (H - 2 * P);

  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" height="220" style={{ background: "#0f172a", borderRadius: 6 }}>
        {/* Linea baseline a 100 (T0) */}
        <line x1={P} x2={W - P} y1={yScale(100)} y2={yScale(100)}
              stroke="#475569" strokeDasharray="4 4" strokeWidth="1" />
        <text x={W - P + 4} y={yScale(100) + 4} fill="#94a3b8" fontSize="10">100</text>

        {/* Date sull'asse X */}
        {stepDates.map((d, i) => (
          <text key={d} x={P + i * xStep} y={H - 8} textAnchor="middle" fill="#94a3b8" fontSize="9">
            {d.slice(5)}
          </text>
        ))}

        {/* Linee per ticker */}
        {normalized.map((n, idx) => {
          const points = n.points.map((p, i) => `${P + i * xStep},${yScale(p)}`).join(" ");
          return <polyline key={n.ticker} points={points} fill="none" stroke={n.color} strokeWidth="2" />;
        })}
      </svg>

      {/* Legenda */}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 12, marginTop: 8, justifyContent: "center" }}>
        {normalized.map(n => {
          const last = n.points[n.points.length - 1];
          const change = last - 100;
          return (
            <div key={n.ticker} style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 12 }}>
              <span style={{ width: 12, height: 3, background: n.color, borderRadius: 1 }} />
              <span style={{ fontWeight: 600 }}>{n.ticker}</span>
              <span style={{ color: change >= 0 ? "#10b981" : "#ef4444" }}>
                {change >= 0 ? "+" : ""}{change.toFixed(1)}%
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ════════════════════════════════════════════════════════════════════
// AI ADVISOR CHAT
// ════════════════════════════════════════════════════════════════════

function AdvisorChat({ scenario, history, finalResult }) {
  const [msgs, setMsgs] = useState([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const endRef = useRef(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [msgs]);

  async function send() {
    if (!input.trim() || busy) return;
    const userMsg = input.trim();
    setInput(""); setBusy(true);
    const newMsgs = [...msgs, { role: "user", content: userMsg }];
    setMsgs(newMsgs);
    try {
      const resp = await fetch(`${API}/api/simulator/v2/advisor`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          scenario, history, final_result: finalResult,
          user_message: userMsg, chat_history: msgs,
        }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      setMsgs([...newMsgs, { role: "assistant", content: data.reply || "(no reply)" }]);
    } catch (e) {
      setMsgs([...newMsgs, { role: "assistant", content: `Errore: ${e.message}` }]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={S.advisorBox}>
      <h3 style={{ marginTop: 0, color: "#a78bfa", display: "flex", alignItems: "center", gap: 8 }}>
        <MessageCircle size={18} /> AI Advisor — chiedimi cosa vuoi sapere sulla partita
      </h3>
      {msgs.length === 0 && (
        <div style={{ color: "#64748b", fontStyle: "italic", fontSize: 13, marginBottom: 12 }}>
          Esempi: <em>"Perché ha shortato TLT al T2?"</em> · <em>"Come avrei potuto fare meglio?"</em>
          · <em>"La tesi del primo turno aveva senso?"</em>
        </div>
      )}
      <div style={S.chatWindow}>
        {msgs.map((m, i) => (
          <div key={i} style={{
            ...S.chatMsg,
            alignSelf: m.role === "user" ? "flex-end" : "flex-start",
            background: m.role === "user" ? "#1e1b3a" : "#1e293b",
            border: m.role === "user" ? "1px solid #6d28d9" : "1px solid #334155",
          }}>
            <div style={{ fontSize: 11, color: "#94a3b8", marginBottom: 4 }}>
              {m.role === "user" ? "Tu" : "AI Advisor"}
            </div>
            <div style={{ whiteSpace: "pre-wrap", lineHeight: 1.5 }}>{m.content}</div>
          </div>
        ))}
        {busy && (
          <div style={{ ...S.chatMsg, alignSelf: "flex-start", background: "#1e293b" }}>
            <Loader2 size={14} style={{ animation: "spin 1s linear infinite" }} /> AI sta pensando...
          </div>
        )}
        <div ref={endRef} />
      </div>
      <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
        <input
          type="text" value={input} onChange={e => setInput(e.target.value)}
          onKeyDown={e => e.key === "Enter" && send()}
          placeholder="Fai una domanda sulla partita..."
          disabled={busy}
          style={S.chatInput}
        />
        <button style={S.btnPrimary} onClick={send} disabled={busy || !input.trim()}>
          <Send size={16} />
        </button>
      </div>
    </div>
  );
}

// ════════════════════════════════════════════════════════════════════
// HELPERS
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
      mktValue = qty * cur; pnl = (cur - entry) * qty; positionsValue += mktValue;
    } else {
      mktValue = qty * cur; pnl = (entry - cur) * qty; positionsValue -= mktValue;
    }
    const pnlPct = (qty && entry) ? (pnl / (qty * entry) * 100) : 0;
    return { ...p, current_price: cur, market_value: mktValue,
             unrealized_pnl: pnl, unrealized_pnl_pct: pnlPct };
  });
  const totalValue = cash + positionsValue;
  const initial = portfolio.initial_capital || totalValue;
  const totalPnl = totalValue - initial;
  return {
    total_value: totalValue, cash, positions_value: positionsValue,
    total_pnl: totalPnl,
    total_pnl_pct: initial ? (totalPnl / initial * 100) : 0,
    initial_capital: initial, positions,
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
  posRow: { display: "flex", alignItems: "center", gap: 8, padding: "8px 10px",
            background: "#0a0e1a", borderRadius: 4, marginBottom: 6 },
  sideTag: { padding: "2px 8px", borderRadius: 4, fontSize: 10, fontWeight: 700, color: "#fff" },
  priceRow: { display: "grid", gridTemplateColumns: "1.2fr 1fr 1fr 1fr", gap: 8,
              padding: "6px 8px", fontSize: 13, color: "#e2e8f0",
              borderTop: "1px solid #1e293b" },
  detailsBox: { background: "#0f172a", border: "1px solid #1e293b", borderRadius: 8,
                padding: 12, marginBottom: 16 },
  summaryHead: { cursor: "pointer", fontWeight: 600, color: "#cbd5e1" },
  stepCard: { padding: 12, background: "#1e293b", borderRadius: 6, marginBottom: 8 },
  tradeRowDetail: { padding: "8px 10px", background: "#0a0e1a", borderRadius: 4,
                    marginBottom: 6, border: "1px solid #1e293b" },
  actionTag: { padding: "3px 10px", borderRadius: 4, fontSize: 11, fontWeight: 700, color: "#fff" },
  nextActionBox: { display: "flex", justifyContent: "space-between", alignItems: "center",
                   gap: 16, padding: 16, background: "#1e1b3a", border: "1px solid #6d28d9",
                   borderRadius: 8, marginTop: 16 },
  finalGrid: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 16 },
  finalBox: { padding: 16, background: "#0f172a", border: "1px solid #1e293b",
              borderRadius: 8 },
  chartBox: { padding: 16, background: "#0f172a", border: "1px solid #1e293b",
              borderRadius: 8, marginBottom: 16 },
  debriefBox: { padding: 16, background: "#1e1b3a", border: "1px solid #6d28d9",
                borderRadius: 8, marginBottom: 16, color: "#e2e8f0" },
  revealBox: { padding: 16, background: "#0f172a", border: "1px solid #1e293b",
               borderRadius: 8, marginBottom: 16, color: "#e2e8f0" },
  advisorBox: { padding: 16, background: "#0f172a", border: "1px solid #6d28d9",
                borderRadius: 8, marginTop: 16 },
  chatWindow: { display: "flex", flexDirection: "column", gap: 8, maxHeight: 400,
                overflow: "auto", padding: 12, background: "#0a0e1a", borderRadius: 6 },
  chatMsg: { padding: "10px 14px", borderRadius: 8, maxWidth: "80%", color: "#e2e8f0",
             fontSize: 14 },
  chatInput: { flex: 1, padding: "10px 14px", background: "#1e293b", border: "1px solid #334155",
               borderRadius: 6, color: "#e2e8f0", fontSize: 14 },
};
