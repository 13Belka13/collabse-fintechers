# L1 live: the buyer pays the seller directly over x402 on Base Sepolia (SPEC §4, §15 layer 1, scenario S01).
# Needs the bank (its /x402 facilitator; else the seller falls back to x402.org for EURC) and the seller running:
#   uvicorn bank.app:app --port 8000 &  uvicorn seller.app:app --port 8001 &
#   python -m scripts.demo_l1 [--asset EURC|tCZK] [--count 1]      (count items x 0,25 €; default 0,25 €)
# Prints the settlement from PAYMENT-RESPONSE, the tx hash with an explorer link and the balance change.
import argparse
import asyncio
import os
import sys
import time

import httpx
from eth_account import Account

from bank import chain, config, money, x402_buyer


def main() -> int:
    p = argparse.ArgumentParser(description="L1: pay the seller over x402 on Base Sepolia")
    p.add_argument("--asset", default="EURC", choices=["EURC", "tCZK"])
    p.add_argument("--count", type=int, default=1, help="items in the offer, 0,25 € each")
    p.add_argument("--seller-id", default="s-beta")
    args = p.parse_args()

    buyer_pk = os.getenv("ALFA_PK", "").strip()
    if not buyer_pk:
        print("TODO(credential): set ALFA_PK (testnet buyer key) in .env, fund it with EURC at faucet.circle.com")
        return 2
    if not config.ASSETS[args.asset]["address"]:
        print(f"TODO(credential): {args.asset} address missing in .env")
        return 2
    buyer = Account.from_key(buyer_pk).address

    health = httpx.get(f"{config.SELLER_URL}/health", timeout=5).json()
    if args.asset not in health["assets"]:
        print(f"seller does not quote {args.asset}: {health}")
        return 1
    offer = httpx.post(f"{config.SELLER_URL}/negotiate", timeout=10, json={
        "deal_id": f"l1-{int(time.time())}", "seller_id": args.seller_id,
        "task": {"criteria": {"count": args.count}}}).json()["offer"]
    amount = int(offer["prices"][args.asset])
    url = f"{config.SELLER_URL}/data/{offer['offer_id']}"
    print(f"offer {offer['offer_id']}: {offer['items_spec']} for {money.fmt(args.asset, amount)} -> {offer['pay_to']}")

    unpaid = httpx.get(url, timeout=10)
    print(f"unpaid GET {url} -> HTTP {unpaid.status_code} (must be 402)")
    if unpaid.status_code != 402:
        return 1

    before = {"buyer": chain.balance(args.asset, buyer), "seller": chain.balance(args.asset, offer["pay_to"])}
    t0 = time.perf_counter()
    r, settle = asyncio.run(x402_buyer.get(url, buyer_pk, args.asset))
    ms = (time.perf_counter() - t0) * 1000
    print(f"paid GET -> HTTP {r.status_code} in {ms:.0f} ms; PAYMENT-RESPONSE: {settle}")
    if r.status_code != 200 or not settle or not settle.get("transaction"):
        return 1
    print(f"items delivered: {len(r.json()['items'])}")
    print(f"tx {settle['transaction']}\n   {chain.tx_link(settle['transaction'])}")

    seller_after = chain.read_retry(lambda: chain.balance(args.asset, offer["pay_to"]),
                                    ok=lambda b: b >= before["seller"] + amount)
    buyer_after = chain.balance(args.asset, buyer)
    print(f"seller +{money.fmt(args.asset, seller_after - before['seller'])}, "
          f"buyer -{money.fmt(args.asset, before['buyer'] - buyer_after)} (read from chain)")
    return 0 if seller_after - before["seller"] == amount else 1


if __name__ == "__main__":
    sys.exit(main())
