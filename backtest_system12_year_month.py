# 系統① パターンA vs B 比較バックテスト
# =====================================================
# パターンA: MA9 > MA20 > MA55（3本パーフェクトオーダー）
# パターンB: 直近2本closeがMA9・MA10より上
# 共通条件: long / 月木 / 18-23時 / 3月・7月除外 / SL60 / TP240

from pathlib import Path
import pandas as pd
import numpy as np

# =========================================
# 設定
# =========================================
DATA_DIR   = Path(r"C:\kabu_trade\data")
EXCEL_FILES = [
    "N225microf_2023.xlsx",
    "N225microf_2024.xlsx",
    "N225microf_2025.xlsx",
    "N225microf_2026.xlsx",
]
MICRO_CSV  = Path(r"C:\kabu_trade\micro_5min.csv")
RESULT_CSV = Path(r"C:\kabu_trade\s1_pattern_ab_result.csv")

SL           = 60
TP           = 240
MAX_HOLD     = 120
TOUCH_PCT    = 0.005
COMMISSION   = 2.2
PT_TO_YEN    = 10

WEEKDAYS     = (0, 3)
HOURS        = (18, 19, 20, 21, 22, 23)
EXCL_MONTHS  = (3, 7)

SESSION_BOUNDARIES = frozenset({1540, 600, 2350})


# =========================================
# データ読み込み
# =========================================
def read_excel(path: Path) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name="5min", engine="openpyxl")
    df = df.rename(columns={
        "日付": "date", "時間": "time",
        "始値": "open", "高値": "high", "安値": "low",
        "終値": "close", "出来高": "volume",
    })
    df["datetime"] = pd.to_datetime(
        df["date"].astype(str).str.strip() + " " + df["time"].astype(str).str.strip(),
        errors="coerce",
    )
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["datetime", "open", "high", "low", "close"]).copy()
    return df[["datetime", "open", "high", "low", "close", "volume"]].sort_values("datetime")


def load_data() -> pd.DataFrame:
    dfs = []
    print("データ読み込み中...")
    for fname in EXCEL_FILES:
        p = DATA_DIR / fname
        if not p.exists():
            print(f"  スキップ: {p}")
            continue
        df = read_excel(p)
        print(f"  {fname}: {len(df)} 本")
        dfs.append(df)

    if MICRO_CSV.exists():
        try:
            df_csv = pd.read_csv(MICRO_CSV, index_col="datetime", parse_dates=True)
            df_csv = df_csv.reset_index()
            if df_csv["datetime"].dt.tz is not None:
                df_csv["datetime"] = (df_csv["datetime"]
                                      .dt.tz_convert("Asia/Tokyo")
                                      .dt.tz_localize(None))
            # CSV は bar START 時刻。Excel も bar START 時刻なので +5min 不要
            # （+5min は hr 計算時に統一適用）
            for c in ["open", "high", "low", "close", "volume"]:
                if c in df_csv.columns:
                    df_csv[c] = pd.to_numeric(df_csv[c], errors="coerce")
            df_csv = (df_csv.dropna(subset=["datetime", "open", "high", "low", "close"])
                      [["datetime", "open", "high", "low", "close", "volume"]]
                      .sort_values("datetime"))
            print(f"  micro_5min.csv: {len(df_csv)} 本")
            dfs.append(df_csv)
        except Exception as e:
            print(f"  micro_5min.csv 読み込み失敗: {e}")

    if not dfs:
        raise FileNotFoundError("データファイルが見つかりません")

    df = (pd.concat(dfs, ignore_index=True)
          .sort_values("datetime")
          .drop_duplicates(subset=["datetime"])
          .reset_index(drop=True))
    print(f"合計: {len(df)} 本  ({df['datetime'].min()} 〜 {df['datetime'].max()})\n")
    return df


# =========================================
# 指標計算
# =========================================
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ma9"]  = df["close"].rolling(9).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma55"] = df["close"].rolling(55).mean()
    ema_fast       = df["close"].ewm(span=12, adjust=False).mean()
    ema_slow       = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"]     = ema_fast - ema_slow
    df["macd_sig"] = df["macd"].ewm(span=9, adjust=False).mean()
    return df


# =========================================
# バックテスト
# =========================================
def run_backtest(df: pd.DataFrame, pattern: str) -> pd.DataFrame:
    arr_open    = df["open"].values
    arr_high    = df["high"].values
    arr_low     = df["low"].values
    arr_close   = df["close"].values
    arr_ma9     = df["ma9"].values
    arr_ma10    = df["ma10"].values
    arr_ma20    = df["ma20"].values
    arr_ma55    = df["ma55"].values
    arr_macd    = df["macd"].values
    arr_msig    = df["macd_sig"].values
    dts         = pd.to_datetime(df["datetime"])
    arr_hour    = dts.dt.hour.values
    arr_minute  = dts.dt.minute.values
    arr_weekday = dts.dt.weekday.values
    arr_month   = dts.dt.month.values
    arr_hm      = arr_hour * 100 + arr_minute
    dt_list     = dts.to_list()

    n = len(df)
    trades = []

    for i in range(2, n - 1):
        ma9  = arr_ma9[i]
        ma10 = arr_ma10[i]
        ma20 = arr_ma20[i]
        ma55 = arr_ma55[i]
        macd = arr_macd[i]
        msig = arr_msig[i]
        lo   = arr_low[i]
        # hr は足終了時刻基準（bar START + 5min）で auto_trade.py と統一
        hr   = (arr_hour[i] * 60 + arr_minute[i] + 5) // 60 % 24
        wd   = arr_weekday[i]
        mo   = arr_month[i]

        # 共通フィルター先行チェック（高速化）
        if hr not in HOURS or wd not in WEEKDAYS or mo in EXCL_MONTHS:
            continue

        if pattern == "A":
            # NaN チェック
            if any(np.isnan(v) for v in [ma9, ma20, ma55, macd, msig]):
                continue
            # MA9 > MA20 > MA55 パーフェクトオーダー
            if not (ma9 > ma20 > ma55):
                continue
            # low が MA9 にタッチ（±0.5%）
            if abs(lo - ma9) / ma9 > TOUCH_PCT:
                continue
        else:
            # パターンB
            ma9p  = arr_ma9[i-1];  ma10p  = arr_ma10[i-1]
            ma9p2 = arr_ma9[i-2];  ma10p2 = arr_ma10[i-2]
            c1    = arr_close[i-1]; c2    = arr_close[i-2]
            if any(np.isnan(v) for v in [ma9, ma10, ma9p, ma10p, ma9p2, ma10p2, macd, msig]):
                continue
            # 直近2本 close が MA9・MA10 の両方より上
            if not (c2 > ma9p2 and c2 > ma10p2 and c1 > ma9p and c1 > ma10p):
                continue
            # low が MA9 OR MA10 にタッチ（±0.5%）
            if not (abs(lo - ma9) / ma9 <= TOUCH_PCT or
                    abs(lo - ma10) / ma10 <= TOUCH_PCT):
                continue

        # MACD GC（共通）
        if macd <= msig:
            continue

        # エントリー（次足 open）
        ei   = i + 1
        ep   = arr_open[ei]
        pnl  = None
        rtype = None
        exit_bar = ei

        for j in range(ei, min(ei + MAX_HOLD, n)):
            bhi = arr_high[j]; blo = arr_low[j]
            if bhi >= ep + TP:
                pnl, rtype, exit_bar = float(TP), "TP", j; break
            if blo <= ep - SL:
                pnl, rtype, exit_bar = float(-SL), "SL", j; break
            if arr_hm[j] in SESSION_BOUNDARIES:
                pnl = float(arr_close[j] - ep)
                rtype, exit_bar = "SESSION", j; break

        if pnl is None:
            close_idx = min(ei + MAX_HOLD - 1, n - 1)
            pnl = float(arr_close[close_idx] - ep)
            rtype, exit_bar = "TIME", close_idx

        pnl -= COMMISSION

        trades.append({
            "pattern":        pattern,
            "signal_dt":      dt_list[i],
            "entry_dt":       dt_list[ei],
            "exit_dt":        dt_list[exit_bar],
            "entry_price":    ep,
            "pnl_pt":         pnl,
            "pnl_yen":        round(pnl * PT_TO_YEN, 0),
            "result":         rtype,
            "signal_hour":    int(hr),
            "signal_weekday": int(wd),
            "signal_month":   int(mo),
            "signal_year":    dts.iloc[i].year,
        })

    if not trades:
        return pd.DataFrame()
    df_t = pd.DataFrame(trades)
    df_t["entry_dt"]  = pd.to_datetime(df_t["entry_dt"])
    df_t["exit_dt"]   = pd.to_datetime(df_t["exit_dt"])
    df_t["signal_dt"] = pd.to_datetime(df_t["signal_dt"])
    return df_t.reset_index(drop=True)


# =========================================
# 集計
# =========================================
def calc_summary(df: pd.DataFrame) -> dict:
    if len(df) == 0:
        return {"n": 0, "win_rate": 0.0, "pnl_pt": 0.0,
                "pnl_yen": 0.0, "ev_pt": 0.0, "pf": 0.0}
    pnl  = df["pnl_pt"].values
    wins = pnl[pnl > 0].sum()
    loss = abs(pnl[pnl < 0].sum())
    n    = len(df)
    return {
        "n":        n,
        "win_rate": float((pnl > 0).mean() * 100),
        "pnl_pt":   float(pnl.sum()),
        "pnl_yen":  float(pnl.sum() * PT_TO_YEN),
        "ev_pt":    float(pnl.sum() / n),
        "pf":       float(wins / loss) if loss > 0 else float("inf"),
    }


SEP  = "=" * 72
SEP2 = "-" * 72


def pf_str(v):
    return f"{v:.3f}" if v != float("inf") else "inf"


def print_overall(s: dict, label: str):
    print(f"  {label}")
    print(f"    件数:{s['n']:>5}  勝率:{s['win_rate']:>5.1f}%  "
          f"損益:{s['pnl_pt']:>+8.1f}pt / {s['pnl_yen']:>+10,.0f}円  "
          f"期待値:{s['ev_pt']:>+6.2f}pt  PF:{pf_str(s['pf'])}")


def print_yearly(df: pd.DataFrame, label: str):
    print(f"  【年別成績】{label}")
    print(f"  {'年':>4}  {'件数':>5}  {'勝率%':>6}  {'損益(pt)':>9}  "
          f"{'損益(円)':>10}  {'PF':>6}")
    print("  " + SEP2[:62])
    for yr in sorted(df["signal_year"].unique()):
        s = calc_summary(df[df["signal_year"] == yr])
        print(f"  {yr}  {s['n']:>5}  {s['win_rate']:>5.1f}%  "
              f"{s['pnl_pt']:>+9.1f}  {s['pnl_yen']:>+10,.0f}  {pf_str(s['pf']):>6}")


def print_hourly(df: pd.DataFrame, label: str):
    print(f"  【時間帯別成績】{label}")
    print(f"  {'時間':>5}  {'件数':>5}  {'勝率%':>6}  {'損益(pt)':>9}  "
          f"{'損益(円)':>10}  {'PF':>6}")
    print("  " + SEP2[:62])
    for hr in sorted(df["signal_hour"].unique()):
        s = calc_summary(df[df["signal_hour"] == hr])
        print(f"  {hr:02d}h    {s['n']:>5}  {s['win_rate']:>5.1f}%  "
              f"{s['pnl_pt']:>+9.1f}  {s['pnl_yen']:>+10,.0f}  {pf_str(s['pf']):>6}")


def print_cross(df: pd.DataFrame, label: str):
    all_months = [m for m in range(1, 13) if m not in EXCL_MONTHS]
    # 損益（円）
    print(f"  【年x月クロス集計 損益(円)】{label}")
    hdr = "  年  " + "".join(f"  {m:>5}月" for m in all_months)
    print(hdr)
    print("  " + SEP2[:len(hdr)])
    pivot = df.pivot_table(
        values="pnl_yen", index="signal_year",
        columns="signal_month", aggfunc="sum", fill_value=0
    )
    for yr in sorted(df["signal_year"].unique()):
        vals = []
        for m in all_months:
            v = int(pivot.loc[yr, m]) if (yr in pivot.index and m in pivot.columns) else 0
            vals.append(f"{v:>+7,}")
        print(f"  {yr}  " + "  ".join(vals))

    # 件数
    print(f"\n  【年x月クロス集計 件数】{label}")
    print(hdr)
    print("  " + SEP2[:len(hdr)])
    pivot_n = df.pivot_table(
        values="pnl_yen", index="signal_year",
        columns="signal_month", aggfunc="count", fill_value=0
    )
    for yr in sorted(df["signal_year"].unique()):
        vals = []
        for m in all_months:
            v = int(pivot_n.loc[yr, m]) if (yr in pivot_n.index and m in pivot_n.columns) else 0
            vals.append(f"{v:>7}")
        print(f"  {yr}  " + "  ".join(vals))


# =========================================
# メイン
# =========================================
def main():
    print(SEP)
    print("  系統① パターンA vs B 比較バックテスト")
    print(f"  SL:{SL}pt  TP:{TP}pt  手数料:{COMMISSION}pt  曜日:月木  時間帯:18-23h  除外月:3・7月")
    print(SEP)
    print()
    print("【パターン条件】")
    print("  A: MA9 > MA20 > MA55（3本PO）+ lowがMA9にタッチ(±0.5%)")
    print("  B: 直近2本closeがMA9・MA10より上 + lowがMA9/MA10どちらかにタッチ(±0.5%)")
    print()

    df = load_data()
    df = add_indicators(df)

    print("バックテスト実行中...")
    ta = run_backtest(df, "A")
    print(f"  パターンA: {len(ta)} 件")
    tb = run_backtest(df, "B")
    print(f"  パターンB: {len(tb)} 件\n")

    sa = calc_summary(ta)
    sb = calc_summary(tb)

    # ── パターンA ──
    print(f"\n{SEP}")
    print("  パターンA: MA9 > MA20 > MA55 パーフェクトオーダー")
    print(SEP)
    print("\n1. 全体成績")
    print_overall(sa, "パターンA")
    print("\n2. 年×月クロス集計")
    if len(ta) > 0:
        print_cross(ta, "パターンA")
    print("\n3. 年別成績")
    if len(ta) > 0:
        print_yearly(ta, "パターンA")
    print("\n4. 時間帯別成績")
    if len(ta) > 0:
        print_hourly(ta, "パターンA")

    # ── パターンB ──
    print(f"\n{SEP}")
    print("  パターンB: 直近2本close > MA9・MA10（auto_trade.py 現行条件）")
    print(SEP)
    print("\n1. 全体成績")
    print_overall(sb, "パターンB")
    print("\n2. 年×月クロス集計")
    if len(tb) > 0:
        print_cross(tb, "パターンB")
    print("\n3. 年別成績")
    if len(tb) > 0:
        print_yearly(tb, "パターンB")
    print("\n4. 時間帯別成績")
    if len(tb) > 0:
        print_hourly(tb, "パターンB")

    # ── 比較サマリー ──
    print(f"\n{SEP}")
    print("  5. パターンA vs B 比較サマリー")
    print(SEP)
    dpf  = sa['pf'] - sb['pf']
    dyen = sa['pnl_yen'] - sb['pnl_yen']
    dn   = sa['n'] - sb['n']
    dev  = sa['ev_pt'] - sb['ev_pt']
    dwr  = sa['win_rate'] - sb['win_rate']
    print(f"\n  {'':>18}  {'パターンA':>12}  {'パターンB':>12}  {'差(A-B)':>12}")
    print("  " + SEP2[:60])
    print(f"  {'件数':>18}  {sa['n']:>12}  {sb['n']:>12}  {dn:>+12}")
    print(f"  {'勝率(%)':>18}  {sa['win_rate']:>11.1f}%  {sb['win_rate']:>11.1f}%  {dwr:>+11.1f}%")
    print(f"  {'損益(pt)':>18}  {sa['pnl_pt']:>+12.1f}  {sb['pnl_pt']:>+12.1f}  "
          f"{sa['pnl_pt']-sb['pnl_pt']:>+12.1f}")
    print(f"  {'損益(円)':>18}  {sa['pnl_yen']:>+12,.0f}  {sb['pnl_yen']:>+12,.0f}  "
          f"{dyen:>+12,.0f}")
    print(f"  {'期待値(pt)':>18}  {sa['ev_pt']:>+12.2f}  {sb['ev_pt']:>+12.2f}  {dev:>+12.2f}")
    print(f"  {'PF':>18}  {pf_str(sa['pf']):>12}  {pf_str(sb['pf']):>12}  {dpf:>+12.3f}")
    print()

    # ── CSV 保存 ──
    all_df = pd.concat([ta, tb], ignore_index=True)
    all_df.to_csv(RESULT_CSV, index=False, encoding="utf-8-sig")
    print(f"[OK] 全トレード保存: {RESULT_CSV}")

    rows = []
    for pattern, df_t, s in [("A", ta, sa), ("B", tb, sb)]:
        rows.append({"pattern": pattern, "year": "全期間",
                     "n": s["n"], "win_rate": round(s["win_rate"], 1),
                     "pnl_pt": round(s["pnl_pt"], 1), "pnl_yen": round(s["pnl_yen"], 0),
                     "ev_pt": round(s["ev_pt"], 2), "pf": round(s["pf"], 3)})
        if len(df_t) > 0:
            for yr in sorted(df_t["signal_year"].unique()):
                s_yr = calc_summary(df_t[df_t["signal_year"] == yr])
                rows.append({"pattern": pattern, "year": yr,
                             "n": s_yr["n"], "win_rate": round(s_yr["win_rate"], 1),
                             "pnl_pt": round(s_yr["pnl_pt"], 1),
                             "pnl_yen": round(s_yr["pnl_yen"], 0),
                             "ev_pt": round(s_yr["ev_pt"], 2),
                             "pf": round(s_yr["pf"], 3)})
    summary_df = pd.DataFrame(rows)
    summary_path = RESULT_CSV.with_name("s1_pattern_ab_summary.csv")
    summary_df.to_csv(summary_path, index=False, encoding="utf-8-sig")
    print(f"[OK] サマリー保存: {summary_path}")


WEEKDAY_NAME = {0: "月", 1: "火", 2: "水", 3: "木", 4: "金"}


def run_backtest_b_nofilter(df: pd.DataFrame) -> pd.DataFrame:
    """パターンB条件のみ・曜日/時間帯/除外月フィルターなし"""
    arr_open    = df["open"].values
    arr_high    = df["high"].values
    arr_low     = df["low"].values
    arr_close   = df["close"].values
    arr_ma9     = df["ma9"].values
    arr_ma10    = df["ma10"].values
    arr_macd    = df["macd"].values
    arr_msig    = df["macd_sig"].values
    dts         = pd.to_datetime(df["datetime"])
    arr_hour    = dts.dt.hour.values
    arr_minute  = dts.dt.minute.values
    arr_weekday = dts.dt.weekday.values
    arr_month   = dts.dt.month.values
    arr_hm      = arr_hour * 100 + arr_minute
    dt_list     = dts.to_list()

    n = len(df)
    trades = []

    for i in range(2, n - 1):
        ma9   = arr_ma9[i];    ma10  = arr_ma10[i]
        ma9p  = arr_ma9[i-1];  ma10p = arr_ma10[i-1]
        ma9p2 = arr_ma9[i-2];  ma10p2= arr_ma10[i-2]
        c1    = arr_close[i-1]; c2   = arr_close[i-2]
        macd  = arr_macd[i];   msig  = arr_msig[i]
        lo    = arr_low[i]

        if any(np.isnan(v) for v in [ma9, ma10, ma9p, ma10p, ma9p2, ma10p2, macd, msig]):
            continue

        # パターンB シグナル条件（フィルターなし）
        if not (c2 > ma9p2 and c2 > ma10p2 and c1 > ma9p and c1 > ma10p):
            continue
        if not (abs(lo - ma9) / ma9 <= TOUCH_PCT or
                abs(lo - ma10) / ma10 <= TOUCH_PCT):
            continue
        if macd <= msig:
            continue

        ei   = i + 1
        ep   = arr_open[ei]
        pnl  = None
        rtype = None
        exit_bar = ei

        for j in range(ei, min(ei + MAX_HOLD, n)):
            bhi = arr_high[j]; blo = arr_low[j]
            if bhi >= ep + TP:
                pnl, rtype, exit_bar = float(TP), "TP", j; break
            if blo <= ep - SL:
                pnl, rtype, exit_bar = float(-SL), "SL", j; break
            if arr_hm[j] in SESSION_BOUNDARIES:
                pnl = float(arr_close[j] - ep)
                rtype, exit_bar = "SESSION", j; break

        if pnl is None:
            close_idx = min(ei + MAX_HOLD - 1, n - 1)
            pnl = float(arr_close[close_idx] - ep)
            rtype, exit_bar = "TIME", close_idx

        pnl -= COMMISSION

        trades.append({
            "signal_dt":      dt_list[i],
            "entry_dt":       dt_list[ei],
            "pnl_pt":         pnl,
            "pnl_yen":        round(pnl * PT_TO_YEN, 0),
            "result":         rtype,
            # hr は足終了時刻基準（bar START + 5min）
            "signal_hour":    int((arr_hour[i] * 60 + arr_minute[i] + 5) // 60 % 24),
            "signal_weekday": int(arr_weekday[i]),
            "signal_month":   int(arr_month[i]),
            "signal_year":    dts.iloc[i].year,
        })

    if not trades:
        return pd.DataFrame()
    df_t = pd.DataFrame(trades)
    df_t["entry_dt"]  = pd.to_datetime(df_t["entry_dt"])
    df_t["signal_dt"] = pd.to_datetime(df_t["signal_dt"])
    return df_t.reset_index(drop=True)


def print_breakdown(df: pd.DataFrame):
    """時間帯別・曜日別・月別・年x月クロス集計を出力"""
    SEP_L = "=" * 72
    SEP_S = "-" * 62

    def row(s):
        return (f"  {s['n']:>5}  {s['win_rate']:>5.1f}%  "
                f"{s['pnl_pt']:>+9.1f}  {s['pnl_yen']:>+10,.0f}  "
                f"{s['ev_pt']:>+7.2f}  {pf_str(s['pf']):>6}")

    hdr = f"  {'件数':>5}  {'勝率%':>6}  {'損益(pt)':>9}  {'損益(円)':>10}  {'期待値':>7}  {'PF':>6}"

    # 1. 時間帯別（全時間帯）
    print(f"\n{SEP_L}")
    print("  [追加] パターンB フィルターなし 時間帯別成績（全時間帯）")
    print(SEP_L)
    print(f"  {'時間帯':>5}" + hdr)
    print("  " + SEP_S)
    for hr in range(24):
        grp = df[df["signal_hour"] == hr]
        if len(grp) == 0:
            continue
        s = calc_summary(grp)
        print(f"  {hr:02d}h  " + row(s))

    # 2. 曜日別（全曜日）
    print(f"\n{SEP_L}")
    print("  [追加] パターンB フィルターなし 曜日別成績")
    print(SEP_L)
    print(f"  {'曜日':>4}" + hdr)
    print("  " + SEP_S)
    for wd in range(5):
        grp = df[df["signal_weekday"] == wd]
        if len(grp) == 0:
            continue
        s = calc_summary(grp)
        name = WEEKDAY_NAME.get(wd, str(wd))
        print(f"  {name}曜  " + row(s))

    # 3. 月別（全月）
    print(f"\n{SEP_L}")
    print("  [追加] パターンB フィルターなし 月別成績")
    print(SEP_L)
    print(f"  {'月':>3}" + hdr)
    print("  " + SEP_S)
    for mo in range(1, 13):
        grp = df[df["signal_month"] == mo]
        if len(grp) == 0:
            continue
        s = calc_summary(grp)
        print(f"  {mo:02d}月" + row(s))

    # 4. 年×月クロス集計（損益円）
    print(f"\n{SEP_L}")
    print("  [追加] パターンB フィルターなし 年x月クロス集計 損益(円)")
    print(SEP_L)
    months = list(range(1, 13))
    hdr_cross = "  年    " + "".join(f"  {m:>5}月" for m in months)
    print(hdr_cross)
    print("  " + "-" * (len(hdr_cross) - 2))
    pivot = df.pivot_table(
        values="pnl_yen", index="signal_year",
        columns="signal_month", aggfunc="sum", fill_value=0
    )
    for yr in sorted(df["signal_year"].unique()):
        vals = []
        for m in months:
            v = int(pivot.loc[yr, m]) if (yr in pivot.index and m in pivot.columns) else 0
            vals.append(f"{v:>+7,}")
        print(f"  {yr}  " + "  ".join(vals))

    # 全体サマリー
    s_all = calc_summary(df)
    print(f"\n  全体: 件数{s_all['n']}  損益{s_all['pnl_yen']:+,.0f}円  PF{pf_str(s_all['pf'])}")


FILTER_HOURS = frozenset({8, 12, 15, 18, 19, 20, 21, 23})
EXCL_HOURS   = frozenset({0, 1, 2, 3, 4, 5, 6, 7, 9, 10, 11, 13, 14, 16, 17})


def print_breakdown_filtered(df_all: pd.DataFrame, df_filt: pd.DataFrame):
    """時間帯フィルター適用後の各種集計を出力"""
    SEP_L = "=" * 72
    SEP_S = "-" * 62

    def row(s):
        return (f"  {s['n']:>5}  {s['win_rate']:>5.1f}%  "
                f"{s['pnl_pt']:>+9.1f}  {s['pnl_yen']:>+10,.0f}  "
                f"{s['ev_pt']:>+7.2f}  {pf_str(s['pf']):>6}")

    hdr = f"  {'件数':>5}  {'勝率%':>6}  {'損益(pt)':>9}  {'損益(円)':>10}  {'期待値':>7}  {'PF':>6}"

    s_all  = calc_summary(df_all)
    s_filt = calc_summary(df_filt)

    # 0. 全体比較
    print(f"\n{SEP_L}")
    print("  [フィルター後] 全体サマリー比較")
    print(SEP_L)
    print(f"  残す時間帯: {sorted(FILTER_HOURS)}")
    print(f"  除外時間帯: {sorted(EXCL_HOURS)}")
    print(f"\n  {'':>12}" + hdr)
    print("  " + SEP_S)
    print(f"  {'フィルターなし':>12}" + row(s_all))
    print(f"  {'フィルター後  ':>12}" + row(s_filt))
    dn   = s_filt['n']   - s_all['n']
    dyen = s_filt['pnl_yen'] - s_all['pnl_yen']
    dpf  = s_filt['pf']  - s_all['pf']
    dev  = s_filt['ev_pt'] - s_all['ev_pt']
    print(f"  {'差(後-前)    ':>12}  {dn:>5}  {'':>6}  {'':>9}  {dyen:>+10,.0f}  {dev:>+7.2f}  {dpf:>+6.3f}")

    # 1. 時間帯別（フィルター後：残した時間帯のみ）
    print(f"\n{SEP_L}")
    print("  [フィルター後] 時間帯別成績")
    print(SEP_L)
    print(f"  {'時間帯':>5}" + hdr)
    print("  " + SEP_S)
    for hr in sorted(FILTER_HOURS):
        grp = df_filt[df_filt["signal_hour"] == hr]
        if len(grp) == 0:
            continue
        s = calc_summary(grp)
        print(f"  {hr:02d}h  " + row(s))

    # 2. 曜日別（フィルター後）
    print(f"\n{SEP_L}")
    print("  [フィルター後] 曜日別成績")
    print(SEP_L)
    print(f"  {'曜日':>4}" + hdr)
    print("  " + SEP_S)
    for wd in range(5):
        grp_all  = df_all[df_all["signal_weekday"] == wd]
        grp_filt = df_filt[df_filt["signal_weekday"] == wd]
        if len(grp_filt) == 0:
            continue
        s_a = calc_summary(grp_all)
        s_f = calc_summary(grp_filt)
        name = WEEKDAY_NAME.get(wd, str(wd))
        print(f"  {name}曜(前)" + row(s_a))
        print(f"  {name}曜(後)" + row(s_f))
        dpf_wd = s_f['pf'] - s_a['pf']
        print(f"  {'  差    ':>5}  {s_f['n']-s_a['n']:>5}  {'':>6}  {'':>9}  "
              f"{s_f['pnl_yen']-s_a['pnl_yen']:>+10,.0f}  "
              f"{s_f['ev_pt']-s_a['ev_pt']:>+7.2f}  {dpf_wd:>+6.3f}")
        print("  " + "-" * 50)

    # 3. 月別（フィルター後）
    print(f"\n{SEP_L}")
    print("  [フィルター後] 月別成績")
    print(SEP_L)
    print(f"  {'月':>6}  {'区分':>6}" + hdr)
    print("  " + SEP_S)
    for mo in range(1, 13):
        grp_all  = df_all[df_all["signal_month"] == mo]
        grp_filt = df_filt[df_filt["signal_month"] == mo]
        if len(grp_filt) == 0 and len(grp_all) == 0:
            continue
        s_a = calc_summary(grp_all)
        s_f = calc_summary(grp_filt)
        dpf_mo = s_f['pf'] - s_a['pf']
        print(f"  {mo:02d}月(前)  " + row(s_a))
        print(f"  {mo:02d}月(後)  " + row(s_f))
        print(f"  {'  差  ':>6}  {'':>6}  {s_f['n']-s_a['n']:>5}  {'':>6}  {'':>9}  "
              f"{s_f['pnl_yen']-s_a['pnl_yen']:>+10,.0f}  "
              f"{s_f['ev_pt']-s_a['ev_pt']:>+7.2f}  {dpf_mo:>+6.3f}")
        print("  " + "-" * 50)


def main_extra(df_with_indicators: pd.DataFrame):
    print(f"\n{'=' * 72}")
    print("  パターンB 追加分析（フィルターなし・全データ対象）")
    print(f"{'=' * 72}")
    print("  条件: close>MA9/MA10(直近2本) + lowタッチ + MACD GC + 手数料2.2pt")
    print("  ※曜日・時間帯・除外月フィルターなし")

    tb_nf = run_backtest_b_nofilter(df_with_indicators)
    print(f"  総件数: {len(tb_nf)}")
    if len(tb_nf) > 0:
        print_breakdown(tb_nf)

    # 時間帯フィルター適用後分析
    if len(tb_nf) > 0:
        tb_filtered = tb_nf[
            tb_nf["signal_hour"].isin(FILTER_HOURS) &
            (tb_nf["signal_weekday"] != 4)
        ].copy()
        print(f"\n  時間帯+金曜除外フィルター後件数: {len(tb_filtered)}")
        print_breakdown_filtered(tb_nf, tb_filtered)

        # 👇ここにだけ置く
        print_month_hour_cross(tb_filtered) 
        analyze_hour_impact_on_weekday(tb_filtered)       

def print_month_hour_cross(df: pd.DataFrame):
    print("\n" + "=" * 72)
    print("  [追加] 年×月×時間帯 クロス集計（損益円）")
    print("=" * 72)

    pivot = df.pivot_table(
        values="pnl_yen",
        index=["signal_year", "signal_month"],
        columns="signal_hour",
        aggfunc="sum",
        fill_value=0
    )

    for (yr, mo) in sorted(pivot.index):
        row = pivot.loc[(yr, mo)]
        vals = "  ".join([f"{int(v):>+7,}" for v in row.values])
        hours = "  ".join([f"{h:02d}h" for h in row.index])
        print(f"\n{yr}年 {mo:02d}月")
        print("  " + hours)
        print("  " + vals)   

def analyze_hour_impact_on_weekday(df: pd.DataFrame):
    print("\n" + "=" * 72)
    print("  [追加] 時間削除インパクト（曜日PF変化）")
    print("=" * 72)

    # 元PF
    print("\n【元データ】")
    for wd in range(5):
        s = calc_summary(df[df["signal_weekday"] == wd])
        name = WEEKDAY_NAME.get(wd, str(wd))
        print(f"  {name}曜 PF: {pf_str(s['pf'])}")

    print("\n【各時間削除後】")
    for hr in sorted(df["signal_hour"].unique()):
        df_cut = df[df["signal_hour"] != hr].copy()

        print(f"\n--- {hr:02d}h削除 ---")
        for wd in range(5):
            grp = df_cut[df_cut["signal_weekday"] == wd]
            if len(grp) == 0:
                continue
            s = calc_summary(grp)
            name = WEEKDAY_NAME.get(wd, str(wd))
            print(f"  {name}曜 PF: {pf_str(s['pf'])}")


# =========================================
# 系統① カスタム条件バックテスト
# =========================================
def run_backtest_b_custom(df: pd.DataFrame,
                          weekdays: tuple,
                          hours: tuple,
                          excl_months: tuple) -> pd.DataFrame:
    """パターンB（系統①条件）・曜日/時間帯/除外月をパラメータで指定"""
    arr_open    = df["open"].values
    arr_high    = df["high"].values
    arr_low     = df["low"].values
    arr_close   = df["close"].values
    arr_ma9     = df["ma9"].values
    arr_ma10    = df["ma10"].values
    arr_macd    = df["macd"].values
    arr_msig    = df["macd_sig"].values
    dts         = pd.to_datetime(df["datetime"])
    arr_hour    = dts.dt.hour.values
    arr_minute  = dts.dt.minute.values
    arr_weekday = dts.dt.weekday.values
    arr_month   = dts.dt.month.values
    arr_hm      = arr_hour * 100 + arr_minute
    dt_list     = dts.to_list()

    wd_set  = frozenset(weekdays)
    hr_set  = frozenset(hours)
    mo_excl = frozenset(excl_months)

    n = len(df)
    trades = []

    for i in range(2, n - 1):
        ma9   = arr_ma9[i];   ma10  = arr_ma10[i]
        ma9p  = arr_ma9[i-1]; ma10p = arr_ma10[i-1]
        ma9p2 = arr_ma9[i-2]; ma10p2= arr_ma10[i-2]
        c1    = arr_close[i-1]; c2  = arr_close[i-2]
        macd  = arr_macd[i];  msig  = arr_msig[i]
        lo    = arr_low[i]

        if any(np.isnan(v) for v in [ma9, ma10, ma9p, ma10p, ma9p2, ma10p2, macd, msig]):
            continue

        hr = (arr_hour[i] * 60 + arr_minute[i] + 5) // 60 % 24
        wd = arr_weekday[i]
        mo = arr_month[i]

        if wd not in wd_set or hr not in hr_set or mo in mo_excl:
            continue

        if not (c2 > ma9p2 and c2 > ma10p2 and c1 > ma9p and c1 > ma10p):
            continue
        if not (abs(lo - ma9) / ma9 <= TOUCH_PCT or
                abs(lo - ma10) / ma10 <= TOUCH_PCT):
            continue
        if macd <= msig:
            continue

        ei   = i + 1
        ep   = arr_open[ei]
        pnl  = None
        rtype = None
        exit_bar = ei

        for j in range(ei, min(ei + MAX_HOLD, n)):
            bhi = arr_high[j]; blo = arr_low[j]
            if bhi >= ep + TP:
                pnl, rtype, exit_bar = float(TP),  "TP",  j; break
            if blo <= ep - SL:
                pnl, rtype, exit_bar = float(-SL), "SL",  j; break
            if arr_hm[j] in SESSION_BOUNDARIES:
                pnl = float(arr_close[j] - ep)
                rtype, exit_bar = "SESSION", j; break

        if pnl is None:
            close_idx = min(ei + MAX_HOLD - 1, n - 1)
            pnl = float(arr_close[close_idx] - ep)
            rtype, exit_bar = "TIME", close_idx

        pnl -= COMMISSION

        trades.append({
            "signal_dt":      dt_list[i],
            "entry_dt":       dt_list[ei],
            "pnl_pt":         pnl,
            "pnl_yen":        round(pnl * PT_TO_YEN, 0),
            "result":         rtype,
            "signal_hour":    int(hr),
            "signal_weekday": int(wd),
            "signal_month":   int(mo),
            "signal_year":    dts.iloc[i].year,
        })

    if not trades:
        return pd.DataFrame()
    df_t = pd.DataFrame(trades)
    df_t["entry_dt"]  = pd.to_datetime(df_t["entry_dt"])
    df_t["signal_dt"] = pd.to_datetime(df_t["signal_dt"])
    return df_t.reset_index(drop=True)


# =========================================
# ビフォー / アフター 出力
# =========================================
def _print_s1_detail(df: pd.DataFrame, label: str):
    """系統① 単体の詳細成績（全体/年別/年×月/時間帯別/曜日別）"""
    W = "=" * 72
    D = "-" * 62

    def row(s):
        return (f"  {s['n']:>5}  {s['win_rate']:>5.1f}%  "
                f"{s['pnl_pt']:>+9.1f}  {s['pnl_yen']:>+10,.0f}  "
                f"{s['ev_pt']:>+7.2f}  {pf_str(s['pf']):>6}")

    hdr = (f"  {'件数':>5}  {'勝率%':>6}  {'損益(pt)':>9}  "
           f"{'損益(円)':>10}  {'期待値':>7}  {'PF':>6}")

    # 1. 全体成績
    print(f"\n{W}")
    print(f"  {label} - 1. 全体成績")
    print(W)
    s = calc_summary(df)
    print(hdr)
    print("  " + D)
    print(row(s))

    # 2. 年別成績
    print(f"\n{W}")
    print(f"  {label} - 2. 年別成績")
    print(W)
    print(f"  {'年':>4}" + hdr)
    print("  " + D)
    for yr in sorted(df["signal_year"].unique()):
        s = calc_summary(df[df["signal_year"] == yr])
        print(f"  {yr}" + row(s))

    # 3. 年×月クロス集計
    print(f"\n{W}")
    print(f"  {label} - 3. 年×月クロス集計（損益円）")
    print(W)
    months = list(range(1, 13))
    hdr_cross = "  年    " + "".join(f"  {m:>5}月" for m in months) + "    合計"
    print(hdr_cross)
    print("  " + "-" * (len(hdr_cross) - 2))
    pivot = df.pivot_table(
        values="pnl_yen", index="signal_year",
        columns="signal_month", aggfunc="sum", fill_value=0
    )
    for yr in sorted(df["signal_year"].unique()):
        vals = []
        total = 0
        for m in months:
            v = int(pivot.loc[yr, m]) if (yr in pivot.index and m in pivot.columns) else 0
            vals.append(f"{v:>+7,}")
            total += v
        print(f"  {yr}  " + "  ".join(vals) + f"  {total:>+8,}")
    # 月合計行
    vals = []
    total = 0
    for m in months:
        v = int(df[df["signal_month"] == m]["pnl_yen"].sum())
        vals.append(f"{v:>+7,}")
        total += v
    print("  " + "-" * (len(hdr_cross) - 2))
    print(f"  計     " + "  ".join(vals) + f"  {total:>+8,}")

    # 4. 時間帯別成績
    print(f"\n{W}")
    print(f"  {label} - 4. 時間帯別成績")
    print(W)
    print(f"  {'時間帯':>5}" + hdr)
    print("  " + D)
    for hr in range(24):
        grp = df[df["signal_hour"] == hr]
        if len(grp) == 0:
            continue
        s = calc_summary(grp)
        print(f"  {hr:02d}h  " + row(s))

    # 5. 曜日別成績
    print(f"\n{W}")
    print(f"  {label} - 5. 曜日別成績")
    print(W)
    print(f"  {'曜日':>4}" + hdr)
    print("  " + D)
    for wd in range(5):
        grp = df[df["signal_weekday"] == wd]
        if len(grp) == 0:
            continue
        s = calc_summary(grp)
        name = WEEKDAY_NAME.get(wd, str(wd))
        print(f"  {name}曜  " + row(s))


def main_comparison(df_with_indicators: pd.DataFrame):
    W = "=" * 72

    # ── ビフォー ──
    BEFORE_WEEKDAYS   = (0, 3)
    BEFORE_HOURS      = (18, 19, 20, 21, 22, 23)
    BEFORE_EXCL_MONTHS = (3, 7)

    # ── アフター ──
    AFTER_WEEKDAYS    = (0, 1, 2, 3)
    AFTER_HOURS       = (8, 12, 15, 18, 19, 20, 21, 23)
    AFTER_EXCL_MONTHS = (7,)

    print(f"\n{W}")
    print("  系統① ビフォー vs アフター 比較バックテスト")
    print(W)
    print(f"\n  【ビフォー条件】")
    print(f"    曜日: 月・木  時間帯: {list(BEFORE_HOURS)}  除外月: {list(BEFORE_EXCL_MONTHS)}")
    print(f"  【アフター条件】")
    print(f"    曜日: 月・火・水・木  時間帯: {list(AFTER_HOURS)}  除外月: {list(AFTER_EXCL_MONTHS)}")

    tb_before = run_backtest_b_custom(
        df_with_indicators, BEFORE_WEEKDAYS, BEFORE_HOURS, BEFORE_EXCL_MONTHS
    )
    tb_after  = run_backtest_b_custom(
        df_with_indicators, AFTER_WEEKDAYS, AFTER_HOURS, AFTER_EXCL_MONTHS
    )
    print(f"\n  ビフォー件数: {len(tb_before)}  アフター件数: {len(tb_after)}")

    # ── 詳細出力 ──
    _print_s1_detail(tb_before, "【ビフォー】")
    _print_s1_detail(tb_after,  "【アフター】")

    # ── 比較サマリー ──
    sb = calc_summary(tb_before)
    sa = calc_summary(tb_after)

    print(f"\n{W}")
    print("  ビフォー vs アフター 比較サマリー")
    print(W)
    print(f"\n  {'':>22}  {'ビフォー':>10}  {'アフター':>10}  {'差(後-前)':>10}")
    print("  " + "-" * 60)
    print(f"  {'件数':>22}  {sb['n']:>10}  {sa['n']:>10}  {sa['n']-sb['n']:>+10}")
    print(f"  {'勝率(%)':>22}  {sb['win_rate']:>9.1f}%  {sa['win_rate']:>9.1f}%  "
          f"{sa['win_rate']-sb['win_rate']:>+9.1f}%")
    print(f"  {'損益(pt)':>22}  {sb['pnl_pt']:>+10.1f}  {sa['pnl_pt']:>+10.1f}  "
          f"{sa['pnl_pt']-sb['pnl_pt']:>+10.1f}")
    print(f"  {'損益(円)':>22}  {sb['pnl_yen']:>+10,.0f}  {sa['pnl_yen']:>+10,.0f}  "
          f"{sa['pnl_yen']-sb['pnl_yen']:>+10,.0f}")
    print(f"  {'期待値(pt)':>22}  {sb['ev_pt']:>+10.2f}  {sa['ev_pt']:>+10.2f}  "
          f"{sa['ev_pt']-sb['ev_pt']:>+10.2f}")
    print(f"  {'PF':>22}  {pf_str(sb['pf']):>10}  {pf_str(sa['pf']):>10}  "
          f"{sa['pf']-sb['pf']:>+10.3f}")
    print()


if __name__ == "__main__":
    df_raw = load_data()
    df_ind = add_indicators(df_raw)
    main_comparison(df_ind)
