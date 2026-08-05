# La funzione obiettivo dell'agente Live — diagnosi, interventi, prossimi passi

> **Note for English readers.** Internal analysis, in Italian. Bottom line: the
> agent had an unwritten objective function — minimise *visible* errors of
> commission, at unlimited cost of errors of omission — and was optimising the
> wrong thing well. The 76% cash figure below is not caution but the arithmetic
> of a sizing formula, and 94% of the "don't act" cycles turned out to be a
> broken external API rather than any judgement about the market. The document
> also concludes that what was running was a cautious momentum trader with a
> geopolitical narrative on top. Simulated portfolio throughout.

**25 luglio 2026** — branch `fix/watchdog-blind-spot` (7 commit, 578 test verdi).

Documento di consegna. Le decisioni che restano ad Andrea sono in fondo.

---

## 1. La diagnosi, corretta dai dati

Il punto di partenza era: *"il cancello d'ingresso pretende movimenti superiori
al 2,5% e quindi l'agente resta fermo"*. I log di produzione dicono altro.

Su **512 cicli** del watchdog (24-25/07):

| Esito | Cicli | Cos'era davvero |
|---|---|---|
| `http_400` | 250 (49%) | **L'API rispondeva errore.** DeepSeek aveva ritirato i model ID |
| `decision_ran_recently` | 229 (45%) | Throttle: la domanda non è stata posta |
| Giudizio sul mercato | 27 (5%) | Valutazione vera |
| Trigger | 6 (1%) | |

**Il 94% dei "non svegliare" non era una valutazione del mercato.** Abbassare la
soglia del 2,5% non avrebbe cambiato nulla: quel giudizio veniva espresso in
27 cicli su 512.

Tre cose che il codice smentisce, e che vale la pena aver chiare:

- **Il cancello non era codice, era un prompt.** Le soglie "2,5% in 5 minuti"
  vivevano dentro il testo inviato a un LLM esterno. Era l'unico vincolo del
  sistema a non essere codice: non testabile, non deterministico, e con il
  fallimento che degradava implicitamente in "non svegliare".
- **Il dato non era quello dichiarato.** Il prompt annunciava
  `PRICE SNAPSHOT (last 5min)` ma passava `chg_pct`, che è la variazione dalla
  **chiusura precedente**. Si chiedeva un giudizio su una finestra che non si
  stava osservando.
- **Il calo di rendimento non è churn.** Negli ultimi quattro giorni le
  operazioni sono state zero: le commissioni sono aritmeticamente escluse. E le
  13 inversioni valgono il 2,6% del *nozionale ruotato di quel titolo*, non del
  NAV — se pesava il 5%, sono ~0,13% di NAV, un ordine di grandezza sotto il
  calo osservato. Il churn è un problema reale di disciplina, ma non è ciò che
  ha eroso il rendimento in questi giorni.

## 2. La funzione obiettivo implicita

Nessuno l'ha mai scritta, ma il codice ne rivela una coerente:

> **minimizzare gli errori di commissione osservabili, sotto un vincolo di
> budget API** — a costo illimitato di errori di omissione.

Tre firme strutturali, indipendenti fra loro:

**La topologia dei veti.** Ogni governatore può solo *impedire* o *ridurre*
un'azione. Non esiste una riga che possa *imporne* una (l'unica eccezione, il
rebalance sopra il 35%, è anch'essa una riduzione di rischio). Il sistema sa
dire "no" solo a chi si muove.

**Il denominatore del sizing.** `alloc_pct = trade_value / cash`: ogni apertura
prende al massimo il 15% di ciò che *resta*, quindi il cash segue `0,85^n`.
Con due posizioni fa 72,25% — l'osservato era 76%. Il portafoglio liquido non
è una scelta prudente dell'agente, è l'output della formula. Anche saturando
tutte e sei le posizioni ammesse non potrebbe scendere sotto il ~38% di cash:
ha una garanzia strutturale di non essere mai pienamente investito.

**La storia sedimentata nei default.** Ogni meccanismo che ha causato una
perdita per *azione* è stato spento e reso opt-in (circuit breaker, lock-in,
trailing di `risk_state`, capital orchestrator — i commenti lo documentano).
Nessuno è mai stato spento perché causava *inazione*: l'inazione non produce
un incidente osservabile, quindi non genera mai un commit correttivo. Il
sistema ha imparato in modo asimmetrico — ogni round di fix lo ha stretto, e
nessuno lo ha mai allargato.

Il 76% di liquidità con +2,9% non è il sistema che si rompe: è il punto fisso
verso cui questa funzione obiettivo converge. Sta ottimizzando bene la cosa
sbagliata.

**Dove diverge da quella che serve: la simmetria.** Il rendimento è una
grandezza a due code — per avere una vittoria devi accettare un errore. Il
sistema misura solo la coda degli errori di commissione e non ha *nessuno
strumento* per vedere l'altra: nessun contatore di occasioni perse, nessun
costo del capitale fermo, nessun log di "non ho svegliato e il mercato si è
mosso". Non può sapere che sta perdendo per omissione.

## 3. Cosa è stato fatto

Sette interventi. Nessuno tocca le parti sane (vedi §5).

| # | Intervento | Effetto |
|---|---|---|
| 1 | **Un guasto non è una decisione** | Un Decision fallito aggiornava "ultima decisione" (throttle 2h) e faceva scattare il backoff a 4h: più il sistema era rotto, più a lungo taceva. Ora gli errori non comprano silenzio; backoff 240→60 min e rumoroso (`WATCHDOG_DEGRADED`) |
| 2 | **Rete deterministica** | Se l'LLM non risponde, il sistema guarda comunque i prezzi — in codice, con soglie esplicite e testabili, e con la finestra temporale dichiarata per quella che è |
| 3 | **Throttle per agente** | Il crypto gira ogni ora 24/7 e il throttle è 2h: ogni run crypto zittiva anche l'equity, che opera su un universo disgiunto. Il ramo equity non poteva quasi mai partire |
| 4 | **Denominatore del sizing** | `compute_allocation_pct` con flag `cash`/`nav`. Default invariato: accenderlo è una decisione sul capitale |
| 5 | **Freschezza prezzi** | Polling 1200s vs cache 700s: per il 41% del tempo lo snapshot era vuoto. Ora le due cadenze sono legate alla fonte |
| 6 | **Orizzonte delle uscite** | Il trailing si attivava a +0,01% portando lo stop a −4%, scavalcando lo stop di profilo (7-15%): una posizione salita del 2% e rientrata veniva **chiusa in perdita** dal meccanismo che doveva proteggerla. Più: il take-profit prometteva un'esecuzione che non avveniva |
| 7 | **Attrito sul dietrofront** | Blocca il rientro dopo uno stop (12h) e l'inversione rapida (24h). Non blocca mai le uscite. Fail-open |
| 8 | **Misura dell'omissione** | `/api/live/inaction`: perché il cancello non sveglia, giorni senza operare, % liquidità, promesse scadute non onorate, costo del capitale fermo |

Sul punto 8: eseguito sui log veri, riproduce l'analisi fatta a mano
(250/229/27/6, valutati davvero il 6,4%, salute "GUASTO"). È lo strumento che
rende verificabile tutto il resto.

## 4. Il lavoro parallelo

Mentre lavoravo, un'altra sessione ha corretto i model ID e aggiunto
`provider_health.py` (sentinella oraria + Telegram). Quel commit dice che in
10 giorni il sistema si è fermato **tre volte** per guasti a dipendenze
esterne (Anthropic 404, DeepSeek 402, DeepSeek 400).

I due lavori sono complementari: quella è la *rilevazione* del guasto
dall'esterno, questa è la rimozione del meccanismo che lo rendeva
**invisibile e auto-amplificante**. Senza il punto 1, al prossimo guasto di un
fornitore il sistema tornerebbe a comprarsi ore di silenzio.

## 5. Cosa NON è stato toccato, e perché

- **Il cricchetto anti-allargamento dello stop** — l'unico governatore
  davvero asimmetrico e ben progettato, nato dal caso NEAR documentato.
- **Lo stop obbligatorio fail-closed sulle aperture** — è la ragione per cui
  la perdita massima è −695 e non un multiplo. *Il −695 è il sistema che ha
  funzionato*: l'errore non è stato lo stop, è stato che non c'era altro in
  portafoglio a compensarlo.
- **`accounting.py` e `simulator/metrics.py`** — puri, senza stato, con la
  coerenza delle commissioni curata a mano.
- **I criteri a priori dell'edge-tracker e il test di permutazione** — è ciò
  che impedisce di spostare l'asticella dopo aver visto i risultati.
- **La validazione prezzi a 3 tier e i gate di mercato aperto** — impediscono
  di operare su prezzi fantasma (incidente NVDA).
- **I cron indipendenti dal watchdog** — oggi sono l'unica ragione per cui il
  sistema decide ancora qualcosa.
- **L'esecuzione automatica dei take-profit** resta spenta: fu disattivata
  dopo un incidente reale. È stata resa *onesta*, non riaccesa.

## 6. Decisioni che restano ad Andrea

**Due cambiano il comportamento sul capitale** e vanno approvate prima del
rilascio:

1. **Soglia di attivazione del trailing** (attiva di default nel branch).
   Posizioni tenute più a lungo, meno uscite premature; in cambio si
   restituisce più guadagno sui rientri. Via di fuga: `trailing_activation_pct = 0`.
2. **Attrito sul dietrofront** (attivo di default). Alcune aperture verranno
   rifiutate. Spegnibile: `churn_guard_enabled = false`.

**Una è spenta e aspetta una tua scelta esplicita:**

3. **Denominatore del sizing.** `sizing_denominator = "nav"` è il singolo
   cambio che sblocca l'esposizione strutturale. Con `cash` il portafoglio non
   può investirsi oltre una certa soglia, per aritmetica. Consiglio di
   accenderlo **dopo** aver osservato una settimana con gli altri fix, così
   l'effetto è attribuibile.

## 7. Il prossimo passo, non fatto

**L'agente ombra** — l'esperimento che falsifica l'edge in settimane invece
che in anni, e che non ho costruito per non improvvisarlo.

L'idea: l'agente scrive la tesi completa (direzione, livello, invalidazione,
obiettivo, orizzonte) **senza eseguire**; in parallelo un generatore casuale
produce tesi con la stessa distribuzione di direzione e size, campionate da
quelle vere. Dopo qualche settimana si confronta la qualità predittiva delle
due serie.

Perché è il singolo esperimento più informativo: con 5-10 operazioni al mese
servono anni di trade reali per distinguere edge da fortuna, mentre l'ombra
accumula segnali giudicabili molto più in fretta, a rischio zero. Se le tesi
dell'agente non battono quelle casuali su ~50 casi, **l'edge geopolitico è
falsificato** — e questa è la domanda che hai detto tu stesso di voler
riaprire.

Un dato che vale la pena sapere prima di riprogettare l'edge: nel codice è
già cambiato senza che nessuno lo dichiarasse. Una "REGOLA FERREA" ripetuta
due volte nel prompt stabilisce che i tecnici pesano *sempre* più delle
notizie; il regime `GEOPOLITICAL` non riceve alcun premio di conviction; non
esiste un solo documento geopolitico ingerito nel sistema (la cartella
`documents/` contiene guide di analisi tecnica e un CV); le fonti sono tre
keyword GDELT e due query NewsAPI, troncate in coda al contesto. Quello che
gira oggi è **un momentum trader cauto con un rivestimento narrativo
geopolitico**.

## 8. Come verificare che stia funzionando

Il criterio, per qualunque cosa si decida dopo: **il benchmark non deve mai
essere il buy-and-hold.** Tutti i backtest attuali confrontano il sistema con
un'alternativa sempre investita, e lo stato reale — liquidità strutturale —
non ha banco di prova. Una funzione obiettivo che batte il buy-and-hold
restando liquida al 76% sta ingannando.

Nell'ordine, e con i dati che ci sono già:

1. `GET /api/live/inaction` — la quota di cicli in cui il mercato viene
   davvero valutato deve salire ben sopra il 6,4% di oggi. Se resta bassa, i
   fix non hanno agito e il resto è discorso.
2. Il tasso di promesse condizionate scadute senza essere onorate: misura
   direttamente "mi pongo un livello e aspetto indefinitamente".
3. **La misura che smonta o conferma la premessa**, e costa minuti: il
   rapporto fra la volatilità del portafoglio attuale e quella di una base
   diversificata a piena esposizione. Se due posizioni concentrate danno già
   una volatilità pari o superiore, allora "sono poco esposto al rischio" è
   falso — sei poco *diversificato*, che è un problema diverso e si risolve
   abbassando il rischio, non alzandolo.
