# Agente di Investimento Geopolitico

Sistema multi-agente che legge eventi geopolitici e macroeconomici, ne stima
l'impatto sui mercati e gestisce un portafoglio **simulato** (paper trading).
Nessun capitale reale viene movimentato.

## Perché esiste

Il progetto nasce come banco di prova su una domanda precisa: cosa serve perché
un LLM prenda decisioni ripetibili invece che plausibili. Un modello che
"ragiona" su una notizia produce sempre una risposta convincente, anche quando i
dati sotto sono sbagliati o mancanti. Quasi tutto il lavoro sta quindi nei
vincoli attorno al modello, non nel prompt.

Non vengono pubblicati risultati di rendimento: il sistema è un esperimento di
architettura decisionale, e numeri di performance su un simulatore direbbero poco
di onesto.

## Come funziona

Pipeline a più stadi, con modelli diversi secondo il costo del passaggio: uno
stadio economico filtra e struttura il flusso di notizie, Claude interviene sulla
sintesi e sulla decisione. Sopra ci sono agenti separati con ruoli distinti —
chi propone candidati, chi decide, chi verifica a posteriori — e un livello di
regole deterministiche che può vincolare l'esito ma non inventarlo.

Le uscite dalle posizioni seguono regole scritte in codice, non lasciate al
modello: in particolare uno stop loss può stringersi ma **mai** allargarsi, per
impedire la razionalizzazione a posteriori di una posizione in perdita.

## Stack

Backend Python + FastAPI, frontend React + TypeScript + Vite, persistenza su
Supabase (Postgres), deploy su Render, notifiche operative via Telegram. LLM:
Claude (Anthropic) e un modello economico per gli stadi ad alto volume.

## Note tecniche

- **Dati corrotti che sembravano un bug di arrotondamento**: alcune serie
  storiche arrivavano deformate. La causa non era il calcolo ma il *fetch
  parallelo*, che superava il rate limit del provider e riceveva risposte
  troncate senza errore esplicito. Diagnosi corretta e serializzazione del
  recupero dati.
- **Paralisi decisionale**: il sistema aveva imparato che non agire non produce
  mai un errore misurabile, e tendeva a restare liquido. Corretto sostituendo la
  soglia secca di confidenza con un cancello sul valore atteso, così l'inazione
  smette di essere gratis.
- **Fragilità dei fornitori**: tre rotture da dipendenze esterne in dieci giorni
  (endpoint rimosso, credito esaurito, modelli ritirati dal provider). Aggiunto
  un monitor orario sullo stato dei fornitori con alert, perché il guasto va
  visto quando accade, non a posteriori.
- **Blackout silenzioso**: il polling dei prezzi si è fermato a lungo senza
  crash, per coroutine appese senza timeout. Da lì timeout espliciti e vincolo di
  istanza singola sui job periodici.

## Avvertenza

Progetto personale a scopo di studio. Non è consulenza finanziaria e non
costituisce raccomandazione di investimento.
