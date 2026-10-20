# Delivery receipts (MODEL_V2 §5.1): the format both sides hash and sign, and the seller key registry.
# - Items are I-JSON (RFC 7493): integers beyond ±(2^53 − 1) travel as strings, strings in NFC, finite numbers.
# - Canonical form of an item: JCS (RFC 8785).
# - Merkle tree, H = SHA-256: leaf = H(0x00 ‖ uint64_be(index) ‖ JCS(item)), node = H(0x01 ‖ left ‖ right);
#   an odd node is promoted to the next level unchanged (no duplication, so no CVE-2012-2459-style collisions).
#   Reference vectors: fixtures/merkle_vectors.json (tests/unit/test_receipts.py recomputes them by hand).
# - Manifest: EIP-712, signed with the seller's own key (not its wallet): {deal_id, offer_hash, version, count,
#   merkle_root, nonce}. The bank keeps each seller key with the time it was registered and revoked.
import hashlib
import math
import time
import unicodedata

import rfc8785
from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import Web3

from bank import config, db, journal

MAX_SAFE_INT = 2**53 - 1
MAX_DELIVERY_BYTES = 10 * 1024 * 1024  # §5.1: up to 10 MB per delivery
MANIFEST_TYPES = {"Manifest": [
    {"name": "deal_id", "type": "string"}, {"name": "offer_hash", "type": "bytes32"},
    {"name": "version", "type": "uint32"}, {"name": "count", "type": "uint32"},
    {"name": "merkle_root", "type": "bytes32"}, {"name": "nonce", "type": "bytes32"}]}


class FormatError(ValueError):
    pass


def domain() -> dict:
    return {"name": "Collabse Delivery Manifest", "version": "1", "chainId": config.CHAIN_ID}


# ---- I-JSON and JCS ----

def ijson_problem(value, path: str = "$") -> str | None:
    """None if `value` is I-JSON with NFC strings, else where and why it is not."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return None if abs(value) <= MAX_SAFE_INT else f"{path}: integer beyond 2^53 must be a string"
    if isinstance(value, float):
        return None if math.isfinite(value) else f"{path}: number is not finite"
    if isinstance(value, str):
        return None if unicodedata.is_normalized("NFC", value) else f"{path}: string is not in NFC"
    if isinstance(value, list):
        return next((p for i, v in enumerate(value) if (p := ijson_problem(v, f"{path}[{i}]"))), None)
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str) or not unicodedata.is_normalized("NFC", k):
                return f"{path}: key {k!r} is not an NFC string"
            if p := ijson_problem(v, f"{path}.{k}"):
                return p
        return None
    return f"{path}: {type(value).__name__} is not JSON"


def jcs(value) -> bytes:
    """RFC 8785 canonical bytes. Raises FormatError for anything outside I-JSON."""
    problem = ijson_problem(value)
    if problem:
        raise FormatError(problem)
    try:
        return rfc8785.dumps(value)
    except Exception as e:  # noqa: BLE001 — the library's domain errors
        raise FormatError(str(e)) from e


def sha256(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def hex32(b: bytes) -> str:
    return "0x" + b.hex()


def unhex32(s: str) -> bytes:
    b = bytes.fromhex(str(s).removeprefix("0x"))
    if len(b) != 32:
        raise FormatError(f"expected 32 bytes, got {len(b)}")
    return b


def offer_hash(offer: dict) -> str:
    """The offer both sides refer to: SHA-256 over its JCS bytes."""
    return hex32(sha256(jcs(offer)))


def terms_hash(terms: dict) -> str:
    """Hash of the task terms a human confirmed (§2.1)."""
    return hex32(sha256(jcs(terms)))


# ---- Merkle tree ----

def leaf(index: int, item) -> bytes:
    if not 0 <= index < 2**64:
        raise FormatError("index out of uint64 range")
    return sha256(b"\x00" + index.to_bytes(8, "big") + jcs(item))


def node(left: bytes, right: bytes) -> bytes:
    return sha256(b"\x01" + left + right)


def _levels(leaves: list[bytes]) -> list[list[bytes]]:
    if not leaves:
        raise FormatError("a delivery has at least one item")
    levels = [leaves]
    while len(levels[-1]) > 1:
        cur = levels[-1]
        nxt = [node(cur[i], cur[i + 1]) for i in range(0, len(cur) - 1, 2)]
        if len(cur) % 2:
            nxt.append(cur[-1])  # odd node promoted, not duplicated
        levels.append(nxt)
    return levels


def merkle_root(items: list) -> str:
    return hex32(_levels([leaf(i, it) for i, it in enumerate(items)])[-1][0])


def merkle_proof(items: list, index: int) -> list[dict]:
    """Sibling path for one item: [{"side": "left"|"right", "hash"}], leaf to root (promoted levels skipped)."""
    levels, out, i = _levels([leaf(n, it) for n, it in enumerate(items)]), [], index
    for level in levels[:-1]:
        sib = i ^ 1
        if sib < len(level):
            out.append({"side": "left" if sib < i else "right", "hash": hex32(level[sib])})
        i //= 2
    return out


def verify_proof(item, index: int, proof: list[dict], root: str) -> bool:
    h = leaf(index, item)
    for step in proof:
        sib = unhex32(step["hash"])
        h = node(sib, h) if step["side"] == "left" else node(h, sib)
    return hex32(h) == root


# ---- manifest ----

def manifest_message(m: dict) -> dict:
    return {"deal_id": str(m["deal_id"]), "offer_hash": unhex32(m["offer_hash"]), "version": int(m["version"]),
            "count": int(m["count"]), "merkle_root": unhex32(m["merkle_root"]), "nonce": unhex32(m["nonce"])}


def sign_manifest(m: dict, private_key: str) -> str:
    signed = Account.sign_typed_data(private_key, domain_data=domain(), message_types=MANIFEST_TYPES,
                                     message_data=manifest_message(m))
    return "0x" + signed.signature.hex().removeprefix("0x")


def manifest_signer(m: dict, signature: str) -> str | None:
    """The address that signed the manifest, or None if it cannot be recovered."""
    try:
        msg = encode_typed_data(domain_data=domain(), message_types=MANIFEST_TYPES, message_data=manifest_message(m))
        return Account.recover_message(msg, signature=bytes.fromhex(str(signature).removeprefix("0x")))
    except Exception:  # noqa: BLE001 — malformed fields or signature: no signer
        return None


# ---- seller key registry ----

def register_key(seller_id: str, address: str, by: str = "seller", now: float | None = None) -> dict:
    """The seller's manifest key (public part only). Idempotent; a revoked key is never re-activated."""
    address, now = Web3.to_checksum_address(address), time.time() if now is None else now
    with db.immediate() as c:
        row = c.execute("SELECT * FROM seller_keys WHERE seller_id=? AND address=?", (seller_id, address)).fetchone()
        if row:
            return dict(row)
        c.execute("INSERT INTO seller_keys (seller_id, address, registered_at) VALUES (?,?,?)",
                  (seller_id, address, now))
        journal.append(by, "seller_key_registered", {"seller_id": seller_id, "address": address})
    return db.one("SELECT * FROM seller_keys WHERE seller_id=? AND address=?", (seller_id, address))


def revoke_key(seller_id: str, address: str, by: str, now: float | None = None):
    now = time.time() if now is None else now
    with db.immediate():
        db.run("UPDATE seller_keys SET revoked_at=? WHERE seller_id=? AND address=? AND revoked_at IS NULL",
               (now, seller_id, Web3.to_checksum_address(address)))
        journal.append(by, "seller_key_revoked", {"seller_id": seller_id, "address": address})


def key_valid(seller_id: str, address: str | None, at: float) -> bool:
    """Was this key the seller's at the moment the bank received the delivery?"""
    if not address:
        return False
    row = db.one("SELECT * FROM seller_keys WHERE seller_id=? AND address=?",
                 (seller_id, Web3.to_checksum_address(address)))
    return bool(row) and row["registered_at"] <= at and (row["revoked_at"] is None or at < row["revoked_at"])
