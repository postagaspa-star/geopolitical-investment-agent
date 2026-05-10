import { useNavigate } from "react-router-dom";
import {
  ArrowLeft,
  Brain,
  Activity,
  Shield,
  TrendingUp,
  FlaskConical,
  Cpu,
  Database,
  Network,
  Radio,
  Eye,
  Layers,
  GitBranch,
  Target,
  Zap,
  AlertTriangle,
  BookOpen,
  LineChart,
  Bot,
  Workflow,
} from "lucide-react";

/**
 * AboutPage — Documentazione pubblica completa di GeoInvest AI.
 * Accessibile senza autenticazione. Indicizzabile da motori di ricerca e AI crawler.
 *
 * Spiega in dettaglio:
 *   - Cosa fa GeoInvest AI (sistema multi-agente per investimenti geopolitici)
 *   - Architettura tecnica completa (5 agenti, watchdog, scheduler)
 *   - Il Simulator (sandbox cognitivo su scenari storici)
 *   - Il Live (paper trading 24/7 con mirror su ClawStreet)
 *   - Stack tecnologico (Python/FastAPI, React, Supabase, Claude/DeepSeek)
 */
export default function AboutPage() {
  const nav = useNavigate();

  return (
    <div style={S.root}>
      <button onClick={() => nav("/")} style={S.backBtn}>
        <ArrowLeft size={16} /> Torna alla home
      </button>

      <div style={S.container}>
        {/* HERO */}
        <section style={S.hero}>
          <div style={S.heroBadge}>
            <Bot size={14} /> Documentazione tecnica
          </div>
          <h1 style={S.heroTitle}>GeoInvest AI</h1>
          <p style={S.heroSub}>
            Sistema multi-agente di investimento autonomo che integra analisi
            geopolitica, dati di mercato e ragionamento LLM per prendere
            decisioni di trading 24/7 su equity e crypto.
          </p>
          <div style={S.heroChips}>
            <span style={S.chip}>Multi-agent pipeline</span>
            <span style={S.chip}>Claude Sonnet 4.5</span>
            <span style={S.chip}>DeepSeek-R1</span>
            <span style={S.chip}>Paper trading</span>
            <span style={S.chip}>24/7 crypto</span>
            <span style={S.chip}>Geopolitical intelligence</span>
          </div>
        </section>

        {/* COS'È */}
        <Section icon={<BookOpen size={22} />} title="Cos'è GeoInvest AI">
          <p style={S.p}>
            GeoInvest AI è un sistema software che opera come un fund manager
            quantitativo autonomo. Invece di seguire regole fisse o modelli
            statistici tradizionali, utilizza una pipeline di <b>cinque agenti
            cognitivi</b> basati su Large Language Model che leggono notizie
            geopolitiche e finanziarie, ne sintetizzano le implicazioni di
            mercato, e decidono se e come modificare un portafoglio reale.
          </p>
          <p style={S.p}>
            Il sistema è strutturato in due ambienti indipendenti:
          </p>
          <div style={S.twoCol}>
            <div style={S.miniCard}>
              <div style={{ ...S.miniIcon, color: "#10b981" }}>
                <TrendingUp size={20} />
              </div>
              <h3 style={S.miniTitle}>Live</h3>
              <p style={S.miniDesc}>
                Bot operativo che gira 24/7 in paper trading. Pipeline
                multi-agente attiva ogni 1 minuto via watchdog. Esegue trade
                reali su portafoglio interno e li riflette su ClawStreet
                Tournament per benchmark pubblico.
              </p>
            </div>
            <div style={S.miniCard}>
              <div style={{ ...S.miniIcon, color: "#a78bfa" }}>
                <FlaskConical size={20} />
              </div>
              <h3 style={S.miniTitle}>Simulator</h3>
              <p style={S.miniDesc}>
                Sandbox cognitivo su scenari storici reali (crisi geopolitiche,
                eventi macro, crash, rally). L'agente decide senza conoscere il
                periodo. Benchmark contro S&P 500, monkey trader e medie di
                settore. Memoria cognitiva persistente tra run.
              </p>
            </div>
          </div>
        </Section>

        {/* PIPELINE MULTI-AGENTE */}
        <Section icon={<Workflow size={22} />} title="Pipeline multi-agente">
          <p style={S.p}>
            Ogni ciclo decisionale attraversa cinque agenti specializzati che
            si passano il contesto in catena. Ogni agente ha un ruolo
            cognitivo distinto e può fallire indipendentemente senza
            corrompere lo stato globale.
          </p>

          <div style={S.agentsList}>
            <AgentCard
              n={1}
              color="#4f8cff"
              icon={<Radio size={18} />}
              name="Watchdog"
              role="Trigger detection"
              freq="ogni 1 minuto"
              desc="Monitora costantemente news feed, prezzi e drift di portafoglio. Decide se vale la pena svegliare gli agenti pesanti. Filtra rumore. Implementa cooldown globale (15 min) per evitare trigger storm. Distingue tra equity (mercato aperto + giorno feriale) e crypto (24/7)."
            />
            <AgentCard
              n={2}
              color="#06b6d4"
              icon={<Eye size={18} />}
              name="Geopolitical Analyst"
              role="Intelligence sintesi"
              freq="on-demand"
              desc="Legge notizie da NewsAPI, RSS feed selezionati e fonti istituzionali. Estrae eventi geopolitici rilevanti (conflitti, sanzioni, elezioni, decisioni banche centrali). Restituisce un dossier strutturato con livello di certezza, attori coinvolti, asset potenzialmente impattati."
            />
            <AgentCard
              n={3}
              color="#f59e0b"
              icon={<LineChart size={18} />}
              name="Market Analyst"
              role="Analisi tecnica + macro"
              freq="on-demand"
              desc="Calcola indicatori tecnici (RSI, MACD, volatilità, drift), analizza correlazioni cross-asset, valuta liquidità e regime di mercato. Integra dati Polygon + yfinance. Restituisce contesto numerico oggettivo per il Decision Agent."
            />
            <AgentCard
              n={4}
              color="#a78bfa"
              icon={<Brain size={18} />}
              name="Decision Agent"
              role="Tesi di investimento + trade"
              freq="on-demand"
              desc="Il cervello del sistema. Riceve l'output di Watchdog + Geopolitical + Market e formula una tesi di investimento articolata. Decide se aprire posizioni, ribilanciare o restare flat. Esegue tool calls per piazzare ordini. Modello: Claude Sonnet 4.5 (Live) o DeepSeek-R1 (Tournament)."
            />
            <AgentCard
              n={5}
              color="#10b981"
              icon={<Cpu size={18} />}
              name="Decision Crypto"
              role="Branch dedicato cripto"
              freq="on-demand 24/7"
              desc="Versione specializzata del Decision Agent per asset cripto. Considera ciclicità di mining, regolamentazione, halving Bitcoin, liquidazioni on-chain. Opera 24/7 indipendentemente dall'orario di mercato equity. Modello: DeepSeek-R1 con reasoning esteso."
            />
          </div>
        </Section>

        {/* WATCHDOG */}
        <Section icon={<Activity size={22} />} title="Watchdog: il cuore reattivo">
          <p style={S.p}>
            Il Watchdog è ciò che distingue GeoInvest AI da un classico bot
            schedulato. Non aspetta orari fissi: <b>monitora costantemente
            l'ambiente</b> e decide quando vale la pena attivare i modelli
            pesanti.
          </p>
          <ul style={S.ul}>
            <li>
              <b>Frequenza:</b> ogni 60 secondi, costo computazionale
              trascurabile (no LLM call).
            </li>
            <li>
              <b>Trigger types:</b> news ad alto impatto, drift di prezzo
              significativo, posizioni sovraesposte, eventi macro
              calendarizzati, rotture tecniche su asset in portafoglio.
            </li>
            <li>
              <b>Cooldown globale:</b> 15 minuti tra un'attivazione full
              pipeline e la successiva. Previene trigger storm su news
              correlate.
            </li>
            <li>
              <b>Routing per asset:</b> trigger crypto attivano sempre Decision
              Crypto. Trigger equity attivano la pipeline standard solo se
              giorno feriale + non festività NYSE + mercato aperto.
            </li>
            <li>
              <b>Rebalance autonomo:</b> se una posizione supera la soglia di
              esposizione (config-driven), il Watchdog può richiedere un
              ribilanciamento mirato senza attivare l'analisi geopolitica.
            </li>
          </ul>
        </Section>

        {/* SIMULATOR */}
        <Section icon={<FlaskConical size={22} />} title="Il Simulator: sandbox cognitivo">
          <p style={S.p}>
            Il Simulator è uno strumento di valutazione qualitativa del
            ragionamento dell'agente. Pesca uno scenario storico reale,
            mostra all'agente solo i dati disponibili a quel momento (no
            data leakage temporale) e lascia che decida cosa fare.
          </p>

          <h3 style={S.h3}>Quattro categorie di scenari</h3>
          <div style={S.fourCol}>
            <CategoryCard
              color="#10b981"
              name="Normale"
              desc="Mercato in regime ordinario. Test della capacità dell'agente di NON forzare trade quando non c'è edge."
            />
            <CategoryCard
              color="#f59e0b"
              name="Geopolitico"
              desc="Crisi internazionali, sanzioni, conflitti, elezioni chiave. Test della reattività e della corretta interpretazione."
            />
            <CategoryCard
              color="#06b6d4"
              name="Macro"
              desc="Decisioni banche centrali, inflazione, recessioni, shock energetici. Test della comprensione delle dinamiche macro."
            />
            <CategoryCard
              color="#ef4444"
              name="Crash / Rally"
              desc="Movimenti estremi (>5% giornaliero). Test della disciplina: tagliare le perdite, cavalcare i rally, evitare panic."
            />
          </div>

          <h3 style={S.h3}>Modalità di esecuzione</h3>
          <ul style={S.ul}>
            <li>
              <b>Single-step:</b> un singolo punto temporale. L'agente decide
              una volta sola e si misura il risultato a 1 settimana / 1 mese
              / 3 mesi.
            </li>
            <li>
              <b>Multi-step (3-5 step):</b> simulazione iterata. L'agente
              vede il risultato della propria decisione e può aggiornare la
              tesi al passo successivo. Test della capacità di adattamento.
            </li>
          </ul>

          <h3 style={S.h3}>Benchmark</h3>
          <ul style={S.ul}>
            <li>
              <b>vs S&P 500:</b> il riferimento classico. Battere il mercato è
              il vero test.
            </li>
            <li>
              <b>vs Monkey trader:</b> agente random. Battere il monkey è il
              minimo sindacale per dimostrare che c'è ragionamento.
            </li>
            <li>
              <b>vs Settore:</b> media degli asset della stessa categoria.
              Misura la qualità della selezione titoli a parità di tema.
            </li>
          </ul>

          <h3 style={S.h3}>Memoria cognitiva</h3>
          <p style={S.p}>
            Il Simulator mantiene una memoria persistente delle simulazioni
            passate: errori ricorrenti, pattern di successo, bias osservati.
            Questa memoria viene iniettata nei prompt dei run successivi
            come "lessons learned", consentendo un meta-apprendimento di
            sessione.
          </p>
        </Section>

        {/* LIVE */}
        <Section icon={<TrendingUp size={22} />} title="Il Live: bot operativo">
          <p style={S.p}>
            Il Live è la modalità di produzione. Il sistema gira 24/7 su
            infrastruttura cloud (Render) e gestisce un portafoglio paper
            trading interno con mirror automatico su <b>ClawStreet
            Tournament</b> per il confronto pubblico con altri bot.
          </p>

          <h3 style={S.h3}>Modalità operative</h3>
          <ul style={S.ul}>
            <li>
              <b>Standard mode (equity):</b> attiva durante giorni feriali +
              festività NYSE. Watchdog a 1 min, Decision Sonnet 4.5,
              ribilanciamento posizioni sovraesposte automatico.
            </li>
            <li>
              <b>Crypto mode:</b> sempre attiva, 24/7. Decision Crypto su
              DeepSeek-R1 con reasoning esteso. Indipendente dagli orari di
              mercato equity.
            </li>
            <li>
              <b>Idle mode:</b> fuori orario di mercato sui equity, weekend,
              festività. Crypto resta operativo. Watchdog continua il
              monitoring ma non triggera la pipeline equity.
            </li>
          </ul>

          <h3 style={S.h3}>Coach Cards</h3>
          <p style={S.p}>
            Sistema di raccomandazioni operative human-readable accessibile
            dalla dashboard Live. Sintetizza le decisioni recenti dell'agente
            in card narrative ("Apertura long su X perché...", "Chiusura Y per
            stop perdita..."), permettendo a un osservatore umano di seguire
            il ragionamento senza leggere log raw.
          </p>

          <h3 style={S.h3}>Chat Decision</h3>
          <p style={S.p}>
            Interfaccia conversazionale per interrogare le decisioni passate.
            "Perché hai aperto questa posizione?", "Cosa ti ha fatto cambiare
            idea su X?". Risposte basate sui log persistiti delle pipeline
            multi-agente.
          </p>
        </Section>

        {/* STACK TECNICO */}
        <Section icon={<Layers size={22} />} title="Stack tecnologico">
          <div style={S.stackGrid}>
            <StackCard
              icon={<Cpu size={18} />}
              title="Backend"
              items={[
                "Python 3.11",
                "FastAPI (server async)",
                "asyncio (pipeline non-blocking)",
                "AsyncIOScheduler (cron jobs)",
                "aiohttp (HTTP client async)",
              ]}
            />
            <StackCard
              icon={<Brain size={18} />}
              title="LLM Layer"
              items={[
                "Claude Sonnet 4.5 (Decision Live)",
                "DeepSeek-R1 (Decision Crypto + Tournament)",
                "Anthropic SDK + tool use",
                "OpenAI-compatible API (DeepSeek)",
                "Tool loop con max 10-15 iterazioni",
              ]}
            />
            <StackCard
              icon={<Database size={18} />}
              title="Data Layer"
              items={[
                "Supabase (PostgreSQL gestito)",
                "SQLite fallback (sviluppo)",
                "Polygon.io (market data)",
                "yfinance (fallback + crypto)",
                "NewsAPI + RSS feed istituzionali",
              ]}
            />
            <StackCard
              icon={<Network size={18} />}
              title="Frontend"
              items={[
                "React 18 + Vite",
                "React Router v6",
                "Recharts (grafici)",
                "Lucide-react (icone)",
                "jsPDF + html2canvas (export PDF)",
              ]}
            />
            <StackCard
              icon={<GitBranch size={18} />}
              title="Deployment"
              items={[
                "Render (Live, dyno + scheduler)",
                "GitHub Actions (Tournament, ogni 2h)",
                "Vercel/Render (frontend statico)",
                "Supabase tier free (state)",
              ]}
            />
            <StackCard
              icon={<Shield size={18} />}
              title="Risk & Safety"
              items={[
                "Paper trading (no fondi reali)",
                "Position sizing config-driven",
                "Stop loss + take profit automatici",
                "Cooldown globale 15 min",
                "Circuit breaker su drawdown",
              ]}
            />
          </div>
        </Section>

        {/* DECISION ENGINES */}
        <Section icon={<Brain size={22} />} title="Decision Engines: dual-model">
          <p style={S.p}>
            Il sistema supporta due engine LLM intercambiabili tramite
            variabile ambiente <code style={S.code}>DECISION_ENGINE</code>.
            Questo permette di benchmarkare modelli diversi sullo stesso
            framework cognitivo.
          </p>
          <div style={S.twoCol}>
            <div style={S.miniCard}>
              <div style={{ ...S.miniIcon, color: "#a78bfa" }}>
                <Zap size={20} />
              </div>
              <h3 style={S.miniTitle}>Claude Sonnet 4.5</h3>
              <p style={S.miniDesc}>
                Default per il bot Live production. Tool use nativo, latenza
                bassa (10-30s per ciclo), reasoning di alta qualità su
                geopolitica e market context. Costo ~$3/M input.
              </p>
            </div>
            <div style={S.miniCard}>
              <div style={{ ...S.miniIcon, color: "#10b981" }}>
                <Target size={20} />
              </div>
              <h3 style={S.miniTitle}>DeepSeek-R1</h3>
              <p style={S.miniDesc}>
                Default per Decision Crypto e variante Tournament. Reasoning
                chain-of-thought esteso (token <code>{"<think>"}</code>
                ), latenza più alta (60-120s), costo ~10x più basso di
                Sonnet. Eccellente su problemi numerici e tecnici.
              </p>
            </div>
          </div>
        </Section>

        {/* DATA FLOW */}
        <Section icon={<Workflow size={22} />} title="Flusso dati per ciclo decisionale">
          <ol style={S.ol}>
            <li>
              <b>Watchdog tick (1 min):</b> raccoglie news feed, prezzi
              correnti, posizioni aperte. Calcola scoring di urgenza.
            </li>
            <li>
              <b>Trigger evaluation:</b> se score &gt; soglia o evento critico
              + cooldown rispettato → attiva la pipeline.
            </li>
            <li>
              <b>Geopolitical Analyst:</b> sintetizza il dossier intelligence
              (sources cited, certainty levels, affected assets).
            </li>
            <li>
              <b>Market Analyst:</b> calcola indicatori tecnici e contesto
              macro per gli asset segnalati.
            </li>
            <li>
              <b>Decision Agent:</b> riceve i due dossier, formula la tesi,
              esegue tool calls (open_position / close_position /
              rebalance / no_trade) con conviction level.
            </li>
            <li>
              <b>Order execution:</b> trade piazzati su portafoglio interno +
              mirror su ClawStreet via API.
            </li>
            <li>
              <b>Persistenza:</b> log strutturati su Supabase (agent_logs,
              trades, positions, decisions). Disponibili per audit + chat
              decision + analytics.
            </li>
          </ol>
        </Section>

        {/* COSA NON È */}
        <Section icon={<AlertTriangle size={22} />} title="Cosa GeoInvest AI NON è">
          <p style={S.p}>
            Per evitare equivoci, ecco cosa il sistema esplicitamente non fa:
          </p>
          <ul style={S.ul}>
            <li>
              <b>Non muove fondi reali.</b> Tutto il trading è in paper. Il
              mirror su ClawStreet è anche lui paper-only (è un torneo di
              bot, non vero broker).
            </li>
            <li>
              <b>Non fornisce consulenza finanziaria.</b> È un progetto
              software di ricerca. Le decisioni dell'agente non sono
              raccomandazioni di investimento.
            </li>
            <li>
              <b>Non garantisce performance.</b> Le simulazioni storiche
              hanno overfitting bias. Performance passata ≠ performance
              futura.
            </li>
            <li>
              <b>Non sostituisce il giudizio umano.</b> È pensato come
              strumento di analisi e ragionamento aumentato, non come
              autopilot.
            </li>
          </ul>
        </Section>

        {/* CTA FINALE */}
        <section style={S.cta}>
          <h2 style={S.ctaTitle}>Pronto a esplorare?</h2>
          <p style={S.ctaSub}>
            Torna alla home e scegli tra il bot Live (osservazione real-time)
            o il Simulator (sandbox cognitivo).
          </p>
          <button onClick={() => nav("/")} style={S.ctaBtn}>
            <ArrowLeft size={16} /> Torna alla home
          </button>
        </section>

        <footer style={S.footer}>
          GeoInvest AI · Sistema multi-agente di investimento autonomo ·
          Paper trading only · Documentazione tecnica v1
        </footer>
      </div>
    </div>
  );
}

/* ───────────────────────────── Subcomponents ───────────────────────────── */

function Section({ icon, title, children }) {
  return (
    <section style={S.section}>
      <h2 style={S.sectionTitle}>
        <span style={S.sectionIcon}>{icon}</span>
        {title}
      </h2>
      <div style={S.sectionBody}>{children}</div>
    </section>
  );
}

function AgentCard({ n, color, icon, name, role, freq, desc }) {
  return (
    <div style={{ ...S.agentCard, borderLeftColor: color }}>
      <div style={S.agentHeader}>
        <div style={{ ...S.agentNum, background: color }}>{n}</div>
        <div style={{ flex: 1 }}>
          <div style={S.agentName}>
            <span style={{ color }}>{icon}</span>
            {name}
          </div>
          <div style={S.agentRole}>{role}</div>
        </div>
        <div style={S.agentFreq}>{freq}</div>
      </div>
      <p style={S.agentDesc}>{desc}</p>
    </div>
  );
}

function CategoryCard({ color, name, desc }) {
  return (
    <div style={{ ...S.catCard, borderTopColor: color }}>
      <div style={{ ...S.catName, color }}>{name}</div>
      <p style={S.catDesc}>{desc}</p>
    </div>
  );
}

function StackCard({ icon, title, items }) {
  return (
    <div style={S.stackCard}>
      <div style={S.stackHeader}>
        <span style={S.stackIcon}>{icon}</span>
        <span style={S.stackTitle}>{title}</span>
      </div>
      <ul style={S.stackUl}>
        {items.map((it, i) => (
          <li key={i} style={S.stackLi}>
            {it}
          </li>
        ))}
      </ul>
    </div>
  );
}

/* ───────────────────────────── Styles ───────────────────────────── */

const S = {
  root: {
    minHeight: "100vh",
    background: "linear-gradient(180deg, #0a0e1a 0%, #0f1424 100%)",
    color: "#e2e8f0",
    fontFamily: "Inter, -apple-system, sans-serif",
    padding: "24px 16px 64px",
  },
  backBtn: {
    position: "fixed",
    top: 20,
    left: 20,
    zIndex: 100,
    display: "inline-flex",
    alignItems: "center",
    gap: 8,
    padding: "10px 16px",
    background: "rgba(20,25,40,0.85)",
    border: "1px solid #2a3248",
    borderRadius: 999,
    color: "#cbd5e1",
    fontSize: "0.85rem",
    fontWeight: 500,
    cursor: "pointer",
    backdropFilter: "blur(8px)",
  },
  container: {
    maxWidth: 1100,
    margin: "0 auto",
    paddingTop: 48,
  },
  hero: {
    textAlign: "center",
    padding: "32px 16px 48px",
  },
  heroBadge: {
    display: "inline-flex",
    alignItems: "center",
    gap: 6,
    padding: "6px 14px",
    background: "rgba(167,139,250,0.12)",
    border: "1px solid rgba(167,139,250,0.3)",
    borderRadius: 999,
    color: "#c4b5fd",
    fontSize: "0.75rem",
    fontWeight: 600,
    letterSpacing: "0.04em",
    textTransform: "uppercase",
    marginBottom: 16,
  },
  heroTitle: {
    fontSize: "3.5rem",
    fontWeight: 700,
    margin: "0 0 16px 0",
    letterSpacing: "-0.02em",
    background: "linear-gradient(135deg, #4f8cff, #a78bfa)",
    WebkitBackgroundClip: "text",
    WebkitTextFillColor: "transparent",
    backgroundClip: "text",
  },
  heroSub: {
    fontSize: "1.15rem",
    color: "#94a3b8",
    margin: "0 auto 24px",
    maxWidth: 720,
    lineHeight: 1.6,
  },
  heroChips: {
    display: "flex",
    flexWrap: "wrap",
    justifyContent: "center",
    gap: 8,
  },
  chip: {
    padding: "5px 12px",
    background: "rgba(79,140,255,0.1)",
    border: "1px solid rgba(79,140,255,0.25)",
    borderRadius: 999,
    color: "#93c5fd",
    fontSize: "0.78rem",
    fontWeight: 500,
  },
  section: {
    marginBottom: 48,
    background: "rgba(20,25,40,0.5)",
    border: "1px solid #1e2538",
    borderRadius: 16,
    padding: "32px",
  },
  sectionTitle: {
    display: "flex",
    alignItems: "center",
    gap: 12,
    fontSize: "1.6rem",
    fontWeight: 600,
    margin: "0 0 20px 0",
    color: "#e2e8f0",
  },
  sectionIcon: {
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    width: 40,
    height: 40,
    background: "rgba(79,140,255,0.12)",
    border: "1px solid rgba(79,140,255,0.3)",
    borderRadius: 10,
    color: "#60a5fa",
  },
  sectionBody: {
    color: "#cbd5e1",
    fontSize: "0.95rem",
    lineHeight: 1.7,
  },
  p: {
    margin: "0 0 16px 0",
  },
  h3: {
    fontSize: "1.1rem",
    fontWeight: 600,
    margin: "24px 0 12px 0",
    color: "#e2e8f0",
  },
  ul: {
    margin: "0 0 16px 0",
    paddingLeft: "20px",
    display: "flex",
    flexDirection: "column",
    gap: 8,
  },
  ol: {
    margin: 0,
    paddingLeft: "20px",
    display: "flex",
    flexDirection: "column",
    gap: 12,
  },
  twoCol: {
    display: "grid",
    gridTemplateColumns: "1fr 1fr",
    gap: 16,
    marginTop: 16,
  },
  fourCol: {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
    gap: 12,
    marginBottom: 16,
  },
  miniCard: {
    background: "rgba(15,20,36,0.7)",
    border: "1px solid #232a40",
    borderRadius: 12,
    padding: 20,
  },
  miniIcon: {
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    width: 36,
    height: 36,
    background: "rgba(255,255,255,0.04)",
    borderRadius: 10,
    marginBottom: 12,
  },
  miniTitle: {
    fontSize: "1.1rem",
    fontWeight: 600,
    margin: "0 0 8px 0",
    color: "#e2e8f0",
  },
  miniDesc: {
    color: "#94a3b8",
    fontSize: "0.88rem",
    lineHeight: 1.55,
    margin: 0,
  },
  agentsList: {
    display: "flex",
    flexDirection: "column",
    gap: 12,
    marginTop: 16,
  },
  agentCard: {
    background: "rgba(15,20,36,0.6)",
    border: "1px solid #232a40",
    borderLeft: "3px solid",
    borderRadius: 10,
    padding: "16px 20px",
  },
  agentHeader: {
    display: "flex",
    alignItems: "center",
    gap: 12,
    marginBottom: 8,
  },
  agentNum: {
    width: 28,
    height: 28,
    borderRadius: "50%",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    color: "#0a0e1a",
    fontWeight: 700,
    fontSize: "0.85rem",
    flexShrink: 0,
  },
  agentName: {
    fontSize: "1.05rem",
    fontWeight: 600,
    color: "#e2e8f0",
    display: "flex",
    alignItems: "center",
    gap: 8,
  },
  agentRole: {
    fontSize: "0.78rem",
    color: "#94a3b8",
    marginTop: 2,
  },
  agentFreq: {
    fontSize: "0.75rem",
    color: "#64748b",
    fontStyle: "italic",
    flexShrink: 0,
  },
  agentDesc: {
    color: "#94a3b8",
    fontSize: "0.88rem",
    lineHeight: 1.6,
    margin: 0,
  },
  catCard: {
    background: "rgba(15,20,36,0.7)",
    border: "1px solid #232a40",
    borderTop: "3px solid",
    borderRadius: 10,
    padding: 16,
  },
  catName: {
    fontSize: "1rem",
    fontWeight: 600,
    marginBottom: 8,
  },
  catDesc: {
    color: "#94a3b8",
    fontSize: "0.85rem",
    lineHeight: 1.55,
    margin: 0,
  },
  stackGrid: {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))",
    gap: 12,
    marginTop: 16,
  },
  stackCard: {
    background: "rgba(15,20,36,0.7)",
    border: "1px solid #232a40",
    borderRadius: 10,
    padding: 16,
  },
  stackHeader: {
    display: "flex",
    alignItems: "center",
    gap: 10,
    marginBottom: 12,
    paddingBottom: 10,
    borderBottom: "1px solid #1e2538",
  },
  stackIcon: {
    color: "#60a5fa",
    display: "inline-flex",
  },
  stackTitle: {
    fontSize: "0.95rem",
    fontWeight: 600,
    color: "#e2e8f0",
  },
  stackUl: {
    listStyle: "none",
    padding: 0,
    margin: 0,
    display: "flex",
    flexDirection: "column",
    gap: 6,
  },
  stackLi: {
    color: "#94a3b8",
    fontSize: "0.82rem",
    paddingLeft: 12,
    position: "relative",
  },
  code: {
    background: "rgba(167,139,250,0.12)",
    color: "#c4b5fd",
    padding: "1px 6px",
    borderRadius: 4,
    fontSize: "0.85em",
    fontFamily: "JetBrains Mono, Menlo, monospace",
  },
  cta: {
    textAlign: "center",
    padding: "48px 24px",
    background:
      "linear-gradient(135deg, rgba(79,140,255,0.08), rgba(167,139,250,0.08))",
    border: "1px solid rgba(167,139,250,0.2)",
    borderRadius: 16,
    marginBottom: 32,
  },
  ctaTitle: {
    fontSize: "1.8rem",
    fontWeight: 600,
    margin: "0 0 12px 0",
    color: "#e2e8f0",
  },
  ctaSub: {
    color: "#94a3b8",
    fontSize: "1rem",
    margin: "0 auto 24px",
    maxWidth: 500,
    lineHeight: 1.6,
  },
  ctaBtn: {
    display: "inline-flex",
    alignItems: "center",
    gap: 8,
    padding: "12px 28px",
    background: "#a78bfa",
    color: "#0a0e1a",
    border: "none",
    borderRadius: 999,
    fontSize: "0.95rem",
    fontWeight: 600,
    cursor: "pointer",
    fontFamily: "inherit",
  },
  footer: {
    textAlign: "center",
    color: "#475569",
    fontSize: "0.78rem",
    paddingTop: 24,
    borderTop: "1px solid #1e2538",
  },
};
