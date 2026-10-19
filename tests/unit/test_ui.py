# Bank UI pages render via TestClient on seeded data (SPEC §13): /bank feed with the client/compliance toggle
# (client text only through codes.client_view: a non-public reason never shows), /mandate, /deals, /deals/{id},
# the dispute card with deadlines and silence outcomes, human actions through the forms, /statement + reconcile,
# AI-generated judge text labelled (AI Act Art. 50).
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bank import codes, db, disputes, escrow, explain, timing, ui
from bank.codes import Code
from tests.unit.test_disputes import T0, make_deal, world  # noqa: F401  (fixture)


@pytest.fixture
def client(world):  # noqa: F811
    app = FastAPI()
    app.include_router(ui.router)
    return TestClient(app)


def record(deal_id, decision, ms=3.2):
    full = decision.get("explanation", "") + "".join(f"\n- [{r['code']}] {r['text']}" for r in decision["reasons"])
    db.run("INSERT INTO decisions (deal_id, kind, outcome, codes_json, evidence_json, explanation_client,"
           " explanation_compliance, ms, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
           (deal_id, "pay", decision["status"], db.dumps([decision["code"]] + [r["code"] for r in decision["reasons"]]),
            db.dumps(decision["reasons"]), codes.client_view(decision)["explanation"], full, ms, time.time()))


def test_feed_client_view_hides_non_public_reasons(client):
    hidden = codes.refuse(Code.COUNTERPARTY_NOT_TRUSTED, [codes.reason(
        "sanctions_near_match", False, "Name is a 92 % match to an EU sanctions list entry.",
        "https://data.europa.eu/sanctions")], explanation="Refused.")
    record("d-hidden", hidden)
    record("d-delta", codes.refuse(Code.COUNTERPARTY_NOT_TRUSTED, [codes.reason(
        "ico_mismatch_on_website", True, "The IČO on delta-data.example differs from ARES.",
        "https://ares.gov.cz/x")], explanation="The seller did not pass the bank's checks."))
    timing.log_llm("judge", None, "anthropic.claude-haiku-4-5", {"input_tokens": 1000, "output_tokens": 100}, 50)
    with timing.timed("decision", "d-delta"):
        pass
    c = client.get("/bank").text
    assert "sanctions" not in c.lower() and "92 %" not in c
    assert codes.NEUTRAL_CLIENT_TEXT.split(" / ")[0] in c  # the client sees only "under review"
    assert "ico_mismatch_on_website" in c and "Refused" in c
    assert "Decision p95" in c and "$0.0015" in c  # 1000 in + 100 out at Haiku 4.5 prices
    comp = client.get("/bank?view=compliance").text
    assert "sanctions_near_match" in comp and "non-public" in comp and "hidden from client" in comp
    assert client.get("/bank/feed?view=client").status_code == 200


def test_dispute_card_shows_steps_deadlines_silence_and_ai_label(client, world):  # noqa: F811
    world.flagged.add("demo-place-03")
    deal_id = make_deal("fake_data")
    disputes.settle(deal_id, now=time.time())
    page = client.get(f"/deals/{deal_id}/dispute").text
    assert explain.AI_LABEL in page  # the judge reason is AI-generated
    assert "Seller silent → the rejection stands" in page and "Buyer silent → item accepted" in page
    assert "data-deadline" in page and "violated" in page and "agreed: Brno" in page
    assert "Accept the item" in page and "Object to the rejection" in page
    # buyer rejects without evidence -> error banner, nothing changes
    r = client.post(f"/deals/{deal_id}/dispute/demo-place-03/review",
                    data={"who": "human:alfa", "action": "reject", "criterion": "category", "evidence": ""})
    assert r.status_code == 200 and "needs evidence" in r.text
    r = client.post(f"/deals/{deal_id}/dispute/demo-place-19/objection",
                    data={"who": "human:gama", "statement": "The warehouse moved to Brno last month."})
    assert "Bank / arbiter decides" in r.text and "written by the seller" in r.text
    r = client.post(f"/deals/{deal_id}/dispute/demo-place-19/decide",
                    data={"who": "human:bank-officer", "verdict": "accept", "note": "Listing out of date."})
    assert "accepted · seller paid" in r.text
    d = disputes.get(deal_id)
    assert next(i for i in d["items"] if i["placeId"] == "demo-place-19")["decided_by"] == "human:bank-officer"


def test_deal_deals_statement_mandate_pages(client, world):  # noqa: F811
    deal_id = make_deal("honest")
    db.run("INSERT INTO journal (ts, actor, event, deal_id, payload_json, prev_hash, hash) VALUES (?,?,?,?,?,?,?)",
           (time.time(), "bank", "escrow_authorized", deal_id, db.dumps({"amount": 5_000_000, "tx_hash": "0x" + "1" * 64}),
            "x", "y"))  # what escrow.authorize journals
    disputes.settle(deal_id, now=time.time())
    page = client.get(f"/deals/{deal_id}").text
    assert "Paid to the seller" in page and "4,75 €" in page and "0,25 €" in page  # holdback still in escrow
    assert "Acceptance criteria" in page and "review 60 s" in page
    assert deal_id in client.get("/deals").text
    st = client.get("/statement").text
    assert "4,75 €" in st and "Reconcile" in st
    # reconcile against the (fake) chain: 0,25 € still capturable
    escrow_state = {"collected": True, "capturable": 250_000, "refundable": 4_750_000}
    import bank.escrow as esc
    orig, esc.read_state = esc.read_state, lambda h: escrow_state
    try:
        assert "reconciled" in client.get("/statement/reconcile").text
        escrow_state["capturable"] = 0
        assert "mismatch" in client.get("/statement/reconcile").text
        assert "matches our ledger" not in client.get(f"/deals/{deal_id}/escrow-state").text
    finally:
        esc.read_state = orig
    m = client.get("/mandate").text
    assert "policy_hash" in m and "Budget left" in m


def test_mandate_lists_vat_acknowledgement_and_acts_in_process(client, world, monkeypatch):  # noqa: F811
    deal_id = make_deal("honest", deal_id="d-vat")
    db.run("UPDATE deals SET state='awaiting_human' WHERE deal_id=?", (deal_id,))
    record(deal_id, codes.refuse(Code.VAT_LIABILITY_RISK, [codes.reason(
        "vat_payer_paid_in_eurc", True, "Beta is a Czech VAT payer; paying in EURC is not to a published account.",
        "https://www.zakonyprolidi.cz/cs/2004-235#p109")]))
    page = client.get("/mandate").text
    assert "Acknowledge the VAT risk for Beta s.r.o. in EURC" in page
    paid = []
    monkeypatch.setattr(ui.deals, "ack_vat", lambda d, by: paid.append((d, by)) or codes.ok(explanation="Locked."),
                        raising=False)
    r = client.post(f"/mandate/deals/{deal_id}/ack-vat", data={"who": "human:alfa"})
    assert paid == [(deal_id, "human:alfa")] and "ok: Locked." in r.text
    assert client.post(f"/mandate/deals/{deal_id}/ack-vat", data={"who": "agent:buyer"}).status_code == 400


def test_alarm_banner_for_an_open_deal_near_expiry(client, world):  # noqa: F811
    deal_id = make_deal("honest", deal_id="d-late", expiry_in=900)
    disputes.settle(deal_id, now=time.time())
    assert "ALARM" in client.get("/bank/feed").text
    assert escrow  # imported for the reconcile patch above


def test_retry_delivery_resends_the_seller_notice_only_for_a_locked_deal(client, world, monkeypatch):  # noqa: F811
    deal_id = make_deal("honest", deal_id="d-wait", state="authorized")
    db.run("DELETE FROM deliveries WHERE deal_id=?", (deal_id,))
    assert "Remind the seller to deliver" in client.get(f"/deals/{deal_id}").text
    calls = []
    monkeypatch.setattr(ui.deals, "pay", lambda d: calls.append(d) or codes.ok())
    assert client.post(f"/deals/{deal_id}/retry-delivery").status_code == 200 and calls == [deal_id]
    db.run("UPDATE deals SET state='closed' WHERE deal_id=?", (deal_id,))
    assert client.post(f"/deals/{deal_id}/retry-delivery").status_code == 409 and calls == [deal_id]
