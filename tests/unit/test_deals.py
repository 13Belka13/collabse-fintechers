# deals.pay and the agent API against the real seller app (x402 402 as the quote), with small fakes for
# streams A (mandate) and S (screening, verifier) until they land, and a fake chain sender.
# N03 swapped payTo -> COUNTERPARTY_NOT_IN_SCOPE, N04 swapped price -> QUOTE_MISMATCH: no tx, no reservation.
import sys
import time
import types

import httpx
import pytest
from eth_account import Account
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bank import api, chain, codes, config, db, deals, escrow, journal
from bank.codes import Code
from seller import app as seller_mod
from tests.unit.test_seller_x402 import TCZK, FakeFacilitator

BETA = "0x70E8B0ed46DC4FD558ee05bCEba06e5AAf00EBd1"


class FakeMandate(types.ModuleType):
    def __init__(self):
        super().__init__("bank.mandate")
        self.calls, self.verdict = [], codes.ok()

    def decide(self, mandate_id, deal, quote, screening, now=None):
        self.calls.append(("decide", deal["eur_cents"], quote["pay_to"], screening["level"]))
        return self.verdict

    def reserve(self, mandate_id, deal_id, eur_cents):
        self.calls.append(("reserve", eur_cents))
        db.run("INSERT INTO reservations (mandate_id, deal_id, amount, state, created_at) VALUES (?,?,?,?,?)",
               (mandate_id, deal_id, eur_cents, "reserved", time.time()))
        return codes.ok()

    def release(self, deal_id):
        self.calls.append(("release",))
        db.run("UPDATE reservations SET state='released' WHERE deal_id=?", (deal_id,))

    def commit(self, deal_id, eur_cents):
        self.calls.append(("commit", eur_cents))
        db.run("UPDATE reservations SET state='committed', amount=? WHERE deal_id=?", (eur_cents, deal_id))


class FakeSender:
    def __init__(self):
        self.sent = []

    def send(self, call, **kw):
        self.sent.append((call.fn_name, call.args, kw["idem_key"]))
        h = "0x%064x" % len(self.sent)
        db.run("INSERT INTO onchain_txs (tx_hash, kind, deal_id, status, idem_key, created_at) VALUES (?,?,?,?,?,?)",
               (h, kw["kind"], kw["deal_id"], "mined", kw["idem_key"], time.time()))
        return {"tx_hash": h, "link": chain.tx_link(h), "status": "mined"}


@pytest.fixture
def world(monkeypatch, seeded):
    monkeypatch.setenv("TCZK", TCZK)
    operator = Account.create()
    monkeypatch.setenv("BANK_OPERATOR_PK", operator.key.hex())
    monkeypatch.setenv("BANK_OPERATOR_ADDR", operator.address)  # the seller pins the escrow operator
    buyer = Account.create()
    monkeypatch.setenv("ALFA_PK", buyer.key.hex())
    config.reload()
    db.run("UPDATE companies SET wallet_address=? WHERE id='alfa'", (buyer.address,))
    for mid in ("m-alfa-1", "m"):  # the fake mandate module keeps no rows; deals.create checks the owner
        db.run("INSERT INTO mandates (mandate_id, company_id, current_version) VALUES (?, 'alfa', 1)", (mid,))
    from bank import tczk
    monkeypatch.setattr(tczk, "is_allowed", lambda addr, address=None: True)
    monkeypatch.setattr(tczk, "is_frozen", lambda addr, address=None: False)

    seller_app = seller_mod.create_app(FakeFacilitator())
    seller_client = TestClient(seller_app)
    monkeypatch.setattr(deals, "seller_http", lambda base_url: TestClient(seller_app))
    bank_app = FastAPI()
    bank_app.include_router(api.router)
    bank = TestClient(bank_app, headers={"x-agent-token": "dev-agent-alfa"})
    monkeypatch.setattr(seller_mod, "bank_post", lambda path, body, seller_id: bank.post(
        path, json=body, headers=seller_mod.seller_headers(seller_id)).raise_for_status())
    locked = {"capturable": 0}
    monkeypatch.setattr(escrow, "read_state", lambda h: {"collected": True, "refundable": 0, **locked})
    monkeypatch.setattr(seller_mod, "read_retry", lambda fn, ok: fn())

    sender = FakeSender()
    real_authorize = escrow.authorize

    def authorize(deal_id):  # the fake chain: an authorize locks maxAmount
        tx = real_authorize(deal_id)
        locked["capturable"] = escrow.stored_payment_info(deal_id)["maxAmount"]
        return tx
    monkeypatch.setattr(escrow, "authorize", authorize)
    monkeypatch.setattr(chain, "sender", lambda pk: sender)

    mandate = FakeMandate()
    screening = types.ModuleType("bank.screening")
    screening.cache = {"30181020": {"level": "ok", "reasons": [], "sources": [], "vat": {"payer": True}}}
    screening.cached = lambda ico: screening.cache.get(ico)
    screening.prefetch = lambda ico, wallet=None, website_url=None: None
    monkeypatch.setitem(sys.modules, "bank.mandate", mandate)
    monkeypatch.setitem(sys.modules, "bank.screening", screening)
    return types.SimpleNamespace(bank=bank, seller=seller_client, sender=sender, mandate=mandate,
                                 screening=screening, locked=locked)


def new_deal(w, asset="EURC"):
    deal_id = w.bank.post("/api/deals", json={"buyer_id": "alfa", "seller_id": "s-beta", "mandate_id": "m-alfa-1",
                                              "task": {"text": "20 warehouses in Brno"}, "asset": asset}).json()["deal_id"]
    reply = w.bank.post(f"/api/deals/{deal_id}/negotiate", json={"message": "20 warehouses in Brno?"}).json()
    return deal_id, reply


def assert_no_money_moved(w, deal_id):
    assert w.sender.sent == []
    assert db.rows("SELECT * FROM onchain_txs") == []
    assert db.rows("SELECT * FROM reservations WHERE deal_id=?", (deal_id,)) == []
    assert db.one("SELECT payment_info_json FROM deals WHERE deal_id=?", (deal_id,))["payment_info_json"] is None


def test_negotiation_proxy(world):
    deal_id, reply = new_deal(world)
    assert reply["offer"]["price"] == "5,00 €" and reply["offer"]["asset"] == "EURC"
    assert "pay_to" not in reply["offer"] and "0x" not in str(reply["offer"])  # agents get ids, not addresses
    deal = deals.get(deal_id)
    assert deal["state"] == "offered" and deal["amount"] == 5_000_000 and deal["offer"]["pay_to"] == BETA
    seller_entries = [e for e in journal.entries(deal_id) if e["actor"] == "agent:seller"]
    assert seller_entries and all(e["payload"]["source"] == "untrusted:seller" for e in seller_entries)


def test_a_slow_seller_turn_is_waited_for_then_answered_504(world, monkeypatch):
    """A live seller turn may take > 10 s on Bedrock: the proxy waits NEGOTIATE_TIMEOUT_S, then a clean 504."""
    asked = {}

    class Slow:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, path, json=None, timeout=None):
            asked["timeout"] = timeout
            raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(deals, "seller_http", lambda base_url: Slow())
    deal_id = world.bank.post("/api/deals", json={"buyer_id": "alfa", "seller_id": "s-beta", "mandate_id": "m-alfa-1",
                                                  "task": {"text": "20 warehouses in Brno"}}).json()["deal_id"]
    r = world.bank.post(f"/api/deals/{deal_id}/negotiate", json={"message": "Price?"})
    assert r.status_code == 504 and config.NEGOTIATE_TIMEOUT_S >= 20
    assert deals.get(deal_id)["state"] != "offered"


def test_injection_text_is_journalled_as_untrusted(world):
    world.seller.post("/mode", json={"seller_id": "s-beta", "mode": "injection"})
    deal_id, reply = new_deal(world)
    assert "SYSTEM:" in reply["reply_text"]
    e = next(e for e in journal.entries(deal_id) if e["event"] == "seller_reply")
    assert "SYSTEM:" in e["payload"]["reply_text"] and e["payload"]["source"] == "untrusted:seller"


@pytest.mark.parametrize("mode,code,reason", [("swap_payto", "COUNTERPARTY_NOT_IN_SCOPE", "payto_not_registered"),
                                              ("swap_price", "QUOTE_MISMATCH", "amount_not_offer")])
def test_swapped_402_is_refused_and_no_money_moves(world, mode, code, reason):  # N03 / N04
    deal_id, _ = new_deal(world)
    world.seller.post("/mode", json={"seller_id": "s-beta", "mode": mode})
    r = world.bank.post(f"/api/deals/{deal_id}/pay")
    assert r.status_code == 200
    view = r.json()
    assert view["status"] == "refused" and view["code"] == code and view["next_tool"] == "ask_human"
    assert [x["code"] for x in view["reasons"]] == [reason] and view["explanation"]
    assert world.mandate.calls == []  # refused before the mandate and before any reservation
    assert_no_money_moved(world, deal_id)
    assert deals.get(deal_id)["state"] == "refused"
    dec = db.one("SELECT * FROM decisions WHERE deal_id=?", (deal_id,))
    assert dec["outcome"] == "refused" and code in dec["codes_json"]
    e = next(e for e in journal.entries(deal_id) if e["event"] == "pay_refused")
    assert e["payload"]["code"] == code and e["payload"]["money_moved"] is False


def test_honest_pay_locks_escrow_and_seller_delivers(world):
    deal_id, _ = new_deal(world)
    view = world.bank.post(f"/api/deals/{deal_id}/pay").json()
    assert view["status"] == "ok" and view["code"] is None and "5,00 €" in view["explanation"]
    assert [s[0] for s in world.sender.sent] == ["authorize", "capture"]  # rule B: 90 % at acceptance
    assert world.sender.sent[0][2] == escrow.idem_key(deal_id, "authorize")
    assert world.mandate.calls[:2] == [("decide", 500, BETA, "ok"), ("reserve", 500)]
    pi = escrow.stored_payment_info(deal_id)
    assert pi["receiver"] == BETA and pi["maxAmount"] == 5_000_000 and pi["token"] == config.EURC
    deal = deals.get(deal_id)
    assert deal["state"] == "delivered"  # the seller checked the escrow and delivered 20 items through the bank
    assert db.one("SELECT COUNT(*) n FROM items WHERE deal_id=? AND status='accepted'", (deal_id,))["n"] == 20
    assert deal["terms"]["rule"] == "B" and deal["terms"]["b_slot"] == 1 and deal["terms"]["tier"] == "T1"
    again = world.bank.post(f"/api/deals/{deal_id}/pay").json()  # a second pay never locks twice
    assert again["status"] == "ok" and len(world.sender.sent) == 2


def test_tczk_deal_uses_tczk_price(world):
    deal_id, reply = new_deal(world, asset="tCZK")
    assert reply["offer"]["price"] == "125,00 Kč"
    assert world.bank.post(f"/api/deals/{deal_id}/pay").json()["status"] == "ok"
    pi = escrow.stored_payment_info(deal_id)
    assert pi["token"] == TCZK and pi["maxAmount"] == 12_500
    assert world.mandate.calls[1] == ("reserve", 500)  # 125,00 Kč = 5,00 € at the demo rate


def test_rule_b_captures_at_acceptance_with_the_pis_idem_key(world):
    deal_id, _ = new_deal(world)
    world.bank.post(f"/api/deals/{deal_id}/pay")
    assert [(s[0], s[2]) for s in world.sender.sent] == [("authorize", escrow.idem_key(deal_id, "authorize")),
                                                         ("capture", escrow.idem_key(deal_id, "capture:1"))]
    assert world.sender.sent[1][1][1] == 4_500_000
    assert ("commit", 450) in world.mandate.calls


def _offer_patch(world, monkeypatch, **change):
    real = seller_mod.offer_rule
    monkeypatch.setattr(seller_mod, "offer_rule", lambda terms, cents, req=None, rule=None: {**real(terms, cents, req, rule), **change})


@pytest.mark.parametrize("change,reason", [
    ({"rule": "D"}, "rule_unknown"), ({"b_pct": 50}, "b_pct_unknown"), ({"window_s": 1}, "window_out_of_bounds"),
    ({"window_s": 10**7}, "window_out_of_bounds"), ({"rule": "C"}, "c_needs_source")])
def test_offer_terms_below_the_rules_are_refused_before_the_mandate(world, monkeypatch, change, reason):
    _offer_patch(world, monkeypatch, **change)
    deal_id, _ = new_deal(world)
    view = world.bank.post(f"/api/deals/{deal_id}/pay").json()
    assert view["code"] == "TERMS_MISMATCH" and view["next_tool"] == "negotiate"
    assert [r["code"] for r in view["reasons"]] == [reason]
    assert world.mandate.calls == [] and world.sender.sent == []


def test_a_dropped_confirmed_predicate_is_refused(world):
    deal_id, _ = new_deal(world)
    offer = deals.get(deal_id)["offer"]
    offer["predicates"] = offer["predicates"][1:]
    db.run("UPDATE deals SET offer_json=? WHERE deal_id=?", (db.dumps(offer), deal_id))
    view = world.bank.post(f"/api/deals/{deal_id}/pay").json()
    assert view["code"] == "TERMS_MISMATCH" and view["reasons"][0]["code"] == "predicates_not_verbatim"


def test_b_unavailable_needs_a_person_then_b_without_claims(world, monkeypatch):
    from bank import disputes
    monkeypatch.setattr(disputes, "b_availability", lambda *a, **k: {
        "available": False, "reason": "b_pair_limit", "text": "Limit reached."})
    deal_id, _ = new_deal(world)
    view = world.bank.post(f"/api/deals/{deal_id}/pay").json()
    assert view["code"] == "CONFIRMATION_REQUIRED" and world.sender.sent == []
    db.run("UPDATE deals SET confirmed_by='human:anna' WHERE deal_id=?", (deal_id,))
    view = world.bank.post(f"/api/deals/{deal_id}/pay").json()
    assert view["status"] == "ok" and deals.get(deal_id)["terms"]["b_slot"] == 0


def test_mandate_refusal_reserves_nothing(world):
    deal_id, _ = new_deal(world)
    world.mandate.verdict = codes.refuse(Code.AMOUNT_ABOVE_LIMIT, [codes.reason("per_deal_max", True, "Above 10 €.")])
    view = world.bank.post(f"/api/deals/{deal_id}/pay").json()
    assert view["code"] == "AMOUNT_ABOVE_LIMIT" and view["next_tool"] == "amend_mandate"
    assert [c[0] for c in world.mandate.calls] == ["decide"]
    assert_no_money_moved(world, deal_id)


def test_b_slot_is_asked_only_after_the_mandate_and_counterparty_checks(world, monkeypatch):
    from bank import disputes
    asked = []
    monkeypatch.setattr(disputes, "b_availability", lambda *a, **k: asked.append(a) or {
        "available": False, "reason": "b_pair_limit", "text": "Limit reached."})
    deal_id, _ = new_deal(world)
    world.mandate.verdict = codes.refuse(Code.AMOUNT_ABOVE_LIMIT, [codes.reason("per_deal_max", True, "Above 10 €.")])
    view = world.bank.post(f"/api/deals/{deal_id}/pay").json()
    assert view["code"] == "AMOUNT_ABOVE_LIMIT" and asked == []  # the refusal is not hidden behind a slot question


def test_vat_risk_waits_for_human(world):
    deal_id, _ = new_deal(world)
    world.mandate.verdict = codes.refuse(Code.VAT_LIABILITY_RISK, [codes.reason("vat_payer_eurc", True, "§109")])
    view = world.bank.post(f"/api/deals/{deal_id}/pay").json()
    assert view["status"] == "requires_user_action" and deals.get(deal_id)["state"] == "awaiting_human"
    assert_no_money_moved(world, deal_id)
    world.mandate.verdict = codes.ok()  # human acknowledged: the agent calls pay again
    assert world.bank.post(f"/api/deals/{deal_id}/pay").json()["status"] == "ok"


def test_confirm_or_ack_without_a_pending_question_is_refused_and_does_not_pay(world):
    deal_id, _ = new_deal(world)  # a fresh 'offered' deal: the agent never asked to pay
    for fn in (deals.confirm, deals.ack_vat):
        with pytest.raises(deals.DealError) as e:
            fn(deal_id, "human:alfa")
        assert e.value.status == 409 and "pending" in str(e.value)
    assert deals.get(deal_id)["state"] == "offered" and deals.get(deal_id)["confirmed_by"] is None
    assert_no_money_moved(world, deal_id)
    world.mandate.verdict = codes.refuse(Code.VAT_LIABILITY_RISK, [codes.reason("vat_payer_eurc", True, "§109")])
    world.bank.post(f"/api/deals/{deal_id}/pay")  # the bank asks about VAT, not for a confirmation
    with pytest.raises(deals.DealError, match="no confirmation is pending"):
        deals.confirm(deal_id, "human:alfa")
    assert_no_money_moved(world, deal_id)


def test_confirm_pays_only_after_the_bank_asked_for_it(world):
    deal_id, _ = new_deal(world)
    world.mandate.verdict = codes.refuse(Code.CONFIRMATION_REQUIRED, [codes.reason("confirm_above", True, "> 6 €")])
    assert world.bank.post(f"/api/deals/{deal_id}/pay").json()["code"] == "CONFIRMATION_REQUIRED"
    with pytest.raises(deals.DealError, match="no VAT question"):
        deals.ack_vat(deal_id, "human:alfa")
    world.mandate.verdict = codes.ok()
    assert deals.confirm(deal_id, "human:alfa")["status"] == "ok" and world.sender.sent[0][0] == "authorize"
    assert deals.get(deal_id)["confirmed_by"] == "human:alfa"


def test_no_cached_screening_fails_closed_with_neutral_text(world):
    deal_id, _ = new_deal(world)
    world.screening.cache.clear()
    view = world.bank.post(f"/api/deals/{deal_id}/pay").json()
    assert view["code"] == "UNDER_REVIEW" and view["explanation"] == codes.NEUTRAL_CLIENT_TEXT
    assert view["reasons"] == [] and "screening" not in str(view)  # the non-public reason stays inside the bank
    assert_no_money_moved(world, deal_id)


def test_seller_rejecting_escrow_returns_the_lock(world, monkeypatch):
    deal_id, _ = new_deal(world)
    monkeypatch.setattr(seller_mod, "verify_escrow", lambda *a, **k: "receiver_not_our_pay_to")
    view = world.bank.post(f"/api/deals/{deal_id}/pay").json()
    assert view["code"] == "QUOTE_MISMATCH" and view["reasons"][0]["code"] == "seller_rejected_escrow"
    assert [s[0] for s in world.sender.sent] == ["authorize", "void"] and ("release",) in world.mandate.calls
    assert deals.get(deal_id)["state"] == "closed"


def test_a_lagging_escrow_read_at_the_seller_never_voids_the_lock(world, monkeypatch):
    deal_id, _ = new_deal(world)
    monkeypatch.setattr(seller_mod, "verify_escrow", lambda *a, **k: "escrow_capturable_not_price")
    view = world.bank.post(f"/api/deals/{deal_id}/pay").json()
    assert view["status"] == "ok" and [s[0] for s in world.sender.sent] == ["authorize"]
    assert deals.get(deal_id)["state"] == "authorized"  # pay again resends the notice (_already_paid)


def test_pay_before_offer_is_409(world):
    deal_id = world.bank.post("/api/deals", json={"buyer_id": "alfa", "seller_id": "s-beta", "mandate_id": "m",
                                                  "task": "x"}).json()["deal_id"]
    assert world.bank.post(f"/api/deals/{deal_id}/pay").status_code == 409
    assert world.bank.post("/api/deals/d-nope/pay").status_code == 404


def test_sellers_and_statement(world):
    sellers = {s["seller_id"] for s in world.bank.get("/api/sellers?category=data").json()}
    assert sellers == {"s-beta", "s-gama", "s-delta", "s-epsilon"}
    check = world.bank.get("/api/sellers/s-beta/check").json()
    assert check["level"] == "ok" and check["public_reasons"] == []
    deal_id, _ = new_deal(world)
    world.bank.post(f"/api/deals/{deal_id}/pay")
    st = world.bank.get("/api/statement?mandate_id=m-alfa-1").json()
    assert st["committed_eur_cents"] == 450 and st["deals"][0]["deal_id"] == deal_id  # rule B: 90 % at acceptance
