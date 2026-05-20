"""
backtest_combined_all.py
系統①③（順張り）＋ 系統④⑤（逆張り）合算 成績レポート
出力: bt_result_combined.txt
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')
import pandas as pd
import numpy as np

import backtest_system123_combined as bt13
import backtest_system45_combined  as bt45

# ====================================================
# ①③ の確定設定（backtest_system123_combined.py の _bt_kwargs と完全一致）
# ====================================================
BT13_KWARGS = dict(
    s1_excl_months  = bt13.S1_EXCL_BASE,        # (3, 5, 11)
    s3_excl_months  = bt13.S3_EXCL_MONTHS,       # (5, 7, 11)
    s1_weekdays     = (0, 1, 2),
    s1_hours_dst    = (2, 8, 15, 18, 19, 21),
    s1_hours_win    = (2, 8, 12, 13, 15, 18, 21, 23),
    s3_hours_dst    = (0, 5, 8, 12, 13, 14, 15, 19, 20, 22, 23),
    s3_hours_win    = (4, 5, 15, 17, 18, 19, 20, 21, 22),
    s3_weekdays_dst = (0, 2, 3, 4),
    s3_weekdays_win = (0, 2, 3, 4),
)

# ====================================================
# ④⑤ の確定設定
# ====================================================
BT45_KWARGS = dict(
    use_recovery = bt45.USE_RECOVERY,
    use_vol      = bt45.USE_VOL,
    use_rsi      = bt45.USE_RSI,
    use_move     = bt45.USE_MOVE,
)

PT_TO_YEN = 10
SEP80 = "=" * 80
SEP72 = "-" * 72

# ====================================================
# DD基準スイッチ
#   True  = 決済月基準（auto_trade.py と同じ）
#   False = エントリー月基準（従来BT）
# ====================================================
USE_SETTLEMENT_MONTH = False


def pf_s(v):
    return "  inf" if v == float("inf") else f"{v:.3f}"


def calc_summary(df):
    if len(df) == 0:
        return {"n": 0, "win_rate": 0.0, "pnl_pt": 0.0, "pnl_yen": 0, "ev": 0.0, "pf": 0.0}
    pnl  = df["pnl_pt"].values.astype(float)
    wins = pnl[pnl > 0].sum()
    loss = abs(pnl[pnl < 0].sum())
    return {
        "n":        len(pnl),
        "win_rate": float((pnl > 0).mean() * 100),
        "pnl_pt":   float(pnl.sum()),
        "pnl_yen":  int(df["pnl_yen"].sum()),
        "ev":       float(pnl.mean()),
        "pf":       float(wins / loss) if loss > 0 else float("inf"),
    }


def _add_exit_dt(trades: pd.DataFrame, df_price: pd.DataFrame) -> pd.DataFrame:
    """trades に exit_dt 列を付与する。価格データから決済バーを再シミュレート。"""
    if len(trades) == 0:
        out = trades.copy(); out["exit_dt"] = pd.NaT; return out

    dts_list = pd.to_datetime(df_price["datetime"]).tolist()
    dt_to_idx = {t: i for i, t in enumerate(dts_list)}
    arr_open = df_price["open"].values
    arr_high = df_price["high"].values
    arr_low  = df_price["low"].values
    arr_hm   = (pd.to_datetime(df_price["datetime"]).dt.hour * 100
                + pd.to_datetime(df_price["datetime"]).dt.minute).values
    arr_wd   = pd.to_datetime(df_price["datetime"]).dt.weekday.values
    n = len(df_price)

    # 系統別パラメータ（bt13/bt45 定数を参照）
    PARAMS = {
        "①": dict(tp=bt13.TP, sl=bt13.SL, max_hold=bt13.MAX_HOLD,
                   side="long",  session_close=True,  weekend_close=False),
        "③": dict(tp=bt13.TP, sl=bt13.SL, max_hold=50,
                   side="short", session_close=False, weekend_close=False),
        "④": dict(tp=bt45.LONG_PARAM["tp"],  sl=bt45.LONG_PARAM["sl"],
                   max_hold=bt45.LONG_PARAM["max_hold"],
                   side="long",  session_close=False, weekend_close=True),
        "⑤": dict(tp=bt45.SHORT_PARAM["tp"], sl=bt45.SHORT_PARAM["sl"],
                   max_hold=bt45.SHORT_PARAM["max_hold"],
                   side="short", session_close=False, weekend_close=True),
    }

    exit_dts = []
    for _, row in trades.iterrows():
        sys_key = row["system"]
        sig_dt  = row["signal_dt"]
        p = PARAMS[sys_key]

        sig_i = dt_to_idx.get(sig_dt)
        if sig_i is None or sig_i + 1 >= n:
            exit_dts.append(sig_dt); continue

        ent_i    = sig_i + 1
        ep       = float(arr_open[ent_i])
        TP       = p["tp"]; SL = p["sl"]; max_hold = p["max_hold"]
        side     = p["side"]
        exit_bar = min(ent_i + max_hold - 1, n - 1)  # TIME デフォルト

        for j in range(ent_i, min(ent_i + max_hold, n)):
            # TP/SL: BT と同じ順序（TP 優先）
            if side == "long":
                if arr_high[j] >= ep + TP:
                    exit_bar = j; break
                if arr_low[j]  <= ep - SL:
                    exit_bar = j; break
            else:
                if arr_low[j]  <= ep - TP:
                    exit_bar = j; break
                if arr_high[j] >= ep + SL:
                    exit_bar = j; break
            if p["session_close"] and arr_hm[j] in bt13.SESSION_BOUNDARIES:
                exit_bar = j; break
            if arr_wd[j] == 0 and arr_hm[j] == 555:            # MON_CLOSE (bt13)
                exit_bar = j; break
            if p["weekend_close"] and arr_wd[j] == 0 and arr_hm[j] == 600:  # WEEKEND (bt45)
                exit_bar = j; break

        exit_dts.append(dts_list[exit_bar])

    out = trades.copy()
    out["exit_dt"] = exit_dts
    return out


def sim_monthly_dd(trades, dd_limit, use_exit_month=False):
    if len(trades) == 0:
        return {"active": pd.DataFrame(), "skipped": 0, "months_triggered": 0}
    # use_exit_month=True 時は決済順に処理（実運用に近い）
    sort_col = "exit_dt" if (use_exit_month and "exit_dt" in trades.columns) else "signal_dt"
    df = trades.sort_values(sort_col).copy()
    if use_exit_month and "exit_dt" in df.columns:
        df["ym"] = df["exit_dt"].apply(lambda x: (x.year, x.month))
    else:
        df["ym"] = list(zip(df["signal_year"], df["signal_month"]))
    keep = []; mo_pnl = {}; triggered = set()
    for _, row in df.iterrows():
        ym = row["ym"]
        mo_pnl.setdefault(ym, 0.0)
        if ym in triggered:
            keep.append(False); continue
        keep.append(True)
        mo_pnl[ym] += row["pnl_yen"]
        if mo_pnl[ym] <= dd_limit:
            triggered.add(ym)
    df["keep"] = keep
    return {
        "active":           df[df["keep"]].drop(columns=["ym","keep"]),
        "skipped":          int((~df["keep"]).sum()),
        "months_triggered": len(triggered),
    }


def print_summary_row(label, s):
    print(f"  {label:10}  {s['n']:>6}  {s['win_rate']:>5.1f}%  "
          f"{s['pnl_pt']:>+12.1f}  {s['pnl_yen']:>+13,}  {s['ev']:>+8.2f}  {pf_s(s['pf'])}")


def main():
    # ── データ読み込み ──
    print("【①③】データ読み込み中...")
    df13  = bt13.add_indicators(bt13.load_data())
    cpi13 = bt13.load_cpi()

    print("\n【④⑤】データ読み込み中...")
    df45  = bt45.add_indicators(bt45.load_data())
    cpi45 = bt45.load_cpi()

    # ── バックテスト実行 ──
    print("\n①③ バックテスト実行中...")
    trades13 = bt13.run_backtest(df13, cpi13, **BT13_KWARGS)

    print("④⑤ バックテスト実行中...")
    trades45 = bt45.run_backtest(df45, cpi45, **BT45_KWARGS)

    # ── 決済月算出（USE_SETTLEMENT_MONTH=True 時のみ）──
    if USE_SETTLEMENT_MONTH:
        print("決済月算出中（①③）...")
        trades13 = _add_exit_dt(trades13, df13)
        print("決済月算出中（④⑤）...")
        trades45 = _add_exit_dt(trades45, df45)

    # ── 列を統一して結合 ──
    common = ["system", "signal_dt", "signal_year", "signal_month",
              "signal_weekday", "pnl_pt", "pnl_yen"]
    if USE_SETTLEMENT_MONTH:
        common = common + ["exit_dt"]
    t13 = trades13[common].copy()
    t45 = trades45[common].copy()
    all_trades = pd.concat([t13, t45], ignore_index=True).sort_values("signal_dt").reset_index(drop=True)

    t1 = all_trades[all_trades["system"] == "①"]
    t3 = all_trades[all_trades["system"] == "③"]
    t4 = all_trades[all_trades["system"] == "④"]
    t5 = all_trades[all_trades["system"] == "⑤"]

    # ============================================================
    print(f"\n{SEP80}")
    print("  1. 全体成績（DD制限なし）")
    print(SEP80)
    print(f"  {'':10}  {'件数':>6}  {'勝率%':>6}  {'損益(pt)':>12}  {'損益(円)':>13}  {'期待値':>8}  {'PF':>7}")
    print("  " + SEP72)
    for lbl, t in [("系統①", t1), ("系統③", t3), ("系統④", t4), ("系統⑤", t5), ("合算", all_trades)]:
        print_summary_row(lbl, calc_summary(t))

    # ============================================================
    print(f"\n{SEP80}")
    print("  2. 年別成績（DD制限なし）")
    print(SEP80)
    print(f"  {'':16}  {'件数':>6}  {'勝率%':>6}  {'損益(pt)':>12}  {'損益(円)':>13}  {'期待値':>8}  {'PF':>7}")
    print("  " + SEP72)
    for yr in sorted(all_trades["signal_year"].unique()):
        for lbl, t in [("系統①", t1), ("系統③", t3), ("系統④", t4), ("系統⑤", t5), ("合算", all_trades)]:
            ty = t[t["signal_year"] == yr]
            s  = calc_summary(ty)
            if s["n"] == 0: continue
            row_lbl = f"{yr}  {lbl}"
            print(f"  {row_lbl:16}  {s['n']:>6}  {s['win_rate']:>5.1f}%  "
                  f"{s['pnl_pt']:>+12.1f}  {s['pnl_yen']:>+13,}  {s['ev']:>+8.2f}  {pf_s(s['pf'])}")
        print("  " + SEP72)

    # ── 個別DD適用（①③: -30,000 / ④⑤: -20,000）──
    DD_13 = -30_000
    DD_45 = -20_000
    t13_all = pd.concat([t1, t3]).sort_values("signal_dt")
    t45_all = pd.concat([t4, t5]).sort_values("signal_dt")
    res13_i    = sim_monthly_dd(t13_all, DD_13)
    res45_i    = sim_monthly_dd(t45_all, DD_45)
    active13_i = res13_i["active"]
    active45_i = res45_i["active"]
    active_indiv = pd.concat([active13_i, active45_i]).sort_values("signal_dt").reset_index(drop=True)

    # 合算DD（参照用）
    DD_LIMIT = -30_000
    res_dd = sim_monthly_dd(all_trades, DD_LIMIT)
    active = res_dd["active"]
    a1 = active[active["system"] == "①"]
    a3 = active[active["system"] == "③"]
    a4 = active[active["system"] == "④"]
    a5 = active[active["system"] == "⑤"]

    # ============================================================
    months = list(range(1, 13))

    def print_cross_table(label, trades_list, dd_label):
        print(f"\n{SEP80}")
        print(f"  3. 年x月クロス集計 - 損益(円)（{dd_label}）")
        print(SEP80)
        hdr = "  年  系統  " + "".join(f"  {m:>5}月" for m in months) + "     合計"
        print(hdr)
        print("-" * len(hdr))
        all_t = pd.concat([t for _, t in trades_list])
        for yr in sorted(all_t["signal_year"].unique()):
            for sys_lbl, t in trades_list + [("合", all_t)]:
                ty = t[t["signal_year"] == yr]
                vals = [f"{int(ty[ty['signal_month']==mo]['pnl_yen'].sum()):>+7,}"
                        if ty[ty['signal_month']==mo]['pnl_yen'].sum() != 0 else "      -"
                        for mo in months]
                print(f"  {yr}  {sys_lbl:>2}   " + "  ".join(vals) + f"  {int(ty['pnl_yen'].sum()):>+9,}")
            print()

    print_cross_table("①③", [("①", active13_i[active13_i["system"]=="①"]),
                               ("③", active13_i[active13_i["system"]=="③"])],
                      f"①③ 月次DD {DD_13:,}円")
    print_cross_table("④⑤", [("④", active45_i[active45_i["system"]=="④"]),
                               ("⑤", active45_i[active45_i["system"]=="⑤"])],
                      f"④⑤ 月次DD {DD_45:,}円")

    # ── 合算月次累積損益（セクション3と同じグリッド形式）──
    print(f"\n  【①③+④⑤ 月次累積損益（個別DD適用後）】")
    hdr2 = "  年  系統  " + "".join(f"  {m:>5}月" for m in months) + "     合計     累積"
    print(hdr2)
    print("-" * len(hdr2))
    cumulative = 0
    for yr in sorted(active_indiv["signal_year"].unique()):
        rows = {
            "①③": active13_i[active13_i["signal_year"]==yr],
            "④⑤": active45_i[active45_i["signal_year"]==yr],
            "合":  active_indiv[active_indiv["signal_year"]==yr],
        }
        yr_total = 0
        for sys_lbl, t in rows.items():
            vals = []
            for mo in months:
                v = int(t[t["signal_month"]==mo]["pnl_yen"].sum())
                vals.append(f"{v:>+7,}" if v != 0 else "      -")
            total = int(t["pnl_yen"].sum())
            if sys_lbl == "合":
                cumulative += total
                print(f"  {yr}  {sys_lbl:>2}   " + "  ".join(vals) + f"  {total:>+9,}  {cumulative:>+9,}")
            else:
                print(f"  {yr}  {sys_lbl:>2}   " + "  ".join(vals) + f"  {total:>+9,}")
        print()

    # ── 合算DD -30,000 版 ──
    print(f"\n  【①③+④⑤ 月次累積損益（合算DD -30,000円適用後）】")
    hdr3 = "  年  系統  " + "".join(f"  {m:>5}月" for m in months) + "     合計     累積"
    print(hdr3)
    print("-" * len(hdr3))

    a_comb = res_dd["active"]
    ac13 = a_comb[a_comb["system"].isin(["①","③"])]
    ac45 = a_comb[a_comb["system"].isin(["④","⑤"])]

    cumulative2 = 0
    for yr in sorted(a_comb["signal_year"].unique()):
        rows2 = {
            "①③": ac13[ac13["signal_year"]==yr],
            "④⑤": ac45[ac45["signal_year"]==yr],
            "合":  a_comb[a_comb["signal_year"]==yr],
        }
        for sys_lbl, t in rows2.items():
            vals = []
            for mo in months:
                v = int(t[t["signal_month"]==mo]["pnl_yen"].sum())
                vals.append(f"{v:>+7,}" if v != 0 else "      -")
            total = int(t["pnl_yen"].sum())
            if sys_lbl == "合":
                cumulative2 += total
                print(f"  {yr}  {sys_lbl:>2}   " + "  ".join(vals) + f"  {total:>+9,}  {cumulative2:>+9,}")
            else:
                print(f"  {yr}  {sys_lbl:>2}   " + "  ".join(vals) + f"  {total:>+9,}")
        print()

    # ============================================================
    print(f"\n{SEP80}")
    print(f"  4. 年別PF（月次DD {DD_LIMIT:,}円適用）")
    print(SEP80)
    print(f"\n  {'年':>6}  {'①N':>5} {'①PF':>6}  {'③N':>5} {'③PF':>6}  "
          f"{'④N':>5} {'④PF':>6}  {'⑤N':>5} {'⑤PF':>6}  {'合N':>6} {'合PF':>7}  {'合損益(円)':>13}")
    print("  " + "-" * 90)
    for yr in sorted(active["signal_year"].unique()):
        r1 = calc_summary(a1[a1["signal_year"] == yr])
        r3 = calc_summary(a3[a3["signal_year"] == yr])
        r4 = calc_summary(a4[a4["signal_year"] == yr])
        r5 = calc_summary(a5[a5["signal_year"] == yr])
        ra = calc_summary(active[active["signal_year"] == yr])
        print(f"  {yr:>6}  {r1['n']:>5} {pf_s(r1['pf']):>6}  {r3['n']:>5} {pf_s(r3['pf']):>6}  "
              f"{r4['n']:>5} {pf_s(r4['pf']):>6}  {r5['n']:>5} {pf_s(r5['pf']):>6}  "
              f"{ra['n']:>6} {pf_s(ra['pf']):>7}  {ra['pnl_yen']:>+13,}")
    print("  " + "-" * 90)
    r1a = calc_summary(a1); r3a = calc_summary(a3)
    r4a = calc_summary(a4); r5a = calc_summary(a5); raa = calc_summary(active)
    print(f"  {'全期間':>6}  {r1a['n']:>5} {pf_s(r1a['pf']):>6}  {r3a['n']:>5} {pf_s(r3a['pf']):>6}  "
          f"{r4a['n']:>5} {pf_s(r4a['pf']):>6}  {r5a['n']:>5} {pf_s(r5a['pf']):>6}  "
          f"{raa['n']:>6} {pf_s(raa['pf']):>7}  {raa['pnl_yen']:>+13,}")
    print(f"  スキップ: {res_dd['skipped']}件  DD発動月: {res_dd['months_triggered']}ヶ月")

    # ============================================================
    print(f"\n{SEP80}")
    print("  5. 月次損失上限分析（系統①③④⑤ 合算）")
    print(SEP80)
    limits = [None, -20_000, -30_000, -40_000, -50_000, -60_000]
    print(f"\n  {'制限(円)':>12}  {'件数':>6}  {'スキップ':>8}  {'発動月':>6}  {'勝率%':>6}  {'損益(円)':>13}  {'PF':>7}")
    print("  " + "-" * 72)
    for lim in limits:
        if lim is None:
            s = calc_summary(all_trades)
            print(f"  {'制限なし':>12}  {s['n']:>6}  {'':>8}  {'':>6}  "
                  f"{s['win_rate']:>5.1f}%  {s['pnl_yen']:>+13,}  {pf_s(s['pf']):>7}")
        else:
            res = sim_monthly_dd(all_trades, lim)
            s   = calc_summary(res["active"])
            print(f"  {lim:>+12,}  {s['n']:>6}  {res['skipped']:>8}  {res['months_triggered']:>6}  "
                  f"{s['win_rate']:>5.1f}%  {s['pnl_yen']:>+13,}  {pf_s(s['pf']):>7}")

    # ── DD -30,000 後の系統別件数内訳 ──
    print(f"\n  系統別件数内訳（月次DD {DD_LIMIT:,}円適用後）")
    print(f"  {'系統':>6}  {'DD前':>6}  {'DD後':>6}  {'スキップ':>8}  {'勝率%':>6}  {'損益(円)':>13}  {'PF':>7}")
    print("  " + "-" * 62)
    for sys_lbl, bf, af in [("①", t1, a1), ("③", t3, a3), ("④", t4, a4), ("⑤", t5, a5),
                             ("合算", all_trades, active)]:
        sb = calc_summary(bf); sa = calc_summary(af)
        skip = sb["n"] - sa["n"]
        print(f"  {sys_lbl:>6}  {sb['n']:>6}  {sa['n']:>6}  {skip:>8}  "
              f"{sa['win_rate']:>5.1f}%  {sa['pnl_yen']:>+13,}  {pf_s(sa['pf']):>7}")

    print(f"\n  系統別個別DD適用後の合算（①③: {DD_13:,}円 / ④⑤: {DD_45:,}円）")
    print(f"  {'系統':>8}  {'DD前':>6}  {'DD後':>6}  {'スキップ':>8}  {'勝率%':>6}  {'損益(円)':>13}  {'PF':>7}")
    print("  " + "-" * 66)
    for lbl, bf, af, res in [
        ("①③", t13_all, active13_i, res13_i),
        ("④⑤", t45_all, active45_i, res45_i),
        ("合算", all_trades, active_indiv, None),
    ]:
        sb = calc_summary(bf); sa = calc_summary(af)
        skip = sb["n"] - sa["n"]
        mo_str = f"発動月:{res['months_triggered']}" if res else ""
        print(f"  {lbl:>8}  {sb['n']:>6}  {sa['n']:>6}  {skip:>8}  "
              f"{sa['win_rate']:>5.1f}%  {sa['pnl_yen']:>+13,}  {pf_s(sa['pf']):>7}  {mo_str}")

    sai = calc_summary(active_indiv)
    print(f"\n  ┌─ 合算DD（{DD_LIMIT:,}）後: {calc_summary(active)['pnl_yen']:>+13,}円  PF {pf_s(calc_summary(active)['pf'])}  件数:{calc_summary(active)['n']}")
    print(f"  └─ 個別DD後の合算    : {sai['pnl_yen']:>+13,}円  PF {pf_s(sai['pf'])}  件数:{sai['n']}")

    # ============================================================
    print(f"\n{SEP80}")
    print(f"  6. スリッページ耐久性分析（個別DD適用後）")
    print(SEP80)
    slips = [0, 2, 4, 6, 8, 10, 15, 20]

    for grp_lbl, grp, dd_lbl in [
        ("①③④⑤ 合算DD", active,       f"合算DD {DD_LIMIT:,}円"),
        ("①③④⑤ 個別DD", active_indiv, f"①③{DD_13:,} / ④⑤{DD_45:,}"),
        ("①③",          active13_i,   f"DD {DD_13:,}円"),
        ("④⑤",          active45_i,   f"DD {DD_45:,}円"),
    ]:
        print(f"\n  [{grp_lbl}]  ({dd_lbl})")
        print(f"  {'slip':>5}  {'件数':>6}  {'勝率%':>6}  {'損益(円)':>13}  {'PF':>7}")
        print("  " + "-" * 46)
        for sl in slips:
            t = grp.copy()
            t["pnl_pt"]  = t["pnl_pt"] - sl
            t["pnl_yen"] = (t["pnl_pt"] * PT_TO_YEN).round().astype(int)
            s = calc_summary(t)
            print(f"  {sl:>3}pt  {s['n']:>6}  {s['win_rate']:>5.1f}%  {s['pnl_yen']:>+13,}  {pf_s(s['pf']):>7}")


if __name__ == "__main__":
    out_path = "bt_result_combined.txt"
    with open(out_path, "w", encoding="utf-8") as f:
        sys.stdout = f
        main()
    sys.stdout = sys.__stdout__
    print(f"出力完了: {out_path}")
    import subprocess
    subprocess.Popen(["code", out_path])
