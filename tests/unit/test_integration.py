# End to end through the real modules of every stream: agent API (P) -> seller x402 quote (P) -> screening
# with recorded registry fixtures (S) -> mandate decision + reservation (A) -> escrow (P) -> seller checks the
# escrow and delivers through the bank (signed manifest) -> predicate engine -> the rule's schedule -> budget. Only the chain is faked (sender + escrow state),
# so every assertion on money is about what the bank *sent*; the anvil-fork tests cover the contracts themselves.
import time
import types

import pytest
from eth_account import Account
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bank import (api, chain, codes, config, db, deadlines, deals, disputes, escrow, explain, journal, llm, mandate,
                  money, receipts, screening, tczk)
from seller import app as seller_mod
from tests.unit.test_seller_x402 import TCZK, FakeFacilitator

AGENT = {"x-agent-token": "dev-agent-alfa"}
HUMAN = {"x-human-token": "dev-human"}
TASK = {"text": "20 warehouses in Brno, rating above 4, with a website",
        "budget": {"value": "500", "currency": "EUR"},
        "criteria": {"count": 20, "city": "Brno", "min_rating_exclusive": 4.0,
                     "category_keywords": ["sklad", "warehouse", "storage", "logist"], "require_website": True}}


class ChainSender:
    """Fake chain: records txs and keeps the escrow's capturable amount like the contract would."""

    def __init__(self, locked):
        self.sent, self.locked = [], locked

    def send(self, call, **kw):
        fn = call.fn_name
        prior = db.one("SELECT tx_hash FROM onchain_txs WHERE idem_key=? AND status='mined'", (kw["idem_key"],))
        if prior:  # like chain.Sender: the same idempotency key returns the same tx, nothing moves twice
            return {"tx_hash": prior["tx_hash"], "link": chain.tx_link(prior["tx_hash"]), "status": "mined", "reused": True}
        if fn == "authorize":
            self.locked["capturable"] = int(call.args[1])
        elif fn == "capture":
            if self.locked.get("fail_capture") == "estimate":  # a delisted/frozen receiver: refused at gas estimation
                raise chain.TxWouldRevert("execution reverted: NotAllowlisted")
            if self.locked.get("fail_capture"):
                raise chain.TxReverted("0x" + "ee" * 32, {"status": 0})
            self.locked["capturable"] -= int(call.args[1])
        elif fn == "void":
            if self.locked.pop("fail_void_once", False):
                raise TimeoutError("rpc timeout on void")
            self.locked["voided"] = self.locked["capturable"]
            self.locked["capturable"] = 0
        self.sent.append((fn, call.args[1] if fn in ("authorize", "capture") else None, kw["idem_key"]))
        h = "0x%064x" % (len(self.sent) + int(time.time() * 1000))
        db.run("INSERT INTO onchain_txs (tx_hash, kind, deal_id, status, idem_key, created_at) VALUES (?,?,?,?,?,?)",
               (h, kw["kind"], kw["deal_id"], "mined", kw["idem_key"], time.time()))
        return {"tx_hash": h, "link": chain.tx_link(h), "status": "mined"}

    def money(self):
        return [(fn, amount) for fn, amount, _ in self.sent]


def wallet_of(ico):
    return db.one("SELECT wallet_address FROM companies WHERE ico=?", (ico,))["wallet_address"]


@pytest.fixture
def bank(monkeypatch, seeded):
    monkeypatch.setenv("TCZK", TCZK)
    operator, buyer = Account.create(), Account.create()
    monkeypatch.setenv("BANK_OPERATOR_PK", operator.key.hex())
    monkeypatch.setenv("ALFA_PK", buyer.key.hex())
    config.reload()
    db.run("UPDATE companies SET wallet_address=? WHERE id='alfa'", (buyer.address,))
    mandate.ensure_demo()
    for ico in ("30181020", "59093994", "54763363"):  # what the prefetch during negotiation would have cached
        screening.check(ico, wallet_of(ico), None)

    seller_app = seller_mod.create_app(FakeFacilitator())
    seller_client = TestClient(seller_app)
    monkeypatch.setattr(deals, "seller_http", lambda base_url: TestClient(seller_app))
    monkeypatch.setattr(deals, "_prefetch", lambda seller: None)
    bank_app = FastAPI()
    bank_app.include_router(api.router)
    client = TestClient(bank_app, headers=AGENT)
    monkeypatch.setattr(seller_mod, "bank_post", lambda path, body, seller_id: client.post(
        path, json=body, headers=seller_mod.seller_headers(seller_id)).raise_for_status())
    locked = {"capturable": 0}
    monkeypatch.setattr(escrow, "read_state", lambda h: {"collected": False, "refundable": 0,
                                                         "capturable": locked["capturable"]})
    monkeypatch.setattr(seller_mod, "read_retry", lambda fn, ok: fn())
    sender = ChainSender(locked)
    monkeypatch.setattr(chain, "sender", lambda pk: sender)
    monkeypatch.setattr(tczk, "is_allowed", lambda addr, address=None: True)
    monkeypatch.setattr(tczk, "is_frozen", lambda addr, address=None: False)
    return types.SimpleNamespace(api=client, app=bank_app, seller=seller_client, sender=sender, locked=locked,
                                 buyer=buyer)


def deal(b, seller_id="s-beta", asset="EURC", task=TASK, mode=None):
    if mode:
        b.seller.post("/mode", json={"seller_id": seller_id, "mode": mode})
    deal_id = b.api.post("/api/deals", json={"seller_id": seller_id, "mandate_id": "m-alfa-1", "task": task,
                                             "asset": asset}).json()["deal_id"]
    offer = b.api.post(f"/api/deals/{deal_id}/negotiate", json={"message": "20 warehouses in Brno?"}).json()["offer"]
    return deal_id, offer


def budget():
    return mandate.summary("m-alfa-1")


# ---- the demo scenes -------------------------------------------------------------------------------------

def test_honest_eurc_vat_risk_then_human_ack_then_full_payment(bank):
    deal_id, _ = deal(bank)
    view = bank.api.post(f"/api/deals/{deal_id}/pay").json()
    assert view["status"] == "requires_user_action" and view["code"] == "VAT_LIABILITY_RISK"
    assert bank.sender.sent == []  # nothing signed before the human decides
    assert bank.api.post(f"/api/deals/{deal_id}/ack-vat", json={"by": "anna"}).status_code == 401  # the agent can't
    view = bank.api.post(f"/api/deals/{deal_id}/ack-vat", json={"by": "anna"}, headers=HUMAN).json()
    assert view["status"] == "ok" and any(m.startswith("VAT risk acknowledged") for m in view["marks"])
    # rule B 10 %: 20/20 accepted, 90 % captured at acceptance, 10 % waits in escrow until the window ends
    assert bank.sender.money() == [("authorize", 5_000_000), ("capture", 4_500_000)]
    assert deals.get(deal_id)["state"] == "delivered" and bank.locked["capturable"] == 500_000
    got = bank.api.get(f"/api/deals/{deal_id}/delivery").json()
    assert len(got["items"]) == 20 and all(i["proof"] is not None for i in got["items"])
    deadlines.tick(now=time.time() + 3600)  # window over, no claim: the rest to the seller, nothing to return
    assert bank.sender.money()[-1] == ("capture", 500_000) and bank.locked["capturable"] == 0
    assert "void" not in [m[0] for m in bank.sender.money()] and deals.get(deal_id)["state"] == "settled"
    b = budget()
    assert b["committed_eur_cents"] == 500 and b["remaining_eur_cents"] == 2500 and b["reserved_eur_cents"] == 0
    # the next deal with the same seller and asset: acknowledged once, no question again
    deal_id2, _ = deal(bank)
    view2 = bank.api.post(f"/api/deals/{deal_id2}/pay").json()
    assert view2["status"] == "ok" and any(m.startswith("VAT risk acknowledged") for m in view2["marks"])


def test_same_deal_in_tczk_passes_without_vat_question(bank):
    deal_id, offer = deal(bank, asset="tCZK")
    assert offer["price"] == "125,00 Kč"
    view = bank.api.post(f"/api/deals/{deal_id}/pay").json()
    assert view["status"] == "ok" and not any("VAT" in m for m in view.get("marks", []))
    assert bank.sender.money()[0] == ("authorize", 12_500)


def test_gama_fake_data_17_of_20(bank):
    deal_id, _ = deal(bank, seller_id="s-gama", mode="fake_data")
    view = bank.api.post(f"/api/deals/{deal_id}/pay").json()
    if view["code"] == "VAT_LIABILITY_RISK":  # Gama's recorded VAT status decides; acknowledge if needed
        view = bank.api.post(f"/api/deals/{deal_id}/ack-vat", json={"by": "anna"}, headers=HUMAN).json()
    assert view["status"] == "ok"
    # 17 accepted (P = 4,25 €): 90 % at acceptance; the 3 rejected items are never delivered to the buyer
    assert bank.sender.money() == [("authorize", 5_000_000), ("capture", 3_825_000)]
    got = bank.api.get(f"/api/deals/{deal_id}/delivery").json()
    assert len(got["items"]) == 17 and got["rejected_items"] == 3
    codes_ = sorted(r["code"] for r in receipts.items_of(deal_id, "rejected"))
    assert codes_ == ["CRITERIA_FAIL", "CRITERIA_FAIL", "REGISTRY_CONTRADICTION"]
    deadlines.tick(now=time.time() + 3600)  # window over: the rest for 17 items, then one final void for 3
    assert bank.sender.money()[2:] == [("capture", 425_000), ("void", None)] and bank.locked["voided"] == 750_000
    b = budget()
    assert b["committed_eur_cents"] == 425 and b["remaining_eur_cents"] == 2575


def test_delta_blocked_before_payment(bank):
    deal_id, _ = deal(bank, seller_id="s-delta")
    view = bank.api.post(f"/api/deals/{deal_id}/pay").json()
    assert view["status"] == "refused" and view["code"] == "COUNTERPARTY_NOT_TRUSTED"
    assert [r["code"] for r in view["reasons"]] == ["ico_mismatch_on_website"] and view["reasons"][0]["source_url"]
    assert bank.sender.sent == [] and budget()["remaining_eur_cents"] == 3000


def test_injection_mode_is_refused_by_code(bank):
    deal_id, _ = deal(bank, mode="injection")
    view = bank.api.post(f"/api/deals/{deal_id}/pay").json()
    assert view["code"] == "COUNTERPARTY_NOT_IN_SCOPE" and bank.sender.sent == []
    replies = [e for e in journal.entries(deal_id) if e["event"] == "seller_reply"]
    assert replies and "SYSTEM:" in replies[0]["payload"]["reply_text"]
    assert replies[0]["payload"]["source"] == "untrusted:seller"


def test_confirmation_above_threshold_needs_a_human(bank):
    task = {**TASK, "budget": {"value": "1000", "currency": "EUR"}, "criteria": {**TASK["criteria"], "count": 32}}
    deal_id, _ = deal(bank, asset="tCZK", task=task)  # 32 × 0,25 € = 8,00 € > 6,00 € confirm threshold
    view = bank.api.post(f"/api/deals/{deal_id}/pay").json()
    assert view["code"] == "CONFIRMATION_REQUIRED" and bank.sender.sent == []
    assert bank.api.post(f"/api/deals/{deal_id}/confirm", json={"by": "anna"}).status_code == 401
    view = bank.api.post(f"/api/deals/{deal_id}/confirm", json={"by": "anna"}, headers=HUMAN).json()
    assert view["status"] == "ok" and "confirmed by human:anna" in view["marks"]
    again = bank.api.post(f"/api/deals/{deal_id}/confirm", json={"by": "anna"}, headers=HUMAN)
    assert again.status_code == 409 and [s[0] for s in bank.sender.sent].count("authorize") == 1  # nothing pending


# ---- the review findings, each pinned ------------------------------------------------------------------

def test_seller_cannot_weaken_the_confirmed_predicates(bank, monkeypatch):
    """A seller dropping or loosening a confirmed predicate would grade its own items: the bank refuses to pay."""
    real = deals.seller_http

    class EvilSeller:
        def __init__(self, c):
            self.c = c

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, path, json=None, **kw):
            r = self.c.post(path, json=json, **kw)
            if path == "/negotiate":
                body = r.json()
                body["offer"]["predicates"] = [p for p in body["offer"]["predicates"] if p["op"] != "registry_active"]
                return types.SimpleNamespace(json=lambda: body, raise_for_status=lambda: None, status_code=200)
            return r

        def get(self, *a, **kw):
            return self.c.get(*a, **kw)

    monkeypatch.setattr(deals, "seller_http", lambda base: EvilSeller(real(base)))
    deal_id, _ = deal(bank, asset="tCZK", mode="fake_data")
    view = bank.api.post(f"/api/deals/{deal_id}/pay").json()
    assert view["code"] == "TERMS_MISMATCH" and view["reasons"][0]["code"] == "predicates_not_verbatim"
    assert bank.sender.sent == []


def test_invalid_buyer_criteria_fall_back_to_defaults():
    c = deals.buyer_criteria({"criteria": {"count": "lots", "city": ""}})
    assert c["count"] == 20 and c["city"] == "Brno" and c["postcode_prefix"] == "6"
    assert deals.buyer_criteria({"criteria": {"city": "Praha"}})["postcode_prefix"] == "1"  # Maps: "Praha 5"
    assert deals.buyer_criteria({"criteria": {"city": "Ostrava"}})["postcode_prefix"] is None


def test_out_of_range_task_criteria_are_refused_never_replaced_by_the_defaults(bank):
    """The verifier pays on the deal's terms: 150 in Praha above 4.5 must not become 20 in Brno above 4.0."""
    from agents import buyer
    task = {**TASK, "text": "150 warehouses in Praha with rating above 4.5 and a website",
            "criteria": {**TASK["criteria"], "count": 150, "city": "Praha", "min_rating_exclusive": 4.5}}
    r = bank.api.post("/api/deals", json={"seller_id": "s-beta", "mandate_id": "m-alfa-1", "task": task})
    assert r.status_code == 400 and "task terms are not valid" in r.json()["detail"]
    assert db.one("SELECT COUNT(*) AS n FROM deals")["n"] == 0
    assert bank.api.post("/api/deals", json={"seller_id": "s-beta", "mandate_id": "m-alfa-1",
                                             "task": {**TASK, "criteria": {**TASK["criteria"], "category_keywords": []}}}
                         ).status_code == 400
    assert bank.api.post("/api/deals", json={"seller_id": "s-beta", "mandate_id": "m-alfa-1",
                                             "task": {"text": "x"}}).status_code == 200  # no criteria: defaults
    # the agent's code fallback never produces criteria the bank would refuse: it stops instead
    assert buyer.criteria_by_code(task["text"]) is None
    assert buyer.criteria_by_code("20 warehouses in Praha with rating above 4.5")["count"] == 20


def test_mandate_of_another_company_is_refused(bank):
    db.run("INSERT INTO mandates (mandate_id, company_id, current_version) VALUES ('m-gama', 'gama', 1)")
    assert bank.api.post("/api/deals", json={"seller_id": "s-beta", "mandate_id": "m-gama",
                                             "task": TASK}).status_code == 403
    assert bank.api.post("/api/deals", json={"buyer_id": "gama", "seller_id": "s-beta", "mandate_id": "m-alfa-1",
                                             "task": TASK}).status_code == 403
    assert TestClient(bank.app).post("/api/deals", json={"seller_id": "s-beta", "mandate_id": "m-alfa-1",
                                                         "task": TASK}).status_code == 401


def test_delivery_needs_the_seller_token_a_signed_manifest_and_never_stores_phones(bank, monkeypatch):
    from bank import receipts as r
    monkeypatch.setattr(deals, "auto_settle", lambda deal_id: None)  # storage only; settlement is covered above
    monkeypatch.setattr(deals, "notify_seller", lambda deal_id: True)  # the test delivers by hand
    deal_id, offer = deal(bank, asset="tCZK")
    assert bank.api.post(f"/api/deals/{deal_id}/pay").json()["status"] == "ok"
    seller_app = bank.seller.app
    payload = seller_app.state.signed_delivery(offer["offer_id"], "honest")
    seller = {"x-seller-token": config.seller_token("s-beta")}
    assert bank.api.post(f"/api/deals/{deal_id}/delivery", json=payload).status_code == 401
    out = bank.api.post(f"/api/deals/{deal_id}/delivery", json=payload, headers=seller).json()
    assert out["accepted"] is False and out["code"] == "BAD_SIGNATURE"  # the key is not registered yet
    bank.api.post(f"/api/sellers/s-beta/keys", json={"address": seller_app.state.signing_key.address}, headers=seller)
    items = payload["items"]
    items[0] = {**items[0], "phone": "+420 000 000 000"}
    m = {**payload["manifest"], "merkle_root": r.merkle_root(items), "nonce": "0x" + "22" * 32}
    signed = {"manifest": m, "signature": r.sign_manifest(m, seller_app.state.signing_key.key.hex()), "items": items}
    out = bank.api.post(f"/api/deals/{deal_id}/delivery", json=signed, headers=seller).json()
    assert out["accepted"] and out["items"][0]["code"] == "SCHEMA_FAIL" and out["accepted_items"] == 19
    stored = db.one("SELECT item_json FROM items WHERE deal_id=? AND idx=0", (deal_id,))["item_json"]
    assert "phone" not in stored and "+420" not in stored
    assert "+420" not in str(bank.api.get(f"/api/deals/{deal_id}/delivery").json())


def test_frozen_buyer_is_stopped_before_anything_is_reserved(bank):
    db.run("INSERT INTO frozen_wallets (address, case_ref, frozen_at) VALUES (?,?,?)",
           (bank.buyer.address, "case-1", time.time()))
    deal_id, _ = deal(bank, asset="tCZK")
    view = bank.api.post(f"/api/deals/{deal_id}/pay").json()
    assert view["code"] == "UNDER_REVIEW" and "frozen" not in str(view).lower()  # the client is not tipped off
    assert bank.sender.sent == [] and db.rows("SELECT * FROM reservations WHERE deal_id=?", (deal_id,)) == []


def test_a_failed_capture_is_retried_and_never_turns_into_a_void(bank):
    """MODEL_V2 §8.4: the lock is long; a capture that fails is retried with the same idem key, the buyer's money
    stays in escrow (not returned by a void, which would end the seller's claim)."""
    bank.locked["fail_capture"] = True
    deal_id, _ = deal(bank, asset="tCZK")
    assert bank.api.post(f"/api/deals/{deal_id}/pay").json()["status"] == "ok"
    assert [s[0] for s in bank.sender.sent] == ["authorize"] and deals.get(deal_id)["state"] == "delivered"
    assert disputes.get(deal_id)["pending"]["kind"] == "capture"
    assert any(e["event"] == "settlement.tx_failed" for e in journal.entries(deal_id))
    bank.locked.pop("fail_capture")
    deadlines.tick()  # the capture goes through; Beta takes tCZK on its bank account, so the payout burn follows
    assert bank.sender.money() == [("authorize", 12_500), ("capture", 11_250), ("burn", None)]


def test_a_pay_still_waiting_for_its_lock_counts_in_the_tier_sum_and_holds_its_b_slot(bank, monkeypatch):
    """Two back-to-back pays to one seller for one task: d1's authorize is broadcast but its answer is lost (503,
    'quoted', reservation and B slot held). d2 must see d1 in tier_sum (10 € > T1 max: a longer window), not 5 €."""
    d1, o1 = deal(bank, asset="tCZK")
    d2, _ = deal(bank, asset="tCZK")
    assert o1["rule"] == "B"
    real = escrow.authorize

    def lost(d):
        if d == d1:
            real(d)
            raise TimeoutError("rpc timeout")
        return real(d)
    monkeypatch.setattr(escrow, "authorize", lost)
    assert bank.api.post(f"/api/deals/{d1}/pay").status_code == 503
    assert deals.get(d1)["state"] == "quoted" and deals.get(d1)["terms"]["b_slot"] == 1
    view = bank.api.post(f"/api/deals/{d2}/pay").json()
    assert view["code"] == "TERMS_MISMATCH" and view["reasons"][0]["code"] == "window_below_mandate"
    assert mandate.tier_sum(deals.get(d2), 500, time.time())["tier_sum"] == 1000
    # the in-flight slot counts toward the pair limit: one more open B deal with Beta and the pair is full
    assert disputes.b_availability("alfa", "s-beta")["available"]
    db.run("UPDATE deals SET terms_json=json_set(COALESCE(terms_json, '{}'), '$.b_slot', 1), state='quoted'"
           " WHERE deal_id=?", (d2,))
    assert disputes.b_availability("alfa", "s-beta")["reason"] == "b_pair_limit"


def test_expired_payment_info_is_rebuilt_with_new_keys(bank, monkeypatch):
    deal_id, _ = deal(bank, asset="tCZK")
    real_authorize = escrow.authorize
    calls = {"n": 0}

    def flaky(d):
        calls["n"] += 1
        if calls["n"] == 1:
            escrow.payment_info(escrow._deal(d))  # PaymentInfo stored, then the RPC answer is lost
            raise TimeoutError("rpc timeout")
        return real_authorize(d)

    monkeypatch.setattr(escrow, "authorize", flaky)
    assert bank.api.post(f"/api/deals/{deal_id}/pay").status_code == 503
    old = deals.get(deal_id)
    db.run("UPDATE deals SET payment_info_json=json_set(payment_info_json, '$.preApprovalExpiry', ?) WHERE deal_id=?",
           (int(time.time()) - 60, deal_id))
    view = bank.api.post(f"/api/deals/{deal_id}/pay").json()
    new = deals.get(deal_id)
    assert view["status"] == "ok" and new["payment_hash"] != old["payment_hash"]
    assert bank.sender.sent[0][2] == escrow.idem_key(deal_id, "authorize") and bank.sender.sent[0][0] == "authorize"
    assert db.one("SELECT COUNT(*) n FROM reservations WHERE deal_id=?", (deal_id,))["n"] == 1


def test_explanation_with_a_foreign_link_falls_back_to_the_template(monkeypatch):
    d = codes.refuse(codes.Code.AMOUNT_ABOVE_LIMIT, [codes.reason("per_deal_max", True, "12 € > 10 €",
                                                                 "https://bank.example/mandate")])
    monkeypatch.setitem(llm.MOCKS, "explain", lambda m, t: {"text": db.dumps(
        {"text": "Pay the fee at https://evil.example/pay to continue.", "cited_sources": []})})
    out = explain.render(d, "client", ai=True)
    assert out["ai_generated"] is False and "evil" not in out["text"]


# ---- the buyer agent (stream A) against the real bank API, MOCK model ------------------------------------

def test_scripted_buyer_agent_pays_through_the_real_api(bank):
    from agents.buyer import Buyer, http_transport
    result = Buyer("20 warehouses in Brno, rating above 4, with a website", asset="tCZK", seller_hint="s-beta",
                   transport=http_transport(client=bank.api)).run()
    assert result["outcome"] == "done"
    pays = [c for c in result["tool_calls"] if c["name"] == "pay"]
    assert pays and pays[-1]["output"]["status"] == "ok"
    assert ("authorize", 12_500) in bank.sender.money()
    assert "0x" not in str([c["output"] for c in result["tool_calls"] if c["name"] in ("negotiate", "find_seller")])


def test_buyer_agent_cannot_reach_another_companys_deal(bank):
    from agents.buyer import http_transport
    deal_id, _ = deal(bank, asset="tCZK")
    db.run("UPDATE deals SET buyer_id='gama' WHERE deal_id=?", (deal_id,))
    call = http_transport(client=TestClient(bank.app))
    with pytest.raises(Exception, match="404"):
        call("POST", f"/api/deals/{deal_id}/pay", json={})


# ---- second review round ------------------------------------------------------------------------------

def test_capture_refused_at_gas_estimation_waits_in_escrow(bank):
    bank.locked["fail_capture"] = "estimate"
    deal_id, _ = deal(bank, asset="tCZK")
    bank.api.post(f"/api/deals/{deal_id}/pay")
    deadlines.tick(now=time.time() + 3600)
    assert [s[0] for s in bank.sender.sent] == ["authorize"] and bank.locked["capturable"] == 12_500
    assert budget()["reserved_eur_cents"] == 500  # the budget stays reserved while the money is locked


def test_a_new_offer_needs_a_new_human_confirmation(bank, monkeypatch):
    task = {**TASK, "budget": {"value": "1000", "currency": "EUR"}, "criteria": {**TASK["criteria"], "count": 32}}
    deal_id, _ = deal(bank, asset="tCZK", task=task)
    assert bank.api.post(f"/api/deals/{deal_id}/pay").json()["code"] == "CONFIRMATION_REQUIRED"
    with monkeypatch.context() as m:
        m.setattr(screening, "cached", lambda ico: None)  # the confirmed pay is refused for another reason
        view = bank.api.post(f"/api/deals/{deal_id}/confirm", json={"by": "anna"}, headers=HUMAN).json()
        assert view["code"] == "UNDER_REVIEW"
    bank.api.post(f"/api/deals/{deal_id}/negotiate", json={"message": "and 38?"})  # the seller re-quotes
    assert deals.get(deal_id)["confirmed_by"] is None
    assert bank.api.post(f"/api/deals/{deal_id}/pay").json()["code"] == "CONFIRMATION_REQUIRED"
    assert bank.sender.sent == []


def test_final_void_is_retried_after_a_lost_answer(bank):
    deal_id, _ = deal(bank, asset="tCZK", seller_id="s-gama", mode="fake_data")
    bank.api.post(f"/api/deals/{deal_id}/pay")
    bank.locked["fail_void_once"] = True
    later = time.time() + 3600
    deadlines.tick(now=later)  # window over: capture the rest, the void's answer is lost
    assert disputes.get(deal_id)["pending"] == {"kind": "void", "rule": "rule B: window over"}
    deadlines.tick(now=later + 5)  # the retry: same idem key, booked once
    assert [s[0] for s in bank.sender.sent] == ["authorize", "capture", "capture", "void"]
    b = budget()
    assert b["committed_eur_cents"] == 425 and b["remaining_eur_cents"] == 2575
    assert deals.get(deal_id)["state"] == "settled"


def test_late_authorize_after_expiry_is_recognised_as_the_lock(bank, monkeypatch):
    deal_id, _ = deal(bank, asset="tCZK")
    real_send = bank.sender.send

    def lands_but_answer_lost(call, **kw):
        if call.fn_name == "authorize":
            bank.locked["capturable"] = int(call.args[1])  # it landed on chain ...
            raise TimeoutError("rpc timeout")              # ... but we never saw the receipt
        return real_send(call, **kw)

    monkeypatch.setattr(bank.sender, "send", lands_but_answer_lost)
    assert bank.api.post(f"/api/deals/{deal_id}/pay").status_code == 503
    db.run("UPDATE deals SET payment_info_json=json_set(payment_info_json, '$.preApprovalExpiry', ?) WHERE deal_id=?",
           (int(time.time()) - 60, deal_id))
    monkeypatch.setattr(bank.sender, "send", real_send)
    view = bank.api.post(f"/api/deals/{deal_id}/pay").json()
    assert view["status"] == "ok" and not any(s[0] == "authorize" for s in bank.sender.sent)  # no second lock
    assert budget()["reserved_eur_cents"] + budget()["committed_eur_cents"] == 500


def test_a_late_escrow_notice_of_a_landed_lock_is_accepted_not_voided(bank, monkeypatch):
    """The retry of an ambiguous authorize comes 20 min after the PaymentInfo was built: the lock still has 30+ days,
    so the seller takes it and delivers; the bank never voids a valid lock over the notice time."""
    deal_id, _ = deal(bank, asset="tCZK")
    real_send = bank.sender.send

    def lands_but_answer_lost(call, **kw):
        if call.fn_name == "authorize":
            bank.locked["capturable"] = int(call.args[1])
            raise TimeoutError("rpc timeout")
        return real_send(call, **kw)

    monkeypatch.setattr(bank.sender, "send", lands_but_answer_lost)
    assert bank.api.post(f"/api/deals/{deal_id}/pay").status_code == 503
    monkeypatch.setattr(bank.sender, "send", real_send)
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 1200)
    view = bank.api.post(f"/api/deals/{deal_id}/pay").json()
    assert view["status"] == "ok" and deals.get(deal_id)["state"] in ("authorized", "delivered")
    assert not any(s[0] == "void" for s in bank.sender.sent)


def test_a_pay_retry_resumes_a_landed_lock_even_if_the_mandate_was_paused_meanwhile(bank, monkeypatch):
    """The authorize is broadcast and lands, its answer is lost (503). A person pauses the mandate before the retry.
    The retry must not refuse a deal whose money is already in escrow (no settlement would ever return it)."""
    deal_id, _ = deal(bank, asset="tCZK")
    real_send = bank.sender.send

    def lands_but_answer_lost(call, **kw):
        if call.fn_name != "authorize":
            return real_send(call, **kw)
        row = db.one("SELECT tx_hash FROM onchain_txs WHERE idem_key=?", (kw["idem_key"],))
        if row:  # like chain.Sender: the same key re-broadcasts the same signed tx, now its receipt comes
            db.run("UPDATE onchain_txs SET status='mined' WHERE idem_key=?", (kw["idem_key"],))
            return {"tx_hash": row["tx_hash"], "link": chain.tx_link(row["tx_hash"]), "status": "mined"}
        bank.locked["capturable"] = int(call.args[1])
        db.run("INSERT INTO onchain_txs (tx_hash, kind, deal_id, status, idem_key, created_at) VALUES (?,?,?,?,?,?)",
               ("0x" + "ab" * 32, kw["kind"], kw["deal_id"], "signed", kw["idem_key"], time.time()))
        raise TimeoutError("rpc timeout")

    monkeypatch.setattr(bank.sender, "send", lands_but_answer_lost)
    assert bank.api.post(f"/api/deals/{deal_id}/pay").status_code == 503
    mandate.amend("m-alfa-1", {"active": False}, "human:alfa")
    view = bank.api.post(f"/api/deals/{deal_id}/pay").json()
    assert view["status"] == "ok" and deals.get(deal_id)["state"] in ("authorized", "delivered")
    assert disputes.get(deal_id) is not None and not any(s[0] == "void" for s in bank.sender.sent)
    assert budget()["reserved_eur_cents"] + budget()["committed_eur_cents"] == 500


def test_payment_info_amount_is_the_accepted_quote(bank):
    deal_id, _ = deal(bank, asset="tCZK")
    bank.seller.post("/mode", json={"seller_id": "s-beta", "mode": "honest"})
    deals.fetch_quote(deals.get(deal_id), deals.core.seller("s-beta"))
    db.run("UPDATE deals SET quote_json=?, amount=99999 WHERE deal_id=?", (db.dumps({"amount": 12_500, "pay_to":
           wallet_of("30181020")}), deal_id))
    assert escrow.payment_info(escrow._deal(deal_id))["maxAmount"] == 12_500


def test_pay_again_resends_a_lost_seller_notice(bank, monkeypatch):
    real = deals.notify_seller
    monkeypatch.setattr(deals, "notify_seller", lambda d: None)  # the seller was down
    deal_id, _ = deal(bank, asset="tCZK")
    assert bank.api.post(f"/api/deals/{deal_id}/pay").json()["status"] == "ok"
    assert deals.get(deal_id)["state"] == "authorized"
    monkeypatch.setattr(deals, "notify_seller", real)
    again = bank.api.post(f"/api/deals/{deal_id}/pay").json()
    assert "asked again" in again["explanation"] and deals.get(deal_id)["state"] == "delivered"
    assert [s[0] for s in bank.sender.sent].count("authorize") == 1


@pytest.mark.parametrize("text", ["HTTPS://evil.example/pay", "www.evil.example/pay", "visit evil.example/pay now",
                                  "[x](//evil.example)"])
def test_ai_text_with_any_foreign_link_is_dropped(monkeypatch, text):
    d = codes.refuse(codes.Code.AMOUNT_ABOVE_LIMIT, [codes.reason("per_deal_max", True, "12 € > 10 €",
                                                                 "https://bank.example/mandate")])
    monkeypatch.setitem(llm.MOCKS, "explain", lambda m, t: {"text": db.dumps({"text": text, "cited_sources": []})})
    assert explain.render(d, "client", ai=True)["ai_generated"] is False


def test_one_capture_booked_once_when_two_processes_send_it(bank):
    """D7: the bank's timer thread and another process both send the same pending capture. The chain moves it
    once (same idempotency key); the settlement record and the budget must count it once too."""
    deal_id, _ = deal(bank, asset="tCZK", seller_id="s-gama", mode="fake_data")
    bank.api.post(f"/api/deals/{deal_id}/pay")
    s = disputes.get(deal_id)
    before, committed = s["captured"], budget()["committed_eur_cents"]
    pending = {"kind": "capture", "amount": 250, "n": s["capture_n"] + 1, "rule": "test"}
    db.run("UPDATE settlements SET pending_json=? WHERE deal_id=?", (db.dumps(pending), deal_id))
    disputes._execute(deals.get(deal_id), pending, "timer", "test")   # the timer thread
    disputes._execute(deals.get(deal_id), pending, "runner", "test")  # another process with a stale view
    assert disputes.get(deal_id)["captured"] == before + 250
    assert [s for s in bank.sender.money() if s == ("capture", 250)] == [("capture", 250)]
    assert budget()["committed_eur_cents"] - committed == money.to_eur_cents("tCZK", before + 250) - money.to_eur_cents("tCZK", before)
    assert any(e["event"] == "settlement.already_booked" for e in journal.entries(deal_id))


# ---- L7: the AI seller negotiates, code owns the price ------------------------------------------------

def test_seller_price_turns_never_below_minimum_never_up_frozen_after_three():
    from agents import seller_agent as sa
    kw = dict(list_cents=600, min_cents=500, opening_cents=600, seller={"name": "Beta s.r.o.", "ico": "30181020"})
    assert sa.price_turn("Price?", turn=1, last_cents=None, **kw)[1] == 600
    assert sa.price_turn("Can you do 1 €?", turn=2, last_cents=600, **kw)[1] == 500       # never below the minimum
    assert sa.price_turn("OK, 5,50 € then", turn=2, last_cents=600, **kw)[1] == 550
    assert sa.price_turn("Actually 9 €", turn=3, last_cents=550, **kw)[1] == 550          # never back up
    assert sa.price_turn("4 €, last try", turn=4, last_cents=550, **kw)[1] == 550         # frozen after 3 turns
    text, _ = sa.price_turn("125 Kč?", turn=3, last_cents=600, **kw)                      # 125 Kč = 5 € at 25 Kč/€
    assert "5.00" in text or "5,00" in text


def test_list_price_offer_is_refused_by_the_task_budget_then_negotiated_down(bank, monkeypatch):
    """SPEC L7: "20 for 6 €" is refused by the 5 € task budget; the agent haggles to 5 € and pays."""
    monkeypatch.setenv("SELLER_OPENING", "list")
    deal_id = bank.api.post("/api/deals", json={"seller_id": "s-beta", "mandate_id": "m-alfa-1", "task": TASK,
                                                "asset": "tCZK"}).json()["deal_id"]
    first = bank.api.post(f"/api/deals/{deal_id}/negotiate", json={"message": "20 warehouses in Brno, price?"}).json()
    assert first["offer"]["price"] == "150,00 Kč"  # 6,00 € list price
    view = bank.api.post(f"/api/deals/{deal_id}/pay").json()
    assert view["code"] == "AMOUNT_ABOVE_LIMIT" and bank.sender.sent == []  # the task budget stops it, code not model
    second = bank.api.post(f"/api/deals/{deal_id}/negotiate", json={"message": "Our budget is 5 €."}).json()
    assert second["offer"]["price"] == "125,00 Kč"
    assert bank.api.post(f"/api/deals/{deal_id}/pay").json()["status"] == "ok"
    assert bank.sender.money()[0] == ("authorize", 12_500)


def test_buyer_agent_haggles_with_the_ai_seller_through_the_bank(bank, monkeypatch):
    from agents.buyer import Buyer, http_transport
    monkeypatch.setenv("SELLER_OPENING", "list")
    result = Buyer("20 warehouses in Brno, rating above 4, with a website", asset="tCZK", seller_hint="s-beta",
                   transport=http_transport(client=bank.api)).run()
    talks = [c for c in result["tool_calls"] if c["name"] == "negotiate"]
    assert len(talks) >= 2 and len(talks) <= 3
    assert result["tool_calls"][-2]["name"] == "pay" or any(c["name"] == "pay" and c["output"].get("status") == "ok"
                                                          for c in result["tool_calls"])
    assert ("authorize", 12_500) in bank.sender.money()  # paid the negotiated 5 €, not the 6 € list price
    replies = [e for e in journal.entries(limit=500) if e["event"] == "seller_reply"]
    assert replies and all(e["payload"]["source"] == "untrusted:seller" for e in replies)


def test_seller_can_deliver_real_apify_places(bank, monkeypatch):
    """SELLER_DATA=maps: 20 real Brno places from the Apify recording. Places have no IČO, so the task's terms
    carry no registry predicate (§6); 17 pass, 3 borderline ones (no warehouse keyword) are rejected and never
    reach the buyer; no personal fields reach the bank."""
    import json as _json
    monkeypatch.setenv("SELLER_DATA", "maps")
    items = seller_mod.make_items(20, "honest")
    recorded = {it["placeId"] for it in _json.loads(seller_mod.RECORDED_MAPS.read_text())["items"]}
    assert len(items) == 20 and all(it["placeId"] in recorded for it in items)
    assert not any(k in it for it in items for k in ("phone", "phoneUnformatted", "reviews"))
    task = {**TASK, "criteria": {**TASK["criteria"], "registry_check": False}}
    deal_id, _ = deal(bank, asset="tCZK", task=task)
    bank.api.post(f"/api/deals/{deal_id}/pay")
    assert receipts.counts(deal_id) == {"accepted_items": 17, "rejected_items": 3, "pending_items": 0}
    assert bank.sender.money()[1] == ("capture", 12_500 * 17 // 20 * 90 // 100)  # rule B: 90 % of P at acceptance