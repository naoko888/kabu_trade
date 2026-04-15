"""
backtest_system12_combined.py
===========================================
系統①（long）＋ 系統③（short）合算バックテスト
auto_trade.py の DST / CPI / 時間条件を完全反映済み

■ 出力構成
  [A] シナリオ1: 系統① (3月・7月除外) ＋ 系統③ (DST/CPI込み)
  [B] シナリオ2: 系統① (3・7・11月除外) ＋ 系統③ (同上)
  [C] backtest_perfect_order.py 比較
      系統③ 現行2線(ma9<ma20) vs 完全PO3線(ma9<ma20<ma55)
  [D] 月次損失上限分析 (-20/-30/-40/-50 万円)
"""

from pathlib import Path
import pandas as pd
import numpy as np

# =========================
# 設定
# =========================
DATA_DIR   = Path(r"C:\kabu_trade\data")
EXCEL_FILES = [
    "N225microf_2023.xlsx",
    "N225microf_2024.xlsx",
    "N225microf_2025.xlsx",
    "N225microf_2026.xlsx",
]
MICRO_CSV   = Path(r"C:\kabu_trade\micro_5min.csv")
CPI_CSV     = Path(r"C:\kabu_trade\economic_calendar.csv")

TP             = 240
SL             = 60
MAX_HOLD       = 120
TOUCH_PCT      = 0.005
COMMISSION_PT  = 2.2
PT_TO_YEN      = 10

SESSION_BOUNDARIES = frozenset({1540, 600, 2350})

# 系統① 条件
S1_WEEKDAYS   = (0, 1, 2)        # 月・火・水
S1_HOURS      = (8, 12, 15, 18, 19, 20, 21, 23)
S1_EXCL_BASE  = (3, 7)           # 3月・7月除外

# 系統③ 条件
S3_WEEKDAYS   = (0, 2, 3, 4)     # 月・水・木・金
S3_EXCL_MONTHS = (7, 11)
S3_HOURS_DST  = (5, 8, 12, 14, 15, 19, 20, 22, 23)
S3_HOURS_WIN  = (5, 12, 15, 19, 20, 21, 22, 23)

# 米国サマータイム期間
_DST_PERIODS = [
    (pd.Timestamp("2023-03-12"), pd.Timestamp("2023-11-05")),
    (pd.Timestamp("2024-03-10"), pd.Timestamp("2024-11-03")),
    (pd.Timestamp("2025-03-09"), pd.Timestamp("2025-11-02")),
    (pd.Timestamp("2026-03-08"), pd.Timestamp("2026-11-01")),
]


# =========================
# データ読み込み
# =========================
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
        d = read_excel(p)
        print(f"  {fname}: {len(d)} 本")
        dfs.append(d)

    if MICRO_CSV.exists():
        try:
            dc = pd.read_csv(MICRO_CSV, index_col="datetime", parse_dates=True).reset_index()
            if dc["datetime"].dt.tz is not None:
                dc["datetime"] = dc["datetime"].dt.tz_convert("Asia/Tokyo").dt.tz_localize(None)
            for c in ["open", "high", "low", "close", "volume"]:
                if c in dc.columns:
                    dc[c] = pd.to_numeric(dc[c], errors="coerce")
            dc = (dc.dropna(subset=["datetime", "open", "high", "low", "close"])
                  [["datetime", "open", "high", "low", "close", "volume"]]
                  .sort_values("datetime"))
            print(f"  micro_5min.csv: {len(dc)} 本")
            dfs.append(dc)
        except Exception as e:
            print(f"  micro_5min.csv 読み込み失敗: {e}")

    if not dfs:
        raise FileNotFoundError("データファイルが見つかりません")

    df = (pd.concat(dfs, ignore_index=True)
          .sort_values("datetime")
          .drop_duplicates(subset=["datetime"])
          .reset_index(drop=True))
    print(f"合計: {len(df)} 本  ({df['datetime'].min()} ~ {df['datetime'].max()})\n")
    return df


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


# =========================
# CPI / DST マスク構築
# =========================
def load_cpi() -> pd.DataFrame:
    for enc in ("utf-8", "utf-8-sig", "cp932"):
        try:
            df = pd.read_csv(CPI_CSV, encoding=enc)
            if "indicator" in df.columns:
                df["release_datetime_jst"] = pd.to_datetime(df["release_datetime_jst"])
                cpi = df[df["indicator"] == "米CPI"].reset_index(drop=True)
                print(f"[OK] CPI読み込み: {len(cpi)}件")
                return cpi
        except Exception:
            continue
    print("[WARN] economic_calendar.csv 読み込み失敗 -> CPI除外無効")
    return pd.DataFrame(columns=["indicator", "release_datetime_jst"])


def build_masks(dts_ns: np.ndarray, cpi_df: pd.DataFrame) -> tuple:
    """
    Returns (dst_mask, cpi_mask) as bool arrays of same length as dts_ns.
    dts_ns: numpy int64 array of nanosecond timestamps
    """
    n = len(dts_ns)
    dst_mask = np.zeros(n, dtype=bool)
    cpi_mask = np.zeros(n, dtype=bool)

    # DST mask
    for start, end in _DST_PERIODS:
        s_ns = start.value
        e_ns = end.value
        dst_mask |= (dts_ns >= s_ns) & (dts_ns <= e_ns)

    # CPI mask (30min before ~ 60min after)
    if len(cpi_df) > 0:
        before_ns = int(pd.Timedelta(minutes=30).total_seconds() * 1e9)
        after_ns  = int(pd.Timedelta(minutes=60).total_seconds() * 1e9)
        for release in cpi_df["release_datetime_jst"]:
            r_ns = pd.Timestamp(release).value
            cpi_mask |= (dts_ns >= r_ns - before_ns) & (dts_ns <= r_ns + after_ns)

    return dst_mask, cpi_mask


# =========================
# バックテスト
# =========================
def run_backtest(df: pd.DataFrame,
                 cpi_df: pd.DataFrame,
                 s1_excl_months=(3, 7),
                 s3_po: bool = False) -> pd.DataFrame:
    """
    s1_excl_months: system ① excluded months
    s3_po: if True, use 3-line PO (ma9<ma20<ma55) for system ③
    """
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
    dts_ns      = dts.values.astype("int64")
    dt_list     = dts.to_list()

    n = len(df)

    # Pre-compute DST / CPI masks
    dst_mask, cpi_mask = build_masks(dts_ns, cpi_df)

    s1_excl_set = set(s1_excl_months)
    s3_excl_set = set(S3_EXCL_MONTHS)

    trades = []

    for i in range(2, n - 1):
        ma9  = arr_ma9[i];  ma10 = arr_ma10[i]
        ma20 = arr_ma20[i]; ma55 = arr_ma55[i]
        macd = arr_macd[i]; msig = arr_msig[i]

        if any(np.isnan(v) for v in [ma9, ma10, ma20, ma55, macd, msig]):
            continue

        hr = (arr_hour[i] * 60 + arr_minute[i] + 5) // 60 % 24
        wd = arr_weekday[i]
        mo = arr_month[i]
        lo = arr_low[i]
        hi = arr_high[i]

        # ─── 系統① ───
        if wd in S1_WEEKDAYS and hr in S1_HOURS and mo not in s1_excl_set:
            ma9p  = arr_ma9[i-1];  ma10p  = arr_ma10[i-1]
            ma9p2 = arr_ma9[i-2];  ma10p2 = arr_ma10[i-2]
            c1    = arr_close[i-1]; c2    = arr_close[i-2]

            if any(np.isnan(v) for v in [ma9p, ma10p, ma9p2, ma10p2]):
                pass
            else:
                above = (c2 > ma9p2 and c2 > ma10p2 and c1 > ma9p and c1 > ma10p)
                touch = (abs(lo - ma9)  / ma9  <= TOUCH_PCT or
                         abs(lo - ma10) / ma10 <= TOUCH_PCT)
                gc    = (macd > msig)

                if above and touch and gc:
                    ep = arr_open[i + 1]
                    pnl, rtype = _exec(arr_high, arr_low, arr_close, arr_hm,
                                       ep, i + 1, "long", n)
                    pnl -= COMMISSION_PT
                    trades.append({
                        "system":        "①",
                        "signal_dt":     dt_list[i],
                        "signal_year":   dt_list[i].year,
                        "signal_month":  mo,
                        "signal_hour":   hr,
                        "signal_weekday": wd,
                        "pnl_pt":        pnl,
                        "pnl_yen":       round(pnl * PT_TO_YEN, 0),
                        "result":        rtype,
                    })

        # ─── 系統③ ───
        if wd in S3_WEEKDAYS and mo not in s3_excl_set and not cpi_mask[i]:
            s3_hours = S3_HOURS_DST if dst_mask[i] else S3_HOURS_WIN
            if hr in s3_hours:
                if s3_po:
                    below = (ma9 < ma20 < ma55)
                else:
                    below = (ma9 < ma20)
                touch_hi = (abs(hi - ma9) / ma9 <= TOUCH_PCT)
                dc       = (macd < msig)

                if below and touch_hi and dc:
                    ep = arr_open[i + 1]
                    pnl, rtype = _exec(arr_high, arr_low, arr_close, arr_hm,
                                       ep, i + 1, "short", n)
                    pnl -= COMMISSION_PT
                    trades.append({
                        "system":        "③",
                        "signal_dt":     dt_list[i],
                        "signal_year":   dt_list[i].year,
                        "signal_month":  mo,
                        "signal_hour":   hr,
                        "signal_weekday": wd,
                        "pnl_pt":        pnl,
                        "pnl_yen":       round(pnl * PT_TO_YEN, 0),
                        "result":        rtype,
                    })

    if not trades:
        return pd.DataFrame()
    return pd.DataFrame(trades).reset_index(drop=True)


def _exec(arr_high, arr_low, arr_close, arr_hm,
          ep: float, ei: int, side: str, n: int):
    """Trade execution kernel (inlined for performance)"""
    pnl   = None
    rtype = None
    exit_bar = ei

    for j in range(ei, min(ei + MAX_HOLD, n)):
        bhi = arr_high[j]
        blo = arr_low[j]
        if side == "long":
            if bhi >= ep + TP:
                pnl, rtype, exit_bar = float(TP),  "TP",  j; break
            if blo <= ep - SL:
                pnl, rtype, exit_bar = float(-SL), "SL",  j; break
        else:
            if blo <= ep - TP:
                pnl, rtype, exit_bar = float(TP),  "TP",  j; break
            if bhi >= ep + SL:
                pnl, rtype, exit_bar = float(-SL), "SL",  j; break
        if arr_hm[j] in SESSION_BOUNDARIES:
            cl = arr_close[j]
            pnl = float(cl - ep) if side == "long" else float(ep - cl)
            rtype, exit_bar = "SESSION", j; break

    if pnl is None:
        cidx = min(ei + MAX_HOLD - 1, n - 1)
        cl   = arr_close[cidx]
        pnl  = float(cl - ep) if side == "long" else float(ep - cl)
        rtype = "TIME"

    return pnl, rtype


# =========================
# 集計
# =========================
def calc_summary(df: pd.DataFrame, col="pnl_pt") -> dict:
    if len(df) == 0:
        return {"n": 0, "win_rate": 0.0, "pnl_pt": 0.0, "pnl_yen": 0.0, "ev_pt": 0.0, "pf": 0.0}
    pnl  = df[col].values if col == "pnl_pt" else df["pnl_pt"].values
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


def pf_str(v):
    return f"{v:.3f}" if v != float("inf") else "  inf"

SEP  = "=" * 80
SEP2 = "-" * 80


# =========================
# 出力
# =========================
def print_scenario_header(label: str):
    print(f"\n{SEP}")
    print(f"  {label}")
    print(SEP)


def print_overall(trades: pd.DataFrame, label: str = ""):
    s1 = calc_summary(trades[trades["system"] == "①"])
    s3 = calc_summary(trades[trades["system"] == "③"])
    sa = calc_summary(trades)

    hdr = f"  {'':10}  {'件数':>5}  {'勝率%':>6}  {'損益(pt)':>9}  {'損益(円)':>11}  {'期待値':>7}  {'PF':>6}"
    print(hdr)
    print("  " + "-" * 65)

    def row(label, s):
        return (f"  {label:10}  {s['n']:>5}  {s['win_rate']:>5.1f}%  "
                f"{s['pnl_pt']:>+9.1f}  {s['pnl_yen']:>+11,.0f}  "
                f"{s['ev_pt']:>+7.2f}  {pf_str(s['pf'])}")

    print(row("系統①", s1))
    print(row("系統③", s3))
    print(row("合算  ", sa))


def print_ym_pnl(trades: pd.DataFrame, title="年×月 × 系統別 損益(円)"):
    print(f"\n  [{title}]")
    months = list(range(1, 13))
    hdr = "  年  " + "".join(f"  {m:>4}月" for m in months) + "   合計"
    print(hdr)
    print("  " + "-" * len(hdr))

    years = sorted(trades["signal_year"].unique())
    for yr in years:
        yr_df = trades[trades["signal_year"] == yr]
        vals = []
        total = 0
        for mo in months:
            v = int(yr_df[yr_df["signal_month"] == mo]["pnl_yen"].sum())
            vals.append(f"{v:>+6,}")
            total += v
        print(f"  {yr}  " + "  ".join(vals) + f"  {total:>+8,}")

    # 月別合計行
    vals = []
    total = 0
    for mo in months:
        v = int(trades[trades["signal_month"] == mo]["pnl_yen"].sum())
        vals.append(f"{v:>+6,}")
        total += v
    print("  " + "-" * (len(hdr) - 2))
    print(f"  計    " + "  ".join(vals) + f"  {total:>+8,}")


def print_ym_count(trades: pd.DataFrame, title="年×月 × 系統別 件数"):
    print(f"\n  [{title}]")

    pivot = trades.pivot_table(
        values="pnl_pt",
        index=["signal_year", "signal_month"],
        columns="system",
        aggfunc="count",
        fill_value=0,
    )

    years = sorted(trades["signal_year"].unique())
    months = list(range(1, 13))

    all_sys = sorted(trades["system"].unique())
    hdr = "  年  月   " + "   ".join(f"{s}件数" for s in all_sys) + "    合計"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    for yr in years:
        yr_df = trades[trades["signal_year"] == yr]
        for mo in sorted(yr_df["signal_month"].unique()):
            mo_df = yr_df[yr_df["signal_month"] == mo]
            parts = []
            total = 0
            for s in all_sys:
                cnt = int((mo_df["system"] == s).sum())
                parts.append(f"{cnt:>5}")
                total += cnt
            print(f"  {yr} {mo:02d}月  " + "   ".join(parts) + f"   {total:>5}")


# =========================
# 月次損失上限シミュレーション
# =========================
def sim_monthly_dd(trades: pd.DataFrame, dd_limit_yen: int) -> dict:
    """
    trades を signal_dt 順に処理し、月次累計損益が dd_limit_yen を下回ったら
    当月残りのトレードをスキップ。
    Returns dict: active_trades DataFrame + stats
    """
    if len(trades) == 0:
        return {"active": pd.DataFrame(), "skipped": 0, "months_triggered": 0}

    df = trades.sort_values("signal_dt").copy()
    df["ym"] = list(zip(df["signal_year"], df["signal_month"]))

    keep = []
    month_pnl = {}
    triggered = set()

    for _, row in df.iterrows():
        ym = row["ym"]
        if ym not in month_pnl:
            month_pnl[ym] = 0.0

        if ym in triggered:
            keep.append(False)
            continue

        keep.append(True)
        month_pnl[ym] += row["pnl_yen"]
        if month_pnl[ym] <= dd_limit_yen:
            triggered.add(ym)

    df["keep"] = keep
    active  = df[df["keep"]].drop(columns=["ym", "keep"])
    skipped = (~df["keep"]).sum()

    return {
        "active":           active,
        "skipped":          int(skipped),
        "months_triggered": len(triggered),
    }


def print_dd_comparison(trades: pd.DataFrame, label: str):
    """月次損失上限を変えたときの PF / 損益 比較"""
    print_scenario_header(f"[D] 月次損失上限分析 - {label}")

    limits = [None, -20_000, -30_000, -40_000, -50_000]
    print(f"\n  {'制限(円)':>12}  {'件数':>5}  {'スキップ':>7}  {'発動月':>5}  "
          f"{'勝率%':>6}  {'損益(円)':>11}  {'PF':>6}")
    print("  " + "-" * 70)

    for lim in limits:
        if lim is None:
            active = trades
            skipped = 0
            months_triggered = 0
            lim_label = "   制限なし"
        else:
            res = sim_monthly_dd(trades, lim)
            active   = res["active"]
            skipped  = res["skipped"]
            months_triggered = res["months_triggered"]
            lim_label = f"{lim:>+12,}"

        s = calc_summary(active)
        print(f"  {lim_label}  {s['n']:>5}  {skipped:>7}  {months_triggered:>5}  "
              f"{s['win_rate']:>5.1f}%  {s['pnl_yen']:>+11,.0f}  {pf_str(s['pf'])}")

    # 年×月クロス集計（制限なし vs -30,000円）
    for lim, lim_lbl in [(-30_000, "月次制限 -30,000円"), (-50_000, "月次制限 -50,000円")]:
        res = sim_monthly_dd(trades, lim)
        active = res["active"]
        print()
        print_ym_pnl(active, f"損益(円) 年×月クロス [{lim_lbl}]")


# =========================
# backtest_perfect_order 比較
# =========================
def print_po_comparison(df: pd.DataFrame, cpi_df: pd.DataFrame):
    print_scenario_header("[C] backtest_perfect_order.py 比較: 系統③ 2線 vs 3線PO")
    print("  注: backtest_perfect_order.py はシグナル時刻基準(bar START)で集計。")
    print("  当スクリプトは bar END (+5min) 基準。時間帯フィルターに若干の差が生じる。\n")

    # 2線版（現行 auto_trade.py 条件）
    t2 = run_backtest(df, cpi_df, s1_excl_months=S1_EXCL_BASE, s3_po=False)
    s3_2 = calc_summary(t2[t2["system"] == "③"])

    # 3線PO版
    t3 = run_backtest(df, cpi_df, s1_excl_months=S1_EXCL_BASE, s3_po=True)
    s3_3 = calc_summary(t3[t3["system"] == "③"])

    print(f"  {'':20}  {'件数':>5}  {'勝率%':>6}  {'損益(pt)':>9}  {'損益(円)':>11}  {'PF':>6}")
    print("  " + "-" * 65)

    def row(lbl, s):
        return (f"  {lbl:20}  {s['n']:>5}  {s['win_rate']:>5.1f}%  "
                f"{s['pnl_pt']:>+9.1f}  {s['pnl_yen']:>+11,.0f}  {pf_str(s['pf'])}")

    print(row("③ 現行2線(ma9<ma20)", s3_2))
    print(row("③ 3線PO(ma9<ma20<ma55)", s3_3))
    dpf  = s3_3["pf"] - s3_2["pf"]
    dyen = s3_3["pnl_yen"] - s3_2["pnl_yen"]
    dn   = s3_3["n"] - s3_2["n"]
    print(f"  {'差 (3線-2線)':20}  {dn:>+5}  {'':>6}  {'':>9}  {dyen:>+11,.0f}  {dpf:>+6.3f}")

    print()
    print_ym_pnl(t2[t2["system"] == "③"].assign(pnl_yen=t2[t2["system"]=="③"]["pnl_yen"]),
                 "③ 2線 損益(円) 年×月")
    print()
    print_ym_pnl(t3[t3["system"] == "③"].assign(pnl_yen=t3[t3["system"]=="③"]["pnl_yen"]),
                 "③ 3線PO 損益(円) 年×月")


# =========================
# メイン
# =========================
def main():
    df  = load_data()
    df  = add_indicators(df)
    cpi = load_cpi()

    print("\nバックテスト実行中...")
    trades = run_backtest(df, cpi, s1_excl_months=S1_EXCL_BASE)
    t1 = trades[trades["system"] == "①"]
    t3 = trades[trades["system"] == "③"]
    print(f"  系統①: {len(t1)}件  系統③: {len(t3)}件  合計: {len(trades)}件")

    # ─ 条件サマリー ─
    print_scenario_header("系統①③合算バックテスト（auto_trade.py 現行条件）")
    print("  系統①: 月・火・水 / 8,12,15,18,19,20,21,23時 / 3月・7月除外")
    print("  系統③: 月水木金 / DST:[5,8,12,14,15,19,20,22,23] 冬:[5,12,15,19,20,21,22,23]")
    print("         / 7月・11月除外 / CPI発表前30min~後60min除外")
    print("  手数料: 2.2pt込み\n")

    # 1. 全体成績
    print_scenario_header("1. 全体成績")
    print_overall(trades)

    # 2. 年別成績（系統別）
    print_scenario_header("2. 年別成績")
    hdr = (f"  {'':4}  {'':6}  {'件数':>5}  {'勝率%':>6}  {'損益(pt)':>9}  "
           f"{'損益(円)':>11}  {'期待値':>7}  {'PF':>6}")
    print(hdr)
    print("  " + "-" * 72)
    for yr in sorted(trades["signal_year"].unique()):
        for sys in ("①", "③", "合"):
            if sys == "合":
                grp = trades[trades["signal_year"] == yr]
            else:
                grp = trades[(trades["signal_year"] == yr) & (trades["system"] == sys)]
            if len(grp) == 0:
                continue
            s = calc_summary(grp)
            tag = f"系統{sys}" if sys != "合" else "合算  "
            print(f"  {yr}  {tag}  {s['n']:>5}  {s['win_rate']:>5.1f}%  "
                  f"{s['pnl_pt']:>+9.1f}  {s['pnl_yen']:>+11,.0f}  "
                  f"{s['ev_pt']:>+7.2f}  {pf_str(s['pf'])}")
        print("  " + "-" * 72)

    # 3. 年×月クロス集計（系統別・合算）
    print_scenario_header("3. 年x月クロス集計 - 損益(円)")
    months = list(range(1, 13))
    hdr_cross = "  年  系統  " + "".join(f"  {m:>4}月" for m in months) + "    合計"
    print(hdr_cross)
    print("  " + "-" * (len(hdr_cross) - 2))
    for yr in sorted(trades["signal_year"].unique()):
        for sys, label in (("①", "①    "), ("③", "③    "), (None, "合算  ")):
            grp = trades[trades["signal_year"] == yr] if sys is None else \
                  trades[(trades["signal_year"] == yr) & (trades["system"] == sys)]
            if len(grp) == 0 and sys is not None:
                continue
            vals = []
            total = 0
            for m in months:
                v = int(grp[grp["signal_month"] == m]["pnl_yen"].sum())
                vals.append(f"{v:>+6,}")
                total += v
            print(f"  {yr}  {label}  " + "  ".join(vals) + f"  {total:>+8,}")
        print("  " + "-" * (len(hdr_cross) - 2))
    # 全期間月合計
    for sys, label in (("①", "①全期間"), ("③", "③全期間"), (None, "合算全期")):
        grp = trades if sys is None else trades[trades["system"] == sys]
        vals = []
        total = 0
        for m in months:
            v = int(grp[grp["signal_month"] == m]["pnl_yen"].sum())
            vals.append(f"{v:>+6,}")
            total += v
        print(f"  計   {label}  " + "  ".join(vals) + f"  {total:>+8,}")

    # 4. 月次DD制限分析
    print_scenario_header("4. 月次損失上限分析（系統①③合算）")
    limits = [None, -20_000, -30_000, -40_000, -50_000]
    print(f"\n  {'制限(円)':>12}  {'件数':>5}  {'スキップ':>7}  {'発動月':>5}  "
          f"{'勝率%':>6}  {'損益(円)':>11}  {'PF':>6}")
    print("  " + "-" * 68)
    for lim in limits:
        if lim is None:
            active = trades
            skipped = 0
            months_triggered = 0
            lim_label = "   制限なし"
        else:
            res = sim_monthly_dd(trades, lim)
            active   = res["active"]
            skipped  = res["skipped"]
            months_triggered = res["months_triggered"]
            lim_label = f"{lim:>+12,}"
        s = calc_summary(active)
        print(f"  {lim_label}  {s['n']:>5}  {skipped:>7}  {months_triggered:>5}  "
              f"{s['win_rate']:>5.1f}%  {s['pnl_yen']:>+11,.0f}  {pf_str(s['pf'])}")


if __name__ == "__main__":
    main()
