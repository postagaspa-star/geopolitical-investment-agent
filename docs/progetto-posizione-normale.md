# Progetto "posizione normale" — Fase 3, su carta

**Fine luglio 2026 — nessuna riga di questo progetto è attiva sul sistema vero.**
Passato da tre giudici critici indipendenti, poi **ricalibrato dopo la
decisione di Andrea**: niente cripto nella base, e una regola di rischio
precisa (quasi mai sotto −10%; fino a −12% solo se il potenziale supera il
+18%). Questo documento riflette la versione aggiornata.

---

## In una frase

Oggi il robot parte fermo e deve convincersi a muoversi; il progetto lo fa
partire **già investito in una base fissa, distribuita e dimensionata da una
formula** — e il suo lavoro diventa decidere gli scostamenti dalla base,
motivandoli. Stare fermi smette di essere gratis.

## Perché serve (i due fatti che l'hanno reso urgente)

1. **La misura del rapporto 1,11**: il portafoglio attuale balla già più di
   una base ben distribuita — con tre posizioni, tutte cripto, tutte nella
   stessa direzione. Il problema vero non è "pochi soldi in gioco": è che i
   pochi in gioco sono un'unica scommessa. La base cura esattamente questo.
2. **Il costo del fermo è invisibile**: nessun numero dice quanto rende il
   capitale lasciato fermo. Con una base di riferimento, ogni giorno passato
   sotto la base diventa un costo contato in euro, come una commissione.

## La decisione che ha cambiato il progetto

Andrea ha tolto le cripto dalla base: la spinta iniziale a favore delle
cripto era nata dal voler guadagnare di più puntando sulla bravura
matematica/quant dell'AI invece che sulla sua capacità di interpretare le
notizie — un ragionamento ora ritirato. E ha fissato una regola di rischio
precisa: **quasi mai sotto −10%**; si può scendere fino a −12% **solo se il
potenziale di rendimento supera il +18%**.

Ho ricalcolato tutto su questa base a **tre soli ingredienti**: azioni
americane, obbligazioni lunghe, oro. Risultato onesto: **senza cripto, il
rendimento massimo osservato in tutta la griglia — anche spingendo il
rischio al limite — è il 6% l'anno.** La soglia del +18% non viene mai
sfiorata: per questa base la regola si riduce, di fatto, al vincolo secco
del −10%.

## Come funziona la base (in parole)

- **Tre ingredienti che si muovono in modo diverso**: azioni americane,
  obbligazioni lunghe, oro. Niente cripto (vedi sopra).
- **Pesi "al contrario del nervosismo"**: ogni mese, chi ha ballato di più
  negli ultimi 60 giorni pesa di meno. Niente opinioni: solo aritmetica.
- **Un termometro decide quanto investire in totale**: si stima quanto
  ballerebbe il paniere e si scala l'esposizione perché il ballo atteso
  resti al bersaglio. Il resto sta in liquidità — come **risultato di un
  calcolo**, non come stato di riposo gratuito. (E la liquidità rende: vedi
  numeri sotto.)
- **La trappola evitata**: la base NON la decide il robot. Se la decidesse
  lui, se la metterebbe bassissima e "rispetterebbe l'obiettivo" restando
  fermo. La fissa la formula, punto.

## Cosa resta al robot

Gli **scostamenti** dalla base, con i paletti fissati dai giudici (validi
anche ora, a maggior ragione senza la valvola di sfogo delle cripto):
- il budget di scostamento si misura in **rischio aggiunto**, non in
  percentuale di peso (sennò tanti scostamenti piccoli = una scommessa
  grossa);
- i tetti per-ingrediente valgono **dopo** gli scostamenti;
- **niente rinnovo automatico**: uno scostamento scaduto ha un periodo di
  riposo prima di poter essere riproposto, e i rinnovi si contano;
- la pagella del robot è il suo guadagno rispetto alla base **corretto per
  il rischio che ha aggiunto** — non il guadagno nudo, che premierebbe i
  biglietti della lotteria;
- **domanda aperta, sospesa** (vedi in fondo): se anche gli scostamenti
  cripto sono da escludere o solo quelli della base.

In più, un **paracadute sul portafoglio vero**: se la perdita dal picco
supera l'8% (margine sotto il 10%, non a ridosso), gli scostamenti si
azzerano da soli e resta la base. Il termometro viene ricontrollato anche
**a metà mese**: quello mensile, da solo, è cieco sulle tempeste improvvise.

## I numeri della prova storica (10 anni: lug 2016 → lug 2026, senza cripto)

Da 100.000, commissioni incluse, ribilancio mensile, cash che rende un 3%
medio annuo (approssimazione: 0-2% nei primi anni, 4-5% negli ultimi), nessuno
sguardo al futuro (verificato da un test automatico), verificato anche su tre
spezzoni separati del decennio (non un solo percorso):

| termometro | 100k diventano | annuo | max perdita storica |
|---|---|---|---|
| 2,5% | 153.150 | 4,4% | −5,4% |
| 3,0% | 156.940 | 4,6% | −6,5% |
| 3,5% | 160.770 | 4,9% | −7,6% |
| **4,0% — scelta raccomandata** | **164.630** | **5,1%** | **−8,7%** |
| 4,5% | 168.290 | 5,4% | −9,8% |
| 5,0% | 171.340 | 5,5% | −10,8% |
| 5,5% | 174.370 | 5,7% | −11,8% |
| 6,0% | 176.950 | 5,9% | −12,4% |
| 7,0% | 180.930 | 6,1% | −14,7% |

### Come leggerla

**Il 4% è la scelta con margine reale, non al filo del rasoio.** Nel pezzo di
decennio con la crisi Covid (2019-2022, il più duro dei tre testati), il 4%
non ha mai superato −8,7%; il 4,5% arriva a −9,8%, troppo vicino al limite
per un backtest che — per costruzione — tende a essere ottimista sulle
perdite future (dieci anni contengono solo 2-3 tempeste vere, quindi il
peggio storico non è il peggio possibile). Dal 5% in su si sfora il −10% già
nei dati passati: fuori regola.

**Il prezzo di questa scelta è un rendimento modesto: circa il 5% l'anno.**
Non è un difetto del calcolo, è l'aritmetica onesta di tre ingredienti
tradizionali tenuti dentro un vincolo di rischio stretto. Un portafoglio
azioni-obbligazioni-oro non produce il 18% l'anno, quindi la clausola di
deroga al −12% non troverà mai applicazione qui: è bene saperlo prima, non
scoprirlo dopo aver costruito qualcosa che promette il contrario.

**Cosa succede alla resa complessiva del robot dipende ora dagli scostamenti.**
La base da sola rende poco per disegno — è la parte "noiosa e sicura". Il
guadagno più consistente dovrà venire dal lavoro dell'intelligenza sopra la
base (gli scostamenti motivati), un budget di rischio a parte, misurato
separatamente. È esattamente il compito che l'agente ombra sta già
verificando in questi giorni: se quel fiuto esiste davvero.

### Cosa questa prova NON dice (limiti, senza sconti)

- È un solo percorso storico (per quanto testato a pezzi): le perdite
  massime sono stime, non garanzie.
- Il termometro guarda indietro: le tempeste improvvise (marzo 2020)
  colpiscono prima che reagisca. Il controllo settimanale aggiunto su
  richiesta dei giudici riduce ma non elimina questo buco.
- Esecuzione alla chiusura, niente scivolamenti oltre le commissioni,
  niente tasse; il "cash al 3%" è una media piatta, non la serie vera.
- Due dei tre ingredienti (SPY e TLT) oggi **non sono comprabili dal
  robot**: serve accendere l'estensione dell'universo (esiste, spenta) o
  replicare con titoli singoli. I numeri vanno rifatti sugli strumenti
  effettivamente comprabili PRIMA della decisione finale.
- Il backtest gira senza i freni del sistema vero (stop automatici, freno
  ai ripensamenti…). Costruendola davvero, la base avrà bisogno di regole
  proprie (vedi sotto): la perdita massima reale andrà rimisurata nella
  tappa pilota, non promessa oggi.
- Questa è SOLO la base: gli scostamenti del robot si aggiungono sopra, in
  meglio o in peggio.

## Il nodo tecnico più delicato (dai giudici, in una riga)

I freni attuali (stop per posizione, freno ai ripensamenti, tetti per
posizione) sono pensati per **scommesse singole**, e applicati alla base la
farebbero a pezzi: uno stop che vende un pezzo di base + il freno che vieta
di ricomprarlo per 12 ore = base storta per giorni. Costruendola, la base
avrà una corsia propria (operazioni marcate "base", esenti dai freni
pensati per le scommesse, protette invece dal paracadute sul paniere
intero) — e ogni divergenza causata dai freni va contata a parte, non
addebitata al robot.

## Cosa NON cambia (zone rosse)

Per le posizioni **del robot** (gli scostamenti): stop obbligatori, divieto
di allargarli, freno ai ripensamenti — tutto identico. Controlli sui prezzi,
orari di borsa, contabilità: intatti ovunque. La base non è un modo per
rischiare di più: è un modo per rischiare **in ordine**, con un numero
addosso a ogni scelta.

## Come si costruirebbe (tappe, ognuna col suo interruttore)

1. **Modalità ombra della base** (2 settimane): il motore calcola ogni
   giorno la base e la SCRIVE soltanto — zero acquisti. Produce già il
   "costo del fermo" giornaliero misurato contro il portafoglio reale, e
   conta quante volte i freni attuali avrebbero litigato con la base.
2. **Pilota piccolo**: la base governa una fetta minoritaria; il resto come
   oggi. Qui si rimisura la perdita massima CON i vincoli veri.
3. **Regime pieno**: base + scostamenti del robot con budget di rischio.
   Solo se le prime due tappe convincono, e con un tuo sì a ciascuna.

## Le decisioni che restano tue

1. **L'universo**: accendo l'estensione già pronta (SPY/TLT/GLD comprabili)
   o la base va replicata con titoli singoli? (Prima della decisione finale
   i numeri vanno rifatti sugli strumenti scelti.)
2. **Si parte con la tappa 1?** Zero rischio: due settimane di soli numeri.
3. **⏸ Domanda in sospeso (chiesta separatamente, non ancora risposta)**:
   togliere le cripto riguarda solo questa base — con il robot che continua
   a fare trading cripto tattico come scostamento — oppure vuoi ripensare
   anche il pezzo cripto del sistema vero, comprese le tre posizioni aperte
   adesso (AVAX, TRX, ETH) e il ciclo che gira ogni ora giorno e notte?
   Sono due cambi di scala molto diversi: il primo tocca solo questo
   progetto su carta, il secondo tocca capitale reale già investito.
