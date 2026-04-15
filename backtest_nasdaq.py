"""
NASDAQ先物（NQ=F）5分足バックテスト
スリッページ込み・RR最適化
"""

import yfinance as yf
import pandas as pd
import numpy as np

SYMBOL   = "NQ=F"
INTERVAL = "5m"
PERIOD   = "60d"

MAX_LOSS_DAY    = 5000
MAX_LOSS_WEEK   = 10000
MAX_CONSEC_LOSS = 3
BB_SQ_TH        = 0.90
ATR_RATIO_TH    = 0.70
BARS            = 4
SLIP            = 1  # スリッページ片道1円

def get_data():
    print("データ取得中...")
    df = yf.download(SYMBOL, interval=INTERVAL, period=PERIOD, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns={"Open":"open","High":"high","Low":"low","Close":"close","Volume":"volume"})
    df = df.dropna().copy()
    print(f"取得完了: {len(df)}本")
    return df

def add_indicators(df):
    df = df.copy()
    df["ma20"]     = df["close"].rolling(20).mean()
    df["ma_slope"] = df["ma20"] - df["ma20"].shift(1)
    df["tr"] = np.maximum(
        df["high"] - df["low"],
        np.maximum(abs(df["high"] - df["close"].shift(1)),
                   abs(df["low"]  - df["close"].shift(1))))
    df["atr14"]        = df["tr"].rolling(14).mean()
    df["atr_avg"]      = df["atr14"].rolling(20).mean()
    df["bb_std"]       = df["close"].rolling(20).std()
    df["bb_width"]     = 4 * df["bb_std"]
    df["bb_width_avg"] = df["bb_width"].rolling(20).mean()
    df["bb_squeeze"]   = df["bb_width"] / df["bb_width_avg"]
    ts_jst = pd.to_datetime(df.index) + pd.Timedelta(hours=9)
    df["hour_jst"] = ts_jst.hour
    df["hhmm_jst"] = ts_jst.hour * 100 + ts_jst.minute
    df["date_jst"] = ts_jst.date
    return df

def is_range(row):
    if pd.isna(row["bb_squeeze"]) or pd.isna(row["atr14"]) or pd.isna(row["atr_avg"]):
        return False
    return (row["bb_squeeze"] < BB_SQ_TH or row["atr14"] < row["atr_avg"] * ATR_RATIO_TH)

def signal(df, i):
    if i < 30: return None
    cur = df.iloc[i]; p1 = df.iloc[i-1]; p2 = df.iloc[i-2]
    if pd.isna(cur["ma_slope"]) or cur["ma_slope"] <= 0: return None
    if is_range(cur): return None
    if p2["high"] < p1["high"] and p2["low"] < p1["low"] and cur["close"] > p1["high"]:
        return "long"
    return None

def backtest(df, stop, tp, lot=1):
    trades = []
    day_pnl = {}; week_pnl = {}; day_consec = {}
    skip_days = set(); skip_weeks = set()

    for i in range(len(df)):
        s = signal(df, i)
        if not s: continue
        if i + 1 >= len(df): continue

        entry_row = df.iloc[i+1]
        hhmm  = int(entry_row["hhmm_jst"])
        date  = entry_row["date_jst"]
        ts_jst = pd.to_datetime(df.index[i+1]) + pd.Timedelta(hours=9)
        week_key = ts_jst.strftime("%Y-W%U")

        if hhmm < 915: continue
        if 1130 < hhmm < 1230: continue
        if int(entry_row["hour_jst"]) >= 13: continue
        if date in skip_days: continue
        if week_key in skip_weeks: continue
        if day_consec.get(date, 0) >= MAX_CONSEC_LOSS: continue

        entry_price = float(entry_row["open"])
        last_bar = min(i+1+BARS-1, len(df)-1)
        result_trade = None

        for j in range(i+1, last_bar+1):
            h = float(df.iloc[j]["high"])
            l = float(df.iloc[j]["low"])
            hhmm_j = int(df.iloc[j]["hhmm_jst"])
            exit_time = df.index[j]

            if 1125 <= hhmm_j <= 1130:
                exit_price = float(df.iloc[j]["close"]) - SLIP
                pnl_1lot = (exit_price - entry_price) - SLIP
                result_trade = {
                    "result": "LUNCH",
                    "exit": exit_price,
                    "pnl_1lot": float(pnl_1lot),
                    "exit_time": exit_time,
                }
                break

            if h >= entry_price + tp:
                exit_price = entry_price + tp - SLIP
                pnl_1lot = tp - SLIP * 2
                result_trade = {
                    "result": "TP",
                    "exit": exit_price,
                    "pnl_1lot": float(pnl_1lot),
                    "exit_time": exit_time,
                }
                break

            if l <= entry_price - stop:
                exit_price = entry_price - stop - SLIP
                pnl_1lot = -(stop + SLIP * 2)
                result_trade = {
                    "result": "SL",
                    "exit": exit_price,
                    "pnl_1lot": float(pnl_1lot),
                    "exit_time": exit_time,
                }
                break

        if result_trade is None:
            exit_price = float(df.iloc[last_bar]["close"]) - SLIP
            pnl_1lot = (exit_price - entry_price) - SLIP
            result_trade = {
                "result": "TIME",
                "exit": exit_price,
                "pnl_1lot": float(pnl_1lot),
                "exit_time": df.index[last_bar],
            }

        pnl1 = result_trade["pnl_1lot"]
        pnl  = pnl1 * lot

        day_pnl[date]      = day_pnl.get(date, 0) + pnl
        week_pnl[week_key] = week_pnl.get(week_key, 0) + pnl
        if pnl < 0:
            day_consec[date] = day_consec.get(date, 0) + 1
        else:
            day_consec[date] = 0
        if day_pnl[date] <= -MAX_LOSS_DAY: skip_days.add(date)
        if week_pnl[week_key] <= -MAX_LOSS_WEEK: skip_weeks.add(week_key)

        trades.append({
            "entry_time":  df.index[i+1],
            "exit_time":   result_trade["exit_time"],
            "entry":       entry_price,
            "exit":        result_trade["exit"],
            "pnl":         pnl,
            "pnl_1lot":    pnl1,
            "result":      result_trade["result"],
            "lot":         lot,
        })

    if not trades: return None, None

    tdf = pd.DataFrame(trades)
    et_jst = (pd.to_datetime(tdf["entry_time"]) + pd.Timedelta(hours=9)).dt.tz_localize(None)
    tdf["month"] = et_jst.dt.to_period("M")

    pnl_arr = tdf["pnl"].to_numpy()
    gp = pnl_arr[pnl_arr>0].sum(); gl = abs(pnl_arr[pnl_arr<=0].sum())
    pf = gp/gl if gl>0 else 0.0
    eq = pd.Series(pnl_arr).cumsum(); dd = eq - eq.cummax()
    monthly = tdf.groupby("month")["pnl"].sum()

    summary = {
        "n":           int(len(tdf)),
        "win_rate":    round(float((tdf["pnl"]>0).mean()*100), 1),
        "pnl":         round(float(tdf["pnl"].sum()), 1),
        "pf":          round(float(pf), 3),
        "max_dd":      round(float(dd.min()), 1),
        "monthly_avg": round(float(monthly.mean()), 1),
        "monthly_min": round(float(monthly.min()), 1),
        "monthly_max": round(float(monthly.max()), 1),
    }
    return tdf, summary

def check_atr(df):
    print("\n" + "="*50)
    print("【NASDAQ 5分足 値幅確認】")
    print("="*50)
    recent = df.tail(500)
    avg_range = (recent["high"] - recent["low"]).mean()
    atr = recent["atr14"].mean()
    print(f"  直近500本の平均値幅: {avg_range:.2f}円")
    print(f"  ATR14平均:           {atr:.2f}円")
    print(f"  現在値:              {recent['close'].iloc[-1]:.1f}円")

def optimize_rr(df):
    print("\n" + "="*65)
    print("【RR最適化】スリッページ込み（片道1円）")
    print("="*65)
    stops = [2, 3, 4, 5, 8, 10]
    tps   = [4, 5, 6, 8, 10, 15, 20]
    results = []
    for stop in stops:
        for tp in tps:
            if tp <= stop: continue
            _, s = backtest(df, stop=stop, tp=tp, lot=1)
            if s and s["n"] >= 5:
                results.append({"STOP":stop,"TP":tp,"RR":round(tp/stop,1),**s})

    if not results:
        print("有効なパターンなし"); return None

    rdf = pd.DataFrame(results).sort_values("pf", ascending=False)
    print(f"\n{'STOP':>4} {'TP':>4} {'RR':>4} | {'回数':>4} {'勝率':>6} {'総損益':>9} {'PF':>6} {'月平均':>9} {'月最低':>9}")
    print("-"*72)
    for _, r in rdf.head(15).iterrows():
        print(f"{r['STOP']:>3}円 {r['TP']:>3}円 {r['RR']:>3.1f} | "
              f"{r['n']:>4} {r['win_rate']:>5.1f}% {r['pnl']:>+9,.0f}円 "
              f"{r['pf']:>6.3f} {r['monthly_avg']:>+9,.0f}円 {r['monthly_min']:>+9,.0f}円")

    best = rdf.iloc[0]
    print(f"\nベスト: STOP={best['STOP']}円 / TP={best['TP']}円 / RR={best['RR']} / 月平均:{best['monthly_avg']:+,.0f}円")
    return rdf

if __name__ == "__main__":
    df = get_data()
    df = add_indicators(df)
    check_atr(df)
    rdf = optimize_rr(df)

    if rdf is not None and len(rdf) > 0:
        best = rdf.iloc[0]
        stop_best = int(best["STOP"]); tp_best = int(best["TP"])

        print(f"\n【枚数別シミュレーション】STOP={stop_best}円 / TP={tp_best}円 / スリッページ込み")
        print(f"{'枚数':>6} | {'月平均':>9} | {'月最低':>9} | {'最大DD':>9}")
        print("-"*45)
        for lot in [1, 2, 3, 5]:
            _, s = backtest(df, stop=stop_best, tp=tp_best, lot=lot)
            if s:
                print(f"{lot:>5}枚  | {s['monthly_avg']:>+9,.0f}円 | "
                      f"{s['monthly_min']:>+9,.0f}円 | {s['max_dd']:>+9,.0f}円")

        print("\n【月別損益（1枚）】")
        trades, _ = backtest(df, stop=stop_best, tp=tp_best, lot=1)
        if trades is not None:
            for m, v in trades.groupby("month")["pnl"].sum().items():
                print(f"  {m}: {v:+,.0f}円")
            trades.to_csv("backtest_nasdaq.csv", index=False, encoding="utf-8-sig")
            print("\nbacktest_nasdaq.csv に保存しました")