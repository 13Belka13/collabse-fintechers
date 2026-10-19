# Tatum notifications for the seller wallets (SPEC §11): set the HMAC secret, then one INCOMING_FUNGIBLE_TX
# subscription per seller wallet on base-sepolia pointing at <PUBLIC_BASE_URL>/webhooks/tatum (bank/webhooks.py).
# Tatum refuses localhost / .local / .test, so going live needs a public tunnel; without one the bank polls the RPC.
#   python -m scripts.tatum_subscribe --dry-run            # print the requests (no secret, no key, no network)
#   PUBLIC_BASE_URL=https://<tunnel> python -m scripts.tatum_subscribe   # live: HMAC secret + subscriptions
#   python -m scripts.tatum_subscribe list | chains
#   python -m scripts.tatum_subscribe delete --id <id> | --all            # --all: ours (seller wallets, base-sepolia)
# Keys come from .env (TATUM_API_KEY, TATUM_HMAC_SECRET); neither is ever printed.
# Builds on prep/spikes/tatum_sub_check.py (pre-hackathon, disclosed): the same v4 subscription call.
import argparse
import json
import os
import sys
from urllib.parse import urlparse

import httpx

from bank import config

API = "https://api.tatum.io/v4"
CHAIN = "base-sepolia"
KIND = "INCOMING_FUNGIBLE_TX"


def seller_wallets() -> dict[str, str]:
    """Seller wallets as scripts/seed.py loads them ({ID}_ADDR in .env overrides the fixture)."""
    data = json.loads((config.ROOT / "fixtures" / "companies.json").read_text())
    return {c["id"]: os.getenv(f"{c['id'].upper()}_ADDR", "").strip() or c["wallet_address"]
            for c in data["companies"] if c["role"] == "seller"}


def webhook_url(base: str) -> str:
    """The public URL Tatum calls; refuses what Tatum refuses (localhost, .local, .test) and plain http."""
    u = urlparse(base or "")
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not host or host in ("localhost", "0.0.0.0") or host.startswith("127.") \
            or host.endswith((".local", ".test", ".localhost")):
        raise SystemExit(f"PUBLIC_BASE_URL must be a public https URL (a tunnel), got {base!r}")
    return base.rstrip("/") + "/webhooks/tatum"


def create_requests(url: str, secret: str, wallets: dict[str, str]) -> list[tuple[str, str, dict]]:
    reqs = [("PUT", "/subscription", {"hmacSecret": secret})]
    for addr in wallets.values():
        reqs.append(("POST", "/subscription", {"type": KIND, "attr": {"chain": CHAIN, "address": addr, "url": url},
                                               "templateId": "enriched", "finality": "confirmed"}))
    return reqs


def show(method: str, path: str, body=None) -> str:
    if isinstance(body, dict) and "hmacSecret" in body:
        body = {**body, "hmacSecret": "***"}
    return f"{method} {API}{path}" + (f" {json.dumps(body)}" if body is not None else "")


def client() -> httpx.Client:
    if not config.TATUM_API_KEY:
        raise SystemExit("TODO(credential): set TATUM_API_KEY in .env")
    return httpx.Client(base_url=API, headers={"x-api-key": config.TATUM_API_KEY}, timeout=30)


def items(resp: httpx.Response) -> list[dict]:
    resp.raise_for_status()
    body = resp.json()
    return body if isinstance(body, list) else body.get("data") or []


def ours(subs: list[dict], wallets: dict[str, str]) -> list[dict]:
    addrs = {a.lower() for a in wallets.values()}
    return [s for s in subs if s.get("type") == KIND and (s.get("attr") or {}).get("chain") == CHAIN
            and str((s.get("attr") or {}).get("address", "")).lower() in addrs]


def create(dry: bool) -> int:
    wallets = seller_wallets()
    if dry:
        url = webhook_url(config.PUBLIC_BASE_URL) if config.PUBLIC_BASE_URL else "https://<PUBLIC_BASE_URL>/webhooks/tatum"
        print("DRY RUN, nothing is sent. Live: PUBLIC_BASE_URL=https://<tunnel> python -m scripts.tatum_subscribe")
        for m, p, b in create_requests(url, "<TATUM_HMAC_SECRET>", wallets):
            print(" ", show(m, p, b))
        return 0
    if not config.PUBLIC_BASE_URL:
        raise SystemExit("refusing to subscribe without PUBLIC_BASE_URL: Tatum needs a public https URL (start a "
                         "tunnel, set PUBLIC_BASE_URL in .env); without it the bank polls the RPC instead")
    url = webhook_url(config.PUBLIC_BASE_URL)
    if not config.TATUM_HMAC_SECRET:
        raise SystemExit("TODO(credential): set TATUM_HMAC_SECRET in .env (the bank rejects unsigned webhooks)")
    with client() as c:
        existing = {(s["attr"]["address"].lower(), s["attr"].get("url"))
                    for s in ours(items(c.get("/subscription", params={"pageSize": 50})), wallets)}
        for m, p, b in create_requests(url, config.TATUM_HMAC_SECRET, wallets):
            if m == "POST" and (b["attr"]["address"].lower(), url) in existing:
                print(f"  exists: {b['attr']['address']} → {url}")
                continue
            r = c.request(m, p, json=b)
            print(f"  {r.status_code} {show(m, p, b)}" + (f" → {r.text[:200]}" if m == "POST" else ""))
            r.raise_for_status()
    return 0


def list_subs(dry: bool) -> int:
    if dry:
        print(" ", show("GET", "/subscription?pageSize=50"))
        return 0
    with client() as c:
        for s in items(c.get("/subscription", params={"pageSize": 50})):
            print(json.dumps(s))
    return 0


def chains(dry: bool) -> int:
    if dry:
        print(" ", show("GET", "/subscription/chains"))
        return 0
    with client() as c:
        r = c.get("/subscription/chains")
        r.raise_for_status()
        print(f"{CHAIN} supported: {CHAIN in r.text}")
    return 0


def delete(dry: bool, sub_id: str | None, every: bool) -> int:
    if not sub_id and not every:
        raise SystemExit("delete needs --id <id> or --all")
    if dry:
        print(" ", show("DELETE", f"/subscription/{sub_id}") if sub_id else
              show("GET", "/subscription?pageSize=50") + f"\n  then DELETE each {KIND} on {CHAIN} for the seller wallets")
        return 0
    with client() as c:
        ids = [sub_id] if sub_id else [s["id"] for s in ours(items(c.get("/subscription", params={"pageSize": 50})),
                                                             seller_wallets())]
        for i in ids:
            r = c.delete(f"/subscription/{i}")
            print(f"  {r.status_code} DELETE {API}/subscription/{i}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Tatum INCOMING_FUNGIBLE_TX subscriptions for the seller wallets")
    ap.add_argument("action", nargs="?", default="create", choices=["create", "list", "chains", "delete"])
    ap.add_argument("--dry-run", action="store_true", help="print the requests; send nothing")
    ap.add_argument("--id", help="delete: the subscription id")
    ap.add_argument("--all", action="store_true", help="delete: every subscription of ours")
    a = ap.parse_args(argv)
    if a.action == "create":
        return create(a.dry_run)
    if a.action == "list":
        return list_subs(a.dry_run)
    if a.action == "chains":
        return chains(a.dry_run)
    return delete(a.dry_run, a.id, a.all)


if __name__ == "__main__":
    sys.exit(main())
