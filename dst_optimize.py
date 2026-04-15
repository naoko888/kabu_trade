from pathlib import Path
import pandas as pd
import numpy as np

DATA_DIR   = Path(r"C:\kabu_trade\data")
FILES      = ["N225microf_2023.xlsx","N225microf_2024.xlsx","N225microf_2025.xlsx","N225microf_2026.xlsx"]
SHEET_NAME = "5min"
MACD_FAST, MACD_SLOW, MACD_SIG = 12, 26, 9
TOUCH_PCT = 0.005; MAX_HOLD = 50; COMMISSION_PT = 2.2; PT_PER_YEN = 10
S_MAS, S_MAM, S_SL, S_TP = 9, 20, 60, 240
s_strong_weekdays = {0, 2, 3, 4}
EXCLUDE_MONTHS    = [7, 11]

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

arr_open  = df["open"].values;  arr_high  = df["high"].values
arr_low   = df["low"].values;   arr_close = df["close"].values
arr_mas   = df["ma9"].values;   arr_mam   = df["ma20"].values
arr_macd  = df["macd"].values;  arr_msig  = df["macd_sig"].values
dts         = pd.to_datetime(df["datetime"])
arr_hour    = dts.dt.hour.values
arr_weekday = dts.dt.weekday.values
arr_dt      = dts.values  # numpy datetime64 for fast DST check

# DST判定を事前ベクトル計算
dst_start = [pd.Timestamp(s).value for s, _ in dst_periods]
dst_end   = [pd.Timestamp(e).value for _, e in dst_periods]
ts_ns = arr_dt.astype("int64")
is_dst_arr = np.zeros(len(df), dtype=bool)
for s, e in zip(dst_start, dst_end):
    is_dst_arr |= (ts_ns >= s) & (ts_ns <= e)

n = len(df); trades = []
# 全フィルター前のシグナルを記録（時間帯フィルターは後で適用）
for i in range(1, n-1):
    mas, mam = arr_mas[i], arr_mam[i]
    if np.isnan(mas) or np.isnan(mam) or np.isnan(arr_macd[i]) or np.isnan(arr_msig[i]): continue
    if not (mas < mam): continue
    if arr_macd[i] >= arr_msig[i]: continue
    if abs(arr_high[i]-mas)/mas > TOUCH_PCT: continue
    if arr_weekday[i] not in s_strong_weekdays: continue
    # 時間帯フィルターは後処理のため全時間帯を通す（元パターン①で使う時間帯を前提）
    # ここでは原論文通り [5,8,9,12,14,15,19,20,21,22,23] の和集合を通す
    if arr_hour[i] not in {5,8,9,12,14,15,19,20,21,22,23}: continue

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
        "pnl": pnl, "result": rtype,
        "signal_hour":    int(arr_hour[i]),
        "signal_weekday": int(arr_weekday[i]),
        "is_dst":         bool(is_dst_arr[i]),
    })

tf_raw = pd.DataFrame(trades)
tf_raw["month"] = pd.to_datetime(tf_raw["datetime"]).dt.month
tf_raw = tf_raw[~tf_raw["month"].isin(EXCLUDE_MONTHS)].reset_index(drop=True)
tf_raw["year"] = pd.to_datetime(tf_raw["datetime"]).dt.year
tf_raw["yen"]  = tf_raw["pnl"] * PT_PER_YEN

# ─ 米CPIフィルター（発表30分前〜60分後除外）─
ECONOMIC_CALENDAR_PATH = Path(r"C:\kabu_trade\economic_calendar.csv")
event_df = pd.read_csv(ECONOMIC_CALENDAR_PATH, encoding="utf-8")
event_df["release_datetime_jst"] = pd.to_datetime(event_df["release_datetime_jst"])
cpi_df = event_df[event_df["indicator"] == "米CPI"].reset_index(drop=True)

def build_cpi_mask(sdt_series, window_before=30, window_after=60):
    releases = cpi_df["release_datetime_jst"].values
    sdt = pd.to_datetime(sdt_series).values
    wb = pd.Timedelta(minutes=window_before).value
    wa = pd.Timedelta(minutes=window_after).value
    diff = sdt[:, None].astype("int64") - releases[None, :].astype("int64")
    mask = (diff >= -wb) & (diff <= wa)
    return pd.Series(mask.any(axis=1), index=sdt_series.index)

cpi_mask = build_cpi_mask(tf_raw["signal_dt"])
n_cpi_excl = cpi_mask.sum()
tf_raw = tf_raw[~cpi_mask].reset_index(drop=True)
print(f"米CPIフィルター除外: {n_cpi_excl}件 → 残り {len(tf_raw)}件")

# ─ 日銀会合日マスク ─
BOJ_DATES = {
    "2023-01-18", "2023-03-10", "2023-04-28", "2023-06-16",
    "2023-07-28", "2023-09-22", "2023-10-31", "2023-12-19",
    "2024-01-23", "2024-03-19", "2024-04-26", "2024-06-14",
    "2024-07-31", "2024-09-20", "2024-10-31", "2024-12-19",
    "2025-01-24", "2025-03-19", "2025-05-01", "2025-06-17",
    "2025-07-31", "2025-09-19", "2025-10-30", "2025-12-19",
}
boj_dates_ts = {pd.Timestamp(d).date() for d in BOJ_DATES}
boj_mask = pd.to_datetime(tf_raw["signal_dt"]).dt.date.isin(boj_dates_ts)

def calc_summary(t):
    if len(t) == 0:
        return {"n":0, "win_rate":0.0, "pnl":0.0, "ev":0.0, "pf":0.0}
    pnl  = t["pnl"].values
    wins = pnl[pnl>0].sum(); loss = abs(pnl[pnl<0].sum()); nn = len(t)
    return {"n": nn, "win_rate": float((pnl>0).mean()*100),
            "pnl": float(pnl.sum()), "ev": float(pnl.sum()/nn),
            "pf": float(wins/loss) if loss>0 else 0.0}

# ─ 各パターン定義 ─
BASE_HOURS = {5,8,9,12,14,15,19,20,21,22,23}

def apply_pattern(raw, dst_hours, win_hours):
    """DST/冬時間で異なる時間帯フィルターを適用"""
    mask = (
        (raw["is_dst"]  & raw["signal_hour"].isin(dst_hours)) |
        (~raw["is_dst"] & raw["signal_hour"].isin(win_hours))
    )
    return raw[mask].reset_index(drop=True)

patterns = [
    ("①現状",          BASE_HOURS,                         BASE_HOURS),
    ("②9時除外",       BASE_HOURS - {9},                   BASE_HOURS - {9}),
    ("③②+冬14除外",   BASE_HOURS - {9},                   BASE_HOURS - {9, 14}),
    ("④③+DST21除外",  BASE_HOURS - {9, 21},               BASE_HOURS - {9, 14}),
]

print("\n" + "="*80)
print("【DST最適化】系統③ 時間帯フィルター ビフォーアフター比較")
print(f"  MA短{S_MAS}, MA中{S_MAM}, SL{S_SL}, TP{S_TP}, short, 手数料{COMMISSION_PT}pt")
print(f"  曜日: 月水木金  除外月: {EXCLUDE_MONTHS}")
print("="*80)

print(f"\n■ パターン別成績比較")
print(f"{'パターン':<16} {'件数':>7} {'勝率%':>7} {'損益(円)':>13} {'期待値(pt)':>10} {'PF':>7}")
print("-"*65)

pat_results = {}
for label, dst_h, win_h in patterns:
    t = apply_pattern(tf_raw, dst_h, win_h)
    s = calc_summary(t)
    pat_results[label] = (t, s)
    print(f"  {label:<14} {s['n']:>7,} {s['win_rate']:>6.1f}% "
          f"{int(t['yen'].sum()):>13,} {s['ev']:>10.2f} {s['pf']:>7.3f}")

# ─ 各パターンのDST/冬時間内訳 ─
print(f"\n■ DST/冬時間 内訳")
print(f"{'パターン':<16} {'DST件数':>7} {'DST損益(円)':>13} {'DST PF':>8}  "
      f"{'冬時間件数':>8} {'冬時間損益(円)':>14} {'冬PF':>8}")
print("-"*80)
for label, dst_h, win_h in patterns:
    t, _ = pat_results[label]
    td = t[t["is_dst"]]; tw = t[~t["is_dst"]]
    sd = calc_summary(td); sw = calc_summary(tw)
    print(f"  {label:<14} {sd['n']:>7,} {int(td['yen'].sum()):>13,} {sd['pf']:>8.3f}  "
          f"{sw['n']:>8,} {int(tw['yen'].sum()):>14,} {sw['pf']:>8.3f}")

# ─ ④の詳細 ─
t4, s4 = pat_results["④③+DST21除外"]
t4 = t4.copy()
t1, s1 = pat_results["①現状"]

print(f"\n{'='*80}")
print(f"【④ 詳細】DST: [5,8,12,14,15,19,20,22,23]  冬時間: [5,8,12,15,19,20,21,22,23]")
print(f"{'='*80}")

# 年別
print(f"\n■ 年別成績（①現状 vs ④最適化）")
print(f"{'年':>5}  {'①現状':^36}  {'④最適化':^36}")
sub = f"       {'件数':>5} {'勝率%':>6} {'損益(円)':>12} {'PF':>6}  " * 2
print(sub); print("-"*len(sub))
for yr in sorted(t4["year"].unique()):
    g1 = t1[t1["year"]==yr]; g4 = t4[t4["year"]==yr]
    s1y = calc_summary(g1); s4y = calc_summary(g4)
    print(f"  {yr}  {s1y['n']:>5} {s1y['win_rate']:>5.1f}% {int(g1['yen'].sum()):>12,} {s1y['pf']:>6.3f}  "
          f"{s4y['n']:>5} {s4y['win_rate']:>5.1f}% {int(g4['yen'].sum()):>12,} {s4y['pf']:>6.3f}")
print("-"*len(sub))
print(f"  {'合計':>4}  {s1['n']:>5} {s1['win_rate']:>5.1f}% {int(t1['yen'].sum()):>12,} {s1['pf']:>6.3f}  "
      f"{s4['n']:>5} {s4['win_rate']:>5.1f}% {int(t4['yen'].sum()):>12,} {s4['pf']:>6.3f}")

# 月別
print(f"\n■ 月別成績（④最適化）")
print(f"{'月':>3}  {'件数':>6} {'勝率%':>7} {'損益(円)':>13} {'期待値(pt)':>10} {'PF':>7}")
print("-"*52)
for mo in range(1, 13):
    if mo in EXCLUDE_MONTHS:
        print(f"  {mo:>2}月  {'(除外月)':^46}"); continue
    g = t4[t4["month"]==mo]
    if len(g)==0:
        print(f"  {mo:>2}月  {'---':^46}"); continue
    s = calc_summary(g)
    print(f"  {mo:>2}月  {s['n']:>6,} {s['win_rate']:>6.1f}% "
          f"{int(g['yen'].sum()):>13,} {s['ev']:>10.2f} {s['pf']:>7.3f}")
s4_all = calc_summary(t4)
print("-"*52)
print(f"  {'合計':>3}  {s4_all['n']:>6,} {s4_all['win_rate']:>6.1f}% "
      f"{int(t4['yen'].sum()):>13,} {s4_all['ev']:>10.2f} {s4_all['pf']:>7.3f}")

# 時間帯別（④のDST/冬時間内訳）
HOURS_DST = sorted({5,8,12,14,15,19,20,22,23})
HOURS_WIN = sorted({5,8,12,15,19,20,21,22,23})
ALL_HOURS = sorted(set(HOURS_DST) | set(HOURS_WIN))

print(f"\n■ 時間帯別成績（④最適化）")
print(f"{'時間帯':>5}  {'全体':^30}  {'DST':^28}  {'冬時間':^28}")
print(f"       {'件数':>5} {'損益(円)':>11} {'PF':>6}  "
      f"{'件数':>5} {'損益(円)':>10} {'PF':>6}  "
      f"{'件数':>5} {'損益(円)':>10} {'PF':>6}")
print("-"*90)
for h in ALL_HOURS:
    g_all = t4[t4["signal_hour"]==h]
    gd    = g_all[g_all["is_dst"]]
    gw    = g_all[~g_all["is_dst"]]
    sa = calc_summary(g_all); sd = calc_summary(gd); sw = calc_summary(gw)
    ya = int(g_all["yen"].sum()) if len(g_all)>0 else 0
    yd = int(gd["yen"].sum()) if len(gd)>0 else 0
    yw = int(gw["yen"].sum()) if len(gw)>0 else 0
    dst_mark = "●" if h in HOURS_DST else "  "
    win_mark = "●" if h in HOURS_WIN else "  "
    print(f"  {h:>3}時  {sa['n']:>5} {ya:>11,} {sa['pf']:>6.3f}  "
          f"{dst_mark}{sd['n']:>4} {yd:>10,} {sd['pf']:>6.3f}  "
          f"{win_mark}{sw['n']:>4} {yw:>10,} {sw['pf']:>6.3f}")
print("-"*90)
print(f"  {'合計':>3}   {s4['n']:>5} {int(t4['yen'].sum()):>11,} {s4['pf']:>6.3f}")
print("\n※ ●=当該期間で有効な時間帯")
print()

# ════════════════════════════════════════════════════════════════════════════
# 追加比較: ④⑤⑥⑦  冬時間8時除外 × 日銀会合日フィルター
# ════════════════════════════════════════════════════════════════════════════

DST4 = frozenset({5,8,12,14,15,19,20,22,23})   # ④DST時間帯（変更なし）
WIN4 = frozenset({5,8,12,15,19,20,21,22,23})    # ④冬時間帯
WIN5 = frozenset({5,12,15,19,20,21,22,23})      # ⑤冬8時除外

def apply_pat(raw, dst_hours, win_hours, excl_boj=False):
    mask = (
        (raw["is_dst"]  & raw["signal_hour"].isin(dst_hours)) |
        (~raw["is_dst"] & raw["signal_hour"].isin(win_hours))
    )
    t = raw[mask].copy()
    if excl_boj:
        bm = pd.to_datetime(t["signal_dt"]).dt.date.isin(boj_dates_ts)
        t = t[~bm].reset_index(drop=True)
    return t.reset_index(drop=True)

p4 = apply_pat(tf_raw, DST4, WIN4, excl_boj=False)
p5 = apply_pat(tf_raw, DST4, WIN5, excl_boj=False)
p6 = apply_pat(tf_raw, DST4, WIN5, excl_boj=True)
p7 = apply_pat(tf_raw, DST4, WIN4, excl_boj=True)

new_patterns = [
    ("④現確定ベース",    p4),
    ("⑤冬8時除外",      p5),
    ("⑥⑤+日銀除外",    p6),
    ("⑦④+日銀除外のみ", p7),
]

print("\n" + "="*72)
print("【追加比較】冬時間8時除外 × 日銀会合日フィルター")
print(f"  手数料{COMMISSION_PT}pt・CPI除外済み")
print("="*72)

print(f"\n■ パターン比較")
print(f"{'パターン':<20} {'件数':>7} {'勝率%':>7} {'損益(円)':>13} {'期待値(pt)':>10} {'PF':>7}")
print("-"*68)
np_results = {}
for label, t in new_patterns:
    s = calc_summary(t)
    np_results[label] = (t, s)
    print(f"  {label:<18} {s['n']:>7,} {s['win_rate']:>6.1f}% "
          f"{int(t['yen'].sum()):>13,} {s['ev']:>10.2f} {s['pf']:>7.3f}")

# ─ 日銀会合日の実際の成績（④ベースでの該当トレード）─
boj_trades_in_p4 = p4[pd.to_datetime(p4["signal_dt"]).dt.date.isin(boj_dates_ts)].copy()
s_boj = calc_summary(boj_trades_in_p4)
print(f"\n■ 日銀会合日の実際の成績（④ベース内の該当トレード）")
print(f"{'':4} {'除外件数':>8} {'損益(円)':>13} {'勝率%':>7} {'PF':>7}")
print("-"*44)
yen_boj = int(boj_trades_in_p4["yen"].sum()) if len(boj_trades_in_p4)>0 else 0
print(f"     {s_boj['n']:>8,} {yen_boj:>13,} {s_boj['win_rate']:>6.1f}% {s_boj['pf']:>7.3f}")

# ─ 冬時間8時の実際の成績（④ベース内の該当トレード）─
win8_trades = p4[(~p4["is_dst"]) & (p4["signal_hour"]==8)].copy()
s_w8 = calc_summary(win8_trades)
print(f"\n■ 冬時間8時の実際の成績（④ベース内の該当トレード）")
print(f"{'':4} {'除外件数':>8} {'損益(円)':>13} {'勝率%':>7} {'PF':>7}")
print("-"*44)
yen_w8 = int(win8_trades["yen"].sum()) if len(win8_trades)>0 else 0
print(f"     {s_w8['n']:>8,} {yen_w8:>13,} {s_w8['win_rate']:>6.1f}% {s_w8['pf']:>7.3f}")

# ─ 最優秀パターン特定 ─
best_label = max(np_results.keys(), key=lambda k: np_results[k][1]["pf"])
t_best, s_best = np_results[best_label]
t_best = t_best.copy()
print(f"\n{'='*72}")
print(f"【最優秀パターン: {best_label}】PF={s_best['pf']:.3f}  件数={s_best['n']:,}")
print(f"{'='*72}")

# 年別
print(f"\n■ 年別成績（④現確定ベース vs {best_label}）")
hdr = f"       {'件数':>5} {'勝率%':>6} {'損益(円)':>12} {'PF':>6}  " * 2
print(hdr); print("-"*len(hdr))
for yr in sorted(t_best["year"].unique()):
    g4y = p4[p4["year"]==yr]; gby = t_best[t_best["year"]==yr]
    s4y = calc_summary(g4y); sby = calc_summary(gby)
    print(f"  {yr}  {s4y['n']:>5} {s4y['win_rate']:>5.1f}% {int(g4y['yen'].sum()):>12,} {s4y['pf']:>6.3f}  "
          f"{sby['n']:>5} {sby['win_rate']:>5.1f}% {int(gby['yen'].sum()):>12,} {sby['pf']:>6.3f}")
print("-"*len(hdr))
s4t = calc_summary(p4)
print(f"  {'合計':>4}  {s4t['n']:>5} {s4t['win_rate']:>5.1f}% {int(p4['yen'].sum()):>12,} {s4t['pf']:>6.3f}  "
      f"{s_best['n']:>5} {s_best['win_rate']:>5.1f}% {int(t_best['yen'].sum()):>12,} {s_best['pf']:>6.3f}")

# 月別
print(f"\n■ 月別成績（{best_label}）")
print(f"{'月':>3}  {'件数':>6} {'勝率%':>7} {'損益(円)':>13} {'期待値(pt)':>10} {'PF':>7}")
print("-"*52)
for mo in range(1, 13):
    if mo in EXCLUDE_MONTHS:
        print(f"  {mo:>2}月  {'(除外月)':^46}"); continue
    g = t_best[t_best["month"]==mo]
    if len(g)==0:
        print(f"  {mo:>2}月  {'---':^46}"); continue
    s = calc_summary(g)
    print(f"  {mo:>2}月  {s['n']:>6,} {s['win_rate']:>6.1f}% "
          f"{int(g['yen'].sum()):>13,} {s['ev']:>10.2f} {s['pf']:>7.3f}")
print("-"*52)
print(f"  {'合計':>3}  {s_best['n']:>6,} {s_best['win_rate']:>6.1f}% "
      f"{int(t_best['yen'].sum()):>13,} {s_best['ev']:>10.2f} {s_best['pf']:>7.3f}")

# 時間帯別（最優秀パターン）
ALL_H_BEST = sorted(set(t_best["signal_hour"].unique()))
print(f"\n■ 時間帯別成績（{best_label}）")
print(f"{'時間帯':>5}  {'全体':^30}  {'DST':^28}  {'冬時間':^28}")
print(f"       {'件数':>5} {'損益(円)':>11} {'PF':>6}  "
      f"{'件数':>5} {'損益(円)':>10} {'PF':>6}  "
      f"{'件数':>5} {'損益(円)':>10} {'PF':>6}")
print("-"*90)
for h in ALL_H_BEST:
    ga = t_best[t_best["signal_hour"]==h]
    gd = ga[ga["is_dst"]]; gw = ga[~ga["is_dst"]]
    sa = calc_summary(ga); sd = calc_summary(gd); sw = calc_summary(gw)
    print(f"  {h:>3}時  {sa['n']:>5} {int(ga['yen'].sum()):>11,} {sa['pf']:>6.3f}  "
          f"{sd['n']:>5} {int(gd['yen'].sum()) if len(gd)>0 else 0:>10,} {sd['pf']:>6.3f}  "
          f"{sw['n']:>5} {int(gw['yen'].sum()) if len(gw)>0 else 0:>10,} {sw['pf']:>6.3f}")
print("-"*90)
print(f"  {'合計':>3}   {s_best['n']:>5} {int(t_best['yen'].sum()):>11,} {s_best['pf']:>6.3f}")
print()

# ════════════════════════════════════════════════════════════════════════════
# 年×月クロス集計（パターン⑤確定版）
# ════════════════════════════════════════════════════════════════════════════
tf5 = p5.copy()
YEARS  = sorted(tf5["year"].unique())
MONTHS_H1 = [1, 2, 3, 4, 5, 6]
MONTHS_H2 = [8, 9, 10, 12]   # 7・11は除外月

def cell(t, yr, mo):
    """(年, 月) セルの成績文字列を返す"""
    g = t[(t["year"]==yr) & (t["month"]==mo)]
    if len(g) == 0:
        return "---"
    s = calc_summary(g)
    yen = int(g["yen"].sum())
    return f"{s['n']}/{s['win_rate']:.0f}%/{yen:+,}/{s['pf']:.3f}"

def col_total(t, mo):
    """月合計セルの成績文字列"""
    g = t[t["month"]==mo]
    if len(g) == 0:
        return "---"
    s = calc_summary(g)
    yen = int(g["yen"].sum())
    return f"{s['n']}/{s['win_rate']:.0f}%/{yen:+,}/{s['pf']:.3f}"

def row_total(t, yr):
    """年合計セルの成績文字列"""
    g = t[t["year"]==yr]
    if len(g) == 0:
        return "---"
    s = calc_summary(g)
    yen = int(g["yen"].sum())
    return f"{s['n']}/{s['win_rate']:.0f}%/{yen:+,}/{s['pf']:.3f}"

COL_W = 26   # 1セルの幅

print("\n" + "="*80)
print("【パターン⑤確定版】年×月 クロス集計")
print("  DST:[5,8,12,14,15,19,20,22,23]  冬:[5,12,15,19,20,21,22,23]")
print("  手数料2.2pt・CPI除外・SL60/TP240  表示: 件数/勝率%/損益円/PF")
print("="*80)

for half_label, months in [("■ 上半期（1〜6月）", MONTHS_H1),
                             ("■ 下半期（8〜12月、7・11除外）", MONTHS_H2)]:
    print(f"\n{half_label}")
    # ヘッダー行
    hdr = f"{'年':>5}  "
    for mo in months:
        hdr += f"{str(mo)+'月':^{COL_W}}"
    hdr += f"  {'年計':^{COL_W}}"
    print(hdr)
    print("-" * len(hdr))
    # 年ごとの行
    for yr in YEARS:
        row = f"  {yr}  "
        for mo in months:
            c = cell(tf5, yr, mo)
            row += f"{c:^{COL_W}}"
        row += f"  {row_total(tf5, yr):^{COL_W}}"
        print(row)
    # 月合計行
    print("-" * len(hdr))
    tot_row = f"  {'合計':>4}  "
    for mo in months:
        c = col_total(tf5, mo)
        tot_row += f"{c:^{COL_W}}"
    s5_all = calc_summary(tf5)
    all_yen = int(tf5["yen"].sum())
    tot_row += f"  {s5_all['n']}/{s5_all['win_rate']:.0f}%/{all_yen:+,}/{s5_all['pf']:.3f}".center(COL_W+2)
    print(tot_row)

# ─ 年別サマリー ─
print(f"\n{'='*60}")
print("■ 年別サマリー（パターン⑤）")
print(f"{'年':>5}  {'件数':>7} {'勝率%':>7} {'損益(円)':>13} {'期待値(pt)':>10} {'PF':>7}")
print("-"*52)
for yr in YEARS:
    g = tf5[tf5["year"]==yr]
    s = calc_summary(g)
    print(f"  {yr}  {s['n']:>7,} {s['win_rate']:>6.1f}% "
          f"{int(g['yen'].sum()):>13,} {s['ev']:>10.2f} {s['pf']:>7.3f}")
print("-"*52)
s5t = calc_summary(tf5)
print(f"  {'合計':>4}  {s5t['n']:>7,} {s5t['win_rate']:>6.1f}% "
      f"{int(tf5['yen'].sum()):>13,} {s5t['ev']:>10.2f} {s5t['pf']:>7.3f}")

# ─ 月別サマリー（安定性評価付き）─
print(f"\n{'='*72}")
print("■ 月別サマリー（全年合計・安定性評価）")
print(f"{'月':>3}  {'件数':>6} {'勝率%':>7} {'損益(円)':>13} {'PF':>7}  安定性")
print("-"*60)
for mo in range(1, 13):
    if mo in EXCLUDE_MONTHS:
        print(f"  {mo:>2}月  {'(除外月)':^52}"); continue
    g_all = tf5[tf5["month"]==mo]
    if len(g_all) == 0:
        print(f"  {mo:>2}月  {'---':^52}"); continue
    s = calc_summary(g_all)
    yen_all = int(g_all["yen"].sum())
    # 安定性: 全年でプラスかチェック
    year_yens = [int(tf5[(tf5["year"]==yr) & (tf5["month"]==mo)]["yen"].sum())
                 for yr in YEARS
                 if len(tf5[(tf5["year"]==yr) & (tf5["month"]==mo)]) > 0]
    neg_years = [yr for yr, y in zip(YEARS, year_yens) if y < 0]
    if len(neg_years) == 0:
        stability = "OK（全年プラス）"
    else:
        stability = f"NG（{','.join(str(y) for y in neg_years)}マイナス）"
    print(f"  {mo:>2}月  {s['n']:>6,} {s['win_rate']:>6.1f}% "
          f"{yen_all:>13,} {s['pf']:>7.3f}  {stability}")
print("-"*60)
print(f"  {'合計':>3}  {s5t['n']:>6,} {s5t['win_rate']:>6.1f}% "
      f"{int(tf5['yen'].sum()):>13,} {s5t['pf']:>7.3f}")
print()

# ════════════════════════════════════════════════════════════════════════════
# 月次ドローダウン制限 検証（パターン⑤確定版）
# ════════════════════════════════════════════════════════════════════════════

def apply_monthly_dd(trades_df, limit_yen):
    """
    月次ドローダウン制限を適用する。
    limit_yen: 制限額（負値、例: -30000）。None なら制限なし。
    戻り値: (フィルター後DataFrame, 発動した月数)
    """
    if limit_yen is None:
        return trades_df.copy(), 0

    t = trades_df.sort_values("datetime").reset_index(drop=True)
    kept = []
    triggered = set()
    cur_ym = None
    cum_yen = 0.0
    hit = False

    for idx, row in t.iterrows():
        ym = (int(row["year"]), int(row["month"]))
        if ym != cur_ym:
            cur_ym = ym
            cum_yen = 0.0
            hit = False

        if hit:
            continue

        kept.append(idx)
        cum_yen += float(row["yen"])

        if cum_yen <= limit_yen:
            hit = True
            triggered.add(ym)

    return t.loc[kept].reset_index(drop=True), len(triggered)

# 対象月数（実データの月数）
all_yms = set(zip(tf5["year"].astype(int), tf5["month"].astype(int)))
total_months = len(all_yms)

LIMITS = [None, -10_000, -20_000, -30_000, -50_000, -100_000, -150_000, -200_000]
LIMIT_LABELS = ["制限なし", "-10,000", "-20,000", "-30,000", "-50,000", "-100,000", "-150,000", "-200,000"]

# 各制限額で計算
dd_results = []
for lim in LIMITS:
    t_filt, n_trig = apply_monthly_dd(tf5, lim)
    s = calc_summary(t_filt)
    # 最大月間損失（月ごとの損益の最小値）
    mo_yens = (t_filt.groupby(["year","month"])["yen"].sum()
               .reindex(pd.MultiIndex.from_tuples(all_yms), fill_value=0))
    max_mo_loss = int(mo_yens.min()) if len(mo_yens) > 0 else 0
    dd_results.append({
        "label": LIMIT_LABELS[LIMITS.index(lim)],
        "t": t_filt, "s": s, "n_trig": n_trig,
        "max_mo_loss": max_mo_loss,
    })

print("\n" + "="*90)
print("【月次ドローダウン制限 検証】パターン⑤確定版")
print(f"  手数料2.2pt・CPI除外・SL60/TP240  対象月数: {total_months}か月")
print("="*90)

# ─ 制限額別サマリー ─
print(f"\n■ 制限額別サマリー")
print(f"{'制限額':>10} {'件数':>7} {'損益(円)':>13} {'PF':>7} {'最大月間損失(円)':>17} {'発動月数':>8}")
print("-"*70)
for r in dd_results:
    s = r["s"]
    trig_str = f"{r['n_trig']}/{total_months}"
    print(f"  {r['label']:>9} {s['n']:>7,} {int(r['t']['yen'].sum()):>13,} "
          f"{s['pf']:>7.3f} {r['max_mo_loss']:>17,} {trig_str:>8}")

# ─ 年別損益クロス ─
print(f"\n■ 各制限額の年別損益（円）")
yr_header = f"{'年':>5}  " + "".join(f"{r['label']:>10}" for r in dd_results)
print(yr_header)
print("-"*len(yr_header))
for yr in YEARS:
    row_str = f"  {yr}  "
    for r in dd_results:
        g = r["t"][r["t"]["year"]==yr]
        yen = int(g["yen"].sum()) if len(g)>0 else 0
        row_str += f"{yen:>10,}"
    print(row_str)
print("-"*len(yr_header))
tot_str = f"  {'合計':>4}  "
for r in dd_results:
    tot_str += f"{int(r['t']['yen'].sum()):>10,}"
print(tot_str)

# ─ 最優秀制限額を特定（PFが最大） ─
best_dd = max(dd_results[1:], key=lambda r: r["s"]["pf"])  # 制限なし除外
print(f"\n  → 最優秀制限額: {best_dd['label']}円  PF={best_dd['s']['pf']:.3f}  件数={best_dd['s']['n']:,}")

# ─ 月別損益（制限なし vs 最優秀）─
t_none = dd_results[0]["t"]
t_best_dd = best_dd["t"]
ALL_MONTHS_ACTIVE = sorted(mo for mo in range(1,13) if mo not in EXCLUDE_MONTHS)

print(f"\n■ 月別損益（制限なし vs {best_dd['label']}円）")
print(f"{'月':>3}  {'制限なし(円)':>13} {best_dd['label']+'円(円)':>14} {'差分(円)':>12}  {'PF無制限':>9} {'PF'+best_dd['label']:>9}")
print("-"*68)
for mo in ALL_MONTHS_ACTIVE:
    g0 = t_none[t_none["month"]==mo]
    gb = t_best_dd[t_best_dd["month"]==mo]
    y0 = int(g0["yen"].sum()) if len(g0)>0 else 0
    yb = int(gb["yen"].sum()) if len(gb)>0 else 0
    s0 = calc_summary(g0); sb = calc_summary(gb)
    diff = yb - y0
    diff_str = f"{diff:+,}"
    print(f"  {mo:>2}月  {y0:>13,} {yb:>14,} {diff_str:>12}  {s0['pf']:>9.3f} {sb['pf']:>9.3f}")
print("-"*68)
y0t = int(t_none["yen"].sum()); ybt = int(t_best_dd["yen"].sum())
s0t = calc_summary(t_none); sbt = calc_summary(t_best_dd)
print(f"  {'合計':>3}  {y0t:>13,} {ybt:>14,} {ybt-y0t:>+12,}  {s0t['pf']:>9.3f} {sbt['pf']:>9.3f}")

# ─ 最優秀制限額の発動月詳細 ─
print(f"\n■ 発動月詳細（{best_dd['label']}円制限）")
print(f"{'年月':>8}  {'制限発動前損益(円)':>18} {'制限後損益(円)':>15} {'削減件数':>8}")
print("-"*58)
total_saved = 0
for ym in sorted(all_yms):
    yr, mo = ym
    g_none = t_none[(t_none["year"]==yr) & (t_none["month"]==mo)]
    g_best = t_best_dd[(t_best_dd["year"]==yr) & (t_best_dd["month"]==mo)]
    y_none = int(g_none["yen"].sum()) if len(g_none)>0 else 0
    y_best = int(g_best["yen"].sum()) if len(g_best)>0 else 0
    n_saved = len(g_none) - len(g_best)
    if n_saved > 0:
        saved = y_best - y_none
        total_saved += saved
        print(f"  {yr}/{mo:02d}  {y_none:>18,} {y_best:>15,} {n_saved:>8,}  ← 発動")
print("-"*58)
print(f"  発動月合計削減: {total_saved:>+,}円  発動月数: {best_dd['n_trig']}/{total_months}")
print()
