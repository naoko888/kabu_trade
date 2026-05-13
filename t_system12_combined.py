[1mdiff --git a/backtest_system123_combined.py b/backtest_system123_combined.py[m
[1mindex 56fa6bd..8a48f33 100644[m
[1m--- a/backtest_system123_combined.py[m
[1m+++ b/backtest_system123_combined.py[m
[36m@@ -35,7 +35,7 @@[m [mCPI_CSV     = Path(r"C:\kabu_trade\economic_calendar.csv")[m
 TP             = 240[m
 SL             = 60[m
 MAX_HOLD       = 120[m
[31m-TOUCH_PCT      = 0.005[m
[32m+[m[32mTOUCH_PCT      = 0.007[m
 COMMISSION_PT  = 2.2[m
 PT_TO_YEN      = 10[m
 [m
[36m@@ -91,6 +91,15 @@[m [mHOLIDAYS = {[m
 # =========================[m
 # データ読み込み[m
 # =========================[m
[32m+[m[32m# 夜間（17:00以降）は翌取引日扱い（XLSXの取引日基準に合わせる）[m
[32m+[m[32mfrom datetime import timedelta[m
[32m+[m[32mdef _to_trading_dt(dt):[m
[32m+[m[32m    if pd.isna(dt) or dt.hour < 17:[m
[32m+[m[32m        return dt[m
[32m+[m[32m    if dt.weekday() == 4:   # 金曜 → +3日（月曜）[m
[32m+[m[32m        return dt + timedelta(days=3)[m
[32m+[m[32m    return dt + timedelta(days=1)  # 月〜木 → +1日[m
[32m+[m
 def read_excel(path: Path) -> pd.DataFrame:[m
     df = pd.read_excel(path, sheet_name="5min", engine="openpyxl")[m
     df = df.rename(columns={[m
[36m@@ -102,12 +111,13 @@[m [mdef read_excel(path: Path) -> pd.DataFrame:[m
         df["date"].astype(str).str.strip() + " " + df["time"].astype(str).str.strip(),[m
         errors="coerce",[m
     )[m
[32m+[m[32m    df["datetime"] = df["datetime"].apply(_to_trading_dt)  # ←これだけ追加[m
[32m+[m
     for c in ["open", "high", "low", "close", "volume"]:[m
         df[c] = pd.to_numeric(df[c], errors="coerce")[m
     df = df.dropna(subset=["datetime", "open", "high", "low", "close"]).copy()[m
     return df[["datetime", "open", "high", "low", "close", "volume"]].sort_values("datetime")[m
 [m
[31m-[m
 def load_data() -> pd.DataFrame:[m
     dfs = [][m
     print("データ読み込み中...")[m
[36m@@ -120,22 +130,6 @@[m [mdef load_data() -> pd.DataFrame:[m
         print(f"  {fname}: {len(d)} 本")[m
         dfs.append(d)[m
 [m
[31m-    if MICRO_CSV.exists():[m
[31m-        try:[m
[31m-            dc = pd.read_csv(MICRO_CSV, index_col="datetime", parse_dates=True).reset_index()[m
[31m-            if dc["datetime"].dt.tz is not None:[m
[31m-                dc["datetime"] = dc["datetime"].dt.tz_convert("Asia/Tokyo").dt.tz_localize(None)[m
[31m-            for c in ["open", "high", "low", "close", "volume"]:[m
[31m-                if c in dc.columns:[m
[31m-                    dc[c] = pd.to_numeric(dc[c], errors="coerce")[m
[31m-            dc = (dc.dropna(subset=["datetime", "open", "high", "low", "close"])[m
[31m-                  [["datetime", "open", "high", "low", "close", "volume"]][m
[31m-                  .sort_values("datetime"))[m
[31m-            print(f"  micro_5min.csv: {len(dc)} 本")[m
[31m-            dfs.append(dc)[m
[31m-        except Exception as e:[m
[31m-            print(f"  micro_5min.csv 読み込み失敗: {e}")[m
[31m-[m
     if not dfs:[m
         raise FileNotFoundError("データファイルが見つかりません")[m
 [m
