"""
ZAIHOU_signals.py
N225マイクロ先物 系統①③④⑤ 共通シグナルモジュール

BT (ZAIHOU_bt.py) と 実運用 (ZAIHOU.py) の両方から import。
グローバル状態を持たない純粋関数として設計。

時間基準:
  系統① : bar END hour    hr = (dt.hour*60 + dt.minute + 5) // 60 % 24
  系統③ : bar START hour  hr = dt.hour
  系統④⑤: bar END hour    hr = (dt.hour*60 + dt.minute - 5) // 60 % 24  (BT45と同式)

月除外の補正（auto_trade.py の 5月漏れバグを修正）:
  ①: S1_EXCL_MONTHS = (3, 5, 11)  ← BT確認済み / auto_trade.py は (3,11) のみ
  ③: S3_EXCL_MONTHS = (5, 7, 11)  ← BT確認済み / auto_trade.py は (7,11) のみ

CPI除外対象: ③⑤（④は対象外）
④⑤ 曜日フィルターなし（BTで全曜日 0–4 を確認済み）
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd


# ==========================================================================
# 共通定数
# ==========================================================================
TOUCH_PCT     = 0.007   # MAタッチ判定閾値（±0.7%）
COMMISSION_PT = 2.2     # 往復手数料（pt）
PT_TO_YEN     = 10      # 1pt = ¥10

# ==========================================================================
# 系統① : 順張り long
# ==========================================================================
S1_EXCL_MONTHS = frozenset((3, 5, 11))
S1_WEEKDAYS    = frozenset((0, 1, 2))        # 月火水
S1_HOURS_DST   = frozenset((2, 8, 18, 19, 21))
S1_HOURS_WIN   = frozenset((2, 8, 12, 18, 21, 23))
S1_TP          = 240
S1_SL          = 60
S1_MAX_HOLD    = 120    # bars

# ==========================================================================
# 系統③ : 順張り short
# ==========================================================================
S3_EXCL_MONTHS = frozenset((5, 7, 11))
S3_WEEKDAYS    = frozenset((0, 2, 3, 4))     # 月水木金
S3_HOURS_DST   = frozenset((0, 5, 8, 19, 20, 23))
S3_HOURS_WIN   = frozenset((4, 5, 17, 18, 19, 20, 21))
S3_TP          = 240
S3_SL          = 60
S3_MAX_HOLD    = 50     # bars

# ==========================================================================
# 系統④ : 逆張り long
# ==========================================================================
S4_MOVE_PCT    = 0.0001
S4_RSI_TH      = 40
S4_LOOKBACK    = 1
S4_HOURS_DST   = frozenset((14, 15, 17, 23))
S4_HOURS_WIN   = frozenset((14, 15, 17, 23))
S4_EXCL_MONTHS = frozenset((7,))
S4_TP          = 400
S4_SL          = 80
S4_MAX_HOLD    = 8

# ==========================================================================
# 系統⑤ : 逆張り short
# ==========================================================================
S5_MOVE_PCT    = 0.0006
S5_RSI_TH      = 40
S5_LOOKBACK    = 4
S5_HOURS_DST   = frozenset((14, 15, 22))
S5_HOURS_WIN   = frozenset((8, 12, 14, 15, 22))
S5_EXCL_MONTHS = frozenset((1, 7))
S5_TP          = 300
S5_SL          = 80
S5_MAX_HOLD    = 6

# ==========================================================================
# DD管理（全系統合算 ①③④⑤）
# ==========================================================================
DD_LIMIT_YEN = -300_000   # 月次DD上限（円）= auto_trade.py ALL_DD_LIMIT_YEN と同値

# ==========================================================================
# 米国サマータイム期間
# ==========================================================================
_DST_PERIODS: list[tuple[pd.Timestamp, pd.Timestamp]] = [
    (pd.Timestamp("2023-03-12"), pd.Timestamp("2023-11-05")),
    (pd.Timestamp("2024-03-10"), pd.Timestamp("2024-11-03")),
    (pd.Timestamp("2025-03-09"), pd.Timestamp("2025-11-02")),
    (pd.Timestamp("2026-03-08"), pd.Timestamp("2026-11-01")),
]


def is_dst(dt: datetime) -> bool:
    """dt が米国サマータイム期間内か（JST naive datetime を渡すこと）"""
    ts = pd.Timestamp(dt)
    for start, end in _DST_PERIODS:
        if start <= ts <= end:
            return True
    return False


# ==========================================================================
# 取引日補正・ソート（CSV保存・ウォームアップ用）
# ==========================================================================
def _adjust_trading_day(dt: datetime) -> datetime:
    """WebSocketの生タイムスタンプを取引日補正（17:00以降→翌営業日付）"""
    if dt.hour >= 17:
        base_date = (dt + timedelta(days=1)).date()
    else:
        base_date = dt.date()
    wd = base_date.weekday()
    if wd == 5:
        base_date += timedelta(days=2)
    elif wd == 6:
        base_date += timedelta(days=1)
    return dt.replace(year=base_date.year, month=base_date.month, day=base_date.day)


def _trading_day_sort_key(dt: datetime) -> datetime:
    """取引日順ソートキー: 17:00未満は翌日扱いにして時系列を正しく並べる"""
    if dt.hour < 17:
        return dt + pd.Timedelta(days=1)
    return dt


# ==========================================================================
# CPI フィルター
# ==========================================================================
def load_cpi_events(
    csv_path: str = r"C:\kabu_trade\economic_calendar.csv",
) -> pd.DataFrame:
    """economic_calendar.csv から米CPI 発表日時を読み込む。
    失敗時は空 DataFrame を返して続行（CPI除外無効）。
    """
    try:
        df = pd.read_csv(csv_path)
        df["release_datetime_jst"] = pd.to_datetime(df["release_datetime_jst"])
        result = df[df["indicator"] == "米CPI"].reset_index(drop=True)
        print(f"[OK] CPIカレンダー読み込み: {len(result)}件")
        return result
    except Exception as e:
        print(f"[WARN] CPIカレンダー読み込み失敗: {e} → CPI除外無効で続行")
        return pd.DataFrame(columns=["indicator", "release_datetime_jst"])


def is_cpi_window(
    dt: datetime,
    cpi_df: pd.DataFrame,
    before_min: int = 30,
    after_min: int = 60,
) -> bool:
    """dt が CPI発表の30分前〜60分後ウィンドウ内か"""
    ts = pd.Timestamp(dt)
    for _, row in cpi_df.iterrows():
        release = row["release_datetime_jst"]
        if (release - timedelta(minutes=before_min)) <= ts <= (release + timedelta(minutes=after_min)):
            return True
    return False


# ==========================================================================
# 指標計算
# ==========================================================================
def add_micro_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """系統①③④⑤⑥ に必要な全指標を計算。
    auto_trade.py の add_micro_indicators と完全一致。
    """
    df = df.copy()

    df["ma9"]  = df["close"].rolling(9).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()

    ema_fast       = df["close"].ewm(span=12, adjust=False).mean()
    ema_slow       = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"]     = ema_fast - ema_slow
    df["macd_sig"] = df["macd"].ewm(span=9, adjust=False).mean()

    df["vol_ma20"]  = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma20"]

    delta   = df["close"].diff()
    avg_up  = delta.clip(lower=0).rolling(14).mean()
    avg_dn  = (-delta.clip(upper=0)).rolling(14).mean()
    rs      = avg_up / avg_dn.replace(0, np.nan)
    df["rsi14"] = 100 - (100 / (1 + rs))

    prev_c = df["close"].shift(1)
    tr     = pd.concat(
        [df["high"] - df["low"],
         (df["high"] - prev_c).abs(),
         (df["low"]  - prev_c).abs()],
        axis=1,
    ).max(axis=1)
    df["atr14"] = tr.rolling(14).mean()

    bb_mid              = df["close"].rolling(20).mean()
    bb_std              = df["close"].rolling(20).std()
    df["bb_width"]      = (bb_std * 4) / bb_mid
    df["bb_width_ma20"] = df["bb_width"].rolling(20).mean()

    return df


# ==========================================================================
# 系統①③ シグナル判定
# ==========================================================================
def check_s1_s3(df: pd.DataFrame, cpi_df: pd.DataFrame) -> list[str]:
    """系統①（順張り long）と系統③（順張り short）のシグナル判定。

    戻り値: 発火した系統名リスト  例: ["①"], ["③"], ["①","③"], []

    時間基準:
      ① bar END hour  : hr = (dt.hour*60 + dt.minute + 5) // 60 % 24
      ③ bar START hour: hr = dt.hour
    """
    if df is None or len(df) < 35:
        return []

    row    = df.iloc[-1]
    row_p  = df.iloc[-2]
    row_p2 = df.iloc[-3]

    for col in ("ma9", "ma10", "ma20", "macd", "macd_sig", "vol_ratio",
                "bb_width", "bb_width_ma20"):
        if pd.isna(row[col]) or pd.isna(row_p[col]) or pd.isna(row_p2[col]):
            return []

    ma9    = row["ma9"];    ma10   = row["ma10"]
    ma9p   = row_p["ma9"];  ma10p  = row_p["ma10"]
    ma9p2  = row_p2["ma9"]; ma10p2 = row_p2["ma10"]
    hi     = row["high"]
    lo     = row["low"]
    c1     = row_p["close"]
    c2     = row_p2["close"]

    dt     = pd.to_datetime(row["datetime"])
    wd     = dt.weekday()
    hr     = (dt.hour * 60 + dt.minute + 5) // 60 % 24   # bar END hour（①用）
    hr_s3  = dt.hour                                        # bar START hour（③用）
    month  = dt.month

    if wd == 5:
        return []

    fired = []

    # ── 系統①（long）──────────────────────────────────────────
    if wd in S1_WEEKDAYS and month not in S1_EXCL_MONTHS:
        s1_hours = S1_HOURS_DST if is_dst(dt.to_pydatetime()) else S1_HOURS_WIN
        if hr in s1_hours:
            above_ma = (c2 > ma9p2 and c2 > ma10p2 and c1 > ma9p and c1 > ma10p)
            touch_lo = (abs(lo - ma9)  / ma9  <= TOUCH_PCT or
                        abs(lo - ma10) / ma10 <= TOUCH_PCT)
            gc       = row["macd"] > row["macd_sig"]
            if above_ma and touch_lo and gc:
                fired.append("①")

    # ── 系統③（short）─────────────────────────────────────────
    # 月曜5時台除外: 日中開始(8:45)まで持ち越し防止
    if wd in S3_WEEKDAYS and month not in S3_EXCL_MONTHS and not (wd == 0 and hr_s3 == 5):
        now_dt   = dt.to_pydatetime()
        s3_hours = S3_HOURS_DST if is_dst(now_dt) else S3_HOURS_WIN
        if hr_s3 in s3_hours and not is_cpi_window(now_dt, cpi_df):
            below_ma = ma9 < row["ma20"]
            touch_hi = abs(hi - ma9) / ma9 <= TOUCH_PCT
            dc       = row["macd"] < row["macd_sig"]
            if below_ma and touch_hi and dc:
                fired.append("③")

    return fired


# ==========================================================================
# 系統④ シグナル判定
# ==========================================================================
def check_s4(df: pd.DataFrame) -> bool:
    """系統④（逆張り long）シグナル判定。

    bar END hour 基準: hr = (dt.hour*60 + dt.minute - 5) // 60 % 24  (BT45と同式)
    曜日フィルターなし。
    """
    if df is None or len(df) < S4_LOOKBACK + 20:
        return False

    cur  = df.iloc[-1]
    prev = df.iloc[-1 - S4_LOOKBACK]

    if pd.isna(cur["rsi14"]):
        return False

    dt    = pd.to_datetime(cur["datetime"])
    hour  = (dt.hour * 60 + dt.minute - 5) // 60 % 24
    month = dt.month

    if month in S4_EXCL_MONTHS:
        return False

    s4_hours = S4_HOURS_DST if is_dst(dt.to_pydatetime()) else S4_HOURS_WIN
    if hour not in s4_hours:
        return False

    move_pct = (cur["close"] - prev["close"]) / prev["close"]
    return move_pct <= -S4_MOVE_PCT and cur["rsi14"] <= S4_RSI_TH


# ==========================================================================
# 系統⑤ シグナル判定
# ==========================================================================
def check_s5(df: pd.DataFrame, cpi_df: pd.DataFrame) -> bool:
    """系統⑤（逆張り short）シグナル判定。

    bar END hour 基準: hr = (dt.hour*60 + dt.minute - 5) // 60 % 24  (BT45と同式)
    CPI除外あり。
    """
    if df is None or len(df) < S5_LOOKBACK + 20:
        return False

    cur  = df.iloc[-1]
    prev = df.iloc[-1 - S5_LOOKBACK]

    if pd.isna(cur["rsi14"]):
        return False

    dt    = pd.to_datetime(cur["datetime"])
    hour  = (dt.hour * 60 + dt.minute - 5) // 60 % 24
    month = dt.month

    if month in S5_EXCL_MONTHS:
        return False

    s5_hours = S5_HOURS_DST if is_dst(dt.to_pydatetime()) else S5_HOURS_WIN
    if hour not in s5_hours:
        return False

    if is_cpi_window(dt.to_pydatetime(), cpi_df):
        return False

    move_pct = (cur["close"] - prev["close"]) / prev["close"]
    return move_pct >= S5_MOVE_PCT and cur["rsi14"] >= S5_RSI_TH

