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
    "move_pct": 0.002,
    "rsi_th": 40,
    "vol_th": 0.8,
    "lookback": 1,
    "recovery_pct": 0.002,
    "tp": 120,
    "sl": 60,
    "max_hold": 6,
}

SHORT_4_PARAM = {
    "move_pct": 0.004,
    "rsi_th": 70,
    "vol_th": 0.8,
    "lookback": 4,
    "recovery_pct": 0.002,
    "tp": 120,
    "sl": 60,
    "max_hold": 6,
}

SHORT_5A_PARAM = {   # 出来高だけ緩める
    "move_pct": 0.004,
    "rsi_th": 70,
    "vol_th": 0.7,
    "lookback": 4,
    "recovery_pct": 0.002,
    "tp": 120,
    "sl": 60,
    "max_hold": 6,
}

SHORT_5B_PARAM = {   # RSIだけ緩める
    "move_pct": 0.004,
    "rsi_th": 65,
    "vol_th": 0.8,
    "lookback": 4,
    "recovery_pct": 0.002,
    "tp": 120,
    "sl": 60,
    "max_hold": 6,
}

SHORT_5C_PARAM = {   # 両方緩める
    "move_pct": 0.004,
    "rsi_th": 65,
    "vol_th": 0.7,
    "lookback": 4,
    "recovery_pct": 0.002,
    "tp": 120,
    "sl": 60,
    "max_hold": 6,
}

SHORT_5D_PARAM = {   # move_pctだけ緩める
    "move_pct": 0.003,
    "rsi_th": 70,
    "vol_th": 0.8,
    "lookback": 4,
    "recovery_pct": 0.002,
    "tp": 120,
    "sl": 60,
    "max_hold": 6,
}

SHORT_5E_PARAM = {   # lookbackだけ緩める
    "move_pct": 0.004,
    "rsi_th": 70,
    "vol_th": 0.8,
    "lookback": 3,
    "recovery_pct": 0.002,
    "tp": 120,
    "sl": 60,
    "max_hold": 6,
}

SHORT_5F_PARAM = {   # move_pct + lookback 両方緩める
    "move_pct": 0.003,
    "rsi_th": 40,
    "vol_th": 0.18,
    "lookback": 4,
    "recovery_pct": 0.002,
    "tp": 120,
    "sl": 60,
    "max_hold": 6,
}
# ←ここに追加
SHORT_5F_ALL_PARAM = {
    "move_pct": 0.003,
    "rsi_th": 40,
    "vol_th": 0.18,
    "lookback": 4,
    "recovery_pct": 0.002,
    "tp": 120,
    "sl": 60,
    "max_hold": 6,
}

LONG_4B_PARAM = {
    "move_pct": 0.001, "rsi_th": 40, "vol_th": 0.8,
    "lookback": 1, "recovery_pct": 0.002,
    "tp": 120, "sl": 60, "max_hold": 6,
}

LONG_4C_PARAM = {
    "move_pct": 0.002, "rsi_th": 40, "vol_th": 0.5,
    "lookback": 1, "recovery_pct": 0.002,
    "tp": 120, "sl": 60, "max_hold": 6,
}

SHORT_5G_PARAM = {
    "move_pct": 0.003, "rsi_th": 40, "vol_th": 0.1,
    "lookback": 4, "recovery_pct": 0.002,
    "tp": 120, "sl": 60, "max_hold": 6,
}

SHORT_5H_PARAM = {
    "move_pct": 0.003, "rsi_th": 40, "vol_th": 0.05,
    "lookback": 4, "recovery_pct": 0.002,
    "tp": 120, "sl": 60, "max_hold": 6,
}

SHORT_5I_PARAM = {  # lookback=1
    "move_pct": 0.003, "rsi_th": 40, "vol_th": 0.18,
    "lookback": 1, "recovery_pct": 0.002,
    "tp": 120, "sl": 60, "max_hold": 6,
}

SHORT_5J_PARAM = {  # lookback=2
    "move_pct": 0.003, "rsi_th": 40, "vol_th": 0.18,
    "lookback": 2, "recovery_pct": 0.002,
    "tp": 120, "sl": 60, "max_hold": 6,
}

SHORT_5K_PARAM = {  # lookback=3
    "move_pct": 0.003, "rsi_th": 40, "vol_th": 0.18,
    "lookback": 3, "recovery_pct": 0.002,
    "tp": 120, "sl": 60, "max_hold": 6,
}

SHORT_5L_PARAM = {
    "move_pct": 0.003, "rsi_th": 40, "vol_th": 0.18,
    "lookback": 4, "recovery_pct": 0.0,
    "tp": 120, "sl": 60, "max_hold": 6,
}

SHORT_5M_PARAM = {
    "move_pct": 0.003, "rsi_th": 40, "vol_th": 0.0,
    "lookback": 4, "recovery_pct": 0.0,
    "tp": 120, "sl": 60, "max_hold": 6,
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
def signal_long(df, i, p, dst_mask, cpi_mask, disable_hour_filter=False):
    if i < max(p["lookback"], 20):
        return False

    cur = df.iloc[i]
    prev = df.iloc[i - p["lookback"]]

    if pd.isna(cur["rsi14"]) or pd.isna(cur["vol_ratio"]):
        return False

    dt = pd.to_datetime(cur["datetime"])
    hour = dt.hour

    # ★追加
    if hour < 5:
        return False

    move_pct = (cur["close"] - prev["close"]) / prev["close"]
    recovery = (cur["close"] - cur["low"]) / cur["close"] if cur["close"] != 0 else 0

    return all([
        move_pct <= -p["move_pct"],
        cur["rsi14"] <= p["rsi_th"],
        cur["vol_ratio"] >= p["vol_th"],
        recovery >= p["recovery_pct"],
    ])

def signal_short(
    df: pd.DataFrame,
    i: int,
    p: dict,
    dst_mask: np.ndarray,
    cpi_mask: np.ndarray,
    disable_hour_filter=False,
    disable_cpi_filter=False,
) -> bool:
    
    if i < max(p["lookback"], 20):
        return False

    cur = df.iloc[i]
    prev = df.iloc[i - p["lookback"]]

    if pd.isna(cur["rsi14"]) or pd.isna(cur["vol_ratio"]):
        return False

    if (not disable_cpi_filter) and cpi_mask[i]:
        return False

    rise_pct = (cur["close"] - prev["close"]) / prev["close"]
    fade = (cur["high"] - cur["close"]) / cur["close"]

    conds = [
        rise_pct >= p["move_pct"],
        cur["rsi14"] >= p["rsi_th"],
        cur["vol_ratio"] >= p["vol_th"],
        fade >= p["recovery_pct"],
    ]

    # ← ここ追加（超重要）
    dt = pd.to_datetime(cur["datetime"])
    hour = (dt - pd.Timedelta(minutes=5)).hour

    if hour < 5:
        return False

    is_dst_now = dst_mask[i]

    if not disable_hour_filter:
        if is_dst_now:
            if hour not in SHORT_HOURS_DST:
                return False
        else:
            if hour not in SHORT_HOURS_WIN:
                return False

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
def run_backtest(
    df: pd.DataFrame,
    cpi_df: pd.DataFrame,
    side: str,
    p: dict,
    label: str = "",
    disable_hour_filter=False,
    disable_cpi_filter=False,
) -> pd.DataFrame:
    
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
        if side == "long":
            ok = signal_long(df, i, p, dst_mask, cpi_mask, disable_hour_filter=disable_hour_filter)
        else:
            ok = signal_short(
                df, i, p, dst_mask, cpi_mask,
                disable_hour_filter=disable_hour_filter,
                disable_cpi_filter=disable_cpi_filter,
            )

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
            "label": label,
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

    # =========================
    # 0〜4時除去 テスト（④⑤）
    # =========================
    print("\n" + "="*80)
    print("  0〜4時除去 結果（④⑤）")
    print("="*80)

    long_4 = run_backtest(df, cpi, "long", LONG_PARAM, "LONG_4")
    
    short_4  = run_backtest(df, cpi, "short", SHORT_4_PARAM,  "SHORT_4")

    short_5a = run_backtest(df, cpi, "short", SHORT_5A_PARAM, "SHORT_5A")
    short_5b = run_backtest(df, cpi, "short", SHORT_5B_PARAM, "SHORT_5B")
    short_5c = run_backtest(df, cpi, "short", SHORT_5C_PARAM, "SHORT_5C")
    short_5d = run_backtest(df, cpi, "short", SHORT_5D_PARAM, "SHORT_5D")
    short_5e = run_backtest(df, cpi, "short", SHORT_5E_PARAM, "SHORT_5E")
    short_5f = run_backtest(df, cpi, "short", SHORT_5F_PARAM, "SHORT_5F")
    short_5f_all = run_backtest(df, cpi, "short", SHORT_5F_ALL_PARAM, "SHORT_5F_ALL", disable_hour_filter=True)

    long_4_all = run_backtest(df, cpi, "long", LONG_PARAM, "LONG_4_ALL", disable_hour_filter=True)

    # ↓ここから追加
    long_4b = run_backtest(df, cpi, "long", LONG_4B_PARAM, "LONG_4B", disable_hour_filter=True)
    long_4c = run_backtest(df, cpi, "long", LONG_4C_PARAM, "LONG_4C", disable_hour_filter=True)
    short_5g = run_backtest(df, cpi, "short", SHORT_5G_PARAM, "SHORT_5G", disable_hour_filter=True)
    short_5h = run_backtest(df, cpi, "short", SHORT_5H_PARAM, "SHORT_5H", disable_hour_filter=True)

    short_5l = run_backtest(df, cpi, "short", SHORT_5L_PARAM, "SHORT_5L", disable_hour_filter=True)
    short_5m = run_backtest(df, cpi, "short", SHORT_5M_PARAM, "SHORT_5M", disable_hour_filter=True)

    both_5l = pd.concat([long_4, short_5l], ignore_index=True).sort_values("entry_time").reset_index(drop=True)
    both_5m = pd.concat([long_4, short_5m], ignore_index=True).sort_values("entry_time").reset_index(drop=True)
    both_4b5l = pd.concat([long_4b, short_5l], ignore_index=True).sort_values("entry_time").reset_index(drop=True)
    both_4b5m = pd.concat([long_4b, short_5m], ignore_index=True).sort_values("entry_time").reset_index(drop=True)

    print("\n[パラメータ除去検証]")
    print_summary("ショート⑤L(rec=0)", short_5l);     print("DD:", calc_dd(short_5l))
    print_summary("ショート⑤M(vol+rec=0)", short_5m); print("DD:", calc_dd(short_5m))
    print_summary("合算④+⑤L",  both_5l);   print("DD:", calc_dd(both_5l))
    print_summary("合算④+⑤M",  both_5m);   print("DD:", calc_dd(both_5m))
    print_summary("合算④B+⑤L", both_4b5l); print("DD:", calc_dd(both_4b5l))
    print_summary("合算④B+⑤M", both_4b5m); print("DD:", calc_dd(both_4b5m))

    both_4b = pd.concat([long_4b, short_5f_all], ignore_index=True).sort_values("entry_time").reset_index(drop=True)
    both_4c = pd.concat([long_4c, short_5f_all], ignore_index=True).sort_values("entry_time").reset_index(drop=True)
    both_5g = pd.concat([long_4, short_5g], ignore_index=True).sort_values("entry_time").reset_index(drop=True)
    both_5h = pd.concat([long_4, short_5h], ignore_index=True).sort_values("entry_time").reset_index(drop=True)
    both_4b5g = pd.concat([long_4b, short_5g], ignore_index=True).sort_values("entry_time").reset_index(drop=True)
    both_4b5h = pd.concat([long_4b, short_5h], ignore_index=True).sort_values("entry_time").reset_index(drop=True)

    print("\n[新パラメータ候補]")
    print_summary("ロング④B", long_4b);      print("DD:", calc_dd(long_4b))
    print_summary("ロング④C", long_4c);      print("DD:", calc_dd(long_4c))
    print_summary("ショート⑤G", short_5g);   print("DD:", calc_dd(short_5g))
    print_summary("ショート⑤H", short_5h);   print("DD:", calc_dd(short_5h))
    print_summary("合算④B+⑤F", both_4b);    print("DD:", calc_dd(both_4b))
    print_summary("合算④C+⑤F", both_4c);    print("DD:", calc_dd(both_4c))
    print_summary("合算④+⑤G", both_5g);     print("DD:", calc_dd(both_5g))
    print_summary("合算④+⑤H", both_5h);     print("DD:", calc_dd(both_5h))
    print_summary("合算④B+⑤G", both_4b5g);  print("DD:", calc_dd(both_4b5g))
    print_summary("合算④B+⑤H", both_4b5h);  print("DD:", calc_dd(both_4b5h))
    # ↑ここまで追加

    both_5f_all_both = pd.concat([long_4_all, short_5f_all], ignore_index=True).sort_values("entry_time").reset_index(drop=True)
    print_summary("合算④⑤全時間", both_5f_all_both)
    print("合算④⑤全時間DD:", calc_dd(both_5f_all_both))

    short_5f_all_no_cpi = run_backtest(
        df, cpi, "short", SHORT_5F_ALL_PARAM,
        "SHORT_5F_ALL_NO_CPI",
        disable_hour_filter=True,
        disable_cpi_filter=True,
    )

    print_summary("ショート⑤D", short_5d)
    print("ショート⑤DDD:", calc_dd(short_5d))

    print_summary("ショート⑤E", short_5e)
    print("ショート⑤EDD:", calc_dd(short_5e))

    print_summary("ショート⑤F", short_5f)
    print("ショート⑤FDD:", calc_dd(short_5f))
    # ←ここ
    print_summary("ショート⑤F_全時間", short_5f_all)
    print("ショート⑤F_ALL_DD:", calc_dd(short_5f_all))

    print_summary("ショート⑤F_全時間_CPI無", short_5f_all_no_cpi)
    print("ショート⑤F_ALL_NO_CPI_DD:", calc_dd(short_5f_all_no_cpi))

    print("\n[ロング④]")
    print_summary("ロング④", long_4)
    print("ロングDD:", calc_dd(long_4))

    print("\n[ショート比較]")
    print_summary("ショート④", short_4)
    print("ショート④DD:", calc_dd(short_4))

    print_summary("ショート⑤A", short_5a)
    print("ショート⑤ADD:", calc_dd(short_5a))

    print_summary("ショート⑤B", short_5b)
    print("ショート⑤BDD:", calc_dd(short_5b))

    print_summary("ショート⑤C", short_5c)
    print("ショート⑤CDD:", calc_dd(short_5c))

    both_4  = pd.concat([long_4, short_4], ignore_index=True).sort_values("entry_time").reset_index(drop=True)
    both_5a = pd.concat([long_4, short_5a], ignore_index=True).sort_values("entry_time").reset_index(drop=True)
    both_5b = pd.concat([long_4, short_5b], ignore_index=True).sort_values("entry_time").reset_index(drop=True)
    both_5c = pd.concat([long_4, short_5c], ignore_index=True).sort_values("entry_time").reset_index(drop=True)
    both_5d = pd.concat([long_4, short_5d], ignore_index=True).sort_values("entry_time").reset_index(drop=True)
    both_5e = pd.concat([long_4, short_5e], ignore_index=True).sort_values("entry_time").reset_index(drop=True)
    both_5f = pd.concat([long_4, short_5f], ignore_index=True).sort_values("entry_time").reset_index(drop=True)
    both_5f_all = pd.concat([long_4, short_5f_all], ignore_index=True).sort_values("entry_time").reset_index(drop=True)

    both_5f_all_no_cpi = pd.concat(
        [long_4, short_5f_all_no_cpi],
        ignore_index=True
    ).sort_values("entry_time").reset_index(drop=True)

    print("\n[合算比較]")
    print_summary("合算④", both_4)
    print("合算④DD:", calc_dd(both_4))

    print_summary("合算⑤A", both_5a)
    print("合算⑤ADD:", calc_dd(both_5a))

    print_summary("合算⑤B", both_5b)
    print("合算⑤BDD:", calc_dd(both_5b))

    print_summary("合算⑤C", both_5c)
    print("合算⑤CDD:", calc_dd(both_5c))

    print_summary("合算⑤D", both_5d)
    print("合算⑤DDD:", calc_dd(both_5d))

    print_summary("合算⑤E", both_5e)
    print("合算⑤EDD:", calc_dd(both_5e))

    print_summary("合算⑤F", both_5f)
    print("合算⑤FDD:", calc_dd(both_5f))

    print_summary("合算⑤F_全時間", both_5f_all)
    print("合算⑤F_ALL_DD:", calc_dd(both_5f_all))

    print_summary("合算⑤F_全時間_CPI無", both_5f_all_no_cpi)
    print("合算⑤F_ALL_NO_CPI_DD:", calc_dd(both_5f_all_no_cpi))

    print_year_month_table(short_4,  "ショート④ 年×月 損益(円)")
    print_year_month_table(short_5a, "ショート⑤A 年×月 損益(円)")
    print_year_month_table(short_5b, "ショート⑤B 年×月 損益(円)")
    print_year_month_table(short_5c, "ショート⑤C 年×月 損益(円)")
    print_year_month_table(short_5d, "ショート⑤D 年×月 損益(円)")
    print_year_month_table(short_5e, "ショート⑤E 年×月 損益(円)")
    print_year_month_table(short_5f, "ショート⑤F 年×月 損益(円)")

    short_4.to_csv(OUT_DIR / "gyakubari_short_4.csv", index=False, encoding="utf-8-sig")
    short_5a.to_csv(OUT_DIR / "gyakubari_short_5a.csv", index=False, encoding="utf-8-sig")
    short_5b.to_csv(OUT_DIR / "gyakubari_short_5b.csv", index=False, encoding="utf-8-sig")
    short_5c.to_csv(OUT_DIR / "gyakubari_short_5c.csv", index=False, encoding="utf-8-sig")
    short_5i = run_backtest(df, cpi, "short", SHORT_5I_PARAM, "SHORT_5I", disable_hour_filter=True)
    short_5j = run_backtest(df, cpi, "short", SHORT_5J_PARAM, "SHORT_5J", disable_hour_filter=True)
    short_5k = run_backtest(df, cpi, "short", SHORT_5K_PARAM, "SHORT_5K", disable_hour_filter=True)

    both_5i = pd.concat([long_4, short_5i], ignore_index=True).sort_values("entry_time").reset_index(drop=True)
    both_5j = pd.concat([long_4, short_5j], ignore_index=True).sort_values("entry_time").reset_index(drop=True)
    both_5k = pd.concat([long_4, short_5k], ignore_index=True).sort_values("entry_time").reset_index(drop=True)
    both_4b5i = pd.concat([long_4b, short_5i], ignore_index=True).sort_values("entry_time").reset_index(drop=True)

    print("\n[lookbackバリエーション]")
    print_summary("ショート⑤I(lb=1)", short_5i); print("DD:", calc_dd(short_5i))
    print_summary("ショート⑤J(lb=2)", short_5j); print("DD:", calc_dd(short_5j))
    print_summary("ショート⑤K(lb=3)", short_5k); print("DD:", calc_dd(short_5k))
    print_summary("合算④+⑤I",  both_5i);  print("DD:", calc_dd(both_5i))
    print_summary("合算④+⑤J",  both_5j);  print("DD:", calc_dd(both_5j))
    print_summary("合算④+⑤K",  both_5k);  print("DD:", calc_dd(both_5k))
    print_summary("合算④B+⑤I", both_4b5i); print("DD:", calc_dd(both_4b5i))

    both_4.to_csv(OUT_DIR / "gyakubari_both_4.csv", index=False, encoding="utf-8-sig")
    both_5a.to_csv(OUT_DIR / "gyakubari_both_5a.csv", index=False, encoding="utf-8-sig")
    both_5b.to_csv(OUT_DIR / "gyakubari_both_5b.csv", index=False, encoding="utf-8-sig")
    both_5c.to_csv(OUT_DIR / "gyakubari_both_5c.csv", index=False, encoding="utf-8-sig")
    short_5d.to_csv(OUT_DIR / "gyakubari_short_5d.csv", index=False, encoding="utf-8-sig")
    short_5e.to_csv(OUT_DIR / "gyakubari_short_5e.csv", index=False, encoding="utf-8-sig")
    short_5f.to_csv(OUT_DIR / "gyakubari_short_5f.csv", index=False, encoding="utf-8-sig")

    both_5d.to_csv(OUT_DIR / "gyakubari_both_5d.csv", index=False, encoding="utf-8-sig")
    both_5e.to_csv(OUT_DIR / "gyakubari_both_5e.csv", index=False, encoding="utf-8-sig")
    both_5f.to_csv(OUT_DIR / "gyakubari_both_5f.csv", index=False, encoding="utf-8-sig")

    short_5f_all.to_csv(OUT_DIR / "gyakubari_short_5f_all.csv", index=False, encoding="utf-8-sig")
    both_5f_all.to_csv(OUT_DIR / "gyakubari_both_5f_all.csv", index=False, encoding="utf-8-sig")

    print(f"\n保存先: {OUT_DIR}")

    # =========================
    # 月次DDチェック（合算⑤F_全時間）
    # =========================
    print("\n" + "="*80)
    print("  合算⑤F_全時間 月次DDチェック")
    print("="*80)

    df = both_5f_all.copy()
    df = df.sort_values("entry_time")

    # 年月キー作成
    df["ym"] = list(zip(df["year"], df["month"]))

    monthly = df.groupby("ym")["pnl_yen"].sum()

    dd_limit = -5000
    bad_months = []

    print(f"{'年月':>10}  {'損益(円)':>12}  判定")
    print("-"*40)

    for ym, pnl in monthly.items():
        flag = "✗" if pnl <= dd_limit else "○"
        if flag == "✗":
            bad_months.append(ym)
        print(f"{str(ym):>10}  {pnl:>+12,.0f}  {flag}")

    print("\n結果")
    print(f"DD発動月数: {len(bad_months)}")

    if bad_months:
        print("発動月:", sorted(bad_months))
    else:
        print("発動月なし")

    print("\n" + "="*80)
    print("  合算⑤F_全時間 年×月 詳細")
    print("="*80)

    df = both_5f_all.copy()

    for (y, m), g in df.groupby(["year", "month"]):
        n = len(g)
        win = (g["pnl_yen"] > 0).sum()
        lose = (g["pnl_yen"] <= 0).sum()
        pnl = g["pnl_yen"].sum()

        pf = (
            g[g["pnl_yen"] > 0]["pnl_yen"].sum() /
            abs(g[g["pnl_yen"] <= 0]["pnl_yen"].sum())
        ) if lose > 0 else float("inf")

        print(f"{y}-{m:02d} 件数:{n:3d} PF:{pf:.2f} 損益:{pnl:+8,.0f}")

    print("\n" + "="*80)
    print("  DD別パフォーマンス比較（合算⑤F_全時間）")
    print("="*80)

    df_dd = both_5f_all.copy()
    df_dd = df_dd.sort_values("entry_time").reset_index(drop=True)
    df_dd["ym"] = list(zip(df_dd["year"], df_dd["month"]))

    dd_levels = [-2000, -3000, -4000, -5000]

    for dd_limit in dd_levels:
        pnl_list = []
        running = 0
        stopped = False
        bad_months = set()

        for _, row in df_dd.iterrows():
            if stopped:
                continue

            running += row["pnl_yen"]
            pnl_list.append(row["pnl_yen"])

            if running <= dd_limit:
                stopped = True
                bad_months.add(row["ym"])

        if len(pnl_list) == 0:
            continue

        wins = [x for x in pnl_list if x > 0]
        losses = [x for x in pnl_list if x <= 0]

        total = sum(pnl_list)
        pf = (sum(wins) / abs(sum(losses))) if losses else float("inf")

        print(f"\nDD {dd_limit}円")
        print(f"件数:{len(pnl_list)} | PF:{pf:.2f} | 損益:{total:+,}")
        print(f"発動月数:{len(bad_months)}")

        if bad_months:
            print("発動月:", sorted(bad_months))
        else:
            print("発動月なし")

            print("\n" + "="*80)
    print("  月単位・エントリー順DD検証（合算⑤F_全時間）")
    print("="*80)

    df_dd = both_5f_all.copy()
    df_dd = df_dd.sort_values("entry_time").reset_index(drop=True)
    df_dd["ym"] = list(zip(df_dd["year"], df_dd["month"]))

    dd_levels = [-2000, -3000, -4000, -5000]

    for dd_limit in dd_levels:
        kept_rows = []
        bad_months = []

        for ym, g in df_dd.groupby("ym", sort=True):
            g = g.sort_values("entry_time").reset_index(drop=True)

            running = 0
            stopped = False

            for _, row in g.iterrows():
                if stopped:
                    continue

                running += row["pnl_yen"]
                kept_rows.append(row)

                if running <= dd_limit:
                    stopped = True
                    bad_months.append(ym)

        if not kept_rows:
            continue

        result_df = pd.DataFrame(kept_rows).sort_values("entry_time").reset_index(drop=True)

        wins = result_df[result_df["pnl_yen"] > 0]["pnl_yen"].sum()
        losses = abs(result_df[result_df["pnl_yen"] <= 0]["pnl_yen"].sum())
        total = result_df["pnl_yen"].sum()
        pf = (wins / losses) if losses > 0 else float("inf")

        print(f"\nDD {dd_limit}円")
        print(f"件数:{len(result_df)} | PF:{pf:.2f} | 損益:{total:+,}")
        print(f"発動月数:{len(bad_months)}")

        if bad_months:
            print("発動月:", sorted(set(bad_months)))
        else:
            print("発動月なし")  
                      

if __name__ == "__main__":
    main()