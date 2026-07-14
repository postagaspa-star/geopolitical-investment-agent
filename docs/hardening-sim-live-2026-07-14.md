# Hardening Sim+Live — branch `feat/sim-live-hardening` (14/07/2026)

Implementazione completa dei fix dell'analisi del 13/07 (108 run Simulator
22/06→13/07, 61 trade Live). Base: `origin/main` @ ee2f487. Un commit per
step, suite verde a ogni step (baseline 403 → finale 487 test).

## Cosa cambia

| Step | Cosa | Stato al deploy |
|------|------|-----------------|
| 1 | **Conviction reale** nei sim_runs (era hardcoded MEDIA in entrambi gli engine v2). Aggregazione: trade a max allocazione del primo step con trade; `None` per run senza trade. | Attivo |
| 2 | **Outcome v2**: verde/giallo/rosso dal confronto col *benchmark ombra a pari esposizione* (shadow = Σ esposizione_step × rendimento benchmark). Zona morta ±0.4pt; HOLD puro giudicato sul contesto (bench ≤−1.5% verde, ≥+1.5% rosso); override P&L ≤−3% → rosso. Legacy salvato in `full_data.outcome_legacy`, input in `full_data.outcome_v2_inputs`. | Attivo |
| 3 | **Benchmark crypto**: monkey (asset random, seed=run_id, stessa esposizione dell'agente) in `perf_monkey_1m`/`delta_monkey` (prima sempre 0) + buy&hold del main asset in `full_data.crypto_benchmarks`. | Attivo |
| 4 | **Riclassificazione retroattiva**: `backend/tools/reclassify_outcomes.py` (dry-run default, `--apply` per scrivere; `outcome_legacy` mai sovrascritto). Nuova `sim_db.update_run_outcome`. | Manuale |
| 5 | **Stop enforcement sim**: `stop_loss_target` numerico nello schema trade dei due engine, salvato sulla posizione, eseguito PRE-turno da `enforce_sim_stops` (tag `executed_stop_loss_sim`, avviso all'LLM). | Flag OFF |
| 6 | **execution_type su trades** (ai/scalper/orchestrator/airbag/partial_sl/partial_tp/stop_enforced/auto_exit/fail_closed/liquidation) + confidence normalizzata (frazioni 0-1 riscalate, `None` per i meccanici — prima 0/100 inquinavano le analisi). Nuova `get_recent_trades_by_ticker`. | Attivo* |
| 7 | **Fix loop fail-closed**: se la posizione ha già uno SL valido, il rifiuto del cricchetto NON chiude più la posizione (teneva il loop SOL 29/06: 4 trade in 34s). + **Cooldown ri-entrata** per-ticker dopo chiusure meccaniche (default 60 min). | Attivo |
| 8 | **Isteresi anti-inversione**: invertire direzione su ticker tradato dall'AI entro 12h richiede `thesis_invalidation` (≥30 char) o confidence ≥80. Chiusure mai bloccate. Nel sim: solo regola nel prompt. | Flag OFF |
| 9a | **Calibrazione conviction** iniettata nell'advice block (tabella conviction→esito dalle run reali; auto-skip sotto 10 run per classe). | Attivo |
| 9b | **Trend participation**: sim → scenario bull_cycle con cap parser 50% e guida esposizione ≥50%; live → regime BTC UPTREND (airbag) + cap crypto ×1.5 (max 30% NAV), prompt e governor da un'unica funzione. | Flag OFF |
| 9c | **Debrief allineato**: il debrief riceve shadow/delta_eq/esposizione e deve essere coerente col colore. | Attivo |

\* Step 6: il codice è resiliente anche senza migrazione (retry senza colonna), ma va applicata per avere i dati.

## Flag e settings (tutti via `settings`, `get_setting`)

| Chiave | Default | Cosa fa |
|--------|---------|---------|
| `sim_stop_enforcement_enabled` | `false` | Esecuzione automatica degli stop dichiarati nel sim |
| `reentry_cooldown_minutes` | `60` | Cooldown ri-apertura post chiusura meccanica (`0` disattiva) |
| `anti_inversion_enabled` | `false` | Isteresi anti-inversione live |
| `anti_inversion_hours` | `12` | Finestra dell'isteresi |
| `sim_trend_participation_enabled` | `false` | Cap 50% + guida esposizione negli scenari bull del sim |
| `live_trend_participation_enabled` | `false` | Cap crypto ×1.5 con regime BTC UPTREND nel live |

## Sequenza di rilascio (per Andrea)

1. **PRIMA del deploy**: Supabase Dashboard → SQL Editor → esegui
   `backend/migrations/add_execution_type.sql` (idempotente; backfill dei
   tag storici + riparazione confidence frazionarie incluse).
2. Merge del branch `feat/sim-live-hardening` su main → deploy Render.
3. **DOPO il deploy**: riclassificazione storico (facoltativa ma consigliata):
   ```
   SUPABASE_URL=... SUPABASE_KEY=... python backend/tools/reclassify_outcomes.py          # dry-run
   SUPABASE_URL=... SUPABASE_KEY=... python backend/tools/reclassify_outcomes.py --apply  # applica
   ```
4. Accendi i flag quando vuoi provarli (consiglio: prima
   `sim_stop_enforcement_enabled` e `sim_trend_participation_enabled` nel
   sim, osservi qualche run, poi valuti i flag live).

## Rollback

- Outcome v2: gli esiti vecchi restano in `full_data.outcome_legacy`
  (lo script di riclassificazione non li sovrascrive mai).
- `execution_type`: `ALTER TABLE trades DROP COLUMN execution_type;`
- Flag: riportali a `false`.
- Fix fail-closed: revert del commit `fix(live): niente fail-closed...`
  (il comportamento vecchio chiudeva SEMPRE al rifiuto dello SL).

## Note tecniche

- Il calcolo shadow è additivo per step (`compute_shadow_return_pct`),
  adeguato su run brevi; esposizione netta = 1 − cash/total (short →
  negativa, clamp ±1.5).
- Il monkey benchmark è deterministico per run (`random.Random(run_id)`).
- `runner.py` (motore V1) NON è stato toccato: i suoi benchmark restano
  costanti finte, marcate `LEGACY` nel codice.
- Test: `python -m pytest backend/tests/ -q` — 487 passed.
