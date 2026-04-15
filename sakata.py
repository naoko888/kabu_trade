"""
日経225マイクロ先物 総合アラートツール
kabuステーションAPI（リアルタイム）+ yfinance（テクニカル指標）

検出内容：
【リアルタイム・株コム】
- 出来高急増（1分足）
- 大陽線・大陰線（5分足・300円）
- 窓開け
- 日中高値更新・安値更新

【蓄積後（起動45分〜）・株コム5分足】
- 3連続陽線・陰線（150円）
- 高値切り上げ・安値切り上げ（逆も）
- 酒田五法
- ダブルトップ・ダブルボトム

【yfinance（遅延あり）】
- MACD全時間足揃い
- パーフェクトオーダー
- バンドウォーク
- BBエクスパンション・スクイーズ

実行方法：
  python sakata.py
"""

import requests
import yfinance as yf
import pandas as pd
import numpy as np
import time
from datetime import datetime
from collections import deque
import pytz
import winsound

JST = pytz.timezone("Asia/Tokyo")

# ══════════════════════════════════════════
# 設定
# ══════════════════════════════════════════

# kabuステーションAPI
API_BASE     = "http://localhost:18080/kabusapi"
API_PASSWORD = "sakimono35oku"
KABU_SYMBOL  = "161060023"   # 日経225マイクロ先物 26/06
KABU_EXCHANGE = 2             # 大阪

# yfinance（テクニカル指標用）
YF_SYMBOL = "NKD=F"

# シグナル設定
BIG_CANDLE_PT   = 300    # 大陽線・大陰線（円）
BIG3_CANDLE_PT  = 150    # 3連続陽線・陰線（円）
VOL_SURGE_TH    = 2.0    # 出来高急増倍率
GAP_TH          = 0.005  # 窓開け閾値（0.5%）
DOUBLE_THRESH   = 0.003  # ダブルトップ・ボトム（0.3%）
DOUBLE_LOOKBACK = 20     # ダブルトップ・ボトム遡り本数

BB_EXPAND_TH    = 1.3
BB_SQUEEZE_TH   = 0.8
MACD_FAST       = 12
MACD_SLOW       = 26
MACD_SIGNAL_LEN = 9
PO_MA_SHORT     = 5
PO_MA_MID       = 25
PO_MA_LONG      = 75
BANDWALK_BARS   = 3

MACD_TF_LIST  = ["5m", "15m", "30m", "60m", "2h", "4h", "8h"]
MACD_TF_LABEL = {
    "5m":"5分","15m":"15分","30m":"30分",
    "60m":"60分","2h":"2時間","4h":"4時間","8h":"8時間"
}

CHECK_INTERVAL = 60 * 5   # 5分ごと

# 5分足バッファ（最大100本）
candles_5m = deque(maxlen=100)
candles_1m = deque(maxlen=30)

# 前回の高値・安値（高値更新・安値更新検出用）
prev_day_high = None
prev_day_low  = None

# ══════════════════════════════════════════
# kabuステーション接続
# ══════════════════════════════════════════

token = None

def get_token():
    global token
    url  = f"{API_BASE}/token"
    res  = requests.post(url, json={"APIPassword": API_PASSWORD})
    if res.status_code == 200:
        token = res.json()["Token"]
        print(f"✅ kabuステーション接続成功")
        return True
    else:
        print(f"❌ トークン取得失敗: {res.text}")
        return False

def kabu_headers():
    return {"Content-Type": "application/json", "X-API-KEY": token}

def register_symbol():
    url  = f"{API_BASE}/register"
    body = {"Symbols": [{"Symbol": KABU_SYMBOL, "Exchange": KABU_EXCHANGE}]}
    res  = requests.put(url, headers=kabu_headers(), json=body)
    print(f"✅ 銘柄登録: {res.status_code}")

# ══════════════════════════════════════════
# kabuステーション リアルタイムデータ取得
# ══════════════════════════════════════════

def get_realtime():
    """現在の価格・出来高・高値・安値・始値を取得"""
    url = f"{API_BASE}/board/{KABU_SYMBOL}@{KABU_EXCHANGE}"
    res = requests.get(url, headers=kabu_headers())
    if res.status_code != 200:
        return None
    d = res.json()
    return {
        "price":       d.get("CurrentPrice"),
        "volume":      d.get("TradingVolume"),
        "high":        d.get("HighPrice"),
        "low":         d.get("LowPrice"),
        "open":        d.get("OpeningPrice"),
        "prev_close":  d.get("PreviousClose"),
        "time":        datetime.now(JST),
    }

# ══════════════════════════════════════════
# 5分足バッファ更新
# ══════════════════════════════════════════

last_5m_time = None
current_5m_candle = None

def update_5m_candle(rt):
    """リアルタイムデータから5分足を蓄積"""
    global last_5m_time, current_5m_candle

    now   = rt["time"]
    # 5分区切り（例：9:00, 9:05, 9:10...）
    slot  = now.replace(second=0, microsecond=0)
    slot  = slot.replace(minute=(now.minute // 5) * 5)

    if last_5m_time is None:
        last_5m_time = slot
        current_5m_candle = {
            "time":   slot,
            "open":   rt["price"],
            "high":   rt["price"],
            "low":    rt["price"],
            "close":  rt["price"],
            "volume": rt["volume"] or 0,
        }
        return

    if slot > last_5m_time:
        # 前の足を確定してバッファに追加
        if current_5m_candle:
            candles_5m.append(current_5m_candle)
        # 新しい足を開始
        current_5m_candle = {
            "time":   slot,
            "open":   rt["price"],
            "high":   rt["price"],
            "low":    rt["price"],
            "close":  rt["price"],
            "volume": rt["volume"] or 0,
        }
        last_5m_time = slot
    else:
        # 同じ足を更新
        if rt["price"]:
            current_5m_candle["high"]  = max(current_5m_candle["high"], rt["price"])
            current_5m_candle["low"]   = min(current_5m_candle["low"],  rt["price"])
            current_5m_candle["close"] = rt["price"]
        if rt["volume"]:
            current_5m_candle["volume"] = rt["volume"]

def get_5m_df():
    """蓄積した5分足をDataFrameに変換（確定済み足のみ）"""
    if len(candles_5m) == 0:
        return None
    df = pd.DataFrame(list(candles_5m))
    df = df.set_index("time")
    return df

# ══════════════════════════════════════════
# yfinanceデータ取得（テクニカル指標用）
# ══════════════════════════════════════════

def get_yf_df(interval, period):
    df = yf.download(YF_SYMBOL, interval=interval, period=period, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns={"Open":"open","High":"high","Low":"low",
                             "Close":"close","Volume":"volume"})
    return df.dropna()

def resample_df(df_1h, hours):
    df = df_1h.resample(f"{hours}h").agg({
        "open":"first","high":"max","low":"min",
        "close":"last","volume":"sum"
    }).dropna()
    return df

def get_yf_all():
    data = {}
    try:
        data["5m"]  = get_yf_df("5m",  "60d")
        data["15m"] = get_yf_df("15m", "60d")
        data["30m"] = get_yf_df("30m", "60d")
        data["60m"] = get_yf_df("60m", "730d")
        data["1d"]  = get_yf_df("1d",  "max")
        df_1h       = data["60m"].copy()
        data["2h"]  = resample_df(df_1h, 2)
        data["4h"]  = resample_df(df_1h, 4)
        data["8h"]  = resample_df(df_1h, 8)
    except Exception as e:
        print(f"  ⚠️ yfinanceエラー: {e}")
    return data

# ══════════════════════════════════════════
# リアルタイム系シグナル検出
# ══════════════════════════════════════════

def detect_gap(rt):
    """窓開け検出"""
    alerts = []
    if not rt["open"] or not rt["prev_close"]:
        return alerts
    gap = (rt["open"] - rt["prev_close"]) / rt["prev_close"]
    if abs(gap) >= GAP_TH:
        direction = "上" if gap > 0 else "下"
        emoji     = "⬆️🕳️" if gap > 0 else "⬇️🕳️"
        alerts.append({
            "type":"GAP","emoji":emoji,
            "signal":f"窓開け{direction}昇",
            "detail":f"窓開け{direction}昇 {gap*100:+.1f}%",
            "price":rt["open"],
            "desc":f"前日終値{rt['prev_close']:,.0f}から{abs(gap*100):.1f}%の窓開け{direction}昇",
            "tags":"#窓開け #ギャップ"
        })
    return alerts

def detect_high_low_update(rt):
    """日中高値更新・安値更新検出"""
    global prev_day_high, prev_day_low
    alerts = []
    price = rt["price"]
    high  = rt["high"]
    low   = rt["low"]
    if not price or not high or not low:
        return alerts

    if prev_day_high is None:
        prev_day_high = high
        prev_day_low  = low
        return alerts

    if high > prev_day_high:
        alerts.append({
            "type":"DAY_HIGH","emoji":"🔺",
            "signal":"日中高値更新",
            "detail":f"日中高値更新 {high:,.0f}円",
            "price":price,
            "desc":f"本日高値を更新（{prev_day_high:,.0f}→{high:,.0f}円）",
            "tags":"#高値更新 #上昇"
        })
        prev_day_high = high

    if low < prev_day_low:
        alerts.append({
            "type":"DAY_LOW","emoji":"🔻",
            "signal":"日中安値更新",
            "detail":f"日中安値更新 {low:,.0f}円",
            "price":price,
            "desc":f"本日安値を更新（{prev_day_low:,.0f}→{low:,.0f}円）",
            "tags":"#安値更新 #下落"
        })
        prev_day_low = low

    return alerts

def detect_vol_surge_rt(rt):
    """出来高急増（リアルタイム・1分足蓄積）"""
    alerts = []
    if not rt["volume"]:
        return alerts

    candles_1m.append(rt["volume"])
    if len(candles_1m) < 22:
        return alerts

    vol_now = candles_1m[-1]
    vol_avg = np.mean(list(candles_1m)[-21:-1])
    if vol_avg == 0:
        return alerts

    # 中央値の10%未満を除外
    series = list(candles_1m)[-21:-1]
    median = np.median(series)
    filtered = [v for v in series if v >= median * 0.1]
    if len(filtered) < 5:
        return alerts
    vol_avg = np.mean(filtered)

    ratio = vol_now / vol_avg
    if ratio >= VOL_SURGE_TH:
        alerts.append({
            "type":"VOL_SURGE","emoji":"📊",
            "signal":"出来高急増",
            "detail":f"出来高急増（1分）{ratio:.1f}倍",
            "price":rt["price"],
            "desc":f"1分足の出来高が平均の{ratio:.1f}倍に急増。大きな動きに注意",
            "tags":"#出来高 #急増"
        })
    return alerts

# ══════════════════════════════════════════
# 5分足蓄積系シグナル検出
# ══════════════════════════════════════════

def detect_big_candle(df):
    """大陽線・大陰線（300円）"""
    alerts = []
    if len(df) < 2:
        return alerts
    c0    = df.iloc[-1]
    price = float(c0["close"])
    body  = abs(c0["close"] - c0["open"])

    if c0["close"] > c0["open"] and body >= BIG_CANDLE_PT:
        alerts.append({
            "type":"BIG_BULL","emoji":"🚀",
            "signal":"大陽線",
            "detail":f"大陽線（5分・{body:.0f}円）",
            "price":price,
            "desc":f"{body:.0f}円の大陽線。強い上昇圧力",
            "tags":"#大陽線 #急騰"
        })
    if c0["close"] < c0["open"] and body >= BIG_CANDLE_PT:
        alerts.append({
            "type":"BIG_BEAR","emoji":"💣",
            "signal":"大陰線",
            "detail":f"大陰線（5分・{body:.0f}円）",
            "price":price,
            "desc":f"{body:.0f}円の大陰線。強い下落圧力",
            "tags":"#大陰線 #急落"
        })
    return alerts

def detect_big3(df):
    """3連続陽線・陰線（150円）"""
    alerts = []
    if len(df) < 3:
        return alerts
    c0=df.iloc[-1]; c1=df.iloc[-2]; c2=df.iloc[-3]
    price = float(c0["close"])

    def is_big_bull(r):
        return r["close"] > r["open"] and abs(r["close"]-r["open"]) >= BIG3_CANDLE_PT
    def is_big_bear(r):
        return r["close"] < r["open"] and abs(r["close"]-r["open"]) >= BIG3_CANDLE_PT

    if is_big_bull(c0) and is_big_bull(c1) and is_big_bull(c2):
        if c0["close"] > c1["close"] > c2["close"]:
            alerts.append({
                "type":"BIG3_BULL","emoji":"🔥🔥🔥",
                "signal":"強烈な上昇継続",
                "detail":f"大きめ陽線3本連続（5分・{BIG3_CANDLE_PT}円以上）",
                "price":price,
                "desc":f"{BIG3_CANDLE_PT}円以上の陽線が3本連続。強い上昇モメンタム",
                "tags":"#大陽線 #上昇継続"
            })
    if is_big_bear(c0) and is_big_bear(c1) and is_big_bear(c2):
        if c0["close"] < c1["close"] < c2["close"]:
            alerts.append({
                "type":"BIG3_BEAR","emoji":"💥💥💥",
                "signal":"強烈な下落継続",
                "detail":f"大きめ陰線3本連続（5分・{BIG3_CANDLE_PT}円以上）",
                "price":price,
                "desc":f"{BIG3_CANDLE_PT}円以上の陰線が3本連続。強い下落モメンタム",
                "tags":"#大陰線 #下落継続"
            })
    return alerts

def detect_highlow_shift(df):
    """高値切り上げ・安値切り上げ（逆も）"""
    alerts = []
    if len(df) < 3:
        return alerts
    c0=df.iloc[-1]; c1=df.iloc[-2]; c2=df.iloc[-3]
    price = float(c0["close"])

    # 高値切り上げ＋安値切り上げ（上昇トレンド確認）
    if c0["high"] > c1["high"] > c2["high"] and c0["low"] > c1["low"] > c2["low"]:
        alerts.append({
            "type":"HL_UP","emoji":"📈",
            "signal":"高値・安値切り上げ",
            "detail":"高値・安値切り上げ（5分）",
            "price":price,
            "desc":"高値・安値ともに切り上げ。上昇トレンド継続中",
            "tags":"#上昇トレンド #切り上げ"
        })
    # 高値切り下げ＋安値切り下げ（下落トレンド確認）
    if c0["high"] < c1["high"] < c2["high"] and c0["low"] < c1["low"] < c2["low"]:
        alerts.append({
            "type":"HL_DOWN","emoji":"📉",
            "signal":"高値・安値切り下げ",
            "detail":"高値・安値切り下げ（5分）",
            "price":price,
            "desc":"高値・安値ともに切り下げ。下落トレンド継続中",
            "tags":"#下落トレンド #切り下げ"
        })
    return alerts

def detect_double(df):
    """ダブルトップ・ダブルボトム"""
    alerts = []
    if len(df) < DOUBLE_LOOKBACK + 5:
        return alerts
    recent    = df.iloc[-DOUBLE_LOOKBACK:]
    price     = float(df["close"].iloc[-1])
    cur_high  = float(df["high"].iloc[-1])
    cur_low   = float(df["low"].iloc[-1])
    prev_high = float(recent["high"].iloc[:-3].max())
    prev_low  = float(recent["low"].iloc[:-3].min())

    if abs(cur_high - prev_high) / prev_high < DOUBLE_THRESH:
        alerts.append({
            "type":"DOUBLE_TOP","emoji":"🔝🔝",
            "signal":"下落転換の可能性",
            "detail":"ダブルトップ（5分）",
            "price":price,
            "desc":f"同水準の高値を2度付け。天井圏での反転サイン（高値:{prev_high:,.0f}円付近）",
            "tags":"#ダブルトップ #天井 #転換"
        })
    if abs(cur_low - prev_low) / prev_low < DOUBLE_THRESH:
        alerts.append({
            "type":"DOUBLE_BOTTOM","emoji":"⬇️⬇️",
            "signal":"上昇転換の可能性",
            "detail":"ダブルボトム（5分）",
            "price":price,
            "desc":f"同水準の安値を2度付け。底値圏での反転サイン（安値:{prev_low:,.0f}円付近）",
            "tags":"#ダブルボトム #底値 #転換"
        })
    return alerts

# 酒田五法
def bsize(r):  return abs(r["close"]-r["open"])
def uwi(r):    return r["high"]-max(r["open"],r["close"])
def lwi(r):    return min(r["open"],r["close"])-r["low"]
def crange(r): return r["high"]-r["low"]
def bull(r):   return r["close"]>r["open"]
def bear(r):   return r["close"]<r["open"]
def doji(r):
    rng = crange(r)
    return rng > 0 and bsize(r)/rng < 0.1

def detect_sakata(df):
    alerts = []
    if len(df) < 3:
        return alerts
    c0=df.iloc[-1]; c1=df.iloc[-2]; c2=df.iloc[-3]
    price = float(c0["close"])
    pats  = []

    if bull(c0) and bsize(c0) >= BIG_CANDLE_PT:
        pats.append(("大陽線","🚀","強気","強烈な上昇圧力","#大陽線 #急騰"))
    if bear(c0) and bsize(c0) >= BIG_CANDLE_PT:
        pats.append(("大陰線","💣","弱気","強烈な下落圧力","#大陰線 #急落"))
    if doji(c0):
        pats.append(("十字線","✝️","中立・転換注意","買い売りが拮抗。トレンド転換の可能性","#十字線"))
    r=crange(c0)
    if r>0 and bull(c0) and not doji(c0) and lwi(c0)>=bsize(c0)*2 and lwi(c0)>=r*0.5:
        pats.append(("カラカサ","☂️","反転上昇の可能性","下値での強い買い戻し","#カラカサ #反転"))
    if r>0 and bear(c0) and not doji(c0) and uwi(c0)>=bsize(c0)*2 and uwi(c0)>=r*0.5:
        pats.append(("射撃線","🎯","反転下落の可能性","上値での強い売り圧力","#射撃線 #反転"))
    if bear(c1) and bull(c0) and c0["open"]<=c1["close"] and c0["close"]>=c1["open"]:
        pats.append(("陽の包み足","🌅","強気反転","前足の陰線を完全に包む陽線","#包み足 #強気反転"))
    if bull(c1) and bear(c0) and c0["open"]>=c1["close"] and c0["close"]<=c1["open"]:
        pats.append(("陰の包み足","🌇","弱気反転","前足の陽線を完全に包む陰線","#包み足 #弱気反転"))
    if (bear(c2) and bsize(c1)<bsize(c2)*0.5 and c1["close"]<c2["close"] and
            bull(c0) and c0["close"]>=(c2["open"]+c2["close"])/2):
        pats.append(("明けの明星","⭐","強気反転","底値圏での強力な反転サイン","#明けの明星 #三川"))
    if (bull(c2) and bsize(c1)<bsize(c2)*0.5 and c1["close"]>c2["close"] and
            bear(c0) and c0["close"]<=(c2["open"]+c2["close"])/2):
        pats.append(("宵の明星","🌙","弱気反転","天井圏での強力な反転サイン","#宵の明星 #三川"))
    if (bull(c2) and bull(c1) and bull(c0) and
            c1["close"]>c2["close"] and c0["close"]>c1["close"] and uwi(c0)<bsize(c0)*0.3):
        pats.append(("赤三兵","🔴🔴🔴","上昇継続","3本連続の強い陽線","#赤三兵 #上昇継続"))
    if (bear(c2) and bear(c1) and bear(c0) and
            c1["close"]<c2["close"] and c0["close"]<c1["close"] and lwi(c0)<bsize(c0)*0.3):
        pats.append(("三羽烏","🐦🐦🐦","下落継続","3本連続の強い陰線","#三羽烏 #下落継続"))

    for name,emoji,signal,desc,tags in pats:
        alerts.append({
            "type":"SAKATA","emoji":emoji,"signal":signal,
            "detail":f"{name}（5分・株コム）","price":price,
            "desc":desc,"tags":f"#酒田五法 {tags}"
        })
    return alerts

# ══════════════════════════════════════════
# yfinance系シグナル検出
# ══════════════════════════════════════════

def calc_macd(df):
    ema_f = df["close"].ewm(span=MACD_FAST, adjust=False).mean()
    ema_s = df["close"].ewm(span=MACD_SLOW, adjust=False).mean()
    macd  = ema_f - ema_s
    sig   = macd.ewm(span=MACD_SIGNAL_LEN, adjust=False).mean()
    return macd, sig

def calc_bb(df, length=20, mult=2.0):
    mid   = df["close"].rolling(length).mean()
    std   = df["close"].rolling(length).std()
    upper = mid + mult * std
    lower = mid - mult * std
    width = 2 * mult * std
    width_avg = width.rolling(length).mean()
    squeeze   = width / width_avg
    return upper, lower, mid, squeeze

def get_macd_state(df):
    if len(df) < MACD_SLOW + MACD_SIGNAL_LEN + 2:
        return None
    macd, sig = calc_macd(df)
    c0_m=macd.iloc[-1]; c1_m=macd.iloc[-2]
    c0_s=sig.iloc[-1];  c1_s=sig.iloc[-2]
    if c1_m <= c1_s and c0_m > c0_s: return "GC"
    if c1_m >= c1_s and c0_m < c0_s: return "DC"
    return None

def detect_macd_all(data, price):
    alerts = []
    states = {}
    for tf in MACD_TF_LIST:
        if tf not in data or len(data[tf]) == 0:
            return alerts
        states[tf] = get_macd_state(data[tf])

    labels = " / ".join([MACD_TF_LABEL[tf] for tf in MACD_TF_LIST])

    if all(states[tf] == "GC" for tf in MACD_TF_LIST):
        alerts.append({
            "type":"MACD_ALL_GC","emoji":"🟢🟢🟢",
            "signal":"超強力な買いシグナル",
            "detail":f"MACD 全時間足GC（{labels}）","price":price,
            "desc":"全時間足でMACDが同時にゴールデンクロス！極めて強い買いサイン",
            "tags":"#MACD #ゴールデンクロス #全時間足揃い"
        })
    elif all(states[tf] == "DC" for tf in MACD_TF_LIST):
        alerts.append({
            "type":"MACD_ALL_DC","emoji":"🔴🔴🔴",
            "signal":"超強力な売りシグナル",
            "detail":f"MACD 全時間足DC（{labels}）","price":price,
            "desc":"全時間足でMACDが同時にデッドクロス！極めて強い売りサイン",
            "tags":"#MACD #デッドクロス #全時間足揃い"
        })
    else:
        gc_count = sum(1 for tf in MACD_TF_LIST if states[tf] == "GC")
        dc_count = sum(1 for tf in MACD_TF_LIST if states[tf] == "DC")
        gc_tfs   = [MACD_TF_LABEL[tf] for tf in MACD_TF_LIST if states[tf] == "GC"]
        dc_tfs   = [MACD_TF_LABEL[tf] for tf in MACD_TF_LIST if states[tf] == "DC"]
        if gc_count >= 5:
            alerts.append({
                "type":"MACD_ALMOST_GC","emoji":"🟡",
                "signal":f"GC揃いかけ（{gc_count}/{len(MACD_TF_LIST)}）",
                "detail":f"MACD GC {gc_count}時間足揃い","price":price,
                "desc":f"GC済み：{' / '.join(gc_tfs)}\n残り{len(MACD_TF_LIST)-gc_count}本でフル揃い",
                "tags":"#MACD #ゴールデンクロス #揃いかけ"
            })
        elif dc_count >= 5:
            alerts.append({
                "type":"MACD_ALMOST_DC","emoji":"🟠",
                "signal":f"DC揃いかけ（{dc_count}/{len(MACD_TF_LIST)}）",
                "detail":f"MACD DC {dc_count}時間足揃い","price":price,
                "desc":f"DC済み：{' / '.join(dc_tfs)}\n残り{len(MACD_TF_LIST)-dc_count}本でフル揃い",
                "tags":"#MACD #デッドクロス #揃いかけ"
            })
    return alerts

def detect_perfect_order(df, tf_label, price):
    alerts = []
    need = max(PO_MA_SHORT, PO_MA_MID, PO_MA_LONG) + 2
    if len(df) < need: return alerts

    ma_s = df["close"].rolling(PO_MA_SHORT).mean()
    ma_m = df["close"].rolling(PO_MA_MID).mean()
    ma_l = df["close"].rolling(PO_MA_LONG).mean()

    s0=ma_s.iloc[-1]; m0=ma_m.iloc[-1]; l0=ma_l.iloc[-1]
    s1=ma_s.iloc[-2]; m1=ma_m.iloc[-2]; l1=ma_l.iloc[-2]

    if pd.isna(s0) or pd.isna(m0) or pd.isna(l0): return alerts

    if s0>m0>l0 and not (s1>m1>l1):
        alerts.append({
            "type":"PERFECT_ORDER","emoji":"🏆",
            "signal":"パーフェクトオーダー成立",
            "detail":f"パーフェクトオーダー（{tf_label}）","price":price,
            "desc":f"{tf_label}足でパーフェクトオーダー成立（{PO_MA_SHORT}MA＞{PO_MA_MID}MA＞{PO_MA_LONG}MA）",
            "tags":"#パーフェクトオーダー #上昇トレンド"
        })
    if s0<m0<l0 and not (s1<m1<l1):
        alerts.append({
            "type":"REVERSE_PO","emoji":"💀",
            "signal":"逆パーフェクトオーダー成立",
            "detail":f"逆パーフェクトオーダー（{tf_label}）","price":price,
            "desc":f"{tf_label}足で逆パーフェクトオーダー成立（{PO_MA_SHORT}MA＜{PO_MA_MID}MA＜{PO_MA_LONG}MA）",
            "tags":"#逆パーフェクトオーダー #下落トレンド"
        })
    return alerts

def detect_bandwalk(df, price):
    alerts = []
    if len(df) < 25 + BANDWALK_BARS: return alerts
    upper, lower, mid, _ = calc_bb(df)

    walk_up  = all(df["close"].iloc[-i] >= upper.iloc[-i] * 0.99 for i in range(1, BANDWALK_BARS+1))
    prev_up  = df["close"].iloc[-(BANDWALK_BARS+1)] >= upper.iloc[-(BANDWALK_BARS+1)] * 0.99
    if walk_up and not prev_up:
        alerts.append({
            "type":"BANDWALK_UP","emoji":"🚀📈",
            "signal":"上昇バンドウォーク",
            "detail":"バンドウォーク上昇（日足）","price":price,
            "desc":f"日足がBB上バンドに沿って{BANDWALK_BARS}本連続上昇",
            "tags":"#バンドウォーク #上昇トレンド"
        })

    walk_dn  = all(df["close"].iloc[-i] <= lower.iloc[-i] * 1.01 for i in range(1, BANDWALK_BARS+1))
    prev_dn  = df["close"].iloc[-(BANDWALK_BARS+1)] <= lower.iloc[-(BANDWALK_BARS+1)] * 1.01
    if walk_dn and not prev_dn:
        alerts.append({
            "type":"BANDWALK_DOWN","emoji":"💣📉",
            "signal":"下落バンドウォーク",
            "detail":"バンドウォーク下落（日足）","price":price,
            "desc":f"日足がBB下バンドに沿って{BANDWALK_BARS}本連続下落",
            "tags":"#バンドウォーク #下落トレンド"
        })
    return alerts

def detect_bb_60(df, price):
    alerts = []
    if len(df) < 40: return alerts
    _, _, _, squeeze = calc_bb(df)
    sq_now  = squeeze.iloc[-1]
    sq_prev = squeeze.iloc[-2]
    if pd.isna(sq_now) or pd.isna(sq_prev): return alerts

    if sq_prev < BB_SQUEEZE_TH and sq_now >= BB_EXPAND_TH:
        alerts.append({
            "type":"BB_EXPAND","emoji":"💥",
            "signal":"大きな動き開始の可能性",
            "detail":"BBエクスパンション（60分）","price":price,
            "desc":"ボリンジャーバンドがスクイーズから急拡大",
            "tags":"#ボリンジャーバンド #エクスパンション"
        })
    elif sq_now < BB_SQUEEZE_TH and sq_prev >= BB_SQUEEZE_TH:
        alerts.append({
            "type":"BB_SQUEEZE","emoji":"🔋",
            "signal":"エネルギー蓄積中",
            "detail":"BBスクイーズ（60分）","price":price,
            "desc":"ボリンジャーバンドが収縮中。大きな動きの前兆の可能性",
            "tags":"#ボリンジャーバンド #スクイーズ"
        })
    return alerts

# ══════════════════════════════════════════
# 投稿文生成
# ══════════════════════════════════════════

def make_post(alert, now):
    return f"""{alert['emoji']}【日経先物アラート】{now.strftime('%m/%d %H:%M')}

📊 {alert['detail']}
💹 現在値：{alert['price']:,.0f}円
📈 シグナル：{alert['signal']}

{alert['desc']}

#日経先物 #日経平均 #テクニカル分析 {alert['tags']}"""

# ══════════════════════════════════════════
# メインループ
# ══════════════════════════════════════════

def main():
    global prev_day_high, prev_day_low

    print("="*55)
    print("🕯️  日経225マイクロ先物 総合アラートツール 起動")
    print(f"   大陽線・大陰線基準：{BIG_CANDLE_PT}円以上")
    print(f"   陽線3本連続基準：{BIG3_CANDLE_PT}円以上")
    print(f"   出来高急増倍率：{VOL_SURGE_TH}倍")
    print("   Ctrl+C で終了")
    print("="*55)

    # kabuステーション接続
    if not get_token():
        print("❌ 起動失敗")
        return
    register_symbol()
    time.sleep(2)

    detected_log = set()
    gap_checked  = False   # 窓開けは1日1回
    yf_data      = {}
    yf_last_fetch = None

    while True:
        now     = datetime.now(JST)
        hhmm    = now.hour * 100 + now.minute
        weekday = now.weekday()

        if weekday >= 5:
            print("土日のため待機中...")
            time.sleep(60 * 30)
            continue

        if not (830 <= hhmm <= 1600):
            time.sleep(60 * 5)
            continue

        today_key = now.strftime("%Y%m%d")

        # 日付が変わったらリセット
        if gap_checked and now.strftime("%Y%m%d") not in detected_log:
            gap_checked   = False
            prev_day_high = None
            prev_day_low  = None
            detected_log  = set()

        print(f"\n[{now.strftime('%H:%M')}] チェック中...")

        try:
            # ── リアルタイムデータ取得 ──
            rt = get_realtime()
            if rt is None or rt["price"] is None:
                print("  ⚠️ リアルタイムデータ取得失敗")
                time.sleep(CHECK_INTERVAL)
                continue

            print(f"  💹 現在値：{rt['price']:,.0f}円")

            # 5分足バッファ更新
            update_5m_candle(rt)
            df_5m = get_5m_df()

            all_alerts = []

            # ── リアルタイム系 ──
            # 出来高急増（1分蓄積）
            all_alerts += detect_vol_surge_rt(rt)

            # 窓開け（1日1回）
            if not gap_checked:
                all_alerts += detect_gap(rt)
                gap_checked = True

            # 日中高値・安値更新
            all_alerts += detect_high_low_update(rt)

            # ── 5分足蓄積系 ──
            if df_5m is not None and len(df_5m) >= 1:
                all_alerts += detect_big_candle(df_5m)

            if df_5m is not None and len(df_5m) >= 3:
                all_alerts += detect_big3(df_5m)
                all_alerts += detect_highlow_shift(df_5m)
                all_alerts += detect_sakata(df_5m)

            if df_5m is not None and len(df_5m) >= DOUBLE_LOOKBACK + 5:
                all_alerts += detect_double(df_5m)

            # ── yfinance系（30分に1回だけ取得）──
            if yf_last_fetch is None or (now - yf_last_fetch).seconds >= 1800:
                print("  📡 yfinanceデータ取得中...")
                yf_data = get_yf_all()
                yf_last_fetch = now

            price = rt["price"]
            if yf_data:
                all_alerts += detect_macd_all(yf_data, price)
                for tf, label in [("5m","5分"),("15m","15分"),("1d","日足")]:
                    if tf in yf_data:
                        all_alerts += detect_perfect_order(yf_data[tf], label, price)
                if "1d" in yf_data:
                    all_alerts += detect_bandwalk(yf_data["1d"], price)
                if "60m" in yf_data:
                    all_alerts += detect_bb_60(yf_data["60m"], price)

            # ── 通知 ──
            if all_alerts:
                for a in all_alerts:
                    key = f"{today_key}_{now.strftime('%H%M')}_{a['detail']}"
                    if key in detected_log:
                        continue
                    detected_log.add(key)
                    winsound.Beep(1000, 500)

                    print(f"\n{'='*55}")
                    print(f"  {a['emoji']} {a['detail']}")
                    print(f"  シグナル：{a['signal']}")
                    print(f"{'='*55}")
                    print("\n📋 コピペ用投稿文：\n")
                    print("-"*55)
                    print(make_post(a, now))
                    print("-"*55)
            else:
                print(f"  → 検出なし（5分足蓄積数：{len(df_5m) if df_5m is not None else 0}本）")

        except Exception as e:
            print(f"  ❌ エラー: {e}")
            # トークン切れの場合は再取得
            if "401" in str(e) or "token" in str(e).lower():
                get_token()

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n⛔ 手動停止")