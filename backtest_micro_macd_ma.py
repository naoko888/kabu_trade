from pathlib import Path
from itertools import product
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

MA1_LIST  = [3, 5, 9]
MA2_LIST  = [10, 20, 26]
SL_LIST   = [30, 40, 60]
TP_MULTS  = [2, 3, 4]

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIG  = 9
TOUCH_PCT     = 0.005   # ±0.5%
MAX_HOLD      = 50      # 最大保有バー数（タイムアウト用）
COMMISSION_PT = 2.2     # 往復手数料（22円 ÷ 10円/pt = 2.2pt）

# =========================================
# データ読み込み（backtest_micro.py から流用）
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
    for p in sorted(set(MA1_LIST + MA2_LIST)):
        df[f"ma{p}"] = df["close"].rolling(p).mean()

    # MACD (12, 26, 9)
    ema_fast       = df["close"].ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow       = df["close"].ewm(span=MACD_SLOW, adjust=False).mean()
    df["macd"]     = ema_fast - ema_slow
    df["macd_sig"] = df["macd"].ewm(span=MACD_SIG, adjust=False).mean()

    # 出来高比率
    df["vol_ma20"]  = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma20"]

    # ボリンジャーバンド幅 & スクイーズ
    bb_mid              = df["close"].rolling(20).mean()
    bb_std              = df["close"].rolling(20).std()
    df["bb_width"]      = (bb_std * 4) / bb_mid            # (upper-lower)/mid = 4σ/mid
    df["bb_width_ma20"] = df["bb_width"].rolling(20).mean()
    df["bb_squeeze"]    = df["bb_width"] / df["bb_width_ma20"]  # <1=収縮

    return df

# =========================================
# バックテスト（MA/MACD シグナル + フィルター付き）
# =========================================
def run_backtest(df: pd.DataFrame, ma1: int, ma2: int,
                 sl: int, tp: int, direction: str,
                 vol_min: float = 0.0,
                 use_bb_squeeze: bool = False,
                 use_bb_expand: bool = False,
                 time_filter: str = "all",
                 day_filter: str = "all",
                 commission: float = 0.0) -> pd.DataFrame:
    col1 = f"ma{ma1}"
    col2 = f"ma{ma2}"

    arr_close      = df["close"].values
    arr_high       = df["high"].values
    arr_low        = df["low"].values
    arr_open       = df["open"].values
    arr_ma1        = df[col1].values
    arr_ma2        = df[col2].values
    arr_macd       = df["macd"].values
    arr_msig       = df["macd_sig"].values
    arr_vol_ratio  = df["vol_ratio"].values
    arr_bb_squeeze = df["bb_squeeze"].values
    arr_bb_width   = df["bb_width"].values
    arr_bb_wma     = df["bb_width_ma20"].values

    # 時間帯・曜日（JST tz-naive 想定、Excelデータは JST 生値）
    dts            = pd.to_datetime(df["datetime"])
    arr_hour       = dts.dt.hour.values
    arr_minute     = dts.dt.minute.values
    arr_weekday    = dts.dt.weekday.values   # 0=月, 1=火, 2=水, 3=木, 4=金

    n = len(df)
    trades = []

    for i in range(2, n - 1):
        m1   = arr_ma1[i];    m2   = arr_ma2[i]
        m1p  = arr_ma1[i-1];  m2p  = arr_ma2[i-1]
        m1p2 = arr_ma1[i-2];  m2p2 = arr_ma2[i-2]

        if any(np.isnan(v) for v in [m1, m2, m1p, m2p, m1p2, m2p2]):
            continue
        if np.isnan(arr_macd[i]) or np.isnan(arr_msig[i]):
            continue

        c1 = arr_close[i-1]
        c2 = arr_close[i-2]
        hi = arr_high[i]
        lo = arr_low[i]

        if direction == "long":
            if not (c2 > m1p2 and c2 > m2p2 and c1 > m1p and c1 > m2p):
                continue
            if not (abs(lo - m1) / m1 <= TOUCH_PCT or abs(lo - m2) / m2 <= TOUCH_PCT):
                continue
            if arr_macd[i] <= arr_msig[i]:
                continue
        else:
            if not (c2 < m1p2 and c2 < m2p2 and c1 < m1p and c1 < m2p):
                continue
            if not (abs(hi - m1) / m1 <= TOUCH_PCT or abs(hi - m2) / m2 <= TOUCH_PCT):
                continue
            if arr_macd[i] >= arr_msig[i]:
                continue

        # ---- 出来高フィルター ----
        if vol_min > 0:
            vr = arr_vol_ratio[i]
            if np.isnan(vr) or vr < vol_min:
                continue

        # ---- BBスクイーズ除外（squeeze>0.9 = スクイーズでない） ----
        if use_bb_squeeze:
            sq = arr_bb_squeeze[i]
            if np.isnan(sq) or sq <= 0.9:
                continue

        # ---- BB幅拡大中（bb_width > bb_width_ma20） ----
        if use_bb_expand:
            bw  = arr_bb_width[i]
            bwm = arr_bb_wma[i]
            if np.isnan(bw) or np.isnan(bwm) or bw <= bwm:
                continue

        # ---- 時間帯フィルター ----
        h, m = arr_hour[i], arr_minute[i]
        if time_filter == "no_open":
            # 9:00〜9:25 除外
            if h == 9 and m <= 25:
                continue
        elif time_filter == "core":
            # 9:30〜14:30 のみ
            after_open  = (h > 9) or (h == 9 and m >= 30)
            before_close = (h < 14) or (h == 14 and m <= 30)
            if not (after_open and before_close):
                continue

        # ---- 曜日フィルター ----
        wd = arr_weekday[i]
        if day_filter == "mon_thu":
            if wd not in (0, 3):
                continue
        elif day_filter == "tue_wed":
            if wd not in (1, 2):
                continue

        # ---- エントリー（次足のopen） ----
        ei    = i + 1
        entry = arr_open[ei]

        pnl   = None
        rtype = None

        exit_bar = ei
        for j in range(ei, min(ei + MAX_HOLD, n)):
            bhi = arr_high[j]
            blo = arr_low[j]
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

        if pnl is None:
            close_idx = min(ei + MAX_HOLD - 1, n - 1)
            final = arr_close[close_idx]
            pnl   = float(final - entry) if direction == "long" else float(entry - final)
            rtype = "TIME"
            exit_bar = close_idx

        pnl -= commission  # 往復手数料控除（0.0 の場合は影響なし）

        trades.append({
            "datetime":      df["datetime"].iloc[ei],
            "exit_datetime": df["datetime"].iloc[exit_bar],
            "pnl":           pnl,
            "result":        rtype,
        })

    if not trades:
        return pd.DataFrame(columns=["datetime", "exit_datetime", "pnl", "result"])
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
        return {"n": 0, "win_rate": 0.0, "pnl": 0.0, "pf": 0.0}
    pnl  = trades_df["pnl"].values
    win  = pnl[pnl > 0].sum()
    loss = abs(pnl[pnl < 0].sum())
    return {
        "n":        len(trades_df),
        "win_rate": float((pnl > 0).mean() * 100),
        "pnl":      float(pnl.sum()),
        "pf":       float(win / loss) if loss > 0 else 0.0,
    }

# =========================================
# 【検証3】MACD 時間足別バックテスト
# =========================================
def run_backtest_macd_tf(df_5min: pd.DataFrame) -> pd.DataFrame:
    """5分足データを各時間足にリサンプルしてMACD GC/DCのみでBT"""

    SL_TF   = 60
    TP_TF   = 240
    HOLD_TF = 50

    rows = []

    for tf_min in [15, 30, 45, 60]:
        rule = f"{tf_min}min"

        # リサンプル（timezone-naive → そのまま使用）
        tf_df = (
            df_5min.set_index("datetime")
            .resample(rule, closed="left", label="left")
            .agg(open=("open", "first"),
                 high=("high", "max"),
                 low=("low", "min"),
                 close=("close", "last"),
                 volume=("volume", "sum"))
            .dropna(subset=["open", "close"])
            .reset_index()
        )

        # MACD 計算
        ema_fast        = tf_df["close"].ewm(span=MACD_FAST, adjust=False).mean()
        ema_slow        = tf_df["close"].ewm(span=MACD_SLOW, adjust=False).mean()
        tf_df["macd"]   = ema_fast - ema_slow
        tf_df["msig"]   = tf_df["macd"].ewm(span=MACD_SIG, adjust=False).mean()

        arr_open  = tf_df["open"].values
        arr_high  = tf_df["high"].values
        arr_low   = tf_df["low"].values
        arr_close = tf_df["close"].values
        arr_macd  = tf_df["macd"].values
        arr_msig  = tf_df["msig"].values
        n         = len(tf_df)

        for direction in ["long", "short"]:
            trades = []
            for i in range(1, n - 1):
                if np.isnan(arr_macd[i]) or np.isnan(arr_msig[i]):
                    continue
                if np.isnan(arr_macd[i-1]) or np.isnan(arr_msig[i-1]):
                    continue

                # クロス判定（前足→現足でクロス確定）
                if direction == "long":
                    # GC：前足 macd<sig かつ 現足 macd>sig
                    if not (arr_macd[i-1] < arr_msig[i-1] and arr_macd[i] > arr_msig[i]):
                        continue
                else:
                    # DC：前足 macd>sig かつ 現足 macd<sig
                    if not (arr_macd[i-1] > arr_msig[i-1] and arr_macd[i] < arr_msig[i]):
                        continue

                ei    = i + 1
                entry = arr_open[ei]
                pnl   = None
                rtype = None

                for j in range(ei, min(ei + HOLD_TF, n)):
                    bhi = arr_high[j]
                    blo = arr_low[j]
                    if direction == "long":
                        if bhi >= entry + TP_TF:
                            pnl, rtype = float(TP_TF),  "TP"; break
                        if blo <= entry - SL_TF:
                            pnl, rtype = float(-SL_TF), "SL"; break
                    else:
                        if blo <= entry - TP_TF:
                            pnl, rtype = float(TP_TF),  "TP"; break
                        if bhi >= entry + SL_TF:
                            pnl, rtype = float(-SL_TF), "SL"; break

                if pnl is None:
                    final = arr_close[min(ei + HOLD_TF - 1, n - 1)]
                    pnl   = float(final - entry) if direction == "long" else float(entry - final)
                    rtype = "TIME"

                trades.append({"pnl": pnl, "result": rtype})

            s  = calc_summary(pd.DataFrame(trades) if trades else pd.DataFrame(columns=["pnl"]))
            ev = round(s["pnl"] / s["n"], 1) if s["n"] > 0 else 0.0
            rows.append({
                "時間足":       f"{tf_min}min",
                "方向":         direction,
                "トレード数":   s["n"],
                "勝率%":        round(s["win_rate"], 1),
                "損益合計":     int(s["pnl"]),
                "期待値/trade": ev,
                "PF":           round(s["pf"], 3),
            })

    return pd.DataFrame(rows)

# =========================================
# run_backtest ラッパー（時間リスト・除外月フィルター追加）
# =========================================
def run_backtest_custom(df: pd.DataFrame, ma1: int, ma2: int,
                        sl: int, tp: int, direction: str,
                        hour_list=None, exclude_months=None,
                        **kwargs) -> pd.DataFrame:
    """
    run_backtest() を呼んだ後、エントリー足の hour / month で
    追加フィルタリングする。既存コードを変更せず機能拡張。
    hour_list      : 許容する hour のリスト。None=すべて
    exclude_months : 除外する month のリスト。None=除外なし
    """
    trades = run_backtest(df, ma1, ma2, sl, tp, direction, **kwargs)
    if len(trades) == 0:
        return trades
    dts = pd.to_datetime(trades["datetime"]).reset_index(drop=True)
    if hour_list is not None:
        mask = dts.dt.hour.isin(hour_list)
        trades = trades.reset_index(drop=True)[mask].reset_index(drop=True)
        dts    = dts[mask].reset_index(drop=True)
    if exclude_months is not None:
        mask   = ~dts.dt.month.isin(exclude_months)
        trades = trades.reset_index(drop=True)[mask].reset_index(drop=True)
    return trades


# =========================================
# シグナルバー指標収集（分析A-4 用）
# =========================================
def collect_signal_metadata(df: pd.DataFrame, ma1: int, ma2: int) -> pd.DataFrame:
    """基本シグナル条件（MA/MACD/タッチ）を満たすバーの指標をすべての曜日分収集"""
    col1 = f"ma{ma1}"
    col2 = f"ma{ma2}"

    arr_close     = df["close"].values
    arr_high      = df["high"].values
    arr_low       = df["low"].values
    arr_ma1       = df[col1].values
    arr_ma2       = df[col2].values
    arr_macd      = df["macd"].values
    arr_msig      = df["macd_sig"].values
    arr_vol_ratio = df["vol_ratio"].values
    arr_bb_width  = df["bb_width"].values

    dts         = pd.to_datetime(df["datetime"])
    arr_weekday = dts.dt.weekday.values
    n           = len(df)

    records = []
    for i in range(2, n - 1):
        m1   = arr_ma1[i];    m2   = arr_ma2[i]
        m1p  = arr_ma1[i-1];  m2p  = arr_ma2[i-1]
        m1p2 = arr_ma1[i-2];  m2p2 = arr_ma2[i-2]

        if any(np.isnan(v) for v in [m1, m2, m1p, m2p, m1p2, m2p2]):
            continue
        if np.isnan(arr_macd[i]) or np.isnan(arr_msig[i]):
            continue

        c1 = arr_close[i-1];  c2 = arr_close[i-2];  lo = arr_low[i]

        # long シグナル（vol/BB/曜日フィルターなし）
        if not (c2 > m1p2 and c2 > m2p2 and c1 > m1p and c1 > m2p):
            continue
        if not (abs(lo - m1) / m1 <= TOUCH_PCT or abs(lo - m2) / m2 <= TOUCH_PCT):
            continue
        if arr_macd[i] <= arr_msig[i]:
            continue

        vr = arr_vol_ratio[i]
        bw = arr_bb_width[i]
        atr_val = float(arr_high[i] - arr_low[i])

        records.append({
            "weekday":   int(arr_weekday[i]),
            "vol_ratio": float(vr) if not np.isnan(vr) else np.nan,
            "bb_width":  float(bw) if not np.isnan(bw) else np.nan,
            "atr":       atr_val,
        })

    return pd.DataFrame(records)


# =========================================
# グループ別集計ヘルパー
# =========================================
def group_summary(grp_df: pd.DataFrame) -> pd.Series:
    pnl  = grp_df["pnl"].values
    win  = pnl[pnl > 0].sum()
    loss = abs(pnl[pnl < 0].sum())
    n    = len(pnl)
    return pd.Series({
        "トレード数":   n,
        "勝率%":        round(float((pnl > 0).mean() * 100), 1),
        "損益合計":     int(pnl.sum()),
        "期待値/trade": round(float(pnl.sum() / n), 1) if n > 0 else 0.0,
        "PF":           round(float(win / loss), 3) if loss > 0 else 0.0,
    })


# =========================================
# メイン
# =========================================
def main():
    df = load_all()
    df = add_indicators(df)

    # =========================================
    # 【全パラメータ総当たり】MA / MACD / RR
    # =========================================
    rows = []
    for ma1, ma2 in product(MA1_LIST, MA2_LIST):
        if ma1 >= ma2:
            continue
        for sl in SL_LIST:
            for tp_mult in TP_MULTS:
                tp = sl * tp_mult
                for direction in ["long", "short"]:
                    trades = run_backtest(df, ma1, ma2, sl, tp, direction)
                    s      = calc_summary(trades)
                    rows.append({
                        "MA1":        ma1,
                        "MA2":        ma2,
                        "SL":         sl,
                        "TP":         tp,
                        "RR":         f"1:{tp_mult}",
                        "方向":       direction,
                        "トレード数": s["n"],
                        "勝率%":      round(s["win_rate"], 1),
                        "損益合計":   int(s["pnl"]),
                        "PF":         round(s["pf"], 3),
                    })

    result = (
        pd.DataFrame(rows)
        .sort_values("PF", ascending=False)
        .reset_index(drop=True)
    )
    result.index += 1

    pd.set_option("display.max_rows", None)
    pd.set_option("display.width", 160)

    print(f"総パラメータ組合せ数: {len(result)}")
    print("\n===== PF ランキング（全組合せ） =====")
    print(result.to_string())
    print("\n===== TOP 10 =====")
    print(result.head(10).to_string())

    # =========================================
    # 【検証1・2】TOP1条件に追加フィルター総当たり
    # MA1=9, MA2=10, SL=60, TP=240, long
    # =========================================
    print("\n\n" + "=" * 70)
    print("【追加フィルター検証】MA1=9, MA2=10, SL=60, TP=240, long")
    print("=" * 70)

    filter_rows = []
    for vol_min in [0.0, 1.5, 2.0]:
        for use_squeeze in [False, True]:
            for use_expand in [False, True]:
                for time_f in ["all", "no_open", "core"]:
                    for day_f in ["all", "mon_thu", "tue_wed"]:
                        t = run_backtest(
                            df, 9, 10, 60, 240, "long",
                            vol_min=vol_min,
                            use_bb_squeeze=use_squeeze,
                            use_bb_expand=use_expand,
                            time_filter=time_f,
                            day_filter=day_f,
                        )
                        s  = calc_summary(t)
                        ev = round(s["pnl"] / s["n"], 1) if s["n"] > 0 else 0.0

                        filter_rows.append({
                            "出来高":     "なし" if vol_min == 0 else f">={vol_min}",
                            "SQ除外":     "○" if use_squeeze else "-",
                            "BB拡大":     "○" if use_expand  else "-",
                            "時間帯":     time_f,
                            "曜日":       day_f,
                            "トレード数": s["n"],
                            "勝率%":      round(s["win_rate"], 1),
                            "損益合計":   int(s["pnl"]),
                            "期待値":     ev,
                            "PF":         round(s["pf"], 3),
                        })

    fdf = (
        pd.DataFrame(filter_rows)
        .sort_values("PF", ascending=False)
        .reset_index(drop=True)
    )
    fdf.index += 1
    print(f"\n総フィルター組合せ数: {len(fdf)}")
    print(fdf.to_string())
    print("\n===== フィルター TOP 20 =====")
    print(fdf.head(20).to_string())

    # =========================================
    # 【検証3】MACD 時間足別（15・30・45・60分）
    # =========================================
    print("\n\n" + "=" * 70)
    print("【検証3】MACD クロス単体バックテスト（時間足別）SL=60 TP=240")
    print("=" * 70)
    tf_result = run_backtest_macd_tf(df)
    print(tf_result.to_string(index=False))

    # =========================================
    # === 分析A: 火水深掘り ===
    # 条件: MA1=9, MA2=10, SL=60, TP=240, long
    #       vol>=2.0, BB拡大中, 火水限定
    # =========================================
    COND = dict(ma1=9, ma2=10, sl=60, tp=240, direction="long",
                vol_min=2.0, use_bb_expand=True,
                use_bb_squeeze=False, time_filter="all", day_filter="tue_wed")

    print("\n\n" + "=" * 70)
    print("=== 分析A: 火水深掘り  MA9/10 SL60 TP240 long vol>=2.0 BB拡大 ===")
    print("=" * 70)

    trades_a = run_backtest(df, **COND)
    if len(trades_a) == 0:
        print("トレードなし")
    else:
        dts_a = pd.to_datetime(trades_a["datetime"])
        trades_a = trades_a.copy()
        trades_a["hour"]    = dts_a.dt.hour
        trades_a["year"]    = dts_a.dt.year
        trades_a["month"]   = dts_a.dt.month

        # ---- A-1: 時間帯別 ----
        print("\n【A-1】時間帯別成績（火水）")
        a1 = (trades_a.groupby("hour")
              .apply(group_summary, include_groups=False)
              .reset_index())
        a1.columns = ["時間帯(h)"] + list(a1.columns[1:])
        print(a1.to_string(index=False))

        # ---- A-2: 年別 ----
        print("\n【A-2】年別成績（火水）")
        a2 = (trades_a.groupby("year")
              .apply(group_summary, include_groups=False)
              .reset_index())
        a2.columns = ["年"] + list(a2.columns[1:])
        print(a2.to_string(index=False))

        # ---- A-3: 月別 ----
        print("\n【A-3】月別成績（火水・全年合算）")
        a3 = (trades_a.groupby("month")
              .apply(group_summary, include_groups=False)
              .reset_index())
        a3.columns = ["月"] + list(a3.columns[1:])
        print(a3.to_string(index=False))

    # ---- A-4: 曜日別市場特性（基本シグナル全件、フィルターなし）----
    print("\n【A-4】曜日別シグナルバー特性（vol/BB/曜日フィルターなし）")
    WDAY = {0: "月", 1: "火", 2: "水", 3: "木", 4: "金"}
    meta = collect_signal_metadata(df, 9, 10)
    if len(meta) == 0:
        print("シグナルなし")
    else:
        a4 = (meta.groupby("weekday")
              .agg(
                  シグナル数=("vol_ratio", "count"),
                  平均vol_ratio=("vol_ratio", "mean"),
                  平均BB幅=("bb_width", "mean"),
                  平均ATR=("atr", "mean"),
              )
              .reset_index())
        a4["曜日"] = a4["weekday"].map(WDAY)
        a4 = a4[["曜日", "シグナル数", "平均vol_ratio", "平均BB幅", "平均ATR"]]
        a4["平均vol_ratio"] = a4["平均vol_ratio"].round(3)
        a4["平均BB幅"]      = a4["平均BB幅"].round(5)
        a4["平均ATR"]       = a4["平均ATR"].round(1)
        print(a4.to_string(index=False))


    # =========================================
    # === フェーズ1：最終確定バックテスト ===
    # =========================================
    print("\n\n" + "=" * 70)
    print("=== フェーズ1：最終確定バックテスト ===")
    print("=" * 70)

    final_cases = [
        (
            "系統①：月木×夜間特化",
            dict(day_filter="mon_thu",
                 hour_list=[18, 19, 20, 21, 22, 23],
                 exclude_months=[3, 7]),
        ),
        (
            "系統②：火水×精密フィルター",
            dict(day_filter="tue_wed",
                 vol_min=2.0, use_bb_expand=True,
                 exclude_months=[5, 6, 7, 8, 9]),
        ),
    ]

    for name, kw in final_cases:
        t  = run_backtest_custom(df, 9, 10, 60, 240, "long", **kw)
        s  = calc_summary(t)
        ev = round(s["pnl"] / s["n"], 1) if s["n"] > 0 else 0.0

        print(f"\n■ {name}")
        print(f"  全体  件数={s['n']:,}  勝率={s['win_rate']:.1f}%  "
              f"損益={int(s['pnl']):,}  期待値={ev}  PF={s['pf']:.3f}")

        if len(t) > 0:
            t2 = t.copy()
            dts = pd.to_datetime(t2["datetime"])
            t2["year"] = dts.dt.year
            ydf = (t2.groupby("year")
                   .apply(group_summary, include_groups=False)
                   .reset_index())
            ydf.columns = ["年"] + list(ydf.columns[1:])
            print("  年別内訳:")
            print(ydf.to_string(index=False))

    # 2系統の横並び比較
    print("\n  ── 2系統比較 ──")
    cmp2 = []
    for name, kw in final_cases:
        t = run_backtest_custom(df, 9, 10, 60, 240, "long", **kw)
        s = calc_summary(t)
        ev = round(s["pnl"] / s["n"], 1) if s["n"] > 0 else 0.0
        cmp2.append({"系統": name, "件数": s["n"],
                     "勝率%": round(s["win_rate"], 1),
                     "損益合計": int(s["pnl"]),
                     "期待値/trade": ev,
                     "PF": round(s["pf"], 3)})
    print(pd.DataFrame(cmp2).to_string(index=False))

    # =========================================
    # === 分析B: 月木深掘り ===
    # 条件: MA1=9, MA2=10, SL=60, TP=240, long
    #       フィルターなし、月木限定
    # =========================================
    print("\n\n" + "=" * 70)
    print("=== 分析B: 月木深掘り  MA9/10 SL60 TP240 long フィルターなし 月木 ===")
    print("=" * 70)

    trades_b = run_backtest(df, 9, 10, 60, 240, "long",
                            vol_min=0.0, use_bb_squeeze=False,
                            use_bb_expand=False, time_filter="all",
                            day_filter="mon_thu")
    if len(trades_b) == 0:
        print("トレードなし")
    else:
        dts_b = pd.to_datetime(trades_b["datetime"])
        trades_b = trades_b.copy()
        trades_b["hour"]  = dts_b.dt.hour
        trades_b["year"]  = dts_b.dt.year
        trades_b["month"] = dts_b.dt.month

        # ---- B-1: 時間帯別 ----
        print("\n【B-1】時間帯別成績（月木）")
        b1 = (trades_b.groupby("hour")
              .apply(group_summary, include_groups=False)
              .reset_index())
        b1.columns = ["時間帯(h)"] + list(b1.columns[1:])
        print(b1.to_string(index=False))

        # ---- B-2: 年別 ----
        print("\n【B-2】年別成績（月木）")
        b2 = (trades_b.groupby("year")
              .apply(group_summary, include_groups=False)
              .reset_index())
        b2.columns = ["年"] + list(b2.columns[1:])
        print(b2.to_string(index=False))

        # ---- B-3: 月別 ----
        print("\n【B-3】月別成績（月木・全年合算）")
        b3 = (trades_b.groupby("month")
              .apply(group_summary, include_groups=False)
              .reset_index())
        b3.columns = ["月"] + list(b3.columns[1:])
        print(b3.to_string(index=False))

    # ---- B-4: 月木 vs 火水 直接比較 ----
    print("\n【B-4】月木 vs 火水 直接比較サマリー")
    compare_cases = [
        ("フィルターなし × 月木",        dict(vol_min=0.0,  use_bb_expand=False, day_filter="mon_thu")),
        ("vol>=2.0 × BB拡大 × 火水", dict(vol_min=2.0,  use_bb_expand=True,  day_filter="tue_wed")),
    ]
    cmp_rows = []
    for label, kwargs in compare_cases:
        t = run_backtest(df, 9, 10, 60, 240, "long",
                         use_bb_squeeze=False, time_filter="all", **kwargs)
        s  = calc_summary(t)
        ev = round(s["pnl"] / s["n"], 1) if s["n"] > 0 else 0.0
        cmp_rows.append({
            "条件":          label,
            "件数":          s["n"],
            "勝率%":         round(s["win_rate"], 1),
            "損益合計":      int(s["pnl"]),
            "期待値/trade":  ev,
            "PF":            round(s["pf"], 3),
        })
    cmp_df = pd.DataFrame(cmp_rows)
    print(cmp_df.to_string(index=False))


    # =========================================
    # === 手数料込みバックテスト（2系統比較） ===
    # COMMISSION_PT = 2.2pt（往復22円 ÷ 10円/pt）
    # =========================================
    print("\n\n" + "=" * 70)
    print(f"=== 手数料込み検証  COMMISSION={COMMISSION_PT}pt（往復22円÷10円/pt） ===")
    print("=" * 70)

    comm_cases = [
        (
            "系統①：月木×夜間",
            dict(day_filter="mon_thu",
                 hour_list=[18, 19, 20, 21, 22, 23],
                 exclude_months=[3, 7]),
        ),
        (
            "系統②：火水×精密",
            dict(day_filter="tue_wed",
                 vol_min=2.0, use_bb_expand=True,
                 exclude_months=[5, 6, 7, 8, 9]),
        ),
    ]

    cmp_comm = []
    for name, kw in comm_cases:
        for comm_val, label in [(0.0, "なし"), (COMMISSION_PT, f"+{COMMISSION_PT}pt")]:
            t  = run_backtest_custom(df, 9, 10, 60, 240, "long",
                                     commission=comm_val, **kw)
            s  = calc_summary(t)
            ev = round(s["pnl"] / s["n"], 1) if s["n"] > 0 else 0.0
            cmp_comm.append({
                "系統":          name,
                "手数料":        label,
                "件数":          s["n"],
                "勝率%":         round(s["win_rate"], 1),
                "損益合計(pt)":  round(s["pnl"], 1),
                "期待値/trade":  ev,
                "PF":            round(s["pf"], 3),
            })

    print(pd.DataFrame(cmp_comm).to_string(index=False))

    # 差分サマリー
    print("\n  ── 手数料影響（差分）──")
    for name, _ in comm_cases:
        rows_n = [r for r in cmp_comm if r["系統"] == name and r["手数料"] == "なし"]
        rows_c = [r for r in cmp_comm if r["系統"] == name and r["手数料"] != "なし"]
        if rows_n and rows_c:
            rn, rc = rows_n[0], rows_c[0]
            pnl_diff = round(rc["損益合計(pt)"] - rn["損益合計(pt)"], 1)
            ev_diff  = round(rc["期待値/trade"] - rn["期待値/trade"], 1)
            pf_diff  = round(rc["PF"] - rn["PF"], 3)
            print(f"  {name}: 損益{pnl_diff:+.1f}pt  期待値{ev_diff:+.1f}pt  PF{pf_diff:+.3f}")

    # =========================================
    # 【タスク2】系統①②：ポジション保有中は新規エントリーしない制約
    # =========================================
    PT_YEN = 10

    print("\n\n" + "=" * 70)
    print("【タスク2】系統①② ポジション保有中新規エントリー禁止（手数料込み）")
    print("=" * 70)

    task2_cases = [
        (
            "系統①：月木×夜間",
            dict(day_filter="mon_thu",
                 hour_list=[18, 19, 20, 21, 22, 23],
                 exclude_months=[3, 7],
                 commission=COMMISSION_PT),
        ),
        (
            "系統②：火水×精密",
            dict(day_filter="tue_wed",
                 vol_min=2.0, use_bb_expand=True,
                 exclude_months=[5, 6, 7, 8, 9],
                 commission=COMMISSION_PT),
        ),
    ]

    task2_monthly = {}   # name -> monthly pnl_yen dict

    for name, kw in task2_cases:
        t_raw = run_backtest_custom(df, 9, 10, 60, 240, "long", **kw)
        t_no  = apply_no_overlap(t_raw)
        s_raw = calc_summary(t_raw)
        s_no  = calc_summary(t_no)
        ev_r  = round(s_raw["pnl"] / s_raw["n"], 2) if s_raw["n"] > 0 else 0.0
        ev_n  = round(s_no["pnl"]  / s_no["n"],  2) if s_no["n"]  > 0 else 0.0

        print(f"\n■ {name}")
        print(f"  {'':22} {'件数':>6} {'勝率%':>7} {'損益(pt)':>10} {'損益(円)':>12} {'期待値':>8} {'PF':>6}")
        print(f"  {'制約なし':<22} {s_raw['n']:>6} {s_raw['win_rate']:>6.1f}% "
              f"{s_raw['pnl']:>10.1f} {s_raw['pnl']*PT_YEN:>12,.0f} {ev_r:>8.2f} {s_raw['pf']:>6.3f}")
        print(f"  {'保有中禁止':<22} {s_no['n']:>6} {s_no['win_rate']:>6.1f}% "
              f"{s_no['pnl']:>10.1f} {s_no['pnl']*PT_YEN:>12,.0f} {ev_n:>8.2f} {s_no['pf']:>6.3f}")

        # 年別
        t_no["year"]  = pd.to_datetime(t_no["datetime"]).dt.year
        t_no["month"] = pd.to_datetime(t_no["datetime"]).dt.month
        t_no["yen"]   = t_no["pnl"] * PT_YEN
        print(f"  年別:")
        for yr in sorted(t_no["year"].unique()):
            g = t_no[t_no["year"] == yr]
            gs = calc_summary(g)
            print(f"    {yr}: 件数={gs['n']:>5}, 勝率={gs['win_rate']:.1f}%, "
                  f"損益={gs['pnl']:>8.1f}pt / {g['yen'].sum():>10,.0f}円, PF={gs['pf']:.3f}")

        # 月別集計を保存
        mo_pnl = {}
        for mo in range(1, 13):
            g = t_no[t_no["month"] == mo]
            mo_pnl[mo] = int(g["yen"].sum()) if len(g) > 0 else 0
        task2_monthly[name] = {"trades": t_no, "mo_pnl": mo_pnl, "summary": s_no}

    # =========================================
    # 【タスク3】系統①②③ 月別比較表（system③インライン計算）
    # =========================================
    print("\n\n" + "=" * 70)
    print("【タスク3】系統①②③ 月別比較表（手数料込み 2.2pt・保有中禁止）")
    print("=" * 70)

    # system③ インライン計算（MA短9 < MA中20, MACD DC, highタッチ, short）
    _HOURS3    = {5, 8, 9, 12, 14, 15, 19, 20, 21, 22, 23}
    _WDAYS3    = {0, 2, 3, 4}
    _EXCL_MO3  = {7, 11}

    _arr_open    = df["open"].values
    _arr_high    = df["high"].values
    _arr_low     = df["low"].values
    _arr_close   = df["close"].values
    _arr_mas     = df["ma9"].values
    _arr_mam     = df["ma20"].values
    _arr_macd    = df["macd"].values
    _arr_msig    = df["macd_sig"].values
    _dts         = pd.to_datetime(df["datetime"])
    _arr_hr      = _dts.dt.hour.values
    _arr_wd      = _dts.dt.weekday.values
    _arr_mo      = _dts.dt.month.values
    _n           = len(df)
    _trades3 = []

    for _i in range(1, _n - 1):
        _mas = _arr_mas[_i];  _mam = _arr_mam[_i]
        if np.isnan(_mas) or np.isnan(_mam) or np.isnan(_arr_macd[_i]) or np.isnan(_arr_msig[_i]):
            continue
        if not (_mas < _mam):
            continue
        if _arr_macd[_i] >= _arr_msig[_i]:
            continue
        if abs(_arr_high[_i] - _mas) / _mas > TOUCH_PCT:
            continue
        if _arr_hr[_i] not in _HOURS3:
            continue
        if _arr_wd[_i] not in _WDAYS3:
            continue
        if _arr_mo[_i] in _EXCL_MO3:
            continue

        _ei    = _i + 1
        _entry = _arr_open[_ei]
        _pnl   = None
        _rtype = None
        _exit_bar = _ei

        for _j in range(_ei, min(_ei + MAX_HOLD, _n)):
            _bhi = _arr_high[_j];  _blo = _arr_low[_j]
            if _blo <= _entry - 240:
                _pnl, _rtype = 240.0, "TP"; _exit_bar = _j; break
            if _bhi >= _entry + 60:
                _pnl, _rtype = -60.0, "SL"; _exit_bar = _j; break

        if _pnl is None:
            _close_idx = min(_ei + MAX_HOLD - 1, _n - 1)
            _pnl   = float(_entry - _arr_close[_close_idx])
            _rtype = "TIME"
            _exit_bar = _close_idx

        _pnl -= COMMISSION_PT

        _trades3.append({
            "datetime":      df["datetime"].iloc[_ei],
            "exit_datetime": df["datetime"].iloc[_exit_bar],
            "pnl":           _pnl,
            "result":        _rtype,
        })

    t3_raw = pd.DataFrame(_trades3) if _trades3 else pd.DataFrame(
        columns=["datetime", "exit_datetime", "pnl", "result"])
    t3_no = apply_no_overlap(t3_raw)
    t3_no["month"] = pd.to_datetime(t3_no["datetime"]).dt.month
    t3_no["yen"]   = t3_no["pnl"] * PT_YEN

    t3_mo_pnl = {}
    t3_mo_n   = {}
    t3_mo_wr  = {}
    for mo in range(1, 13):
        g = t3_no[t3_no["month"] == mo]
        t3_mo_pnl[mo] = int(g["yen"].sum()) if len(g) > 0 else 0
        t3_mo_n[mo]   = len(g)
        t3_mo_wr[mo]  = float((g["pnl"] > 0).mean() * 100) if len(g) > 0 else 0.0

    s1 = task2_monthly["系統①：月木×夜間"]
    s2 = task2_monthly["系統②：火水×精密"]
    t1_no = s1["trades"];  t2_no = s2["trades"]

    def mo_stats(trades_df, mo):
        g = trades_df[trades_df["month"] == mo] if len(trades_df) > 0 else pd.DataFrame()
        n  = len(g)
        wr = float((g["pnl"] > 0).mean() * 100) if n > 0 else 0.0
        yn = int(g["yen"].sum()) if n > 0 else 0
        return n, wr, yn

    MONTH_JP = ["1月","2月","3月","4月","5月","6月","7月","8月","9月","10月","11月","12月"]
    print(f"\n{'月':<4} {'①件数':>5} {'①勝率':>6} {'①損益(円)':>10}  "
          f"{'②件数':>5} {'②勝率':>6} {'②損益(円)':>10}  "
          f"{'③件数':>5} {'③勝率':>6} {'③損益(円)':>10}  {'3系統合計':>10}")
    print("-" * 90)

    total_col = [0, 0, 0, 0]
    for mo in range(1, 13):
        n1, w1, y1 = mo_stats(t1_no, mo)
        n2, w2, y2 = mo_stats(t2_no, mo)
        n3, w3, y3 = t3_mo_n[mo], t3_mo_wr[mo], t3_mo_pnl[mo]
        tot = y1 + y2 + y3
        total_col[0] += y1;  total_col[1] += y2
        total_col[2] += y3;  total_col[3] += tot
        w1s = f"{w1:.0f}%" if n1 > 0 else "-"
        w2s = f"{w2:.0f}%" if n2 > 0 else "-"
        w3s = f"{w3:.0f}%" if n3 > 0 else "-"
        print(f"{MONTH_JP[mo-1]:<4} {n1:>5} {w1s:>6} {y1:>10,}  "
              f"{n2:>5} {w2s:>6} {y2:>10,}  "
              f"{n3:>5} {w3s:>6} {y3:>10,}  {tot:>10,}")

    print("-" * 90)
    s1s = calc_summary(t1_no);  s2s = calc_summary(t2_no);  s3s = calc_summary(t3_no)
    print(f"{'合計':<4} {s1s['n']:>5} {s1s['win_rate']:>5.1f}% {total_col[0]:>10,}  "
          f"{s2s['n']:>5} {s2s['win_rate']:>5.1f}% {total_col[1]:>10,}  "
          f"{s3s['n']:>5} {s3s['win_rate']:>5.1f}% {total_col[2]:>10,}  {total_col[3]:>10,}")
    print(f"\n  ①PF={s1s['pf']:.3f}  ②PF={s2s['pf']:.3f}  ③PF={s3s['pf']:.3f}  "
          f"3系統合計損益: {total_col[3]:,}円")

    # =========================================
    # 【最終レポート】系統①②③ 年別・年×月クロス集計（重複OK・手数料込み）
    # =========================================

    def _yx_report(name, trades_df):
        """年別 + 年×月クロス集計 + 全体サマリーを出力（重複OK）"""
        if len(trades_df) == 0:
            print(f"\n{name}: データなし"); return
        t = trades_df.copy()
        t["_yr"]  = pd.to_datetime(t["datetime"]).dt.year
        t["_mo"]  = pd.to_datetime(t["datetime"]).dt.month
        t["_yen"] = t["pnl"] * PT_YEN

        CW    = 18
        YEARS = sorted(t["_yr"].unique())
        s_all = calc_summary(t)
        ev_a  = s_all["pnl"] / s_all["n"] if s_all["n"] > 0 else 0.0

        print(f"\n{'='*80}")
        print(f"【最終レポート】{name}  （重複OK・手数料込み {COMMISSION_PT}pt）")
        print(f"{'='*80}")

        # --- 年別成績 ---
        print(f"\n■ 年別成績")
        print(f"{'年':>5} {'件数':>6} {'勝率%':>7} {'損益(pt)':>10} {'損益(円)':>12} {'期待値':>8} {'PF':>6}")
        print("-" * 58)
        for yr in YEARS:
            g  = t[t["_yr"] == yr]
            gs = calc_summary(g)
            ev = gs["pnl"] / gs["n"] if gs["n"] > 0 else 0.0
            print(f"  {yr}  {gs['n']:>6} {gs['win_rate']:>6.1f}% "
                  f"{gs['pnl']:>10.1f} {g['_yen'].sum():>12,.0f} {ev:>8.2f} {gs['pf']:>6.3f}")
        print("-" * 58)
        print(f"  {'合計':>4}  {s_all['n']:>6} {s_all['win_rate']:>6.1f}% "
              f"{s_all['pnl']:>10.1f} {t['_yen'].sum():>12,.0f} {ev_a:>8.2f} {s_all['pf']:>6.3f}")

        # --- 年×月クロス集計 ---
        print(f"\n■ 年×月クロス集計（件数/勝率%/損益円）")
        for label, mrange in [("前半 1〜6月", range(1, 7)), ("後半 7〜12月", range(7, 13))]:
            hdr = f"{'年':>4}  " + "  ".join(f"{f'{m}月':^{CW}}" for m in mrange) \
                  + f"  {'半期合計':^{CW}}"
            sep = "-" * len(hdr)
            print(f"\n{label}");  print(sep);  print(hdr);  print(sep)
            mo_acc = {m: {"n": 0, "win": 0, "yen": 0.0} for m in mrange}
            for yr in YEARS:
                cells = [];  h_n = 0;  h_win = 0;  h_yen = 0.0
                for mo in mrange:
                    g = t[(t["_yr"] == yr) & (t["_mo"] == mo)]
                    n = len(g)
                    if n == 0:
                        cells.append(f"{'---':^{CW}}")
                    else:
                        win_n = int((g["pnl"] > 0).sum())
                        yn    = float(g["_yen"].sum())
                        cell  = f"{n}/{win_n/n*100:.0f}%/{int(yn):,}"
                        cells.append(f"{cell:^{CW}}")
                        mo_acc[mo]["n"]   += n
                        mo_acc[mo]["win"] += win_n
                        mo_acc[mo]["yen"] += yn
                        h_n += n;  h_win += win_n;  h_yen += yn
                h_cell = f"{h_n}/{h_win/h_n*100:.0f}%/{int(h_yen):,}" if h_n > 0 else "---"
                print(f"{yr}  " + "  ".join(cells) + f"  {h_cell:^{CW}}")
            print(sep)
            tot_cells = [];  tt_n = 0;  tt_win = 0;  tt_yen = 0.0
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
        print(f"  件数={s_all['n']:,}  勝率={s_all['win_rate']:.1f}%  "
              f"損益={s_all['pnl']:.1f}pt / {t['_yen'].sum():,.0f}円  "
              f"期待値={ev_a:.2f}pt  PF={s_all['pf']:.3f}")

    # 系統①②を重複OKで再実行
    t1_raw = run_backtest_custom(df, 9, 10, 60, 240, "long",
                                 day_filter="mon_thu",
                                 hour_list=[18, 19, 20, 21, 22, 23],
                                 exclude_months=[3, 7],
                                 commission=COMMISSION_PT)
    t2_raw = run_backtest_custom(df, 9, 10, 60, 240, "long",
                                 day_filter="tue_wed",
                                 vol_min=2.0, use_bb_expand=True,
                                 exclude_months=[5, 6, 7, 8, 9],
                                 commission=COMMISSION_PT)

    _yx_report("系統① MA9/10 SL60 TP240 long 月木 18〜23時 3・7月除外", t1_raw)
    _yx_report("系統② MA9/10 SL60 TP240 long 火水 vol>=2.0 BB拡大 5〜9月除外", t2_raw)

    # =========================================
    # 3系統合算 年×月クロス集計
    # =========================================
    print(f"\n{'='*80}")
    print("【最終レポート】3系統合算  （重複OK・手数料込み）")
    print(f"{'='*80}")

    def _prep(trades_df):
        t = trades_df.copy()
        t["_yr"]  = pd.to_datetime(t["datetime"]).dt.year
        t["_mo"]  = pd.to_datetime(t["datetime"]).dt.month
        t["_yen"] = t["pnl"] * PT_YEN
        return t

    r1 = _prep(t1_raw)
    r2 = _prep(t2_raw)
    r3 = _prep(t3_raw)
    YEARS_ALL = sorted(set(r1["_yr"].tolist() + r2["_yr"].tolist() + r3["_yr"].tolist()))
    CW = 18

    # 年別合算
    print(f"\n■ 年別成績（合算）")
    print(f"{'年':>5} {'①件数':>6} {'②件数':>6} {'③件数':>6} {'合計件数':>8} "
          f"{'①損益(円)':>12} {'②損益(円)':>12} {'③損益(円)':>12} {'合計損益(円)':>13}")
    print("-" * 85)
    grand_yen = 0.0
    for yr in YEARS_ALL:
        g1 = r1[r1["_yr"]==yr];  g2 = r2[r2["_yr"]==yr];  g3 = r3[r3["_yr"]==yr]
        y1 = g1["_yen"].sum();   y2 = g2["_yen"].sum();   y3 = g3["_yen"].sum()
        tot_yr = y1 + y2 + y3;  grand_yen += tot_yr
        print(f"  {yr}  {len(g1):>6} {len(g2):>6} {len(g3):>6} {len(g1)+len(g2)+len(g3):>8} "
              f"{int(y1):>12,} {int(y2):>12,} {int(y3):>12,} {int(tot_yr):>13,}")
    print("-" * 85)
    print(f"  {'合計':>4}  {len(r1):>6} {len(r2):>6} {len(r3):>6} {len(r1)+len(r2)+len(r3):>8} "
          f"{int(r1['_yen'].sum()):>12,} {int(r2['_yen'].sum()):>12,} "
          f"{int(r3['_yen'].sum()):>12,} {int(grand_yen):>13,}")

    # 月別合算（年×月クロス、損益円のみ）
    print(f"\n■ 年×月クロス集計 - 3系統合算損益（円）")
    for label, mrange in [("前半 1〜6月", range(1, 7)), ("後半 7〜12月", range(7, 13))]:
        hdr = f"{'年':>4}  " + "  ".join(f"{f'{m}月':^{CW}}" for m in mrange) \
              + f"  {'半期合計':^{CW}}"
        sep = "-" * len(hdr)
        print(f"\n{label}");  print(sep);  print(hdr);  print(sep)
        mo_acc = {m: 0.0 for m in mrange}
        for yr in YEARS_ALL:
            cells = [];  h_yen = 0.0
            for mo in mrange:
                y = (r1[(r1["_yr"]==yr)&(r1["_mo"]==mo)]["_yen"].sum()
                   + r2[(r2["_yr"]==yr)&(r2["_mo"]==mo)]["_yen"].sum()
                   + r3[(r3["_yr"]==yr)&(r3["_mo"]==mo)]["_yen"].sum())
                mo_acc[mo] += y;  h_yen += y
                cells.append(f"{int(y):,}".center(CW))
            print(f"{yr}  " + "  ".join(cells) + f"  {int(h_yen):,}".center(CW))
        print(sep)
        tot_cells = [f"{int(mo_acc[m]):,}".center(CW) for m in mrange]
        tt_yen    = sum(mo_acc[m] for m in mrange)
        print(f"{'合計':>4}  " + "  ".join(tot_cells) + f"  {int(tt_yen):,}".center(CW))

    s1a = calc_summary(t1_raw);  s2a = calc_summary(t2_raw);  s3a = calc_summary(t3_raw)
    all_n   = s1a["n"] + s2a["n"] + s3a["n"]
    all_yen = int(r1["_yen"].sum() + r2["_yen"].sum() + r3["_yen"].sum())
    print(f"\n■ 3系統全体サマリー")
    print(f"  ① 件数={s1a['n']:,}  勝率={s1a['win_rate']:.1f}%  "
          f"損益={int(r1['_yen'].sum()):,}円  PF={s1a['pf']:.3f}")
    print(f"  ② 件数={s2a['n']:,}  勝率={s2a['win_rate']:.1f}%  "
          f"損益={int(r2['_yen'].sum()):,}円  PF={s2a['pf']:.3f}")
    print(f"  ③ 件数={s3a['n']:,}  勝率={s3a['win_rate']:.1f}%  "
          f"損益={int(r3['_yen'].sum()):,}円  PF={s3a['pf']:.3f}")
    print(f"  合計 件数={all_n:,}  合計損益={all_yen:,}円")
    print()


if __name__ == "__main__":
    main()
