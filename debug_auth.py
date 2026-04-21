# debug_auth.py
import urllib.request
import urllib.parse
import urllib.error
import json

with open("credentials.json") as f:
    client_cfg = json.load(f)

print("Top-level keys:", list(client_cfg.keys()))

cfg = client_cfg.get("installed") or client_cfg.get("web")
client_id = cfg["client_id"]
print("client_id:", client_id)

data = urllib.parse.urlencode({
    "client_id": client_id,
    "scope": "https://www.googleapis.com/auth/calendar https://www.googleapis.com/auth/tasks",
}).encode()

try:
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/device/code",
        data=data,
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        print("SUCCESS:", json.loads(resp.read()))
except urllib.error.HTTPError as e:
    print("HTTP Error:", e.code)
    print("Response body:", e.read().decode())