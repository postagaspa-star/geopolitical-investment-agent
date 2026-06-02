import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from agents.crypto_regime import RegimeParams
from simulator.backtest_swing import fetch_daily, _dt_ms
from simulator.backtest_regime import run_regime_backtest


def weekly(bars):
    out = []
    for i in range(0, len(bars), 7):
        chunk = bars[i:i+7]
        if not chunk:
            continue
        out.append({"t": chunk[0]["t"], "open": chunk[0]["open"],
                    "high": max(b["high"] for b in chunk),
                    "low": min(b["low"] for b in chunk),
                    "close": chunk[-1]["close"],
                    "volume": sum(b["volume"] for b in chunk)})
    return out


periods = [('BTC bear22', 'BTCUSDT', '2022-01-01', '2022-12-31'),
           ('SOL bear22', 'SOLUSDT', '2022-01-01', '2022-12-31'),
           ('BTC24 su+scossoni', 'BTCUSDT', '2024-01-01', '2024-12-31'),
           ('ETH 2023-24', 'ETHUSDT', '2023-01-01', '2024-12-31')]

print('%-20s %7s %8s %10s %12s %10s' % ('periodo', 'B&H%', 'cash%', 'short-fast', 'short-conf10', 'short-WEEK'))
for name, sym, d0, d1 in periods:
    b = fetch_daily(sym, _dt_ms(d0), _dt_ms(d1))
    if len(b) < 80:
        print(name, 'skip'); continue
    bh = run_regime_backtest(b, RegimeParams(shield='cash'))['buy_hold_pct']
    cash = run_regime_backtest(b, RegimeParams(shield='cash'), funding_daily_pct=0.03)['return_pct']
    fast = run_regime_backtest(b, RegimeParams(shield='short', short_confirm_bars=2), funding_daily_pct=0.03)['return_pct']
    conf = run_regime_backtest(b, RegimeParams(shield='short', short_confirm_bars=10), funding_daily_pct=0.03)['return_pct']
    wk = run_regime_backtest(weekly(b), RegimeParams(shield='short', short_confirm_bars=2,
                             ema_slow=20, ema_fast=8, atr_len=10), funding_daily_pct=0.21)['return_pct']
    print('%-20s %7.0f %8.1f %10.1f %12.1f %10.1f' % (name, bh, cash, fast, conf, wk))
print("\nLEGGI: cash=esci e basta | short-fast=shorta subito | short-conf10=shorta")
print("dopo 10g di down confermato | short-WEEK=su candele settimanali.")
print("OBIETTIVO: short deve >= cash nei BEAR e NON peggiorare in BTC24.")
