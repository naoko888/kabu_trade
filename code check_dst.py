import pandas as pd
import sys
sys.path.insert(0, r"C:\kabu_trade")
from backtest_system123_combined import load_data, add_indicators, load_cpi, run_backtest, calc_summary, _add_dst_col

df = load_data()
df = add_indicators(df)
cpi = load_cpi()

trades = run_backtest(df, cpi, s1_excl_months=(3,5,11), s1_weekdays=(0,1,2), s1_hours=tuple(range(24)))
t1 = trades[trades["system"] == "①"].copy()
t1 = _add_dst_col(t1)

t1_dst = t1[t1["is_dst"]]
t1_win = t1[~t1["is_dst"]]

print("=== DST期間 時間帯別 ===")
for hr in sorted(t1_dst["signal_hour"].unique()):
    g = t1_dst[t1_dst["signal_hour"] == hr]
    s = calc_summary(g)
    mark = "○" if s["pf"] >= 1.0 else "✗"
    print(f"{hr:>2}時  {s['n']:>5}件  PF{s['pf']:.3f}  {s['pnl_yen']:>+10,.0f}円  {mark}")

print("\n=== 冬時間 時間帯別 ===")
for hr in sorted(t1_win["signal_hour"].unique()):
    g = t1_win[t1_win["signal_hour"] == hr]
    s = calc_summary(g)
    mark = "○" if s["pf"] >= 1.0 else "✗"
    print(f"{hr:>2}時  {s['n']:>5}件  PF{s['pf']:.3f}  {s['pnl_yen']:>+10,.0f}円  {mark}")