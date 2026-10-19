# Generates throwaway Base Sepolia test wallets into .env (testnet only, never fund with real money).
import os
from eth_account import Account

if os.path.exists(".env"):
    raise SystemExit(".env already exists, not overwriting")
lines = []
for role in ("BUYER", "SELLER", "OPERATOR"):
    acct = Account.create()
    lines += [f"{role}_KEY={acct.key.hex()}", f"{role}_ADDR={acct.address}"]
    print(role, acct.address)
with open(".env", "w") as f:
    f.write("\n".join(lines) + "\nRPC_URL=https://sepolia.base.org\n")
