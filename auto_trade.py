import os
import time
import requests
import pandas as pd
import numpy as np
import pytz
from datetime import datetime
from pathlib import Path

# =========================
# 設定
# =========================
API_BASE = "http://localhost:18080/kabusapi"
API_PASSWORD = "sakimono35oku"

# 1570 ETF（変更なし）
SYMBOL   = "1570"
EXCHANGE = 1
LOT      = 1
STOP     = 100
TP       = 250
BARS     = 4

MAX_LOSS_DAY    = 300
MAX_CONSEC_LOSS = 3
BB_SQ_TH        = 0.90
ATR_RATIO_TH    = 0.70
CSV_FILE         = "1570_5min.csv"

# マイクロ先物（dual_signal_bot 統合後パラメータ）
MICRO_TP       = 240          # 旧120 → 系統①②共通
MICRO_SL       = 60           # 旧40  → 系統①②共通
MICRO_LOT      = 1            # マイクロ先物発注枚数（1570の LOT とは独立）
MICRO_CSV_FILE = "micro_5min.csv"
MICRO_DERIV_MONTH   = "202606"
MICRO_CSV_WARMUP    = Path(r"C:\kabu_trade\micro_5min.csv")  # 過去足ウォームアップ用
MICRO_WARMUP_BARS   = 300          # ウォームアップ使用本数

# シグナル判定定数（dual_signal_bot より）
TOUCH_PCT = 0.005   # MAタッチ判定 ±0.5%

# ===== 系統④：逆張りロング =====
SYS4_MOVE_PCT      = 0.002    # 直前1本比 -0.2%以上の下落
SYS4_RSI_TH        = 40       # RSI14 <= 40
SYS4_VOL_TH        = 0.8      # vol_ratio >= 0.8
SYS4_LOOKBACK      = 1        # 何本前との比較か
SYS4_RECOVERY_PCT  = 0.002    # ひげ戻り率 >= 0.2%（(close-low)/close）
SYS4_EXCLUDE_HOURS = {19}     # 除外時間帯（19時は全負けのため除外）
SYS4_TP            = 120      # 利確幅（pt）
SYS4_SL            = 60       # 損切幅（pt）
SYS4_MAX_HOLD      = 6        # 最大保有足数
SYS4_DD_LIMIT_YEN  = -3500    # 累積DD上限（円）。超えたら停止。

POLL_SEC = 1.0
DRY_RUN       = True   # True=1570もDRY（注文なし）
MICRO_DRY_RUN = True   # True=マイクロ先物もDRY（1570と独立して制御可能）

LOG_DIR = Path(r"C:\kabu_trade\logs")  # 累積ログ保存先

JST = pytz.timezone("Asia/Tokyo")

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

def is_trading_time(hhmm: int) -> bool:
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

# ─ 米国サマータイム（DST）判定 ─
_DST_PERIODS = [
    ("2023-03-12", "2023-11-05"),
    ("2024-03-10", "2024-11-03"),
    ("2025-03-09", "2025-11-02"),
    ("2026-03-08", "2026-11-01"),
]

def is_dst(dt: datetime) -> bool:
    """dt が米国サマータイム期間内かどうか判定（JST naive datetime を渡すこと）"""
    ts = pd.Timestamp(dt)
    for start, end in _DST_PERIODS:
        if pd.Timestamp(start) <= ts <= pd.Timestamp(end):
            return True
    return False

# ─ 米CPI フィルター ─
def load_cpi_events(csv_path: str = r"C:\kabu_trade\economic_calendar.csv") -> pd.DataFrame:
    """economic_calendar.csv から米CPI 発表日時を読み込む。
    ファイルが存在しない・壊れている場合は空のDataFrameを返して続行する。
    """
    try:
        df = pd.read_csv(csv_path)
        df["release_datetime_jst"] = pd.to_datetime(df["release_datetime_jst"])
        result = df[df["indicator"] == "米CPI"].reset_index(drop=True)
        print(f"[OK] CPIカレンダー読み込み成功: {len(result)}件")
        return result
    except Exception as e:
        print(f"[WARN] CPIカレンダー読み込み失敗: {e} → CPI除外無効で続行")
        return pd.DataFrame(columns=["indicator", "release_datetime_jst"])

def is_cpi_window(dt: datetime, cpi_df: pd.DataFrame,
                  before_min: int = 30, after_min: int = 60) -> bool:
    """dt が CPI 発表の30分前〜60分後ウィンドウ内かどうか判定"""
    from datetime import timedelta
    ts = pd.Timestamp(dt)
    for _, row in cpi_df.iterrows():
        release = row["release_datetime_jst"]
        if (release - timedelta(minutes=before_min)) <= ts <= (release + timedelta(minutes=after_min)):
            return True
    return False

# =========================
# グローバル状態
# =========================
# 系統③用: 起動時に一度だけ読み込む
cpi_df = load_cpi_events()

token    = None
position = None
day_pnl  = 0
consec_loss = 0
trade_log   = []
signal_log  = []
consecutive_auth_errors = 0
MAX_AUTH_ERRORS  = 5
last_reauth_time = 0
REAUTH_COOLDOWN  = 10

# 1570 5分足
current_bar    = None
completed_bars = []
last_cum_volume = None
last_price      = None
last_signal_bar_time = None

# 1570 DRY
dry_position  = None
dry_day_pnl   = 0
dry_trade_log = []

# マイクロ先物
MICRO_SYMBOL   = None
MICRO_EXCHANGE = None

micro_current_bar    = None
micro_completed_bars = []
micro_last_cum_vol   = None
micro_last_signal_bar_time = None

# マイクロ DRY（系統①②③共通・リスト管理）
micro_dry_positions  = []   # 全系統のオープンポジション一覧
micro_dry_day_pnl    = 0
micro_dry_trade_log  = []

# 系統①③合算 月次ドローダウン制限
MICRO_MONTHLY_DD_LIMIT  = -30_000  # 月次損失制限（円）
MICRO_PT_TO_YEN         = 10       # 1pt = ¥10（マイクロ先物）
MICRO_COMMISSION_YEN    = 22       # 往復手数料（円）= 2.2pt × ¥10
micro_monthly_pnl       = 0.0      # 系統①③の月間累計損益（円）
micro_monthly_skip      = False    # 月次制限フラグ（True=今月のエントリーをスキップ）
micro_current_month     = None     # 現在の年月 (year, month)

# マイクロ先物 インジケーターウォームアップカウンター
# CSV停止期間があった場合、起動後26本は誤シグナルを防ぐためエントリーをスキップ
micro_warmup_remaining = 0   # 0 = 通常稼働

# 系統④ 状態管理
sys4_monthly_pnl_yen      = 0.0   # 系統④ 月次累積損益（円）。毎月1日リセット。
sys4_stopped              = False  # True=今月のDD上限到達で停止中
sys4_current_month        = None   # 現在の年月 (year, month)。月次リセット用。
sys4_last_signal_bar_time = None   # 重複エントリー防止用

# 1570 日中処理済みフラグ（夜間ループ継続用）
etf_closed_today = False


# =========================
# 共通
# =========================
def floor_5min(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0, minute=(dt.minute // 5) * 5)

def headers():
    return {"Content-Type": "application/json", "X-API-KEY": token}

def safe_json(res):
    try:
        return res.json()
    except Exception:
        return {"raw": res.text}

def log(msg):
    print(f"[{datetime.now(JST).strftime('%H:%M:%S')}] {msg}")


# =========================
# トークン・再接続
# =========================
def get_token():
    global token
    res = requests.post(f"{API_BASE}/token", json={"APIPassword": API_PASSWORD}, timeout=10)
    if res.status_code == 200:
        token = res.json()["Token"]
        log(f"[OK] トークン取得成功: {token[:8]}...")
        return True
    log(f"[ERR] トークン取得失敗: {res.text}")
    return False

def get_micro_symbol():
    global MICRO_SYMBOL, MICRO_EXCHANGE
    for code in ["NK225micro", "NK225mini"]:
        url = f"{API_BASE}/symbolname/future?FutureCode={code}&DerivMonth={MICRO_DERIV_MONTH}"
        try:
            res = requests.get(url, headers=headers(), timeout=10)
        except requests.RequestException as e:
            log(f"[WARN] マイクロシンボル取得失敗({code}): {e}")
            continue
        if res.status_code != 200:
            continue
        data = res.json()
        sym = data.get("Symbol")
        if not sym:
            continue
        MICRO_SYMBOL   = sym
        MICRO_EXCHANGE = data.get("Exchange") or 2
        log(f"[OK] マイクロシンボル取得({code}): {MICRO_SYMBOL} / Exchange={MICRO_EXCHANGE}")
        return True
    log("[WARN] マイクロシンボル取得失敗 → マイクロ検証スキップ")
    return False

def register_symbol(retries=5, interval=2.0):
    symbols = [{"Symbol": SYMBOL, "Exchange": EXCHANGE}]
    if MICRO_SYMBOL:
        symbols.append({"Symbol": MICRO_SYMBOL, "Exchange": MICRO_EXCHANGE})

    for i in range(retries):
        res = requests.put(
            f"{API_BASE}/register",
            headers=headers(),
            json={"Symbols": symbols},
            timeout=10
        )
        if res.status_code == 200:
            log(f"[OK] 銘柄登録成功 ({len(symbols)}銘柄)")
            return True
        log(f"[ERR] 銘柄登録失敗({i+1}/{retries}): {res.text}")
        time.sleep(interval)
    return False

def refresh_token_and_reregister():
    global consecutive_auth_errors, last_reauth_time
    now_ts = time.time()
    if now_ts - last_reauth_time < REAUTH_COOLDOWN:
        log(f"[WAIT] 再認証クールダウン中 ({REAUTH_COOLDOWN}秒) → スキップ")
        return False
    log("[RETRY] トークン再取得を実行")
    last_reauth_time = now_ts
    if not get_token():
        consecutive_auth_errors += 1
        return False
    time.sleep(1.0)
    if not register_symbol():
        consecutive_auth_errors += 1
        return False
    consecutive_auth_errors = 0
    return True

def request_with_reauth(method, path, *, json_body=None, retry=1):
    global consecutive_auth_errors
    url = f"{API_BASE}{path}"
    try:
        res = requests.request(
            method=method, url=url,
            headers=headers() if token else {"Content-Type": "application/json"},
            json=json_body, timeout=10
        )
    except requests.RequestException as e:
        log(f"[WARN] 通信エラー: {e}")
        return None

    if res.status_code == 200:
        consecutive_auth_errors = 0
        return res

    txt = res.text or ""
    token_error = (res.status_code == 401) or ("APIキー不一致" in txt) or ("Unauthorized" in txt)

    if token_error and retry > 0:
        log("[WARN] 認証エラー検出 → トークン再取得(1回のみ)")
        ok = refresh_token_and_reregister()
        if not ok:
            log(f"[ERR] トークン再取得失敗 (連続失敗: {consecutive_auth_errors}/{MAX_AUTH_ERRORS})")
            return None
        return request_with_reauth(method, path, json_body=json_body, retry=retry - 1)

    if token_error and retry <= 0:
        consecutive_auth_errors += 1
        log(f"[ERR] 再取得後も認証エラー → 諦め (連続失敗: {consecutive_auth_errors}/{MAX_AUTH_ERRORS})")
    else:
        log(f"[WARN] APIエラー {res.status_code}: {txt}")
    return None


# =========================
# 板取得
# =========================
def get_board():
    res = request_with_reauth("GET", f"/board/{SYMBOL}@{EXCHANGE}")
    return safe_json(res) if res else None

def get_micro_board():
    if not MICRO_SYMBOL:
        return None
    res = request_with_reauth("GET", f"/board/{MICRO_SYMBOL}@{MICRO_EXCHANGE}")
    return safe_json(res) if res else None

def get_price_from_board(board):
    p = board.get("CurrentPrice")
    if p is not None:
        return p
    bid = board.get("BidPrice")
    ask = board.get("AskPrice")
    if bid and ask:
        return (float(bid) + float(ask)) / 2
    return bid or ask

def get_current_price():
    board = get_board()
    return get_price_from_board(board) if board else None


# =========================
# 1570 5分足生成（変更なし）
# =========================
def start_new_bar(bar_time, price, vol_delta):
    return {"datetime": bar_time, "open": price, "high": price, "low": price, "close": price, "volume": max(vol_delta, 0)}

def update_bar_from_board(board):
    global current_bar, completed_bars, last_cum_volume, last_price

    now      = datetime.now(JST)
    bar_time = floor_5min(now)
    price    = get_price_from_board(board)
    if price is None:
        return
    price      = float(price)
    cum_volume = board.get("TradingVolume")

    vol_delta = 0
    if cum_volume is not None:
        try:
            cum_volume = int(cum_volume)
            if last_cum_volume is not None and cum_volume >= last_cum_volume:
                vol_delta = cum_volume - last_cum_volume
            last_cum_volume = cum_volume
        except Exception:
            pass

    last_price = price

    if current_bar is None:
        current_bar = start_new_bar(bar_time, price, vol_delta)
        return
    if current_bar["datetime"] != bar_time:
        completed_bars.append(current_bar)
        current_bar = start_new_bar(bar_time, price, vol_delta)
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
    df = df.drop_duplicates(subset=["datetime"], keep="last").sort_values("datetime")
    df = df.set_index("datetime")
    return df

def save_csv(df):
    if df is None or df.empty:
        return
    if os.path.exists(CSV_FILE):
        existing = pd.read_csv(CSV_FILE, parse_dates=["datetime"]).set_index("datetime")
        combined = pd.concat([existing, df])
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    else:
        combined = df.copy()
    combined.to_csv(CSV_FILE, encoding="utf-8-sig")
    log(f"[FILE] CSV保存: {CSV_FILE}")


# =========================
# マイクロ 5分足生成（変更なし）
# =========================
def update_micro_bar(board):
    global micro_current_bar, micro_completed_bars, micro_last_cum_vol

    price = get_price_from_board(board)
    if price is None:
        return
    price      = float(price)
    now        = datetime.now(JST)
    bar_time   = floor_5min(now).replace(tzinfo=None)
    cum_volume = board.get("TradingVolume")

    vol_delta = 0
    if cum_volume is not None:
        try:
            cum_volume = int(cum_volume)
            if micro_last_cum_vol is not None and cum_volume >= micro_last_cum_vol:
                vol_delta = cum_volume - micro_last_cum_vol
            micro_last_cum_vol = cum_volume
        except Exception:
            pass

    if micro_current_bar is None:
        micro_current_bar = start_new_bar(bar_time, price, vol_delta)
        return
    if micro_current_bar["datetime"] != bar_time:
        micro_completed_bars.append(micro_current_bar)
        micro_current_bar = start_new_bar(bar_time, price, vol_delta)
        return
    micro_current_bar["high"]    = max(micro_current_bar["high"], price)
    micro_current_bar["low"]     = min(micro_current_bar["low"], price)
    micro_current_bar["close"]   = price
    micro_current_bar["volume"] += max(vol_delta, 0)

def micro_bars_to_df():
    rows = micro_completed_bars.copy()
    if micro_current_bar is not None:
        rows.append(micro_current_bar.copy())
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.drop_duplicates(subset=["datetime"], keep="last").sort_values("datetime").reset_index(drop=True)
    return df

def _strip_tz(idx):
    """DatetimeIndex を tz-naive（JST ローカル時刻）に統一する"""
    if hasattr(idx, "tz") and idx.tz is not None:
        return idx.tz_convert("Asia/Tokyo").tz_localize(None)
    return idx

def save_micro_csv(df):
    if df is None or df.empty:
        return
    df_save = df.set_index("datetime")
    df_save.index = _strip_tz(df_save.index)
    if os.path.exists(MICRO_CSV_FILE):
        existing = pd.read_csv(MICRO_CSV_FILE, parse_dates=["datetime"]).set_index("datetime")
        existing.index = _strip_tz(existing.index)
        combined = pd.concat([existing, df_save])
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    else:
        combined = df_save.copy()
    combined.to_csv(MICRO_CSV_FILE, encoding="utf-8-sig")
    log(f"CSV保存: {MICRO_CSV_FILE}")


# =========================
# マイクロ ウォームアップ（新規追加）
# micro_5min.csv の過去足を micro_completed_bars に読み込み
# =========================
def _needs_indicator_warmup(last_dt, now_naive):
    """セッション間ギャップを除外してウォームアップが必要か判定する。

    通常の休憩時間（夜間終了後06:00〜日中開始08:45、昼休み15:40〜17:00）は
    データが来ないのが正常なので除外。それ以外で30分以上空白があればTrue。

    last_dt  : CSV最終足のdatetime（tz-naive JST）
    now_naive: 現在時刻（tz-naive JST）
    """
    from datetime import timedelta

    hhmm = now_naive.hour * 100 + now_naive.minute

    # 朝の休憩（06:00〜08:44）: 夜間セッションは ~05:55 に終了
    # CSV最終足が 05:25 以降であれば正常終了と判断 → ウォームアップ不要
    if 600 <= hhmm < 845:
        night_end = now_naive.replace(hour=5, minute=55, second=0, microsecond=0)
        if last_dt >= night_end - timedelta(minutes=30):
            return False

    # 昼休み（15:40〜16:59）: 日中セッションは ~15:35 に終了
    # CSV最終足が 15:05 以降であれば正常終了と判断 → ウォームアップ不要
    elif 1540 <= hhmm < 1700:
        day_end = now_naive.replace(hour=15, minute=35, second=0, microsecond=0)
        if last_dt >= day_end - timedelta(minutes=30):
            return False

    # 上記以外（取引時間中、または週末明けなど）: 30分超の空白でウォームアップ
    return (now_naive - last_dt).total_seconds() >= 30 * 60


def load_micro_warmup():
    global micro_completed_bars, micro_warmup_remaining
    if not MICRO_CSV_WARMUP.exists():
        log(f"[WARN] ウォームアップCSVなし: {MICRO_CSV_WARMUP} → スキップ")
        return
    try:
        wdf = pd.read_csv(MICRO_CSV_WARMUP)
        wdf["datetime"] = pd.to_datetime(wdf["datetime"], errors="coerce")
        # tz-aware（+09:00）の場合は JST 変換してから naive に。tz-naive（JST）はそのまま使用
        if wdf["datetime"].dt.tz is not None:
            wdf["datetime"] = wdf["datetime"].dt.tz_convert("Asia/Tokyo").dt.tz_localize(None)
        for c in ["open", "high", "low", "close", "volume"]:
            wdf[c] = pd.to_numeric(wdf[c], errors="coerce")
        wdf = (wdf.dropna(subset=["open", "close"])
               .sort_values("datetime")
               .tail(MICRO_WARMUP_BARS)
               .reset_index(drop=True))
        # dict のリストに変換して completed_bars へ投入
        micro_completed_bars = wdf.to_dict("records")
        log(f"[OK] マイクロウォームアップ完了: {len(micro_completed_bars)} 本")

        # ── CSV鮮度チェック: セッション間ギャップを考慮してウォームアップ要否を判定 ──
        if not wdf.empty:
            last_dt = pd.Timestamp(wdf.iloc[-1]["datetime"])
            now_naive = datetime.now(JST).replace(tzinfo=None)
            gap_min = (now_naive - last_dt).total_seconds() / 60
            if _needs_indicator_warmup(last_dt, now_naive):
                micro_warmup_remaining = 26
                log(f"[WARN] CSV最終行が{gap_min:.0f}分前のデータです。"
                    f"インジケーター収束まで{micro_warmup_remaining}本のウォームアップを実施します。")
            else:
                log(f"[OK] CSV最終行は{gap_min:.0f}分前（セッション間ギャップ含む）→ ウォームアップ不要")
    except Exception as e:
        log(f"[WARN] ウォームアップ読み込みエラー: {e}")


# =========================
# CSV品質チェック（起動時：前日データの欠損・出来高0連続確認）
# =========================
def check_csv_quality():
    from datetime import date, timedelta, datetime as _dt

    now_jst = datetime.now(JST)
    today   = now_jst.date()
    prev    = today - timedelta(days=1)

    # 前日が休場なら skip
    if prev.weekday() >= 5 or is_holiday(prev):
        log(f"[CSV-CHECK] 前日({prev})は休場 → チェックスキップ")
        return

    if not MICRO_CSV_WARMUP.exists():
        log("[CSV-CHECK] CSVなし → スキップ")
        return

    try:
        df = pd.read_csv(MICRO_CSV_WARMUP, parse_dates=["datetime"])
        if df["datetime"].dt.tz is not None:
            df["datetime"] = df["datetime"].dt.tz_convert("Asia/Tokyo").dt.tz_localize(None)
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        df = df.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
    except Exception as e:
        log(f"[CSV-CHECK] CSV読み込みエラー: {e}")
        return

    # 前日の期待5分足リストを生成
    # 日中: prev 8:45〜15:35  夜間: prev 17:00〜23:55 + (prev+1) 0:00〜5:50
    cutoff = now_jst.replace(tzinfo=None) - timedelta(minutes=10)  # 未完足を除外
    next_d  = prev + timedelta(days=1)
    expected = []
    t = _dt(prev.year, prev.month, prev.day, 8, 45)
    while t < _dt(prev.year, prev.month, prev.day, 15, 40):
        if t <= cutoff:
            expected.append(t)
        t += timedelta(minutes=5)
    t = _dt(prev.year, prev.month, prev.day, 17, 0)
    while t < _dt(next_d.year, next_d.month, next_d.day, 5, 55):
        if t <= cutoff:
            expected.append(t)
        t += timedelta(minutes=5)

    if not expected:
        log("[CSV-CHECK] チェック対象足なし（セッション未完了）")
        return

    # 実在タイムスタンプ（5分切り捨て）
    existing = set(df["datetime"].dt.floor("5min").tolist())

    # 欠損チェック
    in_gap = False
    gap_start = gap_end = None
    gaps = []
    for ts in expected:
        ts_naive = pd.Timestamp(ts)
        if ts_naive not in existing:
            if not in_gap:
                gap_start = ts
                in_gap = True
            gap_end = ts
        else:
            if in_gap:
                gaps.append((gap_start, gap_end))
                in_gap = False
    if in_gap:
        gaps.append((gap_start, gap_end))

    if gaps:
        for gs, ge in gaps:
            log(f"[CSV-CHECK] 欠損あり: {gs.strftime('%Y-%m-%d %H:%M')} 〜 {ge.strftime('%Y-%m-%d %H:%M')}")
    else:
        log(f"[CSV-CHECK] 前日データ正常: {len(expected)}本")

    # 出来高0連続チェック（取引時間内・休憩除外）
    prev_start  = _dt(prev.year,   prev.month,   prev.day,   8, 45)
    prev_end    = _dt(next_d.year, next_d.month, next_d.day, 5, 55)
    brk_start   = _dt(prev.year,   prev.month,   prev.day,  15, 40)
    brk_end     = _dt(prev.year,   prev.month,   prev.day,  17,  0)
    mask = ((df["datetime"] >= prev_start) & (df["datetime"] < prev_end) &
            ~((df["datetime"] >= brk_start) & (df["datetime"] < brk_end)))
    tdf = df[mask].reset_index(drop=True)

    if "volume" not in tdf.columns or tdf.empty:
        return

    consec = 0
    zs = ze = None
    for _, row in tdf.iterrows():
        if row["volume"] == 0:
            if consec == 0:
                zs = row["datetime"]
            consec += 1
            ze = row["datetime"]
        else:
            if consec >= 5:
                log(f"[CSV-CHECK] 警告: 出来高0が連続 "
                    f"{zs.strftime('%Y-%m-%d %H:%M')}〜{ze.strftime('%Y-%m-%d %H:%M')}（{consec}本）")
                log("[CSV-CHECK] → collect_micro.pyの動作確認を推奨")
            consec = 0
            zs = ze = None
    if consec >= 5:
        log(f"[CSV-CHECK] 警告: 出来高0が連続 "
            f"{zs.strftime('%Y-%m-%d %H:%M')}〜{ze.strftime('%Y-%m-%d %H:%M')}（{consec}本）")
        log("[CSV-CHECK] → collect_micro.pyの動作確認を推奨")


# =========================
# 1570 指標・シグナル（変更なし）
# =========================
def add_indicators(df):
    df = df.copy()
    df["ma20"]     = df["close"].rolling(20).mean()
    df["ma_slope"] = df["ma20"] - df["ma20"].shift(1)

    prev_close = df["close"].shift(1)
    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - prev_close).abs()
    tr3 = (df["low"]  - prev_close).abs()
    df["tr"]      = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["atr14"]   = df["tr"].rolling(14).mean()
    df["atr_avg"] = df["atr14"].rolling(20).mean()

    df["bb_std"]       = df["close"].rolling(20).std()
    df["bb_width"]     = 4 * df["bb_std"]
    df["bb_width_avg"] = df["bb_width"].rolling(20).mean()
    df["bb_squeeze"]   = df["bb_width"] / df["bb_width_avg"]
    return df

def check_signal(df):
    if df is None or len(df) < 3:
        return None
    i   = len(df) - 1
    cur = df.iloc[i]
    p1  = df.iloc[i - 1]
    p2  = df.iloc[i - 2]

    if pd.isna(cur["ma_slope"]) or cur["ma_slope"] <= 0:
        return None
    if (
        not pd.isna(cur["bb_squeeze"]) and not pd.isna(cur["atr14"])
        and not pd.isna(cur["atr_avg"]) and cur["atr_avg"] != 0
    ):
        if cur["bb_squeeze"] < BB_SQ_TH or cur["atr14"] < cur["atr_avg"] * ATR_RATIO_TH:
            log(f"[SKIP] レンジ判定スキップ BB={cur['bb_squeeze']:.2f} ATR比={cur['atr14']/cur['atr_avg']:.2f}")
            return None
    if p2["high"] < p1["high"] and p2["low"] < p1["low"] and cur["high"] > p1["high"]:
        return "long"
    return None


# =========================
# マイクロ 指標計算（dual_signal_bot 統合版）
# MA9/MA10 / MACD(12,26,9) / vol_ratio / BB幅 を計算
# =========================
def add_micro_indicators(df):
    df = df.copy()

    # MA9 / MA10 / MA20
    df["ma9"]  = df["close"].rolling(9).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()  # 系統③用

    # MACD (12, 26, 9)
    ema_fast        = df["close"].ewm(span=12, adjust=False).mean()
    ema_slow        = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"]      = ema_fast - ema_slow
    df["macd_sig"]  = df["macd"].ewm(span=9, adjust=False).mean()

    # 出来高比率
    df["vol_ma20"]  = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma20"]

    # RSI14（系統④用）
    _delta   = df["close"].diff()
    _up      = _delta.clip(lower=0)
    _down    = -_delta.clip(upper=0)
    _avg_up  = _up.rolling(14).mean()
    _avg_dn  = _down.rolling(14).mean()
    _rs      = _avg_up / _avg_dn.replace(0, np.nan)
    df["rsi14"] = 100 - (100 / (1 + _rs))

    # ATR14（系統④ NaNチェック用）
    _prev_c  = df["close"].shift(1)
    _tr1     = df["high"] - df["low"]
    _tr2     = (df["high"] - _prev_c).abs()
    _tr3     = (df["low"]  - _prev_c).abs()
    df["atr14"] = pd.concat([_tr1, _tr2, _tr3], axis=1).max(axis=1).rolling(14).mean()

    # ボリンジャーバンド幅（スクイーズ判定用）
    bb_mid              = df["close"].rolling(20).mean()
    bb_std              = df["close"].rolling(20).std()
    df["bb_width"]      = (bb_std * 4) / bb_mid
    df["bb_width_ma20"] = df["bb_width"].rolling(20).mean()

    return df


def check_sys4_signal(df):
    """系統④：逆張りロング シグナル判定

    条件：
    - 直前SYS4_LOOKBACK本前のcloseと比較してSYS4_MOVE_PCT以上下落
    - RSI14 <= SYS4_RSI_TH
    - vol_ratio >= SYS4_VOL_TH
    - ひげ戻り率 >= SYS4_RECOVERY_PCT（(close-low)/close）
    - 現在時刻の時間（bar開始時刻のhour）がSYS4_EXCLUDE_HOURSに含まれない

    ※ バックテスト（gyakubari_monthly_yearly.py）との完全一致のため：
      - シグナル評価は確定済み足（df_confirmed = df.iloc[:-1]）の最終行で行う
      - hour判定はbar開始時刻のhour（dt.hour）を使用する
      - インジケーターはadd_micro_indicators()で計算済みの
        rsi14・vol_ratio・atr14を使用する
    """
    if df is None or len(df) < 22:  # lookback1 + RSI14期間20 + 余裕
        return False

    cur  = df.iloc[-1]
    prev = df.iloc[-1 - SYS4_LOOKBACK]

    # NaNチェック
    for col in ["rsi14", "vol_ratio", "atr14"]:
        if pd.isna(cur[col]):
            return False

    dt   = pd.to_datetime(cur["datetime"])
    hour = dt.hour  # bar開始時刻のhour（BTと統一）

    # 除外時間帯チェック
    if hour in SYS4_EXCLUDE_HOURS:
        return False

    # 下落率
    move_pct = (cur["close"] - prev["close"]) / prev["close"]
    # ひげ戻り率
    recovery = (cur["close"] - cur["low"]) / cur["close"] if cur["close"] != 0 else 0

    return all([
        move_pct <= -SYS4_MOVE_PCT,
        cur["rsi14"] <= SYS4_RSI_TH,
        cur["vol_ratio"] >= SYS4_VOL_TH,
        recovery >= SYS4_RECOVERY_PCT,
    ])


# =========================
# マイクロ シグナル判定（系統①②③統合版）
# 戻り値: list of fired systems  例 ["①"], ["③"], ["①","③"], []
#
# 系統①: long  月火水 × 8/12/15/18/19/20/21/23時 × 3月・5月・7月・11月除外 / CPI除外なし
# 系統②: long  火水 × vol>=2.0 × BB拡大中 × 5〜9月除外
# 系統③: short 月水木金 × DST:[5,8,12,14,15,19,20,22,23] 冬:[5,12,15,19,20,21,22,23] × 7月・11月除外 × CPI除外
# =========================
def check_micro_signal(df):
    if df is None or len(df) < 30:
        return []

    row   = df.iloc[-1]
    row_p = df.iloc[-2]
    row_p2= df.iloc[-3]

    # 必須カラムNaNチェック
    need = ["ma9", "ma10", "ma20", "macd", "macd_sig", "vol_ratio",
            "bb_width", "bb_width_ma20"]
    for col in need:
        if pd.isna(row[col]) or pd.isna(row_p[col]) or pd.isna(row_p2[col]):
            return []

    m9   = row["ma9"];    m10   = row["ma10"]
    m9p  = row_p["ma9"];  m10p  = row_p["ma10"]
    m9p2 = row_p2["ma9"]; m10p2 = row_p2["ma10"]
    hi   = row["high"]
    lo   = row["low"]
    c1   = row_p["close"];   c2 = row_p2["close"]

    # シグナルバーの日時属性
    # hr    : 足終了時刻基準（+5分）→ 系統① に使用
    # hr_s3 : 足開始時刻基準（bar START hour）→ 系統③ に使用（backtest_system12_combined.py と統一）
    dt    = pd.to_datetime(row["datetime"])
    wd    = dt.weekday()   # 0=月, 1=火, 2=水, 3=木, 4=金, 5=土, 6=日
    hr    = (dt + pd.Timedelta(minutes=5)).hour
    hr_s3 = dt.hour

    # 土曜日（金曜夜間セッション後半 00:00〜05:55）はエントリー禁止
    if wd == 5:
        return []
    month = dt.month

    fired = []

    # ── 系統①②（long 共通基本条件）──
    above_ma = (c2 > m9p2 and c2 > m10p2 and c1 > m9p and c1 > m10p)
    touch_lo = (abs(lo - m9)  / m9  <= TOUCH_PCT or
                abs(lo - m10) / m10 <= TOUCH_PCT)
    gc       = (row["macd"] > row["macd_sig"])

    if above_ma and touch_lo and gc:
        # 系統②: 年平均+14万円のため本番除外中
        # if wd in (1, 2) and month not in (5, 6, 7, 8, 9):
        #     vr  = row["vol_ratio"]
        #     bw  = row["bb_width"]
        #     bwm = row["bb_width_ma20"]
        #     if (not pd.isna(vr) and vr >= 2.0 and
        #             not pd.isna(bw) and not pd.isna(bwm) and bw > bwm):
        #         fired.append("②")
        # 系統①: 月火水 × 8/12/15/18/19/20/21/23時 × 3月・5月・7月・11月除外
        if wd in (0, 1, 2) and hr in (8, 12, 15, 18, 19, 20, 21, 23) and month not in (3, 5, 7, 11):
            if micro_monthly_skip:
                log(f"[系統①] 月次DD制限中のためスキップ ({micro_monthly_pnl:,.0f}円)")
            else:
                fired.append("①")

    # ── 系統③（short 基本条件）──
    # バックテスト（PF1.440・パターン⑤）条件と統一: MA9 < MA20（MA同士の大小比較・1本分）
    m20      = row["ma20"]
    below_ma = (m9 < m20)
    touch_hi = (abs(hi - m9) / m9 <= TOUCH_PCT)
    dc       = (row["macd"] < row["macd_sig"])

    if below_ma and touch_hi and dc:
        # 月水木金 × 5月・7月・11月除外
        if wd in (0, 2, 3, 4) and month not in (5, 7, 11):
            now_dt = dt.to_pydatetime() if hasattr(dt, "to_pydatetime") else dt

            # ── 月次DD制限チェック（リセットは check_micro_entry() 先頭で実施）──
            if micro_monthly_skip:
                log(f"[系統③] 月次DD制限中のためスキップ ({micro_monthly_pnl:,.0f}円)")
                return fired  # ③はfiredに追加しない

            # DST対応時間帯フィルター
            if is_dst(now_dt):
                s3_hours = (5, 8, 12, 14, 15, 19, 20, 22, 23)
            else:
                s3_hours = (5, 12, 15, 19, 20, 21, 22, 23)

            if hr_s3 not in s3_hours:
                log(
                    f"[系統③] 時間帯対象外({hr_s3}時, "
                    f"{'DST' if is_dst(now_dt) else '冬時間'}): スキップ"
                )
            elif is_cpi_window(now_dt, cpi_df):
                log(f"[系統③] CPIウィンドウのためエントリースキップ: {now_dt}")
            else:
                fired.append("③")

    return fired


# =========================
# 発注（1570用・変更なし）
# =========================
def send_order(side, qty, order_type="market", stop_price=None, limit_price=None):
    buy_sell = "1" if side == "buy" else "2"
    body = {
        "Password": API_PASSWORD, "Symbol": SYMBOL, "Exchange": EXCHANGE,
        "SecurityType": 1, "Side": buy_sell, "CashMargin": 2,
        "MarginTradeType": 3, "DelivType": 2, "FundType": "  ",
        "AccountType": 4, "Qty": qty, "ExpireDay": 0,
    }
    if order_type == "market":
        body["FrontOrderType"] = 10; body["Price"] = 0
    elif order_type == "limit":
        body["FrontOrderType"] = 20; body["Price"] = limit_price
    elif order_type == "stop":
        body["FrontOrderType"] = 30; body["Price"] = 0
        body["ReverseLimitOrder"] = {
            "TriggerSec": 1, "TriggerPrice": stop_price,
            "UnderOver": 1, "AfterHitOrderType": 1, "AfterHitPrice": 0
        }
    else:
        log("[ERR] 未対応の注文種別"); return None

    res = request_with_reauth("POST", "/sendorder", json_body=body)
    if res is None:
        return None
    order_id = safe_json(res).get("OrderId", "")
    log(f"[OK] 発注成功 OrderId: {order_id}")
    return order_id


# =========================
# マイクロ 発注（新規追加）
# =========================
def send_micro_order(side):
    """マイクロ先物の成行発注"""
    buy_sell = "1" if side == "buy" else "2"
    body = {
        "Password":        API_PASSWORD,
        "Symbol":          MICRO_SYMBOL,
        "Exchange":        MICRO_EXCHANGE,
        "SecurityType":    1,
        "Side":            buy_sell,
        "CashMargin":      2,
        "MarginTradeType": 3,
        "DelivType":       2,
        "FundType":        "  ",
        "AccountType":     4,
        "Qty":             MICRO_LOT,
        "ExpireDay":       0,
        "FrontOrderType":  10,
        "Price":           0,
    }
    if MICRO_DRY_RUN:
        log(f"[MICRO-DRY] 発注スキップ: side={side}")
        return "DRY"
    res = request_with_reauth("POST", "/sendorder", json_body=body)
    if res is None:
        log(f"[ERR] マイクロ発注失敗: side={side}")
        return None
    oid = safe_json(res).get("OrderId", "")
    log(f"[OK] マイクロ発注成功 OrderId: {oid}")
    return oid


def _wait_for_fill(order_id, max_retries=10, interval=1.0):
    """成行注文の約定完了をポーリングで待機する。
    約定確認できたら実約定価格(float)を返す。タイムアウトなら None を返す。

    kabuステーションAPI: GET /orders/{order_id}
      RecvStatus==2 かつ Details[].RecvStatus==2 で約定済み
      約定価格は Details[].ContractPrice（複数明細の加重平均を取る）
    """
    for attempt in range(1, max_retries + 1):
        time.sleep(interval)
        res = request_with_reauth("GET", f"/orders/{order_id}")
        if res is None:
            log(f"[POLL] 約定確認API失敗 ({attempt}/{max_retries})")
            continue
        data = safe_json(res)
        recv_status = data.get("RecvStatus")
        # RecvStatus: 1=受付中, 2=完了, 3=失効/取消
        if recv_status == 3:
            log(f"[WARN] 注文が失効/取消された (RecvStatus=3) OrderId:{order_id}")
            return None
        if recv_status != 2:
            log(f"[POLL] 約定待ち RecvStatus={recv_status} ({attempt}/{max_retries})")
            continue
        # 約定明細から加重平均約定価格を算出
        details = data.get("Details") or []
        filled_qty   = 0.0
        filled_value = 0.0
        for d in details:
            if d.get("RecvStatus") == 2:
                qty   = float(d.get("Qty") or 0)
                price = float(d.get("ContractPrice") or 0)
                if qty > 0 and price > 0:
                    filled_qty   += qty
                    filled_value += qty * price
        if filled_qty > 0:
            avg_price = filled_value / filled_qty
            log(f"[POLL] 約定確認 ({attempt}/{max_retries}) 約定価格:{avg_price:.0f}  数量:{filled_qty}")
            return avg_price
        # RecvStatus==2 だが明細がまだない場合は再試行
        log(f"[POLL] RecvStatus=2 だが約定明細未取得 ({attempt}/{max_retries})")

    log(f"[WARN] 約定確認タイムアウト ({max_retries}回) OrderId:{order_id}")
    return None


def send_micro_sl_tp_orders(side, sl_price, tp_price):
    """マイクロ先物の SL逆指値注文・TP指値注文を同時発注（本番モードのみ）
    side: "buy" or "sell"（エントリー方向）
    SL逆指値: エントリー反対方向で逆指値
      SHORT(sell): SL上抜け → 買い戻し逆指値 (UnderOver=1)
      LONG(buy):   SL下抜け → 売り逆指値    (UnderOver=2)
    TP指値: エントリー反対方向で指値
    """
    if MICRO_DRY_RUN:
        log("[MICRO-DRY] SL/TP発注スキップ")
        return None, None

    # SL・TP注文はエントリーと反対売買
    exit_side = "1" if side == "sell" else "2"   # SHORTなら買い戻し(1)、LONGなら売り(2)
    under_over = 1 if side == "sell" else 2       # SHORT SL: 上抜け(1)、LONG SL: 下抜け(2)

    sl_body = {
        "Password":        API_PASSWORD,
        "Symbol":          MICRO_SYMBOL,
        "Exchange":        MICRO_EXCHANGE,
        "SecurityType":    1,
        "Side":            exit_side,
        "CashMargin":      2,
        "MarginTradeType": 3,
        "DelivType":       2,
        "FundType":        "  ",
        "AccountType":     4,
        "Qty":             MICRO_LOT,
        "ExpireDay":       0,
        "FrontOrderType":  30,
        "Price":           0,
        "ReverseLimitOrder": {
            "TriggerSec":      1,
            "TriggerPrice":    sl_price,
            "UnderOver":       under_over,
            "AfterHitOrderType": 1,
            "AfterHitPrice":   0,
        },
    }
    tp_body = {
        "Password":        API_PASSWORD,
        "Symbol":          MICRO_SYMBOL,
        "Exchange":        MICRO_EXCHANGE,
        "SecurityType":    1,
        "Side":            exit_side,
        "CashMargin":      2,
        "MarginTradeType": 3,
        "DelivType":       2,
        "FundType":        "  ",
        "AccountType":     4,
        "Qty":             MICRO_LOT,
        "ExpireDay":       0,
        "FrontOrderType":  20,
        "Price":           tp_price,
    }

    sl_oid = tp_oid = None

    res = request_with_reauth("POST", "/sendorder", json_body=sl_body)
    if res:
        sl_oid = safe_json(res).get("OrderId", "")
        log(f"[OK] マイクロSL逆指値発注 OrderId:{sl_oid}  TriggerPrice:{sl_price:.0f}")
    else:
        log(f"[ERR] マイクロSL逆指値発注失敗 TriggerPrice:{sl_price:.0f}")

    res = request_with_reauth("POST", "/sendorder", json_body=tp_body)
    if res:
        tp_oid = safe_json(res).get("OrderId", "")
        log(f"[OK] マイクロTP指値発注 OrderId:{tp_oid}  Price:{tp_price:.0f}")
    else:
        log(f"[ERR] マイクロTP指値発注失敗 Price:{tp_price:.0f}")

    return sl_oid, tp_oid


def cancel_micro_order(order_id):
    """マイクロ先物注文のキャンセル（夜間強制決済前のSL/TP注文取消用）"""
    if not order_id:
        return False
    body = {"OrderId": order_id, "Password": API_PASSWORD}
    res = request_with_reauth("PUT", "/cancelorder", json_body=body)
    if res:
        log(f"[OK] 注文キャンセル成功 OrderId:{order_id}")
        return True
    log(f"[WARN] 注文キャンセル失敗 OrderId:{order_id}")
    return False


# =========================
# エントリー・決済（1570・変更なし）
# =========================
def entry_long():
    global position
    cp = get_current_price()
    if cp is None:
        log("[ERR] 現在値取得失敗"); return
    cp = float(cp)
    log(f"[LONG] ロングエントリー @ {cp:.0f}円 × {LOT}株")
    order_id = send_order("buy", LOT, "market")
    if not order_id:
        return
    time.sleep(1.5)
    sl_price = cp - STOP
    tp_price = cp + TP
    log(f"[SL] 損切り逆指値: {sl_price:.0f}円")
    send_order("sell", LOT, "stop", stop_price=sl_price)
    log(f"[TP] 利確指値: {tp_price:.0f}円")
    send_order("sell", LOT, "limit", limit_price=tp_price)
    position = {
        "side": "long", "entry_time": datetime.now(JST),
        "entry_price": cp, "sl_price": sl_price, "tp_price": tp_price,
        "qty": LOT, "order_id": order_id, "bars": 0,
    }

def close_position(reason):
    global position, day_pnl, consec_loss, trade_log
    if position is None:
        return
    cp = get_current_price()
    if cp is None:
        log("[ERR] 決済時の現在値取得失敗"); return
    cp = float(cp)
    log(f"[EXIT] 決済: {reason} @ {cp:.0f}円")
    send_order("sell", position["qty"], "market")
    pnl = (cp - position["entry_price"]) * position["qty"]
    day_pnl += pnl
    consec_loss = consec_loss + 1 if pnl < 0 else 0
    trade_log.append({
        "entry_time": position["entry_time"].strftime("%Y-%m-%d %H:%M:%S"),
        "exit_time": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
        "entry_price": position["entry_price"], "exit_price": cp,
        "pnl": pnl, "reason": reason,
    })
    log(f"損益: {pnl:+.0f}円 | 本日累計: {day_pnl:+.0f}円")
    position = None
    save_log()

def save_log():
    if not trade_log:
        return
    LOG_DIR.mkdir(exist_ok=True)
    all_file = LOG_DIR / "trade_log_all.csv"
    last_row = pd.DataFrame([trade_log[-1]])
    last_row.to_csv(all_file, mode='a', header=not all_file.exists(), index=False, encoding="utf-8-sig")
    log(f"[LOG] トレードログ保存: {all_file}")

def save_signal_log():
    if not signal_log:
        return
    LOG_DIR.mkdir(exist_ok=True)
    all_file = LOG_DIR / "signal_log_all.csv"
    last_row = pd.DataFrame([signal_log[-1]])
    last_row.to_csv(all_file, mode='a', header=not all_file.exists(), index=False, encoding="utf-8-sig")
    log(f"[LOG] シグナルログ保存: {all_file}")


# =========================
# マイクロ ポジション監視（DRY / 本番共通）
#
# DRYモード: ソフト判定のみ（注文なし）。SL/TP/時間到達をシミュレート。
# 本番モード:
#   SL/TP到達 → ブローカー側の逆指値・指値注文が自動執行済みのため
#               こちらは追跡ログ更新のみ（追加注文なし・二重発注なし）。
#   23:50強制決済 → SL/TP注文をキャンセルして成行決済注文を発注。
# =========================
def monitor_micro_dry(now, hhmm, verbose=False):
    global micro_dry_positions, micro_dry_day_pnl, micro_dry_trade_log

    if not micro_dry_positions:
        return

    board = get_micro_board()
    if not board:
        return
    cp = get_price_from_board(board)
    if cp is None:
        return
    cp = float(cp)

    still_open = []
    for pos in micro_dry_positions:
        side   = pos.get("side", "long")
        pnl    = (cp - pos["entry_price"]) if side == "long" else (pos["entry_price"] - cp)
        reason = None

        if side == "long":
            if cp <= pos["sl_price"]:
                reason = "SL到達"
            elif cp >= pos["tp_price"]:
                reason = "TP到達"
        else:  # short
            if cp >= pos["sl_price"]:
                reason = "SL到達"
            elif cp <= pos["tp_price"]:
                reason = "TP到達"

        if reason is None and hhmm >= 2350:
            reason = "夜間終了強制決済"

        if reason:
            sys_label = pos.get("system", "?")
            prefix = f"[MICRO{sys_label}]"

            # ── 本番モード処理 ──
            if not MICRO_DRY_RUN:
                if reason == "夜間終了強制決済":
                    # SL/TP注文をキャンセルしてから成行決済
                    sl_oid = pos.get("sl_order_id")
                    tp_oid = pos.get("tp_order_id")
                    if sl_oid:
                        cancel_micro_order(sl_oid)
                    if tp_oid:
                        cancel_micro_order(tp_oid)
                    time.sleep(0.5)
                    close_side = "buy" if side == "short" else "sell"
                    close_oid = send_micro_order(close_side)
                    log(f"[LIVE] {prefix} 系統{sys_label} 夜間強制決済発注 OrderId:{close_oid}")
                else:
                    # SL/TP到達: ブローカー側の注文が自動執行済み → 追跡ログのみ
                    log(f"[LIVE] {prefix} 系統{sys_label} {reason} 検知 @ {cp:.0f}"
                        f"  (ブローカー側注文が自動執行済み・追加発注なし)")

            # ── 共通: ログ記録・月次DD更新 ──
            micro_dry_day_pnl += pnl
            micro_dry_trade_log.append({
                "system":      sys_label,
                "side":        side,
                "entry_time":  pos["entry_time"].strftime("%Y-%m-%d %H:%M:%S"),
                "exit_time":   now.strftime("%Y-%m-%d %H:%M:%S"),
                "entry_price": pos["entry_price"],
                "exit_price":  cp,
                "pnl":         round(pnl, 1),
                "reason":      reason,
                "signal_bid":  pos.get("signal_bid"),
                "signal_ask":  pos.get("signal_ask"),
                "spread":      pos.get("spread"),
                "slip_est":    pos.get("slip_est"),
            })
            log(f"[EXIT] {prefix} 系統{sys_label} 決済:{reason} @ {cp:.0f}  損益:{pnl:+.0f}  本日累計:{micro_dry_day_pnl:+.0f}")

            # ── 系統①③合算 月次ドローダウン累計更新 ──
            if sys_label in ("①", "③"):
                global micro_monthly_pnl, micro_monthly_skip
                trade_yen = round(pnl * MICRO_PT_TO_YEN - MICRO_COMMISSION_YEN, 0)
                micro_monthly_pnl += trade_yen
                log(f"[MICRO①③] 月次損益:{micro_monthly_pnl:,.0f}円  DD上限:{MICRO_MONTHLY_DD_LIMIT:,.0f}円")
                if micro_monthly_pnl <= MICRO_MONTHLY_DD_LIMIT and not micro_monthly_skip:
                    micro_monthly_skip = True
                    log(f"[MICRO①③] 月次DD制限発動: 累計{micro_monthly_pnl:,.0f}円 → 今月の残りをスキップ")

            # ── 系統④ 月次DD更新 ──
            if sys_label == "④":
                global sys4_monthly_pnl_yen, sys4_stopped
                trade_yen = round(pnl * MICRO_PT_TO_YEN - MICRO_COMMISSION_YEN, 0)
                sys4_monthly_pnl_yen += trade_yen
                log(f"[系統④] 月次損益:{sys4_monthly_pnl_yen:,.0f}円  DD上限:{SYS4_DD_LIMIT_YEN:,.0f}円")
                if sys4_monthly_pnl_yen <= SYS4_DD_LIMIT_YEN and not sys4_stopped:
                    sys4_stopped = True
                    log(f"[系統④] 月次DD上限到達({sys4_monthly_pnl_yen:,.0f}円) → 今月の系統④を停止")
            LOG_DIR.mkdir(exist_ok=True)
            all_file = LOG_DIR / "micro_dry_log_all.csv"
            last_trade = pd.DataFrame([micro_dry_trade_log[-1]])
            last_trade.to_csv(all_file, mode='a', header=not all_file.exists(), index=False, encoding="utf-8-sig")
            log(f"[LOG] マイクロログ保存: {all_file}")
        else:
            # 系統④: hold_bars インクリメント → MAX_HOLD到達で時間決済
            if pos.get("system") == "④":
                pos["hold_bars"] = pos.get("hold_bars", 0) + 1
                if pos["hold_bars"] >= pos.get("max_hold", SYS4_MAX_HOLD):
                    reason = "TIME決済(MAX_HOLD)"
                    sys_label = "④"
                    prefix    = "[MICRO④]"
                    micro_dry_day_pnl += pnl
                    micro_dry_trade_log.append({
                        "system":      sys_label,
                        "side":        side,
                        "entry_time":  pos["entry_time"].strftime("%Y-%m-%d %H:%M:%S"),
                        "exit_time":   now.strftime("%Y-%m-%d %H:%M:%S"),
                        "entry_price": pos["entry_price"],
                        "exit_price":  cp,
                        "pnl":         round(pnl, 1),
                        "reason":      reason,
                        "signal_bid":  pos.get("signal_bid"),
                        "signal_ask":  pos.get("signal_ask"),
                        "spread":      pos.get("spread"),
                        "slip_est":    pos.get("slip_est"),
                    })
                    log(f"[EXIT] {prefix} 系統④ 決済:{reason} @ {cp:.0f}  損益:{pnl:+.0f}  本日累計:{micro_dry_day_pnl:+.0f}")
                    trade_yen = round(pnl * MICRO_PT_TO_YEN - MICRO_COMMISSION_YEN, 0)
                    sys4_monthly_pnl_yen += trade_yen
                    log(f"[系統④] 月次損益:{sys4_monthly_pnl_yen:,.0f}円  DD上限:{SYS4_DD_LIMIT_YEN:,.0f}円")
                    if sys4_monthly_pnl_yen <= SYS4_DD_LIMIT_YEN and not sys4_stopped:
                        sys4_stopped = True
                        log(f"[系統④] 月次DD上限到達({sys4_monthly_pnl_yen:,.0f}円) → 今月の系統④を停止")
                    LOG_DIR.mkdir(exist_ok=True)
                    all_file = LOG_DIR / "micro_dry_log_all.csv"
                    last_trade = pd.DataFrame([micro_dry_trade_log[-1]])
                    last_trade.to_csv(all_file, mode='a', header=not all_file.exists(), index=False, encoding="utf-8-sig")
                    continue  # still_openに追加しない（クローズ済み）
            still_open.append(pos)
            if verbose:
                mode_tag = "LIVE" if not MICRO_DRY_RUN else "DRY"
                log(f"[MICRO-{mode_tag}] 系統{pos.get('system','?')} {side} 保有中 @ {pos['entry_price']:.0f}  含み:{pnl:+.0f}")

    micro_dry_positions = still_open


# =========================
# マイクロ エントリー判定（系統①②③統合版）
# 系統ごとに独立したポジション追加（複数同時保有OK）
# =========================
def check_micro_entry(now, micro_board):
    global micro_dry_positions, micro_last_signal_bar_time, micro_warmup_remaining
    global micro_monthly_pnl, micro_monthly_skip, micro_current_month
    global sys4_monthly_pnl_yen, sys4_stopped, sys4_last_signal_bar_time

    # ── 系統①③合算 月次リセット（シグナル条件に関わらず毎足チェック）──
    now_ym = (now.year, now.month)
    if micro_current_month != now_ym:
        micro_current_month = now_ym
        micro_monthly_pnl   = 0.0
        micro_monthly_skip  = False
        log(f"[MICRO①③] 月次リセット: {now_ym} 累計損益リセット")

    # ── 系統④ 月次リセット ──
    global sys4_monthly_pnl_yen, sys4_stopped, sys4_current_month
    if sys4_current_month != now_ym:
        sys4_current_month   = now_ym
        sys4_monthly_pnl_yen = 0.0
        sys4_stopped         = False
        log(f"[系統④] 月次リセット: {now_ym} 損益リセット・停止解除")

    df = micro_bars_to_df()
    if df is None or len(df) < 31:   # 確定足30本 + current_bar 1本 = 最低31本
        return

    df = add_micro_indicators(df)

    # ── 確定済み足のみでシグナル評価（BT との整合）──
    # df.iloc[-1] = micro_current_bar（未確定・1tick目から書き換わる）
    # df.iloc[-2] = 直前の確定済み足（BTが評価するのと同じ final OHLC）
    # current_bar を除いた確定足列でシグナル判定し、5分前の完成足を基準にロックする
    df_confirmed    = df.iloc[:-1]                        # current_bar を除外
    latest_bar_time = df_confirmed.iloc[-1]["datetime"]   # 最後の確定足の開始時刻
    if micro_last_signal_bar_time == latest_bar_time:
        return  # 同一確定足で重複判定しない

    # ── セッション跨ぎ / 陳腐化足のスキップ ──
    # セッション間ギャップ（15:40〜17:00 / 5:55〜8:45）中はバー更新が止まるため、
    # 次セッション開始時に前セッション最終足が「確定足」として評価されてしまう問題を防ぐ。
    # ① 前セッションの足（セッション開始時刻より古い）→ 正常なギャップ跨ぎとして除外
    # ② 同セッション内で10分以上経過 → データ遅延等の陳腐化として除外
    from datetime import timedelta as _td

    now_naive = now.replace(tzinfo=None)
    bar_dt    = pd.Timestamp(latest_bar_time).to_pydatetime()

    hhmm_now = now_naive.hour * 100 + now_naive.minute
    if hhmm_now >= 1700:
        # 夜間セッション（17:00〜）
        session_start = now_naive.replace(hour=17, minute=0, second=0, microsecond=0)
    elif hhmm_now >= 845:
        # 日中セッション または 日中→夜間ギャップ（8:45〜16:59）
        session_start = now_naive.replace(hour=8, minute=45, second=0, microsecond=0)
    else:
        # 深夜〜早朝（〜8:44）: 前日17:00スタートの夜間セッション継続中
        yesterday = now_naive - _td(days=1)
        session_start = yesterday.replace(hour=17, minute=0, second=0, microsecond=0)

    if bar_dt < session_start:
        # 前セッションの足: 正常なセッション跨ぎ（陳腐化ではない）→ ロックして除外
        micro_last_signal_bar_time = latest_bar_time
        log(f"[MICRO] 前セッション足スキップ: {bar_dt.strftime('%H:%M')} "
            f"(現セッション開始:{session_start.strftime('%H:%M')}) → 評価対象外")
        return

    bar_age_min = (now_naive - bar_dt).total_seconds() / 60
    if bar_age_min > 10:
        # 同セッション内で10分以上経過: データ遅延等による陳腐化 → ロックして除外
        micro_last_signal_bar_time = latest_bar_time
        log(f"[MICRO] 陳腐化足スキップ: {bar_dt.strftime('%H:%M')} ({bar_age_min:.0f}分前) → 評価対象外")
        return

    micro_last_signal_bar_time = latest_bar_time

    # ── インジケーターウォームアップ中はエントリースキップ ──
    if micro_warmup_remaining > 0:
        micro_warmup_remaining -= 1
        log(f"[MICRO] ウォームアップ中 残り{micro_warmup_remaining}本")
        return

    fired = check_micro_signal(df_confirmed)

    if not fired:
        cp = get_price_from_board(micro_board)
        cp_str = f"{float(cp):.0f}" if cp else "取得失敗"
        log(f"[MICRO] → シグナルなし (現在値:{cp_str} bars:{len(df_confirmed)}本)")

    # ===== 系統④：逆張りロング判定 =====
    # DD上限チェック
    if sys4_stopped:
        log(f"[系統④] 月次DD上限到達のため停止中 (月次DD:{sys4_monthly_pnl_yen:,.0f}円)")
    elif check_sys4_signal(df_confirmed):
        # 同一確定足での重複判定防止
        if sys4_last_signal_bar_time != latest_bar_time:
            sys4_last_signal_bar_time = latest_bar_time

            cp4 = get_price_from_board(micro_board)
            if cp4 is None:
                log("[WARN] [系統④] 現在値取得失敗 → エントリースキップ")
            else:
                cp4      = float(cp4)
                sl_price = cp4 - SYS4_SL
                tp_price = cp4 + SYS4_TP

                best_bid = micro_board.get("BidPrice")
                best_ask = micro_board.get("AskPrice")
                best_bid = float(best_bid) if best_bid else None
                best_ask = float(best_ask) if best_ask else None
                spread   = round(best_ask - best_bid, 1) if (best_ask and best_bid) else None
                slip_est = round(best_ask - cp4, 1) if best_ask else None

                pos4 = {
                    "system":      "④",
                    "side":        "long",
                    "entry_time":  now,
                    "entry_price": cp4,
                    "sl_price":    sl_price,
                    "tp_price":    tp_price,
                    "max_hold":    SYS4_MAX_HOLD,
                    "hold_bars":   0,
                    "signal_bid":  best_bid,
                    "signal_ask":  best_ask,
                    "spread":      spread,
                    "slip_est":    slip_est,
                }

                bid_str  = f"{best_bid:.0f}" if best_bid else "---"
                ask_str  = f"{best_ask:.0f}" if best_ask else "---"
                slip_str = f"{slip_est:+.1f}pt" if slip_est is not None else "---"
                log(f"[LONG] [系統④] 逆張りロング @ {cp4:.0f}"
                    f"  SL:{sl_price:.0f}  TP:{tp_price:.0f}"
                    f"  Bid:{bid_str} Ask:{ask_str} Spread:{spread} SlipEst:{slip_str}")

                if MICRO_DRY_RUN:
                    micro_dry_positions.append(pos4)
                else:
                    oid4 = send_micro_order("buy")
                    if oid4:
                        pos4["order_id"] = oid4
                        fill_price = _wait_for_fill(oid4, max_retries=10, interval=1.0)
                        if fill_price is None:
                            log("[WARN] [系統④] 約定未確認 → 注文キャンセル")
                            cancel_micro_order(oid4)
                        else:
                            pos4["entry_price"] = fill_price
                            pos4["sl_price"]    = fill_price - SYS4_SL
                            pos4["tp_price"]    = fill_price + SYS4_TP
                            log(f"[FILL] [系統④] 約定:{fill_price:.0f}"
                                f"  SL:{pos4['sl_price']:.0f}  TP:{pos4['tp_price']:.0f}")
                            sl_oid4, tp_oid4 = send_micro_sl_tp_orders(
                                "buy", pos4["sl_price"], pos4["tp_price"]
                            )
                            pos4["sl_order_id"] = sl_oid4
                            pos4["tp_order_id"] = tp_oid4
                            micro_dry_positions.append(pos4)

    if not fired:
        return

    cp = get_price_from_board(micro_board)
    if cp is None:
        log("[WARN] [MICRO] 現在値取得失敗 → エントリースキップ")
        return
    cp = float(cp)

    # ── 板価格（Bid/Ask）を取得してスリッページ推定に使用 ──
    best_bid = micro_board.get("BidPrice")
    best_ask = micro_board.get("AskPrice")
    best_bid = float(best_bid) if best_bid else None
    best_ask = float(best_ask) if best_ask else None
    spread   = round(best_ask - best_bid, 1) if (best_ask and best_bid) else None

    for sig in fired:
        side = "short" if sig == "③" else "long"
        if side == "long":
            sl_price = cp - MICRO_SL
            tp_price = cp + MICRO_TP
            # long: 成行買い → Ask で約定（現在値よりAsk分不利）
            slip_est = round(best_ask - cp, 1) if best_ask else None
        else:
            sl_price = cp + MICRO_SL
            tp_price = cp - MICRO_TP
            # short: 成行売り → Bid で約定（現在値よりBid分不利）
            slip_est = round(cp - best_bid, 1) if best_bid else None

        pos = {
            "system":      sig,
            "side":        side,
            "entry_time":  now,
            "entry_price": cp,
            "sl_price":    sl_price,
            "tp_price":    tp_price,
            "signal_bid":  best_bid,
            "signal_ask":  best_ask,
            "spread":      spread,
            "slip_est":    slip_est,
        }

        prefix = f"[MICRO{sig}]"
        direction = "SHORT" if side == "short" else "LONG"
        bid_str  = f"{best_bid:.0f}" if best_bid else "---"
        ask_str  = f"{best_ask:.0f}" if best_ask else "---"
        slip_str = f"{slip_est:+.1f}pt" if slip_est is not None else "---"
        log(f"[{direction}] {prefix} 系統{sig} エントリー @ {cp:.0f}"
            f"  SL:{sl_price:.0f}  TP:{tp_price:.0f}"
            f"  Bid:{bid_str} Ask:{ask_str} Spread:{spread} SlipEst:{slip_str}")

        if MICRO_DRY_RUN:
            micro_dry_positions.append(pos)
        else:
            order_side = "sell" if side == "short" else "buy"
            oid = send_micro_order(order_side)
            if oid:
                pos["order_id"] = oid
                # ── 約定確認ポーリング（最大10回・1秒間隔）──
                fill_price = _wait_for_fill(oid, max_retries=10, interval=1.0)
                if fill_price is None:
                    # タイムアウト: 注文キャンセルしてポジションなし扱い
                    log(f"[WARN] 系統{sig} エントリー約定未確認(10秒タイムアウト) → 注文キャンセル")
                    cancel_micro_order(oid)
                else:
                    # 実約定価格基準でSL/TP価格を再計算
                    if side == "short":
                        actual_sl = fill_price + MICRO_SL
                        actual_tp = fill_price - MICRO_TP
                    else:
                        actual_sl = fill_price - MICRO_SL
                        actual_tp = fill_price + MICRO_TP
                    pos["entry_price"] = fill_price
                    pos["sl_price"]    = actual_sl
                    pos["tp_price"]    = actual_tp
                    log(f"[FILL] 系統{sig} 約定価格:{fill_price:.0f}"
                        f"  SL:{actual_sl:.0f}  TP:{actual_tp:.0f}"
                        f"  (シグナル価格との乖離:{fill_price - cp:+.0f}pt)")
                    sl_oid, tp_oid = send_micro_sl_tp_orders(order_side, actual_sl, actual_tp)
                    pos["sl_order_id"] = sl_oid
                    pos["tp_order_id"] = tp_oid
                    log(f"[ORDER] SL_OrderId:{sl_oid}  TP_OrderId:{tp_oid}")
                    micro_dry_positions.append(pos)


# =========================
# メイン
# =========================
def main():
    global last_signal_bar_time, position, dry_position, dry_day_pnl, dry_trade_log
    global micro_dry_positions, micro_dry_day_pnl, micro_dry_trade_log
    global etf_closed_today

    print("=" * 60)
    print("1570 自動売買 + マイクロ先物 dual_signal 統合Bot 起動")
    print(f"1570: 損切り:-{STOP} 利確:+{TP} LOT:{LOT}  DRY_RUN={DRY_RUN}")
    print(f"マイクロ: SL:{MICRO_SL} TP:{MICRO_TP}  系統①月火水×昼夜間/CPI除外なし  系統③short月水木金  系統④逆張りlong")
    print(f"系統①時間帯: 8/12/15/18/19/20/21/23時  系統③時間帯DST: 5/8/12/14/15/19/20/22/23時  冬: 5/12/15/19/20/21/22/23時")
    print("=" * 60)

    # ── マイクロウォームアップ（起動時に過去足を読み込む）──
    load_micro_warmup()
    check_csv_quality()

    # ── トークン取得 ──
    if not get_token():
        log("[ERR] トークン取得失敗 → 起動中止")
        return
    time.sleep(1.0)
    get_micro_symbol()   # 失敗してもマイクロ検証なしで続行
    if not register_symbol():
        log("[ERR] 銘柄登録失敗 → 起動中止")
        return

    last_csv_min       = -1
    last_micro_csv_min = -1
    last_verbose_min   = -1   # 間引きログ用（5分ごとのみ出力）

    while True:
        now     = datetime.now(JST)
        hhmm    = now.hour * 100 + now.minute
        weekday = now.weekday()
        verbose = (now.minute % 5 == 0 and now.minute != last_verbose_min)
        if verbose:
            last_verbose_min = now.minute

        # 土日・休場日は全処理スキップ（マイクロも休場）
        # 土曜00:00〜05:59は金曜夜間セッション後半のため除外
        is_sat_night_session = (weekday == 5 and hhmm < 600)
        if (weekday >= 5 or is_holiday(now.date())) and not is_sat_night_session:
            time.sleep(60)
            continue

        # ── 深夜終了判定 ──
        # 金曜(weekday==4): 夜間セッションが翌朝05:55まで継続 → 23:50では終了しない
        # 金曜以外: 23:50終了 / 土曜06:00以降: 金曜夜間後半終了 → 終了
        is_session_end = (
            (weekday != 4 and hhmm >= 2350) or  # 金曜以外の23:50終了
            (weekday == 5 and hhmm >= 600)        # 土曜06:00（金曜夜間後半終了後）
        )
        if is_session_end:
            # 金曜夜間はポジション持越しNG（土曜はマーケット休場のため）
            # weekday==4(金)は23:50より前にマイクロポジションが閉じているはずだが
            # 万一残っていた場合は強制決済する
            if micro_dry_positions:
                log("[警告] 金曜夜間終了時にマイクロポジションが残存 → 強制クローズ")
                monitor_micro_dry(now, 2350)  # hhmm=2350渡しで夜間強制決済トリガー
            if position:
                close_position("深夜強制決済")
            if micro_dry_positions:
                monitor_micro_dry(now, hhmm)  # SL/TP判定も行う
            df = bars_to_df()
            if df is not None:
                save_csv(df)
            save_log()
            mdf = micro_bars_to_df()
            if mdf is not None:
                save_micro_csv(mdf)
            # 深夜終了時はトレード都度保存済みのため追加保存不要
            _n_trades = len(micro_dry_trade_log)
            _net_pt   = micro_dry_day_pnl - _n_trades * 2.2   # 往復手数料2.2pt×トレード数を控除
            log(f"[OK] 深夜終了  1570損益:{day_pnl:+.0f}円  マイクロDRY:{_net_pt:+.1f}pt(手数料控除後, {_n_trades}trades)  保有中:{len(micro_dry_positions)}件")
            break

        # ── 認証エラー上限チェック ──
        if consecutive_auth_errors >= MAX_AUTH_ERRORS:
            log(f"[ERR] 認証エラー{MAX_AUTH_ERRORS}回連続 → 安全終了")
            if position:
                log("[WARN] 1570ポジションが残っています。手動で確認してください。")
            df = bars_to_df()
            if df is not None:
                save_csv(df)
            save_log()
            break

        # ── 板取得（共通）──
        # バー更新は取引時間内のみ（5:55〜8:45、15:40〜17:00 の ghost bar 生成を防ぐ）
        # get_board() / get_micro_board() 自体は常に実行し、
        # monitor_micro_dry() / check_micro_entry() での価格参照を維持する
        board = get_board()
        if board and is_trading_time(hhmm):
            update_bar_from_board(board)

        # マイクロ先物は昼休みなし（8:45〜15:40 通し取引）→ 昼休みチェックより前に実行
        micro_board = get_micro_board()
        if micro_board and micro_board.get("CurrentPrice") and is_trading_time(hhmm):
            update_micro_bar(micro_board)

        # ── CSV保存（5分ごと）──
        # 取引時間外（5:55〜8:45、15:40〜17:00）は保存しない
        if is_trading_time(hhmm) and now.minute % 5 == 0 and now.minute != last_csv_min and now.second < 2:
            df = bars_to_df()
            if df is not None:
                save_csv(df)
            last_csv_min = now.minute

        # マイクロ先物は昼休みなし → 昼休み中も保存を継続（is_trading_time が 845〜1540 を含むため問題なし）
        if is_trading_time(hhmm) and now.minute % 5 == 0 and now.minute != last_micro_csv_min and now.second < 2 and MICRO_SYMBOL:
            mdf = micro_bars_to_df()
            if mdf is not None:
                save_micro_csv(mdf)
            last_micro_csv_min = now.minute

        # =================================================
        # マイクロ先物 終日監視ブロック
        # 昼休み continue・1570ブロック内の continue より前に配置することで
        # hhmm>=1300 / 損失制限 / 昼休み 等の影響を受けずに必ず実行される
        # =================================================
        if MICRO_SYMBOL and micro_board and micro_board.get("CurrentPrice"):
            # ポジション損益監視（全系統）
            monitor_micro_dry(now, hhmm, verbose=verbose)
            # シグナル判定（マイクロは昼休みなし・8:45〜翌5:55通し）
            check_micro_entry(now, micro_board)

        # ── 昼休み（1570のみ）──
        if 1130 <= hhmm < 1230:
            time.sleep(1)
            continue

        # =================================================
        # 1570 日中処理ブロック（09:00〜15:00）
        # =================================================
        if 900 <= hhmm < 1500 and not etf_closed_today:

            # ── 1570 DRY ポジション監視 ──
            if DRY_RUN and dry_position is not None:
                cp = get_current_price()
                if cp is not None:
                    cp  = float(cp)
                    pnl = cp - dry_position["entry_price"]
                    reason = None
                    if cp <= dry_position["sl_price"]:
                        reason = "SL到達"
                    elif cp >= dry_position["tp_price"]:
                        reason = "TP到達"
                    elif 1125 <= hhmm < 1130:
                        reason = "昼休み前強制決済"
                    else:
                        df_tmp = bars_to_df()
                        if df_tmp is not None:
                            elapsed = int((df_tmp.index[-1] - floor_5min(dry_position["entry_time"])).total_seconds() // 300)
                            dry_position["bars"] = max(dry_position.get("bars", 0), elapsed)
                            if dry_position["bars"] >= BARS:
                                reason = "時間決済"
                    if reason:
                        dry_day_pnl += pnl
                        dry_trade_log.append({
                            "entry_time": dry_position["entry_time"].strftime("%Y-%m-%d %H:%M:%S"),
                            "exit_time": now.strftime("%Y-%m-%d %H:%M:%S"),
                            "entry_price": dry_position["entry_price"],
                            "exit_price": cp, "pnl": pnl, "reason": reason,
                        })
                        log(f"[EXIT] [DRY] 決済:{reason} @ {cp:.0f}円  損益:{pnl:+.0f}円  本日累計:{dry_day_pnl:+.0f}円")
                        dry_position = None
                        LOG_DIR.mkdir(exist_ok=True)
                        all_file = LOG_DIR / "dry_trade_log_all.csv"
                        last_row = pd.DataFrame([dry_trade_log[-1]])
                        last_row.to_csv(all_file, mode='a', header=not all_file.exists(), index=False, encoding="utf-8-sig")
                        log(f"[LOG] DRYトレードログ保存: {all_file}")
                    elif verbose:
                        log(f"[DRY] 保有中 @ {dry_position['entry_price']:.0f}円  含み:{pnl:+.0f}円  bars={dry_position.get('bars',0)}")

            # ── 1570 実ポジション監視 ──
            if position is not None:
                df = bars_to_df()
                if df is not None and len(df) >= 2:
                    elapsed_bars = int((df.index[-1] - floor_5min(position["entry_time"])).total_seconds() // 300)
                    position["bars"] = max(position["bars"], elapsed_bars)
                cp = get_current_price()
                if cp is not None and verbose:
                    log(f"保有中 含み損益: {float(cp) - position['entry_price']:+.0f}円 / bars={position['bars']}")
                if 1125 <= hhmm <= 1130:
                    close_position("昼休み前強制決済")
                    time.sleep(1)
                    time.sleep(POLL_SEC)
                    continue
                if position["bars"] >= BARS:
                    close_position("時間決済")
                time.sleep(POLL_SEC)
                continue

            # 9:15まではデータ収集のみ
            if hhmm < 915:
                time.sleep(POLL_SEC)
                continue

            # ── 1570 損失制限チェック ──
            if day_pnl <= -MAX_LOSS_DAY:
                log(f"本日損失上限到達 {day_pnl:+.0f}円 → 新規停止")
                time.sleep(POLL_SEC)
                continue
            if consec_loss >= MAX_CONSEC_LOSS:
                log(f"{MAX_CONSEC_LOSS}連敗 → 新規停止")
                time.sleep(POLL_SEC)
                continue
            if hhmm >= 1300:
                time.sleep(POLL_SEC)
                continue

            # ── 1570 シグナル判定 ──
            df = bars_to_df()
            if df is None or len(df) < 3:
                if verbose:
                    log(f"[WAIT] 1570データ蓄積中 ({len(df) if df is not None else 0}/3本)")
                time.sleep(POLL_SEC)
                continue

            df = add_indicators(df)
            latest_bar_time = df.index[-1]
            if last_signal_bar_time != latest_bar_time:
                last_signal_bar_time = latest_bar_time
                sig = check_signal(df)

                if sig is None:
                    cp = get_current_price()
                    price_str = f"{float(cp):.0f}円" if cp else "取得失敗"
                    log(f"[1570] → シグナルなし (現在値:{price_str} bars:{len(df)}本)")

                if sig == "long":
                    cp    = get_current_price()
                    cp_f  = float(cp) if cp is not None else None
                    entered = "no"
                    if DRY_RUN:
                        if dry_position is None:
                            if cp_f is not None:
                                dry_position = {
                                    "entry_time": now, "entry_price": cp_f,
                                    "sl_price": cp_f - STOP, "tp_price": cp_f + TP, "bars": 0,
                                }
                                log(f"[LONG] [DRY] ロングエントリー @ {cp_f:.0f}円  SL:{cp_f-STOP:.0f}  TP:{cp_f+TP:.0f}")
                                entered = "yes"
                        else:
                            log("[LONG] [DRY] シグナルあり（仮想ポジション保有中のためスキップ）")
                    else:
                        log("[LONG] ロングシグナル")
                        entry_long()
                        entered = "yes"
                    signal_log.append({
                        "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
                        "price":    cp_f,
                        "signal":   "long",
                        "entered":  entered,
                    })
                    save_signal_log()

        # ── 1570 日中終了処理（15:00になったら1回だけ）──
        if hhmm >= 1500 and not etf_closed_today:
            if position:
                close_position("大引け強制決済")
            df = bars_to_df()
            if df is not None:
                save_csv(df)
            save_log()
            log(f"[OK] 1570日中終了  損益:{day_pnl:+.0f}円")
            etf_closed_today = True

        time.sleep(POLL_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("[STOP] 手動停止")
        if position:
            log("[WARN] 1570ポジションが残っています")
        df = bars_to_df()
        if df is not None:
            save_csv(df)
        save_log()
        mdf = micro_bars_to_df()
        if mdf is not None:
            save_micro_csv(mdf)
        # トレード都度保存済みのため追加保存不要
