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
 * AboutPage — Documentazione pubblica di GeoInvest AI.
 * Accessibile senza autenticazione, indicizzabile.
 *
 * Descrive architettura e FILOSOFIA (misurare bravura, non fortuna).
 * Volutamente NON espone: prompt, regole decisionali precise che
 * costituiscono l'edge, implementazione delle leve di confidence,
 * ticker/importi. Resta a livello architettura + metodologia.
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
            geopolitica, dati di mercato e ragionamento LLM per decidere su
            equity e crypto — con un'ossessione: misurare bravura, non fortuna.
          </p>
          <div style={S.heroChips}>
            <span style={S.chip}>Pipeline multi-agente</span>
            <span style={S.chip}>Claude Sonnet 4.5</span>
            <span style={S.chip}>DeepSeek-R1 / V3</span>
            <span style={S.chip}>Paper trading</span>
            <span style={S.chip}>Forward testing</span>
            <span style={S.chip}>Edge Tracker</span>
          </div>
        </section>

        {/* COS'È */}
        <Section icon={<BookOpen size={22} />} title="Cos'è GeoInvest AI">
          <p style={S.p}>
            GeoInvest AI opera come un fund manager quantitativo autonomo.
            Invece di seguire regole fisse, usa una pipeline di agenti
            cognitivi basati su Large Language Model che leggono notizie
            geopolitiche e finanziarie, ne sintetizzano le implicazioni di
            mercato e decidono se e come modificare un portafoglio.
          </p>
          <p style={S.p}>
            Tutto il trading è <b>paper</b> (nessun fondo reale). Il valore
            del progetto non è "quanto rende" su un buon mese, ma se esiste un
            <b> vantaggio dimostrabile</b> separato dalla fortuna di regime.
          </p>
          <div style={S.twoCol}>
            <div style={S.miniCard}>
              <div style={{ ...S.miniIcon, color: "#10b981" }}>
                <TrendingUp size={20} />
              </div>
              <h3 style={S.miniTitle}>Live</h3>
              <p style={S.miniDesc}>
                Bot operativo che gira 24/7 su infrastruttura cloud. Pipeline
                multi-agente attivata su trigger dal Watchdog. Portafoglio
                paper interno con stato persistente su database gestito.
              </p>
            </div>
            <div style={S.miniCard}>
              <div style={{ ...S.miniIcon, color: "#a78bfa" }}>
                <FlaskConical size={20} />
              </div>
              <h3 style={S.miniTitle}>Simulator</h3>
              <p style={S.miniDesc}>
                Sandbox cognitivo su scenari storici reali. L'agente decide
                senza conoscere il periodo (no data leakage temporale).
                Benchmark contro S&P 500, monkey trader e media di settore.
                Memoria cognitiva persistente tra run.
              </p>
            </div>
          </div>
        </Section>

        {/* PIPELINE MULTI-AGENTE */}
        <Section icon={<Workflow size={22} />} title="Pipeline multi-agente">
          <p style={S.p}>
            Ogni ciclo decisionale attraversa agenti specializzati con modelli
            diversi e ruoli cognitivi distinti. Ogni agente può fallire
            indipendentemente senza corrompere lo stato globale. Lo scheduler
            attiva il ciclo completo a intervalli regolari; il ramo crypto
            opera 24/7 indipendentemente dagli orari di borsa.
          </p>

          <div style={S.agentsList}>
            <AgentCard
              n={1}
              color="#4f8cff"
              icon={<Radio size={18} />}
              name="Watchdog"
              role="Trigger detection · DeepSeek-V3"
              freq="~5 min (mercato aperto)"
              desc="Sentinella a costo quasi nullo. Assegna un punteggio di urgenza (1-10) a news, drift di prezzo e posizioni sovraesposte. Sopra soglia attiva la pipeline pesante. Cooldown globale 15 min anti trigger-storm. Se una posizione supera la soglia di concentrazione, forza un ribilanciamento mirato."
            />
            <AgentCard
              n={2}
              color="#06b6d4"
              icon={<Eye size={18} />}
              name="Scout — Intelligence a cascata"
              role="Sintesi multi-fonte · DeepSeek-V3"
              freq="ogni 20 min · 24/7"
              desc="Aggrega 9 fonti (GDELT, NewsAPI, news ticker, Reddit, X, trade del Congresso USA, CoinGecko, Fear & Greed, sentiment) in micro-schede L0, poi sintetizzate in report a 8 ore e a 4 giorni. È la memoria di contesto che il Decision legge — niente è analizzato due volte."
            />
            <AgentCard
              n={3}
              color="#f59e0b"
              icon={<LineChart size={18} />}
              name="Technical Analyst"
              role="Analisi quantitativa · DeepSeek-V3"
              freq="on-demand (Fase 2)"
              desc="Calcola indicatori (RSI, MACD, Bollinger, momentum), pattern, livelli di supporto/resistenza e confluenze multi-timeframe. Restituisce contesto numerico oggettivo richiesto esplicitamente dal Decision Agent."
            />
            <AgentCard
              n={4}
              color="#a78bfa"
              icon={<Brain size={18} />}
              name="Decision Standard"
              role="Tesi + trade equity · Claude Sonnet 4.5"
              freq="on-demand (su trigger)"
              desc="Il cervello del sistema per equity/ETF. Riceve contesto cascata + tecnico + portafoglio, formula una tesi causale e decide. Vincolato a un workflow obbligatorio a 4 fasi che impedisce decisioni impulsive."
            />
            <AgentCard
              n={5}
              color="#10b981"
              icon={<Cpu size={18} />}
              name="Decision Crypto"
              role="Branch cripto dedicato · DeepSeek-R1"
              freq="~ogni ora · 24/7"
              desc="Versione specializzata per asset cripto: ciclicità, regolamentazione, dominanza BTC, rischio liquidazioni. Reasoning chain-of-thought esteso. Opera indipendentemente dall'orario di mercato equity."
            />
          </div>
        </Section>

        {/* WORKFLOW 4 FASI */}
        <Section icon={<GitBranch size={22} />} title="Workflow decisionale a 4 fasi">
          <p style={S.p}>
            Il Decision Agent non può "sparare un trade" d'impulso. È vincolato
            a una macchina a stati che il sistema fa rispettare via tool:
          </p>
          <ol style={S.ol}>
            <li>
              <b>Valutazione iniziale</b> — analisi della situazione corrente
              (portafoglio, briefing cascata, sentiment). Minimo 200 caratteri.
            </li>
            <li>
              <b>Richiesta dati tecnici</b> — interroga il Technical Analyst
              sui ticker candidati (max 2 chiamate per run).
            </li>
            <li>
              <b>Tesi finale</b> — tesi causale "se X allora Y perché", piano
              d'azione e rischio principale. Minimo 200 caratteri.
            </li>
            <li>
              <b>Esecuzione</b> — solo ora può chiamare execute_trade. Se prova
              prima di aver completato le fasi, il sistema rifiuta il tool.
            </li>
          </ol>
        </Section>

        {/* MISURARE BRAVURA, NON FORTUNA */}
        <Section icon={<Target size={22} />} title="Misurare bravura, non fortuna">
          <p style={S.p}>
            È il punto identitario del progetto. Un rendimento positivo su un
            mese non distingue <b>edge</b> (bravura) da <b>beta</b> (eri long
            mentre il mercato saliva). Questi strumenti servono a non illudersi.
          </p>

          <h3 style={S.h3}>Edge Tracker</h3>
          <p style={S.p}>
            Applica criteri decisi <b>a priori e mostrati in chiaro</b> (così
            non sono spostabili dopo aver visto i risultati): campione minimo
            di trade chiusi, profit factor calcolato sui dollari, asimmetria
            guadagni/perdite, alpha contro l'S&P 500 sulla vita reale del
            portafoglio, e una confidence che deve davvero discriminare.
            Verdetto onesto: edge dimostrato / nessun edge / in costruzione /
            dati insufficienti. Tutti i dati grezzi del calcolo sono esposti.
          </p>

          <h3 style={S.h3}>Solo le decisioni vere dell'AI</h3>
          <ul style={S.ul}>
            <li>
              <b>Inizio ufficiale ancorato</b>: la misurazione parte dal primo
              movimento operativo reale, escludendo il periodo iniziale
              inattivo — return e alpha coprono la vita vera del portafoglio.
            </li>
            <li>
              <b>Chiusure non-AI escluse</b>: stop forzati, auto-exit e
              chiusure manuali non entrano in nessuna analisi/grafico. L'edge
              misurato riflette solo round-trip decisi dall'AI. Eccezione
              voluta: il termometro di sicurezza del rischio resta onesto.
            </li>
          </ul>

          <h3 style={S.h3}>Calibrazione della confidence</h3>
          <p style={S.p}>
            La "confidence" non è più "quanto mi piace la tesi" ma una
            <b> probabilità verificabile</b> abbinata a un rapporto
            rischio/rendimento atteso obbligatorio. Una diagnosi dedicata
            mostra quali trade rompono la relazione (molto sicuro → perso vs
            poco sicuro → vinto) e l'agente riceve, al momento di decidere, il
            proprio storico di calibrazione — così ritara invece di ripetere
            l'errore tipico: iper-confidenza sulle tesi di consenso già
            scontate dal mercato.
          </p>
        </Section>

        {/* RISCHIO */}
        <Section icon={<Shield size={22} />} title="Gestione del rischio">
          <p style={S.p}>
            Il rischio è gestito a strati, con la sicurezza prima
            dell'aggressività (alcune protezioni automatiche sono opt-in,
            disattivate di default dopo un incidente passato di
            auto-liquidazione):
          </p>
          <ul style={S.ul}>
            <li>
              <b>Profili di rischio</b> (conservativo / moderato / aggressivo):
              tetto per posizione, confidence minima, range stop-loss, numero
              massimo di posizioni, drawdown massimo. È un cancello duro: i
              trade che li violano vengono rifiutati.
            </li>
            <li>
              <b>Recovery mode</b>: sotto una certa perdita, vincoli più
              stretti finché il portafoglio non recupera.
            </li>
            <li>
              <b>Circuit breaker</b> su drawdown 24h, allarme di
              concentrazione, lock-in del profitto e trailing stop dinamico
              (tutti attivabili su scelta).
            </li>
            <li>
              <b>Realismo</b>: commissioni simulate (~0,10%) per non gonfiare
              il P&L, e tre livelli di guardia anti dati-corrotti sul valore
              del portafoglio.
            </li>
          </ul>
        </Section>

        {/* SIMULATOR */}
        <Section icon={<FlaskConical size={22} />} title="Il Simulator: sandbox cognitivo">
          <p style={S.p}>
            Strumento di valutazione del ragionamento dell'agente. Pesca uno
            scenario storico reale, mostra solo i dati disponibili a quel
            momento (no data leakage temporale) e lo lascia decidere.
          </p>

          <h3 style={S.h3}>Categorie di scenario</h3>
          <div style={S.fourCol}>
            <CategoryCard
              color="#10b981"
              name="Normale"
              desc="Mercato ordinario. Test della capacità di NON forzare trade quando non c'è edge."
            />
            <CategoryCard
              color="#f59e0b"
              name="Geopolitico"
              desc="Crisi internazionali, sanzioni, conflitti, elezioni. Test di reattività e interpretazione."
            />
            <CategoryCard
              color="#06b6d4"
              name="Macro"
              desc="Banche centrali, inflazione, recessioni, shock energetici. Test delle dinamiche macro."
            />
            <CategoryCard
              color="#ef4444"
              name="Crash / Rally"
              desc="Movimenti estremi. Test della disciplina: tagliare le perdite, cavalcare i rally, niente panico."
            />
          </div>

          <h3 style={S.h3}>Esecuzione e benchmark</h3>
          <ul style={S.ul}>
            <li>
              <b>Equity</b>: step da 1 settimana, 3-5 turni. <b>Crypto</b>:
              step da 2 giorni, 5-7 turni (DeepSeek-R1). Multi-step: l'agente
              vede l'esito della propria decisione e può aggiornare la tesi.
            </li>
            <li>
              <b>Benchmark</b>: vs S&P 500, vs monkey trader (random) e vs
              media di settore. Metriche pure: Sharpe, max drawdown, profit
              factor.
            </li>
            <li>
              <b>Loop Sim → Live</b>: le lezioni accumulate vengono distillate
              settimanalmente in "Coach Cards" iniettate nel prompt del Live.
              Il sapere del Simulator non va sprecato.
            </li>
          </ul>
        </Section>

        {/* LIVE */}
        <Section icon={<TrendingUp size={22} />} title="Il Live: bot operativo">
          <p style={S.p}>
            Modalità di produzione, 24/7 su cloud. Portafoglio paper interno,
            stato persistente su database gestito. Tre modalità: standard
            (equity, in orario di mercato), crypto (sempre attiva) e idle
            (fuori mercato — crypto resta operativo, il Watchdog continua a
            monitorare).
          </p>
          <h3 style={S.h3}>Strumenti di osservabilità</h3>
          <ul style={S.ul}>
            <li>
              <b>Dashboard & Analytics</b>: equity curve, posizioni, P&L, KPI,
              alpha, calibrazione.
            </li>
            <li>
              <b>Edge Tracker</b>: il verdetto onesto + la diagnosi della
              confidence.
            </li>
            <li>
              <b>Intelligence</b>: le micro-schede dello Scout e il "thinking
              process" per run.
            </li>
            <li>
              <b>Coach Cards & Chat Decision</b>: raccomandazioni leggibili e
              chat per interrogare le decisioni passate ("perché hai aperto
              questa posizione?").
            </li>
          </ul>
          <h3 style={S.h3}>Memoria operativa</h3>
          <p style={S.p}>
            L'agente registra "impegni" condizionati che ricompaiono nei run
            successivi finché non risolti o scaduti, e vede le proprie ultime
            decisioni: chiude il feedback loop ed evita di ripetere errori o
            contraddirsi.
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
                "FastAPI (async)",
                "asyncio (pipeline non-blocking)",
                "Scheduler asincrono",
                "Frontend React servito da FastAPI",
              ]}
            />
            <StackCard
              icon={<Brain size={18} />}
              title="LLM Layer"
              items={[
                "Claude Sonnet 4.5 (Decision equity)",
                "DeepSeek-R1 (Decision Crypto / reasoning)",
                "DeepSeek-V3 (Watchdog/Scout/Technical)",
                "Tool use + tool loop",
                "Engine intercambiabile via env",
              ]}
            />
            <StackCard
              icon={<Database size={18} />}
              title="Data Layer"
              items={[
                "Supabase (PostgreSQL) — produzione",
                "SQLite — sviluppo locale",
                "Polygon → Massive → yfinance (cascata)",
                "GDELT, NewsAPI, Reddit, X, Congresso",
                "CoinGecko + Fear & Greed (crypto)",
              ]}
            />
            <StackCard
              icon={<Network size={18} />}
              title="Frontend"
              items={[
                "React 18 + React Router",
                "Recharts (grafici)",
                "Lucide-react (icone)",
                "Tema scuro, USD only",
                "Error Boundary + degrado controllato",
              ]}
            />
            <StackCard
              icon={<GitBranch size={18} />}
              title="Deployment"
              items={[
                "Render (auto-deploy su push)",
                "Stato su Supabase (indipendente dall'host)",
                "Gateway inferenza con circuit breaker",
                "Resilienza UI a backend instabile",
              ]}
            />
            <StackCard
              icon={<Shield size={18} />}
              title="Risk & Safety"
              items={[
                "Paper trading (no fondi reali)",
                "Profili rischio config-driven",
                "Stop-loss / take-profit validati",
                "Cooldown globale 15 min",
                "Circuit breaker (opt-in)",
              ]}
            />
          </div>
        </Section>

        {/* DECISION ENGINES */}
        <Section icon={<Brain size={22} />} title="Decision Engines: multi-modello">
          <p style={S.p}>
            Lo stesso framework cognitivo può girare su engine LLM diversi
            (selezionabili via variabile ambiente
            <code style={S.code}>DECISION_ENGINE</code>), così si possono
            benchmarkare modelli a parità di pipeline.
          </p>
          <div style={S.twoCol}>
            <div style={S.miniCard}>
              <div style={{ ...S.miniIcon, color: "#a78bfa" }}>
                <Zap size={20} />
              </div>
              <h3 style={S.miniTitle}>Claude Sonnet 4.5</h3>
              <p style={S.miniDesc}>
                Default per il Decision equity. Tool use nativo, latenza bassa,
                reasoning di alta qualità su geopolitica e contesto di mercato.
              </p>
            </div>
            <div style={S.miniCard}>
              <div style={{ ...S.miniIcon, color: "#10b981" }}>
                <Target size={20} />
              </div>
              <h3 style={S.miniTitle}>DeepSeek-R1 / V3</h3>
              <p style={S.miniDesc}>
                R1 (reasoning esteso) per Decision Crypto; V3 per gli agenti di
                supporto (Watchdog, Scout, Technical). Costo molto più basso,
                ottimo su problemi numerici e di sintesi.
              </p>
            </div>
          </div>
        </Section>

        {/* DATA FLOW */}
        <Section icon={<Workflow size={22} />} title="Flusso dati per ciclo decisionale">
          <ol style={S.ol}>
            <li>
              <b>Scout (20 min):</b> aggrega 9 fonti in micro-schede L0 →
              report 8H → report 4D (intelligence a cascata).
            </li>
            <li>
              <b>Watchdog (~5 min):</b> calcola l'urgenza su news, prezzi e
              posizioni. Sopra soglia + cooldown rispettato → attiva.
            </li>
            <li>
              <b>Decision (4 fasi):</b> legge contesto cascata + portafoglio,
              richiede analisi tecnica, formula la tesi, poi esegue.
            </li>
            <li>
              <b>Esecuzione:</b> trade sul portafoglio paper, con validazione
              dei profili di rischio e delle soglie.
            </li>
            <li>
              <b>Persistenza:</b> log strutturati su database (agent_logs,
              trades, positions, decisioni) per audit, chat e analytics.
            </li>
          </ol>
        </Section>

        {/* COSA NON È */}
        <Section icon={<AlertTriangle size={22} />} title="Cosa GeoInvest AI NON è">
          <ul style={S.ul}>
            <li>
              <b>Non muove fondi reali.</b> Tutto il trading è paper. È un
              progetto software di ricerca.
            </li>
            <li>
              <b>Non fornisce consulenza finanziaria.</b> Le decisioni
              dell'agente non sono raccomandazioni di investimento.
            </li>
            <li>
              <b>Non garantisce performance.</b> Performance passata ≠
              performance futura; un campione piccolo è fortuna, non bravura.
            </li>
            <li>
              <b>Non sostituisce il giudizio umano.</b> È analisi e
              ragionamento aumentato, non un autopilot.
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
          Paper trading only · Documentazione tecnica v2
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
