"""
kabuステーション シンボル調査ツール
"""
import requests

API_BASE = "http://localhost:18080/kabusapi"
API_PASSWORD = "sakimono35oku"

def get_token():
    res = requests.post(f"{API_BASE}/token", json={"APIPassword": API_PASSWORD}, timeout=10)
    if res.status_code == 200:
        return res.json()["Token"]
    print(f"トークン取得失敗: {res.text}")
    return None

def search(token, keyword):
    headers = {"Content-Type": "application/json", "X-API-KEY": token}
    body = {"keyword": keyword, "filterCondition": 4}  # 4=先物
    res = requests.post(f"{API_BASE}/symbolsearch", headers=headers, json=body, timeout=10)
    print(f"\n--- symbolsearch '{keyword}' → {res.status_code} ---")
    print(res.text[:2000])

def try_future(token, code, month):
    headers = {"Content-Type": "application/json", "X-API-KEY": token}
    url = f"{API_BASE}/symbolname/future?FutureCode={code}&DerivMonth={month}"
    res = requests.get(url, headers=headers, timeout=10)
    print(f"  {code} month={month} → {res.status_code} {res.text}")

token = get_token()
if not token:
    exit()

print(f"トークン: {token[:10]}...")

# symbolsearchで日経先物を探す
for kw in ["日経225マイクロ", "日経225", "NK225"]:
    search(token, kw)

# symbolname/futureで直接試す
print("\n--- symbolname/future 試行 ---")
codes = ["NK225micro", "NK225mini", "NK225", "225micro", "225mini"]
months = ["0", "202506", "202509", "202512", "202603", "202606"]
for code in codes:
    for month in months:
        try_future(token, code, month)
