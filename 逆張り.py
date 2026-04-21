from pathlib import Path
import pandas as pd
import numpy as np

# ========================================
# 逆張り 強パターン固定バックテスト
# - CPI除外あり
# - DST判定あり
# - 手数料 2.2pt込み
# - 足判定は bar END 基準で統一
# ========================================

DATA_DIR = Path(r"C:\kabu_trade\data")
EXCEL_FILES = [
    "N225microf_2023.xlsx",
    "N225microf_2024.xlsx",
    "N225microf_2025.xlsx",
    "N225microf_2026.xlsx",
]
MICRO_CSV = Path(r"C:\kabu_trade\micro_5min.csv")
CPI_CSV = Path(r"C:\kabu_trade\economic_calendar.csv")
OUT_DIR = Path(r"C:\kabu_trade\data\gyakubari_search")

PT_TO_YEN = 10
COMMISSION_PT = 2.2

# ④で狙う月
TARGET_MONTHS = {1,2,3,4,5,6,7,8,9,10,11,12}

# 強パターン固定
LONG_PARAM = {
    "move_pct": 0.003,
    "rsi_th": 30,
    "vol_th": 0.8,
    "lookback": 2,
    "recovery_pct": 0.002,
    "tp": 120,
    "sl": 60,
    "max_hold": 3,
}
SHORT_PARAM = {
    "move_pct": 0.004,
    "rsi_th": 70,
    "vol_th": 0.8,
    "lookback": 4,
    "recovery_pct": 0.002,
    "tp": 120,
    "sl": 60,
    "max_hold": 6,
}

# 逆張りの時間帯（bar END hour 基準）
# 必要なら後で絞る
LONG_HOURS_DST = (5, 8, 12, 14, 15, 19, 20, 21, 22, 23)
LONG_HOURS_WIN = (5, 8, 12, 15, 19, 20, 21, 22, 23)

SHORT_HOURS_DST = (5, 8, 12, 14, 15, 19, 20, 21, 22, 23)
SHORT_HOURS_WIN = (5, 8, 12, 15, 19, 20, 21, 22, 23)

# 米国サマータイム期間
_DST_PERIODS = [
    (pd.Timestamp("2023-03-12"), pd.Timestamp("2023-11-05")),
    (pd.Timestamp("2024-03-10"), pd.Timestamp("2024-11-03")),
    (pd.Timestamp("2025-03-09"), pd.Timestamp("2025-11-02")),
    (pd.Timestamp("2026-03-08"), pd.Timestamp("2026-11-01")),
]


# ========================================
# データ読み込み
# ========================================
def read_excel(path: Path) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name="5min", engine="openpyxl")
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
            dc = (
                dc.dropna(subset=["datetime", "open", "high", "low", "close"])
                [["datetime", "open", "high", "low", "close", "volume"]]
                .sort_values("datetime")
            )
            print(f"  micro_5min.csv: {len(dc)} 本")
            dfs.append(dc)
        except Exception as e:
            print(f"  micro_5min.csv 読み込み失敗: {e}")

    if not dfs:
        raise FileNotFoundError("データファイルが見つかりません")

    df = (
        pd.concat(dfs, ignore_index=True)
        .sort_values("datetime")
        .drop_duplicates(subset=["datetime"])
        .reset_index(drop=True)
    )
    print(f"合計: {len(df)} 本  ({df['datetime'].min()} ~ {df['datetime'].max()})")
    return df


# ========================================
# 指標
# ========================================
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma20"]

    delta = df["close"].diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    avg_up = up.rolling(14).mean()
    avg_down = down.rolling(14).mean()
    rs = avg_up / avg_down.replace(0, np.nan)
    df["rsi14"] = 100 - (100 / (1 + rs))

    df["year"] = df["datetime"].dt.year
    df["month"] = df["datetime"].dt.month
    df["date"] = df["datetime"].dt.date

    return df


# ========================================
# CPI / DST
# ========================================
def load_cpi() -> pd.DataFrame:
    for enc in ("utf-8", "utf-8-sig", "cp932"):
        try:
            df = pd.read_csv(CPI_CSV, encoding=enc)
            if "indicator" not in df.columns or "release_datetime_jst" not in df.columns:
                continue
            df["release_datetime_jst"] = pd.to_datetime(df["release_datetime_jst"], errors="coerce")
            cpi = df[df["indicator"] == "米CPI"].dropna(subset=["release_datetime_jst"]).reset_index(drop=True)
            print(f"[OK] CPI読み込み: {len(cpi)}件")
            return cpi
        except Exception:
            continue

    print("[WARN] economic_calendar.csv 読み込み失敗 -> CPI除外無効")
    return pd.DataFrame(columns=["release_datetime_jst"])


def build_masks(dts_ns: np.ndarray, cpi_df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    n = len(dts_ns)
    dst_mask = np.zeros(n, dtype=bool)
    cpi_mask = np.zeros(n, dtype=bool)

    for start, end in _DST_PERIODS:
        s_ns = start.value
        e_ns = end.value
        dst_mask |= (dts_ns >= s_ns) & (dts_ns <= e_ns)

    if len(cpi_df) > 0:
        before_ns = int(pd.Timedelta(minutes=30).total_seconds() * 1e9)
        after_ns = int(pd.Timedelta(minutes=60).total_seconds() * 1e9)
        for release in cpi_df["release_datetime_jst"]:
            r_ns = pd.Timestamp(release).value
            cpi_mask |= (dts_ns >= r_ns - before_ns) & (dts_ns <= r_ns + after_ns)

    return dst_mask, cpi_mask


# ========================================
# シグナル
# ========================================
def signal_long(df: pd.DataFrame, i: int, p: dict, dst_mask: np.ndarray, cpi_mask: np.ndarray) -> bool:
    if i < max(p["lookback"], 20):
        return False

    cur = df.iloc[i]
    prev = df.iloc[i - p["lookback"]]

    if pd.isna(cur["rsi14"]) or pd.isna(cur["vol_ratio"]):
        return False

    if cpi_mask[i]:
        return False

    drop_pct = (cur["close"] - prev["close"]) / prev["close"]
    rebound = (cur["close"] - cur["low"]) / cur["close"]

    conds = [
        drop_pct <= -p["move_pct"],
        cur["rsi14"] <= p["rsi_th"],
        cur["vol_ratio"] >= p["vol_th"],
        rebound >= p["recovery_pct"],
    ]
    return all(conds)


def signal_short(df: pd.DataFrame, i: int, p: dict, dst_mask: np.ndarray, cpi_mask: np.ndarray) -> bool:
    if i < max(p["lookback"], 20):
        return False

    cur = df.iloc[i]
    prev = df.iloc[i - p["lookback"]]

    if pd.isna(cur["rsi14"]) or pd.isna(cur["vol_ratio"]):
        return False

    if cpi_mask[i]:
        return False

    rise_pct = (cur["close"] - prev["close"]) / prev["close"]
    fade = (cur["high"] - cur["close"]) / cur["close"]

    conds = [
        rise_pct >= p["move_pct"],
        cur["rsi14"] >= p["rsi_th"],
        cur["vol_ratio"] >= p["vol_th"],
        fade >= p["recovery_pct"],
    ]
    return all(conds)


# ========================================
# 約定
# ========================================
def exec_trade(
    arr_high: np.ndarray,
    arr_low: np.ndarray,
    arr_close: np.ndarray,
    entry_price: float,
    entry_idx: int,
    side: str,
    tp: int,
    sl: int,
    max_hold: int,
    n: int,
) -> tuple[float, int, str]:
    pnl = None
    exit_idx = entry_idx
    result = "TIME"

    for j in range(entry_idx, min(entry_idx + max_hold, n)):
        hi = arr_high[j]
        lo = arr_low[j]

        if side == "long":
            if hi >= entry_price + tp:
                pnl = float(tp)
                exit_idx = j
                result = "TP"
                break
            if lo <= entry_price - sl:
                pnl = float(-sl)
                exit_idx = j
                result = "SL"
                break
        else:
            if lo <= entry_price - tp:
                pnl = float(tp)
                exit_idx = j
                result = "TP"
                break
            if hi >= entry_price + sl:
                pnl = float(-sl)
                exit_idx = j
                result = "SL"
                break

    if pnl is None:
        exit_idx = min(entry_idx + max_hold - 1, n - 1)
        cl = arr_close[exit_idx]
        pnl = float(cl - entry_price) if side == "long" else float(entry_price - cl)
        result = "TIME"

    pnl -= COMMISSION_PT
    return pnl, exit_idx, result


# ========================================
# バックテスト
# ========================================
def run_backtest(df: pd.DataFrame, cpi_df: pd.DataFrame, side: str, p: dict) -> pd.DataFrame:
    dts = pd.to_datetime(df["datetime"])
    dts_ns = dts.values.astype("int64")
    dst_mask, cpi_mask = build_masks(dts_ns, cpi_df)

    arr_open = df["open"].values
    arr_high = df["high"].values
    arr_low = df["low"].values
    arr_close = df["close"].values

    rows = []
    n = len(df)

    for i in range(20, n - 1):
        ok = signal_long(df, i, p, dst_mask, cpi_mask) if side == "long" else signal_short(df, i, p, dst_mask, cpi_mask)
        if not ok:
            continue

        entry_idx = i + 1
        if entry_idx >= n:
            continue

        entry_price = float(arr_open[entry_idx])
        pnl, exit_idx, result = exec_trade(
            arr_high=arr_high,
            arr_low=arr_low,
            arr_close=arr_close,
            entry_price=entry_price,
            entry_idx=entry_idx,
            side=side,
            tp=p["tp"],
            sl=p["sl"],
            max_hold=p["max_hold"],
            n=n,
        )

        rows.append({
            "side": side,
            "signal_time": pd.Timestamp(df.iloc[i]["datetime"]),
            "entry_time": pd.Timestamp(df.iloc[entry_idx]["datetime"]),
            "exit_time": pd.Timestamp(df.iloc[exit_idx]["datetime"]),
            "year": int(df.iloc[i]["year"]),
            "month": int(df.iloc[i]["month"]),
            "pnl_pt": round(float(pnl), 1),
            "pnl_yen": int(round(float(pnl) * PT_TO_YEN, 0)),
            "result": result,
        })

    if not rows:
        return pd.DataFrame()

    return pd.DataFrame(rows)


# ========================================
# 集計
# ========================================
def calc_summary(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"n": 0, "win_rate": 0.0, "pnl_pt": 0.0, "pnl_yen": 0, "ev": 0.0, "pf": 0.0}

    pnl = trades["pnl_pt"].astype(float).values
    wins = pnl[pnl > 0].sum()
    losses = abs(pnl[pnl < 0].sum())
    n = len(pnl)

    return {
        "n": int(n),
        "win_rate": round((pnl > 0).mean() * 100, 1),
        "pnl_pt": round(float(pnl.sum()), 1),
        "pnl_yen": int(round(trades["pnl_yen"].sum(), 0)),
        "ev": round(float(pnl.sum() / n), 2),
        "pf": round(float(wins / losses), 3) if losses > 0 else 0.0,
    }


def print_summary(label: str, trades: pd.DataFrame):
    s = calc_summary(trades)
    print(f"{label:12} 件数:{s['n']:>4}  勝率:{s['win_rate']:>5.1f}%  損益:{s['pnl_yen']:>+9,}円  EV:{s['ev']:>+6.2f}  PF:{s['pf']:.3f}")


def print_year_month_table(trades: pd.DataFrame, title: str):
    print("\n" + "=" * 80)
    print(f"  {title}")
    print("=" * 80)

    if trades.empty:
        print("データなし")
        return

    months = list(range(1, 13))
    years = sorted(trades["year"].unique())

    print("  年  " + "".join(f"  {m:>4}月" for m in months) + "    合計")
    print("  " + "-" * 78)

    for yr in years:
        yr_df = trades[trades["year"] == yr]
        vals = []
        total = 0
        for mo in months:
            v = int(yr_df[yr_df["month"] == mo]["pnl_yen"].sum())
            vals.append(f"{v:>+6,}")
            total += v
        print(f"  {yr}  " + "  ".join(vals) + f"  {total:>+8,}")

def calc_dd(trades):
    if trades.empty:
        return 0

    equity = trades["pnl_yen"].cumsum()
    peak = equity.cummax()
    dd = equity - peak
    return int(dd.min())        


# ========================================
# メイン
# ========================================
def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    df = load_data()
    df = add_indicators(df)
    cpi = load_cpi()

    print("\nバックテスト実行中...")
    long_trades = run_backtest(df, cpi, "long", LONG_PARAM)
    short_trades = run_backtest(df, cpi, "short", SHORT_PARAM)

    both_trades = (
        pd.concat([long_trades, short_trades], ignore_index=True)
        .sort_values("entry_time")
        .reset_index(drop=True)
    )

    print("\n[固定パターン成績]")
    print_summary("ロング④", long_trades)
    print_summary("ショート④", short_trades)
    print_summary("合算④", both_trades)

    print("\n[DD]")
    print("ロングDD:", calc_dd(long_trades))
    print("ショートDD:", calc_dd(short_trades))
    print_year_month_table(long_trades, "ロング④ 年×月 損益(円)")
    print_year_month_table(short_trades, "ショート④ 年×月 損益(円)")
    print_year_month_table(both_trades, "合算④ 年×月 損益(円)")

    long_trades.to_csv(OUT_DIR / "逆張り_long_trades.csv", index=False, encoding="utf-8-sig")
    short_trades.to_csv(OUT_DIR / "逆張り_short_trades.csv", index=False, encoding="utf-8-sig")
    both_trades.to_csv(OUT_DIR / "逆張り_both_trades.csv", index=False, encoding="utf-8-sig")
    print(f"\n保存先: {OUT_DIR}")

def print_hourly(trades, label):
    if trades.empty:
        print(label, "データなし")
        return

    trades = trades.copy()
    trades["hour"] = trades["entry_time"].dt.hour

    g = trades.groupby("hour")["pnl_yen"].agg(["count", "sum"])
    g["pf"] = trades.groupby("hour")["pnl_pt"].apply(
        lambda x: x[x>0].sum() / abs(x[x<0].sum()) if (x<0).sum()!=0 else 0
    )

    print("\n", label)
    print(g.sort_index())

    print_hourly(long_trades, "ロング 時間帯")
    print_hourly(short_trades, "ショート 時間帯")


if __name__ == "__main__":
    main()