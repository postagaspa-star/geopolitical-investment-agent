"""
test_benchmark_comparison.py — il confronto con il mercato a parita' di rischio.

La richiesta (audio di Andrea): "quando il mercato scende del 30%, tu quanto
hai fatto? quando risale, hai fatto di piu'? Sharpe e Sortino tuoi contro i
suoi". Qui si blocca la matematica con serie costruite a mano, dove la
risposta giusta si sa in anticipo.
"""
from datetime import datetime, timedelta, timezone

import performance_report as pr


def _dates(n, start="2026-06-01"):
    d0 = datetime.fromisoformat(start)
    return [(d0 + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]


def _series(values, dates):
    return [{"date": d, "value": v} for d, v in zip(dates, values)]


def _bench(values, dates):
    return [{"date": d, "close": v} for d, v in zip(dates, values)]


def test_align_tiene_solo_le_date_in_comune():
    port = _series([100, 101, 102, 103], _dates(4))
    bench = _bench([50, 51], ["2026-06-01", "2026-06-03"])    # manca il 2 e il 4
    out = pr.align_daily(port, bench)
    assert [a["date"] for a in out] == ["2026-06-01", "2026-06-03"]
    assert out[1]["p"] == 102 and out[1]["b"] == 51


def test_serie_identica_beta_1_alpha_0_cattura_100():
    rb = [0.01, -0.02, 0.015, -0.005, 0.02, -0.01, 0.007]
    rel = pr.relative_stats(rb, rb)
    assert rel["beta"] == 1.0
    assert rel["correlazione"] == 1.0
    assert rel["alpha_annuo_pct"] == 0.0
    assert rel["cattura_salite_pct"] == 100.0
    assert rel["cattura_discese_pct"] == 100.0
    assert rel["giorni_battuto_mercato_pct"] == 0.0      # mai strettamente meglio
    assert rel["tracking_error_pct"] is None            # scostamento zero


def test_meta_del_mercato_beta_0_5_e_discese_dimezzate():
    """Un portafoglio che fa sempre la meta' del mercato: beta 0.5, prende la
    meta' delle salite e perde la meta' nelle discese."""
    rb = [0.02, -0.03, 0.01, -0.02, 0.025, -0.015, 0.01, -0.01]
    rp = [x / 2 for x in rb]
    rel = pr.relative_stats(rp, rb)
    assert rel["beta"] == 0.5
    assert 45 <= rel["cattura_salite_pct"] <= 55
    assert 45 <= rel["cattura_discese_pct"] <= 55


def test_episodi_caduta_del_mercato_e_tenuta_del_portafoglio():
    """Il mercato cade del 10% e poi recupera; il portafoglio nello stesso
    tratto perde il 4%: "ha tenuto meglio". Poi nella risalita prende meno."""
    dates = _dates(8)
    bench = [100, 100, 95, 90, 93, 97, 101, 102]        # picco 100, minimo 90, recupero al 101
    port = [100, 100, 98, 96, 97, 98, 99, 100]
    aligned = pr.align_daily(_series(port, dates), _bench(bench, dates))
    ep = pr.benchmark_episodes(aligned)
    caduta = [e for e in ep if e["tipo"] == "caduta del mercato"][0]
    assert caduta["dal"] == dates[0] and caduta["al"] == dates[3]
    assert caduta["benchmark_pct"] == -10.0
    assert caduta["portafoglio_pct"] == -4.0
    assert caduta["esito"] == "ha tenuto meglio"
    assert caduta["recuperato_il"] == dates[6]
    risalita = [e for e in ep if e["tipo"] == "risalita del mercato"][0]
    assert risalita["dal"] == dates[3] and risalita["al"] == dates[6]
    assert risalita["esito"] == "ha preso di meno"


def test_episodi_caduta_ancora_aperta():
    dates = _dates(5)
    aligned = pr.align_daily(_series([100, 99, 97, 96, 95], dates),
                             _bench([100, 98, 95, 92, 90], dates))
    ep = pr.benchmark_episodes(aligned)
    caduta = [e for e in ep if e["tipo"] == "caduta del mercato"][0]
    assert caduta["recuperato_il"] == "non ancora"
    assert caduta["benchmark_pct"] == -10.0
    assert caduta["portafoglio_pct"] == -5.0


def test_mensile_vs_benchmark_dice_chi_ha_fatto_meglio():
    dates = ["2026-07-01", "2026-07-31", "2026-08-31"]
    aligned = pr.align_daily(_series([100, 110, 112], dates), _bench([100, 105, 115], dates))
    m = pr.monthly_vs_benchmark(aligned)
    assert [x["mese"] for x in m] == ["2026-07", "2026-08"]
    assert m[0]["meglio"] == "portafoglio"      # +10 contro +5
    assert m[1]["meglio"] == "benchmark"        # +1.8 contro +9.5


def test_confronto_non_disponibile_dice_il_motivo():
    port = _series([100, 101, 102], _dates(3))
    assert pr.benchmark_comparison(port, [])["disponibile"] is False
    assert "fornitore" in pr.benchmark_comparison(port, [])["motivo"]
    poche = pr.benchmark_comparison(port, _bench([1, 2], _dates(2)))
    assert poche["disponibile"] is False and "in comune" in poche["motivo"]


def test_confronto_completo_con_40_giorni():
    dates = _dates(40)
    bench = [100 * (1 + 0.002) ** i * (1 + (0.01 if i % 5 == 0 else -0.004)) for i in range(40)]
    port = [b * (1 + 0.0005 * i) for i, b in enumerate(bench)]     # un po' meglio, stesso ritmo
    cr = pr.benchmark_comparison(_series(port, dates), _bench(bench, dates), "S&P 500")
    assert cr["disponibile"] is True
    assert cr["giorni_comuni"] == 40
    assert cr["dal"] == dates[0] and cr["al"] == dates[-1]
    assert [m["metrica"] for m in cr["metriche"]][:2] == [
        "Rendimento nel periodo (%)", "Rendimento annualizzato (%)"]
    assert all(m["portafoglio"] is not None for m in cr["metriche"][:4])
    assert cr["verdetto"] in ("sovraperformato_a_parita_di_rischio",
                              "rendimento_maggiore_ma_rischio_maggiore",
                              "sotto_il_mercato", "insufficiente")
    assert len(cr["serie"]) == 40 and cr["serie"][0]["portafoglio_base100"] == 100.0
    assert "indicativo" in cr["verdetto_dettaglio"]       # 40 < 60 giorni


def test_build_report_include_il_confronto_e_avverte_se_manca():
    port = _series([100000, 100500, 100200], _dates(3))
    history = [{"timestamp": p["date"] + "T16:00:00Z", "total_value": p["value"],
                "cash_balance": 0} for p in port]
    rep = pr.build_report(trades=[], positions=[], portfolio_state={}, history=history)
    assert rep["confronto_rischio"]["disponibile"] is False
    assert any("parita' di rischio" in w for w in rep["avvertenze"])

    rows = _bench([400, 402, 401], _dates(3))
    rep = pr.build_report(trades=[], positions=[], portfolio_state={}, history=history,
                          benchmark_rows=rows)
    assert rep["confronto_rischio"]["disponibile"] is True
    labels = [r["metrica"] for r in pr.summary_rows(rep)]
    assert any("Sharpe - portafoglio" in l for l in labels)
    assert any("Sharpe - S&P 500" in l for l in labels)
    assert "Verdetto a parita' di rischio" in labels


def test_csv_benchmark_giornaliero_allineato_per_data():
    port = _series([100000, 101000, 100500], _dates(3))
    cr = pr.benchmark_comparison(port, _bench([400, 404, 402], _dates(3)))
    rows = pr.benchmark_series_rows(None, cr)
    body = pr.to_csv(rows, pr.CSV_COLUMNS["benchmark"])
    header = body.split("\r\n")[0].split(",")
    assert header[:3] == ["data", "portafoglio_usd", "benchmark_close"]
    assert len([l for l in body.split("\r\n") if l]) == 4
