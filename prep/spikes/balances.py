# Prints ETH, USDC and EURC balances of the spike wallets on Base Sepolia.
import os
from dotenv import load_dotenv
from web3 import Web3

load_dotenv()
w3 = Web3(Web3.HTTPProvider(os.environ["RPC_URL"]))
abi = [{"name": "balanceOf", "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"}]
tokens = {"USDC": "0x036CbD53842c5426634e7929541eC2318f3dCF7e", "EURC": "0x808456652fdb597867f38412077A9182bf77359F"}
for role in ("BUYER", "SELLER", "OPERATOR"):
    a = os.environ[f"{role}_ADDR"]
    out = {"ETH": w3.eth.get_balance(a) / 1e18}
    for t, addr in tokens.items():
        out[t] = w3.eth.contract(address=addr, abi=abi).functions.balanceOf(a).call() / 1e6
    print(role, a, out)
