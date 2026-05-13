import pandas as pd

# ===== 設定 =====
INPUT_FILE = r"C:\kabu_trade\data\N225microf_2026.xlsx"
OUTPUT_FILE = r"C:\kabu_trade\micro_5min.csv"

# ===== 読み込み（5分足シートのみ）=====
df = pd.read_excel(INPUT_FILE, sheet_name="5min")

# ===== カラム変換 =====
df = df.rename(columns={
    "日付": "date",
    "時間": "time",
    "始値": "open",
    "高値": "high",
    "安値": "low",
    "終値": "close",
    "出来高": "volume"
})

# ===== datetime作成 =====
df["datetime"] = pd.to_datetime(df["date"].astype(str) + " " + df["time"].astype(str))
df = df.drop(columns=["date", "time"])

# ===== カラム順 =====
df = df[["datetime", "open", "high", "low", "close", "volume"]]

# ===== 保存（上書き）=====
df.to_csv(OUTPUT_FILE, index=False, encoding="utf-8-sig")

print("5分足CSV上書き完了")