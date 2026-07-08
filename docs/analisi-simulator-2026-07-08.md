# Analisi Simulator — 8 luglio 2026

Diagnosi completa dei 4 problemi segnalati (pipeline scenari, run autonome,
database che si svuota, qualità del ragionamento) con fix applicati e
raccomandazioni sui blocchi nei prompt.

---

## 1. Cosa era rotto e perché — catena dei guasti

La pipeline scenari non falliva per UNA causa ma per TRE, sovrapposte in
sequenza (per questo sembrava "a volte funziona, a volte no"):

| Periodo | Causa | Sintomo |
|---|---|---|
| **15/06 →** | Middleware `_admin_token_guard` (aggiunto ~14-15/06 per chiudere i critici di sicurezza) blocca con **401** tutti i POST senza `x-admin-token` — compreso l'upload scenari di GitHub Actions che si autentica col SUO `X-Scenario-Token` | Ultimo upload riuscito: **14/06 15:50**. Da lì ogni run del generator respinto |
| **22-29/06** | **4 OOM** (container 512MB): 22, 24, 26, 29 giugno | Finestre di downtime → 503 sugli upload, run autonome uccise a metà |
| **30/06 → 08/07** | **Sospensione billing Render** (risolta da te oggi alle 15:13) | Servizio completamente spento per 8 giorni: 503 su tutto, zero run autonome |

Il carico che ha contribuito agli OOM era a sua volta un bug: **il cap
giornaliero delle run autonome non è mai stato applicato** — `runs_today()`
interroga la tabella `sim_runs` che su questo Supabase **non è mai esistita**
(vedi §3), l'eccezione veniva inghiottita e tornava 0. Risultato: **~48 run
al giorno** (una ogni 30 minuti H24) invece delle 5 configurate. Il commento
nel workflow ("Con ~40-50 run/giorno del Simulator...") documentava il bug
credendolo una caratteristica.

### Fix applicati (deployati su main, 367 test verdi)

- **Guard admin**: esenzione mirata `("POST", "/api/simulator/scenarios/dynamic")`
  — l'endpoint resta autenticato col suo token dedicato. Test di regressione aggiunto.
- **`build_scenarios.py`**: gate `/health` PRIMA di generare (se il backend è
  giù: exit 50, zero costi DeepSeek); POST con 5 tentativi che tra un retry e
  l'altro **aspettano** che `/health` torni 200 (fino a 90s) invece dei vecchi
  3 retry in ~10 secondi che morivano sempre dentro la finestra di restart.
- **`daily-scenarios.yml`**: health check che attende fino a 4 minuti il
  backend (prima accettava il 503 e procedeva a bruciare chiamate LLM).
- **Cap giornaliero reale**: contatore `_sim_auto_runs_count::{giorno}` in
  `sim_settings`, incrementato a ogni `insert_run` auto — `runs_today()` lo usa
  quando `sim_runs` non è interrogabile. Da ora le run auto sono davvero max 5/giorno.
- **Trim RAM post-run**: `malloc_trim` subito dopo ogni run autonoma (gli OOM
  di fine giugno cadevano nelle ore delle run; il job periodico da 30' resta
  come backstop).

---

## 2. Run autonome — perché "non sempre funzionavano"

1. **Servizio giù** (OOM + sospensione): lo scheduler APScheduler vive dentro
   il processo — servizio spento = niente tick da 30 minuti. Risolto (billing
   + mitigazioni OOM sopra).
2. **Cap rotto** → 48 run/giorno: contribuiva agli OOM che uccidevano le run
   successive. Risolto.
3. **Oggi l'auto-mode è SPENTO** (`auto_mode_enabled=false`): riaccenderlo è
   un toggle nella SimDashboard (l'endpoint POST richiede l'admin token, quindi
   non potevo farlo io). Il lock soft e l'alternanza equity/crypto sono sani.

---

## 3. Database che "si svuota a soglia" — la vera storia

**La tabella `sim_runs` non è MAI esistita su Supabase** (PGRST205 a ogni
query): l'auto-migration del backend richiede `DATABASE_URL` o
`SUPABASE_DB_PASSWORD`, che su Render non ci sono. Da sempre ogni run finisce
nel **fallback key-value** (`_sim_run_fallback::*` dentro `sim_settings`).

Quel fallback ha DUE meccanismi che cancellano dati:

1. **Cap a 300 run**: al superamento, i più vecchi vengono eliminati
   definitivamente. A 48 run/giorno = ~6 giorni di storia → **è la "soglia"
   che vedevi**: il database non si "svuotava", venivano potati i run più
   vecchi in continuazione.
2. **Il purge "anti-bloat"**: `purge_transient_settings()` e la migration
   `cleanup_settings_bloat.sql` includevano `_sim_run_fallback::*` nei
   prefissi da cancellare di default — trattando lo STORICO come stato
   effimero. Questo ha azzerato la storia (oggi restano **4 run**, 17-22
   giugno; le ~90+ run di maggio-giugno referenziate dalla memoria advice
   sono irrecuperabili — verificato ID per ID via API).

### Fix applicati

- `_sim_run_fallback::` **rimosso dai prefissi di default del purge**
  (db_supabase + db_sqlite + test di regressione "il purge default non tocca
  lo storico").
- Cap fallback **300 → 1000** (le chiavi vivono in `sim_settings` dedicata:
  niente più bloat della `settings` Live né truncation PostgREST delle config).
- **`backend/migrations/create_sim_runs.sql`** — da eseguire una volta sul
  Dashboard Supabase (unico passo manuale, vedi §6): crea la tabella vera,
  persistenza illimitata.
- **Auto-migrazione al boot** (`migrate_fallback_runs_to_table`): quando
  `sim_runs` esiste, il backend travasa da solo tutti i run del fallback nella
  tabella — scansionando per prefisso sia `sim_settings` sia la `settings`
  Live (recupera anche eventuali orfani fuori indice). Idempotente.

---

## 4. Analisi del ragionamento (400 lezioni + 4 run complete)

Lo storico run è perso, ma la **memoria advice** (400 lezioni distillate,
13/05→22/06) e le 4 run superstiti con reasoning step-by-step bastano per una
diagnosi solida.

### 4a. I numeri (campione di 90 run ricostruito dai riassunti [Auto])

| Categoria | Win rate | P&L medio |
|---|---|---|
| crash_rally | **45%** | +0.12% |
| macro | **42%** | -0.24% |
| crash | 33% | -0.71% |
| normale | 9% | -0.81% |
| sideways | 9% | -0.64% |
| regulatory_event | 9% | -0.29% |
| **bull_cycle** | **0%** | -0.92% |
| **geopolitico** | **0%** | -0.44% |
| **TOTALE (n=90)** | **19%** | **-0.50%** |

**Il pattern centrale: l'AI è brava nelle crisi e perde nei regimi tranquilli
e rialzisti.** La postura difensiva (cash alto, size timide) paga quando il
mercato crolla e strangola il rendimento quando il mercato sale — e
bull/normale sono i regimi più frequenti nella realtà. In bull_cycle il
benchmark è di fatto imbattibile per un agente che sta 50-85% cash: 11 run
su 12 YELLOW.

Aggravante: **lo stesso scenario viene rigiocato decine di volte** (pool
piccolo, scelta random): "Ethereum Merge" 11 volte — sempre negativo — con 8
lezioni "sell the news" già in memoria che non hanno corretto il
comportamento. "Estate 2024 range-bound" 13 volte. Le run si accumulano sugli
stessi 3-4 scenari e la memoria si riempie di duplicati.

### 4b. Dove sbaglia il ragionamento (dalle 4 run step-by-step)

1. **Momentum-chasing su rumore a 2 giorni, contro la propria tesi**: in
   entrambe le run sideways dichiara al T0 "ETF ETH = sell-the-news, evito
   ETH" e compra ETH al T1/T2 sul rimbalzo (+1.3%, +2.9%). 3 entrate su 4 su
   massimi locali.
2. **Contraddizioni tra step mai dichiarate**: "chiudo USO" → step dopo
   "mantengo USO, contesto favorevole", senza mai una frase "cambio idea
   perché X". La tesi precedente viene riscritta, non confrontata.
3. **Uscite senza criteri operativi**: stop "mentali" dichiarati all'ingresso
   e mai più menzionati; al culmine (run 3 T6) **l'assenza dello stop diventa
   la giustificazione per non tagliare** ("né motivi per tagliare in assenza
   di uno stop-loss tecnico attivo"). Ingressi sempre motivati, uscite mai
   pre-definite numericamente.
4. **Regole usate come retorica**: la stessa direttiva ("SOL sopra $66") è
   conferma d'ingresso quando vuole comprare e "soglia obsoleta" quando deve
   valutare; la regola "max 30%" letta come "totale" al T2 e "per asset" al
   T3 della stessa run, a seconda della voglia di trade.
5. **Conferme inventate**: cita RSI 4H, volumi, "stop dinamico di sistema già
   attivo" — dati e guardrail che NON esistono nel feed (che contiene solo
   prezzi e headline). Auto-autorizzazione con fatti fabbricati.
6. **Disposition effect**: vende il vincitore (+3.5% SOL) e tiene i perdenti
   ("non mostra segnali di inversione immediata"), conviction ALTA sulle mosse
   peggiori e BASSA sulle migliori.
7. **Sottoesposizione cronica**: conviction ALTA + R/R 2.0 dichiarato → size
   25% e 65% di cash mai toccato in 4 step (run macro GREEN ma +0.66% su un
   movimento settoriale del +18%). Il cash alto salva le run sbagliate e
   castra quelle giuste: un unico dial usato come surrogato della gestione
   del rischio.

Cosa fa bene: **lettura del regime quasi sempre corretta** (le tesi del T0
sono le migliori di ogni run), difesa del capitale nei crash da manuale
(100% cash per 3 step durante il depeg Luna), hold ben motivati quando il
formato di output è completo.

**Due bug di piattaforma scoperti dall'analisi (entrambi fixati e deployati):**

- **Trade decisi e persi in silenzio**: output LLM troncato dal cap
  `max_tokens=4500` a metà del JSON → il parse produceva un finto hold
  (run macro T1: tre trade decisi, mai eseguiti, nessun errore). Ora:
  `finish_reason=length` → retry con budget 6500; JSON dei trade presente ma
  corrotto → un retry, poi **errore visibile**, mai hold silenzioso.
- **Flip long→short involontari**: l'AI esprime il SELL in % del capitale; il
  close di una posizione da 11.4% scritto "11.5%" apriva un micro-short col
  residuo (run crash T4: residui short 0.125 SOL / 0.47 DOT etichettati
  "Exit...to reduce risk"). Ora eccesso ≤30% della posizione = chiusura piena
  (com'è scritto nel prompt, regola C4); flip solo se l'eccesso è sostanziale.

---

## 5. I "blocchi": inventario e cosa togliere

Prima la notizia che ribalta l'ipotesi: **i blocchi accumulati sul Live
(PILASTRI, freeze pre-evento, dead-band, blocco short, vincoli risk-profile)
NON arrivano al Simulator V2.** Gli engine V2 hanno prompt self-contained;
`shared_principles.py` è importato solo dal runner V1 legacy. Nel Simulator
i tuoi timori di sovrapposizione valgono quindi per un set più piccolo — ma
i conflitti ci sono, e sono questi:

### Conflitti reali nei prompt del Simulator

1. **C5 "ROTAZIONE OBBLIGATORIA — NON stare in cash" ⟷ C6.A "protocollo
   capitulation — resta in cash finché non vedi ≥2 segnali"**
   (`v2_engine.py:88` vs `:128`). In un crash i due comandi confliggono
   frontalmente; anche C7 ("lista vuota spesso è la scelta migliore")
   rinforza C6.A contro C5. → **Da risolvere**: C5 va subordinato
   esplicitamente ai protocolli di regime ("la rotazione si applica nei
   regimi trend/normale; in crash vale C6.A").
2. **Doppione R/R ≥ 1.5**: sia come principio (C6-RR, riga 179) sia come
   campo obbligatorio dello schema (C7-rr, riga 231). → **Fondere** in uno.
3. **L'engine CRYPTO è privo dei guardrail chiave**: niente gate R/R 1.5,
   niente protocollo capitulation, niente "fai correre i vincitori / taglia i
   perdenti". E le categorie crypto sono le peggiori (bull_cycle 0% win). Il
   campo `rr` risulta compilato solo nelle run equity: la disciplina esiste
   solo dove il prompt la rende rituale. → **Portare C6.A/C6.C/C6-RR anche
   nel prompt crypto** (adattati: soglie di volatilità crypto).
4. **Advice memory sopra le direttive utente**: il blocco lezioni ("non
   regole assolute, deroghe ammesse") è iniettato FISICAMENTE SOPRA il blocco
   "DIRETTIVE UTENTE — PRIORITÀ MASSIMA". Ordine di lettura e autorità
   dichiarata si contraddicono. → **Invertire l'ordine**.
5. **Direttive persistenti con soglie stantie** ("SOL sopra $66" con SOL a
   145): nelle run vengono usate a convenienza e fanno danni. → Le direttive
   utente con valori numerici assoluti dovrebbero avere una scadenza o essere
   espresse in termini relativi (%, non prezzi).

### La memoria advice va potata (è il "blocco" che si è davvero accumulato)

- 400 lezioni, ma **solo le 5 più RECENTI per categoria vengono iniettate**:
  `quality_score` esiste nel modello e **non viene mai calcolato** → la
  selezione è per recency pura, l'ultima lezione vince anche se contraddice
  le 10 precedenti.
- **Contraddizioni frontali conservate fianco a fianco** (stessa categoria):
  "evitare buying-the-dip in crash sistemico, attendi 2-3 settimane" ⟷ "buy
  the capitulation after sharp drop"; "bad news priced-in: aumenta
  l'esposizione" ⟷ "taglia i proxy entro 48h"; "agire subito in escalation
  (entro 48h)" ⟷ "ritardare gli acquisti 2-4 settimane"; "dopo +30% vendi il
  25%" ⟷ prompt C6.C "fai correre i vincitori, non chiudere presto".
  Iniettate a rotazione per recency → comportamento che flippa da run a run.
- **Duplicati di massa**: ~8 varianti di "sell the news sul Merge" (stesso
  scenario rigiocato 11 volte). Occupano slot delle 5 iniettate senza
  aggiungere informazione — e il comportamento non è mai cambiato (11 run,
  tutte negative): la memoria com'è oggi NON chiude il loop.
- I 93 riassunti "[Auto] ... → OUTCOME" salvati come lezioni non sono lezioni:
  sono log. Inquinano i bucket.
- Il suffisso di regime dei bucket è morto: **tutte e 400 le lezioni stanno in
  `__neutral`** — il detector di regime non esce mai da neutral.

**Raccomandazione advice memory (in ordine):**
1. Dedup semantico per titolo/tema (le ~8 "sell the news Merge" → 1).
2. Buttare i riassunti [Auto] dai bucket (o spostarli in un log separato).
3. Quando due lezioni si contraddicono, tenerne UNA sola con la condizione
   discriminante esplicita ("in crash SISTEMICO aspetta stabilizzazione; in
   sell-off da EVENTO SINGOLO già prezzato compra la capitulation") — la
   condizione è il valore, non la regola.
4. Calcolare davvero `quality_score` (es. outcome della run di origine +
   apply_count) e selezionare le 5 per qualità, non per recency.
5. Guardia anti-ripetizione nell'auto-mode: non rigiocare uno scenario già
   giocato N volte di recente (pesare per "least played").

### Cosa NON togliere

- **C6.A (capitulation)** e la postura difensiva nei crash: sono l'unica cosa
  che produce win rate (crash_rally 45%, la migliore categoria). Il problema
  non è la difensività in crisi — è che non c'è NIENTE che spinga
  l'aggressività nei trend (bull 0%). Prima di togliere freni, aggiungere il
  "motore": una regola esplicita di partecipazione al trend (es. "in
  bull/trend confermato l'esposizione target è ≥60%; il cash sopra il 40% va
  motivato ogni step") — è lo stesso identico problema che hai già risolto sul
  Live con il fix [TREND-DOWN] (24/06) e il branch fix/decision-paralysis:
  regimi scritti solo in chiave difensiva → NO_TRADE seriali.
- **Il cap 5 trade/turno e max 30%/posizione crypto**: mai stati il collo di
  bottiglia nelle run analizzate.

---

## 6. Cosa resta da fare a te (2 minuti)

1. **Supabase** (unico passo davvero necessario): Dashboard → SQL Editor →
   incolla ed esegui `backend/migrations/create_sim_runs.sql` → poi su Render
   "Manual Deploy → Restart service" (o aspetta il prossimo deploy). Al boot
   il backend migra da solo i run del fallback nella tabella vera. *(Non
   potevo farlo io: la sessione Supabase nel browser era scaduta e le
   credenziali/DB password non sono in nessun env accessibile.)*
2. **Riaccendere l'auto-mode** dalla SimDashboard (toggle) — ora col cap
   5/giorno funzionante.
3. *(Consigliato)* Render: attivare un alert su fallimento deploy/OOM, e su
   GitHub le notifiche dei workflow falliti — questa rottura è passata
   inosservata per 24 giorni.

## 7. Stato deploy

- 3 commit su `main` (auto-deploy Render): `3a6e190`, `c565a4c`, `ce0e3e4`
  — anche su branch `fix/simulator-pipeline-db`.
- Suite: **367 test verdi** (3 nuovi test di regressione).
- Workflow scenari: ri-testato end-to-end dopo i fix (dispatch manuale).
- Il branch `fix/decision-paralysis` (Live) resta com'era: non toccato, non
  mergiato — è tuo.
