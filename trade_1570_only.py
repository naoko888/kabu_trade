import pandas as pd
import yfinance as yf

TP = 250
SL = 100
MAX_HOLD_BARS = 4
ROUND_COST = 0

def main():
    print("1570 Yahooバックテスト開始")

    df = yf.download(
        "1570.T",
        interval="5m",
        period="60d",
        auto_adjust=False,
        progress=False
    )

    if df.empty:
        print("データ取得失敗")
        return

    df = df.reset_index()

    # yfinance対策
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if c[1] == "" else c[0] for c in df.columns]

    colmap = {}
    for c in df.columns:
        s = str(c).lower()
        if "datetime" in s:
            colmap[c] = "datetime"
        elif s == "open":
            colmap[c] = "open"
        elif s == "high":
            colmap[c] = "high"
        elif s == "low":
            colmap[c] = "low"
        elif s == "close":
            colmap[c] = "close"
        elif "volume" in s:
            colmap[c] = "volume"

    df = df.rename(columns=colmap)

    need = ["datetime", "open", "high", "low", "close", "volume"]
    miss = [c for c in need if c not in df.columns]
    if miss:
        print("列不足:", miss)
        print("今ある列:", list(df.columns))
        return

    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.sort_values("datetime").reset_index(drop=True)

    # JST化
    if df["datetime"].dt.tz is not None:
        df["datetime"] = df["datetime"].dt.tz_convert("Asia/Tokyo").dt.tz_localize(None)

    # 指標
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma_slope"] = df["ma20"] - df["ma20"].shift(1)

    trades = []

    for i in range(2, len(df) - MAX_HOLD_BARS - 1):
        cur = df.iloc[i]
        p1 = df.iloc[i - 1]
        p2 = df.iloc[i - 2]

        hhmm = cur["datetime"].hour * 100 + cur["datetime"].minute

        # 1570用の時間制限
        if hhmm < 915 or hhmm >= 1300:
            continue

        # ロング条件
        if pd.isna(cur["ma_slope"]) or cur["ma_slope"] <= 0:
            continue

        if not (p2["high"] < p1["high"] and p2["low"] < p1["low"]):
            continue

        if cur["close"] <= p1["high"]:
            continue

        entry = float(df.iloc[i + 1]["open"])
        entry_time = df.iloc[i + 1]["datetime"]

        hit = False

        for j in range(1, MAX_HOLD_BARS + 1):
            idx = i + 1 + j
            bar = df.iloc[idx]

            if bar["high"] >= entry + TP:
                trades.append({
                    "entry_time": entry_time,
                    "exit_time": bar["datetime"],
                    "pnl": TP - ROUND_COST,
                    "result": "TP",
                })
                hit = True
                break

            if bar["low"] <= entry - SL:
                trades.append({
                    "entry_time": entry_time,
                    "exit_time": bar["datetime"],
                    "pnl": -SL - ROUND_COST,
                    "result": "SL",
                })
                hit = True
                break

        if not hit:
            exit_bar = df.iloc[i + 1 + MAX_HOLD_BARS]
            pnl = float(exit_bar["close"] - entry - ROUND_COST)
            trades.append({
                "entry_time": entry_time,
                "exit_time": exit_bar["datetime"],
                "pnl": pnl,
                "result": "TIME",
            })

    if not trades:
        print("トレードなし")
        return

    t = pd.DataFrame(trades)

    win_sum = t.loc[t["pnl"] > 0, "pnl"].sum()
    loss_sum = abs(t.loc[t["pnl"] < 0, "pnl"].sum())
    pf = win_sum / loss_sum if loss_sum != 0 else 0

    print("\n===== 結果 =====")
    print("回数:", len(t))
    print("勝率:", round((t["pnl"] > 0).mean() * 100, 2))
    print("PF:", round(pf, 3))
    print("合計損益:", round(t["pnl"].sum(), 2))

    print("\n===== 決済別 =====")
    print(t.groupby("result")["pnl"].agg(["count", "sum"]))

    t.to_csv("1570_yahoo_backtest_trades.csv", index=False, encoding="utf-8-sig")
    print("\n保存: 1570_yahoo_backtest_trades.csv")

# ===== 月別 =====
t["month"] = pd.to_datetime(t["entry_time"]).dt.to_period("M").astype(str)

monthly = t.groupby("month").agg(
    n=("pnl", "count"),
    win_rate=("pnl", lambda x: (x > 0).mean() * 100),
    pnl=("pnl", "sum"),
).reset_index()

print("\n===== 月別 =====")
print(monthly)

monthly.to_csv("1570_yahoo_backtest_monthly.csv", index=False, encoding="utf-8-sig")
print("保存: 1570_yahoo_backtest_monthly.csv")    

if __name__ == "__main__":
    main()