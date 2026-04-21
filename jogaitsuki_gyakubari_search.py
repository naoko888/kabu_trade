from pathlib import Path
import pandas as pd
import numpy as np
EXCLUDED_MONTHS = set()  # 除外月なし（全月対象）

TOP_N = 50   

# ========================================
# 除外月つき逆張り 総当たり検証
# - long: 下がった後に上がる
# - short: 上がった後に下がる
# ========================================

DATA_DIR = Path(r"C:\kabu_trade\data")
EXCEL_FILES = [
    "N225microf_2023.xlsx",
    "N225microf_2024.xlsx",
    "N225microf_2025.xlsx",
    "N225microf_2026.xlsx",
]
MICRO_CSV = Path(r"C:\kabu_trade\micro_5min.csv")
OUT_DIR = Path(r"C:\kabu_trade\data\gyakubari_search")

PT_TO_YEN = 10
COMMISSION_PT = 2.2

DROP_PCTS = [0.001, 0.002, 0.003, 0.004]   # 0.001追加
RISE_PCTS = [0.001, 0.002, 0.003, 0.004]   # 0.001追加

TP_LIST = [120]
SL_LIST = [60]
MAX_HOLD_LIST = [6]                          # 6固定（3・9削除）

RSI_LOW_LIST  = [30, 35, 40, 45, 50]        # 45・50追加
RSI_HIGH_LIST = [50, 55, 60, 65, 70]        # 50・55追加

LOOKBACK_BARS_LIST = [1, 2]                  # 3・4削除
RECOVERY_PCTS = [0]                          # recovery条件なし（全件拾う）
VOL_RATIO_LIST = [0.8]                       # 1.0削除、0.8固定


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
    print(f"合計: {len(df)} 本")
    return df


# ========================================
# 指標
# ========================================
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma20"]

    delta = df["close"].diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    avg_up = up.rolling(14).mean()
    avg_down = down.rolling(14).mean()
    rs = avg_up / avg_down.replace(0, np.nan)
    df["rsi14"] = 100 - (100 / (1 + rs))

    prev_close = df["close"].shift(1)
    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - prev_close).abs()
    tr3 = (df["low"] - prev_close).abs()
    df["tr"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["atr14"] = df["tr"].rolling(14).mean()

    df["month"] = df["datetime"].dt.month
    df["date"] = df["datetime"].dt.date
    return df


# ========================================
# エントリー条件
# ========================================
def signal_long_reversal(df: pd.DataFrame, i: int, p: dict) -> bool:
    if i < p["lookback"] or i < 20:
        return False

    cur = df.iloc[i]
    prev = df.iloc[i - p["lookback"]]
    if pd.isna(cur["rsi14"]) or pd.isna(cur["vol_ratio"]) or pd.isna(cur["atr14"]):
        return False

    drop_pct = (cur["close"] - prev["close"]) / prev["close"]
    rebound = (cur["close"] - cur["low"]) / cur["close"]

    conds = [
        cur["month"] not in EXCLUDED_MONTHS,
        drop_pct <= -p["move_pct"],
        cur["rsi14"] <= p["rsi_th"],
        cur["vol_ratio"] >= p["vol_th"],
        rebound >= p["recovery_pct"],
    ]
    return all(conds)


def signal_short_reversal(df: pd.DataFrame, i: int, p: dict) -> bool:
    if i < p["lookback"] or i < 20:
        return False

    cur = df.iloc[i]
    prev = df.iloc[i - p["lookback"]]
    if pd.isna(cur["rsi14"]) or pd.isna(cur["vol_ratio"]) or pd.isna(cur["atr14"]):
        return False

    rise_pct = (cur["close"] - prev["close"]) / prev["close"]
    fade = (cur["high"] - cur["close"]) / cur["close"]

    conds = [
        cur["month"] not in EXCLUDED_MONTHS,
        rise_pct >= p["move_pct"],
        cur["rsi14"] >= p["rsi_th"],
        cur["vol_ratio"] >= p["vol_th"],
        fade >= p["recovery_pct"],
    ]
    return all(conds)


# ========================================
# バックテスト
# ========================================
def run_backtest(df: pd.DataFrame, side: str, signal_params: dict, tp: int, sl: int, max_hold: int) -> dict:
    trades = []
    for i in range(20, len(df) - 1):
        ok = signal_long_reversal(df, i, signal_params) if side == "long" else signal_short_reversal(df, i, signal_params)
        if not ok:
            continue

        entry_idx = i + 1
        if entry_idx >= len(df):
            continue

        ep = float(df.iloc[entry_idx]["open"])
        pnl = None

        for j in range(entry_idx, min(entry_idx + max_hold, len(df))):
            hi = float(df.iloc[j]["high"])
            lo = float(df.iloc[j]["low"])
            cl = float(df.iloc[j]["close"])

            if side == "long":
                if hi >= ep + tp:
                    pnl = tp - COMMISSION_PT
                    break
                if lo <= ep - sl:
                    pnl = -sl - COMMISSION_PT
                    break
            else:
                if lo <= ep - tp:
                    pnl = tp - COMMISSION_PT
                    break
                if hi >= ep + sl:
                    pnl = -sl - COMMISSION_PT
                    break

        if pnl is None:
            exit_idx = min(entry_idx + max_hold - 1, len(df) - 1)
            cl = float(df.iloc[exit_idx]["close"])
            raw = (cl - ep) if side == "long" else (ep - cl)
            pnl = raw - COMMISSION_PT

        trades.append(pnl)

    if not trades:
        return {
            "n": 0,
            "win_rate": 0.0,
            "pnl": 0.0,
            "pf": 0.0,
            "ev": 0.0,
        }

    pnl_arr = np.array(trades, dtype=float)
    wins = pnl_arr[pnl_arr > 0].sum()
    losses = abs(pnl_arr[pnl_arr < 0].sum())
    n = len(pnl_arr)
    return {
        "n": n,
        "win_rate": round((pnl_arr > 0).mean() * 100, 1),
        "pnl": round(pnl_arr.sum(), 1),
        "pf": round(wins / losses, 3) if losses > 0 else 0.0,
        "ev": round(pnl_arr.sum() / n, 2),
    }


# ========================================
# 総当たり
# ========================================
def search_side(df: pd.DataFrame, side: str) -> pd.DataFrame:
    rows = []
    move_list = DROP_PCTS if side == "long" else RISE_PCTS
    rsi_list = RSI_LOW_LIST if side == "long" else RSI_HIGH_LIST

    total = (
        len(move_list) * len(rsi_list) * len(VOL_RATIO_LIST) * len(LOOKBACK_BARS_LIST)
        * len(RECOVERY_PCTS) * len(TP_LIST) * len(SL_LIST) * len(MAX_HOLD_LIST)
    )
    done = 0

    for move_pct in move_list:
        for rsi_th in rsi_list:
            for vol_th in VOL_RATIO_LIST:
                for lookback in LOOKBACK_BARS_LIST:
                    for recovery_pct in RECOVERY_PCTS:
                        sig = {
                            "move_pct": move_pct,
                            "rsi_th": rsi_th,
                            "vol_th": vol_th,
                            "lookback": lookback,
                            "recovery_pct": recovery_pct,
                        }
                        for tp in TP_LIST:
                            for sl in SL_LIST:
                                for max_hold in MAX_HOLD_LIST:
                                    done += 1
                                    if done % 500 == 0:
                                        print(f"[{side}] {done:,}/{total:,}")

                                    r = run_backtest(df, side, sig, tp, sl, max_hold)
                                    if r["n"] < 30:
                                        continue

                                    rows.append({
                                        "side": side,
                                        "move_pct": move_pct,
                                        "rsi_th": rsi_th,
                                        "vol_th": vol_th,
                                        "lookback": lookback,
                                        "recovery_pct": recovery_pct,
                                        "tp": tp,
                                        "sl": sl,
                                        "max_hold": max_hold,
                                        **r,
                                    })

    if not rows:
        return pd.DataFrame()

    out = pd.DataFrame(rows)
    out = out.sort_values(["pf", "pnl", "ev", "n"], ascending=[False, False, False, False]).reset_index(drop=True)
    return out


# ========================================
# メイン
# ========================================
def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    df = load_data()
    df = add_indicators(df)

    long_df = search_side(df, "long")
    short_df = search_side(df, "short")

    if not long_df.empty:
        long_df.to_csv(OUT_DIR / "jogaitsuki_gyakubari_long_top.csv", index=False, encoding="utf-8-sig")
        print("\n[LONG TOP]")
        print(long_df.head(TOP_N).to_string(index=False))
        print(f"\nLONG 有効パラメータ組合せ数: {len(long_df)}")
        print(long_df["n"].describe().to_string())

    if not short_df.empty:
        short_df.to_csv(OUT_DIR / "jogaitsuki_gyakubari_short_top.csv", index=False, encoding="utf-8-sig")
        print("\n[SHORT TOP]")
        print(short_df.head(TOP_N).to_string(index=False))
        print(f"\nSHORT 有効パラメータ組合せ数: {len(short_df)}")
        print(short_df["n"].describe().to_string())

    print(f"\n保存先: {OUT_DIR}")


if __name__ == "__main__":
    main()
