# Simulated chain + in-process seller for the demo without testnet keys (MOCK=1, no anvil, no Base Sepolia).
# TODO(credential): only a fallback for the stage until BANK_OPERATOR_PK / ALFA_PK are funded. Everything the bank
# does is the real code path (deals.pay -> escrow.authorize/capture/void -> disputes); only chain.sender and
# escrow.read_state are replaced, the seller app runs in-process, and the UI shows a "SIMULATED CHAIN" banner.
# Enable in the bank process with DEMO_SIM=1 (bank/ui.py lifespan), or call install() in a script.
import os
import secrets
import threading
import time

from eth_account import Account

ACTIVE = False
SIM_TCZK = "0x6a99C9e95DD220172d07D5f730f2e82B466D4B67"  # the prep token's address, only for EIP-712 domains
_lock = threading.Lock()


def sim_link(tx_hash: str) -> str:
    """Never an explorer URL for a simulated tx: it would point at a transaction that does not exist."""
    return f"simulated tx {tx_hash[:10]}… (not on Base Sepolia)"


class SimChain:
    """chain.Sender stand-in: one tx per idem key, escrow paymentState tracked per payment hash."""

    def __init__(self):
        self.state: dict[str, dict] = {}

    def send(self, call, *, kind, deal_id=None, idem_key=None, gas=None, expect_revert=False):
        from bank import chain, db, escrow
        with _lock:
            prior = db.one("SELECT tx_hash FROM onchain_txs WHERE idem_key=?", (idem_key,)) if idem_key else None
            if prior:  # same idem key -> the same tx, as chain.Sender does
                return {"tx_hash": prior["tx_hash"], "status": "simulated", "block_number": None, "gas_used": 0,
                        "link": sim_link(prior["tx_hash"]), "receipt": None}
            h = "0x" + secrets.token_hex(32)
            if isinstance(call, dict):  # a plain tx (the decider salt commitment): nothing to simulate
                db.run("INSERT INTO onchain_txs (tx_hash, kind, deal_id, status, idem_key, created_at)"
                       " VALUES (?,?,?,?,?,?)", (h, kind, deal_id, "simulated", idem_key, time.time()))
                return {"tx_hash": h, "status": "simulated", "block_number": None, "gas_used": 0,
                        "link": sim_link(h), "receipt": None}
            pi = dict(zip([n for n, _ in escrow.PI_FIELDS], call.args[0]))
            st = self._state(escrow.payment_hash(pi))
            if call.fn_name == "authorize":
                st["capturable"] = int(call.args[1])
            elif call.fn_name == "capture":
                if int(call.args[1]) > st["capturable"]:
                    raise chain.TxReverted(f"simulated revert: capture {call.args[1]} > capturable {st['capturable']}")
                if err := escrow.contract_fee_error(pi, *call.args[1:4]):  # the contract's own fee check
                    raise chain.TxReverted(f"simulated revert: {err}")
                st["capturable"] -= int(call.args[1])
                st["refundable"] += int(call.args[1])
                st["collected"] = True
            elif call.fn_name == "void":
                st["capturable"] = 0
            db.run("INSERT INTO onchain_txs (tx_hash, kind, deal_id, status, idem_key, created_at) VALUES (?,?,?,?,?,?)",
                   (h, kind, deal_id, "simulated", idem_key, time.time()))
            return {"tx_hash": h, "status": "simulated", "block_number": None, "gas_used": 0, "link": sim_link(h),
                    "receipt": None}

    def _state(self, h: str) -> dict:
        """paymentState per hash; after a restart rebuilt from the bank's journal of escrow events."""
        if h not in self.state:
            from bank import db, journal
            st = {"collected": False, "capturable": 0, "refundable": 0}
            deal = db.one("SELECT deal_id, amount FROM deals WHERE payment_hash=?", (h,))
            if deal:
                ev = journal.entries(deal["deal_id"], limit=1000)
                if any(e["event"] == "escrow_authorized" for e in ev):
                    captured = sum(int(e["payload"].get("amount", 0)) for e in ev if e["event"] == "escrow_captured")
                    voided = any(e["event"] == "escrow_voided" for e in ev)
                    st = {"collected": captured > 0, "refundable": captured,
                          "capturable": 0 if voided else int(deal["amount"]) - captured}
            self.state[h] = st
        return self.state[h]

    def read_state(self, h: str) -> dict:
        with _lock:
            return dict(self._state(h))


SIM = SimChain()
SELLER = None


def install():
    """Patch the running process: simulated chain, in-process seller, L5 settlement after delivery."""
    global ACTIVE, SELLER
    if ACTIVE:
        return
    from fastapi.testclient import TestClient
    from x402.schemas import SettleResponse, SupportedKind, SupportedResponse, VerifyResponse

    from bank import chain, config, core, deals, disputes, escrow, tczk
    from seller import app as seller_mod

    for name in ("BANK_OPERATOR_PK", "ALFA_PK"):
        if not os.getenv(name):
            os.environ[name] = Account.create().key.hex()
    os.environ.setdefault("TCZK", SIM_TCZK)
    if not os.environ["TCZK"]:
        os.environ["TCZK"] = SIM_TCZK
    config.reload()

    class SimFacilitator:
        def get_supported(self):
            return SupportedResponse(kinds=[SupportedKind(x402_version=2, scheme="exact", network=config.NETWORK)])

        async def verify(self, payload, requirements):
            return VerifyResponse(is_valid=True, payer=payload.payload["authorization"]["from"])

        async def settle(self, payload, requirements):
            return SettleResponse(success=True, transaction="0x" + secrets.token_hex(32), network=config.NETWORK,
                                  payer=payload.payload["authorization"]["from"])

    from bank.app import app as bank_app
    bank_client = TestClient(bank_app)
    SELLER = seller_mod.create_app(SimFacilitator())
    chain.sender = lambda pk: SIM
    escrow.read_state = SIM.read_state
    # tCZK allowlist/freeze live on the token: simulated as "every bank client's wallet is allowed, none frozen"
    tczk.is_allowed = lambda addr, address=None: core.company_by_wallet(addr) is not None
    tczk.is_frozen = lambda addr, address=None: False
    seller_mod.read_retry = lambda fn, ok=None: fn()
    seller_mod.bank_post = lambda path, body, *a, **k: bank_client.post(
        path, json=body, headers={"x-seller-token": getattr(config, "SELLER_TOKEN", "")}).raise_for_status()
    deals.seller_http = lambda base_url: TestClient(SELLER)
    deals.auto_settle = lambda deal_id: disputes.advance(deal_id)  # the rule's schedule (MODEL_V2 §8.2)
    ACTIVE = True
    print("*** DEMO_SIM: simulated chain and in-process seller (no Base Sepolia transactions) ***", flush=True)


def seller_client():
    from fastapi.testclient import TestClient
    return TestClient(SELLER)
