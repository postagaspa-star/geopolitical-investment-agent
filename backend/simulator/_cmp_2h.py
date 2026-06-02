import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from agents.crypto_regime import RegimeParams
from simulator.backtest_swing import _dt_ms
from simulator.backtest_scalper import fetch_klines_range
from simulator.backtest_regime import run_regime_backtest

# 2h: tanti dati → uso periodo recente (1 anno) per non scaricare 4 anni di 2h.
# Confronto scudo-short su 2h vs il riferimento settimanale gia' noto.
periods = [('BTC 2024', 'BTCUSDT', '2024-01-01', '2024-12-31'),
           ('SOL 2024', 'SOLUSDT', '2024-01-01', '2024-12-31'),
           ('ETH 2024', 'ETHUSDT', '2024-01-01', '2024-12-31')]

print('Scudo-short su candele 2H (funding 0.01%/8h = ~0.03%/2h-bar* no: 0.0025%/2h)')
print('%-12s %8s %9s %9s %8s' % ('periodo', 'B&H%', 'cash2h%', 'short2h%', 'trades'))
for name, sym, d0, d1 in periods:
    bars = fetch_klines_range(sym, "2h", _dt_ms(d0), _dt_ms(d1))
    if len(bars) < 300:
        print('%-12s skip (%d)' % (name, len(bars))); continue
    # su 2h: 1 giorno = 12 candele. confirm/short_confirm in candele 2h.
    p_cash = RegimeParams(shield='cash', ema_fast=60, ema_slow=150, atr_len=60,
                          confirm_bars=12, short_confirm_bars=60)
    p_short = RegimeParams(shield='short', ema_fast=60, ema_slow=150, atr_len=60,
                           confirm_bars=12, short_confirm_bars=60)
    # funding per candela 2h: 0.01%/8h * (2/8) = 0.0025%/bar
    bh = run_regime_backtest(bars, p_cash, funding_daily_pct=0)['buy_hold_pct']
    rc = run_regime_backtest(bars, p_cash, funding_daily_pct=0.0025)
    rs = run_regime_backtest(bars, p_short, funding_daily_pct=0.0025)
    print('%-12s %8.0f %9.1f %9.1f %8d' % (name, bh, rc['return_pct'],
          rs['return_pct'], rs['regime_changes']))
print("\nAtteso: il 2h reintroduce il rumore che il settimanale eliminava →")
print("piu' trade, short meno efficace. Verifica coi numeri.")
