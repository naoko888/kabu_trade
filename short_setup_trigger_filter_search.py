from pathlib import Path
import pandas as pd
import numpy as np
from itertools import combinations

# =========================================
# 設定
# =========================================
DATA_DIR = Path(r"C:\kabu_trade\data")

FILES = [
    "N225microf_2023.xlsx",
    "N225microf_2024.xlsx",
    "N225microf_2025.xlsx",
    "N225microf_2026.xlsx",
]

TP = 120
SL = 40
MAX_HOLD_BARS = 6
COST = 22

MIN_N = 50

# =========================================
# 読み込み
# =========================================
def load_all():
    dfs = []
    print("データ読み込み中...")
    for f in FILES:
        path = DATA_DIR / f
        df = pd.read_excel(path, sheet_name="5min", engine="openpyxl")

        df = df.rename(columns={
            "日付": "date", "時間": "time",
            "始値": "open", "高値": "high",
            "安値": "low", "終値": "close",
            "出来高": "volume"
        })

        df["datetime"] = pd.to_datetime(df["date"].astype(str) + " " + df["time"].astype(str))
        df = df[["datetime","open","high","low","close","volume"]]
        dfs.append(df)

    df = pd.concat(dfs).sort_values("datetime").reset_index(drop=True)
    print("本数:", len(df))
    return df

# =========================================
# 指標
# =========================================
def add_indicators(df):
    df = df.copy()

    # MA
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma50"] = df["close"].rolling(50).mean()

    df["ma10_slope"] = df["ma10"] - df["ma10"].shift(1)
    df["ma20_slope"] = df["ma20"] - df["ma20"].shift(1)
    df["ma50_slope"] = df["ma50"] - df["ma50"].shift(1)

    df["dist_ma5"] = df["close"] / df["ma5"] - 1

    # 出来高
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma20"]

    # MACD
    ema12 = df["close"].ewm(span=12).mean()
    ema26 = df["close"].ewm(span=26).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    # RSI
    diff = df["close"].diff()
    up = diff.clip(lower=0).rolling(14).mean()
    down = -diff.clip(upper=0).rolling(14).mean()
    rs = up / down
    df["rsi"] = 100 - (100/(1+rs))

    # ストキャス
    low14 = df["low"].rolling(14).min()
    high14 = df["high"].rolling(14).max()
    df["stoch_k"] = (df["close"]-low14)/(high14-low14)*100
    df["stoch_d"] = df["stoch_k"].rolling(3).mean()

    # ローソク
    df["is_bear"] = df["close"] < df["open"]

    return df.dropna().reset_index(drop=True)

# =========================================
# セットアップ
# =========================================
def setup_pullback(df):
    return (
        (df["close"] < df["ma50"]) &
        (df["ma20_slope"] < 0) &
        (df["dist_ma5"] > 0.003)
    )

def setup_break(df):
    return (
        (df["close"].diff() < -20) &
        (df["vol_ratio"] > 1.2)
    )

def setup_momentum(df):
    return (
        (df["macd"] < df["macd_signal"]) &
        (df["macd_hist"] < df["macd_hist"].shift(1)) &
        (df["ma10_slope"] < 0)
    )

SETUPS = {
    "pullback": setup_pullback,
    "break": setup_break,
    "momentum": setup_momentum
}

# =========================================
# トリガー
# =========================================
def trig_bear(df,i):
    return df.iloc[i]["is_bear"]

def trig_break1(df,i):
    if i<1: return False
    return df.iloc[i]["close"] < df.iloc[i-1]["low"]

def trig_break2(df,i):
    if i<2: return False
    return df.iloc[i]["close"] < min(df.iloc[i-1]["low"],df.iloc[i-2]["low"])

def trig_two(df,i):
    if i<1: return False
    return df.iloc[i]["is_bear"] and df.iloc[i-1]["is_bear"]

def trig_down(df,i):
    if i < 1:
        return False
    return df.iloc[i]["close"] - df.iloc[i-1]["close"] < -20

TRIGGERS = {
    "bear": trig_bear,
    "break1": trig_break1,
    "break2": trig_break2,
    "two": trig_two,
    "down": trig_down
}

# =========================================
# フィルター
# =========================================
FILTERS = {
    "stoch80": lambda d: d["stoch_k"]>=80,
    "stoch_dead": lambda d: (d["stoch_k"]<d["stoch_d"]),
    "rsi70": lambda d: d["rsi"]>=70,
    "macd_weak": lambda d: d["macd_hist"]<d["macd_hist"].shift(1),
    "vol_big": lambda d: d["vol_ratio"]>=1.5,
    "upper_wick": lambda d: (d["high"]-d["close"]) > (d["close"]-d["open"]),
}

# =========================================
# バックテスト
# =========================================
def backtest(df, mask, trigger):
    trades = []

    for i in range(len(df) - 1):
        if not mask.iloc[i]:
            continue
        if not trigger(df, i):
            continue

        entry = df.iloc[i + 1]["open"]

        for j in range(1, MAX_HOLD_BARS + 1):
            if i + 1 + j >= len(df):
                break

            bar = df.iloc[i + 1 + j]

            if bar["low"] <= entry - TP:
                trades.append(TP - COST)
                break

            if bar["high"] >= entry + SL:
                trades.append(-SL - COST)
                break

            if j == MAX_HOLD_BARS:
                trades.append(entry - bar["close"] - COST)

    if len(trades) == 0:
        return None

    pnl = np.array(trades)
    win = pnl[pnl > 0].sum()
    loss = abs(pnl[pnl < 0].sum())
    pf = win / loss if loss != 0 else 0

    return {
        "n": len(trades),
        "win_rate": (pnl > 0).mean() * 100,
        "pnl": pnl.sum(),
        "pf": pf
    }


def backtest_with_details(df, mask, trigger):
    trades = []

    for i in range(len(df) - 1):
        if not mask.iloc[i]:
            continue
        if not trigger(df, i):
            continue

        entry = df.iloc[i + 1]["open"]
        entry_time = pd.to_datetime(df.iloc[i + 1]["datetime"])

        for j in range(1, MAX_HOLD_BARS + 1):
            if i + 1 + j >= len(df):
                break

            bar = df.iloc[i + 1 + j]

            if bar["low"] <= entry - TP:
                trades.append({
                    "entry_time": entry_time,
                    "pnl": TP - COST,
                    "result": "TP"
                })
                break

            if bar["high"] >= entry + SL:
                trades.append({
                    "entry_time": entry_time,
                    "pnl": -SL - COST,
                    "result": "SL"
                })
                break

            if j == MAX_HOLD_BARS:
                trades.append({
                    "entry_time": entry_time,
                    "pnl": entry - bar["close"] - COST,
                    "result": "TIME"
                })

    return pd.DataFrame(trades)

# =========================================
# メイン
# =========================================
def main():
    df = load_all()
    df = add_indicators(df)

    rows = []

    for sname, sfunc in SETUPS.items():
        base = sfunc(df)

        for tname, tfunc in TRIGGERS.items():
            for fnum in range(0, 3):
                for fcomb in combinations(FILTERS.keys(), fnum):
                    mask = base.copy()
                    for f in fcomb:
                        mask &= FILTERS[f](df)

                    res = backtest(df, mask, tfunc)
                    if res is None:
                        continue
                    if res["n"] < MIN_N:
                        continue

                    rows.append({
                        "setup": sname,
                        "trigger": tname,
                        "filters": "+".join(fcomb),
                        **res
                    })

    out = pd.DataFrame(rows)
    out = out.sort_values(["pf", "pnl", "n"], ascending=[False, False, False])

    print(out.head(100))

    save = Path(r"C:\kabu_trade\short_final_search.csv")
    out.to_csv(save, index=False, encoding="utf-8-sig")
    print("保存:", save)

    print("\n===== 本命1本の月別検証 =====")

    mask = (
        setup_break(df)
        & FILTERS["stoch80"](df)
        & FILTERS["rsi70"](df)
    )

    trades = backtest_with_details(df, mask, TRIGGERS["two"])

    if len(trades) == 0:
        print("該当トレードなし")
        return

    trades["month"] = trades["entry_time"].dt.to_period("M")

    monthly = trades.groupby("month").agg(
        n=("pnl", "count"),
        win_rate=("pnl", lambda x: (x > 0).mean() * 100),
        pnl=("pnl", "sum"),
    ).reset_index()

    print(monthly)

    monthly_path = Path(r"C:\kabu_trade\short_best_monthly.csv")
    monthly.to_csv(monthly_path, index=False, encoding="utf-8-sig")
    print("保存:", monthly_path)

    trades = trades.sort_values("entry_time").reset_index(drop=True)
    trades["cum_pnl"] = trades["pnl"].cumsum()
    trades["peak"] = trades["cum_pnl"].cummax()
    trades["dd"] = trades["cum_pnl"] - trades["peak"]

    print("\n最大DD:", trades["dd"].min())

    dd_path = Path(r"C:\kabu_trade\short_best_trades.csv")
    trades.to_csv(dd_path, index=False, encoding="utf-8-sig")
    print("保存:", dd_path)
    
if __name__=="__main__":
    main()