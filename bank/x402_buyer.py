# Buyer side of x402 (SPEC §4.2): the bank pays from a client's custody key. The SDK rejects EURC/tCZK by
# default, so both are allowed explicitly with per-payment caps in atomic units = the mandate's per-deal
# limit (10 €; 250,00 Kč at the demo rate) — one more layer on top of the mandate, not instead of it.
# Technique from prep/spikes/buyer_x402.py (disclosed pre-hackathon spike).
import base64
import json

from eth_account import Account
from x402 import x402Client
from x402.http.clients import x402HttpxClient
from x402.mechanisms.evm.exact import ExactEvmScheme
from x402.mechanisms.evm.signers import EthAccountSigner

from bank import config, money

PER_PAYMENT_CAP_EUR_CENTS = 1000


def spend_controls(cap_eur_cents: int = PER_PAYMENT_CAP_EUR_CENTS) -> dict:
    return {"max_amount_per_payment": False,
            "allowed_assets": [{"network": config.NETWORK, "asset": config.ASSETS[a]["address"],
                                "max_amount_per_payment": str(money.from_eur_cents(a, cap_eur_cents))}
                               for a in ("EURC", "tCZK") if config.ASSETS[a]["address"]]}


def client(private_key: str, asset: str | None = None, cap_eur_cents: int = PER_PAYMENT_CAP_EUR_CENTS) -> x402Client:
    """x402 client for one payer key. asset="EURC"/"tCZK" pays only in that asset when the 402 offers both."""
    c = x402Client().set_spend_controls(spend_controls(cap_eur_cents))
    if asset:
        address = config.ASSETS[asset]["address"].lower()
        c.register_policy(lambda version, reqs: [r for r in reqs if r.asset.lower() == address])
    # register only Base Sepolia (register_exact_evm_client would also add v1 on every legacy network)
    c.register(config.NETWORK, ExactEvmScheme(signer=EthAccountSigner(Account.from_key(private_key))))
    return c


async def get(url: str, private_key: str, asset: str | None = None, timeout: float = 90):
    """Pay for one x402 resource. Returns (response, settlement dict from PAYMENT-RESPONSE or None)."""
    async with x402HttpxClient(client(private_key, asset), timeout=timeout) as http:
        r = await http.get(url)
    header = r.headers.get("payment-response")
    return r, (json.loads(base64.b64decode(header)) if header else None)
