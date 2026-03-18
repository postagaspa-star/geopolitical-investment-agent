import React, { useState, useEffect, useRef } from "react";

// Base URL per le chiamate API (stesso server)
const API = window.location.origin;

// Watchlist predefinita
const DEFAULT_WATCHLIST = {
  energy: ["XOM", "CVX", "SHEL", "TTE", "ENI"],
  defense: ["LMT", "RTX", "NOC", "BA", "LDOS"],
  gold_commodities: ["GLD", "SLV", "USO", "UNG"],
  etf_broad: ["SPY", "QQQ", "EEM", "VEA"],
  europe: ["EWG", "EWI", "EWQ", "EWP"],
};

// Prompt di sistema predefinito
const DEFAULT_SYSTEM_PROMPT =
  "Sei un agente di investimento geopolitico. Analizza le notizie geopolitiche globali e i dati tecnici di mercato per prendere decisioni di trading informate. Valuta i rischi, identifica opportunit\u00e0 e gestisci il portafoglio in modo prudente.";

// Modelli disponibili (nomi API ufficiali Anthropic)
const MODEL_OPTIONS = [
  "claude-sonnet-4-5-20250929",
  "claude-opus-4-20250514",
  "claude-sonnet-4-20250514",
  "claude-3-5-haiku-20241022",
];

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

function Settings({ onBack }) {
  // === Stato Sezione 0: Diagnostica Sistema ===
  const [diagnostics, setDiagnostics] = useState(INITIAL_DIAGNOSTICS);

  // === Stato Sezione 1: Profilo Agente ===
  const [systemPrompt, setSystemPrompt] = useState(DEFAULT_SYSTEM_PROMPT);
  const [modelName, setModelName] = useState(MODEL_OPTIONS[0]);
  const [runInterval, setRunInterval] = useState(6);

  // === Stato Sezione 2: Documenti ===
  const [documents, setDocuments] = useState([]);
  const fileInputRef = useRef(null);

  // === Stato Sezione 3: Configurazione Portafoglio ===
  const [initialBalance, setInitialBalance] = useState(100000);
  const [maxPositionPct, setMaxPositionPct] = useState(10);
  const [stopLossThreshold, setStopLossThreshold] = useState(-15);
  const [maxOpenPositions, setMaxOpenPositions] = useState(8);
  const [minConfidence, setMinConfidence] = useState(40);

  // === Stato Sezione 4: Watchlist ===
  const [watchlist, setWatchlist] = useState(DEFAULT_WATCHLIST);
  const [newTickerInputs, setNewTickerInputs] = useState({});
  const [newSectorName, setNewSectorName] = useState("");

  // === Stato Sezione 5: API Keys ===
  const [anthropicKey, setAnthropicKey] = useState("");
  const [newsApiKey, setNewsApiKey] = useState("");
  const [finnhubKey, setFinnhubKey] = useState("");
  const [connectionTest, setConnectionTest] = useState(null);

  // === Stato UI generale ===
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState(null);

  // Aggiorna un singolo item nella diagnostica
  const setDiagItem = (key, status, msg = "") => {
    setDiagnostics((prev) => ({
      ...prev,
      [key]: { status, message: msg },
    }));
  };

  // Mostra messaggio temporaneo di feedback
  const showMessage = (text, isError = false) => {
    setMessage({ text, isError });
    setTimeout(() => setMessage(null), 3000);
  };

  // === Diagnostica: eseguita al mount in parallelo ===
  const runDiagnostics = async (docsData) => {
    // Test Anthropic
    const testAnthropic = async () => {
      try {
        const res = await fetch(`${API}/api/settings/test-anthropic`);
        const data = await res.json();
        if (data.status === "ok") {
          setDiagItem("anthropic", "ok");
        } else {
          setDiagItem("anthropic", "error", data.message || "Risposta non valida");
        }
      } catch (err) {
        setDiagItem("anthropic", "error", err.message);
      }
    };

    // Test NewsAPI
    const testNewsapi = async () => {
      try {
        const res = await fetch(`${API}/api/settings/test-newsapi`);
        const data = await res.json();
        if (data.status === "ok") {
          setDiagItem("newsapi", "ok");
        } else {
          setDiagItem("newsapi", "error", data.message || "Risposta non valida");
        }
      } catch (err) {
        setDiagItem("newsapi", "error", err.message);
      }
    };

    // Test Finnhub
    const testFinnhub = async () => {
      try {
        const res = await fetch(`${API}/api/settings/test-finnhub`);
        const data = await res.json();
        if (data.status === "ok") {
          setDiagItem("finnhub", "ok");
        } else {
          setDiagItem("finnhub", "error", data.message || "Risposta non valida");
        }
      } catch (err) {
        setDiagItem("finnhub", "error", err.message);
      }
    };

    // Test GDELT (via backend per evitare CORS)
    const testGdelt = async () => {
      try {
        const res = await fetch(`${API}/api/settings/test-gdelt`);
        const data = await res.json();
        if (data.status === "ok") {
          setDiagItem("gdelt", "ok");
        } else {
          setDiagItem("gdelt", "error", data.message || "Risposta non valida");
        }
      } catch (err) {
        setDiagItem("gdelt", "error", err.message);
      }
    };

    // Test yfinance
    const testYfinance = async () => {
      try {
        const res = await fetch(`${API}/api/settings/test-yfinance`);
        const data = await res.json();
        if (data.status === "ok") {
          setDiagItem("yfinance", "ok");
        } else {
          setDiagItem("yfinance", "error", data.message || "Risposta non valida");
        }
      } catch (err) {
        setDiagItem("yfinance", "error", err.message);
      }
    };

    // Test DB
    const testDb = async () => {
      try {
        const res = await fetch(`${API}/api/settings/test-db`);
        const data = await res.json();
        if (data.status === "ok") {
          setDiagItem("db", "ok");
        } else {
          setDiagItem("db", "error", data.message || "Risposta non valida");
        }
      } catch (err) {
        setDiagItem("db", "error", err.message);
      }
    };

    // Test Scheduler (stato agente)
    const testScheduler = async () => {
      try {
        const res = await fetch(`${API}/api/agent/status`);
        const data = await res.json();
        const running = data.is_running || data.scheduler_running || data.status === "running";
        if (running) {
          setDiagItem("scheduler", "ok", "In esecuzione");
        } else {
          setDiagItem("scheduler", "ok", "Attivo (idle)");
        }
      } catch (err) {
        setDiagItem("scheduler", "error", err.message);
      }
    };

    // Documenti: usa i dati già caricati
    const checkDocuments = (docs) => {
      const count = Array.isArray(docs) ? docs.length : 0;
      setDiagItem("documents", "ok", `${count} ${count !== 1 ? "documenti caricati" : "documento caricato"}`);
    };

    // Lancia tutto in parallelo
    await Promise.all([
      testAnthropic(),
      testNewsapi(),
      testFinnhub(),
      testGdelt(),
      testYfinance(),
      testDb(),
      testScheduler(),
    ]);

    checkDocuments(docsData);
  };

  // === Caricamento iniziale di tutte le impostazioni ===
  useEffect(() => {
    const loadSettings = async () => {
      try {
        const res = await fetch(`${API}/api/settings`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const raw = await res.json();
        // Il backend restituisce {"settings": {...}} — estrai il contenuto
        const data = raw.settings ?? raw;

        // Profilo agente
        if (data.system_prompt) setSystemPrompt(data.system_prompt);
        if (data.model_name) setModelName(data.model_name);
        if (data.agent_run_interval_hours)
          setRunInterval(Number(data.agent_run_interval_hours));

        // Configurazione portafoglio
        if (data.initial_balance != null)
          setInitialBalance(Number(data.initial_balance));
        if (data.max_position_pct != null)
          setMaxPositionPct(Number(data.max_position_pct) * 100);
        if (data.stop_loss_threshold != null)
          setStopLossThreshold(Number(data.stop_loss_threshold) * 100);
        if (data.max_open_positions != null)
          setMaxOpenPositions(Number(data.max_open_positions));
        if (data.min_confidence != null)
          setMinConfidence(Number(data.min_confidence));

        // Watchlist (stringa JSON o oggetto)
        if (data.watchlist) {
          try {
            const parsed =
              typeof data.watchlist === "string"
                ? JSON.parse(data.watchlist)
                : data.watchlist;
            setWatchlist(parsed);
          } catch (e) {
            console.error("Errore parsing watchlist:", e);
          }
        }

        // API Keys
        if (data.anthropic_api_key) setAnthropicKey(data.anthropic_api_key);
        if (data.news_api_key) setNewsApiKey(data.news_api_key);
        if (data.finnhub_api_key) setFinnhubKey(data.finnhub_api_key);
      } catch (err) {
        console.error("Errore caricamento impostazioni:", err);
      }
    };

    const init = async () => {
      // Carica impostazioni e documenti in parallelo
      const [, docsData] = await Promise.all([
        loadSettings(),
        loadDocumentsRaw(),
      ]);
      // Lancia diagnostica con il conteggio documenti già disponibile
      runDiagnostics(docsData);
    };

    init();
  }, []);

  // Caricamento lista documenti dal server (restituisce i dati)
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

  // Caricamento lista documenti dal server (aggiorna stato)
  const loadDocuments = async () => {
    const docs = await loadDocumentsRaw();
    return docs;
  };

  // === Salvataggio generico impostazioni via POST ===
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

  // === Handler Sezione 1: Salva profilo agente ===
  const handleSaveProfile = () => {
    saveSettings({
      system_prompt: systemPrompt,
      model_name: modelName,
      agent_run_interval_hours: runInterval,
    });
  };

  // === Handler Sezione 2: Upload e gestione documenti ===
  const handleFileUpload = async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const formData = new FormData();
    formData.append("file", file);
    try {
      const res = await fetch(`${API}/api/documents/upload`, {
        method: "POST",
        body: formData,
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      showMessage("Documento caricato con successo");
      const docs = await loadDocuments();
      setDiagItem(
        "documents",
        "ok",
        `${docs.length} ${docs.length !== 1 ? "documenti caricati" : "documento caricato"}`
      );
    } catch (err) {
      showMessage(`Errore upload: ${err.message}`, true);
    }
    // Reset input per permettere upload dello stesso file
    e.target.value = "";
  };

  // Elimina un documento per ID
  const handleDeleteDoc = async (docId) => {
    try {
      const res = await fetch(`${API}/api/documents/${docId}`, {
        method: "DELETE",
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      showMessage("Documento eliminato");
      const docs = await loadDocuments();
      setDiagItem(
        "documents",
        "ok",
        `${docs.length} ${docs.length !== 1 ? "documenti caricati" : "documento caricato"}`
      );
    } catch (err) {
      showMessage(`Errore eliminazione: ${err.message}`, true);
    }
  };

  // Formattazione dimensione file in KB
  const formatFileSize = (bytes) => {
    if (bytes == null) return "-";
    return `${(bytes / 1024).toFixed(1)} KB`;
  };

  // Formattazione data in formato italiano
  const formatDate = (dateStr) => {
    if (!dateStr) return "-";
    return new Date(dateStr).toLocaleDateString("it-IT", {
      day: "2-digit",
      month: "short",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  };

  // === Handler Sezione 3: Salva configurazione portafoglio ===
  const handleSavePortfolio = () => {
    saveSettings({
      initial_balance: initialBalance,
      max_position_pct: maxPositionPct / 100,
      stop_loss_threshold: stopLossThreshold / 100,
      max_open_positions: maxOpenPositions,
      min_confidence: minConfidence,
    });
  };

  // Reset completo del portafoglio con conferma
  const handleResetPortfolio = async () => {
    if (
      !window.confirm(
        "Sei sicuro di voler resettare il portafoglio? Questa azione \u00e8 irreversibile."
      )
    ) {
      return;
    }
    try {
      const res = await fetch(`${API}/api/portfolio/reset`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ new_balance: initialBalance }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      showMessage("Portafoglio resettato con successo");
    } catch (err) {
      showMessage(`Errore reset: ${err.message}`, true);
    }
  };

  // === Handler Sezione 4: Gestione watchlist ===
  const handleAddTicker = (sector) => {
    const ticker = (newTickerInputs[sector] || "").trim().toUpperCase();
    if (!ticker) return;
    if (watchlist[sector]?.includes(ticker)) {
      showMessage(`${ticker} \u00e8 gi\u00e0 presente in ${sector}`, true);
      return;
    }
    setWatchlist((prev) => ({
      ...prev,
      [sector]: [...(prev[sector] || []), ticker],
    }));
    setNewTickerInputs((prev) => ({ ...prev, [sector]: "" }));
  };

  const handleRemoveTicker = (sector, ticker) => {
    setWatchlist((prev) => ({
      ...prev,
      [sector]: prev[sector].filter((t) => t !== ticker),
    }));
  };

  const handleAddSector = () => {
    const name = newSectorName.trim().toLowerCase().replace(/\s+/g, "_");
    if (!name) return;
    if (watchlist[name]) {
      showMessage("Settore gi\u00e0 esistente", true);
      return;
    }
    setWatchlist((prev) => ({ ...prev, [name]: [] }));
    setNewSectorName("");
  };

  const handleSaveWatchlist = () => {
    saveSettings({ watchlist: JSON.stringify(watchlist) });
  };

  // === Handler Sezione 5: Test connessione e salvataggio API keys ===
  const handleTestConnection = async () => {
    setConnectionTest({ loading: true });
    try {
      const res = await fetch(`${API}/api/test-connection`, {
        method: "POST",
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setConnectionTest(data);
    } catch (err) {
      setConnectionTest({ error: err.message });
    }
  };

  const handleSaveApiKeys = () => {
    const settings = {};
    if (anthropicKey) settings.anthropic_api_key = anthropicKey;
    if (newsApiKey) settings.news_api_key = newsApiKey;
    if (finnhubKey) settings.finnhub_api_key = finnhubKey;
    saveSettings(settings);
  };

  // Re-run diagnostica manualmente
  const handleRerunDiagnostics = async () => {
    setDiagnostics(INITIAL_DIAGNOSTICS);
    const docs = await loadDocumentsRaw();
    runDiagnostics(docs);
  };

  // === Stili inline riutilizzabili ===
  const inputStyle = {
    width: "100%",
    padding: "10px 12px",
    background: "#ffffff",
    border: "1px solid #d1d5db",
    borderRadius: "8px",
    color: "#111827",
    fontSize: "14px",
    fontFamily: "'Inter', -apple-system, sans-serif",
    outline: "none",
    transition: "border-color 0.15s, box-shadow 0.15s",
    boxSizing: "border-box",
  };

  const textareaStyle = {
    ...inputStyle,
    fontFamily: "'Inter', -apple-system, sans-serif",
    fontSize: "14px",
    lineHeight: "1.6",
    resize: "vertical",
    minHeight: "200px",
  };

  const selectStyle = {
    ...inputStyle,
    cursor: "pointer",
    appearance: "none",
    backgroundImage: "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%236b7280' d='M6 8L1 3h10z'/%3E%3C/svg%3E\")",
    backgroundRepeat: "no-repeat",
    backgroundPosition: "right 12px center",
    paddingRight: "32px",
  };

  const labelStyle = {
    display: "block",
    fontSize: "13px",
    color: "#6b7280",
    marginBottom: "6px",
    fontWeight: 500,
  };

  const fieldGroup = {
    marginBottom: "16px",
  };

  const cardStyle = {
    background: "#ffffff",
    border: "1px solid #e5e7eb",
    borderRadius: "12px",
    padding: "24px",
    marginBottom: "24px",
    boxShadow: "0 1px 3px rgba(0,0,0,0.04)",
  };

  const sectionTitle = {
    fontSize: "16px",
    fontWeight: 600,
    color: "#111827",
    margin: 0,
    marginBottom: "20px",
  };

  const btnPrimary = {
    padding: "10px 20px",
    background: "#3b82f6",
    color: "#ffffff",
    border: "none",
    borderRadius: "8px",
    cursor: "pointer",
    fontSize: "14px",
    fontWeight: 500,
    fontFamily: "'Inter', -apple-system, sans-serif",
    transition: "background 0.15s",
  };

  const btnDanger = {
    padding: "10px 20px",
    border: "1px solid #ef4444",
    background: "#ffffff",
    color: "#ef4444",
    borderRadius: "8px",
    cursor: "pointer",
    fontSize: "14px",
    fontWeight: 500,
    fontFamily: "'Inter', -apple-system, sans-serif",
  };

  const btnSecondary = {
    padding: "8px 14px",
    background: "#f3f4f6",
    color: "#374151",
    border: "1px solid #d1d5db",
    borderRadius: "8px",
    cursor: "pointer",
    fontSize: "13px",
    fontWeight: 500,
    fontFamily: "'Inter', -apple-system, sans-serif",
  };

  const tickerBadge = {
    display: "inline-flex",
    alignItems: "center",
    gap: "6px",
    padding: "4px 10px",
    background: "#eff6ff",
    border: "1px solid #bfdbfe",
    borderRadius: "6px",
    fontSize: "13px",
    fontWeight: 600,
    color: "#1d4ed8",
    margin: "3px",
  };

  const removeBtn = {
    background: "none",
    border: "none",
    color: "#9ca3af",
    cursor: "pointer",
    fontSize: "11px",
    padding: "0 2px",
    lineHeight: 1,
    transition: "color 0.15s",
  };

  const inlineRow = {
    display: "flex",
    gap: "8px",
    alignItems: "center",
  };

  // Dati di configurazione per i diag-item
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

  // Focus style handler helper
  const handleFocus = (e) => {
    e.target.style.borderColor = "#3b82f6";
    e.target.style.boxShadow = "0 0 0 3px rgba(59,130,246,0.1)";
  };
  const handleBlur = (e) => {
    e.target.style.borderColor = "#d1d5db";
    e.target.style.boxShadow = "none";
  };

  return (
    <div
      style={{
        background: "#f9fafb",
        minHeight: "100vh",
        padding: "32px",
        color: "#111827",
        fontFamily: "'Inter', -apple-system, sans-serif",
      }}
    >
      <div style={{ maxWidth: "720px", margin: "0 auto" }}>

        {/* Page title */}
        <h1
          style={{
            fontSize: "24px",
            fontWeight: 700,
            color: "#111827",
            marginBottom: "24px",
            letterSpacing: "-0.02em",
          }}
        >
          Impostazioni
        </h1>

        {/* Messaggio di feedback globale */}
        {message && (
          <div
            style={{
              padding: "10px 16px",
              marginBottom: "16px",
              borderRadius: "8px",
              fontSize: "14px",
              fontWeight: 500,
              background: message.isError ? "#fef2f2" : "#ecfdf5",
              color: message.isError ? "#991b1b" : "#065f46",
              border: `1px solid ${message.isError ? "#fecaca" : "#a7f3d0"}`,
            }}
          >
            {message.text}
          </div>
        )}

        {/* ============================================= */}
        {/* SEZIONE 0: DIAGNOSTICA SISTEMA               */}
        {/* ============================================= */}
        <div style={cardStyle}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              marginBottom: "16px",
            }}
          >
            <div style={{ ...sectionTitle, marginBottom: 0 }}>
              Diagnostica Sistema
            </div>
            <button
              style={btnSecondary}
              onClick={handleRerunDiagnostics}
            >
              Riaggiorna
            </button>
          </div>

          <div>
            {diagItems.map(({ key, name }, index) => {
              const diag = diagnostics[key];
              const dotColor =
                diag.status === "ok"
                  ? "#10b981"
                  : diag.status === "error"
                  ? "#ef4444"
                  : "#9ca3af";
              return (
                <div key={key}>
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: "10px",
                      padding: "10px 0",
                    }}
                  >
                    <span
                      style={{
                        width: "8px",
                        height: "8px",
                        borderRadius: "50%",
                        background: dotColor,
                        flexShrink: 0,
                        animation:
                          diag.status === "loading"
                            ? "pulse 1.5s ease-in-out infinite"
                            : "none",
                      }}
                    />
                    <span
                      style={{
                        fontSize: "14px",
                        color: "#374151",
                        fontWeight: 500,
                        minWidth: "130px",
                      }}
                    >
                      {name}
                    </span>
                    <span
                      style={{
                        fontSize: "13px",
                        color: "#6b7280",
                      }}
                    >
                      {diag.status === "loading" && "Verifica..."}
                      {diag.status === "ok" &&
                        `OK${diag.message ? ` — ${diag.message}` : ""}`}
                      {diag.status === "error" &&
                        `Errore${diag.message ? `: ${diag.message}` : ""}`}
                    </span>
                  </div>
                  {index < diagItems.length - 1 && (
                    <div
                      style={{
                        height: "1px",
                        background: "#f3f4f6",
                      }}
                    />
                  )}
                </div>
              );
            })}
          </div>
        </div>

        {/* Pulse animation keyframes */}
        <style>{`
          @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.3; }
          }
        `}</style>

        {/* ============================== */}
        {/* SEZIONE 1: PROFILO AGENTE      */}
        {/* ============================== */}
        <div style={cardStyle}>
          <div style={sectionTitle}>Profilo Agente</div>

          <div style={fieldGroup}>
            <label style={labelStyle}>Prompt di sistema</label>
            <textarea
              rows={8}
              style={textareaStyle}
              value={systemPrompt}
              onChange={(e) => setSystemPrompt(e.target.value)}
              placeholder={DEFAULT_SYSTEM_PROMPT}
              onFocus={handleFocus}
              onBlur={handleBlur}
            />
          </div>

          <div style={{ display: "flex", gap: "16px", flexWrap: "wrap" }}>
            <div style={{ ...fieldGroup, flex: "1 1 250px" }}>
              <label style={labelStyle}>Modello</label>
              <select
                style={selectStyle}
                value={modelName}
                onChange={(e) => setModelName(e.target.value)}
                onFocus={handleFocus}
                onBlur={handleBlur}
              >
                {MODEL_OPTIONS.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            </div>

            <div style={{ ...fieldGroup, flex: "0 1 180px" }}>
              <label style={labelStyle}>Intervallo esecuzione (ore)</label>
              <input
                type="number"
                min={1}
                max={24}
                style={inputStyle}
                value={runInterval}
                onChange={(e) => setRunInterval(Number(e.target.value))}
                onFocus={handleFocus}
                onBlur={handleBlur}
              />
            </div>
          </div>

          <button
            style={{
              ...btnPrimary,
              opacity: saving ? 0.6 : 1,
              cursor: saving ? "not-allowed" : "pointer",
            }}
            onClick={handleSaveProfile}
            disabled={saving}
          >
            {saving ? "Salvando..." : "Salva Profilo"}
          </button>
        </div>

        {/* ============================================= */}
        {/* SEZIONE 2: DOCUMENTI DI ANALISI TECNICA       */}
        {/* ============================================= */}
        <div style={cardStyle}>
          <div style={sectionTitle}>Documenti di Analisi Tecnica</div>

          {/* Upload nascosto + pulsante stilizzato */}
          <input
            type="file"
            accept=".pdf,.txt"
            ref={fileInputRef}
            onChange={handleFileUpload}
            style={{ display: "none" }}
          />
          <button
            style={{ ...btnPrimary, marginBottom: "16px" }}
            onClick={() => fileInputRef.current?.click()}
          >
            Carica Documento
          </button>

          {/* Lista documenti caricati */}
          {documents.length > 0 ? (
            <div style={{ borderRadius: "8px", overflow: "hidden", border: "1px solid #e5e7eb" }}>
              <table
                style={{
                  width: "100%",
                  borderCollapse: "collapse",
                  fontSize: "14px",
                }}
              >
                <thead>
                  <tr style={{ background: "#f9fafb" }}>
                    <th
                      style={{
                        textAlign: "left",
                        padding: "10px 14px",
                        fontWeight: 500,
                        color: "#6b7280",
                        fontSize: "13px",
                        borderBottom: "1px solid #e5e7eb",
                      }}
                    >
                      Nome File
                    </th>
                    <th
                      style={{
                        textAlign: "left",
                        padding: "10px 14px",
                        fontWeight: 500,
                        color: "#6b7280",
                        fontSize: "13px",
                        borderBottom: "1px solid #e5e7eb",
                      }}
                    >
                      Data Upload
                    </th>
                    <th
                      style={{
                        textAlign: "left",
                        padding: "10px 14px",
                        fontWeight: 500,
                        color: "#6b7280",
                        fontSize: "13px",
                        borderBottom: "1px solid #e5e7eb",
                      }}
                    >
                      Dimensione
                    </th>
                    <th
                      style={{
                        width: "40px",
                        borderBottom: "1px solid #e5e7eb",
                      }}
                    ></th>
                  </tr>
                </thead>
                <tbody>
                  {documents.map((doc, i) => (
                    <tr
                      key={doc.id}
                      style={{
                        borderBottom:
                          i < documents.length - 1
                            ? "1px solid #f3f4f6"
                            : "none",
                      }}
                    >
                      <td
                        style={{
                          padding: "10px 14px",
                          color: "#111827",
                          fontWeight: 500,
                        }}
                      >
                        {doc.filename}
                      </td>
                      <td
                        style={{
                          padding: "10px 14px",
                          color: "#6b7280",
                        }}
                      >
                        {formatDate(doc.uploaded_at)}
                      </td>
                      <td
                        style={{
                          padding: "10px 14px",
                          color: "#6b7280",
                        }}
                      >
                        {formatFileSize(doc.file_size)}
                      </td>
                      <td style={{ padding: "10px 14px", textAlign: "center" }}>
                        <button
                          onClick={() => handleDeleteDoc(doc.id)}
                          style={{
                            ...removeBtn,
                            color: "#ef4444",
                            fontSize: "14px",
                            fontWeight: 600,
                          }}
                          title="Elimina documento"
                        >
                          &#10005;
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div
              style={{
                padding: "24px",
                textAlign: "center",
                color: "#9ca3af",
                fontSize: "14px",
                background: "#f9fafb",
                borderRadius: "8px",
                border: "1px dashed #e5e7eb",
              }}
            >
              Nessun documento caricato
            </div>
          )}
        </div>

        {/* ============================================= */}
        {/* SEZIONE 3: CONFIGURAZIONE PORTAFOGLIO         */}
        {/* ============================================= */}
        <div style={cardStyle}>
          <div style={sectionTitle}>Configurazione Portafoglio</div>

          <div style={{ display: "flex", gap: "16px", flexWrap: "wrap" }}>
            <div style={{ ...fieldGroup, flex: "1 1 180px" }}>
              <label style={labelStyle}>Bilancio iniziale ($)</label>
              <input
                type="number"
                style={inputStyle}
                value={initialBalance}
                onChange={(e) => setInitialBalance(Number(e.target.value))}
                onFocus={handleFocus}
                onBlur={handleBlur}
              />
            </div>

            <div style={{ ...fieldGroup, flex: "1 1 140px" }}>
              <label style={labelStyle}>Max posizione (%)</label>
              <input
                type="number"
                style={inputStyle}
                value={maxPositionPct}
                onChange={(e) => setMaxPositionPct(Number(e.target.value))}
                onFocus={handleFocus}
                onBlur={handleBlur}
              />
            </div>

            <div style={{ ...fieldGroup, flex: "1 1 140px" }}>
              <label style={labelStyle}>Stop Loss (%)</label>
              <input
                type="number"
                style={inputStyle}
                value={stopLossThreshold}
                onChange={(e) => setStopLossThreshold(Number(e.target.value))}
                onFocus={handleFocus}
                onBlur={handleBlur}
              />
            </div>

            <div style={{ ...fieldGroup, flex: "1 1 140px" }}>
              <label style={labelStyle}>Max posizioni aperte</label>
              <input
                type="number"
                style={inputStyle}
                value={maxOpenPositions}
                onChange={(e) => setMaxOpenPositions(Number(e.target.value))}
                onFocus={handleFocus}
                onBlur={handleBlur}
              />
            </div>

            <div style={{ ...fieldGroup, flex: "1 1 140px" }}>
              <label style={labelStyle}>Confidenza minima</label>
              <input
                type="number"
                style={inputStyle}
                value={minConfidence}
                onChange={(e) => setMinConfidence(Number(e.target.value))}
                onFocus={handleFocus}
                onBlur={handleBlur}
              />
            </div>
          </div>

          <div style={{ display: "flex", gap: "12px", flexWrap: "wrap" }}>
            <button
              style={{
                ...btnPrimary,
                opacity: saving ? 0.6 : 1,
                cursor: saving ? "not-allowed" : "pointer",
              }}
              onClick={handleSavePortfolio}
              disabled={saving}
            >
              {saving ? "Salvando..." : "Salva Configurazione"}
            </button>
            <button style={btnDanger} onClick={handleResetPortfolio}>
              Resetta Portafoglio
            </button>
          </div>
        </div>

        {/* ============================================= */}
        {/* SEZIONE 4: UNIVERSO ASSET (WATCHLIST)         */}
        {/* ============================================= */}
        <div style={cardStyle}>
          <div style={sectionTitle}>Universo Asset (Watchlist)</div>

          {Object.entries(watchlist).map(([sector, tickers]) => (
            <div key={sector} style={{ marginBottom: "20px" }}>
              {/* Nome settore come intestazione */}
              <div
                style={{
                  fontSize: "13px",
                  fontWeight: 600,
                  color: "#6b7280",
                  textTransform: "uppercase",
                  letterSpacing: "0.04em",
                  marginBottom: "8px",
                }}
              >
                {sector.replace(/_/g, " ")}
              </div>

              {/* Badge dei ticker con pulsante rimozione */}
              <div
                style={{
                  display: "flex",
                  flexWrap: "wrap",
                  marginBottom: "8px",
                }}
              >
                {tickers.map((ticker) => (
                  <span key={ticker} style={tickerBadge}>
                    {ticker}
                    <button
                      style={removeBtn}
                      onClick={() => handleRemoveTicker(sector, ticker)}
                      title={`Rimuovi ${ticker}`}
                    >
                      &#10005;
                    </button>
                  </span>
                ))}
              </div>

              {/* Input per aggiungere un ticker al settore */}
              <div style={inlineRow}>
                <input
                  type="text"
                  placeholder="Ticker..."
                  style={{
                    ...inputStyle,
                    width: "120px",
                    flex: "0 0 auto",
                  }}
                  value={newTickerInputs[sector] || ""}
                  onChange={(e) =>
                    setNewTickerInputs((prev) => ({
                      ...prev,
                      [sector]: e.target.value,
                    }))
                  }
                  onKeyDown={(e) =>
                    e.key === "Enter" && handleAddTicker(sector)
                  }
                  onFocus={handleFocus}
                  onBlur={handleBlur}
                />
                <button
                  style={btnSecondary}
                  onClick={() => handleAddTicker(sector)}
                >
                  Aggiungi
                </button>
              </div>
            </div>
          ))}

          {/* Aggiunta di un nuovo settore */}
          <div
            style={{
              borderTop: "1px solid #e5e7eb",
              paddingTop: "16px",
              marginTop: "8px",
            }}
          >
            <div style={inlineRow}>
              <input
                type="text"
                placeholder="Nome nuovo settore..."
                style={{
                  ...inputStyle,
                  width: "200px",
                  flex: "0 0 auto",
                }}
                value={newSectorName}
                onChange={(e) => setNewSectorName(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleAddSector()}
                onFocus={handleFocus}
                onBlur={handleBlur}
              />
              <button style={btnSecondary} onClick={handleAddSector}>
                Nuovo Settore
              </button>
            </div>
          </div>

          <button
            style={{
              ...btnPrimary,
              marginTop: "16px",
              opacity: saving ? 0.6 : 1,
              cursor: saving ? "not-allowed" : "pointer",
            }}
            onClick={handleSaveWatchlist}
            disabled={saving}
          >
            {saving ? "Salvando..." : "Salva Watchlist"}
          </button>
        </div>

        {/* ============================================= */}
        {/* SEZIONE 5: CONFIGURAZIONE API                 */}
        {/* ============================================= */}
        <div style={cardStyle}>
          <div style={sectionTitle}>Configurazione API</div>

          <div style={fieldGroup}>
            <label style={labelStyle}>Anthropic API Key</label>
            <input
              type="password"
              style={inputStyle}
              value={anthropicKey}
              onChange={(e) => setAnthropicKey(e.target.value)}
              placeholder="Configurata tramite variabile d'ambiente"
              onFocus={handleFocus}
              onBlur={handleBlur}
            />
          </div>

          <div style={fieldGroup}>
            <label style={labelStyle}>NewsAPI Key</label>
            <input
              type="password"
              style={inputStyle}
              value={newsApiKey}
              onChange={(e) => setNewsApiKey(e.target.value)}
              placeholder="Inserisci la chiave NewsAPI"
              onFocus={handleFocus}
              onBlur={handleBlur}
            />
          </div>

          <div style={fieldGroup}>
            <label style={labelStyle}>Finnhub API Key</label>
            <input
              type="password"
              style={inputStyle}
              value={finnhubKey}
              onChange={(e) => setFinnhubKey(e.target.value)}
              placeholder="Inserisci la chiave Finnhub (congressional trading)"
              onFocus={handleFocus}
              onBlur={handleBlur}
            />
          </div>

          <div
            style={{
              display: "flex",
              gap: "12px",
              flexWrap: "wrap",
              alignItems: "center",
            }}
          >
            <button style={btnSecondary} onClick={handleTestConnection}>
              Testa Connessione
            </button>
            <button
              style={{
                ...btnPrimary,
                opacity: saving ? 0.6 : 1,
                cursor: saving ? "not-allowed" : "pointer",
              }}
              onClick={handleSaveApiKeys}
              disabled={saving}
            >
              {saving ? "Salvando..." : "Salva Chiavi API"}
            </button>
          </div>

          {/* Risultato del test di connessione */}
          {connectionTest && (
            <div style={{ marginTop: "16px" }}>
              {connectionTest.loading ? (
                <div
                  style={{
                    color: "#9ca3af",
                    fontSize: "14px",
                  }}
                >
                  Test in corso...
                </div>
              ) : connectionTest.error ? (
                <div
                  style={{
                    fontSize: "14px",
                    color: "#991b1b",
                    background: "#fef2f2",
                    border: "1px solid #fecaca",
                    borderRadius: "8px",
                    padding: "10px 14px",
                  }}
                >
                  Errore: {connectionTest.error}
                </div>
              ) : (
                <div style={{ fontSize: "14px" }}>
                  {connectionTest.anthropic != null && (
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: "8px",
                        marginBottom: "6px",
                      }}
                    >
                      <span
                        style={{
                          width: "8px",
                          height: "8px",
                          borderRadius: "50%",
                          background: connectionTest.anthropic
                            ? "#10b981"
                            : "#ef4444",
                          flexShrink: 0,
                        }}
                      />
                      <span style={{ color: "#374151" }}>Anthropic API:</span>
                      <span
                        style={{
                          color: connectionTest.anthropic
                            ? "#065f46"
                            : "#991b1b",
                          fontWeight: 500,
                        }}
                      >
                        {connectionTest.anthropic ? "Connessa" : "Non connessa"}
                      </span>
                    </div>
                  )}
                  {connectionTest.newsapi != null && (
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: "8px",
                      }}
                    >
                      <span
                        style={{
                          width: "8px",
                          height: "8px",
                          borderRadius: "50%",
                          background: connectionTest.newsapi
                            ? "#10b981"
                            : "#ef4444",
                          flexShrink: 0,
                        }}
                      />
                      <span style={{ color: "#374151" }}>NewsAPI:</span>
                      <span
                        style={{
                          color: connectionTest.newsapi
                            ? "#065f46"
                            : "#991b1b",
                          fontWeight: 500,
                        }}
                      >
                        {connectionTest.newsapi ? "Connessa" : "Non connessa"}
                      </span>
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default Settings;
