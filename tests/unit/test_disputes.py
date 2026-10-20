# Rules A and B and the payment schedule (MODEL_V2 §7, §8.2) with a fake clock and a fake chain sender: the real
# delivery through the bank (signed manifest, predicate engine on synthetic ARES fixtures), the real escrow calls,
# one final void, B claims without investigation and their limits, no void on a failed capture (§8.4).
import time

import pytest

from bank import db, deadlines, deals, disputes, journal, receipts
from tests.unit import dealkit as k

T0 = float(int(time.time()))


@pytest.fixture
def w(monkeypatch, seeded):
    return k.world(monkeypatch)


def test_rule_b_captures_90_percent_at_acceptance_then_the_rest_at_window_end(w):
    deal_id = k.make_deal(w, now=T0)
    out = k.deliver(w, deal_id, now=T0 + 1)
    assert out["accepted"] and out["accepted_items"] == 20
    assert k.captures(w) == [4_500_000] and k.state(deal_id) == "delivered"
    assert deadlines.tick(now=T0 + 20) == []  # window (20 s from acceptance) still open: nothing moves
    deadlines.tick(now=T0 + 22)
    assert k.captures(w) == [4_500_000, 500_000] and "void" not in k.fns(w)
    assert k.state(deal_id) == "settled" and k.reservation(deal_id)["committed"] == 500
    assert any(e["actor"] == "timer" and e["event"] == "settlement.capture" for e in journal.entries(deal_id))


def test_rule_b_with_rejected_items_one_final_void(w):
    deal_id = k.make_deal(w, now=T0)
    out = k.deliver(w, deal_id, mode="fake_data", now=T0 + 1)
    assert [i["code"] for i in out["items"] if i["status"] != "accepted"] == \
        ["CRITERIA_FAIL", "CRITERIA_FAIL", "REGISTRY_CONTRADICTION"]  # no website, Praha, closed by ARES
    assert k.captures(w) == [3_825_000]  # 90 % of P = 17/20 of 5 €
    deadlines.tick(now=T0 + 30)
    assert k.fns(w) == ["capture", "capture", "void"] and k.captures(w)[-1] == 425_000
    view = disputes.money_view(deals.get(deal_id), disputes.get(deal_id))
    assert view == {"total": 5_000_000, "captured": 4_250_000, "returned": 750_000, "in_escrow": 0}
    r = k.reservation(deal_id)
    assert r["committed"] == r["amount"] == 425 and r["state"] == "committed"  # the rest released
    assert len(receipts.fetch(deal_id)["items"]) == 17  # rejected items never reach the buyer


def test_b_claim_refunds_up_to_x_percent_without_investigation(w):
    deal_id = k.make_deal(w, now=T0)
    k.deliver(w, deal_id, mode="fake_data", now=T0 + 1)
    out = disputes.file_b(deal_id, [[1, 0], [1, 1]], "wrong_data", "agent:buyer", now=T0 + 5)
    assert out["amount"] == 425_000  # two items are worth 500 000, capped at 10 % of P = 4 250 000
    deadlines.tick(now=T0 + 30)
    assert k.fns(w) == ["capture", "void"]  # 90 % already paid = P minus the refund: nothing more to capture
    view = disputes.money_view(deals.get(deal_id), disputes.get(deal_id))
    assert view["captured"] == 3_825_000 and view["returned"] == 1_175_000
    # the refunded items are blocked for this seller in this task (§5.2 p. 6)
    blocked = db.rows("SELECT unique_key FROM items WHERE deal_id=? AND refunded='B'", (deal_id,))
    assert len(blocked) == 2
    deal2 = k.make_deal(w, deal_id="d-two", now=T0 + 40)
    again = k.deliver(w, deal2, items=[r["item"] for r in receipts.items_of(deal_id)[:3]], now=T0 + 41)
    assert [i["code"] for i in again["items"]] == ["DUPLICATE"] * 3


@pytest.mark.parametrize("change,status", [
    ({"reason": "i_dont_like_it"}, 400), ({"items": [[1, 19]]}, 409), ({"items": []}, 400),
    ({"now": T0 + 25}, 409)])
def test_b_claim_checks(w, change, status):
    deal_id = k.make_deal(w, now=T0)
    k.deliver(w, deal_id, mode="fake_data", now=T0 + 1)  # item 1/19 is the closed company: rejected
    args = {"items": [[1, 0]], "reason": "closed", "now": T0 + 5, **change}
    with pytest.raises(disputes.DisputeError) as e:
        disputes.file_b(deal_id, args["items"], args["reason"], "agent:buyer", now=args["now"])
    assert e.value.status == status


def test_one_b_claim_per_deal_and_none_without_a_slot(w):
    deal_id = k.make_deal(w, now=T0)
    k.deliver(w, deal_id, now=T0 + 1)
    disputes.file_b(deal_id, [[1, 0]], "closed", "agent:buyer", now=T0 + 2)
    with pytest.raises(disputes.DisputeError):
        disputes.file_b(deal_id, [[1, 1]], "closed", "agent:buyer", now=T0 + 3)
    no_slot = k.make_deal(w, deal_id="d-noslot", now=T0, b_slot=0)
    k.deliver(w, no_slot, now=T0 + 1)
    with pytest.raises(disputes.DisputeError, match="without claims"):
        disputes.file_b(no_slot, [[1, 0]], "closed", "agent:buyer", now=T0 + 2)


def test_b_slots_are_reserved_at_pay_new_buyer_gets_two(w):
    """15 parallel deals of a new buyer do not give 15 refunds: open B deals hold slots (§7B)."""
    assert disputes.b_availability("alfa", "s-beta", now=T0)["available"]
    k.make_deal(w, deal_id="d-b1", now=T0)
    assert disputes.b_availability("alfa", "s-gama", now=T0)["available"]
    k.make_deal(w, deal_id="d-b2", now=T0)
    a = disputes.b_availability("alfa", "s-gama", now=T0)
    assert not a["available"] and a["reason"] == "b_pair_limit" or a["reason"] == "b_new_buyer_limit"
    assert not disputes.b_availability("alfa", "s-beta", now=T0)["available"]


def test_rule_a_pays_at_the_deadline_or_when_a_person_accepts(w):
    deal_id = k.make_deal(w, rule="A", now=T0)
    k.deliver(w, deal_id, mode="fake_data", now=T0 + 1)
    assert k.captures(w) == []  # nothing at acceptance under rule A
    with pytest.raises(disputes.DisputeError):
        disputes.accept(deal_id, "agent:buyer", now=T0 + 2)  # only a person
    disputes.accept(deal_id, "human:anna", now=T0 + 2)
    assert k.fns(w) == ["capture", "void"] and k.captures(w) == [4_250_000]
    other = k.make_deal(w, rule="A", deal_id="d-a2", now=T0, task_hash="t-2")  # same items, another task
    k.deliver(w, other, now=T0 + 1)
    deadlines.tick(now=T0 + 299)
    assert len(k.captures(w)) == 1
    deadlines.tick(now=T0 + 301)  # the delivery deadline
    assert k.captures(w)[-1] == 5_000_000 and k.state(other) == "settled"


def test_no_delivery_by_the_deadline_returns_everything(w):
    deal_id = k.make_deal(w, now=T0)
    deadlines.tick(now=T0 + 301)
    assert k.fns(w) == ["void"] and disputes.get(deal_id)["outcome"] == "no_delivery"
    assert any(e["event"] == "autocode" and e["payload"]["code"] == "NO_DELIVERY" for e in journal.entries(deal_id))
    assert k.reservation(deal_id)["state"] == "released"


def test_a_failed_capture_is_retried_never_voided(w):
    from bank import chain
    deal_id = k.make_deal(w, now=T0)
    w.sender.fail_always = chain.TxWouldRevert("execution reverted: NotAllowlisted")
    k.deliver(w, deal_id, now=T0 + 1)
    deadlines.tick(now=T0 + 30)
    deadlines.tick(now=T0 + 31)
    assert w.sender.sent == [] and disputes.get(deal_id)["pending"]["kind"] == "capture"
    assert k.state(deal_id) == "delivered"  # still locked for the seller, not returned
    w.sender.fail_always = None
    deadlines.tick(now=T0 + 32)
    assert k.captures(w) == [4_500_000, 500_000]


def test_a_lost_answer_resends_the_same_key_and_books_once(w):
    deal_id = k.make_deal(w, now=T0)
    w.sender.fail_next = TimeoutError("rpc timeout")
    k.deliver(w, deal_id, now=T0 + 1)  # the capture went out, its answer was lost
    s = disputes.get(deal_id)
    assert s["pending"]["n"] == 1 and s["captured"] == 0
    deadlines.tick(now=T0 + 2)
    deadlines.tick(now=T0 + 2)
    assert k.captures(w) == [4_500_000] and disputes.get(deal_id)["captured"] == 4_500_000
    assert k.reservation(deal_id)["committed"] == 450


def test_no_new_version_after_the_final_void_or_the_deadline(w):
    deal_id = k.make_deal(w, now=T0)
    k.deliver(w, deal_id, mode="fake_data", now=T0 + 1)
    deadlines.tick(now=T0 + 30)
    with pytest.raises(deals.DealError):
        k.deliver(w, deal_id, count=3, version=2, now=T0 + 31)
    late = k.make_deal(w, deal_id="d-late", now=T0)
    with pytest.raises(deals.DealError, match="deadline"):
        k.deliver(w, late, now=T0 + 301)


def test_alarm_once_when_an_open_deal_is_near_authorization_expiry(w):
    deal_id = k.make_deal(w, now=time.time())
    pi = deals.get(deal_id)["payment_info"]
    pi["authorizationExpiry"] = int(time.time()) + 1800
    db.run("UPDATE deals SET payment_info_json=? WHERE deal_id=?", (db.dumps(pi), deal_id))
    first = deadlines.alarms(time.time())
    assert first and first[0]["deal_id"] == deal_id
    assert deadlines.alarms(time.time()) == []
    e = next(e for e in journal.entries(deal_id) if e["event"] == "escrow_expiry_alarm")
    assert e["actor"] == "timer" and "1 h" in e["payload"]["rule"]


def test_one_failing_deal_never_stops_the_others_and_raises_one_stuck_alarm(w, monkeypatch):
    from bank import ui
    monkeypatch.setattr(deadlines, "_fails", {})
    bad = k.make_deal(w, deal_id="d-a-bad", task_hash="t-bad", now=T0)  # open_ids() is sorted: it runs first
    good = k.make_deal(w, deal_id="d-b-good", task_hash="t-good", now=T0)
    k.deliver(w, good, now=T0 + 1)
    real = disputes.tick
    monkeypatch.setattr(disputes, "tick", lambda deal_id, now=None: (
        (_ for _ in ()).throw(RuntimeError("rpc down")) if deal_id == bad else real(deal_id, now=now)))
    deadlines.tick(now=T0 + 22)
    assert k.state(good) == "settled" and k.captures(w) == [4_500_000, 500_000]
    assert deadlines.tick(now=T0 + 23) == []
    assert deadlines.tick(now=T0 + 24) == [{"deal_id": bad, "alarm": "settlement.stuck"}]
    deadlines.tick(now=T0 + 25)
    events = [e["event"] for e in journal.entries(bad)]
    assert events.count("timer.deal_failed") == 4 and events.count("settlement.stuck") == 1
    failed = next(e for e in journal.entries(bad) if e["event"] == "timer.deal_failed")
    assert failed["actor"] == "timer" and failed["payload"] == {"attempt": 4, "error": "RuntimeError: rpc down"}
    assert [a for a in ui.alarms(T0) if a.get("stuck")] == [{"deal_id": bad, "state": "authorized", "stuck": True}]
    assert any(r.get("event") == "settlement.stuck" for r in ui.feed("client"))


def test_a_deal_that_recovers_starts_counting_again(w, monkeypatch):
    monkeypatch.setattr(deadlines, "_fails", {})
    deal_id = k.make_deal(w, now=T0)
    real, fail = disputes.tick, {"on": True}
    monkeypatch.setattr(disputes, "tick", lambda d, now=None: (
        (_ for _ in ()).throw(RuntimeError("x")) if fail["on"] else real(d, now=now)))
    deadlines.tick(now=T0 + 1), deadlines.tick(now=T0 + 2)
    fail["on"] = False
    deadlines.tick(now=T0 + 3)
    fail["on"] = True
    deadlines.tick(now=T0 + 4), deadlines.tick(now=T0 + 5)
    assert "settlement.stuck" not in [e["event"] for e in journal.entries(deal_id)]
