# Spike: x402 buyer paying the local seller from BUYER_KEY.
import asyncio, base64, json, os
from dotenv import load_dotenv
from eth_account import Account
from x402 import x402Client
from x402.http.clients import x402HttpxClient
from x402.mechanisms.evm.exact import ExactEvmScheme
from x402.mechanisms.evm.signers import EthAccountSigner

load_dotenv()


async def main():
    client = x402Client()
    # EURC is not a default asset: allow it explicitly with a per-payment cap of 1 EURC (atomic units)
    client.set_spend_controls({"allowed_assets": [
        {"network": "eip155:84532", "asset": "0x808456652fdb597867f38412077A9182bf77359F",
         "max_amount_per_payment": "1000000"}]})
    client.register("eip155:*", ExactEvmScheme(signer=EthAccountSigner(Account.from_key(os.environ["BUYER_KEY"]))))
    async with x402HttpxClient(client, timeout=60) as http:
        r = await http.get("http://127.0.0.1:8402/data")
        print(r.status_code, r.text[:300])
        pr = r.headers.get("payment-response")
        if pr:
            print(json.dumps(json.loads(base64.b64decode(pr)), indent=1))


asyncio.run(main())
