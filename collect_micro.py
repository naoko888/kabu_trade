"""
日経225マイクロ先物 データ収集専用
- kabuステーションAPI
- トークン切れ自動再取得
- 5分足CSV保存（出来高含む）
- デイ: 8:45-15:45 / ナイト: 16:30-翌6:00
"""

import os
import time
import requests
import pandas as pd
import pytz
from datetime import datetime

# =========================
# 設定
# =========================
API_BASE = "http://localhost:18080/kabusapi"
API_PASSWORD = "sakimono35oku"

CSV_FILE = "micro_5min.csv"
POLL_SEC = 1.0
JST = pytz.timezone("Asia/Tokyo")

token = None
SYMBOL = None
EXCHANGE = None
last_cum_volume = None
current_bar = None
completed_bars = []
last_reauth_time = 0   # 再認証クールダウン用

# 休場日（土日以外の特別休場日）
HOLIDAYS = {
    # 2026年
    (2026,  1,  2),
    (2026, 11, 23),
    (2026, 12, 31),
    # 2027年
    (2027,  9, 20),
    (2027, 12, 31),
}

def is_holiday(d) -> bool:
    """date オブジェクトが休場日か判定"""
    return (d.year, d.month, d.day) in HOLIDAYS


# =========================
# 共通
# =========================
def log(msg):
    print(f"[{datetime.now(JST).strftime('%H:%M:%S')}] {msg}")

def headers():
    return {
        "Content-Type": "application/json",
        "X-API-KEY": token
    }

def floor_5min(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0, minute=(dt.minute // 5) * 5)

def safe_json(res):
    try:
        return res.json()
    except Exception:
        return {"raw": res.text}

def is_trading_time(hhmm):
    """取引時間かどうか判定
    日中: 8:45〜15:40 / 夜間: 17:00〜翌5:55
    休憩(15:40〜17:00)・深夜終了後(5:55〜8:45)は除外
    """
    if 845 <= hhmm < 1540:   # 日中セッション
        return True
    if hhmm >= 1700:          # 夜間セッション前半
        return True
    if hhmm < 555:            # 夜間セッション後半（深夜〜朝）
        return True
    return False


# =========================
# トークン・認証
# =========================
def get_token():
    global token
    url = f"{API_BASE}/token"
    try:
        res = requests.post(url, json={"APIPassword": API_PASSWORD}, timeout=10)
    except requests.RequestException as e:
        log(f"❌ トークン取得通信失敗: {e}")
        return False

    if res.status_code == 200:
        token = res.json()["Token"]
        log("✅ トークン取得成功")
        return True

    log(f"❌ トークン取得失敗: {res.text}")
    return False

def get_futures_symbol():
    global SYMBOL, EXCHANGE
    # NK225micro → NK225mini の順で6月限(DerivMonth=202606)を取得
    for code in ["NK225micro", "NK225mini"]:
        url = f"{API_BASE}/symbolname/future?FutureCode={code}&DerivMonth=202606"
        try:
            res = requests.get(url, headers=headers(), timeout=10)
        except requests.RequestException as e:
            log(f"⚠ シンボル取得通信失敗({code}): {e}")
            continue

        if res.status_code != 200:
            log(f"⚠ {code} → {res.status_code} {res.text}")
            continue

        data = res.json()
        symbol = data.get("Symbol")
        if not symbol:
            log(f"⚠ {code} → Symbol空")
            continue

        SYMBOL = symbol
        # Exchange はレスポンスに含まれないので先物デフォルト値を使用
        EXCHANGE = data.get("Exchange") or 2
        log(f"✅ 先物シンボル取得({code}): {SYMBOL} ({data.get('SymbolName')}) / Exchange={EXCHANGE}")
        return True

    log("❌ シンボル取得失敗 → kabuステーションの接続を確認してください")
    return False

def register_symbol():
    if SYMBOL is None or EXCHANGE is None:
        log("❌ シンボル未取得")
        return False

    url = f"{API_BASE}/register"
    body = {"Symbols": [{"Symbol": SYMBOL, "Exchange": EXCHANGE}]}

    try:
        res = requests.put(url, headers=headers(), json=body, timeout=10)
    except Exception as e:
        log(f"❌ 通信エラー: {e}")
        return False

    if res.status_code == 200:
        log(f"✅ 登録成功: {SYMBOL} / {EXCHANGE}")
        return True

    log(f"❌ 登録失敗: {res.text}")
    return False

def refresh_auth():
    global last_reauth_time
    # 5秒以内の連続再認証を防ぐ
    elapsed = time.time() - last_reauth_time
    if elapsed < 5:
        time.sleep(5 - elapsed)

    log("🔄 認証を再取得します")
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
        res = requests.request(
            method=method,
            url=url,
            headers=headers() if token else {"Content-Type": "application/json"},
            timeout=10
        )
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
    path = f"/board/{SYMBOL}@{EXCHANGE}"
    res = request_with_reauth("GET", path)
    if res is None:
        return None

    data = safe_json(res)

    if data.get("CurrentPrice") is None:
        log("⚠ board空 → 再登録して再取得")
        if register_symbol():
            time.sleep(1.0)
            res = request_with_reauth("GET", path)
            if res is not None:
                data = safe_json(res)

    return data


# =========================
# 5分足生成
# =========================
def start_new_bar(bar_time, price, vol_delta):
    return {
        "datetime": bar_time,
        "open": price,
        "high": price,
        "low": price,
        "close": price,
        "volume": max(vol_delta, 0),
    }

def update_bar(board):
    global current_bar, completed_bars, last_cum_volume

    if not board:
        return

    price = board.get("CurrentPrice")
    cum_volume = board.get("TradingVolume")

    if price is None:
        return

    now = datetime.now(JST)
    bar_time = floor_5min(now)
    price = float(price)

    vol_delta = 0
    if cum_volume is not None:
        try:
            cum_volume = int(cum_volume)
            if last_cum_volume is not None and cum_volume >= last_cum_volume:
                vol_delta = cum_volume - last_cum_volume
            else:
                # セッション切り替わりで累積出来高がリセットされた場合
                vol_delta = 0
            last_cum_volume = cum_volume
        except Exception:
            vol_delta = 0

    if current_bar is None:
        current_bar = start_new_bar(bar_time, price, vol_delta)
        return

    if current_bar["datetime"] != bar_time:
        completed_bars.append(current_bar)
        current_bar = start_new_bar(bar_time, price, vol_delta)
        return

    current_bar["high"] = max(current_bar["high"], price)
    current_bar["low"] = min(current_bar["low"], price)
    current_bar["close"] = price
    current_bar["volume"] += max(vol_delta, 0)


# =========================
# CSV保存
# =========================
def bars_to_df():
    rows = completed_bars.copy()
    if current_bar is not None:
        rows.append(current_bar.copy())

    if not rows:
        return None

    df = pd.DataFrame(rows)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.drop_duplicates(subset=["datetime"], keep="last").sort_values("datetime")
    df = df.set_index("datetime")
    return df

def _strip_tz(idx):
    """DatetimeIndex を tz-naive JST に統一"""
    if hasattr(idx, "tz") and idx.tz is not None:
        return idx.tz_convert("Asia/Tokyo").tz_localize(None)
    return idx

def save_to_csv(df):
    if df is None or df.empty:
        return

    # 新規バーは tz-aware JST → naive に正規化
    df = df.copy()
    df.index = _strip_tz(df.index)

    if os.path.exists(CSV_FILE):
        existing = pd.read_csv(CSV_FILE, parse_dates=["datetime"]).set_index("datetime")
        existing.index = _strip_tz(existing.index)
        combined = pd.concat([existing, df])
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    else:
        combined = df.copy()

    combined.to_csv(CSV_FILE, encoding="utf-8-sig")


# =========================
# メイン
# =========================
def main():
    print("=" * 55)
    print("📊 日経225マイクロ先物 データ収集開始")
    print(f"   日中: 8:45-15:40 / 夜間: 17:00-翌5:55")
    print(f"   保存先: {CSV_FILE}")
    print("=" * 55)

    if not refresh_auth():
        log("❌ 起動失敗")
        return

    last_logged_min = -1   # ターミナル表示用（5分おき）
    last_csv_min = -1      # CSV保存用（1分おき）

    while True:
        now = datetime.now(JST)
        hhmm = now.hour * 100 + now.minute
        weekday = now.weekday()  # 0=月 〜 6=日

        # 土日・休場日は全休み
        if weekday >= 5 or is_holiday(now.date()):
            log(f"休場日 → 60秒待機 ({now.strftime('%Y-%m-%d')})")
            time.sleep(60)
            continue

        # 非取引時間（セッション間・早朝）
        if not is_trading_time(hhmm):
            log(f"非取引時間 ({now.strftime('%H:%M')}) → 30秒待機")
            time.sleep(30)
            continue

        # --- 取引時間内 ---
        board = get_board()
        if board is None:
            log("⚠ board取得できていない")
        elif board.get("CurrentPrice") is None:
            log(f"⚠ boardは取れたが CurrentPrice が空: {board}")
        else:
            update_bar(board)
            # ターミナル表示は5分おき
            if now.minute % 5 == 0 and now.minute != last_logged_min:
                cp = board.get("CurrentPrice")
                log(f"現在値: {float(cp):,.0f}円")
                last_logged_min = now.minute

        # 1分ごとにCSV保存
        if now.minute != last_csv_min and now.second < 2:
            df = bars_to_df()
            if df is not None and len(df) > 0:
                save_to_csv(df)
                last_csv_min = now.minute

        time.sleep(POLL_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n⛔ 手動停止 → CSV保存中...")
        df = bars_to_df()
        save_to_csv(df)
