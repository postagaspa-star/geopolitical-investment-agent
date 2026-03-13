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

// Modelli disponibili
const MODEL_OPTIONS = [
  "claude-opus-4-5-20250514",
  "claude-sonnet-4-20250514",
  "claude-haiku-4-20250514",
];

function Settings({ onBack }) {
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
  const [connectionTest, setConnectionTest] = useState(null);

  // === Stato UI generale ===
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState(null);

  // Mostra messaggio temporaneo di feedback
  const showMessage = (text, isError = false) => {
    setMessage({ text, isError });
    setTimeout(() => setMessage(null), 3000);
  };

  // === Caricamento iniziale di tutte le impostazioni ===
  useEffect(() => {
    const loadSettings = async () => {
      try {
        const res = await fetch(`${API}/api/settings`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();

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
      } catch (err) {
        console.error("Errore caricamento impostazioni:", err);
      }
    };

    loadSettings();
    loadDocuments();
  }, []);

  // Caricamento lista documenti dal server
  const loadDocuments = async () => {
    try {
      const res = await fetch(`${API}/api/documents`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setDocuments(Array.isArray(data) ? data : []);
    } catch (err) {
      console.error("Errore caricamento documenti:", err);
    }
  };

  // === Salvataggio generico impostazioni via POST ===
  const saveSettings = async (settings) => {
    setSaving(true);
    try {
      const res = await fetch(`${API}/api/settings`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(settings),
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
      loadDocuments();
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
      loadDocuments();
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
    saveSettings(settings);
  };

  // === Stili inline riutilizzabili ===
  const inputStyle = {
    width: "100%",
    padding: "0.5rem 0.75rem",
    background: "rgba(255,255,255,0.05)",
    border: "1px solid var(--border-color)",
    borderRadius: "6px",
    color: "var(--text-primary)",
    fontSize: "0.85rem",
    fontFamily: "var(--font-sans)",
    outline: "none",
  };

  const textareaStyle = {
    ...inputStyle,
    fontFamily: "var(--font-mono)",
    fontSize: "0.8rem",
    lineHeight: "1.5",
    resize: "vertical",
  };

  const selectStyle = {
    ...inputStyle,
    cursor: "pointer",
  };

  const labelStyle = {
    display: "block",
    fontSize: "0.75rem",
    color: "var(--text-secondary)",
    marginBottom: "0.35rem",
    fontWeight: 600,
    textTransform: "uppercase",
    letterSpacing: "0.04em",
  };

  const fieldGroup = {
    marginBottom: "1rem",
  };

  const btnDanger = {
    padding: "0.5rem 1.2rem",
    border: "1px solid var(--negative)",
    background: "transparent",
    color: "var(--negative)",
    borderRadius: "6px",
    cursor: "pointer",
    fontSize: "0.85rem",
    fontWeight: 600,
  };

  const btnSmall = {
    padding: "0.3rem 0.7rem",
    border: "1px solid var(--accent-blue)",
    background: "transparent",
    color: "var(--accent-blue)",
    borderRadius: "4px",
    cursor: "pointer",
    fontSize: "0.75rem",
    fontWeight: 600,
  };

  const tickerBadge = {
    display: "inline-flex",
    alignItems: "center",
    gap: "0.3rem",
    padding: "0.2rem 0.5rem",
    background: "rgba(68, 138, 255, 0.12)",
    border: "1px solid rgba(68, 138, 255, 0.3)",
    borderRadius: "4px",
    fontSize: "0.75rem",
    fontWeight: 700,
    color: "var(--accent-blue)",
    margin: "0.2rem",
  };

  const removeBtn = {
    background: "none",
    border: "none",
    color: "var(--text-muted)",
    cursor: "pointer",
    fontSize: "0.7rem",
    padding: "0 0.15rem",
    lineHeight: 1,
  };

  const inlineRow = {
    display: "flex",
    gap: "0.5rem",
    alignItems: "center",
  };

  return (
    <div
      style={{
        padding: "1.5rem 2rem",
        maxWidth: "1000px",
        margin: "0 auto",
      }}
    >
      {/* Intestazione con freccia indietro */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: "1rem",
          marginBottom: "1.5rem",
        }}
      >
        <button
          onClick={onBack}
          style={{
            background: "none",
            border: "1px solid var(--border-color)",
            color: "var(--text-primary)",
            borderRadius: "6px",
            cursor: "pointer",
            padding: "0.4rem 0.7rem",
            fontSize: "1.1rem",
            lineHeight: 1,
          }}
        >
          &#8592;
        </button>
        <h1
          style={{
            fontSize: "1.4rem",
            fontWeight: 700,
            letterSpacing: "-0.02em",
          }}
        >
          Impostazioni
        </h1>
      </div>

      {/* Messaggio di feedback globale */}
      {message && (
        <div
          style={{
            padding: "0.6rem 1rem",
            marginBottom: "1rem",
            borderRadius: "6px",
            fontSize: "0.85rem",
            fontWeight: 600,
            background: message.isError
              ? "rgba(255,23,68,0.12)"
              : "rgba(0,200,83,0.12)",
            color: message.isError ? "var(--negative)" : "var(--positive)",
            border: `1px solid ${
              message.isError
                ? "rgba(255,23,68,0.3)"
                : "rgba(0,200,83,0.3)"
            }`,
          }}
        >
          {message.text}
        </div>
      )}

      {/* ============================== */}
      {/* SEZIONE 1: PROFILO AGENTE      */}
      {/* ============================== */}
      <div className="card" style={{ marginBottom: "1.25rem" }}>
        <div className="card-title">Profilo Agente</div>

        <div style={fieldGroup}>
          <label style={labelStyle}>Prompt di sistema</label>
          <textarea
            rows={12}
            style={textareaStyle}
            value={systemPrompt}
            onChange={(e) => setSystemPrompt(e.target.value)}
            placeholder={DEFAULT_SYSTEM_PROMPT}
          />
        </div>

        <div style={{ display: "flex", gap: "1rem", flexWrap: "wrap" }}>
          <div style={{ ...fieldGroup, flex: "1 1 250px" }}>
            <label style={labelStyle}>Modello</label>
            <select
              style={selectStyle}
              value={modelName}
              onChange={(e) => setModelName(e.target.value)}
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
            />
          </div>
        </div>

        <button
          className="btn-run"
          onClick={handleSaveProfile}
          disabled={saving}
        >
          {saving ? "Salvando..." : "Salva Profilo"}
        </button>
      </div>

      {/* ============================================= */}
      {/* SEZIONE 2: DOCUMENTI DI ANALISI TECNICA       */}
      {/* ============================================= */}
      <div className="card" style={{ marginBottom: "1.25rem" }}>
        <div className="card-title">Documenti di Analisi Tecnica</div>

        {/* Upload nascosto + pulsante stilizzato */}
        <input
          type="file"
          accept=".pdf,.txt"
          ref={fileInputRef}
          onChange={handleFileUpload}
          style={{ display: "none" }}
        />
        <button
          className="btn-run"
          style={{ marginBottom: "1rem" }}
          onClick={() => fileInputRef.current?.click()}
        >
          Carica Documento
        </button>

        {/* Lista documenti caricati */}
        {documents.length > 0 ? (
          <table className="data-table">
            <thead>
              <tr>
                <th>Nome File</th>
                <th>Data Upload</th>
                <th>Dimensione</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {documents.map((doc) => (
                <tr key={doc.id}>
                  <td>{doc.filename}</td>
                  <td>{formatDate(doc.uploaded_at)}</td>
                  <td>{formatFileSize(doc.file_size)}</td>
                  <td>
                    <button
                      onClick={() => handleDeleteDoc(doc.id)}
                      style={{
                        ...removeBtn,
                        color: "var(--negative)",
                        fontSize: "1rem",
                        fontWeight: 700,
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
        ) : (
          <div className="empty-state">Nessun documento caricato</div>
        )}
      </div>

      {/* ============================================= */}
      {/* SEZIONE 3: CONFIGURAZIONE PORTAFOGLIO         */}
      {/* ============================================= */}
      <div className="card" style={{ marginBottom: "1.25rem" }}>
        <div className="card-title">Configurazione Portafoglio</div>

        <div style={{ display: "flex", gap: "1rem", flexWrap: "wrap" }}>
          <div style={{ ...fieldGroup, flex: "1 1 180px" }}>
            <label style={labelStyle}>Bilancio iniziale ($)</label>
            <input
              type="number"
              style={inputStyle}
              value={initialBalance}
              onChange={(e) => setInitialBalance(Number(e.target.value))}
            />
          </div>

          <div style={{ ...fieldGroup, flex: "1 1 140px" }}>
            <label style={labelStyle}>Max posizione (%)</label>
            <input
              type="number"
              style={inputStyle}
              value={maxPositionPct}
              onChange={(e) => setMaxPositionPct(Number(e.target.value))}
            />
          </div>

          <div style={{ ...fieldGroup, flex: "1 1 140px" }}>
            <label style={labelStyle}>Stop Loss (%)</label>
            <input
              type="number"
              style={inputStyle}
              value={stopLossThreshold}
              onChange={(e) => setStopLossThreshold(Number(e.target.value))}
            />
          </div>

          <div style={{ ...fieldGroup, flex: "1 1 140px" }}>
            <label style={labelStyle}>Max posizioni aperte</label>
            <input
              type="number"
              style={inputStyle}
              value={maxOpenPositions}
              onChange={(e) => setMaxOpenPositions(Number(e.target.value))}
            />
          </div>

          <div style={{ ...fieldGroup, flex: "1 1 140px" }}>
            <label style={labelStyle}>Confidenza minima</label>
            <input
              type="number"
              style={inputStyle}
              value={minConfidence}
              onChange={(e) => setMinConfidence(Number(e.target.value))}
            />
          </div>
        </div>

        <div
          style={{ display: "flex", gap: "0.75rem", flexWrap: "wrap" }}
        >
          <button
            className="btn-run"
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
      <div className="card" style={{ marginBottom: "1.25rem" }}>
        <div className="card-title">Universo Asset (Watchlist)</div>

        {Object.entries(watchlist).map(([sector, tickers]) => (
          <div key={sector} style={{ marginBottom: "1rem" }}>
            {/* Nome settore come intestazione */}
            <div
              style={{
                fontSize: "0.8rem",
                fontWeight: 700,
                color: "var(--text-secondary)",
                textTransform: "uppercase",
                letterSpacing: "0.04em",
                marginBottom: "0.4rem",
              }}
            >
              {sector.replace(/_/g, " ")}
            </div>

            {/* Badge dei ticker con pulsante rimozione */}
            <div
              style={{
                display: "flex",
                flexWrap: "wrap",
                marginBottom: "0.5rem",
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
              />
              <button
                style={btnSmall}
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
            borderTop: "1px solid var(--border-color)",
            paddingTop: "1rem",
            marginTop: "0.5rem",
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
              onKeyDown={(e) =>
                e.key === "Enter" && handleAddSector()
              }
            />
            <button style={btnSmall} onClick={handleAddSector}>
              Nuovo Settore
            </button>
          </div>
        </div>

        <button
          className="btn-run"
          style={{ marginTop: "1rem" }}
          onClick={handleSaveWatchlist}
          disabled={saving}
        >
          {saving ? "Salvando..." : "Salva Watchlist"}
        </button>
      </div>

      {/* ============================================= */}
      {/* SEZIONE 5: CONFIGURAZIONE API                 */}
      {/* ============================================= */}
      <div className="card" style={{ marginBottom: "1.25rem" }}>
        <div className="card-title">Configurazione API</div>

        <div style={fieldGroup}>
          <label style={labelStyle}>Anthropic API Key</label>
          <input
            type="password"
            style={inputStyle}
            value={anthropicKey}
            onChange={(e) => setAnthropicKey(e.target.value)}
            placeholder="Configurata tramite variabile d'ambiente"
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
          />
        </div>

        <div
          style={{
            display: "flex",
            gap: "0.75rem",
            flexWrap: "wrap",
            alignItems: "center",
          }}
        >
          <button className="btn-run" onClick={handleTestConnection}>
            Testa Connessione
          </button>
          <button
            className="btn-run"
            onClick={handleSaveApiKeys}
            disabled={saving}
          >
            {saving ? "Salvando..." : "Salva Chiavi API"}
          </button>
        </div>

        {/* Risultato del test di connessione */}
        {connectionTest && (
          <div style={{ marginTop: "1rem" }}>
            {connectionTest.loading ? (
              <div
                style={{
                  color: "var(--text-muted)",
                  fontSize: "0.85rem",
                }}
              >
                Test in corso...
              </div>
            ) : connectionTest.error ? (
              <div
                className="negative"
                style={{ fontSize: "0.85rem" }}
              >
                Errore: {connectionTest.error}
              </div>
            ) : (
              <div style={{ fontSize: "0.85rem" }}>
                {connectionTest.anthropic != null && (
                  <div style={{ marginBottom: "0.3rem" }}>
                    <span
                      className={
                        connectionTest.anthropic
                          ? "positive"
                          : "negative"
                      }
                      style={{ fontWeight: 700 }}
                    >
                      &#9679;
                    </span>{" "}
                    Anthropic API:{" "}
                    <span
                      className={
                        connectionTest.anthropic
                          ? "positive"
                          : "negative"
                      }
                    >
                      {connectionTest.anthropic
                        ? "Connessa"
                        : "Non connessa"}
                    </span>
                  </div>
                )}
                {connectionTest.newsapi != null && (
                  <div>
                    <span
                      className={
                        connectionTest.newsapi
                          ? "positive"
                          : "negative"
                      }
                      style={{ fontWeight: 700 }}
                    >
                      &#9679;
                    </span>{" "}
                    NewsAPI:{" "}
                    <span
                      className={
                        connectionTest.newsapi
                          ? "positive"
                          : "negative"
                      }
                    >
                      {connectionTest.newsapi
                        ? "Connessa"
                        : "Non connessa"}
                    </span>
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

export default Settings;
