"""
統合スクリプト: 1570自動売買 + 日経225マイクロ先物データ収集
- トークンを1つ共有（kabuステーションは同時1トークンのみ有効）
- スレッドで並列実行
"""

import os
import time
import threading
import requests
import pandas as pd
import pytz
import winsound
from datetime import datetime, date

# =========================
# 共通設定
# =========================
API_BASE     = "http://localhost:18080/kabusapi"
API_PASSWORD = "sakimono35oku"
JST          = pytz.timezone("Asia/Tokyo")

# =========================
# 共有トークン管理
# =========================
token          = None
token_lock     = threading.Lock()
last_reauth_at = 0.0

def log(prefix, msg):
    print(f"[{datetime.now(JST).strftime('%H:%M:%S')}][{prefix}] {msg}")

def _headers():
    return {"Content-Type": "application/json", "X-API-KEY": token}

def _get_token():
    global token
    res = requests.post(f"{API_BASE}/token", json={"APIPassword": API_PASSWORD}, timeout=10)
    if res.status_code == 200:
        token = res.json()["Token"]
        return True
    return False

def _register_symbols(symbols):
    """symbols: [{"Symbol": "...", "Exchange": N}, ...]"""
    res = requests.put(
        f"{API_BASE}/register",
        headers=_headers(),
        json={"Symbols": symbols},
        timeout=10
    )
    if res.status_code == 200:
        return True
    print(f"  登録失敗詳細: status={res.status_code} body={res.text}")
    return False

def refresh_auth(caller=""):
    """トークン再取得 + 両銘柄再登録（5秒クールダウン付き）"""
    global last_reauth_at
    with token_lock:
        elapsed = time.time() - last_reauth_at
        if elapsed < 5:
            time.sleep(5 - elapsed)

        log(caller, "🔄 認証再取得")
        last_reauth_at = time.time()

        if not _get_token():
            log(caller, "❌ トークン取得失敗")
            return False

        time.sleep(0.5)

        # 1570（東証）+ NK225micro（OSE）両方登録
        symbols = [
            {"Symbol": "1570",     "Exchange": 1},
            {"Symbol": MICRO_SYMBOL, "Exchange": MICRO_EXCHANGE},
        ]
        if not _register_symbols(symbols):
            log(caller, "❌ 銘柄登録失敗")
            return False

        log(caller, f"✅ 認証完了 / 銘柄登録: 1570, {MICRO_SYMBOL}")
        return True

def api_get(path, caller="", retry=1):
    url = f"{API_BASE}{path}"
    try:
        res = requests.get(url, headers=_headers(), timeout=10)
    except requests.RequestException as e:
        log(caller, f"⚠ 通信エラー: {e}")
        return None

    if res.status_code == 200:
        return res.json()

    if res.status_code == 401 and retry > 0:
        if refresh_auth(caller):
            return api_get(path, caller, retry - 1)

    return None


# =========================
# マイクロ先物シンボル取得
# =========================
MICRO_SYMBOL   = "161060023"   # 起動時に上書き
MICRO_EXCHANGE = 2

def fetch_micro_symbol():
    global MICRO_SYMBOL, MICRO_EXCHANGE
    # まず動的取得を試みる
    for code in ["NK225micro", "NK225mini"]:
        url = f"{API_BASE}/symbolname/future?FutureCode={code}&DerivMonth=202606"
        try:
            res = requests.get(url, headers=_headers(), timeout=10)
            if res.status_code == 200:
                data = res.json()
                sym = data.get("Symbol")
                if sym:
                    MICRO_SYMBOL   = sym
                    MICRO_EXCHANGE = data.get("Exchange") or 2
                    log("MAIN", f"✅ マイクロ先物: {MICRO_SYMBOL} ({data.get('SymbolName')})")
                    return True
        except Exception:
            pass
    # フォールバック（固定シンボル）
    log("MAIN", f"⚠ 動的取得失敗 → 固定: {MICRO_SYMBOL}")
    return True


# ══════════════════════════════════════════════════════
# 1570 自動売買スレッド
# ══════════════════════════════════════════════════════
S1570          = "1570"
S1570_EXCHANGE = 1
LOT            = 1
STOP           = 100
TP             = 250
BARS           = 4
MAX_LOSS_DAY   = 300
MAX_CONSEC_LOSS = 3
BB_SQ_TH       = 0.90
ATR_RATIO_TH   = 0.70
CSV_1570       = r"c:\kabu_trade\1570_5min.csv"

def run_1570():
    position          = None
    day_pnl           = 0
    consec_loss       = 0
    trade_log         = []
    prev_cum_vol      = 0
    last_signal_t     = None
    last_csv_1570_min = -1
    current_bar_1570  = None
    completed_bars_1570 = []

    log("1570", "🚀 自動売買スレッド開始")

    def board_1570():
        return api_get(f"/board/{S1570}@{S1570_EXCHANGE}", "1570")

    def current_price_1570():
        d = board_1570()
        return d.get("CurrentPrice") if d else None

    def send_order(side, price, qty, order_type="market", stop_price=None, limit_price=None):
        buy_sell = 1 if side == "buy" else 2
        base = {
            "Password": API_PASSWORD, "Symbol": S1570, "Exchange": S1570_EXCHANGE,
            "SecurityType": 1, "Side": str(buy_sell), "CashMargin": 2,
            "MarginTradeType": 3, "DelivType": 2, "FundType": "  ",
            "AccountType": 4, "Qty": qty, "ExpireDay": 0,
        }
        if order_type == "market":
            base.update({"FrontOrderType": 10, "Price": 0})
        elif order_type == "limit":
            base.update({"FrontOrderType": 20, "Price": limit_price})
        elif order_type == "stop":
            base.update({"FrontOrderType": 30, "Price": 0, "StopPrice": stop_price})
        try:
            res = requests.post(f"{API_BASE}/sendorder", headers=_headers(), json=base, timeout=10)
            if res.status_code == 200:
                oid = res.json().get("OrderId", "")
                log("1570", f"  ✅ 発注成功 OrderId: {oid}")
                return oid
        except Exception as e:
            log("1570", f"  ❌ 発注エラー: {e}")
        return None

    def floor_5min(dt):
        return dt.replace(second=0, microsecond=0, minute=(dt.minute // 5) * 5)

    def update_bar_1570(price, vol_delta):
        nonlocal current_bar_1570, completed_bars_1570
        now      = datetime.now(JST)
        bar_time = floor_5min(now)
        if current_bar_1570 is None:
            current_bar_1570 = {"datetime": bar_time, "open": price, "high": price, "low": price, "close": price, "volume": max(vol_delta, 0)}
            return
        if current_bar_1570["datetime"] != bar_time:
            completed_bars_1570.append(current_bar_1570)
            current_bar_1570 = {"datetime": bar_time, "open": price, "high": price, "low": price, "close": price, "volume": max(vol_delta, 0)}
            return
        current_bar_1570["high"]   = max(current_bar_1570["high"], price)
        current_bar_1570["low"]    = min(current_bar_1570["low"], price)
        current_bar_1570["close"]  = price
        current_bar_1570["volume"] += max(vol_delta, 0)

    def completed_to_df_1570():
        """確定済みバーのみ"""
        if not completed_bars_1570:
            return None
        df = pd.DataFrame(completed_bars_1570)
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.drop_duplicates(subset=["datetime"], keep="last").sort_values("datetime").set_index("datetime")
        return df

    def save_csv_1570(df5):
        if df5 is None or df5.empty:
            return
        if os.path.exists(CSV_1570):
            ex = pd.read_csv(CSV_1570, index_col="datetime", parse_dates=True)
            df5 = pd.concat([ex, df5])
            df5 = df5[~df5.index.duplicated(keep="last")].sort_index()
        df5.to_csv(CSV_1570, encoding="utf-8-sig")

    def add_indicators(df):
        df = df.copy()
        df["ma20"]        = df["close"].rolling(20).mean()
        df["ma_slope"]    = df["ma20"] - df["ma20"].shift(1)
        df["tr"]          = df["high"] - df["low"]
        df["atr14"]       = df["tr"].rolling(14).mean()
        df["atr_avg"]     = df["atr14"].rolling(20).mean()
        df["bb_std"]      = df["close"].rolling(20).std()
        df["bb_width"]    = 4 * df["bb_std"]
        df["bb_width_avg"]= df["bb_width"].rolling(20).mean()
        df["bb_squeeze"]  = df["bb_width"] / df["bb_width_avg"]
        return df

    def check_signal(df):
        if len(df) < 30:
            return None
        i = len(df) - 1
        cur = df.iloc[i]; p1 = df.iloc[i-1]; p2 = df.iloc[i-2]
        if pd.isna(cur["ma_slope"]) or cur["ma_slope"] <= 0:
            return None
        if not pd.isna(cur["bb_squeeze"]) and not pd.isna(cur["atr14"]) and not pd.isna(cur["atr_avg"]):
            if cur["bb_squeeze"] < BB_SQ_TH or cur["atr14"] < cur["atr_avg"] * ATR_RATIO_TH:
                log("1570", f"  ⚡ レンジスキップ BB={cur['bb_squeeze']:.2f} ATR比={cur['atr14']/cur['atr_avg']:.2f}")
                return None
        if p2["high"] < p1["high"] and p2["low"] < p1["low"] and cur["close"] > p1["high"]:
            return "long"
        return None

    def entry_long():
        nonlocal position
        cp = current_price_1570()
        if not cp:
            log("1570", "❌ 現在値取得失敗")
            return
        log("1570", f"\n🟢 ロングエントリー @ {cp:.0f}円 × {LOT}枚")
        oid = send_order("buy", cp, LOT, "market")
        if not oid:
            return
        time.sleep(2)
        sl = cp - STOP; tp = cp + TP
        send_order("sell", sl, LOT, "stop", stop_price=sl)
        send_order("sell", tp, LOT, "limit", limit_price=tp)
        position = {"side": "long", "entry_time": datetime.now(JST), "entry_price": cp,
                    "sl_price": sl, "tp_price": tp, "qty": LOT, "order_id": oid, "bars": 0}

    def close_position(reason):
        nonlocal position, day_pnl, consec_loss
        if position is None:
            return
        cp = current_price_1570()
        if not cp:
            return
        log("1570", f"\n🔵 決済: {reason} @ {cp:.0f}円")
        send_order("sell", cp, position["qty"], "market")
        pnl = (cp - position["entry_price"]) * position["qty"]
        day_pnl += pnl
        consec_loss = consec_loss + 1 if pnl < 0 else 0
        trade_log.append({"entry_time": position["entry_time"].strftime("%Y-%m-%d %H:%M"),
                          "exit_time": datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
                          "entry_price": position["entry_price"], "exit_price": cp,
                          "pnl": pnl, "reason": reason})
        log("1570", f"  損益: {pnl:+.0f}円 | 累計: {day_pnl:+.0f}円")
        position = None
        if trade_log:
            fname = f"trade_log_{date.today().strftime('%Y%m%d')}.csv"
            pd.DataFrame(trade_log).to_csv(fname, index=False, encoding="utf-8-sig")

    while True:
        now     = datetime.now(JST)
        hhmm    = now.hour * 100 + now.minute
        weekday = now.weekday()

        if weekday >= 5:
            time.sleep(60)
            continue

        if hhmm >= 1500 and position:
            close_position("大引け強制決済")

        if hhmm >= 1530:
            log("1570", f"15:30終了 | 本日損益: {day_pnl:+.0f}円")
            save_csv_1570(completed_to_df_1570())
            # 翌日まで待機
            time.sleep(60 * 60)
            day_pnl = 0; consec_loss = 0
            current_bar_1570 = None; completed_bars_1570.clear()
            continue

        if hhmm < 900 or (1130 < hhmm < 1230):
            time.sleep(30)
            continue

        board = board_1570()
        if board and board.get("CurrentPrice"):
            cum_vol  = board.get("TradingVolume") or 0
            vol_diff = max(cum_vol - prev_cum_vol, 0)
            prev_cum_vol = cum_vol
            update_bar_1570(float(board["CurrentPrice"]), vol_diff)

        if now.minute != last_csv_1570_min:
            save_csv_1570(completed_to_df_1570())
            last_csv_1570_min = now.minute

        if position is not None:
            position["bars"] += 1
            if 1125 <= hhmm <= 1130:
                close_position("昼休み前強制決済")
                time.sleep(60 * 5)
                continue
            cp = current_price_1570()
            pnl_str = f"{cp - position['entry_price']:+.0f}円" if cp else "取得失敗"
            log("1570", f"ポジション保有中 ({position['bars']}本目) 含み損益: {pnl_str}")
            if position["bars"] >= BARS:
                close_position("時間決済")
            time.sleep(60)
            continue

        if hhmm < 915:
            time.sleep(60)
            continue

        current_5min = now.replace(second=0, microsecond=0, minute=(now.minute // 5) * 5)
        if last_signal_t == current_5min:
            time.sleep(30)
            continue

        if day_pnl <= -MAX_LOSS_DAY:
            log("1570", f"本日損失上限到達 ({day_pnl:+.0f}円) → 本日終了")
            time.sleep(60 * 30)
            continue

        if consec_loss >= MAX_CONSEC_LOSS:
            log("1570", f"{MAX_CONSEC_LOSS}連敗 → 本日終了")
            time.sleep(60 * 30)
            continue

        if hhmm >= 1300:
            time.sleep(60 * 5)
            continue

        log("1570", "シグナル判定中...")
        last_signal_t = current_5min
        try:
            df5 = completed_to_df_1570()

            if df5 is None or len(df5) < 30:
                log("1570", f"  ⚠ データ不足 ({len(completed_bars_1570)}本)")
                time.sleep(60)
                continue

            df5 = add_indicators(df5)
            sig = check_signal(df5)

            if sig == "long":
                log("1570", "  🟢 ロングシグナル！")
                try:
                    winsound.Beep(2000, 400)
                except Exception:
                    pass
                entry_long()
            else:
                cp = current_price_1570()
                log("1570", f"  → シグナルなし ({cp:.0f}円)" if cp else "  → シグナルなし")
        except Exception as e:
            log("1570", f"❌ シグナル判定エラー: {e}")
            import traceback
            traceback.print_exc()

        time.sleep(60)


# ══════════════════════════════════════════════════════
# マイクロ先物データ収集スレッド
# ══════════════════════════════════════════════════════
CSV_MICRO    = r"c:\kabu_trade\micro_5min.csv"
POLL_SEC     = 1.0

def run_micro():
    last_cum_volume = None
    current_bar     = None
    completed_bars  = []
    last_logged_min = -1
    last_csv_min    = -1

    log("MICRO", "📊 データ収集スレッド開始")

    def floor_5min(dt):
        return dt.replace(second=0, microsecond=0, minute=(dt.minute // 5) * 5)

    def is_trading_time(hhmm):
        if 845 <= hhmm < 1545:
            return True
        if hhmm >= 1700 or hhmm < 600:
            return True
        return False

    def start_bar(bar_time, price, vol):
        return {"datetime": bar_time, "open": price, "high": price, "low": price, "close": price, "volume": max(vol, 0)}

    def update_bar(board):
        nonlocal current_bar, last_cum_volume
        if not board:
            return
        price = board.get("CurrentPrice")
        cum_vol = board.get("TradingVolume")
        if price is None:
            return
        now      = datetime.now(JST)
        bar_time = floor_5min(now)
        price    = float(price)
        vol_delta = 0
        if cum_vol is not None:
            try:
                cum_vol = int(cum_vol)
                if last_cum_volume is not None and cum_vol >= last_cum_volume:
                    vol_delta = cum_vol - last_cum_volume
                last_cum_volume = cum_vol
            except Exception:
                pass
        if current_bar is None:
            current_bar = start_bar(bar_time, price, vol_delta)
            return
        if current_bar["datetime"] != bar_time:
            completed_bars.append(current_bar)
            current_bar = start_bar(bar_time, price, vol_delta)
            return
        current_bar["high"]   = max(current_bar["high"], price)
        current_bar["low"]    = min(current_bar["low"], price)
        current_bar["close"]  = price
        current_bar["volume"] += max(vol_delta, 0)

    def completed_to_df():
        """確定済みバーのみ（current_barは含めない）"""
        if not completed_bars:
            return None
        df = pd.DataFrame(completed_bars)
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.drop_duplicates(subset=["datetime"], keep="last").sort_values("datetime").set_index("datetime")
        return df

    def save_csv_micro():
        df = completed_to_df()
        if df is None or df.empty:
            return
        if os.path.exists(CSV_MICRO):
            ex = pd.read_csv(CSV_MICRO, parse_dates=["datetime"]).set_index("datetime")
            df = pd.concat([ex, df])
            df = df[~df.index.duplicated(keep="last")].sort_index()
        df.to_csv(CSV_MICRO, encoding="utf-8-sig")

    while True:
        now     = datetime.now(JST)
        hhmm    = now.hour * 100 + now.minute
        weekday = now.weekday()

        if weekday >= 5:
            time.sleep(60)
            continue

        if not is_trading_time(hhmm):
            time.sleep(30)
            continue

        board = api_get(f"/board/{MICRO_SYMBOL}@{MICRO_EXCHANGE}", "MICRO")
        if board and board.get("CurrentPrice"):
            update_bar(board)
            if now.minute % 5 == 0 and now.minute != last_logged_min:
                log("MICRO", f"現在値: {float(board['CurrentPrice']):,.0f}円")
                last_logged_min = now.minute

        if now.minute != last_csv_min:
            save_csv_micro()
            last_csv_min = now.minute

        time.sleep(POLL_SEC)


# ══════════════════════════════════════════════════════
# メイン
# ══════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 55)
    print("📊 統合起動: 1570自動売買 + マイクロ先物収集")
    print("=" * 55)

    # 初回認証（トークン取得のみ）
    if not _get_token():
        print("❌ トークン取得失敗")
        exit(1)

    time.sleep(0.5)
    fetch_micro_symbol()
    time.sleep(0.5)

    # 両銘柄を一括登録
    ok = _register_symbols([
        {"Symbol": "1570",        "Exchange": 1},
        {"Symbol": MICRO_SYMBOL,  "Exchange": MICRO_EXCHANGE},
    ])
    if ok:
        print(f"✅ 銘柄登録完了: 1570, {MICRO_SYMBOL}")
    else:
        print("❌ 銘柄登録失敗")
        exit(1)

    t1 = threading.Thread(target=run_1570,  name="1570",  daemon=True)
    t2 = threading.Thread(target=run_micro, name="MICRO", daemon=True)
    t1.start()
    t2.start()

    try:
        while t1.is_alive() or t2.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n⛔ 手動停止")
