import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from agents.crypto_regime import classify_regime, RegimeParams, UP, DOWN, SIDE
from simulator.backtest_swing import _dt_ms
from simulator.backtest_scalper import fetch_klines_range

# BTC 2024 su 2h: perche' 1 solo trade? Diagnosi del regime istantaneo.
bars = fetch_klines_range("BTCUSDT", "2h", _dt_ms("2024-01-01"), _dt_ms("2024-12-31"))
print("candele 2h scaricate:", len(bars))

# parametri usati nel test fallito
p = RegimeParams(ema_fast=60, ema_slow=150, atr_len=60, confirm_bars=12, short_confirm_bars=60)
closes = [b["close"] for b in bars]

# conta quante volte il regime ISTANTANEO e' UP/DOWN/SIDE lungo la serie
from collections import Counter
cnt = Counter()
warm = 160
for k in range(warm, len(bars) + 1):
    r = classify_regime(closes[:k], p)
    cnt[r] += 1
print("distribuzione regime istantaneo (param vecchi ema60/150):", dict(cnt))

# prova parametri 2h SENSATI: su 2h, 1 giorno=12 candele.
# EMA fast ~ 12*2=24 (2g), slow ~ 12*7=84 (1 settimana). slope/sep piu' permissivi.
p2 = RegimeParams(ema_fast=24, ema_slow=84, atr_len=24, confirm_bars=6,
                  short_confirm_bars=24, slope_min_pct=0.03, sep_min_pct=0.8,
                  range_lookback=84, range_max_pct=10.0)
cnt2 = Counter()
for k in range(100, len(bars) + 1):
    r = classify_regime(closes[:k], p2)
    cnt2[r] += 1
print("distribuzione regime istantaneo (param 2h sensati):", dict(cnt2))
