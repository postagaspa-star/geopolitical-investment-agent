"""
test_performance_report.py — anti-regressione sul report di performance.

Il report e' quello che l'utente scarica e usa per giudicare se il sistema
guadagna. Se sbaglia un numero, sbaglia il giudizio. Qui si blocca:
  • il registro transazioni: una riga per gamba, commissione e flusso di cassa
    con il segno giusto anche sugli short;
  • la coerenza con accounting.py (il P&L netto del report DEVE essere quello
    del modulo canonico, non una seconda implementazione che divergera');
  • le metriche sul NAV: rendimento, drawdown, e il rifiuto di annualizzare
    volatilita'/Sharpe quando lo storico e' troppo corto;
  • il CSV: intestazione, ordine delle colonne, celle vuote per i valori
    mancanti, separatore alternativo per Excel italiano;
  • le avvertenze: versamenti manuali e campioni piccoli devono essere
    dichiarati, non nascosti.
"""
import accounting
import performance_report as pr


BPS = 10.0  # 0.10% per gamba, lo stesso default del backend


def _trade(ticker, action, qty, price, ts, direction="LONG", conf=80.0, tid=None):
    return {
        "id": tid,
        "ticker": ticker,
        "action": action,
        "quantity": qty,
        "price": price,
        "total_value": qty * price,
        "direction": direction,
        "confidence_score": conf,
        "timestamp": ts,
        "execution_type": "ai",
        "final_decision": f"{action} {ticker}",
    }


def _snap(ts, value):
    return {"timestamp": ts, "total_value": value, "cash_balance": value}


# ── Registro transazioni ────────────────────────────────────────────────────

def test_ledger_una_riga_per_transazione_in_ordine_cronologico():
    trades = [
        _trade("MSFT", "BUY", 5, 300.0, "2026-07-10T10:00:00Z"),
        _trade("AAPL", "BUY", 10, 100.0, "2026-07-01T10:00:00Z"),
    ]
    ledger = pr.transaction_ledger(trades, BPS)
    assert len(ledger) == 2
    # Ordinate per data anche se arrivano disordinate dal DB.
    assert [r["ticker"] for r in ledger] == ["AAPL", "MSFT"]
    assert ledger[0]["data"] == "2026-07-01"


def test_ledger_commissione_e_flusso_cassa_long():
    """BUY: esce cassa per valore + commissione. SELL: entra valore - commissione."""
    ledger = pr.transaction_ledger([
        _trade("AAPL", "BUY", 10, 100.0, "2026-07-01T10:00:00Z"),
        _trade("AAPL", "SELL", 10, 110.0, "2026-07-05T10:00:00Z"),
    ], BPS)

    buy, sell = ledger
    assert buy["valore_lordo_usd"] == 1000.0
    assert buy["commissione_usd"] == 1.0          # 1000 * 10bps
    assert buy["flusso_cassa_usd"] == -1001.0     # paghi valore + fee
    assert buy["tipo"] == "apertura"

    assert sell["valore_lordo_usd"] == 1100.0
    assert sell["commissione_usd"] == 1.1
    assert sell["flusso_cassa_usd"] == 1098.9     # incassi valore - fee
    assert sell["tipo"] == "chiusura"


def test_ledger_short_apertura_incassa_chiusura_paga():
    """Sullo short i ruoli si invertono: il SELL apre e incassa, il BUY chiude
    e paga. Se questo si rompe, il flusso di cassa del CSV esce col segno
    sbagliato proprio sulle operazioni piu' delicate."""
    ledger = pr.transaction_ledger([
        _trade("TSLA", "SELL", 10, 200.0, "2026-07-01T10:00:00Z", direction="SHORT"),
        _trade("TSLA", "BUY", 10, 180.0, "2026-07-04T10:00:00Z", direction="SHORT"),
    ], BPS)

    apri, chiudi = ledger
    assert apri["tipo"] == "apertura"
    assert apri["flusso_cassa_usd"] > 0           # vendere allo scoperto incassa
    assert chiudi["tipo"] == "chiusura"
    assert chiudi["flusso_cassa_usd"] < 0         # ricomprare paga


def test_ledger_marca_i_trade_malformati_invece_di_nasconderli():
    """Un trade sporco nel DB deve comparire nel CSV marcato non valido: se
    sparisce in silenzio, i totali non tornano e nessuno sa perche'."""
    ledger = pr.transaction_ledger([
        _trade("AAPL", "BUY", 10, 100.0, "2026-07-01T10:00:00Z"),
        _trade("", "BUY", 0, 0.0, "2026-07-02T10:00:00Z"),
    ], BPS)
    assert len(ledger) == 2
    assert ledger[0]["valida"] is True
    assert ledger[1]["valida"] is False
    assert ledger[1]["commissione_usd"] == 0.0
    assert ledger[1]["flusso_cassa_usd"] == 0.0


def test_ledger_riconosce_le_chiusure_non_decise_dall_ai():
    ledger = pr.transaction_ledger([
        _trade("AAPL", "BUY", 10, 100.0, "2026-07-01T10:00:00Z", conf=75.0),
        _trade("AAPL", "SELL", 10, 90.0, "2026-07-02T10:00:00Z",
               conf=accounting.MANUAL_CLOSE_CONFIDENCE),
    ], BPS)
    assert ledger[0]["decisore"] == "AI"
    assert ledger[1]["decisore"] == "sistema/manuale"


# ── Coerenza con il modulo canonico dei conti ───────────────────────────────

def test_round_trip_usa_la_matematica_di_accounting():
    """Il report NON deve avere una sua idea di P&L: deve essere quella di
    accounting.compute_closed_trades. Questo test fallisce se qualcuno
    reimplementa il calcolo qui dentro."""
    trades = [
        _trade("AAPL", "BUY", 10, 100.0, "2026-07-01T10:00:00Z"),
        _trade("AAPL", "SELL", 10, 110.0, "2026-07-05T10:00:00Z"),
        _trade("TSLA", "SELL", 5, 200.0, "2026-07-02T10:00:00Z", direction="SHORT"),
        _trade("TSLA", "BUY", 5, 190.0, "2026-07-06T10:00:00Z", direction="SHORT"),
    ]
    canonical = accounting.compute_closed_trades(trades, commission_bps=BPS)
    reported = pr.round_trip_ledger(trades, BPS)

    assert len(reported) == len(canonical) == 2
    assert (sorted(r["pnl_netto_usd"] for r in reported)
            == sorted(c["pnl_usd"] for c in canonical))
    assert (sorted(r["commissioni_usd"] for r in reported)
            == sorted(c["fees_paid"] for c in canonical))


def test_round_trip_calcola_i_giorni_di_detenzione():
    rows = pr.round_trip_ledger([
        _trade("AAPL", "BUY", 10, 100.0, "2026-07-01T10:00:00Z"),
        _trade("AAPL", "SELL", 10, 110.0, "2026-07-09T10:00:00Z"),
    ], BPS)
    assert rows[0]["giorni_detenzione"] == 8
    assert rows[0]["esito"] == "vincente"


def test_commissione_configurata_cambia_il_pnl():
    """Se l'utente alza la commissione, il P&L netto DEVE scendere. E' la
    ragione per cui il valore non va scritto a mano da nessuna parte."""
    trades = [
        _trade("AAPL", "BUY", 10, 100.0, "2026-07-01T10:00:00Z"),
        _trade("AAPL", "SELL", 10, 110.0, "2026-07-05T10:00:00Z"),
    ]
    cheap = pr.round_trip_ledger(trades, 10.0)[0]["pnl_netto_usd"]
    pricey = pr.round_trip_ledger(trades, 100.0)[0]["pnl_netto_usd"]
    assert pricey < cheap


# ── Metriche sulla curva del valore ─────────────────────────────────────────

def test_daily_nav_tiene_un_solo_punto_al_giorno_lultimo():
    history = [
        _snap("2026-07-01T09:00:00Z", 100.0),
        _snap("2026-07-01T16:00:00Z", 105.0),   # chiusura del giorno
        _snap("2026-07-02T16:00:00Z", 110.0),
    ]
    daily = pr.daily_nav(history)
    assert [d["date"] for d in daily] == ["2026-07-01", "2026-07-02"]
    assert daily[0]["value"] == 105.0


def test_max_drawdown_misura_la_caduta_dal_picco():
    assert pr.max_drawdown([100.0, 120.0, 90.0, 130.0])[0] == -25.0
    assert pr.max_drawdown([100.0, 110.0, 120.0])[0] == 0.0
    assert pr.max_drawdown([])[0] == 0.0


def test_nav_metrics_rendimento_totale():
    daily = [{"date": f"2026-07-{d:02d}", "value": v}
             for d, v in zip(range(1, 5), [100.0, 105.0, 102.0, 110.0])]
    m = pr.nav_metrics(daily)
    assert m["rendimento_totale_pct"] == 10.0
    assert m["max_drawdown_pct"] < 0
    assert m["primo_giorno"] == "2026-07-01"


def test_nav_metrics_non_annualizza_su_storico_troppo_corto():
    """Sotto la soglia, volatilita'/Sharpe/Sortino restano None. Un numero
    calcolato su 4 giorni sembra una misura ma e' rumore: mostrarlo sarebbe
    peggio che non mostrare niente."""
    daily = [{"date": f"2026-07-{d:02d}", "value": v}
             for d, v in zip(range(1, 5), [100.0, 105.0, 102.0, 110.0])]
    m = pr.nav_metrics(daily)
    assert m["volatilita_annua_pct"] is None
    assert m["sharpe"] is None
    assert m["sortino"] is None


def test_nav_metrics_annualizza_con_storico_sufficiente():
    values = [100.0 + i * 0.5 + (2.0 if i % 3 == 0 else -1.0) for i in range(40)]
    daily = [{"date": f"2026-07-{(i % 30) + 1:02d}" if i < 30
              else f"2026-08-{(i - 30) + 1:02d}", "value": v}
             for i, v in enumerate(values)]
    m = pr.nav_metrics(daily)
    assert m["volatilita_annua_pct"] is not None
    assert m["sharpe"] is not None


def test_nav_metrics_serie_vuota_non_esplode():
    m = pr.nav_metrics([])
    assert m["punti_giornalieri"] == 0
    assert m["rendimento_totale_pct"] is None


def test_monthly_breakdown_marca_il_primo_mese_come_parziale():
    daily = [
        {"date": "2026-07-20", "value": 100.0},
        {"date": "2026-07-31", "value": 110.0},
        {"date": "2026-08-31", "value": 121.0},
    ]
    months = pr.monthly_breakdown(daily)
    assert [m["mese"] for m in months] == ["2026-07", "2026-08"]
    assert months[0]["parziale"] is True
    assert months[1]["parziale"] is False
    # Agosto parte dalla chiusura di luglio, non dal suo primo punto.
    assert months[1]["valore_iniziale_usd"] == 110.0
    assert months[1]["rendimento_pct"] == 10.0


# ── Statistiche e commissioni ───────────────────────────────────────────────

def test_trade_stats_win_rate_e_profit_factor():
    rounds = [
        {"pnl_netto_usd": 100.0, "pnl_lordo_usd": 102.0, "commissioni_usd": 2.0,
         "giorni_detenzione": 3},
        {"pnl_netto_usd": -50.0, "pnl_lordo_usd": -48.0, "commissioni_usd": 2.0,
         "giorni_detenzione": 5},
        {"pnl_netto_usd": 50.0, "pnl_lordo_usd": 52.0, "commissioni_usd": 2.0,
         "giorni_detenzione": 1},
    ]
    st = pr.trade_stats(rounds)
    assert st["operazioni_chiuse"] == 3
    assert st["vincenti"] == 2 and st["perdenti"] == 1
    assert st["win_rate_pct"] == 66.7
    assert st["profit_factor"] == 3.0            # 150 guadagnati / 50 persi
    assert st["pnl_realizzato_netto_usd"] == 100.0
    assert st["commissioni_totali_usd"] == 6.0
    assert st["durata_media_giorni"] == 3.0


def test_trade_stats_serie_consecutive():
    rounds = [{"pnl_netto_usd": v} for v in [1, 1, 1, -1, -1, 1]]
    st = pr.trade_stats(rounds)
    assert st["serie_vincente_max"] == 3
    assert st["serie_perdente_max"] == 2


def test_trade_stats_senza_operazioni():
    st = pr.trade_stats([])
    assert st["operazioni_chiuse"] == 0
    assert st["win_rate_pct"] is None


def test_fee_summary_peso_sul_profitto_lordo():
    """Il peso confronta le commissioni delle operazioni CHIUSE con il loro
    profitto lordo. La terza transazione apre una posizione ancora in corso: la
    sua commissione entra nel totale speso, ma non nel rapporto — altrimenti si
    dividerebbe un costo gia' sostenuto per un guadagno non ancora realizzato."""
    ledger = [
        {"data": "2026-07-01", "commissione_usd": 1.0, "valore_lordo_usd": 1000.0},
        {"data": "2026-07-05", "commissione_usd": 1.1, "valore_lordo_usd": 1100.0},
        {"data": "2026-08-01", "commissione_usd": 2.0, "valore_lordo_usd": 2000.0},
    ]
    rounds = [{"pnl_lordo_usd": 100.0, "pnl_netto_usd": 97.9,
               "commissioni_usd": 2.1}]
    fees = pr.fee_summary(ledger, rounds, BPS)
    assert fees["commissioni_totali_usd"] == 4.1          # tutto lo speso
    assert fees["commissioni_operazioni_chiuse_usd"] == 2.1
    assert fees["numero_transazioni"] == 3
    assert fees["peso_su_profitto_lordo_pct"] == 2.1      # 2.1 su 100 di lordo
    assert [m["mese"] for m in fees["commissioni_per_mese"]] == ["2026-07", "2026-08"]


def test_fee_summary_le_posizioni_aperte_non_gonfiano_il_peso():
    """Regressione: prima il peso divideva TUTTE le commissioni pagate per il
    solo profitto lordo delle operazioni chiuse. Aprire una posizione grande e
    tenerla peggiorava il numero senza che fosse successo niente."""
    chiuse = [{"data": "2026-07-01", "commissione_usd": 5.0,
               "valore_lordo_usd": 5000.0}]
    rounds = [{"pnl_lordo_usd": 100.0, "pnl_netto_usd": 95.0,
               "commissioni_usd": 5.0}]
    solo_chiuse = pr.fee_summary(chiuse, rounds, BPS)

    con_apertura = pr.fee_summary(
        chiuse + [{"data": "2026-08-01", "commissione_usd": 40.0,
                   "valore_lordo_usd": 40000.0}], rounds, BPS)

    assert (con_apertura["peso_su_profitto_lordo_pct"]
            == solo_chiuse["peso_su_profitto_lordo_pct"] == 5.0)
    assert con_apertura["commissioni_totali_usd"] == 45.0


def test_fee_summary_nessun_rapporto_se_il_lordo_e_negativo():
    """Dividere le commissioni per un profitto negativo darebbe una
    percentuale senza significato. Meglio nessun numero che un numero falso."""
    fees = pr.fee_summary([{"data": "2026-07-01", "commissione_usd": 5.0,
                            "valore_lordo_usd": 1000.0}],
                          [{"pnl_lordo_usd": -200.0, "pnl_netto_usd": -205.0}], BPS)
    assert fees["peso_su_profitto_lordo_pct"] is None


def test_ticker_breakdown_somma_chiuso_e_aperto():
    rounds = [
        {"ticker": "AAPL", "pnl_netto_usd": 100.0, "pnl_lordo_usd": 102.0,
         "commissioni_usd": 2.0},
        {"ticker": "AAPL", "pnl_netto_usd": -30.0, "pnl_lordo_usd": -28.0,
         "commissioni_usd": 2.0},
    ]
    positions = [{"ticker": "AAPL", "quantity": 10, "avg_buy_price": 100.0,
                  "current_price": 105.0, "direction": "LONG"}]
    rows = pr.ticker_breakdown(rounds, positions)
    assert len(rows) == 1
    row = rows[0]
    assert row["operazioni_chiuse"] == 2
    assert row["win_rate_pct"] == 50.0
    assert row["pnl_netto_usd"] == 70.0
    assert row["pnl_aperto_usd"] == 50.0        # (105-100) * 10
    assert row["posizione_aperta"] is True


# ── Report completo ─────────────────────────────────────────────────────────

def _report_di_prova(**kwargs):
    trades = [
        _trade("AAPL", "BUY", 10, 100.0, "2026-07-01T10:00:00Z", tid=1),
        _trade("AAPL", "SELL", 10, 110.0, "2026-07-05T10:00:00Z", tid=2),
    ]
    history = [_snap(f"2026-07-{d:02d}T16:00:00Z", 100000.0 + d * 100)
               for d in range(1, 6)]
    args = dict(
        trades=trades,
        positions=[],
        portfolio_state={"total_value": 100500.0, "cash_balance": 100500.0},
        history=history,
        commission_bps=BPS,
        initial_balance=100000.0,
    )
    args.update(kwargs)
    return pr.build_report(**args)


def test_build_report_contiene_tutte_le_sezioni():
    rep = _report_di_prova()
    for section in ("sintesi", "metriche_nav", "statistiche_operazioni",
                    "commissioni", "mensile", "per_strumento", "curva_valore",
                    "transazioni", "operazioni", "avvertenze"):
        assert section in rep, f"sezione mancante: {section}"
    assert rep["sintesi"]["transazioni_totali"] == 2
    assert rep["sintesi"]["operazioni_chiuse"] == 1
    assert rep["sintesi"]["capitale_iniziale_usd"] == 100000.0
    assert rep["sintesi"]["pnl_totale_usd"] == 500.0


def test_build_report_dichiara_i_versamenti_manuali():
    """Un versamento aumenta il valore del portafoglio senza che nessuno abbia
    guadagnato niente. Se il report non lo dice, il rendimento e' una bugia."""
    rep = _report_di_prova(cash_adjustments=[
        {"timestamp": "2026-07-03T10:00:00Z", "delta": 5000.0,
         "reason": "manual_adjust", "source": "/api/portfolio/adjust-cash"},
    ])
    assert rep["sintesi"]["movimenti_cassa_manuali_usd"] == 5000.0
    assert any("movimenti di cassa manuali" in w.lower()
               for w in rep["avvertenze"])


def test_build_report_avverte_sul_campione_piccolo():
    rep = _report_di_prova()
    assert any("operazioni chiuse" in w for w in rep["avvertenze"])
    assert any("giorni di storico" in w for w in rep["avvertenze"])


def test_build_report_senza_dati_non_esplode():
    rep = pr.build_report(trades=[], positions=[], portfolio_state={},
                          history=[], commission_bps=BPS)
    assert rep["sintesi"]["transazioni_totali"] == 0
    assert rep["metriche_nav"]["rendimento_totale_pct"] is None
    assert rep["curva_valore"] == []


# ── CSV ─────────────────────────────────────────────────────────────────────

def test_csv_intestazione_e_ordine_colonne():
    rep = _report_di_prova()
    body = pr.to_csv(rep["transazioni"], pr.CSV_COLUMNS["transazioni"])
    lines = body.strip().split("\r\n")
    header = lines[0].split(",")
    assert header[:6] == ["id", "data", "ora_utc", "ticker", "operazione",
                          "direzione"]
    assert "commissione_usd" in header
    assert len(lines) == 3          # intestazione + 2 transazioni


def test_csv_valori_mancanti_diventano_celle_vuote():
    body = pr.to_csv([{"a": 1, "b": None}], ["a", "b"])
    assert body.strip().split("\r\n")[1] == "1,"


def test_csv_separatore_punto_e_virgola_per_excel_italiano():
    body = pr.to_csv([{"a": 1, "b": 2}], ["a", "b"], separator=";")
    assert body.strip().split("\r\n")[0] == "a;b"
    assert body.strip().split("\r\n")[1] == "1;2"


def test_csv_non_perde_colonne_non_dichiarate():
    """Se una chiave nuova compare nei dati ma nessuno l'ha aggiunta a
    CSV_COLUMNS, finisce comunque nel file invece di sparire in silenzio."""
    body = pr.to_csv([{"a": 1, "sorpresa": "x"}], ["a"])
    assert "sorpresa" in body.split("\r\n")[0]


def test_csv_vuoto_restituisce_solo_intestazione():
    body = pr.to_csv([], ["a", "b"])
    assert body == "a,b\r\n"


def test_summary_rows_e_una_tabella_metrica_valore():
    rep = _report_di_prova()
    rows = pr.summary_rows(rep)
    assert all(set(r) == {"metrica", "valore"} for r in rows)
    labels = [r["metrica"] for r in rows]
    assert "Capitale iniziale (USD)" in labels
    assert "Commissioni totali (USD)" in labels
    assert "Max drawdown (%)" in labels


def test_summary_rows_include_il_benchmark_quando_disponibile():
    rep = _report_di_prova(benchmark={
        "available": True, "portfolio_return_pct": 5.0, "sp500_return_pct": 3.0,
        "alpha_pct": 2.0, "portfolio_max_drawdown_pct": -4.0,
        "sp500_max_drawdown_pct": -3.0, "verdict": "alpha_plausibile",
        "verdict_detail": "dettaglio",
    })
    labels = [r["metrica"] for r in pr.summary_rows(rep)]
    assert "Differenza alpha (punti percentuali)" in labels
    assert "Verdetto" in labels


def test_benchmark_series_rows_affianca_le_due_curve():
    rows = pr.benchmark_series_rows({
        "available": True,
        "portfolio_series_norm": [100.0, 105.0, 110.0],
        "sp500_series_norm": [100.0, 102.0],
    })
    assert len(rows) == 101                      # da 0% a 100% del periodo
    assert rows[0]["portafoglio_base100"] == 100.0
    assert rows[-1]["portafoglio_base100"] == 110.0
    assert rows[-1]["sp500_base100"] == 102.0


def test_benchmark_series_rows_vuoto_se_manca_il_benchmark():
    assert pr.benchmark_series_rows(None) == []
    assert pr.benchmark_series_rows({"available": False}) == []
