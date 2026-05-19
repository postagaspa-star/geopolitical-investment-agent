import { useEffect, useState } from "react";

const API = window.location.origin;

/**
 * Edge Tracker — "il sistema ha un vantaggio reale, o no, o non si sa ancora?"
 *
 * Mette insieme i dati che gia' esistono (trade chiusi, alpha vs S&P,
 * calibrazione del segnale) e applica criteri DECISI PRIMA e mostrati in
 * chiaro — cosi' non sono spostabili a posteriori. Risponde alla domanda
 * vera: bravura o fortuna? Con campioni statisticamente sufficienti, non
 * sul "buon mese".
 */
const VERDICT = {
  insufficient_data: { color: "#64748b", label: "DATI INSUFFICIENTI",
    sub: "Troppo presto per giudicare" },
  edge_confirmed: { color: "#10b981", label: "EDGE DIMOSTRATO",
    sub: "Tutti i criteri soddisfatti su questo campione" },
  no_edge: { color: "#ef4444", label: "NESSUN EDGE",
    sub: "Ipotesi falsificata su questo periodo" },
  edge_emerging: { color: "#f59e0b", label: "IN COSTRUZIONE",
    sub: "Segnale parziale, serve piu' tempo" },
};

function fmtNum(v) {
  if (v === null || v === undefined) return "—";
  if (v === "∞") return "∞";
  return typeof v === "number" ? v.toFixed(2) : String(v);
}

function Criterion({ label, ok, detail }) {
  const c = ok === true ? "#10b981" : ok === false ? "#ef4444" : "#64748b";
  const icon = ok === true ? "✓" : ok === false ? "✗" : "•";
  return (
    <div style={{ display: "flex", alignItems: "flex-start", gap: 10,
                  padding: "8px 0", borderBottom: "1px solid #1a2030" }}>
      <span style={{ color: c, fontWeight: 700, fontSize: "1rem",
                     minWidth: 16 }}>{icon}</span>
      <div>
        <div style={{ fontSize: "0.86rem", color: "#e2e8f0", fontWeight: 600 }}>
          {label}
        </div>
        {detail && (
          <div style={{ fontSize: "0.76rem", color: "#94a3b8", marginTop: 2,
                        lineHeight: 1.5 }}>{detail}</div>
        )}
      </div>
    </div>
  );
}

function Row({ k, v, strong }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between",
                  padding: "3px 0", fontSize: "0.78rem" }}>
      <span style={{ color: "#94a3b8" }}>{k}</span>
      <span style={{ color: strong ? "#f1f5f9" : "#cbd5e1",
                     fontWeight: strong ? 700 : 500 }}>{v}</span>
    </div>
  );
}

// Una singola operazione nella diagnosi confidence (sicuro-ma-perso /
// poco-sicuro-ma-vinto): ticker, return, confidence, date, ragionamento.
function TradeMini({ t, good }) {
  const col = good ? "#10b981" : "#ef4444";
  return (
    <div style={{ background: "#0a1018", borderRadius: 8,
                  padding: "10px 12px", marginBottom: 8 }}>
      <div style={{ display: "flex", justifyContent: "space-between",
                    alignItems: "baseline", marginBottom: 4 }}>
        <span style={{ fontWeight: 700, color: "#e2e8f0",
                       fontSize: "0.84rem" }}>
          {t.ticker || "—"}
        </span>
        <span style={{ color: col, fontWeight: 700, fontSize: "0.84rem" }}>
          {t.return_pct >= 0 ? "+" : ""}{t.return_pct}%
        </span>
      </div>
      <div style={{ fontSize: "0.72rem", color: "#94a3b8",
                    marginBottom: t.reason ? 5 : 0 }}>
        confidence <strong style={{ color: "#cbd5e1" }}>{t.confidence}</strong>
        {" · "}{t.buy_date || "?"} → {t.sell_date || "?"}
      </div>
      {t.reason && (
        <div style={{ fontSize: "0.72rem", color: "#7c8aa0",
                      lineHeight: 1.45, fontStyle: "italic" }}>
          “{t.reason}”
        </div>
      )}
    </div>
  );
}

export default function EdgeTrackerPage() {
  const [data, setData] = useState(null);
  const [diag, setDiag] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await fetch(`${API}/api/live/edge-tracker`);
        const txt = await res.text();
        if (!alive) return;
        if (!res.ok || !txt.trim()) {
          setError(`Errore caricamento (HTTP ${res.status})`);
        } else {
          try { setData(JSON.parse(txt)); }
          catch { setError("Risposta non valida dal server"); }
        }
      } catch (e) {
        if (alive) setError(String(e.message || e));
      } finally {
        if (alive) setLoading(false);
      }
    })();
    // Diagnosi confidence: fetch separato, non blocca la pagina se fallisce.
    (async () => {
      try {
        const r = await fetch(`${API}/api/live/confidence-diagnosis`);
        const t = await r.text();
        if (!alive || !r.ok || !t.trim()) return;
        try { setDiag(JSON.parse(t)); } catch { /* ignora */ }
      } catch { /* la diagnosi e' opzionale */ }
    })();
    return () => { alive = false; };
  }, []);

  if (loading) {
    return <div className="main-content"><div className="loading-state">
      Analisi in corso…</div></div>;
  }
  if (error || !data) {
    return <div className="main-content"><div className="error-state">
      {error || "Nessun dato"}</div></div>;
  }

  const v = VERDICT[data.verdict] || VERDICT.insufficient_data;
  const samplesPct = Math.min(100,
    Math.round((data.samples / data.min_samples) * 100));

  // Stato di ogni criterio (per i check visivi)
  const pfNum = data.profit_factor === "∞" ? 999
    : Number(data.profit_factor);
  const asymNum = data.asymmetry === "∞" ? 999 : Number(data.asymmetry);
  const enough = data.samples >= data.min_samples;
  const beatsSp = data.alpha_vs_sp_pct !== null
    && data.alpha_vs_sp_pct > 0;
  const pfOk = Number.isFinite(pfNum) && pfNum >= data.criteria.pf_promote;
  const asymOk = Number.isFinite(asymNum)
    && asymNum >= data.criteria.asymmetry_promote;

  return (
    <div className="main-content">
      <div style={{ marginBottom: "1.5rem" }}>
        <h2 style={{ fontSize: "1.4rem", fontWeight: 700, color: "#f1f5f9",
                     marginBottom: 4 }}>Edge Tracker</h2>
        <p style={{ fontSize: "0.85rem", color: "#94a3b8" }}>
          Il sistema ha un vantaggio reale sul mercato, o è fortuna?
          Verdetto onesto con criteri decisi <strong>prima</strong> (qui
          sotto, non modificabili a posteriori).
        </p>
      </div>

      {/* VERDETTO */}
      <div style={{ background: "#111827", border: `1px solid ${v.color}55`,
                    borderLeft: `4px solid ${v.color}`, borderRadius: 12,
                    padding: "20px 24px", marginBottom: 20 }}>
        <div style={{ fontSize: "1.5rem", fontWeight: 800, color: v.color,
                      letterSpacing: "0.02em" }}>{v.label}</div>
        <div style={{ fontSize: "0.82rem", color: "#94a3b8", marginTop: 2 }}>
          {v.sub}
        </div>
        <div style={{ fontSize: "0.9rem", color: "#e2e8f0", marginTop: 12,
                      lineHeight: 1.6 }}>{data.verdict_msg}</div>
      </div>

      {/* CAMPIONI — barra */}
      <div style={{ background: "#111827", border: "1px solid #1f2937",
                    borderRadius: 10, padding: "16px 18px", marginBottom: 16 }}>
        <div style={{ display: "flex", justifyContent: "space-between",
                      marginBottom: 8 }}>
          <span style={{ fontSize: "0.85rem", color: "#cbd5e1",
                         fontWeight: 600 }}>
            Campioni (trade chiusi)
          </span>
          <span style={{ fontSize: "0.85rem", color: enough ? "#10b981"
                          : "#f59e0b", fontWeight: 700 }}>
            {data.samples} / {data.min_samples} minimo
          </span>
        </div>
        <div style={{ height: 8, background: "#0a1018", borderRadius: 999,
                      overflow: "hidden" }}>
          <div style={{ width: `${samplesPct}%`, height: "100%",
                        background: enough ? "#10b981" : "#f59e0b" }} />
        </div>
        <div style={{ fontSize: "0.74rem", color: "#64748b", marginTop: 6 }}>
          {enough ? "Campione sufficiente per un giudizio statistico."
            : `Servono ancora ${data.min_samples - data.samples} trade `
              + "chiusi prima di poter giudicare con onestà."}
        </div>
      </div>

      {/* METRICHE vs CRITERI */}
      <div style={{ background: "#111827", border: "1px solid #1f2937",
                    borderRadius: 10, padding: "16px 18px", marginBottom: 16 }}>
        <div style={{ fontSize: "0.95rem", fontWeight: 700, color: "#e2e8f0",
                      marginBottom: 4 }}>I criteri, uno per uno</div>
        <div style={{ fontSize: "0.74rem", color: "#64748b", marginBottom: 10,
                      lineHeight: 1.5 }}>
          Calcolati su <strong>tutto lo storico</strong> ({data.samples} trade
          chiusi), con la stessa formula di Analytics. Coincidono con
          Analytics in modalità <strong>"Tutto"</strong>; differiscono da
          Analytics su un periodo specifico (30g, 7g…) solo perché lì i
          trade sono filtrati per data — non è un errore.
        </div>
        <Criterion label={`Batte l'S&P 500 (alpha > 0)`} ok={beatsSp}
          detail={data.alpha_vs_sp_pct !== null
            ? `Alpha: ${data.alpha_vs_sp_pct >= 0 ? "+" : ""}`
              + `${data.alpha_vs_sp_pct} punti${data.benchmark_verdict
              ? ` · ${data.benchmark_verdict}` : ""}`
            : "Confronto col mercato non disponibile."} />
        <Criterion
          label={`Profit factor ≥ ${data.criteria.pf_promote}`} ok={pfOk}
          detail={`Attuale: ${fmtNum(data.profit_factor)} `
            + `(per ogni € perso, quanti se ne recuperano)`} />
        <Criterion
          label={`Asimmetria guadagni/perdite ≥ ${data.criteria.asymmetry_promote}`}
          ok={asymOk}
          detail={`Attuale: ${fmtNum(data.asymmetry)} · `
            + `vinci medio +${data.avg_win_pct}% / perdi medio `
            + `${data.avg_loss_pct}% · win rate ${data.win_rate_pct}%`} />
        <Criterion label="La confidence dell'AI discrimina"
          ok={data.calibration_ok} detail={data.calibration_detail} />
      </div>

      {/* DIAGNOSI DELLA CONFIDENCE — quali trade rompono la relazione */}
      {diag && diag.available && (
        <div style={{ background: "#111827",
                      border: `1px solid ${diag.inverted ? "#ef4444" : "#1f2937"}55`,
                      borderLeft: `4px solid ${diag.inverted
                        ? "#ef4444" : "#10b981"}`,
                      borderRadius: 10, padding: "16px 18px",
                      marginBottom: 16 }}>
          <div style={{ fontSize: "0.95rem", fontWeight: 700,
                        color: "#e2e8f0", marginBottom: 4 }}>
            Diagnosi della confidence
          </div>
          <div style={{ fontSize: "0.74rem", color: "#64748b",
                        marginBottom: 10 }}>
            Non "cura" il problema: mostra <strong>quali</strong> operazioni
            lo causano, per capire il pattern prima di correggere.
          </div>
          <div style={{ fontSize: "0.86rem", color: "#e2e8f0",
                        lineHeight: 1.6, padding: "10px 12px",
                        background: "#0d1424", borderRadius: 8,
                        marginBottom: 14 }}>
            {diag.diagnosis}
          </div>

          {/* Terzili: bassa / media / alta confidence */}
          <div style={{ display: "grid",
                        gridTemplateColumns: "1fr 1fr 1fr", gap: 10,
                        marginBottom: 14 }}>
            {[["Bassa conf.", diag.tiers.low, "#94a3b8"],
              ["Media conf.", diag.tiers.mid, "#cbd5e1"],
              ["Alta conf.", diag.tiers.high, "#3b82f6"]].map(
              ([lab, ti, c]) => (
                <div key={lab} style={{ background: "#0a1018",
                              borderRadius: 8, padding: "10px 12px" }}>
                  <div style={{ fontSize: "0.74rem", color: c,
                                fontWeight: 700, marginBottom: 6 }}>
                    {lab}
                  </div>
                  <Row k="Trade" v={ti?.n ?? "—"} />
                  <Row k="Rend. medio" strong
                    v={ti?.avg_return_pct != null
                      ? `${ti.avg_return_pct >= 0 ? "+" : ""}`
                        + `${ti.avg_return_pct}%` : "—"} />
                  <Row k="Win rate" v={ti?.win_rate_pct != null
                    ? `${ti.win_rate_pct}%` : "—"} />
                </div>
              ))}
          </div>

          <div style={{ display: "grid",
                        gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            <div>
              <div style={{ fontSize: "0.8rem", fontWeight: 700,
                            color: "#ef4444", marginBottom: 8 }}>
                Troppo sicuro, ma perso ({diag.overconfident_losers.length})
              </div>
              {diag.overconfident_losers.length === 0 && (
                <div style={{ fontSize: "0.76rem", color: "#64748b" }}>
                  Nessuno nel terzile alto.
                </div>
              )}
              {diag.overconfident_losers.map((t, i) => (
                <TradeMini key={i} t={t} good={false} />
              ))}
            </div>
            <div>
              <div style={{ fontSize: "0.8rem", fontWeight: 700,
                            color: "#10b981", marginBottom: 8 }}>
                Poco sicuro, ma vinto ({diag.underrated_winners.length})
              </div>
              {diag.underrated_winners.length === 0 && (
                <div style={{ fontSize: "0.76rem", color: "#64748b" }}>
                  Nessuno nel terzile basso.
                </div>
              )}
              {diag.underrated_winners.map((t, i) => (
                <TradeMini key={i} t={t} good={true} />
              ))}
            </div>
          </div>

          {diag.recurring_overconfident_tickers
            && diag.recurring_overconfident_tickers.length > 0 && (
            <div style={{ marginTop: 14, padding: "10px 12px",
                          background: "#0d1424", borderRadius: 8 }}>
              <div style={{ fontSize: "0.78rem", color: "#f59e0b",
                            fontWeight: 700, marginBottom: 6 }}>
                Punto cieco ricorrente
              </div>
              <div style={{ fontSize: "0.76rem", color: "#cbd5e1",
                            lineHeight: 1.5 }}>
                Questi titoli compaiono più volte tra i "troppo sicuro
                ma perso" — non è sfortuna isolata, è un errore di
                sistema:{" "}
                {diag.recurring_overconfident_tickers.map((r) =>
                  `${r.ticker} (${r.count}×)`).join(", ")}
              </div>
            </div>
          )}

          {diag.next_levers && diag.next_levers.length > 0 && (
            <div style={{ marginTop: 14 }}>
              <div style={{ fontSize: "0.78rem", fontWeight: 700,
                            color: "#94a3b8", marginBottom: 6,
                            textTransform: "uppercase",
                            letterSpacing: "0.05em" }}>
                Prossime leve (dopo aver capito il pattern)
              </div>
              {diag.next_levers.map((lv, i) => (
                <div key={i} style={{ fontSize: "0.8rem", color: "#cbd5e1",
                              lineHeight: 1.55, padding: "4px 0",
                              display: "flex", gap: 8 }}>
                  <span style={{ color: "#64748b" }}>{i + 1}.</span>
                  <span>{lv}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
      {diag && diag.available === false && (
        <div style={{ background: "#0d1424", border: "1px solid #1f2937",
                      borderRadius: 10, padding: "14px 18px",
                      marginBottom: 16, fontSize: "0.82rem",
                      color: "#94a3b8", lineHeight: 1.55 }}>
          <strong style={{ color: "#cbd5e1" }}>Diagnosi confidence:</strong>{" "}
          {diag.reason}
        </div>
      )}

      {/* DATI GREZZI DEL CALCOLO — trasparenza totale */}
      {data.raw_calc && (
        <div style={{ background: "#111827", border: "1px solid #1f2937",
                      borderRadius: 10, padding: "16px 18px",
                      marginBottom: 16 }}>
          <div style={{ fontSize: "0.95rem", fontWeight: 700,
                        color: "#e2e8f0", marginBottom: 4 }}>
            Dati grezzi del calcolo
          </div>
          <div style={{ fontSize: "0.74rem", color: "#64748b",
                        marginBottom: 12 }}>
            Esattamente cosa è stato usato per confrontare te con l'S&P,
            sullo stesso identico arco di tempo. Niente di nascosto.
          </div>
          <div style={{ display: "grid",
                        gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            <div style={{ background: "#0a1018", borderRadius: 8,
                          padding: "12px 14px" }}>
              <div style={{ fontSize: "0.78rem", color: "#3b82f6",
                            fontWeight: 700, marginBottom: 6 }}>
                Il tuo portafoglio
              </div>
              <Row k="Valore iniziale"
                v={`$${data.raw_calc.portfolio_first_value?.toLocaleString()}`} />
              <Row k="Valore finale"
                v={`$${data.raw_calc.portfolio_last_value?.toLocaleString()}`} />
              <Row k="Dal" v={data.raw_calc.portfolio_first_date || "—"} />
              <Row k="Al" v={data.raw_calc.portfolio_last_date || "—"} />
              <Row k="Punti dati" v={data.raw_calc.portfolio_points} />
              <Row k="Rendimento" strong
                v={data.benchmark_portfolio_return_pct != null
                  ? `${data.benchmark_portfolio_return_pct >= 0 ? "+" : ""}`
                    + `${data.benchmark_portfolio_return_pct}%` : "—"} />
            </div>
            <div style={{ background: "#0a1018", borderRadius: 8,
                          padding: "12px 14px" }}>
              <div style={{ fontSize: "0.78rem", color: "#94a3b8",
                            fontWeight: 700, marginBottom: 6 }}>
                S&P 500 (stesso periodo)
              </div>
              <Row k="Close iniziale"
                v={data.raw_calc.sp500_first_close != null
                  ? `$${data.raw_calc.sp500_first_close}` : "—"} />
              <Row k="Close finale"
                v={data.raw_calc.sp500_last_close != null
                  ? `$${data.raw_calc.sp500_last_close}` : "—"} />
              <Row k="Dal" v={data.raw_calc.sp500_first_date || "—"} />
              <Row k="Al" v={data.raw_calc.sp500_last_date || "—"} />
              <Row k="Punti dati" v={data.raw_calc.sp500_points} />
              <Row k="Rendimento" strong
                v={data.benchmark_sp500_return_pct != null
                  ? `${data.benchmark_sp500_return_pct >= 0 ? "+" : ""}`
                    + `${data.benchmark_sp500_return_pct}%` : "—"} />
            </div>
          </div>
          <div style={{ fontSize: "0.8rem", color: "#e2e8f0",
                        marginTop: 12, padding: "10px 12px",
                        background: "#0d1424", borderRadius: 8 }}>
            Differenza (alpha) ={" "}
            <strong style={{ color: data.alpha_vs_sp_pct >= 0
              ? "#10b981" : "#ef4444" }}>
              {data.alpha_vs_sp_pct != null
                ? `${data.alpha_vs_sp_pct >= 0 ? "+" : ""}`
                  + `${data.alpha_vs_sp_pct} punti`
                : "—"}
            </strong>
            {" "}— il tuo rendimento meno quello dell'S&P, stesso periodo.
            Se le date qui sopra coincidono e l'alpha non torna, segnalalo.
          </div>
        </div>
      )}

      {/* CRITERI DECISI PRIMA — trasparenza anti-bias */}
      <div style={{ background: "#0d1424", border: "1px dashed #334155",
                    borderRadius: 10, padding: "14px 18px" }}>
        <div style={{ fontSize: "0.8rem", fontWeight: 700, color: "#94a3b8",
                      marginBottom: 6, textTransform: "uppercase",
                      letterSpacing: "0.05em" }}>
          Regola del giudizio (fissata in anticipo)
        </div>
        <div style={{ fontSize: "0.84rem", color: "#cbd5e1",
                      lineHeight: 1.6 }}>
          {data.criteria.explain}
        </div>
        <div style={{ fontSize: "0.74rem", color: "#64748b", marginTop: 8 }}>
          Questi numeri sono scritti nel codice e mostrati qui apposta:
          servono a non "spostare l'asticella" dopo aver visto i risultati.
          È la differenza tra misurare e illudersi.
        </div>
      </div>
    </div>
  );
}
