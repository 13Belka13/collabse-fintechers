# Escrow operator for Coinbase AuthCaptureEscrow v1.1 (SPEC §5). The bank holds the operator key and,
# in the prototype, signs the buyer's ReceiveWithAuthorization on its behalf (custody, SPEC §1 decision 2).
# Technique from prep/spikes/escrow_spike.py (disclosed pre-hackathon spike): PaymentInfo tuple order,
# ERC-3009 nonce = getHash(PaymentInfo with payer = 0), capture(pi, amount, 0, 0x0), void(pi).
# Rules: PaymentInfo is persisted BEFORE authorize; capture never at/after authorizationExpiry; the final
# void comes after every capture (void returns the whole remainder, there is no partial void).
# MODEL_V2 §8.1-8.4 terms: feeReceiver = the bank's clearing address, minFeeBps = maxFeeBps = the tariff of the
# deal's tier and rule (fee = floor(amount·bps/10000) at capture, taken from the capture), salt =
# keccak(deal_id ‖ n) so the seller links the lock to the deal itself, authorizationExpiry = delivery deadline +
# window + 30 days (T1) / 120 days (T2), refundExpiry = authorizationExpiry + 30 days.
import time

from eth_abi import encode
from eth_account import Account
from web3 import Web3

from bank import chain, config, core, db, journal, timing

ZERO = "0x0000000000000000000000000000000000000000"
PI_FIELDS = [("operator", "address"), ("payer", "address"), ("receiver", "address"), ("token", "address"),
             ("maxAmount", "uint120"), ("preApprovalExpiry", "uint48"), ("authorizationExpiry", "uint48"),
             ("refundExpiry", "uint48"), ("minFeeBps", "uint16"), ("maxFeeBps", "uint16"),
             ("feeReceiver", "address"), ("salt", "uint256")]
PI_TYPEHASH = Web3.keccak(text="PaymentInfo(" + ",".join(f"{t} {n}" for n, t in PI_FIELDS) + ")")
ADDRESS_FIELDS = {n for n, t in PI_FIELDS if t == "address"}

PRE_APPROVAL_S, DAY_S = 600, 86400  # the authorize must land within 10 min of the PaymentInfo
CAPTURE_MARGIN_S = 30  # a capture sent this close to authorizationExpiry may land after it: don't

_PI = {"name": "paymentInfo", "type": "tuple", "components": [{"name": n, "type": t} for n, t in PI_FIELDS]}


def _fn(name, inputs, outputs=(), view=False):
    return {"name": name, "type": "function", "inputs": inputs, "outputs": list(outputs),
            "stateMutability": "view" if view else "nonpayable"}


ESCROW_ABI = [
    _fn("getHash", [_PI], [{"type": "bytes32"}], view=True),
    _fn("getTokenStore", [{"name": "operator", "type": "address"}], [{"type": "address"}], view=True),
    _fn("paymentState", [{"name": "paymentInfoHash", "type": "bytes32"}],
        [{"name": "hasCollectedPayment", "type": "bool"}, {"name": "capturableAmount", "type": "uint120"},
         {"name": "refundableAmount", "type": "uint120"}], view=True),
    _fn("authorize", [_PI, {"name": "amount", "type": "uint256"}, {"name": "tokenCollector", "type": "address"},
                      {"name": "collectorData", "type": "bytes"}]),
    _fn("capture", [_PI, {"name": "amount", "type": "uint256"}, {"name": "feeAmount", "type": "uint256"},
                    {"name": "feeReceiver", "type": "address"}]),
    _fn("void", [_PI]),
]
RECEIVE_TYPES = {"ReceiveWithAuthorization": [
    {"name": "from", "type": "address"}, {"name": "to", "type": "address"}, {"name": "value", "type": "uint256"},
    {"name": "validAfter", "type": "uint256"}, {"name": "validBefore", "type": "uint256"},
    {"name": "nonce", "type": "bytes32"}]}


class EscrowError(Exception):
    pass


def contract():
    return chain.w3().eth.contract(address=Web3.to_checksum_address(config.ESCROW), abi=ESCROW_ABI)


def as_tuple(pi: dict) -> tuple:
    return tuple(Web3.to_checksum_address(pi[n]) if n in ADDRESS_FIELDS else int(pi[n]) for n, _ in PI_FIELDS)


def payment_hash(pi: dict) -> str:
    """escrow.getHash(pi) computed locally: keccak(abi.encode(chainid, escrow, keccak(abi.encode(TYPEHASH, pi)))).
    Not an EIP-712 digest (SPEC §5.2). Checked against the deployed contract in the anvil fork test."""
    inner = Web3.keccak(encode(["bytes32"] + [t for _, t in PI_FIELDS], [PI_TYPEHASH, *as_tuple(pi)]))
    outer = encode(["uint256", "address", "bytes32"], [config.CHAIN_ID, Web3.to_checksum_address(config.ESCROW), inner])
    return Web3.keccak(outer).to_0x_hex()


def operator_address() -> str:
    if not config.BANK_OPERATOR_PK:
        raise EscrowError("missing BANK_OPERATOR_PK (TODO(credential))")
    return Account.from_key(config.BANK_OPERATOR_PK).address


def _deal(deal_id: str) -> dict:
    deal = db.one("SELECT * FROM deals WHERE deal_id=?", (deal_id,))
    if deal is None:
        raise EscrowError(f"unknown deal {deal_id}")
    return deal


def stored_payment_info(deal_id: str) -> dict:
    pi = db.loads(_deal(deal_id)["payment_info_json"])
    if pi is None:
        raise EscrowError(f"{deal_id}: no PaymentInfo stored")
    return pi


def salt(deal_id: str, n: int) -> int:
    """keccak(utf8(deal_id) ‖ uint256_be(n)): the n-th PaymentInfo of a deal (0 = the lock at pay, then rebuilt
    ones and the split-off of a disputed sum). The seller recomputes it to link a lock to its deal."""
    return int.from_bytes(Web3.solidity_keccak(["string", "uint256"], [deal_id, int(n)]), "big")


def expiries(terms: dict, now: int) -> tuple[int, int]:
    """(authorizationExpiry, refundExpiry) by MODEL_V2 §8.3: long — the bank closes the deal itself."""
    tier = terms.get("tier") or "T1"
    auth = now + int(terms.get("deadline_s") or 0) + int(terms.get("window_s") or 0) \
        + config.ESCROW_EXTRA_DAYS[tier] * DAY_S
    return auth, auth + config.REFUND_EXTRA_DAYS * DAY_S


def fee_terms(terms: dict) -> dict:
    bps = int(terms.get("fee_bps") or 0)
    return {"minFeeBps": bps, "maxFeeBps": bps, "feeReceiver": Web3.to_checksum_address(config.BANK_CLEARING_ADDR)}


def payment_info(deal: dict, now: int | None = None) -> dict:
    """Build PaymentInfo for the deal's quote and terms (maxAmount = price, salt = keccak(deal_id ‖ n)) and persist
    it with its hash BEFORE anything is signed: without it locked funds cannot be captured or voided."""
    quote = db.loads(deal["quote_json"]) or {}
    if not quote.get("pay_to") or not deal.get("asset") or not deal.get("amount"):
        raise EscrowError(f"{deal['deal_id']}: no accepted quote (pay_to, asset, amount)")
    now = int(time.time()) if now is None else now
    terms = db.loads(deal.get("terms_json")) or {}
    auth, refund = expiries(terms, now)
    pi = {"operator": operator_address(), "payer": core.company(deal["buyer_id"])["wallet_address"],
          "receiver": quote["pay_to"], "token": config.ASSETS[deal["asset"]]["address"],
          "maxAmount": int(quote["amount"]), "preApprovalExpiry": now + PRE_APPROVAL_S,
          "authorizationExpiry": auth, "refundExpiry": refund, **fee_terms(terms),
          "salt": str(salt(deal["deal_id"], int(deal.get("pi_n") or 0)))}
    pi = {k: (Web3.to_checksum_address(v) if k in ADDRESS_FIELDS else v) for k, v in pi.items()}
    h = payment_hash(pi)
    with db.immediate():
        db.run("UPDATE deals SET payment_info_json=?, payment_hash=?, updated_at=? WHERE deal_id=?",
               (db.dumps(pi), h, time.time(), deal["deal_id"]))
        db.run("INSERT OR IGNORE INTO payment_infos (payment_hash, deal_id, n, purpose, receiver, max_amount, pi_json,"
               " created_at) VALUES (?,?,?,?,?,?,?,?)", (h, deal["deal_id"], int(deal.get("pi_n") or 0), "deal",
                                                         pi["receiver"], int(pi["maxAmount"]), db.dumps(pi), time.time()))
    journal.append("bank", "payment_info_stored", {"payment_hash": h, "payment_info": pi}, deal_id=deal["deal_id"])
    return pi


def buyer_signature(pi: dict, asset: str, buyer_pk: str) -> bytes:
    """EIP-712 ReceiveWithAuthorization on the token domain, to = collector, value = maxAmount,
    validBefore = preApprovalExpiry, nonce = getHash(pi with payer = 0). 65 bytes r||s||v."""
    meta = config.ASSETS[asset]
    signed = Account.sign_typed_data(
        buyer_pk,
        domain_data={"name": meta["name"], "version": meta["version"], "chainId": config.CHAIN_ID,
                     "verifyingContract": Web3.to_checksum_address(meta["address"])},
        message_types=RECEIVE_TYPES,
        message_data={"from": Web3.to_checksum_address(pi["payer"]), "to": Web3.to_checksum_address(config.COLLECTOR),
                      "value": int(pi["maxAmount"]), "validAfter": 0, "validBefore": int(pi["preApprovalExpiry"]),
                      "nonce": bytes.fromhex(payment_hash({**pi, "payer": ZERO})[2:])})
    return signed.signature


def idem_key(deal_id: str, kind: str) -> str:
    """Idempotency key per PaymentInfo, not per deal: a rebuilt PaymentInfo (new salt after expiry or a new price)
    must never replay the old PaymentInfo's signed txs. kind: "authorize", "capture:<n>", "void"."""
    h = _deal(deal_id)["payment_hash"]
    if not h:
        raise EscrowError(f"{deal_id}: no PaymentInfo stored")
    return f"{deal_id}:{h[2:10]}:{kind}"


def authorized_tx(deal_id: str) -> dict | None:
    """The mined authorize tx of the current PaymentInfo, if any."""
    try:
        key = idem_key(deal_id, "authorize")
    except EscrowError:
        return None
    return db.one("SELECT * FROM onchain_txs WHERE idem_key=? AND status='mined'", (key,))


def authorize(deal_id: str) -> dict:
    """Lock maxAmount from the buyer into escrow. Reuses the stored PaymentInfo (one authorize per PaymentInfo);
    the same idem key again returns the same tx, never a second lock."""
    deal = _deal(deal_id)
    pi = db.loads(deal["payment_info_json"]) or payment_info(deal)
    done = authorized_tx(deal_id)
    if done:  # it already landed (an earlier call only lost the answer): report it, even after preApprovalExpiry
        db.run("UPDATE payment_infos SET authorized=1 WHERE payment_hash=?", (payment_hash(pi),))
        return {"tx_hash": done["tx_hash"], "status": "mined", "block_number": done["block_number"],
                "gas_used": done["gas_used"], "link": chain.tx_link(done["tx_hash"]), "reused": True}
    if int(time.time()) >= int(pi["preApprovalExpiry"]):
        landed = read_state(payment_hash(pi))
        if landed["capturable"] > 0 or landed["collected"]:  # an earlier authorize landed late: it is the lock
            prior = db.one("SELECT tx_hash FROM onchain_txs WHERE idem_key=?", (idem_key(deal_id, "authorize"),)) or {}
            if prior.get("tx_hash"):
                db.run("UPDATE onchain_txs SET status='mined' WHERE tx_hash=?", (prior["tx_hash"],))
            db.run("UPDATE payment_infos SET authorized=1 WHERE payment_hash=?", (payment_hash(pi),))
            journal.append("bank", "escrow_authorized_late", {"capturable": landed["capturable"]}, deal_id=deal_id)
            return {"tx_hash": prior.get("tx_hash"), "status": "mined",
                    "link": chain.tx_link(prior["tx_hash"]) if prior.get("tx_hash") else None, "reused": True}
        raise EscrowError(f"{deal_id}: preApprovalExpiry passed, a new PaymentInfo (new salt) is needed")
    signature = buyer_signature(pi, deal["asset"], core.wallet_key(deal["buyer_id"]))
    call = contract().functions.authorize(as_tuple(pi), int(pi["maxAmount"]),
                                          Web3.to_checksum_address(config.COLLECTOR), signature)
    with timing.timed("escrow_authorize", deal_id):
        tx = chain.sender(config.BANK_OPERATOR_PK).send(call, kind="authorize", deal_id=deal_id,
                                                        idem_key=idem_key(deal_id, "authorize"))
    db.run("UPDATE payment_infos SET authorized=1 WHERE payment_hash=?", (payment_hash(pi),))
    journal.append("bank", "escrow_authorized", {"amount": int(pi["maxAmount"]), "tx_hash": tx["tx_hash"],
                                                 "link": tx["link"]}, deal_id=deal_id)
    return tx


def fee_of(pi: dict, amount: int) -> int:
    """The bank's fee taken from a capture: floor(amount · maxFeeBps / 10000). AuthCaptureEscrow v1.1 _validateFee
    accepts feeAmount in [floor(amount·minFeeBps/10000), floor(amount·maxFeeBps/10000)], so with min = max this
    is the only accepted value."""
    return int(amount) * int(pi["maxFeeBps"]) // 10_000


def capture_fee(pi: dict, amount: int) -> tuple[int, str]:
    """(feeAmount, feeReceiver) for capture(). The contract reverts with InvalidFeeReceiver when PaymentInfo names a
    feeReceiver and capture passes another one — also when the fee rounds to 0 (T0 at 0 bps, a tiny capture) — so
    the configured receiver is always passed; 0x0 only when PaymentInfo leaves it open (then the fee must be 0)."""
    fee, receiver = fee_of(pi, amount), Web3.to_checksum_address(pi.get("feeReceiver") or ZERO)
    if receiver == ZERO and fee:
        raise EscrowError("a fee needs a feeReceiver in PaymentInfo")
    return fee, receiver


def contract_fee_error(pi: dict, amount: int, fee: int, receiver: str) -> str | None:
    """AuthCaptureEscrow v1.1 _validateFee, line by line (verified source of 0xf968…B19c): the revert it would raise
    on capture/charge, or None. Used by the simulated chain and the tests; the chain itself is the authority."""
    lo, hi = int(amount) * int(pi["minFeeBps"]) // 10_000, int(amount) * int(pi["maxFeeBps"]) // 10_000
    configured, receiver = Web3.to_checksum_address(pi["feeReceiver"]), Web3.to_checksum_address(receiver)
    if fee < lo or fee > hi:
        return f"FeeAmountOutOfRange({fee}, {lo}, {hi})"
    if receiver == ZERO and fee > 0:
        return "ZeroFeeReceiver()"
    if configured != ZERO and configured != receiver:
        return f"InvalidFeeReceiver({receiver}, {configured})"
    return None


def capture(deal_id: str, amount: int, n: int) -> dict:
    """Pay `amount` (atomic units, gross) of the locked funds: the seller gets amount − fee, the clearing address
    the fee. Refused locally at/after authorizationExpiry (minus a margin): the contract would revert."""
    pi = stored_payment_info(deal_id)
    if amount <= 0 or amount > int(pi["maxAmount"]):
        raise EscrowError(f"{deal_id}: bad capture amount {amount}")
    if time.time() >= int(pi["authorizationExpiry"]) - CAPTURE_MARGIN_S:
        raise EscrowError(f"{deal_id}: authorizationExpiry reached, capture is no longer possible (void only)")
    fee, receiver = capture_fee(pi, amount)
    call = contract().functions.capture(as_tuple(pi), int(amount), fee, receiver)
    with timing.timed("escrow_capture", deal_id):
        tx = chain.sender(config.BANK_OPERATOR_PK).send(call, kind="capture", deal_id=deal_id,
                                                        idem_key=idem_key(deal_id, f"capture:{n}"))
    journal.append("bank", "escrow_captured", {"amount": int(amount), "fee": fee, "n": n, "tx_hash": tx["tx_hash"],
                                               "link": tx["link"]}, deal_id=deal_id)
    return tx


def void(deal_id: str) -> dict:
    """Return the whole remaining capturable amount to the buyer. Final: call it after the last capture."""
    pi = stored_payment_info(deal_id)
    call = contract().functions.void(as_tuple(pi))
    with timing.timed("escrow_void", deal_id):
        tx = chain.sender(config.BANK_OPERATOR_PK).send(call, kind="void", deal_id=deal_id,
                                                        idem_key=idem_key(deal_id, "void"))
    journal.append("bank", "escrow_voided", {"tx_hash": tx["tx_hash"], "link": tx["link"]}, deal_id=deal_id)
    return tx


def finish(deal_id: str, to_seller: int) -> dict:
    """Settle the whole lock: capture `to_seller` first, then void the rest (never void before a capture)."""
    pi = stored_payment_info(deal_id)
    if not 0 <= to_seller <= int(pi["maxAmount"]):
        raise EscrowError(f"{deal_id}: bad settlement {to_seller}")
    out = {"capture": None, "void": None}
    if to_seller > 0:
        out["capture"] = capture(deal_id, to_seller, 1)
    if to_seller < int(pi["maxAmount"]):
        out["void"] = void(deal_id)
    return out


def _store_pi(deal_id: str, n: int, purpose: str, pi: dict) -> str:
    h = payment_hash(pi)
    db.run("INSERT OR IGNORE INTO payment_infos (payment_hash, deal_id, n, purpose, receiver, max_amount, pi_json,"
           " created_at) VALUES (?,?,?,?,?,?,?,?)", (h, deal_id, n, purpose, pi["receiver"], int(pi["maxAmount"]),
                                                     db.dumps(pi), time.time()))
    return h


def split_off(deal_id: str, amount: int, now: int | None = None) -> dict:
    """§8.2 at window end with an open C claim, after the undisputed part was captured and the old PaymentInfo
    voided: a new PaymentInfo on D only (salt keccak(deal_id ‖ n+1), the §8.3 terms from now), persisted before
    the authorize. Between the void and this authorize D sits in the buyer's wallet under the bank's key with the
    mandate's reservation kept, so neither the buyer nor its agent can spend it."""
    deal = _deal(deal_id)
    now = int(time.time()) if now is None else now
    old, terms = db.loads(deal["payment_info_json"]), db.loads(deal["terms_json"]) or {}
    n = int(deal["pi_n"] or 0) + 1
    auth, refund = expiries({**terms, "deadline_s": 0, "window_s": 0}, now)
    pi = {**old, "maxAmount": int(amount), "preApprovalExpiry": now + PRE_APPROVAL_S, "authorizationExpiry": auth,
          "refundExpiry": refund, "salt": str(salt(deal_id, n))}
    with db.immediate():
        h = _store_pi(deal_id, n, "deal", pi)
        db.run("UPDATE deals SET payment_info_json=?, payment_hash=?, pi_n=?, updated_at=? WHERE deal_id=?",
               (db.dumps(pi), h, n, time.time(), deal_id))
        journal.append("bank", "escrow_split_off", {"n": n, "amount": int(amount), "payment_hash": h},
                       deal_id=deal_id)
    return authorize(deal_id)


# ---- C dispute fees: separate authorizations to the clearing address, captured at once (§7C.4, §8.2) ----

def fee_payment_info(deal_id: str, role: str, amount: int, payer: str, k: int = 0, now: int | None = None) -> dict:
    """receiver = the clearing address, maxFeeBps = 0; stored before anything is signed."""
    deal = _deal(deal_id)
    now = int(time.time()) if now is None else now
    pi = {"operator": operator_address(), "payer": Web3.to_checksum_address(payer),
          "receiver": Web3.to_checksum_address(config.BANK_CLEARING_ADDR),
          "token": config.ASSETS[deal["asset"]]["address"], "maxAmount": int(amount),
          "preApprovalExpiry": now + PRE_APPROVAL_S, "authorizationExpiry": now + DAY_S,
          "refundExpiry": now + (1 + config.REFUND_EXTRA_DAYS) * DAY_S, "minFeeBps": 0, "maxFeeBps": 0,
          "feeReceiver": ZERO, "salt": str(salt(f"{deal_id}:fee:{role}", k))}
    pi = {key: (Web3.to_checksum_address(v) if key in ADDRESS_FIELDS else v) for key, v in pi.items()}
    _store_pi(deal_id, k, f"fee_{role}", pi)
    return pi


def receive_signer(pi: dict, asset: str, signature: str) -> str | None:
    """Who signed the ERC-3009 ReceiveWithAuthorization for this PaymentInfo (the seller pays its fee itself)."""
    from eth_account.messages import encode_typed_data
    meta = config.ASSETS[asset]
    try:
        msg = encode_typed_data(
            domain_data={"name": meta["name"], "version": meta["version"], "chainId": config.CHAIN_ID,
                         "verifyingContract": Web3.to_checksum_address(meta["address"])},
            message_types=RECEIVE_TYPES,
            message_data={"from": Web3.to_checksum_address(pi["payer"]), "to": Web3.to_checksum_address(config.COLLECTOR),
                          "value": int(pi["maxAmount"]), "validAfter": 0, "validBefore": int(pi["preApprovalExpiry"]),
                          "nonce": bytes.fromhex(payment_hash({**pi, "payer": ZERO})[2:])})
        return Account.recover_message(msg, signature=bytes.fromhex(str(signature).removeprefix("0x")))
    except Exception:  # noqa: BLE001
        return None


def pay_fee(deal_id: str, pi: dict, signature: bytes | str) -> dict:
    """Authorize the fee and capture all of it to the clearing address at once (paid, not blocked: a party cannot
    take its fee back by reclaim while the case runs)."""
    h = payment_hash(pi)
    key = f"{deal_id}:{h[2:10]}"
    sig = bytes.fromhex(signature.removeprefix("0x")) if isinstance(signature, str) else signature
    sender = chain.sender(config.BANK_OPERATOR_PK)
    auth = sender.send(contract().functions.authorize(as_tuple(pi), int(pi["maxAmount"]),
                                                      Web3.to_checksum_address(config.COLLECTOR), sig),
                       kind="fee_authorize", deal_id=deal_id, idem_key=f"{key}:fee-authorize")
    cap = sender.send(contract().functions.capture(as_tuple(pi), int(pi["maxAmount"]), 0, ZERO),
                      kind="fee_capture", deal_id=deal_id, idem_key=f"{key}:fee-capture")
    db.run("UPDATE payment_infos SET authorized=1, captured=max_amount WHERE payment_hash=?", (h,))
    journal.append("bank", "escrow_fee_captured", {"amount": int(pi["maxAmount"]), "payer": pi["payer"],
                                                   "tx_hash": cap["tx_hash"], "authorize_tx": auth["tx_hash"]},
                   deal_id=deal_id)
    return cap


def transfer_from_clearing(deal_id: str, asset: str, to: str, amount: int, what: str) -> dict:
    """A fee back to the winner or to the decider: a plain transfer from the clearing address (§7C.5)."""
    if not config.BANK_CLEARING_PK and not config.MOCK:
        raise EscrowError("missing BANK_CLEARING_PK (TODO(credential))")
    call = chain.erc20(config.ASSETS[asset]["address"]).functions.transfer(Web3.to_checksum_address(to), int(amount))
    tx = chain.sender(config.BANK_CLEARING_PK).send(call, kind="transfer", deal_id=deal_id,
                                                    idem_key=f"{deal_id}:clearing:{what}")
    journal.append("bank", "clearing_transfer", {"to": to, "amount": int(amount), "what": what,
                                                 "tx_hash": tx["tx_hash"]}, deal_id=deal_id)
    return tx


def paid(deal_id: str) -> int:
    """'Paid' at deal level (§8.4): gross captures over every PaymentInfo of the deal whose receiver is the seller —
    fee authorizations (receiver = the clearing address) never count."""
    deal = _deal(deal_id)
    seller = (db.loads(deal["quote_json"]) or {}).get("pay_to") or ""
    return sum(r["captured"] for r in db.rows(
        "SELECT receiver, captured FROM payment_infos WHERE deal_id=?", (deal_id,))
        if r["receiver"].lower() == seller.lower())


def locked_amount(deal_id: str) -> int:
    """What can ever reach the seller from this deal's locks: captured on PaymentInfos already voided, plus the
    whole maxAmount of the open ones. Only authorized deal PaymentInfos count (not fee authorizations)."""
    rows = db.rows("SELECT max_amount, captured, voided FROM payment_infos WHERE deal_id=? AND purpose='deal'"
                   " AND authorized=1", (deal_id,))
    if not rows:
        return int(_deal(deal_id)["amount"] or 0)
    return sum(r["captured"] if r["voided"] else r["max_amount"] for r in rows)


def read_state(h: str) -> dict:
    collected, capturable, refundable = contract().functions.paymentState(bytes.fromhex(h.removeprefix("0x"))).call()
    return {"collected": collected, "capturable": capturable, "refundable": refundable}


def state(deal_id: str, ok=lambda s: True) -> dict:
    """Escrow state from the chain (not our DB). Reads lag writes: pass ok= to retry until the expected state."""
    h = _deal(deal_id)["payment_hash"]
    if not h:
        raise EscrowError(f"{deal_id}: no PaymentInfo stored")
    return chain.read_retry(lambda: read_state(h), ok)
