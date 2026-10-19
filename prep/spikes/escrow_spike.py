# Spike: Coinbase AuthCaptureEscrow on Base Sepolia from Python.
# authorize (lock maxAmount) -> capture part to seller -> void rest back to buyer.
# Usage: python escrow_spike.py [check|simulate|run] [EURC|USDC|TCZK]
import os, secrets, sys, time
from dotenv import load_dotenv
from eth_account import Account
from web3 import Web3

load_dotenv()
w3 = Web3(Web3.HTTPProvider(os.environ["RPC_URL"]))
ESCROW = "0xf96815976523E00e65Be8f34cA5e64b4f41EB19c"
COLLECTOR = "0x8612dfdc421f80336cd14E8EF9cb1E765dB5ab88"
TOKENS = {"EURC": ("0x808456652fdb597867f38412077A9182bf77359F", "EURC", "2"),
          "USDC": ("0x036CbD53842c5426634e7929541eC2318f3dCF7e", "USDC", "2")}
if os.getenv("TCZK"):  # demo tokenized deposit, deployed by deploy_tczk.py
    TOKENS["TCZK"] = (os.environ["TCZK"], "Demo Bank tCZK", "1")
ZERO = "0x0000000000000000000000000000000000000000"

PI_COMPONENTS = [{"name": n, "type": t} for n, t in [
    ("operator", "address"), ("payer", "address"), ("receiver", "address"), ("token", "address"),
    ("maxAmount", "uint120"), ("preApprovalExpiry", "uint48"), ("authorizationExpiry", "uint48"),
    ("refundExpiry", "uint48"), ("minFeeBps", "uint16"), ("maxFeeBps", "uint16"),
    ("feeReceiver", "address"), ("salt", "uint256")]]
PI = {"name": "paymentInfo", "type": "tuple", "components": PI_COMPONENTS}


def fn(name, inputs, outputs=(), view=False):
    return {"name": name, "type": "function", "inputs": inputs, "outputs": list(outputs),
            "stateMutability": "view" if view else "nonpayable"}


ESCROW_ABI = [
    fn("getHash", [PI], [{"type": "bytes32"}], view=True),
    fn("paymentState", [{"type": "bytes32"}], [{"type": "bool"}, {"type": "uint120"}, {"type": "uint120"}], view=True),
    fn("authorize", [PI, {"name": "amount", "type": "uint256"}, {"name": "tokenCollector", "type": "address"},
                     {"name": "collectorData", "type": "bytes"}]),
    fn("capture", [PI, {"name": "amount", "type": "uint256"}, {"name": "feeAmount", "type": "uint256"},
                   {"name": "feeReceiver", "type": "address"}]),
    fn("void", [PI]),
]
COLLECTOR_ABI = [fn("authCaptureEscrow", [], [{"type": "address"}], view=True)]
BAL_ABI = [fn("balanceOf", [{"type": "address"}], [{"type": "uint256"}], view=True)]

escrow = w3.eth.contract(address=ESCROW, abi=ESCROW_ABI)
buyer = Account.from_key(os.environ["BUYER_KEY"])
operator = Account.from_key(os.environ["OPERATOR_KEY"])
seller = os.environ["SELLER_ADDR"]


def check():
    for label, a in (("escrow", ESCROW), ("collector", COLLECTOR)):
        print(label, "code bytes:", len(w3.eth.get_code(a)))
    linked = w3.eth.contract(address=COLLECTOR, abi=COLLECTOR_ABI).functions.authCaptureEscrow().call()
    print("collector -> escrow:", linked, "OK" if linked.lower() == ESCROW.lower() else "MISMATCH")


def balances(token):
    t = w3.eth.contract(address=token, abi=BAL_ABI)
    return {k: t.functions.balanceOf(a).call() for k, a in (("buyer", buyer.address), ("seller", seller))}


def send(call):
    tx = call.build_transaction({"from": operator.address, "nonce": w3.eth.get_transaction_count(operator.address, "pending"),
                                 "chainId": w3.eth.chain_id})
    h = w3.eth.send_raw_transaction(operator.sign_transaction(tx).raw_transaction)
    r = w3.eth.wait_for_transaction_receipt(h, timeout=120)
    print(f"  tx {'OK' if r.status == 1 else 'FAILED'}: https://sepolia.basescan.org/tx/0x{h.hex().removeprefix('0x')}")
    if r.status != 1:
        raise SystemExit("transaction reverted")


def run(symbol, simulate=False):
    token, name, version = TOKENS[symbol]
    now = int(time.time())
    max_amount, capture_amount = 20_000, 15_000  # 0.02 locked, 0.015 to seller, 0.005 back
    info = {"operator": operator.address, "payer": buyer.address, "receiver": seller, "token": token,
            "maxAmount": max_amount, "preApprovalExpiry": now + 600, "authorizationExpiry": now + 1800,
            "refundExpiry": now + 3600, "minFeeBps": 0, "maxFeeBps": 0, "feeReceiver": ZERO,
            "salt": secrets.randbits(256)}
    as_tuple = lambda d: tuple(d[c["name"]] for c in PI_COMPONENTS)

    # ERC-3009 nonce = escrow hash of PaymentInfo with payer zeroed (see ERC3009PaymentCollector)
    nonce = escrow.functions.getHash(as_tuple({**info, "payer": ZERO})).call()
    signed = Account.sign_typed_data(
        buyer.key,
        domain_data={"name": name, "version": version, "chainId": w3.eth.chain_id, "verifyingContract": token},
        message_types={"ReceiveWithAuthorization": [
            {"name": "from", "type": "address"}, {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"}, {"name": "validAfter", "type": "uint256"},
            {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"}]},
        message_data={"from": buyer.address, "to": COLLECTOR, "value": max_amount, "validAfter": 0,
                      "validBefore": info["preApprovalExpiry"], "nonce": nonce},
    )
    pi = as_tuple(info)
    if simulate:
        # eth_call: executes authorize against live state without sending a tx or paying gas
        escrow.functions.authorize(pi, max_amount, COLLECTOR, signed.signature).call({"from": operator.address})
        print("simulated authorize: OK (signature, nonce, collector and token pull all valid)")
        try:
            escrow.functions.authorize(pi, max_amount, COLLECTOR, signed.signature).call({"from": buyer.address})
            print("non-operator authorize: unexpectedly OK")
        except Exception as e:
            print("non-operator authorize rejected as expected:", type(e).__name__)
        return
    # persist PaymentInfo BEFORE sending: without it the escrowed funds cannot be voided/captured
    import json
    json.dump({**info, "salt": str(info["salt"])}, open(f"paymentinfo_{symbol}.json", "w"), indent=1)
    print("before", balances(token))
    print("authorize (lock", max_amount, ")")
    send(escrow.functions.authorize(pi, max_amount, COLLECTOR, signed.signature))
    print("  state", escrow.functions.paymentState(escrow.functions.getHash(pi).call()).call(), "after", balances(token))
    print("capture", capture_amount, "to seller")
    send(escrow.functions.capture(pi, capture_amount, 0, ZERO))
    print("void remainder back to buyer")
    send(escrow.functions.void(pi))
    print("after", balances(token))


if __name__ == "__main__":
    check()
    if len(sys.argv) > 1 and sys.argv[1] in ("run", "simulate"):
        run(sys.argv[2] if len(sys.argv) > 2 else "EURC", simulate=sys.argv[1] == "simulate")
