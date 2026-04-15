from pathlib import Path
from itertools import combinations
import pandas as pd
import numpy as np

# =========================================
# 設定
# =========================================
DATA_DIR = Path(r"C:\kabu_trade\data")

FILES = [
    "N225microf_2023.xlsx",
    "N225microf_2024.xlsx",
    "N225microf_2025.xlsx",
    "N225microf_2026.xlsx",
]

SHEET_NAME = "5min"

HORIZON = 6          # 何本先まで見るか
DOWN_TH = 80         # 何円下がったら「大きく下がった」とみなすか
MIN_SUPPORT = 80     # 最低サンプル数
MAX_COMBO = 3        # 1条件 / 2条件 / 3条件まで
TOP_N = 100          # 表示件数


# =========================================
# 読み込み
# =========================================
def read_one_file(path: Path, sheet_name: str = "5min") -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"ファイルがありません: {path}")

    if path.suffix.lower() == ".csv":
        df = pd.read_csv(path)
    else:
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

    if "datetime" not in df.columns:
        if "date" in df.columns and "time" in df.columns:
            df["datetime"] = pd.to_datetime(
                df["date"].astype(str).str.strip() + " " + df["time"].astype(str).str.strip(),
                errors="coerce"
            )
        else:
            raise ValueError(f"datetime列も date/time列もありません: {path.name}")

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
        print(f"  読み込み: {path}")
        df = read_one_file(path, sheet_name=SHEET_NAME)
        print(f"    {len(df)} 本")
        dfs.append(df)

    df = pd.concat(dfs, ignore_index=True)
    df = df.sort_values("datetime").drop_duplicates(subset=["datetime"]).reset_index(drop=True)
    print(f"合計読み込み本数: {len(df)}")
    return df


# =========================================
# 指標
# =========================================
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    ts = pd.to_datetime(df["datetime"])

    df["hour"] = ts.dt.hour
    df["minute"] = ts.dt.minute
    df["weekday"] = ts.dt.day_name()
    df["date_only"] = ts.dt.date

    # MA
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma50"] = df["close"].rolling(50).mean()
    df["ma5_slope"] = df["ma5"] - df["ma5"].shift(1)
    df["ma20_slope"] = df["ma20"] - df["ma20"].shift(1)
    df["ma50_slope"] = df["ma50"] - df["ma50"].shift(1)

    # MA乖離
    df["dist_ma5_pct"] = (df["close"] / df["ma5"] - 1.0) * 100
    df["dist_ma20_pct"] = (df["close"] / df["ma20"] - 1.0) * 100
    df["dist_ma50_pct"] = (df["close"] / df["ma50"] - 1.0) * 100

    # 出来高
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma20"]

    # ATR
    prev_close = df["close"].shift(1)
    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - prev_close).abs()
    tr3 = (df["low"] - prev_close).abs()
    df["tr"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["atr14"] = df["tr"].rolling(14).mean()
    df["atr20"] = df["tr"].rolling(20).mean()
    df["atr_ratio"] = df["atr14"] / df["atr20"]

    # BB
    df["bb_mid20"] = df["close"].rolling(20).mean()
    df["bb_std20"] = df["close"].rolling(20).std()
    df["bb_upper_2"] = df["bb_mid20"] + 2 * df["bb_std20"]
    df["bb_lower_2"] = df["bb_mid20"] - 2 * df["bb_std20"]
    df["bb_upper_3"] = df["bb_mid20"] + 3 * df["bb_std20"]
    df["bb_lower_3"] = df["bb_mid20"] - 3 * df["bb_std20"]
    df["bb_width"] = (df["bb_upper_2"] - df["bb_lower_2"]) / df["bb_mid20"]

    # RSI
    diff = df["close"].diff()
    up = diff.clip(lower=0)
    down = -diff.clip(upper=0)
    roll_up = up.rolling(14).mean()
    roll_down = down.rolling(14).mean()
    rs = roll_up / roll_down.replace(0, np.nan)
    df["rsi14"] = 100 - (100 / (1 + rs))

    # MACD
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    # ストキャス
    low14 = df["low"].rolling(14).min()
    high14 = df["high"].rolling(14).max()
    df["stoch_k"] = (df["close"] - low14) / (high14 - low14).replace(0, np.nan) * 100
    df["stoch_d"] = df["stoch_k"].rolling(3).mean()

    # CCI
    tp = (df["high"] + df["low"] + df["close"]) / 3
    sma_tp = tp.rolling(20).mean()
    mad = tp.rolling(20).apply(lambda x: np.mean(np.abs(x - np.mean(x))), raw=True)
    df["cci20"] = (tp - sma_tp) / (0.015 * mad.replace(0, np.nan))

    # ROC
    df["roc5"] = (df["close"] / df["close"].shift(5) - 1.0) * 100

    # ローソク
    df["body"] = (df["close"] - df["open"]).abs()
    df["range"] = df["high"] - df["low"]
    df["body_ratio"] = df["body"] / df["range"].replace(0, np.nan)
    df["upper_wick"] = df["high"] - df[["open", "close"]].max(axis=1)
    df["lower_wick"] = df[["open", "close"]].min(axis=1) - df["low"]
    df["upper_wick_ratio"] = df["upper_wick"] / df["range"].replace(0, np.nan)
    df["lower_wick_ratio"] = df["lower_wick"] / df["range"].replace(0, np.nan)
    df["is_bull"] = df["close"] > df["open"]
    df["is_bear"] = df["close"] < df["open"]

    # 前足比較
    df["close_change"] = df["close"] - df["close"].shift(1)
    df["close_change_pct"] = (df["close"] / df["close"].shift(1) - 1.0) * 100
    df["high_break_3"] = df["close"] > df["high"].shift(1).rolling(3).max()
    df["low_break_3"] = df["close"] < df["low"].shift(1).rolling(3).min()

    # ギャップ
    daily = df.groupby("date_only").agg(
        day_open=("open", "first"),
        day_close=("close", "last"),
    ).reset_index()
    daily["prev_day_close"] = daily["day_close"].shift(1)
    daily["gap_pct"] = (daily["day_open"] / daily["prev_day_close"] - 1.0) * 100
    gap_map = dict(zip(daily["date_only"], daily["gap_pct"]))
    df["gap_pct"] = df["date_only"].map(gap_map)

    return df


# =========================================
# 下落判定
# =========================================
def add_targets(df: pd.DataFrame, horizon=6, down_th=80) -> pd.DataFrame:
    df = df.copy()

    future_low = pd.concat(
        [df["low"].shift(-k) for k in range(1, horizon + 1)],
        axis=1
    ).min(axis=1)

    future_close = df["close"].shift(-horizon)

    df["future_low_h"] = future_low
    df["future_close_h"] = future_close
    df["down_move"] = future_low - df["close"]
    df["down_big"] = df["down_move"] <= -down_th
    df["down_close_only"] = (future_close - df["close"]) <= -down_th

    return df


# =========================================
# 条件セット
# =========================================
def build_conditions(df: pd.DataFrame):
    c = {}

    # トレンド
    c["ma20_down"] = df["ma20_slope"] < 0
    c["ma50_down"] = df["ma50_slope"] < 0
    c["below_ma20"] = df["close"] < df["ma20"]
    c["below_ma50"] = df["close"] < df["ma50"]
    c["far_above_ma20"] = df["dist_ma20_pct"] >= 0.5
    c["far_below_ma20"] = df["dist_ma20_pct"] <= -0.5

    # BB
    c["bb_upper_2_touch"] = df["close"] >= df["bb_upper_2"]
    c["bb_upper_3_touch"] = df["close"] >= df["bb_upper_3"]
    c["bb_lower_2_touch"] = df["close"] <= df["bb_lower_2"]
    c["bb_width_wide"] = df["bb_width"] >= df["bb_width"].rolling(100).quantile(0.7)

    # RSI
    c["rsi_ge_70"] = df["rsi14"] >= 70
    c["rsi_ge_75"] = df["rsi14"] >= 75
    c["rsi_le_30"] = df["rsi14"] <= 30

    # MACD
    c["macd_hist_down"] = df["macd_hist"] < df["macd_hist"].shift(1)
    c["macd_below_signal"] = df["macd"] < df["macd_signal"]
    c["macd_positive_but_weakening"] = (df["macd"] > 0) & (df["macd_hist"] < df["macd_hist"].shift(1))

    # ストキャス
    c["stoch_overbought"] = df["stoch_k"] >= 80
    c["stoch_dead"] = (df["stoch_k"] < df["stoch_d"]) & (df["stoch_k"].shift(1) >= df["stoch_d"].shift(1))

    # CCI
    c["cci_ge_100"] = df["cci20"] >= 100
    c["cci_ge_150"] = df["cci20"] >= 150
    c["cci_falling"] = df["cci20"] < df["cci20"].shift(1)

    # ROC
    c["roc5_ge_0_5"] = df["roc5"] >= 0.5
    c["roc5_ge_1"] = df["roc5"] >= 1.0
    c["roc5_turn_down"] = df["roc5"] < df["roc5"].shift(1)

    # 出来高
    c["vol_ratio_ge_1_2"] = df["vol_ratio"] >= 1.2
    c["vol_ratio_ge_1_5"] = df["vol_ratio"] >= 1.5
    c["vol_ratio_ge_2_0"] = df["vol_ratio"] >= 2.0

    # ATR
    c["atr_high"] = df["atr_ratio"] >= 1.1
    c["atr_low"] = df["atr_ratio"] <= 0.9

    # ローソク
    c["bear_bar"] = df["is_bear"]
    c["bull_bar"] = df["is_bull"]
    c["upper_wick_long"] = df["upper_wick_ratio"] >= 0.4
    c["lower_wick_long"] = df["lower_wick_ratio"] >= 0.4
    c["body_large"] = df["body_ratio"] >= 0.6
    c["body_small"] = df["body_ratio"] <= 0.3

    # ブレイク
    c["high_break_3"] = df["high_break_3"].fillna(False)
    c["low_break_3"] = df["low_break_3"].fillna(False)

    # 変化
    c["close_up"] = df["close_change"] > 0
    c["close_down"] = df["close_change"] < 0
    c["close_up_big"] = df["close_change_pct"] >= 0.25
    c["close_down_big"] = df["close_change_pct"] <= -0.25

    # ギャップ
    c["gap_up"] = df["gap_pct"] >= 0.2
    c["gap_down"] = df["gap_pct"] <= -0.2
    c["gap_flat"] = (df["gap_pct"] > -0.1) & (df["gap_pct"] < 0.1)

    # 時間帯
    c["hour_9_10"] = df["hour"].isin([9, 10])
    c["hour_11_12"] = df["hour"].isin([11, 12])
    c["hour_13_15"] = df["hour"].isin([13, 14, 15])
    c["night_open"] = df["hour"].isin([16, 17, 18])
    c["late_night"] = df["hour"].isin([22, 23, 0, 1, 2])

    # 曜日
    c["monday"] = df["weekday"] == "Monday"
    c["friday"] = df["weekday"] == "Friday"

    return {k: v.fillna(False) for k, v in c.items()}


# =========================================
# 評価
# =========================================
def evaluate_mask(df: pd.DataFrame, mask: pd.Series, name: str):
    grp = df[mask].copy()
    n = len(grp)
    if n < MIN_SUPPORT:
        return None

    hit_rate = grp["down_big"].mean() * 100
    base_rate = df["down_big"].mean() * 100
    edge = hit_rate - base_rate

    avg_down = grp["down_move"].mean()
    median_down = grp["down_move"].median()

    return {
        "combo": name,
        "n": int(n),
        "down_big_rate_pct": round(hit_rate, 2),
        "base_rate_pct": round(base_rate, 2),
        "edge_pct": round(edge, 2),
        "avg_down_move": round(avg_down, 2),
        "median_down_move": round(median_down, 2),
    }


def run_search(df: pd.DataFrame, conditions: dict, max_combo=3):
    rows = []
    names = list(conditions.keys())

    for r in range(1, max_combo + 1):
        for combo in combinations(names, r):
            mask = conditions[combo[0]].copy()
            for name in combo[1:]:
                mask &= conditions[name]

            row = evaluate_mask(df, mask, " + ".join(combo))
            if row is not None:
                rows.append(row)

    out = pd.DataFrame(rows)
    if len(out) == 0:
        return out

    out = out.sort_values(
        ["edge_pct", "down_big_rate_pct", "n"],
        ascending=[False, False, False]
    ).reset_index(drop=True)
    return out


# =========================================
# 追加集計
# =========================================
def single_condition_report(df: pd.DataFrame, conditions: dict):
    rows = []
    for name, mask in conditions.items():
        row = evaluate_mask(df, mask, name)
        if row is not None:
            rows.append(row)

    out = pd.DataFrame(rows)
    if len(out) == 0:
        return out

    return out.sort_values(
        ["edge_pct", "down_big_rate_pct", "n"],
        ascending=[False, False, False]
    ).reset_index(drop=True)


def save_outputs(single_df: pd.DataFrame, combo_df: pd.DataFrame):
    out_dir = Path(r"C:\kabu_trade")
    single_path = out_dir / "down_single_report.csv"
    combo_path = out_dir / "down_combo_report.csv"

    single_df.to_csv(single_path, index=False, encoding="utf-8-sig")
    combo_df.to_csv(combo_path, index=False, encoding="utf-8-sig")

    print(f"\n保存: {single_path}")
    print(f"保存: {combo_path}")

# =========================================
# トリガー候補
# =========================================
def setup_short_mask(df: pd.DataFrame) -> pd.Series:
    return (
        (df["close"] >= df["bb_upper_3"]) &
        (df["vol_ratio"] >= 1.5)
    ).fillna(False)


def trigger_next_open(df: pd.DataFrame, i: int) -> bool:
    return True


def trigger_break_prev_low(df: pd.DataFrame, i: int) -> bool:
    if i < 1:
        return False
    return df.iloc[i]["close"] < df.iloc[i - 1]["low"]


def trigger_bear_bar(df: pd.DataFrame, i: int) -> bool:
    return df.iloc[i]["close"] < df.iloc[i]["open"]


def trigger_two_bear_bars(df: pd.DataFrame, i: int) -> bool:
    if i < 1:
        return False
    return (
        (df.iloc[i]["close"] < df.iloc[i]["open"]) and
        (df.iloc[i - 1]["close"] < df.iloc[i - 1]["open"])
    )


def trigger_fail_rebound(df: pd.DataFrame, i: int) -> bool:
    if i < 1:
        return False
    cur = df.iloc[i]
    prev = df.iloc[i - 1]
    return (
        cur["high"] <= prev["high"] and
        cur["close"] < cur["open"]
    )


def trigger_break_2bar_low(df: pd.DataFrame, i: int) -> bool:
    if i < 2:
        return False
    prev2_low = min(df.iloc[i - 1]["low"], df.iloc[i - 2]["low"])
    return df.iloc[i]["close"] < prev2_low


# =========================================
# ショート用バックテスト
# =========================================
def run_backtest_short_trigger(df: pd.DataFrame, trigger_func, trigger_name: str):
    setup = setup_short_mask(df)
    trades = []

    for i in range(len(df) - 2):
        if not setup.iloc[i]:
            continue

        trigger_idx = i + 1
        if trigger_idx >= len(df) - 1:
            continue

        if not trigger_func(df, trigger_idx):
            continue

        entry_idx = trigger_idx + 1
        if entry_idx >= len(df):
            continue

        entry = float(df.iloc[entry_idx]["open"])
        entry_dt = pd.to_datetime(df.iloc[entry_idx]["datetime"])

        closed = False
        for j in range(1, 7):
            k = entry_idx + j
            if k >= len(df):
                break

            bar = df.iloc[k]

            # 利確: 120円下
            if bar["low"] <= entry - 120:
                trades.append({
                    "trigger": trigger_name,
                    "entry_time": entry_dt,
                    "pnl": 98.0,   # 120 - 22
                    "result": "TP",
                    "weekday": entry_dt.day_name(),
                    "hour": entry_dt.hour,
                })
                closed = True
                break

            # 損切: 40円上
            if bar["high"] >= entry + 40:
                trades.append({
                    "trigger": trigger_name,
                    "entry_time": entry_dt,
                    "pnl": -62.0,  # -40 - 22
                    "result": "SL",
                    "weekday": entry_dt.day_name(),
                    "hour": entry_dt.hour,
                })
                closed = True
                break

            # 時間切れ
            if j == 6:
                pnl = float(entry - bar["close"] - 22)
                trades.append({
                    "trigger": trigger_name,
                    "entry_time": entry_dt,
                    "pnl": pnl,
                    "result": "TIME",
                    "weekday": entry_dt.day_name(),
                    "hour": entry_dt.hour,
                })
                closed = True

        if not closed:
            continue

    if len(trades) == 0:
        return pd.DataFrame(columns=["trigger", "entry_time", "pnl", "result", "weekday", "hour"])

    return pd.DataFrame(trades)


def run_trigger_search(df: pd.DataFrame):
    trigger_map = {
        "next_open": trigger_next_open,
        "break_prev_low": trigger_break_prev_low,
        "bear_bar": trigger_bear_bar,
        "two_bear_bars": trigger_two_bear_bars,
        "fail_rebound": trigger_fail_rebound,
        "break_2bar_low": trigger_break_2bar_low,
    }

    rows = []

    for name, func in trigger_map.items():
        trades = run_backtest_short_trigger(df, func, name)

        if len(trades) == 0:
            rows.append({
                "trigger": name,
                "n": 0,
                "win_rate": 0.0,
                "pnl": 0.0,
                "pf": 0.0,
            })
            continue

        pnl = trades["pnl"]
        win = pnl[pnl > 0].sum()
        loss = abs(pnl[pnl < 0].sum())
        pf = win / loss if loss != 0 else 0.0

        rows.append({
            "trigger": name,
            "n": len(trades),
            "win_rate": round((pnl > 0).mean() * 100, 2),
            "pnl": round(pnl.sum(), 2),
            "pf": round(pf, 2),
        })

    out = pd.DataFrame(rows)
    return out.sort_values(["pf", "pnl", "n"], ascending=[False, False, False]).reset_index(drop=True)

# =========================================
# メイン
# =========================================
def main():
    df = load_all()
    df = add_indicators(df)
    df = df.dropna().reset_index(drop=True)

    print("\n===== overlap_only =====")
    t = run_backtest(df, signal_overlap_only)
    print_details("overlap_only", t)
    print_monthly(t)
    print_month_full_stats(t)
    print_total_stats(t, "overlap_only")

    print("\n===== ショート候補 =====")
    shorts = find_short_patterns(df)
    print(shorts.head(30).to_string(index=False))

if __name__ == "__main__":
    main()