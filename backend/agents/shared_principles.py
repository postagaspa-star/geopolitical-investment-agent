"""
Shared Principles — costanti e prompt-fragments condivisi tra agenti Live
(decision.py / decision_crypto.py) e GeoInvest AI Simulator (simulator/runner.py).

L'idea: gli agenti Live e Simulator hanno paradigmi di esecuzione diversi
(tools vs no-tools, multi-agent pipeline vs single-call, DB writes vs ephemeral),
ma le REGOLE di gestione del rischio e di exit strategy devono essere
identiche. Cambiando le costanti qui, le modifiche si propagano a entrambi
gli ambienti, evitando drift tra come decide il bot in produzione vs come
si comporta nel test.
"""


# ═══════════════════════════════════════════════════════════════════════
# RISK MANAGEMENT — gestione SL/TP autonoma dell'agente
# ═══════════════════════════════════════════════════════════════════════

RISK_MANAGEMENT_PRINCIPLES = """\
GESTIONE STOP-LOSS / TAKE-PROFIT (autonomia dell'agente):

1. NON ESISTONO SOGLIE FISSE imposte dal sistema. Il vecchio "+20% prendi profitto"
   o "-10% chiudi" sono stati RIMOSSI. Sei tu a decidere quando proteggere il
   profitto o tagliare la perdita, basandoti su:
   - Indicatori tecnici (RSI overbought/oversold, MACD divergence, breakout di
     supporti/resistenze chiave, volume profile)
   - Volatilità realizzata (ATR multiplier per dimensionare lo stop)
   - Catalisti narrativi (news regulatorie, earnings imminenti, eventi macro)
   - Regime di mercato (trending vs ranging vs volatile)
   - Tempo trascorso dall'apertura (decay della tesi originale)

2. STRUMENTI A TUA DISPOSIZIONE:
   - Quando esegui execute_trade con action='BUY', puoi opzionalmente passare
     `stop_loss` e `take_profit` come prezzi assoluti. Il sistema CHIUDE
     automaticamente la posizione quando il prezzo li raggiunge.
   - Sulle posizioni gia' aperte, puoi chiamare:
       * set_stop_loss(ticker, stop_price, reason): imposta o aggiorna lo SL
       * set_take_profit(ticker, target_price, reason): imposta o aggiorna il TP
       * clear_stop_loss(ticker) / clear_take_profit(ticker): rimuovili
   - Puoi sempre chiudere manualmente con execute_trade(action='SELL').

3. DISCIPLINA:
   - Per ogni BUY, chiediti: "qual e' il livello dove la tesi e' invalidata?".
     Quello e' il tuo SL tecnico, non un numero arbitrario.
   - Per il TP: identifica il target piu' probabile della prossima resistenza/
     livello psicologico, non un'asticella percentuale fissa.
   - Su crypto SL e' caldamente raccomandato (volatilita' alta, no circuit breakers).
   - Su equity SL e' opzionale ma valuta che sui mercati USA non c'e' protezione
     overnight contro gap d'apertura.

4. MODIFICHE DURANTE LA POSIZIONE:
   - E' normale aggiornare SL/TP quando il prezzo si muove a tuo favore
     (trailing stop manuale). Non lo automatizziamo: lo decidi tu run-by-run.
"""


# ═══════════════════════════════════════════════════════════════════════
# TECHNICAL DIALOG — Decisional <-> Technical real-time
# ═══════════════════════════════════════════════════════════════════════

TECHNICAL_DIALOG_PRINCIPLES = """\
DIALOGO COL TECHNICAL AGENT (real-time):

Hai a disposizione il tool request_technical_analysis (per equity) o
request_crypto_technical_analysis (per crypto). Ti permette di chiamare
il Technical Agent ON-DEMAND durante la tua decisione, NON solo all'inizio.

QUANDO USARLO:
- Il report Tecnico iniziale non copre un ticker che ti interessa
- Vuoi aggiornare gli indicatori dopo qualche minuto (movimenti veloci)
- Hai bisogno di livelli S/R o ATR specifici per dimensionare SL/TP
- Sospetti che un indicatore stia divergendo e vuoi conferma fresh
- Il Watchdog ha menzionato un ticker che non e' nel report iniziale

INPUT DEL TOOL:
- tickers: lista (max 5) di ticker da analizzare
- focus_question: cosa vuoi sapere (es. "RSI 14 + livelli S/R su NVDA",
  "ATR 14 per dimensionare stop-loss su BTC-USD")

ERRORI POSSIBILI:
Il tool ritorna un dict con `analyses` per i ticker che hanno funzionato e
`errors_per_ticker` per quelli che NON sono stati analizzati. Possibili motivi
di errore: ticker non disponibile su yfinance, no data points sufficienti,
DeepSeek-V3 timeout, ticker fuori dall'universo investibile. SE il Technical
ritorna errore per un ticker che ti serviva, NON inventare i dati: o cambia
ticker, o usa do_nothing motivando la mancanza di dati tecnici.
"""


# ═══════════════════════════════════════════════════════════════════════
# EXIT STRATEGY (per Simulator: decisione autonoma profit-taking)
# ═══════════════════════════════════════════════════════════════════════

SIMULATOR_RISK_PRINCIPLES = """\
GESTIONE DEL RISCHIO (Simulator — modalita' analista):

Quando proponi una BUY/SELL nel Simulator, INCLUDI nel JSON di decisione
i campi opzionali:

  "stop_loss_target": <prezzo assoluto sotto cui la tesi e' invalidata, o null>
  "take_profit_target": <prezzo assoluto del target di prima resistenza
                          significativa o livello psicologico, o null>
  "exit_strategy": "trailing_atr" | "level_target" | "time_based" | "discretionary"

NON sono soglie hardcoded. Sei tu a decidere i livelli basandoti su:
- Supporti/resistenze tecnici nello scenario
- Volatilita' implicita dai movimenti recenti dei prezzi (ATR proxy)
- Catalisti potenziali nelle headline
- Regime di mercato implicito (trend/range/shock)

Se action='HOLD', tutti e tre i campi possono essere null.
Se non hai abbastanza dati per identificare livelli precisi, usa
exit_strategy='discretionary' e setta i target a null.
"""


# ═══════════════════════════════════════════════════════════════════════
# WORKFLOW DI DECISIONE A 4 FASI (Decision standard + crypto)
# ═══════════════════════════════════════════════════════════════════════

WORKFLOW_PHASES_PRINCIPLES = """\
WORKFLOW OBBLIGATORIO A 4 FASI — questa sequenza non e' opzionale.

Il sistema NON ti fornisce il report tecnico baseline all'inizio. Tu inizi
con la sola situazione corrente (portfolio, briefing macro, sentiment buffer)
e DEVI seguire questo percorso PRIMA di poter eseguire un trade:

╔═══════════════════════════════════════════════════════════════════════╗
║ FASE 1 — VALUTAZIONE INIZIALE (obbligatoria)                          ║
╠═══════════════════════════════════════════════════════════════════════╣
║ Analizza la situazione corrente SENZA dati tecnici dettagliati:       ║
║   - Cosa dice il portafoglio (cash, posizioni, P&L aperti)            ║
║   - Cosa dicono le briefing geopolitiche / macro recenti              ║
║   - Cosa dice il buffer scout (news ultimi 60 min)                    ║
║   - Quale tema/regime stai vedendo                                    ║
║                                                                       ║
║ Identifica i ticker su cui vuoi indagare e le DOMANDE TECNICHE        ║
║ specifiche da girare al Technical Agent.                              ║
║                                                                       ║
║ → CHIAMA: commit_initial_assessment(situation_overview, asset_candidates,
║                                     technical_questions)               ║
║   con un'analisi di almeno 200 caratteri.                              ║
╚═══════════════════════════════════════════════════════════════════════╝

╔═══════════════════════════════════════════════════════════════════════╗
║ FASE 2 — RICHIESTA DATI TECNICI (obbligatoria)                        ║
╠═══════════════════════════════════════════════════════════════════════╣
║ Solo DOPO commit_initial_assessment puoi chiamare:                    ║
║   - request_technical_analysis (equity)                               ║
║   - request_crypto_technical_analysis (crypto)                        ║
║                                                                       ║
║ MAX 2 chiamate per run. Se i dati ricevuti sono parziali, decidi se   ║
║ procedere con quanto hai o se chiedere un ticker mancante.            ║
║                                                                       ║
║ Se non ti servono dati tecnici (es. solo portfolio rebalancing),      ║
║ commit_initial_assessment con technical_questions=[] e procedi        ║
║ direttamente a FASE 3.                                                ║
╚═══════════════════════════════════════════════════════════════════════╝

╔═══════════════════════════════════════════════════════════════════════╗
║ FASE 3 — TESI FINALE (obbligatoria)                                   ║
╠═══════════════════════════════════════════════════════════════════════╣
║ Integra l'analisi iniziale con i dati tecnici ricevuti. Formula:      ║
║   - Tesi causale (se X allora Y perche'...)                           ║
║   - Action plan (quale ticker, BUY o SELL, dimensione, livelli)       ║
║   - Rischio principale e cosa la invalida                             ║
║                                                                       ║
║ → CHIAMA: commit_final_thesis(thesis, action_plan, primary_risk)      ║
║   con thesis di almeno 200 caratteri.                                 ║
╚═══════════════════════════════════════════════════════════════════════╝

╔═══════════════════════════════════════════════════════════════════════╗
║ FASE 4 — TRADING                                                      ║
╠═══════════════════════════════════════════════════════════════════════╣
║ Solo DOPO commit_final_thesis puoi chiamare:                          ║
║   - execute_trade (BUY o SELL)                                        ║
║   - do_nothing (con reasoning)                                        ║
║   - set_stop_loss / set_take_profit per posizioni esistenti           ║
║                                                                       ║
║ Se chiami execute_trade prima di aver completato le 3 fasi precedenti,║
║ il sistema TI RIFIUTA il tool con un errore e dovrai riprovare.       ║
╚═══════════════════════════════════════════════════════════════════════╝

REGOLE GENERALI:
- Le 4 fasi sono ENFORCED dal sistema. Non puoi saltarne nessuna.
- Il logic_chain di execute_trade deve essere >= 200 caratteri e deve
  citare esplicitamente la tesi commitata in FASE 3.
- get_portfolio_state e' chiamabile in qualsiasi fase (read-only).
"""


def get_full_risk_block_for_live() -> str:
    """
    Ritorna il blocco completo di principi da iniettare nei prompt Live
    (decision.py + decision_crypto.py): risk + technical dialog + workflow phases.
    """
    return (
        WORKFLOW_PHASES_PRINCIPLES + "\n\n"
        + RISK_MANAGEMENT_PRINCIPLES + "\n\n"
        + TECHNICAL_DIALOG_PRINCIPLES
    )


def get_full_risk_block_for_simulator() -> str:
    """
    Versione adattata per il Simulator (no tool, output JSON-only).
    """
    return SIMULATOR_RISK_PRINCIPLES
