import pandas as pd
import sys
sys.path.insert(0, r"C:\kabu_trade")
from micro_performance_summary import (
    load_micro_csv, add_indicators, load_cpi, build_bt_trades
)

csv_df = load_micro_csv()
cpi_df = load_cpi()
csv_df = add_indicators(csv_df)
bt_df = build_bt_trades(csv_df, cpi_df)

mask1 = (bt_df["entry_time"] >= "2026-04-27 16:50") & (bt_df["entry_time"] <= "2026-04-27 17:15")
mask2 = (bt_df["entry_time"] >= "2026-04-28 02:50") & (bt_df["entry_time"] <= "2026-04-28 03:15")
print(bt_df[mask1 | mask2][["system","side","entry_time","reason"]])