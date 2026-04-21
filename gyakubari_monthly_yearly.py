from pathlib import Path
import pandas as pd
import numpy as np

# ========================================
# 逆張り上位4条件 月別×年別クロス集計
# ========================================

DATA_DIR = Path(r"C:\kabu_trade\data")
EXCEL_FILES = [
    "N225microf_2023.xlsx",
    "N225microf_2024.xlsx",
    "N225microf_2025.xlsx",
    "N225microf_2026.xlsx",
]
MICRO_CSV = Path(r"C:\kabu_trade\micro_5min.csv")
OUT_DIR = Path(r"C:\kabu_trade\data\gyakubari_search")

COMMISSION_PT = 2.2
PT_TO_YEN    = 10
TP       = 120
SL       = 60
MAX_HOLD = 6

# 分析する4条件
TARGETS = [
    {"label": "LONG_A",  "side": "long",  "move_pct": 0.002, "rsi_th": 40, "vol_th": 0.8, "lookback": 1, "recovery_pct": 0.002, "exclude_hours": {19}},
    {"label": "LONG_B",  "side": "long",  "move_pct": 0.003, "rsi_th": 35, "vol_th": 0.8, "lookback": 1, "recovery_pct": 0.002},
    {"label": "SHORT_A", "side": "short", "move_pct": 0.004, "rsi_th": 70, "vol_th": 0.8, "lookback": 1, "recovery_pct": 0.002},
    {"label": "SHORT_B", "side": "short", "move_pct": 0.004, "rsi_th": 50, "vol_th": 0.8, "lookback": 1, "recovery_pct": 0.002},
]


# ========================================
# データ読み込み
# ========================================
def read_excel(path: Path) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name="5min", engine="openpyxl")
    df = df.rename(columns={
        "日付": "date", "時間": "time",
        "始値": "open", "高値": "high",
        "安値": "low", "終値": "close", "出来高": "volume",
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
    for fname in EXCEL_FILES:
        p = DATA_DIR / fname
        if not p.exists():
            continue
        dfs.append(read_excel(p))
    if MICRO_CSV.exists():
        try:
            dc = pd.read_csv(MICRO_CSV, index_col="datetime", parse_dates=True).reset_index()
            if dc["datetime"].dt.tz is not None:
                dc["datetime"] = dc["datetime"].dt.tz_convert("Asia/Tokyo").dt.tz_localize(None)
            for c in ["open", "high", "low", "close", "volume"]:
                if c in dc.columns:
                    dc[c] = pd.to_numeric(dc[c], errors="coerce")
            dc = (
                dc.dropna(subset=["datetime", "open", "high", "low", "close"])
                [["datetime", "open", "high", "low", "close", "volume"]]
                .sort_values("datetime")
            )
            dfs.append(dc)
        except Exception:
            pass
    df = (
        pd.concat(dfs, ignore_index=True)
        .sort_values("datetime")
        .drop_duplicates(subset=["datetime"])
        .reset_index(drop=True)
    )
    return df


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["vol_ma20"]  = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma20"]
    delta    = df["close"].diff()
    up       = delta.clip(lower=0)
    down     = -delta.clip(upper=0)
    avg_up   = up.rolling(14).mean()
    avg_down = down.rolling(14).mean()
    rs       = avg_up / avg_down.replace(0, np.nan)
    df["rsi14"] = 100 - (100 / (1 + rs))
    prev_close = df["close"].shift(1)
    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - prev_close).abs()
    tr3 = (df["low"]  - prev_close).abs()
    df["atr14"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1).rolling(14).mean()
    df["month"] = df["datetime"].dt.month
    df["year"]  = df["datetime"].dt.year
    return df


# ========================================
# シグナル判定
# ========================================
def check_signal(df: pd.DataFrame, i: int, p: dict) -> bool:
    if i < p["lookback"] or i < 20:
        return False
    cur  = df.iloc[i]
    prev = df.iloc[i - p["lookback"]]
    if pd.isna(cur["rsi14"]) or pd.isna(cur["vol_ratio"]) or pd.isna(cur["atr14"]):
        return False

    if p["side"] == "long":
        move = (cur["close"] - prev["close"]) / prev["close"]
        fade = (cur["close"] - cur["low"]) / cur["close"]
        return all([
            move <= -p["move_pct"],
            cur["rsi14"] <= p["rsi_th"],
            cur["vol_ratio"] >= p["vol_th"],
            fade >= p["recovery_pct"],
        ])
    else:
        move = (cur["close"] - prev["close"]) / prev["close"]
        fade = (cur["high"] - cur["close"]) / cur["close"]
        return all([
            move >= p["move_pct"],
            cur["rsi14"] >= p["rsi_th"],
            cur["vol_ratio"] >= p["vol_th"],
            fade >= p["recovery_pct"],
        ])


# ========================================
# バックテスト（月・年ラベル付き）
# ========================================
def run_backtest(df: pd.DataFrame, p: dict) -> pd.DataFrame:
    records = []
    exclude_hours = p.get("exclude_hours", set())
    for i in range(20, len(df) - 1):
        if not check_signal(df, i, p):
            continue
        if int(df.iloc[i]["datetime"].hour) in exclude_hours:
            continue
        entry_idx = i + 1
        if entry_idx >= len(df):
            continue
        ep    = float(df.iloc[entry_idx]["open"])
        month = int(df.iloc[i]["month"])
        year  = int(df.iloc[i]["year"])
        pnl   = None
        for j in range(entry_idx, min(entry_idx + MAX_HOLD, len(df))):
            hi = float(df.iloc[j]["high"])
            lo = float(df.iloc[j]["low"])
            if p["side"] == "long":
                if hi >= ep + TP: pnl = TP - COMMISSION_PT; break
                if lo <= ep - SL: pnl = -SL - COMMISSION_PT; break
            else:
                if lo <= ep - TP: pnl = TP - COMMISSION_PT; break
                if hi >= ep + SL: pnl = -SL - COMMISSION_PT; break
        if pnl is None:
            exit_idx = min(entry_idx + MAX_HOLD - 1, len(df) - 1)
            cl  = float(df.iloc[exit_idx]["close"])
            raw = (cl - ep) if p["side"] == "long" else (ep - cl)
            pnl = raw - COMMISSION_PT
        records.append({"datetime": df.iloc[i]["datetime"], "year": year, "month": month, "hour": int(df.iloc[i]["datetime"].hour), "pnl": pnl})
    return pd.DataFrame(records)


# ========================================
# 集計関数
# ========================================
def calc_stats(arr):
    if len(arr) == 0:
        return {"n": 0, "pnl": 0, "pf": "-", "win%": "-"}
    wins   = arr[arr > 0].sum()
    losses = abs(arr[arr < 0].sum())
    return {
        "n":    len(arr),
        "pnl":  round(arr.sum(), 0),
        "pf":   round(wins / losses, 2) if losses > 0 else "inf",
        "win%": round((arr > 0).mean() * 100, 1),
    }


def print_cross_table(trades: pd.DataFrame, label: str):
    print(f"\n{'='*70}")
    print(f"【{label}】")
    print(f"{'='*70}")

    years  = sorted(trades["year"].unique())
    months = list(range(1, 13))

    # ---- 月×年 PnL クロス表 ----
    print("\n▼ PnL クロス表（行=月, 列=年）")
    header = f"{'月':>4}" + "".join(f"{y:>8}" for y in years) + f"{'合計':>8} {'件数':>5} {'PF':>6}"
    print(header)
    print("-" * len(header))

    month_totals = []
    for m in months:
        row_vals = []
        row_pnl  = 0
        row_n    = 0
        for y in years:
            arr = trades[(trades["month"] == m) & (trades["year"] == y)]["pnl"].values
            pnl = round(arr.sum(), 0) if len(arr) > 0 else 0
            row_vals.append(pnl)
            row_pnl += pnl
            row_n   += len(arr)
        arr_m  = trades[trades["month"] == m]["pnl"].values
        st     = calc_stats(arr_m)
        month_totals.append({"month": m, "pnl": row_pnl, "n": row_n, "st": st})
        cells = "".join(f"{v:>8.0f}" for v in row_vals)
        print(f"{m:>3}月{cells}{row_pnl:>8.0f} {row_n:>5} {st['pf']:>6}")

    # 年合計行
    print("-" * len(header))
    year_pnls = []
    for y in years:
        arr = trades[trades["year"] == y]["pnl"].values
        year_pnls.append(round(arr.sum(), 0))
    total_arr = trades["pnl"].values
    st_total  = calc_stats(total_arr)
    cells = "".join(f"{v:>8.0f}" for v in year_pnls)
    print(f"{'合計':>4}{cells}{round(total_arr.sum(),0):>8.0f} {len(total_arr):>5} {st_total['pf']:>6}")

    # ---- 年別サマリー ----
    print("\n▼ 年別サマリー")
    print(f"{'年':>6} {'件数':>5} {'PnL':>8} {'PF':>6} {'勝率%':>7}")
    print("-" * 38)
    for y in years:
        arr = trades[trades["year"] == y]["pnl"].values
        st  = calc_stats(arr)
        print(f"{y:>6} {st['n']:>5} {st['pnl']:>8} {st['pf']:>6} {st['win%']:>7}")
    print("-" * 38)
    print(f"{'全体':>6} {st_total['n']:>5} {round(total_arr.sum(),0):>8} {st_total['pf']:>6} {st_total['win%']:>7}")

    # ---- 不良月リスト ----
    print("\n▼ 月別サマリー（PF昇順）")
    print(f"{'月':>4} {'件数':>5} {'PnL':>8} {'PF':>6} {'勝率%':>7}")
    print("-" * 38)
    month_rows = []
    for m in months:
        arr = trades[trades["month"] == m]["pnl"].values
        st  = calc_stats(arr)
        month_rows.append((m, st))
    # PF昇順（数値のみ、infは末尾）
    def pf_sort(x):
        pf = x[1]["pf"]
        return pf if isinstance(pf, (int, float)) else 999
    for m, st in sorted(month_rows, key=pf_sort):
        print(f"{m:>3}月 {st['n']:>5} {st['pnl']:>8} {st['pf']:>6} {st['win%']:>7}")


def print_hour_cross_table(trades: pd.DataFrame, label: str):
    print(f"\n{'='*70}")
    print(f"【{label} 時間帯別×年別クロス集計】")
    print(f"{'='*70}")

    years = sorted(trades["year"].unique())
    hours = sorted(trades["hour"].unique())

    # ---- 時間帯×年 PnL クロス表 ----
    print("\n▼ PnL クロス表（行=時間帯, 列=年）")
    header = f"{'時':>4}" + "".join(f"{y:>8}" for y in years) + f"{'合計':>8} {'件数':>5} {'PF':>6} {'勝率%':>7}"
    print(header)
    print("-" * len(header))

    for h in hours:
        row_vals = []
        for y in years:
            arr = trades[(trades["hour"] == h) & (trades["year"] == y)]["pnl"].values
            row_vals.append(round(arr.sum(), 0) if len(arr) > 0 else 0)

        arr_h  = trades[trades["hour"] == h]["pnl"].values
        st     = calc_stats(arr_h)
        cells  = "".join(f"{v:>8.0f}" for v in row_vals)
        pf_str = str(st["pf"]) if st["n"] > 0 else "-"
        wr_str = str(st["win%"]) if st["n"] > 0 else "-"
        print(f"{h:>3}時{cells}{round(arr_h.sum(),0):>8.0f} {st['n']:>5} {pf_str:>6} {wr_str:>7}")

    # 合計行
    print("-" * len(header))
    year_pnls = []
    for y in years:
        arr = trades[trades["year"] == y]["pnl"].values
        year_pnls.append(round(arr.sum(), 0))
    total_arr = trades["pnl"].values
    st_total  = calc_stats(total_arr)
    cells = "".join(f"{v:>8.0f}" for v in year_pnls)
    print(f"{'合計':>4}{cells}{round(total_arr.sum(),0):>8.0f} {len(total_arr):>5} {st_total['pf']:>6} {st_total['win%']:>7}")

    # ---- 時間帯別サマリー（PF昇順）----
    print("\n▼ 時間帯別サマリー（PF昇順）")
    print(f"{'時':>4} {'件数':>5} {'PnL':>8} {'PF':>6} {'勝率%':>7}")
    print("-" * 38)
    hour_rows = []
    for h in hours:
        arr = trades[trades["hour"] == h]["pnl"].values
        st  = calc_stats(arr)
        hour_rows.append((h, st))

    def pf_sort(x):
        pf = x[1]["pf"]
        return pf if isinstance(pf, (int, float)) else 999

    for h, st in sorted(hour_rows, key=pf_sort):
        pnl_str = str(st["pnl"]) if st["n"] > 0 else "-"
        pf_str  = str(st["pf"])  if st["n"] > 0 else "-"
        wr_str  = str(st["win%"]) if st["n"] > 0 else "-"
        print(f"{h:>3}時 {st['n']:>5} {pnl_str:>8} {pf_str:>6} {wr_str:>7}")


def analyze_dd(trades: pd.DataFrame, label: str):
    print(f"\n{'='*60}")
    print(f"【{label} DD分析】")
    print(f"{'='*60}")

    pnl_arr = trades["pnl"].values
    n = len(pnl_arr)
    if n == 0:
        print("トレードなし")
        return

    cumsum = np.cumsum(pnl_arr)
    peak   = np.maximum.accumulate(cumsum)
    dd     = cumsum - peak

    # 最大DD発生時点
    max_dd_idx = dd.argmin()
    max_dd     = dd[max_dd_idx]
    max_dd_dt  = trades.iloc[max_dd_idx]["datetime"]

    # DDの開始点（直前ピーク）
    peak_idx = 0
    for k in range(max_dd_idx, -1, -1):
        if cumsum[k] == peak[max_dd_idx]:
            peak_idx = k
            break

    # 最大連敗（発生月も記録）
    max_consec = 0
    cur_consec = 0
    consec_start_idx = 0
    best_streak_start = 0
    for idx, p2 in enumerate(pnl_arr):
        if p2 < 0:
            if cur_consec == 0:
                consec_start_idx = idx
            cur_consec += 1
            if cur_consec > max_consec:
                max_consec = cur_consec
                best_streak_start = consec_start_idx
        else:
            cur_consec = 0

    # 連敗期間の日付
    if max_consec > 0:
        streak_start_dt = trades.iloc[best_streak_start]["datetime"]
        streak_end_dt   = trades.iloc[best_streak_start + max_consec - 1]["datetime"]
    else:
        streak_start_dt = streak_end_dt = None

    # 最大連続損失額
    max_consec_loss_pt = 0
    cur_loss_pt        = 0
    cl_start_idx       = 0
    best_cl_start      = 0
    best_cl_end        = 0
    for idx, p2 in enumerate(pnl_arr):
        if p2 < 0:
            if cur_loss_pt == 0:
                cl_start_idx = idx
            cur_loss_pt += abs(p2)
            if cur_loss_pt > max_consec_loss_pt:
                max_consec_loss_pt = cur_loss_pt
                best_cl_start = cl_start_idx
                best_cl_end   = idx
        else:
            cur_loss_pt = 0

    wins_arr   = pnl_arr[pnl_arr > 0]
    losses_arr = pnl_arr[pnl_arr < 0]
    total_wins   = wins_arr.sum()
    total_losses = abs(losses_arr.sum())
    pf = round(total_wins / total_losses, 3) if total_losses > 0 else "inf"

    print(f"  総トレード数      : {n}件")
    print(f"  総損益            : {round(cumsum[-1],1)}pt  ({round(cumsum[-1]*PT_TO_YEN):,}円)")
    print(f"  PF                : {pf}")
    print(f"  勝率              : {round((pnl_arr>0).mean()*100,1)}%")
    print(f"  EV                : {round(cumsum[-1]/n,2)}pt")
    print(f"  ───────────────────────────────────────")
    print(f"  最大DD            : {round(max_dd,1)}pt  ({round(max_dd*PT_TO_YEN):,}円)")
    print(f"  最大DD発生        : {max_dd_dt.strftime('%Y年%m月')}  (トレード#{max_dd_idx+1})")
    print(f"  DD開始(ピーク)    : {trades.iloc[peak_idx]['datetime'].strftime('%Y年%m月')}  (トレード#{peak_idx+1})")
    if streak_start_dt:
        print(f"  最大連敗          : {max_consec}連敗  ({streak_start_dt.strftime('%Y年%m月')}〜{streak_end_dt.strftime('%Y年%m月')})")
    if max_consec_loss_pt > 0:
        cl_s = trades.iloc[best_cl_start]["datetime"].strftime('%Y年%m月')
        cl_e = trades.iloc[best_cl_end]["datetime"].strftime('%Y年%m月')
        print(f"  最大連続損失      : {round(max_consec_loss_pt,1)}pt  ({round(max_consec_loss_pt*PT_TO_YEN):,}円)  ({cl_s}〜{cl_e})")
    print(f"  ───────────────────────────────────────")

    # エントリー順の損益推移（月ベース）
    print(f"\n  ▼ 月別エントリー順 損益推移（累積）")
    print(f"  {'#':>4} {'年月':>8} {'pnl':>7} {'累積pt':>8} {'累積円':>10}")
    print(f"  " + "-"*45)
    for idx, row in trades.iterrows():
        cum = cumsum[trades.index.get_loc(idx)]
        marker = " ← MAX DD" if trades.index.get_loc(idx) == max_dd_idx else ""
        print(f"  {trades.index.get_loc(idx)+1:>4} {row['datetime'].strftime('%Y/%m'):>8} {row['pnl']:>7.1f} {cum:>8.1f} {round(cum*PT_TO_YEN):>9,}円{marker}")

    # 年別DD
    print(f"\n  ▼ 年別最大DD")
    print(f"  {'年':>6} {'件数':>5} {'最大DD(pt)':>11} {'最大DD(円)':>11} {'最大連敗':>8}")
    for yr in sorted(trades["year"].unique()):
        t_yr  = trades[trades["year"] == yr]["pnl"].values
        if len(t_yr) == 0:
            continue
        cs_yr = np.cumsum(t_yr)
        pk_yr = np.maximum.accumulate(cs_yr)
        dd_yr = (cs_yr - pk_yr).min()
        mc    = 0; cc = 0
        for p2 in t_yr:
            if p2 < 0: cc += 1; mc = max(mc, cc)
            else: cc = 0
        print(f"  {yr:>6} {len(t_yr):>5} {round(dd_yr,1):>11} {round(dd_yr*PT_TO_YEN):>10,}円 {mc:>6}連敗")


# ========================================
# メイン
# ========================================
def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("データ読み込み中...")
    df = load_data()
    df = add_indicators(df)
    print(f"合計: {len(df)} 本")

    for p in TARGETS:
        trades = run_backtest(df, p)
        if trades.empty:
            print(f"\n{p['label']}: トレードなし")
            continue
        print_cross_table(trades, p["label"])
        if p["label"] == "LONG_A":
            print_hour_cross_table(trades, p["label"])
        analyze_dd(trades, p["label"])
        # CSV保存
        trades.to_csv(
            OUT_DIR / f"cross_{p['label']}.csv",
            index=False, encoding="utf-8-sig"
        )

    print("\n完了")


if __name__ == "__main__":
    main()
