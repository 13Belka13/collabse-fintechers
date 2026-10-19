# Route B on Base Sepolia: buyer signs an x402 `exact` payment in tCZK with the official SDK;
# the bank (acting as facilitator) screens it and settles it on-chain.
import asyncio, os
from dotenv import load_dotenv
from eth_account import Account
from web3 import Web3
from x402 import x402Client
from x402.mechanisms.evm.exact import ExactEvmScheme
from x402.mechanisms.evm.signers import EthAccountSigner
from x402.schemas import PaymentRequired

load_dotenv()
w3 = Web3(Web3.HTTPProvider(os.environ["RPC_URL"]))
bank, buyer = Account.from_key(os.environ["BANK_KEY"]), Account.from_key(os.environ["BUYER_KEY"])
TCZK, SELLER = os.environ["TCZK"], os.environ["SELLER_ADDR"]
abi = [{"name": "transferWithAuthorization", "type": "function", "stateMutability": "nonpayable", "outputs": [],
        "inputs": [{"type": "address"}, {"type": "address"}, {"type": "uint256"}, {"type": "uint256"},
                   {"type": "uint256"}, {"type": "bytes32"}, {"type": "bytes"}]},
       {"name": "allowlisted", "type": "function", "stateMutability": "view", "inputs": [{"type": "address"}], "outputs": [{"type": "bool"}]}]
token = w3.eth.contract(address=TCZK, abi=abi)


async def sign():
    client = x402Client()
    client.set_spend_controls({"allowed_assets": [{"network": "eip155:84532", "asset": TCZK, "max_amount_per_payment": "100000"}]})
    client.register("eip155:*", ExactEvmScheme(signer=EthAccountSigner(buyer)))
    req = PaymentRequired.model_validate({"x402Version": 2, "resource": {"url": "http://seller/orders/demo"}, "accepts": [{
        "scheme": "exact", "network": "eip155:84532", "asset": TCZK, "amount": "500", "payTo": SELLER,
        "maxTimeoutSeconds": 300, "extra": {"name": "Demo Bank tCZK", "version": "1"}}]})
    return (await client.create_payment_payload(req)).payload


p = asyncio.run(sign())
a = p["authorization"]
# bank screening (stand-in for the facilitator's on_before_verify hook)
if not token.functions.allowlisted(a["to"]).call():
    raise SystemExit("bank blocked: NOT_BANK_CLIENT")
tx = token.functions.transferWithAuthorization(
    a["from"], a["to"], int(a["value"]), int(a["validAfter"]), int(a["validBefore"]),
    bytes.fromhex(a["nonce"].removeprefix("0x")), bytes.fromhex(p["signature"].removeprefix("0x"))
).build_transaction({"from": bank.address, "nonce": w3.eth.get_transaction_count(bank.address, "pending"), "chainId": w3.eth.chain_id})
h = w3.eth.send_raw_transaction(bank.sign_transaction(tx).raw_transaction)
r = w3.eth.wait_for_transaction_receipt(h, timeout=180)
print("x402-signed 5.00 tCZK settled by bank:", "OK" if r.status == 1 else "FAILED",
      "https://sepolia.basescan.org/tx/0x" + h.hex().removeprefix("0x"))
