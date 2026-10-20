# Rule C (MODEL_V2 §7C, §8.2) on a fake clock and a fake chain: the bank's own snapshot at delivery, admissibility
# at filing (bank-extracted field value, same type, different), no new versions after the claim, negotiation, fees
# PAID to the clearing address with their deadlines, the split-off of D at window end, the drand draw with a
# committed salt, the sample, the decision 3 of 5 -> 60 % of D back, execution 10 business days later, fees split.
import time

import pytest
from eth_account import Account

from bank import claims_c, config, core, db, deadlines, deals, deciders, disputes, escrow, journal, snapshots
from seller.app import company_items
from tests.unit import dealkit as k

T0 = float(int(time.time()))
BD = None  # set per test from config (demo business day in seconds)
C_PREDICATES = k.TERMS_PREDICATES[:4] + [
    {"op": "max_count", "n": 10}, {"op": "source", "template": "ares", "key_field": "ico"},
    {"op": "json_schema", "schema": {"type": "object", "properties": {"founded": {
        "type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"}}}}]
PRICE_C = 8_000_000  # 10 items for 8 € — T2 at the demo scale


@pytest.fixture
def w(monkeypatch, seeded):
    buyer, seller_wallet, operator = Account.create(), Account.create(), Account.create()
    monkeypatch.setenv("ALFA_PK", buyer.key.hex())
    monkeypatch.setenv("BANK_OPERATOR_PK", operator.key.hex())
    config.reload()
    db.run("UPDATE companies SET wallet_address=? WHERE id='alfa'", (buyer.address,))
    db.run("UPDATE companies SET wallet_address=? WHERE id='beta'", (seller_wallet.address,))
    world = k.world(monkeypatch)
    world.seller_wallet, world.notices = seller_wallet, []
    monkeypatch.setattr(deals, "notify_seller", lambda d, kind="deal", amount=None: world.notices.append(
        (d, kind, amount)) or True)
    return world


def c_deal(w, deal_id="d-c", now=T0):
    window = config.WINDOW_S["T2"][0]
    deal_id = k.make_deal(w, rule="C", deal_id=deal_id, count=10, predicates=C_PREDICATES, amount=PRICE_C,
                          window_s=window, deadline_s=300, now=now, task_hash=deal_id)
    out = k.deliver(w, deal_id, mode="misdated", count=10, now=now + 1)
    assert out["accepted_items"] == 10 and k.captures(w) == []  # rule C: nothing at acceptance
    return deal_id, window


def all_items(field="founded"):
    return [[1, i] for i in range(10)], {f"1/{i}": field for i in range(10)}


def test_snapshot_is_taken_at_delivery_from_the_template_url(w):
    deal_id, _ = c_deal(w)
    snap = snapshots.get(deal_id, 1, 0, "ares")
    item = disputes.receipts.items_of(deal_id)[0]["item"]
    assert snap["status"] == "ok" and snap["url"] == snapshots.build_url("ares", item["ico"])
    assert snap["fields"]["founded"] != item["founded"] and snap["body_hash"].startswith("0x")
    assert any(e["event"] == "snapshot.ok" for e in journal.entries(deal_id, limit=500))


def test_admissibility_is_checked_by_the_bank_before_any_fee(w):
    deal_id, _ = c_deal(w)
    items, fields = all_items()
    fields["1/0"], fields["1/1"] = "website", "name"  # no mapping / the source says the same
    out = claims_c.file(deal_id, items, fields, "agent:buyer", now=T0 + 5)
    assert out["admissible"] == 8 and {x["why"] for x in out["excluded"]} == {
        "no published field mapping for this field (only B protects it)", "the source says the same"}
    assert out["D"] == 6_400_000 and out["fee"] == 660_000  # 0,50 € + 8 × 0,02 € = 0,66 € > 10 % of D (0,64 €)
    with pytest.raises(claims_c.ClaimError):  # one C claim per deal
        claims_c.file(deal_id, items, fields, "agent:buyer", now=T0 + 6)
    with pytest.raises(deals.DealError, match="C claim"):  # no new versions after a C claim
        deals.record_delivery(deal_id, k.signed(w, deal_id, company_items(12, "honest")[10:], version=2),
                              now=T0 + 7)


def test_nothing_admissible_means_no_claim_and_no_fee(w):
    deal_id, _ = c_deal(w)
    items, fields = all_items("name")  # the ARES name equals the item's name for every item
    out = claims_c.file(deal_id, items, fields, "agent:buyer", now=T0 + 5)
    assert out["claim"] is None and out["admissible"] == 0 and claims_c.get(deal_id) is None


def test_episode_3_negotiation_fees_draw_sample_decision_split_and_execution(w):
    global BD
    BD = config.BUSINESS_DAY_S
    deal_id, window = c_deal(w)
    items, fields = all_items()
    out = claims_c.file(deal_id, items, fields, "agent:buyer", now=T0 + 5)
    fee = out["fee"]
    assert out["D"] == PRICE_C and fee == 800_000  # 10 % of D (0,80 €) > 0,50 € + 10 × 0,02 €
    deadlines.tick(now=T0 + 5 + 2 * BD + 1)  # negotiation over: the buyer's fee is due
    assert claims_c.get(deal_id)["state"] == "buyer_fee"
    claims_c.pay_fee(deal_id, "buyer", "agent:buyer", now=T0 + 5 + 2 * BD + 2)
    with pytest.raises(claims_c.ClaimError):  # the seller's fee must be signed by the seller's wallet
        claims_c.pay_fee(deal_id, "seller", "seller", signature="0x" + "11" * 65)
    pi = claims_c.fee_terms(deal_id, "seller")
    sig = escrow.buyer_signature(pi, "EURC", w.seller_wallet.key.hex())  # same ERC-3009 shape, the seller's key
    claims_c.pay_fee(deal_id, "seller", "seller", signature=sig.hex(), now=T0 + 5 + 2 * BD + 3)
    fee_caps = [(fn, amount, key) for fn, amount, key, _ in w.sender.sent if "fee" in key]
    assert [(fn, amount) for fn, amount, _ in fee_caps] == [("authorize", fee), ("capture", fee)] * 2
    c = claims_c.get(deal_id)
    assert c["state"] == "decider" and escrow.paid(deal_id) == 0  # fees to the clearing address are not "paid"
    case = deciders.get(c["id"])
    opened = case["opened_at"]

    # window end: the undisputed part (0 here) captured, D split off: void, then a new lock on D (n = 1)
    end = T0 + 1 + window + 1
    deadlines.tick(now=max(end, opened + 1))
    assert k.fns(w)[-2:] == ["void", "authorize"] and w.notices[-1] == (deal_id, "split_off", PRICE_C)
    assert deals.get(deal_id)["pi_n"] == 1 and int(deals.get(deal_id)["payment_info"]["salt"]) == escrow.salt(deal_id, 1)
    assert disputes.get(deal_id)["split_n"] == 1 and k.state(deal_id) == "dispute_c"
    assert k.reservation(deal_id)["state"] == "reserved"  # D in the buyer's wallet for a few blocks: reserve kept

    # the draw: the schedule was fixed at stage open; the salt is hidden until the decision
    assert "salt" not in deciders.public(case) and not deciders.public(case)["decider_assigned"]
    after_sample = deciders.round_time(case["sample_round"]) + 1
    deadlines.tick(now=after_sample)
    case = deciders.get(c["id"])
    assert case["decider"] in {d["id"] for d in deciders.pool()} and len(case["sample"]) == 5
    with pytest.raises(deciders.DeciderError) as e:
        claims_c.decide(deal_id, "dec-x", {}, now=after_sample + 1)
    assert e.value.status == 403
    verdicts = {str(i): ("buyer" if n < 3 else "seller") for n, i in enumerate(case["sample"])}
    claims_c.decide(deal_id, case["decider"], verdicts, now=after_sample + 2)
    case = deciders.get(c["id"])
    assert deciders.public(case)["salt"] == case["salt"]  # revealed: anyone can recheck the draw
    left, _ = deciders.eligible(case)
    assert left[deciders.draw(case["selection_beacon"]["randomness"], case["salt"]) % len(left)]["id"] == case["decider"]

    deadlines.tick(now=after_sample + 3 + 9 * BD)  # within the 10 business days: nothing moves
    assert claims_c.get(deal_id)["state"] == "decided"
    deadlines.tick(now=after_sample + 3 + 10 * BD + 1)
    assert claims_c.get(deal_id)["state"] == "executed"
    assert k.captures(w)[-1] == 3_200_000 and k.fns(w)[-1] == "void"  # 40 % of D to the seller, 60 % back
    view = disputes.money_view(deals.get(deal_id), disputes.get(deal_id))
    assert view["captured"] == 3_200_000 and k.state(deal_id) == "settled"
    transfers = {key.rsplit(":", 1)[-1]: amount for fn, amount, key, _ in w.sender.sent if fn == "transfer"}
    assert transfers == {"fee_back_buyer": 480_000, "fee_back_seller": 320_000, "decider_fee": 800_000}
    assert k.reservation(deal_id)["committed"] == 320


def test_seller_not_paying_its_fee_decides_for_the_buyer_without_a_decider(w):
    deal_id, window = c_deal(w)
    items, fields = all_items()
    claims_c.file(deal_id, items, fields, "agent:buyer", now=T0 + 5)
    bd = config.BUSINESS_DAY_S
    deadlines.tick(now=T0 + 5 + 2 * bd + 1)
    claims_c.pay_fee(deal_id, "buyer", "agent:buyer", now=T0 + 5 + 2 * bd + 2)
    deadlines.tick(now=T0 + 5 + 5 * bd + 3)  # 3 business days passed
    c = claims_c.get(deal_id)
    assert c["state"] == "refund" and c["data"]["outcome"] == "for_buyer_without_decider"
    transfers = [(amount, key) for fn, amount, key, _ in w.sender.sent if fn == "transfer"]
    assert transfers == [(800_000, f"{deal_id}:clearing:fee_back_buyer")]  # the buyer's fee back in full
    deadlines.tick(now=T0 + 1 + window + 10)  # window end: D is not captured, one final void returns it
    assert k.captures(w) == [] and k.fns(w)[-1] == "void" and k.state(deal_id) == "settled"


def test_buyer_not_paying_its_fee_withdraws_the_claim_and_d_goes_to_the_seller(w):
    deal_id, window = c_deal(w)
    items, fields = all_items()
    claims_c.file(deal_id, items, fields, "agent:buyer", now=T0 + 5)
    bd = config.BUSINESS_DAY_S
    deadlines.tick(now=T0 + 5 + 2 * bd + 1)
    deadlines.tick(now=T0 + 5 + 5 * bd + 2)
    assert claims_c.get(deal_id)["state"] == "withdrawn"
    assert k.captures(w) == [PRICE_C]  # D to the seller at once, nothing frozen without a fee


def test_seller_agreeing_refunds_d_at_window_end_without_fees(w):
    deal_id, window = c_deal(w)
    items, fields = all_items()
    claims_c.file(deal_id, items[:4], fields, "agent:buyer", now=T0 + 5)
    claims_c.agree(deal_id, "seller")
    with pytest.raises(claims_c.ClaimError):
        claims_c.agree(deal_id, "seller")
    deadlines.tick(now=T0 + 1 + window + 1)
    assert k.captures(w) == [4_800_000] and k.fns(w)[-1] == "void"  # 6 undisputed items paid, 4 refunded
    assert not [x for x in w.sender.sent if "fee" in x[2]]


def test_c_claim_after_the_window_is_refused(w):
    deal_id, window = c_deal(w)
    items, fields = all_items()
    with pytest.raises(claims_c.ClaimError):
        claims_c.file(deal_id, items, fields, "agent:buyer", now=T0 + 1 + window + 1)


def test_api_files_a_c_claim_by_ids(w, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from bank import api
    deal_id, _ = c_deal(w)
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app, headers={"x-agent-token": "dev-agent-alfa"})
    items, fields = all_items()
    r = client.post(f"/api/deals/{deal_id}/claims", json={"rule": "C", "items": items[:2], "fields": fields})
    assert r.status_code == 200 and r.json()["admissible"] == 2
    assert client.post(f"/api/deals/{deal_id}/claims/c/decision", json={"decider": "dec-1", "verdicts": {}}).status_code == 401
    assert core.seller("s-beta")


def test_deal_page_and_the_pseudonymised_decider_page(w):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from bank import ui
    deal_id, _ = c_deal(w)
    items, fields = all_items()
    claims_c.file(deal_id, items, fields, "agent:buyer", now=T0 + 5)
    app = FastAPI()
    app.include_router(ui.router)
    client = TestClient(app)
    page = client.get(f"/deals/{deal_id}").text
    assert "C dispute" in page and "D = 8,00" in page and "Negotiation until" in page
    dec = client.get(f"/deciders/{deal_id}").text
    assert "Buyer A" in dec and "Beta" not in dec and "Alfa" not in dec  # pseudonyms only


def _to_seller_fee(w, deal_id):
    bd = config.BUSINESS_DAY_S
    claims_c.file(deal_id, *all_items(), "agent:buyer", now=T0 + 5)
    deadlines.tick(now=T0 + 5 + 2 * bd + 1)
    claims_c.pay_fee(deal_id, "buyer", "agent:buyer", now=T0 + 5 + 2 * bd + 2)
    sig = escrow.buyer_signature(claims_c.fee_terms(deal_id, "seller"), "EURC", w.seller_wallet.key.hex())
    return claims_c.pay_fee(deal_id, "seller", "seller", signature=sig.hex(), now=T0 + 5 + 2 * bd + 3)


def _under_review(n, buyer="alfa", seller="s-beta"):
    """n other C claims already with a decider (rows only: the limits read the claims table)."""
    return [db.run("INSERT INTO claims (deal_id, rule, buyer_id, seller_id, items_json, amount, state, filed_at,"
                   " data_json) VALUES (?,?,?,?,?,?,?,?,?)", (f"d-other-{buyer}-{i}", "C", buyer, seller, "[]", 1,
                                                              "decider", T0, "{}")).lastrowid for i in range(n)]


def test_two_claims_of_one_buyer_under_review_queue_the_third_and_d_stays_held(w):
    others = _under_review(2)
    deal_id, _ = c_deal(w)
    c = _to_seller_fee(w, deal_id)
    assert c["state"] == "queued" and "one buyer against one seller" in c["data"]["queue_reason"]
    assert deciders.get(c["id"]) is None  # no decider stage, no draw while queued
    assert claims_c.held(deals.get(deal_id), disputes.get(deal_id)) == (PRICE_C, True)  # an open C claim
    deadlines.tick(now=T0 + 5 + 2 * config.BUSINESS_DAY_S + 10)
    assert claims_c.get(deal_id)["state"] == "queued"
    db.run("UPDATE claims SET state='executed' WHERE id=?", (others[0],))  # a slot frees up
    deadlines.tick(now=T0 + 5 + 2 * config.BUSINESS_DAY_S + 11)
    c = claims_c.get(deal_id)
    assert c["state"] == "decider" and deciders.get(c["id"])["step"] == "first"
    events = [e["event"] for e in journal.entries(deal_id, limit=500)]
    assert "claim_c.review_queued" in events and "claim_c.review_started" in events


def test_seller_limit_counts_independent_buyers_only(w, monkeypatch):
    assert claims_c.seller_limit("s-beta") == 3  # max(3, 5 % of open rule-C deals)
    db.run("INSERT INTO companies (id, ico, name, role) VALUES ('omega','14645441','Omega s.r.o.','buyer')")
    _under_review(3, buyer="omega")  # three independent buyers' claims would fill the seller's limit...
    deal_id, _ = c_deal(w)
    c = _to_seller_fee(w, deal_id)
    assert c["state"] == "queued" and "seller's review limit" in c["data"]["queue_reason"]
    # ...but claims of buyers related to the seller (common owner with Beta, EvSM) do not count
    monkeypatch.setitem(claims_c.evsm._extract()["owners"], "14645441", ["bo-21c9"])
    deadlines.tick(now=T0 + 5 + 2 * config.BUSINESS_DAY_S + 10)
    assert claims_c.get(deal_id)["state"] == "decider"


def test_queue_is_fifo_by_the_time_both_fees_came_in(w):
    others = _under_review(2)
    first, _ = c_deal(w, deal_id="d-c1")
    second, _ = c_deal(w, deal_id="d-c2")
    assert _to_seller_fee(w, first)["state"] == "queued"
    assert _to_seller_fee(w, second)["state"] == "queued"
    db.run("UPDATE claims SET state='executed' WHERE id=?", (others[0],))
    c2 = claims_c.get(second)
    assert claims_c.start_review(c2, T0 + 10 * config.BUSINESS_DAY_S) is False  # the earlier claim goes first
    assert claims_c.start_review(claims_c.get(first), T0 + 10 * config.BUSINESS_DAY_S) is True
    assert claims_c.get(second)["state"] == "queued"  # the pair limit is full again


def _decided(w, deal_id="d-c", buyer_items=3):
    """A C claim through fees, the draw and a decision: `buyer_items` of the 5 sampled items upheld."""
    _to_seller_fee(w, deal_id)
    c = claims_c.get(deal_id)
    case = deciders.get(c["id"])
    after = deciders.round_time(case["sample_round"]) + 1
    deadlines.tick(now=after)
    case = deciders.get(c["id"])
    verdicts = {str(i): ("buyer" if n < buyer_items else "seller") for n, i in enumerate(case["sample"])}
    claims_c.decide(deal_id, case["decider"], verdicts, now=after + 1)
    return claims_c.get(deal_id), deciders.get(c["id"]), after + 2


def _pool(n):
    return lambda: [{"id": f"dec-{i}", "name": f"D{i}", "address": f"0x{'dec0':0>36}{i:04x}", "related_icos": []}
                    for i in range(1, n + 1)]


def test_loser_rereview_majority_of_three_and_f3_back_when_it_wins(w, monkeypatch):
    monkeypatch.setattr(deciders, "pool", _pool(5))
    deal_id, window = c_deal(w)
    c, case, now = _decided(w, deal_id)
    assert c["data"]["buyer_share_pct"] == 60 and claims_c.loser(c) == "seller"
    with pytest.raises(claims_c.ClaimError) as e:  # the winner cannot ask
        claims_c.request_rereview(deal_id, "buyer", "agent:buyer", now=now)
    assert e.value.status == 403
    with pytest.raises(claims_c.ClaimError):  # the seller signs its own F3 authorization
        claims_c.request_rereview(deal_id, "seller", "seller", signature="0x" + "11" * 65, now=now)
    pi = claims_c.rereview_terms(deal_id, "seller")
    f3 = claims_c.rereview_fee("EURC")
    assert int(pi["maxAmount"]) == f3 == 1_000_000  # 2 × the 50 € base, demo ÷100
    sig = escrow.buyer_signature(pi, "EURC", w.seller_wallet.key.hex())
    c = claims_c.request_rereview(deal_id, "seller", "seller", signature=sig.hex(), now=now)
    assert c["state"] == "rereview" and claims_c.held(deals.get(deal_id), disputes.get(deal_id))[1]
    rr = deciders.get(c["id"])["rereview"]
    assert rr["salt"] != case["salt"] and "salt" not in deciders.public(deciders.get(c["id"]))["rereview"]
    assert any(e["event"] == "decider.rereview_opened" and e["payload"]["salt_hash"] == rr["salt_hash"]
               for e in journal.entries(deal_id, limit=500))
    t = deciders.round_time(rr["selection_round"]) + 1
    deadlines.tick(now=t)
    chosen = deciders.get(c["id"])["rereview"]["deciders"]
    assert len(chosen) == 2 and case["decider"] not in chosen  # two NEW deciders
    seller_all = {str(i): "seller" for i in case["sample"]}
    with pytest.raises(deciders.DeciderError):
        claims_c.decide(deal_id, case["decider"], seller_all, now=t + 1)  # the first decider is out
    claims_c.decide(deal_id, chosen[0], seller_all, now=t + 1)
    assert claims_c.get(deal_id)["state"] == "rereview"  # one of two
    c = claims_c.decide(deal_id, chosen[1], seller_all, now=t + 2)
    assert c["state"] == "decided" and c["data"]["buyer_share_pct"] == 0 and c["data"]["changed"] is True
    assert c["data"]["execute_after"] == t + 2 + 10 * config.BUSINESS_DAY_S  # p. 9 runs from the final decision
    deadlines.tick(now=c["data"]["execute_after"] + 1)
    assert claims_c.get(deal_id)["state"] == "executed" and k.captures(w)[-1] == PRICE_C  # D to the seller
    transfers = {key.rsplit(":", 1)[-1]: amount for fn, amount, key, _ in w.sender.sent if fn == "transfer"}
    assert transfers == {"fee_back_seller": 800_000, "rereview_fee_1": 500_000, "rereview_fee_2": 500_000,
                         "rereview_fee_back": 800_000}  # F3 back out of the first decider's 0,80 €


def test_rereview_with_a_small_pool_goes_to_the_reserve_provider_and_is_only_once(w):
    deal_id, _ = c_deal(w)
    c, case, now = _decided(w, deal_id, buyer_items=2)  # 40 %: the buyer lost; the bank signs for the buyer
    c = claims_c.request_rereview(deal_id, "buyer", "agent:buyer", now=now)
    t = deciders.round_time(deciders.get(c["id"])["rereview"]["selection_round"]) + 1
    deadlines.tick(now=t)
    assert deciders.get(c["id"])["rereview"]["deciders"] == ["reserve"]  # 2 left < minimum pool of 3
    c = claims_c.decide(deal_id, "reserve", {str(i): "seller" for i in case["sample"]}, now=t + 1)
    assert c["data"]["buyer_share_pct"] == 0 and c["data"]["changed"] is False  # worse for the requester: no refund
    with pytest.raises(claims_c.ClaimError):
        claims_c.request_rereview(deal_id, "buyer", "agent:buyer", now=t + 2)


def test_rereview_after_five_business_days_is_refused(w):
    deal_id, _ = c_deal(w)
    c, _, now = _decided(w, deal_id, buyer_items=2)
    with pytest.raises(claims_c.ClaimError):
        claims_c.request_rereview(deal_id, "buyer", "agent:buyer", now=now + 5 * config.BUSINESS_DAY_S + 1)


def test_random_audit_of_five_percent_of_decisions(w):
    now = T0 + 1000
    r = deciders.first_round_after(now)
    case = {"claim_id": 77, "deal_id": "d-audit", "step": "first", "sides": {"buyer": "64146103", "seller": "30181020"},
            "exclude": [], "challenges": {"buyer": {}, "seller": {}}, "decider": "dec-1", "sample": [0, 1, 2],
            "sample_round": r, "verdicts": None, "decided_at": None, "salt_revealed": False}
    rand = "ab" * 32
    salts = (f"{i:064x}" for i in range(10_000))
    hits = [s_ for s_ in salts if deciders.draw(rand, s_, b"audit") % 100 < deciders.AUDIT_PCT]
    assert 300 < len(hits) < 700  # about 5 % of salts
    deciders._save({**case, "salt": hits[0], "sample_beacon": {"randomness": rand}})
    deciders.decide(77, "dec-1", {"0": "buyer", "1": "buyer", "2": "seller"}, now=now)
    a = deciders.get(77)["audit"]
    assert a["decider"] == "reserve" and deciders.public(deciders.get(77))["audit"] == {"done": False}
    with pytest.raises(deciders.DeciderError):
        deciders.audit(77, "dec-2", {"0": "buyer", "1": "buyer", "2": "seller"}, now=now + 1)
    deciders.audit(77, "reserve", {"0": "buyer", "1": "seller", "2": "seller"}, now=now + 1)
    ev = [e for e in journal.entries("d-audit", limit=50) if e["event"] == "decider.audited"]
    assert ev[0]["payload"]["diverged"] == 1 and "penalty" in ev[0]["payload"]["consequence"]
    miss = next(s_ for s_ in (f"{i:064x}" for i in range(100)) if s_ not in hits)
    deciders._save({**case, "claim_id": 78, "salt": miss, "sample_beacon": {"randomness": rand}})
    deciders.decide(78, "dec-1", {"0": "buyer", "1": "buyer", "2": "seller"}, now=now)
    assert "audit" not in deciders.get(78)


def test_items_refunded_under_b_cannot_go_into_a_c_claim(w):
    deal_id, _ = c_deal(w)
    disputes.file_b(deal_id, [[1, 0]], "wrong_data", "agent:buyer", now=T0 + 3)
    out = claims_c.file(deal_id, *all_items(), "agent:buyer", now=T0 + 5)
    assert out["admissible"] == 9 and out["excluded"] == [
        {"version": 1, "index": 0, "field": "founded", "why": "not an accepted item without a claim"}]


def test_items_of_a_c_claim_cannot_be_refunded_under_b(w):
    deal_id, _ = c_deal(w)
    claims_c.file(deal_id, [[1, i] for i in range(5)], {f"1/{i}": "founded" for i in range(5)}, "agent:buyer",
                  now=T0 + 3)
    with pytest.raises(disputes.DisputeError, match="C claim") as e:
        disputes.file_b(deal_id, [[1, 0]], "wrong_data", "agent:buyer", now=T0 + 5)
    assert e.value.status == 409 and disputes.claims(deal_id, "B") == []
    out = disputes.file_b(deal_id, [[1, 7]], "wrong_data", "agent:buyer", now=T0 + 6)  # an undisputed item is fine
    assert out["amount"] == 800_000


def test_task_id_is_part_of_the_hashed_terms():
    task = {"terms": {"count": 5, "predicates": [{"op": "required", "field": "ico"}]}}
    a, b = deals.task_terms({**task, "task_id": "t-1"}), deals.task_terms({**task, "task_id": "t-2"})
    assert a["hash"] != b["hash"] and a["task_id"] == "t-1"  # two tasks never share `unique` or tier sums
    assert deals.task_terms({**task, "task_id": "t-1"})["hash"] == a["hash"]


def test_deal_page_shows_a_queued_claim_and_a_rereview(w):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from bank import ui
    app = FastAPI()
    app.include_router(ui.router)
    client = TestClient(app)
    others = _under_review(2)
    deal_id, _ = c_deal(w)
    _to_seller_fee(w, deal_id)
    assert "waits for a review slot (at most 2 C claims" in client.get(f"/deals/{deal_id}").text
    db.run("DELETE FROM claims WHERE id IN (?,?)", tuple(others))
    deadlines.tick(now=T0 + 5 + 2 * config.BUSINESS_DAY_S + 10)
    c = claims_c.get(deal_id)
    after = deciders.round_time(deciders.get(c["id"])["sample_round"]) + 1
    deadlines.tick(now=after)
    case = deciders.get(c["id"])
    claims_c.decide(deal_id, case["decider"], {str(i): "seller" for i in case["sample"]}, now=after + 1)
    claims_c.request_rereview(deal_id, "buyer", "agent:buyer", now=after + 2)
    assert "Re-review paid by the buyer" in client.get(f"/deals/{deal_id}").text


def test_a_late_rereview_decider_is_replaced_by_the_reserve_provider(w, monkeypatch):
    monkeypatch.setattr(deciders, "pool", _pool(5))
    deal_id, _ = c_deal(w)
    c, case, now = _decided(w, deal_id)
    sig = escrow.buyer_signature(claims_c.rereview_terms(deal_id, "seller"), "EURC", w.seller_wallet.key.hex())
    c = claims_c.request_rereview(deal_id, "seller", "seller", signature=sig.hex(), now=now)
    rr = deciders.get(c["id"])["rereview"]
    t = deciders.round_time(rr["selection_round"]) + 1
    deadlines.tick(now=t)
    first, late = deciders.get(c["id"])["rereview"]["deciders"]
    claims_c.decide(deal_id, first, {str(i): "seller" for i in case["sample"]}, now=t + 1)
    t2 = deciders.deadline({"sample_round": rr["selection_round"]}) + 1
    deadlines.tick(now=t2)
    assert deciders.get(c["id"])["rereview"]["deciders"] == [first, "reserve"]
    with pytest.raises(deciders.DeciderError):
        claims_c.decide(deal_id, late, {str(i): "seller" for i in case["sample"]}, now=t2 + 1)
    c = claims_c.decide(deal_id, "reserve", {str(i): "seller" for i in case["sample"]}, now=t2 + 2)
    assert c["state"] == "decided" and c["data"]["buyer_share_pct"] == 0  # majority of three: first + two "seller"


# ---- the decider page takes a credential: the decider id comes from the token, never from the form ----

def test_decider_tokens_are_derived_per_decider_or_configured(seeded, monkeypatch):
    monkeypatch.setattr(config, "DECIDER_TOKENS", "")
    monkeypatch.setattr(config, "DECIDER_SECRET", "s3cret-base")
    t = deciders.tokens()
    assert set(t) == {"dec-1", "dec-2", "dec-3", "reserve"} and len(set(t.values())) == 4
    assert all(len(v) == 16 for v in t.values()) and "s3cret" not in str(t)
    assert deciders.by_token(t["dec-2"]) == "dec-2" and deciders.by_token("") is None
    assert deciders.by_token("dec-2") is None and deciders.by_token(t["dec-2"][:-1]) is None
    monkeypatch.setattr(config, "DECIDER_TOKENS", "dec-1:tokA, dec-2:tokB")
    assert deciders.tokens() == {"dec-1": "tokA", "dec-2": "tokB"} and deciders.by_token("tokB") == "dec-2"


def test_decider_page_takes_the_decider_from_its_token_not_the_form(w):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from bank import ui
    app = FastAPI()
    app.include_router(ui.router)
    client = TestClient(app, headers={"x-human-token": "dev-human"})
    deal_id, _ = c_deal(w)
    _to_seller_fee(w, deal_id)
    deadlines.tick(now=T0 + 5 + 2 * config.BUSINESS_DAY_S + 10)
    c = claims_c.get(deal_id)
    deadlines.tick(now=deciders.round_time(deciders.get(c["id"])["sample_round"]) + 1)
    case = deciders.get(c["id"])
    page = client.get(f"/deciders/{deal_id}").text
    assert 'name="token"' in page and "dec-1" not in page and 'name="decider"' not in page
    verdicts, tokens = {f"item-{i}": "seller" for i in case["sample"]}, deciders.tokens()
    other = next(d for d in tokens if d != case["decider"])
    r = client.post(f"/deciders/{deal_id}", data={**verdicts, "token": "wrong", "decider": case["decider"]})
    assert "Unknown decider token" in r.text and claims_c.get(deal_id)["state"] == "decider"
    r = client.post(f"/deciders/{deal_id}", data={**verdicts, "token": tokens[other], "decider": case["decider"]})
    assert "only the assigned decider" in r.text and claims_c.get(deal_id)["state"] == "decider"
    from bank import api
    api_app = FastAPI()
    api_app.include_router(api.router)
    body = {"decider": case["decider"], "verdicts": {str(i): "seller" for i in case["sample"]}}
    for token, status in ((config.DECIDER_TOKEN, 401), (tokens[other], 403)):  # the shared secret names nobody
        r = TestClient(api_app).post(f"/api/deals/{deal_id}/claims/c/decision", json=body,
                                     headers={"x-decider-token": token})
        assert r.status_code == status and claims_c.get(deal_id)["state"] == "decider"
    r = client.post(f"/deciders/{deal_id}", data={**verdicts, "token": tokens[case["decider"]], "decider": other})
    assert "decided" in r.text and claims_c.get(deal_id)["state"] == "decided"
    assert [e["actor"] for e in journal.entries(deal_id) if e["event"] == "decider.decided"] == \
        [f"decider:{case['decider']}"]


def test_lost_c_claims_count_back_over_the_demo_scaled_30_days():
    for filed in (T0 - 10, T0 - config.span(30 * 86400) - 10):
        db.run("INSERT INTO claims (deal_id, rule, buyer_id, seller_id, items_json, amount, state, filed_at,"
               " data_json) VALUES ('d', 'C', 'alfa', 's-beta', '[]', 1, 'executed', ?, ?)",
               (filed, db.dumps({"buyer_share_pct": 40})))
    assert claims_c.lost_c("alfa", T0) == 1


def _both_fees_paid(w, deal_id, t):
    BD = config.BUSINESS_DAY_S
    deadlines.tick(now=t + 2 * BD + 1)
    claims_c.pay_fee(deal_id, "buyer", "agent:buyer", now=t + 2 * BD + 2)
    sig = escrow.buyer_signature(claims_c.fee_terms(deal_id, "seller"), "EURC", w.seller_wallet.key.hex())
    claims_c.pay_fee(deal_id, "seller", "seller", signature=sig.hex(), now=t + 2 * BD + 3)


def test_no_split_off_while_items_are_pending_and_nothing_leaves_the_split_lock_before_the_decision(w, monkeypatch):
    items = company_items(10, "misdated")
    down, real = {str(items[8]["ico"]).strip(), str(items[9]["ico"]).strip()}, snapshots._mock_body
    ares = {"down": True}

    def body(template, key):  # ARES is out for two items: their snapshot (and so the item) waits
        if ares["down"] and key in down:
            raise snapshots.Wait("ARES 503")
        return real(template, key)
    monkeypatch.setattr(snapshots, "_mock_body", body)
    deal_id = k.make_deal(w, rule="C", deal_id="d-c", count=10, predicates=C_PREDICATES, amount=PRICE_C,
                          window_s=config.WINDOW_S["T2"][0], deadline_s=300, now=T0, task_hash="d-c")
    assert k.deliver(w, deal_id, mode="misdated", count=10, now=T0 + 1, items=items)["pending_items"] == 2
    out = claims_c.file(deal_id, [[1, i] for i in range(5)], {f"1/{i}": "founded" for i in range(5)}, "agent:buyer",
                        now=T0 + 5)
    assert out["D"] == 4_000_000
    _both_fees_paid(w, deal_id, T0 + 5)
    deal_txs = lambda: [(fn, a) for fn, a, key, _ in w.sender.sent if ":fee-" not in key]  # noqa: E731

    end = disputes.get(deal_id)["window_end_at"]
    deadlines.tick(now=end + 1)  # items still pending: the window is not over, so no split-off yet
    assert deal_txs() == [] and disputes.get(deal_id)["split_n"] is None

    ares["down"] = False
    deadlines.tick(now=end + 200)  # the two items are accepted now: their window starts now
    end2 = disputes.get(deal_id)["window_end_at"]
    deadlines.tick(now=end2 + 1)  # the undisputed half is captured first, then D is split off
    assert deal_txs() == [("capture", 4_000_000), ("void", None), ("authorize", 4_000_000)]
    s = disputes.get(deal_id)
    assert s["split_n"] == 1 and s["captured"] == 4_000_000 and escrow.locked_amount(deal_id) == 8_000_000
    disputes.delivered(deal_id, now=end2 + 50)  # never reopens the window after the split-off
    assert disputes.get(deal_id)["window_end_at"] == end2
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from bank import api
    app = FastAPI()
    app.include_router(api.router)
    r = TestClient(app, headers={"x-human-token": config.HUMAN_TOKEN}).post(
        f"/api/deals/{deal_id}/accept-items", json={"items": [[1, 0]], "by": "anna"})
    assert r.status_code == 409 and "split off" in r.json()["detail"]  # no "accept anyway" after the split-off
    monkeypatch.setattr(disputes, "payable", lambda deal: PRICE_C * 2)  # even if P grew, D stays held
    deadlines.tick(now=end2 + 100)
    assert deal_txs()[3:] == [] and claims_c.get(deal_id)["state"] in claims_c.OPEN


def _to_split(w, monkeypatch, first_error, attr="fail_next"):
    """A C claim on all items with both fees paid; the first authorize of the split-off fails with first_error."""
    deal_id, window = c_deal(w)
    items, fields = all_items()
    claims_c.file(deal_id, items, fields, "agent:buyer", now=T0 + 5)
    _both_fees_paid(w, deal_id, T0 + 5)
    end = max(T0 + 1 + window + 1, deciders.get(claims_c.get(deal_id)["id"])["opened_at"] + 1)
    real = escrow.split_off

    def first_fails(*a, **kw):
        if not hasattr(first_fails, "done"):
            first_fails.done = True
            setattr(w.sender, attr, first_error)
        return real(*a, **kw)
    monkeypatch.setattr(escrow, "split_off", first_fails)
    deadlines.tick(now=end)
    assert disputes.get(deal_id)["pending"]["kind"] == "split" and deals.get(deal_id)["pi_n"] == 1
    return deal_id, end


def _deal_pis(deal_id):
    return [(r["n"], r["max_amount"], r["authorized"]) for r in db.rows(
        "SELECT n, max_amount, authorized FROM payment_infos WHERE deal_id=? AND purpose='deal' ORDER BY n",
        (deal_id,))]


def test_a_retried_split_off_authorizes_the_stored_payment_info_never_a_second_lock(w, monkeypatch):
    deal_id, end = _to_split(w, monkeypatch, TimeoutError("receipt wait timed out"))  # the tx went out, answer lost
    deadlines.tick(now=end + 1)
    auths = [a for fn, a, key, _ in w.sender.sent if fn == "authorize" and ":fee-" not in key]
    assert auths == [PRICE_C]  # ONE split lock of D (the retry resent the same idem key)
    assert deals.get(deal_id)["pi_n"] == 1 and disputes.get(deal_id)["split_n"] == 1
    assert _deal_pis(deal_id)[1:] == [(1, PRICE_C, 1)] and escrow.locked_amount(deal_id) == PRICE_C


def test_a_split_payment_info_that_never_locked_is_rebuilt_after_pre_approval_expiry(w, monkeypatch):
    deal_id, end = _to_split(w, monkeypatch, ValueError("gas estimation reverted"), attr="fail_always")
    w.sender.fail_always = None
    later = time.time() + escrow.PRE_APPROVAL_S + 1
    monkeypatch.setattr(escrow.time, "time", lambda: later)
    deadlines.tick(now=end + 1)
    auths = [a for fn, a, key, _ in w.sender.sent if fn == "authorize" and ":fee-" not in key]
    assert auths == [PRICE_C] and deals.get(deal_id)["pi_n"] == 2 and disputes.get(deal_id)["split_n"] == 2
    assert _deal_pis(deal_id)[1:] == [(1, PRICE_C, 0), (2, PRICE_C, 1)]


def _timer_fires_during_the_fee(monkeypatch, timer_now):
    """The timer thread fires while the fee's authorize and capture are in flight (the API runs in a threadpool)."""
    import threading
    real, timer = escrow.pay_fee, {}

    def slow_pay_fee(deal_id, pi, sig):
        tx = real(deal_id, pi, sig)
        timer["t"] = threading.Thread(target=lambda: deadlines.tick(now=timer_now))
        timer["t"].start()
        time.sleep(0.3)  # the timer reaches the claim before pay_fee books the fee
        return tx
    monkeypatch.setattr(escrow, "pay_fee", slow_pay_fee)
    return timer


def test_a_fee_paid_at_its_deadline_and_the_timer_never_both_act_on_the_claim(w, monkeypatch):
    bd = config.BUSINESS_DAY_S
    deal_id, _ = c_deal(w)
    claims_c.file(deal_id, *all_items(), "agent:buyer", now=T0 + 5)
    deadlines.tick(now=T0 + 5 + 2 * bd + 1)
    until = claims_c.get(deal_id)["data"]["buyer_fee_until"]
    timer = _timer_fires_during_the_fee(monkeypatch, until + 1)
    claims_c.pay_fee(deal_id, "buyer", "agent:buyer", now=until - 1)
    timer["t"].join(timeout=20)
    assert claims_c.get(deal_id)["state"] == "seller_fee"  # the fee came in time: not withdrawn under it
    assert k.captures(w) == [] and deal_id in disputes.open_ids()  # D did not go to the seller

    until = claims_c.get(deal_id)["data"]["seller_fee_until"]
    sig = escrow.buyer_signature(claims_c.fee_terms(deal_id, "seller"), "EURC", w.seller_wallet.key.hex())
    timer = _timer_fires_during_the_fee(monkeypatch, until + 1)
    claims_c.pay_fee(deal_id, "seller", "seller", signature=sig.hex(), now=until - 1)
    timer["t"].join(timeout=20)
    c = claims_c.get(deal_id)
    assert c["state"] == "decider" and c["data"].get("outcome") is None and deal_id in disputes.open_ids()
    assert not [key for fn, _, key, _ in w.sender.sent if fn == "transfer"]  # no fee back: the seller paid in time


def test_a_claim_read_before_another_writer_moved_it_is_not_overwritten(w):
    deal_id, _ = c_deal(w)
    claims_c.file(deal_id, *all_items(), "agent:buyer", now=T0 + 5)
    stale = claims_c.get(deal_id)
    claims_c.agree(deal_id, "seller", now=T0 + 6)  # another writer moved it to refund
    with pytest.raises(claims_c.ClaimError) as e:
        claims_c._set(stale, "buyer_fee", "timer", "negotiation_over", buyer_fee_until=T0 + 7)
    assert e.value.status == 409 and claims_c.get(deal_id)["state"] == "refund"


def test_a_fee_payment_info_past_pre_approval_expiry_and_never_sent_is_rebuilt(w, monkeypatch):
    bd = config.BUSINESS_DAY_S
    deal_id, _ = c_deal(w)
    claims_c.file(deal_id, *all_items(), "agent:buyer", now=T0 + 5)
    deadlines.tick(now=T0 + 5 + 2 * bd + 1)
    w.sender.fail_always = ValueError("gas estimation reverted")  # refused before anything was signed
    with pytest.raises(ValueError):
        claims_c.pay_fee(deal_id, "buyer", "agent:buyer", now=T0 + 5 + 2 * bd + 2)
    w.sender.fail_always = None
    first = claims_c.get(deal_id)["data"]["fee_pi_buyer"]
    later = time.time() + escrow.PRE_APPROVAL_S + 1  # the retry comes more than 10 minutes later
    monkeypatch.setattr(escrow.time, "time", lambda: later)
    claims_c.pay_fee(deal_id, "buyer", "agent:buyer", now=T0 + 5 + 2 * bd + 3)
    pi = claims_c.get(deal_id)["data"]["fee_pi_buyer"]
    assert claims_c.get(deal_id)["state"] == "seller_fee" and pi["salt"] != first["salt"]
    assert int(pi["preApprovalExpiry"]) > later  # signed and authorized before it expires
    keys = [key for fn, _, key, _ in w.sender.sent if fn == "authorize" and ":fee-" in key]
    assert keys == [f"{deal_id}:{escrow.payment_hash(pi)[2:10]}:fee-authorize"]


def test_a_failed_fee_transfer_is_written_down_and_resent_by_the_timer_after_the_deal_closed(w, monkeypatch):
    deal_id, window = c_deal(w)
    claims_c.file(deal_id, *all_items(), "agent:buyer", now=T0 + 5)
    bd = config.BUSINESS_DAY_S
    deadlines.tick(now=T0 + 5 + 2 * bd + 1)
    claims_c.pay_fee(deal_id, "buyer", "agent:buyer", now=T0 + 5 + 2 * bd + 2)
    real, gas = escrow.transfer_from_clearing, {"none": True}

    def transfer(*a, **kw):  # the clearing address has no gas yet: the node refuses the transfer
        if gas["none"]:
            raise ValueError("insufficient funds for gas")
        return real(*a, **kw)
    monkeypatch.setattr(escrow, "transfer_from_clearing", transfer)
    deadlines.tick(now=T0 + 5 + 5 * bd + 3)  # the seller did not pay: the buyer's fee is owed back
    deadlines.tick(now=T0 + 1 + window + 10)  # window end: the settlement closes all the same
    gas["none"] = False
    owed = claims_c.get(deal_id)["data"]["transfers"]
    assert k.state(deal_id) == "settled" and deal_id not in disputes.open_ids()
    assert [(t["what"], t["amount"], t["done"]) for t in owed] == [("fee_back_buyer", 800_000, False)]
    deadlines.tick(now=T0 + 1 + window + 20)  # gas arrived: the timer resends it under the same idem key
    deadlines.tick(now=T0 + 1 + window + 30)
    transfers = [(amount, key) for fn, amount, key, _ in w.sender.sent if fn == "transfer"]
    assert transfers == [(800_000, f"{deal_id}:clearing:fee_back_buyer")]  # once
    assert claims_c.get(deal_id)["data"]["transfers"][0]["done"] is True


def test_a_clearing_transfer_that_keeps_failing_is_journalled_once_and_then_waits_for_a_person(w, monkeypatch):
    deal_id, _ = c_deal(w)
    claims_c.file(deal_id, *all_items(), "agent:buyer", now=T0 + 5)
    bd = config.BUSINESS_DAY_S
    deadlines.tick(now=T0 + 5 + 2 * bd + 1)
    claims_c.pay_fee(deal_id, "buyer", "agent:buyer", now=T0 + 5 + 2 * bd + 2)
    calls = []
    monkeypatch.setattr(escrow, "transfer_from_clearing", lambda *a, **kw: calls.append(a) or 1 / 0)
    for i in range(claims_c.CLEAR_TRIES + 5):
        deadlines.tick(now=T0 + 5 + 5 * bd + 3 + i)
    events = [e["event"] for e in journal.entries(deal_id, limit=2000) if e["event"].startswith("clearing_transfer")]
    assert len(calls) == claims_c.CLEAR_TRIES and sorted(events) == ["clearing_transfer.stuck", "clearing_transfer_failed"]
