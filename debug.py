import yfinance as yf
import pandas as pd

df = yf.download('1570.T', period='60d',
                 interval='5m', progress=False)
df.columns = ['close','high','low','open','volume']
df['ma5'] = df['close'].rolling(5).mean()
df['ma20'] = df['close'].rolling(20).mean()
df['ma20_slope'] = df['ma20'] - df['ma20'].shift(3)

W = 8
results = {
    'MAng': 0,
    '押しなし': 0,
    '安値割れ': 0,
    '陽線でない': 0,
    '高値更新なし': 0,
    'OK': 0
}

ok_list = []

for i in range(25, len(df)):
    row = df.iloc[i]

    # MAトレンド確認
    if not (df['ma5'].iloc[i] > df['ma20'].iloc[i]
            and df['ma20_slope'].iloc[i] > 0):
        results['MAng'] += 1
        continue

    window = df.iloc[max(0, i-W):i]
    if len(window) < 2:
        continue

    window_high = window['high'].max()
    window_low = window['low'].min()

    # 押し確認
    pullback = window.iloc[-2:]
    if pullback['close'].iloc[-1] >= \
       pullback['close'].iloc[0]:
        results['押しなし'] += 1
        continue

    # 安値割れ確認
    pullback_low = pullback['low'].min()
    pre_low = window.iloc[:-2]['low'].min() \
              if len(window) > 2 else window_low
    if pullback_low < pre_low * 0.998:
        results['安値割れ'] += 1
        continue

    # 陽線確認
    if row['close'] <= row['open']:
        results['陽線でない'] += 1
        continue

    # 高値更新確認
    if row['close'] <= window_high:
        results['高値更新なし'] += 1
        continue

    results['OK'] += 1
    ok_list.append({
        'time': df.index[i],
        'close': row['close']
    })

print("【OK一覧】")
for o in ok_list[:10]:
    print(f"  {o['time']} @ {o['close']:.0f}")

print()
print("【条件別脱落数】")
for k, v in results.items():
    print(f"  {k}: {v}回")
