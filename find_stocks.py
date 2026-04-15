import yfinance as yf

stocks = {
    '9434.T': 'ソフトバンク',
    '9433.T': 'KDDI',
    '8306.T': '三菱UFJ',
    '7203.T': 'トヨタ',
    '6758.T': 'ソニー',
    '9432.T': 'NTT',
    '8411.T': 'みずほ',
    '9984.T': 'ソフトバンクG',
    '7974.T': '任天堂',
    '6861.T': 'キーエンス',
}

print("銘柄スクリーニング結果")
print("="*60)

for code, name in stocks.items():
    try:
        df = yf.download(code, period="5d", interval="1d", 
                        progress=False)
        if len(df) > 0:
            price = float(df["Close"].iloc[-1].iloc[0] 
                         if hasattr(df["Close"].iloc[-1], 'iloc') 
                         else df["Close"].iloc[-1])
            vol = float(df["Volume"].iloc[-1].iloc[0] 
                       if hasattr(df["Volume"].iloc[-1], 'iloc') 
                       else df["Volume"].iloc[-1])
            high = float(df["High"].max().iloc[0] 
                        if hasattr(df["High"].max(), 'iloc') 
                        else df["High"].max())
            low = float(df["Low"].min().iloc[0] 
                       if hasattr(df["Low"].min(), 'iloc') 
                       else df["Low"].min())
            range_pct = (high - low) / low * 100
            print(f"{name}({code})")
            print(f"  株価:   {price:.0f}円")
            print(f"  出来高: {vol/10000:.0f}万株")
            print(f"  値幅:   {range_pct:.1f}%")
            print()
    except Exception as e:
        print(f"{name}: エラー {e}")

print("="*60)
print("デイトレ向き条件：株価500円以下・出来高1000万株以上・値幅1%以上")
