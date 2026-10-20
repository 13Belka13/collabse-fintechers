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


def jcs(value, nfc: bool = True) -> bytes:
    """RFC 8785 canonical bytes. Raises FormatError for anything outside I-JSON. nfc=False only for the tree:
    a non-NFC string still has a leaf (the item then fails SCHEMA_FAIL), a number JCS cannot write has none."""
    problem = ijson_problem(value)
    if problem and (nfc or "NFC" not in problem):
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
    return sha256(b"\x00" + index.to_bytes(8, "big") + jcs(item, nfc=False))


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


# ---- delivery through the bank (§5.2): one event = delivery + receipt; the buyer gets only accepted items ----

MAX_VERSIONS, MAX_ROOT_RETRIES = 3, 3


class Refused(Exception):
    """A delivery the bank does not take (state, deadline, after a C claim) — an HTTP error, not an autocode."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def proof_from_leaves(leaves: list[bytes], index: int) -> list[dict]:
    levels, out, i = _levels(leaves), [], index
    for level in levels[:-1]:
        sib = i ^ 1
        if sib < len(level):
            out.append({"side": "left" if sib < i else "right", "hash": hex32(level[sib])})
        i //= 2
    return out


def _attempt(deal_id, version, now, code, detail, manifest=None, signature=None, signer=None, bank_root=None,
             accepted=False):
    db.run("INSERT INTO delivery_attempts (deal_id, version, received_at, code, detail, manifest_json, signature,"
           " signer, bank_root, accepted) VALUES (?,?,?,?,?,?,?,?,?,?)",
           (deal_id, version, now, code, detail, db.dumps(manifest) if manifest else None, signature, signer,
            bank_root, int(accepted)))
    journal.append("bank", "delivery.accepted" if accepted else "delivery.refused",
                   {"version": version, "code": code, "detail": detail, "bank_root": bank_root,
                    "source": "untrusted:seller"}, deal_id=deal_id)


def _keys(deal_id: str, task_hash: str | None, seller_id: str, skip: tuple | None = None) -> tuple[set, set, int]:
    """(unique keys already taken in this deal or this task, keys this seller refunded in this task, items taken
    in this deal). Pending items hold their key and slot until they are decided."""
    skip = skip or ("", -1, -1)
    taken = db.rows("SELECT deal_id, version, idx, unique_key FROM items WHERE status IN ('accepted','pending')"
                    " AND (deal_id=? OR (task_hash IS NOT NULL AND task_hash=?))", (deal_id, task_hash))
    taken = [r for r in taken if (r["deal_id"], r["version"], r["idx"]) != skip]
    blocked = {r["unique_key"] for r in db.rows(
        "SELECT unique_key FROM items WHERE task_hash=? AND seller_id=? AND refunded IS NOT NULL",
        (task_hash, seller_id))}
    return {r["unique_key"] for r in taken}, blocked, sum(1 for r in taken if r["deal_id"] == deal_id)


def receive(deal: dict, payload: dict, now: float | None = None, registry=None, snapshot=None) -> dict:
    """Seller -> bank: {manifest, signature, items}. The bank's journal time is the delivery time. Returns the
    bank's answer to the seller: accepted, or a delivery code (BAD_SIGNATURE, WRONG_DEAL, ROOT_MISMATCH with the
    bank's own root, QUOTA_EXCEEDED); per item what passed and why not."""
    from bank import verifier
    now = time.time() if now is None else now
    deal_id, terms = deal["deal_id"], deal["terms"]
    m, sig, items = payload.get("manifest") or {}, payload.get("signature") or "", payload.get("items")
    if not isinstance(items, list) or not isinstance(m, dict):
        raise Refused(400, "a delivery is {manifest, signature, items: [...]}")
    version = m.get("version") if isinstance(m.get("version"), int) else None
    attempts = db.rows("SELECT * FROM delivery_attempts WHERE deal_id=?", (deal_id,))
    accepted_versions = sorted(a["version"] for a in attempts if a["accepted"])

    def refuse(code, detail, **kw):
        _attempt(deal_id, version, now, code, detail, m, sig, **kw)
        return {"accepted": False, "code": code, "detail": detail, "version": version, **kw}

    if len(accepted_versions) >= MAX_VERSIONS:
        return refuse("QUOTA_EXCEEDED", f"at most {MAX_VERSIONS} versions per deal")
    if sum(1 for a in attempts if a["code"] == "ROOT_MISMATCH") >= MAX_ROOT_RETRIES:
        return refuse("QUOTA_EXCEEDED", f"at most {MAX_ROOT_RETRIES} resends after ROOT_MISMATCH")
    if len(items) > int(terms["count"]) or len(db.dumps(items).encode()) > MAX_DELIVERY_BYTES:
        return refuse("QUOTA_EXCEEDED", f"at most {terms['count']} items and 10 MB per version")
    signer = manifest_signer(m, sig)
    if not key_valid(deal["seller_id"], signer, now):
        return refuse("BAD_SIGNATURE", "not signed by a key the seller had registered at the time of receipt",
                      signer=signer)
    if m.get("deal_id") != deal_id or m.get("offer_hash") != offer_hash(deal["offer"]):
        return refuse("WRONG_DEAL", "the manifest names another deal or offer", signer=signer)
    if any(a["accepted"] and db.loads(a["manifest_json"]).get("nonce") == m.get("nonce") for a in attempts):
        return refuse("WRONG_DEAL", "nonce already used", signer=signer)
    if version in accepted_versions:
        return refuse("WRONG_DEAL", f"version {version} was already accepted", signer=signer)
    if version != (accepted_versions[-1] + 1 if accepted_versions else 1):
        return refuse("WRONG_DEAL", "versions go 1, 2, 3 without gaps", signer=signer)
    try:
        leaves = [leaf(i, it) for i, it in enumerate(items)]
        bank_root = hex32(_levels(leaves)[-1][0])
    except FormatError as e:
        return refuse("ROOT_MISMATCH", f"no canonical form: {e}", signer=signer)
    if m.get("count") != len(items) or str(m.get("merkle_root", "")).lower() != bank_root:
        return refuse("ROOT_MISMATCH", f"the bank counts {len(items)} items with root {bank_root}", signer=signer,
                      bank_root=bank_root)

    keys, blocked, taken = _keys(deal_id, terms.get("task_hash"), deal["seller_id"])
    snap = (lambda i, it, p: snapshot(deal, version, i, it, p)) if snapshot else None
    results = verifier.evaluate(items, terms["predicates"], ordered=int(terms["count"]), accepted_keys=keys,
                                blocked_keys=blocked, already_accepted=taken, registry=registry, snapshot=snap)
    with db.immediate() as c:
        _attempt(deal_id, version, now, None, f"{len(items)} items", m, sig, signer=signer, bank_root=bank_root,
                 accepted=True)
        for it, lf, r in zip(items, leaves, results):
            stored = {k: v for k, v in it.items() if k not in verifier.PERSONAL_FIELDS} if isinstance(it, dict) else it
            c.execute("INSERT INTO items (deal_id, version, idx, item_json, leaf, unique_key, status, code,"
                      " predicate_json, detail, task_hash, seller_id, received_at, decided_at)"
                      " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (deal_id, version, r["index"], db.dumps(stored), hex32(lf), r["unique_key"], r["status"],
                       r["code"], db.dumps(r["predicate"]) if r["predicate"] else None, r["detail"],
                       terms.get("task_hash"), deal["seller_id"], now, None if r["status"] == "pending" else now))
        journal.append("bank", "delivery.autocodes", {"version": version, **counts(deal_id)}, deal_id=deal_id)
    return {"accepted": True, "code": None, "version": version, "bank_root": bank_root,
            "items": [{k: r[k] for k in ("index", "status", "code", "detail")} for r in results], **counts(deal_id)}


def recheck_pending(deal: dict, now: float | None = None, registry=None, snapshot=None) -> int:
    """Pending items (registry or snapshot not answered yet): try again. Returns how many got decided."""
    from bank import verifier
    now, terms, done = time.time() if now is None else now, deal["terms"], 0
    for row in db.rows("SELECT * FROM items WHERE deal_id=? AND status='pending' ORDER BY version, idx",
                       (deal["deal_id"],)):
        keys, blocked, taken = _keys(deal["deal_id"], terms.get("task_hash"), deal["seller_id"],
                                     skip=(row["deal_id"], row["version"], row["idx"]))
        version, idx = row["version"], row["idx"]
        snap = (lambda i, it, p: snapshot(deal, version, idx, it, p)) if snapshot else None
        (r,) = verifier.evaluate([db.loads(row["item_json"])], terms["predicates"], ordered=int(terms["count"]),
                                 accepted_keys=keys, blocked_keys=blocked, already_accepted=taken, registry=registry,
                                 snapshot=snap)
        if r["status"] == "pending":
            continue
        with db.immediate():
            db.run("UPDATE items SET status=?, code=?, predicate_json=?, detail=?, unique_key=?, decided_at=?"
                   " WHERE deal_id=? AND version=? AND idx=? AND status='pending'",
                   (r["status"], r["code"], db.dumps(r["predicate"]) if r["predicate"] else None, r["detail"],
                    r["unique_key"], now, row["deal_id"], version, idx))
            journal.append("bank", "delivery.item_decided", {"version": version, "index": idx,
                                                             "status": r["status"], "code": r["code"]},
                           deal_id=deal["deal_id"])
        done += 1
    return done


def counts(deal_id: str) -> dict:
    by = {r["status"]: r["n"] for r in db.rows("SELECT status, COUNT(*) n FROM items WHERE deal_id=? GROUP BY status",
                                               (deal_id,))}
    return {"accepted_items": by.get("accepted", 0), "rejected_items": by.get("rejected", 0),
            "pending_items": by.get("pending", 0)}


def items_of(deal_id: str, status: str | None = None) -> list[dict]:
    sql, args = "SELECT * FROM items WHERE deal_id=?", [deal_id]
    if status:
        sql, args = sql + " AND status=?", [deal_id, status]
    rows = db.rows(sql + " ORDER BY version, idx", tuple(args))
    for r in rows:
        r["item"] = db.loads(r.pop("item_json"))
        r["predicate"] = db.loads(r.pop("predicate_json"))
    return rows


def waive(deal_id: str, keys: list[tuple[int, int]], by: str, ordered: int, now: float | None = None) -> int:
    """'Accept anyway' (§5.2 p. 7): only a human of the buyer, before the final void, within the lock (never more
    paid items than ordered). Items with personal data were never stored and cannot be accepted."""
    if not by.startswith("human:"):
        raise Refused(403, "only a person of the buyer can accept rejected items")
    now, done = time.time() if now is None else now, 0
    with db.immediate():
        room = ordered - counts(deal_id)["accepted_items"]
        for version, idx in keys:
            row = db.one("SELECT * FROM items WHERE deal_id=? AND version=? AND idx=?", (deal_id, version, idx))
            if row is None or row["status"] != "rejected" or "personal data" in (row["detail"] or ""):
                raise Refused(409, f"item {version}/{idx} cannot be accepted")
            if done >= room:
                raise Refused(409, f"at most {ordered} items are paid")
            db.run("UPDATE items SET status='accepted', waived_by=?, decided_at=? WHERE deal_id=? AND version=?"
                   " AND idx=?", (by, now, deal_id, version, idx))
            done += 1
        journal.append(by, "delivery.accepted_anyway", {"items": [list(k) for k in keys]}, deal_id=deal_id)
    return done


def fetch(deal_id: str) -> dict:
    """What the buyer receives: only the accepted items, each with its inclusion proof against the root of the
    seller-signed manifest of its version (anyone can check the seller delivered exactly this)."""
    manifests = {a["version"]: db.loads(a["manifest_json"]) for a in db.rows(
        "SELECT version, manifest_json FROM delivery_attempts WHERE deal_id=? AND accepted=1", (deal_id,))}
    leaves = {}
    for r in db.rows("SELECT version, idx, leaf FROM items WHERE deal_id=? ORDER BY version, idx", (deal_id,)):
        leaves.setdefault(r["version"], []).append(unhex32(r["leaf"]))
    out = [{"version": r["version"], "index": r["idx"], "item": r["item"], "leaf": r["leaf"],
            "proof": proof_from_leaves(leaves[r["version"]], r["idx"]), "refunded": r["refunded"]}
           for r in items_of(deal_id, "accepted")]
    return {"deal_id": deal_id, "items": out, **counts(deal_id),
            "manifests": [{"version": v, **{k: m[k] for k in ("merkle_root", "count", "offer_hash")}}
                          for v, m in sorted(manifests.items())]}
