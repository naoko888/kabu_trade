"""
zh_entry.py
シグナル判定・エントリー実行・逆ポジ決済。
依存: zh_config, zh_utils, zh_api, zh_bar, zh_order, zh_monitor, ZAIHOU_signals
"""
import time
from datetime import datetime, timedelta

import pandas as pd
import ZAIHOU_signals as sig
import zh_api
import zh_bar
import zh_order
import zh_monitor
from zh_config import (
    API_PASSWORD, DRY_RUN, LOT,
    PT_TO_YEN, COMMISSION_YEN,
    DD_LIMIT_YEN, LOG_DIR,
)
from zh_utils import log, send_discord, safe_json, _sess_exchange, _board_price

# ==========================================================================
# 状態（このモジュールが所有）
# ==========================================================================
last_signal_bar_time = None

s4_last_bar: object = None
s5_last_bar: object = None

cpi_df: pd.DataFrame | None = None

# ==========================================================================
# エントリー実行
# ==========================================================================
def _enter_position(system: str, side: str, cp: float, board: dict,
                    now: datetime, session_exchange: int) -> None:
    sl_pt = zh_monitor._SL_MAP[system]
    tp_pt = zh_monitor._TP_MAP[system]
    mh    = zh_monitor._MH_MAP[system]

    if side == "long":
        sl_price = cp - sl_pt
        tp_price = cp + tp_pt
    else:
        sl_price = cp + sl_pt
        tp_price = cp - tp_pt

    best_bid = board.get("BidPrice")
    best_ask = board.get("AskPrice")
    best_bid = float(best_bid) if best_bid else None
    best_ask = float(best_ask) if best_ask else None
    spread   = round(best_ask - best_bid, 1) if (best_ask and best_bid) else None
    slip_est = (round(best_ask - cp, 1) if side == "long" and best_ask else
                round(cp - best_bid, 1) if side == "short" and best_bid else None)

    arrow  = "📈" if side == "long" else "📉"
    bid_s  = f"{best_bid:.0f}" if best_bid else "---"
    ask_s  = f"{best_ask:.0f}" if best_ask else "---"
    slip_s = f"{slip_est:+.1f}pt" if slip_est is not None else "---"
    log(f"[{side.upper()}] 系統{system} @ {cp:.0f}"
        f"  SL:{sl_price:.0f}  TP:{tp_price:.0f}"
        f"  Bid:{bid_s} Ask:{ask_s} Spread:{spread} SlipEst:{slip_s}")
    send_discord(f"{arrow} 系統{system} {side.upper()} @ {cp:.0f}"
                 f"  SL:{sl_price:.0f}  TP:{tp_price:.0f}")

    pos = {
        "system":        system,
        "side":          side,
        "entry_time":    now,
        "entry_price":   cp,
        "sl_price":      sl_price,
        "tp_price":      tp_price,
        "max_hold":      mh,
        "signal_bid":    best_bid,
        "signal_ask":    best_ask,
        "spread":        spread,
        "slip_est":      slip_est,
        "entry_weekday": now.weekday(),
    }

    if DRY_RUN:
        with zh_monitor._positions_lock:
            zh_monitor.positions.append(pos)
            zh_monitor.save_positions()
        return

    # ── 本番モード ──
    order_side = "buy" if side == "long" else "sell"
    existing_ids = zh_order.get_existing_execution_ids(order_side)
    if existing_ids is None:
        msg = f"⚠️ 系統{system} 事前建玉ID取得失敗 → エントリースキップ(機会損失)"
        log(f"[WARN] {msg}"); send_discord(msg)
        return
    oid = zh_order.send_entry_order(order_side, session_exchange)
    if not oid:
        log(f"[WARN] 系統{system} エントリー発注失敗 → スキップ")
        return
    fill = zh_order.wait_for_fill(oid)
    if fill is None:
        log(f"[WARN] 系統{system} 約定未確認 → 注文キャンセル")
        zh_order.cancel_order(oid)
        return
    # 実約定価格でSL/TP再計算
    pos["entry_price"] = fill
    pos["sl_price"]    = fill - sl_pt if side == "long" else fill + sl_pt
    pos["tp_price"]    = fill + tp_pt if side == "long" else fill - tp_pt
    log(f"[FILL] 系統{system} 約定:{fill:.0f}"
        f"  SL:{pos['sl_price']:.0f}  TP:{pos['tp_price']:.0f}"
        f"  (シグナル価格との乖離:{fill - cp:+.0f}pt)")
    hold_id = zh_order.get_hold_id(order_side, existing_ids)
    if not hold_id:
        log(f"[WARN] 系統{system} HoldID取得失敗 → ClosePositionOrder=0で代替")
    sl_oid = zh_order.send_sl_order(order_side, pos["sl_price"], session_exchange, hold_id)
    tp_oid = zh_order.send_tp_order(order_side, pos["tp_price"], session_exchange, hold_id)
    pos["sl_order_id"] = sl_oid
    pos["tp_order_id"] = tp_oid
    pos["order_id"]    = oid
    pos["hold_id"]     = hold_id
    log(f"[ORDER] SL_OrderId:{sl_oid}  TP_OrderId:{tp_oid}  HoldID:{hold_id}")

    # SL受付確認
    if sl_oid:
        sl_result = zh_order.check_order_active(sl_oid)
        if sl_result is False:
            # 非アクティブ確定 → 裸ポジ → TP取消・緊急返済
            msg = f"⚠️ 系統{system} SL非アクティブ確定(裸ポジ) → TP取消・緊急返済"
            log(f"[WARN] {msg}"); send_discord(msg)
            if tp_oid:
                zh_order.cancel_order(tp_oid)
            close_side = "sell" if side == "long" else "buy"
            if hold_id:
                _body = {
                    "Password": API_PASSWORD, "Symbol": zh_api.SYMBOL,
                    "Exchange": session_exchange,
                    "TradeType": 2, "TimeInForce": 2,
                    "Side": "1" if close_side == "sell" else "2",
                    "Qty": LOT, "Price": 0, "ExpireDay": 0,
                    "FrontOrderType": 120,
                    "ClosePositions": [{"HoldID": hold_id, "Qty": LOT}],
                }
                res_cl = zh_api.request_with_reauth("POST", "/sendorder/future", json_body=_body)
                if res_cl:
                    log(f"[OK] 緊急返済発注(HoldID指定) OrderId:{safe_json(res_cl).get('OrderId','')}")
                else:
                    send_discord(f"🚨緊急 系統{system} 緊急返済失敗 手動決済要 HoldID:{hold_id}")
            else:
                oid_em = zh_order.send_entry_order(close_side, session_exchange, trade_type=2)
                if not oid_em:
                    send_discord(f"🚨緊急 系統{system} 緊急返済失敗 手動決済要")
            return  # zh_monitor.positions に追加しない
        elif sl_result is None:
            # 通信不明 → 警告のみ（reconcileに委ねる）
            log(f"[WARN] 系統{system} SL注文確認不能(通信不明) → 継続監視")
            send_discord(f"⚠️ 系統{system} SL注文確認不能 OrderId:{sl_oid}")

    # TP受付確認（SL生存中のため通知のみ）
    if tp_oid and zh_order.check_order_active(tp_oid) is False:
        log(f"[WARN] 系統{system} TP注文非アクティブ確定 OrderId:{tp_oid}")
        send_discord(f"⚠️ 系統{system} TP注文非アクティブ確定 OrderId:{tp_oid}")

    with zh_monitor._positions_lock:
        zh_monitor.positions.append(pos)
        zh_monitor.save_positions()

# ==========================================================================
# 逆ポジ決済
# ==========================================================================
def _close_opposite(system: str, new_side: str, now: datetime, board: dict) -> None:
    """同一系統で逆方向ポジションがあれば成行決済（逆ポジ対応）"""
    opposite = "short" if new_side == "long" else "long"
    to_close = [p for p in zh_monitor.positions if p["system"] == system and p["side"] == opposite]
    if not to_close:
        return
    cp    = _board_price(board) or 0.0
    hhmm  = now.hour * 100 + now.minute
    _sess = _sess_exchange(hhmm)

    for pos in to_close:
        log(f"[逆ポジ] 系統{system} 既存{opposite}決済 → 新規{new_side}へ")
        if not DRY_RUN:
            for k in ("sl_order_id", "tp_order_id"):
                if pos.get(k):
                    zh_order.cancel_order(pos[k])
            time.sleep(0.3)
            close_side = "buy" if opposite == "short" else "sell"
            oid = zh_order.send_entry_order(close_side, _sess, trade_type=2)
            if oid and oid != "DRY":
                fill = zh_order.wait_for_fill(oid)
                if fill is None:
                    # 約定未確認 → 新規エントリーもスキップして手動確認
                    msg = f"⚠️緊急 系統{system} 逆ポジ決済 約定未確認 OrderId:{oid} → 新規エントリースキップ"
                    log(f"[ALERT] {msg}"); send_discord(msg)
                    return   # _enter_position を呼ばない
                cp = float(fill)

        pnl = (cp - pos["entry_price"]) if opposite == "long" else (pos["entry_price"] - cp)
        zh_monitor.day_pnl += pnl
        trade_yen = round(pnl * PT_TO_YEN - COMMISSION_YEN, 0)
        if (pos["entry_time"].year, pos["entry_time"].month) == (now.year, now.month):
            zh_monitor.monthly_pnl_yen += trade_yen
        if zh_monitor.monthly_pnl_yen <= DD_LIMIT_YEN and not zh_monitor.monthly_stopped:
            zh_monitor.monthly_stopped = True
            send_discord(f"⚠️ 月次DD上限到達({zh_monitor.monthly_pnl_yen:,.0f}円) → 今月全系統停止")

        zh_monitor.trade_log.append({
            "system": system, "side": opposite,
            "entry_time":  pos["entry_time"].strftime("%Y-%m-%d %H:%M:%S"),
            "exit_time":   now.strftime("%Y-%m-%d %H:%M:%S"),
            "entry_price": pos["entry_price"], "exit_price": cp,
            "pnl": round(pnl, 1), "reason": "逆ポジ決済",
            "signal_bid": pos.get("signal_bid"), "signal_ask": pos.get("signal_ask"),
            "spread": pos.get("spread"), "slip_est": pos.get("slip_est"),
        })
        LOG_DIR.mkdir(exist_ok=True)
        all_file = LOG_DIR / "micro_dry_log_all.csv"
        pd.DataFrame([zh_monitor.trade_log[-1]]).to_csv(
            all_file, mode="a", header=not all_file.exists(),
            index=False, encoding="utf-8-sig",
        )
        with zh_monitor._positions_lock:
            try:
                zh_monitor.positions.remove(pos)
            except ValueError:
                pass
        zh_monitor.save_positions()
        send_discord(f"🔄 系統{system} 逆ポジ決済 @ {cp:.0f}  損益:{pnl:+.0f}pt")

# ==========================================================================
# エントリー判定
# ==========================================================================
def check_entry(now: datetime, board: dict) -> None:
    global last_signal_bar_time
    global s4_last_bar, s5_last_bar

    if not zh_bar.can_trade:
        return

    # ── 月次リセット ──
    now_ym = (now.year, now.month)
    if zh_monitor.monthly_ym != now_ym:
        zh_monitor.monthly_ym      = now_ym
        zh_monitor.monthly_pnl_yen = 0.0
        zh_monitor.monthly_stopped = False
        log(f"[全系統] 月次リセット: {now_ym}")

    df = zh_bar.bars_to_df()
    if df is None or len(df) < 31:
        return

    df              = sig.add_micro_indicators(df)
    df_confirmed    = df.iloc[:-1]
    latest_bar_time = df_confirmed.iloc[-1]["datetime"]
    if last_signal_bar_time == latest_bar_time:
        return

    # ── セッション跨ぎ / 陳腐化バースキップ ──
    now_naive = now.replace(tzinfo=None)
    bar_dt    = pd.Timestamp(latest_bar_time).to_pydatetime()
    hhmm_now  = now_naive.hour * 100 + now_naive.minute
    sess_ex   = _sess_exchange(hhmm_now)

    if hhmm_now >= 1700:
        session_start = now_naive.replace(hour=17, minute=0, second=0, microsecond=0)
    elif hhmm_now >= 845:
        session_start = now_naive.replace(hour=8, minute=45, second=0, microsecond=0)
    else:
        session_start = (now_naive - timedelta(days=1)).replace(
            hour=17, minute=0, second=0, microsecond=0
        )

    if bar_dt < session_start:
        last_signal_bar_time = latest_bar_time
        log(f"[SKIP] previous_session_bar bar_dt={bar_dt.strftime('%H:%M')}")
        return
    if hhmm_now >= 1700 and bar_dt.hour < 17:
        last_signal_bar_time = latest_bar_time
        log("[SKIP] night_open_daybarsignal")
        return
    if now.weekday() == 5 and 500 <= hhmm_now < 600:
        last_signal_bar_time = latest_bar_time
        log("[SKIP] friday_night_entry_block")
        return

    bar_age_min = (now_naive - bar_dt).total_seconds() / 60
    if bar_age_min > 10:
        last_signal_bar_time = latest_bar_time
        log(f"[SKIP] stale_bar bar_age={bar_age_min:.0f}min")
        return

    last_signal_bar_time = latest_bar_time

    # ── ウォームアップ中 ──
    if zh_bar.warmup_remaining > 0:
        zh_bar.warmup_remaining -= 1
        if zh_bar.warmup_remaining == 0:
            log("[WARMUP COMPLETE] 通常稼働開始")
        else:
            log(f"[WARMUP] 残り{zh_bar.warmup_remaining}本")
        return

    cp = _board_price(board)
    if cp is None:
        log("[WARN] 現在値取得失敗")
        return
    cp = float(cp)

    log(f"[SIGNAL CHECK] bar_dt={bar_dt.strftime('%H:%M')} DD停止={zh_monitor.monthly_stopped}"
        f" bars={len(df_confirmed)}")

    # ── 系統①③ ──
    fired_13 = sig.check_s1_s3(df_confirmed, cpi_df)
    if not fired_13:
        log(f"[MICRO] → ①③シグナルなし (現在値:{cp:.0f})")
    for system in fired_13:
        if zh_monitor.monthly_stopped:
            log(f"[系統{system}] 月次DD停止中 ({zh_monitor.monthly_pnl_yen:,.0f}円)")
            continue
        side = "long" if system == "①" else "short"
        _close_opposite(system, side, now, board)
        _enter_position(system, side, cp, board, now, sess_ex)

    # ── 系統④ ──
    if not zh_monitor.monthly_stopped and sig.check_s4(df_confirmed):
        if s4_last_bar != latest_bar_time:
            s4_last_bar = latest_bar_time
            _close_opposite("④", "long", now, board)
            _enter_position("④", "long", cp, board, now, sess_ex)
    elif zh_monitor.monthly_stopped:
        log(f"[系統④] 月次DD停止中 ({zh_monitor.monthly_pnl_yen:,.0f}円)")

    # ── 系統⑤ ──
    if not zh_monitor.monthly_stopped and sig.check_s5(df_confirmed, cpi_df):
        if s5_last_bar != latest_bar_time:
            s5_last_bar = latest_bar_time
            _close_opposite("⑤", "short", now, board)
            _enter_position("⑤", "short", cp, board, now, sess_ex)
    elif zh_monitor.monthly_stopped:
        log(f"[系統⑤] 月次DD停止中 ({zh_monitor.monthly_pnl_yen:,.0f}円)")
