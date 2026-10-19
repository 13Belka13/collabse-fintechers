# Creates an INCOMING_FUNGIBLE_TX subscription on base-sepolia for the seller, confirms, deletes it.
import os, httpx
from dotenv import load_dotenv

load_dotenv(override=True)
h = {"x-api-key": os.environ["TATUM_API_KEY"].strip()}
body = {"type": "INCOMING_FUNGIBLE_TX", "attr": {"address": os.environ["SELLER_ADDR"], "chain": "base-sepolia",
        "url": "https://example.com/agent-pay-webhook-check"}}
r = httpx.post("https://api.tatum.io/v4/subscription", headers=h, json=body, timeout=30)
print("create:", r.status_code, r.text[:200])
if r.status_code in (200, 201):
    sid = r.json().get("id")
    lst = httpx.get("https://api.tatum.io/v4/subscription?pageSize=10", headers=h, timeout=30).json()
    print("listed:", any(s.get("id") == sid for s in (lst if isinstance(lst, list) else lst.get("data", []))))
    d = httpx.delete(f"https://api.tatum.io/v4/subscription/{sid}", headers=h, timeout=30)
    print("delete:", d.status_code)
