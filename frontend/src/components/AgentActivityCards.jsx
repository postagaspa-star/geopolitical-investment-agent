import { useEffect, useState, useMemo } from "react";
import { Eye, BookOpen, BarChart3, Brain, Target, AlertCircle } from "lucide-react";

const API = window.location.origin;

/**
 * AgentActivityCards — sostituisce la lista log raw nella Dashboard Live.
 *
 * Mostra card strutturate per:
 *  - Watchdog: SOLO trigger positivi (= ha svegliato Decision/Tech) con motivazione
 *  - Scout: solo "creato N schede" + breve riassunto
 *  - Technical: tutte le analisi sui ticker
 *  - Decision: due card → ragionamento + decisione presa
 *
 * Design: niente scroll interno (le card mostrano tutto il contenuto),
 * border-left colorato per identificare il tipo, hover lift soft.
 */
export default function AgentActivityCards() {
  const [logs, setLogs] = useState([]);
  const [loading, setLoading] = useState(true);

  const reload = async () => {
    try {
      // limit 200 -> 80: la UI mostra comunque solo le prime 30 card
      // (.slice(0,30)). 80 log bastano per coprirle anche dopo il
      // filtraggio del rumore. Riduce ~60% del payload egress Supabase.
      const res = await fetch(`${API}/api/logs?limit=80`);
      if (res.ok) setLogs(await res.json());
    } catch {}
    setLoading(false);
  };

  useEffect(() => {
    reload();
    // poll 15s -> 30s: l'endpoint /api/logs e' cachato 20s lato backend,
    // pollare ogni 15s sprecava query. 30s dimezza i fetch lato client
    // e si allinea bene alla cache backend.
    const id = setInterval(reload, 30000);
    return () => clearInterval(id);
  }, []);

  const cards = useMemo(() => buildCards(logs), [logs]);

  if (loading) return <div style={S.empty}>Caricamento…</div>;
  if (!cards.length) return <div style={S.empty}>Nessuna attività recente</div>;

  return (
    <>
      <style>{KEYFRAMES}</style>
      <div style={S.list}>
        {cards.slice(0, 30).map((c, i) => (
          <div
            key={c.key || i}
            className="agent-card"
            style={{
              animation: "agentCardIn 0.35s ease-out both",
              animationDelay: `${Math.min(i * 0.02, 0.3)}s`,
            }}
          >
            <CardRenderer card={c} />
          </div>
        ))}
      </div>
    </>
  );
}

// ═══════════════════════════════════════════════════════════════════════
// PARSING: trasforma log raw in card
// ═══════════════════════════════════════════════════════════════════════

function buildCards(logs) {
  const cards = [];
  const skipNoise = new Set(["watchdog_throttled", "watchdog_complete"]);

  for (const log of logs) {
    const phase = (log.phase || "").toUpperCase();
    let content;
    try { content = JSON.parse(log.content || "{}"); }
    catch { content = { raw: log.content }; }
    const ts = log.timestamp;

    // ─── WATCHDOG: trigger + eventi rebalance (anche skipped/throttled) ───
    // Skip solo gli eventi puramente "rumore" (throttle Decision, no-trigger
    // generico). Mostra invece sempre gli eventi rebalance: il trigger e' il
    // caso ovvio, ma anche skipped/throttled sono informativi (l'utente vuole
    // capire perche' il watchdog non sta agendo su una posizione overweight).
    if (phase === "WATCHDOG") {
      if (skipNoise.has(content.event) && !content.trigger) continue;
      if (content.event === "watchdog_complete" && !content.trigger) continue;
      if (content.event === "watchdog_throttled") continue;

      // Eventi rebalance "non-trigger" (skipped/throttled): mostrali con
      // variant diagnostica perche' la motivazione e' utile per debugging.
      if (content.event === "watchdog_rebalance_skipped"
          || content.event === "watchdog_rebalance_throttled") {
        cards.push({
          key: `wd_${log.id || ts}`,
          type: "watchdog",
          ts,
          urgency: 0,
          reason: content.reason,
          focus_tickers: content.ticker_overweight ? [content.ticker_overweight] : [],
          variant: content.event === "watchdog_rebalance_skipped"
            ? "rebalance_skipped" : "rebalance_throttled",
          pct_of_portfolio: content.pct_of_portfolio,
          all_overweight: content.all_overweight || [],
        });
        continue;
      }

      if (content.trigger) {
        cards.push({
          key: `wd_${log.id || ts}`,
          type: "watchdog",
          ts,
          urgency: content.urgency,
          reason: content.reason,
          focus_tickers: content.focus_tickers
            || (content.ticker_overweight ? [content.ticker_overweight] : []),
          deep_check: content.deep_check,
          portfolio_tickers: content.portfolio_tickers,
          variant: content.event === "watchdog_rebalance_trigger" ? "rebalance" : "trigger",
          pct_of_portfolio: content.pct_of_portfolio,
        });
      }
      continue;
    }

    if (phase === "ORCHESTRATOR") continue;

    // ─── SCOUT: solo scout_20min_complete con riassunto ───
    if (phase === "SCOUT" && content.event === "scout_20min_complete") {
      const sources = content.sources_summary || {};
      const okSources = Object.entries(sources)
        .filter(([_, v]) => v.status === "ok" && v.count > 0)
        .map(([k]) => k);
      cards.push({
        key: `sc_${log.id || ts}`,
        type: "scout",
        ts,
        cards_created: content.micro_cards || 0,
        written_to_buffer: content.written_to_buffer || 0,
        skipped_duplicates: content.skipped_duplicates || 0,
        sources_active: okSources,
        sources_total: Object.keys(sources).length,
      });
      continue;
    }
    if (phase.startsWith("SCOUT")) continue;

    // ─── TECHNICAL ───
    if (phase === "TECH_WORKER" && content.event === "technical_analysis_complete") {
      cards.push({
        key: `tw_${log.id || ts}`,
        type: "technical",
        ts,
        engine: content.engine,
        analyses: content.analyses_summary || [],
        summary: content.summary_text,
        tickers_analyzed: content.tickers_analyzed,
        tickers_requested: content.tickers_requested,
      });
      continue;
    }
    if (phase === "TECH_CRYPTO" && content.event === "tech_crypto_complete") {
      cards.push({
        key: `tc_${log.id || ts}`,
        type: "technical_crypto",
        ts,
        engine: content.engine,
        analyses: content.analyses_summary || [],
        summary: content.summary_text,
        tickers_analyzed: content.tickers_analyzed,
        tickers_with_data: content.tickers_with_data || [],
      });
      continue;
    }

    // ─── DECISION ───
    if (phase === "DECISION_REASONING") {
      // FIX: il Decision Crypto produce sia DECISION_REASONING che
      // DECISION_CRYPTO_COMPLETE, entrambi col reasoning_text. Per evitare
      // doppia card "Decision — Ragionamento" + "Decision Crypto" con
      // contenuto sovrapposto, skippiamo la card per agent="crypto" — il
      // ragionamento e' gia' renderizzato (con thesis/action_plan/primary_risk
      // separati) dalla DecisionCryptoCard.
      if (content.agent === "crypto") continue;
      cards.push({
        key: `dr_${log.id || ts}`,
        type: "decision_reasoning",
        ts,
        model: content.model,
        reasoning_text: content.reasoning_text,
      });
      continue;
    }
    if (phase === "DECISION_TRADE") {
      cards.push({
        key: `dt_${log.id || ts}`,
        type: "decision_decision",
        ts,
        action: content.action, ticker: content.ticker, qty: content.qty,
        price: content.price, confidence: content.confidence,
        stop_loss: content.stop_loss, take_profit: content.take_profit,
        decision_label: "TRADE",
      });
      continue;
    }
    if (phase === "DECISION_NO_TRADE") {
      cards.push({
        key: `dn_${log.id || ts}`,
        type: "decision_decision",
        ts,
        decision_label: "NO TRADE",
        reasoning: content.reasoning,
      });
      continue;
    }
    if (phase === "DECISION_ERROR") {
      cards.push({
        key: `de_${log.id || ts}`,
        type: "decision_error",
        ts,
        error_type: content.error_type,
        error_message: content.error_message,
      });
      continue;
    }

    if (phase === "DECISION_CRYPTO_COMPLETE") {
      // FIX: includere reasoning_text + thesis/action_plan/primary_risk
      // (ora salvati anche in DECISION_CRYPTO_COMPLETE oltre che in
      // DECISION_REASONING). Fallback chain: reasoning_text → final_text →
      // thesis (primo non vuoto). Senza fallback, R1 che termina con solo
      // tool calls e final_text="" mostrava "(testo vuoto)" sebbene il
      // ragionamento esistesse nel record DECISION_REASONING separato.
      const richText = content.reasoning_text || content.final_text
                        || content.thesis || "";
      cards.push({
        key: `dcc_${log.id || ts}`,
        type: "decision_crypto",
        ts, model: content.model,
        trades_executed: content.trades_executed || 0,
        final_text: richText,
        thesis: content.thesis || "",
        action_plan: content.action_plan || "",
        primary_risk: content.primary_risk || "",
      });
      continue;
    }
    if (phase === "DECISION_CRYPTO_TRADE") {
      cards.push({
        key: `dct_${log.id || ts}`,
        type: "decision_decision",
        ts,
        action: content.action, ticker: content.ticker, qty: content.qty,
        price: content.price, confidence: content.confidence,
        decision_label: "CRYPTO TRADE",
      });
      continue;
    }
    if (phase === "DECISION_CRYPTO_TRADE_FAILED") {
      // Senza questa card l'utente vedeva trade silenziosamente fallito
      // ("ha fatto l'operazione ma non ha mostrato nulla altro"). Ora
      // appare un card rossa con il motivo del rifiuto.
      cards.push({
        key: `dctf_${log.id || ts}`,
        type: "decision_trade_failed",
        ts,
        action: content.action, ticker: content.ticker, qty: content.qty,
        price: content.price, reason: content.reason || "?",
      });
      continue;
    }
  }

  return cards.sort((a, b) => new Date(b.ts) - new Date(a.ts));
}

// ═══════════════════════════════════════════════════════════════════════
// RENDER
// ═══════════════════════════════════════════════════════════════════════

function timeAgo(iso) {
  if (!iso) return "?";
  const diff = Date.now() - new Date(iso).getTime();
  const m = Math.floor(diff / 60000);
  if (m < 1) return "ora";
  if (m < 60) return `${m}m fa`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h fa`;
  return `${Math.floor(h / 24)}g fa`;
}

function CardRenderer({ card }) {
  switch (card.type) {
    case "watchdog": return <WatchdogCard c={card} />;
    case "scout": return <ScoutCard c={card} />;
    case "technical": return <TechCard c={card} />;
    case "technical_crypto": return <TechCard c={card} crypto />;
    case "decision_reasoning": return <ReasoningCard c={card} />;
    case "decision_decision": return <DecisionCard c={card} />;
    case "decision_crypto": return <DecisionCryptoCard c={card} />;
    case "decision_trade_failed": return <TradeFailedCard c={card} />;
    case "decision_error": return <ErrorCard c={card} />;
    default: return null;
  }
}

// ─── Header riutilizzabile ──────────────────────────────────────────────
function CardHeader({ icon: Icon, iconColor, title, subtitle, ts, badges }) {
  return (
    <div style={S.head}>
      <div style={{ ...S.iconWrap, background: `${iconColor}20` }}>
        <Icon size={15} style={{ color: iconColor }} />
      </div>
      <div style={{ display: "flex", flexDirection: "column", minWidth: 0, flex: 1 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <strong style={S.title}>{title}</strong>
          {badges}
        </div>
        {subtitle && <div style={S.subtitle}>{subtitle}</div>}
      </div>
      <span style={S.timestamp}>{timeAgo(ts)}</span>
    </div>
  );
}

// ─── Cards ──────────────────────────────────────────────────────────────

function WatchdogCard({ c }) {
  // Variant determina colore/titolo: trigger=giallo, rebalance=arancione,
  // skipped=grigio (informativo), throttled=blu (cooldown attivo)
  const variant = c.variant || "trigger";
  const config = {
    trigger:             { tone: "#fbbf24", title: "Watchdog → trigger" },
    rebalance:           { tone: "#fb923c", title: "Watchdog → REBALANCE TRIGGER" },
    rebalance_skipped:   { tone: "#94a3b8", title: "Watchdog → rebalance skipped" },
    rebalance_throttled: { tone: "#60a5fa", title: "Watchdog → rebalance throttled" },
  }[variant] || { tone: "#fbbf24", title: "Watchdog" };

  // Per rebalance mostra anche pct di portafoglio e altri overweight
  const showRebalanceDetails =
    variant === "rebalance" || variant === "rebalance_skipped" || variant === "rebalance_throttled";

  return (
    <div style={S.card(config.tone)}>
      <CardHeader
        icon={Eye} iconColor={config.tone} title={config.title} ts={c.ts}
        badges={
          variant === "trigger" || variant === "rebalance"
            ? <span style={S.urgencyBadge}>urgency {c.urgency}/10</span>
            : null
        }
      />
      <div style={S.body}>
        <span style={S.label}>Motivazione</span>
        <span style={S.value}>{c.reason || "(nessuna motivazione registrata)"}</span>
      </div>
      {showRebalanceDetails && c.pct_of_portfolio !== undefined && (
        <div style={S.body}>
          <span style={S.label}>% del NAV</span>
          <span style={S.value}>{c.pct_of_portfolio?.toFixed?.(1) ?? c.pct_of_portfolio}%</span>
        </div>
      )}
      {c.focus_tickers?.length > 0 && (
        <div style={S.tickerRow}>
          <span style={S.labelInline}>Focus:</span>
          {c.focus_tickers.map(t => <span key={t} style={S.tickerPill}>{t}</span>)}
        </div>
      )}
      {showRebalanceDetails && c.all_overweight?.length > 1 && (
        <div style={S.tickerRow}>
          <span style={S.labelInline}>Altri overweight:</span>
          {c.all_overweight.filter(t => !c.focus_tickers?.includes(t))
            .map(t => <span key={t} style={S.tickerPill}>{t}</span>)}
        </div>
      )}
    </div>
  );
}

function ScoutCard({ c }) {
  const tone = "#a78bfa";
  return (
    <div style={S.card(tone)}>
      <CardHeader
        icon={BookOpen} iconColor={tone} title="Scout" ts={c.ts}
        subtitle={`${c.sources_active.length}/${c.sources_total} fonti attive`}
      />
      <div style={S.statsRow}>
        <Stat value={c.cards_created} label="schede" big />
        <Stat value={c.written_to_buffer} label="scritte" />
        <Stat value={c.skipped_duplicates} label="duplicate" muted />
      </div>
      {c.sources_active.length > 0 && (
        <div style={S.tickerRow}>
          {c.sources_active.map(s => <span key={s} style={S.sourceTag}>{s}</span>)}
        </div>
      )}
    </div>
  );
}

function TechCard({ c, crypto }) {
  const tone = crypto ? "#0891b2" : "#06b6d4";
  return (
    <div style={S.card(tone)}>
      <CardHeader
        icon={BarChart3} iconColor={tone}
        title={`Technical ${crypto ? "Crypto" : "Equity"}`}
        ts={c.ts}
        badges={<span style={S.engineBadge}>{c.engine}</span>}
      />
      {c.summary && <div style={S.summaryBox}>{c.summary}</div>}
      {c.analyses.length > 0 && (
        <div style={S.tableWrap}>
          <table style={S.table}>
            <thead>
              <tr>
                <th style={S.th}>Ticker</th>
                <th style={S.th}>Signal</th>
                <th style={S.th}>Trend</th>
                <th style={{...S.th, textAlign: "right"}}>Conf</th>
              </tr>
            </thead>
            <tbody>
              {c.analyses.map((a, i) => (
                <tr key={i} style={i % 2 === 0 ? S.trEven : S.trOdd}>
                  <td style={{...S.td, fontFamily: "ui-monospace, SFMono-Regular, monospace", fontWeight: 600}}>{a.ticker}</td>
                  <td style={S.td}>
                    <span style={{
                      ...S.signalChip,
                      color: signalColor(a.signal),
                      borderColor: `${signalColor(a.signal)}40`,
                      background: `${signalColor(a.signal)}15`,
                    }}>
                      {a.signal || "—"}
                    </span>
                  </td>
                  <td style={{...S.td, color: "#cbd5e1"}}>{a.trend || "—"}</td>
                  <td style={{...S.td, textAlign: "right", color: "#cbd5e1"}}>
                    {a.confidence != null ? `${a.confidence}%` : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function ReasoningCard({ c }) {
  const tone = "#10b981";
  return (
    <div style={S.card(tone)}>
      <CardHeader
        icon={Brain} iconColor={tone} title="Decision — Ragionamento" ts={c.ts}
        badges={<span style={S.engineBadge}>{c.model}</span>}
      />
      <div style={S.reasoningBox}>
        {c.reasoning_text || <em style={{ color: "#64748b" }}>(testo vuoto)</em>}
      </div>
    </div>
  );
}

function DecisionCard({ c }) {
  const isTrade = c.decision_label !== "NO TRADE";
  const isBuy = c.action === "BUY";
  const tone = !isTrade ? "#64748b" : isBuy ? "#10b981" : "#ef4444";
  const isCrypto = c.decision_label === "CRYPTO TRADE";

  return (
    <div style={{ ...S.card(tone), background: "#0d1424" }}>
      <CardHeader
        icon={Target} iconColor={tone}
        title={`Decision — ${c.decision_label}`}
        ts={c.ts}
        badges={isCrypto ? <span style={S.cryptoBadge}>R1</span> : null}
      />
      {isTrade ? (
        <div style={S.tradeBlock}>
          <div style={S.tradeMain}>
            <span style={{...S.actionTag, background: tone, color: "#fff"}}>
              {c.action}
            </span>
            <span style={S.tradeQty}>{c.qty}</span>
            <span style={S.tradeTicker}>{c.ticker}</span>
            <span style={S.tradeAt}>@</span>
            <span style={S.tradePrice}>${c.price?.toFixed(2)}</span>
          </div>
          <div style={S.tradeMeta}>
            <MetaItem label="Confidence" value={`${c.confidence}%`} />
            {c.stop_loss && <MetaItem label="Stop Loss" value={`$${c.stop_loss}`} color="#ef4444" />}
            {c.take_profit && <MetaItem label="Take Profit" value={`$${c.take_profit}`} color="#10b981" />}
          </div>
        </div>
      ) : (
        <div style={S.body}>
          <span style={S.label}>Motivazione</span>
          <span style={S.value}>{c.reasoning || "(non specificato)"}</span>
        </div>
      )}
    </div>
  );
}

function DecisionCryptoCard({ c }) {
  const tone = "#f472b6";
  // Componi il body: usa thesis/action_plan/primary_risk se disponibili,
  // altrimenti fallback a final_text. Prima la card mostrava solo
  // final_text troncato a 500 char (tabella markdown a meta') o
  // "(testo vuoto)" quando R1 terminava con solo tool calls.
  const hasRich = c.thesis || c.action_plan || c.primary_risk;
  return (
    <div style={{ ...S.card(tone), background: "#15101a" }}>
      <CardHeader
        icon={Target} iconColor={tone} title="Decision Crypto" ts={c.ts}
        badges={
          <>
            <span style={S.cryptoBadge}>R1</span>
            <span style={S.tradesBadge}>{c.trades_executed} trade</span>
          </>
        }
      />
      {hasRich ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 10,
                       paddingTop: 4 }}>
          {c.thesis && (
            <div>
              <div style={{ ...S.label, color: tone, marginBottom: 4 }}>💡 Tesi</div>
              <div style={S.reasoningBox}>{c.thesis}</div>
            </div>
          )}
          {c.action_plan && (
            <div>
              <div style={{ ...S.label, color: tone, marginBottom: 4 }}>📋 Piano d'azione</div>
              <div style={S.reasoningBox}>{c.action_plan}</div>
            </div>
          )}
          {c.primary_risk && (
            <div>
              <div style={{ ...S.label, color: "#ef4444", marginBottom: 4 }}>⚠️ Rischio principale</div>
              <div style={S.reasoningBox}>{c.primary_risk}</div>
            </div>
          )}
          {c.final_text && (
            <div>
              <div style={{ ...S.label, marginBottom: 4 }}>✅ Conclusione</div>
              <div style={S.reasoningBox}>{c.final_text}</div>
            </div>
          )}
        </div>
      ) : (
        <div style={S.reasoningBox}>
          {c.final_text || (
            <em style={{ color: "#64748b" }}>
              R1 ha completato il run senza output testuale finale (solo tool calls).
              Vedi card "Decision — Ragionamento" sopra per la tesi completa.
            </em>
          )}
        </div>
      )}
    </div>
  );
}

function TradeFailedCard({ c }) {
  // Card rossa per DECISION_CRYPTO_TRADE_FAILED: senza questa, l'utente
  // vedeva il trade fallito silenziosamente (no card, solo absent log).
  const tone = "#ef4444";
  return (
    <div style={{ ...S.card(tone), background: "#1f0e0e" }}>
      <CardHeader
        icon={AlertCircle} iconColor={tone}
        title={<span style={{ color: tone }}>Trade RIFIUTATO</span>}
        ts={c.ts}
        badges={<span style={{ ...S.cryptoBadge, color: tone,
                                 borderColor: tone, background: "rgba(239,68,68,0.10)" }}>
          CRYPTO</span>}
      />
      <div style={S.tradeBlock}>
        <div style={S.tradeMain}>
          <span style={{ ...S.actionTag, background: tone, color: "#fff" }}>
            {c.action}
          </span>
          <span style={S.tradeQty}>{c.qty}</span>
          <span style={S.tradeTicker}>{c.ticker}</span>
          {c.price && (
            <>
              <span style={S.tradeAt}>@</span>
              <span style={S.tradePrice}>${Number(c.price).toFixed(2)}</span>
            </>
          )}
        </div>
        <div style={{ marginTop: 8 }}>
          <span style={{ ...S.label, color: tone }}>Motivo rifiuto</span>
          <span style={S.value}>{c.reason}</span>
        </div>
      </div>
    </div>
  );
}

function ErrorCard({ c }) {
  const tone = "#ef4444";
  return (
    <div style={{ ...S.card(tone), background: "#1f0e0e" }}>
      <CardHeader
        icon={AlertCircle} iconColor={tone}
        title={<span style={{ color: tone }}>Decision ERROR</span>}
        ts={c.ts}
      />
      <div style={S.body}>
        <span style={{...S.label, color: tone}}>{c.error_type}</span>
        <span style={S.value}>{c.error_message}</span>
      </div>
    </div>
  );
}

// ─── Helper components ──────────────────────────────────────────────────

function Stat({ value, label, big, muted }) {
  return (
    <div style={S.stat}>
      <div style={{
        fontSize: big ? 22 : 16,
        fontWeight: 700,
        color: muted ? "#64748b" : "#e2e8f0",
        lineHeight: 1.1,
      }}>{value}</div>
      <div style={{ fontSize: 11, color: "#64748b", marginTop: 2 }}>{label}</div>
    </div>
  );
}

function MetaItem({ label, value, color }) {
  return (
    <div style={S.metaItem}>
      <div style={S.metaLabel}>{label}</div>
      <div style={{ ...S.metaValue, color: color || "#e2e8f0" }}>{value}</div>
    </div>
  );
}

const signalColor = (s) => {
  if (s === "BUY") return "#10b981";
  if (s === "SELL") return "#ef4444";
  if (s === "HOLD") return "#f59e0b";
  return "#94a3b8";
};

// ─── Animations ─────────────────────────────────────────────────────────
// Wrapper .agent-card applica l'hover-lift al primo figlio (la card vera),
// che e' il div con S.card(tone) e che possiede box-shadow + border.
const KEYFRAMES = `
@keyframes agentCardIn {
  from { opacity: 0; transform: translateY(6px); }
  to { opacity: 1; transform: translateY(0); }
}
.agent-card { cursor: default; }
.agent-card > div {
  transition: transform 0.2s ease, box-shadow 0.2s ease, border-color 0.2s ease;
}
.agent-card:hover > div {
  transform: translateY(-1px);
  box-shadow: 0 6px 18px rgba(0,0,0,0.45);
  border-color: #2d3748;
}
`;

// ─── Styles ─────────────────────────────────────────────────────────────

const S = {
  list: { display: "flex", flexDirection: "column", gap: 12 },

  // Card factory: prende il colore del tipo, lo applica al border-left
  card: (toneColor) => ({
    background: "#111827",
    border: "1px solid #1f2937",
    borderLeft: `3px solid ${toneColor}`,
    padding: "14px 18px",
    borderRadius: 10,
    boxShadow: "0 1px 3px rgba(0,0,0,0.25)",
    transition: "transform 0.2s ease, box-shadow 0.2s ease, border-color 0.2s ease",
  }),

  // Header
  head: {
    display: "flex", alignItems: "center", gap: 12, marginBottom: 10,
  },
  iconWrap: {
    width: 28, height: 28, borderRadius: 7,
    display: "flex", alignItems: "center", justifyContent: "center",
    flexShrink: 0,
  },
  title: { fontSize: 14, fontWeight: 600, color: "#f1f5f9", letterSpacing: "0.01em" },
  subtitle: { fontSize: 11, color: "#64748b", marginTop: 2 },
  timestamp: { color: "#64748b", fontSize: 11, fontFamily: "ui-monospace, SFMono-Regular, monospace", flexShrink: 0 },

  // Body — label/value strutturate
  body: { fontSize: 13, lineHeight: 1.6, display: "flex", flexDirection: "column", gap: 4 },
  label: {
    fontSize: 10, fontWeight: 600, textTransform: "uppercase",
    letterSpacing: "0.06em", color: "#64748b",
  },
  labelInline: {
    fontSize: 10, fontWeight: 600, textTransform: "uppercase",
    letterSpacing: "0.06em", color: "#64748b", marginRight: 4,
  },
  value: { color: "#e2e8f0" },

  // Badges
  urgencyBadge: {
    padding: "2px 8px", borderRadius: 4,
    background: "linear-gradient(90deg, #92400e, #b45309)",
    color: "#fed7aa", fontSize: 10, fontWeight: 700, letterSpacing: "0.04em",
  },
  engineBadge: {
    padding: "2px 7px", borderRadius: 4,
    background: "#1e293b", color: "#94a3b8",
    fontSize: 10, fontFamily: "ui-monospace, SFMono-Regular, monospace",
    border: "1px solid #334155",
  },
  cryptoBadge: {
    padding: "2px 7px", borderRadius: 4,
    background: "#831843", color: "#fce7f3",
    fontSize: 10, fontWeight: 700, letterSpacing: "0.04em",
  },
  tradesBadge: {
    padding: "2px 7px", borderRadius: 4,
    background: "#1e293b", color: "#94a3b8",
    fontSize: 10, fontWeight: 600,
  },

  // Scout stats
  statsRow: {
    display: "flex", gap: 18, marginTop: 4, marginBottom: 6,
    paddingLeft: 4,
  },
  stat: { display: "flex", flexDirection: "column", alignItems: "flex-start" },

  // Tickers / sources
  tickerRow: { display: "flex", gap: 6, flexWrap: "wrap", marginTop: 8, alignItems: "center" },
  tickerPill: {
    padding: "3px 9px", borderRadius: 999,
    background: "#1e293b", color: "#cbd5e1",
    fontSize: 11, fontFamily: "ui-monospace, SFMono-Regular, monospace",
    fontWeight: 600,
    border: "1px solid #334155",
  },
  sourceTag: {
    padding: "2px 7px", borderRadius: 3,
    background: "#0f172a", color: "#94a3b8",
    fontSize: 10, fontFamily: "ui-monospace, SFMono-Regular, monospace",
    border: "1px solid #1f2937",
  },

  // Tech table
  summaryBox: {
    fontSize: 13, color: "#e2e8f0", lineHeight: 1.55,
    padding: "8px 12px", background: "#0f172a", borderRadius: 6,
    marginBottom: 8,
  },
  tableWrap: { borderRadius: 6, overflow: "hidden", border: "1px solid #1f2937" },
  table: { width: "100%", borderCollapse: "collapse", fontSize: 12 },
  th: {
    padding: "6px 10px", textAlign: "left", fontSize: 10,
    fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.06em",
    color: "#64748b", background: "#0f172a",
    borderBottom: "1px solid #1f2937",
  },
  td: { padding: "8px 10px", color: "#e2e8f0", borderBottom: "1px solid #1a2030" },
  trEven: { background: "transparent" },
  trOdd: { background: "rgba(15,23,42,0.4)" },
  signalChip: {
    display: "inline-block", padding: "2px 8px", borderRadius: 4,
    fontSize: 11, fontWeight: 700, letterSpacing: "0.04em",
    border: "1px solid",
  },

  // Reasoning text full
  reasoningBox: {
    fontSize: 13, lineHeight: 1.7, color: "#e2e8f0",
    whiteSpace: "pre-wrap", wordBreak: "break-word",
    padding: "12px 14px",
    background: "#0a1018",
    borderRadius: 6,
    border: "1px solid #1a2030",
  },

  // Decision trade
  tradeBlock: { display: "flex", flexDirection: "column", gap: 10 },
  tradeMain: {
    display: "flex", alignItems: "baseline", gap: 8, flexWrap: "wrap",
    padding: "10px 14px", background: "#0a1018", borderRadius: 8,
    border: "1px solid #1a2030",
  },
  actionTag: {
    padding: "3px 10px", borderRadius: 4,
    fontSize: 11, fontWeight: 700, letterSpacing: "0.05em",
    alignSelf: "center",
  },
  tradeQty: { fontSize: 18, fontWeight: 700, color: "#cbd5e1" },
  tradeTicker: {
    fontSize: 18, fontWeight: 700, color: "#f1f5f9",
    fontFamily: "ui-monospace, SFMono-Regular, monospace",
    letterSpacing: "0.02em",
  },
  tradeAt: { fontSize: 14, color: "#64748b" },
  tradePrice: { fontSize: 18, fontWeight: 600, color: "#e2e8f0" },
  tradeMeta: {
    display: "flex", gap: 16, paddingLeft: 4,
  },
  metaItem: { display: "flex", flexDirection: "column" },
  metaLabel: {
    fontSize: 10, fontWeight: 600, textTransform: "uppercase",
    letterSpacing: "0.06em", color: "#64748b", marginBottom: 2,
  },
  metaValue: { fontSize: 13, fontWeight: 600 },

  // Empty
  empty: {
    color: "#64748b", padding: "32px 24px", textAlign: "center", fontSize: 13,
    background: "#0f172a", borderRadius: 8, border: "1px dashed #1f2937",
  },
};
