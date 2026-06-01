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
