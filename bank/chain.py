# Base Sepolia access: web3 client, one serialised sender per key, chunked log reads.
# Lessons from prep/ (REFERENCE §8a, disclosed pre-hackathon spikes): "pending" nonce + local
# counter per key, retry on nonce clashes, idempotency key per money tx, never trust a read
# right after a write, eth_getLogs in <= 50-block chunks.
#
# Idempotency rule: once a tx for an idem_key is signed, its exact bytes are stored. Until that tx is
# provably dead (its nonce consumed by another tx, or the node rejected it outright) every retry
# re-broadcasts the same bytes — same nonce, so it can never land twice.
import threading
import time

from eth_account import Account
from web3 import Web3
from web3.exceptions import TransactionNotFound

from bank import config, db, timing

ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "a", "type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "transfer", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "to", "type": "address"}, {"name": "value", "type": "uint256"}],
     "outputs": [{"type": "bool"}]},
    {"name": "name", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "string"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint8"}]},
    {"name": "Transfer", "type": "event", "anonymous": False, "inputs": [
        {"name": "from", "type": "address", "indexed": True},
        {"name": "to", "type": "address", "indexed": True},
        {"name": "value", "type": "uint256", "indexed": False}]},
]
TRANSFER_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)").to_0x_hex()
# The nonce is taken by another tx: ours with this nonce can never land.
NONCE_ERRORS = ("replacement transaction underpriced", "nonce too low", "nonce has already been used",
                "invalid nonce", "invalid transaction nonce")
# The node refused the tx outright (it was never in the mempool); the nonce is still free.
REJECTED_ERRORS = ("insufficient funds", "not have enough balance", "intrinsic gas", "exceeds block gas limit",
                   "invalid sender", "transaction underpriced", "max fee per gas less than block base fee")

_w3 = None
_chain_id = None
_w3_lock = threading.Lock()


class TxReverted(Exception):
    def __init__(self, tx_hash, receipt):
        super().__init__(f"transaction reverted: {tx_link(tx_hash)}")
        self.tx_hash, self.receipt = tx_hash, receipt


def w3() -> Web3:
    global _w3
    with _w3_lock:
        if _w3 is None:
            # request_kwargs headers replace web3's defaults: keep Content-Type or anvil/Tatum reject the call
            headers = {"Content-Type": "application/json"}
            if "tatum" in config.RPC_URL and config.TATUM_API_KEY:
                headers["x-api-key"] = config.TATUM_API_KEY
            _w3 = Web3(Web3.HTTPProvider(config.RPC_URL, request_kwargs={"headers": headers, "timeout": 30}))
        return _w3


def chain_id() -> int:
    global _chain_id
    if _chain_id is None:
        _chain_id = w3().eth.chain_id
    return _chain_id


def set_w3(client: Web3 | None):
    """Swap the client (tests use an in-memory eth-tester chain); resets per-key senders."""
    global _w3, _chain_id
    _w3, _chain_id = client, None
    Sender._by_addr.clear()


def tx_link(tx_hash: str) -> str:
    return f"{config.EXPLORER}/tx/{_hex(tx_hash)}"


def address_link(addr: str) -> str:
    return f"{config.EXPLORER}/address/{addr}"


def _hex(h) -> str:
    h = h if isinstance(h, str) else h.hex()
    return h if h.startswith("0x") else "0x" + h


def erc20(address: str):
    return w3().eth.contract(address=Web3.to_checksum_address(address), abi=ERC20_ABI)


def balance(asset: str, owner: str) -> int:
    return erc20(config.ASSETS[asset]["address"]).functions.balanceOf(Web3.to_checksum_address(owner)).call()


def read_retry(fn, ok=lambda v: True, tries=6, delay=1.0):
    """Reads lag writes on load-balanced RPCs: retry until ok(value) or give up and return the last value."""
    value = fn()
    for _ in range(tries - 1):
        if ok(value):
            return value
        time.sleep(delay)
        value = fn()
    return value


def get_logs(address: str, topics: list, from_block: int, to_block: int | None = None, step: int = 50) -> list:
    """eth_getLogs in <= step-block chunks (public RPC answers 413 on big ranges)."""
    client = w3()
    to_block = client.eth.block_number if to_block is None else to_block
    out = []
    for start in range(from_block, to_block + 1, step):
        out += client.eth.get_logs({"address": Web3.to_checksum_address(address), "topics": topics,
                                    "fromBlock": start, "toBlock": min(start + step - 1, to_block)})
    return out


def transfers(token: str, from_block: int, to_block: int | None = None, frm: str | None = None,
              to: str | None = None) -> list[dict]:
    """Decoded ERC-20 Transfer events, optionally filtered by sender/recipient."""
    pad = lambda a: "0x" + a.lower().removeprefix("0x").rjust(64, "0")  # noqa: E731
    topics = [TRANSFER_TOPIC, pad(frm) if frm else None, pad(to) if to else None]
    while topics and topics[-1] is None:
        topics.pop()
    out = []
    for log in get_logs(token, topics, from_block, to_block):
        out.append({"from": Web3.to_checksum_address("0x" + log["topics"][1].hex()[-40:]),
                    "to": Web3.to_checksum_address("0x" + log["topics"][2].hex()[-40:]),
                    "value": int(log["data"].hex() or "0", 16), "tx_hash": _hex(log["transactionHash"]),
                    "block": log["blockNumber"]})
    return out


def _receipt(tx_hash):
    try:
        return w3().eth.get_transaction_receipt(tx_hash)
    except TransactionNotFound:
        return None


def _known(tx_hash) -> bool:
    try:
        w3().eth.get_transaction(tx_hash)
        return True
    except TransactionNotFound:
        return False


def _mark_dead(tx_hash):
    db.run("UPDATE onchain_txs SET status='failed', idem_key=NULL WHERE tx_hash=?", (tx_hash,))


class Sender:
    """One per key. Signing + broadcast are serialised (nonce from a local counter seeded by 'pending');
    waiting for the receipt is not, so one slow tx does not block the key."""

    _by_addr: dict[str, "Sender"] = {}
    _registry_lock = threading.Lock()

    @classmethod
    def for_key(cls, private_key: str) -> "Sender":
        acct = Account.from_key(private_key)
        with cls._registry_lock:
            if acct.address not in cls._by_addr:
                cls._by_addr[acct.address] = cls(acct)
            return cls._by_addr[acct.address]

    def __init__(self, acct):
        self.acct, self.address = acct, acct.address
        self.lock = threading.Lock()
        self.nonce = None

    def _pending(self) -> int:
        return w3().eth.get_transaction_count(self.address, "pending")

    def send(self, call, *, kind: str, deal_id: str | None = None, idem_key: str | None = None,
             gas: int | None = None, expect_revert: bool = False, timeout: float = 120, tries: int = 4) -> dict:
        """Sign, broadcast and wait for one tx. `call` is a contract function call or a tx dict.
        Same idem_key again -> the same tx (re-broadcast if needed, or its stored result), never a second one.
        expect_revert=True: manual gas (estimate_gas raises on revert); returns the failed receipt instead of raising.
        On a timeout or an ambiguous RPC error it raises and keeps the key: call again with the same idem_key."""
        gas = 300_000 if expect_revert and gas is None else gas
        tx_hash = None
        with self.lock:
            if idem_key:
                prior = db.one("SELECT * FROM onchain_txs WHERE idem_key=?", (idem_key,))
                if prior and prior["status"] in ("mined", "reverted"):
                    if prior["status"] == "reverted" and not expect_revert:
                        raise TxReverted(prior["tx_hash"], None)
                    return {"tx_hash": prior["tx_hash"], "status": prior["status"], "block_number": prior["block_number"],
                            "gas_used": prior["gas_used"], "link": tx_link(prior["tx_hash"]), "reused": True}
                if prior:
                    tx_hash = self._resume(prior)
            if tx_hash is None:
                tx_hash = self._sign_and_broadcast(call, kind, deal_id, idem_key, gas, tries)
        with timing.timed(f"tx_in_block:{kind}", deal_id):
            receipt = w3().eth.wait_for_transaction_receipt(tx_hash, timeout=timeout, poll_latency=0.5)
        return self._finish(tx_hash, receipt, expect_revert)

    def _resume(self, prior) -> str | None:
        """A signed tx exists for this key. Return its hash if it is mined, pending or re-broadcast;
        None only if it provably can never land (then a fresh tx may be built)."""
        h = prior["tx_hash"]
        if _receipt(h) is not None or _known(h):
            return h
        try:
            w3().eth.send_raw_transaction(bytes.fromhex(prior["raw_tx"].removeprefix("0x")))
            return h
        except Exception as e:  # noqa: BLE001 — RPC errors arrive as ValueError/Web3RPCError
            msg = str(e).lower()
            if "already known" in msg:
                return h
            if any(m in msg for m in NONCE_ERRORS):
                # the nonce is used — by our tx (receipt not visible yet on a lagging node) or by another one
                if read_retry(lambda: _receipt(h), ok=lambda r: r is not None) is not None:
                    return h
                _mark_dead(h)
                self.nonce = None
                return None
            if any(m in msg for m in REJECTED_ERRORS):
                _mark_dead(h)
                return None
            raise  # still ambiguous: keep the row and the key

    def _sign_and_broadcast(self, call, kind, deal_id, idem_key, gas, tries) -> str:
        last_error = None
        for _ in range(tries):
            if self.nonce is None:
                self.nonce = self._pending()
            nonce = self.nonce
            signed = self.acct.sign_transaction(self._build(call, nonce, gas))
            tx_hash, raw = _hex(signed.hash), _hex(signed.raw_transaction)
            existing = db.one("SELECT status, idem_key FROM onchain_txs WHERE tx_hash=?", (tx_hash,))
            if existing and existing["status"] in ("mined", "reverted"):
                self.nonce = max(nonce + 1, self._pending())  # identical tx already landed: our counter is stale
                continue
            if existing:  # the same bytes were signed before and never confirmed: this send owns them now
                db.run("UPDATE onchain_txs SET status='signed', kind=?, deal_id=?,"
                       " idem_key=COALESCE(idem_key, ?) WHERE tx_hash=?", (kind, deal_id, idem_key, tx_hash))
            else:
                db.run("INSERT INTO onchain_txs (tx_hash, kind, deal_id, status, idem_key, sender, nonce, raw_tx,"
                       " created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                       (tx_hash, kind, deal_id, "signed", idem_key, self.address, nonce, raw, time.time()))
            try:
                with timing.timed(f"broadcast:{kind}", deal_id):
                    w3().eth.send_raw_transaction(signed.raw_transaction)
            except Exception as e:  # noqa: BLE001
                msg = str(e).lower()
                if "already known" in msg:  # the same signed tx is already in the mempool
                    self.nonce = nonce + 1
                    return tx_hash
                if any(m in msg for m in NONCE_ERRORS):
                    _mark_dead(tx_hash)
                    last_error = e
                    self.nonce = max(nonce + 1, self._pending())
                    time.sleep(1)
                    continue
                if any(m in msg for m in REJECTED_ERRORS):
                    _mark_dead(tx_hash)  # never entered the mempool; the nonce stays free
                    raise
                self.nonce = None  # ambiguous (timeout, connection): the node may have it — keep row and key
                raise
            self.nonce = nonce + 1
            return tx_hash
        raise RuntimeError(f"{kind}: nonce retries exhausted") from last_error

    def _build(self, call, nonce, gas):
        base = {"from": self.address, "nonce": nonce, "chainId": chain_id()}
        if gas is not None:
            base["gas"] = gas
        if isinstance(call, dict):
            tx = {**base, **call}
            if "gas" not in tx:
                tx["gas"] = w3().eth.estimate_gas({k: v for k, v in tx.items() if k != "nonce"})
            if "maxFeePerGas" not in tx and "gasPrice" not in tx:
                tx["gasPrice"] = w3().eth.gas_price
            return tx
        return call.build_transaction(base)

    def _finish(self, tx_hash, receipt, expect_revert):
        status = "mined" if receipt["status"] == 1 else "reverted"
        db.run("UPDATE onchain_txs SET status=?, block_number=?, gas_used=? WHERE tx_hash=?",
               (status, receipt["blockNumber"], receipt["gasUsed"], tx_hash))
        if status == "reverted" and not expect_revert:
            raise TxReverted(tx_hash, receipt)
        return {"tx_hash": tx_hash, "status": status, "block_number": receipt["blockNumber"],
                "gas_used": receipt["gasUsed"], "link": tx_link(tx_hash), "receipt": receipt}


def sender(private_key: str) -> Sender:
    if not private_key:
        raise RuntimeError("missing private key in .env (TODO(credential))")
    return Sender.for_key(private_key)
