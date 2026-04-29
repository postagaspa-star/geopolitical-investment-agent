import React, { useState, useEffect, useRef } from "react";

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
    label: "Scout (Sonnet 4.5)",
    description: "Raccoglie e analizza notizie da 7 fonti ogni 20 minuti, 24/7.",
  },
  {
    key: "prompt_technical",
    label: "Technical (DeepSeek-V3)",
    description: "Esegue analisi tecnica quantitativa quando il Watchdog triggera la pipeline.",
  },
  {
    key: "prompt_decision",
    label: "Decision (Sonnet 4.5)",
    description: "Decide buy/sell/hold incrociando geopolitica + tecnico. Max 1 esecuzione/ora.",
  },
];

function Settings({ onBack, onDataRefresh }) {
  // === Diagnostica ===
  const [diagnostics, setDiagnostics] = useState(INITIAL_DIAGNOSTICS);

  // === Prompt per i 3 agenti ===
  const [prompts, setPrompts] = useState({
    prompt_scout: "",
    prompt_technical: "",
    prompt_decision: "",
  });
  const [promptDefaults, setPromptDefaults] = useState({
    prompt_scout: "",
    prompt_technical: "",
    prompt_decision: "",
  });

  // === Documenti ===
  const [documents, setDocuments] = useState([]);
  const fileInputRef = useRef(null);

  // === ClawStreet ===
  const [csStatus, setCsStatus] = useState(null);
  const [csRegistering, setCsRegistering] = useState(false);

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

    await Promise.all(tests.map(async ({ key, url }) => {
      try {
        const res = await fetch(`${API}${url}`);
        const data = await res.json();
        if (data.status === "ok") setDiagItem(key, "ok");
        else setDiagItem(key, "error", data.message || "Risposta non valida");
      } catch (err) {
        setDiagItem(key, "error", err.message);
      }
    }));

    // Scheduler
    try {
      const res = await fetch(`${API}/api/agent/status`);
      const data = await res.json();
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
      // Carica le impostazioni esistenti
      try {
        const res = await fetch(`${API}/api/settings`);
        if (res.ok) {
          const raw = await res.json();
          const data = raw.settings ?? raw;
          setPrompts({
            prompt_scout: data.prompt_scout || "",
            prompt_technical: data.prompt_technical || "",
            prompt_decision: data.prompt_decision || "",
          });
        }
      } catch (err) {
        console.error("Errore caricamento impostazioni:", err);
      }

      // Carica i prompt di default
      try {
        const res = await fetch(`${API}/api/settings/prompt-defaults`);
        if (res.ok) {
          const data = await res.json();
          setPromptDefaults({
            prompt_scout: data.prompt_scout || "",
            prompt_technical: data.prompt_technical || "",
            prompt_decision: data.prompt_decision || "",
          });
        }
      } catch (err) {
        console.error("Errore caricamento prompt default:", err);
      }

      // Carica documenti
      const docs = await loadDocumentsRaw();

      // ClawStreet
      try {
        const csRes = await fetch(`${API}/api/clawstreet/status`);
        if (csRes.ok) setCsStatus(await csRes.json());
      } catch {}

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

  const handleResetPrompt = (key) => {
    if (!window.confirm("Ripristinare il prompt di default per questo agente?")) return;
    setPrompts((prev) => ({ ...prev, [key]: "" }));
    saveSettings({ [key]: "" });
  };

  // === Documenti ===
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

  // === ClawStreet ===
  const handleRegisterClawStreet = async () => {
    setCsRegistering(true);
    try {
      const res = await fetch(`${API}/api/clawstreet/register`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.error || `HTTP ${res.status}`);
      }
      showMessage("Bot registrato su ClawStreet con successo!");
      const csRes = await fetch(`${API}/api/clawstreet/status`);
      if (csRes.ok) setCsStatus(await csRes.json());
    } catch (err) {
      showMessage(`Errore registrazione: ${err.message}`, true);
    }
    setCsRegistering(false);
  };

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
        <h1 style={{
          fontSize: "24px", fontWeight: 700, color: "#111827",
          marginBottom: "24px", letterSpacing: "-0.02em",
        }}>
          Impostazioni
        </h1>

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
                : diag.status === "error" ? "#ef4444" : "#9ca3af";
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
        </div>

        <style>{`
          @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }
        `}</style>

        {/* === PROMPT DI SISTEMA (3 AGENTI) === */}
        <div style={cardStyle}>
          <div style={sectionTitle}>Prompt di Sistema per Agente</div>
          <p style={{ color: "#6b7280", fontSize: "0.85rem", margin: "0 0 20px 0", lineHeight: 1.5 }}>
            Personalizza il comportamento di ogni agente del workflow. Lasciando vuoto il campo,
            verra' usato il prompt di default integrato.
            Il modello e l'intervallo di esecuzione sono fissi e non configurabili: Watchdog (DeepSeek-V3) ogni 1 min market-only,
            Scout (Sonnet 4.5) ogni 20 min 24/7, Technical (DeepSeek-V3) e Decision (Sonnet 4.5) on-demand.
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

        {/* === DOCUMENTI === */}
        <div style={cardStyle}>
          <div style={sectionTitle}>Documenti di Analisi Tecnica</div>

          <input type="file" accept=".pdf,.txt" ref={fileInputRef}
            onChange={handleFileUpload} style={{ display: "none" }} />
          <button style={{ ...btnPrimary, marginBottom: "16px" }}
            onClick={() => fileInputRef.current?.click()}>
            Carica Documento
          </button>

          {documents.length > 0 ? (
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
                  {documents.map((doc, i) => (
                    <tr key={doc.id} style={{
                      borderBottom: i < documents.length - 1 ? "1px solid #f3f4f6" : "none" }}>
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
              border: "1px dashed #e5e7eb" }}>Nessun documento caricato</div>
          )}
        </div>

        {/* === CLAWSTREET === */}
        <div style={cardStyle}>
          <div style={sectionTitle}>ClawStreet</div>
          <p style={{ color: "#9ca3af", fontSize: "0.85rem", margin: "0 0 16px 0" }}>
            Registra il bot sulla piattaforma pubblica ClawStreet per condividere i trade
            e comparire nella leaderboard. I dati di mercato ClawStreet (sentiment, macro, screener)
            sono gia' integrati automaticamente nello Scout.
          </p>

          {csStatus?.registered ? (
            <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
              <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
                <span style={{ width: "8px", height: "8px", borderRadius: "50%",
                  background: "#10b981", flexShrink: 0 }} />
                <span style={{ color: "#374151", fontWeight: 500 }}>Registrato</span>
              </div>
              <div style={{ fontSize: "0.85rem", color: "#6b7280" }}>
                <strong>Nome:</strong> {csStatus.bot_name || "GeoInvest AI"}
                {csStatus.bot_ticker && <> &middot; <strong>Ticker:</strong> {csStatus.bot_ticker}</>}
              </div>
              {csStatus.public_url && (
                <a href={csStatus.public_url} target="_blank" rel="noopener noreferrer"
                  style={{ color: "#3b82f6", fontSize: "0.85rem", textDecoration: "none" }}>
                  &#128279; Pagina pubblica del bot &rarr;
                </a>
              )}
              {csStatus.claim_url && (
                <a href={csStatus.claim_url} target="_blank" rel="noopener noreferrer"
                  style={{ color: "#8b5cf6", fontSize: "0.85rem", textDecoration: "none" }}>
                  &#128273; Claim URL &rarr;
                </a>
              )}
            </div>
          ) : (
            <div>
              <div style={{ display: "flex", gap: "8px", alignItems: "center", marginBottom: "12px" }}>
                <span style={{ width: "8px", height: "8px", borderRadius: "50%",
                  background: "#9ca3af", flexShrink: 0 }} />
                <span style={{ color: "#6b7280" }}>Non registrato</span>
              </div>
              <button style={{ ...btnPrimary,
                opacity: csRegistering ? 0.6 : 1,
                cursor: csRegistering ? "not-allowed" : "pointer" }}
                onClick={handleRegisterClawStreet} disabled={csRegistering}>
                {csRegistering ? "Registrazione in corso..." : "Registra Bot su ClawStreet"}
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default Settings;
