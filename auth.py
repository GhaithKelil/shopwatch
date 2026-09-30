import base64, hashlib, json, os, secrets, sys, webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs, urlencode
import requests
from dotenv import load_dotenv

load_dotenv()
KEY = os.environ["ETSY_KEYSTRING"]
REDIRECT = "http://localhost:3003/callback"
SCOPES = "transactions_r shops_r profile_r"
if len(sys.argv) != 2:
    sys.exit("Usage: python auth.py <shop_name>")
shop_name = sys.argv[1].lower()  # must match a name in SHOP_NAMES

verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
challenge = base64.urlsafe_b64encode(
    hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
state = secrets.token_urlsafe(16)

url = "https://www.etsy.com/oauth/connect?" + urlencode({
    "response_type": "code", "client_id": KEY, "redirect_uri": REDIRECT,
    "scope": SCOPES, "state": state,
    "code_challenge": challenge, "code_challenge_method": "S256",
})

class Handler(BaseHTTPRequestHandler):
    code = None
    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        if q.get("state", [""])[0] == state:
            Handler.code = q.get("code", [""])[0]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Done. You can close this tab.")

webbrowser.open(url)
server = HTTPServer(("localhost", 3003), Handler)
while Handler.code is None:
    server.handle_request()

r = requests.post("https://api.etsy.com/v3/public/oauth/token", data={
    "grant_type": "authorization_code", "client_id": KEY,
    "redirect_uri": REDIRECT, "code": Handler.code, "code_verifier": verifier,
})
r.raise_for_status()
tokens_dir = os.environ.get("TOKENS_DIR", ".")
os.makedirs(tokens_dir, exist_ok=True)
with open(os.path.join(tokens_dir, f"tokens_{shop_name}.json"), "w") as f:
    json.dump(r.json(), f)
print("Saved tokens for", shop_name)