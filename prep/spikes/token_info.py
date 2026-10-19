# Reads on-chain EIP-712 domain data (name, version) for USDC and EURC on Base Sepolia.
from web3 import Web3

w3 = Web3(Web3.HTTPProvider("https://sepolia.base.org"))
abi = [{"name": n, "inputs": [], "outputs": [{"type": t}], "stateMutability": "view", "type": "function"}
       for n, t in (("name", "string"), ("version", "string"), ("decimals", "uint8"), ("symbol", "string"))]
for label, addr in (("USDC", "0x036CbD53842c5426634e7929541eC2318f3dCF7e"), ("EURC", "0x808456652fdb597867f38412077A9182bf77359F")):
    c = w3.eth.contract(address=Web3.to_checksum_address(addr), abi=abi)
    print(label, addr, {n: c.functions[n]().call() for n in ("name", "version", "decimals", "symbol")})
print("chain_id", w3.eth.chain_id)
