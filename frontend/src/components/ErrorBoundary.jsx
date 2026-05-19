import React from "react";

/**
 * ErrorBoundary — rete di sicurezza dell'interfaccia.
 *
 * Senza questo, QUALSIASI errore di render in un componente figlio
 * (es. una pagina che legge dati parziali da un backend in riavvio)
 * smontava l'INTERA app → "schermo blu". Qui l'errore viene catturato
 * e mostrato in un pannello recuperabile, mentre il resto della
 * dashboard resta vivo. Un intoppo temporaneo non azzera piu' tutto.
 *
 * Volutamente una class component: gli error boundary in React si
 * fanno SOLO cosi' (niente equivalente con gli hooks).
 */
export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false, err: null };
  }

  static getDerivedStateFromError(err) {
    return { hasError: true, err };
  }

  componentDidCatch(err, info) {
    // Log in console per diagnostica; NON rilancia (altrimenti
    // l'errore risalirebbe e smonterebbe comunque l'app).
    try {
      console.error("[ErrorBoundary]", err, info && info.componentStack);
    } catch {
      /* no-op */
    }
  }

  handleRetry = () => this.setState({ hasError: false, err: null });

  render() {
    if (!this.state.hasError) return this.props.children;
    const e = this.state.err;
    const msg = String((e && e.message) || e || "Errore sconosciuto").slice(0, 300);
    return (
      <div style={{
        minHeight: "55vh", display: "flex", alignItems: "center",
        justifyContent: "center", padding: "2rem",
      }}>
        <div style={{
          maxWidth: 520, width: "100%", background: "#111827",
          border: "1px solid #334155", borderRadius: 12,
          padding: "24px 28px", color: "#e2e8f0",
        }}>
          <div style={{ fontSize: "1.1rem", fontWeight: 700, marginBottom: 6 }}>
            Qualcosa è andato storto in questa schermata
          </div>
          <div style={{
            fontSize: "0.85rem", color: "#94a3b8", lineHeight: 1.6,
            marginBottom: 14,
          }}>
            Di solito è un intoppo temporaneo del backend (dati parziali o
            servizio in riavvio), non un dato perso: i dati di trading sono
            su Supabase. Riprova tra un istante o ricarica la pagina.
          </div>
          <div style={{
            fontSize: "0.72rem", color: "#64748b", background: "#0a1018",
            borderRadius: 8, padding: "8px 10px", marginBottom: 16,
            fontFamily: "monospace", wordBreak: "break-word",
          }}>
            {msg}
          </div>
          <div style={{ display: "flex", gap: 10 }}>
            <button onClick={this.handleRetry} style={{
              background: "#3b82f6", color: "#fff", border: "none",
              borderRadius: 8, padding: "8px 16px", fontWeight: 600,
              cursor: "pointer", fontSize: "0.85rem",
            }}>Riprova</button>
            <button onClick={() => window.location.reload()} style={{
              background: "transparent", color: "#cbd5e1",
              border: "1px solid #334155", borderRadius: 8,
              padding: "8px 16px", fontWeight: 600, cursor: "pointer",
              fontSize: "0.85rem",
            }}>Ricarica pagina</button>
          </div>
        </div>
      </div>
    );
  }
}
