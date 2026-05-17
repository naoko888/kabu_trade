# micro_performance_summary.py
import pandas as pd
from pathlib import Path

# =========================
# パス
# =========================
LIVE_LOG_FILE = Path(r"C:\kabu_trade\logs\micro_dry_log_all.csv")
MICRO_CSV_FILE = Path(r"C:\kabu_trade\micro_5min.csv")
CPI_CSV_FILE = Path(r"C:\kabu_trade\economic_calendar.csv")
OUT_FILE = Path(r"C:\kabu_trade\logs\micro_performance_summary.csv")

# =========================
# 設定
# =========================
PERIODS = [5, 10, 20, 40, 80]
SHOW_TODAY = True

PT_TO_YEN = 10
COMMISSION_YEN = 22
COMMISSION_PT = COMMISSION_YEN / PT_TO_YEN

MICRO_TP = 240
MICRO_SL = 60
TOUCH_PCT = 0.007
MAX_HOLD = 120

MICRO_MONTHLY_DD_LIMIT = -30_0000  # 円

# ★ 実運用コードと揃える
S1_WEEKDAYS = (0, 1, 2)  # 月火水
S1_HOURS_DST = (2, 8, 15, 18, 19, 21)
S1_HOURS_WIN = (2, 8, 12, 13, 15, 18, 21, 23)
S1_EXCL_MONTHS = (3, 11)

S3_WEEKDAYS = (0, 2, 3, 4)  # 月水木金
S3_EXCL_MONTHS = (7, 11)
S3_HOURS_DST = (0, 5, 8, 12, 13, 14, 15, 19, 20, 22, 23)
S3_HOURS_WIN = (4, 5, 15, 17, 18, 19, 20, 21, 22)

# 系統④：逆張りロング
SYS4_MOVE_PCT      = 0.002
SYS4_RSI_TH        = 40
SYS4_VOL_TH        = 0.8
SYS4_LOOKBACK      = 1
SYS4_RECOVERY_PCT  = 0.002
SYS4_EXCLUDE_HOURS = {19}
SYS4_TP            = 120
SYS4_SL            = 60
SYS4_MAX_HOLD      = 6

# 系統⑤：逆張りショート
SYS5_MOVE_PCT      = 0.003
SYS5_RSI_TH        = 70
SYS5_VOL_TH        = 0.8
SYS5_LOOKBACK      = 3
SYS5_RECOVERY_PCT  = 0.002
SYS5_TP            = 120
SYS5_SL            = 60
SYS5_MAX_HOLD      = 6

# ④⑤合算DD
SYS45_DD_LIMIT_YEN = -3000

SESSION_BOUNDARIES = frozenset({2350})


def _trading_day_sort_key(dt):
    """取引日順ソートキー: 17:00未満は翌日扱いにして夜間→深夜→日中の順に並べる。"""
    if dt.hour < 17:
        return dt + pd.Timedelta(days=1)
    return dt


_DST_PERIODS = [
    (pd.Timestamp("2023-03-12"), pd.Timestamp("2023-11-05")),
    (pd.Timestamp("2024-03-10"), pd.Timestamp("2024-11-03")),
    (pd.Timestamp("2025-03-09"), pd.Timestamp("2025-11-02")),
    (pd.Timestamp("2026-03-08"), pd.Timestamp("2026-11-01")),
]


# =========================
# 共通
# =========================
def is_dst(ts: pd.Timestamp) -> bool:
    for start, end in _DST_PERIODS:
        if start <= ts <= end:
            return True
    return False


def get_trade_date(ts: pd.Timestamp):
    from datetime import timedelta

    if ts.hour >= 17:
        base_date = (ts + timedelta(days=1)).date()
    else:
        base_date = ts.date()

    wd = base_date.weekday()

    if wd == 5:
        base_date += timedelta(days=2)
    elif wd == 6:
        base_date += timedelta(days=1)

    return base_date


def load_cpi():
    if not CPI_CSV_FILE.exists():
        return pd.DataFrame(columns=["release_datetime_jst"])

    for enc in ("utf-8", "utf-8-sig", "cp932"):
        try:
            df = pd.read_csv(CPI_CSV_FILE, encoding=enc)
            if "indicator" not in df.columns or "release_datetime_jst" not in df.columns:
                continue
            df["release_datetime_jst"] = pd.to_datetime(df["release_datetime_jst"], errors="coerce")
            df = df[df["indicator"] == "米CPI"].dropna(subset=["release_datetime_jst"]).copy()
            return df.reset_index(drop=True)
        except Exception:
            continue

    return pd.DataFrame(columns=["release_datetime_jst"])


def is_cpi_window(ts: pd.Timestamp, cpi_df: pd.DataFrame, before_min=30, after_min=60) -> bool:
    if cpi_df.empty:
        return False
    for rel in cpi_df["release_datetime_jst"]:
        if rel - pd.Timedelta(minutes=before_min) <= ts <= rel + pd.Timedelta(minutes=after_min):
            return True
    return False


def pf_str(v):
    return f"{v:.3f}" if v != float("inf") else "inf"


# =========================
# 実運用ログ
# =========================
def load_live_log():
    if not LIVE_LOG_FILE.exists():
        print("実運用ログが見つかりません")
        return None

    df = pd.read_csv(LIVE_LOG_FILE)

    need_cols = ["system", "side", "entry_time", "exit_time", "pnl"]
    missing = [c for c in need_cols if c not in df.columns]
    if missing:
        print(f"実運用ログの列不足: {missing}")
        return None

    df["entry_time"] = pd.to_datetime(df["entry_time"], errors="coerce")
    df["exit_time"] = pd.to_datetime(df["exit_time"], errors="coerce")
    df["pnl"] = pd.to_numeric(df["pnl"], errors="coerce")

    df = df.dropna(subset=["entry_time", "exit_time", "pnl"]).copy()
    df = df[df["system"].astype(str).isin(["①", "③", "④", "⑤"])].copy()

    # 手数料込み
    df["pnl_pt"] = df["pnl"] - COMMISSION_PT
    df["pnl_yen"] = (df["pnl_pt"] * PT_TO_YEN).round(0).astype(int)

    # ★ 比較は entry_time ベースの取引日でそろえる
    df["trade_date"] = df["entry_time"].apply(get_trade_date)

    return df.sort_values("entry_time").reset_index(drop=True)

DATA_DIR = Path(r"C:\kabu_trade\data")
EXCEL_FILES = [
    "N225microf_2023.xlsx",
    "N225microf_2024.xlsx",
    "N225microf_2025.xlsx",
    "N225microf_2026.xlsx",
]

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
    df = df.dropna(subset=["datetime", "open", "high", "low", "close"]).copy()
    df = df[["datetime", "open", "high", "low", "close", "volume"]].copy()
    sort_keys = df["datetime"].map(_trading_day_sort_key)
    return df.iloc[sort_keys.argsort(kind="stable")].reset_index(drop=True)

# =========================
# micro_5min.csv 読み込み
# =========================
def load_micro_csv():
    if not MICRO_CSV_FILE.exists():
        print("micro_5min.csv が見つかりません")
        return None

    df = pd.read_csv(MICRO_CSV_FILE)
    need_cols = ["datetime", "open", "high", "low", "close", "volume"]
    missing = [c for c in need_cols if c not in df.columns]
    if missing:
        print(f"micro_5min.csv の列不足: {missing}")
        return None

    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df.dropna(subset=["datetime", "open", "high", "low", "close"]).copy()
    df = df.drop_duplicates(subset=["datetime"], keep="last")
    sort_keys = df["datetime"].map(_trading_day_sort_key)
    df = df.iloc[sort_keys.argsort(kind="stable")].reset_index(drop=True)
    return df

def add_indicators(df: pd.DataFrame):
    df = df.copy()

    df["ma9"] = df["close"].rolling(9).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()

    ema_fast = df["close"].ewm(span=12, adjust=False).mean()
    ema_slow = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"] = ema_fast - ema_slow
    df["macd_sig"] = df["macd"].ewm(span=9, adjust=False).mean()

    # 追加
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma20"]

    delta = df["close"].diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    avg_up = up.rolling(14).mean()
    avg_down = down.rolling(14).mean()
    rs = avg_up / avg_down.replace(0, pd.NA)
    df["rsi14"] = 100 - (100 / (1 + rs))

    return df


# =========================
# BT実行
# =========================
def exec_trade_sys(df: pd.DataFrame, entry_idx: int, side: str, tp: int, sl: int, max_hold: int,
                   force_session_close: bool = True, entry_weekday: int = -1):
    ep = float(df.iloc[entry_idx]["open"])

    for j in range(entry_idx, min(entry_idx + max_hold, len(df))):
        row = df.iloc[j]
        bhi = float(row["high"])
        blo = float(row["low"])
        dt = pd.Timestamp(row["datetime"])
        hhmm = dt.hour * 100 + dt.minute

        if side == "long":
            if bhi >= ep + tp:
                return tp - COMMISSION_PT, dt, "TP"
            if blo <= ep - sl:
                return -sl - COMMISSION_PT, dt, "SL"
        else:
            if blo <= ep - tp:
                return tp - COMMISSION_PT, dt, "TP"
            if bhi >= ep + sl:
                return -sl - COMMISSION_PT, dt, "SL"

        # 23:50強制決済（force_session_close=Trueの場合のみ）
        if force_session_close and hhmm in SESSION_BOUNDARIES:
            close_price = float(row["close"])
            pnl = (close_price - ep) if side == "long" else (ep - close_price)
            return pnl - COMMISSION_PT, dt, "SESSION"

        # 土曜に入ったら強制決済（金曜エントリーのみ）
        if entry_weekday == 4 and dt.weekday() == 5:
            close_price = float(row["close"])
            pnl = (close_price - ep) if side == "long" else (ep - close_price)
            return pnl - COMMISSION_PT, dt, "SAT_CLOSE"

        # 月曜05:55強制決済
        if dt.weekday() == 0 and hhmm == 555:
            close_price = float(row["close"])
            pnl = (close_price - ep) if side == "long" else (ep - close_price)
            return pnl - COMMISSION_PT, dt, "MON_CLOSE"

    last_idx = min(entry_idx + max_hold - 1, len(df) - 1)
    last_row = df.iloc[last_idx]
    dt = pd.Timestamp(last_row["datetime"])
    close_price = float(last_row["close"])
    pnl = (close_price - ep) if side == "long" else (ep - close_price)
    return pnl - COMMISSION_PT, dt, "TIME"

def build_bt_trades(df: pd.DataFrame, cpi_df: pd.DataFrame):
    trades = []

    for i in range(3, len(df)):
        sig_i = i - 1   # 判定に使う確定足
        ent_i = i       # エントリー足

        ent_dt = pd.Timestamp(df.iloc[ent_i]["datetime"])
        if ent_dt.hour == 17 and ent_dt.minute == 0:
            continue

        row = df.iloc[sig_i]
        row_p = df.iloc[sig_i - 1]
        row_p2 = df.iloc[sig_i - 2]

        need = ["ma9", "ma10", "ma20", "macd", "macd_sig", "rsi14", "vol_ratio"]
        if any(pd.isna(row[c]) for c in need):
            continue
        if any(pd.isna(row_p[c]) for c in need):
            continue
        if any(pd.isna(row_p2[c]) for c in need):
            continue

        dt = pd.Timestamp(df.iloc[sig_i]["datetime"])
        wd = dt.weekday()
        hr    = (dt + pd.Timedelta(minutes=5)).hour  # 系統①用（bar END hour）
        hr_s3 = dt.hour                              # 系統③用（bar START hour）
        month = dt.month

        m9 = float(row["ma9"])
        m10 = float(row["ma10"])
        m20 = float(row["ma20"])
        hi = float(row["high"])
        lo = float(row["low"])
        c1 = float(row_p["close"])
        c2 = float(row_p2["close"])
        m9p = float(row_p["ma9"])
        m10p = float(row_p["ma10"])
        m9p2 = float(row_p2["ma9"])
        m10p2 = float(row_p2["ma10"])
        macd = float(row["macd"])
        macd_sig = float(row["macd_sig"])

        # 系統①
        above_ma = (c2 > m9p2 and c2 > m10p2 and c1 > m9p and c1 > m10p)
        touch_lo = (abs(lo - m9) / m9 <= TOUCH_PCT) or (abs(lo - m10) / m10 <= TOUCH_PCT)
        gc = macd > macd_sig

        s1_hours = S1_HOURS_DST if is_dst(dt) else S1_HOURS_WIN
        if (
            above_ma
            and touch_lo
            and gc
            and wd in S1_WEEKDAYS
            and hr in s1_hours
            and month not in S1_EXCL_MONTHS
        ):
            pnl_pt, exit_time, reason = exec_trade_sys(df, ent_i, "long", MICRO_TP, MICRO_SL, MAX_HOLD)
            entry_time = pd.Timestamp(df.iloc[ent_i]["datetime"])
            trades.append({
                "system": "①",
                "side": "long",
                "entry_time": entry_time,
                "exit_time": exit_time,
                "pnl_pt": round(pnl_pt, 4),
                "pnl_yen": int(round(pnl_pt * PT_TO_YEN, 0)),
                "reason": reason,
                "trade_date": entry_time.date(),
            })

        # 系統③
        below_ma = m9 < m20
        touch_hi = abs(hi - m9) / m9 <= TOUCH_PCT
        dc = macd < macd_sig

        if (
            below_ma
            and touch_hi
            and dc
            and wd in S3_WEEKDAYS
            and month not in S3_EXCL_MONTHS
        ):
            s3_hours = S3_HOURS_DST if is_dst(dt) else S3_HOURS_WIN

            if hr_s3 in s3_hours and not is_cpi_window(dt, cpi_df) and not (wd == 0 and hr_s3 == 5):
                pnl_pt, exit_time, reason = exec_trade_sys(
                df, ent_i, "short", MICRO_TP, MICRO_SL,
                max_hold=50,
                force_session_close=False,
                entry_weekday=wd,
                )
                entry_time = pd.Timestamp(df.iloc[ent_i]["datetime"])
                trades.append({
                    "system": "③",
                    "side": "short",
                    "entry_time": entry_time,
                    "exit_time": exit_time,
                    "pnl_pt": round(pnl_pt, 4),
                    "pnl_yen": int(round(pnl_pt * PT_TO_YEN, 0)),
                    "reason": reason,
                    "trade_date": entry_time.date(),
                })

        # 系統④（逆張りロング）
        move_pct_4 = (row["close"] - row_p["close"]) / row_p["close"]
        recovery_4 = (row["close"] - row["low"]) / row["close"] if row["close"] != 0 else 0
        hr_s4 = dt.hour  # bar START hour

        if (
            hr_s4 not in SYS4_EXCLUDE_HOURS
            and not pd.isna(row["rsi14"])
            and not pd.isna(row["vol_ratio"])
            and move_pct_4 <= -SYS4_MOVE_PCT
            and row["rsi14"] <= SYS4_RSI_TH
            and row["vol_ratio"] >= SYS4_VOL_TH
            and recovery_4 >= SYS4_RECOVERY_PCT
        ):
            pnl_pt, exit_time, reason = exec_trade_sys(df, ent_i, "long", SYS4_TP, SYS4_SL, SYS4_MAX_HOLD)
            entry_time = pd.Timestamp(df.iloc[ent_i]["datetime"])
            trades.append({
                "system": "④",
                "side": "long",
                "entry_time": entry_time,
                "exit_time": exit_time,
                "pnl_pt": round(pnl_pt, 4),
                "pnl_yen": int(round(pnl_pt * PT_TO_YEN, 0)),
                "reason": reason,
                "trade_date": entry_time.date(),
            })

        # 系統⑤（逆張りショート）
        prev5 = df.iloc[sig_i - SYS5_LOOKBACK]
        move_pct_5 = (row["close"] - prev5["close"]) / prev5["close"]
        recovery_5 = (row["high"] - row["close"]) / row["close"] if row["close"] != 0 else 0

        if (
            not pd.isna(row["rsi14"])
            and not pd.isna(row["vol_ratio"])
            and move_pct_5 >= SYS5_MOVE_PCT
            and row["rsi14"] >= SYS5_RSI_TH
            and row["vol_ratio"] >= SYS5_VOL_TH
            and recovery_5 >= SYS5_RECOVERY_PCT
        ):
            pnl_pt, exit_time, reason = exec_trade_sys(df, ent_i, "short", SYS5_TP, SYS5_SL, SYS5_MAX_HOLD)
            entry_time = pd.Timestamp(df.iloc[ent_i]["datetime"])
            trades.append({
                "system": "⑤",
                "side": "short",
                "entry_time": entry_time,
                "exit_time": exit_time,
                "pnl_pt": round(pnl_pt, 4),
                "pnl_yen": int(round(pnl_pt * PT_TO_YEN, 0)),
                "reason": reason,
                "trade_date": entry_time.date(),
            })

    if not trades:
        return pd.DataFrame(columns=["system", "side", "entry_time", "exit_time", "pnl_pt", "pnl_yen", "reason", "trade_date"])

    bt = pd.DataFrame(trades).sort_values("entry_time").reset_index(drop=True)
    return apply_monthly_dd(bt)


def apply_monthly_dd(bt: pd.DataFrame):
    if bt.empty:
        return bt

    sorted_bt = bt.sort_values("entry_time").reset_index(drop=True).copy()

    keep = []
    month_pnl_13 = {}   # 系統①③合算
    month_pnl_45 = {}   # 系統④⑤合算
    stopped_13 = set()
    stopped_45 = set()

    for _, row in sorted_bt.iterrows():
        ym = (row["trade_date"].year, row["trade_date"].month)
        sys = row["system"]

        # 月次初期化
        if ym not in month_pnl_13:
            month_pnl_13[ym] = 0.0
        if ym not in month_pnl_45:
            month_pnl_45[ym] = 0.0

        # 系統①③
        if sys in ("①", "③"):
            if ym in stopped_13:
                keep.append(False)
                continue
            keep.append(True)
            month_pnl_13[ym] += float(row["pnl_yen"])
            if month_pnl_13[ym] <= MICRO_MONTHLY_DD_LIMIT:
                stopped_13.add(ym)

        # 系統④⑤
        elif sys in ("④", "⑤"):
            if ym in stopped_45:
                keep.append(False)
                continue
            keep.append(True)
            month_pnl_45[ym] += float(row["pnl_yen"])
            if month_pnl_45[ym] <= SYS45_DD_LIMIT_YEN:
                stopped_45.add(ym)

        else:
            keep.append(True)

    sorted_bt["keep"] = keep
    sorted_bt = sorted_bt[sorted_bt["keep"]].drop(columns=["keep"]).reset_index(drop=True)
    return sorted_bt

# =========================
# 集計
# =========================
def calc_metrics(df: pd.DataFrame):
    if df is None or df.empty:
        return {
            "PF": 0.0,
            "勝率": 0.0,
            "Long①": 0,
            "Long④": 0,
            "Short③": 0,
            "Short⑤": 0,
            "件数": 0,
            "期待値pt": 0.0,
            "損益pt": 0.0,
            "損益円": 0,
        }

    pnl = df["pnl_pt"].astype(float)
    wins = pnl[pnl > 0].sum()
    loss = abs(pnl[pnl < 0].sum())

    n = len(df)
    win_rate = (pnl > 0).mean() * 100
    pf = wins / loss if loss > 0 else 0.0
    ev = pnl.sum() / n

    return {
        "PF": round(float(pf), 3),
        "勝率": round(float(win_rate), 1),

        # ★ここ追加
        "Long①": int(((df["system"] == "①") & (df["side"] == "long")).sum()),
        "Long④": int(((df["system"] == "④") & (df["side"] == "long")).sum()),
        "Short③": int(((df["system"] == "③") & (df["side"] == "short")).sum()),
        "Short⑤": int(((df["system"] == "⑤") & (df["side"] == "short")).sum()),

        "件数": int(n),
        "期待値pt": round(float(ev), 2),
        "損益pt": round(float(pnl.sum()), 1),
        "損益円": int(round(df["pnl_yen"].sum(), 0)),
    }


def make_diff_row(live_m: dict, bt_m: dict):
    return {
        "PF": round(live_m["PF"] - bt_m["PF"], 3),
        "勝率": round(live_m["勝率"] - bt_m["勝率"], 1),

        # ★ここ修正
        "Long①": live_m["Long①"] - bt_m["Long①"],
        "Long④": live_m["Long④"] - bt_m["Long④"],
        "Short③": live_m["Short③"] - bt_m["Short③"],
        "Short⑤": live_m["Short⑤"] - bt_m["Short⑤"],

        "件数": live_m["件数"] - bt_m["件数"],
        "期待値pt": round(live_m["期待値pt"] - bt_m["期待値pt"], 2),
        "損益pt": round(live_m["損益pt"] - bt_m["損益pt"], 1),
        "損益円": live_m["損益円"] - bt_m["損益円"],
    }


def make_signal_key(df):
    if df is None or df.empty:
        return set()
    tmp = df.copy()
    tmp["entry_time_5m"] = tmp["entry_time"].dt.floor("5min")
    time_str = tmp["entry_time_5m"].dt.strftime("%H:%M")
    key = tmp["trade_date"].astype(str) + " " + time_str
    return set(
        zip(
            key,
            tmp["system"].astype(str),
            tmp["side"].astype(str),
        )
    )


def print_signal_list(df: pd.DataFrame):
    if df is None or df.empty:
        print("なし")
        return
    for _, r in df.sort_values("entry_time").iterrows():
        t = f"{r['trade_date'].strftime('%m/%d')} {r['entry_time'].strftime('%H:%M')}"
        print(f"{t}  {r['system']}  {r['side']}")

def print_bar_count_diff(title: str, live_df: pd.DataFrame, bt_df: pd.DataFrame):
    print(f"\n===== {title}の同一バー件数比較 =====")

    def make_bar_count(df: pd.DataFrame):
        if df is None or df.empty:
            return pd.DataFrame(columns=["bar_time", "system", "side", "count"])

        tmp = df.copy()
        tmp["bar_time"] = pd.to_datetime(
            tmp["trade_date"].astype(str) + " " + tmp["entry_time"].dt.floor("5min").dt.strftime("%H:%M")
        )

        g = (
            tmp.groupby(["bar_time", "system", "side"])
               .size()
               .reset_index(name="count")
        )
        return g

    live_cnt = make_bar_count(live_df)
    bt_cnt = make_bar_count(bt_df)

    merged = pd.merge(
        live_cnt,
        bt_cnt,
        on=["bar_time", "system", "side"],
        how="outer",
        suffixes=("_live", "_bt"),
    ).fillna(0).infer_objects(copy=False)

    merged["count_live"] = merged["count_live"].astype(int)
    merged["count_bt"] = merged["count_bt"].astype(int)
    merged["diff"] = merged["count_live"] - merged["count_bt"]

    same = merged[merged["diff"] == 0].copy()
    diff = merged[merged["diff"] != 0].copy()

    print(f"一致バー: {len(same)}件")
    print(f"差分バー: {len(diff)}件")

    if diff.empty:
        print("差分なし")
        return

    print("\n[差分あり]")
    diff = diff.sort_values(["bar_time", "system", "side"])
    for _, r in diff.iterrows():
        bar_ts = pd.Timestamp(r["bar_time"])
        t = bar_ts.strftime('%m/%d %H:%M')
        print(
            f"  {t}  {r['system']}  {r['side']}  "
            f"実運用={r['count_live']}件  BT={r['count_bt']}件"
        )
def print_match_result(title: str, live_df: pd.DataFrame, bt_df: pd.DataFrame):
    print(f"\n===== {title}のシグナル一致判定 =====")

    live_keys = make_signal_key(live_df)
    bt_keys = make_signal_key(bt_df)

    matched = sorted(live_keys & bt_keys)
    live_only = sorted(live_keys - bt_keys)
    bt_only = sorted(bt_keys - live_keys)

    print(f"一致: {len(matched)}件")
    for t, sys, side in matched:
        print(f"  {pd.Timestamp(t).strftime('%m/%d %H:%M')}  {sys}  {side}")

    print(f"\n実運用のみ: {len(live_only)}件")
    for t, sys, side in live_only:
        print(f"  {pd.Timestamp(t).strftime('%m/%d %H:%M')}  {sys}  {side}")

    print(f"\nBTのみ: {len(bt_only)}件")
    for t, sys, side in bt_only:
        print(f"  {pd.Timestamp(t).strftime('%m/%d %H:%M')}  {sys}  {side}")


def print_metric_row(period_label: str, label: str, m: dict, bar_count="-"):
    print(
        f"{period_label:>6}"
        f"{label:>10}"
        f"{str(bar_count):>12}"
        f"{m['PF']:>10.3f}"
        f"{str(m['勝率'])+'%':>10}"
        f"{m['Long①']:>8}"
        f"{m['Long④']:>8}"
        f"{m['Short③']:>8}"
        f"{m['Short⑤']:>8}"
        f"{m['件数']:>8}"
        f"{m['期待値pt']:>12.2f}"
        f"{m['損益pt']:>12.1f}"
        f"{m['損益円']:>14,}"
    )


# =========================
# XLSX全期間ロード（BT用）
# =========================
def load_xlsx_for_bt() -> pd.DataFrame:
    dfs = []
    for fname in EXCEL_FILES:
        path = DATA_DIR / fname
        if path.exists():
            dfs.append(read_excel(path))
    if not dfs:
        return pd.DataFrame()
    return (pd.concat(dfs, ignore_index=True)
            .drop_duplicates(subset=["datetime"], keep="last")
            .reset_index(drop=True))


# =========================
# メイン
# =========================
def main():
    now = pd.Timestamp.now()
    print(f"★★★ 実行基準時刻: {now.strftime('%Y-%m-%d %H:%M:%S')} ★★★")

    live_df = load_live_log()
    csv_df  = load_micro_csv()
    cpi_df  = load_cpi()
    xlsx_df = load_xlsx_for_bt()

    if live_df is None or live_df.empty:
        print("実運用ログが空です")
        return
    if csv_df is None or csv_df.empty:
        print("データファイルが空です")
        return

    csv_bar_count  = len(csv_df)
    xlsx_bar_count = len(xlsx_df) if not xlsx_df.empty else 0

    # ===== 未来データカット（最重要） =====

    csv_df = add_indicators(csv_df)
    bt_df  = build_bt_trades(csv_df, cpi_df)

    if not xlsx_df.empty:
        xlsx_df    = add_indicators(xlsx_df)
        bt_xlsx_df = build_bt_trades(xlsx_df, cpi_df)
    else:
        bt_xlsx_df = pd.DataFrame()

    if bt_df is None or bt_df.empty:
        print("BTトレードが0件です")
        return

    # ===== 実運用 / BT も now までに制限 =====
    live_df = live_df[live_df["entry_time"] <= now].copy()
    bt_df   = bt_df[bt_df["entry_time"] <= now + pd.Timedelta(days=3)].copy()
    if not bt_xlsx_df.empty:
        bt_xlsx_df = bt_xlsx_df[bt_xlsx_df["entry_time"] <= now + pd.Timedelta(days=3)].copy()

    # ===== 取引日 =====
    today_trade_date = get_trade_date(now)
    yesterday_trade_date = today_trade_date - pd.Timedelta(days=1)

    rows = []

    print("\n===== 実運用 vs BT 比較（手数料込み） =====\n")
    print(
    f"{'期間':>6}"
    f"{'区分':>10}"
    f"{'使用バー数':>12}"
    f"{'PF':>10}"
    f"{'勝率':>10}"
    f"{'Long①':>8}"
    f"{'Long④':>8}"
    f"{'Short③':>8}"
    f"{'Short⑤':>8}"
    f"{'件数':>8}"
    f"{'期待値pt':>12}"
    f"{'損益pt':>12}"
    f"{'損益円':>14}"
)

    # =========================
    # 本日
    # =========================
    if SHOW_TODAY:
        live_today     = live_df[live_df["trade_date"].astype(str) == str(today_trade_date)].copy()
        bt_today       = bt_df[bt_df["trade_date"].astype(str) == str(today_trade_date)].copy()
        bt_xlsx_today  = bt_xlsx_df[bt_xlsx_df["trade_date"].astype(str) == str(today_trade_date)].copy() if not bt_xlsx_df.empty else pd.DataFrame()

        live_m    = calc_metrics(live_today)
        bt_m      = calc_metrics(bt_today)
        bt_xlsx_m = calc_metrics(bt_xlsx_today)
        diff_m    = make_diff_row(live_m, bt_m)

        for label, m, bc in [
            ("実運用",   live_m,    "-"),
            ("BT(CSV)",  bt_m,      csv_bar_count),
            ("BT(XLSX)", bt_xlsx_m, xlsx_bar_count),
            ("差分",     diff_m,    "-"),
        ]:
            print_metric_row("本日", label, m, bar_count=bc)
            row = {"期間": "本日", "区分": label}
            row.update(m)
            rows.append(row)

        print("-" * 100)

        # =========================
        # 昨日
        # =========================
        live_yesterday    = live_df[live_df["trade_date"].astype(str) == str(yesterday_trade_date)].copy()
        bt_yesterday      = bt_df[bt_df["trade_date"].astype(str) == str(yesterday_trade_date)].copy()
        bt_xlsx_yesterday = bt_xlsx_df[bt_xlsx_df["trade_date"].astype(str) == str(yesterday_trade_date)].copy() if not bt_xlsx_df.empty else pd.DataFrame()

        live_m    = calc_metrics(live_yesterday)
        bt_m      = calc_metrics(bt_yesterday)
        bt_xlsx_m = calc_metrics(bt_xlsx_yesterday)
        diff_m    = make_diff_row(live_m, bt_m)

        for label, m, bc in [
            ("実運用",   live_m,    "-"),
            ("BT(CSV)",  bt_m,      csv_bar_count),
            ("BT(XLSX)", bt_xlsx_m, xlsx_bar_count),
            ("差分",     diff_m,    "-"),
        ]:
            print_metric_row("昨日", label, m, bar_count=bc)
            row = {"期間": "昨日", "区分": label}
            row.update(m)
            rows.append(row)

        print("-" * 100)


        # =========================
        # シグナル一致判定（2セット）
        # =========================
        print_match_result("本日 実運用 vs BT(CSV)",  live_today,     bt_today)
        print_match_result("本日 実運用 vs BT(XLSX)", live_today,     bt_xlsx_today)
        print_match_result("昨日 実運用 vs BT(CSV)",  live_yesterday, bt_yesterday)
        print_match_result("昨日 実運用 vs BT(XLSX)", live_yesterday, bt_xlsx_yesterday)

        # =========================
        # 同一バー件数比較（2セット）
        # =========================
        print_bar_count_diff("本日 実運用 vs BT(CSV)",  live_today,     bt_today)
        print_bar_count_diff("本日 実運用 vs BT(XLSX)", live_today,     bt_xlsx_today)
        print_bar_count_diff("昨日 実運用 vs BT(CSV)",  live_yesterday, bt_yesterday)
        print_bar_count_diff("昨日 実運用 vs BT(XLSX)", live_yesterday, bt_xlsx_yesterday)

    # =========================
    # 期間比較（Now基準）
    # =========================
    for d in PERIODS:
        start_dt = now - pd.Timedelta(days=d)

        live_sub = live_df[
            (live_df["entry_time"] >= start_dt) &
            (live_df["entry_time"] <= now)
        ].copy()

        bt_sub = bt_df[
            (bt_df["entry_time"] >= start_dt) &
            (bt_df["entry_time"] <= now)
        ].copy()

        bt_xlsx_sub = bt_xlsx_df[
            (bt_xlsx_df["entry_time"] >= start_dt) &
            (bt_xlsx_df["entry_time"] <= now)
        ].copy() if not bt_xlsx_df.empty else pd.DataFrame()

        live_m    = calc_metrics(live_sub)
        bt_m      = calc_metrics(bt_sub)
        bt_xlsx_m = calc_metrics(bt_xlsx_sub)
        diff_m    = make_diff_row(live_m, bt_m)

        for label, m, bc in [
            ("実運用",   live_m,    "-"),
            ("BT(CSV)",  bt_m,      csv_bar_count),
            ("BT(XLSX)", bt_xlsx_m, xlsx_bar_count),
            ("差分",     diff_m,    "-"),
        ]:
            print_metric_row(f"{d}日", label, m, bar_count=bc)
            row = {"期間": f"{d}日", "区分": label}
            row.update(m)
            rows.append(row)

        print("-" * 100)

    out_df = pd.DataFrame(rows)
    OUT_FILE.parent.mkdir(exist_ok=True)

    # =========================
    # 年×月クロス集計
    # =========================
    def print_ym_cross(title: str, df: pd.DataFrame):
        if df is None or df.empty:
            print(f"\n{title}: データなし")
            return
        print(f"\n===== {title} 年×月クロス集計（損益円）=====")
        months = list(range(1, 13))
        df = df.copy()
        df["year"] = df["entry_time"].dt.year
        df["month"] = df["entry_time"].dt.month
        print("  年    " + "".join(f"  {m:>4}月" for m in months) + "    合計")
        print("  " + "-" * 100)
        for yr in sorted(df["year"].unique()):
            vals = []
            total = 0
            for m in months:
                v = int(df[(df["year"] == yr) & (df["month"] == m)]["pnl_yen"].sum())
                vals.append(f"{v:>+7,}")
                total += v
            print(f"  {yr}  " + "  ".join(vals) + f"  {total:>+9,}")

    # 実運用デモ（先に表示）
    print_ym_cross("実運用デモ", live_df)

    # BT CSV（月次DD適用後）
    print_ym_cross("BT CSV（月次DD適用後）", bt_df)

    # BT XLSX（月次DD適用後）
    print_ym_cross("BT XLSX（月次DD適用後）", bt_xlsx_df)

    out_df.to_csv(OUT_FILE, index=False, encoding="utf-8-sig")
    print(f"\n[保存] {OUT_FILE}")

if __name__ == "__main__":
    main()