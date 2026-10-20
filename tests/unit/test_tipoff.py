# No tipping-off (§38 Act 253/2008, SPEC §6.7): the demo seller Madda Transport s.r.o. (s-epsilon) is a near match
# to an EU sanctions entry. The scripted scenario "sanctions_hit" runs on the simulated chain: the bank holds the
# payment as UNDER_REVIEW; the client, its agent and /story get only the neutral text (the same as a registry
# timeout); the compliance views show the reason with its source.
import json

from fastapi.testclient import TestClient

from bank import codes, core, db, mandate, screening, ui
from bank.codes import Code
from bank.screening import sanctions
from scripts import demo_scenarios as ds
from tests.unit.test_console import client, sim  # noqa: F401 — the fixtures (in-process app, simulated chain)

EPSILON = "33116172"
REF = "EU.2776.36"  # the listed company the name is close to (fixtures/sanctions/eu_companies.json)
LEAKS = ("sanction", "sankc", REF, "97/100", "near_match", "near match", "webgate", "programme", "SYR")


def leaks(text: str) -> list[str]:
    """Context of every leaked word. The scenario's own key in the demo buttons' URLs (/demo/sanctions_hit) is the
    presenter's control, not something the bank tells the client."""
    low = text.lower().replace("/demo/sanctions_hit", "")
    return [low[max(0, i - 120):i + 60] for w in LEAKS if (i := low.find(w.lower())) >= 0]


def test_epsilon_is_seeded_known_to_the_seller_and_a_near_match(seeded):
    sel = core.seller("s-epsilon")
    assert sel["ico"] == EPSILON and sel["name"] == "Madda Transport s.r.o." and screening.ico_checksum_ok(EPSILON)
    from seller.app import load_sellers
    assert load_sellers()["s-epsilon"]["pay_to"] == sel["wallet_address"]
    hit = sanctions.screen(sel["name"], EPSILON)
    m = hit["matches"][0]
    assert [r["code"] for r in hit["reasons"]] == ["sanctions_near_match"] and hit["reasons"][0]["public"] is False
    assert m["kind"] == "near" and m["ref"] == REF and sanctions.FUZZY_MIN <= m["score"] < 100
    res = screening.check(EPSILON, sel["wallet_address"], sel["website_url"])  # recorded fixture, no network
    assert res["demo_fixture"] and res["level"] == "review"
    assert [r["code"] for r in res["reasons"]] == ["sanctions_near_match"]  # nothing else: only the hidden reason


def test_sanctions_hit_client_sees_only_the_neutral_text_compliance_sees_why(client, sim):  # noqa: F811
    out = ds.run_safe("sanctions_hit")
    assert out is not None and out["outcome"] == Code.UNDER_REVIEW, out
    deal_id = out["deals"]["s-epsilon"]
    assert not leaks(out["text"]), out["text"]  # the agent's answer

    # (a) the bank's decision: UNDER_REVIEW for the non-public near match, nothing locked
    row = db.one("SELECT * FROM decisions WHERE deal_id=? AND kind='pay'", (deal_id,))
    d = ui.decision_of(row)
    assert d["code"] == Code.UNDER_REVIEW and [r["code"] for r in d["reasons"]] == ["sanctions_near_match"]
    assert db.one("SELECT COUNT(*) n FROM journal WHERE deal_id=? AND event LIKE 'escrow_%'", (deal_id,))["n"] == 0

    # (b) every client text is the neutral one and identical to a registry timeout's
    timeout = codes.client_view(codes.refuse(Code.UNDER_REVIEW, [screening._registry_fail("ares", None)],
                                             deal_id=deal_id))
    assert codes.client_view(d) == timeout and timeout["explanation"] == codes.NEUTRAL_CLIENT_TEXT
    assert row["explanation_client"] == codes.NEUTRAL_CLIENT_TEXT
    assert ui.explain_row(row, "client")["text"] == codes.NEUTRAL_CLIENT_TEXT
    s = ui.story(deal_id)
    story_text = " ".join([s["headline"]] + [f"{st['title']} {st['text']}" for st in s["steps"]])
    assert codes.NEUTRAL_CLIENT_TEXT in story_text and not leaks(story_text), story_text
    assert not leaks(client.get(f"/story/panel?deal_id={deal_id}").text)
    page = client.get(f"/story?deal_id={deal_id}").text
    assert not leaks(page) and f'href="/deals/{deal_id}#decisions"' in page and "Compliance view →" in page
    assert not leaks(client.get("/bank?view=client").text)

    # (d) what agents/buyer.py receives from the bank API: the check and a direct pay attempt, neutral only
    bank = TestClient(__import__("bank.app", fromlist=["app"]).app)
    call = ds._agent_call(bank)
    check = call("GET", "/api/sellers/s-epsilon/check")
    assert check == {"seller_id": "s-epsilon", "level": "review", "public_reasons": [],
                     "explanation": codes.NEUTRAL_CLIENT_TEXT}
    again = call("POST", "/api/deals", json={"buyer_id": "alfa", "seller_id": "s-epsilon",
                                             "mandate_id": mandate.DEMO_MANDATE_ID, "task": ds.TASK})["deal_id"]
    call("POST", f"/api/deals/{again}/negotiate", json={"message": "Your price for the dataset?"})
    paid = call("POST", f"/api/deals/{again}/pay", json={})
    assert paid == {"status": "refused", "code": "UNDER_REVIEW", "reasons": [], "deal_id": again,
                    "explanation": codes.NEUTRAL_CLIENT_TEXT, "next_tool": "retry_later"}
    assert not leaks(json.dumps(paid, ensure_ascii=False) + json.dumps(check, ensure_ascii=False))

    # (c) compliance: the reason, its source, and what the client was told instead
    comp = ui.explain_row(row, "compliance")
    assert REF in comp["text"] and comp["hidden_from_client"] and comp["client_text"] == codes.NEUTRAL_CLIENT_TEXT
    assert comp["reasons"][0]["source_url"] == sanctions.XML_URL
    for url in ("/bank?view=compliance", f"/deals/{deal_id}"):
        html = client.get(url).text
        assert "sanctions_near_match" in html and REF in html and f'href="{sanctions.XML_URL}"' in html, url
        assert "non-public" in html, url
    assert "hidden from client" in client.get("/bank?view=compliance").text
