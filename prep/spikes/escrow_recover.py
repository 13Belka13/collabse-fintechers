# Recovers PaymentInfo of our latest authorization from PaymentAuthorized logs and voids the rest.
import os, time
from dotenv import load_dotenv
from eth_account import Account
from web3 import Web3
import escrow_spike as es

load_dotenv()
w3, escrow, operator = es.w3, es.escrow, es.operator
event_abi = {"anonymous": False, "name": "PaymentAuthorized", "type": "event", "inputs": [
    {"indexed": True, "name": "paymentInfoHash", "type": "bytes32"},
    {"indexed": False, **es.PI},
    {"indexed": False, "name": "amount", "type": "uint256"},
    {"indexed": False, "name": "tokenCollector", "type": "address"}]}
ev = w3.eth.contract(address=es.ESCROW, abi=[event_abi]).events.PaymentAuthorized
latest = w3.eth.block_number
logs = []
for start in range(latest - 900, latest + 1, 50):
    logs += ev().get_logs(from_block=start, to_block=min(start + 49, latest))
ours = [l for l in logs if l.args.paymentInfo["operator"].lower() == operator.address.lower()]
print("our authorizations found:", len(ours))
info = ours[-1].args.paymentInfo
pi = tuple(info[c["name"]] for c in es.PI_COMPONENTS)
state = escrow.functions.paymentState(escrow.functions.getHash(pi).call()).call()
print("state (collected, capturable, refundable):", state)
if state[1] > 0:
    print("void remainder", state[1])
    es.send(escrow.functions.void(pi))
print("after", es.balances(info["token"]))
