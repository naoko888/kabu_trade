"""
zh_order.py
発注・約定確認・SL/TP発注・キャンセル・注文状態確認。
kabuステーションへの注文送受信を担当。判断ロジックは持たない。
依存: zh_config, zh_utils, zh_api
"""
import time

import zh_api
from zh_config import API_PASSWORD, LOT, DRY_RUN
from zh_utils import log, safe_json


def send_entry_order(side: str, session_exchange: int, trade_type: int = 1) -> str | None:
    """成行発注（先物専用 /sendorder/future）. trade_type=1:新規 / 2:返済. DRY_RUN時は"DRY"を返す"""
    if DRY_RUN:
        log(f"[DRY] 成行{side} スキップ")
        return "DRY"
    body = {
        "Password": API_PASSWORD, "Symbol": zh_api.SYMBOL,
        "Exchange": session_exchange,
        "TradeType": trade_type,
        "TimeInForce": 2,    # FAK（成行はFAK必須）
        "Side": "2" if side == "buy" else "1",   # 先物: 1=売 / 2=買（株式と逆）
        "Qty": LOT, "Price": 0, "ExpireDay": 0,
        "FrontOrderType": 120,
    }
    if trade_type == 2:
        body["ClosePositionOrder"] = 0  # 返済: FIFO（日付古い順）
    res = zh_api.request_with_reauth("POST", "/sendorder/future", json_body=body)
    if res:
        oid = safe_json(res).get("OrderId", "")
        log(f"[OK] 成行発注({side} TradeType:{trade_type}) OrderId:{oid}")
        return oid
    log(f"[ERR] 成行発注失敗: side={side}")
    return None


def wait_for_fill(order_id: str, max_retries: int = 10, interval: float = 1.0) -> float | None:
    """成行約定確認ポーリング。約定平均価格を返す。タイムアウト時None"""
    for attempt in range(1, max_retries + 1):
        time.sleep(interval)
        res = zh_api.request_with_reauth("GET", f"/orders?product=3&id={order_id}")
        if res is None:
            continue
        orders = safe_json(res)
        if not isinstance(orders, list) or not orders:
            log(f"[POLL] 注文データなし ({attempt}/{max_retries})")
            continue
        order   = orders[0]
        state   = order.get("State")
        cum_qty = float(order.get("CumQty") or 0)
        # State: 1=待機 2=処理中 3=処理済 4=訂正取消待ち 5=終了(全約定/取消)
        if state == 5 and cum_qty <= 0:
            log(f"[WARN] 注文取消/失効 OrderId:{order_id}")
            return None
        if cum_qty >= LOT:
            details = order.get("Details") or []
            qty_sum = val_sum = 0.0
            for d in details:
                log(f"[DEBUG] RecType: {d.get('RecType')}, Price: {d.get('Price')}")  # 一時調査用
                if d.get("RecType") == 8:  # 約定明細
                    q = float(d.get("Qty") or 0)
                    p = float(d.get("Price") or 0)
                    if q > 0 and p > 0:
                        qty_sum += q
                        val_sum += q * p
            if qty_sum > 0:
                avg = val_sum / qty_sum
                log(f"[POLL] 約定確認 価格:{avg:.0f} ({attempt}回)")
                return avg
        log(f"[POLL] 約定待ち State={state} CumQty={cum_qty} ({attempt}/{max_retries})")
    log(f"[WARN] 約定確認タイムアウト OrderId:{order_id}")
    return None


def get_existing_execution_ids(entry_side: str) -> set | None:
    """エントリー前に既存建玉のExecutionID一覧を取得。通信失敗時はNoneを返す（空setと区別）"""
    broker_side = "2" if entry_side == "buy" else "1"
    res = zh_api.request_with_reauth(
        "GET", f"/positions?product=3&symbol={zh_api.SYMBOL}&side={broker_side}&addinfo=false")
    if not res:
        return None
    data = safe_json(res)
    if not isinstance(data, list):
        return None
    return {str(p.get("ExecutionID", "")) for p in data
            if float(p.get("LeavesQty", 0)) > 0}


def get_hold_id(entry_side: str, existing_ids: set | None) -> str | None:
    """エントリー約定後、既存IDと比較して新規建玉のExecutionID(HoldID)を返す"""
    broker_side = "2" if entry_side == "buy" else "1"
    res = zh_api.request_with_reauth(
        "GET", f"/positions?product=3&symbol={zh_api.SYMBOL}&side={broker_side}&addinfo=false")
    if not res:
        return None
    data = safe_json(res)
    if not isinstance(data, list):
        return None
    if existing_ids is None:
        log("[WARN] HoldID: 事前ID取得失敗 → 最新ExecutionIDで代替")
        candidates = [p for p in data if float(p.get("LeavesQty", 0)) > 0]
    else:
        candidates = [p for p in data
                      if float(p.get("LeavesQty", 0)) > 0
                      and str(p.get("ExecutionID", "")) not in existing_ids]
    if not candidates:
        return None
    newest = max(candidates, key=lambda p: str(p.get("ExecutionID", "")))
    hid = str(newest.get("ExecutionID", ""))
    log(f"[HOLD] HoldID取得: {hid}")
    return hid if hid else None



def send_tp_order(side: str, tp_price: float, session_exchange: int,
                  hold_id: str | None = None) -> str | None:
    """TP指値発注（先物専用 /sendorder/future）. side=エントリー方向("buy"|"sell")"""
    if DRY_RUN:
        return None
    exit_side = "2" if side == "sell" else "1"  # 先物: 1=売 / 2=買（株式と逆）
    body = {
        "Password": API_PASSWORD, "Symbol": zh_api.SYMBOL,
        "Exchange": session_exchange,
        "TradeType": 2,
        "TimeInForce": 1,
        "Side": exit_side,
        "Qty": LOT, "Price": tp_price, "ExpireDay": 0,
        "FrontOrderType": 20,
    }
    if hold_id:
        body["ClosePositions"] = [{"HoldID": hold_id, "Qty": LOT}]
    else:
        body["ClosePositionOrder"] = 0
    res = zh_api.request_with_reauth("POST", "/sendorder/future", json_body=body)
    if res:
        oid = safe_json(res).get("OrderId", "")
        log(f"[OK] TP指値発注 Exchange:{session_exchange} Price:{tp_price:.0f} OrderId:{oid}")
        return oid
    log(f"[ERR] TP発注失敗 Price:{tp_price:.0f}")
    return None


def cancel_order(order_id: str) -> bool:
    if not order_id:
        return False
    res = zh_api.request_with_reauth("PUT", "/cancelorder",
                                     json_body={"OrderId": order_id})
    if res:
        result_code = safe_json(res).get("Result", -1)
        if result_code == 0:
            log(f"[OK] 注文キャンセル OrderId:{order_id}")
            return True
        log(f"[WARN] キャンセル受付エラー Result:{result_code} OrderId:{order_id}")
        return False
    log(f"[WARN] 注文キャンセル失敗 OrderId:{order_id}")
    return False


def check_order_active(order_id: str) -> bool | None:
    """True=生存確認 / False=非アクティブ確定(CumQty=0) / None=通信失敗・不明"""
    res = zh_api.request_with_reauth("GET", f"/orders?product=3&id={order_id}&details=false")
    if not res:
        return None  # 通信失敗 → 不明
    orders = safe_json(res)
    if not isinstance(orders, list) or not orders:
        return None  # 空/異常 → 不明
    order = orders[0]
    state = order.get("State")
    if state in (1, 2, 3):
        return True  # 待機・処理中・処理済 → 生存
    if state == 5 and float(order.get("CumQty") or 0) == 0:
        return False  # 約定なし終了 → 発注エラー/取消確定
    return None  # State=4, State=5+約定済, その他 → 不明
