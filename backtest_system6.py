"""
backtest_system6.py
=========================================
【逆張り BB版】系統⑥ 売り買い両対応
- 低ボラ時（BB幅収縮）にバンドタッチで逆張り
- 下バンドタッチ → 新規買い
- 上バンドタッチ → 新規売り
"""

import sys
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path
import pandas as pd
import numpy as np

# =========================
# 設定
# =========================
DATA_DIR  = Path(r"C:\kabu_trade\data")
EXCEL_FILES = [
    "N225microf_2023.xlsx",
    "N225microf_2024.xlsx",
    "N225microf_2025.xlsx",
    "N225microf_2026.xlsx",
]
MICRO_CSV = Path(r"C:\kabu_trade\micro_5min.csv")
CPI_CSV   = Path(r"C:\kabu_trade\economic_calendar.csv")

COMMISSION_PT = 2.2
PT_TO_YEN     = 10
DD_LIMIT      = -30_000

# ── BBパラメータ ──
BB_PERIOD   = 20      # BBバンド期間（5分足）
BB_SIGMA    = 2.0     # σ

# ── エントリー条件 ──
BB_WIDTH_TH = 0.030   # 低ボラ判定: bb_width < この値（0.0 = 無効）
TP          = 200     # 利確幅（pt）
SL          = 80      # 損切幅（pt）
MAX_HOLD    = 6       # 最大保有バー数

# ── 時間帯・曜日・除外月 ──
S6_HOURS_DST   = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23)
S6_HOURS_WIN   = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23)
S6_WEEKDAYS    = (0, 1, 2, 3, 4)
S6_EXCL_MONTHS: tuple = ()


# DST期間
_DST_PERIODS = [
    (pd.Timestamp("2023-03-12"), pd.Timestamp("2023-11-05")),
    (pd.Timestamp("2024-03-10"), pd.Timestamp("2024-11-03")),
    (pd.Timestamp("2025-03-09"), pd.Timestamp("2025-11-02")),
    (pd.Timestamp("2026-03-08"), pd.Timestamp("2026-11-01")),
]


def _trading_day_sort_key(dt):
    if dt.hour < 17:
        trading_date = (dt - pd.Timedelta(days=1)).date()
    else:
        trading_date = dt.date()
    return (pd.Timestamp(trading_date), dt)


# =========================
# データ読み込み
# =========================
def read_excel(path: Path) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name="5min", engine="openpyxl")
    df = df.rename(columns={
        "日付": "date", "時間": "time",
        "始値": "open", "高値": "high", "安値": "low",
        "終値": "close", "出来高": "volume",
    })
    df["datetime"] = pd.to_datetime(
        df["date"].astype(str).str.strip() + " " + df["time"].astype(str).str.strip(),
        errors="coerce",
    )
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["datetime", "open", "high", "low", "close"]).copy()
    return df[["datetime", "open", "high", "low", "close", "volume"]].sort_values("datetime")


def load_data() -> pd.DataFrame:
    dfs = []
    print("データ読み込み中...")
    for fname in EXCEL_FILES:
        p = DATA_DIR / fname
        if not p.exists():
            print(f"  スキップ: {p}")
            continue
        d = read_excel(p)
        print(f"  {fname}: {len(d)} 本")
        dfs.append(d)
    if MICRO_CSV.exists():
        try:
            dc = pd.read_csv(MICRO_CSV, index_col="datetime", parse_dates=True).reset_index()
            if dc["datetime"].dt.tz is not None:
                dc["datetime"] = dc["datetime"].dt.tz_convert("Asia/Tokyo").dt.tz_localize(None)
            for c in ["open", "high", "low", "close", "volume"]:
                if c in dc.columns:
                    dc[c] = pd.to_numeric(dc[c], errors="coerce")
            dc = dc.dropna(subset=["datetime", "open", "high", "low", "close"])[
                ["datetime", "open", "high", "low", "close", "volume"]].sort_values("datetime")
            print(f"  micro_5min.csv: {len(dc)} 本")
            dfs.append(dc)
        except Exception as e:
            print(f"  micro_5min.csv 読み込み失敗: {e}")
    if not dfs:
        raise FileNotFoundError("データファイルが見つかりません")
    df = pd.concat(dfs, ignore_index=True).drop_duplicates(subset=["datetime"]).copy()
    df["_tday"]     = df["datetime"].dt.date
    df["_sort_grp"] = (df["datetime"].dt.hour < 17).astype(int)
    df = df.sort_values(["_tday", "_sort_grp", "datetime"]).drop(columns=["_tday", "_sort_grp"]).reset_index(drop=True)
    print(f"合計: {len(df)} 本  ({df['datetime'].min()} ~ {df['datetime'].max()})\n")
    return df


ADX_PERIOD = 14   # ADX計算期間

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    # ── BB ──
    roll = df["close"].rolling(BB_PERIOD)
    df["bb_mid"]   = roll.mean()
    df["bb_std"]   = roll.std()
    df["bb_upper"] = df["bb_mid"] + BB_SIGMA * df["bb_std"]
    df["bb_lower"] = df["bb_mid"] - BB_SIGMA * df["bb_std"]
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]
    # ── ADX（Wilder平滑化）──
    hi = df["high"]; lo = df["low"]; cl = df["close"]
    up   = hi.diff()
    down = -lo.diff()
    dm_plus  = np.where((up > down) & (up > 0), up, 0.0)
    dm_minus = np.where((down > up) & (down > 0), down, 0.0)
    tr = pd.concat([
        hi - lo,
        (hi - cl.shift()).abs(),
        (lo - cl.shift()).abs(),
    ], axis=1).max(axis=1)
    n = ADX_PERIOD
    alpha = 1.0 / n
    tr_s   = tr.ewm(alpha=alpha, adjust=False).mean() * n
    dmp_s  = pd.Series(dm_plus,  index=df.index).ewm(alpha=alpha, adjust=False).mean() * n
    dmm_s  = pd.Series(dm_minus, index=df.index).ewm(alpha=alpha, adjust=False).mean() * n
    di_plus  = 100 * dmp_s / tr_s.replace(0, np.nan)
    di_minus = 100 * dmm_s / tr_s.replace(0, np.nan)
    dx = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus).replace(0, np.nan)
    df["adx"] = dx.ewm(alpha=alpha, adjust=False).mean() * n / n
    # ── 曜日 ──
    df["trading_weekday"] = df["datetime"].apply(
        lambda dt: (dt - pd.Timedelta(days=1)).weekday() if dt.hour < 17 else dt.weekday()
    )
    return df


def load_cpi() -> pd.DataFrame:
    for enc in ("utf-8", "utf-8-sig", "cp932"):
        try:
            df = pd.read_csv(CPI_CSV, encoding=enc)
            if "indicator" not in df.columns:
                continue
            df["release_datetime_jst"] = pd.to_datetime(df["release_datetime_jst"], errors="coerce")
            cpi = df[df["indicator"] == "米CPI"].dropna(
                subset=["release_datetime_jst"]).reset_index(drop=True)
            print(f"[OK] CPI読み込み: {len(cpi)}件")
            return cpi
        except Exception:
            continue
    print("[WARN] CPI読み込み失敗 -> CPI除外無効")
    return pd.DataFrame(columns=["release_datetime_jst"])


def build_masks(dts_ns: np.ndarray, cpi_df: pd.DataFrame) -> tuple:
    n = len(dts_ns)
    dst_mask = np.zeros(n, dtype=bool)
    cpi_mask = np.zeros(n, dtype=bool)
    for start, end in _DST_PERIODS:
        dst_mask |= (dts_ns >= start.value) & (dts_ns <= end.value)
    if len(cpi_df) > 0:
        b_ns = int(pd.Timedelta(minutes=30).total_seconds() * 1e9)
        a_ns = int(pd.Timedelta(minutes=60).total_seconds() * 1e9)
        for r in cpi_df["release_datetime_jst"]:
            r_ns = pd.Timestamp(r).value
            cpi_mask |= (dts_ns >= r_ns - b_ns) & (dts_ns <= r_ns + a_ns)
    return dst_mask, cpi_mask


# =========================
# バックテスト
# =========================
ADX_TH = 25.0   # ADX閾値（この値未満 = 低ボラ・非トレンド）

def run_backtest(
    df: pd.DataFrame,
    cpi_df: pd.DataFrame,
    bb_width_th: float = None,
    adx_th: float = None,
    tp: int = None,
    sl: int = None,
    max_hold: int = None,
    s6_weekdays=None,
    s6_hours_dst=None,
    s6_hours_win=None,
    s6_excl_months=None,
) -> pd.DataFrame:

    _width_th = bb_width_th  if bb_width_th  is not None else BB_WIDTH_TH
    _adx_th   = adx_th       if adx_th       is not None else ADX_TH
    _tp       = tp           if tp           is not None else TP
    _sl       = sl           if sl           is not None else SL
    _mh       = max_hold     if max_hold     is not None else MAX_HOLD
    _wd       = set(s6_weekdays)    if s6_weekdays    is not None else set(S6_WEEKDAYS)
    _hdst     = set(s6_hours_dst)   if s6_hours_dst   is not None else set(S6_HOURS_DST)
    _hwin     = set(s6_hours_win)   if s6_hours_win   is not None else set(S6_HOURS_WIN)
    _excl     = set(s6_excl_months) if s6_excl_months is not None else set(S6_EXCL_MONTHS)

    arr_open  = df["open"].values
    arr_high  = df["high"].values
    arr_low   = df["low"].values
    arr_close = df["close"].values
    arr_upper = df["bb_upper"].values
    arr_lower = df["bb_lower"].values
    arr_width = df["bb_width"].values
    arr_adx   = df["adx"].values

    dts     = pd.to_datetime(df["datetime"])
    dts_ns  = dts.values.astype("int64")
    arr_wd  = df["trading_weekday"].values
    arr_mo  = dts.dt.month.values
    arr_hr  = dts.dt.hour.values
    arr_min = dts.dt.minute.values
    arr_hm  = arr_hr * 100 + arr_min
    arr_pwd = dts.dt.weekday.values

    dst_mask, cpi_mask = build_masks(dts_ns, cpi_df)
    n    = len(df)
    rows = []

    for i in range(BB_PERIOD, n - 1):
        if np.isnan(arr_upper[i]) or np.isnan(arr_lower[i]) or np.isnan(arr_width[i]):
            continue

        hr  = (arr_hr[i] * 60 + arr_min[i] - 5) // 60 % 24
        mo  = arr_mo[i]
        wd  = arr_wd[i]
        dst = dst_mask[i]

        if wd not in _wd or mo in _excl:
            continue
        if hr not in (_hdst if dst else _hwin):
            continue
        if cpi_mask[i]:
            continue
        if np.isnan(arr_adx[i]) or arr_adx[i] >= _adx_th:
            continue

        low_vol = (_width_th == 0.0) or (arr_width[i] < _width_th)
        if not low_vol:
            continue

        side = None
        if arr_close[i] <= arr_lower[i]:
            side = "long"
        elif arr_close[i] >= arr_upper[i]:
            side = "short"

        if side is None:
            continue

        ep  = float(arr_open[i + 1])
        pnl, _, rtype = _exec(
            arr_high, arr_low, arr_close, ep, i + 1, side,
            _tp, _sl, _mh, n, arr_hm=arr_hm, arr_pwd=arr_pwd
        )
        pnl -= COMMISSION_PT
        rows.append({
            "system":         "⑥",
            "side":           side,
            "signal_dt":      dts.iloc[i],
            "signal_year":    int(dts.iloc[i].year),
            "signal_month":   mo,
            "signal_weekday": wd,
            "signal_hour":    hr,
            "pnl_pt":         round(pnl, 1),
            "pnl_yen":        int(round(pnl * PT_TO_YEN)),
            "result":         rtype,
        })

    return pd.DataFrame(rows) if rows else pd.DataFrame()


def _exec(arr_high, arr_low, arr_close, ep, entry_i, side, tp, sl, max_hold, n,
          arr_hm=None, arr_pwd=None):
    GAP_BOUNDS = frozenset({1540, 555})
    for j in range(entry_i, min(entry_i + max_hold, n)):
        if arr_hm is not None and arr_hm[j] in GAP_BOUNDS:
            cl = arr_close[j]
            pnl = float(cl - ep) if side == "long" else float(ep - cl)
            return pnl, j, "TIME"
        hi = arr_high[j]; lo = arr_low[j]
        if side == "long":
            tp_hit = hi >= ep + tp
            sl_hit = lo <= ep - sl
            if tp_hit and sl_hit: return -sl, j, "SL"
            if tp_hit:            return  tp, j, "TP"
            if sl_hit:            return -sl, j, "SL"
        else:
            tp_hit = lo <= ep - tp
            sl_hit = hi >= ep + sl
            if tp_hit and sl_hit: return -sl, j, "SL"
            if tp_hit:            return  tp, j, "TP"
            if sl_hit:            return -sl, j, "SL"
        if arr_hm is not None and arr_pwd is not None:
            if arr_pwd[j] == 0 and arr_hm[j] == 600:
                cl = arr_close[j]
                pnl = float(cl - ep) if side == "long" else float(ep - cl)
                return pnl, j, "WEEKEND"
    exit_i = min(entry_i + max_hold - 1, n - 1)
    cl = arr_close[exit_i]
    pnl = float(cl - ep) if side == "long" else float(ep - cl)
    return pnl, exit_i, "TIME"


# =========================
# 集計ユーティリティ
# =========================
def calc_summary(df: pd.DataFrame) -> dict:
    if len(df) == 0:
        return {"n": 0, "win_rate": 0.0, "pnl_yen": 0, "ev": 0.0, "pf": 0.0}
    pnl  = df["pnl_pt"].values.astype(float)
    wins = pnl[pnl > 0].sum()
    loss = abs(pnl[pnl < 0].sum())
    n    = len(pnl)
    return {
        "n":        n,
        "win_rate": float((pnl > 0).mean() * 100),
        "pnl_yen":  int(df["pnl_yen"].sum()),
        "ev":       float(pnl.sum() / n),
        "pf":       float(wins / loss) if loss > 0 else float("inf"),
    }


def pf_s(v):
    return "  inf" if v == float("inf") else f"{v:.3f}"


def sim_monthly_dd(trades: pd.DataFrame, dd_limit: int) -> dict:
    if len(trades) == 0:
        return {"active": pd.DataFrame(), "skipped": 0, "months_triggered": 0}
    df = trades.sort_values("signal_dt").copy()
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
        "active":           df[df["keep"]].drop(columns=["ym", "keep"]),
        "skipped":          int((~df["keep"]).sum()),
        "months_triggered": len(triggered),
    }


SEP = "=" * 78


# =========================
# main
# =========================
def main():
    df  = add_indicators(load_data())
    cpi = load_cpi()

    # ── 1. ADX閾値スキャン（メイン：低ボラフィルター効果確認）──
    print(f"\n{SEP}")
    print("  1. ADX閾値スキャン（bb_width_th=0.0、時間帯フィルターなし、TP200/SL80）")
    print(SEP)
    print(f"\n  {'adx_th':>8}  {'買いN':>7} {'買いPF':>7}  {'売りN':>7} {'売りPF':>7}  "
          f"{'合N':>7} {'合PF':>7}  {'損益(円)':>13}")
    print("  " + "-" * 80)
    for adx in [999, 40, 35, 30, 25, 20, 15, 10]:
        t = run_backtest(df, cpi, bb_width_th=0.0, adx_th=float(adx))
        if t.empty:
            print(f"  {adx:>8}  (トレードなし)"); continue
        tl = t[t["side"] == "long"]; ts = t[t["side"] == "short"]
        sl_ = calc_summary(tl); ss_ = calc_summary(ts); sa_ = calc_summary(t)
        lbl = "（無効）" if adx == 999 else f"< {adx:>2}"
        print(f"  {lbl:>8}  {sl_['n']:>7} {pf_s(sl_['pf']):>7}  {ss_['n']:>7} {pf_s(ss_['pf']):>7}  "
              f"{sa_['n']:>7} {pf_s(sa_['pf']):>7}  {sa_['pnl_yen']:>+13,}")

    # ── 2. ADX × BB幅 組み合わせスキャン ──
    print(f"\n{SEP}")
    print("  2. ADX × BB幅 組み合わせスキャン（TP200/SL80）")
    print(SEP)
    print(f"\n  {'adx_th':>8} {'bb_w_th':>8}  {'合N':>7} {'合PF':>7}  {'損益(円)':>13}")
    print("  " + "-" * 52)
    for adx in [999, 25, 20]:
        for bw in [0.0, 0.025, 0.030]:
            t = run_backtest(df, cpi, adx_th=float(adx), bb_width_th=bw)
            s = calc_summary(t)
            a_lbl = "（無効）" if adx == 999 else f"< {adx:>2}"
            b_lbl = "（無効）" if bw  == 0.0  else f"{bw:.3f}"
            print(f"  {a_lbl:>8} {b_lbl:>8}  {s['n']:>7} {pf_s(s['pf']):>7}  {s['pnl_yen']:>+13,}")

    # ── 3. TP/SLスキャン（ADX_TH固定）──
    print(f"\n{SEP}")
    print(f"  3. TP/SLスキャン（adx_th={ADX_TH}、bb_width_th={BB_WIDTH_TH}）")
    print(SEP)
    print(f"\n  {'TP':>5} {'SL':>5} {'max_hold':>9}  {'合N':>7} {'勝率%':>6} {'損益(円)':>13} {'PF':>7}")
    print("  " + "-" * 60)
    for tp_ in [100, 150, 200, 250, 300]:
        for sl_ in [50, 80, 100]:
            for mh_ in [4, 6, 8]:
                t = run_backtest(df, cpi, tp=tp_, sl=sl_, max_hold=mh_)
                s = calc_summary(t)
                if s["n"] == 0:
                    continue
                print(f"  {tp_:>5} {sl_:>5} {mh_:>9}  {s['n']:>7} {s['win_rate']:>5.1f}% "
                      f"{s['pnl_yen']:>+13,} {pf_s(s['pf']):>7}")

    # ── 以降は確定パラメータで実行 ──
    trades = run_backtest(df, cpi)
    if trades.empty:
        print("トレードなし"); return

    tl = trades[trades["side"] == "long"]
    ts = trades[trades["side"] == "short"]

    print(f"\n{SEP}")
    print(f"  4. 全体成績（adx_th={ADX_TH} bb_width_th={BB_WIDTH_TH} TP={TP} SL={SL} max_hold={MAX_HOLD}）")
    print(SEP)
    for lbl, t in [("⑥ 買い", tl), ("⑥ 売り", ts), ("合算", trades)]:
        s = calc_summary(t)
        print(f"  {lbl:6}  件数:{s['n']:>6}  勝率:{s['win_rate']:>5.1f}%  "
              f"損益:{s['pnl_yen']:>+10,}円  EV:{s['ev']:>+6.2f}  PF:{pf_s(s['pf'])}")

    # ── 4. DD適用後 ──
    res  = sim_monthly_dd(trades, DD_LIMIT)
    act  = res["active"]
    al   = act[act["side"] == "long"]
    as_  = act[act["side"] == "short"]
    print(f"\n{SEP}")
    print(f"  5. 月次DD {DD_LIMIT:,}円適用後")
    print(SEP)
    for lbl, t in [("⑥ 買い", al), ("⑥ 売り", as_), ("合算", act)]:
        s = calc_summary(t)
        print(f"  {lbl:6}  件数:{s['n']:>6}  勝率:{s['win_rate']:>5.1f}%  "
              f"損益:{s['pnl_yen']:>+10,}円  PF:{pf_s(s['pf'])}")
    print(f"  スキップ: {res['skipped']}件  DD発動月: {res['months_triggered']}ヶ月")

    # ── 5. 年別成績 ──
    print(f"\n{SEP}")
    print(f"  6. 年別成績（DD適用後）")
    print(SEP)
    print(f"  {'年':>6}  {'買いN':>6} {'買いPF':>7}  {'売りN':>6} {'売りPF':>7}  "
          f"{'合N':>6} {'合PF':>7}  {'損益(円)':>13}")
    print("  " + "-" * 78)
    for yr in sorted(act["signal_year"].unique()):
        yl = al[al["signal_year"] == yr]
        ys = as_[as_["signal_year"] == yr]
        ya = act[act["signal_year"] == yr]
        sl_ = calc_summary(yl); ss_ = calc_summary(ys); sa_ = calc_summary(ya)
        print(f"  {yr:>6}  {sl_['n']:>6} {pf_s(sl_['pf']):>7}  {ss_['n']:>6} {pf_s(ss_['pf']):>7}  "
              f"{sa_['n']:>6} {pf_s(sa_['pf']):>7}  {sa_['pnl_yen']:>+13,}")

    # ── 6. 時間帯別成績（DD適用後） ──
    print(f"\n{SEP}")
    print(f"  7. 時間帯別成績（DD適用後、合算）")
    print(SEP)
    print(f"  {'時':>4}  {'件数':>6}  {'勝率%':>6}  {'損益(円)':>13}  {'PF':>7}")
    print("  " + "-" * 46)
    for hr_ in range(24):
        th = act[act["signal_hour"] == hr_]
        if len(th) == 0:
            continue
        s = calc_summary(th)
        print(f"  {hr_:>2}時  {s['n']:>6}  {s['win_rate']:>5.1f}%  {s['pnl_yen']:>+13,}  {pf_s(s['pf'])}")

    # ── 7. 曜日別成績（DD適用後） ──
    WD = {0: "月", 1: "火", 2: "水", 3: "木", 4: "金"}
    print(f"\n{SEP}")
    print(f"  8. 曜日別成績（DD適用後）")
    print(SEP)
    print(f"  {'曜日':>4}  {'件数':>6}  {'勝率%':>6}  {'損益(円)':>13}  {'PF':>7}")
    print("  " + "-" * 46)
    for wd_ in range(5):
        tw = act[act["signal_weekday"] == wd_]
        if len(tw) == 0:
            continue
        s = calc_summary(tw)
        print(f"  {WD[wd_]:>4}  {s['n']:>6}  {s['win_rate']:>5.1f}%  {s['pnl_yen']:>+13,}  {pf_s(s['pf'])}")

    # ── 8. 月別成績（DD適用後） ──
    print(f"\n{SEP}")
    print(f"  9. 月別成績（DD適用後）")
    print(SEP)
    print(f"  {'月':>4}  {'件数':>6}  {'勝率%':>6}  {'損益(円)':>13}  {'PF':>7}")
    print("  " + "-" * 46)
    for mo_ in range(1, 13):
        tm = act[act["signal_month"] == mo_]
        if len(tm) == 0:
            continue
        s = calc_summary(tm)
        print(f"  {mo_:>2}月  {s['n']:>6}  {s['win_rate']:>5.1f}%  {s['pnl_yen']:>+13,}  {pf_s(s['pf'])}")

    # ── 9. 月次損失上限スキャン ──
    print(f"\n{SEP}")
    print("  10. 月次DD上限スキャン（系統⑥単体）")
    print(SEP)
    print(f"\n  {'制限(円)':>12}  {'件数':>6}  {'スキップ':>8}  {'発動月':>6}  {'勝率%':>6}  {'損益(円)':>13}  {'PF':>7}")
    print("  " + "-" * 72)
    for lim in [None, -10_000, -20_000, -30_000, -40_000, -50_000]:
        if lim is None:
            s = calc_summary(trades)
            print(f"  {'制限なし':>12}  {s['n']:>6}  {'':>8}  {'':>6}  "
                  f"{s['win_rate']:>5.1f}%  {s['pnl_yen']:>+13,}  {pf_s(s['pf'])}")
        else:
            r = sim_monthly_dd(trades, lim)
            s = calc_summary(r["active"])
            print(f"  {lim:>+12,}  {s['n']:>6}  {r['skipped']:>8}  {r['months_triggered']:>6}  "
                  f"{s['win_rate']:>5.1f}%  {s['pnl_yen']:>+13,}  {pf_s(s['pf'])}")

    # ── 11. 2月特化分析 ──
    print(f"\n{SEP}")
    print("  11. 2月特化分析（2月補填シミュレーション）")
    print(SEP)

    FEB_EXCL   = tuple(m for m in range(1, 13) if m != 2)
    FEB_STRONG = {4, 8, 9, 16, 17, 18, 19, 22}

    t_feb_raw = run_backtest(df, cpi, adx_th=999.0, bb_width_th=0.0,
                              s6_excl_months=FEB_EXCL)
    t_feb_adx = run_backtest(df, cpi, adx_th=25.0,  bb_width_th=0.0,
                              s6_excl_months=FEB_EXCL)
    t_feb_def = trades[trades["signal_month"] == 2].copy()

    print(f"\n  ■ 2月 フィルター別成績（DD未適用）:")
    print(f"  {'条件':^28}  {'時間帯':^8}  {'件数':>5}  {'PF':>7}  {'損益(円)':>10}")
    print("  " + "-" * 64)
    for cond, t_base in [("フィルターなし",       t_feb_raw),
                          ("ADX<25",            t_feb_adx),
                          ("ADX<25 + BB<0.03",  t_feb_def)]:
        for hlbl, hrs in [("全時間", None), ("強時間帯", FEB_STRONG)]:
            tf = t_base[t_base["signal_hour"].isin(hrs)] if hrs is not None else t_base
            s  = calc_summary(tf)
            print(f"  {cond:^28}  {hlbl:^8}  {s['n']:>5}  {pf_s(s['pf']):>7}  {s['pnl_yen']:>+10,}")

    # ①③+④⑤ 2月実績（合算DD後 -30,000円近辺で打ち切り）
    FEB_REF = {2024: -30_062, 2025: -30_560, 2026: -30_062}

    print(f"\n  ■ ⑥追加時 年別2月推定（ADX<25、強時間帯のみ）:")
    t_best = t_feb_adx[t_feb_adx["signal_hour"].isin(FEB_STRONG)]
    print(f"  {'年':>5}  {'⑥件数':>6}  {'⑥PF':>7}  {'⑥損益':>8}  {'①③④⑤(実績)':>12}  {'合算推定':>10}  {'DD回避':>6}")
    print("  " + "-" * 68)
    for yr in [2024, 2025, 2026]:
        yf = t_best[t_best["signal_year"] == yr] if not t_best.empty else pd.DataFrame()
        s6 = calc_summary(yf)
        ref = FEB_REF.get(yr, 0)
        total = ref + s6["pnl_yen"]
        ok = "○" if total > DD_LIMIT else "×"
        print(f"  {yr:>5}  {s6['n']:>6}  {pf_s(s6['pf']):>7}  {s6['pnl_yen']:>+8,}  {ref:>+12,}  {total:>+10,}  {ok:>6}")
    print(f"\n  ※ ①③+④⑤実績はDD打ち切り後の値（実際の損失はより大きい可能性あり）")
    print(f"  ※ 合算推定は単純加算（実際のDD複合シミュは backtest_combined_all.py で確認要）")

    # ── 12. 時間帯 × 月除外 フィルタースキャン（件数・EV重視）──
    print(f"\n{SEP}")
    print("  12. 時間帯 × 月除外 フィルタースキャン（DD-30,000円適用後、EV順）")
    print(SEP)
    print(f"  ★ = 件数5000+かつEV≥10pt  ◎ = 件数3000+かつEV≥5pt")
    print(f"\n  {'時間帯':^30} {'月除外':^14}  {'件数':>6} {'EV(pt)':>7} {'勝率%':>6} {'PF':>7} {'損益(円)':>12}")
    print("  " + "-" * 88)

    _H_COMBOS = [
        ("全時間",                   None),
        ("強6h{3,9,15,16,18,23}",   frozenset({3, 9, 15, 16, 18, 23})),
        ("強7h+4時",                 frozenset({3, 4, 9, 15, 16, 18, 23})),
        ("強8h+14時",                frozenset({3, 4, 9, 14, 15, 16, 18, 23})),
        ("強9h+10時",                frozenset({3, 4, 9, 10, 14, 15, 16, 18, 23})),
        ("強10h+22時",               frozenset({3, 4, 9, 10, 14, 15, 16, 18, 22, 23})),
        ("強11h+6時",                frozenset({3, 4, 6, 9, 10, 14, 15, 16, 18, 22, 23})),
        ("強12h+0時",                frozenset({0, 3, 4, 6, 9, 10, 14, 15, 16, 18, 22, 23})),
    ]
    _M_COMBOS = [
        ("除外なし",        ()),
        ("1・3月除外",      (1, 3)),
        ("1・2・3月除外",   (1, 2, 3)),
    ]

    _scan12 = []
    for _hl, _hrs in _H_COMBOS:
        for _ml, _mex in _M_COMBOS:
            _hset = _hrs if _hrs is not None else set(range(24))
            _t12 = run_backtest(df, cpi,
                                s6_hours_dst=_hset, s6_hours_win=_hset,
                                s6_excl_months=_mex)
            _r12 = sim_monthly_dd(_t12, DD_LIMIT)
            _s12 = calc_summary(_r12["active"])
            _scan12.append((_hl, _ml, _s12["n"], _s12["ev"], _s12["win_rate"], _s12["pf"], _s12["pnl_yen"]))

    _scan12.sort(key=lambda x: x[3], reverse=True)
    for _hl, _ml, _n12, _ev12, _wr12, _pf12, _pnl12 in _scan12:
        _flag = " ★" if _n12 >= 5000 and _ev12 >= 10 else (" ◎" if _n12 >= 3000 and _ev12 >= 5 else "")
        print(f"  {_hl:^30} {_ml:^14}  {_n12:>6} {_ev12:>+7.2f} {_wr12:>5.1f}% {pf_s(_pf12):>7} {_pnl12:>+12,}{_flag}")

    # ── 13. 金曜除外の影響確認（最良時間帯で）──
    print(f"\n{SEP}")
    print("  13. 金曜除外 × 時間帯フィルター（DD適用後）")
    print(SEP)
    print(f"\n  {'時間帯':^30} {'曜日':^8}  {'件数':>6} {'EV(pt)':>7} {'PF':>7} {'損益(円)':>12}")
    print("  " + "-" * 76)

    _WD_ALL  = (0, 1, 2, 3, 4)
    _WD_NOFR = (0, 1, 2, 3)
    for _hl, _hrs in _H_COMBOS[:6]:
        _hset = _hrs if _hrs is not None else set(range(24))
        for _wl, _wds in [("月〜金", _WD_ALL), ("月〜木(金除外)", _WD_NOFR)]:
            _t13 = run_backtest(df, cpi,
                                s6_hours_dst=_hset, s6_hours_win=_hset,
                                s6_weekdays=_wds,
                                s6_excl_months=(1, 3))
            _r13 = sim_monthly_dd(_t13, DD_LIMIT)
            _s13 = calc_summary(_r13["active"])
            print(f"  {_hl:^30} {_wl:^8}  {_s13['n']:>6} {_s13['ev']:>+7.2f} {pf_s(_s13['pf']):>7} {_s13['pnl_yen']:>+12,}")


if __name__ == "__main__":
    out_path = "bt_result_system6.txt"
    with open(out_path, "w", encoding="utf-8") as f:
        sys.stdout = f
        main()
    sys.stdout = sys.__stdout__
    print(f"出力完了: {out_path}")
    import subprocess
    subprocess.Popen(["code", out_path])
