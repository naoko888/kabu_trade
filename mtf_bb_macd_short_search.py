from pathlib import Path
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

TP = 120
SL = 40
MAX_HOLD_BARS = 6
ROUND_COST = 22

TOP_N = 200

BB_TFS = ["15min", "30min", "60min"]
MACD_TFS = ["15min", "30min", "60min"]

USE_5M_TRIGGERS = {
    "break_prev_high": True,
    "break_2bar_high": True,
}

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


def load_all():
    dfs = []
    print("データ読み込み中...")
    for f in FILES:
        path = DATA_DIR / f
        print(f"読み込み開始: {path}")
        try:
            df = pd.read_excel(path, sheet_name="5min", engine="openpyxl")
            print(f"  読み込み成功: {len(df)} rows")

            df = df.rename(columns={
                "日付": "date", "時間": "time",
                "始値": "open", "高値": "high",
                "安値": "low", "終値": "close",
                "出来高": "volume"
            })

            df["datetime"] = pd.to_datetime(
                df["date"].astype(str) + " " + df["time"].astype(str),
                errors="coerce"
            )
            df = df[["datetime", "open", "high", "low", "close", "volume"]]
            dfs.append(df)

        except Exception as e:
            print(f"  読み込み失敗: {path}")
            print(f"  エラー: {repr(e)}")
            raise

    df = pd.concat(dfs).sort_values("datetime").reset_index(drop=True)
    print("本数:", len(df))
    return df


# =========================================
# 5分足 指標
# =========================================
def add_indicators_5m(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    ts = pd.to_datetime(df["datetime"])
    df["hour"] = ts.dt.hour
    df["weekday"] = ts.dt.day_name()

    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd_5m"] = ema12 - ema26
    df["macd_signal_5m"] = df["macd_5m"].ewm(span=9, adjust=False).mean()
    df["macd_hist_5m"] = df["macd_5m"] - df["macd_signal_5m"]

    df["is_bear"] = df["close"] < df["open"]

    return df


# =========================================
# 上位足作成
# =========================================
def resample_ohlcv(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    x = df.copy().set_index("datetime")

    out = x.resample(tf, label="right", closed="right").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna().reset_index()

    return out


def add_tf_features(tf_df: pd.DataFrame, tf_name: str) -> pd.DataFrame:
    d = tf_df.copy()

    # BB
    d[f"bb_mid20_{tf_name}"] = d["close"].rolling(20).mean()
    d[f"bb_std20_{tf_name}"] = d["close"].rolling(20).std()
    d[f"bb_upper_2_{tf_name}"] = d[f"bb_mid20_{tf_name}"] + 2 * d[f"bb_std20_{tf_name}"]
    d[f"bb_upper_3_{tf_name}"] = d[f"bb_mid20_{tf_name}"] + 3 * d[f"bb_std20_{tf_name}"]
    d[f"bb_width_{tf_name}"] = (
        (d[f"bb_upper_2_{tf_name}"] - (d[f"bb_mid20_{tf_name}"] - 2 * d[f"bb_std20_{tf_name}"]))
        / d[f"bb_mid20_{tf_name}"]
    )

    # MACD
    ema12 = d["close"].ewm(span=12, adjust=False).mean()
    ema26 = d["close"].ewm(span=26, adjust=False).mean()
    d[f"macd_{tf_name}"] = ema12 - ema26
    d[f"macd_signal_{tf_name}"] = d[f"macd_{tf_name}"].ewm(span=9, adjust=False).mean()
    d[f"macd_hist_{tf_name}"] = d[f"macd_{tf_name}"] - d[f"macd_signal_{tf_name}"]

    keep_cols = ["datetime"]
    keep_cols += [c for c in d.columns if c.endswith(f"_{tf_name}")]
    return d[keep_cols]


def merge_tf_to_5m(base_df: pd.DataFrame, tf_feature_df: pd.DataFrame) -> pd.DataFrame:
    # 上位足確定値を5分足に前方埋め
    out = pd.merge_asof(
        base_df.sort_values("datetime"),
        tf_feature_df.sort_values("datetime"),
        on="datetime",
        direction="backward"
    )
    return out


def build_mtf_features(df_5m: pd.DataFrame) -> pd.DataFrame:
    out = df_5m.copy()

    tf_map = {
        "15min": "15m",
        "30min": "30m",
        "60min": "60m",
    }

    for tf in BB_TFS:
        tf_df = resample_ohlcv(df_5m, tf)
        feat = add_tf_features(tf_df, tf_map[tf])
        out = merge_tf_to_5m(out, feat)

    return out


# =========================================
# 条件
# =========================================
def make_bb_conditions(df: pd.DataFrame, tf_label: str) -> dict:
    c = {}

    close_col = "close"
    bb2 = f"bb_upper_2_{tf_label}"
    bb3 = f"bb_upper_3_{tf_label}"
    mid = f"bb_mid20_{tf_label}"
    width = f"bb_width_{tf_label}"

    c[f"bb2_touch_{tf_label}"] = df[close_col] >= df[bb2]
    c[f"bb3_touch_{tf_label}"] = df[close_col] >= df[bb3]
    c[f"bb_upper_zone_{tf_label}"] = df[close_col] >= (df[mid] + (df[bb2] - df[mid]) * 0.7)
    c[f"bb_width_expand_{tf_label}"] = df[width] > df[width].shift(1)
    c[f"bb_pullback_zone_{tf_label}"] = (
        (df["close"] < df[f"bb_upper_2_{tf_label}"]) &
       (df["close"] > df[f"bb_mid20_{tf_label}"])
)

    return {k: v.fillna(False) for k, v in c.items()}


def make_macd_conditions(df: pd.DataFrame, tf_label: str) -> dict:
    c = {}

    macd = f"macd_{tf_label}"
    signal = f"macd_signal_{tf_label}"
    hist = f"macd_hist_{tf_label}"

    c[f"macd_hist_down_{tf_label}"] = df[hist] < df[hist].shift(1)
    c[f"macd_hist_down_2_{tf_label}"] = (df[hist] < df[hist].shift(1)) & (df[hist].shift(1) < df[hist].shift(2))
    c[f"macd_below_signal_{tf_label}"] = df[macd] < df[signal]
    c[f"macd_dead_cross_{tf_label}"] = (df[macd] < df[signal]) & (df[macd].shift(1) >= df[signal].shift(1))
    c[f"macd_positive_weakening_{tf_label}"] = (df[macd] > 0) & (df[hist] < df[hist].shift(1))

    return {k: v.fillna(False) for k, v in c.items()}

def make_macd_conditions_long(df: pd.DataFrame, tf_label: str) -> dict:
    c = {}

    macd = f"macd_{tf_label}"
    signal = f"macd_signal_{tf_label}"
    hist = f"macd_hist_{tf_label}"

    c[f"macd_above_signal_{tf_label}"] = df[macd] > df[signal]
    c[f"macd_golden_cross_{tf_label}"] = (df[macd] > df[signal]) & (df[macd].shift(1) <= df[signal].shift(1))
    c[f"macd_hist_up_{tf_label}"] = df[hist] > df[hist].shift(1)
    c[f"macd_hist_up_2_{tf_label}"] = (df[hist] > df[hist].shift(1)) & (df[hist].shift(1) > df[hist].shift(2))
    c[f"macd_positive_{tf_label}"] = df[macd] > 0

    return {k: v.fillna(False) for k, v in c.items()}


# =========================================
# 5分トリガー
# =========================================
def trigger_next_open(df: pd.DataFrame, i: int) -> bool:
    return True


def trigger_bear_bar(df: pd.DataFrame, i: int) -> bool:
    return bool(df.iloc[i]["close"] < df.iloc[i]["open"])


def trigger_break_prev_low(df: pd.DataFrame, i: int) -> bool:
    if i < 1:
        return False
    return bool(df.iloc[i]["close"] < df.iloc[i - 1]["low"])


def trigger_break_2bar_low(df: pd.DataFrame, i: int) -> bool:
    if i < 2:
        return False
    prev2_low = min(df.iloc[i - 1]["low"], df.iloc[i - 2]["low"])
    return bool(df.iloc[i]["close"] < prev2_low)


def trigger_two_bear_bars(df: pd.DataFrame, i: int) -> bool:
    if i < 1:
        return False
    return bool(
        (df.iloc[i]["close"] < df.iloc[i]["open"]) and
        (df.iloc[i - 1]["close"] < df.iloc[i - 1]["open"])
    )

def trigger_bull_bar(df: pd.DataFrame, i: int) -> bool:
    return bool(df.iloc[i]["close"] > df.iloc[i]["open"])


def trigger_break_prev_high(df: pd.DataFrame, i: int) -> bool:
    if i < 1:
        return False
    return bool(df.iloc[i]["close"] > df.iloc[i - 1]["high"])


def trigger_break_2bar_high(df: pd.DataFrame, i: int) -> bool:
    if i < 2:
        return False
    prev2_high = max(df.iloc[i - 1]["high"], df.iloc[i - 2]["high"])
    return bool(df.iloc[i]["close"] > prev2_high)


def trigger_two_bull_bars(df: pd.DataFrame, i: int) -> bool:
    if i < 1:
        return False
    return bool(
        (df.iloc[i]["close"] > df.iloc[i]["open"]) and
        (df.iloc[i - 1]["close"] > df.iloc[i - 1]["open"])
    )


TRIGGER_MAP = {
    "next_open": trigger_next_open,
    "bull_bar": trigger_bull_bar,
    "break_prev_high": trigger_break_prev_high,
    "break_2bar_high": trigger_break_2bar_high,
    "two_bull_bars": trigger_two_bull_bars,
}


# =========================================
# バックテスト
# =========================================
def run_backtest_long_mask_trigger(df: pd.DataFrame, setup_mask: pd.Series, trigger_func, combo_name: str):
    trades = []

    for i in range(len(df) - 1):
        if not setup_mask.iloc[i]:
            continue
        if not trigger_func(df, i):
            continue

        # ★13時以降OFF
        if df.iloc[i]["hour"] >= 13:
            continue

        entry = float(df.iloc[i + 1]["open"])
        entry_dt = pd.to_datetime(df.iloc[i + 1]["datetime"])

        for j in range(1, MAX_HOLD_BARS + 1):
            idx = i + 1 + j
            if idx >= len(df):
                break

            bar = df.iloc[idx]

            # ★ロングTP
            if bar["high"] >= entry + TP:
                trades.append({
                    "combo": combo_name,
                    "entry_time": entry_dt,
                    "pnl": TP - ROUND_COST,
                    "result": "TP",
                })
                break

            # ★ロングSL
            if bar["low"] <= entry - SL:
                trades.append({
                    "combo": combo_name,
                    "entry_time": entry_dt,
                    "pnl": -SL - ROUND_COST,
                    "result": "SL",
                })
                break

            if j == MAX_HOLD_BARS:
                trades.append({
                    "combo": combo_name,
                    "entry_time": entry_dt,
                    "pnl": float(bar["close"] - entry - ROUND_COST),
                    "result": "TIME",
                })

    return pd.DataFrame(trades)


def summarize_trades(trades: pd.DataFrame, combo_name: str) -> dict:
    if len(trades) == 0:
        return {
            "combo": combo_name,
            "n": 0,
            "win_rate": 0.0,
            "pnl": 0.0,
            "pf": 0.0,
        }

    pnl = trades["pnl"]
    win_sum = pnl[pnl > 0].sum()
    loss_sum = abs(pnl[pnl < 0].sum())
    pf = win_sum / loss_sum if loss_sum != 0 else 0.0

    return {
        "combo": combo_name,
        "n": int(len(trades)),
        "win_rate": round((pnl > 0).mean() * 100, 2),
        "pnl": round(float(pnl.sum()), 2),
        "pf": round(float(pf), 3),
    }


# =========================================
# 総当たり
# =========================================
def run_mtf_search(df: pd.DataFrame) -> pd.DataFrame:
    rows = []

    tf_label_map = {
        "15min": "15m",
        "30min": "30m",
        "60min": "60m",
    }

    all_bb = {}
    for tf in BB_TFS:
        all_bb.update(make_bb_conditions(df, tf_label_map[tf]))

    all_macd = {}
    for tf in MACD_TFS:
        all_macd.update(make_macd_conditions_long(df, tf_label_map[tf]))

    bb_items = list(all_bb.items())
    macd_items = list(all_macd.items())

    for bb_name, bb_mask in bb_items:
        for macd_name, macd_mask in macd_items:
            setup_mask = (bb_mask & macd_mask).fillna(False)
            setup_mask &= (df["macd_60m"] > 0)
            setup_mask &= (df["bb_width_30m"] > df["bb_width_30m"].rolling(50).mean())
            setup_mask &= (df["close"] > df["bb_mid20_30m"])
            setup_mask &= (df["close"] < df["bb_upper_2_30m"])
            setup_mask &= (df["close"] > df["bb_mid20_30m"])
            setup_mask &= (df["low"] > df["low"].shift(1))

            for trig_name, use_flag in USE_5M_TRIGGERS.items():
                if not use_flag:
                    continue

                trigger_func = TRIGGER_MAP[trig_name]
                combo_name = f"{bb_name} + {macd_name} + {trig_name}"

                trades = run_backtest_long_mask_trigger(df, setup_mask, trigger_func, combo_name)
                row = summarize_trades(trades, combo_name)
                rows.append(row)

    out = pd.DataFrame(rows)
    if len(out) == 0:
        return out

    out = out.sort_values(
        ["pf", "pnl", "win_rate", "n"],
        ascending=[False, False, False, False]
    ).reset_index(drop=True)

    return out


# =========================================
# メイン
# =========================================

def main():
    print("★★★ MTF BB × MACD ショートからのロング総検証 ★★★")

    df = load_all()
    df = add_indicators_5m(df)
    df = build_mtf_features(df)

    need_cols = [
        "close", "open", "high", "low",
        "bb_upper_2_15m", "bb_upper_3_15m", "bb_mid20_15m", "bb_width_15m",
        "bb_upper_2_30m", "bb_upper_3_30m", "bb_mid20_30m", "bb_width_30m",
        "bb_upper_2_60m", "bb_upper_3_60m", "bb_mid20_60m", "bb_width_60m",
        "macd_15m", "macd_signal_15m", "macd_hist_15m",
        "macd_30m", "macd_signal_30m", "macd_hist_30m",
        "macd_60m", "macd_signal_60m", "macd_hist_60m",
    ]
    df = df.dropna(subset=need_cols).reset_index(drop=True)

    result = run_mtf_search(df)

    if len(result) == 0:
        print("結果なし")
        return

    print("\n===== TOP結果 =====")
    print(result.head(TOP_N))

    save_path = Path(r"C:\kabu_trade\mtf_bb_macd_short_search.csv")
    result.to_csv(save_path, index=False, encoding="utf-8-sig")
    print(f"\n保存: {save_path}")

    # =========================
    # TOP1の詳細分析
    # =========================
    top_combo = result.iloc[0]["combo"]
    print("\n===== TOP1 詳細分析 =====")
    print("対象:", top_combo)

    # 同じ条件で再計算
    bb_name, macd_name, trig_name = top_combo.split(" + ")

    bb_mask = make_bb_conditions(df, bb_name.split("_")[-1])[bb_name]
    macd_mask = make_macd_conditions_long(df, macd_name.split("_")[-1])[macd_name]

    setup_mask = (bb_mask & macd_mask).fillna(False)
    setup_mask &= (df["macd_60m"] > 0)
    setup_mask &= (df["bb_width_30m"] > df["bb_width_30m"].rolling(50).mean())
    setup_mask &= (df["close"] > df["bb_mid20_30m"])
    setup_mask &= (df["close"] < df["bb_upper_2_30m"])
    setup_mask &= (df["low"] > df["low"].shift(1))

    trigger_func = TRIGGER_MAP[trig_name]
    trades = run_backtest_long_mask_trigger(df, setup_mask, trigger_func, top_combo)

    # 年
    trades["year"] = trades["entry_time"].dt.year
    print("\n===== 年別 =====")
    print(trades.groupby("year")["pnl"].agg(["count", "sum"]))

    # 時間
    trades["hour"] = trades["entry_time"].dt.hour
    print("\n===== 時間帯 =====")
    print(trades.groupby("hour")["pnl"].agg(["count", "sum"]))

    # 曜日
    trades["weekday"] = trades["entry_time"].dt.day_name()
    print("\n===== 曜日 =====")
    print(trades.groupby("weekday")["pnl"].agg(["count", "sum"]))


if __name__ == "__main__":
    main()
    