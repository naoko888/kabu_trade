# 系統①③合算 月次ドローダウン制限 バックテスト
# ==============================================
# 目的: 月次DD制限の適正値を探す（系統①③合算）
# データ: C:/kabu_trade/data/N225microf_20xx.xlsx + micro_5min.csv（存在時）
from pathlib import Path
from datetime import timedelta
import heapq

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
MICRO_CSV   = Path(r"C:\kabu_trade\micro_5min.csv")   # collect_micro.py 生成 CSV
ECON_CSV    = Path(r"C:\kabu_trade\economic_calendar.csv")
RESULT_CSV  = Path(r"C:\kabu_trade\s1s3_monthly_dd_result.csv")

SL        = 60     # ストップロス（pt）
TP        = 240    # テイクプロフィット（pt）
MAX_HOLD  = 120    # 最大保有バー数（タイムアウト）
TOUCH_PCT = 0.005  # MAタッチ判定 ±0.5%

COMMISSION_PT  = 2.2   # 往復手数料（pt）
PT_TO_YEN      = 10    # 1pt = ¥10（マイクロ先物）
COMMISSION_YEN = 22    # 往復手数料（円）

# 強制決済バーのhhmm（バー終了時刻）
SESSION_BOUNDARIES = frozenset({1540, 600, 2350})

# 月次DD制限の試行値（None = 制限なし）
DD_LIMITS = [-30_000, -40_000, -50_000, -60_000, -70_000, -80_000, None]

# =========================================
# 米国夏時間（DST）判定
# =========================================
_DST_PERIODS = [
    ("2023-03-12", "2023-11-05"),
    ("2024-03-10", "2024-11-03"),
    ("2025-03-09", "2025-11-02"),
    ("2026-03-08", "2026-11-01"),
]

def is_dst(dt) -> bool:
    ts = pd.Timestamp(dt)
    for start, end in _DST_PERIODS:
        if pd.Timestamp(start) <= ts <= pd.Timestamp(end):
            return True
    return False


# =========================================
# CPI フィルター
# =========================================
def load_cpi_events() -> pd.DataFrame:
    try:
        df = pd.read_csv(ECON_CSV)
        df["release_datetime_jst"] = pd.to_datetime(df["release_datetime_jst"])
        result = df[df["indicator"] == "米CPI"].reset_index(drop=True)
        print(f"[OK] CPIカレンダー読み込み: {len(result)} 件")
        return result
    except Exception as e:
        print(f"[WARN] CPIカレンダー読み込み失敗: {e} → CPI除外無効")
        return pd.DataFrame(columns=["indicator", "release_datetime_jst"])


def build_cpi_mask(dt_series: pd.Series, cpi_df: pd.DataFrame,
                   before_min: int = 30, after_min: int = 60) -> np.ndarray:
    """ベクトル化版 CPI ウィンドウ判定（is_cpi_window のループ版より高速）"""
    if len(cpi_df) == 0:
        return np.zeros(len(dt_series), dtype=bool)
    releases = cpi_df["release_datetime_jst"].values.astype("int64")
    sdt      = pd.to_datetime(dt_series).values.astype("int64")
    wb = int(pd.Timedelta(minutes=before_min).value)
    wa = int(pd.Timedelta(minutes=after_min).value)
    diff = sdt[:, None] - releases[None, :]         # shape (N, M)
    mask = (diff >= -wb) & (diff <= wa)
    return mask.any(axis=1)


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
    df = df.dropna(subset=["datetime", "open", "high", "low", "close", "volume"]).copy()
    return df[["datetime", "open", "high", "low", "close", "volume"]].sort_values("datetime")


def load_data() -> pd.DataFrame:
    dfs = []
    print("データ読み込み中...")
    for fname in EXCEL_FILES:
        p = DATA_DIR / fname
        if not p.exists():
            print(f"  スキップ（存在しない）: {p}")
            continue
        df = read_excel(p)
        print(f"  {fname}: {len(df)} 本")
        dfs.append(df)

    # collect_micro.py が生成する CSV があれば末尾に追加
    if MICRO_CSV.exists():
        try:
            df_csv = pd.read_csv(MICRO_CSV, index_col="datetime", parse_dates=True)
            df_csv.index.name = "datetime"
            df_csv = df_csv.reset_index()
            if df_csv["datetime"].dt.tz is not None:
                df_csv["datetime"] = (df_csv["datetime"]
                                      .dt.tz_convert("Asia/Tokyo")
                                      .dt.tz_localize(None))
            # CSV の datetime は bar START 時刻 → +5min で bar END に変換
            df_csv["datetime"] = df_csv["datetime"] + pd.Timedelta(minutes=5)
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
        raise FileNotFoundError("データファイルが1本も見つかりませんでした")

    df = pd.concat(dfs, ignore_index=True)
    df = (df.sort_values("datetime")
          .drop_duplicates(subset=["datetime"])
          .reset_index(drop=True))
    print(f"合計: {len(df)} 本  ({df['datetime'].min()} 〜 {df['datetime'].max()})\n")
    return df


# =========================================
# 指標計算
# =========================================
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ma9"]      = df["close"].rolling(9).mean()
    df["ma10"]     = df["close"].rolling(10).mean()
    ema_fast       = df["close"].ewm(span=12, adjust=False).mean()
    ema_slow       = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"]     = ema_fast - ema_slow
    df["macd_sig"] = df["macd"].ewm(span=9, adjust=False).mean()
    return df


# =========================================
# 系統①③ 生トレード生成（月次DD制限なし）
# =========================================
def generate_raw_trades(df: pd.DataFrame, cpi_df: pd.DataFrame) -> pd.DataFrame:
    """
    シグナルバー i でエントリー判定 → i+1 バーの open でエントリー。
    bar の datetime は END 時刻（Excel 標準）。
    複数同時保有 OK。手数料未控除。
    """
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
    arr_weekday = dts.dt.weekday.values   # 0=月 … 4=金
    arr_month   = dts.dt.month.values
    arr_hm      = arr_hour * 100 + arr_minute
    dt_list     = dts.to_list()

    # 系統③ CPI マスク（ベクトル化で事前計算）
    cpi_mask = build_cpi_mask(dts, cpi_df)  # True = CPI ウィンドウ

    # 系統③ DST 判定（各バーで計算）
    # DST の判定は日付のみ依存なので日付ごとにキャッシュ
    date_dst_cache: dict = {}
    def _is_dst_bar(i: int) -> bool:
        d = dt_list[i].date()
        if d not in date_dst_cache:
            date_dst_cache[d] = is_dst(dt_list[i])
        return date_dst_cache[d]

    n = len(df)
    trades = []

    for i in range(2, n - 1):
        ma9  = arr_ma9[i];   ma10  = arr_ma10[i]
        ma9p = arr_ma9[i-1]; ma10p = arr_ma10[i-1]
        ma9p2= arr_ma9[i-2]; ma10p2= arr_ma10[i-2]

        if np.isnan(ma9) or np.isnan(ma10) or np.isnan(ma9p) or np.isnan(ma10p) \
                or np.isnan(ma9p2) or np.isnan(ma10p2):
            continue
        if np.isnan(arr_macd[i]) or np.isnan(arr_msig[i]):
            continue

        hi  = arr_high[i];  lo   = arr_low[i]
        c1  = arr_close[i-1];  c2 = arr_close[i-2]
        hour   = arr_hour[i]
        wd     = arr_weekday[i]
        month  = arr_month[i]

        # ──────────────────────────────────────────
        # 系統① シグナル条件（long）
        # ──────────────────────────────────────────
        above_ma = (c2 > ma9p2 and c2 > ma10p2 and c1 > ma9p and c1 > ma10p)
        touch_lo = (abs(lo - ma9) / ma9 <= TOUCH_PCT or
                    abs(lo - ma10) / ma10 <= TOUCH_PCT)
        gc       = arr_macd[i] > arr_msig[i]

        if (above_ma and touch_lo and gc
                and wd in (0, 3)
                and hour in (18, 19, 20, 21, 22, 23)
                and month not in (3, 7)):
            ei = i + 1
            ep = arr_open[ei]
            entry_dt = dt_list[ei]

            pnl, rtype, exit_bar = None, None, ei
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

            trades.append({
                "system":      "①",
                "entry_dt":    entry_dt,
                "exit_dt":     dt_list[exit_bar],
                "signal_dt":   dt_list[i],
                "entry_price": ep,
                "exit_price":  (ep + TP if rtype == "TP" else
                                ep - SL if rtype == "SL" else
                                arr_close[exit_bar]),
                "pnl_pt":      pnl,
                "result":      rtype,
            })

        # ──────────────────────────────────────────
        # 系統③ シグナル条件（short）
        # ──────────────────────────────────────────
        below_ma = (c2 < ma9p2 and c2 < ma10p2 and c1 < ma9p and c1 < ma10p)
        touch_hi = (abs(hi - ma9) / ma9 <= TOUCH_PCT or
                    abs(hi - ma10) / ma10 <= TOUCH_PCT)
        dc       = arr_macd[i] < arr_msig[i]

        if (below_ma and touch_hi and dc
                and wd in (0, 2, 3, 4)
                and month not in (7, 11)):
            # DST 時間帯フィルター
            if _is_dst_bar(i):
                s3_hours = (5, 8, 12, 14, 15, 19, 20, 22, 23)
            else:
                s3_hours = (5, 12, 15, 19, 20, 21, 22, 23)

            if hour not in s3_hours:
                continue

            # CPI ウィンドウ除外
            if cpi_mask[i]:
                continue

            ei = i + 1
            ep = arr_open[ei]
            entry_dt = dt_list[ei]

            pnl, rtype, exit_bar = None, None, ei
            for j in range(ei, min(ei + MAX_HOLD, n)):
                bhi = arr_high[j]; blo = arr_low[j]
                if blo <= ep - TP:
                    pnl, rtype, exit_bar = float(TP), "TP", j; break
                if bhi >= ep + SL:
                    pnl, rtype, exit_bar = float(-SL), "SL", j; break
                if arr_hm[j] in SESSION_BOUNDARIES:
                    pnl = float(ep - arr_close[j])
                    rtype, exit_bar = "SESSION", j; break
            if pnl is None:
                close_idx = min(ei + MAX_HOLD - 1, n - 1)
                pnl = float(ep - arr_close[close_idx])
                rtype, exit_bar = "TIME", close_idx

            trades.append({
                "system":      "③",
                "entry_dt":    entry_dt,
                "exit_dt":     dt_list[exit_bar],
                "signal_dt":   dt_list[i],
                "entry_price": ep,
                "exit_price":  (ep - TP if rtype == "TP" else
                                ep + SL if rtype == "SL" else
                                arr_close[exit_bar]),
                "pnl_pt":      pnl,
                "result":      rtype,
            })

    if not trades:
        return pd.DataFrame(columns=[
            "system", "entry_dt", "exit_dt", "signal_dt",
            "entry_price", "exit_price", "pnl_pt", "result"
        ])

    df_trades = pd.DataFrame(trades)
    df_trades["entry_dt"] = pd.to_datetime(df_trades["entry_dt"])
    df_trades["exit_dt"]  = pd.to_datetime(df_trades["exit_dt"])
    df_trades["signal_dt"] = pd.to_datetime(df_trades["signal_dt"])
    return df_trades.sort_values("entry_dt").reset_index(drop=True)


# =========================================
# 月次DD制限 適用
# =========================================
def apply_monthly_dd(raw_df: pd.DataFrame, limit_yen) -> tuple:
    """
    limit_yen: None → 制限なし
    Returns:
        accepted_df     : 採用されたトレードのDataFrame
        skipped_df      : スキップされたトレードのDataFrame
        activated_months: DD制限が発動した (year, month) のリスト
    処理方針:
      - エントリー順に1件ずつ処理
      - そのエントリーより前に決済したトレードの損益を月次累計に反映
      - 月次累計 <= limit_yen になった時点でその月の以降エントリーをスキップ
      - 発動時点の保有ポジションはSL/TPまで継続
      - 同一エントリー時刻の複数トレードは全件同一状態で判定
    """
    if limit_yen is None:
        return raw_df.copy(), pd.DataFrame(columns=raw_df.columns), []

    trades_list = raw_df.sort_values("entry_dt").reset_index(drop=True).to_dict("records")

    monthly_pnl   = {}   # (year, month) -> float yen（EXIT 月で集計）
    monthly_skip  = {}   # (year, month) -> bool
    activated_months = []

    accepted_flags = []

    # 優先度付きキュー: (exit_dt, counter, trade_dict)
    pq      = []
    counter = 0

    for trade in trades_list:
        entry_dt = trade["entry_dt"]
        ym_entry = (entry_dt.year, entry_dt.month)

        # このエントリーより前に EXIT したトレードを全部処理
        while pq and pq[0][0] < entry_dt:
            exit_dt_ts, _, closed = heapq.heappop(pq)
            ym_exit   = (exit_dt_ts.year, exit_dt_ts.month)
            trade_yen = round(closed["pnl_pt"] * PT_TO_YEN - COMMISSION_YEN, 0)
            monthly_pnl[ym_exit] = monthly_pnl.get(ym_exit, 0.0) + trade_yen
            if (monthly_pnl[ym_exit] <= limit_yen
                    and not monthly_skip.get(ym_exit, False)):
                monthly_skip[ym_exit] = True
                activated_months.append(ym_exit)

        # エントリー判定
        if monthly_skip.get(ym_entry, False):
            accepted_flags.append(False)
        else:
            accepted_flags.append(True)
            heapq.heappush(pq, (trade["exit_dt"], counter, trade))
            counter += 1

    # キューを空にして最終的な monthly_pnl を完成させる
    while pq:
        exit_dt_ts, _, closed = heapq.heappop(pq)
        ym_exit   = (exit_dt_ts.year, exit_dt_ts.month)
        trade_yen = round(closed["pnl_pt"] * PT_TO_YEN - COMMISSION_YEN, 0)
        monthly_pnl[ym_exit] = monthly_pnl.get(ym_exit, 0.0) + trade_yen
        if (monthly_pnl[ym_exit] <= limit_yen
                and not monthly_skip.get(ym_exit, False)):
            monthly_skip[ym_exit] = True
            activated_months.append(ym_exit)

    df_sorted = raw_df.sort_values("entry_dt").reset_index(drop=True)
    flags     = np.array(accepted_flags)
    accepted_df = df_sorted[flags].reset_index(drop=True)
    skipped_df  = df_sorted[~flags].reset_index(drop=True)

    return accepted_df, skipped_df, sorted(activated_months)


# =========================================
# 集計ユーティリティ
# =========================================
def calc_summary(df: pd.DataFrame) -> dict:
    """手数料控除後の集計。pnl_pt は手数料前の値を想定。"""
    if len(df) == 0:
        return {"n": 0, "win_rate": 0.0, "pnl_pt": 0.0,
                "pnl_yen": 0.0, "ev_pt": 0.0, "pf": 0.0}
    pnl = df["pnl_pt"].values - COMMISSION_PT
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


def calc_yearly(df: pd.DataFrame) -> dict:
    """年別集計。"""
    df = df.copy()
    df["year"] = pd.to_datetime(df["entry_dt"]).dt.year
    result = {}
    for yr in sorted(df["year"].unique()):
        result[yr] = calc_summary(df[df["year"] == yr])
    return result


def limit_label(limit_yen) -> str:
    return "制限なし" if limit_yen is None else f"{limit_yen:,}円"


# =========================================
# 出力ユーティリティ
# =========================================
SEP  = "=" * 72
SEP2 = "-" * 72

def print_section(title: str):
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)


def print_overall(s: dict, label: str):
    print(f"  {label}")
    print(f"    件数:{s['n']:>5}  勝率:{s['win_rate']:>5.1f}%  "
          f"損益:{s['pnl_pt']:>+8.1f}pt / {s['pnl_yen']:>+10,.0f}円  "
          f"期待値:{s['ev_pt']:>+6.2f}pt  PF:{s['pf']:.2f}")


def print_yearly(yr_dict: dict, label: str):
    print(f"  【年別】{label}")
    print(f"  {'年':>4}  {'件数':>5}  {'勝率%':>6}  {'損益(pt)':>9}  "
          f"{'損益(円)':>10}  {'PF':>5}")
    print("  " + SEP2[:64])
    for yr in sorted(yr_dict.keys()):
        s = yr_dict[yr]
        if s["n"] == 0:
            continue
        print(f"  {yr}  {s['n']:>5}  {s['win_rate']:>5.1f}%  "
              f"{s['pnl_pt']:>+9.1f}  {s['pnl_yen']:>+10,.0f}  {s['pf']:>5.2f}")


# =========================================
# メイン
# =========================================
def main():
    print(SEP)
    print("  系統①③合算 月次ドローダウン制限 バックテスト")
    print(f"  SL:{SL}pt  TP:{TP}pt  手数料:{COMMISSION_PT}pt({COMMISSION_YEN}円)")
    print(SEP)

    # ── データ読み込み ──
    df = load_data()
    df = add_indicators(df)
    cpi_df = load_cpi_events()

    # ── 生トレード生成（制限なし・手数料未控除）──
    print("\n生トレード生成中...")
    raw_df = generate_raw_trades(df, cpi_df)
    print(f"生トレード総数: {len(raw_df)} 件  "
          f"（系統①: {(raw_df['system']=='①').sum()}件  "
          f"系統③: {(raw_df['system']=='③').sum()}件）")

    if len(raw_df) == 0:
        print("トレードが1件もありません。データを確認してください。")
        return

    # ── 制限なしの基準値 ──
    base_s   = calc_summary(raw_df)
    base_yr  = calc_yearly(raw_df)
    base_s1  = calc_summary(raw_df[raw_df["system"] == "①"])
    base_s3  = calc_summary(raw_df[raw_df["system"] == "③"])

    # ── 全制限値をループ処理 ──
    summary_rows = []

    for limit_yen in DD_LIMITS:
        label = limit_label(limit_yen)
        accepted_df, skipped_df, activated_months = apply_monthly_dd(raw_df, limit_yen)

        s = calc_summary(accepted_df)
        yr_dict = calc_yearly(accepted_df)

        # スキップされたトレードの「除外による損益影響」
        skipped_s = calc_summary(skipped_df)
        skip_pnl_yen = skipped_s["pnl_yen"]  # スキップしたトレードの仮想損益

        # 制限なしとの比較
        pnl_diff = s["pnl_yen"] - base_s["pnl_yen"]
        pf_diff  = s["pf"]      - base_s["pf"]
        n_diff   = s["n"]       - base_s["n"]

        # ────────────────────────────────
        # 出力
        # ────────────────────────────────
        print_section(f"月次DD制限: {label}")

        # 1. 全体成績
        print_overall(s, "全体成績（手数料控除後）")
        print()

        # 2. 年別成績
        print_yearly(yr_dict, label)
        print()

        # 3. 月次DD発動統計
        n_activated = len(activated_months)
        months_str = (", ".join(f"{y}/{m:02d}" for y, m in activated_months)
                      if activated_months else "なし")
        print(f"  【月次DD発動統計】")
        print(f"    発動回数:          {n_activated} 回")
        print(f"    発動月:            {months_str}")
        print(f"    除外トレード数:    {len(skipped_df)} 件")
        if len(skipped_df) > 0:
            sk_s1 = (skipped_df["system"] == "①").sum()
            sk_s3 = (skipped_df["system"] == "③").sum()
            print(f"      ├ 系統①: {sk_s1} 件  系統③: {sk_s3} 件")
            print(f"    除外による損益影響: {skip_pnl_yen:+,.0f}円（スキップ分の仮想損益）")
        print()

        # 4. 制限なしとの比較
        if limit_yen is not None:
            print(f"  【制限なしとの比較】")
            print(f"    損益差: {pnl_diff:+,.0f}円  PF差: {pf_diff:+.3f}  件数差: {n_diff:+d}")
        print()

        # CSV用行
        summary_rows.append({
            "limit_yen":        label,
            "n_trades":         s["n"],
            "win_rate_pct":     round(s["win_rate"], 1),
            "pnl_pt":           round(s["pnl_pt"], 1),
            "pnl_yen":          round(s["pnl_yen"], 0),
            "ev_pt":            round(s["ev_pt"], 2),
            "pf":               round(s["pf"], 3),
            "dd_activations":   n_activated,
            "activated_months": months_str,
            "skipped_trades":   len(skipped_df),
            "skip_pnl_yen":     round(skip_pnl_yen, 0),
            "vs_base_pnl_yen":  round(pnl_diff, 0),
            "vs_base_pf":       round(pf_diff, 3),
        })

    # ── 制限なし 参考: 系統別内訳 ──
    print_section("参考: 制限なし 系統別内訳")
    print_overall(base_s,  "①③ 合算")
    print_overall(base_s1, "系統① のみ")
    print_overall(base_s3, "系統③ のみ")
    print_yearly(base_yr, "①③ 合算")
    print()

    # ── 推奨値の算出 ──
    print_section("推奨月次DD制限値")
    _print_recommendation(summary_rows, base_s)

    # ── CSV 保存 ──
    result_df = pd.DataFrame(summary_rows)
    result_df.to_csv(RESULT_CSV, index=False, encoding="utf-8-sig")
    print(f"\n[OK] 結果保存: {RESULT_CSV}")


# =========================================
# 推奨値 算出・提示
# =========================================
def _print_recommendation(rows: list, base_s: dict):
    """
    推奨基準:
      1. PF が制限なしより大幅に下がらない（PF差 > -0.05）
      2. 発動回数が少ない（発動月 <= 全期間の3割）
      3. 除外トレード数が少ない
      4. 損益影響（除外分の仮想損益が正 = スキップしたほうが良かったトレードが多い）
    """
    # 制限なし行を除く
    candidates = [r for r in rows if r["limit_yen"] != "制限なし"]
    if not candidates:
        return

    # スコア計算（小さいほど良い: 発動回数 × 0.3 + PF悪化 × 10 - 除外益/1000）
    base_pf = base_s["pf"]
    for r in candidates:
        pf_penalty   = max(0.0, base_pf - r["pf"]) * 10
        act_penalty  = r["dd_activations"] * 0.3
        skip_benefit = -r["skip_pnl_yen"] / 10_000   # スキップ分損失回避が多いほど良い
        r["_score"]  = pf_penalty + act_penalty - skip_benefit

    best = min(candidates, key=lambda r: r["_score"])

    print(f"\n  推奨制限値: {best['limit_yen']}")
    print(f"\n  【根拠】")
    print(f"  ・PF: {best['pf']:.3f}（制限なし比 {best['vs_base_pf']:+.3f}）")
    print(f"  ・DD発動: {best['dd_activations']} 回  "
          f"発動月: {best['activated_months']}")
    print(f"  ・除外トレード: {best['skipped_trades']} 件  "
          f"除外分仮想損益: {best['skip_pnl_yen']:+,.0f}円")
    print(f"  ・制限なしとの損益差: {best['vs_base_pnl_yen']:+,.0f}円")
    print()
    print(f"  【判断基準】")
    print(f"  ・PF低下が小さく、かつ発動回数が少ない制限値を優先しました。")
    print(f"  ・除外トレードの仮想損益がマイナス（= スキップが損失回避に貢献）")
    print(f"    であれば制限の効果が高いと判断します。")
    print(f"  ・制限が厳しすぎると「本来利益になるトレードもスキップ」するため、")
    print(f"    PF・損益ともに下がります。スコアが最小の制限値を推奨します。")
    print()

    # 全候補サマリー表
    print(f"  {'制限値':>10}  {'件数':>5}  {'PF':>5}  {'PF差':>6}  "
          f"{'発動':>4}  {'除外':>4}  {'除外損益(円)':>12}  {'損益差(円)':>12}")
    print("  " + "-" * 72)
    for r in candidates:
        marker = " ←推奨" if r["limit_yen"] == best["limit_yen"] else ""
        print(f"  {r['limit_yen']:>10}  {r['n_trades']:>5}  {r['pf']:>5.3f}  "
              f"{r['vs_base_pf']:>+6.3f}  {r['dd_activations']:>4}  "
              f"{r['skipped_trades']:>4}  {r['skip_pnl_yen']:>+12,.0f}  "
              f"{r['vs_base_pnl_yen']:>+12,.0f}{marker}")


if __name__ == "__main__":
    main()
