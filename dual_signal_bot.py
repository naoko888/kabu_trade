#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dual_signal_bot.py
系統①（月木×夜間18-23時）と系統②（火水×vol>=2.0×BB拡大）を
並列監視し、シグナル発火時に日経225マイクロ先物を取引するBot。
kabuステーションAPI（localhost:18080）使用。
auto_trade.py の API 構造（request_with_reauth 等）を踏襲。
"""

import os
import time
import json
import logging
import logging.handlers
import requests
import pandas as pd
import numpy as np
import pytz
from datetime import datetime, timedelta
from pathlib import Path

# =========================================
# 設定
# =========================================
API_BASE     = "http://localhost:18080/kabusapi"
API_PASSWORD = "sakimono35oku"       # kabuステーションAPIパスワード

SYMBOL   = "161060023"  # 日経225マイクロ先物（限月コードは変更要）
EXCHANGE = 2            # 大証

SL_PT    = 60           # ストップロス（pt）
TP_PT    = 240          # 利確（pt）
LOT      = 1            # 1枚固定

CSV_PATH    = Path(r"C:\kabu_trade\micro_5min.csv")  # ウォームアップ用5分足CSV
LOG_PATH    = Path(r"C:\kabu_trade\logs\dual_signal_bot.log")
WARMUP_BARS = 300       # ウォームアップ使用バー数
POLL_SEC    = 10.0      # ボードポーリング間隔（秒）
TOKEN_TTL   = 3300      # トークン更新間隔（秒、1時間より少し短め）
MAX_AUTH_ERR = 5        # 連続認証エラー上限

DRY_RUN = False         # True=注文なし（シミュレーション）

JST = pytz.timezone("Asia/Tokyo")

# シグナル判定定数
TOUCH_PCT = 0.005       # MAタッチ判定 ±0.5%

# =========================================
# ロガー設定
# =========================================
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logger = logging.getLogger("dual_signal_bot")
logger.setLevel(logging.DEBUG)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                         datefmt="%Y-%m-%d %H:%M:%S")
# ファイル出力（日次ローテーション、30日保持）
_fh = logging.handlers.TimedRotatingFileHandler(
    LOG_PATH, when="midnight", backupCount=30, encoding="utf-8"
)
_fh.setFormatter(_fmt)
# コンソール出力
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
logger.addHandler(_fh)
logger.addHandler(_sh)

def log(msg: str):
    """INFO レベルで出力するショートカット"""
    logger.info(msg)

# =========================================
# グローバル状態
# =========================================
_token: str = ""
_token_time: float = 0.0
_consecutive_auth_errors: int = 0

# =========================================
# API 認証
# =========================================
def _headers() -> dict:
    return {"X-API-KEY": _token, "Content-Type": "application/json"}


def get_token(force: bool = False) -> bool:
    """APIトークンを取得。成功 True / 失敗 False"""
    global _token, _token_time
    now = time.time()
    if not force and _token and (now - _token_time) < TOKEN_TTL:
        return True
    try:
        r = requests.post(f"{API_BASE}/token",
                          json={"APIPassword": API_PASSWORD}, timeout=10)
        r.raise_for_status()
        _token      = r.json()["Token"]
        _token_time = now
        log(f"トークン取得成功: {_token[:8]}...")
        return True
    except Exception as e:
        logger.warning(f"トークン取得失敗: {e}")
        return False


def _refresh_token() -> bool:
    """トークン再取得してシンボル登録も再実行"""
    global _consecutive_auth_errors
    if not get_token(force=True):
        _consecutive_auth_errors += 1
        logger.warning(f"トークン再取得失敗 (連続: {_consecutive_auth_errors}/{MAX_AUTH_ERR})")
        return False
    # シンボル登録（プッシュ受信用、なくても板取得は可能だが念のため）
    try:
        requests.put(
            f"{API_BASE}/register",
            headers=_headers(),
            json={"Symbols": [{"Symbol": SYMBOL, "Exchange": EXCHANGE}]},
            timeout=10,
        )
    except Exception:
        pass  # 登録失敗は無視
    _consecutive_auth_errors = 0
    return True


def api_request(method: str, path: str, body: dict = None, retry: int = 1):
    """汎用APIリクエスト。認証エラー時は1回だけ再取得してリトライ"""
    global _consecutive_auth_errors
    url = f"{API_BASE}{path}"
    try:
        r = requests.request(
            method, url,
            headers=_headers(),
            json=body,
            timeout=10,
        )
    except requests.RequestException as e:
        logger.warning(f"通信エラー {method} {path}: {e}")
        return None

    if r.status_code == 200:
        _consecutive_auth_errors = 0
        try:
            return r.json()
        except Exception:
            return {}

    # 認証エラー → 再取得してリトライ
    if r.status_code == 401 and retry > 0:
        logger.warning("認証エラー → トークン再取得してリトライ")
        if _refresh_token():
            return api_request(method, path, body, retry=0)
        return None

    logger.warning(f"APIエラー {r.status_code}: {r.text[:200]}")
    return None


# =========================================
# 板取得
# =========================================
def get_board() -> dict | None:
    """現在の板情報を取得"""
    return api_request("GET", f"/board/{SYMBOL}@{EXCHANGE}")


# =========================================
# 発注
# =========================================
def send_order(side: str, order_type: str = "market",
               price: float = 0) -> str | None:
    """
    発注。
    side       : "buy"（買） or "sell"（売）
    order_type : "market"（成行） or "limit"（指値）
    price      : 指値の場合に使用
    戻り値     : OrderId（失敗時 None）
    """
    buy_sell = "2" if side == "buy" else "1"
    body = {
        "Password":       API_PASSWORD,
        "Symbol":         SYMBOL,
        "Exchange":       EXCHANGE,
        "SecurityType":   1,        # 先物
        "Side":           buy_sell,
        "CashMargin":     2,        # 先物は2固定
        "MarginTradeType": 3,
        "DelivType":      2,
        "FundType":       "  ",
        "AccountType":    4,
        "Qty":            LOT,
        "ExpireDay":      0,
    }
    if order_type == "market":
        body["FrontOrderType"] = 10
        body["Price"] = 0
    else:  # limit
        body["FrontOrderType"] = 20
        body["Price"] = price

    if DRY_RUN:
        log(f"[DRY] 発注: side={side} type={order_type} price={price}")
        return "DRY_ORDER"

    res = api_request("POST", "/sendorder", body=body)
    if res is None:
        logger.error(f"発注失敗: side={side}")
        return None
    oid = res.get("OrderId", "")
    log(f"発注成功: side={side} OrderId={oid}")
    return oid


# =========================================
# ポジション確認
# =========================================
def get_position() -> dict | None:
    """保有中のマイクロ先物ポジションを返す（なければ None）"""
    res = api_request("GET", "/positions", body=None)
    if not isinstance(res, list):
        return None
    for p in res:
        if str(p.get("Symbol", "")) == SYMBOL:
            return p
    return None


# =========================================
# 5分足 OHLCV 構築
# =========================================
def _floor_5min(dt: datetime) -> datetime:
    """dt が属する5分足の開始時刻（秒・マイクロ秒ゼロ）"""
    m5 = (dt.minute // 5) * 5
    return dt.replace(minute=m5, second=0, microsecond=0)


class OHLCVBuilder:
    """
    ボードポーリング（CurrentPrice / TradingVolume）から
    5分足 OHLCV を組み立てるクラス。
    TradingVolume は当日累計なので差分で出来高を計算する。
    """

    def __init__(self, warmup_df: pd.DataFrame):
        # 過去足のコピーを保持（最大500本）
        self.bars = warmup_df[
            ["datetime", "open", "high", "low", "close", "volume"]
        ].copy().tail(500).reset_index(drop=True)

        self._slot:     datetime | None = None   # 現在建造中の足の開始時刻
        self._open:     float    = 0.0
        self._high:     float    = 0.0
        self._low:      float    = 0.0
        self._close:    float    = 0.0
        self._bar_vol:  float    = 0.0
        self._last_cumvol: float = 0.0

    def update(self, price: float, cumvol: float, dt: datetime) -> bool:
        """
        最新ボードデータで内部状態を更新。
        5分足が確定したら True を返す（シグナル判定トリガー）。
        """
        # タイムゾーンを除去して比較
        if dt.tzinfo is not None:
            dt = dt.astimezone(JST).replace(tzinfo=None)

        slot = _floor_5min(dt)

        # 初回
        if self._slot is None:
            self._slot          = slot
            self._open          = price
            self._high          = price
            self._low           = price
            self._close         = price
            self._last_cumvol   = cumvol
            return False

        # 同じ5分足の中
        if slot == self._slot:
            self._high  = max(self._high, price)
            self._low   = min(self._low,  price)
            self._close = price
            delta = max(0.0, cumvol - self._last_cumvol)
            self._bar_vol     += delta
            self._last_cumvol  = cumvol
            return False

        # 5分足が変わった → 前の足を確定して新しい足を開始
        new_bar = pd.DataFrame([{
            "datetime": self._slot,
            "open":     self._open,
            "high":     self._high,
            "low":      self._low,
            "close":    self._close,
            "volume":   self._bar_vol,
        }])
        self.bars = pd.concat([self.bars, new_bar], ignore_index=True)
        if len(self.bars) > 500:
            self.bars = self.bars.iloc[-500:].reset_index(drop=True)

        logger.debug(f"足確定: {self._slot} "
                     f"O={self._open} H={self._high} "
                     f"L={self._low} C={self._close} V={self._bar_vol:.0f}")

        # 新足開始
        self._slot          = slot
        self._open          = price
        self._high          = price
        self._low           = price
        self._close         = price
        self._bar_vol       = 0.0
        # 新しい足の累積出来高ベースをリセット
        self._last_cumvol   = cumvol

        return True  # 足確定


# =========================================
# 指標計算（バックテストと同一ロジック）
# =========================================
def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """MA9/MA10 / MACD(12,26,9) / vol_ratio / BB幅 を計算"""
    df = df.copy()

    df["ma9"]  = df["close"].rolling(9).mean()
    df["ma10"] = df["close"].rolling(10).mean()

    ema_fast        = df["close"].ewm(span=12, adjust=False).mean()
    ema_slow        = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"]      = ema_fast - ema_slow
    df["macd_sig"]  = df["macd"].ewm(span=9, adjust=False).mean()

    df["vol_ma20"]  = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma20"]

    bb_mid              = df["close"].rolling(20).mean()
    bb_std              = df["close"].rolling(20).std()
    df["bb_width"]      = (bb_std * 4) / bb_mid
    df["bb_width_ma20"] = df["bb_width"].rolling(20).mean()

    return df


# =========================================
# シグナル判定
# =========================================
def check_signal(df: pd.DataFrame) -> str | None:
    """
    最新3本のデータからシグナルを判定。
    戻り値 : "①"（系統①発火）/ "②"（系統②発火）/ None（シグナルなし）
    系統①と②が両方発火した場合は "①" を優先。
    """
    if len(df) < 30:
        return None

    # シグナルバー(i), 1本前(i-1), 2本前(i-2)
    row   = df.iloc[-1]
    row_p = df.iloc[-2]
    row_p2= df.iloc[-3]

    # 必須カラムが NaN でないか確認
    need = ["ma9", "ma10", "macd", "macd_sig", "vol_ratio",
            "bb_width", "bb_width_ma20"]
    for col in need:
        if pd.isna(row[col]) or pd.isna(row_p[col]) or pd.isna(row_p2[col]):
            return None

    # ── 基本条件（long 共通） ──
    m9   = row["ma9"];    m10   = row["ma10"]
    m9p  = row_p["ma9"];  m10p  = row_p["ma10"]
    m9p2 = row_p2["ma9"]; m10p2 = row_p2["ma10"]
    lo   = row["low"]
    c1   = row_p["close"];   c2 = row_p2["close"]

    # 直近2本closeが両MA（各バーのMA）より上
    above_ma = (c2 > m9p2 and c2 > m10p2 and c1 > m9p and c1 > m10p)
    # 現在足のlowがMA9またはMA10にタッチ（±0.5%以内）
    touch    = (abs(lo - m9)  / m9  <= TOUCH_PCT or
                abs(lo - m10) / m10 <= TOUCH_PCT)
    # MACD GC済み
    gc       = (row["macd"] > row["macd_sig"])

    if not (above_ma and touch and gc):
        return None

    # シグナルバーの日時属性
    dt    = pd.to_datetime(row["datetime"])
    wd    = dt.weekday()   # 0=月, 1=火, 2=水, 3=木, 4=金
    hr    = dt.hour
    month = dt.month

    result = None

    # ── 系統② チェック ──
    # 火水 × vol>=2.0 × BB拡大中 × 除外月なし(5-9月除外)
    if wd in (1, 2) and month not in (5, 6, 7, 8, 9):
        vr  = row["vol_ratio"]
        bw  = row["bb_width"]
        bwm = row["bb_width_ma20"]
        if (not pd.isna(vr) and vr >= 2.0 and
                not pd.isna(bw) and not pd.isna(bwm) and bw > bwm):
            result = "②"

    # ── 系統① チェック（①優先のため後から上書き） ──
    # 月木 × 18〜23時 × 除外月なし(3・7月除外)
    if wd in (0, 3) and hr in (18, 19, 20, 21, 22, 23) and month not in (3, 7):
        result = "①"

    return result


# =========================================
# メインループ
# =========================================
def main():
    log("=" * 60)
    log("dual_signal_bot 起動")
    log(f"  対象: {SYMBOL}@{EXCHANGE}  SL={SL_PT}pt TP={TP_PT}pt")
    log(f"  DRY_RUN={DRY_RUN}")
    log("=" * 60)

    # ── ウォームアップデータ読み込み ──
    if not CSV_PATH.exists():
        logger.error(f"ウォームアップCSVが見つかりません: {CSV_PATH}")
        return

    log(f"ウォームアップ読み込み: {CSV_PATH}")
    warmup = pd.read_csv(CSV_PATH)
    warmup["datetime"] = pd.to_datetime(warmup["datetime"], utc=True,
                                        errors="coerce")
    warmup["datetime"] = warmup["datetime"].dt.tz_localize(None)
    for c in ["open", "high", "low", "close", "volume"]:
        warmup[c] = pd.to_numeric(warmup[c], errors="coerce")
    warmup = (warmup.dropna(subset=["open", "close"])
              .tail(WARMUP_BARS)
              .reset_index(drop=True))
    log(f"ウォームアップ完了: {len(warmup)} 本")

    # ── トークン初期取得 ──
    for attempt in range(1, 6):
        if get_token(force=True):
            break
        logger.warning(f"トークン取得失敗 ({attempt}/5)、60秒待機...")
        time.sleep(60)
    else:
        logger.error("トークン取得に5回失敗、終了します")
        return

    # ── OHLCV ビルダー初期化 ──
    builder = OHLCVBuilder(warmup)

    # ── ポジション管理変数 ──
    in_position  = False
    entry_price  = 0.0
    entry_system = ""
    entry_time   = None

    log("シグナル監視ループ開始...")

    while True:
        try:
            # ── 連続認証エラー上限チェック ──
            if _consecutive_auth_errors >= MAX_AUTH_ERR:
                logger.error(f"認証エラー連続 {MAX_AUTH_ERR} 回 → 終了します")
                break

            # ── ボードポーリング ──
            board = get_board()
            if board is None:
                time.sleep(POLL_SEC)
                continue

            now_price = float(board.get("CurrentPrice") or 0)
            cum_vol   = float(board.get("TradingVolume") or 0)
            pt_str    = board.get("CurrentPriceTime", "")

            try:
                now_dt = datetime.fromisoformat(pt_str)
            except Exception:
                now_dt = datetime.now(JST)

            if now_price <= 0:
                time.sleep(POLL_SEC)
                continue

            # ── ポジション保有中は SL/TP を監視 ──
            if in_position:
                if now_price >= entry_price + TP_PT:
                    log(f"[{entry_system}] TP到達 "
                        f"現在値={now_price} エントリー={entry_price:.0f} "
                        f"利益=+{TP_PT}pt")
                    send_order("sell")
                    in_position = False

                elif now_price <= entry_price - SL_PT:
                    log(f"[{entry_system}] SL到達 "
                        f"現在値={now_price} エントリー={entry_price:.0f} "
                        f"損失=-{SL_PT}pt")
                    send_order("sell")
                    in_position = False

                time.sleep(POLL_SEC)
                continue

            # ── 足確定チェック ──
            bar_closed = builder.update(now_price, cum_vol, now_dt)
            if not bar_closed:
                time.sleep(POLL_SEC)
                continue

            # ── 足確定 → 指標計算 → シグナル判定 ──
            df_ind = calc_indicators(builder.bars)
            signal = check_signal(df_ind)

            if signal is None:
                time.sleep(POLL_SEC)
                continue

            log(f"★ シグナル発火: 系統{signal}  "
                f"時刻={now_dt.strftime('%Y-%m-%d %H:%M')}  "
                f"参考価格={now_price:.0f}")

            # ── 発注 ──
            oid = send_order("buy")
            if oid or DRY_RUN:
                in_position  = True
                entry_price  = now_price   # 参考値（約定値は orders API で確認推奨）
                entry_system = signal
                entry_time   = now_dt
                log(f"[{signal}] エントリー完了 参考価格={entry_price:.0f} "
                    f"SL目標={entry_price - SL_PT:.0f} "
                    f"TP目標={entry_price + TP_PT:.0f}")
            else:
                logger.error("発注失敗 → シグナル無効化")

        except KeyboardInterrupt:
            log("手動停止（Ctrl+C）")
            break
        except Exception as e:
            logger.exception(f"予期しないエラー: {e}")
            time.sleep(60)   # 未処理例外は60秒待機して継続

        time.sleep(POLL_SEC)

    log("dual_signal_bot 終了")


if __name__ == "__main__":
    main()
