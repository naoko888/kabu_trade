import pandas as pd
import yfinance as yf

TP = 250
SL = 100
MAX_HOLD_BARS = 4
ROUND_COST = 0

BB_SQ_TH = 0.90
ATR_RATIO_TH = 0.70


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

    # 指標（auto_trade.py add_indicators と同一）
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma_slope"] = df["ma20"] - df["ma20"].shift(1)

    prev_close = df["close"].shift(1)
    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - prev_close).abs()
    tr3 = (df["low"] - prev_close).abs()
    df["tr"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["atr14"] = df["tr"].rolling(14).mean()
    df["atr_avg"] = df["atr14"].rolling(20).mean()

    df["bb_std"] = df["close"].rolling(20).std()
    df["bb_width"] = 4 * df["bb_std"]
    df["bb_width_avg"] = df["bb_width"].rolling(20).mean()
    df["bb_squeeze"] = df["bb_width"] / df["bb_width_avg"]

    trades = []

    # TIME決済でi+1+MAX_HOLD_BARS+1（=i+6）のopenを使うため範囲を1本余分に確保
    for i in range(2, len(df) - MAX_HOLD_BARS - 2):
        cur = df.iloc[i]
        p1 = df.iloc[i - 1]
        p2 = df.iloc[i - 2]

        hhmm = cur["datetime"].hour * 100 + cur["datetime"].minute

        # 1570用の時間制限
        if hhmm < 915 or hhmm >= 1300:
            continue

        # ロング条件（auto_trade.py check_signal と同一）
        if pd.isna(cur["ma_slope"]) or cur["ma_slope"] <= 0:
            continue

        # レンジフィルター（auto_trade.py BB_SQ_TH=0.90 / ATR_RATIO_TH=0.70）
        if (
            not pd.isna(cur["bb_squeeze"]) and not pd.isna(cur["atr14"])
            and not pd.isna(cur["atr_avg"]) and cur["atr_avg"] != 0
        ):
            if cur["bb_squeeze"] < BB_SQ_TH or cur["atr14"] < cur["atr_avg"] * ATR_RATIO_TH:
                continue

        if not (p2["high"] < p1["high"] and p2["low"] < p1["low"]):
            continue

        # ①: closeではなくhighで判定（バー中に一度でも超えたか）
        if cur["high"] <= p1["high"]:
            continue

        # 次足始値でエントリー（Yahooは現在値なし）
        entry_bar = df.iloc[i + 1]
        entry = float(entry_bar["open"])
        entry_time = entry_bar["datetime"]

        bb_sq_val = round(float(cur["bb_squeeze"]), 3) if not pd.isna(cur["bb_squeeze"]) else None
        atr_ratio_val = round(float(cur["atr14"] / cur["atr_avg"]), 3) if (
            not pd.isna(cur["atr14"]) and not pd.isna(cur["atr_avg"]) and cur["atr_avg"] != 0
        ) else None

        hit = False

        for j in range(1, MAX_HOLD_BARS + 1):
            idx = i + 1 + j
            bar = df.iloc[idx]

            tp_hit = bar["high"] >= entry + TP
            sl_hit = bar["low"] <= entry - SL

            if tp_hit and sl_hit:
                # ④: 同一バー内でTP/SL両方跨いだ場合、始値から近い方が先に到達
                dist_tp = (entry + TP) - float(bar["open"])
                dist_sl = float(bar["open"]) - (entry - SL)
                if dist_tp <= dist_sl:
                    trades.append({
                        "entry_time": entry_time,
                        "exit_time": bar["datetime"],
                        "entry_price": entry,
                        "exit_price": entry + TP,
                        "pnl": TP - ROUND_COST,
                        "result": "TP",
                        "bb_squeeze": bb_sq_val,
                        "atr_ratio": atr_ratio_val,
                    })
                else:
                    trades.append({
                        "entry_time": entry_time,
                        "exit_time": bar["datetime"],
                        "entry_price": entry,
                        "exit_price": entry - SL,
                        "pnl": -SL - ROUND_COST,
                        "result": "SL",
                        "bb_squeeze": bb_sq_val,
                        "atr_ratio": atr_ratio_val,
                    })
                hit = True
                break

            if tp_hit:
                trades.append({
                    "entry_time": entry_time,
                    "exit_time": bar["datetime"],
                    "entry_price": entry,
                    "exit_price": entry + TP,
                    "pnl": TP - ROUND_COST,
                    "result": "TP",
                    "bb_squeeze": bb_sq_val,
                    "atr_ratio": atr_ratio_val,
                })
                hit = True
                break

            if sl_hit:
                trades.append({
                    "entry_time": entry_time,
                    "exit_time": bar["datetime"],
                    "entry_price": entry,
                    "exit_price": entry - SL,
                    "pnl": -SL - ROUND_COST,
                    "result": "SL",
                    "bb_squeeze": bb_sq_val,
                    "atr_ratio": atr_ratio_val,
                })
                hit = True
                break

        if not hit:
            # TIME決済: 4本経過後の次足始値（auto_trade.pyの現在値≒次足open）
            exit_bar = df.iloc[i + 1 + MAX_HOLD_BARS + 1]
            exit_price = float(exit_bar["open"])
            pnl = exit_price - entry - ROUND_COST
            trades.append({
                "entry_time": entry_time,
                "exit_time": exit_bar["datetime"],
                "entry_price": entry,
                "exit_price": exit_price,
                "pnl": pnl,
                "result": "TIME",
                "bb_squeeze": bb_sq_val,
                "atr_ratio": atr_ratio_val,
            })

    if not trades:
        print("トレードなし")
        return

    t = pd.DataFrame(trades)
    t["entry_time"] = pd.to_datetime(t["entry_time"])

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

    # 月別
    t["month"] = t["entry_time"].dt.to_period("M").astype(str)
    monthly = t.groupby("month").agg(
        n=("pnl", "count"),
        win_rate=("pnl", lambda x: (x > 0).mean() * 100),
        pnl=("pnl", "sum"),
    ).reset_index()
    print("\n===== 月別 =====")
    print(monthly.to_string(index=False))

    # 曜日別
    DOW = {0: "月", 1: "火", 2: "水", 3: "木", 4: "金"}
    t["weekday_num"] = t["entry_time"].dt.weekday
    t["weekday"] = t["weekday_num"].map(DOW)
    dow = t.groupby(["weekday_num", "weekday"]).agg(
        n=("pnl", "count"),
        win_rate=("pnl", lambda x: (x > 0).mean() * 100),
        pnl=("pnl", "sum"),
    ).reset_index().drop(columns="weekday_num")
    print("\n===== 曜日別 =====")
    print(dow.to_string(index=False))

    # 時間帯別
    t["hour"] = t["entry_time"].dt.hour
    hourly = t.groupby("hour").agg(
        n=("pnl", "count"),
        win_rate=("pnl", lambda x: (x > 0).mean() * 100),
        pnl=("pnl", "sum"),
    ).reset_index()
    print("\n===== 時間帯別 =====")
    print(hourly.to_string(index=False))

    # 連勝・連敗
    max_win, max_loss, cur_streak = 0, 0, 0
    for pnl in t["pnl"]:
        if pnl > 0:
            cur_streak = cur_streak + 1 if cur_streak > 0 else 1
            max_win = max(max_win, cur_streak)
        else:
            cur_streak = cur_streak - 1 if cur_streak < 0 else -1
            max_loss = max(max_loss, abs(cur_streak))
    print(f"\n===== 連勝・連敗 =====")
    print(f"最大連勝: {max_win}  最大連敗: {max_loss}")

    # CSV保存
    t.to_csv("1570_yahoo_backtest_trades.csv", index=False, encoding="utf-8-sig")
    print("\n保存: 1570_yahoo_backtest_trades.csv")

    monthly.to_csv("1570_yahoo_backtest_monthly.csv", index=False, encoding="utf-8-sig")
    dow.to_csv("1570_yahoo_backtest_dow.csv", index=False, encoding="utf-8-sig")
    hourly.to_csv("1570_yahoo_backtest_hourly.csv", index=False, encoding="utf-8-sig")
    print("保存: 月別/曜日別/時間帯別CSV")


if __name__ == "__main__":
    main()
