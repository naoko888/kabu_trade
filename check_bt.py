import sys
sys.path.insert(0, r'C:\kabu_trade')
import pandas as pd
from backtest_system123_combined import load_data, add_indicators, load_cpi, run_backtest, calc_summary

df = load_data()
df = add_indicators(df)
cpi = load_cpi()

trades_all = run_backtest(df, cpi, s1_excl_months=(), s1_weekdays=(0,1,2,3,4), s1_hours=tuple(range(24)), s3_hours=tuple(range(24)))
trades_may = trades_all[trades_all['signal_month'] == 5].copy()
trades_may['hour'] = pd.to_datetime(trades_may['signal_dt']).dt.hour
t_filtered = trades_may[trades_may['hour'].isin([17,18,19])].copy()

print('=== 5月 17〜19時（BT基準=18〜20時相当）===')
for yr in sorted(t_filtered['signal_year'].unique()):
    g = t_filtered[t_filtered['signal_year']==yr].sort_values('signal_dt')
    s = calc_summary(g)
    cum = g['pnl_yen'].cumsum()
    min_dd = cum.min()
    print(f"{yr}年: 件数:{s['n']} 勝率:{s['win_rate']:.0f}% 損益:{s['pnl_yen']:+,.0f}円 PF:{s['pf']:.3f} 最大DD:{min_dd:+,.0f}円")

s_all = calc_summary(t_filtered)
print(f"全期間: 件数:{s_all['n']} 勝率:{s_all['win_rate']:.0f}% 損益:{s_all['pnl_yen']:+,.0f}円 PF:{s_all['pf']:.3f}")