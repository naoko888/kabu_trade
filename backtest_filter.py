"""
勝ちトレード深掘り分析 & 最適戦略探索
- どのパターン・時間・曜日・相場環境で勝てているか
- 1日2000円に近づくための条件を数値で探す
"""

import yfinance as yf
import pandas as pd
import numpy as np
import pytz
import itertools

JST = pytz.timezone("Asia/Tokyo")

SYMBOL   = "1570.T"
NAME     = "日経レバ"
PERIOD   = "60d"
INTERVAL = "5m"

SL = 100
TP = 200
ZONE_PCT      = 0.005
GAP_THRESHOLD = 0.02
HIGE_WINDOW   = 3
MAX_PER_DAY   = 2
DAILY_LOSS_LIMIT = -500

# =====================================
# データ取得・指標
# =====================================
def get_5min():
    df = yf.download(SYMBOL, period=PERIOD, interval=INTERVAL, progress=False)
    df.columns = ["close","high","low","open","volume"]
    df = df[["open","high","low","close","volume"]]
    return df.dropna()

def get_daily():
    df = yf.download(SYMBOL, period="180d", interval="1d", progress=False)
    df.columns = ["close","high","low","open","volume"]
    df = df[["open","high","low","close","volume"]]
    return df.dropna()

def add_indicators(df):
    # MACD
    ema_fast      = df["close"].ewm(span=5,  adjust=False).mean()
    ema_slow      = df["close"].ewm(span=20, adjust=False).mean()
    df["macd"]    = ema_fast - ema_slow
    df["macd_sig"]= df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"]= df["macd"] - df["macd_sig"]
    # MA
    df["ma5"]  = df["close"].rolling(5).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma50"] = df["close"].rolling(50).mean()
    # ボラティリティ（ATR的な代替）
    df["hl_range"] = df["high"] - df["low"]
    df["atr5"]     = df["hl_range"].rolling(5).mean()
    # 出来高移動平均
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["vol_ratio"]= df["volume"] / df["vol_ma20"]  # 1以上=出来高多い
    return df

def add_daily_info(df):
    df["prev_high"]  = df["high"].shift(1)
    df["prev_low"]   = df["low"].shift(1)
    df["prev_close"] = df["close"].shift(1)
    return df

def build_daily_map(df):
    m = {}
    for d in df.index:
        key = pd.Timestamp(d.date())
        m[key] = {
            "prev_high":  float(df.loc[d,"prev_high"])  if not pd.isna(df.loc[d,"prev_high"])  else None,
            "prev_low":   float(df.loc[d,"prev_low"])   if not pd.isna(df.loc[d,"prev_low"])   else None,
            "prev_close": float(df.loc[d,"prev_close"]) if not pd.isna(df.loc[d,"prev_close"]) else None,
            "open":       float(df.loc[d,"open"])        if not pd.isna(df.loc[d,"open"])        else None,
        }
    return m

def get_daily_info(daily_map, dt):
    dj  = dt.astimezone(JST) if dt.tzinfo else dt
    key = pd.Timestamp(dj.date())
    av  = [d for d in daily_map if d <= key]
    return daily_map[max(av)] if av else None

# =====================================
# ゾーン・フィルター
# =====================================
def get_zone_ref(daily_info, day_high, day_low):
    if daily_info is None:
        return day_high, day_low
    prev_close = daily_info.get("prev_close")
    today_open = daily_info.get("open")
    use_today  = True
    if prev_close and today_open:
        gap = abs(today_open - prev_close) / prev_close
        if gap < GAP_THRESHOLD:
            use_today = False
    if use_today:
        return day_high, day_low
    return (daily_info.get("prev_high") or day_high,
            daily_info.get("prev_low")  or day_low)

def is_high_zone(price, ref_high):
    return abs(price - ref_high) / ref_high <= ZONE_PCT

def is_low_zone(price, ref_low):
    return abs(price - ref_low) / ref_low <= ZONE_PCT

def hige_filter_short_ok(df, i):
    if i < HIGE_WINDOW: return True
    win = df.iloc[i-HIGE_WINDOW:i]
    ng = sum(1 for _, r in win.iterrows()
             if (min(r["open"],r["close"]) - r["low"]) >
                (r["high"] - max(r["open"],r["close"])) and
                (min(r["open"],r["close"]) - r["low"]) >
                abs(r["close"]-r["open"]) * 0.5)
    return ng < HIGE_WINDOW // 2 + 1

def hige_filter_long_ok(df, i):
    if i < HIGE_WINDOW: return True
    win = df.iloc[i-HIGE_WINDOW:i]
    ng = sum(1 for _, r in win.iterrows()
             if (r["high"] - max(r["open"],r["close"])) >
                (min(r["open"],r["close"]) - r["low"]) and
                (r["high"] - max(r["open"],r["close"])) >
                abs(r["close"]-r["open"]) * 0.5)
    return ng < HIGE_WINDOW // 2 + 1

def time_status(dt):
    dj = dt.astimezone(JST) if dt.tzinfo else dt
    h, m = dj.hour, dj.minute
    t = (h, m)
    if (9,0) <= t <= (9,14):   return "no_entry"
    if (12,25) <= t <= (12,29): return "morning_close"
    if (12,30) <= t <= (12,44): return "lunch"
    if (12,45) <= t <= (12,59): return "no_entry"
    if (9,14) < t < (13,30):   return "trading"
    if (13,30) <= t < (14,0):  return "close_only"
    if t >= (14,0):             return "force_close"
    return "closed"

# =====================================
# エントリー判定（詳細ラベル付き）
# =====================================
def check_short(df, i, ref_high, ref_low):
    if i < 6: return False, ""
    row   = df.iloc[i]
    price = row["close"]
    if not hige_filter_short_ok(df, i): return False, "下髭NG"

    # パターンA：高値圏
    if is_high_zone(price, ref_high):
        win4   = df.iloc[i-4:i]
        highs  = win4["high"].values
        max_p  = highs[0]; upd = 0
        for h in highs[1:]:
            if h > max_p: upd += 1
            max_p = max(max_p, h)
        if upd > 1: return False, "高値更新2本超"
        r3 = df.iloc[i-3:i]["high"].values
        cut = r3[-1] < r3[0]
        side = abs(r3[-1]-r3[0])/r3[0] < 0.003
        if not (cut or side): return False, "パターン不一致"

        # 追加情報
        vol_ratio  = row["vol_ratio"] if not pd.isna(row["vol_ratio"]) else 1.0
        macd_h     = row["macd_hist"] if not pd.isna(row["macd_hist"]) else 0
        above_ma20 = price > row["ma20"] if not pd.isna(row["ma20"]) else False
        pattern    = "切り下げ" if cut else "ヨコヨコ"
        return True, f"A_{pattern}|vol_r={vol_ratio:.2f}|macd={'下' if macd_h<0 else '上'}|{'MA上' if above_ma20 else 'MA下'}"

    # パターンB：安値圏戻り売り
    if is_low_zone(price, ref_low):
        if i < 4: return False, ""
        win4     = df.iloc[i-4:i]
        bull_vol = win4[win4["close"]>=win4["open"]]["volume"].sum()
        bear_vol = win4[win4["close"]< win4["open"]]["volume"].sum()
        if bull_vol >= bear_vol: return False, "陽線出来高優勢"
        cb = 0
        for j in range(len(win4)-1,-1,-1):
            if win4.iloc[j]["close"] >= win4.iloc[j]["open"]: cb += 1
            else: break
        if cb >= 2: return False, "追随陽線2本"

        # 追加情報
        vol_diff   = (bear_vol - bull_vol) / (bear_vol + bull_vol + 1)
        vol_ratio  = row["vol_ratio"] if not pd.isna(row["vol_ratio"]) else 1.0
        macd_h     = row["macd_hist"] if not pd.isna(row["macd_hist"]) else 0
        return True, f"B_戻り売り|vol_優位={vol_diff:.2f}|vol_r={vol_ratio:.2f}|macd={'下' if macd_h<0 else '上'}"

    return False, ""

def check_long(df, i, ref_high, ref_low):
    if i < 6: return False, ""
    row   = df.iloc[i]
    price = row["close"]
    if is_high_zone(price, ref_high): return False, "高値圏禁止"
    if not hige_filter_long_ok(df, i): return False, "上髭NG"

    if is_low_zone(price, ref_low):
        win4 = df.iloc[i-4:i]
        lows = win4["low"].values
        bd_count = 0
        base_low = lows[0]
        for j, lo in enumerate(lows[1:],1):
            if lo < base_low * 0.998:
                body_lo   = min(win4.iloc[j]["open"], win4.iloc[j]["close"])
                lower_wick= body_lo - lo
                body      = abs(win4.iloc[j]["close"] - win4.iloc[j]["open"])
                if lower_wick > body * 0.5: bd_count += 1
                else: return False, "安値割れ戻りなし"
        if bd_count > 1: return False, "安値割れ2本超"
        rec_high = win4["high"].max()
        if price < rec_high * 0.998: return False, "直近高値更新なし"

        vol_ratio = row["vol_ratio"] if not pd.isna(row["vol_ratio"]) else 1.0
        macd_h    = row["macd_hist"] if not pd.isna(row["macd_hist"]) else 0
        below_ma20= price < row["ma20"] if not pd.isna(row["ma20"]) else False
        return True, f"L_安値堅|vol_r={vol_ratio:.2f}|macd={'上' if macd_h>0 else '下'}|{'MA下' if below_ma20 else 'MA上'}"

    return False, ""

def close_pos(pos, row, trades, result, ct, pnl=None):
    if pnl is None:
        pnl = (pos["entry"]-row["close"]) if pos["side"]=="short" \
              else (row["close"]-pos["entry"])
    pos.update({"exit_time":ct,"exit_price":row["close"],"pnl":pnl,"result":result})
    trades.append(dict(pos))

# =====================================
# バックテスト（全情報記録）
# =====================================
def backtest_full(df5, daily_map):
    trades    = []
    pos       = None
    day_counts= {}
    day_pnl   = {}
    day_ohlc  = {}

    for i in range(1, len(df5)):
        row = df5.iloc[i]
        ct  = df5.index[i]
        st  = time_status(ct)
        dj  = ct.astimezone(JST)
        day = dj.date()

        if day not in day_ohlc:
            day_ohlc[day] = {"high":row["high"],"low":row["low"]}
        else:
            day_ohlc[day]["high"] = max(day_ohlc[day]["high"],row["high"])
            day_ohlc[day]["low"]  = min(day_ohlc[day]["low"], row["low"])

        if day not in day_counts: day_counts[day] = {"long":0,"short":0}
        if day not in day_pnl:   day_pnl[day]    = 0

        if pos and st == "morning_close":
            close_pos(pos,row,trades,"前場終了",ct)
            day_pnl[day] += trades[-1]["pnl"]; pos=None; continue
        if pos and st == "force_close":
            close_pos(pos,row,trades,"大引け",ct)
            day_pnl[day] += trades[-1]["pnl"]; pos=None; continue
        if st in ["no_entry","lunch","morning_close","force_close","closed"]:
            continue

        if pos:
            entry = pos["entry"]; side = pos["side"]
            pnl   = (entry-row["close"]) if side=="short" else (row["close"]-entry)
            if pnl <= -SL:
                close_pos(pos,row,trades,"損切り",ct,-SL)
                day_pnl[day]+=trades[-1]["pnl"]; pos=None; continue
            if pnl >= TP:
                close_pos(pos,row,trades,"利確",ct,TP)
                day_pnl[day]+=trades[-1]["pnl"]; pos=None; continue
            if (i-pos["bar"]) >= 12:
                close_pos(pos,row,trades,"時間",ct,pnl)
                day_pnl[day]+=trades[-1]["pnl"]; pos=None; continue
            continue

        if st != "trading": continue
        if day_pnl.get(day,0) <= DAILY_LOSS_LIMIT: continue
        if dj.weekday() in [1,2]: continue
        di2 = get_daily_info(daily_map, ct)
        if di2:
            pc=di2.get('prev_close'); to=di2.get('open')
            if pc and to and (to-pc)/pc > 0.005: continue
        vr=row['vol_ratio'] if str(row['vol_ratio'])!='nan' else 1.0
        if 0.8<=vr<=1.5: continue
        at=row['atr5'] if str(row['atr5'])!='nan' else 150
        if at<100: continue

        di       = get_daily_info(daily_map, ct)
        d_high   = day_ohlc[day]["high"]
        d_low    = day_ohlc[day]["low"]
        ref_high, ref_low = get_zone_ref(di, d_high, d_low)

        # ショート
        if day_counts[day]["short"] < MAX_PER_DAY:
            ok, reason = check_short(df5, i, ref_high, ref_low)
            if ok:
                # 相場環境を記録
                atr       = row["atr5"] if not pd.isna(row["atr5"]) else 0
                gap_pct   = 0
                if di and di.get("prev_close") and di.get("open"):
                    gap_pct = (di["open"]-di["prev_close"])/di["prev_close"]*100
                pos = {
                    "side":"short","entry_time":ct,"entry":row["close"],
                    "bar":i,"reason":reason,
                    "hour":dj.hour,"weekday":dj.strftime("%a"),
                    "atr":atr,"gap_pct":gap_pct,
                    "vol_ratio":row["vol_ratio"] if not pd.isna(row["vol_ratio"]) else 1.0,
                    "ma_align": "上" if (not pd.isna(row["ma5"]) and not pd.isna(row["ma20"]) and row["ma5"]<row["ma20"]) else "下",
                }
                day_counts[day]["short"] += 1
                continue

        # ロング
        if day_counts[day]["long"] < MAX_PER_DAY:
            ok, reason = check_long(df5, i, ref_high, ref_low)
            if ok:
                atr     = row["atr5"] if not pd.isna(row["atr5"]) else 0
                gap_pct = 0
                if di and di.get("prev_close") and di.get("open"):
                    gap_pct = (di["open"]-di["prev_close"])/di["prev_close"]*100
                pos = {
                    "side":"long","entry_time":ct,"entry":row["close"],
                    "bar":i,"reason":reason,
                    "hour":dj.hour,"weekday":dj.strftime("%a"),
                    "atr":atr,"gap_pct":gap_pct,
                    "vol_ratio":row["vol_ratio"] if not pd.isna(row["vol_ratio"]) else 1.0,
                    "ma_align": "下" if (not pd.isna(row["ma5"]) and not pd.isna(row["ma20"]) and row["ma5"]>row["ma20"]) else "上",
                }
                day_counts[day]["long"] += 1

    return trades

# =====================================
# 深掘り分析
# =====================================
def deep_analysis(trades):
    df = pd.DataFrame(trades)
    df["win"]     = df["pnl"] > 0
    df["pattern"] = df["reason"].str.extract(r"^([AB]_\S+|L_\S+)")
    df["macd_dir"]= df["reason"].str.extract(r"macd=(上|下)")
    df["vol_r"]   = df["reason"].str.extract(r"vol_r=(\d+\.\d+)").astype(float)

    total = len(df)
    wr    = df["win"].mean()*100
    tpnl  = df["pnl"].sum()

    print(f"\n{'='*60}")
    print(f"総トレード:{total}  勝率:{wr:.1f}%  総損益:{tpnl:.0f}円")
    print(f"{'='*60}")

    # ── パターン別 ──
    print("\n【パターン別勝率・損益】")
    pg = df.groupby("pattern").agg(
        回数=("pnl","count"),
        勝率=("win", lambda x: f"{x.mean()*100:.0f}%"),
        損益=("pnl","sum"),
        平均=("pnl","mean"),
    ).sort_values("損益", ascending=False)
    print(pg.to_string())

    # ── 時間帯別 ──
    print("\n【時間帯別勝率・損益】")
    hg = df.groupby("hour").agg(
        回数=("pnl","count"),
        勝率=("win", lambda x: f"{x.mean()*100:.0f}%"),
        損益=("pnl","sum"),
        平均=("pnl","mean"),
    ).sort_values("損益", ascending=False)
    print(hg.to_string())

    # ── 曜日別 ──
    print("\n【曜日別勝率・損益】")
    order = ["Mon","Tue","Wed","Thu","Fri"]
    wg = df.groupby("weekday").agg(
        回数=("pnl","count"),
        勝率=("win", lambda x: f"{x.mean()*100:.0f}%"),
        損益=("pnl","sum"),
        平均=("pnl","mean"),
    ).reindex([w for w in order if w in df["weekday"].unique()])
    print(wg.to_string())

    # ── MACD方向別 ──
    print("\n【MACDヒスト方向別】")
    mg = df.groupby("macd_dir").agg(
        回数=("pnl","count"),
        勝率=("win", lambda x: f"{x.mean()*100:.0f}%"),
        損益=("pnl","sum"),
        平均=("pnl","mean"),
    )
    print(mg.to_string())

    # ── 出来高比率別（高い/普通/低い）──
    print("\n【出来高比率別（vol_ratio）】")
    df["vol_tier"] = pd.cut(df["vol_r"],
                            bins=[0, 0.8, 1.5, 99],
                            labels=["低(<0.8)","普通(0.8-1.5)","高(>1.5)"])
    vg = df.groupby("vol_tier", observed=True).agg(
        回数=("pnl","count"),
        勝率=("win", lambda x: f"{x.mean()*100:.0f}%"),
        損益=("pnl","sum"),
        平均=("pnl","mean"),
    )
    print(vg.to_string())

    # ── MA並び別 ──
    print("\n【MA並び別（ma5 vs ma20）】")
    ag = df.groupby("ma_align").agg(
        回数=("pnl","count"),
        勝率=("win", lambda x: f"{x.mean()*100:.0f}%"),
        損益=("pnl","sum"),
        平均=("pnl","mean"),
    )
    print(ag.to_string())

    # ── ATR（ボラ）別 ──
    print("\n【ボラティリティ別（ATR5）】")
    df["atr_tier"] = pd.cut(df["atr"],
                            bins=[0, 100, 200, 99999],
                            labels=["低(<100)","中(100-200)","高(>200)"])
    atg = df.groupby("atr_tier", observed=True).agg(
        回数=("pnl","count"),
        勝率=("win", lambda x: f"{x.mean()*100:.0f}%"),
        損益=("pnl","sum"),
        平均=("pnl","mean"),
    )
    print(atg.to_string())

    # ── ギャップ別 ──
    print("\n【ギャップ方向別】")
    df["gap_dir"] = df["gap_pct"].apply(
        lambda x: "ギャップアップ" if x > 0.5 else ("ギャップダウン" if x < -0.5 else "フラット"))
    gg = df.groupby("gap_dir").agg(
        回数=("pnl","count"),
        勝率=("win", lambda x: f"{x.mean()*100:.0f}%"),
        損益=("pnl","sum"),
        平均=("pnl","mean"),
    )
    print(gg.to_string())

    # ── 最強条件の組み合わせ ──
    print(f"\n{'='*60}")
    print("【最強条件の組み合わせ TOP10】")
    print(f"{'='*60}")
    df["cond"] = (
        df["pattern"].astype(str) + "|" +
        df["hour"].astype(str) + "時|" +
        df["weekday"] + "|" +
        "MACD" + df["macd_dir"].astype(str)
    )
    cg = df.groupby("cond").agg(
        回数=("pnl","count"),
        勝率=("win", lambda x: round(x.mean()*100,0)),
        損益=("pnl","sum"),
        平均=("pnl", lambda x: round(x.mean(),0)),
    ).query("回数 >= 2").sort_values("損益", ascending=False).head(10)
    print(cg.to_string())

    # ── 1日2000円シミュレーション ──
    print(f"\n{'='*60}")
    print("【1日2000円達成シミュレーション】")
    print(f"{'='*60}")
    # 勝率・1回平均損益から必要トレード数を逆算
    for wr_target in [45, 50, 55, 60]:
        for avg_pnl in [150, 180, 200]:
            # 期待値 = wr*avg_win + (1-wr)*(-SL)
            wr_d = wr_target/100
            ev   = wr_d * avg_pnl + (1-wr_d) * (-SL)
            if ev > 0:
                n = 2000 / ev
                print(f"  勝率{wr_target}% 平均利益{avg_pnl}pt → 期待値{ev:.0f}pt/回 → {n:.1f}回/日必要")

    # ── 実際の上位日を分析 ──
    print(f"\n【1日500円以上の日のパターン】")
    df["date"] = pd.to_datetime(df["entry_time"]).dt.tz_convert(JST).dt.date
    day_pnl_df = df.groupby("date")["pnl"].sum()
    good_days  = day_pnl_df[day_pnl_df >= 500].index
    if len(good_days):
        gd = df[df["date"].isin(good_days)]
        print(f"  好調日数: {len(good_days)}日")
        print(f"  好調日の勝率: {gd['win'].mean()*100:.0f}%")
        print(f"  好調日の平均損益: {gd['pnl'].mean():.0f}円/回")
        print(f"  好調日の平均トレード数: {len(gd)/len(good_days):.1f}回/日")
        print(f"  好調日のパターン分布:")
        print(gd["pattern"].value_counts().to_string())

    print(f"\n【1日-200円以下の日のパターン】")
    bad_days = day_pnl_df[day_pnl_df <= -200].index
    if len(bad_days):
        bd = df[df["date"].isin(bad_days)]
        print(f"  不調日数: {len(bad_days)}日")
        print(f"  不調日の勝率: {bd['win'].mean()*100:.0f}%")
        print(f"  不調日のパターン分布:")
        print(bd["pattern"].value_counts().to_string())

    return df

# =====================================
# メイン
# =====================================
if __name__ == "__main__":
    print("📥 データ取得中...")
    df5 = get_5min()
    dfd = get_daily()
    df5 = add_indicators(df5)
    dfd = add_daily_info(dfd)
    dmap= build_daily_map(dfd)

    print("🔍 バックテスト実行中...")
    trades = backtest_full(df5, dmap)

    print(f"\n✅ {len(trades)}トレード記録")
    deep_analysis(trades)
