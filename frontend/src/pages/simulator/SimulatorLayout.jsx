import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { Home, Play, History, ArrowLeft, FlaskConical } from "lucide-react";

/**
 * Layout shared by tutte le schermate del Simulator.
 * Sidebar con: Home, Scenario Runner, Storico & Analytics.
 */
export default function SimulatorLayout() {
  const nav = useNavigate();

  return (
    <div style={S.root}>
      <aside style={S.sidebar}>
        <button style={S.backBtn} onClick={() => nav("/")}>
          <ArrowLeft size={16} /> Esci
        </button>

        <div style={S.brand}>
          <FlaskConical size={20} />
          <strong>Simulator</strong>
        </div>

        <nav style={S.nav}>
          <NavLink to="/simulator" end style={({isActive}) => ({...S.link, ...(isActive ? S.linkActive : {})})}>
            <Home size={16} /> Dashboard
          </NavLink>
          <NavLink to="/simulator/runner" style={({isActive}) => ({...S.link, ...(isActive ? S.linkActive : {})})}>
            <Play size={16} /> Scenario Runner
          </NavLink>
          <NavLink to="/simulator/history" style={({isActive}) => ({...S.link, ...(isActive ? S.linkActive : {})})}>
            <History size={16} /> Storico & Analytics
          </NavLink>
        </nav>

        <div style={S.note}>
          Simulator: ambiente di test su scenari storici reali. Non tocca il
          portafoglio Live.
        </div>
      </aside>

      <main style={S.main}>
        <Outlet />
      </main>
    </div>
  );
}

const S = {
  root: { display: "flex", minHeight: "100vh", background: "#0a0e1a", color: "#e2e8f0",
          fontFamily: "Inter, -apple-system, sans-serif" },
  sidebar: { width: 240, background: "#111827", borderRight: "1px solid #1f2937",
             padding: "20px 14px", display: "flex", flexDirection: "column", gap: 16 },
  backBtn: { background: "transparent", border: "1px solid #374151", color: "#9ca3af",
             padding: "6px 10px", borderRadius: 6, cursor: "pointer", fontSize: 13,
             display: "inline-flex", alignItems: "center", gap: 6, alignSelf: "flex-start" },
  brand: { display: "flex", alignItems: "center", gap: 8, color: "#a78bfa", fontSize: "1.1rem" },
  nav: { display: "flex", flexDirection: "column", gap: 4 },
  link: { display: "flex", alignItems: "center", gap: 8, padding: "10px 12px",
          borderRadius: 6, color: "#cbd5e1", textDecoration: "none", fontSize: 14 },
  linkActive: { background: "#1e293b", color: "#a78bfa" },
  note: { marginTop: "auto", fontSize: 11, color: "#64748b", lineHeight: 1.4,
          padding: "10px", background: "#0f172a", borderRadius: 6 },
  main: { flex: 1, overflow: "auto", padding: "24px 32px" },
};
