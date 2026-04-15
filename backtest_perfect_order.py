from pathlib import Path
from itertools import product
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

# =========================================
# 設定
# =========================================
DATA_DIR   = Path(r"C:\kabu_trade\data")
FILES      = [
    "N225microf_2023.xlsx",
    "N225microf_2024.xlsx",
    "N225microf_2025.xlsx",
    "N225microf_2026.xlsx",
]
SHEET_NAME = "5min"

MA_SHORT_LIST = [5, 7, 9]
MA_MID_LIST   = [20, 25, 21]
MA_LONG_LIST  = [75, 55, 99]

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIG  = 9

SL_LIST      = [30, 60]
TP_MULTS     = [2, 3, 4]

TOUCH_PCT            = 0.005   # ±0.5%
MAX_HOLD             = 50      # 最大保有バー数（タイムアウト用）
COMMISSION_PT        = 2.2     # 往復手数料（22円 ÷ 10円/pt = 2.2pt）
FORCE_CLOSE_SESSION  = True    # True=セッション境界強制決済あり / False=現行ロジック
#   強制決済時刻: 15:40（日中終了）/ 06:00（夜間終了）/ 23:50（深夜強制）


# =========================================
# 米国夏時間・雇用統計フィルター
# =========================================
def is_us_summer_time(dt) -> bool:
    """米国夏時間（3月第2日曜〜11月第1日曜）かどうか判定"""
    march = datetime(dt.year, 3, 1)
    second_sunday_march = march + timedelta(days=(6 - march.weekday()) % 7 + 7)
    november = datetime(dt.year, 11, 1)
    first_sunday_november = november + timedelta(days=(6 - november.weekday()) % 7)
    return second_sunday_march <= dt < first_sunday_november


def is_nonfarm_payroll_time(dt) -> bool:
    """毎月第1金曜日の雇用統計発表時間帯かどうか判定
    夏時間: 20:30〜23:00 JST
    冬時間: 21:30〜翌0:00 JST
    """
    if dt.weekday() != 4:       # 金曜日でなければFalse
        return False
    if not (1 <= dt.day <= 7):  # 第1金曜日（1〜7日）でなければFalse
        return False
    h = dt.hour
    m = dt.minute
    hm = h * 100 + m  # hhmm形式
    if is_us_summer_time(dt):
        return 2030 <= hm < 2300   # 夏時間: 20:30〜23:00
    else:
        return hm >= 2130 or hm < 100  # 冬時間: 21:30〜翌1:00（0時またぎ）


# =========================================
# 経済指標カレンダー読み込み・フィルター
# =========================================
ECONOMIC_CALENDAR_PATH = Path(r"C:\kabu_trade\economic_calendar.csv")

def load_event_times(csv_path=None):
    """economic_calendar.csv を読み込む（UTF-8 / cp932 両対応）"""
    path = csv_path or ECONOMIC_CALENDAR_PATH
    for enc in ("utf-8", "utf-8-sig", "cp932"):
        try:
            df = pd.read_csv(path, encoding=enc)
            if "indicator" in df.columns:
                df["release_datetime_jst"] = pd.to_datetime(df["release_datetime_jst"])
                return df
        except Exception:
            continue
    raise ValueError(f"economic_calendar.csv を読み込めません: {path}")


def build_event_mask(trades_signal_dt: pd.Series,
                     event_df: pd.DataFrame,
                     window_before: int = 30,
                     window_after: int = 60) -> pd.Series:
    """signal_dt が各指標発表時間帯（前30分〜後60分）に該当するか判定するベクトル化版。
    is_event_window() のループ版より大幅に高速。"""
    releases = event_df["release_datetime_jst"].values  # numpy datetime64
    sdt = pd.to_datetime(trades_signal_dt).values       # numpy datetime64
    wb = pd.Timedelta(minutes=window_before).value
    wa = pd.Timedelta(minutes=window_after).value
    # shape: (len(sdt), len(releases))
    diff = sdt[:, None].astype("int64") - releases[None, :].astype("int64")
    mask = (diff >= -wb) & (diff <= wa)
    return pd.Series(mask.any(axis=1), index=trades_signal_dt.index)


TOP_N = 20

# =========================================
# データ読み込み（backtest_micro_macd_ma.py から流用）
# =========================================
def read_one_file(path: Path, sheet_name: str = "5min") -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name=sheet_name, engine="openpyxl")
    df = df.rename(columns={
        "日付": "date",
        "時間": "time",
        "始値": "open",
        "高値": "high",
        "安値": "low",
        "終値": "close",
        "出来高": "volume",
    })
    df["datetime"] = pd.to_datetime(
        df["date"].astype(str).str.strip() + " " + df["time"].astype(str).str.strip(),
        errors="coerce"
    )
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["datetime", "open", "high", "low", "close", "volume"]).copy()
    df = df.sort_values("datetime").reset_index(drop=True)
    return df[["datetime", "open", "high", "low", "close", "volume"]]


def load_all() -> pd.DataFrame:
    dfs = []
    print("データ読み込み中...")
    for f in FILES:
        path = DATA_DIR / f
        if not path.exists():
            print(f"  スキップ（存在しない）: {path}")
            continue
        print(f"  読み込み: {path}")
        df = read_one_file(path, sheet_name=SHEET_NAME)
        print(f"    {len(df)} 本")
        dfs.append(df)
    if not dfs:
        raise FileNotFoundError("Excelファイルが1本も見つかりませんでした。")
    df = pd.concat(dfs, ignore_index=True)
    df = df.sort_values("datetime").drop_duplicates(subset=["datetime"]).reset_index(drop=True)
    print(f"合計読み込み本数: {len(df)}\n")
    return df


# =========================================
# 指標計算
# =========================================
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # 必要な全MA期間を事前計算
    all_periods = sorted(set(MA_SHORT_LIST + MA_MID_LIST + MA_LONG_LIST))
    for p in all_periods:
        df[f"ma{p}"] = df["close"].rolling(p).mean()

    # MACD (12, 26, 9)
    ema_fast       = df["close"].ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow       = df["close"].ewm(span=MACD_SLOW, adjust=False).mean()
    df["macd"]     = ema_fast - ema_slow
    df["macd_sig"] = df["macd"].ewm(span=MACD_SIG, adjust=False).mean()

    return df


# =========================================
# バックテスト（パーフェクトオーダー＋押し目/戻り売り）
# signal_dt: シグナルバーのdatetime を trades に含める
# hour_filter / weekday_filter: Noneなら全件、リストなら該当のみ
# =========================================
def run_backtest(df: pd.DataFrame,
                 ma_short: int, ma_mid: int, ma_long: int,
                 sl: int, tp: int,
                 direction: str,
                 commission: float = 0.0,
                 hour_filter=None,
                 weekday_filter=None) -> pd.DataFrame:
    col_s = f"ma{ma_short}"
    col_m = f"ma{ma_mid}"
    col_l = f"ma{ma_long}"

    arr_open    = df["open"].values
    arr_high    = df["high"].values
    arr_low     = df["low"].values
    arr_close   = df["close"].values
    arr_mas     = df[col_s].values
    arr_mam     = df[col_m].values
    arr_mal     = df[col_l].values
    arr_macd    = df["macd"].values
    arr_msig    = df["macd_sig"].values
    dts         = pd.to_datetime(df["datetime"])
    arr_hour    = dts.dt.hour.values
    arr_weekday = dts.dt.weekday.values   # 0=月, 4=金

    n = len(df)
    trades = []

    for i in range(1, n - 1):
        mas = arr_mas[i]
        mam = arr_mam[i]
        mal = arr_mal[i]

        if any(np.isnan(v) for v in [mas, mam, mal]):
            continue
        if np.isnan(arr_macd[i]) or np.isnan(arr_msig[i]):
            continue

        hi = arr_high[i]
        lo = arr_low[i]

        if direction == "long":
            if not (mas > mam > mal):
                continue
            if arr_macd[i] <= arr_msig[i]:
                continue
            if abs(lo - mas) / mas > TOUCH_PCT:
                continue
        else:  # short
            if not (mas < mam < mal):
                continue
            if arr_macd[i] >= arr_msig[i]:
                continue
            if abs(hi - mas) / mas > TOUCH_PCT:
                continue

        # 時間帯・曜日フィルター（シグナルバー基準）
        if hour_filter is not None and arr_hour[i] not in hour_filter:
            continue
        if weekday_filter is not None and arr_weekday[i] not in weekday_filter:
            continue

        # エントリー（次足のopen）
        ei    = i + 1
        entry = arr_open[ei]

        pnl   = None
        rtype = None

        for j in range(ei, min(ei + MAX_HOLD, n)):
            bhi = arr_high[j]
            blo = arr_low[j]
            if direction == "long":
                if bhi >= entry + tp:
                    pnl, rtype = float(tp),  "TP"; break
                if blo <= entry - sl:
                    pnl, rtype = float(-sl), "SL"; break
            else:
                if blo <= entry - tp:
                    pnl, rtype = float(tp),  "TP"; break
                if bhi >= entry + sl:
                    pnl, rtype = float(-sl), "SL"; break

        if pnl is None:
            close_idx = min(ei + MAX_HOLD - 1, n - 1)
            final = arr_close[close_idx]
            pnl   = float(final - entry) if direction == "long" else float(entry - final)
            rtype = "TIME"

        pnl -= commission

        trades.append({
            "signal_dt": df["datetime"].iloc[i],
            "datetime":  df["datetime"].iloc[ei],
            "pnl":       pnl,
            "result":    rtype,
            "signal_hour":    int(arr_hour[i]),
            "signal_weekday": int(arr_weekday[i]),
        })

    if not trades:
        return pd.DataFrame(columns=["signal_dt", "datetime", "pnl", "result",
                                     "signal_hour", "signal_weekday"])
    return pd.DataFrame(trades)


# =========================================
# バックテスト（条件緩和版：長期MA条件なし）
# =========================================
def run_backtest_relaxed(df: pd.DataFrame,
                         ma_short: int, ma_mid: int,
                         sl: int, tp: int,
                         direction: str,
                         commission: float = 0.0,
                         hour_filter=None,
                         weekday_filter=None,
                         force_close_session: bool = False) -> pd.DataFrame:
    col_s = f"ma{ma_short}"
    col_m = f"ma{ma_mid}"

    arr_open    = df["open"].values
    arr_high    = df["high"].values
    arr_low     = df["low"].values
    arr_close   = df["close"].values
    arr_mas     = df[col_s].values
    arr_mam     = df[col_m].values
    arr_macd    = df["macd"].values
    arr_msig    = df["macd_sig"].values
    dts         = pd.to_datetime(df["datetime"])
    arr_hour    = dts.dt.hour.values
    arr_minute  = dts.dt.minute.values
    arr_weekday = dts.dt.weekday.values
    arr_hm      = arr_hour * 100 + arr_minute   # hhmm形式（セッション境界判定用）

    SESSION_BOUNDARIES = frozenset({1540, 600, 2350})  # 15:40 / 06:00 / 23:50

    n = len(df)
    trades = []

    for i in range(1, n - 1):
        mas = arr_mas[i]
        mam = arr_mam[i]

        if any(np.isnan(v) for v in [mas, mam]):
            continue
        if np.isnan(arr_macd[i]) or np.isnan(arr_msig[i]):
            continue

        hi = arr_high[i]
        lo = arr_low[i]

        if direction == "long":
            # 短期MA > 中期MA かつ MACD GC かつ lowが短期MAにタッチ
            if not (mas > mam):
                continue
            if arr_macd[i] <= arr_msig[i]:
                continue
            if abs(lo - mas) / mas > TOUCH_PCT:
                continue
        else:  # short
            if not (mas < mam):
                continue
            if arr_macd[i] >= arr_msig[i]:
                continue
            if abs(hi - mas) / mas > TOUCH_PCT:
                continue

        # 時間帯・曜日フィルター（シグナルバー基準）
        if hour_filter is not None and arr_hour[i] not in hour_filter:
            continue
        if weekday_filter is not None and arr_weekday[i] not in weekday_filter:
            continue

        # エントリー（次足のopen）
        ei    = i + 1
        entry = arr_open[ei]

        pnl      = None
        rtype    = None
        exit_bar = ei

        for j in range(ei, min(ei + MAX_HOLD, n)):
            bhi = arr_high[j]
            blo = arr_low[j]
            # TP/SL 判定（セッション境界より優先）
            if direction == "long":
                if bhi >= entry + tp:
                    pnl, rtype = float(tp),  "TP"; exit_bar = j; break
                if blo <= entry - sl:
                    pnl, rtype = float(-sl), "SL"; exit_bar = j; break
            else:
                if blo <= entry - tp:
                    pnl, rtype = float(tp),  "TP"; exit_bar = j; break
                if bhi >= entry + sl:
                    pnl, rtype = float(-sl), "SL"; exit_bar = j; break
            # セッション境界強制決済（TP/SLに届かなかった場合のみ適用）
            if force_close_session and arr_hm[j] in SESSION_BOUNDARIES:
                final = arr_close[j]
                pnl   = float(final - entry) if direction == "long" else float(entry - final)
                rtype = "SESSION"
                exit_bar = j
                break

        if pnl is None:
            close_idx = min(ei + MAX_HOLD - 1, n - 1)
            final = arr_close[close_idx]
            pnl   = float(final - entry) if direction == "long" else float(entry - final)
            rtype = "TIME"
            exit_bar = close_idx

        pnl -= commission

        trades.append({
            "signal_dt":      df["datetime"].iloc[i],
            "datetime":       df["datetime"].iloc[ei],
            "exit_datetime":  df["datetime"].iloc[exit_bar],
            "pnl":            pnl,
            "result":         rtype,
            "signal_hour":    int(arr_hour[i]),
            "signal_weekday": int(arr_weekday[i]),
        })

    if not trades:
        return pd.DataFrame(columns=["signal_dt", "datetime", "exit_datetime", "pnl", "result",
                                     "signal_hour", "signal_weekday"])
    return pd.DataFrame(trades)


# =========================================
# ポジション重複除去
# =========================================
def apply_no_overlap(trades_df: pd.DataFrame) -> pd.DataFrame:
    """前トレードのexit_datetimeより後のentry_datetimeのみ残す"""
    if len(trades_df) == 0:
        return trades_df
    t = trades_df.sort_values("datetime").reset_index(drop=True)
    keep = []
    last_exit = pd.Timestamp("1900-01-01")
    for _, row in t.iterrows():
        entry_dt = pd.to_datetime(row["datetime"])
        if entry_dt > last_exit:
            keep.append(True)
            last_exit = pd.to_datetime(row["exit_datetime"])
        else:
            keep.append(False)
    return t[keep].reset_index(drop=True)


# =========================================
# 集計
# =========================================
def calc_summary(trades_df: pd.DataFrame) -> dict:
    if len(trades_df) == 0:
        return {"n": 0, "win_rate": 0.0, "pnl": 0.0, "ev": 0.0, "pf": 0.0}
    pnl  = trades_df["pnl"].values
    wins = pnl[pnl > 0].sum()
    loss = abs(pnl[pnl < 0].sum())
    n    = len(trades_df)
    return {
        "n":        n,
        "win_rate": float((pnl > 0).mean() * 100),
        "pnl":      float(pnl.sum()),
        "ev":       float(pnl.sum() / n),
        "pf":       float(wins / loss) if loss > 0 else 0.0,
    }


def print_summary_header():
    print(f"{'項目':<14} {'手数料なし':>12} {'手数料あり':>12}")
    print("-" * 40)


def print_summary_row(label, v0, v1, fmt=".1f"):
    print(f"{label:<14} {v0:>12{fmt}} {v1:>12{fmt}}")


# =========================================
# メイン
# =========================================
def main():
    df = load_all()
    df = add_indicators(df)

    # =========================================
    # パラメータ総当たり
    # =========================================
    results = []

    valid_combos = [
        (s, m, l)
        for s, m, l in product(MA_SHORT_LIST, MA_MID_LIST, MA_LONG_LIST)
        if s < m < l
    ]

    total = len(valid_combos) * len(SL_LIST) * len(TP_MULTS) * 2
    done  = 0
    print(f"パラメータ組み合わせ数: {total}\n")

    for (ma_s, ma_m, ma_l), sl, tp_mult, direction in product(
        valid_combos, SL_LIST, TP_MULTS, ["long", "short"]
    ):
        tp = sl * tp_mult
        trades = run_backtest(df, ma_s, ma_m, ma_l, sl, tp, direction, commission=0.0)
        summary = calc_summary(trades)
        summary.update({
            "ma_short":  ma_s,
            "ma_mid":    ma_m,
            "ma_long":   ma_l,
            "sl":        sl,
            "tp":        tp,
            "tp_mult":   tp_mult,
            "direction": direction,
        })
        results.append(summary)

        done += 1
        if done % 50 == 0:
            print(f"  進捗: {done}/{total}")

    res_df = pd.DataFrame(results)
    res_df = res_df[res_df["n"] > 0].copy()
    res_df = res_df.sort_values("pf", ascending=False).reset_index(drop=True)

    # =========================================
    # 集計表示（TOP 20）
    # =========================================
    print("\n" + "=" * 80)
    print(f"パーフェクトオーダー バックテスト結果 TOP{TOP_N}（手数料なし）")
    print("=" * 80)
    print(f"{'#':>3} {'方向':>5} {'MA短':>4} {'MA中':>4} {'MA長':>4} "
          f"{'SL':>4} {'TP':>4} {'Mult':>4} "
          f"{'件数':>5} {'勝率%':>7} {'損益合計':>10} {'期待値':>8} {'PF':>6}")
    print("-" * 80)

    top20 = res_df.head(TOP_N)
    for rank, row in top20.iterrows():
        print(
            f"{rank+1:>3} {row['direction']:>5} "
            f"{int(row['ma_short']):>4} {int(row['ma_mid']):>4} {int(row['ma_long']):>4} "
            f"{int(row['sl']):>4} {int(row['tp']):>4} {int(row['tp_mult']):>4}x "
            f"{int(row['n']):>5} {row['win_rate']:>6.1f}% "
            f"{row['pnl']:>10.1f} {row['ev']:>8.2f} {row['pf']:>6.2f}"
        )

    # =========================================
    # ベストパラメータ（PF1位）手数料込み比較
    # =========================================
    best = top20.iloc[0]
    print("\n" + "=" * 80)
    print("ベストパラメータ（PF1位）手数料込み成績比較")
    print(f"  方向={best['direction']}, MA短={int(best['ma_short'])}, "
          f"MA中={int(best['ma_mid'])}, MA長={int(best['ma_long'])}, "
          f"SL={int(best['sl'])}, TP={int(best['tp'])} ({int(best['tp_mult'])}x), "
          f"手数料={COMMISSION_PT}pt")
    print("=" * 80)

    trades_no_com = run_backtest(
        df,
        int(best["ma_short"]), int(best["ma_mid"]), int(best["ma_long"]),
        int(best["sl"]), int(best["tp"]),
        best["direction"],
        commission=0.0,
    )
    trades_with_com = run_backtest(
        df,
        int(best["ma_short"]), int(best["ma_mid"]), int(best["ma_long"]),
        int(best["sl"]), int(best["tp"]),
        best["direction"],
        commission=COMMISSION_PT,
    )

    s0 = calc_summary(trades_no_com)
    s1 = calc_summary(trades_with_com)

    print(f"{'項目':<14} {'手数料なし':>12} {'手数料あり':>12}")
    print("-" * 40)
    print(f"{'件数':<14} {s0['n']:>12} {s1['n']:>12}")
    print(f"{'勝率%':<14} {s0['win_rate']:>11.1f}% {s1['win_rate']:>11.1f}%")
    print(f"{'損益合計(pt)':<14} {s0['pnl']:>12.1f} {s1['pnl']:>12.1f}")
    print(f"{'期待値/trade':<14} {s0['ev']:>12.2f} {s1['ev']:>12.2f}")
    print(f"{'PF':<14} {s0['pf']:>12.2f} {s1['pf']:>12.2f}")
    print()

    # =========================================
    # ベストパラメータ固定（MA短9, MA中20, MA長55, SL30, TP60, long）
    # =========================================
    BEST_MAS  = 9
    BEST_MAM  = 20
    BEST_MAL  = 55
    BEST_SL   = 30
    BEST_TP   = 60
    BEST_DIR  = "long"

    best_trades = run_backtest(
        df, BEST_MAS, BEST_MAM, BEST_MAL, BEST_SL, BEST_TP, BEST_DIR, commission=0.0
    )

    # =========================================
    # 【分析1】時間帯別成績
    # =========================================
    print("\n" + "=" * 80)
    print("【分析1】時間帯別成績")
    print(f"  固定パラメータ: MA短{BEST_MAS}, MA中{BEST_MAM}, MA長{BEST_MAL}, "
          f"SL{BEST_SL}, TP{BEST_TP}, {BEST_DIR}（手数料なし）")
    print("=" * 80)
    print(f"{'時間帯':>6} {'件数':>6} {'勝率%':>7} {'損益合計':>10} {'期待値':>8} {'PF':>6}")
    print("-" * 50)

    hour_pf = {}
    for hour in sorted(best_trades["signal_hour"].unique()):
        grp = best_trades[best_trades["signal_hour"] == hour]
        s   = calc_summary(grp)
        hour_pf[hour] = s
        print(f"  {hour:02d}:xx  {s['n']:>6} {s['win_rate']:>6.1f}% "
              f"{s['pnl']:>10.1f} {s['ev']:>8.2f} {s['pf']:>6.2f}")

    # =========================================
    # 【分析2】曜日別成績
    # =========================================
    WEEKDAY_NAME = {0: "月", 1: "火", 2: "水", 3: "木", 4: "金"}

    print("\n" + "=" * 80)
    print("【分析2】曜日別成績")
    print(f"  固定パラメータ: MA短{BEST_MAS}, MA中{BEST_MAM}, MA長{BEST_MAL}, "
          f"SL{BEST_SL}, TP{BEST_TP}, {BEST_DIR}（手数料なし）")
    print("=" * 80)
    print(f"{'曜日':>4} {'件数':>6} {'勝率%':>7} {'損益合計':>10} {'期待値':>8} {'PF':>6}")
    print("-" * 50)

    weekday_pf = {}
    for wd in sorted(best_trades["signal_weekday"].unique()):
        grp = best_trades[best_trades["signal_weekday"] == wd]
        s   = calc_summary(grp)
        weekday_pf[wd] = s
        name = WEEKDAY_NAME.get(wd, str(wd))
        print(f"  {name}曜日  {s['n']:>6} {s['win_rate']:>6.1f}% "
              f"{s['pnl']:>10.1f} {s['ev']:>8.2f} {s['pf']:>6.2f}")

    # =========================================
    # 【分析3】条件緩和版バックテスト（長期MA条件なし）
    # =========================================
    print("\n" + "=" * 80)
    print("【分析3】条件緩和版バックテスト（長期MA条件削除）TOP20（手数料なし）")
    print("  条件: 短期MA>中期MA かつ MACD GC かつ lowが短期MAにタッチ±0.5%")
    print("=" * 80)

    # 緩和版パラメータ（MA_MID=[20,21,25]）
    MA2_RELAX = [20, 21, 25]
    relax_results = []

    r_total = len(MA_SHORT_LIST) * len(MA2_RELAX) * len(SL_LIST) * len(TP_MULTS) * 2
    r_done  = 0
    print(f"パラメータ組み合わせ数: {r_total}\n")

    for ma_s, ma_m, sl, tp_mult, direction in product(
        MA_SHORT_LIST, MA2_RELAX, SL_LIST, TP_MULTS, ["long", "short"]
    ):
        if ma_s >= ma_m:
            continue
        tp = sl * tp_mult
        trades = run_backtest_relaxed(df, ma_s, ma_m, sl, tp, direction, commission=0.0)
        summary = calc_summary(trades)
        summary.update({
            "ma_short":  ma_s,
            "ma_mid":    ma_m,
            "sl":        sl,
            "tp":        tp,
            "tp_mult":   tp_mult,
            "direction": direction,
        })
        relax_results.append(summary)

        r_done += 1
        if r_done % 50 == 0:
            print(f"  進捗: {r_done}/{r_total}")

    relax_df = pd.DataFrame(relax_results)
    relax_df = relax_df[relax_df["n"] > 0].copy()
    relax_df = relax_df.sort_values("pf", ascending=False).reset_index(drop=True)

    print(f"\n{'#':>3} {'方向':>5} {'MA短':>4} {'MA中':>4} "
          f"{'SL':>4} {'TP':>4} {'Mult':>4} "
          f"{'件数':>6} {'勝率%':>7} {'損益合計':>10} {'期待値':>8} {'PF':>6}")
    print("-" * 72)

    relax_top20 = relax_df.head(TOP_N)
    for rank, row in relax_top20.iterrows():
        print(
            f"{rank+1:>3} {row['direction']:>5} "
            f"{int(row['ma_short']):>4} {int(row['ma_mid']):>4} "
            f"{int(row['sl']):>4} {int(row['tp']):>4} {int(row['tp_mult']):>4}x "
            f"{int(row['n']):>6} {row['win_rate']:>6.1f}% "
            f"{row['pnl']:>10.1f} {row['ev']:>8.2f} {row['pf']:>6.2f}"
        )

    # 緩和版ベストパラメータ（手数料込み比較）
    r_best = relax_top20.iloc[0]
    print("\n" + "-" * 72)
    print(f"緩和版ベスト（PF1位）手数料込み比較")
    print(f"  方向={r_best['direction']}, MA短={int(r_best['ma_short'])}, "
          f"MA中={int(r_best['ma_mid'])}, "
          f"SL={int(r_best['sl'])}, TP={int(r_best['tp'])} ({int(r_best['tp_mult'])}x), "
          f"手数料={COMMISSION_PT}pt")
    print("-" * 72)

    r_trades0 = run_backtest_relaxed(
        df, int(r_best["ma_short"]), int(r_best["ma_mid"]),
        int(r_best["sl"]), int(r_best["tp"]), r_best["direction"], commission=0.0
    )
    r_trades1 = run_backtest_relaxed(
        df, int(r_best["ma_short"]), int(r_best["ma_mid"]),
        int(r_best["sl"]), int(r_best["tp"]), r_best["direction"], commission=COMMISSION_PT
    )
    rs0 = calc_summary(r_trades0)
    rs1 = calc_summary(r_trades1)

    print(f"{'項目':<14} {'手数料なし':>12} {'手数料あり':>12}")
    print("-" * 40)
    print(f"{'件数':<14} {rs0['n']:>12} {rs1['n']:>12}")
    print(f"{'勝率%':<14} {rs0['win_rate']:>11.1f}% {rs1['win_rate']:>11.1f}%")
    print(f"{'損益合計(pt)':<14} {rs0['pnl']:>12.1f} {rs1['pnl']:>12.1f}")
    print(f"{'期待値/trade':<14} {rs0['ev']:>12.2f} {rs1['ev']:>12.2f}")
    print(f"{'PF':<14} {rs0['pf']:>12.2f} {rs1['pf']:>12.2f}")

    # =========================================
    # 【分析4】時間帯×曜日フィルター適用版
    # =========================================
    print("\n" + "=" * 80)
    print("【分析4】時間帯×曜日フィルター適用版")
    print(f"  固定パラメータ: MA短{BEST_MAS}, MA中{BEST_MAM}, MA長{BEST_MAL}, "
          f"SL{BEST_SL}, TP{BEST_TP}, {BEST_DIR}（手数料なし）")
    print("=" * 80)

    # 分析1・2の結果から EV>0 の時間帯・曜日を抽出
    strong_hours   = [h  for h,  s in hour_pf.items()    if s["ev"] > 0 and s["n"] >= 20]
    strong_weekdays = [wd for wd, s in weekday_pf.items() if s["ev"] > 0 and s["n"] >= 100]

    print(f"  強い時間帯（EV>0, n>=20）: {sorted(strong_hours)}")
    print(f"  強い曜日  （EV>0, n>=100）: "
          f"{[WEEKDAY_NAME.get(d,str(d))+'曜' for d in sorted(strong_weekdays)]}\n")

    scenarios = [
        ("フィルターなし（ベース）", None, None),
        ("時間帯フィルターのみ",      strong_hours, None),
        ("曜日フィルターのみ",        None, strong_weekdays),
        ("時間帯×曜日 両方",          strong_hours, strong_weekdays),
    ]

    print(f"{'シナリオ':<22} {'件数':>6} {'勝率%':>7} {'損益合計':>10} {'期待値':>8} {'PF':>6}")
    print("-" * 66)

    for label, hf, wf in scenarios:
        t = run_backtest(
            df, BEST_MAS, BEST_MAM, BEST_MAL, BEST_SL, BEST_TP, BEST_DIR,
            commission=0.0,
            hour_filter=hf,
            weekday_filter=wf,
        )
        s = calc_summary(t)
        print(f"  {label:<20} {s['n']:>6} {s['win_rate']:>6.1f}% "
              f"{s['pnl']:>10.1f} {s['ev']:>8.2f} {s['pf']:>6.2f}")

    # 手数料込みでも確認（両方フィルター）
    t_com = run_backtest(
        df, BEST_MAS, BEST_MAM, BEST_MAL, BEST_SL, BEST_TP, BEST_DIR,
        commission=COMMISSION_PT,
        hour_filter=strong_hours,
        weekday_filter=strong_weekdays,
    )
    sc = calc_summary(t_com)
    print(f"  {'時間帯×曜日 手数料込み':<20} {sc['n']:>6} {sc['win_rate']:>6.1f}% "
          f"{sc['pnl']:>10.1f} {sc['ev']:>8.2f} {sc['pf']:>6.2f}")
    print()

    # =========================================
    # 【緩和版ショート戦略の詳細分析】
    # MA短=9, MA中=20, SL=60, TP=240, short 固定
    # =========================================
    S_MAS = 9
    S_MAM = 20
    S_SL  = 60
    S_TP  = 240
    S_DIR = "short"

    short_base = run_backtest_relaxed(
        df, S_MAS, S_MAM, S_SL, S_TP, S_DIR, commission=0.0
    )

    # =========================================
    # 【分析S-1】時間帯別成績（ショート緩和版）
    # =========================================
    print("\n" + "=" * 80)
    print("【分析S-1】緩和版ショート 時間帯別成績")
    print(f"  固定パラメータ: MA短{S_MAS}, MA中{S_MAM}, SL{S_SL}, TP{S_TP}, {S_DIR}（手数料なし）")
    print("=" * 80)
    print(f"{'時間帯':>6} {'件数':>6} {'勝率%':>7} {'損益合計':>10} {'期待値':>8} {'PF':>6}")
    print("-" * 50)

    short_hour_pf = {}
    for hour in sorted(short_base["signal_hour"].unique()):
        grp = short_base[short_base["signal_hour"] == hour]
        s   = calc_summary(grp)
        short_hour_pf[hour] = s
        print(f"  {hour:02d}:xx  {s['n']:>6} {s['win_rate']:>6.1f}% "
              f"{s['pnl']:>10.1f} {s['ev']:>8.2f} {s['pf']:>6.2f}")

    # =========================================
    # 【分析S-2】曜日別成績（ショート緩和版）
    # =========================================
    print("\n" + "=" * 80)
    print("【分析S-2】緩和版ショート 曜日別成績")
    print(f"  固定パラメータ: MA短{S_MAS}, MA中{S_MAM}, SL{S_SL}, TP{S_TP}, {S_DIR}（手数料なし）")
    print("=" * 80)
    print(f"{'曜日':>4} {'件数':>6} {'勝率%':>7} {'損益合計':>10} {'期待値':>8} {'PF':>6}")
    print("-" * 50)

    short_weekday_pf = {}
    for wd in sorted(short_base["signal_weekday"].unique()):
        grp  = short_base[short_base["signal_weekday"] == wd]
        s    = calc_summary(grp)
        short_weekday_pf[wd] = s
        name = WEEKDAY_NAME.get(wd, str(wd))
        print(f"  {name}曜日  {s['n']:>6} {s['win_rate']:>6.1f}% "
              f"{s['pnl']:>10.1f} {s['ev']:>8.2f} {s['pf']:>6.2f}")

    # =========================================
    # 【分析S-3】時間帯×曜日フィルター適用（ショート緩和版）
    # =========================================
    print("\n" + "=" * 80)
    print("【分析S-3】緩和版ショート 時間帯×曜日フィルター適用版")
    print(f"  固定パラメータ: MA短{S_MAS}, MA中{S_MAM}, SL{S_SL}, TP{S_TP}, {S_DIR}")
    print("=" * 80)

    s_strong_hours    = [h  for h,  s in short_hour_pf.items()    if s["ev"] > 0 and s["n"] >= 20]
    s_strong_weekdays = [wd for wd, s in short_weekday_pf.items() if s["ev"] > 0 and s["n"] >= 100]

    print(f"  強い時間帯（EV>0, n>=20）: {sorted(s_strong_hours)}")
    print(f"  強い曜日  （EV>0, n>=100）: "
          f"{[WEEKDAY_NAME.get(d,str(d))+'曜' for d in sorted(s_strong_weekdays)]}\n")

    s_scenarios = [
        ("フィルターなし（ベース）", None,            None,              0.0),
        ("時間帯フィルターのみ",      s_strong_hours,  None,              0.0),
        ("曜日フィルターのみ",        None,            s_strong_weekdays, 0.0),
        ("時間帯×曜日 両方",          s_strong_hours,  s_strong_weekdays, 0.0),
        ("時間帯×曜日 手数料込み",    s_strong_hours,  s_strong_weekdays, COMMISSION_PT),
    ]

    print(f"{'シナリオ':<22} {'件数':>6} {'勝率%':>7} {'損益合計':>10} {'期待値':>8} {'PF':>6}")
    print("-" * 66)

    for label, hf, wf, com in s_scenarios:
        t = run_backtest_relaxed(
            df, S_MAS, S_MAM, S_SL, S_TP, S_DIR,
            commission=com,
            hour_filter=hf,
            weekday_filter=wf,
        )
        s = calc_summary(t)
        print(f"  {label:<20} {s['n']:>6} {s['win_rate']:>6.1f}% "
              f"{s['pnl']:>10.1f} {s['ev']:>8.2f} {s['pf']:>6.2f}")

    # =========================================
    # 【分析S-4】ロング緩和版との最終比較
    # =========================================
    print("\n" + "=" * 80)
    print("【分析S-4】ロング緩和版 vs ショート緩和版 最終比較（手数料込み 2.2pt）")
    print("=" * 80)

    # ① ロング緩和版：時間帯フィルター×木曜除外（wd=3を除く）
    long_strong_hours    = strong_hours   # 分析4で算出済み
    long_strong_weekdays = [wd for wd in strong_weekdays if wd != 3]  # 木曜(3)除外

    t_long = run_backtest_relaxed(
        df, 9, 20, 60, 240, "long",
        commission=COMMISSION_PT,
        hour_filter=long_strong_hours,
        weekday_filter=long_strong_weekdays,
    )

    # ② ショート：時間帯フィルター×弱曜日除外
    t_short = run_backtest_relaxed(
        df, S_MAS, S_MAM, S_SL, S_TP, S_DIR,
        commission=COMMISSION_PT,
        hour_filter=s_strong_hours,
        weekday_filter=s_strong_weekdays,
    )

    sl_long  = calc_summary(t_long)
    sl_short = calc_summary(t_short)

    # ③ ①②同時運用（合算）
    combined_pnl      = sl_long["pnl"]  + sl_short["pnl"]
    combined_n        = sl_long["n"]    + sl_short["n"]
    combined_wins     = t_long["pnl"].clip(lower=0).sum() + t_short["pnl"].clip(lower=0).sum()
    combined_loss_abs = abs(t_long["pnl"].clip(upper=0).sum()) + abs(t_short["pnl"].clip(upper=0).sum())
    combined_win_n    = (t_long["pnl"] > 0).sum() + (t_short["pnl"] > 0).sum()
    combined_pf       = combined_wins / combined_loss_abs if combined_loss_abs > 0 else 0.0
    combined_wr       = combined_win_n / combined_n * 100 if combined_n > 0 else 0.0
    combined_ev       = combined_pnl / combined_n if combined_n > 0 else 0.0

    print(f"\n{'条件':<32} {'件数':>6} {'勝率%':>7} {'損益合計':>10} {'期待値':>8} {'PF':>6}")
    print("-" * 72)
    print(f"  {'①ロング(時間帯×曜日, 手数料込)':<30} {sl_long['n']:>6} {sl_long['win_rate']:>6.1f}% "
          f"{sl_long['pnl']:>10.1f} {sl_long['ev']:>8.2f} {sl_long['pf']:>6.2f}")
    print(f"  {'②ショート(時間帯×曜日, 手数料込)':<30} {sl_short['n']:>6} {sl_short['win_rate']:>6.1f}% "
          f"{sl_short['pnl']:>10.1f} {sl_short['ev']:>8.2f} {sl_short['pf']:>6.2f}")
    print(f"  {'③①②同時運用（合算）':<30} {combined_n:>6} {combined_wr:>6.1f}% "
          f"{combined_pnl:>10.1f} {combined_ev:>8.2f} {combined_pf:>6.2f}")
    print("-" * 72)
    print(f"  {'④参考：分析4ロング手数料込み':<30} {sc['n']:>6} {sc['win_rate']:>6.1f}% "
          f"{sc['pnl']:>10.1f} {sc['ev']:>8.2f} {sc['pf']:>6.2f}")
    print()

    # =========================================
    # 【分析C】ショート緩和版 年別・月別安定性確認
    # MA短9, MA中20, SL60, TP240, short
    # 時間帯フィルター + 曜日フィルター（火曜除外）+ 手数料込み
    # =========================================
    # t_short は S-4 で算出済み（手数料込み・フィルター適用）
    PT_PER_YEN = 10  # N225マイクロ: 1pt = 10円

    tc = t_short.copy()
    tc["datetime"] = pd.to_datetime(tc["datetime"])
    tc["year"]  = tc["datetime"].dt.year
    tc["month"] = tc["datetime"].dt.month
    tc["yen"]   = tc["pnl"] * PT_PER_YEN

    def group_summary(grp: pd.DataFrame) -> dict:
        s = calc_summary(grp)
        s["yen"] = float(grp["yen"].sum())
        return s

    # =========================================
    # 【C-1】年別成績
    # =========================================
    print("\n" + "=" * 80)
    print("【分析C-1】ショート緩和版 年別成績（フィルター適用・手数料込み）")
    print(f"  条件: MA短{S_MAS}, MA中{S_MAM}, SL{S_SL}, TP{S_TP}, {S_DIR}, 手数料{COMMISSION_PT}pt")
    print("=" * 80)
    print(f"{'年':>5} {'件数':>6} {'勝率%':>7} {'損益(pt)':>10} {'損益(円)':>12} {'期待値':>8} {'PF':>6}")
    print("-" * 58)

    for yr in sorted(tc["year"].unique()):
        grp = tc[tc["year"] == yr]
        s   = group_summary(grp)
        print(f"  {yr}  {s['n']:>6} {s['win_rate']:>6.1f}% "
              f"{s['pnl']:>10.1f} {s['yen']:>12,.0f} {s['ev']:>8.2f} {s['pf']:>6.2f}")

    # 全期間合計
    s_all = group_summary(tc)
    print("-" * 58)
    print(f"  {'合計':>4}  {s_all['n']:>6} {s_all['win_rate']:>6.1f}% "
          f"{s_all['pnl']:>10.1f} {s_all['yen']:>12,.0f} {s_all['ev']:>8.2f} {s_all['pf']:>6.2f}")

    # =========================================
    # 【C-2】月別成績（全年合算）
    # =========================================
    print("\n" + "=" * 80)
    print("【分析C-2】ショート緩和版 月別成績（全年合算・フィルター適用・手数料込み）")
    print("=" * 80)
    print(f"{'月':>3} {'件数':>6} {'勝率%':>7} {'損益(pt)':>10} {'損益(円)':>12} {'期待値':>8} {'PF':>6}")
    print("-" * 58)

    for mo in range(1, 13):
        grp = tc[tc["month"] == mo]
        if len(grp) == 0:
            print(f"  {mo:>2}月  {'---':>6}")
            continue
        s = group_summary(grp)
        print(f"  {mo:>2}月  {s['n']:>6} {s['win_rate']:>6.1f}% "
              f"{s['pnl']:>10.1f} {s['yen']:>12,.0f} {s['ev']:>8.2f} {s['pf']:>6.2f}")

    # =========================================
    # 【C-3】年×月クロス集計（損益合計・円）
    # =========================================
    print("\n" + "=" * 80)
    print("【分析C-3】年×月クロス集計（損益合計・円）")
    print("=" * 80)

    pivot = tc.pivot_table(
        values="yen", index="year", columns="month", aggfunc="sum", fill_value=0
    )
    pivot.columns = [f"{int(c):>2}月" for c in pivot.columns]
    pivot["年間合計"] = pivot.sum(axis=1)

    # ヘッダー
    col_names = list(pivot.columns)
    header = f"{'年':>5}  " + "  ".join(f"{c:>8}" for c in col_names)
    print(header)
    print("-" * len(header))

    for yr, row in pivot.iterrows():
        vals = "  ".join(f"{int(v):>8,}" for v in row.values)
        print(f"  {yr}  {vals}")

    # 月別合計行
    totals = pivot.sum(axis=0)
    tot_str = "  ".join(f"{int(v):>8,}" for v in totals.values)
    print("-" * len(header))
    print(f"  {'合計':>4}  {tot_str}")
    print()

    # =========================================
    # 【系統③最終確定バックテスト】
    # 時間帯×曜日フィルター ＋ 7月・11月除外
    # =========================================
    EXCLUDE_MONTHS = [7, 11]

    def run_short_filtered(month_exclude=None, hour_f=None, weekday_f=None,
                           com=0.0, force_close_session=False):
        """緩和版ショートを実行し、月除外をpost-filterで適用"""
        t = run_backtest_relaxed(
            df, S_MAS, S_MAM, S_SL, S_TP, S_DIR,
            commission=com,
            hour_filter=hour_f,
            weekday_filter=weekday_f,
            force_close_session=force_close_session,
        )
        if month_exclude and len(t) > 0:
            t["_month"] = pd.to_datetime(t["datetime"]).dt.month
            t = t[~t["_month"].isin(month_exclude)].drop(columns="_month")
        t["yen"] = t["pnl"] * PT_PER_YEN
        return t.reset_index(drop=True)

    # 最終フィルター適用トレード（手数料込み）
    tf = run_short_filtered(
        month_exclude=EXCLUDE_MONTHS,
        hour_f=s_strong_hours,
        weekday_f=s_strong_weekdays,
        com=COMMISSION_PT,
    )

    print("\n" + "=" * 80)
    print("【系統③最終確定バックテスト】ショート緩和版（全フィルター＋除外月）")
    print(f"  MA短{S_MAS}, MA中{S_MAM}, SL{S_SL}, TP{S_TP}, {S_DIR}, 手数料{COMMISSION_PT}pt")
    print(f"  時間帯: {sorted(s_strong_hours)}")
    print(f"  曜日  : {[WEEKDAY_NAME.get(d,str(d))+'曜' for d in sorted(s_strong_weekdays)]}")
    print(f"  除外月: {EXCLUDE_MONTHS}")
    print("=" * 80)

    # ① 全体成績
    sf = group_summary(tf)
    print(f"\n①全体成績:")
    print(f"  件数={sf['n']}, 勝率={sf['win_rate']:.1f}%, "
          f"損益={sf['pnl']:.1f}pt / {sf['yen']:,.0f}円, "
          f"期待値={sf['ev']:.2f}pt, PF={sf['pf']:.2f}")

    # ② 年別成績
    tf["year"]  = pd.to_datetime(tf["datetime"]).dt.year
    tf["month"] = pd.to_datetime(tf["datetime"]).dt.month

    print(f"\n②年別成績:")
    print(f"{'年':>5} {'件数':>6} {'勝率%':>7} {'損益(pt)':>10} {'損益(円)':>12} {'期待値':>8} {'PF':>6}")
    print("-" * 58)
    for yr in sorted(tf["year"].unique()):
        grp = tf[tf["year"] == yr]
        s   = group_summary(grp)
        print(f"  {yr}  {s['n']:>6} {s['win_rate']:>6.1f}% "
              f"{s['pnl']:>10.1f} {s['yen']:>12,.0f} {s['ev']:>8.2f} {s['pf']:>6.2f}")
    print("-" * 58)
    print(f"  {'合計':>4}  {sf['n']:>6} {sf['win_rate']:>6.1f}% "
          f"{sf['pnl']:>10.1f} {sf['yen']:>12,.0f} {sf['ev']:>8.2f} {sf['pf']:>6.2f}")

    # ③ 月別成績（全年合算）
    print(f"\n③月別成績（全年合算・7月/11月除外済み）:")
    print(f"{'月':>3} {'件数':>6} {'勝率%':>7} {'損益(pt)':>10} {'損益(円)':>12} {'期待値':>8} {'PF':>6}")
    print("-" * 58)
    for mo in range(1, 13):
        if mo in EXCLUDE_MONTHS:
            print(f"  {mo:>2}月  {'(除外)':>6}")
            continue
        grp = tf[tf["month"] == mo]
        if len(grp) == 0:
            print(f"  {mo:>2}月  {'---':>6}")
            continue
        s = group_summary(grp)
        print(f"  {mo:>2}月  {s['n']:>6} {s['win_rate']:>6.1f}% "
              f"{s['pnl']:>10.1f} {s['yen']:>12,.0f} {s['ev']:>8.2f} {s['pf']:>6.2f}")

    # ④ 段階的改善比較表
    print(f"\n④段階的フィルター改善比較（手数料込み {COMMISSION_PT}pt）:")
    print(f"{'フィルター段階':<26} {'件数':>6} {'勝率%':>7} {'損益(pt)':>11} {'損益(円)':>12} {'期待値':>8} {'PF':>6}")
    print("-" * 78)

    stages = [
        ("フィルターなし",               None,            None,              []),
        ("①時間帯フィルター",             s_strong_hours,  None,              []),
        ("②+曜日フィルター(火除外)",      s_strong_hours,  s_strong_weekdays, []),
        ("③+月除外(7・11月)",            s_strong_hours,  s_strong_weekdays, EXCLUDE_MONTHS),
    ]

    for label, hf, wf, mex in stages:
        t_s = run_short_filtered(month_exclude=mex if mex else None,
                                 hour_f=hf, weekday_f=wf, com=COMMISSION_PT)
        s_s = group_summary(t_s)
        print(f"  {label:<24} {s_s['n']:>6} {s_s['win_rate']:>6.1f}% "
              f"{s_s['pnl']:>11.1f} {s_s['yen']:>12,.0f} {s_s['ev']:>8.2f} {s_s['pf']:>6.2f}")
    print()

    # =========================================
    # 【タスク1】系統③：ポジション保有中は新規エントリーしない制約
    # =========================================
    print("\n" + "=" * 80)
    print("【タスク1】系統③ ポジション保有中新規エントリー禁止（手数料込み）")
    print(f"  MA短{S_MAS}, MA中{S_MAM}, SL{S_SL}, TP{S_TP}, {S_DIR}, 手数料{COMMISSION_PT}pt")
    print(f"  時間帯: {sorted(s_strong_hours)}  曜日: 月水木金  除外月: {EXCLUDE_MONTHS}")
    print("=" * 80)

    # tfは系統③最終確定トレード（手数料込み・全フィルター適用済み）
    tf_no = apply_no_overlap(tf)

    s_base = group_summary(tf)
    s_no   = group_summary(tf_no)

    print(f"\n{'':22} {'件数':>6} {'勝率%':>7} {'損益(pt)':>10} {'損益(円)':>12} {'期待値':>8} {'PF':>6}")
    print("-" * 72)
    print(f"  {'制約なし（全フィルター適用）':<20} {s_base['n']:>6} {s_base['win_rate']:>6.1f}% "
          f"{s_base['pnl']:>10.1f} {s_base['yen']:>12,.0f} {s_base['ev']:>8.2f} {s_base['pf']:>6.2f}")
    print(f"  {'保有中禁止':<20} {s_no['n']:>6} {s_no['win_rate']:>6.1f}% "
          f"{s_no['pnl']:>10.1f} {s_no['yen']:>12,.0f} {s_no['ev']:>8.2f} {s_no['pf']:>6.2f}")

    # 年別
    tf_no["year"]  = pd.to_datetime(tf_no["datetime"]).dt.year
    tf_no["month"] = pd.to_datetime(tf_no["datetime"]).dt.month
    tf_no["yen"]   = tf_no["pnl"] * PT_PER_YEN

    print(f"\n年別成績（保有中禁止）:")
    print(f"{'年':>5} {'件数':>6} {'勝率%':>7} {'損益(pt)':>10} {'損益(円)':>12} {'期待値':>8} {'PF':>6}")
    print("-" * 58)
    for yr in sorted(tf_no["year"].unique()):
        g  = tf_no[tf_no["year"] == yr]
        gs = group_summary(g)
        print(f"  {yr}  {gs['n']:>6} {gs['win_rate']:>6.1f}% "
              f"{gs['pnl']:>10.1f} {gs['yen']:>12,.0f} {gs['ev']:>8.2f} {gs['pf']:>6.2f}")
    print("-" * 58)
    print(f"  {'合計':>4}  {s_no['n']:>6} {s_no['win_rate']:>6.1f}% "
          f"{s_no['pnl']:>10.1f} {s_no['yen']:>12,.0f} {s_no['ev']:>8.2f} {s_no['pf']:>6.2f}")

    # 月別
    print(f"\n月別成績（保有中禁止）:")
    print(f"{'月':>3} {'件数':>6} {'勝率%':>7} {'損益(pt)':>10} {'損益(円)':>12} {'期待値':>8} {'PF':>6}")
    print("-" * 58)
    for mo in range(1, 13):
        if mo in EXCLUDE_MONTHS:
            print(f"  {mo:>2}月  {'(除外)':>6}")
            continue
        g = tf_no[tf_no["month"] == mo]
        if len(g) == 0:
            print(f"  {mo:>2}月  {'---':>6}")
            continue
        gs = group_summary(g)
        print(f"  {mo:>2}月  {gs['n']:>6} {gs['win_rate']:>6.1f}% "
              f"{gs['pnl']:>10.1f} {gs['yen']:>12,.0f} {gs['ev']:>8.2f} {gs['pf']:>6.2f}")
    print()

    # =========================================
    # 【最終レポート】系統③ 年別・年×月クロス集計（重複OK・手数料込み）
    # =========================================
    # tf は重複OK版（apply_no_overlap 適用前）の最終フィルタートレード
    def _cell(grp):
        """セル文字列: 件数/勝率%/損益円"""
        n = len(grp)
        if n == 0:
            return "---"
        wr  = (grp["pnl"] > 0).sum() / n * 100
        yn  = int(grp["yen"].sum())
        return f"{n}/{wr:.0f}%/{yn:,}"

    CW = 18  # セル幅

    print("\n" + "=" * 80)
    print("【最終レポート】系統③ MA短9/MA中20 SL60 TP240 short（重複OK・手数料込み）")
    print(f"  フィルター: 時間帯{sorted(s_strong_hours)} / 曜日[月水木金] / 除外月{EXCLUDE_MONTHS}")
    print("=" * 80)

    # --- 年別成績 ---
    s3_all = calc_summary(tf)
    ev3    = s3_all["pnl"] / s3_all["n"] if s3_all["n"] > 0 else 0.0
    print(f"\n■ 年別成績")
    print(f"{'年':>5} {'件数':>6} {'勝率%':>7} {'損益(pt)':>10} {'損益(円)':>12} {'期待値':>8} {'PF':>6}")
    print("-" * 58)
    for yr in sorted(tf["year"].unique()):
        g  = tf[tf["year"] == yr]
        gs = calc_summary(g)
        ev = gs["pnl"] / gs["n"] if gs["n"] > 0 else 0.0
        print(f"  {yr}  {gs['n']:>6} {gs['win_rate']:>6.1f}% "
              f"{gs['pnl']:>10.1f} {g['yen'].sum():>12,.0f} {ev:>8.2f} {gs['pf']:>6.3f}")
    print("-" * 58)
    print(f"  {'合計':>4}  {s3_all['n']:>6} {s3_all['win_rate']:>6.1f}% "
          f"{s3_all['pnl']:>10.1f} {tf['yen'].sum():>12,.0f} {ev3:>8.2f} {s3_all['pf']:>6.3f}")

    # --- 年×月クロス集計（前半・後半に分割） ---
    print(f"\n■ 年×月クロス集計（件数/勝率%/損益円）")
    YEARS3 = sorted(tf["year"].unique())

    for label, mrange in [("前半 1〜6月", range(1, 7)), ("後半 7〜12月", range(7, 13))]:
        hdr = f"{'年':>4}  " + "  ".join(f"{f'{m}月':^{CW}}" for m in mrange) \
              + f"  {'半期合計':^{CW}}"
        sep = "-" * len(hdr)
        print(f"\n{label}")
        print(sep);  print(hdr);  print(sep)
        mo_acc = {m: {"n": 0, "win": 0, "yen": 0.0} for m in mrange}
        for yr in YEARS3:
            cells = []
            h_n = 0;  h_win = 0;  h_yen = 0.0
            for mo in mrange:
                g = tf[(tf["year"] == yr) & (tf["month"] == mo)]
                n = len(g)
                cells.append(f"{_cell(g):^{CW}}")
                if n > 0:
                    mo_acc[mo]["n"]   += n
                    mo_acc[mo]["win"] += int((g["pnl"] > 0).sum())
                    mo_acc[mo]["yen"] += float(g["yen"].sum())
                    h_n   += n
                    h_win += int((g["pnl"] > 0).sum())
                    h_yen += float(g["yen"].sum())
            h_cell = f"{h_n}/{h_win/h_n*100:.0f}%/{int(h_yen):,}" if h_n > 0 else "---"
            print(f"{yr}  " + "  ".join(cells) + f"  {h_cell:^{CW}}")
        print(sep)
        tot_cells = []
        tt_n = 0;  tt_win = 0;  tt_yen = 0.0
        for mo in mrange:
            md = mo_acc[mo]
            if md["n"] == 0:
                tot_cells.append(f"{'---':^{CW}}")
            else:
                wr = md["win"] / md["n"] * 100
                tot_cells.append(f"{md['n']}/{wr:.0f}%/{int(md['yen']):,}".center(CW))
                tt_n += md["n"];  tt_win += md["win"];  tt_yen += md["yen"]
        tt_cell = f"{tt_n}/{tt_win/tt_n*100:.0f}%/{int(tt_yen):,}" if tt_n > 0 else "---"
        print(f"{'合計':>4}  " + "  ".join(tot_cells) + f"  {tt_cell:^{CW}}")

    # --- 全体サマリー ---
    print(f"\n■ 全体成績サマリー")
    print(f"  件数={s3_all['n']:,}  勝率={s3_all['win_rate']:.1f}%  "
          f"損益={s3_all['pnl']:.1f}pt / {tf['yen'].sum():,.0f}円  "
          f"期待値={ev3:.2f}pt  PF={s3_all['pf']:.3f}")
    print()

    # =========================================
    # 【雇用統計フィルター】ビフォーアフター比較
    # =========================================
    print("\n" + "=" * 80)
    print("【雇用統計フィルター】系統③ ビフォーアフター比較")
    print("  毎月第1金曜日: 夏時間20:30〜23:00 / 冬時間21:30〜翌1:00 をエントリー除外")
    print("=" * 80)

    # tf は系統③最終確定トレード（全フィルター適用済み）
    # signal_dt を使って雇用統計時間帯を除外
    nfp_mask = pd.to_datetime(tf["signal_dt"]).apply(is_nonfarm_payroll_time)
    tf_nfp   = tf[~nfp_mask].reset_index(drop=True)
    tf_nfp["year"]  = pd.to_datetime(tf_nfp["datetime"]).dt.year
    tf_nfp["month"] = pd.to_datetime(tf_nfp["datetime"]).dt.month
    tf_nfp["yen"]   = tf_nfp["pnl"] * PT_PER_YEN

    n_excluded = nfp_mask.sum()

    s_before = calc_summary(tf)
    s_after  = calc_summary(tf_nfp)
    ev_before = s_before["pnl"] / s_before["n"] if s_before["n"] > 0 else 0.0
    ev_after  = s_after["pnl"]  / s_after["n"]  if s_after["n"]  > 0 else 0.0

    print(f"\n  除外対象トレード数: {n_excluded}件")
    print()

    # ─ 全体比較表 ─
    W = 20
    print(f"{'項目':<16} {'除外なし（現状）':>{W}} {'除外あり':>{W}}")
    print("-" * (16 + W * 2 + 2))
    print(f"{'件数':<16} {s_before['n']:>{W},} {s_after['n']:>{W},}")
    print(f"{'勝率%':<16} {s_before['win_rate']:>{W}.1f} {s_after['win_rate']:>{W}.1f}")
    print(f"{'損益(円)':<16} {int(tf['yen'].sum()):>{W},} {int(tf_nfp['yen'].sum()):>{W},}")
    print(f"{'期待値(pt)':<16} {ev_before:>{W}.2f} {ev_after:>{W}.2f}")
    print(f"{'PF':<16} {s_before['pf']:>{W}.3f} {s_after['pf']:>{W}.3f}")

    # ─ 年別比較 ─
    print(f"\n■ 年別比較")
    hdr = f"{'年':>5}  {'除外なし':^36}  {'除外あり':^36}"
    sub = f"       {'件数':>5} {'勝率%':>6} {'損益(円)':>12} {'PF':>6}    {'件数':>5} {'勝率%':>6} {'損益(円)':>12} {'PF':>6}"
    print(hdr)
    print(sub)
    print("-" * len(sub))
    all_years = sorted(tf["year"].unique())
    for yr in all_years:
        gb = tf[tf["year"] == yr]
        ga = tf_nfp[tf_nfp["year"] == yr]
        sb = calc_summary(gb)
        sa = calc_summary(ga)
        yb = int(gb["yen"].sum())
        ya = int(ga["yen"].sum()) if len(ga) > 0 else 0
        print(f"  {yr}  "
              f"{sb['n']:>5} {sb['win_rate']:>6.1f}% {yb:>12,} {sb['pf']:>6.3f}    "
              f"{sa['n']:>5} {sa['win_rate']:>6.1f}% {ya:>12,} {sa['pf']:>6.3f}")
    sb_all = calc_summary(tf)
    sa_all = calc_summary(tf_nfp)
    print("-" * len(sub))
    print(f"  {'合計':>4}  "
          f"{sb_all['n']:>5} {sb_all['win_rate']:>6.1f}% {int(tf['yen'].sum()):>12,} {sb_all['pf']:>6.3f}    "
          f"{sa_all['n']:>5} {sa_all['win_rate']:>6.1f}% {int(tf_nfp['yen'].sum()):>12,} {sa_all['pf']:>6.3f}")

    # ─ 月別比較 ─
    print(f"\n■ 月別比較（全年合算）")
    sub3 = f"       {'件数':>5} {'勝率%':>6} {'損益(円)':>12} {'PF':>6}    {'件数':>5} {'勝率%':>6} {'損益(円)':>12} {'PF':>6}"
    print(sub3)
    print("-" * len(sub3))
    for mo in range(1, 13):
        if mo in EXCLUDE_MONTHS:
            print(f"  {mo:>2}月  {'(除外月)':^78}")
            continue
        gb = tf[tf["month"] == mo]
        ga = tf_nfp[tf_nfp["month"] == mo]
        if len(gb) == 0:
            print(f"  {mo:>2}月  {'---':^78}")
            continue
        sb = calc_summary(gb)
        sa = calc_summary(ga) if len(ga) > 0 else {"n": 0, "win_rate": 0.0, "pnl": 0.0, "pf": 0.0}
        yb = int(gb["yen"].sum())
        ya = int(ga["yen"].sum()) if len(ga) > 0 else 0
        print(f"  {mo:>2}月  "
              f"{sb['n']:>5} {sb['win_rate']:>6.1f}% {yb:>12,} {sb['pf']:>6.3f}    "
              f"{sa['n']:>5} {sa['win_rate']:>6.1f}% {ya:>12,} {sa['pf']:>6.3f}")
    print()

    # ─ 除外された雇用統計トレード詳細 ─
    if n_excluded > 0:
        print(f"■ 除外されたトレード詳細（第1金曜・雇用統計時間帯）")
        excl = tf[nfp_mask][["signal_dt", "datetime", "pnl", "result", "signal_hour"]].copy()
        excl["yen"] = excl["pnl"] * PT_PER_YEN
        excl["first_fri"] = pd.to_datetime(excl["signal_dt"]).dt.strftime("%Y-%m-%d")
        by_date = excl.groupby("first_fri").agg(
            件数=("pnl", "count"),
            損益pt=("pnl", "sum"),
            損益円=("yen", "sum"),
        ).reset_index()
        by_date.columns = ["日付", "件数", "損益pt", "損益(円)"]
        print(f"  {'日付':<14} {'件数':>5} {'損益(pt)':>10} {'損益(円)':>12}")
        print("  " + "-" * 44)
        for _, row in by_date.iterrows():
            print(f"  {row['日付']:<14} {int(row['件数']):>5} {row['損益pt']:>10.1f} {int(row['損益(円)']):>12,}")
        s_excl = calc_summary(excl)
        print("  " + "-" * 44)
        print(f"  {'合計':<14} {n_excluded:>5} {s_excl['pnl']:>10.1f} {int(excl['yen'].sum()):>12,}")
        print(f"\n  除外トレード成績: 勝率={s_excl['win_rate']:.1f}%  PF={s_excl['pf']:.3f}")
    print()

    # =========================================
    # 【重要指標フィルター】指標別ビフォーアフター比較
    # =========================================
    print("\n" + "=" * 80)
    print("【重要指標フィルター】指標別ビフォーアフター比較（手数料込み 2.2pt、系統③）")
    print("  除外ウィンドウ: 発表30分前〜発表60分後")
    print("=" * 80)

    # カレンダー読み込み
    event_df   = load_event_times()
    cpi_df     = event_df[event_df["indicator"] == "米CPI"]
    ppi_df     = event_df[event_df["indicator"] == "米PPI"]
    ism_mfg_df = event_df[event_df["indicator"] == "米ISM製造業"]
    ism_svc_df = event_df[event_df["indicator"] == "米ISM非製造業"]

    print(f"\n  カレンダー件数: CPI={len(cpi_df)}件 / PPI={len(ppi_df)}件 / "
          f"ISM製造業={len(ism_mfg_df)}件 / ISM非製造業={len(ism_svc_df)}件")

    sdt = tf["signal_dt"]  # signal_dt 列（系統③最終確定トレード）

    patterns = [
        ("①除外なし",        None),
        ("②CPI除外",         cpi_df),
        ("③PPI除外",         ppi_df),
        ("④ISM製造業除外",   ism_mfg_df),
        ("⑤ISM非製造業除外", ism_svc_df),
        ("⑥全指標除外",      event_df),
    ]

    results_pat = []
    for label, edf in patterns:
        if edf is None:
            t = tf.copy()
        else:
            mask = build_event_mask(sdt, edf, window_before=30, window_after=60)
            t = tf[~mask].reset_index(drop=True)
        s = calc_summary(t)
        ev = s["pnl"] / s["n"] if s["n"] > 0 else 0.0
        yen = int(t["yen"].sum()) if len(t) > 0 else 0
        excl_n = len(tf) - len(t)
        results_pat.append((label, s, ev, yen, excl_n, t))

    # ─ 全体比較表 ─
    print(f"\n■ 指標別ビフォーアフター比較")
    print(f"{'パターン':<18} {'件数':>7} {'除外件数':>6} {'勝率%':>7} {'損益(円)':>13} {'期待値(pt)':>10} {'PF':>7}")
    print("-" * 75)
    for label, s, ev, yen, excl_n, _ in results_pat:
        excl_str = f"(-{excl_n})" if excl_n > 0 else "  ---  "
        print(f"  {label:<16} {s['n']:>7,} {excl_str:>8} {s['win_rate']:>6.1f}% "
              f"{yen:>13,} {ev:>10.2f} {s['pf']:>7.3f}")

    # ─ ①ベースと⑥全除外の年別比較 ─
    tf_all_excl = results_pat[5][5]  # ⑥全指標除外
    tf_all_excl["year"]  = pd.to_datetime(tf_all_excl["datetime"]).dt.year
    tf_all_excl["month"] = pd.to_datetime(tf_all_excl["datetime"]).dt.month
    tf_all_excl["yen"]   = tf_all_excl["pnl"] * PT_PER_YEN

    print(f"\n■ 年別比較（①除外なし vs ⑥全指標除外）")
    print(f"{'年':>5}  {'①除外なし':>14} {'⑥全除外':>14} {'差分(円)':>12} {'PF①':>7} {'PF⑥':>7}")
    print("-" * 62)
    for yr in sorted(tf["year"].unique()):
        g1 = tf[tf["year"] == yr]
        g6 = tf_all_excl[tf_all_excl["year"] == yr]
        y1 = int(g1["yen"].sum())
        y6 = int(g6["yen"].sum()) if len(g6) > 0 else 0
        s1 = calc_summary(g1); s6 = calc_summary(g6)
        print(f"  {yr}  {y1:>14,} {y6:>14,} {y6-y1:>+12,} {s1['pf']:>7.3f} {s6['pf']:>7.3f}")
    y1t = int(tf["yen"].sum()); y6t = int(tf_all_excl["yen"].sum())
    s1t = calc_summary(tf); s6t = calc_summary(tf_all_excl)
    print("-" * 62)
    print(f"  {'合計':>4}  {y1t:>14,} {y6t:>14,} {y6t-y1t:>+12,} {s1t['pf']:>7.3f} {s6t['pf']:>7.3f}")

    # ─ ①ベースと⑥全除外の月別比較 ─
    print(f"\n■ 月別比較（①除外なし vs ⑥全指標除外、7月・11月除外済み）")
    print(f"{'月':>3}  {'①除外なし':>14} {'⑥全除外':>14} {'差分(円)':>12} {'PF①':>7} {'PF⑥':>7}")
    print("-" * 62)
    for mo in range(1, 13):
        if mo in EXCLUDE_MONTHS:
            print(f"  {mo:>2}月  {'(除外月)':^56}")
            continue
        g1 = tf[tf["month"] == mo]
        g6 = tf_all_excl[tf_all_excl["month"] == mo]
        if len(g1) == 0:
            print(f"  {mo:>2}月  {'---':^56}")
            continue
        y1 = int(g1["yen"].sum())
        y6 = int(g6["yen"].sum()) if len(g6) > 0 else 0
        s1 = calc_summary(g1); s6 = calc_summary(g6) if len(g6)>0 else {"pf":0.0}
        print(f"  {mo:>2}月  {y1:>14,} {y6:>14,} {y6-y1:>+12,} {s1['pf']:>7.3f} {s6['pf']:>7.3f}")

    # ─ 各指標の除外トレード成績 ─
    print(f"\n■ 各指標の除外トレード成績（除外することで得られる損益変化）")
    print(f"{'指標':<14} {'除外件数':>6} {'除外トレード損益(円)':>20} {'勝率%':>7} {'PF':>7}")
    print("-" * 58)
    for label, edf in [("米CPI", cpi_df), ("米PPI", ppi_df),
                        ("米ISM製造業", ism_mfg_df), ("米ISM非製造業", ism_svc_df),
                        ("全指標合計", event_df)]:
        mask = build_event_mask(sdt, edf, window_before=30, window_after=60)
        excl_t = tf[mask].copy()
        if len(excl_t) == 0:
            print(f"  {label:<12} {'0':>6}  {'---':>20}")
            continue
        excl_t["yen"] = excl_t["pnl"] * PT_PER_YEN
        s = calc_summary(excl_t)
        print(f"  {label:<12} {len(excl_t):>6}  {int(excl_t['yen'].sum()):>20,} {s['win_rate']:>6.1f}% {s['pf']:>7.3f}")
    print()

    # =========================================
    # 【サマータイム vs 冬時間】成績比較
    # =========================================
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

    print("\n" + "=" * 80)
    print("【サマータイム vs 冬時間】系統③ 成績比較（手数料込み 2.2pt）")
    print("="*80)

    tf["is_dst"] = pd.to_datetime(tf["signal_dt"]).apply(is_summer_time)
    tf_dst = tf[tf["is_dst"]].copy()
    tf_win = tf[~tf["is_dst"]].copy()

    s_dst = group_summary(tf_dst)
    s_win = group_summary(tf_win)
    s_all = group_summary(tf)

    # ─ 全体比較 ─
    print(f"\n■ サマータイム vs 冬時間 成績比較")
    print(f"{'期間':<12} {'件数':>7} {'勝率%':>7} {'損益(円)':>13} {'期待値(pt)':>10} {'PF':>7}")
    print("-"*60)
    for label, s, t in [("サマータイム", s_dst, tf_dst), ("冬時間", s_win, tf_win), ("合計", s_all, tf)]:
        print(f"  {label:<10} {s['n']:>7,} {s['win_rate']:>6.1f}% {int(t['yen'].sum()):>13,} "
              f"{s['ev']:>10.2f} {s['pf']:>7.3f}")

    # ─ 年別 ─
    print(f"\n■ 年別 × サマータイム/冬時間")
    sub = (f"       {'件数':>5} {'勝率%':>6} {'損益(円)':>12} {'PF':>6}  ") * 2
    print(f"{'年':>5}  {'サマータイム':^36}  {'冬時間':^36}")
    print(sub); print("-"*len(sub))
    for yr in sorted(tf["year"].unique()):
        gd = tf_dst[tf_dst["year"]==yr]; gw = tf_win[tf_win["year"]==yr]
        sd = group_summary(gd); sw = group_summary(gw)
        yd = int(gd["yen"].sum()) if len(gd)>0 else 0
        yw = int(gw["yen"].sum()) if len(gw)>0 else 0
        print(f"  {yr}  {sd['n']:>5} {sd['win_rate']:>5.1f}% {yd:>12,} {sd['pf']:>6.3f}  "
              f"{sw['n']:>5} {sw['win_rate']:>5.1f}% {yw:>12,} {sw['pf']:>6.3f}")

    # ─ 時間帯別クロス集計 ─
    HOURS = sorted(s_strong_hours)
    print(f"\n■ 時間帯別 × サマータイム/冬時間 クロス集計")
    print(f"{'時間帯':>5}  {'サマータイム':^34}  {'冬時間':^34}  {'差(PF)':>8}")
    print(f"       {'件数':>5} {'勝率%':>6} {'損益(円)':>11} {'PF':>6}  "
          f"{'件数':>5} {'勝率%':>6} {'損益(円)':>11} {'PF':>6}")
    print("-"*86)
    for h in HOURS:
        gd = tf_dst[tf_dst["signal_hour"]==h]
        gw = tf_win[tf_win["signal_hour"]==h]
        sd = group_summary(gd); sw = group_summary(gw)
        yd = int(gd["yen"].sum()) if len(gd)>0 else 0
        yw = int(gw["yen"].sum()) if len(gw)>0 else 0
        dpf = sd["pf"] - sw["pf"]
        print(f"  {h:>3}時  {sd['n']:>5} {sd['win_rate']:>5.1f}% {yd:>11,} {sd['pf']:>6.3f}  "
              f"{sw['n']:>5} {sw['win_rate']:>5.1f}% {yw:>11,} {sw['pf']:>6.3f}  {dpf:>+8.3f}")
    print("-"*86)
    print(f"  {'合計':>3}   {s_dst['n']:>5} {s_dst['win_rate']:>5.1f}% "
          f"{int(tf_dst['yen'].sum()):>11,} {s_dst['pf']:>6.3f}  "
          f"{s_win['n']:>5} {s_win['win_rate']:>5.1f}% "
          f"{int(tf_win['yen'].sum()):>11,} {s_win['pf']:>6.3f}")

    # ─ 月別 ─
    print(f"\n■ 月別 × サマータイム/冬時間")
    print(f"{'月':>3}  {'サマータイム':^36}  {'冬時間':^36}")
    print(f"       {'件数':>5} {'勝率%':>6} {'損益(円)':>12} {'PF':>6}  "
          f"{'件数':>5} {'勝率%':>6} {'損益(円)':>12} {'PF':>6}")
    print("-"*86)
    for mo in range(1, 13):
        if mo in EXCLUDE_MONTHS:
            print(f"  {mo:>2}月  {'(除外月)':^80}"); continue
        gd = tf_dst[tf_dst["month"]==mo]; gw = tf_win[tf_win["month"]==mo]
        if len(gd)==0 and len(gw)==0:
            print(f"  {mo:>2}月  {'---':^80}"); continue
        sd = group_summary(gd); sw = group_summary(gw)
        yd = int(gd["yen"].sum()) if len(gd)>0 else 0
        yw = int(gw["yen"].sum()) if len(gw)>0 else 0
        print(f"  {mo:>2}月  {sd['n']:>5} {sd['win_rate']:>5.1f}% {yd:>12,} {sd['pf']:>6.3f}  "
              f"{sw['n']:>5} {sw['win_rate']:>5.1f}% {yw:>12,} {sw['pf']:>6.3f}")
    print()

    # =========================================
    # 【DST最適化】時間帯フィルター ビフォーアフター比較
    # =========================================
    print("\n" + "="*80)
    print("【DST最適化】系統③ 時間帯フィルター ビフォーアフター比較")
    print(f"  MA短{S_MAS}, MA中{S_MAM}, SL{S_SL}, TP{S_TP}, short, 手数料{COMMISSION_PT}pt")
    print("="*80)

    # DST判定をトレードに付与（signal_dt ベース）
    def _is_dst(dt):
        for start, end in dst_periods:
            if pd.Timestamp(start) <= dt <= pd.Timestamp(end):
                return True
        return False

    tf["is_dst"] = pd.to_datetime(tf["signal_dt"]).apply(_is_dst)

    BASE_H = {5,8,9,12,14,15,19,20,21,22,23}

    def apply_dst_pattern(t, dst_h, win_h):
        mask = (
            (t["is_dst"]  & t["signal_hour"].isin(dst_h)) |
            (~t["is_dst"] & t["signal_hour"].isin(win_h))
        )
        return t[mask].reset_index(drop=True)

    opt_patterns = [
        ("①現状",           BASE_H,                  BASE_H),
        ("②9時除外",        BASE_H - {9},            BASE_H - {9}),
        ("③②+冬14除外",    BASE_H - {9},            BASE_H - {9, 14}),
        ("④③+DST21除外",   BASE_H - {9, 21},        BASE_H - {9, 14}),
    ]

    print(f"\n■ パターン別成績比較")
    print(f"{'パターン':<16} {'件数':>7} {'勝率%':>7} {'損益(円)':>13} {'期待値(pt)':>10} {'PF':>7}")
    print("-"*65)
    opt_results = {}
    for label, dh, wh in opt_patterns:
        t = apply_dst_pattern(tf, dh, wh)
        t = t.copy(); t["yen"] = t["pnl"] * PT_PER_YEN
        s = group_summary(t)
        opt_results[label] = (t, s)
        print(f"  {label:<14} {s['n']:>7,} {s['win_rate']:>6.1f}% "
              f"{int(t['yen'].sum()):>13,} {s['ev']:>10.2f} {s['pf']:>7.3f}")

    print(f"\n■ DST/冬時間 内訳")
    print(f"{'パターン':<16} {'DST件数':>7} {'DST損益(円)':>13} {'DST PF':>8}  "
          f"{'冬時間件数':>8} {'冬時間損益(円)':>14} {'冬PF':>8}")
    print("-"*80)
    for label, dh, wh in opt_patterns:
        t, _ = opt_results[label]
        td = t[t["is_dst"]]; tw = t[~t["is_dst"]]
        sd = group_summary(td); sw = group_summary(tw)
        print(f"  {label:<14} {sd['n']:>7,} {int(td['yen'].sum()):>13,} {sd['pf']:>8.3f}  "
              f"{sw['n']:>8,} {int(tw['yen'].sum()):>14,} {sw['pf']:>8.3f}")

    # ─ ④の詳細 ─
    t4, s4 = opt_results["④③+DST21除外"]
    t1, s1 = opt_results["①現状"]
    t4["year"]  = pd.to_datetime(t4["datetime"]).dt.year
    t4["month"] = pd.to_datetime(t4["datetime"]).dt.month

    print(f"\n{'='*80}")
    print(f"【④ 詳細】DST: [5,8,12,14,15,19,20,22,23]  冬時間: [5,8,12,15,19,20,21,22,23]")
    print(f"{'='*80}")

    # 年別
    print(f"\n■ 年別成績（①現状 vs ④最適化）")
    sub = f"       {'件数':>5} {'勝率%':>6} {'損益(円)':>12} {'PF':>6}  " * 2
    print(f"{'年':>5}  {'①現状':^36}  {'④最適化':^36}")
    print(sub); print("-"*len(sub))
    for yr in sorted(t4["year"].unique()):
        g1 = t1[t1["year"]==yr] if "year" in t1.columns else \
             t1[pd.to_datetime(t1["datetime"]).dt.year==yr]
        g4 = t4[t4["year"]==yr]
        s1y = group_summary(g1); s4y = group_summary(g4)
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
        s = group_summary(g)
        print(f"  {mo:>2}月  {s['n']:>6,} {s['win_rate']:>6.1f}% "
              f"{int(g['yen'].sum()):>13,} {s['ev']:>10.2f} {s['pf']:>7.3f}")
    print("-"*52)
    print(f"  {'合計':>3}  {s4['n']:>6,} {s4['win_rate']:>6.1f}% "
          f"{int(t4['yen'].sum()):>13,} {s4['ev']:>10.2f} {s4['pf']:>7.3f}")

    # 時間帯別（④）
    HOURS_DST4 = sorted({5,8,12,14,15,19,20,22,23})
    HOURS_WIN4 = sorted({5,8,12,15,19,20,21,22,23})
    ALL_H4 = sorted(set(HOURS_DST4) | set(HOURS_WIN4))
    print(f"\n■ 時間帯別成績（④最適化）")
    print(f"{'時間帯':>5}  {'全体':^30}  {'DST':^28}  {'冬時間':^28}")
    print(f"       {'件数':>5} {'損益(円)':>11} {'PF':>6}  "
          f"{'件数':>5} {'損益(円)':>10} {'PF':>6}  "
          f"{'件数':>5} {'損益(円)':>10} {'PF':>6}")
    print("-"*90)
    for h in ALL_H4:
        ga = t4[t4["signal_hour"]==h]
        gd = ga[ga["is_dst"]]; gw = ga[~ga["is_dst"]]
        sa = group_summary(ga); sd = group_summary(gd); sw = group_summary(gw)
        ya = int(ga["yen"].sum()) if len(ga)>0 else 0
        yd = int(gd["yen"].sum()) if len(gd)>0 else 0
        yw = int(gw["yen"].sum()) if len(gw)>0 else 0
        dm = "●" if h in HOURS_DST4 else "  "
        wm = "●" if h in HOURS_WIN4 else "  "
        print(f"  {h:>3}時  {sa['n']:>5} {ya:>11,} {sa['pf']:>6.3f}  "
              f"{dm}{sd['n']:>4} {yd:>10,} {sd['pf']:>6.3f}  "
              f"{wm}{sw['n']:>4} {yw:>10,} {sw['pf']:>6.3f}")
    print("-"*90)
    print(f"  {'合計':>3}   {s4['n']:>5} {int(t4['yen'].sum()):>11,} {s4['pf']:>6.3f}")
    print("\n※ ●=当該期間で有効な時間帯")
    print()

    # =========================================
    # 【系統③ 最終確定版パターン⑤】年×月クロス集計
    # DST時間帯: [5,8,12,14,15,19,20,22,23]
    # 冬時間帯:  [5,12,15,19,20,21,22,23]
    # CPI除外: 発表前30分〜後60分
    # 手数料: 2.2pt（22円）込み / SL60 / TP240
    # =========================================
    print("\n" + "="*80)
    print("【系統③ 最終確定版パターン⑤】年×月クロス集計")
    print("  DST時間帯: [5,8,12,14,15,19,20,22,23]")
    print("  冬時間帯:  [5,12,15,19,20,21,22,23]（冬の8時除外）")
    print("  CPI除外: 発表30分前〜60分後 / 手数料2.2pt / SL60 / TP240")
    print("="*80)

    # ── パターン⑤ トレードセット構築 ──
    # tf は系統③確定トレード（DST判定付き・CPI未除外・時間帯はBASE_H）
    # CPI除外
    cpi_df_p5 = event_df[event_df["indicator"] == "米CPI"]
    cpi_mask_p5 = build_event_mask(tf["signal_dt"], cpi_df_p5, window_before=30, window_after=60)
    tf_p5 = tf[~cpi_mask_p5].copy().reset_index(drop=True)

    # DST対応時間帯フィルター（冬の8時を除外）
    DST5 = frozenset({5, 8, 12, 14, 15, 19, 20, 22, 23})
    WIN5 = frozenset({5, 12, 15, 19, 20, 21, 22, 23})
    p5_mask = (
        (tf_p5["is_dst"]  & tf_p5["signal_hour"].isin(DST5)) |
        (~tf_p5["is_dst"] & tf_p5["signal_hour"].isin(WIN5))
    )
    tf_p5 = tf_p5[p5_mask].copy().reset_index(drop=True)
    tf_p5["yen"]   = tf_p5["pnl"] * PT_PER_YEN
    tf_p5["year"]  = pd.to_datetime(tf_p5["datetime"]).dt.year
    tf_p5["month"] = pd.to_datetime(tf_p5["datetime"]).dt.month

    n_cpi_excl = cpi_mask_p5.sum()
    print(f"\n  CPI除外: {n_cpi_excl}件 → 残り {len(tf_p5)}件")

    YEARS5   = sorted(tf_p5["year"].unique())
    MONTHS_A = [m for m in range(1, 13) if m not in EXCLUDE_MONTHS]
    CW5 = 20  # セル幅

    def _s5(grp):
        """(n, 勝率%, 損益円, PF) のセル文字列"""
        n = len(grp)
        if n == 0:
            return "---"
        wr  = (grp["pnl"] > 0).sum() / n * 100
        yn  = int(grp["yen"].sum())
        pf_v = grp["pnl"].clip(lower=0).sum()
        lo_v = abs(grp["pnl"].clip(upper=0).sum())
        pf  = pf_v / lo_v if lo_v > 0 else 0.0
        return f"{n}/{wr:.0f}%/{yn:+,}/{pf:.3f}"

    # ── 損益（円）ピボット ──
    print(f"\n■ 年×月 損益（円）")
    pivot_yen = tf_p5.pivot_table(
        values="yen", index="year", columns="month", aggfunc="sum", fill_value=0
    )
    # 除外月を列に追加（0埋め）
    for mo in range(1, 13):
        if mo not in pivot_yen.columns:
            pivot_yen[mo] = 0
    pivot_yen = pivot_yen[[c for c in range(1, 13)]].copy()

    hdr_yen = f"{'年':>5}  " + "  ".join(f"{m:>2}月".rjust(9) for m in range(1, 13)) + f"  {'年計':>10}"
    print(hdr_yen)
    print("-" * len(hdr_yen))
    for yr in YEARS5:
        if yr not in pivot_yen.index:
            continue
        row_vals = []
        for mo in range(1, 13):
            if mo in EXCLUDE_MONTHS:
                row_vals.append(f"{'(除外)':>9}")
            else:
                v = int(pivot_yen.loc[yr, mo])
                row_vals.append(f"{v:>9,}")
        yr_total = int(tf_p5[tf_p5["year"]==yr]["yen"].sum())
        print(f"  {yr}  " + "  ".join(row_vals) + f"  {yr_total:>10,}")
    print("-" * len(hdr_yen))
    tot_vals = []
    for mo in range(1, 13):
        if mo in EXCLUDE_MONTHS:
            tot_vals.append(f"{'(除外)':>9}")
        else:
            v = int(tf_p5[tf_p5["month"]==mo]["yen"].sum())
            tot_vals.append(f"{v:>9,}")
    grand_total = int(tf_p5["yen"].sum())
    print(f"  {'合計':>4}  " + "  ".join(tot_vals) + f"  {grand_total:>10,}")

    # ── 件数ピボット ──
    print(f"\n■ 年×月 件数")
    pivot_n = tf_p5.pivot_table(
        values="pnl", index="year", columns="month", aggfunc="count", fill_value=0
    )
    for mo in range(1, 13):
        if mo not in pivot_n.columns:
            pivot_n[mo] = 0
    pivot_n = pivot_n[[c for c in range(1, 13)]].copy()

    hdr_n = f"{'年':>5}  " + "  ".join(f"{m:>2}月".rjust(6) for m in range(1, 13)) + f"  {'年計':>6}"
    print(hdr_n)
    print("-" * len(hdr_n))
    for yr in YEARS5:
        if yr not in pivot_n.index:
            continue
        row_vals = []
        for mo in range(1, 13):
            if mo in EXCLUDE_MONTHS:
                row_vals.append(f"{'(除)':>6}")
            else:
                row_vals.append(f"{int(pivot_n.loc[yr, mo]):>6,}")
        yr_n = len(tf_p5[tf_p5["year"]==yr])
        print(f"  {yr}  " + "  ".join(row_vals) + f"  {yr_n:>6,}")
    print("-" * len(hdr_n))
    tot_n_vals = []
    for mo in range(1, 13):
        if mo in EXCLUDE_MONTHS:
            tot_n_vals.append(f"{'(除)':>6}")
        else:
            tot_n_vals.append(f"{len(tf_p5[tf_p5['month']==mo]):>6,}")
    print(f"  {'合計':>4}  " + "  ".join(tot_n_vals) + f"  {len(tf_p5):>6,}")

    # ── 件数/勝率%/損益円/PF クロス集計（前半・後半分割）──
    print(f"\n■ 詳細クロス集計（件数/勝率%/損益円/PF）")
    for half_label, mrange in [("上半期（1〜6月）", range(1, 7)),
                                ("下半期（8〜12月、7・11除外）", [8, 9, 10, 12])]:
        hdr = f"{'年':>4}  " + "  ".join(f"{m}月".center(CW5) for m in mrange) \
              + f"  {'半期計'.center(CW5)}"
        sep = "-" * len(hdr)
        print(f"\n{half_label}")
        print(sep); print(hdr); print(sep)

        mo_acc = {m: {"n": 0, "win": 0, "yen": 0.0, "pos": 0.0, "neg": 0.0} for m in mrange}
        for yr in YEARS5:
            cells = []
            h_n = 0; h_win = 0; h_yen = 0.0; h_pos = 0.0; h_neg = 0.0
            for mo in mrange:
                g = tf_p5[(tf_p5["year"]==yr) & (tf_p5["month"]==mo)]
                cells.append(_s5(g).center(CW5))
                n = len(g)
                if n > 0:
                    mo_acc[mo]["n"]   += n
                    mo_acc[mo]["win"] += int((g["pnl"] > 0).sum())
                    mo_acc[mo]["yen"] += float(g["yen"].sum())
                    mo_acc[mo]["pos"] += float(g["pnl"].clip(lower=0).sum())
                    mo_acc[mo]["neg"] += float(abs(g["pnl"].clip(upper=0).sum()))
                    h_n   += n
                    h_win += int((g["pnl"] > 0).sum())
                    h_yen += float(g["yen"].sum())
                    h_pos += float(g["pnl"].clip(lower=0).sum())
                    h_neg += float(abs(g["pnl"].clip(upper=0).sum()))
            if h_n > 0:
                h_pf  = h_pos / h_neg if h_neg > 0 else 0.0
                h_cell = f"{h_n}/{h_win/h_n*100:.0f}%/{int(h_yen):+,}/{h_pf:.3f}"
            else:
                h_cell = "---"
            print(f"{yr}  " + "  ".join(cells) + f"  {h_cell.center(CW5)}")
        print(sep)
        tot_cells = []
        tt_n = 0; tt_win = 0; tt_yen = 0.0; tt_pos = 0.0; tt_neg = 0.0
        for mo in mrange:
            md = mo_acc[mo]
            if md["n"] == 0:
                tot_cells.append("---".center(CW5))
            else:
                wr  = md["win"] / md["n"] * 100
                pf  = md["pos"] / md["neg"] if md["neg"] > 0 else 0.0
                tot_cells.append(f"{md['n']}/{wr:.0f}%/{int(md['yen']):+,}/{pf:.3f}".center(CW5))
                tt_n   += md["n"];  tt_win  += md["win"]
                tt_yen += md["yen"]; tt_pos += md["pos"]; tt_neg += md["neg"]
        tt_pf   = tt_pos / tt_neg if tt_neg > 0 else 0.0
        tt_cell = f"{tt_n}/{tt_win/tt_n*100:.0f}%/{int(tt_yen):+,}/{tt_pf:.3f}" if tt_n > 0 else "---"
        print(f"{'合計':>4}  " + "  ".join(tot_cells) + f"  {tt_cell.center(CW5)}")

    # ── 年別サマリー ──
    p5_all = calc_summary(tf_p5)
    print(f"\n{'='*60}")
    print("■ 年別サマリー（パターン⑤）")
    print(f"{'年':>5}  {'件数':>7} {'勝率%':>7} {'損益(円)':>13} {'期待値(pt)':>10} {'PF':>7}")
    print("-"*52)
    for yr in YEARS5:
        g = tf_p5[tf_p5["year"]==yr]
        s = calc_summary(g)
        print(f"  {yr}  {s['n']:>7,} {s['win_rate']:>6.1f}% "
              f"{int(g['yen'].sum()):>13,} {s['ev']:>10.2f} {s['pf']:>7.3f}")
    print("-"*52)
    print(f"  {'合計':>4}  {p5_all['n']:>7,} {p5_all['win_rate']:>6.1f}% "
          f"{int(tf_p5['yen'].sum()):>13,} {p5_all['ev']:>10.2f} {p5_all['pf']:>7.3f}")

    # ── 月別サマリー（安定性評価）──
    print(f"\n{'='*72}")
    print("■ 月別サマリー（全年合計・安定性評価）")
    print(f"{'月':>3}  {'件数':>6} {'勝率%':>7} {'損益(円)':>13} {'PF':>7}  安定性")
    print("-"*62)
    for mo in range(1, 13):
        if mo in EXCLUDE_MONTHS:
            print(f"  {mo:>2}月  {'(除外月)':^54}"); continue
        g_all = tf_p5[tf_p5["month"]==mo]
        if len(g_all) == 0:
            print(f"  {mo:>2}月  {'---':^54}"); continue
        s = calc_summary(g_all)
        yen_all = int(g_all["yen"].sum())
        neg_yrs = [yr for yr in YEARS5
                   if int(tf_p5[(tf_p5["year"]==yr) & (tf_p5["month"]==mo)]["yen"].sum()) < 0]
        stability = "OK（全年プラス）" if not neg_yrs \
                    else f"NG（{','.join(str(y) for y in neg_yrs)}マイナス）"
        print(f"  {mo:>2}月  {s['n']:>6,} {s['win_rate']:>6.1f}% "
              f"{yen_all:>13,} {s['pf']:>7.3f}  {stability}")
    print("-"*62)
    print(f"  {'合計':>3}  {p5_all['n']:>6,} {p5_all['win_rate']:>6.1f}% "
          f"{int(tf_p5['yen'].sum()):>13,} {p5_all['pf']:>7.3f}")

    # CSV保存
    out_yen = tf_p5.pivot_table(
        values="yen", index="year", columns="month", aggfunc="sum", fill_value=0
    )
    out_n = tf_p5.pivot_table(
        values="pnl", index="year", columns="month", aggfunc="count", fill_value=0
    )
    out_yen.columns = [f"{int(c)}月_損益円" for c in out_yen.columns]
    out_n.columns   = [f"{int(c)}月_件数"   for c in out_n.columns]
    out_yen["年間損益円"] = out_yen.sum(axis=1)
    out_n["年間件数"]    = out_n.sum(axis=1)
    out_csv = pd.concat([out_yen, out_n], axis=1)
    csv_path = Path(r"C:\kabu_trade") / "s3_pattern5_cross.csv"
    out_csv.to_csv(csv_path, encoding="utf-8-sig")
    print(f"\n  → CSV保存: {csv_path}")
    print()

    # =========================================
    # 【系統③パターン⑤】日内最大ドローダウン分析
    # =========================================
    print("\n" + "="*80)
    print("【系統③パターン⑤】日内最大ドローダウン分析")
    print("  エントリー時刻順に累計損益を追跡し、当日高値からの最大下落を計算")
    print("="*80)

    def calc_intraday_dd(group: pd.DataFrame) -> dict:
        """1日分のトレード群から日内最大DDを計算する"""
        t = group.sort_values("datetime").reset_index(drop=True)
        cum = t["yen"].cumsum().values
        # 日内DD: cumの各時点での peak - current の最大
        peak    = cum[0]
        max_dd  = 0.0
        for v in cum:
            if v > peak:
                peak = v
            dd = v - peak          # 負値（下落） or 0
            if dd < max_dd:
                max_dd = dd
        return {
            "件数":         len(t),
            "日次損益円":   int(t["yen"].sum()),
            "日内最大DD円": int(max_dd),   # 0 or 負値
        }

    # 日付列を追加（エントリー日 = datetime の日付）
    tf_p5["date"] = pd.to_datetime(tf_p5["datetime"]).dt.date

    # 日別集計
    daily_rows = []
    for date, grp in tf_p5.groupby("date"):
        r = calc_intraday_dd(grp)
        r["日付"] = date
        daily_rows.append(r)

    daily_df = pd.DataFrame(daily_rows)[["日付", "件数", "日次損益円", "日内最大DD円"]]
    daily_df = daily_df.sort_values("日内最大DD円").reset_index(drop=True)  # 大きい順（負値）

    # CSV保存
    dd_csv_path = Path(r"C:\kabu_trade") / "s3_daily_dd.csv"
    daily_df.to_csv(dd_csv_path, index=False, encoding="utf-8-sig")
    print(f"\n  日別サマリー CSV保存: {dd_csv_path}  ({len(daily_df)}日分)")

    # ── ワースト30日 ──
    print(f"\n■ ワースト30日（日内最大DD大きい順）")
    print(f"{'日付':<13} {'件数':>5} {'日次損益(円)':>13} {'日内最大DD(円)':>16}")
    print("-"*52)
    for _, row in daily_df.head(30).iterrows():
        print(f"  {str(row['日付']):<11} {int(row['件数']):>5,} "
              f"{int(row['日次損益円']):>13,} {int(row['日内最大DD円']):>16,}")

    # ── 分布集計 ──
    dd_vals = daily_df["日内最大DD円"].values   # すべて 0 or 負値
    n_days  = len(daily_df)

    bins = [
        ("DD なし（0円）",        dd_vals == 0),
        ("DD  -1万円以内",        (dd_vals < 0) & (dd_vals >= -10_000)),
        ("DD  -2万円以内",        (dd_vals < -10_000) & (dd_vals >= -20_000)),
        ("DD  -3万円以内",        (dd_vals < -20_000) & (dd_vals >= -30_000)),
        ("DD  -5万円以内",        (dd_vals < -30_000) & (dd_vals >= -50_000)),
        ("DD  -5万円超",          dd_vals < -50_000),
    ]

    print(f"\n■ 日内最大DD 分布（全{n_days}日）")
    print(f"{'区分':<20} {'日数':>6} {'割合%':>8}  {'平均DD(円)':>12}  {'最大DD(円)':>12}")
    print("-"*66)
    for label, mask in bins:
        cnt  = int(mask.sum())
        pct  = cnt / n_days * 100 if n_days > 0 else 0.0
        vals = dd_vals[mask]
        avg  = int(vals.mean()) if cnt > 0 else 0
        worst = int(vals.min()) if cnt > 0 else 0
        print(f"  {label:<18} {cnt:>6,} {pct:>7.1f}%  {avg:>12,}  {worst:>12,}")
    print("-"*66)
    print(f"  {'合計':<18} {n_days:>6,} {'100.0%':>8}")

    # ── 月別平均DD ──
    daily_df["月"] = pd.to_datetime(daily_df["日付"]).dt.month
    daily_df["年"] = pd.to_datetime(daily_df["日付"]).dt.year

    print(f"\n■ 月別 平均日内最大DD（全年合算）")
    print(f"{'月':>3}  {'対象日数':>8} {'平均DD(円)':>13} {'最大DD(円)':>13} {'DD発生率%':>10}")
    print("-"*55)
    for mo in MONTHS_A:
        gm = daily_df[daily_df["月"]==mo]
        if len(gm) == 0:
            continue
        avg_dd  = int(gm["日内最大DD円"].mean())
        worst_dd = int(gm["日内最大DD円"].min())
        rate    = (gm["日内最大DD円"] < 0).sum() / len(gm) * 100
        print(f"  {mo:>2}月  {len(gm):>8,} {avg_dd:>13,} {worst_dd:>13,} {rate:>9.1f}%")

    # ── 年別平均DD ──
    print(f"\n■ 年別 平均日内最大DD")
    print(f"{'年':>5}  {'対象日数':>8} {'平均DD(円)':>13} {'最大DD(円)':>13} {'DD発生率%':>10}")
    print("-"*55)
    for yr in YEARS5:
        gy = daily_df[daily_df["年"]==yr]
        if len(gy) == 0:
            continue
        avg_dd   = int(gy["日内最大DD円"].mean())
        worst_dd = int(gy["日内最大DD円"].min())
        rate     = (gy["日内最大DD円"] < 0).sum() / len(gy) * 100
        print(f"  {yr}  {len(gy):>8,} {avg_dd:>13,} {worst_dd:>13,} {rate:>9.1f}%")

    print()

    # =========================================
    # 【系統③パターン⑤】スリッページ耐性テスト
    # ショート: 約定価格 = 次足open - slip → PnL から slip×10円 を追加控除
    # =========================================
    print("\n" + "="*80)
    print("【系統③パターン⑤】スリッページ耐性テスト")
    print("  ショート: 約定価格 = 次足open - slip(pt)  → 1トレードあたり slip×10円 追加控除")
    print("  手数料2.2pt(22円)はそのまま維持")
    print("="*80)

    SLIP_LEVELS  = [0, 1, 2, 3, 5, 10]
    PT_TO_YEN    = 10   # 1pt = 10円

    def apply_slip(trades: pd.DataFrame, slip_pt: float) -> pd.DataFrame:
        """スリッページ分をPnLから差し引いたコピーを返す"""
        t = trades.copy()
        t["yen"] = t["yen"] - slip_pt * PT_TO_YEN   # 1トレードあたりの追加コスト
        t["pnl"] = t["pnl"] - slip_pt               # pt換算も更新
        return t

    # ── 内容①: スリッページ別サマリー ──
    print(f"\n■ スリッページ別 全体サマリー")
    print(f"{'スリッページ(pt)':>16} {'件数':>7} {'勝率%':>7} {'損益(円)':>13} {'期待値(pt)':>10} {'PF':>7}  {'vs 0pt':>8}")
    print("-"*72)

    slip_rows = []
    base_yen  = None
    for slip in SLIP_LEVELS:
        ts  = apply_slip(tf_p5, slip)
        s   = calc_summary(ts)
        yen = int(ts["yen"].sum())
        if base_yen is None:
            base_yen = yen
        diff_str = f"{yen - base_yen:+,}" if slip > 0 else "---"
        print(f"  {slip:>14}pt  {s['n']:>7,} {s['win_rate']:>6.1f}% "
              f"{yen:>13,} {s['ev']:>10.2f} {s['pf']:>7.3f}  {diff_str:>8}")
        slip_rows.append({
            "スリッページpt": slip,
            "件数": s["n"],
            "勝率%": round(s["win_rate"], 1),
            "損益円": yen,
            "期待値pt": round(s["ev"], 2),
            "PF": round(s["pf"], 3),
        })

    # CSV①
    slip_csv_path = Path(r"C:\kabu_trade") / "s3_slip_test.csv"
    pd.DataFrame(slip_rows).to_csv(slip_csv_path, index=False, encoding="utf-8-sig")
    print(f"\n  → CSV保存: {slip_csv_path}")

    # ── 年別×スリッページ ──
    print(f"\n■ 年別損益（円）× スリッページ")
    hdr = f"{'年':>5}  " + "  ".join(f"{s}pt".rjust(12) for s in SLIP_LEVELS)
    print(hdr); print("-"*len(hdr))
    for yr in YEARS5:
        row_str = f"  {yr}  "
        for slip in SLIP_LEVELS:
            ts = apply_slip(tf_p5[tf_p5["year"]==yr], slip)
            row_str += f"{int(ts['yen'].sum()):>12,}"
            if slip != SLIP_LEVELS[-1]:
                row_str += "  "
        print(row_str)
    print("-"*len(hdr))
    tot_str = f"  {'合計':>4}  "
    for slip in SLIP_LEVELS:
        ts  = apply_slip(tf_p5, slip)
        tot_str += f"{int(ts['yen'].sum()):>12,}"
        if slip != SLIP_LEVELS[-1]:
            tot_str += "  "
    print(tot_str)

    # ── 内容②: 時間帯×スリッページ耐性 ──
    SLIP_CROSS = [0, 2, 5, 10]
    ALL_HOURS5 = sorted(set(DST5) | set(WIN5))

    print(f"\n■ 時間帯 × スリッページ PF")
    hdr2 = (f"{'時間帯':>5}  {'件数':>6}  "
            + "  ".join(f"{'PF('+str(s)+'pt)':>10}" for s in SLIP_CROSS))
    print(hdr2); print("-"*len(hdr2))

    hour_slip_rows = []
    for h in ALL_HOURS5:
        thr = tf_p5[tf_p5["signal_hour"] == h]
        if len(thr) == 0:
            continue
        pf_vals = []
        for slip in SLIP_CROSS:
            ts = apply_slip(thr, slip)
            s  = calc_summary(ts)
            pf_vals.append(round(s["pf"], 3))
        line = f"  {h:>3}時  {len(thr):>6,}  "
        line += "  ".join(f"{v:>10.3f}" for v in pf_vals)
        print(line)
        hour_slip_rows.append({
            "時間帯": f"{h}時",
            "件数": len(thr),
            **{f"PF_{s}pt": v for s, v in zip(SLIP_CROSS, pf_vals)},
        })
    print("-"*len(hdr2))
    # 合計行
    tot_pf_vals = []
    for slip in SLIP_CROSS:
        ts = apply_slip(tf_p5, slip)
        s  = calc_summary(ts)
        tot_pf_vals.append(round(s["pf"], 3))
    print(f"  {'合計':>3}   {len(tf_p5):>6,}  " + "  ".join(f"{v:>10.3f}" for v in tot_pf_vals))

    # CSV②
    slip_hour_csv = Path(r"C:\kabu_trade") / "s3_slip_by_hour.csv"
    pd.DataFrame(hour_slip_rows).to_csv(slip_hour_csv, index=False, encoding="utf-8-sig")
    print(f"\n  → CSV保存: {slip_hour_csv}")

    # ── PF維持ラインの表示 ──
    print(f"\n■ PF>1.0 を維持できる最大スリッページ")
    # 全体
    for slip in SLIP_LEVELS:
        ts = apply_slip(tf_p5, slip)
        s  = calc_summary(ts)
        if s["pf"] < 1.0:
            print(f"  全体: {slip}pt でPF<1.0 に転落（直前 {SLIP_LEVELS[SLIP_LEVELS.index(slip)-1]}pt まで PF>=1.0）")
            break
    else:
        print(f"  全体: {SLIP_LEVELS[-1]}pt までPF>=1.0 を維持")

    # 時間帯別
    print(f"\n  時間帯別 PF<1.0 転落スリッページ:")
    for h in ALL_HOURS5:
        thr = tf_p5[tf_p5["signal_hour"] == h]
        if len(thr) == 0:
            continue
        limit = None
        for slip in SLIP_LEVELS:
            ts = apply_slip(thr, slip)
            s  = calc_summary(ts)
            if s["pf"] < 1.0:
                limit = slip
                break
        if limit is None:
            print(f"    {h:>2}時: {SLIP_LEVELS[-1]}pt まで維持")
        else:
            prev = SLIP_LEVELS[SLIP_LEVELS.index(limit)-1] if SLIP_LEVELS.index(limit) > 0 else 0
            print(f"    {h:>2}時: {limit}pt で転落（{prev}pt まで維持）")
    print()


    # =========================================
    # 【系統③ セッション境界強制決済 比較】
    # FORCE_CLOSE_SESSION: 強制決済あり / なし × 全期間 / DST / 冬時間
    # =========================================
    print("\n\n" + "="*80)
    print("【系統③ セッション境界強制決済 比較（6パターン）】")
    print("  強制決済時刻: 15:40（日中終了）/ 06:00（夜間終了）/ 23:50（深夜強制）")
    print(f"  TP/SL到達が優先。到達しなかった場合のみセッション境界でclose決済。")
    print(f"  ベース条件: CPI除外 + DST最適時間帯(DST5/WIN5) + 除外月{EXCLUDE_MONTHS} + 手数料{COMMISSION_PT}pt")
    print("="*80)

    # ── 強制決済あり版 パターン⑤パイプラインと同一構成 ──
    tf_fc_raw = run_short_filtered(
        month_exclude=EXCLUDE_MONTHS,
        hour_f=s_strong_hours,
        weekday_f=s_strong_weekdays,
        com=COMMISSION_PT,
        force_close_session=True,
    )
    tf_fc_raw["is_dst"] = pd.to_datetime(tf_fc_raw["signal_dt"]).apply(_is_dst)
    cpi_mask_fc = build_event_mask(
        tf_fc_raw["signal_dt"], cpi_df_p5, window_before=30, window_after=60
    )
    tf_fc_raw = tf_fc_raw[~cpi_mask_fc].copy().reset_index(drop=True)
    fc_dst_mask = (
        (tf_fc_raw["is_dst"]  & tf_fc_raw["signal_hour"].isin(DST5)) |
        (~tf_fc_raw["is_dst"] & tf_fc_raw["signal_hour"].isin(WIN5))
    )
    tf_fc = tf_fc_raw[fc_dst_mask].copy().reset_index(drop=True)
    tf_fc["yen"]   = tf_fc["pnl"] * PT_PER_YEN
    tf_fc["year"]  = pd.to_datetime(tf_fc["datetime"]).dt.year
    tf_fc["month"] = pd.to_datetime(tf_fc["datetime"]).dt.month

    # ── 期間別に分割（is_dst は各dfに既存） ──
    tf_p5_dst = tf_p5[tf_p5["is_dst"]].copy()
    tf_p5_win = tf_p5[~tf_p5["is_dst"]].copy()
    tf_fc_dst  = tf_fc[tf_fc["is_dst"]].copy()
    tf_fc_win  = tf_fc[~tf_fc["is_dst"]].copy()

    # ── 平均保有時間ヘルパー ──
    def avg_hold_hours(t):
        if len(t) == 0:
            return 0.0
        h = (pd.to_datetime(t["exit_datetime"]) - pd.to_datetime(t["datetime"])).dt.total_seconds() / 3600
        return float(h.mean())

    # ── 決済種別内訳ヘルパー ──
    def result_breakdown(t):
        if len(t) == 0:
            return "TP=0 SL=0 SESSION=0 TIME=0"
        rc = t["result"].value_counts()
        return (f"TP={rc.get('TP',0)} SL={rc.get('SL',0)} "
                f"SESSION={rc.get('SESSION',0)} TIME={rc.get('TIME',0)}")

    # ── 6パターン定義 ──
    patterns_6 = [
        ("①全期間・強制決済なし",    tf_p5,     False),
        ("②全期間・強制決済あり",    tf_fc,     True),
        ("③DST期間・強制決済なし",   tf_p5_dst, False),
        ("④DST期間・強制決済あり",   tf_fc_dst, True),
        ("⑤冬時間・強制決済なし",    tf_p5_win, False),
        ("⑥冬時間・強制決済あり",    tf_fc_win, True),
    ]

    # ── サマリー表 ──
    print(f"\n■ 6パターン サマリー比較")
    hdr6 = (f"{'パターン':<22} {'件数':>6} {'勝率%':>7} {'損益(円)':>13} "
            f"{'期待値(pt)':>10} {'PF':>7} {'平均保有(h)':>11}")
    print(hdr6)
    print("-" * len(hdr6))
    for label, t, _ in patterns_6:
        if len(t) == 0:
            print(f"  {label:<20}  データなし")
            continue
        s  = calc_summary(t)
        yn = int(t["yen"].sum())
        ah = avg_hold_hours(t)
        print(f"  {label:<20} {s['n']:>6,} {s['win_rate']:>6.1f}% {yn:>13,} "
              f"{s['ev']:>10.2f} {s['pf']:>7.3f} {ah:>10.2f}h")

    # ── 決済種別内訳 ──
    print(f"\n■ 決済種別内訳（強制決済ありのSESSION件数を確認）")
    for label, t, _ in patterns_6:
        bd = result_breakdown(t)
        print(f"  {label:<22}: {bd}")

    # ── 年別損益 × 3ペア比較 ──
    all_years_6 = sorted(set(
        list(tf_p5["year"].unique()) + list(tf_fc["year"].unique())
    ))
    print(f"\n■ 年別損益（円）比較")

    pair_names = [
        ("全期間",   tf_p5,     tf_fc),
        ("DST期間",  tf_p5_dst, tf_fc_dst),
        ("冬時間",   tf_p5_win, tf_fc_win),
    ]
    for period_label, t_no, t_fc in pair_names:
        print(f"\n  ─ {period_label}（強制決済なし vs あり）─")
        sub_y = (f"  {'年':>5}  {'件数':>5} {'損益(円)':>12} {'勝率%':>6} {'PF':>7}  "
                 f"{'件数':>5} {'損益(円)':>12} {'勝率%':>6} {'PF':>7}  {'差分(円)':>12}")
        print(f"  {'':>5}  {'─ 強制決済なし ─':^34}  {'─ 強制決済あり ─':^34}  {'差分':>12}")
        print(sub_y)
        print("  " + "-" * (len(sub_y) - 2))
        tot_no_yen = 0; tot_fc_yen = 0
        for yr in all_years_6:
            gn = t_no[t_no["year"] == yr] if len(t_no) > 0 else pd.DataFrame()
            gf = t_fc[t_fc["year"] == yr] if len(t_fc) > 0 else pd.DataFrame()
            sn = calc_summary(gn); sf = calc_summary(gf)
            yn = int(gn["yen"].sum()) if len(gn) > 0 else 0
            yf = int(gf["yen"].sum()) if len(gf) > 0 else 0
            diff = yf - yn
            tot_no_yen += yn; tot_fc_yen += yf
            print(f"  {yr}  {sn['n']:>5} {yn:>12,} {sn['win_rate']:>5.1f}% {sn['pf']:>7.3f}  "
                  f"{sf['n']:>5} {yf:>12,} {sf['win_rate']:>5.1f}% {sf['pf']:>7.3f}  {diff:>+12,}")
        sn_all = calc_summary(t_no); sf_all = calc_summary(t_fc)
        diff_total = tot_fc_yen - tot_no_yen
        print("  " + "-" * (len(sub_y) - 2))
        print(f"  {'合計':>5}  {sn_all['n']:>5} {tot_no_yen:>12,} {sn_all['win_rate']:>5.1f}% {sn_all['pf']:>7.3f}  "
              f"{sf_all['n']:>5} {tot_fc_yen:>12,} {sf_all['win_rate']:>5.1f}% {sf_all['pf']:>7.3f}  "
              f"{diff_total:>+12,}")

    # ── 月別損益 × 3ペア比較 ──
    print(f"\n■ 月別損益（円）比較")
    for period_label, t_no, t_fc in pair_names:
        t_no2 = t_no.copy(); t_fc2 = t_fc.copy()
        if len(t_no2) > 0:
            t_no2["month"] = pd.to_datetime(t_no2["datetime"]).dt.month
        if len(t_fc2) > 0:
            t_fc2["month"] = pd.to_datetime(t_fc2["datetime"]).dt.month
        print(f"\n  ─ {period_label}（強制決済なし vs あり）─")
        print(f"  {'月':>3}  {'強制決済なし':>14} {'強制決済あり':>14} {'差分(円)':>12}")
        print("  " + "-" * 46)
        for mo in range(1, 13):
            if mo in EXCLUDE_MONTHS:
                print(f"  {mo:>2}月  {'(除外)':^44}")
                continue
            gn = t_no2[t_no2["month"] == mo] if len(t_no2) > 0 else pd.DataFrame()
            gf = t_fc2[t_fc2["month"] == mo] if len(t_fc2) > 0 else pd.DataFrame()
            yn = int(gn["yen"].sum()) if len(gn) > 0 else 0
            yf = int(gf["yen"].sum()) if len(gf) > 0 else 0
            diff = yf - yn
            nn = len(gn); nf = len(gf)
            print(f"  {mo:>2}月  {yn:>14,}({nn}件) {yf:>14,}({nf}件) {diff:>+12,}")
        yn_tot = int(t_no2["yen"].sum()) if len(t_no2) > 0 else 0
        yf_tot = int(t_fc2["yen"].sum()) if len(t_fc2) > 0 else 0
        print("  " + "-" * 46)
        print(f"  {'合計':>3}  {yn_tot:>14,}        {yf_tot:>14,}        {yf_tot-yn_tot:>+12,}")

    # ── 平均保有時間 詳細 ──
    print(f"\n■ 平均保有時間 詳細")
    print(f"  {'パターン':<22} {'平均保有(h)':>11} {'平均保有(min)':>13} {'最大(h)':>9} {'最小(min)':>10}")
    print("  " + "-" * 68)
    for label, t, _ in patterns_6:
        if len(t) == 0:
            print(f"  {label:<22}  データなし")
            continue
        hold_h = (pd.to_datetime(t["exit_datetime"]) - pd.to_datetime(t["datetime"])).dt.total_seconds() / 3600
        print(f"  {label:<22} {hold_h.mean():>10.2f}h {hold_h.mean()*60:>12.0f}分 "
              f"{hold_h.max():>8.1f}h {hold_h.min()*60:>9.0f}分")
    print()


if __name__ == "__main__":
    main()
