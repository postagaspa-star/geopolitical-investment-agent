import React from "react";
import ReactDOM from "react-dom/client";
import "./index.css";
// Layer responsive opzionale. Per disattivarlo: rimuovi la riga sotto.
// Nessuna modifica al codice React, solo CSS override via media queries.
import "./responsive.css";
import App from "./App";
import ErrorBoundary from "./components/ErrorBoundary";

const root = ReactDOM.createRoot(document.getElementById("root"));
root.render(
  <React.StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </React.StrictMode>
);
