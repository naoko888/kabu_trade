"""
zh_monitor.py
ポジション監視・決済・SL/TP管理・ブローカー整合・ポジション永続化。
保有ポジションに関するすべての判断と実行を担当する。
依存: zh_config, zh_utils, zh_api, zh_order, ZAIHOU_signals
"""
import threading
import time
import json
from datetime import datetime
import pandas as pd
import ZAIHOU_signals as sig
import zh_api
import zh_order
from zh_config import (
    DRY_RUN, LOT, API_PASSWORD,
    PT_TO_YEN, COMMISSION_YEN,
    OPEN_POS_FILE, LOG_DIR,
    DD_LIMIT_YEN, TICK_UNIT, JST,
)
from zh_utils import log, send_discord, safe_json, _sess_exchange, _board_price

# ==========================================================================
# 状態（このモジュールが所有）
# ==========================================================================
_positions_lock = threading.Lock()
positions:  list  = []
day_pnl:    float = 0.0
trade_log:  list  = []

monthly_pnl_yen: float       = 0.0
monthly_stopped: bool        = False
monthly_ym:      tuple | None = None

# ==========================================================================
# 系統別パラメータマップ
# ==========================================================================
_SL_MAP = {"①": sig.S1_SL, "③": sig.S3_SL, "④": sig.S4_SL, "⑤": sig.S5_SL}
_TP_MAP = {"①": sig.S1_TP, "③": sig.S3_TP, "④": sig.S4_TP, "⑤": sig.S5_TP}
_MH_MAP = {"①": sig.S1_MAX_HOLD, "③": sig.S3_MAX_HOLD,
           "④": sig.S4_MAX_HOLD, "⑤": sig.S5_MAX_HOLD}

# ==========================================================================
# WebSocket 価格 tick コールバック（zh_bar から呼ばれる）
# ==========================================================================
def _ws_check_sl_tp(price: float) -> None:
    if not positions:
        return
    now  = datetime.now(JST)
    hhmm = now.hour * 100 + now.minute
    monitor_positions(now, hhmm, {"CurrentPrice": price})

# ==========================================================================
# ポジション永続化 / 月次損益復元
# ==========================================================================
def save_positions() -> None:
    with open(OPEN_POS_FILE, "w", encoding="utf-8") as f:
        json.dump(positions, f, ensure_ascii=False, default=str)

def load_positions() -> None:
    """DRY_RUNモード用: JSONからポジションを読み込む"""
    global positions
    if not OPEN_POS_FILE.exists():
        return
    with open(OPEN_POS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    for p in data:
        if isinstance(p.get("entry_time"), str):
            p["entry_time"] = datetime.fromisoformat(p["entry_time"])
    positions = data

def restore_from_broker() -> None:
    """本番モード用: ブローカーAPIを正としてポジションを復元する（J2）
    コンテキストファイル（open_positions.json）を補助情報として使用。
    ブローカーにない = 停止中に約定済みとして破棄。hold_idなし = 除外。
    """
    global positions

    # コンテキストファイルを読み込む（hold_id をキーに）
    context_map: dict = {}
    if OPEN_POS_FILE.exists():
        try:
            with open(OPEN_POS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            for p in data:
                if isinstance(p.get("entry_time"), str):
                    p["entry_time"] = datetime.fromisoformat(p["entry_time"])
                hid = p.get("hold_id")
                if hid:
                    context_map[hid] = p
        except Exception as e:
            log(f"[WARN] コンテキストファイル読み込み失敗: {e}")

    # ブローカーからポジション取得
    res = zh_api.request_with_reauth(
        "GET", f"/positions?product=3&symbol={zh_api.SYMBOL}&addinfo=false")
    if res is None:
        log("[WARN] restore_from_broker: /positions照会失敗 → コンテキストのみで復元")
        positions = list(context_map.values())
        return
    pos_data = safe_json(res)
    if not isinstance(pos_data, list):
        log("[WARN] restore_from_broker: /positions異常レスポンス → コンテキストのみで復元")
        positions = list(context_map.values())
        return

    # ブローカー建玉と照合して復元
    broker_hids = {
        str(p.get("ExecutionID"))
        for p in pos_data if float(p.get("LeavesQty", 0)) > 0
    }
    recovered = []
    for bp in pos_data:
        if float(bp.get("LeavesQty", 0)) <= 0:
            continue
        hid = str(bp.get("ExecutionID", ""))
        if hid in context_map:
            pos = context_map[hid].copy()
            pos["entry_price"] = float(bp.get("Price", pos.get("entry_price", 0)))
            recovered.append(pos)
            log(f"[RESUME] 系統{pos['system']} {pos['side']} @ {pos['entry_price']}  HoldID:{hid}")
        else:
            side = "short" if str(bp.get("Side", "")) == "1" else "long"
            msg = (f"⚠️ 不明ポジション(コンテキストなし) "
                   f"HoldID:{hid} Side:{side} Price:{bp.get('Price')} → 監視不可・手動確認要")
            log(f"[RESUME] {msg}"); send_discord(msg)

    # コンテキストにあってブローカーにない → 停止中に約定済み
    for hid, ctx in context_map.items():
        if hid not in broker_hids:
            msg = (f"⚠️ 系統{ctx.get('system','?')} 起動時消滅検知"
                   f"(停止中に約定済みの可能性) HoldID:{hid}")
            log(f"[RESUME] {msg}"); send_discord(msg)

    positions = recovered
    save_positions()
    log(f"[RESUME] ブローカーから{len(positions)}件復元")

def restore_monthly_pnl() -> None:
    global monthly_pnl_yen, monthly_stopped, monthly_ym
    all_file = LOG_DIR / "micro_dry_log_all.csv"
    if not all_file.exists():
        return
    try:
        df = pd.read_csv(all_file)
        if not {"entry_time", "pnl", "system"}.issubset(df.columns):
            return
        df["entry_time"] = pd.to_datetime(df["entry_time"], errors="coerce")
        df["pnl"]        = pd.to_numeric(df["pnl"], errors="coerce")
        df = df.dropna(subset=["entry_time", "pnl"])
        now = datetime.now(JST)
        ym  = (now.year, now.month)
        tgt = df[
            (df["entry_time"].dt.year  == ym[0]) &
            (df["entry_time"].dt.month == ym[1]) &
            (df["system"].astype(str).isin(["①", "③", "④", "⑤"]))
        ]
        total = float((tgt["pnl"] * PT_TO_YEN - COMMISSION_YEN).round(0).sum())
        monthly_pnl_yen = total
        monthly_ym      = ym
        if total <= DD_LIMIT_YEN:
            monthly_stopped = True
        log(f"[RESUME] 月次損益復元: {total:,.0f}円  DD停止={monthly_stopped}")
    except Exception as e:
        log(f"[WARN] 月次損益復元失敗: {e}")

# ==========================================================================
# ポジション監視
# ==========================================================================
def monitor_positions(now: datetime, hhmm: int, board=None) -> None:
    if not positions:
        return
    with _positions_lock:
        _monitor_inner(now, hhmm, board)

def _monitor_inner(now: datetime, hhmm: int, board) -> None:
    global positions, day_pnl, monthly_pnl_yen, monthly_stopped

    b  = board if board is not None else zh_api.get_board()
    cp = _board_price(b)
    if cp is None:
        return
    cp = float(cp)

    still_open = []
    for pos in positions:
        side   = pos["side"]
        system = pos["system"]
        pnl    = (cp - pos["entry_price"]) if side == "long" else (pos["entry_price"] - cp)
        reason = None

        # ── TP/SL ──
        if side == "long":
            if   cp <= pos["sl_price"]:         reason = "SL到達"
            elif cp >= pos["tp_price"] + TICK_UNIT: reason = "TP到達"
        else:
            if   cp >= pos["sl_price"]:         reason = "SL到達"
            elif cp <= pos["tp_price"] - TICK_UNIT: reason = "TP到達"

        # ── セッション終了強制決済 ──
        if reason is None:
            _gap = (1530 <= hhmm < 1700) or (540 <= hhmm < 845)  # 暫定: 15:30/5:40強制決済
            gap_trigger = _gap
            if gap_trigger:
                if pos.get("_gap_triggered") is None:
                    pos["_gap_triggered"] = now
                    reason = "セッション終了強制決済"

        # ── 金曜夜間強制決済 (土曜06:00〜) ──
        if reason is None and now.weekday() == 5 and hhmm >= 600:
            reason = "金曜夜間強制決済"

        # ── ①: 23:50強制決済 ──
        if reason is None and system == "①" and hhmm >= 2350:
            reason = "夜間終了強制決済"

        # ── MAX_HOLD 時間決済 ──
        if reason is None:
            elapsed = int((now - pos["entry_time"]).total_seconds() / 300)
            if elapsed >= pos.get("max_hold", _MH_MAP.get(system, 9999)):
                reason = "TIME決済(MAX_HOLD)"

        if reason:
            # ── 本番モード処理 ──
            if not DRY_RUN:
                _sess      = _sess_exchange(hhmm)
                close_side = "buy" if side == "short" else "sell"
                if reason == "セッション終了強制決済":
                    _sess = 23 if (1530 <= hhmm < 1700) else 24  # 暫定: 15:30基準

                if reason == "TP到達":
                    # GET /positions でポジションが消えているか確認
                    hid = pos.get("hold_id")
                    tp_confirmed = False
                    res_pos = zh_api.request_with_reauth("GET", f"/positions?product=3&symbol={zh_api.SYMBOL}&addinfo=false")
                    if res_pos is not None:
                        pos_data = safe_json(res_pos)
                        if isinstance(pos_data, list):
                            if hid:
                                tp_confirmed = not any(
                                    str(p.get("ExecutionID")) == hid and float(p.get("LeavesQty", 0)) > 0
                                    for p in pos_data
                                )
                            else:
                                bside = "1" if side == "short" else "2"
                                tp_confirmed = not any(
                                    str(p.get("Side", "")) == bside and float(p.get("LeavesQty", 0)) > 0
                                    for p in pos_data
                                )
                    if not tp_confirmed:
                        log(f"[WAIT] 系統{system} TP未約定(ポジション残存) → 次ティック再確認")
                        still_open.append(pos)
                        continue
                    cp  = pos["tp_price"]
                    pnl = (cp - pos["entry_price"]) if side == "long" else (pos["entry_price"] - cp)
                    log(f"[LIVE] 系統{system} TP約定確認(ポジション消滅):{cp:.0f}")
                elif reason == "SL到達":
                    # ソフトウェアSL: TP キャンセル → HoldQty==0 確認 → ClosePositions
                    tp_oid     = pos.get("tp_order_id")
                    hid        = pos.get("hold_id")
                    _tp_closed = False

                    # ─ アクション前ブローカー確認（マニュアル準拠）─
                    if _is_position_already_closed(hid, tp_oid):
                        cp         = pos["tp_price"]
                        pnl        = (cp - pos["entry_price"]) if side == "long" else (pos["entry_price"] - cp)
                        reason     = "TP約定(SL検知後確認)"
                        _tp_closed = True
                        log(f"[SL] 系統{system} TP約定済み確認 → {cp:.0f}で記録")

                    if not _tp_closed:
                        if tp_oid:
                            zh_order.cancel_order(tp_oid)
                        if tp_oid and hid:
                            released = _wait_for_hold_release(hid)
                            if released is None:
                                cp         = pos["tp_price"]
                                pnl        = (cp - pos["entry_price"]) if side == "long" else (pos["entry_price"] - cp)
                                reason     = "TP約定(SL_POLL確認)"
                                _tp_closed = True
                                log(f"[SL_POLL] 系統{system} TP約定済み確認(安全網) → {cp:.0f}で記録")
                            elif not released:
                                send_discord(f"🚨最緊急 系統{system} SL HoldID解放タイムアウト 手動決済要")
                                still_open.append(pos)
                                continue

                    if not _tp_closed:
                        if hid:
                            _body = {
                                "Password": API_PASSWORD, "Symbol": zh_api.SYMBOL,
                                "Exchange": _sess,
                                "TradeType": 2, "TimeInForce": 2,
                                "Side": "1" if close_side == "sell" else "2",
                                "Qty": LOT, "Price": 0, "ExpireDay": 0,
                                "FrontOrderType": 120,
                                "ClosePositions": [{"HoldID": hid, "Qty": LOT}],
                            }
                            res_cl = zh_api.request_with_reauth("POST", "/sendorder/future", json_body=_body)
                            rj = safe_json(res_cl) if res_cl else {}
                            close_oid = rj.get("OrderId") if rj.get("Result") == 0 else None
                            log(f"[SL_POLL] ClosePositions結果 Result:{rj.get('Result')} OrderId:{close_oid}")
                        else:
                            close_oid = zh_order.send_entry_order(close_side, _sess, trade_type=2)
                        if close_oid and close_oid != "DRY":
                            fill = zh_order.wait_for_fill(close_oid, max_retries=5, interval=1.0)
                            if fill is not None:
                                cp  = float(fill)
                                pnl = (cp - pos["entry_price"]) if side == "long" else (pos["entry_price"] - cp)
                                log(f"[LIVE] 系統{system} SL成行決済:{cp:.0f}")
                            else:
                                send_discord(f"🚨最緊急 系統{system} SL決済未確認 手動決済要")
                                still_open.append(pos)
                                continue
                        else:
                            send_discord(f"🚨最緊急 系統{system} SL決済失敗 手動決済要")
                            still_open.append(pos)
                            continue
                    # TP は上記で cancel 実施済み（または _is_position_already_closed で検知）
                else:
                    # 強制決済: TPキャンセル → HoldQty解放確認 → HoldID名指し決済（SLと同方式）
                    tp_ok = zh_order.cancel_order(pos["tp_order_id"]) if pos.get("tp_order_id") else True
                    if not tp_ok:
                        _emergency_flat(system, _sess)
                        still_open.append(pos)
                        continue
                    hid = pos.get("hold_id")
                    if pos.get("tp_order_id") and hid:
                        if _wait_for_hold_release(hid) is False:
                            msg = f"🚨最緊急 系統{system} 強制決済 HoldID解放タイムアウト 手動決済要"
                            log(f"[ALERT] {msg}"); send_discord(msg)
                            still_open.append(pos)
                            continue
                        # released is None → ポジション消滅 → 後続のポジション存在確認で処理
                    # ポジション存在確認（SL/TPが先に約定済みの可能性を排除）
                    res_pos = zh_api.request_with_reauth("GET", f"/positions?product=3&symbol={zh_api.SYMBOL}&addinfo=false")
                    if res_pos is not None:
                        pos_data = safe_json(res_pos)
                        if isinstance(pos_data, list):
                            if hid:
                                still_exists = any(
                                    str(p.get("ExecutionID")) == hid and float(p.get("LeavesQty", 0)) > 0
                                    for p in pos_data
                                )
                            else:
                                bside = "1" if side == "short" else "2"
                                still_exists = any(
                                    str(p.get("Side", "")) == bside and float(p.get("LeavesQty", 0)) > 0
                                    for p in pos_data
                                )
                            if not still_exists:
                                msg = f"⚠️ 系統{system} 強制決済スキップ(ポジションなし SL/TP約定済みの可能性)"
                                log(f"[WARN] {msg}"); send_discord(msg)
                                continue
                    if hid:
                        _body = {
                            "Password": API_PASSWORD, "Symbol": zh_api.SYMBOL,
                            "Exchange": _sess,
                            "TradeType": 2, "TimeInForce": 2,
                            "Side": "1" if close_side == "sell" else "2",
                            "Qty": LOT, "Price": 0, "ExpireDay": 0,
                            "FrontOrderType": 120,
                            "ClosePositions": [{"HoldID": hid, "Qty": LOT}],
                        }
                        res_cl = zh_api.request_with_reauth("POST", "/sendorder/future", json_body=_body)
                        rj = safe_json(res_cl) if res_cl else {}
                        close_oid = rj.get("OrderId") if rj.get("Result") == 0 else None
                        log(f"[FORCE] ClosePositions Result:{rj.get('Result')} OrderId:{close_oid}")
                    else:
                        close_oid = zh_order.send_entry_order(close_side, _sess, trade_type=2)
                    if close_oid and close_oid != "DRY":
                        fill = zh_order.wait_for_fill(close_oid)
                        if fill is None:
                            msg = f"⚠️緊急 系統{system} {reason} 約定未確認 OrderId:{close_oid}"
                            log(f"[ALERT] {msg}"); send_discord(msg)
                            still_open.append(pos)
                            continue
                        cp  = float(fill)
                        pnl = (cp - pos["entry_price"]) if side == "long" else (pos["entry_price"] - cp)

            # ── 共通: ログ記録 ──
            day_pnl += pnl
            trade_yen = round(pnl * PT_TO_YEN - COMMISSION_YEN, 0)
            if (pos["entry_time"].year, pos["entry_time"].month) == (now.year, now.month):
                monthly_pnl_yen += trade_yen

            trig = pos.get("_gap_triggered")
            trig_s = trig.strftime("%H:%M:%S") if trig else "-"
            log(f"[EXIT] 系統{system} {side} 決済:{reason}(triggered:{trig_s})"
                f" @ {cp:.0f}  損益:{pnl:+.0f}pt  本日:{day_pnl:+.0f}pt")
            send_discord(f"🔔 系統{system} 決済:{reason} @ {cp:.0f}"
                         f"  損益:{pnl:+.0f}pt  本日:{day_pnl:+.0f}pt")
            log(f"[全系統] 月次損益:{monthly_pnl_yen:,.0f}円  DD上限:{DD_LIMIT_YEN:,.0f}円")

            if monthly_pnl_yen <= DD_LIMIT_YEN and not monthly_stopped:
                monthly_stopped = True
                msg = f"⚠️ 月次DD上限到達({monthly_pnl_yen:,.0f}円) → 今月全系統停止"
                log(f"[全系統] {msg}"); send_discord(msg)

            trade_log.append({
                "system":      system, "side": side,
                "entry_time":  pos["entry_time"].strftime("%Y-%m-%d %H:%M:%S"),
                "exit_time":   now.strftime("%Y-%m-%d %H:%M:%S"),
                "entry_price": pos["entry_price"], "exit_price": cp,
                "pnl":         round(pnl, 1), "reason": reason,
                "signal_bid":  pos.get("signal_bid"), "signal_ask": pos.get("signal_ask"),
                "spread":      pos.get("spread"),      "slip_est":   pos.get("slip_est"),
            })
            LOG_DIR.mkdir(exist_ok=True)
            all_file = LOG_DIR / "micro_dry_log_all.csv"
            pd.DataFrame([trade_log[-1]]).to_csv(
                all_file, mode="a", header=not all_file.exists(),
                index=False, encoding="utf-8-sig",
            )
        else:
            still_open.append(pos)

    positions = still_open
    save_positions()

# ==========================================================================
# HoldID 拘束解放待ち（SL決済前に使用）
# ==========================================================================
def _wait_for_hold_release(hid: str, max_retries: int = 10, interval: float = 0.3) -> bool:
    """TP キャンセル後、HoldQty==0（HoldID解放）をポーリングで確認する。
    解放確認できれば True、タイムアウトは False。
    ※HoldQty==0 後に ClosePositions が必ず成功するかは実機検証中。"""
    for attempt in range(1, max_retries + 1):
        time.sleep(interval)
        res = zh_api.request_with_reauth(
            "GET", f"/positions?product=3&symbol={zh_api.SYMBOL}&addinfo=false"
        )
        if res is None:
            continue
        pd_ = safe_json(res)
        if not isinstance(pd_, list):
            continue
        tgt = next((p for p in pd_ if str(p.get("ExecutionID")) == hid), None)
        if tgt is None:
            log(f"[SL_POLL] HoldID:{hid} ポジション消滅(TP約定済み) attempt={attempt}")
            return None  # TP約定済み → ClosePositions不要
        hold = float(tgt.get("HoldQty", 1))
        log(f"[SL_POLL] HoldID:{hid} HoldQty={hold} attempt={attempt}")
        if hold == 0:
            log(f"[SL_POLL] HoldQty==0 確認 → ClosePositions送信へ")
            return True
    log(f"[SL_POLL] タイムアウト HoldID:{hid} ({max_retries}回)")
    return False


def _is_position_already_closed(hid: str | None, order_id: str | None) -> bool:
    """アクション前にブローカー状態を確認し、ポジションが既にクローズ済みかを返す。
    ① /positions : LeavesQty>0 のポジションが存在するか（マニュアル: 残数量）
    ② /orders    : State=5 かつ CumQty>0 か（マニュアル: 終了＋全約定）
    将来: SL発動前(order_id=tp_oid) / TP発動前(order_id=sl_oid) 共通で使用可"""
    if hid:
        res = zh_api.request_with_reauth(
            "GET", f"/positions?product=3&symbol={zh_api.SYMBOL}&addinfo=false"
        )
        if res is not None:
            pd_ = safe_json(res)
            if isinstance(pd_, list):
                exists = any(
                    str(p.get("ExecutionID")) == hid and float(p.get("LeavesQty", 0)) > 0
                    for p in pd_
                )
                if not exists:
                    log(f"[BROKER] HoldID:{hid} ポジション消滅確認 → クローズ済み")
                    return True
    if order_id:
        res = zh_api.request_with_reauth(
            "GET", f"/orders?product=3&id={order_id}&details=false"
        )
        if res is not None:
            orders = safe_json(res)
            if isinstance(orders, list) and orders:
                o = orders[0]
                if o.get("State") == 5 and float(o.get("CumQty") or 0) > 0:
                    log(f"[BROKER] OrderId:{order_id} 注文約定済み確認 → クローズ済み")
                    return True
    return False


# ==========================================================================
# 緊急フラット
# ==========================================================================
def _emergency_flat(system: str, sess: int) -> None:
    """TP/SLキャンセル失敗時の緊急フラット"""
    res = zh_api.request_with_reauth("GET", f"/positions?product=3&symbol={zh_api.SYMBOL}&addinfo=false")
    if not res:
        send_discord(f"⚠️緊急 系統{system} /positions照会失敗 手動確認要")
        return
    data = safe_json(res)
    if not isinstance(data, list):
        return
    net = sum(
        (int(p.get("LeavesQty", 0)) if str(p.get("Side", "")) == "2"
         else -int(p.get("LeavesQty", 0)))
        for p in data
    )
    if net != 0:
        rec_side = "buy" if net < 0 else "sell"
        oid = zh_order.send_entry_order(rec_side, sess, trade_type=2)
        if oid:
            send_discord(f"⚠️緊急 系統{system} 逆ポジ{net:+d}枚→フラット OrderId:{oid}")
        else:
            send_discord(f"🚨最緊急 系統{system} フラット失敗 手動決済要")

# ==========================================================================
# SL/TP再発注（セッション切替）/ ブローカー整合
# ==========================================================================
def replace_close_orders(session_exchange: int) -> None:
    """セッション切替時にTP注文を新セッションで再発注（SLはソフトウェア監視のため不要）"""
    if DRY_RUN:
        return
    with _positions_lock:
        log(f"[REPLACE] 対象: {len(positions)}件  Exchange={session_exchange}")
        for pos in positions:
            order_side = "sell" if pos["side"] == "short" else "buy"
            # TP 再発注（SLはソフトウェア監視のため再発注不要）
            tp_oid = pos.get("tp_order_id")
            if tp_oid:
                zh_order.cancel_order(tp_oid)
                time.sleep(0.2)
            new_tp = zh_order.send_tp_order(order_side, pos["tp_price"], session_exchange, pos.get("hold_id"))
            pos["tp_order_id"] = new_tp
            if new_tp:
                tp_result = zh_order.check_order_active(new_tp)
                if tp_result is not True:
                    log(f"[WARN] 系統{pos['system']} REPLACE TP未確認"
                        f"(result={tp_result}) OrderId:{new_tp}")
                    send_discord(f"⚠️ 系統{pos['system']} REPLACE TP未確認"
                                 f"(result={tp_result}) OrderId:{new_tp}")
            log(f"  系統{pos['system']} {pos['side']}"
                f"  SL:{pos['sl_price']:.0f}(ソフトウェア監視)"
                f"  TP:{pos['tp_price']:.0f}(OrderId:{new_tp})")
        save_positions()


# ==========================================================================
# ポジション報告
# ==========================================================================
def report_positions(cp_now: float) -> list:
    lines = []
    for p in positions:
        side  = p["side"]
        pnl   = (cp_now - p["entry_price"]) if side == "long" else (p["entry_price"] - cp_now)
        arrow = "📈" if side == "long" else "📉"
        lines.append(
            f"{arrow} 系統{p['system']} {side.upper()} @ {p['entry_price']:.0f}"
            f"  現在:{cp_now:.0f}  含み:{pnl:+.0f}pt"
            f"  SL:{p['sl_price']:.0f}  TP:{p['tp_price']:.0f}"
        )
    return lines
