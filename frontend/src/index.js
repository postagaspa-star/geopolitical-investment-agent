import React from "react";
import ReactDOM from "react-dom/client";
import "./index.css";
// Layer responsive opzionale. Per disattivarlo: rimuovi la riga sotto.
// Nessuna modifica al codice React, solo CSS override via media queries.
import "./responsive.css";
import App from "./App";
import ErrorBoundary from "./components/ErrorBoundary";

// Interceptor globale: se l'utente ha configurato un admin token (Settings →
// localStorage "gi_admin_token"), lo aggiunge come header X-Admin-Token a TUTTE
// le richieste. Così l'auth opt-in del backend (ADMIN_API_TOKEN) funziona senza
// modificare ogni singola fetch. Se nessun token è impostato: nessun effetto.
(function installAdminTokenInterceptor() {
  if (typeof window === "undefined" || window.__giFetchPatched) return;
  const orig = window.fetch.bind(window);
  window.fetch = (input, init = {}) => {
    try {
      const token = window.localStorage.getItem("gi_admin_token");
      if (token) {
        init = { ...init, headers: { ...(init.headers || {}), "X-Admin-Token": token } };
      }
    } catch { /* localStorage non disponibile: ignora */ }
    return orig(input, init);
  };
  window.__giFetchPatched = true;
})();

const root = ReactDOM.createRoot(document.getElementById("root"));
root.render(
  <React.StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </React.StrictMode>
);
