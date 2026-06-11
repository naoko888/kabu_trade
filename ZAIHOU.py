"""
ZAIHOU.py
N225マイクロ先物 自動売買 (系統①③④⑤)
ZAIHOU_signals.py のシグナル関数を使用。
auto_trade.py のマイクロ先物部分を完全置き換え。
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")

import time
import traceback
from datetime import datetime

import ZAIHOU_signals as sig
import zh_api
import zh_bar
import zh_monitor
import zh_entry
from zh_config import (
    DERIV_MONTH, NEXT_DERIV_MONTH,
    DRY_RUN, DD_LIMIT_YEN,
    POLL_SEC,
    MAX_AUTH_ERRORS,
    JST,
)
from zh_utils import (
    send_discord, log,
    is_holiday, _sess_exchange, _board_price,
)

# ==========================================================================
# グローバル状態
# ==========================================================================
# token / SYMBOL / EXCHANGE / _collect_symbols / _bar_state → zh_api.py が所有

# completed_bars / current_bar / last_cum_vol / warmup_remaining / can_trade → zh_bar.py が所有

# last_signal_bar_time / s4_last_bar / s5_last_bar / cpi_df → zh_entry.py が所有

# zh_monitor._positions_lock / zh_monitor.positions / zh_monitor.day_pnl / zh_monitor.trade_log → zh_monitor.py が所有
# zh_monitor.monthly_pnl_yen / zh_monitor.monthly_stopped / zh_monitor.monthly_ym → zh_monitor.py が所有

# zh_bar.warmup_remaining / zh_bar.can_trade → zh_bar.py が所有

# consecutive_auth_errors / last_reauth_time → zh_api.py が所有

# headers / get_token / get_symbol / _init_collect_symbols /
# register_symbol / request_with_reauth / get_board → zh_api.py

# バー管理 / WebSocket(_ws_on_message, start_ws) / ウォームアップ → zh_bar.py

# _ws_check_sl_tp → zh_monitor.py
# zh_monitor.save_positions / zh_monitor.load_positions / restore_monthly_pnl → zh_monitor.py

# send_entry_order / wait_for_fill / get_existing_execution_ids / get_hold_id /
# send_sl_order / send_tp_order / cancel_order / check_order_active → zh_order.py

# zh_monitor._SL_MAP / zh_monitor._TP_MAP / _MH_MAP / zh_monitor.monitor_positions / _monitor_inner / _emergency_flat /
# replace_close_orders / zh_monitor.reconcile_positions → zh_monitor.py
# _enter_position / _close_opposite / check_entry → zh_entry.py

# ==========================================================================
# メインループ
# ==========================================================================
def main():
    send_discord("🟢 ZAIHOU起動")
    print("=" * 60)
    print(f"ZAIHOU N225マイクロ先物 (系統①③④⑤)  DRY_RUN={DRY_RUN}")
    print(f"DD_LIMIT={DD_LIMIT_YEN:,}円  DERIV={DERIV_MONTH}/{NEXT_DERIV_MONTH}")
    print(f"SL: ①{sig.S1_SL} ③{sig.S3_SL} ④{sig.S4_SL} ⑤{sig.S5_SL}")
    print(f"TP: ①{sig.S1_TP} ③{sig.S3_TP} ④{sig.S4_TP} ⑤{sig.S5_TP}")
    print("=" * 60)

    # ── 初期化（APIなしでできるもの）──
    zh_entry.cpi_df = sig.load_cpi_events()
    if DRY_RUN:
        zh_monitor.load_positions()  # DRY_RUN: JSONからそのまま読み込む
        if zh_monitor.positions:
            log(f"[RESUME] 保有ポジション復元(DRY): {len(zh_monitor.positions)}件")
            for p in zh_monitor.positions:
                log(f"  系統{p['system']} {p['side']} @ {p['entry_price']}")
    zh_monitor.restore_monthly_pnl()
    zh_bar.can_trade = zh_bar.load_warmup()
    if not zh_bar.can_trade:
        log("[STOP] データ不足 → エントリー無効化")

    # ── API接続 ──
    if not zh_api.get_token():
        log("[ERR] トークン取得失敗 → 起動中止")
        return
    time.sleep(1)
    zh_api.get_symbol()
    zh_api._init_collect_symbols()
    if not zh_api.register_symbol():
        log("[ERR] 銘柄登録失敗 → 起動中止")
        return
    zh_bar._price_tick_callback = zh_monitor._ws_check_sl_tp
    zh_bar.start_ws()

    # ── 起動時ポジション復元（本番モードのみ、ブローカーAPIを正とする）──
    if not DRY_RUN:
        zh_monitor.restore_from_broker()
        if zh_monitor.positions:
            log(f"[RESUME] 保有ポジション復元: {len(zh_monitor.positions)}件")

    # ── 起動時 SL/TP 再発注（保有ポジションがある場合）──
    if zh_monitor.positions and not DRY_RUN:
        _h    = datetime.now(JST)
        _hhmm = _h.hour * 100 + _h.minute
        _se   = _sess_exchange(_hhmm)
        log(f"[RESTORE_SL] 起動時 SL復元 Exchange={_se}")
        zh_monitor.restore_sl_orders(_se)

    last_micro_csv_min   = -1
    last_sl_replace_hhmm = -1   # 16:45 / 800 の重複防止
    last_pos_report_hhmm = -1
    last_hourly_h        = -1
    last_verbose_min     = -1
    last_heartbeat_h     = -1

    while True:
        now     = datetime.now(JST)
        hhmm    = now.hour * 100 + now.minute
        weekday = now.weekday()
        verbose = (now.minute % 5 == 0 and now.minute != last_verbose_min)
        if verbose:
            last_verbose_min = now.minute

        # ── 週末終了 (土曜06:00〜) ──
        if weekday == 5 and hhmm >= 600:
            if zh_monitor.positions:
                log("[警告] 金曜夜間終了時にポジション残存 → 強制クローズ")
                zh_monitor.monitor_positions(now, 2350)
            mdf = zh_bar.bars_to_df()
            if mdf is not None:
                zh_bar.save_micro_csv(mdf)
            log(f"[OK] 週末終了  本日:{zh_monitor.day_pnl:+.0f}pt  ({len(zh_monitor.trade_log)}trades)")
            send_discord(f"🔴 ZAIHOU 週末終了  本日:{zh_monitor.day_pnl:+.0f}pt")
            break

        # ── 土日・休場 (土曜夜間セッションは除外) ──
        is_sat_night = (weekday == 5 and hhmm < 600)
        if (weekday >= 5 or is_holiday(now.date())) and not is_sat_night:
            time.sleep(60)
            continue

        # ── 認証エラー上限 ──
        if zh_api.consecutive_auth_errors >= MAX_AUTH_ERRORS:
            log(f"[ERR] 認証エラー{MAX_AUTH_ERRORS}回連続 → 安全終了")
            send_discord("🚨 ZAIHOU 認証エラー上限 → 終了")
            break

        # ── 板取得 ──
        board = zh_api.get_board()

        # ── ボード参照バー補完（volume=0 時間帯も 5 分足を補完）──
        zh_bar.update_from_board(hhmm, board, now.replace(tzinfo=None))

        # ── CSV保存 (5分ごと) ──
        _save_ok = ((845 <= hhmm < 1540) or (hhmm >= 1700) or (hhmm <= 605)
                    or (1540 <= hhmm <= 1550))
        if _save_ok and now.minute % 5 == 0 and now.minute != last_micro_csv_min and zh_api.SYMBOL:
            mdf = zh_bar.bars_to_df()
            if mdf is not None:
                zh_bar.save_micro_csv(mdf)
            zh_bar.save_collect_csv()
            last_micro_csv_min = now.minute

        # ── ポジション監視 / エントリー判定 ──
        if zh_api.SYMBOL and board and board.get("CurrentPrice"):
            zh_monitor.monitor_positions(now, hhmm, board)
            zh_entry.check_entry(now, board)

        # ── SL/TP 再発注 (セッション前気配・1回のみ) ──
        # 16:45 = 夜間前気配  /  8:00 = 日中前気配（実際のセッション開始は 8:45）
        if not DRY_RUN and zh_monitor.positions:
            if hhmm == 1645 and last_sl_replace_hhmm != 1645:
                log("[REPLACE] 夜間前気配(16:45) → SL/TP 再発注 Exchange=24")
                zh_monitor.replace_close_orders(24)
                last_sl_replace_hhmm = 1645
            elif hhmm == 800 and last_sl_replace_hhmm != 800:
                log("[REPLACE] 日中前気配(8:00) → SL/TP 再発注 Exchange=23")
                zh_monitor.replace_close_orders(23)
                last_sl_replace_hhmm = 800

        # ── 定時ポジション報告 (8:30 / 16:45) ──
        if zh_monitor.positions:
            cp_now = _board_price(board)
            if cp_now is not None:
                if hhmm in (830, 1645) and last_pos_report_hhmm != hhmm:
                    lines = zh_monitor.report_positions(cp_now)
                    send_discord("🕐 定時ポジション報告\n" + "\n".join(lines))
                    last_pos_report_hhmm = hhmm
                if now.minute == 0 and last_hourly_h != now.hour:
                    lines = zh_monitor.report_positions(cp_now)
                    if lines:
                        send_discord("⏱ 保有中\n" + "\n".join(lines))
                    last_hourly_h = now.hour

        if verbose:
            log(f"[TICK] hhmm={hhmm} ポジション:{len(zh_monitor.positions)}件"
                f" 月次:{zh_monitor.monthly_pnl_yen:,.0f}円 DD停止={zh_monitor.monthly_stopped}")

        if now.hour != last_heartbeat_h and now.minute == 0:
            send_discord(f"💚 ZAIHOU稼働中  ポジ:{len(zh_monitor.positions)}件  月次:{zh_monitor.monthly_pnl_yen:,.0f}円")
            last_heartbeat_h = now.hour

        time.sleep(POLL_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("[STOP] 手動停止")
        send_discord("🔴 ZAIHOU 手動停止")
        mdf = zh_bar.bars_to_df()
        if mdf is not None:
            zh_bar.save_micro_csv(mdf)
    except Exception:
        err = traceback.format_exc()
        log(f"[ERR] 例外:\n{err}")
        send_discord(f"🚨 ZAIHOU エラー\n```\n{err[:1500]}\n```")
        raise
