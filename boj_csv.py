import pandas as pd
from datetime import datetime, timedelta

# 日銀 発表日リスト
BOJ_ANNOUNCEMENTS = [
    "2023-01-18","2023-03-10","2023-04-28","2023-06-16",
    "2023-07-28","2023-09-22","2023-10-31","2023-12-19",
    "2024-01-23","2024-03-19","2024-04-26","2024-06-14",
    "2024-07-31","2024-09-20","2024-10-31","2024-12-19",
    "2025-01-24","2025-03-19","2025-05-01","2025-06-17",
    "2025-07-31","2025-09-19","2025-10-30","2025-12-19",
    "2026-01-23","2026-03-19","2026-04-28","2026-06-16",
    "2026-07-31","2026-09-18","2026-10-30","2026-12-18",
]

# 除外時間設定（11:30～13:30）
EXCLUDE_BEFORE = 30   # 分
EXCLUDE_AFTER  = 120  # 分

rows = []

for d in BOJ_ANNOUNCEMENTS:
    base = datetime.strptime(d + " 12:00", "%Y-%m-%d %H:%M")

    start = base - timedelta(minutes=EXCLUDE_BEFORE)
    end   = base + timedelta(minutes=EXCLUDE_AFTER)

    rows.append({
        "event": "BOJ",
        "start": start.strftime("%Y-%m-%d %H:%M:%S"),
        "end": end.strftime("%Y-%m-%d %H:%M:%S")
    })

boj_df = pd.DataFrame(rows)

# 既存CSVがある場合は読み込み
try:
    existing = pd.read_csv("economic_calendar.csv")
    merged = pd.concat([existing, boj_df], ignore_index=True)
except FileNotFoundError:
    merged = boj_df

# 保存
merged.to_csv("economic_calendar.csv", index=False)

print("追加完了")