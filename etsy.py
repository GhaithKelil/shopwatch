import json, os, requests
from dotenv import load_dotenv

load_dotenv()
KEY = os.environ.get("ETSY_KEYSTRING", "")
SECRET = os.environ.get("ETSY_SHARED_SECRET", "")
API_KEY = f"{KEY}:{SECRET}" if SECRET else KEY
BASE = "https://openapi.etsy.com/v3/application"

class Etsy:
    def __init__(self, shop_name):
        self.shop_name = shop_name
        self.path = os.path.join(os.environ.get("TOKENS_DIR", "."), f"tokens_{shop_name}.json")
        if not os.path.exists(self.path):
            raise FileNotFoundError(f"Tokens file not found: {self.path}")
        with open(self.path, "r", encoding="utf-8") as f:
            self.tokens = json.load(f)
        token_str = self.tokens.get("access_token", "")
        if self.tokens.get("user_id"):
            self.user_id = str(self.tokens["user_id"])
        elif "." in token_str:
            self.user_id = token_str.split(".")[0]
        else:
            self.user_id = ""
        self.shop_id = None

    def _refresh(self):
        if not KEY:
            raise ValueError("ETSY_KEYSTRING environment variable is required to refresh Etsy tokens.")
        r = requests.post("https://api.etsy.com/v3/public/oauth/token", data={
            "grant_type": "refresh_token", "client_id": KEY,
            "refresh_token": self.tokens["refresh_token"],
        })
        r.raise_for_status()
        self.tokens = r.json()
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.tokens, f)

    def get(self, path, params=None):
        if not KEY:
            raise ValueError("ETSY_KEYSTRING environment variable is required to make Etsy API requests.")
        for attempt in range(2):
            h = {"x-api-key": API_KEY,
                 "Authorization": f"Bearer {self.tokens['access_token']}"}
            r = requests.get(BASE + path, headers=h, params=params, timeout=30)
            if r.status_code == 401 and attempt == 0:
                self._refresh()
                continue
            r.raise_for_status()
            return r.json()

    def get_shop_id(self):
        if not self.shop_id:
            self.shop_id = self.get(f"/users/{self.user_id}/shops")["shop_id"]
        return self.shop_id

    def receipts(self, min_created=None):
        out, offset = [], 0
        while True:
            params = {"limit": 100, "offset": offset}
            if min_created:
                params["min_created"] = min_created
            data = self.get(f"/shops/{self.get_shop_id()}/receipts", params)
            results = data.get("results", [])
            out += results
            count = data.get("count", 0)
            offset += 100
            if offset >= count or not results:
                return out