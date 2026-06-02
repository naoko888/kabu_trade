"""
zh_utils.py
汎用ユーティリティ。副作用のない純粋関数 + ログ・Discord通知。
他のモジュールはここから log / send_discord / _board_price 等を import する。
依存: zh_config のみ
"""
import requests
from datetime import datetime

from zh_config import DISCORD_WEBHOOK_URL, LOG_DIR, JST, HOLIDAYS


def floor_5min(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0, minute=(dt.minute // 5) * 5)


def is_holiday(d) -> bool:
    return (d.year, d.month, d.day) in HOLIDAYS


def _sess_exchange(hhmm: int) -> int:
    """現在時刻(hhmm)からセッションの市場コードを返す。23=日中 / 24=夜間"""
    return 24 if (hhmm >= 1540 or hhmm < 600) else 23


def _board_price(board) -> float | None:
    """板情報から現在値を取得する。CurrentPrice → (Bid+Ask)/2 → 片方の順で試みる"""
    if board is None:
        return None
    p = board.get("CurrentPrice")
    if p is not None:
        return float(p)
    bid = board.get("BidPrice")
    ask = board.get("AskPrice")
    if bid and ask:
        return (float(bid) + float(ask)) / 2
    return float(bid or ask) if (bid or ask) else None


def safe_json(res) -> dict:
    try:
        return res.json()
    except Exception:
        return {"raw": res.text}


def send_discord(msg: str) -> None:
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": msg}, timeout=5)
    except Exception as e:
        print(f"[WARN] Discord通知エラー: {e}")


def log(msg: str) -> None:
    ts = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts[11:]}] {msg}")
    try:
        LOG_DIR.mkdir(exist_ok=True)
        with open(LOG_DIR / "ZAIHOU_debug.log", "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass
    if "[ERR]" in msg:
        send_discord(f"🚨 {msg}")
    elif "[WARN]" in msg:
        send_discord(f"⚠️ {msg}")
