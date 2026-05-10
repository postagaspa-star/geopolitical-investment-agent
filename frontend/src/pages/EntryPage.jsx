import { useNavigate } from "react-router-dom";
import { TrendingUp, FlaskConical, ArrowRight, BookOpen } from "lucide-react";

/**
 * Pagina iniziale: scelta tra GeoInvest AI Live e GeoInvest AI Simulator.
 * Live = bot reale che opera con paper trading su portafoglio interno.
 * Simulator = ambiente di scenari storici per testare il ragionamento dell'agente.
 */
export default function EntryPage() {
  const nav = useNavigate();

  return (
    <div style={styles.root}>
      <div style={styles.container}>
        <div style={styles.header}>
          <h1 style={styles.title}>GeoInvest AI</h1>
          <p style={styles.subtitle}>Scegli la modalità</p>
        </div>

        <div style={styles.cardsRow}>
          {/* LIVE */}
          <button
            style={{ ...styles.card, ...styles.cardLive }}
            onClick={() => nav("/live")}
          >
            <div style={styles.cardIcon}>
              <TrendingUp size={48} strokeWidth={1.6} />
            </div>
            <h2 style={styles.cardTitle}>Live</h2>
            <p style={styles.cardDesc}>
              Bot operativo in tempo reale. Pipeline multi-agente che analizza
              mercati e crypto 24/7 ed esegue trade su portafoglio paper.
            </p>
            <ul style={styles.cardFeatures}>
              <li>Watchdog ogni 1 min</li>
              <li>Decision Sonnet 4.5 + Decision Crypto R1</li>
              <li>Portafoglio paper trading reale</li>
            </ul>
            <span style={styles.cardCta}>
              Apri Live <ArrowRight size={16} />
            </span>
          </button>

          {/* SIMULATOR */}
          <button
            style={{ ...styles.card, ...styles.cardSim }}
            onClick={() => nav("/simulator")}
          >
            <div style={styles.cardIcon}>
              <FlaskConical size={48} strokeWidth={1.6} />
            </div>
            <h2 style={styles.cardTitle}>Simulator</h2>
            <p style={styles.cardDesc}>
              Ambiente di test su scenari storici reali. Testa il ragionamento
              dell'agente su crisi geopolitiche, eventi macro, crash/rally —
              senza rivelare il periodo.
            </p>
            <ul style={styles.cardFeatures}>
              <li>4 categorie: Normale, Geopolitico, Macro, Crash/Rally</li>
              <li>Single-step + Multi-step (3-5 step)</li>
              <li>Benchmark vs S&P 500 + monkey + settore</li>
              <li>Memoria cognitiva tra run</li>
            </ul>
            <span style={styles.cardCta}>
              Apri Simulator <ArrowRight size={16} />
            </span>
          </button>
        </div>

        <div style={styles.footer}>
          Le due modalità sono indipendenti. Il Simulator NON tocca il
          portafoglio Live né muove fondi reali — è solo un sandbox cognitivo.
        </div>
      </div>

      {/* Floating Action Button — sempre visibile in basso a destra,
          porta alla pagina pubblica di documentazione (/about) */}
      <button
        onClick={() => nav("/about")}
        style={styles.fab}
        title="Scopri come funziona GeoInvest AI"
        onMouseEnter={(e) => {
          e.currentTarget.style.background = "#c4b5fd";
          e.currentTarget.style.transform = "translateY(-2px)";
        }}
        onMouseLeave={(e) => {
          e.currentTarget.style.background = "#a78bfa";
          e.currentTarget.style.transform = "translateY(0)";
        }}
      >
        <BookOpen size={18} />
        <span>Come funziona</span>
      </button>
    </div>
  );
}

const styles = {
  root: {
    minHeight: "100vh",
    background: "linear-gradient(135deg, #0a0e1a 0%, #1a1f2e 100%)",
    color: "#e2e8f0",
    fontFamily: "Inter, -apple-system, sans-serif",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    padding: "32px 16px",
  },
  container: {
    maxWidth: "1100px",
    width: "100%",
  },
  header: {
    textAlign: "center",
    marginBottom: "48px",
  },
  title: {
    fontSize: "3rem",
    fontWeight: 700,
    margin: "0 0 8px 0",
    letterSpacing: "-0.02em",
    background: "linear-gradient(135deg, #4f8cff, #a78bfa)",
    WebkitBackgroundClip: "text",
    WebkitTextFillColor: "transparent",
    backgroundClip: "text",
  },
  subtitle: {
    color: "#94a3b8",
    fontSize: "1.1rem",
    margin: 0,
  },
  cardsRow: {
    display: "grid",
    gridTemplateColumns: "1fr 1fr",
    gap: "24px",
  },
  card: {
    background: "rgba(20, 25, 40, 0.7)",
    border: "1px solid #2a3248",
    borderRadius: "16px",
    padding: "32px",
    cursor: "pointer",
    transition: "all 0.2s",
    textAlign: "left",
    color: "#e2e8f0",
    fontFamily: "inherit",
    display: "flex",
    flexDirection: "column",
    gap: "12px",
  },
  cardLive: {
    borderTopColor: "#10b981",
    borderTopWidth: "3px",
  },
  cardSim: {
    borderTopColor: "#a78bfa",
    borderTopWidth: "3px",
  },
  cardIcon: {
    color: "#94a3b8",
    marginBottom: "8px",
  },
  cardTitle: {
    fontSize: "1.75rem",
    fontWeight: 600,
    margin: 0,
  },
  cardDesc: {
    color: "#94a3b8",
    fontSize: "0.95rem",
    lineHeight: 1.5,
    margin: 0,
  },
  cardFeatures: {
    listStyle: "none",
    padding: 0,
    margin: "12px 0",
    color: "#cbd5e1",
    fontSize: "0.85rem",
    display: "flex",
    flexDirection: "column",
    gap: "6px",
  },
  cardCta: {
    marginTop: "auto",
    display: "inline-flex",
    alignItems: "center",
    gap: "6px",
    color: "#4f8cff",
    fontWeight: 600,
    fontSize: "0.95rem",
  },
  footer: {
    marginTop: "32px",
    textAlign: "center",
    color: "#64748b",
    fontSize: "0.85rem",
  },
  fab: {
    position: "fixed",
    bottom: 24,
    right: 24,
    zIndex: 9999,
    display: "inline-flex",
    alignItems: "center",
    gap: 8,
    padding: "14px 22px",
    background: "#a78bfa",
    color: "#0a0e1a",
    border: "none",
    borderRadius: 999,
    fontSize: "0.92rem",
    fontWeight: 700,
    cursor: "pointer",
    fontFamily: "inherit",
    boxShadow:
      "0 6px 24px rgba(167,139,250,0.45), 0 2px 8px rgba(0,0,0,0.3)",
    transition: "all 0.2s ease",
  },
};
