import requests

# kabuステーションAPIに接続テスト
BASE_URL = "http://localhost:18080/kabusapi"

def get_token():
    url = f"{BASE_URL}/token"
    
    data = {
        "APIPassword": "sakimono35oku"
    }
    
    response = requests.post(url, json=data)
    
    if response.status_code == 200:
        token = response.json()["Token"]
        print(f"✅ 接続成功！")
        print(f"Token: {token}")
        return token
    else:
        print(f"❌ 接続失敗")
        print(f"エラー: {response.text}")
        return None

if __name__ == "__main__":
    get_token()