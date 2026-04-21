import pandas as pd
df = pd.read_csv(r'C:\kabu_trade\micro_5min.csv', parse_dates=['datetime'])
print(f'総行数: {len(df)}')
print(f'期間: {df["datetime"].min()} ~ {df["datetime"].max()}')
mask = (df['datetime'] >= '2026-04-17 17:00') & (df['datetime'] <= '2026-04-18 06:00')
fri = df[mask]
print(f'4/17夜間行数: {len(fri)}')
print(fri[['datetime','close','volume']].head(3).to_string())
print(fri[['datetime','close','volume']].tail(3).to_string())