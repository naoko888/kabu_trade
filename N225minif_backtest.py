"""
python　N225minif_backtest.py　
===========================================
系統①（long）＋ 系統③（short）合算バックテスト
auto_trade.py の DST / CPI / 時間条件を完全反映済み

■ 出力構成
  [A] シナリオ1: 系統① (3月・7月) ＋ 系統③ (DST/CPI込み)
  [B] シナリオ2: 系統① (3・7・11月除外) ＋ 系統③ (同上)
  [C] backtest_perfect_order.py 比較
      系統③ 現行2線(ma9<ma20) vs 完全PO3線(ma9<ma20<ma55)
  [D] 月次損失上限分析 (-20/-30/-40/-50 万円)
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
DATA_DIR   = Path(r"C:\kabu_trade\data")
EXCEL_FILES = [
    "225mini2011d.xls",
    "N225minif_2012.xls",
    "N225minif_2013.xls",
    "N225minif_2014.xls",
    "N225minif_2015.xls",
    "N225minif_2016.xlsx",
    "N225minif_2017.xlsx",
    "N225minif_2018.xlsx",
    "N225minif_2019.xlsx",
    "N225minif_2020.xlsx",
    "N225minif_2021.xlsx",
    "N225minif_2022.xlsx",
    "N225minif_2023.xlsx",
    "N225minif_2024.xlsx",
    "N225minif_2025.xlsx",
    "N225minif_2026.xlsx",
]

MICRO_CSV   = Path(r"C:\kabu_trade\micro_5min.csv")
CPI_CSV     = Path(r"C:\kabu_trade\economic_calendar.csv")

TP             = 240
SL             = 60
MAX_HOLD       = 120
TOUCH_PCT      = 0.005
COMMISSION_PT  = 2.2
PT_TO_YEN      = 10

SESSION_BOUNDARIES = frozenset({2350})

# 系統① 条件
S1_WEEKDAYS = (0, 1, 2, 3, 4)
S1_HOURS      = (8, 12, 15, 18, 19, 20, 21, 23)
S1_EXCL_BASE  = (3, 5, 7, 11)

# 系統③ 条件（backtest_perfect_order.py パターン⑤ 準拠）
# 時刻: bar START hour（+5min シフトなし）
# 曜日: s_strong_weekdays（commission=0 EV>0 n>=100）= 月・水・木・金
# 除外月: 5月・7月・11月
# 時間帯: DST/冬時間で分岐（bar START hour 基準）
# CPI除外: 有効（発表前30分〜後60分）
S3_WEEKDAYS    = (0, 2, 3, 4)     # 月・水・木・金
# 変更後
S3_EXCL_MONTHS = (7, 11)
S3_HOURS_DST   = (5, 8, 12, 14, 15, 19, 20, 22, 23)  # DST期間 bar START hour
S3_HOURS_WIN   = (5, 12, 15, 19, 20, 21, 22, 23)      # 冬時間  bar START hour

# 米国サマータイム期間
_DST_PERIODS = [
    (pd.Timestamp("2023-03-12"), pd.Timestamp("2023-11-05")),
    (pd.Timestamp("2024-03-10"), pd.Timestamp("2024-11-03")),
    (pd.Timestamp("2025-03-09"), pd.Timestamp("2025-11-02")),
    (pd.Timestamp("2026-03-08"), pd.Timestamp("2026-11-01")),
]

HOLIDAYS = {
    date(2023,1,3), date(2023,2,23), date(2023,3,21),
    date(2023,5,3), date(2023,5,4), date(2023,5,5),
    date(2023,7,17), date(2023,8,11), date(2023,9,18),
    date(2023,10,9), date(2023,11,23),
    date(2024,1,3), date(2024,2,12), date(2024,2,23),
    date(2024,3,20), date(2024,4,29), date(2024,5,3),
    date(2024,5,6), date(2024,7,15), date(2024,9,23),
    date(2025,1,3), date(2025,2,11), date(2025,2,24),
    date(2025,3,20), date(2025,4,29), date(2025,5,5),
    date(2025,5,6), date(2025,7,21), date(2025,8,11),
    date(2025,9,23), date(2025,10,13), date(2025,11,3),
    date(2025,11,24),
    date(2026,1,12), date(2026,2,11), date(2026,2,23),
    date(2026,3,20), date(2026,4,29), date(2026,5,4),
    date(2026,5,5), date(2026,5,6), date(2026,7,20),
    date(2026,8,11), date(2026,9,21), date(2026,9,22),
    date(2026,9,23), date(2026,10,12), date(2026,11,3),
}


# =========================
# データ読み込み
# =========================
def read_excel(path: Path) -> pd.DataFrame:
    engine = "xlrd" if path.suffix == ".xls" else "openpyxl"
    df = pd.read_excel(path, sheet_name="5min", engine=engine)
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

    # ===== BB追加 =====
    df["bb_mid"] = df["close"].rolling(20).mean()
    df["bb_std"] = df["close"].rolling(20).std()
    df["bb_up"]  = df["bb_mid"] + 2 * df["bb_std"]
    df["bb_low"] = df["bb_mid"] - 2 * df["bb_std"]

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


def load_indicator(name: str) -> pd.Series:
    """economic_calendar.csv から指定指標の発表時刻を返す"""
    for enc in ("utf-8", "utf-8-sig", "cp932"):
        try:
            df = pd.read_csv(CPI_CSV, encoding=enc)
            if "indicator" in df.columns and "release_datetime_jst" in df.columns:
                sub = df[df["indicator"] == name].copy()
                sub["release_datetime_jst"] = pd.to_datetime(sub["release_datetime_jst"])
                sub = sub.dropna(subset=["release_datetime_jst"])
                return sub["release_datetime_jst"].reset_index(drop=True)
        except Exception:
            continue
    return pd.Series(dtype="datetime64[ns]")


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
                 s3_excl_months=None,
                 s3_po: bool = False,
                 use_ma_dist=False,
                 use_entry_limit=False,
                 ma_dist_th: float = 0.003,
                 s1_weekdays=None,
                 s1_hours=None,
                 s3_hours=None,
                 skip_holidays=False) -> pd.DataFrame:
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
    dt_list     = dts.to_list()
    dts_ns      = dts.values.astype("int64")

    n = len(df)

    # DST / CPI マスク（CPI除外はパターン⑤と同じ条件）
    dst_mask, cpi_mask = build_masks(dts_ns, cpi_df if cpi_df is not None else pd.DataFrame(columns=["release_datetime_jst"]))
    s1_excl_set = set(s1_excl_months)
    s3_excl_set = set(S3_EXCL_MONTHS) if s3_excl_months is None else set(s3_excl_months)
    _s1_wd = set(S1_WEEKDAYS) if s1_weekdays is None else set(s1_weekdays)
    _s1_hr = set(S1_HOURS) if s1_hours is None else set(s1_hours)
    _s3_hr = None if s3_hours is None else set(s3_hours)
    _holiday_dates = HOLIDAYS if skip_holidays else set()

    trades = []

    # ←ここに入れる
    last_entry_i = -999
    position_bars = 0

    for i in range(3, n):
        sig_i = i - 1
        ent_i = i

        # ★ここに追加
        if position_bars > 0:
            position_bars += 1
            if position_bars > 5:
                position_bars = 0

        ma9  = arr_ma9[sig_i];  ma10 = arr_ma10[sig_i]
        ma20 = arr_ma20[sig_i]; ma55 = arr_ma55[sig_i]
        macd = arr_macd[sig_i]; msig = arr_msig[sig_i]

        if any(np.isnan(v) for v in [ma9, ma10, ma20, ma55, macd, msig]):
            continue

        hr    = (arr_hour[sig_i] * 60 + arr_minute[sig_i] + 5) // 60 % 24  # 系統①用（bar END hour）
        h_raw = arr_hour[sig_i]                                               # 系統③用（bar START hour）
        wd = arr_weekday[sig_i]
        mo = arr_month[sig_i]
        lo = arr_low[sig_i]
        hi = arr_high[sig_i]

        entry_ok = True
        if use_entry_limit:
            entry_ok = (position_bars == 0)

        if skip_holidays:
            _sig_date = dt_list[sig_i].date()
            if dt_list[sig_i].hour < 6:
                _sig_date = (dt_list[sig_i] - pd.Timedelta(days=1)).date()
            if _sig_date in _holiday_dates:
                continue

        # ─── 系統① ───
        if wd in _s1_wd and hr in _s1_hr and mo not in s1_excl_set:
            ma9p  = arr_ma9[sig_i-1];  ma10p  = arr_ma10[sig_i-1]
            ma9p2 = arr_ma9[sig_i-2];  ma10p2 = arr_ma10[sig_i-2]
            c1    = arr_close[sig_i-1]; c2    = arr_close[sig_i-2]

            if any(np.isnan(v) for v in [ma9p, ma10p, ma9p2, ma10p2]):
                pass
            else:
                above = (c2 > ma9p2 and c2 > ma10p2 and c1 > ma9p and c1 > ma10p)
                touch = (abs(lo - ma9)  / ma9  <= TOUCH_PCT or
                         abs(lo - ma10) / ma10 <= TOUCH_PCT)
                gc    = (macd > msig)

                ma_dist_ok = True
                if use_ma_dist:
                    ma_dist_ok = abs(arr_close[sig_i] - arr_ma20[sig_i]) / arr_ma20[sig_i] <= ma_dist_th

                if above and touch and gc and ma_dist_ok and entry_ok:
                    ep = arr_open[ent_i]
                    pnl, rtype = _exec(arr_high, arr_low, arr_close, arr_hm,
                                       ep, ent_i, "long", n)
                    pnl -= COMMISSION_PT
                    trades.append({
                        "system":        "①",
                        "signal_dt":     dt_list[sig_i],
                        "signal_year":   dt_list[sig_i].year,
                        "signal_month":  mo,
                        "signal_hour":   hr,
                        "signal_weekday": wd,
                        "pnl_pt":        pnl,
                        "pnl_yen":       round(pnl * PT_TO_YEN, 0),
                        "result":        rtype,
                    })
                    position_bars = 1

        ma_dist_ok = True
        if use_ma_dist:
            ma_dist_ok = abs(arr_close[sig_i] - arr_ma20[sig_i]) / arr_ma20[sig_i] <= ma_dist_th

        # ─── 系統③ ───
        # bar START hour で判定（backtest_perfect_order.py パターン⑤ 準拠）
        # DST対応時間帯フィルター + CPI除外（s_strong_weekdays = 月・木）
        # 5月は後半（16日以降）のみ
        _s3_may_ok = (mo != 5) or (dt_list[sig_i].day >= 16)
        if wd in S3_WEEKDAYS and mo not in s3_excl_set and not cpi_mask[sig_i] and _s3_may_ok:
            
            _s3_active = _s3_hr if _s3_hr is not None else (S3_HOURS_DST if dst_mask[sig_i] else S3_HOURS_WIN)
            if h_raw in _s3_active:
                if s3_po:
                    below = (ma9 < ma20 < ma55)
                else:
                    below = (ma9 < ma20)
                touch_hi = (abs(hi - ma9) / ma9 <= TOUCH_PCT)
                dc       = (macd < msig)

                if below and touch_hi and dc and ma_dist_ok and entry_ok:
                    ep = arr_open[ent_i]
                    pnl, rtype = _exec(arr_high, arr_low, arr_close, arr_hm,
                                       ep, ent_i, "short", n,
                                       force_session_close=False,  # パターン⑤準拠: セッション境界強制決済なし
                                       max_hold=50)                # パターン⑤準拠: 最大保有50本
                    pnl -= COMMISSION_PT
                    trades.append({
                        "system":        "③",
                        "signal_dt":     dt_list[sig_i],
                        "signal_year":   dt_list[sig_i].year,
                        "signal_month":  mo,
                        "signal_hour":   hr,
                        "signal_weekday": wd,
                        "pnl_pt":        pnl,
                        "pnl_yen":       round(pnl * PT_TO_YEN, 0),
                        "result":        rtype,
                    })
                    position_bars = 1
            

    if not trades:
        return pd.DataFrame()
    return pd.DataFrame(trades).reset_index(drop=True)


def _exec(arr_high, arr_low, arr_close, arr_hm,
          ep: float, ei: int, side: str, n: int,
          force_session_close: bool = True,
          max_hold: int = MAX_HOLD):
    """Trade execution kernel (inlined for performance)
    force_session_close=False: no 23:50 forced exit (matches backtest_perfect_order.py behavior)
    max_hold: max bars to hold (default MAX_HOLD=120; use 50 for system ③ パターン⑤準拠)
    """
    pnl   = None
    rtype = None
    exit_bar = ei

    for j in range(ei, min(ei + max_hold, n)):
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
        if force_session_close and arr_hm[j] in SESSION_BOUNDARIES:
            cl = arr_close[j]
            pnl = float(cl - ep) if side == "long" else float(ep - cl)
            rtype, exit_bar = "SESSION", j; break

    if pnl is None:
        cidx = min(ei + max_hold - 1, n - 1)
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
    trades = run_backtest(df, cpi, s1_weekdays=(0,1,2,3,4))
    t1 = trades[trades["system"] == "①"]
    t3 = trades[trades["system"] == "③"]
    print(f"  系統①: {len(t1)}件  系統③: {len(t3)}件  合計: {len(trades)}件")

    # ─ 条件サマリー ─
    print_scenario_header("系統①③合算バックテスト（auto_trade.py 現行条件）")
    print("  系統①: 月〜金 / 8,12,15,18,19,20,21,23時 / 3月・5月・7月・11月除外 / CPI除外なし")
    print("  系統③: 月・水・木・金(bar START hour) / DST:[5,8,12,14,15,19,20,22,23] 冬:[5,12,15,19,20,21,22,23]")
    print("         / 5月・7月・11月除外 / CPI発表前30min~後60min除外")
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
        
        # =========================
    # MA距離フィルター総当たり（Before / After）
    # =========================
    print_scenario_header("MA距離フィルター総当たり（Before / After）")

    ma_dist_list = [0.002, 0.003, 0.004]

    # Before
    base_trades = run_backtest(
        df, cpi,
        s1_excl_months=S1_EXCL_BASE,
        s3_po=False,
        use_ma_dist=False,
        use_entry_limit=False,
    )
    base_s = calc_summary(base_trades)

    print("\n  [Before]")
    print(f"  件数={base_s['n']}  勝率={base_s['win_rate']:.1f}%  "
          f"損益(pt)={base_s['pnl_pt']:+.1f}  損益(円)={base_s['pnl_yen']:+,.0f}  "
          f"期待値={base_s['ev_pt']:+.2f}  PF={pf_str(base_s['pf'])}")

    print("\n  [After: MA距離フィルター]")
    print(f"  {'閾値':>8}  {'件数':>6}  {'勝率%':>6}  {'損益(pt)':>10}  "
          f"{'損益(円)':>12}  {'期待値':>8}  {'PF':>6}  {'Δ損益(pt)':>10}  {'ΔPF':>8}")
    print("  " + "-" * 95)

    for ma_th in ma_dist_list:
        ma_trades = run_backtest(
            df, cpi,
            s1_excl_months=S1_EXCL_BASE,
            s3_po=False,
            use_ma_dist=True,
            use_entry_limit=False,
            ma_dist_th=ma_th,
        )
        s = calc_summary(ma_trades)

        diff_pnl = s["pnl_pt"] - base_s["pnl_pt"]
        diff_pf  = s["pf"] - base_s["pf"]

        print(f"  {ma_th:>8.3f}  {s['n']:>6}  {s['win_rate']:>5.1f}%  "
              f"{s['pnl_pt']:>+10.1f}  {s['pnl_yen']:>+12,.0f}  "
              f"{s['ev_pt']:>+8.2f}  {pf_str(s['pf'])}  "
              f"{diff_pnl:>+10.1f}  {diff_pf:>+8.3f}")
        
    # =========================
    # DD発動月の詳細表示
    # =========================
    print_scenario_header("DD発動月 詳細")

    for lim in [-20_000, -30_000, -40_000, -50_000]:
        res = sim_monthly_dd(trades, lim)
        df_dd = trades.sort_values("signal_dt").copy()
        df_dd["ym"] = list(zip(df_dd["signal_year"], df_dd["signal_month"]))

        month_pnl = {}
        triggered = set()

        for _, row in df_dd.iterrows():
            ym = row["ym"]
            if ym not in month_pnl:
                month_pnl[ym] = 0.0

            if ym in triggered:
                continue

            month_pnl[ym] += row["pnl_yen"]
            if month_pnl[ym] <= lim:
                triggered.add(ym)

        # 表示
        ym_list = sorted(triggered)

        print(f"\n制限 {lim:+,}円 → 発動月 {len(ym_list)}件")

        for y, m in ym_list:
            print(f"  {y}-{str(m).zfill(2)}")

    # =========================
    # 除外月分析（系統①③）
    # =========================
    print_scenario_header("【除外月分析】系統① 除外月（3/5/7/11月）の成績")

    # 除外月なしで再BT
    trades_all = run_backtest(df, cpi, s1_excl_months=())
    t1_excl = trades_all[
        (trades_all["system"] == "①") &
        (trades_all["signal_month"].isin([3, 5, 7, 11]))
    ].copy()

    # 月別
    print("\n  [月別]")
    print(f"  {'月':>4}  {'件数':>5}  {'勝率%':>6}  {'損益(円)':>11}  {'PF':>6}")
    print("  " + "-" * 45)
    for mo in [3, 5, 7, 11]:
        grp = t1_excl[t1_excl["signal_month"] == mo]
        s = calc_summary(grp)
        print(f"  {mo:>3}月  {s['n']:>5}  {s['win_rate']:>5.1f}%  {s['pnl_yen']:>+11,.0f}  {pf_str(s['pf'])}")

    # 曜日別
    print("\n  [曜日別]")
    day_names = {0:"月曜", 1:"火曜", 2:"水曜", 3:"木曜", 4:"金曜"}
    print(f"  {'曜日':>4}  {'件数':>5}  {'勝率%':>6}  {'損益(円)':>11}  {'PF':>6}")
    print("  " + "-" * 45)
    for wd in [0, 1, 2]:
        grp = t1_excl[t1_excl["signal_weekday"] == wd]
        s = calc_summary(grp)
        print(f"  {day_names[wd]}  {s['n']:>5}  {s['win_rate']:>5.1f}%  {s['pnl_yen']:>+11,.0f}  {pf_str(s['pf'])}")

    # 時間帯別
    print("\n  [時間帯別]")
    print(f"  {'時間':>4}  {'件数':>5}  {'勝率%':>6}  {'損益(円)':>11}  {'PF':>6}")
    print("  " + "-" * 45)
    for hr in sorted(t1_excl["signal_hour"].unique()):
        grp = t1_excl[t1_excl["signal_hour"] == hr]
        s = calc_summary(grp)
        print(f"  {hr:>3}時  {s['n']:>5}  {s['win_rate']:>5.1f}%  {s['pnl_yen']:>+11,.0f}  {pf_str(s['pf'])}")

    # 年別
    print("\n  [年別]")
    print(f"  {'年':>4}  {'件数':>5}  {'勝率%':>6}  {'損益(円)':>11}  {'PF':>6}")
    print("  " + "-" * 45)
    for yr in sorted(t1_excl["signal_year"].unique()):
        grp = t1_excl[t1_excl["signal_year"] == yr]
        s = calc_summary(grp)
        print(f"  {yr}  {s['n']:>5}  {s['win_rate']:>5.1f}%  {s['pnl_yen']:>+11,.0f}  {pf_str(s['pf'])}")

    # 7月 年別詳細
    t1_jul = t1_excl[t1_excl["signal_month"] == 7]
    if not t1_jul.empty:
        print("\n  [7月 年別詳細]")
        print(f"  {'年':>4}  {'件数':>5}  {'勝率%':>6}  {'損益(円)':>11}  {'PF':>6}")
        print("  " + "-" * 45)
        for yr in sorted(t1_jul["signal_year"].unique()):
            grp = t1_jul[t1_jul["signal_year"] == yr]
            s = calc_summary(grp)
            print(f"  {yr}  {s['n']:>5}  {s['win_rate']:>5.1f}%  {s['pnl_yen']:>+11,.0f}  {pf_str(s['pf'])}")

        print("\n  [7月 DD分析]")
        for yr in sorted(t1_jul["signal_year"].unique()):
            grp = t1_jul[t1_jul["signal_year"] == yr].copy()
            grp = grp.sort_values("signal_dt")

            grp["cum"]  = grp["pnl_yen"].cumsum()
            grp["peak"] = grp["cum"].cummax()
            grp["dd"]   = grp["cum"] - grp["peak"]

            max_dd  = grp["dd"].min()
            dd_idx  = grp["dd"].idxmin()
            dd_time = grp.loc[dd_idx, "signal_dt"].strftime("%Y-%m-%d %H:%M")

            loss    = grp["pnl_yen"] < 0
            streak  = (loss != loss.shift()).cumsum()
            max_losing_streak = int(loss.groupby(streak).sum().max()) if loss.any() else 0

            print(f"  {yr}  最大DD: {max_dd:,.0f}円  発生日: {dd_time}  最大連敗: {max_losing_streak}")


    print_scenario_header("【除外月分析】系統③ 除外月（5/7/11月）の成績")

    # 系統③用：除外月なし（5/7/11月をBTに含める）で再BT
    trades_all3 = run_backtest(df, cpi, s1_excl_months=(), s3_excl_months=())
    t3_excl = trades_all3[
        (trades_all3["system"] == "③") &
        (trades_all3["signal_month"].isin([5, 7, 11]))
    ].copy()

    # 月別
    print("\n  [月別]")
    print(f"  {'月':>4}  {'件数':>5}  {'勝率%':>6}  {'損益(円)':>11}  {'PF':>6}")
    print("  " + "-" * 45)
    for mo in [5, 7, 11]:
        grp = t3_excl[t3_excl["signal_month"] == mo]
        s = calc_summary(grp)
        print(f"  {mo:>3}月  {s['n']:>5}  {s['win_rate']:>5.1f}%  {s['pnl_yen']:>+11,.0f}  {pf_str(s['pf'])}")

    # 曜日別
    print("\n  [曜日別]")
    print(f"  {'曜日':>4}  {'件数':>5}  {'勝率%':>6}  {'損益(円)':>11}  {'PF':>6}")
    print("  " + "-" * 45)
    for wd in [0, 2, 3, 4]:
        grp = t3_excl[t3_excl["signal_weekday"] == wd]
        s = calc_summary(grp)
        print(f"  {day_names[wd]}  {s['n']:>5}  {s['win_rate']:>5.1f}%  {s['pnl_yen']:>+11,.0f}  {pf_str(s['pf'])}")

    # 時間帯別
    print("\n  [時間帯別]")
    print(f"  {'時間':>4}  {'件数':>5}  {'勝率%':>6}  {'損益(円)':>11}  {'PF':>6}")
    print("  " + "-" * 45)
    for hr in sorted(t3_excl["signal_hour"].unique()):
        grp = t3_excl[t3_excl["signal_hour"] == hr]
        s = calc_summary(grp)
        print(f"  {hr:>3}時  {s['n']:>5}  {s['win_rate']:>5.1f}%  {s['pnl_yen']:>+11,.0f}  {pf_str(s['pf'])}")

    # 年別
    print("\n  [年別]")
    print(f"  {'年':>4}  {'件数':>5}  {'勝率%':>6}  {'損益(円)':>11}  {'PF':>6}")
    print("  " + "-" * 45)
    for yr in sorted(t3_excl["signal_year"].unique()):
        grp = t3_excl[t3_excl["signal_year"] == yr]
        s = calc_summary(grp)
        print(f"  {yr}  {s['n']:>5}  {s['win_rate']:>5.1f}%  {s['pnl_yen']:>+11,.0f}  {pf_str(s['pf'])}")

    # 7月 年別詳細
    t3_jul = t3_excl[t3_excl["signal_month"] == 7]
    if not t3_jul.empty:
        print("\n  [7月 年別詳細]")
        print(f"  {'年':>4}  {'件数':>5}  {'勝率%':>6}  {'損益(円)':>11}  {'PF':>6}")
        print("  " + "-" * 45)
        for yr in sorted(t3_jul["signal_year"].unique()):
            grp = t3_jul[t3_jul["signal_year"] == yr]
            s = calc_summary(grp)
            print(f"  {yr}  {s['n']:>5}  {s['win_rate']:>5.1f}%  {s['pnl_yen']:>+11,.0f}  {pf_str(s['pf'])}")

    # =========================
    # 系統① DST影響分析
    # =========================
    # 通常月（除外月以外）の系統① を trades（シナリオA: s1_excl=(3,5,7,11)）から取得
    t1_normal = trades[
        (trades["system"] == "①") &
        (~trades["signal_month"].isin([3, 5, 7, 11]))
    ].copy()
    t1_normal = _add_dst_col(t1_normal)
    t1_normal_dst = t1_normal[t1_normal["is_dst"]]
    t1_normal_win = t1_normal[~t1_normal["is_dst"]]

    print_scenario_header("【DST影響分析】系統① 通常月（3/5/7/11月除外） DST vs 冬時間")
    _print_dst_hour_table(t1_normal_dst, t1_normal_win)

    # 7月のみ（trades_all = s1_excl=() で取得済み）
    t1_jul_dst_ana = _add_dst_col(t1_jul)
    t1_jul_dst = t1_jul_dst_ana[t1_jul_dst_ana["is_dst"]]
    t1_jul_win = t1_jul_dst_ana[~t1_jul_dst_ana["is_dst"]]

    print_scenario_header("【DST影響分析】系統① 7月のみ DST vs 冬時間")
    _print_dst_hour_table(t1_jul_dst, t1_jul_win)

    # =========================
    # 系統① DST/冬時間別 推奨時間帯まとめ
    # =========================
    print_scenario_header("【時間帯最適化】系統① 推奨時間帯（PF>=1.0 を○、<1.0 を✗、件数0を-）")

    S1_ALL_HOURS = (8, 12, 15, 18, 19, 20, 21, 23)
    DAY_H   = (8, 12, 15)
    NIGHT_H = (18, 19, 20, 21, 23)

    def _hour_rec(dst_df, win_df, hours, section_label):
        print(f"\n  {section_label}")
        print(
            f"  {'時間':>4}  "
            f"{'DST件数':>7}  {'DST勝率%':>8}  {'DST損益(円)':>12}  {'DST PF':>7}  {'DST':>4}  │  "
            f"{'冬件数':>6}  {'冬勝率%':>7}  {'冬損益(円)':>12}  {'冬PF':>6}  {'冬':>4}"
        )
        print("  " + "-" * 100)
        for hr in hours:
            d  = dst_df[dst_df["signal_hour"] == hr]
            w  = win_df[win_df["signal_hour"] == hr]
            sd = calc_summary(d)
            sw = calc_summary(w)
            d_mark = "-" if sd["n"] == 0 else ("○" if sd["pf"] >= 1.0 else "✗")
            w_mark = "-" if sw["n"] == 0 else ("○" if sw["pf"] >= 1.0 else "✗")
            print(
                f"  {hr:>3}時  "
                f"{sd['n']:>7}  {sd['win_rate']:>7.1f}%  {sd['pnl_yen']:>+12,.0f}  {pf_str(sd['pf']):>7}  {d_mark:>4}  │  "
                f"{sw['n']:>6}  {sw['win_rate']:>6.1f}%  {sw['pnl_yen']:>+12,.0f}  {pf_str(sw['pf']):>6}  {w_mark:>4}"
            )

    print("\n■ 通常月（3/5/7/11月除外）")
    _hour_rec(t1_normal_dst, t1_normal_win, DAY_H,   "─日中─")
    _hour_rec(t1_normal_dst, t1_normal_win, NIGHT_H, "─夜間─")

    print("\n■ 7月")
    _hour_rec(t1_jul_dst, t1_jul_win, DAY_H,   "─日中─")
    _hour_rec(t1_jul_dst, t1_jul_win, NIGHT_H, "─夜間─")

    # 推奨時間帯サマリー
    print("\n■ 推奨時間帯サマリー（通常月ベース PF>=1.0）")
    dst_keep = [hr for hr in S1_ALL_HOURS
                if calc_summary(t1_normal_dst[t1_normal_dst["signal_hour"] == hr])["pf"] >= 1.0
                and calc_summary(t1_normal_dst[t1_normal_dst["signal_hour"] == hr])["n"] > 0]
    win_keep = [hr for hr in S1_ALL_HOURS
                if calc_summary(t1_normal_win[t1_normal_win["signal_hour"] == hr])["pf"] >= 1.0
                and calc_summary(t1_normal_win[t1_normal_win["signal_hour"] == hr])["n"] > 0]
    print(f"  DST期間 推奨時間帯: {dst_keep}")
    print(f"  冬時間  推奨時間帯: {win_keep}")
    common = sorted(set(dst_keep) & set(win_keep))
    dst_only = sorted(set(dst_keep) - set(win_keep))
    win_only = sorted(set(win_keep) - set(dst_keep))
    print(f"  共通（両期間○）: {common}")
    print(f"  DSTのみ○:       {dst_only}")
    print(f"  冬時間のみ○:    {win_only}")

    # =========================
    # 系統① DST版 vs 現行 比較
    # =========================
    # DST版時間帯定義
    S1_DST_HOURS_NEW = [8, 15, 18, 19, 20, 21]
    S1_WIN_HOURS_NEW = [8, 12, 15, 18, 20, 21, 23]

    # trades_all（除外月なし）の system① に DST フラグを付与
    t1_all = trades_all[trades_all["system"] == "①"].copy()
    t1_all = _add_dst_col(t1_all)

    # DST版: DST期間は S1_DST_HOURS_NEW、冬時間は S1_WIN_HOURS_NEW でフィルタ
    # さらに除外月（3,5,7,11）を適用して現行と比較条件を揃える
    t1_dst_ver = t1_all[
        (
            (t1_all["is_dst"]  & t1_all["signal_hour"].isin(S1_DST_HOURS_NEW)) |
            (~t1_all["is_dst"] & t1_all["signal_hour"].isin(S1_WIN_HOURS_NEW))
        ) &
        (~t1_all["signal_month"].isin([3, 5, 7, 11]))
    ].copy()

    # 現行: trades（シナリオA: s1_excl=(3,5,7,11)）の system①
    t1_current = trades[trades["system"] == "①"].copy()

    s_cur = calc_summary(t1_current)
    s_dst = calc_summary(t1_dst_ver)

    # ③ 7月除外なしDST版: excl=(3,5,11)、t1_all は除外月なし（7月含む）
    t1_no7_dst_ver = t1_all[
        (
            (t1_all["is_dst"]  & t1_all["signal_hour"].isin(S1_DST_HOURS_NEW)) |
            (~t1_all["is_dst"] & t1_all["signal_hour"].isin(S1_WIN_HOURS_NEW))
        ) &
        (~t1_all["signal_month"].isin([3, 5, 11]))
    ].copy()

    s_cur = calc_summary(t1_current)
    s_dst = calc_summary(t1_dst_ver)
    s_no7 = calc_summary(t1_no7_dst_ver)

    print_scenario_header("【系統① 比較】現行 / DST版 / 7月除外なしDST版")

    # --- 全体比較 ---
    print(f"\n  {'':12}  {'件数':>6}  {'勝率%':>6}  {'損益(円)':>12}  {'PF':>7}")
    print("  " + "-" * 55)
    print(f"  {'①現行':12}  {s_cur['n']:>6}  {s_cur['win_rate']:>5.1f}%  {s_cur['pnl_yen']:>+12,.0f}  {pf_str(s_cur['pf']):>7}")
    print(f"  {'②DST版':12}  {s_dst['n']:>6}  {s_dst['win_rate']:>5.1f}%  {s_dst['pnl_yen']:>+12,.0f}  {pf_str(s_dst['pf']):>7}")
    d2n  = s_dst['n'] - s_cur['n']
    d2p  = s_dst['pnl_yen'] - s_cur['pnl_yen']
    d2pf = round(s_dst['pf'] - s_cur['pf'], 3)
    print(f"  {'  差分(②-①)':12}  {d2n:>+6}  {'---':>6}   {d2p:>+12,.0f}  {d2pf:>+7.3f}")
    print(f"  {'③7月込みDST':12}  {s_no7['n']:>6}  {s_no7['win_rate']:>5.1f}%  {s_no7['pnl_yen']:>+12,.0f}  {pf_str(s_no7['pf']):>7}")
    d3n  = s_no7['n'] - s_cur['n']
    d3p  = s_no7['pnl_yen'] - s_cur['pnl_yen']
    d3pf = round(s_no7['pf'] - s_cur['pf'], 3)
    print(f"  {'  差分(③-①)':12}  {d3n:>+6}  {'---':>6}   {d3p:>+12,.0f}  {d3pf:>+7.3f}")

    print(f"\n  時間帯定義: DST={S1_DST_HOURS_NEW}  冬={S1_WIN_HOURS_NEW}")

    # --- 年別成績 ---
    print("\n  [年別成績]")
    print(f"  {'年':>4}  {'現行損益':>11}  {'現行PF':>7}  │  {'DST版損益':>11}  {'DST版PF':>8}  │  {'7月込み損益':>11}  {'7月込みPF':>9}")
    print("  " + "-" * 85)
    all_years = sorted(
        set(t1_current["signal_year"].unique()) |
        set(t1_dst_ver["signal_year"].unique()) |
        set(t1_no7_dst_ver["signal_year"].unique())
    )
    for yr in all_years:
        sc = calc_summary(t1_current[t1_current["signal_year"] == yr])
        sd = calc_summary(t1_dst_ver[t1_dst_ver["signal_year"] == yr])
        sn = calc_summary(t1_no7_dst_ver[t1_no7_dst_ver["signal_year"] == yr])
        print(
            f"  {yr}  {sc['pnl_yen']:>+11,.0f}  {pf_str(sc['pf']):>7}  │"
            f"  {sd['pnl_yen']:>+11,.0f}  {pf_str(sd['pf']):>8}  │"
            f"  {sn['pnl_yen']:>+11,.0f}  {pf_str(sn['pf']):>9}"
        )

    # --- 7月のみ ---
    t1c_jul = t1_current[t1_current["signal_month"] == 7]
    t1d_jul = t1_dst_ver[t1_dst_ver["signal_month"] == 7]
    t1n_jul = t1_no7_dst_ver[t1_no7_dst_ver["signal_month"] == 7]
    print("\n  [7月のみ]")
    print(f"  {'':12}  {'件数':>6}  {'勝率%':>6}  {'損益(円)':>12}  {'PF':>7}")
    print("  " + "-" * 55)
    for lbl, grp in [("①現行", t1c_jul), ("②DST版", t1d_jul), ("③7月込みDST", t1n_jul)]:
        s = calc_summary(grp)
        print(f"  {lbl:12}  {s['n']:>6}  {s['win_rate']:>5.1f}%  {s['pnl_yen']:>+12,.0f}  {pf_str(s['pf']):>7}")

    # ④⑤ ③7月込みDST版 に月次DD制限を適用
    print_scenario_header("【系統① 月次DD制限比較】③7月込みDST版 へのDD制限適用")

    for lim_label, lim in [("-30,000円", -30_000), ("-40,000円", -40_000)]:
        res     = sim_monthly_dd(t1_no7_dst_ver, lim)
        active  = res["active"]
        skipped = res["skipped"]
        months_triggered = res["months_triggered"]
        s_dd    = calc_summary(active)

        # 発動月リストを再計算（sim_monthly_dd は triggered set を返さないため）
        df_tmp = t1_no7_dst_ver.sort_values("signal_dt").copy()
        df_tmp["ym"] = list(zip(df_tmp["signal_year"], df_tmp["signal_month"]))
        month_pnl_tmp  = {}
        triggered_months = []
        triggered_set  = set()
        for _, row in df_tmp.iterrows():
            ym = row["ym"]
            if ym not in month_pnl_tmp:
                month_pnl_tmp[ym] = 0.0
            if ym in triggered_set:
                continue
            month_pnl_tmp[ym] += row["pnl_yen"]
            if month_pnl_tmp[ym] <= lim:
                triggered_set.add(ym)
                triggered_months.append(ym)

        jul_triggered = any(m == 7 for _, m in triggered_months)

        print(f"\n  ③ + 月次DD {lim_label}")
        print(f"  {'':12}  {'件数':>6}  {'勝率%':>6}  {'損益(円)':>12}  {'PF':>7}")
        print("  " + "-" * 55)
        print(f"  {'③ベース':12}  {s_no7['n']:>6}  {s_no7['win_rate']:>5.1f}%  {s_no7['pnl_yen']:>+12,.0f}  {pf_str(s_no7['pf']):>7}")
        print(f"  {'DD制限後':12}  {s_dd['n']:>6}  {s_dd['win_rate']:>5.1f}%  {s_dd['pnl_yen']:>+12,.0f}  {pf_str(s_dd['pf']):>7}")
        print(f"  スキップ: {skipped}件  発動月: {months_triggered}ヶ月  7月発動: {'あり' if jul_triggered else 'なし'}")
        print(f"  発動月リスト: {sorted(triggered_months)}")

    # =========================
    # 系統① 曜日 × DST/冬時間 分析
    # =========================
    # 曜日分析用: 月〜金全曜日（既存シナリオへの影響ゼロ）
    trades_all_5wd = run_backtest(
        df,
        cpi,
        s1_excl_months=(),
        s1_weekdays=(0,1,2,3,4),
        s1_hours=tuple(range(24)),
        s3_hours=tuple(range(24)),
    )

    t1_all_5wd = trades_all_5wd[trades_all_5wd["system"] == "①"].copy()
    t1_all_5wd = _add_dst_col(t1_all_5wd)

    # =========================
    # 系統① 時間 × 曜日 PF表（DST / 冬時間）
    # =========================
    print_scenario_header("【系統① 時間×曜日 PF表】除外月(3,5,11) 月〜金 / DST・冬時間別")

    t1_hw = t1_all_5wd[~t1_all_5wd["signal_month"].isin([3, 5, 11])].copy()
    t1_hw_dst = t1_hw[t1_hw["is_dst"]].copy()
    t1_hw_win = t1_hw[~t1_hw["is_dst"]].copy()

    print("\n[DST期間]")
    print(f"  {'時':>3}    月           火           水           木           金")
    print("  ---------------------------------------------------------------")
    for hr in range(24):
        row = f"{hr:>3}  "
        for wd in range(5):
            d = t1_hw_dst[(t1_hw_dst["signal_weekday"]==wd) & (t1_hw_dst["signal_hour"]==hr)]
            s = calc_summary(d)
            cell = "   ---    " if s["n"] == 0 else f"{s['pf']:.3f}({s['n']}){'○' if s['pf'] >= 1.0 else '✗'}"
            row += f"{cell:<11}"
        print("  " + row)

    print("\n[冬時間]")
    print(f"  {'時':>3}    月           火           水           木           金")
    print("  ---------------------------------------------------------------")
    for hr in range(24):
        row = f"{hr:>3}  "
        for wd in range(5):
            w = t1_hw_win[(t1_hw_win["signal_weekday"]==wd) & (t1_hw_win["signal_hour"]==hr)]
            s = calc_summary(w)
            cell = "   ---    " if s["n"] == 0 else f"{s['pf']:.3f}({s['n']}){'○' if s['pf'] >= 1.0 else '✗'}"
            row += f"{cell:<11}"
        print("  " + row)

    # =========================
    # 系統③ 時間×曜日 PF表
    # =========================
    print_scenario_header("【系統③ 時間×曜日 PF表】除外月(5,7,11) 月水木金 / DST・冬時間別")

    t3_hw = trades_all_5wd[trades_all_5wd["system"] == "③"].copy()
    t3_hw = _add_dst_col(t3_hw)

    t3_hw = t3_hw[~t3_hw["signal_month"].isin([5,7,11])]

    t3_dst = t3_hw[t3_hw["is_dst"]]
    t3_win = t3_hw[~t3_hw["is_dst"]]

    # =========================
    # 系統③ 全時間帯分析
    # =========================
    print_scenario_header("【系統③ 全時間帯分析】除外月(5,7,11) 月水木金 / 0〜23時")

    t3_all = trades_all_5wd[trades_all_5wd["system"] == "③"].copy()
    t3_all = _add_dst_col(t3_all)
    t3_all = t3_all[~t3_all["signal_month"].isin([5, 7, 11])]

    t3_dst_all = t3_all[t3_all["is_dst"]]
    t3_win_all = t3_all[~t3_all["is_dst"]]

    _ah_hdr = (
        f"  {'時':>3}  "
        f"{'件数(DST)':>9}  {'勝率%':>6}  {'損益(円)':>11}  {'PF':>7}  {'判定':>4}  │  "
        f"{'件数(冬)':>8}  {'勝率%':>6}  {'損益(円)':>11}  {'PF':>7}  {'判定':>4}"
    )
    print(_ah_hdr)
    print("  " + "-" * 92)

    for hr in range(24):
        d = t3_dst_all[t3_dst_all["signal_hour"] == hr]
        w = t3_win_all[t3_win_all["signal_hour"] == hr]
        sd = calc_summary(d)
        sw = calc_summary(w)

        md = "-" if sd["n"] == 0 else ("○" if sd["pf"] >= 1.0 else "✗")
        mw = "-" if sw["n"] == 0 else ("○" if sw["pf"] >= 1.0 else "✗")

        print(
            f"  {hr:>3}  "
            f"{sd['n']:>9}  {sd['win_rate']:>5.1f}%  {sd['pnl_yen']:>+11,.0f}  {pf_str(sd['pf']):>7}  {md:>4}  │  "
            f"{sw['n']:>8}  {sw['win_rate']:>5.1f}%  {sw['pnl_yen']:>+11,.0f}  {pf_str(sw['pf']):>7}  {mw:>4}"
        )

    print("\n[系統③ DST期間]")
    print(f"  {'時':>3}    月           火           水           木           金")
    print("  ---------------------------------------------------------------")

    for hr in range(24):
        row = f"{hr:>3}  "

        for wd in range(5):
            d = t3_dst[(t3_dst["signal_weekday"]==wd) & (t3_dst["signal_hour"]==hr)]
            s = calc_summary(d)

            if s["n"] == 0:
                cell = "   ---    "
            else:
                mark = "○" if s["pf"] >= 1.0 else "✗"
                cell = f"{s['pf']:.3f}({s['n']}){mark}"

            row += f"{cell:<11}"

        print("  " + row)

    print("\n[系統③ 冬時間]")
    print(f"  {'時':>3}    月           火           水           木           金")
    print("  ---------------------------------------------------------------")

    for hr in range(24):
        row = f"{hr:>3}  "

        for wd in range(5):
            w = t3_win[(t3_win["signal_weekday"]==wd) & (t3_win["signal_hour"]==hr)]
            s = calc_summary(w)

            if s["n"] == 0:
                cell = "   ---    "
            else:
                mark = "○" if s["pf"] >= 1.0 else "✗"
                cell = f"{s['pf']:.3f}({s['n']}){mark}"

            row += f"{cell:<11}"

        print("  " + row)

    t1_wd_analysis = t1_all_5wd[
        (
            (t1_all_5wd["is_dst"]  & t1_all_5wd["signal_hour"].isin(S1_DST_HOURS_NEW)) |
            (~t1_all_5wd["is_dst"] & t1_all_5wd["signal_hour"].isin(S1_WIN_HOURS_NEW))
        ) &
        (~t1_all_5wd["signal_month"].isin([3, 5, 11]))
    ].copy()

    print_scenario_header("【系統① 曜日 × DST/冬時間分析】③7月込みDST版 除外月(3,5,11)")

    _day_names = {0: "月曜", 1: "火曜", 2: "水曜", 3: "木曜", 4: "金曜"}
    t1_wd_dst_s = t1_wd_analysis[t1_wd_analysis["is_dst"]]
    t1_wd_win_s = t1_wd_analysis[~t1_wd_analysis["is_dst"]]

    _wd_header = (
        f"  {'曜日':>4}  "
        f"{'件数(DST)':>9}  {'勝率%':>6}  {'損益(円)':>11}  {'PF':>7}  {'判定':>4}  │  "
        f"{'件数(冬)':>8}  {'勝率%':>6}  {'損益(円)':>11}  {'PF':>7}  {'判定':>4}"
    )
    print(_wd_header)
    print("  " + "-" * 92)
    for wd in range(5):
        d  = t1_wd_dst_s[t1_wd_dst_s["signal_weekday"] == wd]
        w  = t1_wd_win_s[t1_wd_win_s["signal_weekday"] == wd]
        sd = calc_summary(d)
        sw = calc_summary(w)
        d_mark = "-" if sd["n"] == 0 else ("○" if sd["pf"] >= 1.0 else "✗")
        w_mark = "-" if sw["n"] == 0 else ("○" if sw["pf"] >= 1.0 else "✗")
        print(
            f"  {_day_names[wd]:>4}  "
            f"{sd['n']:>9}  {sd['win_rate']:>5.1f}%  {sd['pnl_yen']:>+11,.0f}  {pf_str(sd['pf']):>7}  {d_mark:>4}  │  "
            f"{sw['n']:>8}  {sw['win_rate']:>5.1f}%  {sw['pnl_yen']:>+11,.0f}  {pf_str(sw['pf']):>7}  {w_mark:>4}"
        )

    dst_wd_keep = [wd for wd in range(5)
                   if calc_summary(t1_wd_dst_s[t1_wd_dst_s["signal_weekday"] == wd])["pf"] >= 1.0
                   and calc_summary(t1_wd_dst_s[t1_wd_dst_s["signal_weekday"] == wd])["n"] > 0]
    win_wd_keep = [wd for wd in range(5)
                   if calc_summary(t1_wd_win_s[t1_wd_win_s["signal_weekday"] == wd])["pf"] >= 1.0
                   and calc_summary(t1_wd_win_s[t1_wd_win_s["signal_weekday"] == wd])["n"] > 0]
    common_wd   = sorted(set(dst_wd_keep) & set(win_wd_keep))
    dst_only_wd = sorted(set(dst_wd_keep) - set(win_wd_keep))
    win_only_wd = sorted(set(win_wd_keep) - set(dst_wd_keep))

    print(f"\n  [曜日サマリー]")
    print(f"  DST期間 推奨曜日: {[_day_names[w] for w in dst_wd_keep]}")
    print(f"  冬時間  推奨曜日: {[_day_names[w] for w in win_wd_keep]}")
    print(f"  共通（両期間○）: {[_day_names[w] for w in common_wd]}")
    print(f"  DSTのみ○:       {[_day_names[w] for w in dst_only_wd]}")
    print(f"  冬時間のみ○:    {[_day_names[w] for w in win_only_wd]}")


    # =========================
    # 系統① 22時 × 曜日 分析（DST/冬時間別）
    # =========================
    print_scenario_header("【系統① 22時 × 曜日分析】③7月込みDST版 除外月(3,5,11)")

    t1_22_dst = t1_wd_dst_s[t1_wd_dst_s["signal_hour"] == 22].copy()
    t1_22_win = t1_wd_win_s[t1_wd_win_s["signal_hour"] == 22].copy()

    _h22_header = (
        f"  {'曜日':>4}  "
        f"{'件数(DST)':>9}  {'勝率%':>6}  {'損益(円)':>11}  {'PF':>7}  {'判定':>4}  │  "
        f"{'件数(冬)':>8}  {'勝率%':>6}  {'損益(円)':>11}  {'PF':>7}  {'判定':>4}"
    )
    print(_h22_header)
    print("  " + "-" * 92)

    dst_keep_days = []
    win_keep_days = []

    for wd in range(5):
        d  = t1_22_dst[t1_22_dst["signal_weekday"] == wd]
        w  = t1_22_win[t1_22_win["signal_weekday"] == wd]
        sd = calc_summary(d)
        sw = calc_summary(w)

        d_mark = "-" if sd["n"] == 0 else ("○" if sd["pf"] >= 1.0 else "✗")
        w_mark = "-" if sw["n"] == 0 else ("○" if sw["pf"] >= 1.0 else "✗")

        if sd["n"] > 0 and sd["pf"] >= 1.0:
            dst_keep_days.append(_day_names[wd])
        if sw["n"] > 0 and sw["pf"] >= 1.0:
            win_keep_days.append(_day_names[wd])

        print(
            f"  {_day_names[wd]:>4}  "
            f"{sd['n']:>9}  {sd['win_rate']:>5.1f}%  {sd['pnl_yen']:>+11,.0f}  {pf_str(sd['pf']):>7}  {d_mark:>4}  │  "
            f"{sw['n']:>8}  {sw['win_rate']:>5.1f}%  {sw['pnl_yen']:>+11,.0f}  {pf_str(sw['pf']):>7}  {w_mark:>4}"
        )

    print("\n  [22時サマリー]")
    print(f"  DST期間 推奨曜日: {dst_keep_days}")
    print(f"  冬時間  推奨曜日: {win_keep_days}")
    print(f"  共通（両期間○）: {sorted(set(dst_keep_days) & set(win_keep_days))}")
    print(f"  DSTのみ○:       {sorted(set(dst_keep_days) - set(win_keep_days))}")
    print(f"  冬時間のみ○:    {sorted(set(win_keep_days) - set(dst_keep_days))}")

    # =========================
    # 系統① 指標除外効果確認
    # =========================
    print_scenario_header("【系統① 指標除外効果確認】発表前30分〜後60分 (現行除外月設定)")

    _t1_base = trades[trades["system"] == "①"].copy()
    _s1_base = calc_summary(_t1_base)

    _ind_targets = [
        ("米CPI",        load_indicator("米CPI")),
        ("米PPI",        load_indicator("米PPI")),
        ("米ISM製造業",   load_indicator("米ISM製造業")),
        ("米ISM非製造業", load_indicator("米ISM非製造業")),
    ]

    _ind_hdr = f"  {'パターン':>18}  {'件数':>5}  {'勝率%':>6}  {'損益(円)':>11}  {'PF':>7}  {'除外件数':>6}"
    print(_ind_hdr)
    print("  " + "-" * 68)
    print(f"  {'指標なし(ベース)':>18}  {_s1_base['n']:>5}  {_s1_base['win_rate']:>5.1f}%  "
          f"{_s1_base['pnl_yen']:>+11,.0f}  {pf_str(_s1_base['pf']):>7}  {'—':>6}")

    _all_rels = []
    for _ind_name, _rels in _ind_targets:
        if _rels.empty:
            print(f"  {_ind_name+'除外':>18}  (データなし)")
            continue
        _all_rels.append(_rels)
        _t1_f = _excl_indicator_s1(_t1_base, _rels)
        _sf   = calc_summary(_t1_f)
        _excl_n = _s1_base["n"] - _sf["n"]
        print(f"  {_ind_name+'除外':>18}  {_sf['n']:>5}  {_sf['win_rate']:>5.1f}%  "
              f"{_sf['pnl_yen']:>+11,.0f}  {pf_str(_sf['pf']):>7}  {_excl_n:>5}件")

    if _all_rels:
        _t1_all = _t1_base.copy()
        for _rels in _all_rels:
            _t1_all = _excl_indicator_s1(_t1_all, _rels)
        _s_all  = calc_summary(_t1_all)
        _excl_all = _s1_base["n"] - _s_all["n"]
        print(f"  {'全指標除外':>18}  {_s_all['n']:>5}  {_s_all['win_rate']:>5.1f}%  "
              f"{_s_all['pnl_yen']:>+11,.0f}  {pf_str(_s_all['pf']):>7}  {_excl_all:>5}件")

    # =========================
    # 系統① 全時間帯 × DST/冬時間分析
    # =========================
    print_scenario_header("【系統① 全時間帯分析】除外月(3,5,11) 月〜金 / 0〜23時")
    trades_all_hrs = run_backtest(
        df,
        cpi,
        s1_excl_months=(3,5,11),
        s1_weekdays=(0,1,2,3,4),
        s1_hours=tuple(range(24)),
    )
    

    t1_ah = trades_all_hrs[trades_all_hrs["system"] == "①"].copy()
    t1_ah = _add_dst_col(t1_ah)
    _ah_dst = t1_ah[t1_ah["is_dst"]]
    _ah_win = t1_ah[~t1_ah["is_dst"]]

    _ah_hdr = (
        f"  {'時':>3}  "
        f"{'件数(DST)':>9}  {'勝率%':>6}  {'損益(円)':>11}  {'PF':>7}  {'判定':>4}  │  "
        f"{'件数(冬)':>8}  {'勝率%':>6}  {'損益(円)':>11}  {'PF':>7}  {'判定':>4}"
    )
    print(_ah_hdr)
    print("  " + "-" * 92)
    for _hr in range(24):
        _d = _ah_dst[_ah_dst["signal_hour"] == _hr]
        _w = _ah_win[_ah_win["signal_hour"] == _hr]
        _sd = calc_summary(_d)
        _sw = calc_summary(_w)
        if _sd["n"] == 0 and _sw["n"] == 0:
            continue
        _dm = "-" if _sd["n"] == 0 else ("○" if _sd["pf"] >= 1.0 else "✗")
        _wm = "-" if _sw["n"] == 0 else ("○" if _sw["pf"] >= 1.0 else "✗")
        print(
            f"  {_hr:>3}  "
            f"{_sd['n']:>9}  {_sd['win_rate']:>5.1f}%  {_sd['pnl_yen']:>+11,.0f}  {pf_str(_sd['pf']):>7}  {_dm:>4}  │  "
            f"{_sw['n']:>8}  {_sw['win_rate']:>5.1f}%  {_sw['pnl_yen']:>+11,.0f}  {pf_str(_sw['pf']):>7}  {_wm:>4}"
        )

    _dst_h_keep = [h for h in range(24)
                   if calc_summary(_ah_dst[_ah_dst["signal_hour"] == h])["pf"] >= 1.0
                   and calc_summary(_ah_dst[_ah_dst["signal_hour"] == h])["n"] > 0]
    _win_h_keep = [h for h in range(24)
                   if calc_summary(_ah_win[_ah_win["signal_hour"] == h])["pf"] >= 1.0
                   and calc_summary(_ah_win[_ah_win["signal_hour"] == h])["n"] > 0]
    print(f"\n  [時間帯サマリー]")
    print(f"  DST期間 推奨時間帯: {_dst_h_keep}")
    print(f"  冬時間  推奨時間帯: {_win_h_keep}")
    print(f"  共通（両期間○）: {sorted(set(_dst_h_keep) & set(_win_h_keep))}")

    # =========================
    # 祝日除外効果確認
    # =========================
    print_scenario_header("【祝日除外効果確認】祝日なし vs 祝日除外あり（系統①③）")

    trades_hol = run_backtest(df, cpi, s1_weekdays=(0,1,2,3,4), skip_holidays=True)
    t1_base = trades[trades["system"] == "①"]
    t3_base = trades[trades["system"] == "③"]
    t1_hol  = trades_hol[trades_hol["system"] == "①"]
    t3_hol  = trades_hol[trades_hol["system"] == "③"]

    print(f"\n  {'':16}  {'件数':>5}  {'勝率%':>6}  {'損益(円)':>11}  {'PF':>7}  {'除外件数':>6}")
    print("  " + "-" * 65)
    for sys_label, base_df, hol_df in [
        ("系統①", t1_base, t1_hol),
        ("系統③", t3_base, t3_hol),
    ]:
        sb = calc_summary(base_df)
        sh = calc_summary(hol_df)
        excl_n = sb["n"] - sh["n"]
        print(f"  {sys_label+'(祝日なし)':16}  {sb['n']:>5}  {sb['win_rate']:>5.1f}%  {sb['pnl_yen']:>+11,.0f}  {pf_str(sb['pf']):>7}  {'—':>6}")
        print(f"  {sys_label+'(祝日除外)':16}  {sh['n']:>5}  {sh['win_rate']:>5.1f}%  {sh['pnl_yen']:>+11,.0f}  {pf_str(sh['pf']):>7}  {excl_n:>5}件")
        dpf  = sh["pf"] - sb["pf"]
        dyen = sh["pnl_yen"] - sb["pnl_yen"]
        print(f"  {'  差分':16}  {-excl_n:>+5}  {'---':>6}   {dyen:>+11,.0f}  {dpf:>+7.3f}")
        print()

    # =========================
    # 除外月(3/5/11月) × 祝日除外 比較 + 時間×曜日 PF表
    # =========================
    print_scenario_header("【除外月 × 祝日除外 比較 + 時間×曜日 PF表】系統① 3/5/11月")

    def _apply_holiday_filter(df: pd.DataFrame) -> pd.DataFrame:
        sig_dts = pd.to_datetime(df["signal_dt"])
        sig_dates = sig_dts.dt.date.where(
            sig_dts.dt.hour >= 6,
            (sig_dts - pd.Timedelta(days=1)).dt.date,
        )
        keep = ~pd.Series(sig_dates.values, index=df.index).isin(HOLIDAYS)
        return df[keep].copy()

    for mo in [3, 5, 11]:
        t1_mo     = t1_excl[t1_excl["signal_month"] == mo].copy()
        t1_mo_hol = _apply_holiday_filter(t1_mo)

        sb     = calc_summary(t1_mo)
        sh     = calc_summary(t1_mo_hol)
        excl_n = sb["n"] - sh["n"]

        print(f"\n{'='*60}")
        print(f"  ── {mo}月 ──")
        print(f"{'='*60}")

        # ① 祝日あり vs 祝日除外（月全体）
        print(f"\n  [祝日除外 比較]")
        print(f"  {'':16}  {'件数':>5}  {'勝率%':>6}  {'損益(円)':>11}  {'PF':>7}  {'除外件数':>6}")
        print("  " + "-" * 65)
        print(f"  {'祝日あり':16}  {sb['n']:>5}  {sb['win_rate']:>5.1f}%  "
              f"{sb['pnl_yen']:>+11,.0f}  {pf_str(sb['pf']):>7}  {'—':>6}")
        print(f"  {'祝日除外':16}  {sh['n']:>5}  {sh['win_rate']:>5.1f}%  "
              f"{sh['pnl_yen']:>+11,.0f}  {pf_str(sh['pf']):>7}  {excl_n:>5}件")
        dpf  = sh["pf"] - sb["pf"]
        dyen = sh["pnl_yen"] - sb["pnl_yen"]
        print(f"  {'  差分':16}  {-excl_n:>+5}  {'---':>6}   {dyen:>+11,.0f}  {dpf:>+7.3f}")

        # ① 月次DD -30,000円 確認（祝日除外後）
        res_dd = sim_monthly_dd(t1_mo_hol, -30_000)
        s_dd   = calc_summary(res_dd["active"])
        df_tmp = t1_mo_hol.sort_values("signal_dt").copy()
        df_tmp["ym"] = list(zip(df_tmp["signal_year"], df_tmp["signal_month"]))
        _mp = {}; _tset = set(); _tlist = []
        for _, _row in df_tmp.iterrows():
            ym = _row["ym"]
            if ym not in _mp: _mp[ym] = 0.0
            if ym in _tset: continue
            _mp[ym] += _row["pnl_yen"]
            if _mp[ym] <= -30_000:
                _tset.add(ym); _tlist.append(ym)

        print(f"\n  [月次DD -30,000円（祝日除外後）]")
        print(f"  件数: {s_dd['n']}  損益: {s_dd['pnl_yen']:+,.0f}円  "
              f"PF: {pf_str(s_dd['pf'])}  発動月: {res_dd['months_triggered']}ヶ月")
        if _tlist:
            print(f"  発動月リスト: {sorted(_tlist)}")
        else:
            print(f"  発動月なし")

        # ② 時間×曜日 PF表（DST / 冬時間）
        t1_mo_ana = _add_dst_col(t1_mo)
        t1_mo_dst = t1_mo_ana[t1_mo_ana["is_dst"]]
        t1_mo_win = t1_mo_ana[~t1_mo_ana["is_dst"]]

        for period_label, period_df in [(f"{mo}月 DST期間", t1_mo_dst),
                                        (f"{mo}月 冬時間",  t1_mo_win)]:
            print(f"\n  [{period_label} 時間×曜日 PF表]")
            print(f"  {'時':>3}    月           火           水           木           金")
            print("  ---------------------------------------------------------------")
            for hr in range(24):
                row_str  = f"{hr:>3}  "
                has_data = False
                for wd in range(5):
                    grp = period_df[
                        (period_df["signal_weekday"] == wd) &
                        (period_df["signal_hour"]    == hr)
                    ]
                    s = calc_summary(grp)
                    if s["n"] > 0:
                        has_data = True
                    cell = ("   ---    " if s["n"] == 0
                            else f"{s['pf']:.3f}({s['n']}){'○' if s['pf'] >= 1.0 else '✗'}")
                    row_str += f"{cell:<11}"
                if has_data:
                    print("  " + row_str)

# =========================
    # 【除外月 前半/後半分析】系統①③
    # 各除外月を前半(1〜15日)/後半(16〜末日)に分けて検証
    # DD上限 -15,000円は除外月のみに適用（通常月は既存の-30,000円を使用）
    # trades_all  : 系統① s1_excl_months=() で再BT済み
    # trades_all3 : 系統③ s3_excl_months=() で再BT済み
    # =========================
    print_scenario_header("【除外月 前半/後半分析】系統①③ 全除外月の前半/後半分割検証")

    MAY_DD_LIMIT = -15_000  # 除外月のDD上限

    # 対象除外月の定義
    # 系統①：3月・5月・11月（7月は既に採用済みのため除外）
    # 系統③：5月・7月・11月
    S1_TARGET_MONTHS = [3, 5, 11]
    S3_TARGET_MONTHS = [5, 7, 11]

    def half_label(day):
        return "前半(1〜15日)" if day <= 15 else "後半(16〜末日)"

    def analyze_half(t_sys, sys_label, target_months):
        """除外月ごとに前半/後半分析を行い結果を出力する"""

        for mo in target_months:
            t_mo = t_sys[t_sys["signal_month"] == mo].copy()
            if len(t_mo) == 0:
                print(f"\n  [{sys_label}] {mo}月: データなし")
                continue

            t_mo["half"] = pd.to_datetime(t_mo["signal_dt"]).dt.day.map(
                lambda d: "前半(1〜15日)" if d <= 15 else "後半(16〜末日)"
            )

            print(f"\n{'='*72}")
            print(f"  [{sys_label}] {mo}月")
            print(f"{'='*72}")

            # ── 全体サマリー ──
            print(f"\n  ■ サマリー（手数料2.2pt込み）")
            print(f"  {'区分':>14}  {'件数':>5}  {'勝率%':>6}  {'損益(円)':>11}  {'PF':>7}")
            print("  " + "-" * 50)
            for half in ["前半(1〜15日)", "後半(16〜末日)", f"{mo}月全体"]:
                grp = t_mo if half == f"{mo}月全体" else t_mo[t_mo["half"] == half]
                s = calc_summary(grp)
                print(f"  {half:>14}  {s['n']:>5}  {s['win_rate']:>5.1f}%  "
                      f"{s['pnl_yen']:>+11,.0f}  {pf_str(s['pf'])}")

            # ── 年別 ──
            print(f"\n  ■ 年別損益(円)")
            print(f"  {'年':>4}  {'前半(1〜15日)':>16}  {'後半(16〜末日)':>16}  {'月合計':>10}")
            print("  " + "-" * 55)
            for yr in sorted(t_mo["signal_year"].unique()):
                g = t_mo[t_mo["signal_year"] == yr]
                gf = g[g["half"] == "前半(1〜15日)"]
                gb = g[g["half"] == "後半(16〜末日)"]
                sf = calc_summary(gf)
                sb = calc_summary(gb)
                st = calc_summary(g)
                print(f"  {yr}  "
                      f"{sf['pnl_yen']:>+11,.0f}({sf['n']}件)  "
                      f"{sb['pnl_yen']:>+11,.0f}({sb['n']}件)  "
                      f"{st['pnl_yen']:>+9,.0f}")
            # 合計
            gf_all = t_mo[t_mo["half"] == "前半(1〜15日)"]
            gb_all = t_mo[t_mo["half"] == "後半(16〜末日)"]
            print("  " + "-" * 55)
            print(f"  {'合計':>4}  "
                  f"{calc_summary(gf_all)['pnl_yen']:>+11,.0f}({len(gf_all)}件)  "
                  f"{calc_summary(gb_all)['pnl_yen']:>+11,.0f}({len(gb_all)}件)  "
                  f"{calc_summary(t_mo)['pnl_yen']:>+9,.0f}")

            # ── DD上限-15,000円シミュレーション ──
            print(f"\n  ■ DD上限 {MAY_DD_LIMIT:+,}円 適用")
            print(f"  {'区分':>14}  {'件数':>5}  {'スキップ':>7}  {'損益(円)':>11}  {'PF':>7}")
            print("  " + "-" * 54)
            for half in ["前半(1〜15日)", "後半(16〜末日)"]:
                grp = t_mo[t_mo["half"] == half].copy()
                if len(grp) == 0:
                    continue
                res = sim_monthly_dd(grp, MAY_DD_LIMIT)
                s = calc_summary(res["active"])
                print(f"  {half:>14}  {s['n']:>5}  {res['skipped']:>7}  "
                      f"{s['pnl_yen']:>+11,.0f}  {pf_str(s['pf'])}")

            # ── 安定性評価 ──
            print(f"\n  ■ 安定性・採用判断（PF>=1.2 かつ全年プラス）")
            for half in ["前半(1〜15日)", "後半(16〜末日)"]:
                grp = t_mo[t_mo["half"] == half]
                s = calc_summary(grp)
                neg_yrs = [
                    yr for yr in sorted(grp["signal_year"].unique())
                    if calc_summary(grp[grp["signal_year"] == yr])["pnl_yen"] < 0
                ]
                ok_pf     = s["pf"] >= 1.2
                ok_stable = len(neg_yrs) == 0
                verdict   = "✅ 採用候補" if (ok_pf and ok_stable) else "❌ 見送り"
                stable_str = "✅ 全年プラス" if ok_stable else f"❌ マイナス年: {[int(y) for y in neg_yrs]}"
                print(f"  {half}: PF={pf_str(s['pf'])}  {stable_str}  → {verdict}")

    # ── 系統① 分析 ──
    print_scenario_header("系統① 除外月 前半/後半分析")
    t1_all_excl = trades_all[trades_all["system"] == "①"].copy()
    analyze_half(t1_all_excl, "系統①", S1_TARGET_MONTHS)

    # ── 系統③ 分析 ──
    print_scenario_header("系統③ 除外月 前半/後半分析")
    t3_all_excl = trades_all3[trades_all3["system"] == "③"].copy()
    analyze_half(t3_all_excl, "系統③", S3_TARGET_MONTHS)

    # ── 系統①③ 合算 分析 ──
    print_scenario_header("系統①③ 合算 除外月 前半/後半分析")

    # 対象月は①③の和集合
    ALL_TARGET_MONTHS = sorted(set(S1_TARGET_MONTHS) | set(S3_TARGET_MONTHS))

    for mo in ALL_TARGET_MONTHS:
        # ①は対象月のみ、③は対象月のみ抽出（それぞれの除外月定義に従う）
        t1_mo = t1_all_excl[t1_all_excl["signal_month"] == mo].copy() \
                if mo in S1_TARGET_MONTHS else pd.DataFrame()
        t3_mo = t3_all_excl[t3_all_excl["signal_month"] == mo].copy() \
                if mo in S3_TARGET_MONTHS else pd.DataFrame()

        # half列追加
        for t in [t1_mo, t3_mo]:
            if len(t) > 0:
                t["half"] = pd.to_datetime(t["signal_dt"]).dt.day.map(
                    lambda d: "前半(1〜15日)" if d <= 15 else "後半(16〜末日)"
                )

        # 合算用に結合
        t_combined = pd.concat([t1_mo, t3_mo], ignore_index=True)
        if len(t_combined) == 0:
            continue

        print(f"\n{'='*72}")
        print(f"  [①③合算] {mo}月")
        print(f"{'='*72}")

        # ── 合算サマリー ──
        print(f"\n  ■ 合算サマリー（手数料2.2pt込み）")
        print(f"  {'区分':>14}  {'件数':>5}  {'勝率%':>6}  {'損益(円)':>11}  {'PF':>7}")
        print("  " + "-" * 50)
        for half in ["前半(1〜15日)", "後半(16〜末日)", f"{mo}月全体"]:
            grp = t_combined if half == f"{mo}月全体" else t_combined[t_combined["half"] == half]
            s = calc_summary(grp)
            print(f"  {half:>14}  {s['n']:>5}  {s['win_rate']:>5.1f}%  "
                  f"{s['pnl_yen']:>+11,.0f}  {pf_str(s['pf'])}")

        # ── 年別合算 ──
        print(f"\n  ■ 年別損益(円)（①③合算）")
        print(f"  {'年':>4}  {'前半(1〜15日)':>16}  {'後半(16〜末日)':>16}  {'月合計':>10}")
        print("  " + "-" * 55)
        for yr in sorted(t_combined["signal_year"].unique()):
            g  = t_combined[t_combined["signal_year"] == yr]
            gf = g[g["half"] == "前半(1〜15日)"]
            gb = g[g["half"] == "後半(16〜末日)"]
            sf = calc_summary(gf); sb = calc_summary(gb); st = calc_summary(g)
            print(f"  {yr}  "
                  f"{sf['pnl_yen']:>+11,.0f}({sf['n']}件)  "
                  f"{sb['pnl_yen']:>+11,.0f}({sb['n']}件)  "
                  f"{st['pnl_yen']:>+9,.0f}")
        gf_c = t_combined[t_combined["half"] == "前半(1〜15日)"]
        gb_c = t_combined[t_combined["half"] == "後半(16〜末日)"]
        print("  " + "-" * 55)
        print(f"  {'合計':>4}  "
              f"{calc_summary(gf_c)['pnl_yen']:>+11,.0f}({len(gf_c)}件)  "
              f"{calc_summary(gb_c)['pnl_yen']:>+11,.0f}({len(gb_c)}件)  "
              f"{calc_summary(t_combined)['pnl_yen']:>+9,.0f}")

        # ── 安定性・採用判断（合算） ──
        print(f"\n  ■ 安定性・採用判断（合算・PF>=1.2 かつ全年プラス）")
        for half in ["前半(1〜15日)", "後半(16〜末日)"]:
            grp = t_combined[t_combined["half"] == half]
            s = calc_summary(grp)
            neg_yrs = [
                yr for yr in sorted(grp["signal_year"].unique())
                if calc_summary(grp[grp["signal_year"] == yr])["pnl_yen"] < 0
            ]
            ok_pf     = s["pf"] >= 1.2
            ok_stable = len(neg_yrs) == 0
            verdict   = "✅ 採用候補" if (ok_pf and ok_stable) else "❌ 見送り"
            stable_str = "✅ 全年プラス" if ok_stable else f"❌ マイナス年: {[int(y) for y in neg_yrs]}"
            print(f"  {half}: PF={pf_str(s['pf'])}  {stable_str}  → {verdict}")


def _excl_indicator_s1(t1: pd.DataFrame, releases: pd.Series,
                        before_min: int = 30, after_min: int = 60) -> pd.DataFrame:
    """system① トレードから指標ウィンドウ内の signal_dt を除外"""
    if releases.empty:
        return t1
    before_ns = int(pd.Timedelta(minutes=before_min).total_seconds() * 1e9)
    after_ns  = int(pd.Timedelta(minutes=after_min).total_seconds() * 1e9)
    sig_ns = pd.to_datetime(t1["signal_dt"]).values.astype("int64")
    mask = np.zeros(len(sig_ns), dtype=bool)
    for r in releases:
        r_ns = pd.Timestamp(r).value
        mask |= (sig_ns >= r_ns - before_ns) & (sig_ns <= r_ns + after_ns)
    return t1[~mask].copy()


def _add_dst_col(df: pd.DataFrame) -> pd.DataFrame:
    """signal_dt から DST フラグ列を追加する。
    build_masks() と完全同一ロジック（dts_ns nanosecond 比較）を使用。
    """
    df = df.copy()
    dts_ns = pd.to_datetime(df["signal_dt"]).values.astype("int64")
    dst_mask = np.zeros(len(dts_ns), dtype=bool)
    for start, end in _DST_PERIODS:
        s_ns = start.value
        e_ns = end.value
        dst_mask |= (dts_ns >= s_ns) & (dts_ns <= e_ns)
    df["is_dst"] = dst_mask
    return df


def _print_dst_hour_table(dst_df: pd.DataFrame, win_df: pd.DataFrame):
    """時間帯別 DST/冬時間 比較テーブルを出力（日中・夜間セクション分け）"""
    all_hours = sorted(
        set(dst_df["signal_hour"].unique()) | set(win_df["signal_hour"].unique())
    )
    day_hours   = [h for h in all_hours if h in (8, 12, 15)]
    night_hours = [h for h in all_hours if h in (18, 19, 20, 21, 23)]

    header = (
        f"  {'時間':>4}  "
        f"{'件数(DST)':>9}  {'勝率%':>6}  {'損益(円)':>11}  {'PF':>6}  │  "
        f"{'件数(冬)':>8}  {'勝率%':>6}  {'損益(円)':>11}  {'PF':>6}"
    )
    sep = "  " + "-" * 88

    for label, hours in [("  ─日中─", day_hours), ("  ─夜間─", night_hours)]:
        if not hours:
            continue
        print(label)
        print(header)
        print(sep)
        for hr in hours:
            d  = dst_df[dst_df["signal_hour"] == hr]
            w  = win_df[win_df["signal_hour"] == hr]
            sd = calc_summary(d)
            sw = calc_summary(w)
            print(
                f"  {hr:>3}時  "
                f"{sd['n']:>9}  {sd['win_rate']:>5.1f}%  {sd['pnl_yen']:>+11,.0f}  {pf_str(sd['pf'])}"
                f"  │  "
                f"{sw['n']:>8}  {sw['win_rate']:>5.1f}%  {sw['pnl_yen']:>+11,.0f}  {pf_str(sw['pf'])}"
            )

if __name__ == "__main__":
    import sys
    with open("bt_result.txt", "w", encoding="utf-8") as f:
        sys.stdout = f
        main()
