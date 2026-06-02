# Ricerca sul trading automatico crypto — sintesi onesta

> Documento di consolidamento dopo ~150 backtest su dati reali Binance.
> Tutto il codice descritto è committato in locale, **tutti i flag OFF, niente
> in produzione**. Scopo: dare il quadro VERO (inclusi i limiti) prima di
> decidere cosa attivare.

## 1. La domanda di partenza

Costruire un "cervello" che fa trading crypto in automatico per generare
guadagni — idealmente cavalcando i picchi al rialzo e shortando ai ribassi.

## 2. Cosa abbiamo costruito e testato (7 motori, 3 famiglie)

| Motore | File | Idea |
|---|---|---|
| Decision core (R1+core) | `agents/crypto_signal_core.py` | scorer deterministico + LLM narratore |
| Scalper momentum-reversal | `agents/crypto_scalper.py` | intraday, flip long↔short su inversione |
| Scalper aggressivo v2 | (stesso, `.aggressive()`) | conviction-sizing, pyramiding, molto capitale |
| Scalper hybrid | (stesso, `.hybrid()`) | trend-following in su + difensivo in giù |
| Swing trend-following | `agents/crypto_swing.py` | candele 1d, chandelier stop, tiene per giorni |
| Meta-cervello 3 stati | `agents/crypto_regime.py` | UP=tieni / DOWN=scudo / SIDE=cash |
| Mean-reversion | `agents/crypto_meanrev.py` | compra l'eccesso al ribasso nel laterale |

Backtest: `simulator/backtest_*.py` e `stress_test_scalper.py`. Dati reali
Binance, commissioni 10bps/lato, slippage incluso dove indicato.

## 3. I risultati, per regime di mercato

| Famiglia | Salita (uptrend) | Discesa (downtrend) | Laterale |
|---|---|---|---|
| Trend-following / scalper | ❌ perde (edge −15÷−250) | 🟢 protegge | ❌ perde (overtrading) |
| Scudo (meta in downtrend) | — | 🟢🟢 **edge +56** (3/3 bear) | — |
| Mean-reversion | (non opera) | (non opera) | 🟡 ≈ stare fermi (−0,3% medio) |

**Numeri chiave verificati:**
- Scudo in bear: SOL −89% → meta −13÷−33%. BTC −58% → meta −7÷−18%. ETH −53% → −17÷−29%.
- Uptrend: SOL +1127% → ogni motore ha preso una frazione (380% nel migliore dei casi). Nessuno batte il buy&hold.
- Laterale: mean-rev fa 1 trade/periodo, drawdown <1,1%, ma return medio −0,28% (le commissioni di quell'unico trade).

## 4. La conclusione robusta (confermata su ~150 backtest)

> **Il valore aggiunto di un robot, sulle crypto, è UNO solo e robustissimo:
> PROTEGGERE quando il mercato scende. In salita conviene TENERE. Nel laterale
> conviene STARE FERMI.**

Questo perché:
- in salita, catturare un +1000% richiede di NON vendere mai; ogni stop/flip
  erode il movimento (limite strutturale, non di taratura);
- nel laterale, commissioni + falsi segnali > guadagno potenziale;
- in discesa, evitare un −60% vale enormemente, e un filtro di regime lento
  basta per i bear lunghi.

## 5. Il meta-sistema che i dati indicano

```
   UP        →  TIENI (holding)          ← cattura i rialzi come buy&hold
   DOWN      →  SCUDO CASH (esci)        ← l'unico vero valore (+56 edge)
   SIDEWAYS  →  CASH FERMO               ← stare fermi è già ottimale
```

## 6. I LIMITI — cosa NON sappiamo ancora (da leggere prima di attivare)

1. **Scudo short gonfiato dal funding.** Tenere short per mesi costa funding
   ogni 8h, non incluso nei backtest. → **usare la variante CASH** (+56, onesta),
   non short (+63, illusorio).
2. **Flash-crash veloci non testati.** Gli eventi testati erano finestre di 8
   giorni su candele daily. Un −30% in 24-48h lo scudo daily lo vede troppo
   tardi. Il +56 vale per crash LENTI e LUNGHI.
3. **Cherry-picking dei periodi.** Le finestre (2021 bull, 2022 bear) le ho
   scelte io. Manca un test su storico CONTINUO completo (2017→oggi) senza
   selezione.
4. **Sistema completo mai validato end-to-end.** Ho testato i pezzi, non
   "tieni/scudo/cash" come un tutt'uno su storico lungo: il whipsaw UP↔SIDE
   potrebbe ancora erodere valore.

## 7. L'architettura proposta per lo scudo (a DUE velocità)

Per chiudere il buco #2 (flash-crash), lo scudo non deve usare un solo segnale:

- **Sensore LENTO** (regime daily, `crypto_regime`): per i bear lunghi.
- **Grilletto RAPIDO d'emergenza** (candele 1h/4h): un crollo brusco attiva lo
  scudo all'istante, bypassando i 6 giorni di conferma. Gli indicatori intraday
  dello **scalper** (apparentemente inutili come motore) servono qui come
  SENSORE di velocità. Aggancio naturale: `risk_state` (circuit breaker già
  presente ma di fatto spento).

Così nessun lavoro è sprecato: scalper = sensore rapido, swing/regime = sensore
lento, meta = orchestratore.

## 8. Stato del codice

- Branch `main`, commit locali da `0e17daf` a `9f0ab55` (+ questo doc).
- **NON pushato in produzione. Tutti i flag OFF** (`crypto_scalper_enabled`,
  `crypto_decision_engine=core_bound` è l'unico attivo e riguarda il decisore
  R1, non gli scalper/swing).
- Test: 100+ unit test verdi su tutti i motori.
- Governatori di rischio (`risk_profile`, `risk_state`) preservati.

## 9. Prossimi passi (ordine consigliato)

1. **Rifinire il meta** "tieni/scudo-cash/cash" e validarlo end-to-end su
   storico continuo lungo (chiude il buco #4).
2. **Potenziare lo scudo** con il grilletto rapido d'emergenza intraday
   (chiude il buco #2) e testarlo su flash-crash reali (COVID mar-2020,
   crollo ago-2024 24h).
3. Solo dopo, e solo su decisione esplicita, valutare l'attivazione live
   (gate sul comportamento di produzione).

---

# AGGIORNAMENTO FINALE (fase rotazione + scudo settimanale + conferma fuori campione)

## 10. La rotazione multi-asset (selezione su universo, non cherry-pick)

Per togliere il cherry-picking (buco #3), il sistema valuta TUTTE le major a
ogni step e sceglie da solo quante/quali (`crypto_selector.py`). DUE scoperte
metodologiche fondamentali:

- **Backtest "ballerino" → reso deterministico.** L'allineamento dati con union
  dei timestamp dava +349% o +287% sugli stessi input. Fix: timeline master +
  forward-fill (`backtest_portfolio.py:_align`). CONSEGUENZA: l'edge della
  rotazione momentum crollava da +156 (artefatto) a ~+12 reale. Sul periodo
  recente pulito (2024+) la rotazione momentum PERDE (edge 14-major −59,
  50-major −29). **La rotazione momentum NON ha edge.**
- **Universo 50 peggiora il survivorship bias** (top-50 di oggi sul passato
  esclude i morti: LUNA era top-10). Va testato solo sul recente. Sulle 50 nel
  recente: overtrading -76%. Confermato: piu' ampio ≠ meglio.

## 11. Lo SCORER STRUTTURALE e il timeframe (intuizione di Andrea)

- **Struttura+volume > RSI/EMA**, su 4h: `crypto_structural.py` (breakout
  Donchian + conferma volume). Sul recente 4h/14-major: edge +13,5 (primo
  positivo del progetto) vs −44 del momentum. MA: stress su 3 periodi normali
  con costi reali (20bps) → media +32 ma viene da 1 solo periodo fortunato
  (H2-2024 +129), 2 su 3 negativi → **fortuna-di-periodo, non edge.**
- **Timeframe: piu' lento = meglio.** 2h (ben tarato: EMA24/84, non EMA60/150
  che lasciava il regime 95% SIDEWAYS) fa +23-43%, sotto buy&hold; settimanale
  vince. Confermato in ogni direzione.

## 12. LO SCUDO SETTIMANALE — il risultato vero del progetto

`crypto_regime.py` con `short_confirm_bars` (short solo su crollo MACRO
confermato, cura il dead-cat-bounce) + resample settimanale
(`backtest_portfolio_weekly_shield.py`, `confirm_robustness.py`).

**Portafoglio 14-major aggregato, 2021-2024 (deterministico, funding incluso,
benchmark onesto):**
- Scudo SHORT settimanale: **+71,9% / max DD −27,5%**
- Scudo CASH settimanale: **+66,8% / max DD −36,7%**
- Tieni le 14 equipesate: +21,0% / max DD −80,1%

**CONFERMA FUORI CAMPIONE 2017-2020** (ciclo mai visto, bear −84% del 2018,
BTC/ETH/LTC — le uniche con dati): scudo CASH batte il buy&hold su tutti e 3
(BTC +305 vs +206, ETH +11,5 vs −20, LTC +87 vs +25). **Edge robusto su 2 cicli
indipendenti.**

**MA — walk-forward anno-per-anno (la natura vera):** lo scudo cash perde
quasi ogni anno SU (2021: −41 edge; 2023: −32) e vince solo nell'anno GIU'
(2022: +14). Verdetto definitivo:

> **NON e' alpha** (in salita rende MENO del mercato — paghi un "premio").
> **E' un'ASSICURAZIONE ANTI-CRASH robusta**: ti fa rinunciare a parte dei
> guadagni in bull, in cambio di NON subire i −80% nei bear (2018, 2022).
> Brilla quando arriva un bear, costa quando non arriva. Come l'assicurazione
> sulla casa. Il "+72%/−27%" del 2021-24 riflette il fatto che quel periodo
> CONTENEVA un grande bear (2022) da cui proteggere.

### Conclusione del progetto (~250 backtest)
1. **Alpha (battere il mercato in salita): NON ESISTE** con prezzo+volume e
   regole deterministiche. Ogni "vittoria" era bug, survivorship bias, o
   fortuna-di-periodo — sparita appena rimossi i bias.
2. **Assicurazione anti-crash: ESISTE ed e' robusta** (scudo cash settimanale,
   confermato su 2 cicli). E' l'unico risultato che ha retto a ogni test onesto.
3. **Per GeoInvest**: integrare lo scudo come MODULO di protezione opzionale
   (non sostituto del trading), che si attiva su bear confermato settimanale.
   Versione CASH consigliata (il motore robusto, senza il rischio direzionale
   dello short — che aggiunge solo +5% guadagno ma puo' far male nei rimbalzi).

## 13. Stato del codice (finale)
- Branch `main`, commit locali fino a `d33d5db`. **NON pushato. Tutti i flag OFF.**
- File di ricerca in `backend/simulator/`: backtest_{scalper,swing,regime,
  meanrev,portfolio,portfolio_weekly_shield}.py, stress_{test_scalper,
  structural}.py, sweep_portfolio_risk.py, confirm_robustness.py, _cmp/_diag.
- Motori in `backend/agents/`: crypto_{scalper,swing,regime,meanrev,signal_core,
  selector,structural}.py. Test in `backend/tests/test_crypto_*.py` (tutti verdi).
- Governatori di rischio (`risk_profile`, `risk_state`) preservati.
