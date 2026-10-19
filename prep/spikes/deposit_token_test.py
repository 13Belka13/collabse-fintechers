# Spike: demo tokenized deposit (tCZK) on an in-memory chain (no ETH, no network).
# Checks: bank mint, allowlist, EIP-3009 transfer by signature, replay protection,
# and that a signature produced by the official x402 Python SDK settles on this token.
import asyncio, os, secrets, time
import solcx
from eth_account import Account
from web3 import Web3, EthereumTesterProvider
from x402 import x402Client
from x402.mechanisms.evm.exact import ExactEvmScheme
from x402.mechanisms.evm.signers import EthAccountSigner
from x402.schemas import PaymentRequired

src = open(os.path.join("contracts", "DepositToken.sol")).read()
out = solcx.compile_source(src, output_values=["abi", "bin"], solc_version="0.8.26")
abi, bytecode = next(v for k, v in out.items() if k.endswith(":DepositToken")).values()

w3 = Web3(EthereumTesterProvider())
bank, relayer = w3.eth.accounts[0], w3.eth.accounts[1]  # relayer = whoever submits (facilitator / bank)
buyer, seller, outsider = Account.create(), Account.create(), Account.create()
chain = w3.eth.chain_id

h = w3.eth.contract(abi=abi, bytecode=bytecode).constructor("Demo Bank tCZK").transact({"from": bank})
token = w3.eth.contract(address=w3.eth.get_transaction_receipt(h).contractAddress, abi=abi)
tx = lambda call, sender=bank: w3.eth.wait_for_transaction_receipt(call.transact({"from": sender}))
for a in (buyer.address, seller.address):
    tx(token.functions.setAllowlisted(a, True))
tx(token.functions.mint(buyer.address, 100_000))  # 1000.00 tCZK
bal = lambda a: token.functions.balanceOf(a).call() / 100
print("minted: buyer", bal(buyer.address), "tCZK")

TYPES = {"TransferWithAuthorization": [
    {"name": "from", "type": "address"}, {"name": "to", "type": "address"}, {"name": "value", "type": "uint256"},
    {"name": "validAfter", "type": "uint256"}, {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"}]}
DOMAIN = {"name": "Demo Bank tCZK", "version": "1", "chainId": chain, "verifyingContract": token.address}


def authorize(to, value, nonce=None):
    msg = {"from": buyer.address, "to": to, "value": value, "validAfter": 0,
           "validBefore": w3.eth.get_block("latest").timestamp + 3600, "nonce": nonce or secrets.token_bytes(32)}
    sig = Account.sign_typed_data(buyer.key, domain_data=DOMAIN, message_types=TYPES, message_data=msg).signature
    return msg, sig


def submit(msg, sig):
    return tx(token.functions.transferWithAuthorization(
        msg["from"], msg["to"], msg["value"], msg["validAfter"], msg["validBefore"], msg["nonce"], sig), relayer)


def expect_revert(label, fn):
    try:
        fn()
        print("FAIL (no revert):", label)
    except Exception as e:
        print("OK reverted:", label, "->", str(e).split("(")[0][:60])


msg, sig = authorize(seller.address, 25_000)
submit(msg, sig)
print("signed transfer 250.00: buyer", bal(buyer.address), "seller", bal(seller.address))
expect_revert("replay of the same authorization", lambda: submit(msg, sig))
m2, s2 = authorize(outsider.address, 1_000)
expect_revert("transfer to a non-client", lambda: submit(m2, s2))
expect_revert("mint by non-bank", lambda: tx(token.functions.mint(seller.address, 1), relayer))
m3, s3 = authorize(seller.address, 1_000)
m3_tampered = {**m3, "value": 50_000}
expect_revert("tampered amount", lambda: submit(m3_tampered, s3))


async def sdk_payment():
    # Same signature the x402 SDK makes for USDC/EURC, aimed at our token
    client = x402Client()
    client.set_spend_controls({"allowed_assets": True})
    client.register("eip155:*", ExactEvmScheme(signer=EthAccountSigner(buyer)))
    required = PaymentRequired.model_validate({"x402Version": 2, "resource": {"url": "http://local/data"}, "accepts": [{
        "scheme": "exact", "network": f"eip155:{chain}", "asset": token.address, "amount": "500",
        "payTo": seller.address, "maxTimeoutSeconds": 300, "extra": {"name": "Demo Bank tCZK", "version": "1"}}]})
    return await client.create_payment_payload(required)


payload = asyncio.run(sdk_payment())
p = payload.payload
auth = p["authorization"]
before = bal(seller.address)
tx(token.functions.transferWithAuthorization(
    auth["from"], auth["to"], int(auth["value"]), int(auth["validAfter"]), int(auth["validBefore"]),
    bytes.fromhex(auth["nonce"].removeprefix("0x")), bytes.fromhex(p["signature"].removeprefix("0x"))), relayer)
print("x402 SDK signature settled on tCZK: seller", before, "->", bal(seller.address))
