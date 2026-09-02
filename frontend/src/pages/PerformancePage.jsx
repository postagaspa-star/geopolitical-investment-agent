import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  ResponsiveContainer,
  AreaChart, Area, BarChart, Bar, LineChart, Line,
  XAxis, YAxis, CartesianGrid, Tooltip, Legend, ReferenceLine, Cell,
} from 'recharts';
import { Download, FileSpreadsheet, AlertTriangle } from 'lucide-react';

// Pagina "Performance": il rendiconto del portafoglio live.
//
// Tutti i numeri arrivano da UN endpoint (/api/live/performance) e i CSV
// dallo stesso calcolo (/api/live/performance/export). Non c'e' matematica
// dei soldi in questo file: e' una scelta, non una dimenticanza. Analytics
// ricalcola i round-trip nel browser e teneva la commissione scritta a mano,
// con il rischio che schermo e backend raccontassero due storie diverse. Qui
// il browser disegna e basta.
//
// Sui grafici: mai due assi Y sullo stesso grafico. Valore e drawdown sono
// due misure di scala diversa e stanno in due grafici impilati che
// condividono l'asse dei tempi — si leggono insieme senza che una scala
// venga schiacciata sull'altra.

const API = window.location.origin;

// Colori: gli stessi del resto della dashboard.
//   blu    = il tuo portafoglio
//   grigio = il benchmark (linea di riferimento, tratteggiata)
//   verde/rosso = esito (guadagno/perdita), mai usati come "serie 3 e 4"
const C = {
  portfolio: '#3b82f6',
  benchmark: '#94a3b8',
  positive: '#10b981',
  negative: '#ef4444',
  fee: '#f59e0b',
  ink: '#f1f5f9',
  inkSoft: '#cbd5e1',
  inkMuted: '#64748b',
  grid: '#1e293b',
  surface: '#111827',
  surfaceDeep: '#0f172a',
  border: '#1f2937',
};

const PERIOD_OPTIONS = [
  { key: 'all', label: 'Tutto il periodo' },
  { key: '365d', label: 'Ultimo anno' },
  { key: '90d', label: 'Ultimi 90 giorni' },
  { key: '30d', label: 'Ultimi 30 giorni' },
  { key: '7d', label: 'Ultimi 7 giorni' },
];

// I file scaricabili. L'ordine e' quello di utilita': la richiesta piu'
// frequente ("dammi tutte le transazioni con le commissioni") sta per prima.
const DOWNLOADS = [
  {
    dataset: 'transazioni',
    label: 'Tutte le transazioni',
    hint: 'Una riga per ogni acquisto e vendita, con la commissione pagata e il movimento di cassa.',
    primary: true,
  },
  {
    dataset: 'operazioni',
    label: 'Operazioni chiuse',
    hint: 'Apertura abbinata alla sua chiusura, con guadagno o perdita al netto delle commissioni.',
  },
  {
    dataset: 'sintesi',
    label: 'Sintesi completa',
    hint: 'Tutte le metriche della pagina in due colonne: metrica e valore.',
  },
  {
    dataset: 'curva_valore',
    label: 'Curva del valore',
    hint: 'Il valore del portafoglio giorno per giorno e quanto eri sotto il massimo.',
  },
  {
    dataset: 'mensile',
    label: 'Rendimento mensile',
    hint: 'Quanto ha reso ogni mese, da chiusura a chiusura.',
  },
  {
    dataset: 'per_strumento',
    label: 'Dettaglio per titolo',
    hint: 'Guadagno, commissioni e percentuale di operazioni vincenti su ogni ticker.',
  },
  {
    dataset: 'benchmark',
    label: 'Confronto con S&P 500',
    hint: 'Le due curve affiancate, entrambe partite da 100.',
  },
];

// ── Formattazione ───────────────────────────────────────────────────────────

const usd = (n, digits = 2) =>
  n == null || Number.isNaN(Number(n))
    ? '—'
    : new Intl.NumberFormat('en-US', {
        style: 'currency', currency: 'USD',
        minimumFractionDigits: digits, maximumFractionDigits: digits,
      }).format(Number(n));

const pct = (n, digits = 2) =>
  n == null || Number.isNaN(Number(n))
    ? '—'
    : `${Number(n) > 0 ? '+' : ''}${Number(n).toFixed(digits)}%`;

const num = (n, digits = 2) =>
  n == null || Number.isNaN(Number(n)) ? '—' : Number(n).toFixed(digits);

const int = (n) => (n == null ? '—' : new Intl.NumberFormat('it-IT').format(n));

const signColor = (n) =>
  n == null ? C.inkMuted : Number(n) >= 0 ? C.positive : C.negative;

// ── Pezzi di interfaccia ────────────────────────────────────────────────────

function Card({ title, hint, right, children, style }) {
  return (
    <div style={{
      background: C.surface, border: `1px solid ${C.border}`,
      borderRadius: 10, padding: '1rem 1.1rem', ...style,
    }}>
      {(title || right) && (
        <div style={{
          display: 'flex', justifyContent: 'space-between',
          alignItems: 'flex-start', gap: 12, marginBottom: hint ? 6 : 12,
        }}>
          <span style={{ fontSize: '0.95rem', fontWeight: 700, color: C.ink }}>
            {title}
          </span>
          {right}
        </div>
      )}
      {hint && (
        <p style={{
          fontSize: '0.72rem', color: C.inkMuted, margin: '0 0 12px 0',
          lineHeight: 1.5,
        }}>{hint}</p>
      )}
      {children}
    </div>
  );
}

function Kpi({ label, value, sub, color }) {
  return (
    <div style={{
      background: C.surface, border: `1px solid ${C.border}`,
      borderRadius: 10, padding: '0.85rem 1rem',
    }}>
      <div style={{
        fontSize: '0.68rem', color: C.inkMuted, textTransform: 'uppercase',
        letterSpacing: '0.04em', marginBottom: 6,
      }}>{label}</div>
      <div style={{
        fontSize: '1.25rem', fontWeight: 700, color: color || C.ink,
        lineHeight: 1.15,
      }}>{value}</div>
      {sub && (
        <div style={{ fontSize: '0.7rem', color: C.inkMuted, marginTop: 4 }}>
          {sub}
        </div>
      )}
    </div>
  );
}

// Riga metrica → valore, con la spiegazione in parole semplici accanto.
// Le metriche di rischio hanno nomi da addetti ai lavori: senza una frase
// che le traduce, la tabella e' decorazione.
function MetricRow({ label, value, color, meaning }) {
  return (
    <div style={{
      display: 'grid', gridTemplateColumns: 'minmax(160px, 1.1fr) auto',
      gap: 10, padding: '0.5rem 0',
      borderBottom: `1px solid ${C.grid}`, alignItems: 'baseline',
    }}>
      <div>
        <div style={{ fontSize: '0.82rem', color: C.inkSoft }}>{label}</div>
        {meaning && (
          <div style={{ fontSize: '0.68rem', color: C.inkMuted, marginTop: 2,
                        lineHeight: 1.45 }}>{meaning}</div>
        )}
      </div>
      <div style={{ fontSize: '0.88rem', fontWeight: 700,
                    color: color || C.ink, textAlign: 'right' }}>
        {value}
      </div>
    </div>
  );
}

const tooltipStyle = {
  background: C.surfaceDeep, border: `1px solid ${C.grid}`,
  borderRadius: 8, fontSize: 12, color: C.ink,
};

function EmptyChart({ message }) {
  return (
    <div style={{
      height: 200, display: 'flex', alignItems: 'center',
      justifyContent: 'center', color: C.inkMuted, fontSize: '0.8rem',
      textAlign: 'center', padding: '0 1rem', lineHeight: 1.5,
    }}>{message}</div>
  );
}

// ── Pagina ──────────────────────────────────────────────────────────────────

export default function PerformancePage() {
  const [report, setReport] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [period, setPeriod] = useState('all');
  const [separator, setSeparator] = useState(',');
  const [downloading, setDownloading] = useState(null);
  const [downloadError, setDownloadError] = useState(null);

  useEffect(() => {
    let annullato = false;
    const carica = async () => {
      setLoading(true);
      setError(null);
      try {
        const res = await fetch(`${API}/api/live/performance?period=${period}`);
        if (!res.ok) throw new Error(`il server ha risposto ${res.status}`);
        const data = await res.json();
        if (data?.error) throw new Error(data.error);
        if (!annullato) setReport(data);
      } catch (err) {
        if (!annullato) setError(err.message || 'errore nel caricamento');
      } finally {
        if (!annullato) setLoading(false);
      }
    };
    carica();
    return () => { annullato = true; };
  }, [period]);

  // Il download passa da fetch (non da un link diretto) per due motivi:
  // l'header con l'admin token viene aggiunto dall'interceptor globale, e un
  // errore del server si vede come messaggio invece di finire salvato dentro
  // un file .csv che poi non si apre.
  const scarica = useCallback(async (dataset) => {
    setDownloading(dataset);
    setDownloadError(null);
    try {
      const url = `${API}/api/live/performance/export`
        + `?dataset=${encodeURIComponent(dataset)}`
        + `&period=${encodeURIComponent(period)}`
        + `&sep=${encodeURIComponent(separator)}`;
      const res = await fetch(url);
      if (!res.ok) {
        let dettaglio = `il server ha risposto ${res.status}`;
        try {
          const body = await res.json();
          if (body?.error) dettaglio = body.error;
        } catch { /* la risposta non era JSON */ }
        throw new Error(dettaglio);
      }
      const blob = await res.blob();
      const nome = (res.headers.get('content-disposition') || '')
        .match(/filename="?([^";]+)"?/)?.[1] || `${dataset}.csv`;
      const href = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = href;
      a.download = nome;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(href);
    } catch (err) {
      setDownloadError(`Download "${dataset}" non riuscito: ${err.message}`);
    } finally {
      setDownloading(null);
    }
  }, [period, separator]);

  const equity = useMemo(
    () => (report?.curva_valore || []).map((p) => ({
      data: p.date, valore: p.value, drawdown: p.drawdown_pct,
    })),
    [report],
  );

  // Le due curve del benchmark hanno lunghezze diverse (gli snapshot del
  // portafoglio sono molti piu' delle chiusure giornaliere dell'indice). Le
  // si riporta su un asse comune: la percentuale di periodo trascorso.
  const confronto = useMemo(() => {
    const b = report?.benchmark;
    if (!b?.available) return [];
    const port = b.portfolio_series_norm || [];
    const spy = b.sp500_series_norm || [];
    if (!port.length && !spy.length) return [];
    const punti = 80;
    const valoreA = (serie, frazione) => {
      if (!serie.length) return null;
      if (serie.length === 1) return serie[0];
      const pos = frazione * (serie.length - 1);
      const basso = Math.floor(pos);
      const alto = Math.ceil(pos);
      const peso = pos - basso;
      return Number((serie[basso] * (1 - peso) + serie[alto] * peso).toFixed(2));
    };
    return Array.from({ length: punti + 1 }, (_, i) => {
      const frazione = i / punti;
      return {
        avanzamento: Math.round(frazione * 100),
        Portafoglio: valoreA(port, frazione),
        'S&P 500': valoreA(spy, frazione),
      };
    });
  }, [report]);

  const mensile = useMemo(
    () => (report?.mensile || []).map((m) => ({
      mese: m.mese,
      rendimento: m.rendimento_pct,
      parziale: m.parziale,
    })),
    [report],
  );

  const commissioniMese = useMemo(
    () => (report?.commissioni?.commissioni_per_mese || []).map((m) => ({
      mese: m.mese, commissioni: m.commissioni_usd,
    })),
    [report],
  );

  // available=true non basta: se manca la quotazione dell'indice non c'e'
  // niente da confrontare, e mostrare una curva sola sotto il titolo
  // "confronto" e' peggio che non mostrare niente.
  const bench = report?.benchmark;
  const confrontoValido = Boolean(
    bench?.available
    && bench.sp500_return_pct != null
    && confronto.some((p) => p['S&P 500'] != null),
  );

  if (loading) {
    return (
      <div className="main-content">
        <div className="loading-state">Preparo il rendiconto...</div>
      </div>
    );
  }

  if (error || !report) {
    return (
      <div className="main-content">
        <div className="error-state">
          Non riesco a caricare il rendiconto: {error || 'nessun dato'}
        </div>
      </div>
    );
  }

  const s = report.sintesi || {};
  const nav = report.metriche_nav || {};
  const st = report.statistiche_operazioni || {};
  const fees = report.commissioni || {};
  const troncato = report.troncato || {};

  const selectStyle = {
    background: C.surfaceDeep, color: C.ink,
    border: `1px solid ${C.border}`, borderRadius: 8,
    padding: '0.4rem 0.6rem', fontSize: '0.78rem',
  };

  return (
    <div className="main-content">
      {/* Intestazione */}
      <div style={{
        marginBottom: '1.25rem', display: 'flex', gap: 12,
        justifyContent: 'space-between', alignItems: 'flex-end',
        flexWrap: 'wrap',
      }}>
        <div>
          <h2 style={{ fontSize: '1.4rem', fontWeight: 700, color: C.ink,
                       margin: '0 0 0.25rem 0' }}>
            Performance
          </h2>
          <p style={{ fontSize: '0.8rem', color: C.inkMuted, margin: 0,
                      maxWidth: 620, lineHeight: 1.5 }}>
            Il rendiconto completo del portafoglio: quanto ha reso, quanto
            rischio e' costato, quanto se n'e' andato in commissioni e come si
            confronta con l'S&amp;P 500. Ogni tabella si puo' scaricare in CSV.
          </p>
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center',
                      flexWrap: 'wrap' }}>
          <label style={{ fontSize: '0.72rem', color: C.inkMuted }}>
            Periodo{' '}
            <select value={period} onChange={(e) => setPeriod(e.target.value)}
                    style={selectStyle}>
              {PERIOD_OPTIONS.map((o) => (
                <option key={o.key} value={o.key}>{o.label}</option>
              ))}
            </select>
          </label>
          <label style={{ fontSize: '0.72rem', color: C.inkMuted }}
                 title="Excel in italiano si aspetta il punto e virgola: con la virgola mette tutto in una colonna sola. Fogli Google e pandas preferiscono la virgola.">
            Separatore CSV{' '}
            <select value={separator}
                    onChange={(e) => setSeparator(e.target.value)}
                    style={selectStyle}>
              <option value=",">virgola (standard)</option>
              <option value=";">punto e virgola (Excel IT)</option>
            </select>
          </label>
        </div>
      </div>

      {/* Download — e' il motivo principale per cui si apre questa pagina,
          quindi sta in cima e non in fondo. */}
      <Card
        title={<span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <FileSpreadsheet size={16} /> Scarica i dati
        </span>}
        hint={`Ogni file copre il periodo selezionato (${
          PERIOD_OPTIONS.find((o) => o.key === period)?.label.toLowerCase()
        }) e usa la commissione configurata, ${num(fees.commissione_bps, 1)} bps `
          + `(${num(fees.commissione_pct_per_operazione, 3)}% per operazione).`}
        style={{ marginBottom: '1rem' }}>
        <div style={{
          display: 'grid', gap: 10,
          gridTemplateColumns: 'repeat(auto-fill, minmax(230px, 1fr))',
        }}>
          {DOWNLOADS.map((d) => (
            <button
              key={d.dataset}
              onClick={() => scarica(d.dataset)}
              disabled={downloading === d.dataset}
              title={d.hint}
              style={{
                textAlign: 'left', cursor: downloading ? 'wait' : 'pointer',
                background: d.primary ? '#1d4ed8' : C.surfaceDeep,
                border: `1px solid ${d.primary ? '#2563eb' : C.border}`,
                borderRadius: 8, padding: '0.7rem 0.85rem', color: C.ink,
                opacity: downloading === d.dataset ? 0.6 : 1,
              }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8,
                            fontSize: '0.84rem', fontWeight: 700 }}>
                <Download size={14} />
                {downloading === d.dataset ? 'Preparo il file...' : d.label}
              </div>
              <div style={{ fontSize: '0.7rem', color: d.primary ? '#bfdbfe' : C.inkMuted,
                            marginTop: 4, lineHeight: 1.45 }}>
                {d.hint}
              </div>
            </button>
          ))}
        </div>
        {downloadError && (
          <div style={{
            marginTop: 10, padding: '8px 12px', borderRadius: 8,
            background: '#7f1d1d33', border: '1px solid #b91c1c',
            color: '#fecaca', fontSize: '0.76rem',
          }}>{downloadError}</div>
        )}
      </Card>

      {/* Avvertenze: se un numero non e' affidabile va detto qui sopra, non
          in una nota a pie' di pagina che nessuno legge. */}
      {(report.avvertenze || []).length > 0 && (
        <Card style={{ marginBottom: '1rem', borderColor: '#f59e0b55',
                       background: '#78350f22' }}>
          <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
            <AlertTriangle size={16} color={C.fee} style={{ flexShrink: 0, marginTop: 2 }} />
            <div>
              <div style={{ fontSize: '0.84rem', fontWeight: 700, color: C.fee,
                            marginBottom: 6 }}>
                Da tenere presente leggendo questi numeri
              </div>
              <ul style={{ margin: 0, paddingLeft: '1.1rem', color: C.inkSoft,
                           fontSize: '0.76rem', lineHeight: 1.6 }}>
                {report.avvertenze.map((w, i) => <li key={i}>{w}</li>)}
              </ul>
            </div>
          </div>
        </Card>
      )}

      {/* Numeri principali */}
      <div style={{
        display: 'grid', gap: 10, marginBottom: '1rem',
        gridTemplateColumns: 'repeat(auto-fill, minmax(190px, 1fr))',
      }}>
        <Kpi label="Valore attuale" value={usd(s.valore_attuale_usd)}
             sub={`di cui liquidi ${usd(s.liquidita_usd)}`} />
        <Kpi label="Guadagno totale" value={usd(s.pnl_totale_usd)}
             sub={pct(s.pnl_totale_pct)} color={signColor(s.pnl_totale_usd)} />
        <Kpi label="Realizzato (chiuso)" value={usd(s.pnl_realizzato_netto_usd)}
             sub="al netto delle commissioni"
             color={signColor(s.pnl_realizzato_netto_usd)} />
        <Kpi label="Non realizzato (aperto)" value={usd(s.pnl_non_realizzato_usd)}
             sub={`${int(s.posizioni_aperte)} posizioni aperte`}
             color={signColor(s.pnl_non_realizzato_usd)} />
        <Kpi label="Commissioni pagate" value={usd(s.commissioni_totali_usd)}
             sub={fees.peso_su_profitto_lordo_pct != null
               ? `${num(fees.peso_su_profitto_lordo_pct, 1)}% del guadagno lordo`
               : `su ${int(s.transazioni_totali)} transazioni`}
             color={C.fee} />
        <Kpi label="Operazioni chiuse" value={int(st.operazioni_chiuse)}
             sub={st.win_rate_pct != null
               ? `${num(st.win_rate_pct, 1)}% vincenti`
               : 'nessuna ancora'} />
        <Kpi label="Perdita massima" value={pct(nav.max_drawdown_pct)}
             sub={nav.max_drawdown_data
               ? `toccata il ${nav.max_drawdown_data}`
               : 'dal massimo raggiunto'}
             color={C.negative} />
        <Kpi label="Alpha vs S&P 500"
             value={confrontoValido ? pct(bench.alpha_pct) : '—'}
             sub={confrontoValido
               ? `tu ${pct(bench.portfolio_return_pct)} · indice ${pct(bench.sp500_return_pct)}`
               : "quotazione dell'indice non disponibile"}
             color={confrontoValido ? signColor(bench.alpha_pct) : C.inkMuted} />
      </div>

      {/* Andamento — grafico 1: solo il valore. */}
      <Card
        title="Andamento del portafoglio"
        hint="Il valore totale (posizioni + liquidita') alla chiusura di ogni giorno. E' la linea che conta: tutto il resto della pagina spiega perche' ha questa forma."
        style={{ marginBottom: '1rem' }}>
        {equity.length < 2 ? (
          <EmptyChart message="Servono almeno due giorni di storico per disegnare l'andamento." />
        ) : (
          <ResponsiveContainer width="100%" height={280}>
            <AreaChart data={equity} margin={{ top: 5, right: 12, left: 4, bottom: 0 }}>
              <defs>
                <linearGradient id="perfValore" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={C.portfolio} stopOpacity={0.35} />
                  <stop offset="100%" stopColor={C.portfolio} stopOpacity={0.02} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke={C.grid} vertical={false} />
              <XAxis dataKey="data" stroke={C.inkMuted} fontSize={11}
                     minTickGap={40} tickMargin={6} />
              <YAxis stroke={C.inkMuted} fontSize={11} width={70}
                     domain={['auto', 'auto']}
                     tickFormatter={(v) => `$${Math.round(v / 1000)}k`} />
              <Tooltip contentStyle={tooltipStyle}
                       formatter={(v) => [usd(v), 'Valore']}
                       labelFormatter={(l) => `Giorno ${l}`} />
              <Area type="monotone" dataKey="valore" stroke={C.portfolio}
                    strokeWidth={2} fill="url(#perfValore)" dot={false}
                    name="Valore" />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </Card>

      {/* Andamento — grafico 2: il drawdown, con lo stesso asse dei tempi.
          Due assi Y sullo stesso grafico schiacciano una delle due scale:
          meglio due grafici allineati. */}
      <Card
        title="Quanto eri sotto il massimo"
        hint="Zero significa che sei su un nuovo massimo. -10% significa che in quel giorno valevi il 10% in meno del tuo picco. E' il rischio che hai davvero corso, quello che il rendimento da solo non racconta."
        style={{ marginBottom: '1rem' }}>
        {equity.length < 2 ? (
          <EmptyChart message="Storico insufficiente." />
        ) : (
          <ResponsiveContainer width="100%" height={180}>
            <AreaChart data={equity} margin={{ top: 5, right: 12, left: 4, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke={C.grid} vertical={false} />
              <XAxis dataKey="data" stroke={C.inkMuted} fontSize={11}
                     minTickGap={40} tickMargin={6} />
              <YAxis stroke={C.inkMuted} fontSize={11} width={70}
                     domain={['auto', 0]} tickFormatter={(v) => `${v}%`} />
              <Tooltip contentStyle={tooltipStyle}
                       formatter={(v) => [`${Number(v).toFixed(2)}%`, 'Sotto il massimo']}
                       labelFormatter={(l) => `Giorno ${l}`} />
              <ReferenceLine y={0} stroke={C.inkMuted} strokeDasharray="2 4" />
              <Area type="monotone" dataKey="drawdown" stroke={C.negative}
                    strokeWidth={1.5} fill={C.negative} fillOpacity={0.18}
                    dot={false} name="Sotto il massimo" />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </Card>

      {/* Confronto con il benchmark: grafico a parte, come richiesto. */}
      <Card
        title="Confronto con l'S&P 500"
        hint="Le due curve partono entrambe da 100 lo stesso giorno, cosi' si confrontano a parita' di punto di partenza. L'asse orizzontale e' la percentuale di periodo trascorso, perche' le due serie hanno un numero di punti diverso. Battere l'indice in rendimento non basta: conta anche quanto rischio e' costato (la perdita massima qui sotto)."
        style={{ marginBottom: '1rem' }}>
        {!confrontoValido ? (
          <EmptyChart message={bench?.available
            ? "La quotazione dell'S&P 500 per questo periodo non e' arrivata dal fornitore dati. Senza l'indice non c'e' niente da confrontare: meglio dirlo che disegnare una curva sola sotto il titolo \"confronto\"."
            : "Confronto non disponibile: serve abbastanza storico del portafoglio e la quotazione dell'indice per lo stesso periodo."} />
        ) : (
          <>
            <ResponsiveContainer width="100%" height={280}>
              <LineChart data={confronto} margin={{ top: 5, right: 12, left: 4, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke={C.grid} vertical={false} />
                <XAxis dataKey="avanzamento" stroke={C.inkMuted} fontSize={11}
                       tickFormatter={(v) => `${v}%`} tickMargin={6}
                       minTickGap={45} />
                <YAxis stroke={C.inkMuted} fontSize={11} width={50}
                       domain={['auto', 'auto']} />
                <Tooltip contentStyle={tooltipStyle}
                         formatter={(v, n) => [v != null ? v.toFixed(2) : '—', n]}
                         labelFormatter={(l) => `${l}% del periodo trascorso`} />
                <Legend wrapperStyle={{ fontSize: 12 }} />
                <ReferenceLine y={100} stroke={C.inkMuted} strokeDasharray="2 4" />
                <Line type="monotone" dataKey="Portafoglio" stroke={C.portfolio}
                      strokeWidth={2} dot={false} />
                <Line type="monotone" dataKey="S&P 500" stroke={C.benchmark}
                      strokeWidth={2} strokeDasharray="5 4" dot={false} />
              </LineChart>
            </ResponsiveContainer>

            <div style={{
              display: 'grid', gap: 10, marginTop: 12,
              gridTemplateColumns: 'repeat(auto-fit, minmax(230px, 1fr))',
            }}>
              <MetricRow label="Rendimento tuo"
                         value={pct(bench.portfolio_return_pct)}
                         color={signColor(bench.portfolio_return_pct)} />
              <MetricRow label="Rendimento S&P 500"
                         value={pct(bench.sp500_return_pct)} />
              <MetricRow label="Differenza (alpha)"
                         value={bench.alpha_pct != null
                           ? `${bench.alpha_pct > 0 ? '+' : ''}${bench.alpha_pct} punti`
                           : '—'}
                         color={signColor(bench.alpha_pct)} />
              <MetricRow label="Perdita max tua"
                         value={pct(bench.portfolio_max_drawdown_pct)}
                         color={C.negative} />
              <MetricRow label="Perdita max S&P 500"
                         value={pct(bench.sp500_max_drawdown_pct)} />
            </div>

            {bench.verdict_detail && (
              <div style={{
                marginTop: 12, padding: '10px 12px', borderRadius: 8,
                background: `${C.surfaceDeep}`, border: `1px solid ${C.grid}`,
                fontSize: '0.78rem', color: C.inkSoft, lineHeight: 1.55,
              }}>
                {bench.verdict_detail}
              </div>
            )}
          </>
        )}
      </Card>

      {/* Rendimento mensile */}
      <Card
        title="Rendimento mese per mese"
        hint="Da chiusura a chiusura. Il primo mese e' quasi sempre parziale (il portafoglio non e' nato il primo del mese) ed e' segnalato nel CSV."
        style={{ marginBottom: '1rem' }}>
        {mensile.length === 0 ? (
          <EmptyChart message="Non c'e' ancora un mese intero di storico." />
        ) : (
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={mensile} margin={{ top: 5, right: 12, left: 4, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke={C.grid} vertical={false} />
              <XAxis dataKey="mese" stroke={C.inkMuted} fontSize={11} tickMargin={6} />
              <YAxis stroke={C.inkMuted} fontSize={11} width={50}
                     tickFormatter={(v) => `${v}%`} />
              <Tooltip contentStyle={tooltipStyle} cursor={{ fill: '#1e293b55' }}
                       formatter={(v) => [pct(v), 'Rendimento']} />
              <ReferenceLine y={0} stroke={C.inkMuted} />
              <Bar dataKey="rendimento" name="Rendimento" radius={[4, 4, 0, 0]}>
                {mensile.map((m, i) => (
                  <Cell key={i} fill={signColor(m.rendimento)}
                        fillOpacity={m.parziale ? 0.55 : 1} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        )}
      </Card>

      {/* Commissioni */}
      <Card
        title="Quanto costano le commissioni"
        hint={`Ogni acquisto e ogni vendita paga ${num(fees.commissione_bps, 1)} bps `
          + `(${num(fees.commissione_pct_per_operazione, 3)}% del valore). Sembra poco, `
          + `ma si paga due volte per ogni operazione e su ogni operazione: e' il costo `
          + `di fare tanto trading.`}
        style={{ marginBottom: '1rem' }}>
        <div style={{
          display: 'grid', gap: 16,
          gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))',
        }}>
          <div>
            <MetricRow label="Commissioni totali"
                       value={usd(fees.commissioni_totali_usd)} color={C.fee} />
            <MetricRow label="Transazioni" value={int(fees.numero_transazioni)} />
            <MetricRow label="Commissione media"
                       value={usd(fees.commissione_media_usd)} />
            <MetricRow label="Volume lordo negoziato"
                       value={usd(fees.volume_lordo_negoziato_usd, 0)}
                       meaning="La somma di tutto quello che e' passato di mano, acquisti e vendite." />
            <MetricRow label="Guadagno prima delle commissioni"
                       value={usd(fees.pnl_lordo_usd)}
                       color={signColor(fees.pnl_lordo_usd)} />
            <MetricRow label="Guadagno dopo le commissioni"
                       value={usd(fees.pnl_netto_usd)}
                       color={signColor(fees.pnl_netto_usd)} />
            <MetricRow label="Peso sul guadagno lordo"
                       value={fees.peso_su_profitto_lordo_pct != null
                         ? `${num(fees.peso_su_profitto_lordo_pct, 1)}%` : '—'}
                       color={C.fee}
                       meaning={fees.peso_su_profitto_lordo_pct != null
                         ? 'Di ogni dollaro guadagnato lordo, quanto se n\'e\' andato in commissioni.'
                         : 'Calcolabile solo con un guadagno lordo positivo.'} />
          </div>
          <div>
            {commissioniMese.length === 0 ? (
              <EmptyChart message="Nessuna commissione registrata nel periodo." />
            ) : (
              <ResponsiveContainer width="100%" height={220}>
                <BarChart data={commissioniMese}
                          margin={{ top: 5, right: 12, left: 4, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke={C.grid} vertical={false} />
                  <XAxis dataKey="mese" stroke={C.inkMuted} fontSize={11} tickMargin={6} />
                  <YAxis stroke={C.inkMuted} fontSize={11} width={55}
                         tickFormatter={(v) => `$${v}`} />
                  <Tooltip contentStyle={tooltipStyle} cursor={{ fill: '#1e293b55' }}
                           formatter={(v) => [usd(v), 'Commissioni']} />
                  <Bar dataKey="commissioni" name="Commissioni" fill={C.fee}
                       radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            )}
          </div>
        </div>
      </Card>

      {/* Rischio e statistiche, affiancate */}
      <div style={{
        display: 'grid', gap: '1rem', marginBottom: '1rem',
        gridTemplateColumns: 'repeat(auto-fit, minmax(330px, 1fr))',
      }}>
        <Card title="Rendimento e rischio"
              hint="Calcolati sulla curva del valore ridotta a un punto per giorno: e' l'unico modo di annualizzare correttamente. Dove trovi un trattino, lo storico non basta ancora per quella misura.">
          <MetricRow label="Rendimento totale"
                     value={pct(nav.rendimento_totale_pct)}
                     color={signColor(nav.rendimento_totale_pct)}
                     meaning="Dal primo all'ultimo giorno del periodo." />
          <MetricRow label="Rendimento annualizzato"
                     value={pct(nav.rendimento_annualizzato_pct)}
                     color={signColor(nav.rendimento_annualizzato_pct)}
                     meaning="Quanto renderebbe in un anno mantenendo questo ritmo." />
          <MetricRow label="Volatilita' annua"
                     value={nav.volatilita_annua_pct != null
                       ? `${num(nav.volatilita_annua_pct, 1)}%` : '—'}
                     meaning="Quanto oscilla il valore. Piu' e' alta, piu' il percorso e' movimentato." />
          <MetricRow label="Sharpe" value={num(nav.sharpe)}
                     meaning="Rendimento per unita' di oscillazione. Sopra 1 e' buono, sotto 0 hai corso rischio per niente." />
          <MetricRow label="Sortino" value={num(nav.sortino)}
                     meaning="Come lo Sharpe, ma conta solo le oscillazioni verso il basso: salire tanto non e' un difetto." />
          <MetricRow label="Calmar" value={num(nav.calmar)}
                     meaning="Rendimento annuo diviso la perdita massima. Quanto guadagni per ogni punto di crollo sopportato." />
          <MetricRow label="Perdita massima"
                     value={pct(nav.max_drawdown_pct)} color={C.negative}
                     meaning={nav.max_drawdown_data
                       ? `La caduta piu' profonda dal massimo raggiunto, toccata il ${nav.max_drawdown_data}.`
                       : "La caduta piu' profonda dal massimo raggiunto."} />
          <MetricRow label="Giorni in guadagno"
                     value={nav.giorni_positivi_pct != null
                       ? `${num(nav.giorni_positivi_pct, 1)}%` : '—'} />
          <MetricRow label="Miglior giorno" value={pct(nav.miglior_giorno_pct)}
                     color={C.positive} />
          <MetricRow label="Peggior giorno" value={pct(nav.peggior_giorno_pct)}
                     color={C.negative} />
          <MetricRow label="Giorni con dati"
                     value={int(nav.punti_giornalieri)}
                     meaning={nav.primo_giorno
                       ? `Dal ${nav.primo_giorno} al ${nav.ultimo_giorno}, un arco di ${int(nav.giorni_calendario)} giorni.`
                       : 'Nessuna rilevazione nel periodo.'} />
        </Card>

        <Card title="Come vanno le operazioni"
              hint="Solo le operazioni chiuse, con guadagno e perdita gia' al netto delle commissioni.">
          <MetricRow label="Operazioni chiuse" value={int(st.operazioni_chiuse)} />
          <MetricRow label="Vincenti / perdenti"
                     value={`${int(st.vincenti)} / ${int(st.perdenti)}`} />
          <MetricRow label="Percentuale vincenti"
                     value={st.win_rate_pct != null
                       ? `${num(st.win_rate_pct, 1)}%` : '—'} />
          <MetricRow label="Profit factor" value={num(st.profit_factor)}
                     color={st.profit_factor != null
                       ? (st.profit_factor >= 1 ? C.positive : C.negative)
                       : C.inkMuted}
                     meaning="Quanti dollari guadagnati per ogni dollaro perso. Sotto 1 significa che stai perdendo." />
          <MetricRow label="Guadagno medio"
                     value={usd(st.guadagno_medio_usd)} color={C.positive} />
          <MetricRow label="Perdita media"
                     value={usd(st.perdita_media_usd)} color={C.negative} />
          <MetricRow label="Rapporto guadagno/perdita"
                     value={num(st.rapporto_guadagno_perdita)}
                     meaning="Quanto e' grande una vincita media rispetto a una perdita media." />
          <MetricRow label="Aspettativa per operazione"
                     value={usd(st.aspettativa_per_operazione_usd)}
                     color={signColor(st.aspettativa_per_operazione_usd)}
                     meaning="Quanto ti aspetti di guadagnare, in media, ogni volta che apri una posizione." />
          <MetricRow label="Miglior operazione"
                     value={usd(st.miglior_operazione_usd)} color={C.positive} />
          <MetricRow label="Peggior operazione"
                     value={usd(st.peggior_operazione_usd)} color={C.negative} />
          <MetricRow label="Durata media"
                     value={st.durata_media_giorni != null
                       ? `${num(st.durata_media_giorni, 1)} giorni` : '—'} />
          <MetricRow label="Serie vincente / perdente"
                     value={`${int(st.serie_vincente_max)} / ${int(st.serie_perdente_max)}`}
                     meaning="Il massimo numero di operazioni consecutive andate bene e andate male." />
          <MetricRow label="Chiusure non decise dall'AI"
                     value={int(st.chiusure_manuali)}
                     meaning="Stop automatici, circuit breaker o chiusure manuali. Contano nel P&L, ma non sono merito o colpa del modello." />
        </Card>
      </div>

      {/* Dettaglio per titolo */}
      <Card title="Dettaglio per titolo"
            hint="Dove sono finiti i soldi, strumento per strumento. Il guadagno aperto e' quello delle posizioni ancora in portafoglio: puo' ancora cambiare."
            style={{ marginBottom: '1rem' }}>
        {(report.per_strumento || []).length === 0 ? (
          <EmptyChart message="Nessuna operazione registrata nel periodo." />
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse',
                            fontSize: '0.78rem', minWidth: 620 }}>
              <thead>
                <tr style={{ color: C.inkMuted, textAlign: 'right' }}>
                  <th style={{ textAlign: 'left', padding: '6px 8px' }}>Titolo</th>
                  <th style={{ padding: '6px 8px' }}>Chiuse</th>
                  <th style={{ padding: '6px 8px' }}>Vincenti</th>
                  <th style={{ padding: '6px 8px' }}>Realizzato</th>
                  <th style={{ padding: '6px 8px' }}>Commissioni</th>
                  <th style={{ padding: '6px 8px' }}>Aperto</th>
                </tr>
              </thead>
              <tbody>
                {report.per_strumento.map((r) => (
                  <tr key={r.ticker} style={{ borderTop: `1px solid ${C.grid}`,
                                              textAlign: 'right' }}>
                    <td style={{ textAlign: 'left', padding: '6px 8px',
                                 color: C.ink, fontWeight: 600 }}>
                      {r.ticker}
                      {r.posizione_aperta && (
                        <span style={{ color: C.portfolio, fontSize: '0.68rem',
                                       marginLeft: 6 }}>• aperta</span>
                      )}
                    </td>
                    <td style={{ padding: '6px 8px', color: C.inkSoft }}>
                      {int(r.operazioni_chiuse)}
                    </td>
                    <td style={{ padding: '6px 8px', color: C.inkSoft }}>
                      {r.win_rate_pct != null ? `${num(r.win_rate_pct, 0)}%` : '—'}
                    </td>
                    <td style={{ padding: '6px 8px', color: signColor(r.pnl_netto_usd),
                                 fontWeight: 600 }}>
                      {usd(r.pnl_netto_usd)}
                    </td>
                    <td style={{ padding: '6px 8px', color: C.fee }}>
                      {usd(r.commissioni_usd)}
                    </td>
                    <td style={{ padding: '6px 8px', color: signColor(r.pnl_aperto_usd) }}>
                      {r.posizione_aperta ? usd(r.pnl_aperto_usd) : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* Transazioni recenti */}
      <Card
        title="Ultime transazioni"
        hint="Le piu' recenti, con la commissione riga per riga. Il file CSV contiene tutte quelle del periodo, insieme alla motivazione con cui sono state decise."
        right={
          <span style={{ fontSize: '0.7rem', color: C.inkMuted, paddingTop: 3 }}>
            {troncato.transazioni_totali != null
              ? `${int(troncato.transazioni_mostrate)} di ${int(troncato.transazioni_totali)}`
              : `${int((report.transazioni || []).length)} righe`}
          </span>
        }>
        {(report.transazioni || []).length === 0 ? (
          <EmptyChart message="Nessuna transazione nel periodo selezionato." />
        ) : (
          <div style={{ overflowX: 'auto', maxHeight: 460, overflowY: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse',
                            fontSize: '0.76rem', minWidth: 760 }}>
              <thead>
                <tr style={{ color: C.inkMuted, textAlign: 'right',
                             position: 'sticky', top: 0, background: C.surface }}>
                  <th style={{ textAlign: 'left', padding: '6px 8px' }}>Data</th>
                  <th style={{ textAlign: 'left', padding: '6px 8px' }}>Titolo</th>
                  <th style={{ textAlign: 'left', padding: '6px 8px' }}>Operazione</th>
                  <th style={{ padding: '6px 8px' }}>Quantita'</th>
                  <th style={{ padding: '6px 8px' }}>Prezzo</th>
                  <th style={{ padding: '6px 8px' }}>Valore</th>
                  <th style={{ padding: '6px 8px' }}>Commissione</th>
                  <th style={{ padding: '6px 8px' }}>Cassa</th>
                </tr>
              </thead>
              <tbody>
                {[...report.transazioni].reverse().map((t, i) => (
                  <tr key={t.id ?? `${t.timestamp}-${i}`}
                      style={{ borderTop: `1px solid ${C.grid}`, textAlign: 'right',
                               opacity: t.valida === false ? 0.5 : 1 }}>
                    <td style={{ textAlign: 'left', padding: '6px 8px',
                                 color: C.inkMuted, whiteSpace: 'nowrap' }}>
                      {t.data} <span style={{ opacity: 0.6 }}>{t.ora_utc?.slice(0, 5)}</span>
                    </td>
                    <td style={{ textAlign: 'left', padding: '6px 8px',
                                 color: C.ink, fontWeight: 600 }}>{t.ticker}</td>
                    <td style={{ textAlign: 'left', padding: '6px 8px' }}>
                      <span style={{
                        color: t.operazione === 'BUY' ? C.positive : C.negative,
                        fontWeight: 600,
                      }}>{t.operazione}</span>
                      <span style={{ color: C.inkMuted, marginLeft: 6 }}>
                        {t.direzione === 'SHORT' ? 'short' : ''} {t.tipo}
                      </span>
                      {t.valida === false && (
                        <span style={{ color: C.fee, marginLeft: 6 }}
                              title="Riga malformata nel database: esclusa dai calcoli.">
                          ⚠
                        </span>
                      )}
                    </td>
                    <td style={{ padding: '6px 8px', color: C.inkSoft }}>
                      {t.quantita}
                    </td>
                    <td style={{ padding: '6px 8px', color: C.inkSoft }}>
                      {usd(t.prezzo_usd, 2)}
                    </td>
                    <td style={{ padding: '6px 8px', color: C.inkSoft }}>
                      {usd(t.valore_lordo_usd, 0)}
                    </td>
                    <td style={{ padding: '6px 8px', color: C.fee }}>
                      {usd(t.commissione_usd)}
                    </td>
                    <td style={{ padding: '6px 8px', color: signColor(t.flusso_cassa_usd) }}>
                      {usd(t.flusso_cassa_usd, 0)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <p style={{ fontSize: '0.7rem', color: C.inkMuted, marginTop: '1rem',
                  lineHeight: 1.6 }}>
        Report generato il {String(report.generato_il || '').slice(0, 19).replace('T', ' ')} UTC.
        Importi in dollari. Portafoglio simulato: nessun denaro reale si muove.
      </p>
    </div>
  );
}
