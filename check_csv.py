import pandas as pd
import requests
from pathlib import Path
from datetime import datetime, timedelta
import pytz

MICRO_CSV = Path(r"C:\kabu_trade\micro_5min.csv")
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1499602285536477205/VvoI_2kXK_mau-Zxp2grYY5tlsQ-CXtg3DEuANFZOkmTfVyLTo1-nO8YxUFtwkunf74P"
CHECK_BARS = 300
JST = pytz.timezone("Asia/Tokyo")

def send_discord(msg):
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": msg})
    except Exception as e:
        print(f"[WARN] Discord通知エラー: {e}")

def is_trading_time(hhmm):
    if 845 <= hhmm < 1540:
        return True
    if hhmm >= 1700:
        return True
    if hhmm < 555:
        return True
    return False

def check():
    if not MICRO_CSV.exists():
        send_discord("⚠️ [CSV-CHECK] micro_5min.csv が存在しません")
        return

    try:
        df = pd.read_csv(MICRO_CSV, parse_dates=["datetime"])
        if df["datetime"].dt.tz is not None:
            df["datetime"] = df["datetime"].dt.tz_convert("Asia/Tokyo").dt.tz_localize(None)
        df = df.sort_values("datetime").reset_index(drop=True)
    except Exception as e:
        send_discord(f"⚠️ [CSV-CHECK] CSV読み込みエラー: {e}")
        return

    # 現在時刻（10分前まで）
    now = datetime.now(JST).replace(tzinfo=None)
    cutoff = now - timedelta(minutes=10)

    # 過去に遡って営業時間内の期待足を300本分生成
    expected = []
    t = cutoff.replace(second=0, microsecond=0)
    t = t - timedelta(minutes=t.minute % 5)  # 5分切り捨て

    while len(expected) < CHECK_BARS:
        hhmm = t.hour * 100 + t.minute
        if is_trading_time(hhmm):
            expected.append(t)
        t -= timedelta(minutes=5)

    expected = sorted(expected)

    # CSVの既存タイムスタンプをセットに
    existing = set(df["datetime"].dt.floor("5min").tolist())

    # 欠損チェック
    missing = [t for t in expected if pd.Timestamp(t) not in existing]

    if missing:
        # 連続した欠損をまとめて表示
        ranges = []
        start = missing[0]
        end = missing[0]
        for m in missing[1:]:
            if (m - end).total_seconds() <= 300:
                end = m
            else:
                ranges.append((start, end))
                start = m
                end = m
        ranges.append((start, end))

        lines = [f"{s.strftime('%m/%d %H:%M')}〜{e.strftime('%H:%M')}" for s, e in ranges]
        msg = f"⚠️ [CSV-CHECK] 直近{CHECK_BARS}本に欠損あり:\n" + "\n".join(lines[:5])
        send_discord(msg)
        print(msg)
    else:
        print(f"[OK] 直近{CHECK_BARS}本の欠損なし")

if __name__ == "__main__":
    check()