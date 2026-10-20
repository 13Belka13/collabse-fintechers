# P3: a person approves an agent's payment from a phone. CONFIRMATION_REQUIRED -> /story shows a QR code to
# /approve/{deal_id}?t=<token>; the token is one-time, for one deal, 10 minutes, stored only as a hash; a valid one
# confirms through deals.confirm exactly once, an expired, used, foreign or missing one moves nothing. The Caddyfile
# lets only /health and /approve/* past the UI password. Same harness as test_integration (real modules, fake chain).
import re

import pytest

from bank import approve, config, db, deals, journal, mandate, ui
from scripts import demo_scenarios
from tests.unit.test_integration import TASK, bank, deal  # noqa: F401  (bank is a fixture)

JURY_TASK = {**TASK, "budget": {"value": "1000", "currency": "EUR"}}


@pytest.fixture
def web(bank):  # noqa: F811
    bank.app.include_router(ui.router)
    bank.app.include_router(approve.router)
    return bank


def pending_deal(b, seller_id="s-gama"):
    """Gama will not go below 8,00 € (the "Jury approves" scenario): above the 6 € threshold, within the 10 € limit."""
    b.seller.post("/mode", json={"seller_id": seller_id, "mode": "honest", "floor_eur_cents": 800}).raise_for_status()
    deal_id, offer = deal(b, seller_id=seller_id, task=JURY_TASK)
    assert offer["price_eur_cents"] == 800
    view = b.api.post(f"/api/deals/{deal_id}/pay").json()
    assert view["code"] == "CONFIRMATION_REQUIRED" and b.sender.sent == []
    return deal_id


def qr_token(b, deal_id) -> str:
    html = b.api.get(f"/story/panel?deal_id={deal_id}").text
    assert "Scan to approve from your phone" in html and "<svg" in html
    return re.search(rf"/approve/{deal_id}\?t=([A-Za-z0-9_-]+)", html).group(1)


def authorizes(b):
    return [m for m in b.sender.money() if m[0] == "authorize"]


def test_valid_token_confirms_exactly_once_and_story_shows_the_jury(web):
    deal_id = pending_deal(web)
    token = qr_token(web, deal_id)
    assert qr_token(web, deal_id) == token  # the QR stays the same between refreshes of /story
    assert token not in str(db.rows("SELECT * FROM approvals"))  # only the hash is stored
    page = web.api.get(f"/approve/{deal_id}?t={token}")
    assert page.status_code == 200
    for text in ("Alfa s.r.o.", "8,00 €", "Gama s.r.o.", "20 warehouse records", "6,00 €", "Approve", "Decline"):
        assert text in page.text
    assert authorizes(web) == []  # opening the page (a phone's link preview too) moves nothing

    done = web.api.post(f"/approve/{deal_id}", data={"t": token, "action": "approve"})
    assert done.status_code == 200 and "Approved. Thank you!" in done.text and "locked in escrow" in done.text
    assert "human:jury" not in done.text and "a jury member" in done.text
    assert authorizes(web) == [("authorize", 8_000_000)]
    assert deals.get(deal_id)["confirmed_by"] == approve.BY
    assert [e["actor"] for e in journal.entries(deal_id, limit=500) if e["event"] == "payment_confirmed"] == [approve.BY]

    again = web.api.post(f"/approve/{deal_id}", data={"t": token, "action": "approve"})
    assert again.status_code == 409 and "already used" in again.text
    assert web.api.get(f"/approve/{deal_id}?t={token}").status_code == 410
    assert authorizes(web) == [("authorize", 8_000_000)]  # exactly once

    story = web.api.get(f"/story/panel?deal_id={deal_id}").text
    assert "Confirmed by a jury member" in story and "escrow safe" in story and "Scan to approve" not in story
    assert "Note: confirmed by a jury member." in story


def test_expired_reused_foreign_and_missing_tokens_move_nothing(web):
    a, b = pending_deal(web), pending_deal(web)
    token_a, token_b = qr_token(web, a), qr_token(web, b)
    # foreign: deal A's token does not open or approve deal B; a missing or made-up token neither
    for t in (token_a, "", "x" * 22):
        page = web.api.get(f"/approve/{b}?t={t}")
        assert page.status_code == 403 and "not valid" in page.text and "Gama" not in page.text  # nothing leaks
        assert web.api.post(f"/approve/{b}", data={"t": t, "action": "approve"}).status_code == 403
    # expired: 10 minutes
    exp = db.one("SELECT expires_at FROM approvals WHERE deal_id=?", (a,))["expires_at"]
    assert approve.check(a, token_a, now=exp - 1) is None and approve.check(a, token_a, now=exp) == "expired"
    with pytest.raises(deals.DealError, match="expired"):
        approve.act(a, token_a, "approve", now=exp + 1)
    assert authorizes(web) == [] and deals.get(a)["confirmed_by"] is None and deals.get(b)["confirmed_by"] is None
    # in its last minute the QR on /story gets a new token; one a phone has just scanned still works until it expires
    new, _, url = approve.issue(a, "https://demo.example", now=exp - 30)
    assert new != token_a and url == f"https://demo.example/approve/{a}?t={new}"
    assert approve.check(a, token_a, now=exp - 20) is None and approve.check(a, new, now=exp + 2) is None
    assert approve.issue(a, "https://demo.example", now=exp - 20)[0] == new  # and stays the same between refreshes
    # reused: one approval per deal — the first use spends every token of the deal, even when two phones race
    assert approve.use(a, token_a, now=exp - 10) is None
    assert approve.use(a, token_a, now=exp - 10) == "used" and approve.check(a, new, now=exp - 10) == "used"
    assert approve.check(b, token_b) is None  # another deal's token is untouched
    assert authorizes(web) == []


def test_decline_moves_nothing_and_the_story_says_so(web):
    deal_id = pending_deal(web)
    token = qr_token(web, deal_id)
    done = web.api.post(f"/approve/{deal_id}", data={"t": token, "action": "decline"})
    assert done.status_code == 200 and "Declined. No money moved." in done.text
    assert deals.get(deal_id)["state"] == "refused" and web.sender.sent == []
    assert mandate.summary("m-alfa-1")["reserved_eur_cents"] == 0
    story = web.api.get(f"/story/panel?deal_id={deal_id}").text
    assert "A jury member declined the payment." in story and "No money moved" in story
    assert "Scan to approve" not in story
    assert web.api.post(f"/approve/{deal_id}", data={"t": token, "action": "approve"}).status_code == 409
    with pytest.raises(deals.DealError):  # only a deal that waits for a person can be declined
        deals.decline(deal_id, "human:anna")


def test_no_qr_unless_the_bank_waits_for_a_confirmation(web):
    deal_id, _ = deal(web)  # Beta in EURC: the bank first asks about the VAT risk, not for a confirmation
    assert web.api.post(f"/api/deals/{deal_id}/pay").json()["code"] == "VAT_LIABILITY_RISK"
    assert "Scan to approve" not in web.api.get(f"/story/panel?deal_id={deal_id}").text
    assert approve.story_qr(deal_id) is None and approve.pending(deal_id) is None
    with pytest.raises(deals.DealError, match="invalid"):
        approve.act(deal_id, "anything", "approve")


def test_mandate_button_stays_the_fallback(web):
    deal_id = pending_deal(web)
    qr_token(web, deal_id)
    r = web.api.post(f"/mandate/deals/{deal_id}/confirm", headers={"x-human-token": config.HUMAN_TOKEN},
                     follow_redirects=False)
    assert r.status_code == 303 and authorizes(web) == [("authorize", 8_000_000)]
    assert approve.pending(deal_id) is None


def test_price_floor_lasts_one_run_of_the_seller(web):
    pending_deal(web)  # Gama at 8,00 €
    web.seller.post("/mode", json={"seller_id": "s-gama", "mode": "fake_data"}).raise_for_status()  # next scenario
    _, offer = deal(web, seller_id="s-gama", task=JURY_TASK)
    assert offer["price_eur_cents"] == 500  # back to the usual minimum: the floor does not leak into other scenarios


def test_qr_svg_carries_the_url():
    url = "https://63-182-160-195.sslip.io/approve/d-0123456789?t=abc_DEF-123"
    svg = approve.qr_svg(url)
    assert svg.startswith("<svg") and f'aria-label="QR code: {url}"' in svg and "<path" in svg
    assert "<image" not in svg  # vector paths, no embedded bitmap


def test_jury_scenario_sits_between_the_threshold_and_the_limit():
    s = demo_scenarios.SCENARIOS["jury"]
    p = mandate.DEFAULTS
    assert mandate.cents(p["confirm_above"]) < s["floor"] <= mandate.cents(p["per_deal_max"]) and s["floor"] <= s["budget"]
    assert "jury" in demo_scenarios.SCENARIOS


def test_caddy_lets_only_health_and_approve_past_the_password():
    text = (demo_scenarios.config.ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    open_paths = re.findall(r"@(\w+) path (\S+)", body)
    assert open_paths == [("health", "/health"), ("approve", "/approve/*")]
    for name, _ in open_paths:  # those handles only proxy; nothing else is reachable without the password
        block = re.search(rf"handle @{name} \{{(.*?)\n\t\}}", body, re.S).group(1)
        assert block.strip() == "reverse_proxy app:8000"
    catch_all = re.search(r"\n\thandle \{(.*?)\n\t\}", body, re.S).group(1)
    assert "basic_auth" in catch_all and "{$UI_USER} {$UI_HASH}" in catch_all
    assert body.count("handle") == 3 and "route" not in body


def test_renegotiated_price_refuses_the_old_link_and_the_new_qr_works(web):
    """The QR is bound to the payment it was made for (offer, amount, asset, payee): a phone that loaded the card at
    8,00 € cannot approve the price the agent renegotiated while the bank waited."""
    deal_id = pending_deal(web)
    old = qr_token(web, deal_id)
    assert "8,00 €" in web.api.get(f"/approve/{deal_id}?t={old}").text  # the phone shows the old price
    web.seller.post("/mode", json={"seller_id": "s-gama", "mode": "honest", "floor_eur_cents": 700}).raise_for_status()
    offer = web.api.post(f"/api/deals/{deal_id}/negotiate", json={"message": "7 euro?"}).json()["offer"]
    assert offer and offer["price_eur_cents"] == 700
    assert web.api.post(f"/api/deals/{deal_id}/pay").json()["code"] == "CONFIRMATION_REQUIRED"  # waits again

    assert approve.check(deal_id, old) == "changed"
    page = web.api.get(f"/approve/{deal_id}?t={old}")
    assert page.status_code == 409 and "changed since this link was made" in page.text and "7,00" not in page.text
    done = web.api.post(f"/approve/{deal_id}", data={"t": old, "action": "approve"})
    assert done.status_code == 409 and "scan the new QR" in done.text
    with pytest.raises(deals.DealError, match="changed"):
        approve.act(deal_id, old, "approve")
    assert authorizes(web) == [] and deals.get(deal_id)["confirmed_by"] is None  # nothing paid

    new = qr_token(web, deal_id)  # /story never reuses a token made for the older offer
    assert new != old
    card = web.api.get(f"/approve/{deal_id}?t={new}")
    assert card.status_code == 200 and "7,00 €" in card.text and "Gama s.r.o." in card.text and "8,00 €" not in card.text
    done = web.api.post(f"/approve/{deal_id}", data={"t": new, "action": "approve"})
    assert done.status_code == 200 and "Approved. Thank you!" in done.text
    assert authorizes(web) == [("authorize", 7_000_000)]


def test_unchanged_offer_keeps_the_link_and_it_works_once(web):
    deal_id = pending_deal(web)
    token = qr_token(web, deal_id)
    assert web.api.post(f"/api/deals/{deal_id}/pay").json()["code"] == "CONFIRMATION_REQUIRED"  # asked again, same offer
    assert qr_token(web, deal_id) == token and approve.check(deal_id, token) is None
    assert web.api.post(f"/approve/{deal_id}", data={"t": token, "action": "approve"}).status_code == 200
    assert web.api.post(f"/approve/{deal_id}", data={"t": token, "action": "approve"}).status_code == 409
    assert authorizes(web) == [("authorize", 8_000_000)]


def test_confirm_refuses_a_binding_of_another_offer_under_the_lock(web):
    deal_id = pending_deal(web)
    with pytest.raises(deals.DealError, match="changed"):
        deals.confirm(deal_id, approve.BY, binding="0" * 64)
    assert authorizes(web) == [] and deals.get(deal_id)["confirmed_by"] is None
    assert deals.payment_binding(deals.get(deal_id)) is not None
