# Checks the Apify and Tatum credentials from .env without printing them.
import os, httpx
from dotenv import load_dotenv

load_dotenv(override=True)
apify, tatum = os.getenv("APIFY_TOKEN", "").strip(), os.getenv("TATUM_API_KEY", "").strip()
print("APIFY_TOKEN present:", bool(apify), "| TATUM_API_KEY present:", bool(tatum))

if apify:
    h = {"Authorization": f"Bearer {apify}"}
    me = httpx.get("https://api.apify.com/v2/users/me", headers=h, timeout=30)
    print("Apify /users/me:", me.status_code)
    if me.status_code == 200:
        d = me.json()["data"]
        print("  user:", d.get("username"), "| plan:", (d.get("plan") or {}).get("id"))
    lim = httpx.get("https://api.apify.com/v2/users/me/limits", headers=h, timeout=30)
    if lim.status_code == 200:
        d = lim.json()["data"]
        print("  monthly usage limit USD:", (d.get("limits") or {}).get("maxMonthlyUsageUsd"),
              "| used USD:", (d.get("current") or {}).get("monthlyUsageUsd"))

if tatum:
    r = httpx.post("https://base-sepolia.gateway.tatum.io", headers={"x-api-key": tatum},
                   json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}, timeout=30)
    print("Tatum RPC base-sepolia:", r.status_code, "block", int(r.json()["result"], 16) if r.status_code == 200 and "result" in r.json() else r.text[:200])
    s = httpx.get("https://api.tatum.io/v4/subscription?pageSize=10", headers={"x-api-key": tatum}, timeout=30)
    print("Tatum v4 subscriptions API:", s.status_code, s.text[:120] if s.status_code != 200 else "ok")
