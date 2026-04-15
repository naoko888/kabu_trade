import requests

BASE_URL = "http://localhost:18080/kabusapi"
SYMBOL = "161060023"  # 日経225マイクロ先物 26/06

def get_token():
    url = f"{BASE_URL}/token"
    data = {"APIPassword": "sakimono35oku"}
    response = requests.post(url, json=data)
    return response.json()["Token"]

def get_board(token):
    """板情報を取得"""
    url = f"{BASE_URL}/board/{SYMBOL}@2"
    headers = {"X-API-KEY": token}
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        data = response.json()
        print(f"✅ データ取得成功！")
        print(f"銘柄名: {data.get('SymbolName')}")
        print(f"現在値: {data.get('CurrentPrice')}")
        print(f"始値:   {data.get('OpeningPrice')}")
        print(f"高値:   {data.get('HighPrice')}")
        print(f"安値:   {data.get('LowPrice')}")
        print(f"出来高: {data.get('TradingVolume')}")
        return data
    else:
        print(f"❌ 失敗: {response.text}")
        return None

if __name__ == "__main__":
    token = get_token()
    get_board(token)
