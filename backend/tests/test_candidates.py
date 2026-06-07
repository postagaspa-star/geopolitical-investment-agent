"""
test_candidates.py — Step 2: rotation come driver (leader, coppie, satellite).
Puro: opera su rotation_data sintetico, niente DB/rete.
"""
import candidates as C


def _rd(rows, dq="ok"):
    return {"data_quality": dq, "rows": rows}


# Categorie REALISTICHE come le produce lo scan widened:
#  - ticker già in rotazione → categoria di rotazione (XLE=energy_commodity, XLU=defensive, EWZ=international)
#  - ticker solo-satellite → satellite_category (TUR=single_country)
ROWS = [
    {"ticker": "XLE", "category": "energy_commodity", "rotation_score": 12.5,
     "rs5d_vs_spy_pct": 3.2, "rsi14": 61, "adv_usd_20d": 2.0e9},
    {"ticker": "XLU", "category": "defensive", "rotation_score": 8.0,
     "rs5d_vs_spy_pct": 2.0, "rsi14": 55, "adv_usd_20d": 1.0e9},
    {"ticker": "NVDA", "category": "high_movement_equity", "rotation_score": -1.0,
     "rs5d_vs_spy_pct": -2.0, "rsi14": 48, "adv_usd_20d": 3.0e10},
    {"ticker": "TUR", "category": "single_country", "rotation_score": 15.0,
     "rs5d_vs_spy_pct": 5.0, "rsi14": 65, "adv_usd_20d": 1.0e6},   # ADV troppo basso
    {"ticker": "EWZ", "category": "international", "rotation_score": 9.0,
     "rs5d_vs_spy_pct": 3.0, "rsi14": 58, "adv_usd_20d": 5.0e8},
]


def test_satellite_candidates_liquidity_filter():
    sats = [s["ticker"] for s in C.extract_satellite_candidates(_rd(ROWS))]
    assert "XLE" in sats and "EWZ" in sats and "XLU" in sats
    assert "TUR" not in sats   # ADV 1e6 < floor 5e6
    assert "NVDA" not in sats  # non satellite
    # ordinati per rotation_score desc
    assert sats[0] == "XLE"


def test_pairs_anticorrelated():
    pairs = C.extract_rotation_pairs(_rd(ROWS))
    labels = {(p["strong_side"], p["weak_side"]) for p in pairs}
    # international (RS 3.0) e defensive (RS 2.0) entrambi > high_movement (RS -2.0)
    assert ("international", "high_movement_equity") in labels
    assert ("defensive", "high_movement_equity") in labels
    # ordinati per spread desc
    assert pairs[0]["spread_pct"] >= pairs[-1]["spread_pct"]


def test_leaders_per_category():
    leaders = C.extract_rotation_leaders(_rd(ROWS), per_category=1)
    by_cat = {g["category"]: g["leaders"] for g in leaders}
    assert by_cat["energy_commodity"][0]["ticker"] == "XLE"
    assert len(by_cat["single_country"]) == 1


def test_build_block_nonempty():
    blk = C.build_candidate_block(_rd(ROWS), focus_tickers=["NVDA"])
    assert "UNIVERSO ESTESO" in blk
    assert "XLE" in blk
    assert "SATELLITE" in blk


def test_build_block_empty_on_degraded():
    assert C.build_candidate_block(_rd(ROWS, dq="degraded")) == ""


def test_build_block_empty_on_norows():
    assert C.build_candidate_block({"rows": []}) == ""
    assert C.build_candidate_block(None) == ""
