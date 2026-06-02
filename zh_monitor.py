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
    global positions
    if not OPEN_POS_FILE.exists():
        return
    with open(OPEN_POS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    for p in data:
        if isinstance(p.get("entry_time"), str):
            p["entry_time"] = datetime.fromisoformat(p["entry_time"])
    positions = data

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
            _gap = (1540 <= hhmm < 1700) or (555 <= hhmm < 845)
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
                    _sess = 23 if (1540 <= hhmm < 1700) else 24

                if reason == "TP到達":
                    # GET /positions でHoldIDのポジションが消えているか確認
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
                    sl_oid = pos.get("sl_order_id")
                    if sl_oid:
                        zh_order.cancel_order(sl_oid)
                elif reason == "SL到達":
                    sl_oid = pos.get("sl_order_id")
                    hid    = pos.get("hold_id")
                    _tp_already_cancelled = False
                    if sl_oid:
                        fill = zh_order.wait_for_fill(sl_oid, max_retries=5, interval=1.0)
                        if fill is not None:
                            cp  = fill
                            pnl = (cp - pos["entry_price"]) if side == "long" else (pos["entry_price"] - cp)
                            log(f"[LIVE] 系統{system} SL約定確認:{cp:.0f}")
                        else:
                            # タイムアウト → GET /positions でポジション生死確認
                            res_pos = zh_api.request_with_reauth(
                                "GET", f"/positions?product=3&symbol={zh_api.SYMBOL}&addinfo=false")
                            if res_pos is None:
                                log(f"[WARN] 系統{system} SL未確認+/positions失敗 → 次ティック再確認")
                                still_open.append(pos)
                                continue
                            pos_data = safe_json(res_pos)
                            if not isinstance(pos_data, list):
                                log(f"[WARN] 系統{system} SL確認: /positions異常レスポンス")
                                still_open.append(pos)
                                continue
                            if hid:
                                pos_exists = any(
                                    str(p.get("ExecutionID")) == hid and float(p.get("LeavesQty", 0)) > 0
                                    for p in pos_data)
                            else:
                                bside = "1" if side == "short" else "2"
                                pos_exists = any(
                                    str(p.get("Side", "")) == bside and float(p.get("LeavesQty", 0)) > 0
                                    for p in pos_data)
                            if not pos_exists:
                                # ポジション消滅 → SL約定済み(価格不明) → ボード価格で代替記録
                                log(f"[LIVE] 系統{system} SL約定確認(ポジション消滅 価格不明):{cp:.0f}")
                                send_discord(f"⚠️ 系統{system} SL約定価格不明 → ボード価格{cp:.0f}で記録")
                            else:
                                # ポジション残存 → SL未約定 → TP取消・緊急成行返済
                                msg = f"⚠️緊急 系統{system} SL未約定(ポジション残存) → TP取消・緊急成行返済"
                                log(f"[WARN] {msg}"); send_discord(msg)
                                tp_oid_c = pos.get("tp_order_id")
                                if tp_oid_c:
                                    zh_order.cancel_order(tp_oid_c)
                                    _tp_already_cancelled = True
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
                                    if res_cl:
                                        rj = safe_json(res_cl)
                                        close_oid = rj.get("OrderId", "") if rj.get("Result") == 0 else None
                                    else:
                                        close_oid = None
                                else:
                                    close_oid = zh_order.send_entry_order(close_side, _sess, trade_type=2)
                                if close_oid and close_oid != "DRY":
                                    emg_fill = zh_order.wait_for_fill(close_oid, max_retries=5, interval=1.0)
                                    if emg_fill is not None:
                                        cp  = float(emg_fill)
                                        pnl = (cp - pos["entry_price"]) if side == "long" else (pos["entry_price"] - cp)
                                        log(f"[LIVE] 系統{system} 緊急返済約定:{cp:.0f}")
                                    else:
                                        send_discord(f"🚨最緊急 系統{system} SL未確認+緊急返済未確認 手動決済要")
                                        still_open.append(pos)
                                        continue
                                else:
                                    send_discord(f"🚨最緊急 系統{system} SL未確認+緊急返済失敗 手動決済要")
                                    still_open.append(pos)
                                    continue
                    if not _tp_already_cancelled:
                        tp_oid = pos.get("tp_order_id")
                        if tp_oid:
                            zh_order.cancel_order(tp_oid)
                else:
                    # 強制決済: SL・TP両方キャンセル確認 → 成行
                    sl_ok = zh_order.cancel_order(pos["sl_order_id"]) if pos.get("sl_order_id") else True
                    tp_ok = zh_order.cancel_order(pos["tp_order_id"]) if pos.get("tp_order_id") else True
                    time.sleep(0.5)
                    if not (sl_ok and tp_ok):
                        _emergency_flat(system, _sess)
                        still_open.append(pos)
                        continue
                    # ポジション存在確認（キャンセル直前にSL/TPが約定済みの可能性を排除）
                    res_pos = zh_api.request_with_reauth("GET", f"/positions?product=3&symbol={zh_api.SYMBOL}&addinfo=false")
                    if res_pos is not None:
                        pos_data = safe_json(res_pos)
                        if isinstance(pos_data, list):
                            hid = pos.get("hold_id")
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
    """セッション切替時にSL・TP注文を新セッションで再発注（全系統対象）"""
    if DRY_RUN:
        return
    with _positions_lock:
        log(f"[REPLACE] 対象: {len(positions)}件  Exchange={session_exchange}")
        for pos in positions:
            order_side = "sell" if pos["side"] == "short" else "buy"
            # SL 再発注
            sl_oid = pos.get("sl_order_id")
            if sl_oid:
                zh_order.cancel_order(sl_oid)
                time.sleep(0.2)
            new_sl = zh_order.send_sl_order(order_side, pos["sl_price"], session_exchange, pos.get("hold_id"))
            pos["sl_order_id"] = new_sl
            if new_sl:
                sl_result = zh_order.check_order_active(new_sl)
                if sl_result is not True:
                    log(f"[WARN] 系統{pos['system']} REPLACE SL未確認"
                        f"(result={sl_result}) OrderId:{new_sl}")
                    send_discord(f"⚠️ 系統{pos['system']} REPLACE SL未確認"
                                 f"(result={sl_result}) OrderId:{new_sl}")
            # TP 再発注
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
                f"  SL:{pos['sl_price']:.0f}(OrderId:{new_sl})"
                f"  TP:{pos['tp_price']:.0f}(OrderId:{new_tp})")
        save_positions()

def reconcile_positions(now: datetime) -> None:
    """ブローカーポジション整合チェック（本番モードのみ）"""
    if DRY_RUN or not zh_api.SYMBOL:
        return
    with _positions_lock:
        if not positions:
            return
        internal_net   = sum(1 if p["side"] == "long" else -1 for p in positions)
        internal_gross = len(positions)

    res = zh_api.request_with_reauth("GET", f"/positions?product=3&symbol={zh_api.SYMBOL}&addinfo=false")
    if not res:
        log("[RECONCILE] /positions照会失敗 → スキップ")
        return
    data = safe_json(res)
    if not isinstance(data, list):
        return
    actual_net   = 0
    actual_gross = 0
    for p in data:
        qty          = int(p.get("LeavesQty", 0))
        actual_gross += qty
        actual_net   += qty if str(p.get("Side", "")) == "2" else -qty

    if actual_net == internal_net and actual_gross == internal_gross:
        return

    msg = (f"⚠️ポジション不一致 "
           f"内部:net{internal_net:+d}/{internal_gross}件 "
           f"ブローカー:net{actual_net:+d}/gross{actual_gross}枚")
    log(f"[RECONCILE] {msg}"); send_discord(msg)

    hhmm  = now.hour * 100 + now.minute
    _sess = _sess_exchange(hhmm)

    if actual_gross == 0:
        with _positions_lock:
            for pos in list(positions):
                for k in ("sl_order_id", "tp_order_id"):
                    if pos.get(k):
                        zh_order.cancel_order(pos[k])
            positions.clear()
        save_positions()
        send_discord("⚠️TP/SL見逃し検知 → ブラケット注文キャンセル・内部クリア(ブローカー既約定)")
    elif actual_net * internal_net < 0:
        close_side = "buy" if actual_net < 0 else "sell"
        oid = zh_order.send_entry_order(close_side, _sess, trade_type=2)
        if oid:
            send_discord(f"🚨逆ポジ緊急フラット → 成行 OrderId:{oid}")
        else:
            send_discord("🚨最緊急 逆ポジフラット失敗 手動決済要")

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
