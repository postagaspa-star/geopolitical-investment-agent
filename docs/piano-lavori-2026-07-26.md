# Piano di lavoro — il robot deve imparare a guadagnare, non solo a non sbagliare

**26 luglio 2026 — area di lavoro separata (branch `fix/watchdog-blind-spot`)**

Questo documento è la mappa di quello che farò. Serve a te per seguire i lavori
e a me per non perdere la rotta. È scritto apposta in lingua semplice.

---

## Da dove partiamo (riassunto in tre righe)

1. Il robot oggi ottimizza la cosa sbagliata: "non fare mai errori visibili".
   Tutte le sue regole sanno solo dire "no, fermati"; nessuna sa dire
   "muoviti, i soldi fermi non fruttano". Stare fermo è l'unica strategia che
   non gli costa mai un rimprovero — e lui, giustamente, la sceglie.
2. Buona parte dell'immobilità che vedevi era però un guasto tecnico: un
   servizio esterno rotto veniva scambiato per "mercato tranquillo", e una
   formula matematica gli impedisce comunque di investire più di tanto (il
   famoso 76% di soldi fermi è il risultato della formula, non prudenza).
3. I guasti li ho già riparati in area separata (8 pacchetti di modifiche,
   578 controlli automatici tutti verdi). Ora: ricontrollare, misurare, e
   costruire il test che dice se il robot ha davvero fiuto.

---

## Le regole che valgono per tutto il piano (zone rosse)

- **Non tocco il sistema vero.** Tutto avviene in un'area separata. Portare le
  modifiche sul robot in funzione ("accenderle in produzione") lo decidi
  soltanto tu.
- **Non tocco i freni di sicurezza**: lo stop obbligatorio su ogni acquisto,
  il divieto di allargare uno stop già messo, i controlli contro i prezzi
  sballati, il rispetto degli orari di borsa, la contabilità. Sono le cose che
  hanno impedito danni peggiori — la perdita da −695 è stata il sistema che ha
  funzionato, non che ha fallito.
- **La vendita automatica al "prendi profitto" resta spenta** (fu spenta dopo
  un incidente vero): ora il target raggiunto almeno avvisa, ma non vende da
  solo.
- **Niente numeri inventati**: ogni misura dichiara quando i dati non bastano
  per un verdetto, invece di fingere precisione.
- **Ogni novità ha il suo interruttore di spegnimento.**

---

## Il metodo: un checkpoint prima di ogni passo

Prima di ogni passo importante mi fermo e rispondo a queste domande:

1. Ha ancora senso, alla luce di quello che ho scoperto nel frattempo?
2. Si può fare in modo più semplice?
3. Cosa potrebbe rompere nel resto del sistema, e come me ne accorgerei?
4. Il passo precedente ha mantenuto le promesse? (rifaccio girare i controlli
   automatici, rileggo le modifiche, e quando possibile faccio una prova
   pratica sul serio)

Se una risposta non mi convince, mi fermo e te lo dico invece di procedere.

---

## Fase 0 — Ricontrollo di tutto il lavoro già fatto

**Cosa faccio:** rifaccio girare tutti i controlli automatici; verifico che le
mie modifiche vadano d'accordo con quelle fatte nel frattempo dall'altra
sessione (i nomi dei modelli corretti + l'allarme sui fornitori esterni);
controllo che il programma parta senza errori; e verifico sul sistema vero che
il guasto del servizio esterno sia rientrato davvero dopo la correzione.

**Rischi:** nessuno — solo letture e prove in area separata.

**Esito atteso:** "tutto a posto", oppure una lista di cose da sistemare prima
di andare avanti.

---

## Fase 1 — Le misure che mancano

**Misura A — Il portafoglio "balla" già abbastanza?**
Confronto quanto oscilla da un giorno all'altro il valore del portafoglio
attuale (2 posizioni + tanta liquidità) con quanto oscillerebbe un portafoglio
ben distribuito e investito più a fondo. Se balla già uguale o di più, la
diagnosi cambia: il problema non è "pochi soldi investiti" ma "troppo
concentrati su poche cose" — e la cura è distribuire, non rischiare di più.
Questa risposta orienta tutto il resto.

**Misura B — Fotografia aggiornata del sistema vero.**
Il cancello di risveglio ora valuta davvero il mercato? Quante "promesse"
("comprerò se scende a X") scadono nel nulla? Una parte si vede già dai
registri; il quadro completo arriverà solo quando i fix saranno in produzione.

**✋ Checkpoint con te:** ti porto i numeri e ti propongo di portare i fix sul
sistema vero. Decidi tu. Dopo il via libera: una settimana di osservazione con
la nuova pagella (`/api/live/inaction`).

---

## Fase 2 — L'agente ombra: il test del fiuto

**Cosa costruisco:** ogni giorno di borsa il sistema scrive previsioni finte,
senza toccare un euro: "compra" per finta i 3 titoli più forti secondo il suo
radar e "vende" per finta i 3 più deboli, con verifica automatica dopo 5
giorni di borsa. In parallelo, un gemello cieco fa lo stesso numero di
previsioni **a caso**, sugli stessi titoli possibili. Dopo circa 50 previsioni
chiuse per parte, si confrontano: il robot batte il caso, sì o no?

**Perché è il passo più importante:** con i trade veri (5-10 al mese)
servirebbero anni per distinguere la fortuna dalla bravura. Con le previsioni
finte bastano poche settimane, a costo quasi zero e rischio zero. E si misura
il segnale che davvero guida il robot oggi (il radar dei titoli forti/deboli),
non l'etichetta che c'è scritta sopra ("geopolitica").

**Se il robot non batte il caso:** il fiuto attuale non esiste, e la Fase 3
diventa la strada obbligata.

**Attenzioni:** nessun verdetto prima di ~50 previsioni chiuse per parte; nei
giorni di borsa chiusa non si scrive nulla; se mancano i prezzi la previsione
si annulla invece di inventare; se i dati del radar quel giorno sono sporchi,
non si scrive nulla e lo si dichiara.

---

## Fase 3 — Il progetto "posizione normale" (solo su carta, per ora)

**L'idea:** rovesciare il punto di partenza. Oggi il robot parte fermo e deve
convincersi a muoversi superando tre livelli di paura; domani partirebbe già
investito in una base fissa e ben distribuita, e il suo lavoro sarebbe solo
decidere gli **scostamenti** dalla base, motivandoli. Stare fermi smette di
essere gratis: ogni giorno passato troppo indietro rispetto alla base, mentre
il mercato si muove, diventa un costo contato in euro — esattamente come oggi
si contano le commissioni.

**La trappola da evitare** (scoperta facendo criticare l'idea da giudici
indipendenti): la base NON deve deciderla il robot — altrimenti se la mette
bassissima e poi "rispetta l'obiettivo" restando fermo, barando senza saperlo.
La base la fissa una formula agganciata a quanto si muove il mercato.

**Cosa produco:** un progetto scritto + una prova su dati storici contro due
alternative: (a) il comportamento attuale, (b) il restare investiti e basta.
**Niente viene acceso sul sistema vero in questa fase.**

**✋ Checkpoint con te:** con i numeri della prova storica davanti, decidi tu
se costruirlo davvero.

---

## Fase 4 — Accensioni graduali (tutte decisioni tue)

Una alla volta, con circa una settimana di osservazione tra l'una e l'altra,
così ogni effetto è attribuibile alla sua causa:

1. **Portare in produzione i fix già pronti.** Nota onesta: due novità sono
   accese di serie (il respiro sui guadagni prima che scatti la protezione, e
   il freno ai ripensamenti rapidi) — approvando il deploy approvi anche
   quelle. Entrambe hanno l'interruttore per tornare indietro.
2. **L'interruttore del calcolo sul patrimonio totale** — quello che sblocca
   il 76% fermo. È spento; si accende solo per tua scelta.
3. **Se le misure delle Fasi 1-2 lo sostengono:** il pilota della "posizione
   normale" (Fase 3 costruita davvero).

---

## Stato dei lavori (aggiorno qui man mano)

- [x] Piano scritto e condiviso
- [ ] Fase 0 — ricontrollo
- [ ] Fase 1 — misure (A: ballo del portafoglio, B: fotografia sistema vero)
- [ ] ✋ Checkpoint deploy (decisione di Andrea)
- [ ] Fase 2 — agente ombra costruito e in osservazione
- [ ] Fase 3 — progetto su carta + prova storica
- [ ] ✋ Checkpoint "posizione normale" (decisione di Andrea)
- [ ] Fase 4 — accensioni graduali (decisioni di Andrea)
