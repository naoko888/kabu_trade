from pathlib import Path
import pandas as pd
import numpy as np

DATA_DIR   = Path(r"C:\kabu_trade\data")
FILES      = ["N225microf_2023.xlsx","N225microf_2024.xlsx","N225microf_2025.xlsx","N225microf_2026.xlsx"]
SHEET_NAME = "5min"
MACD_FAST, MACD_SLOW, MACD_SIG = 12, 26, 9
TOUCH_PCT = 0.005; MAX_HOLD = 50; COMMISSION_PT = 2.2; PT_PER_YEN = 10
S_MAS, S_MAM, S_SL, S_TP = 9, 20, 60, 240
s_strong_hours    = {5,8,9,12,14,15,19,20,21,22,23}
s_strong_weekdays = {0,2,3,4}
EXCLUDE_MONTHS    = [7, 11]

# サマータイム期間定義
dst_periods = [
    ("2023-03-12", "2023-11-05"),
    ("2024-03-10", "2024-11-03"),
    ("2025-03-09", "2025-11-02"),
    ("2026-03-08", "2026-11-01"),
]

def is_summer_time(dt):
    for start, end in dst_periods:
        if pd.Timestamp(start) <= dt <= pd.Timestamp(end):
            return True
    return False

def read_one_file(path):
    df = pd.read_excel(path, sheet_name=SHEET_NAME, engine="openpyxl")
    df = df.rename(columns={"日付":"date","時間":"time","始値":"open","高値":"high",
                             "安値":"low","終値":"close","出来高":"volume"})
    df["datetime"] = pd.to_datetime(
        df["date"].astype(str).str.strip()+" "+df["time"].astype(str).str.strip(), errors="coerce")
    for c in ["open","high","low","close","volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["datetime","open","high","low","close","volume"]) \
             .sort_values("datetime").reset_index(drop=True) \
             [["datetime","open","high","low","close","volume"]]

print("データ読み込み中...")
dfs = [read_one_file(DATA_DIR/f) for f in FILES if (DATA_DIR/f).exists()]
df = pd.concat(dfs, ignore_index=True).sort_values("datetime").drop_duplicates(subset=["datetime"]).reset_index(drop=True)
print(f"合計: {len(df)}本")

ema_fast = df["close"].ewm(span=MACD_FAST, adjust=False).mean()
ema_slow = df["close"].ewm(span=MACD_SLOW, adjust=False).mean()
df["macd"]     = ema_fast - ema_slow
df["macd_sig"] = df["macd"].ewm(span=MACD_SIG, adjust=False).mean()
df["ma9"]  = df["close"].rolling(9).mean()
df["ma20"] = df["close"].rolling(20).mean()

arr_open  = df["open"].values;  arr_high = df["high"].values
arr_low   = df["low"].values;   arr_close = df["close"].values
arr_mas   = df["ma9"].values;   arr_mam  = df["ma20"].values
arr_macd  = df["macd"].values;  arr_msig = df["macd_sig"].values
dts = pd.to_datetime(df["datetime"])
arr_hour    = dts.dt.hour.values
arr_weekday = dts.dt.weekday.values
n = len(df); trades = []

for i in range(1, n-1):
    mas, mam = arr_mas[i], arr_mam[i]
    if np.isnan(mas) or np.isnan(mam) or np.isnan(arr_macd[i]) or np.isnan(arr_msig[i]): continue
    if not (mas < mam): continue
    if arr_macd[i] >= arr_msig[i]: continue
    if abs(arr_high[i]-mas)/mas > TOUCH_PCT: continue
    if arr_hour[i] not in s_strong_hours: continue
    if arr_weekday[i] not in s_strong_weekdays: continue
    ei = i+1; entry = arr_open[ei]; pnl = None; rtype = None; exit_bar = ei
    for j in range(ei, min(ei+MAX_HOLD, n)):
        if arr_low[j]  <= entry-S_TP:  pnl,rtype,exit_bar = float(S_TP),  "TP", j; break
        if arr_high[j] >= entry+S_SL:  pnl,rtype,exit_bar = float(-S_SL), "SL", j; break
    if pnl is None:
        close_idx = min(ei+MAX_HOLD-1, n-1)
        pnl = float(entry-arr_close[close_idx]); rtype = "TIME"; exit_bar = close_idx
    pnl -= COMMISSION_PT
    trades.append({
        "signal_dt":      df["datetime"].iloc[i],
        "datetime":       df["datetime"].iloc[ei],
        "exit_datetime":  df["datetime"].iloc[exit_bar],
        "pnl":            pnl, "result": rtype,
        "signal_hour":    int(arr_hour[i]),
        "signal_weekday": int(arr_weekday[i]),
    })

tf = pd.DataFrame(trades)
tf["month"] = pd.to_datetime(tf["datetime"]).dt.month
tf = tf[~tf["month"].isin(EXCLUDE_MONTHS)].reset_index(drop=True)
tf["year"]  = pd.to_datetime(tf["datetime"]).dt.year
tf["yen"]   = tf["pnl"] * PT_PER_YEN
tf["is_dst"] = pd.to_datetime(tf["signal_dt"]).apply(is_summer_time)
print(f"系統③最終トレード数: {len(tf)}件")

def calc_summary(t):
    if len(t) == 0:
        return {"n":0, "win_rate":0.0, "pnl":0.0, "ev":0.0, "pf":0.0}
    pnl  = t["pnl"].values
    wins = pnl[pnl>0].sum(); loss = abs(pnl[pnl<0].sum()); nn = len(t)
    return {"n": nn, "win_rate": float((pnl>0).mean()*100),
            "pnl": float(pnl.sum()), "ev": float(pnl.sum()/nn),
            "pf": float(wins/loss) if loss>0 else 0.0}

tf_dst  = tf[tf["is_dst"]].copy()
tf_win  = tf[~tf["is_dst"]].copy()

print("\n" + "="*72)
print("【サマータイム vs 冬時間】系統③ 成績比較（手数料込み 2.2pt）")
print("="*72)

# ─ 全体比較 ─
s_dst = calc_summary(tf_dst)
s_win = calc_summary(tf_win)
s_all = calc_summary(tf)

print(f"\n■ サマータイム vs 冬時間 成績比較")
print(f"{'期間':<12} {'件数':>7} {'勝率%':>7} {'損益(円)':>13} {'期待値(pt)':>10} {'PF':>7}")
print("-"*60)
rows = [("サマータイム", s_dst, tf_dst), ("冬時間", s_win, tf_win), ("合計", s_all, tf)]
for label, s, t in rows:
    yen = int(t["yen"].sum()) if len(t)>0 else 0
    print(f"  {label:<10} {s['n']:>7,} {s['win_rate']:>6.1f}% {yen:>13,} {s['ev']:>10.2f} {s['pf']:>7.3f}")

# ─ 年別×DST/WIN ─
print(f"\n■ 年別×サマータイム/冬時間")
print(f"{'年':>5}  {'サマータイム':^36}  {'冬時間':^36}")
sub = f"       {'件数':>5} {'勝率%':>6} {'損益(円)':>12} {'PF':>6}  " * 2
print(sub); print("-"*len(sub))
for yr in sorted(tf["year"].unique()):
    gd = tf_dst[tf_dst["year"]==yr]; gw = tf_win[tf_win["year"]==yr]
    sd = calc_summary(gd); sw = calc_summary(gw)
    yd = int(gd["yen"].sum()) if len(gd)>0 else 0
    yw = int(gw["yen"].sum()) if len(gw)>0 else 0
    print(f"  {yr}  {sd['n']:>5} {sd['win_rate']:>5.1f}% {yd:>12,} {sd['pf']:>6.3f}  "
          f"{sw['n']:>5} {sw['win_rate']:>5.1f}% {yw:>12,} {sw['pf']:>6.3f}")

# ─ 時間帯別×DST/WIN クロス集計 ─
HOURS = sorted(s_strong_hours)
print(f"\n■ 時間帯別 × サマータイム/冬時間 クロス集計")
print(f"{'時間帯':>5}  {'サマータイム':^32}  {'冬時間':^32}  {'差(PF)':>8}")
print(f"       {'件数':>5} {'勝率%':>6} {'損益(円)':>11} {'PF':>6}  "
      f"{'件数':>5} {'勝率%':>6} {'損益(円)':>11} {'PF':>6}")
print("-"*84)
for h in HOURS:
    gd = tf_dst[tf_dst["signal_hour"]==h]
    gw = tf_win[tf_win["signal_hour"]==h]
    sd = calc_summary(gd); sw = calc_summary(gw)
    yd = int(gd["yen"].sum()) if len(gd)>0 else 0
    yw = int(gw["yen"].sum()) if len(gw)>0 else 0
    dpf = sd["pf"] - sw["pf"]
    dpf_str = f"{dpf:+.3f}"
    print(f"  {h:>3}時  {sd['n']:>5} {sd['win_rate']:>5.1f}% {yd:>11,} {sd['pf']:>6.3f}  "
          f"{gw.__len__():>5} {sw['win_rate']:>5.1f}% {yw:>11,} {sw['pf']:>6.3f}  {dpf_str:>8}")
print("-"*84)
# 合計行
print(f"  {'合計':>3}   {s_dst['n']:>5} {s_dst['win_rate']:>5.1f}% {int(tf_dst['yen'].sum()):>11,} {s_dst['pf']:>6.3f}  "
      f"{s_win['n']:>5} {s_win['win_rate']:>5.1f}% {int(tf_win['yen'].sum()):>11,} {s_win['pf']:>6.3f}")

# ─ 月別×DST/WIN ─
print(f"\n■ 月別 × サマータイム/冬時間")
print(f"{'月':>3}  {'サマータイム':^36}  {'冬時間':^36}")
print(f"       {'件数':>5} {'勝率%':>6} {'損益(円)':>12} {'PF':>6}  "
      f"{'件数':>5} {'勝率%':>6} {'損益(円)':>12} {'PF':>6}")
print("-"*84)
for mo in range(1, 13):
    if mo in EXCLUDE_MONTHS:
        print(f"  {mo:>2}月  {'(除外月)':^78}"); continue
    gd = tf_dst[tf_dst["month"]==mo]; gw = tf_win[tf_win["month"]==mo]
    if len(gd)==0 and len(gw)==0:
        print(f"  {mo:>2}月  {'---':^78}"); continue
    sd = calc_summary(gd); sw = calc_summary(gw)
    yd = int(gd["yen"].sum()) if len(gd)>0 else 0
    yw = int(gw["yen"].sum()) if len(gw)>0 else 0
    print(f"  {mo:>2}月  {sd['n']:>5} {sd['win_rate']:>5.1f}% {yd:>12,} {sd['pf']:>6.3f}  "
          f"{sw['n']:>5} {sw['win_rate']:>5.1f}% {yw:>12,} {sw['pf']:>6.3f}")

print()
