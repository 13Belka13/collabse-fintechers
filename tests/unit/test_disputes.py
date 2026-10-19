# Disputes + the timer with a fake clock (SPEC §9, layer L5). The real verifier (MOCK judge) decides the items,
# the real escrow.capture/void build the calls, a fake chain sender records them (idempotent per idem key like
# chain.Sender). N13 buyer silent -> capture, N14 seller silent -> void of that part, holdback released after
# its window, capture of everything owed before the final void, no double capture on repeated ticks.
import json
import time
import types

import pytest

from bank import chain, db, deadlines, disputes, escrow, journal, llm, mandate
from bank import verifier  # noqa: F401  (imported first, so its judge mock does not replace the test's)
from seller.app import DEMO_DISPUTE_TERMS, make_items

ADDR = "0x491BeCCB4E26E1c5781E1E44c48Ad474D04d2140"
BETA = "0x70E8B0ed46DC4FD558ee05bCEba06e5AAf00EBd1"
EURC = "0x808456652fdb597867f38412077A9182bf77359F"
T0 = float(int(time.time()))  # the fake clock starts now (escrow expiry is real time + 24 h); tests move it


class FakeSender:
    """Same idem key -> same tx, nothing new on chain (what chain.Sender guarantees)."""

    def __init__(self):
        self.sent, self.by_key, self.fail_next = [], {}, None

    def send(self, call, **kw):
        key = kw["idem_key"]
        if key in self.by_key:
            return self.by_key[key]
        self.sent.append((call.fn_name, call.args[1] if call.fn_name == "capture" else None, key))
        tx = {"tx_hash": "0x%064x" % len(self.sent), "link": chain.tx_link("0x%064x" % len(self.sent)), "status": "mined"}
        self.by_key[key] = tx
        if self.fail_next:  # the tx went out but the answer was lost (RPC timeout)
            err, self.fail_next = self.fail_next, None
            raise err
        return tx


@pytest.fixture
def world(monkeypatch, seeded):
    sender = FakeSender()
    monkeypatch.setattr(chain, "sender", lambda pk: sender)
    flagged: set[str] = set()

    def judge(messages, tools):  # MOCK judge: everything plausible except the ids the test flags
        batch = json.loads(messages[0]["content"].split("<items>", 1)[1].split("</items>", 1)[0])
        return {"text": json.dumps([{"placeId": it["placeId"], "plausible_warehouse": it["placeId"] not in flagged,
                                     "reason": "Looks like a café, not storage." if it["placeId"] in flagged
                                     else "Storage business."} for it in batch])}
    monkeypatch.setitem(llm.MOCKS, "judge", judge)
    mandate.ensure_demo()
    return types.SimpleNamespace(sender=sender, flagged=flagged)


def make_deal(mode="honest", deal_id="d-test", expiry_in=86400, state="delivered"):
    offer = {"offer_id": "o-1", "count": 20, "prices": {"EURC": "5000000"}, "dispute_terms": DEMO_DISPUTE_TERMS,
             "criteria": {"count": 20, "city": "Brno", "min_rating_exclusive": 4.0}}
    now = int(time.time())
    pi = {"operator": ADDR, "payer": ADDR, "receiver": BETA, "token": EURC, "maxAmount": 5_000_000,
          "preApprovalExpiry": now + 600, "authorizationExpiry": now + expiry_in, "refundExpiry": now + 7 * 86400,
          "minFeeBps": 0, "maxFeeBps": 0, "feeReceiver": "0x" + "0" * 40, "salt": "1"}
    db.run("INSERT INTO deals (deal_id, buyer_id, seller_id, mandate_id, offer_json, criteria_json, asset, amount,"
           " payment_info_json, payment_hash, state, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
           (deal_id, "alfa", "s-beta", "m-alfa-1", db.dumps(offer), db.dumps(offer["criteria"]), "EURC", 5_000_000,
            db.dumps(pi), escrow.payment_hash(pi), state, time.time()))
    db.run("INSERT INTO deliveries (deal_id, items_json, created_at) VALUES (?,?,?)",
           (deal_id, db.dumps(make_items(20, mode)), time.time()))
    assert mandate.reserve("m-alfa-1", deal_id, 500)["status"] == "ok"
    return deal_id


def captures(w):
    # (amount, "capture:n"): the idem key's suffix, whatever prefix escrow puts before it
    return [(amount, ":".join(key.split(":")[-2:])) for fn, amount, key in w.sender.sent if fn == "capture"]


def fns(w):
    return [fn for fn, _, _ in w.sender.sent]


def timer_events(deal_id):
    return [e for e in journal.entries(deal_id, limit=1000) if e["actor"] == "timer"]


def reservation(deal_id):
    return db.one("SELECT * FROM reservations WHERE deal_id=?", (deal_id,))


def state(deal_id):
    return db.one("SELECT state FROM deals WHERE deal_id=?", (deal_id,))["state"]


def test_honest_deal_pays_minus_holdback_then_releases_it(world):
    deal_id = make_deal("honest")
    d = disputes.settle(deal_id, now=T0)
    assert d["items"] == [] and d["holdback"] == 250_000  # 5 % of 5,00 €
    assert captures(world) == [(4_750_000, "capture:1")]
    assert state(deal_id) == "settled"
    assert deadlines.tick(now=T0 + 299) == []  # holdback window not over yet
    deadlines.tick(now=T0 + 301)
    assert captures(world)[-1] == (250_000, "capture:2")
    assert "void" not in fns(world)  # everything went to the seller: nothing to return
    assert state(deal_id) == "closed"
    assert reservation(deal_id)["committed"] == 500
    e = next(e for e in timer_events(deal_id) if e["event"] == "dispute.holdback_released")
    assert "holdback_s" in e["payload"]["rule"]


def test_n14_seller_silent_after_rejection_voids_that_part(world):
    deal_id = make_deal("fake_data")  # 2 of 20 break the rules (no website, wrong city)
    d = disputes.settle(deal_id, now=T0)
    assert [it["origin"] for it in d["items"]] == ["rules", "rules"] and d["step"] == "objection"
    assert d["holdback"] == 225_000 and captures(world) == [(4_275_000, "capture:1")]
    assert state(deal_id) == "disputed"
    deadlines.tick(now=T0 + 119)
    assert len(world.sender.sent) == 1  # objection window still open
    deadlines.tick(now=T0 + 121)  # seller silent -> rejection stands; holdback still running -> no void yet
    d = disputes.get(deal_id)
    assert all(it["outcome"] == "rejected" and it["decided_by"] == "timer" for it in d["items"])
    assert fns(world) == ["capture"]
    deadlines.tick(now=T0 + 301)  # holdback over: capture it, THEN void the rejected part
    assert fns(world) == ["capture", "capture", "void"]
    assert captures(world)[-1] == (225_000, "capture:2")
    view = disputes.money_view({"amount": 5_000_000}, disputes.get(deal_id))
    assert view == {"total": 5_000_000, "captured": 4_500_000, "returned": 500_000, "in_escrow": 0, "holdback": 0}
    rules = [e["payload"]["rule"] for e in timer_events(deal_id) if e["event"] == "dispute.default_applied"]
    assert rules == ["on_seller_silence: rejection_stands"] * 2
    assert reservation(deal_id)["committed"] == 450 and reservation(deal_id)["amount"] == 450  # rest released
    assert state(deal_id) == "closed"


def test_n13_buyer_silent_on_flagged_items_captures_them(world):
    world.flagged.update({"demo-place-03", "demo-place-04"})
    deal_id = make_deal("honest")
    d = disputes.settle(deal_id, now=T0)
    assert [(it["origin"], it["step"]) for it in d["items"]] == [("judge", "review")] * 2
    assert d["items"][0]["judge"]["ai_generated"] is True
    assert captures(world) == [(4_275_000, "capture:1")]  # 18 accepted minus 5 % holdback
    deadlines.tick(now=T0 + 61)  # buyer silent -> accepted -> paid at once
    assert captures(world)[-1] == (500_000, "capture:2")
    e = [e for e in timer_events(deal_id) if e["event"] == "dispute.default_applied"]
    assert {x["payload"]["rule"] for x in e} == {"on_buyer_silence: accept"}
    assert any(x["event"] == "dispute.capture" and x["actor"] == "timer" for x in timer_events(deal_id))
    deadlines.tick(now=T0 + 301)
    assert captures(world)[-1] == (225_000, "capture:3") and "void" not in fns(world)
    assert sum(a for a, _ in captures(world)) == 5_000_000


def test_two_ticks_never_capture_twice_and_a_lost_answer_resends_the_same_key(world):
    world.flagged.add("demo-place-05")
    deal_id = make_deal("honest")
    disputes.settle(deal_id, now=T0)
    world.sender.fail_next = TimeoutError("rpc timeout")
    deadlines.tick(now=T0 + 61)  # tx went out, answer lost: pending kept, not booked
    d = disputes.get(deal_id)
    assert d["pending"] == {"kind": "capture", "amount": 250_000, "n": 2} and d["captured"] == 4_512_500
    deadlines.tick(now=T0 + 62)
    deadlines.tick(now=T0 + 62)
    assert captures(world) == [(4_512_500, "capture:1"), (250_000, "capture:2")]
    assert disputes.get(deal_id)["captured"] == 4_762_500 and disputes.get(deal_id)["pending"] is None
    assert reservation(deal_id)["committed"] == 477  # 4,7625 € rounded up once, never more than reserved


def test_buyer_rejection_needs_a_criterion_and_evidence_then_seller_objects_and_bank_decides(world):
    world.flagged.update({"demo-place-03", "demo-place-04"})
    deal_id = make_deal("honest")
    disputes.settle(deal_id, now=T0)
    with pytest.raises(disputes.DisputeError) as e:
        disputes.buyer_review(deal_id, "demo-place-03", "reject", "human:alfa", criterion="i_dont_like_it",
                              evidence="ugly", now=T0 + 5)
    assert e.value.status == 400
    with pytest.raises(disputes.DisputeError):
        disputes.buyer_review(deal_id, "demo-place-03", "reject", "human:alfa", criterion="category", evidence="",
                              now=T0 + 5)
    d = disputes.buyer_review(deal_id, "demo-place-03", "reject", "human:alfa", criterion="category",
                              evidence="The place is a café: https://maps.example/place/demo-place-03", now=T0 + 5)
    it = next(i for i in d["items"] if i["placeId"] == "demo-place-03")
    assert it["step"] == "objection" and it["rejection"]["by"] == "human:alfa"
    assert it["deadline_at"] == T0 + 5 + DEMO_DISPUTE_TERMS["objection_window_s"]
    disputes.buyer_review(deal_id, "demo-place-04", "accept", "human:alfa", now=T0 + 6)
    assert captures(world)[-1] == (250_000, "capture:2")  # accepted item paid at once
    disputes.seller_objection(deal_id, "demo-place-03", "It is a storage hall, see the photos.", "human:beta",
                              now=T0 + 10)
    assert disputes.get(deal_id)["seller_statement"].startswith("It is a storage hall")
    with pytest.raises(disputes.DisputeError) as e:  # wrong step: the bank decides only after the objection
        disputes.buyer_review(deal_id, "demo-place-03", "accept", "human:alfa", now=T0 + 11)
    assert e.value.status == 409
    disputes.decide(deal_id, "demo-place-03", "accept", "human:bank-officer", note="Photos convince.", now=T0 + 20)
    assert captures(world)[-1] == (250_000, "capture:3")
    actors = {e["actor"] for e in journal.entries(deal_id, limit=1000) if e["event"].startswith("dispute.")}
    assert {"human:alfa", "human:beta", "human:bank-officer"} <= actors
    deadlines.tick(now=T0 + 301)
    assert sum(a for a, _ in captures(world)) == 5_000_000 and "void" not in fns(world)


def test_decision_timeout_applies_the_rules_verdict(world):
    deal_id = make_deal("fake_data")
    disputes.settle(deal_id, now=T0)
    for pid in ("demo-place-19", "demo-place-20"):
        disputes.seller_objection(deal_id, pid, "The item meets the order.", "human:beta", now=T0 + 10)
    deadlines.tick(now=T0 + 10 + 121)  # nobody decided: the rules say both broke the agreed criteria
    d = disputes.get(deal_id)
    assert [it["rule"] for it in d["items"]] == ["on_decision_timeout: rules_verdict"] * 2
    assert all(it["outcome"] == "rejected" for it in d["items"])


def test_deadlines_never_pass_authorization_expiry_minus_guard(world):
    deal_id = make_deal("fake_data", expiry_in=200)  # escrow expires in 200 s; holdback window is 300 s
    d = disputes.settle(deal_id, now=time.time())
    expiry = disputes.deals.get(deal_id)["payment_info"]["authorizationExpiry"]
    assert all(it["deadline_at"] <= expiry - 30 for it in d["items"])
    assert d["holdback_until"] <= expiry - 30


def test_alarm_once_when_an_open_deal_is_near_authorization_expiry(world):
    deal_id = make_deal("honest", expiry_in=1800)
    disputes.settle(deal_id, now=time.time())
    first = deadlines.alarms(time.time())
    assert first and first[0]["deal_id"] == deal_id
    assert deadlines.alarms(time.time()) == []
    e = next(e for e in journal.entries(deal_id) if e["event"] == "escrow_expiry_alarm")
    assert e["actor"] == "timer" and "1 h" in e["payload"]["rule"]


def test_timer_adopts_a_deal_that_deals_settle_left_verified(world):
    world.flagged.add("demo-place-07")
    deal_id = make_deal("honest")
    from bank import verifier
    plan = verifier.settlement(deal_id)
    escrow.capture(deal_id, plan["capture_now"], 1)  # what deals.settle does today when held > 0
    db.run("UPDATE deals SET state='verified' WHERE deal_id=?", (deal_id,))
    done = deadlines.tick(now=T0)
    assert {"deal_id": deal_id, "opened": True} in done
    d = disputes.get(deal_id)
    assert d["captured"] == 4_750_000 and d["capture_n"] == 1 and d["holdback"] == 0
    deadlines.tick(now=T0 + 61)
    assert captures(world)[-1] == (250_000, "capture:2")
    assert state(deal_id) == "closed"


def test_human_actor_is_required(world):
    world.flagged.add("demo-place-02")
    deal_id = make_deal("honest")
    disputes.settle(deal_id, now=T0)
    with pytest.raises(disputes.DisputeError):
        disputes.buyer_review(deal_id, "demo-place-02", "accept", "agent:buyer", now=T0 + 1)
