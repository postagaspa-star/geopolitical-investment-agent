import React, { useState, useEffect, useRef } from "react";
import HealthPanel from "./HealthPanel";

// Base URL per le chiamate API (stesso server)
const API = window.location.origin;

// Stato iniziale diagnostica
const INITIAL_DIAGNOSTICS = {
  anthropic: { status: "loading", message: "" },
  newsapi:   { status: "loading", message: "" },
  finnhub:   { status: "loading", message: "" },
  gdelt:     { status: "loading", message: "" },
  yfinance:  { status: "loading", message: "" },
  db:        { status: "loading", message: "" },
  scheduler: { status: "loading", message: "" },
  documents: { status: "loading", message: "" },
};

// Etichette dei prompt per ciascun agente
const PROMPT_AGENTS = [
  {
    key: "prompt_scout",
    label: "Scout (DeepSeek-V3)",
    description: "Raccoglie e analizza notizie da 9 fonti ogni 20 minuti, 24/7. Produce inoltre report aggregati 8H e 4D.",
  },
  {
    key: "prompt_technical",
    label: "Technical (DeepSeek-V3) — equity/ETF",
    description: "Analisi tecnica sui mercati tradizionali (azioni S&P 500 + GLD/SLV/USO). Triggerato dal Watchdog. NON opera su crypto.",
  },
  {
    key: "prompt_decision",
    label: "Decision (Sonnet 4.5) — equity/ETF",
    description: "Decide buy/sell/hold sui mercati tradizionali durante orari NYSE/LSE/XETRA. Max 1 esecuzione/ora. NON opera su crypto.",
  },
  {
    key: "prompt_technical_crypto",
    label: "Technical Crypto (DeepSeek-V3) — 24/7",
    description: "Analisi tecnica focalizzata SOLO sui 14 ticker crypto supportati (BTC, ETH, SOL, ecc.). Gira ogni ora 24/7 indipendentemente dal Watchdog.",
  },
  {
    key: "prompt_decision_crypto",
    label: "Decision Crypto (DeepSeek-R1 reasoning) — 24/7",
    description: "Decisional autonomo focalizzato SOLO sulle crypto supportate (14 ticker). Riceve il Technical Crypto + buffer + documenti crypto-specifici. Gira ogni ora 24/7.",
  },
];

function Settings({ onBack, onDataRefresh }) {
  // === Diagnostica ===
  const [diagnostics, setDiagnostics] = useState(INITIAL_DIAGNOSTICS);

  // === Pannello "Stato app" (cruscotto di salute) ===
  const [healthOpen, setHealthOpen] = useState(false);

  // === Prompt per i 5 agenti ===
  const [prompts, setPrompts] = useState({
    prompt_scout: "",
    prompt_technical: "",
    prompt_decision: "",
    prompt_technical_crypto: "",
    prompt_decision_crypto: "",
  });
  const [promptDefaults, setPromptDefaults] = useState({
    prompt_scout: "",
    prompt_technical: "",
    prompt_decision: "",
    prompt_technical_crypto: "",
    prompt_decision_crypto: "",
  });

  // === Documenti ===
  const [documents, setDocuments] = useState([]);
  const fileInputRef = useRef(null);

  // === Direttive Utente (priorità massima, free-form text) ===
  const [directivesText, setDirectivesText] = useState("");
  const [directivesTextSaved, setDirectivesTextSaved] = useState("");
  const [directivesSaving, setDirectivesSaving] = useState(false);

  // === Risk Profile (vincoli numerici hard) ===
  const [riskActiveKey, setRiskActiveKey] = useState("moderate");
  const [riskActiveSaved, setRiskActiveSaved] = useState("moderate");
  const [riskProfiles, setRiskProfiles] = useState(null);   // {conservative:{...}, moderate:{...}, aggressive:{...}}
  const [riskSaving, setRiskSaving] = useState(false);

  // === UI ===
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState(null);

  const setDiagItem = (key, status, msg = "") => {
    setDiagnostics((prev) => ({ ...prev, [key]: { status, message: msg } }));
  };

  const showMessage = (text, isError = false) => {
    setMessage({ text, isError });
    setTimeout(() => setMessage(null), 3000);
  };

  // === Diagnostica ===
  const runDiagnostics = async (docsData) => {
    const tests = [
      { key: "anthropic", url: "/api/settings/test-anthropic" },
      { key: "newsapi",   url: "/api/settings/test-newsapi" },
      { key: "finnhub",   url: "/api/settings/test-finnhub" },
      { key: "gdelt",     url: "/api/settings/test-gdelt" },
      { key: "yfinance",  url: "/api/settings/test-yfinance" },
      { key: "db",        url: "/api/settings/test-db" },
    ];

    // Parser robusto: prima check res.ok, poi tenta JSON con fallback testo.
    // Bug precedente: res.json() su body vuoto/HTML lanciava
    // "Failed to execute 'json' on 'Response': Unexpected end of JSON input"
    // mascherando lo status code reale (500/502/CORS).
    const safeFetchJson = async (url) => {
      const res = await fetch(`${API}${url}`);
      if (!res.ok) {
        // Prova a leggere il body per messaggio di errore utile
        let detail = "";
        try {
          const txt = await res.text();
          detail = txt ? ` — ${txt.slice(0, 120)}` : "";
        } catch (_) { /* ignore */ }
        throw new Error(`HTTP ${res.status}${detail}`);
      }
      const txt = await res.text();
      if (!txt || !txt.trim()) {
        throw new Error(`Risposta vuota (HTTP ${res.status})`);
      }
      try {
        return JSON.parse(txt);
      } catch (_) {
        throw new Error(`Risposta non JSON (HTTP ${res.status}): ${txt.slice(0, 100)}`);
      }
    };

    await Promise.all(tests.map(async ({ key, url }) => {
      try {
        const data = await safeFetchJson(url);
        if (data.status === "ok") setDiagItem(key, "ok");
        else setDiagItem(key, "error", data.message || "Risposta non valida");
      } catch (err) {
        setDiagItem(key, "error", err.message);
      }
    }));

    // Scheduler
    try {
      const data = await safeFetchJson("/api/agent/status");
      const running = data.is_running || data.scheduler_running || data.status === "running";
      setDiagItem("scheduler", "ok", running ? "In esecuzione" : "Attivo (idle)");
    } catch (err) {
      setDiagItem("scheduler", "error", err.message);
    }

    // Documenti
    const count = Array.isArray(docsData) ? docsData.length : 0;
    setDiagItem("documents", "ok", `${count} ${count !== 1 ? "documenti caricati" : "documento caricato"}`);
  };

  // === Caricamento iniziale ===
  useEffect(() => {
    const loadAll = async () => {
      // Prima carico i default — servono come fallback se il custom e' vuoto
      let defaults = {
        prompt_scout: "", prompt_technical: "", prompt_decision: "",
        prompt_technical_crypto: "", prompt_decision_crypto: "",
      };
      try {
        const res = await fetch(`${API}/api/settings/prompt-defaults`);
        if (res.ok) {
          const data = await res.json();
          defaults = {
            prompt_scout: data.prompt_scout || "",
            prompt_technical: data.prompt_technical || "",
            prompt_decision: data.prompt_decision || "",
            prompt_technical_crypto: data.prompt_technical_crypto || "",
            prompt_decision_crypto: data.prompt_decision_crypto || "",
          };
          setPromptDefaults(defaults);
        }
      } catch (err) {
        console.error("Errore caricamento prompt default:", err);
      }

      // Poi carico i custom: se vuoto/inesistente, pre-carico il default cosi' l'utente
      // vede il testo e puo' modificarlo direttamente (non solo placeholder grigio)
      try {
        const res = await fetch(`${API}/api/settings`);
        if (res.ok) {
          const raw = await res.json();
          const data = raw.settings ?? raw;
          setPrompts({
            prompt_scout: (data.prompt_scout && data.prompt_scout.trim()) || defaults.prompt_scout || "",
            prompt_technical: (data.prompt_technical && data.prompt_technical.trim()) || defaults.prompt_technical || "",
            prompt_decision: (data.prompt_decision && data.prompt_decision.trim()) || defaults.prompt_decision || "",
            prompt_technical_crypto: (data.prompt_technical_crypto && data.prompt_technical_crypto.trim()) || defaults.prompt_technical_crypto || "",
            prompt_decision_crypto: (data.prompt_decision_crypto && data.prompt_decision_crypto.trim()) || defaults.prompt_decision_crypto || "",
          });
        }
      } catch (err) {
        console.error("Errore caricamento impostazioni:", err);
      }

      // Direttive utente (free-form text)
      try {
        const dirRes = await fetch(`${API}/api/settings/directives`);
        if (dirRes.ok) {
          const dirData = await dirRes.json();
          const t = (dirData && typeof dirData.text === "string") ? dirData.text : "";
          setDirectivesText(t);
          setDirectivesTextSaved(t);
        }
      } catch {}

      // Risk profile (preset numerici)
      try {
        const rpRes = await fetch(`${API}/api/settings/risk-profile`);
        if (rpRes.ok) {
          const rpData = await rpRes.json();
          if (rpData.profiles) setRiskProfiles(rpData.profiles);
          if (rpData.active_key) {
            setRiskActiveKey(rpData.active_key);
            setRiskActiveSaved(rpData.active_key);
          }
        }
      } catch {}

      // Carica documenti
      const docs = await loadDocumentsRaw();

      // Diagnostica
      runDiagnostics(docs);
    };

    loadAll();
  }, []);

  const loadDocumentsRaw = async () => {
    try {
      const res = await fetch(`${API}/api/documents`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const docs = Array.isArray(data) ? data : [];
      setDocuments(docs);
      return docs;
    } catch (err) {
      console.error("Errore caricamento documenti:", err);
      setDocuments([]);
      return [];
    }
  };

  // === Salvataggio impostazioni ===
  const saveSettings = async (settings) => {
    setSaving(true);
    try {
      const res = await fetch(`${API}/api/settings`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ settings }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      showMessage("Impostazioni salvate con successo");
    } catch (err) {
      showMessage(`Errore salvataggio: ${err.message}`, true);
    } finally {
      setSaving(false);
    }
  };

  const handleSavePrompt = (key) => {
    saveSettings({ [key]: prompts[key] });
  };

  // === Trigger manuale yfinance / Massive poll ===
  const [pollStatus, setPollStatus] = useState({ running: false, result: null });

  // === Aggiusta liquidità (sottrae o aggiunge al cash) ===
  const [adjustCashStatus, setAdjustCashStatus] = useState({ running: false, result: null });
  const [adjustCashInput, setAdjustCashInput] = useState("-5587.96");
  const handleAdjustCash = async () => {
    if (adjustCashStatus.running) return;
    const delta = parseFloat(adjustCashInput);
    if (!Number.isFinite(delta) || delta === 0) {
      setAdjustCashStatus({ running: false, result: { ok: false, data: { error: "Inserisci un delta numerico (positivo per aggiungere, negativo per sottrarre)" } } });
      return;
    }
    if (!window.confirm(
      `Aggiusta liquidità: ${delta >= 0 ? "+" : ""}$${Math.abs(delta).toLocaleString()}.\n\n` +
      `Modifica solo cash_balance. Il total_value sarà ricalcolato come\n` +
      `nuovo cash + sum(qty × current_price). Snapshot del nuovo stato\n` +
      `viene scritto immediatamente per allineare il chart.\n\n` +
      "Operazione IRREVERSIBILE. Procedere?"
    )) return;
    setAdjustCashStatus({ running: true, result: null });
    try {
      const r = await fetch(`${API}/api/portfolio/adjust-cash?delta=${delta}&confirm=true`, { method: "POST" });
      const data = await r.json();
      setAdjustCashStatus({ running: false, result: { ok: r.ok && data.status === "ok", data } });
    } catch (e) {
      setAdjustCashStatus({ running: false, result: { ok: false, data: { error: String(e) } } });
    }
  };

  // === DB Diagnostic + run migrations ===
  const [dbDiagStatus, setDbDiagStatus] = useState({ running: false, result: null });
  const [migrateStatus, setMigrateStatus] = useState({ running: false, result: null });

  const handleDbDiagnostic = async () => {
    if (dbDiagStatus.running) return;
    setDbDiagStatus({ running: true, result: null });
    try {
      const r = await fetch(`${API}/api/portfolio/db-diagnostic`);
      const data = await r.json();
      setDbDiagStatus({ running: false, result: { ok: r.ok, data } });
    } catch (e) {
      setDbDiagStatus({ running: false, result: { ok: false, data: { error: String(e) } } });
    }
  };

  const handleRunMigrations = async () => {
    if (migrateStatus.running) return;
    if (!window.confirm(
      "Triggera _ensure_schema_migrations() che applica le ALTER TABLE pending " +
      "(v11 quantity NUMERIC, v10 SL/TP, etc.).\n\n" +
      "Idempotente. Richiede DATABASE_URL o SUPABASE_DB_PASSWORD configurato " +
      "su Render → Environment.\n\nProcedere?"
    )) return;
    setMigrateStatus({ running: true, result: null });
    try {
      const r = await fetch(`${API}/api/portfolio/run-migrations`, { method: "POST" });
      const data = await r.json();
      setMigrateStatus({ running: false, result: { ok: r.ok && data.status === "ok", data } });
      // Re-run diagnostic to confirm fix
      if (r.ok && data.status === "ok") {
        setTimeout(() => handleDbDiagnostic(), 500);
      }
    } catch (e) {
      setMigrateStatus({ running: false, result: { ok: false, data: { error: String(e) } } });
    }
  };
  const handleManualPoll = async () => {
    if (pollStatus.running) return;
    setPollStatus({ running: true, result: null });
    try {
      const res = await fetch(`${API}/api/prices/trigger-poll`, { method: "POST" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const summary =
        `${data.tickers ?? 0} ticker richiesti — ${data.quotes_written ?? 0} salvati ` +
        `(massive=${data.massive_count ?? 0}, yf=${data.yfinance_count ?? 0}) ` +
        `posizioni ${data.positions_updated ?? 0}, snapshot=${data.snapshot_saved ? "sì" : "no"} ` +
        `[${data.duration_seconds}s]`;
      setPollStatus({ running: false, result: { ok: true, summary } });
      showMessage(`Poll manuale OK: ${summary}`);
      // Re-run diagnostics per aggiornare gli indicatori
      runDiagnostics(documents);
      // Notifica il parent (App) per aggiornare dashboard
      if (typeof onDataRefresh === "function") onDataRefresh();
    } catch (err) {
      setPollStatus({ running: false, result: { ok: false, summary: err.message } });
      showMessage(`Errore poll manuale: ${err.message}`, true);
    }
  };

  const handleResetPrompt = (key) => {
    if (!window.confirm("Ripristinare il prompt di default per questo agente?")) return;
    // Reset: ripristino il testo del default nel textarea + cancello l'override sul DB
    setPrompts((prev) => ({ ...prev, [key]: promptDefaults[key] || "" }));
    saveSettings({ [key]: "" });
  };

  // === Documenti ===
  // category: 'generic' (default, mercati tradizionali) | 'crypto' (Decision Crypto)
  const handleFileUpload = async (e, category = "generic") => {
    const file = e.target.files[0];
    if (!file) return;
    const formData = new FormData();
    formData.append("file", file);
    try {
      const res = await fetch(`${API}/api/documents/upload?category=${encodeURIComponent(category)}`, {
        method: "POST",
        body: formData,
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      showMessage(`Documento ${category === "crypto" ? "CRYPTO" : "generico"} caricato con successo`);
      const docs = await loadDocumentsRaw();
      setDiagItem("documents", "ok",
        `${docs.length} ${docs.length !== 1 ? "documenti caricati" : "documento caricato"}`);
    } catch (err) {
      showMessage(`Errore upload: ${err.message}`, true);
    }
    e.target.value = "";
  };

  const handleDeleteDoc = async (docId) => {
    try {
      const res = await fetch(`${API}/api/documents/${docId}`, { method: "DELETE" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      showMessage("Documento eliminato");
      const docs = await loadDocumentsRaw();
      setDiagItem("documents", "ok",
        `${docs.length} ${docs.length !== 1 ? "documenti caricati" : "documento caricato"}`);
    } catch (err) {
      showMessage(`Errore eliminazione: ${err.message}`, true);
    }
  };

  const formatFileSize = (bytes) => bytes == null ? "-" : `${(bytes / 1024).toFixed(1)} KB`;
  const formatDate = (dateStr) => !dateStr ? "-" :
    new Date(dateStr).toLocaleDateString("it-IT", {
      day: "2-digit", month: "short", year: "numeric",
      hour: "2-digit", minute: "2-digit",
    });

  const handleRerunDiagnostics = async () => {
    setDiagnostics(INITIAL_DIAGNOSTICS);
    const docs = await loadDocumentsRaw();
    runDiagnostics(docs);
  };

  // === Stili inline ===
  const inputStyle = {
    width: "100%", padding: "10px 12px", background: "#ffffff",
    border: "1px solid #d1d5db", borderRadius: "8px", color: "#111827",
    fontSize: "14px", fontFamily: "'Inter', -apple-system, sans-serif",
    outline: "none", transition: "border-color 0.15s, box-shadow 0.15s",
    boxSizing: "border-box",
  };
  const textareaStyle = {
    ...inputStyle, lineHeight: "1.6", resize: "vertical", minHeight: "180px",
    fontFamily: "'JetBrains Mono', 'Menlo', monospace", fontSize: "13px",
  };
  const labelStyle = {
    display: "block", fontSize: "13px", color: "#6b7280",
    marginBottom: "6px", fontWeight: 500,
  };
  const cardStyle = {
    background: "#ffffff", border: "1px solid #e5e7eb",
    borderRadius: "12px", padding: "24px", marginBottom: "24px",
    boxShadow: "0 1px 3px rgba(0,0,0,0.04)",
  };
  const sectionTitle = {
    fontSize: "16px", fontWeight: 600, color: "#111827",
    margin: 0, marginBottom: "20px",
  };
  const btnPrimary = {
    padding: "10px 20px", background: "#3b82f6", color: "#ffffff",
    border: "none", borderRadius: "8px", cursor: "pointer",
    fontSize: "14px", fontWeight: 500,
    fontFamily: "'Inter', -apple-system, sans-serif",
  };
  const btnSecondary = {
    padding: "8px 14px", background: "#f3f4f6", color: "#374151",
    border: "1px solid #d1d5db", borderRadius: "8px", cursor: "pointer",
    fontSize: "13px", fontWeight: 500,
  };
  const thStyle = {
    padding: "10px 12px", textAlign: "left", fontSize: "0.78rem",
    fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.04em",
    color: "#374151", borderBottom: "1px solid #e5e7eb",
  };
  const tdStyle = {
    padding: "10px 12px", verticalAlign: "top",
  };
  const removeBtn = {
    background: "none", border: "none", color: "#9ca3af",
    cursor: "pointer", fontSize: "11px", padding: "0 2px", lineHeight: 1,
  };

  const handleFocus = (e) => {
    e.target.style.borderColor = "#3b82f6";
    e.target.style.boxShadow = "0 0 0 3px rgba(59,130,246,0.1)";
  };
  const handleBlur = (e) => {
    e.target.style.borderColor = "#d1d5db";
    e.target.style.boxShadow = "none";
  };

  const diagItems = [
    { key: "anthropic", name: "Anthropic API" },
    { key: "newsapi",   name: "NewsAPI" },
    { key: "finnhub",   name: "Finnhub API" },
    { key: "gdelt",     name: "GDELT Project" },
    { key: "yfinance",  name: "yFinance" },
    { key: "db",        name: "Database" },
    { key: "scheduler", name: "Scheduler Agente" },
    { key: "documents", name: "Documenti" },
  ];

  return (
    <div style={{
      background: "#f9fafb", minHeight: "100vh", padding: "32px",
      color: "#111827", fontFamily: "'Inter', -apple-system, sans-serif",
    }}>
      <div style={{ maxWidth: "820px", margin: "0 auto" }}>
        <div style={{ display: "flex", alignItems: "center",
          justifyContent: "space-between", marginBottom: "24px" }}>
          <h1 style={{
            fontSize: "24px", fontWeight: 700, color: "#111827",
            margin: 0, letterSpacing: "-0.02em",
          }}>
            Impostazioni
          </h1>
          <button onClick={() => setHealthOpen(true)} style={{
            background: "#111827", color: "#fff", border: "none",
            borderRadius: "8px", padding: "8px 14px", cursor: "pointer",
            fontSize: "13px", fontWeight: 600, whiteSpace: "nowrap",
          }}>
            ● Stato app
          </button>
        </div>

        <HealthPanel open={healthOpen} onClose={() => setHealthOpen(false)} />

        {message && (
          <div style={{
            padding: "10px 16px", marginBottom: "16px", borderRadius: "8px",
            fontSize: "14px", fontWeight: 500,
            background: message.isError ? "#fef2f2" : "#ecfdf5",
            color: message.isError ? "#991b1b" : "#065f46",
            border: `1px solid ${message.isError ? "#fecaca" : "#a7f3d0"}`,
          }}>
            {message.text}
          </div>
        )}

        {/* === DIAGNOSTICA SISTEMA === */}
        <div style={cardStyle}>
          <div style={{ display: "flex", alignItems: "center",
            justifyContent: "space-between", marginBottom: "16px" }}>
            <div style={{ ...sectionTitle, marginBottom: 0 }}>Diagnostica Sistema</div>
            <button style={btnSecondary} onClick={handleRerunDiagnostics}>Riaggiorna</button>
          </div>
          <div>
            {diagItems.map(({ key, name }, index) => {
              const diag = diagnostics[key];
              const dotColor = diag.status === "ok" ? "#10b981"
                : diag.status === "error" ? "#ef4444"
                : diag.status === "warn" ? "#f59e0b" : "#9ca3af";
              return (
                <div key={key}>
                  <div style={{ display: "flex", alignItems: "center",
                    gap: "10px", padding: "10px 0" }}>
                    <span style={{
                      width: "8px", height: "8px", borderRadius: "50%",
                      background: dotColor, flexShrink: 0,
                      animation: diag.status === "loading"
                        ? "pulse 1.5s ease-in-out infinite" : "none",
                    }} />
                    <span style={{ fontSize: "14px", color: "#374151",
                      fontWeight: 500, minWidth: "130px" }}>{name}</span>
                    <span style={{ fontSize: "13px", color: "#6b7280" }}>
                      {diag.status === "loading" && "Verifica..."}
                      {diag.status === "ok" && `OK${diag.message ? ` — ${diag.message}` : ""}`}
                      {diag.status === "warn" && `Avviso${diag.message ? `: ${diag.message}` : ""}`}
                      {diag.status === "error" && `Errore${diag.message ? `: ${diag.message}` : ""}`}
                    </span>
                  </div>
                  {index < diagItems.length - 1 && (
                    <div style={{ height: "1px", background: "#f3f4f6" }} />
                  )}
                </div>
              );
            })}
          </div>

          {/* Trigger manuale Price Polling */}
          <div style={{ marginTop: "20px", paddingTop: "16px", borderTop: "1px solid #f3f4f6" }}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "8px" }}>
              <div>
                <div style={{ fontSize: "14px", fontWeight: 600, color: "#111827" }}>
                  Aggiornamento prezzi manuale
                </div>
                <div style={{ fontSize: "12px", color: "#6b7280", marginTop: "2px" }}>
                  Forza un ciclo immediato di Price Polling (Massive + yFinance) — utile dopo un deploy o se i prezzi sembrano fermi.
                </div>
              </div>
              <button
                style={{
                  ...btnSecondary,
                  background: pollStatus.running ? "#e5e7eb" : "#3b82f6",
                  color: pollStatus.running ? "#6b7280" : "#fff",
                  borderColor: pollStatus.running ? "#d1d5db" : "#3b82f6",
                  cursor: pollStatus.running ? "not-allowed" : "pointer",
                  whiteSpace: "nowrap",
                }}
                onClick={handleManualPoll}
                disabled={pollStatus.running}
              >
                {pollStatus.running ? "In corso..." : "Aggiorna prezzi"}
              </button>
            </div>
            {pollStatus.result && (
              <div style={{
                marginTop: "8px", padding: "8px 12px", borderRadius: "6px",
                fontSize: "12px",
                background: pollStatus.result.ok ? "#ecfdf5" : "#fef2f2",
                color: pollStatus.result.ok ? "#065f46" : "#991b1b",
                border: `1px solid ${pollStatus.result.ok ? "#a7f3d0" : "#fecaca"}`,
              }}>
                {pollStatus.result.summary}
              </div>
            )}
          </div>

          {/* Aggiusta liquidita: sottrae o aggiunge al cash_balance */}
          <div style={{ marginTop: "20px", paddingTop: "16px", borderTop: "1px solid #f3f4f6" }}>
            <div style={{ marginBottom: "10px" }}>
              <div style={{ fontSize: "14px", fontWeight: 600, color: "#111827" }}>
                Aggiusta liquidita
              </div>
              <div style={{ fontSize: "12px", color: "#6b7280", marginTop: "2px" }}>
                Modifica il cash_balance di un delta (positivo = aggiungi, negativo = sottrai).
                Il total_value viene ricalcolato e uno snapshot viene scritto subito per
                allineare il chart al dashboard. Le posizioni non vengono toccate.
              </div>
            </div>
            <div style={{ display: "flex", gap: "8px", alignItems: "center", marginBottom: "8px" }}>
              <span style={{ fontSize: "13px", color: "#374151" }}>Delta:</span>
              <span style={{ fontSize: "13px", color: "#6b7280" }}>$</span>
              <input
                type="number"
                step="0.01"
                value={adjustCashInput}
                onChange={(e) => setAdjustCashInput(e.target.value)}
                disabled={adjustCashStatus.running}
                style={{
                  padding: "6px 10px",
                  border: "1px solid #d1d5db",
                  borderRadius: "6px",
                  fontSize: "13px",
                  width: "160px",
                  fontFamily: "monospace",
                }}
              />
              <button
                style={{
                  ...btnSecondary,
                  background: adjustCashStatus.running ? "#e5e7eb" : "#10b981",
                  color: adjustCashStatus.running ? "#6b7280" : "#fff",
                  borderColor: adjustCashStatus.running ? "#d1d5db" : "#10b981",
                  cursor: adjustCashStatus.running ? "not-allowed" : "pointer",
                  whiteSpace: "nowrap",
                }}
                onClick={handleAdjustCash}
                disabled={adjustCashStatus.running}
              >
                {adjustCashStatus.running ? "..." : "Applica"}
              </button>
            </div>
            {adjustCashStatus.result && (
              <div style={{
                marginTop: "8px", padding: "10px 12px", borderRadius: "6px",
                fontSize: "12px",
                background: adjustCashStatus.result.ok ? "#ecfdf5" : "#fef2f2",
                color: adjustCashStatus.result.ok ? "#065f46" : "#991b1b",
                border: `1px solid ${adjustCashStatus.result.ok ? "#a7f3d0" : "#fecaca"}`,
              }}>
                <div>{adjustCashStatus.result.data?.message || adjustCashStatus.result.data?.error || "Stato sconosciuto"}</div>
                {adjustCashStatus.result.ok && adjustCashStatus.result.data?.new_total != null && (
                  <div style={{ marginTop: 4, fontFamily: "monospace", fontSize: "11px" }}>
                    Cash: ${adjustCashStatus.result.data.old_cash?.toLocaleString()} -> ${adjustCashStatus.result.data.new_cash?.toLocaleString()}
                    {" - "}
                    Total: ${adjustCashStatus.result.data.old_total?.toLocaleString()} -> ${adjustCashStatus.result.data.new_total?.toLocaleString()}
                  </div>
                )}
              </div>
            )}
          </div>

          {/* DB Diagnostic + run migrations */}
          <div style={{ marginTop: "20px", paddingTop: "16px", borderTop: "1px solid #f3f4f6" }}>
            <div style={{ marginBottom: "10px" }}>
              <div style={{ fontSize: "14px", fontWeight: 600, color: "#111827" }}>
                Diagnostica DB scritture
              </div>
              <div style={{ fontSize: "12px", color: "#6b7280", marginTop: "2px" }}>
                Verifica se gli agenti riescono a eseguire trade. Test write con
                quantity frazionali (0.5 BTC, sell parziali). Se la migration v11
                NUMERIC non e' applicata, il DB rifiuta con
                <code> "invalid input syntax for type integer"</code>.
              </div>
            </div>
            <div style={{ display: "flex", gap: "8px", alignItems: "center", flexWrap: "wrap" }}>
              <button
                style={{
                  ...btnSecondary,
                  background: dbDiagStatus.running ? "#e5e7eb" : "#3b82f6",
                  color: dbDiagStatus.running ? "#6b7280" : "#fff",
                  borderColor: dbDiagStatus.running ? "#d1d5db" : "#3b82f6",
                  cursor: dbDiagStatus.running ? "not-allowed" : "pointer",
                }}
                onClick={handleDbDiagnostic}
                disabled={dbDiagStatus.running}
              >
                {dbDiagStatus.running ? "Diagnostica..." : "Diagnostica DB"}
              </button>
              <button
                style={{
                  ...btnSecondary,
                  background: migrateStatus.running ? "#e5e7eb" : "#f59e0b",
                  color: migrateStatus.running ? "#6b7280" : "#fff",
                  borderColor: migrateStatus.running ? "#d1d5db" : "#f59e0b",
                  cursor: migrateStatus.running ? "not-allowed" : "pointer",
                }}
                onClick={handleRunMigrations}
                disabled={migrateStatus.running}
              >
                {migrateStatus.running ? "Migration..." : "Esegui migration"}
              </button>
            </div>

            {dbDiagStatus.result?.data && (
              <div style={{
                marginTop: "10px", padding: "12px 14px", borderRadius: "6px",
                fontSize: "12px",
                background:
                  dbDiagStatus.result.data.verdict === "OK" ? "#ecfdf5" :
                  dbDiagStatus.result.data.verdict === "MIGRATION_V11_NOT_APPLIED" ? "#fef2f2" : "#fffbeb",
                color:
                  dbDiagStatus.result.data.verdict === "OK" ? "#065f46" :
                  dbDiagStatus.result.data.verdict === "MIGRATION_V11_NOT_APPLIED" ? "#991b1b" : "#92400e",
                border: `1px solid ${
                  dbDiagStatus.result.data.verdict === "OK" ? "#a7f3d0" :
                  dbDiagStatus.result.data.verdict === "MIGRATION_V11_NOT_APPLIED" ? "#fecaca" : "#fde68a"
                }`,
              }}>
                <div style={{ fontWeight: 700, marginBottom: 6 }}>
                  {dbDiagStatus.result.data.verdict === "OK" ? "✓ " :
                   dbDiagStatus.result.data.verdict === "MIGRATION_V11_NOT_APPLIED" ? "✗ " : "⚠ "}
                  {dbDiagStatus.result.data.verdict}
                </div>
                <div style={{ marginBottom: 8 }}>
                  {dbDiagStatus.result.data.verdict_message}
                </div>
                <details>
                  <summary style={{ cursor: "pointer", fontWeight: 600 }}>
                    Dettagli test ({dbDiagStatus.result.data.results?.length})
                  </summary>
                  <div style={{ marginTop: 6, fontFamily: "monospace", fontSize: "11px" }}>
                    {dbDiagStatus.result.data.results?.map((r, i) => (
                      <div key={i} style={{
                        padding: "4px 0",
                        borderBottom: "1px dashed rgba(0,0,0,0.06)",
                        color: r.status === "error" ? "#dc2626" : r.status === "ok" ? "inherit" : "#92400e",
                      }}>
                        <strong>{r.test}</strong>: {r.status.toUpperCase()}
                        {r.message && <div style={{ color: "#6b7280" }}>{r.message}</div>}
                        {r.error && <div style={{ color: "#dc2626", marginTop: 2 }}>{r.error}</div>}
                        {r.fix_suggestion && <div style={{ color: "#0891b2", marginTop: 2 }}>{r.fix_suggestion}</div>}
                      </div>
                    ))}
                  </div>
                </details>
              </div>
            )}

            {migrateStatus.result?.data && (
              <div style={{
                marginTop: "8px", padding: "10px 12px", borderRadius: "6px",
                fontSize: "12px",
                background: migrateStatus.result.ok ? "#ecfdf5" : "#fef2f2",
                color: migrateStatus.result.ok ? "#065f46" : "#991b1b",
                border: `1px solid ${migrateStatus.result.ok ? "#a7f3d0" : "#fecaca"}`,
              }}>
                <div>{migrateStatus.result.data.message || migrateStatus.result.data.error}</div>
                {migrateStatus.result.data.hint && (
                  <div style={{ marginTop: 6, whiteSpace: "pre-wrap", fontFamily: "monospace", fontSize: "11px" }}>
                    {migrateStatus.result.data.hint}
                  </div>
                )}
              </div>
            )}
          </div>
        </div>

        <style>{`
          @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }
        `}</style>

        {/* === DIRETTIVE UTENTE (PRIORITÀ MASSIMA) === */}
        <div style={{ ...cardStyle, borderLeft: "4px solid #dc2626" }}>
          <div style={{ ...sectionTitle, color: "#dc2626" }}>
            🔴 Direttive Utente (Priorità Massima)
          </div>
          <p style={{ color: "#374151", fontSize: "0.9rem", margin: "0 0 16px 0", lineHeight: 1.6 }}>
            Testo libero che viene iniettato <strong>IN CIMA</strong> al system prompt di
            <strong> Decision (Sonnet 4.5)</strong>, <strong>Decision Crypto (R1)</strong> e
            dei <strong>Simulator</strong> (Equity + Crypto). Ha priorità su prompt base
            e principi condivisi. Usalo per imporre regole tipo:
            <em> "Non limitarti a 5 trade, lavora fino al cap di 15", "Ignora segnali contradditori se VIX &gt; 30",
            "Non shortare durante regimi rialzisti BTC".</em>
          </p>

          <textarea
            rows={10}
            style={textareaStyle}
            value={directivesText}
            onChange={(e) => setDirectivesText(e.target.value)}
            placeholder={
              "Esempio (puoi scrivere liberamente):\n\n" +
              "1. Non limitarti mai a 5 operazioni: il vero cap è 15. Apri tutte le\n" +
              "   posizioni che la tesi giustifica fino a quel cap.\n\n" +
              "2. Quando BTC fa breakout > 3% in 24h, non fare HOLD su crypto:\n" +
              "   o entri o esci esplicitamente, niente esitazioni.\n\n" +
              "3. Stop loss su conviction ALTA: usa 1/3 della distanza tecnica\n" +
              "   normale (sei più sicuro, accetti meno rumore).\n"
            }
            maxLength={5000}
          />

          <div style={{ display: "flex", justifyContent: "space-between",
                        alignItems: "center", marginTop: 8 }}>
            <div style={{ fontSize: "0.75rem", color: "#9ca3af" }}>
              {directivesText.length}/5000 caratteri
              {directivesText !== directivesTextSaved && (
                <span style={{ color: "#dc2626", marginLeft: 8 }}>· non salvato</span>
              )}
            </div>
            <div style={{ display: "flex", gap: 8 }}>
              {directivesText !== directivesTextSaved && (
                <button
                  onClick={() => setDirectivesText(directivesTextSaved)}
                  disabled={directivesSaving}
                  style={{
                    padding: "8px 14px", background: "#f3f4f6", color: "#374151",
                    border: "1px solid #d1d5db", borderRadius: 6, fontSize: "0.85rem",
                    cursor: "pointer",
                  }}
                >
                  Annulla modifiche
                </button>
              )}
              <button
                onClick={async () => {
                  setDirectivesSaving(true);
                  try {
                    const r = await fetch(`${API}/api/settings/directives`, {
                      method: "POST",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({ text: directivesText }),
                    });
                    if (r.ok) {
                      const data = await r.json();
                      const t = data.text ?? directivesText;
                      setDirectivesText(t); setDirectivesTextSaved(t);
                      showMessage("Direttive salvate. Verranno applicate ai prossimi run.");
                    } else {
                      showMessage("Errore salvataggio direttive.", true);
                    }
                  } catch {
                    showMessage("Errore di rete.", true);
                  } finally {
                    setDirectivesSaving(false);
                  }
                }}
                disabled={directivesSaving || directivesText === directivesTextSaved}
                style={{
                  padding: "8px 18px", background: "#dc2626", color: "#fff",
                  border: "none", borderRadius: 6, fontWeight: 600, fontSize: "0.85rem",
                  cursor: (directivesSaving || directivesText === directivesTextSaved)
                            ? "not-allowed" : "pointer",
                  opacity: (directivesSaving || directivesText === directivesTextSaved) ? 0.5 : 1,
                }}
              >
                {directivesSaving ? "Salvo..." : "Salva direttive"}
              </button>
            </div>
          </div>
        </div>

        {/* === RISK PROFILE (HARD CONSTRAINTS) === */}
        <div style={{ ...cardStyle, borderLeft: "4px solid #f59e0b" }}>
          <div style={{ ...sectionTitle, color: "#b45309" }}>
            ⚖️  Profilo di Rischio (Vincoli Numerici Hard)
          </div>
          <p style={{ color: "#374151", fontSize: "0.9rem", margin: "0 0 16px 0", lineHeight: 1.6 }}>
            Definisce i <strong>vincoli numerici hard</strong> applicati al
            Decision Agent (sia equity sia crypto) <em>prima</em> di eseguire
            qualsiasi trade. Sono soglie che il sistema rifiuta automaticamente
            se violate (max allocazione, confidence minima, range Stop Loss,
            ecc). Cambia profilo per influenzare quanto rischio si prende.
          </p>

          {/* Selettore profilo */}
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 18 }}>
            {["conservative", "moderate", "aggressive"].map((key) => {
              const prof = riskProfiles?.[key];
              if (!prof) return null;
              const active = riskActiveKey === key;
              return (
                <button
                  key={key}
                  onClick={() => setRiskActiveKey(key)}
                  style={{
                    flex: "1 1 220px",
                    padding: "14px 16px",
                    background: active ? "#fef3c7" : "#fff",
                    border: active ? "2px solid #f59e0b" : "1px solid #d1d5db",
                    borderRadius: 8,
                    cursor: "pointer",
                    textAlign: "left",
                    transition: "all 0.15s",
                  }}
                >
                  <div style={{ fontSize: "1rem", fontWeight: 700, color: "#111827" }}>
                    {prof.icon} {prof.label}
                  </div>
                  <div style={{ fontSize: "0.78rem", color: "#6b7280", marginTop: 4, lineHeight: 1.4 }}>
                    {prof.summary}
                  </div>
                </button>
              );
            })}
          </div>

          {/* Tabella comparativa */}
          {riskProfiles && (
            <div style={{
              overflowX: "auto", marginBottom: 14,
              border: "1px solid #e5e7eb", borderRadius: 8, background: "#fff",
            }}>
              <table style={{
                width: "100%", borderCollapse: "collapse", fontSize: "0.83rem",
                fontFamily: "system-ui, sans-serif",
              }}>
                <thead>
                  <tr style={{ background: "#f9fafb", borderBottom: "1px solid #e5e7eb" }}>
                    <th style={thStyle}>Parametro</th>
                    {["conservative", "moderate", "aggressive"].map((k) => (
                      <th
                        key={k}
                        style={{
                          ...thStyle,
                          background: riskActiveKey === k ? "#fef3c7" : "transparent",
                          color: riskActiveKey === k ? "#92400e" : "#374151",
                        }}
                      >
                        {riskProfiles[k]?.icon} {riskProfiles[k]?.label}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {[
                    {
                      label: "Max % per trade — Equity/ETF",
                      get: (p) => `${p.max_position_pct_equity?.toFixed(1)}%`,
                      hint: "Cap allocazione su singola posizione azionaria/ETF (rispetto al cash disponibile)",
                    },
                    {
                      label: "Max % per trade — Crypto",
                      get: (p) => `${p.max_position_pct_crypto?.toFixed(1)}%`,
                      hint: "Cap allocazione su singola posizione crypto (volatilità più alta → cap più stretto)",
                    },
                    {
                      label: "Confidence minima per eseguire",
                      get: (p) => p.min_confidence?.toFixed(2),
                      hint: "Sotto questa soglia (0–1), il trade viene rifiutato automaticamente. Forza l'AI ad aspettare setup chiari",
                    },
                    {
                      label: "Stop Loss range — Equity",
                      get: (p) => `${p.sl_min_pct_equity}%–${p.sl_max_pct_equity}%`,
                      hint: "Distanza % obbligatoria del SL dall'entry, fuori da questo range → trade rifiutato",
                    },
                    {
                      label: "Stop Loss range — Crypto",
                      get: (p) => `${p.sl_min_pct_crypto}%–${p.sl_max_pct_crypto}%`,
                      hint: "Range più ampio per la volatilità crypto",
                    },
                    {
                      label: "Posizioni aperte simultanee (max)",
                      get: (p) => `${p.max_open_positions}`,
                      hint: "Numero massimo di posizioni aperte sul portafoglio. Più alto = più diversificazione, ma anche più rumore",
                    },
                    {
                      label: "Stop portfolio (drawdown max)",
                      get: (p) => `${p.max_portfolio_drawdown_pct}%`,
                      hint: "Sopra questa perdita dal capitale iniziale, il sistema blocca nuove APERTURE (chiusure restano permesse)",
                    },
                    {
                      label: "News max age (catalyst)",
                      get: (p) => `${p.news_freshness_min_hours}h`,
                      hint: "Le notizie più vecchie di questo limite NON sono considerate catalyst valido",
                    },
                  ].map((row, i) => (
                    <tr
                      key={i}
                      style={{ borderTop: i > 0 ? "1px solid #f3f4f6" : "none" }}
                    >
                      <td style={{ ...tdStyle, color: "#374151", fontWeight: 500 }}>
                        <div>{row.label}</div>
                        <div style={{ fontSize: "0.72rem", color: "#9ca3af",
                                      marginTop: 2, lineHeight: 1.3, fontWeight: 400 }}>
                          {row.hint}
                        </div>
                      </td>
                      {["conservative", "moderate", "aggressive"].map((k) => (
                        <td
                          key={k}
                          style={{
                            ...tdStyle,
                            textAlign: "center",
                            background: riskActiveKey === k ? "#fffbeb" : "transparent",
                            color: riskActiveKey === k ? "#92400e" : "#1f2937",
                            fontWeight: riskActiveKey === k ? 700 : 500,
                            fontFamily: "monospace",
                          }}
                        >
                          {riskProfiles[k] ? row.get(riskProfiles[k]) : "—"}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* Bottoni salva/annulla */}
          <div style={{ display: "flex", justifyContent: "space-between",
                        alignItems: "center", marginTop: 12 }}>
            <div style={{ fontSize: "0.78rem", color: "#6b7280" }}>
              Profilo attivo: <strong>{riskProfiles?.[riskActiveSaved]?.label || riskActiveSaved}</strong>
              {riskActiveKey !== riskActiveSaved && (
                <span style={{ color: "#dc2626", marginLeft: 8 }}>
                  · modifica non salvata
                </span>
              )}
            </div>
            <div style={{ display: "flex", gap: 8 }}>
              {riskActiveKey !== riskActiveSaved && (
                <button
                  onClick={() => setRiskActiveKey(riskActiveSaved)}
                  disabled={riskSaving}
                  style={{
                    padding: "8px 14px", background: "#f3f4f6", color: "#374151",
                    border: "1px solid #d1d5db", borderRadius: 6, fontSize: "0.85rem",
                    cursor: "pointer",
                  }}
                >
                  Annulla
                </button>
              )}
              <button
                onClick={async () => {
                  setRiskSaving(true);
                  try {
                    const r = await fetch(`${API}/api/settings/risk-profile`, {
                      method: "POST",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({ profile: riskActiveKey }),
                    });
                    if (r.ok) {
                      const data = await r.json();
                      const k = data.active_key || riskActiveKey;
                      setRiskActiveKey(k);
                      setRiskActiveSaved(k);
                      showMessage(`Profilo "${riskProfiles?.[k]?.label || k}" salvato. Verrà applicato ai prossimi run.`);
                    } else {
                      showMessage("Errore salvataggio profilo.", true);
                    }
                  } catch {
                    showMessage("Errore di rete.", true);
                  } finally {
                    setRiskSaving(false);
                  }
                }}
                disabled={riskSaving || riskActiveKey === riskActiveSaved}
                style={{
                  padding: "8px 18px", background: "#f59e0b", color: "#fff",
                  border: "none", borderRadius: 6, fontWeight: 600, fontSize: "0.85rem",
                  cursor: (riskSaving || riskActiveKey === riskActiveSaved) ? "not-allowed" : "pointer",
                  opacity: (riskSaving || riskActiveKey === riskActiveSaved) ? 0.5 : 1,
                }}
              >
                {riskSaving ? "Salvo..." : "Applica profilo"}
              </button>
            </div>
          </div>
        </div>

        {/* === PROMPT DI SISTEMA (3 AGENTI) === */}
        <div style={cardStyle}>
          <div style={sectionTitle}>Prompt di Sistema per Agente</div>
          <p style={{ color: "#6b7280", fontSize: "0.85rem", margin: "0 0 20px 0", lineHeight: 1.5 }}>
            Personalizza il comportamento di ogni agente del workflow. Lasciando vuoto il campo,
            verra' usato il prompt di default integrato.
            Il modello e l'intervallo di esecuzione sono fissi e non configurabili: Watchdog (DeepSeek-V3) ogni 1 min 24/7,
            Scout (DeepSeek-V3) ogni 20 min 24/7 + report aggregati 8H/4D, Technical (DeepSeek-V3) on-demand.
            Il Decision Agent ha due profili: <strong>Sonnet 4.5</strong> durante orari di mercato (max 1/h) e
            <strong>DeepSeek-R1</strong> overnight per crypto 24/7 (max 1 ogni 2h30).
          </p>

          {PROMPT_AGENTS.map(({ key, label, description }) => (
            <div key={key} style={{ marginBottom: "28px" }}>
              <div style={{ marginBottom: "8px" }}>
                <span style={{ fontSize: "14px", fontWeight: 600, color: "#111827" }}>{label}</span>
                <div style={{ fontSize: "12px", color: "#9ca3af", marginTop: "2px" }}>{description}</div>
              </div>
              <textarea
                rows={10}
                style={textareaStyle}
                value={prompts[key]}
                onChange={(e) => setPrompts((prev) => ({ ...prev, [key]: e.target.value }))}
                placeholder={promptDefaults[key] || "Prompt di default..."}
                onFocus={handleFocus}
                onBlur={handleBlur}
              />
              <div style={{ display: "flex", gap: "8px", marginTop: "8px" }}>
                <button
                  style={{ ...btnPrimary, opacity: saving ? 0.6 : 1,
                    cursor: saving ? "not-allowed" : "pointer" }}
                  onClick={() => handleSavePrompt(key)}
                  disabled={saving}
                >
                  {saving ? "Salvando..." : "Salva"}
                </button>
                <button
                  style={btnSecondary}
                  onClick={() => handleResetPrompt(key)}
                  disabled={saving}
                >
                  Ripristina default
                </button>
              </div>
            </div>
          ))}
        </div>

        {/* === DOCUMENTI GENERICI (Equity / ETF) === */}
        <div style={cardStyle}>
          <div style={sectionTitle}>Documenti — Mercati Tradizionali (Equity/ETF)</div>
          <p style={{ color: "#6b7280", fontSize: "0.85rem", margin: "0 0 14px 0" }}>
            Caricati nel context del Decision normale (Sonnet 4.5) e del Technical normale.
            NON visibili al Decision Crypto.
          </p>

          <input type="file" accept=".pdf,.txt" ref={fileInputRef}
            onChange={(e) => handleFileUpload(e, "generic")} style={{ display: "none" }} />
          <button style={{ ...btnPrimary, marginBottom: "16px" }}
            onClick={() => fileInputRef.current?.click()}>
            Carica Documento Generico
          </button>

          {documents.filter(d => (d.category || "generic") === "generic").length > 0 ? (
            <div style={{ borderRadius: "8px", overflow: "hidden", border: "1px solid #e5e7eb" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "14px" }}>
                <thead>
                  <tr style={{ background: "#f9fafb" }}>
                    <th style={{ textAlign: "left", padding: "10px 14px", fontWeight: 500,
                      color: "#6b7280", fontSize: "13px", borderBottom: "1px solid #e5e7eb" }}>Nome File</th>
                    <th style={{ textAlign: "left", padding: "10px 14px", fontWeight: 500,
                      color: "#6b7280", fontSize: "13px", borderBottom: "1px solid #e5e7eb" }}>Data Upload</th>
                    <th style={{ textAlign: "left", padding: "10px 14px", fontWeight: 500,
                      color: "#6b7280", fontSize: "13px", borderBottom: "1px solid #e5e7eb" }}>Dimensione</th>
                    <th style={{ width: "40px", borderBottom: "1px solid #e5e7eb" }} />
                  </tr>
                </thead>
                <tbody>
                  {documents.filter(d => (d.category || "generic") === "generic").map((doc, i, arr) => (
                    <tr key={doc.id} style={{
                      borderBottom: i < arr.length - 1 ? "1px solid #f3f4f6" : "none" }}>
                      <td style={{ padding: "10px 14px", color: "#111827", fontWeight: 500 }}>{doc.filename}</td>
                      <td style={{ padding: "10px 14px", color: "#6b7280" }}>{formatDate(doc.uploaded_at)}</td>
                      <td style={{ padding: "10px 14px", color: "#6b7280" }}>{formatFileSize(doc.file_size)}</td>
                      <td style={{ padding: "10px 14px", textAlign: "center" }}>
                        <button onClick={() => handleDeleteDoc(doc.id)}
                          style={{ ...removeBtn, color: "#ef4444", fontSize: "14px", fontWeight: 600 }}
                          title="Elimina documento">&#10005;</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div style={{ padding: "24px", textAlign: "center", color: "#9ca3af",
              fontSize: "14px", background: "#f9fafb", borderRadius: "8px",
              border: "1px dashed #e5e7eb" }}>Nessun documento generico caricato</div>
          )}
        </div>

        {/* === DOCUMENTI CRYPTO === */}
        <div style={cardStyle}>
          <div style={sectionTitle}>Documenti — Crypto-only (per Decision Crypto)</div>
          <p style={{ color: "#6b7280", fontSize: "0.85rem", margin: "0 0 14px 0" }}>
            Caricati ESCLUSIVAMENTE nel context del Decision Crypto (DeepSeek-R1).
            Ideali per: framework on-chain, strategie altcoin, piani di entry/exit
            su BTC/ETH, regole di stop-loss crypto-specifiche, tabelle di liquidazione.
          </p>

          <input type="file" accept=".pdf,.txt" id="cryptoFileInput"
            onChange={(e) => handleFileUpload(e, "crypto")} style={{ display: "none" }} />
          <button style={{ ...btnPrimary, marginBottom: "16px",
                           background: "#f472b6", borderColor: "#f472b6" }}
            onClick={() => document.getElementById("cryptoFileInput")?.click()}>
            Carica Documento Crypto
          </button>

          {documents.filter(d => (d.category || "generic") === "crypto").length > 0 ? (
            <div style={{ borderRadius: "8px", overflow: "hidden", border: "1px solid #fbcfe8" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "14px" }}>
                <thead>
                  <tr style={{ background: "#fdf2f8" }}>
                    <th style={{ textAlign: "left", padding: "10px 14px", fontWeight: 500,
                      color: "#9d174d", fontSize: "13px", borderBottom: "1px solid #fbcfe8" }}>Nome File</th>
                    <th style={{ textAlign: "left", padding: "10px 14px", fontWeight: 500,
                      color: "#9d174d", fontSize: "13px", borderBottom: "1px solid #fbcfe8" }}>Data Upload</th>
                    <th style={{ textAlign: "left", padding: "10px 14px", fontWeight: 500,
                      color: "#9d174d", fontSize: "13px", borderBottom: "1px solid #fbcfe8" }}>Dimensione</th>
                    <th style={{ width: "40px", borderBottom: "1px solid #fbcfe8" }} />
                  </tr>
                </thead>
                <tbody>
                  {documents.filter(d => (d.category || "generic") === "crypto").map((doc, i, arr) => {
                    const isPreset = doc.is_preset === true || doc.is_preset === 1;
                    return (
                    <tr key={doc.id} style={{
                      borderBottom: i < arr.length - 1 ? "1px solid #fce7f3" : "none" }}>
                      <td style={{ padding: "10px 14px", color: "#111827", fontWeight: 500 }}>
                        {doc.filename}
                        {isPreset && (
                          <span style={{ marginLeft: 8, fontSize: 10, fontWeight: 700,
                                         padding: "2px 6px", borderRadius: 3,
                                         background: "#fbcfe8", color: "#9d174d" }}>
                            PRESET
                          </span>
                        )}
                      </td>
                      <td style={{ padding: "10px 14px", color: "#6b7280" }}>{formatDate(doc.uploaded_at)}</td>
                      <td style={{ padding: "10px 14px", color: "#6b7280" }}>{formatFileSize(doc.file_size)}</td>
                      <td style={{ padding: "10px 14px", textAlign: "center" }}>
                        {isPreset ? (
                          <span title="I preset non sono eliminabili" style={{ color: "#9ca3af", fontSize: 14 }}>🔒</span>
                        ) : (
                          <button onClick={() => handleDeleteDoc(doc.id)}
                            style={{ ...removeBtn, color: "#ef4444", fontSize: "14px", fontWeight: 600 }}
                            title="Elimina documento crypto">&#10005;</button>
                        )}
                      </td>
                    </tr>
                  );})}
                </tbody>
              </table>
            </div>
          ) : (
            <div style={{ padding: "24px", textAlign: "center", color: "#9ca3af",
              fontSize: "14px", background: "#fdf2f8", borderRadius: "8px",
              border: "1px dashed #fbcfe8" }}>Nessun documento crypto caricato</div>
          )}
        </div>

      </div>
    </div>
  );
}

export default Settings;
