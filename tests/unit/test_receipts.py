# Delivery receipt format (MODEL_V2 §5.1): I-JSON/NFC, JCS, the Merkle tree against hand-computed hashes and the
# published vectors, inclusion proofs, the EIP-712 manifest and the seller key registry.
import hashlib
import json

import pytest
from eth_account import Account

from bank import config, receipts

VECTORS = json.loads((config.ROOT / "fixtures" / "merkle_vectors.json").read_text(encoding="utf-8"))["vectors"]


def h(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def test_jcs_sorts_keys_and_formats_numbers():
    assert receipts.jcs({"b": 1, "a": [1e21, 0.1, -0.0, 100.0], "é": "é"}) == \
        '{"a":[1e+21,0.1,0,100],"b":1,"é":"é"}'.encode()


@pytest.mark.parametrize("bad", [2**53, -(2**53), float("nan"), float("inf"), "é", {"é": 1}, {1, 2}])
def test_outside_ijson_is_refused(bad):
    with pytest.raises(receipts.FormatError):
        receipts.jcs({"x": bad})


def test_big_numbers_travel_as_strings():
    assert receipts.jcs({"x": str(2**60)}) == b'{"x":"1152921504606846976"}'
    assert receipts.ijson_problem({"x": 2**53 - 1}) is None


def test_tree_by_hand_three_items_odd_node_promoted():
    items = [{"a": 1}, {"b": 2}, {"c": 3}]
    leaves = [h(b"\x00" + i.to_bytes(8, "big") + json.dumps(it, separators=(",", ":")).encode())
              for i, it in enumerate(items)]
    root = h(b"\x01" + h(b"\x01" + leaves[0] + leaves[1]) + leaves[2])  # leaf 2 promoted, not paired with itself
    assert receipts.merkle_root(items) == "0x" + root.hex()
    assert receipts.merkle_root(items) != receipts.merkle_root(items + [items[2]])


def test_leaf_index_binds_position():
    a, b = {"a": 1}, {"b": 2}
    assert receipts.merkle_root([a, b]) != receipts.merkle_root([b, a])


@pytest.mark.parametrize("v", VECTORS, ids=[v["name"] for v in VECTORS])
def test_published_vectors(v):
    assert [receipts.jcs(it).decode() for it in v["items"]] == v["jcs"]
    assert [receipts.hex32(receipts.leaf(i, it)) for i, it in enumerate(v["items"])] == v["leaves"]
    assert receipts.merkle_root(v["items"]) == v["root"]


def test_empty_delivery_has_no_root():
    with pytest.raises(receipts.FormatError):
        receipts.merkle_root([])


@pytest.mark.parametrize("n", [1, 2, 3, 5, 8, 13])
def test_inclusion_proofs(n):
    items = [{"i": i} for i in range(n)]
    root = receipts.merkle_root(items)
    for i in range(n):
        proof = receipts.merkle_proof(items, i)
        assert receipts.verify_proof(items[i], i, proof, root)
        assert not receipts.verify_proof({"i": -1}, i, proof, root)


def manifest(**kw):
    return {"deal_id": "d-1", "offer_hash": receipts.offer_hash({"offer_id": "o-1"}), "version": 1, "count": 2,
            "merkle_root": receipts.merkle_root([{"a": 1}, {"b": 2}]), "nonce": "0x" + "11" * 32, **kw}


def test_manifest_signature_recovers_the_seller_key():
    key = Account.create()
    m = manifest()
    sig = receipts.sign_manifest(m, key.key.hex())
    assert receipts.manifest_signer(m, sig) == key.address
    assert receipts.manifest_signer({**m, "version": 2}, sig) != key.address  # any field change breaks it
    assert receipts.manifest_signer({**m, "count": 3}, sig) != key.address
    assert receipts.manifest_signer(m, "0x1234") is None


def test_key_registry_validity_window():
    key = Account.create().address
    receipts.register_key("s-beta", key, now=100)
    assert not receipts.key_valid("s-beta", key, 99) and receipts.key_valid("s-beta", key, 100)
    assert not receipts.key_valid("s-gama", key, 150)  # another seller's key is not this seller's
    receipts.revoke_key("s-beta", key, "human:compliance", now=200)
    assert receipts.key_valid("s-beta", key, 199) and not receipts.key_valid("s-beta", key, 200)
    receipts.register_key("s-beta", key, now=300)  # a revoked key stays revoked
    assert not receipts.key_valid("s-beta", key, 301)
    assert not receipts.key_valid("s-beta", None, 150)
