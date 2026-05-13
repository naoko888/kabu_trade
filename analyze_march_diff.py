"""
analyze_march_diff.py
SAT_CLOSE / gap_check 由来差異を分離集計するスクリプト
基準: backtest_system123_combined.py (gap無し・SAT_CLOSE無し)
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')
import pandas as pd
from pathlib import Path

DATA_DIR = Path(r"C:\kabu_trade\data")
CPI_CSV  = Path(r"C:\kabu_trade\economic_calendar.csv")
EXCEL_FILES = [
    "N225microf_2023.xlsx",
    "N225microf_2024.xlsx",
    "N225microf_2025.xlsx",
    "N225microf_2026.xlsx",
]

# ── 設定: backtest_system123_combined.py メインシナリオ準拠 ──
S1_WEEKDAYS   = (0, 1, 2)
S1_HOURS_DST  = (8, 15, 18, 19, 20, 21)
S1_HOURS_WIN  = (8, 12, 15, 18, 21, 23)
S1_EXCL_MONTHS = (3, 11)

S3_WEEKDAYS    = (0, 2, 3, 4)
S3_HOURS_DST   = (0, 5, 8, 12, 14, 15, 19, 20, 22, 23)
S3_HOURS_WIN   = (5, 12, 15, 19, 20, 21, 22)
S3_EXCL_MONTHS = (7, 11)

MICRO_TP       = 240
MICRO_SL       = 60
TOUCH_PCT      = 0.007
COMMISSION_PT  = 2.2
PT_TO_YEN      = 10
SESSION_BOUNDS = frozenset({2350})
MAX_HOLD       = 120

_DST = [
    (pd.Timestamp("2023-03-12"), pd.Timestamp("2023-11-05")),
    (pd.Timestamp("2024-03-10"), pd.Timestamp("2024-11-03")),
    (pd.Timestamp("2025-03-09"), pd.Timestamp("2025-11-02")),
    (pd.Timestamp("2026-03-08"), pd.Timestamp("2026-11-01")),
]

def is_dst(ts):
    for s, e in _DST:
        if s <= ts <= e:
            return True
    return False

def _sort_key(dt):
    return dt + pd.Timedelta(days=1) if dt.hour < 17 else dt

def load_xlsx():
    dfs = []
    for fname in EXCEL_FILES:
        p = DATA_DIR / fname
        if not p.exists():
            continue
        df = pd.read_excel(p, sheet_name="5min", engine="openpyxl")
        df = df.rename(columns={"日付":"date","時間":"time","始値":"open",
                                 "高値":"high","安値":"low","終値":"close","出来高":"volume"})
        df["datetime"] = pd.to_datetime(df["date"].astype(str) + " " + df["time"].astype(str))
        for c in ["open","high","low","close","volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["datetime","open","high","low","close"]).copy()
        df = df[["datetime","open","high","low","close","volume"]].copy()
        sk = df["datetime"].map(_sort_key)
        df = df.iloc[sk.argsort(kind="stable")].reset_index(drop=True)
        dfs.append(df)
    df_all = pd.concat(dfs, ignore_index=True)
    df_all = df_all.drop_duplicates(subset=["datetime"]).reset_index(drop=True)
    sk = df_all["datetime"].map(_sort_key)
    df_all = df_all.iloc[sk.argsort(kind="stable")].reset_index(drop=True)
    return df_all

def add_indicators(df):
    df = df.copy()
    df["ma9"]      = df["close"].rolling(9).mean()
    df["ma10"]     = df["close"].rolling(10).mean()
    df["ma20"]     = df["close"].rolling(20).mean()
    ema_f          = df["close"].ewm(span=12, adjust=False).mean()
    ema_s          = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"]     = ema_f - ema_s
    df["macd_sig"] = df["macd"].ewm(span=9, adjust=False).mean()
    return df

def load_cpi():
    for enc in ("utf-8","utf-8-sig","cp932"):
        try:
            df = pd.read_csv(CPI_CSV, encoding=enc)
            if "indicator" in df.columns:
                df["release_datetime_jst"] = pd.to_datetime(
                    df["release_datetime_jst"], errors="coerce")
                return df[df["indicator"] == "米CPI"].dropna(
                    subset=["release_datetime_jst"]).reset_index(drop=True)
        except Exception:
            continue
    return pd.DataFrame(columns=["release_datetime_jst"])

_cpi_mask = None  # pre-computed boolean Series, indexed by df position

def build_cpi_mask(df, cpi_df, bef=30, aft=60):
    """Vectorised: True for every bar within [release-bef, release+aft]."""
    mask = pd.Series(False, index=df.index)
    if cpi_df.empty:
        return mask
    dts = df["datetime"]  # pandas Series of Timestamps
    bef_td = pd.Timedelta(minutes=bef)
    aft_td = pd.Timedelta(minutes=aft)
    for r in cpi_df["release_datetime_jst"]:
        mask |= (dts >= r - bef_td) & (dts <= r + aft_td)
    return mask

def exec_trade(df, ei, side, tp, sl, mhold, force_sc=False, entry_wd=-1, use_sat=False):
    ep = float(df.iloc[ei]["open"])
    for j in range(ei, min(ei + mhold, len(df))):
        row  = df.iloc[j]
        dt   = pd.Timestamp(row["datetime"])
        hhmm = dt.hour * 100 + dt.minute
        bhi  = float(row["high"])
        blo  = float(row["low"])
        if side == "long":
            if bhi >= ep + tp: return tp  - COMMISSION_PT, dt, "TP"
            if blo <= ep - sl: return -sl - COMMISSION_PT, dt, "SL"
        else:
            if blo <= ep - tp: return tp  - COMMISSION_PT, dt, "TP"
            if bhi >= ep + sl: return -sl - COMMISSION_PT, dt, "SL"
        if force_sc and hhmm in SESSION_BOUNDS:
            cl  = float(row["close"])
            pnl = (cl - ep) if side == "long" else (ep - cl)
            return pnl - COMMISSION_PT, dt, "SESSION"
        if use_sat and entry_wd == 4 and dt.weekday() == 5:
            cl  = float(row["close"])
            pnl = (cl - ep) if side == "long" else (ep - cl)
            return pnl - COMMISSION_PT, dt, "SAT_CLOSE"
    cidx = min(ei + mhold - 1, len(df) - 1)
    cl   = float(df.iloc[cidx]["close"])
    pnl  = (cl - ep) if side == "long" else (ep - cl)
    return pnl - COMMISSION_PT, pd.Timestamp(df.iloc[cidx]["datetime"]), "TIME"


def build_bt(df, cpi_df, use_gap=False, use_sat=False):
    trades  = []
    skipped = []  # gap_check によるスキップ記録

    cpi_mask = build_cpi_mask(df, cpi_df).values  # numpy bool array

    cols = ["ma9","ma10","ma20","macd","macd_sig"]
    for i in range(3, len(df)):
        si = i - 1   # signal bar
        ei = i       # entry  bar

        row = df.iloc[si]
        if any(pd.isna(row[c]) for c in cols):
            continue

        dt    = pd.Timestamp(row["datetime"])
        wd    = dt.weekday()
        hr    = (dt + pd.Timedelta(minutes=5)).hour   # bar-END hour (系統①)
        h_raw = dt.hour                                # bar-START hour (系統③)
        mo    = dt.month
        dst   = is_dst(dt)

        # ── 系統① ──
        s1_hr = S1_HOURS_DST if dst else S1_HOURS_WIN
        if wd in S1_WEEKDAYS and hr in s1_hr and mo not in S1_EXCL_MONTHS:
            rp  = df.iloc[si - 1]; rp2 = df.iloc[si - 2]
            if not any(pd.isna(rp[c]) for c in cols) and not any(pd.isna(rp2[c]) for c in cols):
                above = (float(rp2["close"]) > float(rp2["ma9"])  and
                         float(rp2["close"]) > float(rp2["ma10"]) and
                         float(rp["close"])  > float(rp["ma9"])   and
                         float(rp["close"])  > float(rp["ma10"]))
                touch = (abs(float(row["low"]) - float(row["ma9"]))  / float(row["ma9"])  <= TOUCH_PCT or
                         abs(float(row["low"]) - float(row["ma10"])) / float(row["ma10"]) <= TOUCH_PCT)
                gc    = float(row["macd"]) > float(row["macd_sig"])
                if above and touch and gc:
                    etime = pd.Timestamp(df.iloc[ei]["datetime"])
                    pnl, xdt, rsn = exec_trade(df, ei, "long", MICRO_TP, MICRO_SL, MAX_HOLD, force_sc=True)
                    trades.append(dict(system="①", side="long", signal_dt=dt,
                                       entry_time=etime, month=mo, year=dt.year,
                                       pnl_pt=pnl, pnl_yen=round(pnl*PT_TO_YEN), reason=rsn))

        # ── 系統③ ──
        s3_hr = S3_HOURS_DST if dst else S3_HOURS_WIN
        if wd in S3_WEEKDAYS and mo not in S3_EXCL_MONTHS and not cpi_mask[si]:
            below = float(row["ma9"]) < float(row["ma20"])
            thi   = abs(float(row["high"]) - float(row["ma9"])) / float(row["ma9"]) <= TOUCH_PCT
            dc    = float(row["macd"]) < float(row["macd_sig"])
            if below and thi and dc and h_raw in s3_hr:
                ent_dt  = pd.Timestamp(df.iloc[ei]["datetime"])
                gap_min = (ent_dt - dt).total_seconds() / 60
                if use_gap and gap_min > 10:
                    skipped.append(dict(signal_dt=dt, entry_dt=ent_dt,
                                        month=mo, year=dt.year,
                                        h_raw=h_raw, gap_min=gap_min, wd=wd))
                    continue
                pnl, xdt, rsn = exec_trade(df, ei, "short", MICRO_TP, MICRO_SL,
                                            50, force_sc=False,
                                            entry_wd=wd, use_sat=use_sat)
                trades.append(dict(system="③", side="short", signal_dt=dt,
                                   entry_time=ent_dt, month=mo, year=dt.year,
                                   pnl_pt=pnl, pnl_yen=round(pnl*PT_TO_YEN),
                                   reason=rsn, gap_min=gap_min, wd=wd, h_raw=h_raw))

    return (pd.DataFrame(trades),
            pd.DataFrame(skipped) if skipped else pd.DataFrame(columns=["signal_dt","month","year","gap_min","h_raw"]))


def summary(t, label):
    if t is None or t.empty:
        print(f"  {label:40s}: 0件  0円")
        return
    n   = len(t)
    pyn = int(t["pnl_yen"].sum())
    wr  = (t["pnl_yen"] > 0).mean() * 100
    print(f"  {label:40s}: {n:4d}件  {pyn:+9,}円  勝率{wr:4.1f}%")


if __name__ == "__main__":
    print("XLSXロード中...")
    df     = load_xlsx()
    df     = add_indicators(df)
    cpi_df = load_cpi()
    print(f"  データ: {len(df)}本\n")

    print("BT実行中（4パターン）...")
    trA, _    = build_bt(df, cpi_df, use_gap=False, use_sat=False)   # 基準
    trB, skB  = build_bt(df, cpi_df, use_gap=True,  use_sat=False)   # gap_checkのみ
    trC, _    = build_bt(df, cpi_df, use_gap=False, use_sat=True)    # SAT_CLOSEのみ
    trD, skD  = build_bt(df, cpi_df, use_gap=True,  use_sat=True)    # micro_perf基準

    TARGET_YEAR  = 2026
    TARGET_MONTH = 3

    def m(tr, sys=None):
        if tr is None or tr.empty: return pd.DataFrame()
        t = tr[(tr["year"] == TARGET_YEAR) & (tr["month"] == TARGET_MONTH)]
        if sys:
            t = t[t["system"] == sys]
        return t

    print(f"\n{'='*65}")
    print(f"  2026年3月  全体（①＋③）")
    print(f"{'='*65}")
    summary(m(trA), "A: 基準 (gap無・SAT_CLOSE無)")
    summary(m(trB), "B: gap_checkのみ有効")
    summary(m(trC), "C: SAT_CLOSEのみ有効")
    summary(m(trD), "D: gap+SAT_CLOSE (micro_perf基準)")

    print(f"\n{'='*65}")
    print(f"  2026年3月  系統③のみ")
    print(f"{'='*65}")
    summary(m(trA,"③"), "A: 基準 (gap無・SAT_CLOSE無)")
    summary(m(trB,"③"), "B: gap_checkのみ有効")
    summary(m(trC,"③"), "C: SAT_CLOSEのみ有効")
    summary(m(trD,"③"), "D: gap+SAT_CLOSE (micro_perf基準)")

    # gap check 影響分離
    a3 = m(trA,"③"); b3 = m(trB,"③"); c3 = m(trC,"③"); d3 = m(trD,"③")
    gap_n   = len(a3) - len(b3)
    gap_yen = int(a3["pnl_yen"].sum()) - int(b3["pnl_yen"].sum() if not b3.empty else 0)
    sat_n   = 0
    sat_yen = int(a3["pnl_yen"].sum() if not a3.empty else 0) - int(c3["pnl_yen"].sum() if not c3.empty else 0)

    print(f"\n{'='*65}")
    print(f"  差異内訳（2026年3月 系統③）")
    print(f"{'='*65}")
    print(f"  gap_check由来  件数差: {gap_n:+d}件  損益差: {gap_yen:+,}円")
    print(f"  SAT_CLOSE由来  件数差:  0件  損益差: {sat_yen:+,}円  (同件数・exit変化)")

    # gap スキップ明細（2026年3月）
    if not skB.empty:
        sk_m = skB[(skB["year"] == TARGET_YEAR) & (skB["month"] == TARGET_MONTH)]
        if not sk_m.empty:
            print(f"\n  [gap_checkスキップ一覧 2026年3月] {len(sk_m)}件")
            print(f"  {'シグナル日時':20s}  {'h_raw':>5}  {'gap_min':>7}  {'曜日':>4}")
            days = {0:'月',1:'火',2:'水',3:'木',4:'金'}
            for _, r in sk_m.sort_values("signal_dt").iterrows():
                print(f"  {str(r['signal_dt']):20s}  {int(r['h_raw']):>5}  {r['gap_min']:>7.0f}  {days.get(int(r['wd']),'-'):>4}")

    # SAT_CLOSE 影響トレード（2026年3月）
    if not c3.empty:
        sat_trades = c3[c3["reason"] == "SAT_CLOSE"]
        if not sat_trades.empty:
            print(f"\n  [SAT_CLOSE確定トレード 2026年3月] {len(sat_trades)}件")
            for _, r in sat_trades.sort_values("entry_time").iterrows():
                a_pnl = a3[a3["entry_time"] == r["entry_time"]]["pnl_yen"].values
                a_str = f"{int(a_pnl[0]):+,}円" if len(a_pnl) > 0 else "N/A"
                print(f"  {str(r['entry_time']):20s}  SAT_CLOSE:{r['pnl_yen']:+,}円 / 基準:{a_str}")
        else:
            print(f"\n  [SAT_CLOSE確定トレード 2026年3月] 0件")

    # 年間サマリー（参考）
    print(f"\n{'='*65}")
    print(f"  2026年 全体サマリー（系統③ 年間）")
    print(f"{'='*65}")
    def yr(tr, sys=None):
        if tr is None or tr.empty: return pd.DataFrame()
        t = tr[tr["year"] == TARGET_YEAR]
        if sys: t = t[t["system"] == sys]
        return t
    summary(yr(trA,"③"), "A: 基準")
    summary(yr(trD,"③"), "D: micro_perf基準")
    gap_yr_n   = len(yr(trA,"③")) - len(yr(trB,"③"))
    gap_yr_yen = int(yr(trA,"③")["pnl_yen"].sum()) - int(yr(trB,"③")["pnl_yen"].sum() if not yr(trB,"③").empty else 0)
    sat_yr_yen = int(yr(trA,"③")["pnl_yen"].sum()) - int(yr(trC,"③")["pnl_yen"].sum() if not yr(trC,"③").empty else 0)
    print(f"\n  [年間 gap_check影響]  件数差:{gap_yr_n:+d}件  損益差:{gap_yr_yen:+,}円")
    print(f"  [年間 SAT_CLOSE影響]  件数差:  0件  損益差:{sat_yr_yen:+,}円")
