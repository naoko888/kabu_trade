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
ECONOMIC_CALENDAR_PATH = Path(r"C:\kabu_trade\economic_calendar.csv")

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

arr_open=df["open"].values; arr_high=df["high"].values
arr_low=df["low"].values;   arr_close=df["close"].values
arr_mas=df["ma9"].values;   arr_mam=df["ma20"].values
arr_macd=df["macd"].values; arr_msig=df["macd_sig"].values
dts=pd.to_datetime(df["datetime"])
arr_hour=dts.dt.hour.values; arr_weekday=dts.dt.weekday.values
n=len(df); trades=[]

for i in range(1, n-1):
    mas,mam = arr_mas[i],arr_mam[i]
    if np.isnan(mas) or np.isnan(mam) or np.isnan(arr_macd[i]) or np.isnan(arr_msig[i]): continue
    if not (mas < mam): continue
    if arr_macd[i] >= arr_msig[i]: continue
    if abs(arr_high[i]-mas)/mas > TOUCH_PCT: continue
    if arr_hour[i] not in s_strong_hours: continue
    if arr_weekday[i] not in s_strong_weekdays: continue
    ei=i+1; entry=arr_open[ei]; pnl=None; rtype=None; exit_bar=ei
    for j in range(ei, min(ei+MAX_HOLD,n)):
        if arr_low[j] <= entry-S_TP:  pnl,rtype,exit_bar=float(S_TP),"TP",j; break
        if arr_high[j] >= entry+S_SL: pnl,rtype,exit_bar=float(-S_SL),"SL",j; break
    if pnl is None:
        close_idx=min(ei+MAX_HOLD-1,n-1)
        pnl=float(entry-arr_close[close_idx]); rtype="TIME"; exit_bar=close_idx
    pnl -= COMMISSION_PT
    trades.append({"signal_dt":df["datetime"].iloc[i], "datetime":df["datetime"].iloc[ei],
                   "exit_datetime":df["datetime"].iloc[exit_bar], "pnl":pnl, "result":rtype,
                   "signal_hour":int(arr_hour[i]), "signal_weekday":int(arr_weekday[i])})

tf = pd.DataFrame(trades)
tf["month"] = pd.to_datetime(tf["datetime"]).dt.month
tf = tf[~tf["month"].isin(EXCLUDE_MONTHS)].reset_index(drop=True)
tf["year"]  = pd.to_datetime(tf["datetime"]).dt.year
tf["yen"]   = tf["pnl"] * PT_PER_YEN
print(f"系統③最終トレード数: {len(tf)}件")

def calc_summary(t):
    if len(t)==0: return {"n":0,"win_rate":0.0,"pnl":0.0,"pf":0.0}
    pnl=t["pnl"].values; wins=pnl[pnl>0].sum(); loss=abs(pnl[pnl<0].sum()); nn=len(t)
    return {"n":nn, "win_rate":float((pnl>0).mean()*100), "pnl":float(pnl.sum()),
            "pf":float(wins/loss) if loss>0 else 0.0}

def build_event_mask(sdt_series, edf, window_before=30, window_after=60):
    releases = edf["release_datetime_jst"].values
    sdt = pd.to_datetime(sdt_series).values
    wb = pd.Timedelta(minutes=window_before).value
    wa = pd.Timedelta(minutes=window_after).value
    diff = sdt[:,None].astype("int64") - releases[None,:].astype("int64")
    mask = (diff >= -wb) & (diff <= wa)
    return pd.Series(mask.any(axis=1), index=sdt_series.index)

event_df   = pd.read_csv(ECONOMIC_CALENDAR_PATH, encoding="utf-8")
event_df["release_datetime_jst"] = pd.to_datetime(event_df["release_datetime_jst"])
cpi_df     = event_df[event_df["indicator"]=="米CPI"]
ppi_df     = event_df[event_df["indicator"]=="米PPI"]
ism_mfg_df = event_df[event_df["indicator"]=="米ISM製造業"]
ism_svc_df = event_df[event_df["indicator"]=="米ISM非製造業"]

sdt = tf["signal_dt"]
patterns = [
    ("①除外なし",        None),
    ("②CPI除外",         cpi_df),
    ("③PPI除外",         ppi_df),
    ("④ISM製造業除外",   ism_mfg_df),
    ("⑤ISM非製造業除外", ism_svc_df),
    ("⑥全指標除外",      event_df),
]

print("\n"+"="*80)
print("【重要指標フィルター】指標別ビフォーアフター比較（手数料込み 2.2pt、系統③）")
print("  除外ウィンドウ: 発表30分前〜発表60分後")
print("="*80)
print(f"\n  カレンダー: CPI={len(cpi_df)}件 / PPI={len(ppi_df)}件 "
      f"/ ISM製造業={len(ism_mfg_df)}件 / ISM非製造業={len(ism_svc_df)}件")

results_pat = []
for label, edf in patterns:
    if edf is None:
        t = tf.copy()
    else:
        mask = build_event_mask(sdt, edf)
        t = tf[~mask].reset_index(drop=True)
    s = calc_summary(t)
    ev = s["pnl"]/s["n"] if s["n"]>0 else 0.0
    yen = int(t["yen"].sum()) if len(t)>0 else 0
    excl_n = len(tf) - len(t)
    results_pat.append((label, s, ev, yen, excl_n, t))

# ─ 全体比較表 ─
print(f"\n■ 指標別ビフォーアフター比較")
print(f"{'パターン':<18} {'件数':>7} {'除外件数':>8} {'勝率%':>7} {'損益(円)':>13} {'期待値(pt)':>10} {'PF':>7}")
print("-"*77)
for label, s, ev, yen, excl_n, _ in results_pat:
    excl_str = f"(-{excl_n})" if excl_n>0 else "---"
    print(f"  {label:<16} {s['n']:>7,} {excl_str:>8} {s['win_rate']:>6.1f}% "
          f"{yen:>13,} {ev:>10.2f} {s['pf']:>7.3f}")

# ─ ①vs⑥ 年別 ─
tf_all_excl = results_pat[5][5].copy()
tf_all_excl["year"]  = pd.to_datetime(tf_all_excl["datetime"]).dt.year
tf_all_excl["month"] = pd.to_datetime(tf_all_excl["datetime"]).dt.month
tf_all_excl["yen"]   = tf_all_excl["pnl"] * PT_PER_YEN

print(f"\n■ 年別比較（①除外なし vs ⑥全指標除外）")
print(f"{'年':>5}  {'①除外なし(円)':>14} {'⑥全除外(円)':>14} {'差分(円)':>12} {'PF①':>7} {'PF⑥':>7}")
print("-"*63)
for yr in sorted(tf["year"].unique()):
    g1=tf[tf["year"]==yr]; g6=tf_all_excl[tf_all_excl["year"]==yr]
    y1=int(g1["yen"].sum()); y6=int(g6["yen"].sum()) if len(g6)>0 else 0
    s1=calc_summary(g1); s6=calc_summary(g6)
    print(f"  {yr}  {y1:>14,} {y6:>14,} {y6-y1:>+12,} {s1['pf']:>7.3f} {s6['pf']:>7.3f}")
y1t=int(tf["yen"].sum()); y6t=int(tf_all_excl["yen"].sum())
s1t=calc_summary(tf); s6t=calc_summary(tf_all_excl)
print("-"*63)
print(f"  {'合計':>4}  {y1t:>14,} {y6t:>14,} {y6t-y1t:>+12,} {s1t['pf']:>7.3f} {s6t['pf']:>7.3f}")

# ─ ①vs⑥ 月別 ─
print(f"\n■ 月別比較（①除外なし vs ⑥全指標除外）")
print(f"{'月':>3}  {'①除外なし(円)':>14} {'⑥全除外(円)':>14} {'差分(円)':>12} {'PF①':>7} {'PF⑥':>7}")
print("-"*63)
for mo in range(1,13):
    if mo in EXCLUDE_MONTHS:
        print(f"  {mo:>2}月  {'(除外月)':^58}"); continue
    g1=tf[tf["month"]==mo]; g6=tf_all_excl[tf_all_excl["month"]==mo]
    if len(g1)==0: print(f"  {mo:>2}月  {'---':^58}"); continue
    y1=int(g1["yen"].sum()); y6=int(g6["yen"].sum()) if len(g6)>0 else 0
    s1=calc_summary(g1); s6=calc_summary(g6) if len(g6)>0 else {"pf":0.0}
    print(f"  {mo:>2}月  {y1:>14,} {y6:>14,} {y6-y1:>+12,} {s1['pf']:>7.3f} {s6['pf']:>7.3f}")

# ─ 各指標の除外候補成績 ─
print(f"\n■ 各指標の除外トレード成績（除外候補の実際の成績）")
print(f"{'指標':<14} {'除外件数':>7} {'損益(円)':>14} {'勝率%':>7} {'PF':>7}")
print("-"*55)
for lbl, edf in [("米CPI",cpi_df),("米PPI",ppi_df),
                 ("米ISM製造業",ism_mfg_df),("米ISM非製造業",ism_svc_df),
                 ("全指標合計",event_df)]:
    mask = build_event_mask(sdt, edf)
    excl_t = tf[mask].copy()
    if len(excl_t)==0:
        print(f"  {lbl:<12} {'0':>7}  {'---':>14}"); continue
    excl_t["yen"] = excl_t["pnl"] * PT_PER_YEN
    s=calc_summary(excl_t)
    print(f"  {lbl:<12} {len(excl_t):>7}  {int(excl_t['yen'].sum()):>14,} {s['win_rate']:>6.1f}% {s['pf']:>7.3f}")
print()
