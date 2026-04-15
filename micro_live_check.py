"""
日経225マイクロ先物 リアルタイム検証（DRY_RUNのみ）
signal_overlap_only: deep + vol_up + gap_small + rsi30
TP=120, SL=40, MAX_HOLD_BARS=6
"""

import os
import time
import requests
import pandas as pd
import numpy as np
import pytz
from datetime import datetime, date

# =========================
# 設定
# =========================
API_BASE     = "http://localhost:18080/kabusapi"
API_PASSWORD = "sakimono35oku"

TP            = 120
SL            = 40
MAX_HOLD_BARS = 6
MIN_BARS      = 3    # シグナル判定開始に必要な最低本数（指標NaNなら自動スキップ）

CSV_FILE = "micro_live_5min.csv"
POLL_SEC = 1.0
JST      = pytz.timezone("Asia/Tokyo")

# =========================
# グローバル状態
# =========================
token    = None
SYMBOL   = None
EXCHANGE = None

last_cum_volume  = None
current_bar      = None
completed_bars   = []
last_reauth_time = 0

last_signal_bar_time = None
dry_position         = None
dry_day_pnl          = 0
dry_trade_log        = []


# =========================
# 共通
# =========================
def log(msg):
    print(f"[{datetime.now(JST).strftime('%H:%M:%S')}] {msg}")

def headers():
    return {"Content-Type": "application/json", "X-API-KEY": token}

def safe_json(res):
    try:
        return res.json()
    except Exception:
        return {"raw": res.text}

def floor_5min(dt):
    return dt.replace(second=0, microsecond=0, minute=(dt.minute // 5) * 5)

def is_trading_time(hhmm):
    if 845 <= hhmm < 1545:
        return True
    if hhmm >= 1700 or hhmm < 600:
        return True
    return False


# =========================
# 認証
# =========================
def get_token():
    global token
    try:
        res = requests.post(f"{API_BASE}/token", json={"APIPassword": API_PASSWORD}, timeout=10)
    except requests.RequestException as e:
        log(f"❌ トークン取得通信失敗: {e}")
        return False
    if res.status_code == 200:
        token = res.json()["Token"]
        log(f"✅ トークン取得成功: {token[:8]}...")
        return True
    log(f"❌ トークン取得失敗: {res.text}")
    return False

def get_futures_symbol():
    global SYMBOL, EXCHANGE
    for code in ["NK225micro", "NK225mini"]:
        url = f"{API_BASE}/symbolname/future?FutureCode={code}&DerivMonth=202606"
        try:
            res = requests.get(url, headers=headers(), timeout=10)
        except requests.RequestException as e:
            log(f"⚠ シンボル取得失敗({code}): {e}")
            continue
        if res.status_code != 200:
            continue
        data = res.json()
        symbol = data.get("Symbol")
        if not symbol:
            continue
        SYMBOL   = symbol
        EXCHANGE = data.get("Exchange") or 2
        log(f"✅ シンボル取得({code}): {SYMBOL} / Exchange={EXCHANGE}")
        return True
    log("❌ シンボル取得失敗")
    return False

def register_symbol():
    if SYMBOL is None:
        return False
    try:
        res = requests.put(
            f"{API_BASE}/register",
            headers=headers(),
            json={"Symbols": [{"Symbol": SYMBOL, "Exchange": EXCHANGE}]},
            timeout=10
        )
    except Exception as e:
        log(f"❌ 登録通信エラー: {e}")
        return False
    if res.status_code == 200:
        log(f"✅ 銘柄登録成功: {SYMBOL}")
        return True
    log(f"❌ 銘柄登録失敗: {res.text}")
    return False

def refresh_auth():
    global last_reauth_time
    elapsed = time.time() - last_reauth_time
    if elapsed < 5:
        time.sleep(5 - elapsed)
    log("🔄 認証再取得")
    last_reauth_time = time.time()
    if not get_token():
        return False
    time.sleep(0.5)
    if not get_futures_symbol():
        return False
    time.sleep(0.5)
    if not register_symbol():
        return False
    time.sleep(1.0)
    return True

def request_with_reauth(method, path, retry=1):
    url = f"{API_BASE}{path}"
    try:
        res = requests.request(method=method, url=url, headers=headers(), timeout=10)
    except requests.RequestException as e:
        log(f"⚠ 通信エラー: {e}")
        return None
    if res.status_code == 200:
        return res
    if res.status_code == 401 and retry > 0:
        log("⚠ 401 → トークン再取得")
        if refresh_auth():
            return request_with_reauth(method, path, retry=retry - 1)
    return None


# =========================
# 板取得
# =========================
def get_board():
    res = request_with_reauth("GET", f"/board/{SYMBOL}@{EXCHANGE}")
    return safe_json(res) if res else None

def get_price(board):
    p = board.get("CurrentPrice")
    if p is not None:
        return float(p)
    bid = board.get("BidPrice")
    ask = board.get("AskPrice")
    if bid and ask:
        return (float(bid) + float(ask)) / 2
    return float(bid or ask) if (bid or ask) else None


# =========================
# 5分足生成
# =========================
def update_bar(board):
    global current_bar, completed_bars, last_cum_volume

    price      = get_price(board)
    cum_volume = board.get("TradingVolume")
    if price is None:
        return

    now      = datetime.now(JST)
    bar_time = floor_5min(now)

    vol_delta = 0
    if cum_volume is not None:
        try:
            cum_volume = int(cum_volume)
            if last_cum_volume is not None and cum_volume >= last_cum_volume:
                vol_delta = cum_volume - last_cum_volume
            last_cum_volume = cum_volume
        except Exception:
            pass

    if current_bar is None:
        current_bar = {"datetime": bar_time, "open": price, "high": price, "low": price, "close": price, "volume": max(vol_delta, 0)}
        return

    if current_bar["datetime"] != bar_time:
        completed_bars.append(current_bar)
        current_bar = {"datetime": bar_time, "open": price, "high": price, "low": price, "close": price, "volume": max(vol_delta, 0)}
        return

    current_bar["high"]    = max(current_bar["high"], price)
    current_bar["low"]     = min(current_bar["low"], price)
    current_bar["close"]   = price
    current_bar["volume"] += max(vol_delta, 0)

def bars_to_df():
    rows = completed_bars.copy()
    if current_bar is not None:
        rows.append(current_bar.copy())
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.drop_duplicates(subset=["datetime"], keep="last").sort_values("datetime").reset_index(drop=True)
    return df

def save_csv(df):
    if df is None or df.empty:
        return
    df_save = df.set_index("datetime")
    if os.path.exists(CSV_FILE):
        existing = pd.read_csv(CSV_FILE, parse_dates=["datetime"]).set_index("datetime")
        combined = pd.concat([existing, df_save])
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    else:
        combined = df_save
    combined.to_csv(CSV_FILE, encoding="utf-8-sig")


# =========================
# 指標（backtest_micro.pyと同一）
# =========================
def add_indicators(df):
    df = df.copy()

    ts = pd.to_datetime(df["datetime"])
    df["hour"] = ts.dt.hour

    df["ma20"]        = df["close"].rolling(20).mean()
    df["ma20_slope"]  = df["ma20"] - df["ma20"].shift(1)
    df["dist_ma20_pct"] = (df["close"] / df["ma20"] - 1.0) * 100

    df["vol_ma20"]  = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma20"]

    diff     = df["close"].diff()
    up       = diff.clip(lower=0)
    down     = -diff.clip(upper=0)
    roll_up  = up.rolling(14).mean()
    roll_dn  = down.rolling(14).mean()
    rs       = roll_up / roll_dn.replace(0, np.nan)
    df["rsi14"] = 100 - (100 / (1 + rs))

    df["gap_pct"] = df["open"] / df["close"].shift(1) - 1

    return df


# =========================
# シグナル（backtest_micro.pyと同一）
# =========================
def signal_overlap_only(df, i):
    if i < 1:
        return False
    cur  = df.iloc[i]
    prev = df.iloc[i - 1]

    # signal_pullback_deep
    if pd.isna(cur["ma20_slope"])   or cur["ma20_slope"] <= 0:
        return False
    if pd.isna(prev["dist_ma20_pct"]) or prev["dist_ma20_pct"] > -0.3:
        return False
    if cur["close"] <= prev["high"]:
        return False

    # filter_cur_vol_up
    if pd.isna(cur["vol_ratio"]) or cur["vol_ratio"] < 1.2:
        return False

    # filter_gap_small_minus
    if pd.isna(cur["gap_pct"]) or cur["gap_pct"] < -0.2:
        return False

    # filter_rsi_30
    if pd.isna(prev["rsi14"]) or prev["rsi14"] > 30:
        return False

    return True


# =========================
# 仮想決済ログ保存
# =========================
def save_dry_log():
    if not dry_trade_log:
        return
    filename = f"micro_dry_log_{date.today().strftime('%Y%m%d')}.csv"
    pd.DataFrame(dry_trade_log).to_csv(filename, index=False, encoding="utf-8-sig")
    log(f"📊 DRYログ保存: {filename}")


# =========================
# メイン
# =========================
def main():
    global last_signal_bar_time, dry_position, dry_day_pnl, dry_trade_log

    print("=" * 60)
    print("📊 日経225マイクロ先物 リアルタイム検証（DRY_RUN）")
    print(f"   signal: overlap_only  TP:{TP}  SL:{SL}  BARS:{MAX_HOLD_BARS}")
    print("=" * 60)

    if not refresh_auth():
        log("❌ 起動失敗")
        return

    last_csv_min = -1

    while True:
        now     = datetime.now(JST)
        hhmm    = now.hour * 100 + now.minute
        weekday = now.weekday()

        if weekday >= 5:
            log("土日 → 60秒待機")
            time.sleep(60)
            continue

        # デイセッション終了
        if hhmm >= 1545:
            log(f"✅ デイ終了 本日仮想損益: {dry_day_pnl:+.0f}円")
            df = bars_to_df()
            if df is not None:
                save_csv(df)
            save_dry_log()
            break

        if not is_trading_time(hhmm):
            time.sleep(30)
            continue

        board = get_board()
        if board and board.get("CurrentPrice"):
            update_bar(board)

        # 1分ごとにCSV保存
        if now.minute != last_csv_min and now.second < 2:
            df = bars_to_df()
            if df is not None:
                save_csv(df)
            last_csv_min = now.minute

        # 仮想ポジション監視
        if dry_position is not None and board and board.get("CurrentPrice"):
            cp  = get_price(board)
            pnl = cp - dry_position["entry_price"]
            reason = None

            if cp <= dry_position["sl_price"]:
                reason = "SL到達"
            elif cp >= dry_position["tp_price"]:
                reason = "TP到達"
            elif hhmm >= 1540:
                reason = "大引け強制決済"
            else:
                df_tmp = bars_to_df()
                if df_tmp is not None:
                    entry_bar = floor_5min(dry_position["entry_time"])
                    elapsed   = int((floor_5min(now) - entry_bar).total_seconds() // 300)
                    dry_position["bars"] = elapsed
                    if elapsed >= MAX_HOLD_BARS:
                        reason = "時間決済"

            if reason:
                dry_day_pnl += pnl
                dry_trade_log.append({
                    "entry_time":  dry_position["entry_time"].strftime("%Y-%m-%d %H:%M:%S"),
                    "exit_time":   now.strftime("%Y-%m-%d %H:%M:%S"),
                    "entry_price": dry_position["entry_price"],
                    "exit_price":  cp,
                    "pnl":         round(pnl, 1),
                    "reason":      reason,
                })
                log(f"🔵 [DRY] 決済:{reason} @ {cp:.0f}  損益:{pnl:+.0f}  本日累計:{dry_day_pnl:+.0f}")
                dry_position = None
                save_dry_log()
            else:
                log(f"[DRY] 保有中 @ {dry_position['entry_price']:.0f}  含み:{pnl:+.0f}  bars={dry_position.get('bars',0)}")

        # 9:00まではデータ収集のみ
        if hhmm < 900:
            time.sleep(POLL_SEC)
            continue

        # シグナル判定
        df = bars_to_df()
        if df is None or len(df) < MIN_BARS:
            log(f"⏳ データ蓄積中 ({len(df) if df is not None else 0}/{MIN_BARS}本)")
            time.sleep(POLL_SEC)
            continue

        df = add_indicators(df)
        i  = len(df) - 1

        latest_bar_time = df.iloc[-1]["datetime"]
        if last_signal_bar_time == latest_bar_time:
            time.sleep(POLL_SEC)
            continue

        last_signal_bar_time = latest_bar_time
        sig = signal_overlap_only(df, i)

        if sig:
            if dry_position is None and board:
                cp = get_price(board)
                if cp:
                    dry_position = {
                        "entry_time":  now,
                        "entry_price": cp,
                        "sl_price":    cp - SL,
                        "tp_price":    cp + TP,
                        "bars":        0,
                    }
                    log(f"🟢 [DRY] エントリー @ {cp:.0f}  SL:{cp-SL:.0f}  TP:{cp+TP:.0f}")
            else:
                log("🟢 [DRY] シグナルあり（仮想ポジション保有中のためスキップ）")
        else:
            cp = get_price(board) if board else None
            log(f"→ シグナルなし (現在値:{cp:.0f}円 bars:{len(df)}本)" if cp else f"→ シグナルなし (bars:{len(df)}本)")

        time.sleep(POLL_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("⛔ 手動停止")
        df = bars_to_df()
        if df is not None:
            save_csv(df)
        save_dry_log()
