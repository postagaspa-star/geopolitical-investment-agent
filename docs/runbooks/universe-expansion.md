# Runbook — Universo esteso (EXPANDED_UNIVERSE)

Allargamento dell'universo equity da "core" (S&P 500 + 3 ETF commodity + 14 crypto)
a **core + cintura satellite** (ETF settoriali, single-country/EM, tematici,
materie prime, size/style), per applicare il vantaggio macro/ciclo dove i mercati
sono meno efficienti. Tutto dietro **un solo flag, default OFF**.

## Il flag

- **Env var:** `EXPANDED_UNIVERSE` — `1/true/on/yes` = ON, altrimenti OFF.
- Letto da `universe.expanded_universe_enabled()`.
- **OFF = comportamento storico byte-identico:** prompt del Decision, universo
  dello scan di rotazione (89 ticker), sizing e commissioni invariati.
- Cambiarlo richiede **restart** del processo (il prompt del Decision lo legge a
  import). Su Render: setta la env var e redeploy/restart.

## Cosa cambia quando è ON

| Area | OFF | ON |
|---|---|---|
| Scan rotazione (`rotation_scan`) | 89 ticker | 89 + ~38 satellite, con `adv_usd_20d` |
| Candidati Decision | focus + rotation (testo) | + blocco "UNIVERSO ESTESO" con leader, coppie anti-correlate, satellite liquidi (`candidates.build_candidate_block`) |
| Sizing satellite (`risk_profile`) | — | moltiplicatore 0.3/0.4/0.5 + cap esposizione aggregata 8/15/25% per profilo |
| Slippage (`portfolio`) | solo commissione | + surcharge satellite (`SATELLITE_SLIPPAGE_BPS`, 15bps) |
| Scout news (`data_fetchers`) | 3 GDELT / 2 NewsAPI / watchlist 22 | + query EM/commodity/rotazione + news sui satellite |

I governatori di rischio del core (max posizione, max posizioni, drawdown stop,
range stop-loss, confidence minima) **restano invariati**: il satellite vive
SOPRA di essi, mai al posto loro.

## Sequenza di rollout (gate → paper → decisione)

1. **Gate in backtest (il segnale più veloce).** Da `backend/`:
   ```
   python -m simulator.backtest_expanded_universe 2022-01-01 2024-12-31 8
   ```
   Confronta CORE vs ESTESO su una rotazione momentum con **slippage ADV-aware**.
   Attiva solo se: edge di rendimento ≥ 0 **oppure** la diversità (breadth,
   effective-N) sale **senza** peggiorare Sharpe/maxDD in modo materiale.

2. **Baseline diversità (prima dell'attivazione).** Esegui dove ci sono i dati
   Supabase:
   ```
   python diversity.py
   ```
   Annota breadth / effective-N / satellite_share / off_mega_share di partenza.

3. **Attiva in paper dietro flag.** `EXPANDED_UNIVERSE=1` + restart. L'esecuzione
   è paper (nessun broker reale): rischio reale contenuto.

4. **Shadow-compare.** Il job `diversity_snapshot_job` (scheduler, ogni 6h) logga
   `DIVERSITY_SNAPSHOT` su `agent_logs`. `health_check` espone `universe_mode` e
   `latest_diversity`. Confronta la copertura reale ON vs la baseline e vs il
   backtest per N giorni.

5. **Decisione.** Se diversità ↑ (eN↑, satellite_share↑, off_mega↑) **a parità o
   meglio** di rischio/rendimento → mantieni ON. Altrimenti → OFF (rollback) e
   ritara i parametri.

## Rollback

`EXPANDED_UNIVERSE=0` (o togli la env) + restart. Torna immediatamente al core
byte-identico. Nessuna migrazione dati: il satellite non lascia stato persistente
oltre alle eventuali posizioni già aperte (gestibili con le normali chiusure).

## Parametri tunabili

- `universe.SATELLITE_MIN_ADV_USD` (floor liquidità, default $5M/g; env `SATELLITE_MIN_ADV_USD`).
- `universe.MAX_SATELLITE_CANDIDATES`, `MAX_ROTATION_LEADERS_PER_CATEGORY`, `ROTATION_PAIR_MIN_SPREAD_PCT`.
- `risk_profile` per profilo: `satellite_sizing_mult`, `max_satellite_exposure_pct`.
- `portfolio.SATELLITE_SLIPPAGE_BPS` (surcharge live).
- `universe.SATELLITE` (composizione della cintura).

## Test

```
python -m pytest tests/test_universe.py tests/test_liquidity.py \
  tests/test_candidates.py tests/test_risk_satellite.py \
  tests/test_backtest_expanded.py tests/test_diversity.py -q
```
