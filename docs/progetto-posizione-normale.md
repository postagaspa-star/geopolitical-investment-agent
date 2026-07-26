# Progetto "posizione normale" — Fase 3, su carta

**Fine luglio 2026 — nessuna riga di questo progetto è attiva sul sistema vero.**
Passato da tre giudici critici indipendenti e ricalibrato più volte nel
confronto con Andrea. Impostazione attuale: cripto **complementari** (tetto
10%, non più il centro della strategia) e limite di perdita massima **alzato
consapevolmente a −20%** (prima: quasi mai sotto −10%).

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

## Le scelte di Andrea che calibrano il progetto

- **Cripto complementari, non dominanti**: restano nel paniere col tetto al
  10% del portafoglio. La spinta iniziale a puntarci sopra (guadagnare di
  più scommettendo sulla bravura matematica dell'AI invece che
  sull'interpretazione delle notizie) è stata ridimensionata.
- **Limite di perdita: −20%**, alzato consapevolmente dopo aver visto che il
  vecchio limite (−10%) comprimeva la resa attesa al 6,6% l'anno. La vecchia
  clausola "fino a −12% se il potenziale supera +18%" è superata — e
  comunque non sarebbe mai scattata: questa base non arriva al +18% nemmeno
  spingendo il rischio oltre ogni regola.

## Come funziona la base (in parole)

- **Quattro ingredienti che si muovono in modo diverso**: azioni americane,
  obbligazioni lunghe, oro, e cripto con un **tetto fisso al 10%** — tetto
  che vale sul portafoglio finale, scostamenti del robot inclusi (senza
  questa clausola il robot potrebbe ricreare la concentrazione di oggi a
  colpi di scostamenti "piccoli" e rinnovabili).
- **Pesi "al contrario del nervosismo"**: ogni mese, chi ha ballato di più
  negli ultimi 60 giorni pesa di meno. Niente opinioni: solo aritmetica.
- **Un termometro decide quanto investire in totale**: si stima quanto
  ballerebbe il paniere e si scala l'esposizione perché il ballo atteso
  resti al bersaglio. Il resto sta in liquidità — come **risultato di un
  calcolo**, non come stato di riposo gratuito (e la liquidità rende).
- **La trappola evitata**: la base NON la decide il robot. Se la decidesse
  lui, se la metterebbe bassissima e "rispetterebbe l'obiettivo" restando
  fermo. La fissa la formula, punto.

## Cosa resta al robot

Gli **scostamenti** dalla base, con i paletti fissati dai giudici:
- il budget di scostamento si misura in **rischio aggiunto**, non in
  percentuale di peso;
- i tetti (cripto 10% incluso) valgono **dopo** gli scostamenti;
- **niente rinnovo automatico**: uno scostamento scaduto ha un periodo di
  riposo prima di poter essere riproposto, e i rinnovi si contano;
- la pagella del robot è il suo guadagno rispetto alla base **corretto per
  il rischio che ha aggiunto**, non il guadagno nudo.

In più, un **paracadute sul portafoglio vero**: se la perdita dal picco
supera il **16%** (margine sotto il limite del 20%, non a ridosso), gli
scostamenti si azzerano da soli e resta la base. Il termometro viene
ricontrollato anche **a metà mese**: quello mensile, da solo, è cieco sulle
tempeste improvvise.

## I numeri della prova storica (10 anni: lug 2016 → lug 2026)

Da 100.000, commissioni incluse, ribilancio mensile, cash che rende un 3%
medio annuo (approssimazione dichiarata), nessuno sguardo al futuro
(verificato da un test automatico), controllato anche su tre spezzoni
separati del decennio:

| termometro | 100k diventano | annuo | max perdita storica |
|---|---|---|---|
| 4,0% (vecchia regola −10%) | 188.860 | 6,6% | −9,2% |
| 6,0% | 219.000 | 8,2% | −13,7% |
| 7,0% | 230.620 | 8,8% | −16,0% |
| **8,0% — scelta raccomandata** | **241.140** | **9,2%** | **−18,4%** |
| 8,5% (a filo del limite) | 245.650 | 9,4% | −19,5% |
| 9,0% (sfora) | 250.360 | 9,7% | −20,4% |
| 10,0% (sfora) | 260.000 ca. | 10,0% | −22,2% |

### Come leggerla

**L'8% è la scelta coerente col nuovo limite.** Perdita massima storica
−18,4%: margine sotto il −20% nella stessa proporzione che avevamo usato per
la vecchia regola (il backtest è ottimista per costruzione: dieci anni
contengono solo 2-3 tempeste, il peggio futuro tende a superare il peggio
passato). L'8,5% arriva a −19,5%: a filo, scartato. La robustezza regge
anche qui: cambiando la finestra di misura da 60 a 90 giorni i numeri quasi
non si muovono (9,5% annuo, −18,6%).

**Cosa compra davvero il rischio in più — attenzione, non è proporzionale.**
Raddoppiare la tolleranza di perdita (da −10% a −20%) NON raddoppia la
resa: si passa dal 6,6% al 9,2% l'anno, cioè +2,6 punti. E oltre non c'è
quasi niente da comprare: sopra il termometro ~9% la formula smette di
mordere, perché l'esposizione è già al 100% in metà dei mesi e il sistema
non usa leva. **Questa base ha un tetto naturale intorno al 10% l'anno**,
qualunque rischio si accetti. Sul decennio, la scelta nuova produce ~52.000
in più della vecchia (241.000 contro 189.000).

**Con più spazio di rischio, anche la cripto respira di più.** Al termometro
8% la base sta investita in media all'83% (contro il 46% di prima) e la
fetta cripto effettiva sale al 5,7% medio del patrimonio, toccando il tetto
del 10% nei periodi tranquilli. Complementare, ma non più simbolica.

**Il confronto onesto con le alternative.** Tutto-azioni ha reso di più
(13,1%) ma con −34% di perdita massima: fuori anche dal nuovo limite, e con
anni interi a −21%. La vecchia posizione "25% Bitcoin" (17-20% annuo, −30%)
resta fuori regola pure lei. La base all'8% è il punto più alto
raggiungibile **dentro** la tua regola con questi ingredienti.

### Cosa questa prova NON dice (limiti, senza sconti)

- È un solo percorso storico (per quanto testato a pezzi): le perdite
  massime sono stime, non garanzie. Un −20% di limite significa accettare
  che un giorno il conto possa segnare **−18% e passa davvero**: è bene
  immaginarselo su 100.000 euro (−18.400) prima, non durante.
- Il termometro guarda indietro: le tempeste improvvise colpiscono prima
  che reagisca. Il controllo settimanale riduce ma non elimina questo buco.
- Esecuzione alla chiusura, niente scivolamenti oltre le commissioni,
  niente tasse; il "cash al 3%" è una media piatta, non la serie vera.
- Due dei quattro ingredienti (SPY e TLT) oggi **non sono comprabili dal
  robot**: serve accendere l'estensione dell'universo (esiste, spenta) o
  replicare con titoli singoli. I numeri vanno rifatti sugli strumenti
  effettivamente comprabili PRIMA della decisione finale.
- Il backtest gira senza i freni del sistema vero: la perdita massima reale
  andrà rimisurata nella tappa pilota, non promessa oggi.
- Questa è SOLO la base: gli scostamenti del robot si aggiungono sopra, in
  meglio o in peggio — è ciò che l'agente ombra sta misurando.

## Un'osservazione a parte, non un'azione da fare ora

Il principio "cripto complementari, non dominanti" descrive la base. Oggi
però il portafoglio vero è investito al 100% in cripto (le tre posizioni
aperte — AVAX, TRX, ETH — sono tutte cripto). Non è qualcosa che ho toccato
né che serve decidere adesso: è un fatto da tenere a mente per quando si
deciderà come far convivere la base col comportamento del robot dal vivo.

## Il nodo tecnico più delicato (dai giudici, in una riga)

I freni attuali (stop per posizione, freno ai ripensamenti, tetti per
posizione) sono pensati per **scommesse singole**, e applicati alla base la
farebbero a pezzi. Costruendola, la base avrà una corsia propria (operazioni
marcate "base", esenti dai freni pensati per le scommesse, protette invece
dal paracadute sul paniere intero) — e ogni divergenza causata dai freni va
contata a parte, non addebitata al robot.

## Cosa NON cambia (zone rosse)

Per le posizioni **del robot** (gli scostamenti): stop obbligatori, divieto
di allargarli, freno ai ripensamenti — tutto identico. Controlli sui prezzi,
orari di borsa, contabilità: intatti ovunque.

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
