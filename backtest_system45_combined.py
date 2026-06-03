"""
backtest_system45_combined.py
=========================================
【逆張り版】系統④（long）＋ 系統⑤（short）合算バックテスト
系統①③と同じ分析パイプライン（時間帯・曜日・月・DD制限）

■ ON/OFF フラグ（設定セクションで切り替え）
  USE_RECOVERY : recovery_pct / fade 条件 ON/OFF
  USE_VOL      : vol_ratio 条件 ON/OFF
  USE_RSI      : RSI条件 ON/OFF
  USE_MOVE     : move_pct 条件 ON/OFF（OFFで実質全バー対象）
"""

import sys
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path
from datetime import date
import pandas as pd
import numpy as np

# =========================
# 設定
# =========================
DATA_DIR  = Path(r"C:\kabu_trade\data")
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
DD_LIMIT      = -20_000   # 月次DD上限（円）

# ★ ON/OFFフラグ
USE_RECOVERY = False  # False にすると recovery_pct 条件を無視
USE_VOL      = False  # False にすると vol_ratio 条件を無視
USE_RSI      = True   # False にすると RSI条件を無視
USE_MOVE     = True   # False にすると move_pct条件を無視

# 系統④ ロングパラメータ
LONG_PARAM = {
    "move_pct":     0.0001,
    "rsi_th":       40,
    "vol_th":       0.8,
    "lookback":     1,
    "recovery_pct": 0.002,
    "tp": 400, "sl": 80, "max_hold": 8,
}

# 系統⑤ ショートパラメータ
SHORT_PARAM = {
    "move_pct":     0.0006,
    "rsi_th":       40,
    "vol_th":       0.18,
    "lookback":     4,
    "recovery_pct": 0.002,
    "tp": 300, "sl": 80, "max_hold": 6,
}

# 時間帯デフォルト（bar END hour 基準）
S4_HOURS_DST = (14, 15, 16, 17, 23)
S4_HOURS_WIN = (14, 15, 16, 17, 23)
S5_HOURS_DST = (14, 15, 22)                    # 除外: 5,20,21,23（全確認済）
S5_HOURS_WIN = (8, 12, 14, 15, 22)            # 除外: 5,20,21,23（全確認済）

S4_WEEKDAYS  = (0, 1, 2, 3, 4)   # 全曜日
S5_WEEKDAYS  = (0, 1, 2, 3, 4)
S4_EXCL_MONTHS: tuple = (7,)
S5_EXCL_MONTHS: tuple = (1, 7)

# 取引日順ソートキー: 17:00以上=その日が取引日、17:00未満=前日が取引日
# (取引日, datetime) タプルで返すことで夜間後半(翌0時〜)と翌夜間(17時〜)の混在を防ぐ
def _trading_day_sort_key(dt):
    if dt.hour < 17:
        trading_date = (dt - pd.Timedelta(days=1)).date()
    else:
        trading_date = dt.date()
    return (pd.Timestamp(trading_date), dt)


# DST期間
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
    if not dfs:
        raise FileNotFoundError("データファイルが見つかりません")
    df = pd.concat(dfs, ignore_index=True).drop_duplicates(subset=["datetime"]).copy()
    df["_tday"] = df["datetime"].apply(
        lambda dt: (dt - pd.Timedelta(days=1)).date() if dt.hour < 17 else dt.date()
    )
    df = df.sort_values(["_tday", "datetime"]).drop(columns=["_tday"]).reset_index(drop=True)
    print(f"合計: {len(df)} 本  ({df['datetime'].min()} ~ {df['datetime'].max()})\n")
    return df


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["vol_ma20"]  = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma20"]
    delta    = df["close"].diff()
    avg_up   = delta.clip(lower=0).rolling(14).mean()
    avg_down = (-delta.clip(upper=0)).rolling(14).mean()
    rs = avg_up / avg_down.replace(0, np.nan)
    df["rsi14"] = 100 - (100 / (1 + rs))
    # 取引日の曜日: 17時未満は前日が取引日（夜間後半・日中も前日扱い）
    df["trading_weekday"] = df["datetime"].apply(
        lambda dt: (dt - pd.Timedelta(days=1)).weekday() if dt.hour < 17 else dt.weekday()
    )
    return df


def load_cpi() -> pd.DataFrame:
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


def build_masks(dts_ns: np.ndarray, cpi_df: pd.DataFrame) -> tuple:
    n = len(dts_ns)
    dst_mask = np.zeros(n, dtype=bool)
    cpi_mask = np.zeros(n, dtype=bool)
    for start, end in _DST_PERIODS:
        dst_mask |= (dts_ns >= start.value) & (dts_ns <= end.value)
    if len(cpi_df) > 0:
        b_ns = int(pd.Timedelta(minutes=30).total_seconds() * 1e9)
        a_ns = int(pd.Timedelta(minutes=60).total_seconds() * 1e9)
        for r in cpi_df["release_datetime_jst"]:
            r_ns = pd.Timestamp(r).value
            cpi_mask |= (dts_ns >= r_ns - b_ns) & (dts_ns <= r_ns + a_ns)
    return dst_mask, cpi_mask


# =========================
# バックテスト
# =========================
def run_backtest(
    df: pd.DataFrame,
    cpi_df: pd.DataFrame,
    s4_weekdays=None,
    s4_hours_dst=None,
    s4_hours_win=None,
    s4_excl_months=None,
    s5_weekdays=None,
    s5_hours_dst=None,
    s5_hours_win=None,
    s5_excl_months=None,
    use_recovery: bool = True,
    use_vol: bool = True,
    use_rsi: bool = True,
    use_move: bool = True,
) -> pd.DataFrame:

    _s4_wd   = set(s4_weekdays)   if s4_weekdays   is not None else set(S4_WEEKDAYS)
    _s4_hdst = set(s4_hours_dst)  if s4_hours_dst  is not None else set(S4_HOURS_DST)
    _s4_hwin = set(s4_hours_win)  if s4_hours_win  is not None else set(S4_HOURS_WIN)
    _s4_excl = set(s4_excl_months) if s4_excl_months is not None else set(S4_EXCL_MONTHS)
    _s5_wd   = set(s5_weekdays)   if s5_weekdays   is not None else set(S5_WEEKDAYS)
    _s5_hdst = set(s5_hours_dst)  if s5_hours_dst  is not None else set(S5_HOURS_DST)
    _s5_hwin = set(s5_hours_win)  if s5_hours_win  is not None else set(S5_HOURS_WIN)
    _s5_excl = set(s5_excl_months) if s5_excl_months is not None else set(S5_EXCL_MONTHS)

    arr_open  = df["open"].values
    arr_high  = df["high"].values
    arr_low   = df["low"].values
    arr_close = df["close"].values
    arr_rsi   = df["rsi14"].values
    arr_vol   = df["vol_ratio"].values

    dts     = pd.to_datetime(df["datetime"])
    dts_ns  = dts.values.astype("int64")
    arr_wd  = df["trading_weekday"].values   # 取引日の曜日（17時未満は前日扱い）
    arr_mo  = dts.dt.month.values
    arr_hr  = dts.dt.hour.values
    arr_min = dts.dt.minute.values
    arr_hm  = arr_hr * 100 + arr_min          # HHMM形式（強制決済判定用）
    arr_pwd = dts.dt.weekday.values            # 物理曜日（週末強制決済用）

    dst_mask, cpi_mask = build_masks(dts_ns, cpi_df)
    n = len(df)
    rows = []
    lp = LONG_PARAM
    sp = SHORT_PARAM

    for i in range(max(lp["lookback"], sp["lookback"], 20), n - 1):
        if np.isnan(arr_rsi[i]) or np.isnan(arr_vol[i]):
            continue

        # bar END hour（5分引く）
        hr  = (arr_hr[i] * 60 + arr_min[i] - 5) // 60 % 24
        mo  = arr_mo[i]
        wd  = arr_wd[i]
        dst = dst_mask[i]

        # ─── 系統④ ロング ───
        if wd in _s4_wd and mo not in _s4_excl:
            if hr in (_s4_hdst if dst else _s4_hwin):
                prev_i = i - lp["lookback"]
                move = (arr_close[i] - arr_close[prev_i]) / arr_close[prev_i]
                recovery = (arr_close[i] - arr_low[i]) / arr_close[i] if arr_close[i] != 0 else 0

                conds = []
                if use_move:
                    conds.append(move <= -lp["move_pct"])
                if use_rsi:
                    conds.append(arr_rsi[i] <= lp["rsi_th"])
                if use_vol:
                    conds.append(arr_vol[i] >= lp["vol_th"])
                if use_recovery:
                    conds.append(recovery >= lp["recovery_pct"])

                if all(conds):
                    if arr_hm[i + 1] in {1700, 845}:
                        continue
                    ep = float(arr_open[i + 1])
                    pnl, exit_i, rtype = _exec(arr_high, arr_low, arr_close, arr_open,
                                               ep, i + 1, "long",
                                               lp["tp"], lp["sl"], lp["max_hold"], n,
                                               arr_hm=arr_hm, arr_pwd=arr_pwd,
                                               close_before_gap=True)
                    pnl -= COMMISSION_PT
                    rows.append({
                        "system": "④",
                        "signal_dt":      dts.iloc[i],
                        "signal_year":    int(dts.iloc[i].year),
                        "signal_month":   mo,
                        "signal_weekday": wd,
                        "signal_hour":    hr,
                        "pnl_pt":  round(pnl, 1),
                        "pnl_yen": int(round(pnl * PT_TO_YEN)),
                        "result":  rtype,
                    })

        # ─── 系統⑤ ショート ───
        if wd in _s5_wd and mo not in _s5_excl and not cpi_mask[i]:
            if hr in (_s5_hdst if dst else _s5_hwin):
                prev_i = i - sp["lookback"]
                rise = (arr_close[i] - arr_close[prev_i]) / arr_close[prev_i]
                fade = (arr_high[i] - arr_close[i]) / arr_close[i] if arr_close[i] != 0 else 0

                conds = []
                if use_move:
                    conds.append(rise >= sp["move_pct"])
                if use_rsi:
                    conds.append(arr_rsi[i] >= sp["rsi_th"])
                if use_vol:
                    conds.append(arr_vol[i] >= sp["vol_th"])
                if use_recovery:
                    conds.append(fade >= sp["recovery_pct"])

                if all(conds):
                    if arr_hm[i + 1] in {1700, 845}:
                        continue
                    ep = float(arr_open[i + 1])
                    pnl, exit_i, rtype = _exec(arr_high, arr_low, arr_close, arr_open,
                                               ep, i + 1, "short",
                                               sp["tp"], sp["sl"], sp["max_hold"], n,
                                               arr_hm=arr_hm, arr_pwd=arr_pwd,
                                               close_before_gap=True)
                    pnl -= COMMISSION_PT
                    rows.append({
                        "system": "⑤",
                        "signal_dt":      dts.iloc[i],
                        "signal_year":    int(dts.iloc[i].year),
                        "signal_month":   mo,
                        "signal_weekday": wd,
                        "signal_hour":    hr,
                        "pnl_pt":  round(pnl, 1),
                        "pnl_yen": int(round(pnl * PT_TO_YEN)),
                        "result":  rtype,
                    })

    return pd.DataFrame(rows) if rows else pd.DataFrame()


def _exec(arr_high, arr_low, arr_close, arr_open, ep, entry_i, side, tp, sl, max_hold, n,
          arr_hm=None, arr_pwd=None, close_before_gap=False):
    GAP_BOUNDARIES = frozenset({1540, 555})  # 15:40=昼終値, 5:55=夜間終値
    for j in range(entry_i, min(entry_i + max_hold, n)):
        # ギャップ前強制決済（①③④⑤用）
        if close_before_gap and arr_hm is not None and arr_hm[j] in GAP_BOUNDARIES:
            cl = arr_close[j]
            pnl = float(cl - ep) if side == "long" else float(ep - cl)
            return pnl, j, f"GAP_CLOSE_{arr_hm[j]}"

        hi = arr_high[j]; lo = arr_low[j]; op = arr_open[j]
        if side == "long":
            if hi >= ep + tp:
                # openがTP方向に超えていたらopen価格で決済（より有利）
                exit_p = op if op >= ep + tp else ep + tp
                return float(exit_p - ep), j, "TP"
            if lo <= ep - sl:
                # openがSL方向に超えていたらopen価格で決済（より不利）
                exit_p = op if op <= ep - sl else ep - sl
                return float(exit_p - ep), j, "SL"
        else:
            if lo <= ep - tp:
                # openがTP方向に超えていたらopen価格で決済（より有利）
                exit_p = op if op <= ep - tp else ep - tp
                return float(ep - exit_p), j, "TP"
            if hi >= ep + sl:
                # openがSL方向に超えていたらopen価格で決済（より不利）
                exit_p = op if op >= ep + sl else ep + sl
                return float(ep - exit_p), j, "SL"
        # 週末強制決済: 物理月曜06:00（日曜夜間終了→08:45日中開始前のギャップ直前）
        if arr_hm is not None and arr_pwd is not None:
            if arr_pwd[j] == 0 and arr_hm[j] == 600:
                cl = arr_close[j]
                pnl = float(cl - ep) if side == "long" else float(ep - cl)
                return pnl, j, "WEEKEND"
    exit_i = min(entry_i + max_hold - 1, n - 1)
    cl = arr_close[exit_i]
    pnl = float(cl - ep) if side == "long" else float(ep - cl)
    return pnl, exit_i, "TIME"


# =========================
# 集計・出力ユーティリティ
# =========================
def calc_summary(df: pd.DataFrame) -> dict:
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


SEP = "=" * 78


def print_header(label):
    print(f"\n{SEP}\n  {label}\n{SEP}")


def sim_monthly_dd(trades: pd.DataFrame, dd_limit: int) -> dict:
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
        "active":           df[df["keep"]].drop(columns=["ym","keep"]),
        "skipped":          int((~df["keep"]).sum()),
        "months_triggered": len(triggered),
    }


def all_years_negative(df, year_col, key_col, key, min_trades=20, min_years=2):
    grp = df[df[key_col] == key]
    years = grp[year_col].unique()
    valid = 0; neg = 0
    for yr in years:
        g = grp[grp[year_col] == yr]
        if len(g) < min_trades:
            continue
        valid += 1
        pnl = g["pnl_pt"].values.astype(float)
        wins = pnl[pnl > 0].sum(); loss = abs(pnl[pnl < 0].sum())
        pf = wins / loss if loss > 0 else float("inf")
        if pf < 1.0:
            neg += 1
    return valid >= min_years and neg == valid


# =========================
# main
# =========================
def main():
    df  = add_indicators(load_data())
    cpi = load_cpi()

    # ─── ON/OFF比較 ───
    print_header("ON/OFFフラグ比較（条件を1つずつ外していく）")
    combos = [
        # rec   vol    rsi    move   ラベル
        (True,  True,  True,  True,  "全ON（元の条件）         "),
        (False, True,  True,  True,  "recovery=OFF            "),
        (False, False, True,  True,  "recovery+vol=OFF        "),
        (False, False, False, True,  "recovery+vol+RSI=OFF    "),
        (False, False, False, False, "全OFF（時間帯のみ）       "),
    ]
    print(f"\n  {'設定':<26}  {'④N':>6} {'④PF':>6}  {'⑤N':>6} {'⑤PF':>6}  {'合N':>6} {'合PF':>6}  {'合損益(円)':>12}")
    print("  " + "-" * 82)
    for rec, vol, rsi, move, lbl in combos:
        t = run_backtest(df, cpi, use_recovery=rec, use_vol=vol, use_rsi=rsi, use_move=move)
        if t.empty:
            print(f"  {lbl}  (トレードなし)")
            continue
        t4 = t[t["system"]=="④"]; t5 = t[t["system"]=="⑤"]
        s4 = calc_summary(t4); s5 = calc_summary(t5); sa = calc_summary(t)
        print(f"  {lbl}  {s4['n']:>6} {pf_s(s4['pf']):>6}  {s5['n']:>6} {pf_s(s5['pf']):>6}  "
              f"{sa['n']:>6} {pf_s(sa['pf']):>6}  {sa['pnl_yen']:>+12,}")

    # ─── 以降は USE_RECOVERY / USE_VOL 設定で実行 ───
    trades = run_backtest(df, cpi,
                         use_recovery=USE_RECOVERY, use_vol=USE_VOL,
                         use_rsi=USE_RSI, use_move=USE_MOVE)
    if trades.empty:
        print("トレードなし"); return
    t4 = trades[trades["system"]=="④"]
    t5 = trades[trades["system"]=="⑤"]

    print_header(f"全体成績（recovery={'ON' if USE_RECOVERY else 'OFF'} vol={'ON' if USE_VOL else 'OFF'}）")
    for lbl, t in [("系統④ ロング", t4), ("系統⑤ ショート", t5), ("合算", trades)]:
        s = calc_summary(t)
        print(f"  {lbl:10}  件数:{s['n']:>5}  勝率:{s['win_rate']:>5.1f}%  "
              f"損益:{s['pnl_yen']:>+10,}円  EV:{s['ev']:>+6.2f}  PF:{pf_s(s['pf'])}")

    WD_NAMES = {0:"月",1:"火",2:"水",3:"木",4:"金"}
    MO_NAMES = {1:"1月",2:"2月",3:"3月",4:"4月",5:"5月",6:"6月",
                7:"7月",8:"8月",9:"9月",10:"10月",11:"11月",12:"12月"}

    # ─── 時間帯別PF ───
    print_header("時間帯別 PF（bar END hour 基準）")
    for sys_lbl, t in [("④", t4), ("⑤", t5)]:
        dst_t = trades[trades["system"]==sys_lbl]  # 全データからDST判定済み
        hours = sorted(t["signal_hour"].unique())
        print(f"\n  【系統{sys_lbl}】")
        print(f"  {'時':>4}  {'件数':>5}  {'勝率%':>6}  {'EV':>7}  {'PF':>7}  {'損益(円)':>11}")
        print("  " + "-" * 52)
        for hr in hours:
            s = calc_summary(t[t["signal_hour"]==hr])
            print(f"  {hr:>2}時  {s['n']:>5}  {s['win_rate']:>5.1f}%  {s['ev']:>+6.2f}  "
                  f"{pf_s(s['pf']):>7}  {s['pnl_yen']:>+11,}")

    # ─── 曜日別PF ───
    print_header("曜日別 PF")
    for sys_lbl, t in [("④", t4), ("⑤", t5), ("合算", trades)]:
        print(f"\n  【系統{sys_lbl}】")
        print(f"  {'曜日':>4}  {'件数':>5}  {'勝率%':>6}  {'EV':>7}  {'PF':>7}  {'損益(円)':>11}  毎年-")
        print("  " + "-" * 60)
        for wd in range(5):
            s = calc_summary(t[t["signal_weekday"]==wd])
            if s["n"] == 0: continue
            flag = "◎" if all_years_negative(t, "signal_year", "signal_weekday", wd) else ""
            print(f"  {WD_NAMES[wd]:>3}  {s['n']:>5}  {s['win_rate']:>5.1f}%  {s['ev']:>+6.2f}  "
                  f"{pf_s(s['pf']):>7}  {s['pnl_yen']:>+11,}  {flag}")

    # ─── 月別PF ───
    print_header("月別 PF")
    for sys_lbl, t in [("④", t4), ("⑤", t5), ("合算", trades)]:
        print(f"\n  【系統{sys_lbl}】")
        print(f"  {'月':>5}  {'件数':>5}  {'勝率%':>6}  {'EV':>7}  {'PF':>7}  {'損益(円)':>11}  毎年-")
        print("  " + "-" * 62)
        for mo in range(1, 13):
            s = calc_summary(t[t["signal_month"]==mo])
            if s["n"] == 0: continue
            flag = "◎" if all_years_negative(t, "signal_year", "signal_month", mo) else ""
            print(f"  {MO_NAMES[mo]:>5}  {s['n']:>5}  {s['win_rate']:>5.1f}%  {s['ev']:>+6.2f}  "
                  f"{pf_s(s['pf']):>7}  {s['pnl_yen']:>+11,}  {flag}")

    # ─── 月次DD制限分析 ───
    print_header(f"月次損失上限分析（合算）  DD制限={DD_LIMIT:,}円")
    limits = [None, -15_000, -20_000, -30_000, -40_000]
    print(f"\n  {'制限(円)':>12}  {'件数':>5}  {'スキップ':>7}  {'発動月':>5}  {'勝率%':>6}  {'損益(円)':>11}  {'PF':>7}")
    print("  " + "-" * 68)
    for lim in limits:
        if lim is None:
            s = calc_summary(trades)
            print(f"  {'   制限なし':>12}  {s['n']:>5}  {'':>7}  {'':>5}  "
                  f"{s['win_rate']:>5.1f}%  {s['pnl_yen']:>+11,}  {pf_s(s['pf']):>7}")
        else:
            res = sim_monthly_dd(trades, lim)
            s   = calc_summary(res["active"])
            print(f"  {lim:>+12,}  {s['n']:>5}  {res['skipped']:>7}  {res['months_triggered']:>5}  "
                  f"{s['win_rate']:>5.1f}%  {s['pnl_yen']:>+11,}  {pf_s(s['pf']):>7}")

    # ─── 年×月クロス集計（DD適用後） ───
    print_header(f"年×月 クロス集計（月次DD {DD_LIMIT:,}円適用後）")
    trades_dd = sim_monthly_dd(trades, DD_LIMIT)["active"]
    months = list(range(1, 13))
    hdr = "  年  系統  " + "".join(f"  {m:>4}月" for m in months) + "    合計"
    print(hdr)
    print("-" * len(hdr))
    for yr in sorted(trades_dd["signal_year"].unique()):
        for sys_lbl in ["④", "⑤", "合"]:
            if sys_lbl == "合":
                t = trades_dd[trades_dd["signal_year"]==yr]
            else:
                t = trades_dd[(trades_dd["signal_year"]==yr) & (trades_dd["system"]==sys_lbl)]
            row_vals = []
            for mo in months:
                v = t[t["signal_month"]==mo]["pnl_yen"].sum()
                row_vals.append(f"{int(v):>+7,}" if v != 0 else "      -")
            total = int(t["pnl_yen"].sum())
            print(f"  {yr}  {sys_lbl:>2}   " + "  ".join(row_vals) + f"  {total:>+8,}")
        print()

    # ─── スリッページ耐久性 ───
    print_header("スリッページ耐久性（合算・DD適用後）")
    slips = [0, 2, 4, 6, 8, 10, 15, 20]
    print(f"\n  {'slip':>5}  {'件数':>5}  {'勝率%':>6}  {'損益(円)':>12}  {'PF':>7}")
    print("  " + "-" * 46)
    for sl in slips:
        t = trades_dd.copy()
        t["pnl_pt"]  = t["pnl_pt"] - sl
        t["pnl_yen"] = (t["pnl_pt"] * PT_TO_YEN).astype(int)
        s = calc_summary(t)
        print(f"  {sl:>3}pt  {s['n']:>5}  {s['win_rate']:>5.1f}%  {s['pnl_yen']:>+12,}  {pf_s(s['pf']):>7}")


def _slip_pf(pf, total_ev_pt, win_rate_pct, n, slip_pt):
    """スリッページ適用後のPF計算"""
    if n == 0 or pf <= 1:
        return 0.0
    p = win_rate_pct / 100
    total_pnl = total_ev_pt * n
    gl = total_pnl / (pf - 1)
    gw = pf * gl
    gw2 = gw - p * n * slip_pt
    gl2 = gl + (1 - p) * n * slip_pt
    return gw2 / gl2 if gl2 > 0 else float("inf")


def grid_search_s4(df, cpi):
    """系統④ TP/SL グリッドサーチ"""
    from itertools import product as iprod
    TP_LIST = [80, 100, 120, 150, 200, 250, 300, 400]
    SL_LIST = [50, 60, 80, 100, 120, 150]
    orig_tp, orig_sl = LONG_PARAM["tp"], LONG_PARAM["sl"]

    rows = []
    total = len(TP_LIST) * len(SL_LIST)
    done = 0
    for tp, sl in iprod(TP_LIST, SL_LIST):
        LONG_PARAM["tp"] = tp
        LONG_PARAM["sl"] = sl
        t = run_backtest(df, cpi, use_recovery=USE_RECOVERY, use_vol=USE_VOL,
                         use_rsi=USE_RSI, use_move=USE_MOVE)
        t4 = t[t["system"] == "④"] if not t.empty else t
        s = calc_summary(t4)
        n, wr, ev, pf0, pnl = s["n"], s["win_rate"], s["ev"], s["pf"], s["pnl_yen"]
        pf4 = _slip_pf(pf0, ev, wr, n, 4)
        pf8 = _slip_pf(pf0, ev, wr, n, 8)
        rows.append((tp, sl, n, wr, ev, pf0, pf4, pf8, pnl))
        done += 1
        print(f"\r  検証中... {done}/{total}", end="", flush=True)

    LONG_PARAM["tp"] = orig_tp
    LONG_PARAM["sl"] = orig_sl

    rows.sort(key=lambda x: x[5], reverse=True)
    print(f"\r", end="")
    print("\n" + "=" * 90)
    print("  系統④ TP/SL グリッドサーチ結果（PF降順）")
    print("=" * 90)
    print(f"  {'TP':>4}  {'SL':>4}  {'件数':>6}  {'勝率%':>6}  {'EV(pt)':>7}  "
          f"{'PF(0)':>7}  {'PF(4pt)':>7}  {'PF(8pt)':>7}  {'損益(万円)':>9}")
    print("  " + "-" * 85)
    for tp, sl, n, wr, ev, pf0, pf4, pf8, pnl in rows:
        flag = " ★" if pf0 >= 1.30 else (" ▲" if pf0 >= 1.20 else "")
        print(f"  {tp:>4}  {sl:>4}  {n:>6}  {wr:>5.1f}%  {ev:>+7.2f}  "
              f"{pf0:>7.3f}  {pf4:>7.3f}  {pf8:>7.3f}  {pnl/10000:>+9.1f}万{flag}")


if __name__ == "__main__":
    if "--grid" in sys.argv:
        _df = add_indicators(load_data())
        _cpi = load_cpi()
        grid_search_s4(_df, _cpi)
    else:
        with open("bt_result45.txt", "w", encoding="utf-8") as f:
            sys.stdout = f
            main()
        sys.stdout = sys.__stdout__
        try:
            import subprocess
            subprocess.Popen(["code", "bt_result45.txt"])
        except FileNotFoundError:
            pass
