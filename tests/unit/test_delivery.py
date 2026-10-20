# Delivery through the bank (MODEL_V2 §5.2): the seller's key at the time of receipt, deal and offer binding, the
# bank's own root, quotas, versions (P never decreases, unique and max_count across versions), only accepted items
# reach the buyer with proofs, "accept anyway" only by a person within the lock.
import time

import pytest
from eth_account import Account

from bank import db, deals, disputes, receipts
from seller.app import company_items
from tests.unit import dealkit as k

T0 = float(int(time.time()))


@pytest.fixture
def w(monkeypatch, seeded):
    return k.world(monkeypatch)


def send(w, deal_id, payload, now=None):
    return deals.record_delivery(deal_id, payload, now=T0 + 1 if now is None else now)


def test_unregistered_or_revoked_key_is_bad_signature(w):
    deal_id = k.make_deal(w, now=T0)
    items = company_items(20, "honest")
    stranger = Account.create()
    out = send(w, deal_id, k.signed(w, deal_id, items, key=stranger))
    assert out == {**out, "accepted": False, "code": "BAD_SIGNATURE"}
    receipts.revoke_key("s-beta", w.key.address, "human:compliance", now=T0)
    assert send(w, deal_id, k.signed(w, deal_id, items))["code"] == "BAD_SIGNATURE"
    assert db.one("SELECT COUNT(*) n FROM items WHERE deal_id=?", (deal_id,))["n"] == 0


def test_manifest_for_another_deal_or_offer_is_wrong_deal(w):
    deal_id = k.make_deal(w, now=T0)
    p = k.signed(w, deal_id, company_items(20, "honest"))
    other = {**p["manifest"], "offer_hash": receipts.offer_hash({"offer_id": "o-other"})}
    out = send(w, deal_id, {**p, "manifest": other, "signature": receipts.sign_manifest(other, w.key.key.hex())})
    assert out["code"] == "WRONG_DEAL"


def test_root_mismatch_returns_the_banks_root_and_the_seller_may_resend(w):
    deal_id = k.make_deal(w, now=T0)
    items = company_items(20, "honest")
    p = k.signed(w, deal_id, items)
    tampered = {**p, "items": items[:19] + [{**items[19], "name": "Other s.r.o."}]}
    out = send(w, deal_id, tampered)
    assert out["code"] == "ROOT_MISMATCH" and out["bank_root"] == receipts.merkle_root(tampered["items"])
    assert send(w, deal_id, p)["accepted"]  # the same version again, now matching


def test_quotas_root_resends_versions_and_count(w):
    deal_id = k.make_deal(w, now=T0)
    items = company_items(20, "honest")
    bad = {**k.signed(w, deal_id, items), "items": items[::-1]}
    for _ in range(3):
        assert send(w, deal_id, bad)["code"] == "ROOT_MISMATCH"
    assert send(w, deal_id, k.signed(w, deal_id, items))["code"] == "QUOTA_EXCEEDED"
    many = k.make_deal(w, deal_id="d-many", now=T0, count=2, task_hash="t-many")
    assert send(w, many, k.signed(w, many, company_items(3, "honest")))["code"] == "QUOTA_EXCEEDED"


def test_versions_replace_failed_items_p_never_decreases(w):
    deal_id = k.make_deal(w, now=T0)
    v1 = company_items(20, "fake_data")
    assert send(w, deal_id, k.signed(w, deal_id, v1))["accepted_items"] == 17
    assert send(w, deal_id, k.signed(w, deal_id, v1, version=1, nonce="0x" + "ab" * 32))["code"] == "WRONG_DEAL"
    spare = company_items(23, "honest")[20:]
    v2 = [v1[0]] + spare  # a repeat of an accepted item + three replacements
    out = send(w, deal_id, k.signed(w, deal_id, v2, version=2))
    assert [i["code"] or i["status"] for i in out["items"]] == ["DUPLICATE", "accepted", "accepted", "accepted"]
    assert out["accepted_items"] == 20 and disputes.payable(deals.get(deal_id)) == 5_000_000
    v3 = company_items(25, "honest")[23:]
    out = send(w, deal_id, k.signed(w, deal_id, v3, version=3))
    assert [i["code"] for i in out["items"]] == ["CRITERIA_FAIL", "CRITERIA_FAIL"]  # never more than 20 paid
    assert send(w, deal_id, k.signed(w, deal_id, v3[:1], version=4))["code"] == "QUOTA_EXCEEDED"
    assert send(w, deal_id, k.signed(w, deal_id, v3, version=5))["code"] == "QUOTA_EXCEEDED"


def test_version_gap_is_refused(w):
    deal_id = k.make_deal(w, now=T0)
    assert send(w, deal_id, k.signed(w, deal_id, company_items(2, "honest"), version=2))["code"] == "WRONG_DEAL"


def test_the_buyer_gets_only_accepted_items_with_proofs(w):
    deal_id = k.make_deal(w, now=T0)
    send(w, deal_id, k.signed(w, deal_id, company_items(20, "fake_data")))
    got = receipts.fetch(deal_id)
    assert len(got["items"]) == 17 and got["rejected_items"] == 3
    root = got["manifests"][0]["merkle_root"]
    assert all(receipts.verify_proof(i["item"], i["index"], i["proof"], root) for i in got["items"])
    assert "Praha" not in str(got["items"])


def test_accept_anyway_only_a_person_within_the_lock(w):
    deal_id = k.make_deal(w, now=T0)
    send(w, deal_id, k.signed(w, deal_id, company_items(20, "fake_data")))
    rejected = [(r["version"], r["idx"]) for r in receipts.items_of(deal_id, "rejected")]
    with pytest.raises(receipts.Refused):
        receipts.waive(deal_id, rejected[:1], "agent:buyer", 20)
    assert receipts.waive(deal_id, rejected[:1], "human:anna", 20) == 1
    assert receipts.counts(deal_id)["accepted_items"] == 18
    assert any(e["event"] == "delivery.accepted_anyway" for e in __import__("bank").journal.entries(deal_id))


def test_personal_data_items_are_not_stored_and_cannot_be_accepted(w):
    deal_id = k.make_deal(w, now=T0)
    items = company_items(20, "honest")
    items[0] = {**items[0], "phone": "+420 111 222 333"}
    out = send(w, deal_id, k.signed(w, deal_id, items))
    assert out["items"][0]["code"] == "SCHEMA_FAIL"
    assert "+420" not in db.one("SELECT item_json FROM items WHERE deal_id=? AND idx=0", (deal_id,))["item_json"]
    with pytest.raises(receipts.Refused):
        receipts.waive(deal_id, [(1, 0)], "human:anna", 20)


def test_registry_outage_keeps_items_pending_then_the_timer_decides(w, monkeypatch):
    from bank import deadlines, verifier
    deal_id = k.make_deal(w, now=T0)
    down = {"on": True}
    real = verifier.registry_record

    def flaky(ico):
        if down["on"]:
            raise verifier.Pending("ARES timeout")
        return real(ico)
    monkeypatch.setattr(verifier, "registry_record", flaky)
    out = send(w, deal_id, k.signed(w, deal_id, company_items(20, "honest")))
    assert out["pending_items"] == 20 and k.captures(w) == []
    deadlines.tick(now=T0 + 30)
    assert k.captures(w) == []  # pending items: no money moves, nothing is rejected
    down["on"] = False
    deadlines.tick(now=T0 + 31)
    assert receipts.counts(deal_id)["accepted_items"] == 20 and k.captures(w) == [4_500_000]


def test_registry_wait_moves_the_delivery_deadline(w, monkeypatch):
    """§5.1: the seller cannot replace an item the bank has not decided, so the deadline moves by the wait."""
    from bank import deadlines, journal, verifier
    deal_id = k.make_deal(w, rule="A", deadline_s=100, now=T0)
    down = {"on": True}
    real = verifier.registry_record

    def flaky(ico):
        if down["on"]:
            raise verifier.Pending("ARES timeout")
        return real(ico)
    monkeypatch.setattr(verifier, "registry_record", flaky)
    assert send(w, deal_id, k.signed(w, deal_id, company_items(20, "honest")), now=T0 + 50)["pending_items"] == 20
    deadlines.tick(now=T0 + 120)  # past the agreed deadline, items still waiting: it keeps moving
    s = disputes.get(deal_id)
    assert deadlines.delivery_deadline(s, T0 + 120) == T0 + 170 and k.captures(w) == []
    down["on"] = False
    deadlines.tick(now=T0 + 150)  # decided after 100 s of waiting: deadline = T0 + 100 + 100
    assert receipts.counts(deal_id)["accepted_items"] == 20
    assert deadlines.delivery_deadline(s, T0 + 150) == T0 + 200 and k.captures(w) == []  # rule A waits for it
    ev = [e for e in journal.entries(deal_id, limit=50) if e["event"] == "delivery.deadline_extended"]
    assert ev and ev[0]["payload"]["waited_s"] == 100
    late = send(w, deal_id, k.signed(w, deal_id, company_items(20, "honest"), version=2), now=T0 + 190)
    assert late["version"] == 2  # taken in: the deadline moved (its items are duplicates of version 1)
    with pytest.raises(deals.DealError, match="deadline"):
        send(w, deal_id, k.signed(w, deal_id, company_items(20, "honest"), version=3), now=T0 + 201)
    deadlines.tick(now=T0 + 201)
    assert k.captures(w) == [5_000_000]


def test_wait_extension_is_capped_at_five_business_days(w, monkeypatch):
    from bank import config, deadlines, verifier
    deal_id = k.make_deal(w, rule="A", deadline_s=100, now=T0)
    monkeypatch.setattr(verifier, "registry_record", lambda ico: (_ for _ in ()).throw(verifier.Pending("down")))
    send(w, deal_id, k.signed(w, deal_id, company_items(20, "honest")), now=T0 + 10)
    far = T0 + 100 * config.BUSINESS_DAY_S
    assert deadlines.waited(deal_id, far) == 5 * config.BUSINESS_DAY_S
    # items accepted anyway by a person do not count as waiting (their decided_at is the waiver)
    db.run("UPDATE items SET status='accepted', waived_by='human:anna', decided_at=? WHERE deal_id=?", (far, deal_id))
    assert deadlines.waited(deal_id, far) == 0
