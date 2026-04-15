from pathlib import Path
from itertools import product, combinations
import math
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

BASE_SHEET = "5min"   # 元データ
OUTPUT_DIR = DATA_DIR / "exhaustive_result"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

SESSION_START = "08:45"
SESSION_END = "18:00"
NO_OVERNIGHT = True

LONG_TP = 120
LONG_SL = 40
SHORT_TP = 40
SHORT_SL = 120
COST_PER_TRADE = 22
MAX_HOLD_BARS_CANDIDATES = [3, 6, 9, 12]

# 使う時間足
TIMEFRAMES = [5, 10, 15, 30]

# MA候補
MA_FASTS = [5, 10, 20]
MA_SLOWS = [25, 50, 75]

# BB候補
BB_PERIODS = [10, 20, 25]
BB_SIGMAS = [1.8, 2.0, 2.5, 3.0]

# 出来高MA候補
VOL_MA_PERIODS = [10, 20, 30]
VOL_THRESHOLDS = [0.8, 1.0, 1.2, 1.5]

# MACD候補
MACD_FASTS = [6, 12, 18]
MACD_SLOWS = [19, 26, 35]
MACD_SIGNALS = [5, 9, 12]

# 一目候補
ICHI_TENKAN = [7, 9, 12]
ICHI_KIJUN = [22, 26, 30]
ICHI_SENKOU_B = [44, 52, 60]

# 指標ごとの時間足候補
INDICATOR_TIMEFRAMES = [5, 15, 30]

# エントリー条件の閾値候補
MA_DIST_THRESH = [0.0, 0.1, 0.2]
BB_TOUCH_MARGIN = [0.0, 0.1]

# 時間帯集計
TIME_BUCKETS = [
    ("08:45-09:29", "08:45", "09:29"),
    ("09:30-10:29", "09:30", "10:29"),
    ("10:30-11:29", "10:30", "11:29"),
    ("11:30-12:29", "11:30", "12:29"),
    ("12:30-13:29", "12:30", "13:29"),
    ("13:30-14:29", "13:30", "14:29"),
    ("14:30-15:29", "14:30", "15:29"),
    ("15:30-16:29", "15:30", "16:29"),
    ("16:30-17:29", "16:30", "17:29"),
    ("17:30-18:00", "17:30", "18:00"),
]

# 使う指標のON/OFF組み合わせ
INDICATOR_NAMES = ["ma", "macd", "ichimoku", "bb", "volume"]
MIN_ACTIVE_INDICATORS = 3

# 上位抽出条件
MIN_TRADES_FOR_RANK = 20
TOP_N = 10


# =========================================
# 読み込み
# =========================================
def read_one_file(path: Path, sheet_name: str = BASE_SHEET) -> pd.DataFrame:
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
    for f in FILES:
        path = DATA_DIR / f
        if not path.exists():
            print(f"[警告] ファイルなし: {path}")
            continue
        dfs.append(read_one_file(path))

    if not dfs:
        raise FileNotFoundError("Excelファイルが見つかりません。DATA_DIR と FILES を確認してください。")

    df = pd.concat(dfs, ignore_index=True)
    df = df.sort_values("datetime").drop_duplicates(subset=["datetime"]).reset_index(drop=True)
    return df


# =========================================
# 前処理
# =========================================
def filter_session(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["time_str"] = out["datetime"].dt.strftime("%H:%M")
    out = out[(out["time_str"] >= SESSION_START) & (out["time_str"] <= SESSION_END)].copy()
    out = out.drop(columns=["time_str"])
    out["trade_date"] = out["datetime"].dt.date
    return out.reset_index(drop=True)


def resample_ohlcv(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    if minutes == 5:
        out = df.copy()
        out["trade_date"] = out["datetime"].dt.date
        return out.reset_index(drop=True)

    work = df.copy().set_index("datetime")
    agg = work.resample(f"{minutes}min", label="right", closed="right").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna().reset_index()

    agg = filter_session(agg)
    return agg.reset_index(drop=True)

RESAMPLED_CACHE = {}

def get_resampled(df, tf):
    if tf not in RESAMPLED_CACHE:
        RESAMPLED_CACHE[tf] = resample_ohlcv(df, tf)
    return RESAMPLED_CACHE[tf]


def attach_time_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    ts = pd.to_datetime(out["datetime"])
    out["hour"] = ts.dt.hour
    out["minute"] = ts.dt.minute
    out["month"] = ts.dt.to_period("M").astype(str)
    out["weekday"] = ts.dt.day_name()
    out["year"] = ts.dt.year
    out["iso_week"] = ts.dt.isocalendar().week.astype(int)
    return out


# =========================================
# 指標計算
# =========================================
def add_ma(df: pd.DataFrame, fast: int, slow: int, prefix: str) -> pd.DataFrame:
    out = df.copy()
    out[f"{prefix}_ma_fast"] = out["close"].rolling(fast).mean()
    out[f"{prefix}_ma_slow"] = out["close"].rolling(slow).mean()
    out[f"{prefix}_ma_fast_slope"] = out[f"{prefix}_ma_fast"] - out[f"{prefix}_ma_fast"].shift(1)
    out[f"{prefix}_ma_slow_slope"] = out[f"{prefix}_ma_slow"] - out[f"{prefix}_ma_slow"].shift(1)
    out[f"{prefix}_ma_dist_pct"] = (out["close"] / out[f"{prefix}_ma_fast"] - 1.0) * 100
    return out


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def add_macd(df: pd.DataFrame, fast: int, slow: int, signal: int, prefix: str) -> pd.DataFrame:
    out = df.copy()
    macd_fast = ema(out["close"], fast)
    macd_slow = ema(out["close"], slow)
    out[f"{prefix}_macd"] = macd_fast - macd_slow
    out[f"{prefix}_macd_signal"] = ema(out[f"{prefix}_macd"], signal)
    out[f"{prefix}_macd_hist"] = out[f"{prefix}_macd"] - out[f"{prefix}_macd_signal"]
    return out


def add_bb(df: pd.DataFrame, period: int, sigma: float, prefix: str) -> pd.DataFrame:
    out = df.copy()
    mid = out["close"].rolling(period).mean()
    std = out["close"].rolling(period).std()
    out[f"{prefix}_bb_mid"] = mid
    out[f"{prefix}_bb_upper"] = mid + sigma * std
    out[f"{prefix}_bb_lower"] = mid - sigma * std
    return out


def add_volume(df: pd.DataFrame, period: int, prefix: str) -> pd.DataFrame:
    out = df.copy()
    out[f"{prefix}_vol_ma"] = out["volume"].rolling(period).mean()
    out[f"{prefix}_vol_ratio"] = out["volume"] / out[f"{prefix}_vol_ma"]
    return out


def add_ichimoku(df: pd.DataFrame, tenkan: int, kijun: int, senkou_b: int, prefix: str) -> pd.DataFrame:
    out = df.copy()
    high_t = out["high"].rolling(tenkan).max()
    low_t = out["low"].rolling(tenkan).min()
    high_k = out["high"].rolling(kijun).max()
    low_k = out["low"].rolling(kijun).min()
    high_b = out["high"].rolling(senkou_b).max()
    low_b = out["low"].rolling(senkou_b).min()

    out[f"{prefix}_tenkan"] = (high_t + low_t) / 2
    out[f"{prefix}_kijun"] = (high_k + low_k) / 2
    out[f"{prefix}_senkou_a"] = ((out[f"{prefix}_tenkan"] + out[f"{prefix}_kijun"]) / 2).shift(kijun)
    out[f"{prefix}_senkou_b"] = ((high_b + low_b) / 2).shift(kijun)
    out[f"{prefix}_chikou"] = out["close"].shift(-kijun)
    return out


def merge_indicator_to_base(base_df: pd.DataFrame, ind_df: pd.DataFrame, prefix: str) -> pd.DataFrame:
    keep_cols = ["datetime"] + [
        c for c in ind_df.columns
        if c.startswith(f"{prefix}_")
    ]
    merged = pd.merge_asof(
        base_df.sort_values("datetime"),
        ind_df[keep_cols].sort_values("datetime"),
        on="datetime",
        direction="backward"
    )
    return merged


def build_feature_df(base_5m: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    df = base_5m.copy()

    # MA
    ma_tf = get_resampled(base_5m, cfg["ma_tf"])
    ma_tf = add_ma(ma_tf, cfg["ma_fast"], cfg["ma_slow"], "ma")
    df = merge_indicator_to_base(df, ma_tf, "ma")

    # MACD
    macd_tf = get_resampled(base_5m, cfg["macd_tf"])
    macd_tf = add_macd(macd_tf, cfg["macd_fast"], cfg["macd_slow"], cfg["macd_signal"], "macd")
    df = merge_indicator_to_base(df, macd_tf, "macd")

    # 一目
    ichi_tf = get_resampled(base_5m, cfg["ichi_tf"])
    ichi_tf = add_ichimoku(ichi_tf, cfg["tenkan"], cfg["kijun"], cfg["senkou_b"], "ichi")
    df = merge_indicator_to_base(df, ichi_tf, "ichi")

    # BB
    bb_tf = get_resampled(base_5m, cfg["bb_tf"])
    bb_tf = add_bb(bb_tf, cfg["bb_period"], cfg["bb_sigma"], "bb")
    df = merge_indicator_to_base(df, bb_tf, "bb")

    # 出来高
    vol_tf = get_resampled(base_5m, cfg["vol_tf"])
    vol_tf = add_volume(vol_tf, cfg["vol_period"], "vol")
    df = merge_indicator_to_base(df, vol_tf, "vol")

    return df.dropna().reset_index(drop=True)


# =========================================
# 条件判定
# =========================================
def time_bucket_label(ts: pd.Timestamp) -> str:
    hhmm = ts.strftime("%H:%M")
    for name, start, end in TIME_BUCKETS:
        if start <= hhmm <= end:
            return name
    return "other"


def is_msq_month(month_str: str) -> bool:
    y, m = month_str.split("-")
    return int(m) in [3, 6, 9, 12]


def active_indicators(flag_dict: dict) -> str:
    on = [k for k, v in flag_dict.items() if v]
    return "+".join(on)


def long_signal(row: pd.Series, prev: pd.Series, cfg: dict) -> bool:
    flags = cfg["use"]
    checks = []

    if flags["ma"]:
        checks.append(
            pd.notna(row["ma_ma_fast"]) and
            pd.notna(row["ma_ma_slow"]) and
            row["close"] > row["ma_ma_fast"] and
            row["ma_ma_fast"] > row["ma_ma_slow"] and
            row["ma_ma_fast_slope"] > 0 and
            row["ma_ma_dist_pct"] >= cfg["ma_dist_thresh"]
        )

    if flags["macd"]:
        checks.append(
            pd.notna(row["macd_macd"]) and
            pd.notna(row["macd_macd_signal"]) and
            row["macd_macd"] > row["macd_macd_signal"] and
            row["macd_macd_hist"] > prev["macd_macd_hist"]
        )

    if flags["ichimoku"]:
        cloud_top = max(row["ichi_senkou_a"], row["ichi_senkou_b"])
        checks.append(
            pd.notna(row["ichi_tenkan"]) and
            pd.notna(row["ichi_kijun"]) and
            pd.notna(cloud_top) and
            row["ichi_tenkan"] > row["ichi_kijun"] and
            row["close"] > cloud_top
        )

    if flags["bb"]:
        checks.append(
            pd.notna(prev["bb_bb_lower"]) and
            prev["low"] <= prev["bb_bb_lower"] * (1 + cfg["bb_touch_margin"] / 100) and
            row["close"] > row["open"]
        )

    if flags["volume"]:
        checks.append(
            pd.notna(row["vol_vol_ratio"]) and
            row["vol_vol_ratio"] >= cfg["vol_threshold"]
        )

    return len(checks) > 0 and all(checks)


def short_signal(row: pd.Series, prev: pd.Series, cfg: dict) -> bool:
    flags = cfg["use"]
    checks = []

    if flags["ma"]:
        checks.append(
            pd.notna(row["ma_ma_fast"]) and
            pd.notna(row["ma_ma_slow"]) and
            row["close"] < row["ma_ma_fast"] and
            row["ma_ma_fast"] < row["ma_ma_slow"] and
            row["ma_ma_fast_slope"] < 0 and
            row["ma_ma_dist_pct"] <= -cfg["ma_dist_thresh"]
        )

    if flags["macd"]:
        checks.append(
            pd.notna(row["macd_macd"]) and
            pd.notna(row["macd_macd_signal"]) and
            row["macd_macd"] < row["macd_macd_signal"] and
            row["macd_macd_hist"] < prev["macd_macd_hist"]
        )

    if flags["ichimoku"]:
        cloud_bottom = min(row["ichi_senkou_a"], row["ichi_senkou_b"])
        checks.append(
            pd.notna(row["ichi_tenkan"]) and
            pd.notna(row["ichi_kijun"]) and
            pd.notna(cloud_bottom) and
            row["ichi_tenkan"] < row["ichi_kijun"] and
            row["close"] < cloud_bottom
        )

    if flags["bb"]:
        checks.append(
            pd.notna(prev["bb_bb_upper"]) and
            prev["high"] >= prev["bb_bb_upper"] * (1 - cfg["bb_touch_margin"] / 100) and
            row["close"] < row["open"]
        )

    if flags["volume"]:
        checks.append(
            pd.notna(row["vol_vol_ratio"]) and
            row["vol_vol_ratio"] >= cfg["vol_threshold"]
        )

    return len(checks) > 0 and all(checks)


# =========================================
# バックテスト
# =========================================
def run_backtest(df: pd.DataFrame, cfg: dict, side: str) -> pd.DataFrame:
    trades = []
    max_hold = cfg["max_hold_bars"]

    for i in range(1, len(df) - 1):
        row = df.iloc[i]
        prev = df.iloc[i - 1]
        nxt = df.iloc[i + 1]

        if row["trade_date"] != nxt["trade_date"]:
            continue

        if side == "long":
            ok = long_signal(row, prev, cfg)
        else:
            ok = short_signal(row, prev, cfg)
        if not ok:
            continue

        entry_time = nxt["datetime"]
        entry = float(nxt["open"])
        exit_reason = None
        pnl = None
        exit_time = None

        for j in range(1, max_hold + 1):
            k = i + j
            if k >= len(df):
                break
            bar = df.iloc[k]
            if NO_OVERNIGHT and bar["trade_date"] != row["trade_date"]:
                break

            if side == "long":
                if bar["high"] >= entry + LONG_TP:
                    pnl = LONG_TP - COST_PER_TRADE
                    exit_reason = "TP"
                    exit_time = bar["datetime"]
                    break
                if bar["low"] <= entry - LONG_SL:
                    pnl = -LONG_SL - COST_PER_TRADE
                    exit_reason = "SL"
                    exit_time = bar["datetime"]
                    break
            else:
                if bar["low"] <= entry - SHORT_TP:
                    pnl = SHORT_TP - COST_PER_TRADE
                    exit_reason = "TP"
                    exit_time = bar["datetime"]
                    break
                if bar["high"] >= entry + SHORT_SL:
                    pnl = -SHORT_SL - COST_PER_TRADE
                    exit_reason = "SL"
                    exit_time = bar["datetime"]
                    break

            is_last = (j == max_hold)
            next_cross_day = (k + 1 < len(df) and df.iloc[k + 1]["trade_date"] != row["trade_date"])
            if is_last or (NO_OVERNIGHT and next_cross_day):
                if side == "long":
                    pnl = float(bar["close"] - entry - COST_PER_TRADE)
                else:
                    pnl = float(entry - bar["close"] - COST_PER_TRADE)
                exit_reason = "TIME"
                exit_time = bar["datetime"]
                break

        if pnl is None:
            continue

        trades.append({
            "side": side,
            "entry_time": entry_time,
            "exit_time": exit_time,
            "entry_price": entry,
            "pnl": pnl,
            "result": exit_reason,
            "month": str(pd.to_datetime(entry_time).to_period("M")),
            "weekday": pd.to_datetime(entry_time).day_name(),
            "hour": pd.to_datetime(entry_time).hour,
            "time_bucket": time_bucket_label(pd.to_datetime(entry_time)),
            "year": pd.to_datetime(entry_time).year,
            "iso_week": int(pd.to_datetime(entry_time).isocalendar().week),
            "is_msq_month": is_msq_month(str(pd.to_datetime(entry_time).to_period("M"))),
        })

    return pd.DataFrame(trades)


# =========================================
# 集計
# =========================================
def calc_stats(trades: pd.DataFrame) -> dict:
    if trades is None or len(trades) == 0:
        return {
            "n": 0,
            "win": 0,
            "lose": 0,
            "win_rate": 0.0,
            "pnl": 0.0,
            "pf": 0.0,
            "avg_pnl": 0.0,
        }

    pnl = trades["pnl"]
    wins = pnl[pnl > 0]
    losses = pnl[pnl <= 0]
    loss_abs = abs(losses.sum())

    return {
        "n": int(len(trades)),
        "win": int((pnl > 0).sum()),
        "lose": int((pnl <= 0).sum()),
        "win_rate": float((pnl > 0).mean() * 100),
        "pnl": float(pnl.sum()),
        "pf": float(wins.sum() / loss_abs) if loss_abs > 0 else 0.0,
        "avg_pnl": float(pnl.mean()),
    }


def aggregate_by(trades: pd.DataFrame, key: str) -> pd.DataFrame:
    if len(trades) == 0:
        return pd.DataFrame()

    def _one(g: pd.DataFrame) -> pd.Series:
        s = calc_stats(g)
        return pd.Series(s)

    return trades.groupby(key, dropna=False).apply(_one).reset_index()


def msq_weekly_stats(trades: pd.DataFrame) -> pd.DataFrame:
    if len(trades) == 0:
        return pd.DataFrame()
    msq = trades[trades["is_msq_month"]].copy()
    if len(msq) == 0:
        return pd.DataFrame()

    def _one(g: pd.DataFrame) -> pd.Series:
        s = calc_stats(g)
        return pd.Series(s)

    return msq.groupby(["month", "iso_week"]).apply(_one).reset_index()


# =========================================
# パターン生成
# =========================================
def generate_use_patterns():
    results = []
    for r in range(MIN_ACTIVE_INDICATORS, len(INDICATOR_NAMES) + 1):
        for combo in combinations(INDICATOR_NAMES, r):
            flags = {name: (name in combo) for name in INDICATOR_NAMES}
            results.append(flags)
    return results


def generate_param_space():
    use_patterns = generate_use_patterns()

    for use in use_patterns:
        for max_hold in MAX_HOLD_BARS_CANDIDATES:
            for ma_tf, ma_fast, ma_slow, ma_dist in product(INDICATOR_TIMEFRAMES, MA_FASTS, MA_SLOWS, MA_DIST_THRESH):
                if ma_fast >= ma_slow:
                    continue
                for macd_tf, macd_fast, macd_slow, macd_signal in product(INDICATOR_TIMEFRAMES, MACD_FASTS, MACD_SLOWS, MACD_SIGNALS):
                    if macd_fast >= macd_slow:
                        continue
                    for ichi_tf, tenkan, kijun, senkou_b in product(INDICATOR_TIMEFRAMES, ICHI_TENKAN, ICHI_KIJUN, ICHI_SENKOU_B):
                        if not (tenkan < kijun < senkou_b):
                            continue
                        for bb_tf, bb_period, bb_sigma, bb_margin in product(INDICATOR_TIMEFRAMES, BB_PERIODS, BB_SIGMAS, BB_TOUCH_MARGIN):
                            for vol_tf, vol_period, vol_threshold in product(INDICATOR_TIMEFRAMES, VOL_MA_PERIODS, VOL_THRESHOLDS):
                                yield {
                                    "use": use,
                                    "max_hold_bars": max_hold,
                                    "ma_tf": ma_tf,
                                    "ma_fast": ma_fast,
                                    "ma_slow": ma_slow,
                                    "ma_dist_thresh": ma_dist,
                                    "macd_tf": macd_tf,
                                    "macd_fast": macd_fast,
                                    "macd_slow": macd_slow,
                                    "macd_signal": macd_signal,
                                    "ichi_tf": ichi_tf,
                                    "tenkan": tenkan,
                                    "kijun": kijun,
                                    "senkou_b": senkou_b,
                                    "bb_tf": bb_tf,
                                    "bb_period": bb_period,
                                    "bb_sigma": bb_sigma,
                                    "bb_touch_margin": bb_margin,
                                    "vol_tf": vol_tf,
                                    "vol_period": vol_period,
                                    "vol_threshold": vol_threshold,
                                }


def pattern_name(cfg: dict) -> str:
    return (
        f"use={active_indicators(cfg['use'])}"
        f"|hold={cfg['max_hold_bars']}"
        f"|ma={cfg['ma_tf']}m:{cfg['ma_fast']}/{cfg['ma_slow']}/{cfg['ma_dist_thresh']}"
        f"|macd={cfg['macd_tf']}m:{cfg['macd_fast']}/{cfg['macd_slow']}/{cfg['macd_signal']}"
        f"|ichi={cfg['ichi_tf']}m:{cfg['tenkan']}/{cfg['kijun']}/{cfg['senkou_b']}"
        f"|bb={cfg['bb_tf']}m:{cfg['bb_period']}/{cfg['bb_sigma']}/{cfg['bb_touch_margin']}"
        f"|vol={cfg['vol_tf']}m:{cfg['vol_period']}/{cfg['vol_threshold']}"
    )


# =========================================
# メイン検証
# =========================================
def run_exhaustive(base_df: pd.DataFrame, max_patterns: int | None = None):
    rows = []
    params = generate_param_space()

    for idx, cfg in enumerate(params, start=1):
        if max_patterns is not None and idx > max_patterns:
            break

        try:
            feat = build_feature_df(base_df, cfg)
            if len(feat) == 0:
                continue

            for side in ["long", "short"]:
                trades = run_backtest(feat, cfg, side)
                s = calc_stats(trades)
                row = {
                    "pattern_id": idx,
                    "side": side,
                    "pattern": pattern_name(cfg),
                    "active_indicators": active_indicators(cfg["use"]),
                    **cfg,
                    **s,
                }
                rows.append(row)

            if idx % 50 == 0:
                print(f"進捗: {idx} パターン")

        except Exception as e:
            rows.append({
                "pattern_id": idx,
                "side": "error",
                "pattern": pattern_name(cfg),
                "error": repr(e),
            })

    result = pd.DataFrame(rows)
    if len(result) == 0:
        return result

    return result


def rerun_top_patterns(base_df: pd.DataFrame, ranking_df: pd.DataFrame, top_n: int = TOP_N) -> tuple[pd.DataFrame, dict]:
    required_cols = ["side", "n", "pf", "pnl", "win_rate"]
    if ranking_df is None or len(ranking_df) == 0:
        print("[警告] ranking_df が空です")
        return pd.DataFrame(), {}

    missing = [c for c in required_cols if c not in ranking_df.columns]
    if missing:
        print(f"[警告] 上位抽出に必要な列がありません: {missing}")
        print(f"[確認] ranking_df columns = {list(ranking_df.columns)}")
        err_df = ranking_df.copy()
        err_df.to_csv(OUTPUT_DIR / "ranking_debug_missing_columns.csv", index=False, encoding="utf-8-sig")
        return pd.DataFrame(), {}

    valid = ranking_df[(ranking_df["side"].isin(["long", "short"])) & (ranking_df["n"] >= MIN_TRADES_FOR_RANK)].copy()
    if len(valid) == 0:
        print("[警告] 上位抽出対象の有効パターンがありません")
        ranking_df.to_csv(OUTPUT_DIR / "ranking_debug_no_valid_rows.csv", index=False, encoding="utf-8-sig")
        return pd.DataFrame(), {}

    valid = valid.sort_values(["pf", "pnl", "win_rate", "n"], ascending=[False, False, False, False]).head(top_n)

    detail_rows = []
    trade_map = {}

    for _, r in valid.iterrows():
        cfg = {
            "use": r["use"],
            "max_hold_bars": int(r["max_hold_bars"]),
            "ma_tf": int(r["ma_tf"]),
            "ma_fast": int(r["ma_fast"]),
            "ma_slow": int(r["ma_slow"]),
            "ma_dist_thresh": float(r["ma_dist_thresh"]),
            "macd_tf": int(r["macd_tf"]),
            "macd_fast": int(r["macd_fast"]),
            "macd_slow": int(r["macd_slow"]),
            "macd_signal": int(r["macd_signal"]),
            "ichi_tf": int(r["ichi_tf"]),
            "tenkan": int(r["tenkan"]),
            "kijun": int(r["kijun"]),
            "senkou_b": int(r["senkou_b"]),
            "bb_tf": int(r["bb_tf"]),
            "bb_period": int(r["bb_period"]),
            "bb_sigma": float(r["bb_sigma"]),
            "bb_touch_margin": float(r["bb_touch_margin"]),
            "vol_tf": int(r["vol_tf"]),
            "vol_period": int(r["vol_period"]),
            "vol_threshold": float(r["vol_threshold"]),
        }

        feat = build_feature_df(base_df, cfg)
        trades = run_backtest(feat, cfg, r["side"])
        tag = f"top_{int(r['pattern_id'])}_{r['side']}"
        trades["pattern_tag"] = tag
        trade_map[tag] = trades.copy()

        total = calc_stats(trades)
        monthly = aggregate_by(trades, "month")
        hourly = aggregate_by(trades, "time_bucket")
        msq_weekly = msq_weekly_stats(trades)

        detail_rows.append({
            "pattern_tag": tag,
            "pattern_id": int(r["pattern_id"]),
            "side": r["side"],
            "pattern": r["pattern"],
            "n": total["n"],
            "win": total["win"],
            "lose": total["lose"],
            "win_rate": total["win_rate"],
            "pnl": total["pnl"],
            "pf": total["pf"],
            "avg_pnl": total["avg_pnl"],
        })

        monthly.to_csv(OUTPUT_DIR / f"{tag}_monthly.csv", index=False, encoding="utf-8-sig")
        hourly.to_csv(OUTPUT_DIR / f"{tag}_time_bucket.csv", index=False, encoding="utf-8-sig")
        msq_weekly.to_csv(OUTPUT_DIR / f"{tag}_msq_weekly.csv", index=False, encoding="utf-8-sig")
        trades.to_csv(OUTPUT_DIR / f"{tag}_trades.csv", index=False, encoding="utf-8-sig")

    return pd.DataFrame(detail_rows), trade_map


def main():
    base = load_all()
    base = filter_session(base)
    base = attach_time_columns(base)

    # まず総数の概算だけ出す
    use_count = sum(math.comb(len(INDICATOR_NAMES), r) for r in range(MIN_ACTIVE_INDICATORS, len(INDICATOR_NAMES)+1))
    estimated = (
        use_count
        * len(MAX_HOLD_BARS_CANDIDATES)
        * sum(1 for f in MA_FASTS for s in MA_SLOWS if f < s) * len(INDICATOR_TIMEFRAMES) * len(MA_DIST_THRESH)
        * sum(1 for f in MACD_FASTS for s in MACD_SLOWS if f < s) * len(INDICATOR_TIMEFRAMES) * len(MACD_SIGNALS)
        * sum(1 for t in ICHI_TENKAN for k in ICHI_KIJUN for b in ICHI_SENKOU_B if t < k < b) * len(INDICATOR_TIMEFRAMES)
        * len(INDICATOR_TIMEFRAMES) * len(BB_PERIODS) * len(BB_SIGMAS) * len(BB_TOUCH_MARGIN)
        * len(INDICATOR_TIMEFRAMES) * len(VOL_MA_PERIODS) * len(VOL_THRESHOLDS)
    )
    print(f"概算パターン数: {estimated:,}")

    # 必要なら max_patterns を設定して試運転
    ranking = run_exhaustive(base_df=base, max_patterns=50)
    if len(ranking) == 0:
        print("結果なし")
        return

    ranking.to_pickle(OUTPUT_DIR / "ranking_raw.pkl")
    ranking.to_csv(OUTPUT_DIR / "ranking_all.csv", index=False, encoding="utf-8-sig")

    print("[確認] ranking columns:", list(ranking.columns))
    if "error" in ranking.columns and ranking["error"].notna().any():
        print("[確認] first error:", ranking.loc[ranking["error"].notna(), "error"].iloc[0])
    if "error" in ranking.columns:
        err_only = ranking[ranking["error"].notna()] if ranking["error"].notna().any() else pd.DataFrame()
        if len(err_only) > 0:
            err_only.to_csv(OUTPUT_DIR / "ranking_errors.csv", index=False, encoding="utf-8-sig")
            print(f"[警告] エラー行あり: {len(err_only)} 件")

    top_summary, _ = rerun_top_patterns(base, ranking, top_n=TOP_N)
    if len(top_summary) > 0:
        top_summary = top_summary.sort_values(["pf", "pnl", "win_rate", "n"], ascending=[False, False, False, False])
        top_summary.to_csv(OUTPUT_DIR / "top10_summary.csv", index=False, encoding="utf-8-sig")
    else:
        print("[警告] top10_summary は空です")

    print("保存完了")
    print(OUTPUT_DIR)


if __name__ == "__main__":
    main()
