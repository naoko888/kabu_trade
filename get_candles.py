import requests
import pandas as pd

BASE_URL = "http://localhost:18080/kabusapi"
SYMBOL = "161060023"  # 日経225マイクロ先物 26/06

def get_token():
    url = f"{BASE_URL}/token"
    data = {"APIPassword": "sakimono35oku"}
    response = requests.post(url, json=data)
    return response.json()["Token"]

def get_candles(token):
    """30分足データを取得"""
    url = f"{BASE_URL}/prices/symbol"
    headers = {"X-API-KEY": token}
    params = {
        "symbol": SYMBOL,
        "exchange": "2",
        "type": "4",      # 30分足
        "count": "100",   # 100本分
    }
    
    response = requests.get(url, headers=headers, params=params)
    
    if response.status_code == 200:
        data = response.json()
        prices = data.get("kline", [])
        
        df = pd.DataFrame(prices)
        df.columns = ["time", "open", "high", "low", "close", "volume"]
        df["time"] = pd.to_datetime(df["time"])
        
        print(f"✅ {len(df)}本のローソク足取得成功！")
        print(df.tail(5))
        return df
    else:
        print(f"❌ 失敗: {response.text}")
        return None

if __name__ == "__main__":
    token = get_token()
    get_candles(token)
