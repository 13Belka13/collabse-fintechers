# Deploys the demo tokenized deposit tCZK to Base Sepolia from BANK_KEY, allowlists clients and
# escrow infrastructure, mints 1000.00 tCZK to the buyer, and stores TCZK=<address> in .env.
import os
import solcx
from dotenv import load_dotenv
from eth_account import Account
from web3 import Web3

load_dotenv()
w3 = Web3(Web3.HTTPProvider(os.environ["RPC_URL"]))
bank = Account.from_key(os.environ["BANK_KEY"])
COLLECTOR = "0x8612dfdc421f80336cd14E8EF9cb1E765dB5ab88"
TOKEN_STORE = "0xd2b0C305F62427414e9624a2beD72a3792e15cf8"  # escrow.getTokenStore(OPERATOR_ADDR)

out = solcx.compile_source(open("contracts/DepositToken.sol").read(), output_values=["abi", "bin"], solc_version="0.8.26")
abi, bytecode = next(v for k, v in out.items() if k.endswith(":DepositToken")).values()
nonce = w3.eth.get_transaction_count(bank.address, "pending")


def send(tx):
    global nonce
    tx = tx if "to" in tx or "data" in tx else tx
    tx.update({"from": bank.address, "nonce": nonce, "chainId": w3.eth.chain_id})
    h = w3.eth.send_raw_transaction(bank.sign_transaction(tx).raw_transaction)
    nonce += 1
    r = w3.eth.wait_for_transaction_receipt(h, timeout=180)
    assert r.status == 1, f"reverted: {h.hex()}"
    return r


if os.getenv("TCZK"):
    token = w3.eth.contract(address=os.environ["TCZK"], abi=abi)
    print("using existing tCZK", token.address)
else:
    r = send(w3.eth.contract(abi=abi, bytecode=bytecode).constructor("Demo Bank tCZK").build_transaction({"from": bank.address}))
    token = w3.eth.contract(address=r.contractAddress, abi=abi)
    open(".env", "a").write(f"TCZK={token.address}\n")
    print("deployed tCZK", token.address, f"https://sepolia.basescan.org/address/{token.address}")

for who in (os.environ["BUYER_ADDR"], os.environ["SELLER_ADDR"], COLLECTOR, TOKEN_STORE):
    if not token.functions.allowlisted(who).call():
        send(token.functions.setAllowlisted(who, True).build_transaction({"from": bank.address}))
        print("allowlisted", who)
r = send(token.functions.mint(os.environ["BUYER_ADDR"], 100_000).build_transaction({"from": bank.address}))
print("minted 1000.00 tCZK to buyer:", "https://sepolia.basescan.org/tx/0x" + r.transactionHash.hex().removeprefix("0x"))
print("buyer balance", token.functions.balanceOf(os.environ["BUYER_ADDR"]).call() / 100, "tCZK")
