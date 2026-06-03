"""
ZAIHOU_bt.py
ZAIHOU_signals.py パラメータ検証 BT

既存 BT エンジン（bt13 / bt45 / bt6）を ZAIHOU_signals のパラメータで実行し、
bt_result_combined.txt Section 1（DD制限なし）の結果と照合する。

期待値:
  ①:  5,623件   +724,494円
  ③:  9,917件   +915,126円
  ④:  4,980件   +419,540円
  ⑤:  5,433件   +768,424円
  ⑥:  3,110件 +1,581,080円  # 除去中
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")

import ZAIHOU_signals as sig
import backtest_system123_combined as bt13
import backtest_system45_combined  as bt45
# import backtest_system6b           as bt6  # ⑥除去中

# ──────────────────────────────────────────────────────────────
# 期待値（bt_result_combined.txt Section 1 DD制限なし）
# ──────────────────────────────────────────────────────────────
EXPECTED = {
    "①": {"n":  5_623, "pnl_yen":    724_494},
    "③": {"n":  9_917, "pnl_yen":    915_126},
    "④": {"n":  4_223, "pnl_yen":     31_694},
    "⑤": {"n":  6_270, "pnl_yen":    646_710},
    # "⑥": {"n":  3_110, "pnl_yen":  1_581_080},  # ⑥除去中
}


def _t(fs):
    """frozenset → sorted tuple（BT kwargs 用）"""
    return tuple(sorted(fs))


def _bt13_kwargs():
    """ZAIHOU_signals の定数から BT13 用 kwargs を生成"""
    return dict(
        s1_excl_months  = _t(sig.S1_EXCL_MONTHS),
        s3_excl_months  = _t(sig.S3_EXCL_MONTHS),
        s1_weekdays     = _t(sig.S1_WEEKDAYS),
        s1_hours_dst    = _t(sig.S1_HOURS_DST),
        s1_hours_win    = _t(sig.S1_HOURS_WIN),
        s3_hours_dst    = _t(sig.S3_HOURS_DST),
        s3_hours_win    = _t(sig.S3_HOURS_WIN),
        s3_weekdays_dst = _t(sig.S3_WEEKDAYS),
        s3_weekdays_win = _t(sig.S3_WEEKDAYS),
    )


def _bt45_kwargs():
    """ZAIHOU_signals の定数から BT45 用 kwargs を生成"""
    return dict(
        use_recovery   = False,
        use_vol        = False,
        use_rsi        = True,
        use_move       = True,
        s4_hours_dst   = _t(sig.S4_HOURS_DST),
        s4_hours_win   = _t(sig.S4_HOURS_WIN),
        s5_hours_dst   = _t(sig.S5_HOURS_DST),
        s5_hours_win   = _t(sig.S5_HOURS_WIN),
        s4_excl_months = _t(sig.S4_EXCL_MONTHS),
        s5_excl_months = _t(sig.S5_EXCL_MONTHS),
    )


# def _bt6_kwargs():  # ⑥除去中
#     return dict(
#         thresh      = sig.S6_THRESH,
#         tp          = sig.S6_TP,
#         sl          = sig.S6_SL,
#         max_hold    = sig.S6_MAX_HOLD,
#         cd          = sig.S6_CD_MIN // 5,
#         hours       = sig.S6_HOURS,
#         excl_months = (),
#     )


def main():
    # ── データ・インジケーター ──────────────────────────────────
    print("データ読み込み中...")
    df13  = bt13.add_indicators(bt13.load_data())
    cpi13 = bt13.load_cpi()

    df45  = bt45.add_indicators(bt45.load_data())
    cpi45 = bt45.load_cpi()

    # df6   = bt6.add_indicators(bt6.load_data())  # ⑥除去中
    # cpi6  = bt6.load_cpi()

    # ── BT 実行 ────────────────────────────────────────────────
    print("\n①③ バックテスト実行中...")
    t13 = bt13.run_backtest(df13, cpi13, **_bt13_kwargs())

    print("④⑤ バックテスト実行中...")
    t45 = bt45.run_backtest(df45, cpi45, **_bt45_kwargs())

    # print("⑥  バックテスト実行中...")  # ⑥除去中
    # t6  = bt6.run_backtest(df6, cpi6, **_bt6_kwargs())

    # ── 結果比較 ───────────────────────────────────────────────
    results = {
        "①": t13[t13["system"] == "①"],
        "③": t13[t13["system"] == "③"],
        "④": t45[t45["system"] == "④"],
        "⑤": t45[t45["system"] == "⑤"],
        # "⑥": t6,  # ⑥除去中
    }

    SEP = "=" * 74
    print(f"\n{SEP}")
    print("  ZAIHOU_signals パラメータ検証結果")
    print(f"  比較対象: bt_result_combined.txt Section 1（DD制限なし）")
    print(SEP)
    print(f"\n  {'系統':>4}  {'期待件数':>8}  {'実件数':>8}  {'件数':>5}  "
          f"{'期待損益(円)':>14}  {'実損益(円)':>14}  {'損益':>5}")
    print("  " + "-" * 70)

    all_ok = True
    for s, df in results.items():
        n       = len(df)
        pnl_yen = int(df["pnl_yen"].sum())
        exp_n   = EXPECTED[s]["n"]
        exp_pnl = EXPECTED[s]["pnl_yen"]
        n_ok    = "✓" if n == exp_n else "✗"
        p_ok    = "✓" if pnl_yen == exp_pnl else "✗"
        if n_ok != "✓" or p_ok != "✓":
            all_ok = False
        print(f"  {s:>4}  {exp_n:>8,}  {n:>8,}  {n_ok:>5}  "
              f"{exp_pnl:>+14,}  {pnl_yen:>+14,}  {p_ok:>5}")

    print()
    if all_ok:
        print("  ✓ 全系統一致 — ZAIHOU_signals パラメータ検証 PASSED")
    else:
        print("  ✗ 不一致あり — ZAIHOU_signals パラメータを確認してください")
    print(SEP)


if __name__ == "__main__":
    main()
