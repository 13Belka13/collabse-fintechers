# End to end through the real modules of every stream: agent API (P) -> seller x402 quote (P) -> screening
# with recorded registry fixtures (S) -> mandate decision + reservation (A) -> escrow (P) -> seller checks the
# escrow and delivers -> verifier (S) -> capture/void -> budget. Only the chain is faked (sender + escrow state),
# so every assertion on money is about what the bank *sent*; the anvil-fork tests cover the contracts themselves.
import time
import types

import pytest
from eth_account import Account
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bank import api, chain, codes, config, db, deadlines, deals, escrow, explain, journal, llm, mandate, money, screening, tczk
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
    monkeypatch.setattr(seller_mod, "bank_post", lambda path, body: client.post(
        path, json=body, headers={"x-seller-token": config.SELLER_TOKEN}).raise_for_status())
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
    # 20/20 accepted: 95 % now, the 5 % holdback stays in escrow until its window ends (SPEC §9.3)
    assert bank.sender.money() == [("authorize", 5_000_000), ("capture", 4_750_000)]
    assert deals.get(deal_id)["state"] == "settled" and bank.locked["capturable"] == 250_000
    deadlines.tick(now=time.time() + 3600)  # holdback window over, no claim: the timer pays it to the seller
    assert bank.sender.money()[-1] == ("capture", 250_000) and bank.locked["capturable"] == 0
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


def test_gama_fake_data_18_of_20(bank):
    deal_id, _ = deal(bank, seller_id="s-gama", mode="fake_data")
    view = bank.api.post(f"/api/deals/{deal_id}/pay").json()
    if view["code"] == "VAT_LIABILITY_RISK":  # Gama's recorded VAT status decides; acknowledge if needed
        view = bank.api.post(f"/api/deals/{deal_id}/ack-vat", json={"by": "anna"}, headers=HUMAN).json()
    assert view["status"] == "ok"
    # 18 accepted minus the 5 % holdback now; the 2 rejected items wait for the seller's objection window
    assert bank.sender.money() == [("authorize", 5_000_000), ("capture", 4_275_000)]
    assert deals.get(deal_id)["state"] == "disputed"
    deadlines.tick(now=time.time() + 3600)  # seller silent, holdback over: capture the holdback, then return the rest
    assert bank.sender.money()[2:] == [("capture", 225_000), ("void", None)] and bank.locked["voided"] == 500_000
    b = budget()
    assert b["committed_eur_cents"] == 450 and b["remaining_eur_cents"] == 2550


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


# ---- the review findings, each pinned ------------------------------------------------------------------

def test_seller_criteria_are_ignored_the_buyers_decide_the_payment(bank, monkeypatch):
    """A seller offering junk criteria (no website, any city, rating 0) would grade its own fake items as good."""
    junk = {"count": 20, "min_rating": 0, "min_rating_exclusive": 0, "require_website": False, "city": "Anywhere",
            "postcode_prefix": "0"}
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
                body["offer"]["criteria"] = junk
                return types.SimpleNamespace(json=lambda: body, raise_for_status=lambda: None, status_code=200)
            return r

        def get(self, *a, **kw):
            return self.c.get(*a, **kw)

    monkeypatch.setattr(deals, "seller_http", lambda base: EvilSeller(real(base)))
    deal_id, offer = deal(bank, asset="tCZK", mode="fake_data")
    assert deals.get(deal_id)["criteria"]["city"] == "Brno" and offer["criteria"]["require_website"] is True
    bank.api.post(f"/api/deals/{deal_id}/pay")
    # 18 of 20 by the buyer's rules (95 % now: 10 688 of 11 250 haléřů), not 20 of 20 by the seller's
    assert ("capture", 10_688) in bank.sender.money()


def test_invalid_buyer_criteria_fall_back_to_defaults():
    c = deals.buyer_criteria({"criteria": {"count": "lots", "city": ""}})
    assert c["count"] == 20 and c["city"] == "Brno" and c["postcode_prefix"] == "6"
    assert deals.buyer_criteria({"criteria": {"city": "Praha"}})["postcode_prefix"] is None


def test_mandate_of_another_company_is_refused(bank):
    db.run("INSERT INTO mandates (mandate_id, company_id, current_version) VALUES ('m-gama', 'gama', 1)")
    assert bank.api.post("/api/deals", json={"seller_id": "s-beta", "mandate_id": "m-gama",
                                             "task": TASK}).status_code == 403
    assert bank.api.post("/api/deals", json={"buyer_id": "gama", "seller_id": "s-beta", "mandate_id": "m-alfa-1",
                                             "task": TASK}).status_code == 403
    assert TestClient(bank.app).post("/api/deals", json={"seller_id": "s-beta", "mandate_id": "m-alfa-1",
                                                         "task": TASK}).status_code == 401


def test_delivery_needs_the_seller_token_and_drops_phones(bank, monkeypatch):
    monkeypatch.setattr(deals, "auto_settle", lambda deal_id: None)  # storage only; settlement is covered above
    deal_id, _ = deal(bank, asset="tCZK")
    db.run("UPDATE deals SET state='authorized' WHERE deal_id=?", (deal_id,))
    item = {"placeId": "p1", "title": "Sklad", "phone": "+420 000 000 000", "reviews": [{"name": "Reviewer"}]}
    assert bank.api.post(f"/api/deals/{deal_id}/delivery", json={"items": [item]}).status_code == 401
    r = bank.api.post(f"/api/deals/{deal_id}/delivery", json={"items": [item]},
                      headers={"x-seller-token": config.SELLER_TOKEN})
    assert r.status_code == 200
    stored = db.one("SELECT items_json FROM deliveries WHERE deal_id=?", (deal_id,))["items_json"]
    assert "phone" not in stored and "Reviewer" not in stored


def test_frozen_buyer_is_stopped_before_anything_is_reserved(bank):
    db.run("INSERT INTO frozen_wallets (address, case_ref, frozen_at) VALUES (?,?,?)",
           (bank.buyer.address, "case-1", time.time()))
    deal_id, _ = deal(bank, asset="tCZK")
    view = bank.api.post(f"/api/deals/{deal_id}/pay").json()
    assert view["code"] == "UNDER_REVIEW" and "frozen" not in str(view).lower()  # the client is not tipped off
    assert bank.sender.sent == [] and db.rows("SELECT * FROM reservations WHERE deal_id=?", (deal_id,)) == []


def test_failed_capture_returns_the_lock_and_frees_the_budget(bank):
    bank.locked["fail_capture"] = True
    deal_id, _ = deal(bank, asset="tCZK")
    assert bank.api.post(f"/api/deals/{deal_id}/pay").json()["status"] == "ok"  # locked; settlement failed after
    assert deals.get(deal_id)["state"] == "closed" and [s[0] for s in bank.sender.sent] == ["authorize", "void"]
    assert budget()["remaining_eur_cents"] == 3000


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

def test_capture_refused_at_gas_estimation_still_returns_the_lock(bank):
    bank.locked["fail_capture"] = "estimate"
    deal_id, _ = deal(bank, asset="tCZK")
    bank.api.post(f"/api/deals/{deal_id}/pay")
    assert deals.get(deal_id)["state"] == "closed" and [s[0] for s in bank.sender.sent] == ["authorize", "void"]
    assert budget()["remaining_eur_cents"] == 3000


def test_a_new_offer_needs_a_new_human_confirmation(bank, monkeypatch):
    task = {**TASK, "budget": {"value": "1000", "currency": "EUR"}, "criteria": {**TASK["criteria"], "count": 32}}
    deal_id, _ = deal(bank, asset="tCZK", task=task)
    with monkeypatch.context() as m:
        m.setattr(screening, "cached", lambda ico: None)  # the confirmed pay is refused for another reason
        view = bank.api.post(f"/api/deals/{deal_id}/confirm", json={"by": "anna"}, headers=HUMAN).json()
        assert view["code"] == "UNDER_REVIEW"
    bank.api.post(f"/api/deals/{deal_id}/negotiate", json={"message": "and 38?"})  # the seller re-quotes
    assert deals.get(deal_id)["confirmed_by"] is None
    assert bank.api.post(f"/api/deals/{deal_id}/pay").json()["code"] == "CONFIRMATION_REQUIRED"
    assert bank.sender.sent == []


def test_settle_can_be_retried_after_a_failed_void(bank, monkeypatch):
    monkeypatch.setattr(deals, "auto_settle", lambda deal_id: None)
    deal_id, _ = deal(bank, asset="tCZK", seller_id="s-gama", mode="fake_data")
    bank.api.post(f"/api/deals/{deal_id}/pay")
    bank.locked["fail_void_once"] = True
    with pytest.raises(TimeoutError):
        deals.settle(deal_id)
    out = deals.settle(deal_id)  # the retry: same capture (idem key), no second commit, then the void
    assert out["state"] == "settled" and [s[0] for s in bank.sender.sent] == ["authorize", "capture", "void"]
    b = budget()
    assert b["committed_eur_cents"] == 450 and b["remaining_eur_cents"] == 2550


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
    assert "asked again" in again["explanation"] and deals.get(deal_id)["state"] == "settled"
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
    once (same idempotency key); the dispute record and the budget must count it once too."""
    from bank import disputes
    deal_id, _ = deal(bank, asset="tCZK", seller_id="s-gama", mode="fake_data")
    bank.api.post(f"/api/deals/{deal_id}/pay")
    d = disputes.get(deal_id)
    before, committed = d["captured"], budget()["committed_eur_cents"]
    pending = {"kind": "capture", "amount": 250, "n": d["capture_n"] + 1}
    db.run("UPDATE disputes SET pending_json=? WHERE deal_id=?", (db.dumps(pending), deal_id))
    disputes._execute(deals.get(deal_id), pending, "timer", "test")   # the timer thread
    disputes._execute(deals.get(deal_id), pending, "runner", "test")  # another process with a stale view
    assert disputes.get(deal_id)["captured"] == before + 250
    assert [s for s in bank.sender.money() if s == ("capture", 250)] == [("capture", 250)]
    assert budget()["committed_eur_cents"] - committed == money.to_eur_cents("tCZK", before + 250) - money.to_eur_cents("tCZK", before)
    assert any(e["event"] == "dispute.already_booked" for e in journal.entries(deal_id))
