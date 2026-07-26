# Progetto "posizione normale" — Fase 3, su carta

**Fine luglio 2026 — nessuna riga di questo progetto è attiva sul sistema vero.**
Questo documento è passato da tre giudici critici indipendenti prima di
arrivare a te; le loro obiezioni sono integrate (e una mia svista corretta).
Serve a una cosa sola: darti numeri e scelte per decidere se costruirlo.

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

## Come funziona la base (in parole)

- **Quattro ingredienti che si muovono in modo diverso**: azioni americane,
  obbligazioni lunghe, oro, e una fetta di cripto con un **tetto fisso al
  10%** — tetto che vale sul portafoglio **finale**, scostamenti del robot
  inclusi (richiesta esplicita dei giudici: senza questa clausola il robot
  potrebbe ricreare la concentrazione di oggi a colpi di scostamenti
  "piccoli" e rinnovabili).
- **Pesi "al contrario del nervosismo"**: ogni mese, chi ha ballato di più
  negli ultimi 60 giorni pesa di meno. Niente opinioni: solo aritmetica.
- **Un termometro decide quanto investire in totale**: si stima quanto
  ballerebbe il paniere e si scala l'esposizione perché il ballo atteso
  resti al bersaglio. Il resto sta in liquidità — come **risultato di un
  calcolo**, non come stato di riposo gratuito. (E la liquidità rende: vedi
  tabella, la differenza non è piccola.)
- **La trappola evitata**: la base NON la decide il robot. Se la decidesse
  lui, se la metterebbe bassissima e "rispetterebbe l'obiettivo" restando
  fermo. La fissa la formula, punto.

## Cosa resta al robot

Gli **scostamenti** dalla base, con quattro paletti fissati dai giudici:
- il budget di scostamento si misura in **rischio aggiunto**, non in
  percentuale di peso (sennò tanti scostamenti piccoli = una scommessa
  grossa);
- i tetti (cripto 10%, per-ingrediente) valgono **dopo** gli scostamenti;
- **niente rinnovo automatico**: uno scostamento scaduto ha un periodo di
  riposo prima di poter essere riproposto, e i rinnovi si contano;
- la pagella del robot è il suo guadagno rispetto alla base **corretto per
  il rischio che ha aggiunto** — non il guadagno nudo, che premierebbe i
  biglietti della lotteria.

In più, un **paracadute sul portafoglio vero**: se la perdita dal picco
supera una soglia (~10%), gli scostamenti si azzerano da soli e resta la
base. E il termometro viene ricontrollato anche **a metà mese** (una volta a
settimana): quello mensile, da solo, è cieco sulle tempeste improvvise.

## I numeri della prova storica (10 anni: lug 2016 → lug 2026)

Da 100.000, commissioni incluse, ribilancio mensile, nessuno sguardo al
futuro (verificato da un test automatico). "Cash al 3%" = la liquidità non
investita rende un 3% annuo medio — approssimazione dichiarata (0-2% nei
primi anni, 4-5% negli ultimi):

| strategia | 100k diventano | annuo | max perdita |
|---|---|---|---|
| Base, termometro 4% | 160.330 | 4,8% | **−10,0%** |
| Base, termometro 5% | 179.570 | 6,0% | −12,4% |
| **Base, termometro 5% + cash al 3%** | **204.520** | **7,4%** | **−11,5%** |
| Base, termometro 7% | 213.570 | 7,9% | −16,9% |
| Base, termometro 7% + cash al 3% | 230.620 | 8,8% | −16,0% |
| Base 7% **senza** cripto | 170.350 | 5,5% | −15,4% |
| Base 7%, finestra 90 giorni | 220.330 | 8,2% | −17,1% |
| Base, termometro 9% | 240.210 | 9,2% | −20,9% |
| "Oggi": 25% Bitcoin + 75% fermo | 477.920 | 17,0% | −31,9% |
| "Oggi" + cash al 3% | 597.940 | 19,7% | −30,3% |
| Tutto azioni (SPY) | 340.230 | 13,1% | −34,1% |
| Classico 60/40 | 182.840 | 6,2% | −28,2% |

### Come leggerla (le quattro cose che contano)

**1. Il tuo limite del 12% ha un prezzo — e va preso con margine.** Qui devo
correggere la prima versione di questo documento: il termometro al 5% NON
"rispetta" il limite — lo sfora (−12,4%). E i giudici ricordano una regola
generale: la perdita massima futura è quasi sempre PEGGIORE di quella del
passato, perché dieci anni contengono solo 2-3 tempeste. Se il 12% è un
limite **duro**, serve il termometro al 4% (perdita storica −10%, con un po'
di margine) e la resa onesta da aspettarsi è il 5-6% l'anno con il cash che
rende. Se invece accetti che il 12% sia un limite **indicativo**, il 5% +
cash (7,4% annuo, −11,5%) è l'equilibrio migliore della tabella.

**2. La liquidità che rende cambia i numeri.** È l'obiezione più concreta
dei giudici alla prima versione: una strategia che tiene metà del capitale
in liquidità non può contare quella liquidità come "zero". Col cash al 3%,
il termometro 5% passa da 6,0% a 7,4% annuo. (Nota per il futuro: nel
sistema vero questo significa che la liquidità della base andrebbe
parcheggiata in qualcosa che rende, non lasciata sul conto.)

**3. La riga "Oggi" è un biglietto vincente, non una strategia — e ora c'è
anche la controprova.** Il 17-20% annuo viene dall'aver tenuto il 25% nel
biglietto vincente del decennio (Bitcoin), col senno di poi; la stessa
struttura con le cripto perdenti (Luna, FTX) sarebbe stata una rovina, e la
perdita massima (−32%) rompe la tua regola di 2,5 volte. La controprova è la
riga "senza cripto": togliendo il 10% di Bitcoin dalla base, la resa scende
da 7,9% a 5,5% — perfino dentro la base, una fetta del risultato del
decennio è merito del biglietto vincente. Va saputo, non nascosto.

**4. Il termometro batte le ricette fisse, e non è un colpo di fortuna dei
parametri.** La base al 7% rende più del 60/40 (7,9% contro 6,2%) con
perdita massima molto minore (−17% contro −28%). E cambiando la finestra di
misura da 60 a 90 giorni il risultato quasi non si muove (8,2%, −17,1%):
segno che non abbiamo pescato il numero fortunato.

### Cosa questa prova NON dice (limiti, senza sconti)

- È un solo percorso storico: le perdite massime sono stime, non garanzie.
- Il termometro guarda indietro: le tempeste improvvise (marzo 2020)
  colpiscono prima che reagisca. Il controllo settimanale aggiunto su
  richiesta dei giudici riduce ma non elimina questo buco.
- Esecuzione alla chiusura, niente scivolamenti oltre le commissioni,
  niente tasse; il "cash al 3%" è una media piatta, non la serie vera.
- Due dei quattro ingredienti (SPY e TLT) oggi **non sono comprabili dal
  robot**: serve accendere l'estensione dell'universo (esiste, spenta) o
  replicare con titoli singoli. I giudici chiedono — giustamente — di
  rifare i numeri sugli strumenti effettivamente comprabili PRIMA della
  decisione finale.
- Il backtest gira senza i freni del sistema vero (stop automatici, freno
  ai ripensamenti…). Costruendolo davvero, la base avrà bisogno di regole
  proprie (vedi sotto): la perdita massima reale andrà rimisurata nella
  tappa pilota, non promessa oggi.
- Questa è SOLO la base: gli scostamenti del robot si aggiungono sopra, in
  meglio o in peggio — è esattamente ciò che l'agente ombra sta misurando.

## Il nodo tecnico più delicato (dai giudici, in una riga)

I freni attuali (stop per posizione, freno ai ripensamenti, tetti per
posizione) sono pensati per **scommesse singole**, e applicati alla base la
farebbero a pezzi: uno stop che vende un pezzo di base + il freno che vieta
di ricomprarlo per 12 ore = base storta per giorni. Costruendola, la base
avrà una corsia propria (operazioni marcate "base", esenti dai freni
pensati per le scommesse, protette invece da un paracadute sul paniere
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

## Le tre decisioni che sono tue

1. **Il limite di perdita**: il 12% è duro (→ termometro 4%, resa attesa
   5-6% con cash) o indicativo (→ termometro 5% + cash, resa attesa ~7%,
   sforamenti possibili)? Oppure lo alzi consapevolmente?
2. **L'universo**: accendo l'estensione già pronta (SPY/TLT/GLD comprabili)
   o la base va replicata con titoli singoli? (Prima della decisione finale
   i numeri vanno rifatti sugli strumenti scelti.)
3. **Si parte con la tappa 1?** Zero rischio: due settimane di soli numeri.
