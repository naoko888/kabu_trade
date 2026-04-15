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

# 未来判定
HORIZON = 2          # 2本先（10分）
DOWN_TH = 40         # 40円以上下げたら「下げ」
UP_TH = 40           # 40円以上上げたら「上げ」

# 組み合わせ探索
MIN_SUPPORT = 80     # 最低サンプル数
MAX_COMBO = 3        # 1条件 / 2条件 / 3条件
TOP_N = 300

OUT_DIR = Path(r"C:\kabu_trade")

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
# 指標追加
# =========================================
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    ts = pd.to_datetime(df["datetime"])

    df["hour"] = ts.dt.hour
    df["minute"] = ts.dt.minute
    df["weekday"] = ts.dt.day_name()
    df["date_only"] = ts.dt.date

    # =====================================
    # MA系
    # =====================================
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma50"] = df["close"].rolling(50).mean()

    df["ma5_slope"] = df["ma5"] - df["ma5"].shift(1)
    df["ma10_slope"] = df["ma10"] - df["ma10"].shift(1)
    df["ma20_slope"] = df["ma20"] - df["ma20"].shift(1)
    df["ma50_slope"] = df["ma50"] - df["ma50"].shift(1)

    df["dist_ma5_pct"] = (df["close"] / df["ma5"] - 1.0) * 100
    df["dist_ma10_pct"] = (df["close"] / df["ma10"] - 1.0) * 100
    df["dist_ma20_pct"] = (df["close"] / df["ma20"] - 1.0) * 100
    df["dist_ma50_pct"] = (df["close"] / df["ma50"] - 1.0) * 100

    # =====================================
    # 出来高
    # =====================================
    df["vol_ma5"] = df["volume"].rolling(5).mean()
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["vol_ratio5"] = df["volume"] / df["vol_ma5"]
    df["vol_ratio20"] = df["volume"] / df["vol_ma20"]

    # =====================================
    # ATR
    # =====================================
    prev_close = df["close"].shift(1)
    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - prev_close).abs()
    tr3 = (df["low"] - prev_close).abs()
    df["tr"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["atr14"] = df["tr"].rolling(14).mean()
    df["atr20"] = df["tr"].rolling(20).mean()
    df["atr_ratio"] = df["atr14"] / df["atr20"]

    # =====================================
    # ボリンジャー
    # =====================================
    df["bb_mid20"] = df["close"].rolling(20).mean()
    df["bb_std20"] = df["close"].rolling(20).std()
    df["bb_upper_1"] = df["bb_mid20"] + 1 * df["bb_std20"]
    df["bb_upper_2"] = df["bb_mid20"] + 2 * df["bb_std20"]
    df["bb_upper_3"] = df["bb_mid20"] + 3 * df["bb_std20"]
    df["bb_lower_1"] = df["bb_mid20"] - 1 * df["bb_std20"]
    df["bb_lower_2"] = df["bb_mid20"] - 2 * df["bb_std20"]
    df["bb_lower_3"] = df["bb_mid20"] - 3 * df["bb_std20"]
    df["bb_width"] = (df["bb_upper_2"] - df["bb_lower_2"]) / df["bb_mid20"]
    df["bb_pos"] = (df["close"] - df["bb_mid20"]) / (df["bb_std20"].replace(0, np.nan))

    # =====================================
    # RSI
    # =====================================
    diff = df["close"].diff()
    up = diff.clip(lower=0)
    down = -diff.clip(upper=0)
    roll_up = up.rolling(14).mean()
    roll_down = down.rolling(14).mean()
    rs = roll_up / roll_down.replace(0, np.nan)
    df["rsi14"] = 100 - (100 / (1 + rs))

    # =====================================
    # MACD
    # =====================================
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    # =====================================
    # ストキャス
    # =====================================
    low14 = df["low"].rolling(14).min()
    high14 = df["high"].rolling(14).max()
    df["stoch_k"] = (df["close"] - low14) / (high14 - low14).replace(0, np.nan) * 100
    df["stoch_d"] = df["stoch_k"].rolling(3).mean()

    # =====================================
    # CCI
    # =====================================
    tp = (df["high"] + df["low"] + df["close"]) / 3
    sma_tp = tp.rolling(20).mean()
    mad = tp.rolling(20).apply(lambda x: np.mean(np.abs(x - np.mean(x))), raw=True)
    df["cci20"] = (tp - sma_tp) / (0.015 * mad.replace(0, np.nan))

    # =====================================
    # ROC
    # =====================================
    df["roc3"] = (df["close"] / df["close"].shift(3) - 1.0) * 100
    df["roc5"] = (df["close"] / df["close"].shift(5) - 1.0) * 100

    # =====================================
    # ローソク足
    # =====================================
    df["body"] = (df["close"] - df["open"]).abs()
    df["range"] = df["high"] - df["low"]
    df["body_ratio"] = df["body"] / df["range"].replace(0, np.nan)
    df["upper_wick"] = df["high"] - df[["open", "close"]].max(axis=1)
    df["lower_wick"] = df[["open", "close"]].min(axis=1) - df["low"]
    df["upper_wick_ratio"] = df["upper_wick"] / df["range"].replace(0, np.nan)
    df["lower_wick_ratio"] = df["lower_wick"] / df["range"].replace(0, np.nan)
    df["is_bull"] = df["close"] > df["open"]
    df["is_bear"] = df["close"] < df["open"]

    # =====================================
    # 値動き・ブレイク
    # =====================================
    df["close_change"] = df["close"] - df["close"].shift(1)
    df["close_change_pct"] = (df["close"] / df["close"].shift(1) - 1.0) * 100

    df["high_break_1"] = df["close"] > df["high"].shift(1)
    df["low_break_1"] = df["close"] < df["low"].shift(1)

    df["high_break_2"] = df["close"] > df["high"].shift(1).rolling(2).max()
    df["low_break_2"] = df["close"] < df["low"].shift(1).rolling(2).min()

    df["high_break_3"] = df["close"] > df["high"].shift(1).rolling(3).max()
    df["low_break_3"] = df["close"] < df["low"].shift(1).rolling(3).min()

    # =====================================
    # 日足ギャップ
    # =====================================
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
# ターゲット
# =========================================
def add_targets(df: pd.DataFrame, horizon=2, down_th=40, up_th=40) -> pd.DataFrame:
    df = df.copy()

    future_low = pd.concat(
        [df["low"].shift(-k) for k in range(1, horizon + 1)],
        axis=1
    ).min(axis=1)

    future_high = pd.concat(
        [df["high"].shift(-k) for k in range(1, horizon + 1)],
        axis=1
    ).max(axis=1)

    future_close = df["close"].shift(-horizon)

    df["future_low_h"] = future_low
    df["future_high_h"] = future_high
    df["future_close_h"] = future_close

    df["down_move"] = future_low - df["close"]
    df["up_move"] = future_high - df["close"]

    df["down_big"] = df["down_move"] <= -down_th
    df["up_big"] = df["up_move"] >= up_th

    df["not_down_big"] = ~df["down_big"].fillna(False)

    return df


# =========================================
# 条件群
# =========================================
def build_conditions(df: pd.DataFrame):
    c = {}

    # ================================
    # トレンド / MA
    # ================================
    c["ma5_down"] = df["ma5_slope"] < 0
    c["ma10_down"] = df["ma10_slope"] < 0
    c["ma20_down"] = df["ma20_slope"] < 0
    c["ma50_down"] = df["ma50_slope"] < 0

    c["below_ma5"] = df["close"] < df["ma5"]
    c["below_ma10"] = df["close"] < df["ma10"]
    c["below_ma20"] = df["close"] < df["ma20"]
    c["below_ma50"] = df["close"] < df["ma50"]

    c["far_above_ma5"] = df["dist_ma5_pct"] >= 0.3
    c["far_above_ma10"] = df["dist_ma10_pct"] >= 0.4
    c["far_above_ma20"] = df["dist_ma20_pct"] >= 0.5
    c["far_above_ma50"] = df["dist_ma50_pct"] >= 0.8

    c["far_below_ma5"] = df["dist_ma5_pct"] <= -0.3
    c["far_below_ma10"] = df["dist_ma10_pct"] <= -0.4
    c["far_below_ma20"] = df["dist_ma20_pct"] <= -0.5
    c["far_below_ma50"] = df["dist_ma50_pct"] <= -0.8

    # ================================
    # ボリンジャー
    # ================================
    c["bb_upper_1_touch"] = df["close"] >= df["bb_upper_1"]
    c["bb_upper_2_touch"] = df["close"] >= df["bb_upper_2"]
    c["bb_upper_3_touch"] = df["close"] >= df["bb_upper_3"]

    c["bb_lower_1_touch"] = df["close"] <= df["bb_lower_1"]
    c["bb_lower_2_touch"] = df["close"] <= df["bb_lower_2"]
    c["bb_lower_3_touch"] = df["close"] <= df["bb_lower_3"]

    c["bb_pos_ge_1"] = df["bb_pos"] >= 1
    c["bb_pos_ge_2"] = df["bb_pos"] >= 2
    c["bb_pos_le_minus1"] = df["bb_pos"] <= -1
    c["bb_pos_le_minus2"] = df["bb_pos"] <= -2

    c["bb_width_expand"] = df["bb_width"] > df["bb_width"].shift(1)
    c["bb_width_wide"] = df["bb_width"] >= df["bb_width"].rolling(100).quantile(0.7)

    # ================================
    # RSI
    # ================================
    c["rsi_ge_60"] = df["rsi14"] >= 60
    c["rsi_ge_70"] = df["rsi14"] >= 70
    c["rsi_ge_75"] = df["rsi14"] >= 75
    c["rsi_le_40"] = df["rsi14"] <= 40
    c["rsi_le_30"] = df["rsi14"] <= 30
    c["rsi_falling"] = df["rsi14"] < df["rsi14"].shift(1)

    # ================================
    # MACD
    # ================================
    c["macd_below_signal"] = df["macd"] < df["macd_signal"]
    c["macd_dead_cross"] = (df["macd"] < df["macd_signal"]) & (df["macd"].shift(1) >= df["macd_signal"].shift(1))
    c["macd_hist_down"] = df["macd_hist"] < df["macd_hist"].shift(1)
    c["macd_hist_down_2"] = (df["macd_hist"] < df["macd_hist"].shift(1)) & (df["macd_hist"].shift(1) < df["macd_hist"].shift(2))
    c["macd_positive_weakening"] = (df["macd"] > 0) & (df["macd_hist"] < df["macd_hist"].shift(1))

    # ================================
    # ストキャス
    # ================================
    c["stoch_overbought"] = df["stoch_k"] >= 80
    c["stoch_oversold"] = df["stoch_k"] <= 20
    c["stoch_dead_cross"] = (df["stoch_k"] < df["stoch_d"]) & (df["stoch_k"].shift(1) >= df["stoch_d"].shift(1))
    c["stoch_falling"] = df["stoch_k"] < df["stoch_k"].shift(1)

    # ================================
    # CCI
    # ================================
    c["cci_ge_100"] = df["cci20"] >= 100
    c["cci_ge_150"] = df["cci20"] >= 150
    c["cci_le_minus100"] = df["cci20"] <= -100
    c["cci_falling"] = df["cci20"] < df["cci20"].shift(1)

    # ================================
    # ROC
    # ================================
    c["roc3_ge_0_3"] = df["roc3"] >= 0.3
    c["roc3_ge_0_5"] = df["roc3"] >= 0.5
    c["roc5_ge_0_5"] = df["roc5"] >= 0.5
    c["roc5_ge_1_0"] = df["roc5"] >= 1.0
    c["roc3_turn_down"] = df["roc3"] < df["roc3"].shift(1)
    c["roc5_turn_down"] = df["roc5"] < df["roc5"].shift(1)

    # ================================
    # 出来高
    # ================================
    c["vol_ratio5_ge_1_2"] = df["vol_ratio5"] >= 1.2
    c["vol_ratio5_ge_1_5"] = df["vol_ratio5"] >= 1.5
    c["vol_ratio20_ge_1_2"] = df["vol_ratio20"] >= 1.2
    c["vol_ratio20_ge_1_5"] = df["vol_ratio20"] >= 1.5
    c["vol_ratio20_ge_2_0"] = df["vol_ratio20"] >= 2.0
    c["vol_down_vs_prev"] = df["volume"] < df["volume"].shift(1)

    # ================================
    # ATR
    # ================================
    c["atr_high"] = df["atr_ratio"] >= 1.1
    c["atr_low"] = df["atr_ratio"] <= 0.9

    # ================================
    # ローソク足
    # ================================
    c["bear_bar"] = df["is_bear"]
    c["bull_bar"] = df["is_bull"]
    c["body_large"] = df["body_ratio"] >= 0.6
    c["body_small"] = df["body_ratio"] <= 0.3
    c["upper_wick_long"] = df["upper_wick_ratio"] >= 0.4
    c["lower_wick_long"] = df["lower_wick_ratio"] >= 0.4
    c["upper_wick_gt_body"] = df["upper_wick"] > df["body"]
    c["lower_wick_gt_body"] = df["lower_wick"] > df["body"]

    # ================================
    # 値動き
    # ================================
    c["close_up"] = df["close_change"] > 0
    c["close_down"] = df["close_change"] < 0
    c["close_up_big"] = df["close_change_pct"] >= 0.2
    c["close_down_big"] = df["close_change_pct"] <= -0.2

    # ================================
    # ブレイク
    # ================================
    c["high_break_1"] = df["high_break_1"].fillna(False)
    c["low_break_1"] = df["low_break_1"].fillna(False)
    c["high_break_2"] = df["high_break_2"].fillna(False)
    c["low_break_2"] = df["low_break_2"].fillna(False)
    c["high_break_3"] = df["high_break_3"].fillna(False)
    c["low_break_3"] = df["low_break_3"].fillna(False)

    # ================================
    # ギャップ
    # ================================
    c["gap_up"] = df["gap_pct"] >= 0.2
    c["gap_down"] = df["gap_pct"] <= -0.2
    c["gap_flat"] = (df["gap_pct"] > -0.1) & (df["gap_pct"] < 0.1)

    # ================================
    # 時間
    # ================================
    c["hour_9_10"] = df["hour"].isin([9, 10])
    c["hour_11_12"] = df["hour"].isin([11, 12])
    c["hour_13_15"] = df["hour"].isin([13, 14, 15])
    c["night_open"] = df["hour"].isin([16, 17, 18])
    c["late_night"] = df["hour"].isin([22, 23, 0, 1, 2])

    # ================================
    # 曜日
    # ================================
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

    down_count = int(grp["down_big"].sum())
    not_down_count = int(grp["not_down_big"].sum())
    up_count = int(grp["up_big"].sum())

    down_rate = grp["down_big"].mean() * 100
    not_down_rate = grp["not_down_big"].mean() * 100
    up_rate = grp["up_big"].mean() * 100

    base_down_rate = df["down_big"].mean() * 100
    edge_down = down_rate - base_down_rate

    avg_down_move = grp["down_move"].mean()
    median_down_move = grp["down_move"].median()

    return {
        "combo": name,
        "n": int(n),
        "down_count": down_count,
        "not_down_count": not_down_count,
        "up_count": up_count,
        "down_rate_pct": round(down_rate, 2),
        "not_down_rate_pct": round(not_down_rate, 2),
        "up_rate_pct": round(up_rate, 2),
        "base_down_rate_pct": round(base_down_rate, 2),
        "edge_down_pct": round(edge_down, 2),
        "avg_down_move": round(avg_down_move, 2),
        "median_down_move": round(median_down_move, 2),
    }


def run_search(df: pd.DataFrame, conditions: dict, max_combo=3):
    rows = []
    names = list(conditions.keys())

    for r in range(1, max_combo + 1):
        print(f"{r}条件組み合わせ検証中...")
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
        ["edge_down_pct", "down_rate_pct", "n"],
        ascending=[False, False, False]
    ).reset_index(drop=True)
    return out


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
        ["edge_down_pct", "down_rate_pct", "n"],
        ascending=[False, False, False]
    ).reset_index(drop=True)


# =========================================
# 下げた時 vs 下げなかった時 比較
# =========================================
def compare_down_vs_not_down(df: pd.DataFrame, feature_cols: list):
    down_df = df[df["down_big"]].copy()
    not_down_df = df[~df["down_big"]].copy()

    rows = []
    for col in feature_cols:
        if col not in df.columns:
            continue

        if pd.api.types.is_bool_dtype(df[col]):
            down_mean = down_df[col].mean() * 100
            not_down_mean = not_down_df[col].mean() * 100
        else:
            down_mean = down_df[col].mean()
            not_down_mean = not_down_df[col].mean()

        rows.append({
            "feature": col,
            "down_mean": round(float(down_mean), 4) if pd.notna(down_mean) else np.nan,
            "not_down_mean": round(float(not_down_mean), 4) if pd.notna(not_down_mean) else np.nan,
            "diff_down_minus_not_down": round(float(down_mean - not_down_mean), 4) if pd.notna(down_mean) and pd.notna(not_down_mean) else np.nan,
            "down_count": int(len(down_df)),
            "not_down_count": int(len(not_down_df)),
        })

    out = pd.DataFrame(rows)
    if len(out) == 0:
        return out

    out = out.sort_values("diff_down_minus_not_down", ascending=False).reset_index(drop=True)
    return out


# =========================================
# 保存
# =========================================
def save_outputs(single_df: pd.DataFrame, combo_df: pd.DataFrame, compare_df: pd.DataFrame):
    single_path = OUT_DIR / "short_down_single_report.csv"
    combo_path = OUT_DIR / "short_down_combo_report.csv"
    compare_path = OUT_DIR / "short_down_vs_notdown_compare.csv"

    single_df.to_csv(single_path, index=False, encoding="utf-8-sig")
    combo_df.to_csv(combo_path, index=False, encoding="utf-8-sig")
    compare_df.to_csv(compare_path, index=False, encoding="utf-8-sig")

    print(f"\n保存: {single_path}")
    print(f"保存: {combo_path}")
    print(f"保存: {compare_path}")


# =========================================
# メイン
# =========================================
def main():
    print("★★★ ショート用 兆候総検証 開始 ★★★")

    df = load_all()
    df = add_indicators(df)
    df = add_targets(df, horizon=HORIZON, down_th=DOWN_TH, up_th=UP_TH)

    need_cols = [
        "ma5", "ma10", "ma20", "ma50",
        "dist_ma5_pct", "dist_ma10_pct", "dist_ma20_pct", "dist_ma50_pct",
        "vol_ratio5", "vol_ratio20",
        "atr_ratio",
        "bb_upper_1", "bb_upper_2", "bb_upper_3",
        "bb_lower_1", "bb_lower_2", "bb_lower_3",
        "bb_width", "bb_pos",
        "rsi14", "macd", "macd_signal", "macd_hist",
        "stoch_k", "stoch_d", "cci20", "roc3", "roc5",
        "body_ratio", "upper_wick_ratio", "lower_wick_ratio",
        "gap_pct"
    ]
    df = df.dropna(subset=need_cols).reset_index(drop=True)

    print("\n条件作成中...")
    conditions = build_conditions(df)
    print(f"条件数: {len(conditions)}")

    print("\n単体条件レポート作成中...")
    single_df = single_condition_report(df, conditions)

    print("\n組み合わせ総検証中...")
    combo_df = run_search(df, conditions, max_combo=MAX_COMBO)

    feature_cols = [
        "open", "high", "low", "close", "volume",
        "ma5_slope", "ma10_slope", "ma20_slope", "ma50_slope",
        "dist_ma5_pct", "dist_ma10_pct", "dist_ma20_pct", "dist_ma50_pct",
        "vol_ratio5", "vol_ratio20",
        "atr_ratio",
        "bb_width", "bb_pos",
        "rsi14", "macd", "macd_signal", "macd_hist",
        "stoch_k", "stoch_d", "cci20", "roc3", "roc5",
        "body_ratio", "upper_wick_ratio", "lower_wick_ratio",
        "close_change", "close_change_pct",
        "gap_pct",
        "is_bull", "is_bear",
        "high_break_1", "low_break_1", "high_break_2", "low_break_2", "high_break_3", "low_break_3",
    ]

    print("\n下げた時 vs 下げなかった時 比較中...")
    compare_df = compare_down_vs_not_down(df, feature_cols)

    print("\n===== 単体 TOP =====")
    print(single_df.head(TOP_N))

    print("\n===== 組み合わせ TOP =====")
    print(combo_df.head(TOP_N))

    print("\n===== 比較 TOP =====")
    print(compare_df.head(50))

    save_outputs(single_df, combo_df, compare_df)


if __name__ == "__main__":
    main()