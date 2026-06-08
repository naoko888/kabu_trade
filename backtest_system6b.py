"""
backtest_system6b.py
=========================================
【系統⑥ 新シグナル候補スキャン】
A: 前日終値乖離逆張り
   → 現在価格が前日終値から X pt 以上乖離 → 逆張りエントリー
B: 前場→後場転換
   → 前場(9:00-11:30)が X pt 以上動いた方向を後場(12:30-15:00)でフェード
"""

import sys
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path
import pandas as pd
import numpy as np

DATA_DIR    = Path(r"C:\kabu_trade\data")
EXCEL_FILES = [
    "N225microf_2023.xlsx",
    "N225microf_2024.xlsx",
    "N225microf_2025.xlsx",
    "N225microf_2026.xlsx",
]
MICRO_CSV = Path(r"C:\kabu_trade\micro_5min.csv")
CPI_CSV   = Path(r"C:\kabu_trade\economic_calendar.csv")

COMMISSION_PT = 2.2
PT_TO_YEN     = 10
DD_LIMIT      = -30_000
SEP           = "=" * 78

# ── 確定パラメータ（combined用） ──
S6_THRESH = 250
S6_TP     = 200
S6_SL     = 80
S6_MH     = 6
S6_CD     = 18   # クールダウン 18本=90分
S6_HOURS  = frozenset({3, 4, 9, 10, 14, 16, 18, 22, 23})
S6_DD     = -15_000


# =========================
# データ読み込み
# =========================
def read_excel(path):
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


def load_data():
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
    if not dfs:
        raise FileNotFoundError("データファイルが見つかりません")
    df = pd.concat(dfs, ignore_index=True).drop_duplicates(subset=["datetime"]).copy()
    df["_tday"]     = df["datetime"].dt.date
    df["_sort_grp"] = (df["datetime"].dt.hour < 17).astype(int)
    df = df.sort_values(["_tday", "_sort_grp", "datetime"]).drop(columns=["_sort_grp"]).reset_index(drop=True)
    print(f"合計: {len(df)} 本  ({df['datetime'].min()} ~ {df['datetime'].max()})\n")
    return df


def load_cpi():
    for enc in ("utf-8", "utf-8-sig", "cp932"):
        try:
            df = pd.read_csv(CPI_CSV, encoding=enc)
            if "indicator" not in df.columns:
                continue
            df["release_datetime_jst"] = pd.to_datetime(df["release_datetime_jst"], errors="coerce")
            cpi = df[df["indicator"] == "米CPI"].dropna(
                subset=["release_datetime_jst"]).reset_index(drop=True)
            print(f"[OK] CPI読み込み: {len(cpi)}件")
            return cpi
        except Exception:
            continue
    print("[WARN] CPI読み込み失敗 -> CPI除外無効")
    return pd.DataFrame(columns=["release_datetime_jst"])


# =========================
# 指標追加
# =========================
def add_indicators(df):
    df = df.copy()

    # ── 前日終値（A用）──
    # 日中バー(hour<17)の最終終値を前日終値として使用。
    # _tdayでgroupbyすると夜間バーと翌日中バーが同グループになり当日終値を参照してしまうため、
    # 物理日付×日中バーのみで集計し前日付の終値をマップする。
    _dts         = pd.to_datetime(df["datetime"])
    _xlsx_date   = _dts.dt.date
    _daytime     = _dts.dt.hour < 17
    session_close = df[_daytime].groupby(_xlsx_date[_daytime])["close"].last()
    _sdates      = sorted(session_close.index)
    prev_session_map = {_sdates[i]: float(session_close[_sdates[i-1]]) for i in range(1, len(_sdates))}
    df["prev_day_close"] = _xlsx_date.map(prev_session_map)
    df["dev_from_prev"]  = df["close"] - df["prev_day_close"]

    # ── 前場の値動き 9:00〜11:30（B用）──
    def calc_morning_move(group):
        h  = group["datetime"].dt.hour
        mi = group["datetime"].dt.minute
        morning = group[((h == 9) | (h == 10) | ((h == 11) & (mi <= 30)))]
        if len(morning) < 2:
            return np.nan
        return float(morning["close"].iloc[-1] - morning["open"].iloc[0])

    mm = df.groupby("_tday").apply(calc_morning_move)
    df["morning_move"] = df["_tday"].map(mm)

    # ── 曜日 ──
    df["trading_weekday"] = df["datetime"].apply(
        lambda dt: (dt - pd.Timedelta(days=1)).weekday() if dt.hour < 17 else dt.weekday()
    )

    return df


def build_cpi_mask(dts_ns, cpi_df):
    mask = np.zeros(len(dts_ns), dtype=bool)
    if len(cpi_df) > 0:
        b_ns = int(pd.Timedelta(minutes=30).total_seconds() * 1e9)
        a_ns = int(pd.Timedelta(minutes=60).total_seconds() * 1e9)
        for r in cpi_df["release_datetime_jst"]:
            r_ns = pd.Timestamp(r).value
            mask |= (dts_ns >= r_ns - b_ns) & (dts_ns <= r_ns + a_ns)
    return mask


# =========================
# 決済ロジック（共通）
# =========================
def _exec(arr_high, arr_low, arr_close, arr_open, ep, entry_i, side, tp, sl, max_hold, n,
          arr_hm=None, arr_pwd=None):
    GAP_BOUNDS = frozenset({1540, 555})
    for j in range(entry_i, min(entry_i + max_hold, n)):
        if arr_hm is not None and arr_hm[j] in GAP_BOUNDS:
            cl = arr_close[j]
            pnl = float(cl - ep) if side == "long" else float(ep - cl)
            return pnl, j, "TIME"
        hi = arr_high[j]; lo = arr_low[j]; op = arr_open[j]
        if side == "long":
            tp_hit = hi >= ep + tp
            sl_hit = lo <= ep - sl
            if tp_hit and sl_hit:
                exit_p = op if op <= ep - sl else ep - sl
                return float(exit_p - ep), j, "SL"
            if tp_hit:
                exit_p = op if op >= ep + tp else ep + tp
                return float(exit_p - ep), j, "TP"
            if sl_hit:
                exit_p = op if op <= ep - sl else ep - sl
                return float(exit_p - ep), j, "SL"
        else:
            tp_hit = lo <= ep - tp
            sl_hit = hi >= ep + sl
            if tp_hit and sl_hit:
                exit_p = op if op >= ep + sl else ep + sl
                return float(ep - exit_p), j, "SL"
            if tp_hit:
                exit_p = op if op <= ep - tp else ep - tp
                return float(ep - exit_p), j, "TP"
            if sl_hit:
                exit_p = op if op >= ep + sl else ep + sl
                return float(ep - exit_p), j, "SL"
        if arr_hm is not None and arr_pwd is not None:
            if arr_pwd[j] == 0 and arr_hm[j] in {555, 600}:
                cl = arr_close[j]
                pnl = float(cl - ep) if side == "long" else float(ep - cl)
                return pnl, j, "WEEKEND"
    exit_i = min(entry_i + max_hold - 1, n - 1)
    cl = arr_close[exit_i]
    pnl = float(cl - ep) if side == "long" else float(ep - cl)
    return pnl, exit_i, "TIME"


# =========================
# 集計
# =========================
def calc_summary(df):
    if len(df) == 0:
        return {"n": 0, "win_rate": 0.0, "pnl_yen": 0, "ev": 0.0, "pf": 0.0}
    pnl  = df["pnl_pt"].values.astype(float)
    wins = pnl[pnl > 0].sum()
    loss = abs(pnl[pnl < 0].sum())
    n    = len(pnl)
    return {
        "n":        n,
        "win_rate": float((pnl > 0).mean() * 100),
        "pnl_yen":  int(df["pnl_yen"].sum()),
        "ev":       float(pnl.sum() / n),
        "pf":       float(wins / loss) if loss > 0 else float("inf"),
    }


def pf_s(v):
    return "  inf" if v == float("inf") else f"{v:.3f}"


def sim_monthly_dd(trades, dd_limit):
    if len(trades) == 0:
        return {"active": pd.DataFrame(), "skipped": 0, "months_triggered": 0}
    df = trades.sort_values("signal_dt").copy()
    df["ym"] = list(zip(df["signal_year"], df["signal_month"]))
    keep = []; mo_pnl = {}; triggered = set()
    for _, row in df.iterrows():
        ym = row["ym"]
        mo_pnl.setdefault(ym, 0.0)
        if ym in triggered:
            keep.append(False); continue
        keep.append(True)
        mo_pnl[ym] += row["pnl_yen"]
        if mo_pnl[ym] <= dd_limit:
            triggered.add(ym)
    df["keep"] = keep
    return {
        "active":           df[df["keep"]].drop(columns=["ym", "keep"]),
        "skipped":          int((~df["keep"]).sum()),
        "months_triggered": len(triggered),
    }


# =========================
# バックテスト A: 前日終値乖離逆張り
# =========================
def run_backtest_A(df, cpi_df, thresh_pt, tp, sl, max_hold,
                   hours=None, weekdays=None, excl_months=(), cd=0):
    arr_open  = df["open"].values
    arr_high  = df["high"].values
    arr_low   = df["low"].values
    arr_close = df["close"].values
    arr_dev   = df["dev_from_prev"].values

    dts     = pd.to_datetime(df["datetime"])
    dts_ns  = dts.values.astype("int64")
    arr_hr  = dts.dt.hour.values
    arr_mi  = dts.dt.minute.values
    arr_hm  = arr_hr * 100 + arr_mi
    arr_mo  = dts.dt.month.values
    arr_wd  = df["trading_weekday"].values
    arr_pwd = dts.dt.weekday.values

    cpi_mask = build_cpi_mask(dts_ns, cpi_df)
    _hours   = set(hours)    if hours    is not None else set(range(24))
    _wdays   = set(weekdays) if weekdays is not None else set(range(5))
    _excl    = set(excl_months)

    n = len(df)
    rows = []
    last_sig_i = -(cd + 1)  # クールダウン用: 最後にシグナルが出たバーIndex
    for i in range(1, n - 1):
        if np.isnan(arr_dev[i]) or np.isnan(arr_dev[i-1]):
            continue
        if arr_mo[i] in _excl or arr_wd[i] not in _wdays:
            continue
        if arr_hr[i] not in _hours or cpi_mask[i]:
            continue

        if cd > 0:
            # クールダウン方式: cd本以上経過し、かつ閾値超過中
            if i - last_sig_i < cd:
                continue
            if arr_dev[i] >= thresh_pt:
                side = "short"
            elif arr_dev[i] <= -thresh_pt:
                side = "long"
            else:
                continue
        else:
            # 初回突破方式（cd=0）
            if arr_dev[i] >= thresh_pt and arr_dev[i-1] < thresh_pt:
                side = "short"
            elif arr_dev[i] <= -thresh_pt and arr_dev[i-1] > -thresh_pt:
                side = "long"
            else:
                continue
        last_sig_i = i

        ep = float(arr_open[i + 1])
        pnl, exit_i, rtype = _exec(arr_high, arr_low, arr_close, arr_open, ep, i + 1, side,
                               tp, sl, max_hold, n, arr_hm=arr_hm, arr_pwd=arr_pwd)
        pnl -= COMMISSION_PT
        rows.append({
            "system":         "⑥A",
            "side":           side,
            "signal_dt":      dts.iloc[i],
            "signal_year":    int(dts.iloc[i].year),
            "signal_month":   int(arr_mo[i]),
            "signal_weekday": int(arr_wd[i]),
            "signal_hour":    int(arr_hr[i]),
            "pnl_pt":         round(pnl, 1),
            "pnl_yen":        int(round(pnl * PT_TO_YEN)),
            "result":         rtype,
            "entry_price":    ep,
            "entry_dt":       dts.iloc[i + 1],
            "exit_dt":        dts.iloc[exit_i],
        })

    return pd.DataFrame(rows) if rows else pd.DataFrame()


# =========================
# run_backtest: combined用ラッパー（確定パラメータ）
# =========================
def run_backtest(df, cpi_df, thresh=None, tp=None, sl=None, max_hold=None,
                 cd=None, hours=None, excl_months=()):
    if thresh   is None: thresh   = S6_THRESH
    if tp       is None: tp       = S6_TP
    if sl       is None: sl       = S6_SL
    if max_hold is None: max_hold = S6_MH
    if cd       is None: cd       = S6_CD
    if hours    is None: hours    = S6_HOURS
    t = run_backtest_A(df, cpi_df, thresh, tp, sl, max_hold,
                       hours=hours, excl_months=excl_months, cd=cd)
    if not t.empty:
        t = t.copy()
        t["system"] = "⑥"
    return t


# =========================
# バックテスト B: 前場→後場転換
# =========================
def run_backtest_B(df, cpi_df, thresh_pt, tp, sl, max_hold,
                   weekdays=None, excl_months=()):
    arr_open  = df["open"].values
    arr_high  = df["high"].values
    arr_low   = df["low"].values
    arr_close = df["close"].values
    arr_mm    = df["morning_move"].values
    tday_arr  = df["_tday"].values

    dts     = pd.to_datetime(df["datetime"])
    dts_ns  = dts.values.astype("int64")
    arr_hr  = dts.dt.hour.values
    arr_mi  = dts.dt.minute.values
    arr_hm  = arr_hr * 100 + arr_mi
    arr_mo  = dts.dt.month.values
    arr_wd  = df["trading_weekday"].values
    arr_pwd = dts.dt.weekday.values

    cpi_mask = build_cpi_mask(dts_ns, cpi_df)
    _wdays   = set(weekdays) if weekdays is not None else set(range(5))
    _excl    = set(excl_months)

    n = len(df)
    rows = []
    traded_days = set()

    for i in range(1, n - 1):
        if np.isnan(arr_mm[i]):
            continue

        # 後場: 12:30〜15:00
        h, mi = arr_hr[i], arr_mi[i]
        is_afternoon = (
            (h == 12 and mi >= 30) or
            h == 13 or
            h == 14 or
            (h == 15 and mi == 0)
        )
        if not is_afternoon:
            continue

        tday = tday_arr[i]
        if tday in traded_days:
            continue
        if arr_mo[i] in _excl or arr_wd[i] not in _wdays:
            continue
        if cpi_mask[i]:
            continue

        # 前場の方向をフェード
        if arr_mm[i] >= thresh_pt:
            side = "short"
        elif arr_mm[i] <= -thresh_pt:
            side = "long"
        else:
            continue

        traded_days.add(tday)
        ep = float(arr_open[i + 1])
        pnl, exit_i, rtype = _exec(arr_high, arr_low, arr_close, arr_open, ep, i + 1, side,
                               tp, sl, max_hold, n, arr_hm=arr_hm, arr_pwd=arr_pwd)
        pnl -= COMMISSION_PT
        rows.append({
            "system":         "⑥B",
            "side":           side,
            "signal_dt":      dts.iloc[i],
            "signal_year":    int(dts.iloc[i].year),
            "signal_month":   int(arr_mo[i]),
            "signal_weekday": int(arr_wd[i]),
            "signal_hour":    int(arr_hr[i]),
            "pnl_pt":         round(pnl, 1),
            "pnl_yen":        int(round(pnl * PT_TO_YEN)),
            "result":         rtype,
            "entry_price":    ep,
            "entry_dt":       dts.iloc[i + 1],
            "exit_dt":        dts.iloc[exit_i],
        })

    return pd.DataFrame(rows) if rows else pd.DataFrame()


def print_monthly(trades, label=""):
    if trades.empty:
        print(f"  {label}: トレードなし")
        return
    WD_JP = {0: "月", 1: "火", 2: "水", 3: "木", 4: "金"}
    print(f"\n  {'月':>4}  {'件数':>5}  {'勝率%':>6}  {'EV(pt)':>7}  {'損益(円)':>12}  {'PF':>7}")
    print("  " + "-" * 54)
    for mo_ in range(1, 13):
        tm = trades[trades["signal_month"] == mo_]
        if len(tm) == 0:
            continue
        s = calc_summary(tm)
        mark = " ★" if mo_ == 2 else ""
        print(f"  {mo_:>2}月  {s['n']:>5}  {s['win_rate']:>5.1f}%  "
              f"{s['ev']:>+7.2f}  {s['pnl_yen']:>+12,}  {pf_s(s['pf'])}{mark}")


def print_yearly(trades):
    if trades.empty:
        return
    print(f"\n  {'年':>6}  {'件数':>5}  {'勝率%':>6}  {'EV(pt)':>7}  {'損益(円)':>12}  {'PF':>7}")
    print("  " + "-" * 56)
    for yr in sorted(trades["signal_year"].unique()):
        ty = trades[trades["signal_year"] == yr]
        s  = calc_summary(ty)
        print(f"  {yr:>6}  {s['n']:>5}  {s['win_rate']:>5.1f}%  "
              f"{s['ev']:>+7.2f}  {s['pnl_yen']:>+12,}  {pf_s(s['pf'])}")


# =========================
# main
# =========================
def main():
    df  = add_indicators(load_data())
    cpi = load_cpi()

    # ============================================================
    # A: 前日終値乖離逆張り スキャン
    # ============================================================
    print(f"\n{SEP}")
    print("  A: 前日終値乖離逆張り スキャン（全期間・全時間・CPI除外）")
    print(SEP)
    print(f"\n  {'閾値':>6} {'TP':>5} {'SL':>5} {'MH':>4}  "
          f"{'件数':>6} {'買PF':>7} {'売PF':>7} {'合PF':>7} {'EV(pt)':>7} {'損益(円)':>12}")
    print("  " + "-" * 78)

    best_a = {"ev": -999, "params": None, "trades": None}
    for thresh in [100, 150, 200, 250, 300, 400]:
        for tp in [150, 200, 250]:
            for sl in [80, 100]:
                for mh in [4, 6, 8]:
                    t = run_backtest_A(df, cpi, thresh, tp, sl, mh)
                    s = calc_summary(t)
                    if s["n"] == 0:
                        continue
                    tl = t[t["side"] == "long"]
                    ts = t[t["side"] == "short"]
                    sl_ = calc_summary(tl)
                    ss_ = calc_summary(ts)
                    mark = " ◀" if s["ev"] > best_a["ev"] else ""
                    print(f"  {thresh:>6} {tp:>5} {sl:>5} {mh:>4}  "
                          f"{s['n']:>6} {pf_s(sl_['pf']):>7} {pf_s(ss_['pf']):>7} "
                          f"{pf_s(s['pf']):>7} {s['ev']:>+7.2f} {s['pnl_yen']:>+12,}{mark}")
                    if s["ev"] > best_a["ev"] and s["n"] >= 500:
                        best_a = {"ev": s["ev"], "params": (thresh, tp, sl, mh), "trades": t}

    # A ベストパラメ: 月別・年別
    if best_a["params"]:
        th, tp, sl, mh = best_a["params"]
        print(f"\n  ── A ベスト（EV最大、件数≥500）: 閾値={th} TP={tp} SL={sl} MH={mh} ──")
        print(f"\n  [月別成績]")
        print_monthly(best_a["trades"])
        print(f"\n  [年別成績]")
        print_yearly(best_a["trades"])

    # A: 2月にフォーカス（複数閾値）
    print(f"\n{SEP}")
    print("  A: 2月 × 閾値別成績")
    print(SEP)
    print(f"\n  {'閾値':>6} {'TP':>5} {'SL':>5}  "
          f"{'2月件数':>7} {'2月勝率':>7} {'2月EV':>7} {'2月PF':>7} {'2月損益':>10}")
    print("  " + "-" * 64)
    for thresh in [100, 150, 200, 250, 300]:
        for tp in [150, 200]:
            for sl in [80, 100]:
                t = run_backtest_A(df, cpi, thresh, tp, sl, max_hold=6)
                feb = t[t["signal_month"] == 2] if not t.empty else pd.DataFrame()
                s   = calc_summary(feb)
                if s["n"] == 0:
                    continue
                print(f"  {thresh:>6} {tp:>5} {sl:>5}  "
                      f"{s['n']:>7} {s['win_rate']:>6.1f}% {s['ev']:>+7.2f} "
                      f"{pf_s(s['pf']):>7} {s['pnl_yen']:>+10,}")

    # ============================================================
    # B: 前場→後場転換 スキャン
    # ============================================================
    print(f"\n{SEP}")
    print("  B: 前場→後場転換 スキャン（全期間・後場12:30-15:00・CPI除外）")
    print(SEP)
    print(f"\n  {'閾値':>6} {'TP':>5} {'SL':>5} {'MH':>4}  "
          f"{'件数':>6} {'買PF':>7} {'売PF':>7} {'合PF':>7} {'EV(pt)':>7} {'損益(円)':>12}")
    print("  " + "-" * 78)

    best_b = {"ev": -999, "params": None, "trades": None}
    for thresh in [50, 100, 150, 200, 300]:
        for tp in [100, 150, 200]:
            for sl in [50, 80, 100]:
                for mh in [4, 6, 8]:
                    t = run_backtest_B(df, cpi, thresh, tp, sl, mh)
                    s = calc_summary(t)
                    if s["n"] == 0:
                        continue
                    tl = t[t["side"] == "long"]
                    ts = t[t["side"] == "short"]
                    sl_ = calc_summary(tl)
                    ss_ = calc_summary(ts)
                    mark = " ◀" if s["ev"] > best_b["ev"] else ""
                    print(f"  {thresh:>6} {tp:>5} {sl:>5} {mh:>4}  "
                          f"{s['n']:>6} {pf_s(sl_['pf']):>7} {pf_s(ss_['pf']):>7} "
                          f"{pf_s(s['pf']):>7} {s['ev']:>+7.2f} {s['pnl_yen']:>+12,}{mark}")
                    if s["ev"] > best_b["ev"] and s["n"] >= 200:
                        best_b = {"ev": s["ev"], "params": (thresh, tp, sl, mh), "trades": t}

    # B ベストパラメ: 月別・年別
    if best_b["params"]:
        th, tp, sl, mh = best_b["params"]
        print(f"\n  ── B ベスト（EV最大、件数≥200）: 閾値={th} TP={tp} SL={sl} MH={mh} ──")
        print(f"\n  [月別成績]")
        print_monthly(best_b["trades"])
        print(f"\n  [年別成績]")
        print_yearly(best_b["trades"])

    # B: 2月にフォーカス
    print(f"\n{SEP}")
    print("  B: 2月 × 閾値別成績")
    print(SEP)
    print(f"\n  {'閾値':>6} {'TP':>5} {'SL':>5}  "
          f"{'2月件数':>7} {'2月勝率':>7} {'2月EV':>7} {'2月PF':>7} {'2月損益':>10}")
    print("  " + "-" * 64)
    for thresh in [50, 100, 150, 200, 300]:
        for tp in [100, 150, 200]:
            for sl in [80, 100]:
                t = run_backtest_B(df, cpi, thresh, tp, sl, max_hold=6)
                feb = t[t["signal_month"] == 2] if not t.empty else pd.DataFrame()
                s   = calc_summary(feb)
                if s["n"] == 0:
                    continue
                print(f"  {thresh:>6} {tp:>5} {sl:>5}  "
                      f"{s['n']:>7} {s['win_rate']:>6.1f}% {s['ev']:>+7.2f} "
                      f"{pf_s(s['pf']):>7} {s['pnl_yen']:>+10,}")

    # ============================================================
    # A vs B: 2月の買い売り別比較（上位パラメ）
    # ============================================================
    print(f"\n{SEP}")
    print("  A vs B: 2月 買い・売り別成績（TP=200 SL=80 MH=6）")
    print(SEP)
    print(f"\n  {'手法':^5} {'閾値':>6}  {'買件数':>6} {'買EV':>7} {'買PF':>7}  "
          f"{'売件数':>6} {'売EV':>7} {'売PF':>7}  {'合PF':>7}")
    print("  " + "-" * 80)
    for thresh in [100, 150, 200, 250, 300]:
        for tag, t in [
            ("A", run_backtest_A(df, cpi, thresh, 200, 80, 6)),
            ("B", run_backtest_B(df, cpi, thresh, 200, 80, 6)),
        ]:
            feb = t[t["signal_month"] == 2] if not t.empty else pd.DataFrame()
            if feb.empty:
                continue
            fl = feb[feb["side"] == "long"]
            fs = feb[feb["side"] == "short"]
            sl_ = calc_summary(fl)
            ss_ = calc_summary(fs)
            sa_ = calc_summary(feb)
            print(f"  {tag:^5} {thresh:>6}  "
                  f"{sl_['n']:>6} {sl_['ev']:>+7.2f} {pf_s(sl_['pf']):>7}  "
                  f"{ss_['n']:>6} {ss_['ev']:>+7.2f} {pf_s(ss_['pf']):>7}  "
                  f"{pf_s(sa_['pf']):>7}")

    # ============================================================
    # DD適用後の全体成績（Aベスト・Bベスト）
    # ============================================================
    print(f"\n{SEP}")
    print("  DD -30,000円 適用後: A・B ベスト比較")
    print(SEP)
    for tag, info in [("A", best_a), ("B", best_b)]:
        if info["params"] is None:
            continue
        th, tp, sl, mh = info["params"]
        t  = info["trades"]
        r  = sim_monthly_dd(t, DD_LIMIT)
        s  = calc_summary(r["active"])
        print(f"\n  [{tag}] 閾値={th} TP={tp} SL={sl} MH={mh}")
        print(f"  全体: 件数={s['n']}  勝率={s['win_rate']:.1f}%  "
              f"EV={s['ev']:+.2f}pt  PF={pf_s(s['pf'])}  損益={s['pnl_yen']:+,}円")
        print(f"  スキップ={r['skipped']}件  DD発動={r['months_triggered']}ヶ月")
        print_yearly(r["active"])


if __name__ == "__main__":
    out_path = "bt_result_system6b.txt"
    with open(out_path, "w", encoding="utf-8") as f:
        sys.stdout = f
        main()
    sys.stdout = sys.__stdout__
    print(f"出力完了: {out_path}")
    import subprocess
    subprocess.Popen(["code", out_path])
