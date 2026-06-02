"""
zh_bar.py
5分足バーデータの収集・加工・保存 + WebSocket受信 + ウォームアップ。
依存: zh_config, zh_utils, zh_api, ZAIHOU_signals
"""
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

import zh_api
import ZAIHOU_signals as sig
from zh_config import MICRO_CSV, WARMUP_BARS, JST
from zh_utils import floor_5min, log

# ==========================================================================
# 状態（このモジュールが所有）
# ==========================================================================
completed_bars: list       = []
current_bar:    dict | None = None
last_cum_vol:   int  | None = None

warmup_remaining: int  = 0
can_trade:        bool = True

_bar_state_lock = threading.Lock()

# 起動時に ZAIHOU.py から設定するコールバック（価格 tick を受けたときに呼ぶ）
# Phase 6 以降は zh_monitor.monitor_positions を直接渡す
_price_tick_callback = None

# ==========================================================================
# バー構築
# ==========================================================================
def _start_bar(bar_time, price, vol_delta):
    return {"datetime": bar_time, "open": price, "high": price,
            "low": price, "close": price, "volume": max(vol_delta, 0)}


def _update_main_bar(bar_time, price, vol_delta: int = 0) -> None:
    global completed_bars, current_bar
    if current_bar is None:
        current_bar = _start_bar(bar_time, price, vol_delta)
        return
    if current_bar["datetime"] != bar_time:
        completed_bars.append(current_bar)
        current_bar = _start_bar(bar_time, price, vol_delta)
        return
    current_bar["high"]    = max(current_bar["high"], price)
    current_bar["low"]     = min(current_bar["low"], price)
    current_bar["close"]   = price
    current_bar["volume"] += max(vol_delta, 0)


def _update_collect_bar(sym, bar_time, price, vol_delta: int = 0) -> None:
    st = zh_api._bar_state.get(sym)
    if st is None:
        return
    if st["current"] is None:
        st["current"] = _start_bar(bar_time, price, vol_delta)
        return
    if st["current"]["datetime"] != bar_time:
        st["completed"].append(st["current"])
        st["current"] = _start_bar(bar_time, price, vol_delta)
        return
    st["current"]["high"]    = max(st["current"]["high"], price)
    st["current"]["low"]     = min(st["current"]["low"], price)
    st["current"]["close"]   = price
    st["current"]["volume"] += max(vol_delta, 0)

# ==========================================================================
# DataFrame変換・CSV保存
# ==========================================================================
def bars_to_df() -> pd.DataFrame | None:
    rows = completed_bars.copy()
    if current_bar is not None:
        rows.append(current_bar.copy())
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.drop_duplicates(subset=["datetime"], keep="last")
    sort_keys = df["datetime"].map(sig._trading_day_sort_key)
    return df.iloc[sort_keys.argsort(kind="stable")].reset_index(drop=True)


def _strip_tz(idx):
    if hasattr(idx, "tz") and idx.tz is not None:
        return idx.tz_convert("Asia/Tokyo").tz_localize(None)
    return idx


def save_micro_csv(df: pd.DataFrame, path=None) -> None:
    if df is None or df.empty:
        return
    p = Path(path) if path else MICRO_CSV
    df_save = df.set_index("datetime") if "datetime" in df.columns else df.copy()
    df_save.index = _strip_tz(df_save.index)
    if p.exists():
        try:
            ex = pd.read_csv(p, parse_dates=["datetime"]).set_index("datetime")
            ex.index = _strip_tz(ex.index)
            df_save = pd.concat([ex, df_save])
            df_save = df_save[~df_save.index.duplicated(keep="last")]
        except Exception as e:
            log(f"[WARN] CSV読み込み失敗(新規作成): {e}")
    sort_keys = df_save.index.map(sig._trading_day_sort_key)
    df_save = df_save.iloc[sort_keys.argsort(kind="stable")]
    df_save.to_csv(p, encoding="utf-8-sig", index_label="datetime")

def save_collect_csv() -> None:
    """翌限月等の収集シンボルのバーデータを CSV に保存する"""
    for sym, csv_path in zh_api._collect_symbols.items():
        if sym == zh_api.SYMBOL:
            continue
        with _bar_state_lock:
            st   = zh_api._bar_state.get(sym, {})
            rows = st.get("completed", [])[:]
            if st.get("current"):
                rows.append(st["current"].copy())
        if rows:
            tmp = pd.DataFrame(rows)
            tmp["datetime"] = pd.to_datetime(tmp["datetime"])
            tmp = tmp.drop_duplicates(subset=["datetime"], keep="last")
            save_micro_csv(tmp, path=csv_path)

# ==========================================================================
# ウォームアップ
# ==========================================================================
def _needs_warmup(last_dt, now_naive) -> bool:
    hhmm = now_naive.hour * 100 + now_naive.minute
    if now_naive.weekday() == 0:
        fri_end = (now_naive - timedelta(days=3)).replace(
            hour=5, minute=55, second=0, microsecond=0
        )
        if last_dt >= fri_end - timedelta(minutes=30):
            return False
    if 600 <= hhmm < 845:
        night_end = now_naive.replace(hour=5, minute=55, second=0, microsecond=0)
        if last_dt >= night_end - timedelta(minutes=30):
            return False
    elif 1540 <= hhmm < 1700:
        day_end = now_naive.replace(hour=15, minute=35, second=0, microsecond=0)
        if last_dt >= day_end - timedelta(minutes=30):
            return False
    if last_dt > now_naive:
        return False
    return (now_naive - last_dt).total_seconds() >= 30 * 60


def load_warmup() -> bool:
    global completed_bars, warmup_remaining
    if not MICRO_CSV.exists():
        log(f"[WARN] ウォームアップCSVなし: {MICRO_CSV}")
        return True
    try:
        wdf = pd.read_csv(MICRO_CSV)
        wdf["datetime"] = pd.to_datetime(wdf["datetime"], errors="coerce")
        if wdf["datetime"].dt.tz is not None:
            wdf["datetime"] = (wdf["datetime"]
                               .dt.tz_convert("Asia/Tokyo")
                               .dt.tz_localize(None))
        for c in ["open", "high", "low", "close", "volume"]:
            wdf[c] = pd.to_numeric(wdf[c], errors="coerce")
        wdf = (wdf.dropna(subset=["open", "close"])
               .assign(_sk=lambda x: x["datetime"].map(sig._trading_day_sort_key))
               .sort_values("_sk", kind="stable")
               .drop(columns=["_sk"])
               .reset_index(drop=True))
        wdf.to_csv(MICRO_CSV, index=False, encoding="utf-8-sig")
        wdf = wdf.tail(WARMUP_BARS).reset_index(drop=True)
        completed_bars = wdf.to_dict("records")
        log(f"[OK] ウォームアップ: {len(completed_bars)}本")
        if not wdf.empty:
            last_dt   = pd.Timestamp(wdf["datetime"].max())
            now_naive = datetime.now(JST).replace(tzinfo=None)
            if _needs_warmup(last_dt, now_naive):
                warmup_remaining = 26
                log(f"[WARMUP START] 26本  最終CSV={last_dt.strftime('%H:%M')}")
        if len(completed_bars) < WARMUP_BARS:
            log(f"[STOP] データ不足({len(completed_bars)}/{WARMUP_BARS}本) → エントリー停止")
            return False
        return True
    except Exception as e:
        log(f"[WARN] ウォームアップ読み込みエラー: {e}")
        return True

# ==========================================================================
# WebSocket
# ==========================================================================
def _ws_on_message(ws, message):
    global last_cum_vol
    import json as _json
    try:
        data = _json.loads(message)
    except Exception:
        return
    sym        = data.get("Symbol")
    price_now  = data.get("CurrentPrice")
    price_time = data.get("CurrentPriceTime")
    if price_now is None or price_time is None:
        return
    price_now = float(price_now)
    bar_time  = floor_5min(
        datetime.fromisoformat(price_time).astimezone(JST).replace(tzinfo=None)
    )
    bar_time = sig._adjust_trading_day(bar_time)
    cum = data.get("TradingVolume")

    if sym == zh_api.SYMBOL:
        vol_delta = 0
        if cum is not None:
            cum = int(cum)
            if last_cum_vol is not None:
                vol_delta = cum - last_cum_vol if cum >= last_cum_vol else cum
            last_cum_vol = cum
        _update_main_bar(bar_time, price_now, vol_delta)
        if _price_tick_callback:
            _price_tick_callback(price_now)
    elif sym in zh_api._bar_state:
        with _bar_state_lock:
            st = zh_api._bar_state[sym]
            vol_delta = 0
            if cum is not None:
                cum = int(cum)
                prev = st["last_vol"]
                vol_delta = (cum - prev) if (prev is not None and cum >= prev) else cum
                st["last_vol"] = cum
            _update_collect_bar(sym, bar_time, price_now, vol_delta)


def start_ws() -> None:
    import websocket
    def _run():
        ws = websocket.WebSocketApp(
            "ws://localhost:18080/kabusapi/websocket",
            on_message=_ws_on_message,
        )
        ws.run_forever()
    threading.Thread(target=_run, daemon=True).start()
    log("[OK] WebSocket起動")

def update_from_board(hhmm: int, board: dict, now_naive) -> None:
    """ウォールクロックで main bar を補完（WebSocket tick が来ない時間帯向け）
    vol_delta=0 のため WebSocket 由来のボリュームは上書きしない。
    境界: 夜間後半〜6:00 / 日中〜15:45 のみ。6:05・15:50 は幽霊バーを防ぐため除外。
    """
    if not zh_api.SYMBOL or not board:
        return
    cp = board.get("CurrentPrice")
    if not cp:
        return
    if not ((845 <= hhmm <= 1545) or hhmm >= 1700 or hhmm <= 600):
        return
    _bt_now = sig._adjust_trading_day(floor_5min(now_naive))
    if current_bar is None or current_bar.get("datetime") != _bt_now:
        _update_main_bar(_bt_now, float(cp), 0)
