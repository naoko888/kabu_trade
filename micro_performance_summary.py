"""
micro_performance_summary.py
実運用 vs BT（XLSX全期間 / CSV直近）比較サマリー
BT エンジン: backtest_system123_combined + backtest_system45_combined
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import timedelta

import backtest_system123_combined as bt13

# ===== パス =====
LIVE_LOG_FILE = Path(r"C:\kabu_trade\logs\micro_dry_log_all.csv")
DATA_DIR      = Path(r"C:\kabu_trade\data")
EXCEL_FILES   = [
    "N225microf_2023.xlsx",
    "N225microf_2024.xlsx",
    "N225microf_2025.xlsx",
    "N225microf_2026.xlsx",
]
MICRO_CSV = Path(r"C:\kabu_trade\micro_5min.csv")
OUT_FILE  = Path(r"C:\kabu_trade\logs\micro_performance_summary.csv")

# ===== 定数 =====
PT_TO_YEN     = 10
COMMISSION_PT = 2.2
ALL_DD_LIMIT  = -30_000   # 月次合算DD上限（backtest_combined_all.py と同値）

# BT設定（backtest_combined_all.py と完全一致）
BT13_KWARGS = dict(
    s1_excl_months  = bt13.S1_EXCL_BASE,
    s3_excl_months  = bt13.S3_EXCL_MONTHS,
    s1_weekdays     = (0, 1, 2),
    s1_hours_dst    = (2, 8, 18, 19, 21),
    s1_hours_win    = (2, 8, 12, 18, 21, 23),
    s3_hours_dst    = (0, 5, 8, 19, 20, 23),
    s3_hours_win    = (4, 5, 17, 18, 19, 20, 21),
    s3_weekdays_dst = (0, 2, 3, 4),
    s3_weekdays_win = (0, 2, 3, 4),
)
# TP/SL/side（表示用）※ ④⑤は実運用停止中のため除外
SYS_PARAMS = {
    "①": dict(tp=bt13.TP, sl=bt13.SL, side="long"),
    "③": dict(tp=bt13.TP, sl=bt13.SL, side="short"),
}

# 除外月（実運用のみ内訳用）
SYS_EXCL_MONTHS = {
    "①": set(bt13.S1_EXCL_BASE),
    "③": set(bt13.S3_EXCL_MONTHS),
}

SEP = "=" * 80


# ===== ユーティリティ =====
def get_trade_date(ts: pd.Timestamp):
    """entry_time → 取引日（17時以降は翌日扱い、週末は月曜補正）"""
    if ts.hour >= 17:
        base = (ts + timedelta(days=1)).date()
    else:
        base = ts.date()
    wd = base.weekday()
    if wd == 5:
        base += timedelta(days=2)
    elif wd == 6:
        base += timedelta(days=1)
    return base


def _add_side(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["side"] = df["system"].map(lambda s: SYS_PARAMS.get(s, {}).get("side", "?"))
    return df


def _tp_sl_price(ep: float, sys: str):
    p   = SYS_PARAMS.get(sys, {})
    tp  = p.get("tp", 0)
    sl  = p.get("sl", 0)
    side = p.get("side", "long")
    if side == "long":
        return int(round(ep + tp)), int(round(ep - sl))
    else:
        return int(round(ep - tp)), int(round(ep + sl))


def _pf(wins, loss):
    return wins / loss if loss > 0 else float("inf")


def _pf_s(v):
    return "  inf" if v == float("inf") else f"{v:.3f}"


# ===== データ読み込み =====
def _tday_sort(df: pd.DataFrame) -> pd.DataFrame:
    """bt45 スタイル: _tday グローバルソート"""
    df = df.drop_duplicates(subset=["datetime"], keep="last").copy()
    df["_tday"] = df["datetime"].apply(
        lambda dt: (dt - pd.Timedelta(days=1)).date() if dt.hour < 17 else dt.date()
    )
    return df.sort_values(["_tday", "datetime"]).drop(columns=["_tday"]).reset_index(drop=True)


def _bt13_sort(df: pd.DataFrame) -> pd.DataFrame:
    """bt13 スタイル: _trading_day_sort_key でソート（bt13.read_excel と同一）"""
    df = df.drop_duplicates(subset=["datetime"], keep="last").copy()
    keys = df["datetime"].map(bt13._trading_day_sort_key)
    return df.iloc[keys.argsort(kind="stable")].reset_index(drop=True)


def _load_raw_xlsx() -> tuple:
    """全 XLSX → (raw_bt13, raw_bt45) 両方とも取引日順ソート"""
    dfs = []
    for fname in EXCEL_FILES:
        p = DATA_DIR / fname
        if p.exists():
            dfs.append(bt13.read_excel(p))   # bt13.read_excel は _trading_day_sort_key ソート済み
    if not dfs:
        return pd.DataFrame(), pd.DataFrame()
    raw = pd.concat(dfs, ignore_index=True)
    td = _bt13_sort(raw)   # 先物は取引日順（夜間→深夜→日中）が正しい
    return td, td.copy()


def _parse_csv() -> pd.DataFrame:
    if not MICRO_CSV.exists():
        return pd.DataFrame()
    df = pd.read_csv(MICRO_CSV)
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["datetime", "open", "high", "low", "close"]).copy()


def _load_raw_csv() -> tuple:
    """micro_5min.csv → (raw_bt13, raw_bt45) 両方とも取引日順ソート"""
    raw = _parse_csv()
    if raw.empty:
        return pd.DataFrame(), pd.DataFrame()
    td = _bt13_sort(raw)   # 先物は取引日順（夜間→深夜→日中）が正しい
    return td, td.copy()


def load_live_log() -> pd.DataFrame:
    if not LIVE_LOG_FILE.exists():
        print("実運用ログが見つかりません")
        return pd.DataFrame()
    df = pd.read_csv(LIVE_LOG_FILE)
    df["entry_time"] = pd.to_datetime(df["entry_time"], errors="coerce")
    df["exit_time"]  = pd.to_datetime(df.get("exit_time", pd.Series(dtype=str)), errors="coerce")
    df["pnl"]        = pd.to_numeric(df["pnl"], errors="coerce")
    df["entry_price"] = pd.to_numeric(df.get("entry_price", np.nan), errors="coerce") if "entry_price" in df.columns else np.nan
    df = df.dropna(subset=["entry_time", "pnl"]).copy()
    df = df[df["system"].astype(str).isin(["①", "③"])].copy()
    df["pnl_pt"]     = df["pnl"] - COMMISSION_PT
    df["pnl_yen"]    = (df["pnl_pt"] * PT_TO_YEN).round(0).astype(int)
    df["trade_date"] = df["entry_time"].apply(get_trade_date)
    return df.sort_values("entry_time").reset_index(drop=True)


# ===== BT 実行 =====
def _add_entry_info(trades: pd.DataFrame, price_df: pd.DataFrame) -> pd.DataFrame:
    """signal_dt + 5min → entry_time / entry_price / trade_date / side を付与"""
    if trades.empty:
        for c in ["entry_time", "entry_price", "trade_date", "side"]:
            trades[c] = np.nan if c == "entry_price" else None
        return trades
    dt_open = price_df.set_index("datetime")["open"].to_dict()
    t = trades.copy()
    t["entry_time"]  = t["signal_dt"] + pd.Timedelta(minutes=5)
    t["entry_price"] = t["entry_time"].map(dt_open)
    t["trade_date"]  = pd.to_datetime(t["signal_dt"]).dt.date  # signal_dt は調整済みなので date() のみ
    return _add_side(t)


def _apply_dd(trades: pd.DataFrame) -> pd.DataFrame:
    """合算月次 DD 適用（backtest_combined_all.py の sim_monthly_dd と同ロジック）"""
    if trades.empty:
        return trades
    df = trades.sort_values("signal_dt").copy()
    df["_ym"] = list(zip(df["signal_year"].astype(int), df["signal_month"].astype(int)))
    keep = []; mo_pnl = {}; triggered = set()
    for _, row in df.iterrows():
        ym = row["_ym"]
        mo_pnl.setdefault(ym, 0.0)
        if ym in triggered:
            keep.append(False)
            continue
        keep.append(True)
        mo_pnl[ym] += float(row["pnl_yen"])
        if mo_pnl[ym] <= ALL_DD_LIMIT:
            triggered.add(ym)
    df["_keep"] = keep
    return df[df["_keep"]].drop(columns=["_ym", "_keep"]).reset_index(drop=True)


def run_bt(raw_bt13: pd.DataFrame, cpi_df: pd.DataFrame, label: str = "") -> pd.DataFrame:
    """raw price df → ①③ BT 実行 + entry_info + DD 適用"""
    if raw_bt13.empty:
        return pd.DataFrame()
    if label:
        print(f"  BT実行中 ({label})...")
    df13 = bt13.add_indicators(raw_bt13.copy())
    t13  = bt13.run_backtest(df13, cpi_df, **BT13_KWARGS)
    if t13.empty:
        return pd.DataFrame()
    combined = (_add_entry_info(t13, raw_bt13)
                .sort_values("signal_dt")
                .reset_index(drop=True))
    combined["pnl_yen"] = combined["pnl_yen"].astype(float).round(0).astype(int)
    return _apply_dd(combined)


# ===== セクション1: BT 合計検証（backtest_combined_all.py との照合用）=====
def print_bt_verification(bt_xlsx: pd.DataFrame):
    print(f"\n{SEP}")
    print(f"  1. BT XLSX 全体成績（DD制限なし）  ← backtest_combined_all.py と照合")
    print(f"{SEP}")
    if bt_xlsx.empty:
        print("  データなし")
        return

    # DD 適用前の全トレードを再構成（bt_xlsx は DD 適用済みのため別途合算）
    print(f"  ※ 以下は DD 適用後（ALL_DD_LIMIT={ALL_DD_LIMIT:,}円）の数値")
    print(f"  {'系統':>4}  {'件数':>6}  {'勝率%':>6}  {'損益pt':>10}  {'損益(円)':>12}  {'EV(pt)':>8}  {'PF':>7}")
    print("  " + "-" * 65)
    total_n = total_yen = 0
    for sys in ["①", "③"]:
        sub = bt_xlsx[bt_xlsx["system"] == sys]
        if sub.empty:
            continue
        pnl = sub["pnl_pt"].astype(float)
        n   = len(sub)
        wins = pnl[pnl > 0].sum()
        loss = abs(pnl[pnl < 0].sum())
        wr   = (pnl > 0).mean() * 100
        ev   = pnl.sum() / n
        yen  = int(sub["pnl_yen"].sum())
        total_n += n; total_yen += yen
        print(f"  {sys:>4}  {n:>6}  {wr:>5.1f}%  {pnl.sum():>+10.1f}  {yen:>+12,}  {ev:>+8.2f}  {_pf_s(_pf(wins, loss)):>7}")
    print("  " + "-" * 65)
    pnl_all = bt_xlsx["pnl_pt"].astype(float)
    wins_all = pnl_all[pnl_all > 0].sum()
    loss_all = abs(pnl_all[pnl_all < 0].sum())
    ev_all = pnl_all.sum() / len(bt_xlsx) if len(bt_xlsx) > 0 else 0
    print(f"  {'合算':>4}  {total_n:>6}  {(pnl_all > 0).mean()*100:>5.1f}%  {pnl_all.sum():>+10.1f}  {total_yen:>+12,}  {ev_all:>+8.2f}  {_pf_s(_pf(wins_all, loss_all)):>7}")


# ===== セクション3: シグナル一致判定 =====
def _make_match_key(df: pd.DataFrame) -> pd.DataFrame:
    """trade_date をキーにする（live/BT とも取引日付で統一）"""
    df = df.copy()
    date_str = df["trade_date"].astype(str)
    hm       = df["entry_time"].dt.floor("5min").dt.strftime("%H:%M")
    df["_key"] = date_str + "|" + df["system"].astype(str) + "|" + df["side"].astype(str) + "|" + hm
    return df


def _print_day_match(day_date, live_day: pd.DataFrame, bt_day: pd.DataFrame, bt_label: str):
    day_str = pd.Timestamp(str(day_date)).strftime("%m/%d")
    print(f"\n----- {day_str} 【{bt_label}】 -----")

    live_k = _make_match_key(live_day) if not live_day.empty else pd.DataFrame(columns=["_key"])
    bt_k   = _make_match_key(bt_day)   if not bt_day.empty   else pd.DataFrame(columns=["_key"])

    live_idx = live_k.set_index("_key") if not live_k.empty else pd.DataFrame()
    bt_idx   = bt_k.set_index("_key")   if not bt_k.empty   else pd.DataFrame()
    live_keys = set(live_idx.index) if not live_idx.empty else set()
    bt_keys   = set(bt_idx.index)   if not bt_idx.empty   else set()

    matched   = sorted(live_keys & bt_keys)
    live_only = sorted(live_keys - bt_keys)
    bt_only   = sorted(bt_keys   - live_keys)

    def _get(df_idx, key):
        rows = df_idx[df_idx.index == key]
        return rows.iloc[0] if not rows.empty else None

    def _split(key):
        parts = key.split("|")
        return parts[0], parts[1], parts[2], parts[3]

    # EP差閾値: これ以上は「時刻一致・EP乖離」として分離
    EP_DIFF_THRESHOLD = 50

    genuine, drift = [], []
    for key in matched:
        live_row = _get(live_idx, key)
        bt_row   = _get(bt_idx,   key)
        ep_l = float(live_row.get("entry_price", np.nan)) if live_row is not None else np.nan
        ep_b = float(bt_row.get("entry_price",   np.nan)) if bt_row   is not None else np.nan
        diff = abs(ep_l - ep_b) if not (np.isnan(ep_l) or np.isnan(ep_b)) else 0
        (genuine if diff <= EP_DIFF_THRESHOLD else drift).append(key)

    # [一致]
    print(f"[一致] {len(genuine)}件")
    for key in genuine:
        live_row = _get(live_idx, key)
        bt_row   = _get(bt_idx,   key)
        date_str, sys, side, hm = _split(key)
        ep_l = float(live_row.get("entry_price", np.nan)) if live_row is not None else np.nan
        ep_b = float(bt_row.get("entry_price",   np.nan)) if bt_row   is not None else np.nan
        if not np.isnan(ep_l):
            tp_p, sl_p = _tp_sl_price(ep_l, sys)
            diff   = int(ep_l - ep_b) if not np.isnan(ep_b) else "?"
            ep_b_s = str(int(ep_b)) if not np.isnan(ep_b) else "?"
            diff_s = f"{diff:>+4}" if isinstance(diff, int) else diff
            print(f"  {day_str} {hm}  {sys} {side:<5}  [実]EP:{int(ep_l)} TP:{tp_p} SL:{sl_p}  [BT]EP:{ep_b_s}  差:{diff_s}")
        else:
            print(f"  {day_str} {hm}  {sys} {side:<5}  EP:?")

    # [時刻一致・EP乖離] データ差異（週末ギャップ等でBTと実市場が乖離）
    if drift:
        print(f"[時刻一致・EP乖離 >{EP_DIFF_THRESHOLD}pt] {len(drift)}件  ← BTデータと実市場の価格差異")
        for key in drift:
            live_row = _get(live_idx, key)
            bt_row   = _get(bt_idx,   key)
            date_str, sys, side, hm = _split(key)
            ep_l = float(live_row.get("entry_price", np.nan)) if live_row is not None else np.nan
            ep_b = float(bt_row.get("entry_price",   np.nan)) if bt_row   is not None else np.nan
            diff = int(ep_l - ep_b) if not (np.isnan(ep_l) or np.isnan(ep_b)) else 0
            print(f"  {day_str} {hm}  {sys} {side:<5}  [実]EP:{int(ep_l) if not np.isnan(ep_l) else '?'}  [BT]EP:{int(ep_b) if not np.isnan(ep_b) else '?'}  差:{diff:>+4}")

    # [実運用のみ]
    print(f"[実運用のみ] {len(live_only)}件")
    excl_cnt = mismatch_cnt = 0
    for key in live_only:
        live_row = _get(live_idx, key)
        date_str, sys, side, hm = _split(key)
        ep  = float(live_row.get("entry_price", np.nan)) if live_row is not None else np.nan
        yen = int(live_row.get("pnl_yen", 0))            if live_row is not None else 0
        month   = pd.Timestamp(date_str).month
        is_excl = sys in SYS_EXCL_MONTHS and month in SYS_EXCL_MONTHS[sys]
        tag = "[除外月]" if is_excl else "[不一致]"
        if is_excl: excl_cnt += 1
        else:       mismatch_cnt += 1
        if not np.isnan(ep):
            tp_p, sl_p = _tp_sl_price(ep, sys)
            print(f"  {day_str} {hm}  {sys} {side:<5}  EP:{int(ep)} TP:{tp_p} SL:{sl_p}  pnl:{yen:+,}  {tag}")
        else:
            print(f"  {day_str} {hm}  {sys} {side:<5}  EP:?  pnl:{yen:+,}  {tag}")
    if live_only:
        print(f"  ▶ 内訳: 除外月:{excl_cnt}件 / 不一致:{mismatch_cnt}件")

    # [BTのみ]
    print(f"[BTのみ] {len(bt_only)}件")
    for key in bt_only:
        bt_row = _get(bt_idx, key)
        date_str, sys, side, hm = _split(key)
        ep  = float(bt_row.get("entry_price", np.nan)) if bt_row is not None else np.nan
        yen = int(bt_row.get("pnl_yen", 0))            if bt_row is not None else 0
        if not np.isnan(ep):
            tp_p, sl_p = _tp_sl_price(ep, sys)
            print(f"  {day_str} {hm}  {sys} {side:<5}  EP:{int(ep)} TP:{tp_p} SL:{sl_p}  pnl:{yen:+,}")
        else:
            print(f"  {day_str} {hm}  {sys} {side:<5}  EP:?  pnl:{yen:+,}")


def print_signal_match_section(live_df: pd.DataFrame, bt_xlsx: pd.DataFrame, bt_csv: pd.DataFrame, days: int = 5):
    # 実運用が存在する日のみ表示（BTのみの日は除外）
    if live_df.empty or "entry_time" not in live_df.columns:
        print("  実運用ログなし")
        return
    today     = get_trade_date(pd.Timestamp.now())
    all_dates = set(live_df["trade_date"].unique())
    recent    = sorted([d for d in all_dates if d <= today], reverse=True)[:days]

    for day in recent:
        live_d = live_df[live_df["trade_date"] == day].copy()
        xlsx_d = bt_xlsx[bt_xlsx["trade_date"] == day].copy() if not bt_xlsx.empty else pd.DataFrame()
        csv_d  = bt_csv[bt_csv["trade_date"]  == day].copy() if not bt_csv.empty  else pd.DataFrame()
        _print_day_match(day, live_d, xlsx_d, "Excelデータ BT")
        _print_day_match(day, live_d, csv_d,  "CSV BT")


# ===== セクション4: 損益サマリー =====
def print_pnl_summary(live_df: pd.DataFrame, bt_xlsx: pd.DataFrame, bt_csv: pd.DataFrame, now: pd.Timestamp):
    today = get_trade_date(now)
    yest  = today - timedelta(days=1)
    wd = yest.weekday()
    if wd == 6: yest -= timedelta(days=2)
    elif wd == 5: yest -= timedelta(days=1)

    def _sum(df, filt):
        if df.empty: return 0, 0
        sub = filt(df)
        return len(sub), int(sub["pnl_yen"].sum()) if not sub.empty else 0

    def by_td(target_date):
        return lambda df: df[df["trade_date"] == target_date]

    def by_et(delta_days):
        cutoff = now - pd.Timedelta(days=delta_days)
        return lambda df: df[df["entry_time"] >= cutoff]

    periods = [
        ("本日",  by_td(today)),
        ("昨日",  by_td(yest)),
        ("5日",   by_et(5)),
        ("10日",  by_et(10)),
    ]

    print(f"  {'期間':>4}  {'実運用N':>8}  {'実運用(円)':>14}  {'BT-XLSX(円)':>14}  {'BT-CSV(円)':>14}")
    print("  " + "-" * 62)
    for label, filt in periods:
        n_live,  yen_live  = _sum(live_df, filt)
        _,       yen_xlsx  = _sum(bt_xlsx,  filt)
        _,       yen_csv   = _sum(bt_csv,   filt)
        print(f"  {label:>4}  {n_live:>8}  {yen_live:>+14,}  {yen_xlsx:>+14,}  {yen_csv:>+14,}")

    print(f"\n  系統別（実運用 5日）")
    live_5 = live_df[live_df["entry_time"] >= now - pd.Timedelta(days=5)] if not live_df.empty else pd.DataFrame()
    for sys in ["①", "③"]:
        sub = live_5[live_5["system"] == sys] if not live_5.empty else pd.DataFrame()
        yen = int(sub["pnl_yen"].sum()) if not sub.empty else 0
        print(f"    {sys}:  {len(sub):>4}件  {yen:>+10,}")


# ===== セクション5: 年×月クロス集計 =====
def _print_ym_table(title: str, df: pd.DataFrame, yr_col: str, mo_col: str):
    if df is None or df.empty:
        print(f"\n===== {title}: データなし =====")
        return
    print(f"\n===== {title} =====")
    months = range(1, 13)
    print("  年  " + "".join(f"  {m:>2}月" for m in months) + "     合計")
    print("  " + "-" * 97)
    for yr in sorted(df[yr_col].unique()):
        df_yr = df[df[yr_col] == yr]
        total = 0
        vals  = []
        for m in months:
            v = int(df_yr[df_yr[mo_col] == m]["pnl_yen"].sum())
            vals.append(f"{v:>+8,}" if v != 0 else f"{'':>8}")
            total += v
        print(f"  {yr}  " + "  ".join(vals) + f"  {total:>+10,}")


def print_ym_cross(live_df: pd.DataFrame, bt_xlsx: pd.DataFrame, bt_csv: pd.DataFrame):
    if not live_df.empty:
        ld = live_df.copy()
        ld["_yr"] = ld["entry_time"].dt.year
        ld["_mo"] = ld["entry_time"].dt.month
        _print_ym_table("実運用デモ", ld, "_yr", "_mo")

    if not bt_xlsx.empty:
        _print_ym_table("BT XLSX（月次DD適用後）", bt_xlsx, "signal_year", "signal_month")

    if not bt_csv.empty:
        _print_ym_table("BT CSV（月次DD適用後）", bt_csv, "signal_year", "signal_month")


# ===== メイン =====
def main():
    now = pd.Timestamp.now()
    print(f"\n実行日時: {now.strftime('%Y-%m-%d %H:%M:%S')}\n")

    cpi_df  = bt13.load_cpi()
    live_df = load_live_log()
    # 未来のトレードを除外（ログには将来分も記録されている場合があるため）
    if not live_df.empty:
        live_df = live_df[live_df["entry_time"] <= now].copy()

    print("XLSX データ読み込み中...")
    raw_xlsx_bt13, _ = _load_raw_xlsx()
    print(f"  {len(raw_xlsx_bt13):,} 本  ({raw_xlsx_bt13['datetime'].min()} ~ {raw_xlsx_bt13['datetime'].max()})" if not raw_xlsx_bt13.empty else "  データなし")

    print("CSV データ読み込み中...")
    raw_csv_bt13, _ = _load_raw_csv()
    print(f"  {len(raw_csv_bt13):,} 本  ({raw_csv_bt13['datetime'].min()} ~ {raw_csv_bt13['datetime'].max()})" if not raw_csv_bt13.empty else "  データなし")

    bt_xlsx = run_bt(raw_xlsx_bt13, cpi_df, label="XLSX")
    bt_csv  = run_bt(raw_csv_bt13,  cpi_df, label="CSV")

    # BT の未来シグナルを除外（trade_date で比較。entry_time は調整済みで物理時刻と異なる場合あり）
    today_td = get_trade_date(now)
    if not bt_xlsx.empty:
        bt_xlsx = bt_xlsx[bt_xlsx["trade_date"] <= today_td].copy()
    if not bt_csv.empty:
        bt_csv = bt_csv[bt_csv["trade_date"] <= today_td].copy()

    # セクション1: BT検証
    print_bt_verification(bt_xlsx)

    # セクション3: シグナル一致判定
    print(f"\n{SEP}")
    print(f"  3. シグナル一致判定（直近 5 日）")
    print(f"{SEP}")
    if not live_df.empty:
        print_signal_match_section(live_df, bt_xlsx, bt_csv, days=5)
    else:
        print("  実運用ログなし")

    # セクション4: 損益サマリー
    print(f"\n{SEP}")
    print(f"  4. 損益サマリー")
    print(f"{SEP}")
    if not live_df.empty:
        print_pnl_summary(live_df, bt_xlsx, bt_csv, now)
    else:
        print("  実運用ログなし")

    # セクション5: 年×月クロス集計
    print(f"\n{SEP}")
    print(f"  5. 年×月クロス集計（損益円）")
    print(f"{SEP}")
    print_ym_cross(live_df, bt_xlsx, bt_csv)

    # CSV 保存
    if not live_df.empty:
        OUT_FILE.parent.mkdir(exist_ok=True)
        live_df.to_csv(OUT_FILE, index=False, encoding="utf-8-sig")
        print(f"\n[保存] {OUT_FILE}")


if __name__ == "__main__":
    main()
