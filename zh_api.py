"""
zh_api.py
kabuステーションAPIとの通信を一元管理。
認証・銘柄管理・板取得・全HTTPリクエストはここを通す。
依存: zh_config, zh_utils のみ
"""
import requests
import time

from zh_config import (
    API_BASE, API_PASSWORD,
    DERIV_MONTH, NEXT_DERIV_MONTH,
    MICRO_CSV,
    MAX_AUTH_ERRORS, REAUTH_COOLDOWN,
)
from zh_utils import log, safe_json

# ==========================================================================
# 状態（このモジュールが所有）
# ==========================================================================
token:    str | None = None
SYMBOL:   str | None = None
EXCHANGE: int | None = None

consecutive_auth_errors: int   = 0
last_reauth_time:        float = 0.0

# Phase 4 で zh_bar.py に移動予定
_collect_symbols: dict = {}   # {symbol: csv_path}
_bar_state:       dict = {}   # {symbol: {current, completed, last_vol}}

# ==========================================================================
# 内部ユーティリティ
# ==========================================================================
def headers() -> dict:
    return {"Content-Type": "application/json", "X-API-KEY": token}

# ==========================================================================
# 認証
# ==========================================================================
def get_token() -> bool:
    global token
    try:
        res = requests.post(f"{API_BASE}/token",
                            json={"APIPassword": API_PASSWORD}, timeout=10)
        if res.status_code == 200:
            token = res.json()["Token"]
            log(f"[OK] トークン取得: {token[:8]}...")
            return True
    except Exception as e:
        log(f"[ERR] トークン取得例外: {e}")
    log("[ERR] トークン取得失敗")
    return False

def request_with_reauth(method, path, *, json_body=None, retry=1):
    global consecutive_auth_errors, last_reauth_time
    url = f"{API_BASE}{path}"
    try:
        res = requests.request(
            method=method, url=url,
            headers=headers() if token else {"Content-Type": "application/json"},
            json=json_body, timeout=10,
        )
    except requests.RequestException as e:
        log(f"[WARN] 通信エラー: {e}")
        return None
    if res.status_code == 200:
        consecutive_auth_errors = 0
        return res
    txt = res.text or ""
    is_auth = res.status_code == 401 or "APIキー不一致" in txt or "Unauthorized" in txt
    if is_auth and retry > 0:
        now_ts = time.time()
        if now_ts - last_reauth_time >= REAUTH_COOLDOWN:
            log("[RETRY] トークン再取得")
            last_reauth_time = now_ts
            if get_token():
                time.sleep(1)
                register_symbol()
                consecutive_auth_errors = 0
                return request_with_reauth(method, path, json_body=json_body, retry=retry - 1)
    if is_auth:
        consecutive_auth_errors += 1
    log(f"[WARN] APIエラー {res.status_code}: {txt[:100]}")
    return None

# ==========================================================================
# 銘柄管理
# ==========================================================================
def get_symbol() -> bool:
    global SYMBOL, EXCHANGE
    url = f"{API_BASE}/symbolname/future?FutureCode=NK225micro&DerivMonth={DERIV_MONTH}"
    try:
        res = requests.get(url, headers=headers(), timeout=10)
    except Exception as e:
        log(f"[WARN] シンボル取得失敗: {e}")
        log("[ERR] シンボル取得失敗")
        return False
    if res.status_code != 200:
        log("[ERR] シンボル取得失敗")
        return False
    data = res.json()
    sym = data.get("Symbol")
    if not sym:
        log("[ERR] シンボル取得失敗")
        return False
    SYMBOL   = sym
    EXCHANGE = data.get("Exchange") or 2
    log(f"[OK] シンボル取得: {SYMBOL}  Exchange={EXCHANGE}")
    return True

def _init_collect_symbols() -> None:
    global _collect_symbols, _bar_state
    months = {DERIV_MONTH: str(MICRO_CSV)}
    if NEXT_DERIV_MONTH:
        months[NEXT_DERIV_MONTH] = str(
            MICRO_CSV.parent / f"micro_5min_{NEXT_DERIV_MONTH}.csv"
        )
    for deriv, csv_path in months.items():
        if deriv == DERIV_MONTH and SYMBOL:
            _collect_symbols[SYMBOL] = csv_path
            _bar_state[SYMBOL] = {"current": None, "completed": [], "last_vol": None}
            continue
        for code in ["NK225micro", "NK225mini"]:
            try:
                res = requests.get(
                    f"{API_BASE}/symbolname/future?FutureCode={code}&DerivMonth={deriv}",
                    headers=headers(), timeout=10,
                )
                sym = res.json().get("Symbol") if res.status_code == 200 else None
            except Exception:
                sym = None
            if sym:
                _collect_symbols[sym] = csv_path
                _bar_state[sym] = {"current": None, "completed": [], "last_vol": None}
                log(f"[OK] 収集限月登録: {deriv} → {sym}")
                break

def register_symbol(retries: int = 5) -> bool:
    syms = []
    if SYMBOL:
        syms.append({"Symbol": SYMBOL, "Exchange": EXCHANGE})
    for s in _collect_symbols:
        if s != SYMBOL:
            syms.append({"Symbol": s, "Exchange": EXCHANGE or 2})
    if not syms:
        return False
    for i in range(retries):
        res = request_with_reauth("PUT", "/register", json_body={"Symbols": syms})
        if res and res.status_code == 200:
            log(f"[OK] 銘柄登録({len(syms)}銘柄)")
            return True
        log(f"[ERR] 銘柄登録失敗({i+1}/{retries})")
        time.sleep(2)
    return False

# ==========================================================================
# 板取得
# ==========================================================================
def get_board() -> dict | None:
    if not SYMBOL:
        return None
    res = request_with_reauth("GET", f"/board/{SYMBOL}@{EXCHANGE}")
    return safe_json(res) if res else None
