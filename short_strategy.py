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
    "N225microf_2026.xlsx"
]

SHEET_NAME = "5min"

TP = 120
SL = 40
MAX_HOLD_BARS = 6

# =========================================
# 読み込み
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

    df["ma20"] = df["close"].rolling(20).mean()
    df["ma20_slope"] = df["ma20"] - df["ma20"].shift(1)
    df["dist_ma20_pct"] = (df["close"] / df["ma20"] - 1.0) * 100

    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma20"]

    df["atr"] = (df["high"] - df["low"]).rolling(14).mean()
    df["atr_ratio"] = df["atr"] / df["close"]

    df["bb_mid20"] = df["close"].rolling(20).mean()
    df["bb_std20"] = df["close"].rolling(20).std()
    df["bb_upper_2"] = df["bb_mid20"] + 2 * df["bb_std20"]
    df["bb_lower_2"] = df["bb_mid20"] - 2 * df["bb_std20"]
    df["bb_upper_3"] = df["bb_mid20"] + 3 * df["bb_std20"]
    df["bb_lower_3"] = df["bb_mid20"] - 3 * df["bb_std20"]

    diff = df["close"].diff()
    up = diff.clip(lower=0)
    down = -diff.clip(upper=0)
    roll_up = up.rolling(14).mean()
    roll_down = down.rolling(14).mean()
    rs = roll_up / roll_down.replace(0, np.nan)
    df["rsi14"] = 100 - (100 / (1 + rs))

    df["ma50"] = df["close"].rolling(50).mean()
    df["ma50_slope"] = df["ma50"] - df["ma50"].shift(1)

    df["body"] = (df["close"] - df["open"]).abs()
    df["lower_wick"] = df[["open", "close"]].min(axis=1) - df["low"]
    df["body_ratio"] = df["body"] / (df["high"] - df["low"] + 1e-9)

    df["gap_pct"] = df["open"] / df["close"].shift(1) - 1

    return df

# =========================================
# シグナル
# =========================================
def signal_pullback_deep(df, i):
    if i < 1:
        return False

    cur = df.iloc[i]
    prev = df.iloc[i - 1]

    # 元の条件だけに戻す
    if cur["ma20_slope"] <= 0:
        return False

    if prev["dist_ma20_pct"] > -0.3:
        return False

    if cur["close"] <= prev["high"]:
        return False

    return True


def signal_pullback_mid(df, i):
    if i < 1:
        return False

    cur = df.iloc[i]
    prev = df.iloc[i - 1]

    if cur["ma20_slope"] <= 2:
        return False
    if prev["close"] >= prev["open"]:
        return False
    if prev["dist_ma20_pct"] > -0.15:
        return False
    if cur["close"] <= prev["high"]:
        return False
    if cur["close"] <= cur["open"]:
        return False

    return True


def signal_breakout_range(df, i):
    if i < 3:
        return False

    cur = df.iloc[i]

    if cur["hour"] in [3, 14, 15, 22]:
        return False

    recent_high = df.iloc[i - 3:i]["high"].max()

    if cur["ma20_slope"] <= 0:
        return False
    if cur["close"] <= recent_high:
        return False
    if cur["vol_ratio"] < 1.2:
        return False
    if cur["close"] <= cur["open"]:
        return False

    return True


def signal_anti_breakout(df, i):
    if i < 3:
        return False

    cur = df.iloc[i]

    if cur["hour"] in [3, 14, 15, 22]:
        return False

    recent_high = df.iloc[i - 3:i]["high"].max()

    if cur["ma20_slope"] <= 0:
        return False
    if cur["close"] <= recent_high:
        return False
    if cur["vol_ratio"] < 1.2:
        return False
    if cur["close"] <= cur["open"]:
        return False

    return True

def filter_none(df, i):
    return True

def filter_bb_lower_2(df, i):
    prev = df.iloc[i - 1]
    return pd.notna(prev["bb_lower_2"]) and prev["close"] <= prev["bb_lower_2"]

def filter_bb_lower_3(df, i):
    prev = df.iloc[i - 1]
    return pd.notna(prev["bb_lower_3"]) and prev["close"] <= prev["bb_lower_3"]

def filter_rsi_30(df, i):
    prev = df.iloc[i - 1]
    return pd.notna(prev["rsi14"]) and prev["rsi14"] <= 30

def filter_rsi_25(df, i):
    prev = df.iloc[i - 1]
    return pd.notna(prev["rsi14"]) and prev["rsi14"] <= 25

def filter_ma50_up(df, i):
    cur = df.iloc[i]
    return pd.notna(cur["ma50_slope"]) and cur["ma50_slope"] > 0

def filter_close_above_ma50(df, i):
    cur = df.iloc[i]
    return pd.notna(cur["ma50"]) and cur["close"] > cur["ma50"]

def filter_prev_vol_low(df, i):
    prev = df.iloc[i - 1]
    return pd.notna(prev["vol_ratio"]) and prev["vol_ratio"] < 1.0

def filter_prev_vol_normal(df, i):
    prev = df.iloc[i - 1]
    return pd.notna(prev["vol_ratio"]) and prev["vol_ratio"] < 1.2

def filter_cur_vol_up(df, i):
    cur = df.iloc[i]
    return pd.notna(cur["vol_ratio"]) and cur["vol_ratio"] >= 1.2

def filter_gap_non_negative(df, i):
    cur = df.iloc[i]
    return pd.notna(cur["gap_pct"]) and cur["gap_pct"] >= 0

def filter_gap_small_minus(df, i):
    cur = df.iloc[i]
    return pd.notna(cur["gap_pct"]) and cur["gap_pct"] >= -0.2

def filter_lower_wick_long(df, i):
    prev = df.iloc[i - 1]
    return (
        pd.notna(prev["lower_wick"]) and
        pd.notna(prev["body"]) and
        prev["body"] > 0 and
        prev["lower_wick"] >= prev["body"]
    )

def filter_body_small(df, i):
    prev = df.iloc[i - 1]
    return (
        pd.notna(prev["body_ratio"]) and
        prev["body_ratio"] <= 0.4
    )

def filter_atr_low(df, i):
    cur = df.iloc[i]
    return pd.notna(cur["atr_ratio"]) and cur["atr_ratio"] <= 1.0

def make_deep_signal(extra_filter_func):
    def _signal(df, i):
        if not signal_pullback_deep(df, i):
            return False
        return extra_filter_func(df, i)
    return _signal

def signal_overlap_only(df, i):
    return make_deep_signal(
        lambda d, k: (
            filter_cur_vol_up(d, k)
            and filter_gap_small_minus(d, k)
            and filter_rsi_30(d, k)
        )
    )(df, i)

def run_full_combo(df):
    filters = {
        "bb2": filter_bb_lower_2,
        "bb3": filter_bb_lower_3,
        "rsi30": filter_rsi_30,
        "rsi25": filter_rsi_25,
        "ma50": filter_close_above_ma50,
        "vol_low": filter_prev_vol_low,
        "vol_normal": filter_prev_vol_normal,
        "vol_up": filter_cur_vol_up,
        "gap_ok": filter_gap_non_negative,
        "gap_small": filter_gap_small_minus,
        "wick": filter_lower_wick_long,
        "body": filter_body_small,
    }

    rows = []
    keys = list(filters.keys())

    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):

            name1 = keys[i]
            name2 = keys[j]

            f1 = filters[name1]
            f2 = filters[name2]

            def combo(df, k):
                return f1(df, k) and f2(df, k)

            trades = run_backtest(df, make_deep_signal(combo))
            s = summary(trades)

            rows.append({
                "combo": f"{name1}+{name2}",
                "n": s["n"],
                "win_rate": s["win_rate"],
                "pnl": s["pnl"],
                "pf": s["pf"],
            })

def find_short_patterns(df):
    df = df.copy()

    future_low = pd.concat([df["low"].shift(-k) for k in range(1, 7)], axis=1).min(axis=1)
    df["down_big"] = (future_low - df["close"]) <= -80

    rows = []

    filters = {
        "bb_upper_2": lambda d,i: d.iloc[i]["close"] >= d.iloc[i]["bb_upper_2"],
        "bb_upper_3": lambda d,i: d.iloc[i]["close"] >= d.iloc[i]["bb_upper_3"],
        "rsi_high": lambda d,i: d.iloc[i]["rsi14"] >= 70,
        "vol_spike": lambda d,i: d.iloc[i]["vol_ratio"] >= 1.5,
        "gap_up": lambda d,i: d.iloc[i]["gap_pct"] >= 0.2,
        "ma50_down": lambda d,i: d.iloc[i]["ma50_slope"] < 0,
    }

    keys = list(filters.keys())

    for i in range(len(keys)):
        for j in range(i+1, len(keys)):
            f1 = filters[keys[i]]
            f2 = filters[keys[j]]

            mask = []
            for k in range(len(df)):
                if f1(df,k) and f2(df,k):
                    mask.append(True)
                else:
                    mask.append(False)

            grp = df[mask]
            if len(grp) < 50:
                continue

            rate = grp["down_big"].mean()*100

            rows.append({
                "combo": f"{keys[i]}+{keys[j]}",
                "n": len(grp),
                "down_rate": round(rate,2)
            })

    return pd.DataFrame(rows).sort_values(["down_rate","n"], ascending=False)          

# =========================================
# バックテスト
# =========================================
def run_backtest(df, signal_func):
    trades = []

    for i in range(len(df) - 1):
        if not signal_func(df, i):
            continue

        entry = float(df.iloc[i + 1]["open"])

        for j in range(1, MAX_HOLD_BARS + 1):
            if i + j >= len(df):
                break

            bar = df.iloc[i + j]

            if bar["high"] >= entry + TP:
                trades.append({
                    "pnl": TP - 22,
                    "weekday": pd.to_datetime(df.iloc[i + 1]["datetime"]).day_name(),
                    "hour": pd.to_datetime(df.iloc[i + 1]["datetime"]).hour,
                    "datetime": df.iloc[i + 1]["datetime"],
                    "result": "TP"
                })
                break

            if bar["low"] <= entry - SL:
                trades.append({
                    "pnl": -SL - 22,
                    "weekday": pd.to_datetime(df.iloc[i + 1]["datetime"]).day_name(),
                    "hour": pd.to_datetime(df.iloc[i + 1]["datetime"]).hour,
                    "datetime": df.iloc[i + 1]["datetime"],
                    "result": "SL"
                })
                break

            if j == MAX_HOLD_BARS:
                trades.append({
                    "pnl": float(bar["close"] - entry - 22),
                    "weekday": pd.to_datetime(df.iloc[i + 1]["datetime"]).day_name(),
                    "datetime": df.iloc[i + 1]["datetime"],
                    "hour": pd.to_datetime(df.iloc[i + 1]["datetime"]).hour,
                    "result": "TIME"
                })

    if len(trades) == 0:
        return pd.DataFrame(columns=["pnl", "weekday", "hour", "datetime", "result"])

    return pd.DataFrame(trades)

def run_backtest_short(df, signal_func):
    trades = []

    for i in range(len(df) - 1):
        if not signal_func(df, i):
            continue

        entry = float(df.iloc[i + 1]["open"])

        for j in range(1, MAX_HOLD_BARS + 1):
            if i + j >= len(df):
                break

            bar = df.iloc[i + j]

            if bar["low"] <= entry - 40:
                trades.append({
                    "pnl": 40 - 22,
                    "weekday": pd.to_datetime(df.iloc[i + 1]["datetime"]).day_name(),
                    "hour": pd.to_datetime(df.iloc[i + 1]["datetime"]).hour,
                    "result": "TP"
                })
                break

            if bar["high"] >= entry + 120:
                trades.append({
                    "pnl": -120 - 22,
                    "weekday": pd.to_datetime(df.iloc[i + 1]["datetime"]).day_name(),
                    "hour": pd.to_datetime(df.iloc[i + 1]["datetime"]).hour,
                    "result": "SL"
                })
                break

            if j == MAX_HOLD_BARS:
                trades.append({
                    "pnl": float(entry - bar["close"] - 22),
                    "weekday": pd.to_datetime(df.iloc[i + 1]["datetime"]).day_name(),
                    "hour": pd.to_datetime(df.iloc[i + 1]["datetime"]).hour,
                    "result": "TIME"
                })

        if len(trades) == 0:
            return pd.DataFrame(columns=["pnl", "weekday", "hour", "result"])

        return pd.DataFrame(trades)

# =========================================
# 表示
# =========================================
def print_month_full_stats(trades_df):
    if len(trades_df) == 0:
        print("データなし")
        return

    df = trades_df.copy()
    df["month"] = pd.to_datetime(df["datetime"]).dt.to_period("M")

    def calc(group):
        pnl = group["pnl"]
        wins = pnl[pnl > 0]
        losses = pnl[pnl < 0]

        return pd.Series({
            "n": len(pnl),
            "win_rate": (pnl > 0).mean() * 100,
            "win_sum": wins.sum(),
            "loss_sum": losses.sum(),
            "pnl": pnl.sum()
        })

    out = df.groupby("month").apply(calc)

    print("\n【月別フル分析】")
    print(out)
def print_total_stats(trades_df, name):
    pnl = trades_df["pnl"]

    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]

    print(f"\n【全体集計: {name}】")
    print({
        "n": len(pnl),
        "win_rate": (pnl > 0).mean() * 100,
        "win_sum": wins.sum(),
        "loss_sum": losses.sum(),
        "pnl": pnl.sum(),
        "pf": wins.sum() / abs(losses.sum()) if len(losses) > 0 else 0
    })

def print_details(name, trades_df):
    print(f"\n===== {name} =====")
    print(summary(trades_df))

    if len(trades_df) == 0:
        return

    print("\n【曜日別】")
    print(trades_df.groupby("weekday")["pnl"].sum())

    print("\n【時間帯別】")
    print(trades_df.groupby("hour")["pnl"].sum())

    print("\n【決済別】")
    print(trades_df.groupby("result")["pnl"].agg(["count", "sum"]))


def summary(trades_df):
    if trades_df is None or len(trades_df) == 0 or "pnl" not in trades_df.columns:
        return {
            "n": 0,
            "win_rate": 0.0,
            "pnl": 0.0,
            "pf": 0.0,
        }

    pnl_arr = trades_df["pnl"].values
    win = pnl_arr[pnl_arr > 0].sum()
    loss = abs(pnl_arr[pnl_arr < 0].sum())
    pf = win / loss if loss != 0 else 0

    return {
        "n": len(trades_df),
        "win_rate": float((pnl_arr > 0).mean() * 100),
        "pnl": float(pnl_arr.sum()),
        "pf": float(pf),
    }

def print_monthly(trades_df):
    trades_df = trades_df.copy()
    trades_df["month"] = pd.to_datetime(trades_df["datetime"]).dt.to_period("M")

    print("\n【月別】")
    print(trades_df.groupby("month")["pnl"].agg(["count", "sum"]))

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
# ← ここに出す（外！）
if __name__ == "__main__":
    main()