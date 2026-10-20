# The Apify actor company-check in the bank (SPEC §10.1, bank/screening/website.py): its answer mapped fail-closed
# (no level -> review, "high" -> review), the live run kept and cached, the line on the deal page and /story.
# No network: a fake apify-client and recorded registry answers.
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bank import codes, screening, ui
from bank.screening import website
from tests.unit import dealkit as k
from tests.unit.fake_apify import FakeApify

BETA, DELTA = "30181020", "54763363"


# --- the company-check actor: risk mapping, fail-closed ---

def actor_out(level="low", score=0, **signals):
    sig = {"website_reachable": True, "website_mentions_ico": True, "website_other_icos": [], **signals}
    return {"ico": BETA, "websiteUrl": "https://beta.example", "level": level, "score": score,
            "signals": [{"name": n, "value": v, "source_url": "https://beta.example/kontakt"} for n, v in sig.items()]}


def test_verdict_maps_the_actor_risk_level_fail_closed():
    assert website.verdict(actor_out()) == []
    assert website.verdict(actor_out("medium", 20, maps_listing_found=False)) == []  # "look at it", not a stop
    high = website.verdict(actor_out("high", 70, website_reachable=False, maps_listing_found=False))
    assert [r["code"] for r in high] == ["website_unreachable"]  # a signal reason already covers it
    only_level = website.verdict(actor_out("high", 60))
    assert [r["code"] for r in only_level] == ["website_risk_high"] and screening.level_of(only_level) == "review"
    for broken in ({}, {"level": "low", "signals": []}, {**actor_out(), "level": "none"}, {**actor_out(), "level": None}):
        rs = website.verdict(broken)
        assert [r["code"] for r in rs][-1] == "website_check_failed" and rs[-1]["public"] is False
        assert screening.level_of(rs) == "review"  # never "ok" on a missing answer
    delta = website.verdict(actor_out("high", 80, website_mentions_ico=False, website_other_icos=["14645441"]))
    assert [r["code"] for r in delta] == ["ico_mismatch_on_website"] and screening.level_of(delta) == "blocked"


def test_run_actor_records_the_run_and_caches_a_day(monkeypatch):
    fake = FakeApify([actor_out()])
    out = website.run_actor(BETA, "https://beta.example", "Beta s.r.o.", city="Praha", client=fake)
    assert out["_run"]["id"] == "RunAbc123" and out["_run"]["origin"] == "live"
    _, kw = fake.calls[0]
    assert kw["run_input"]["city"] == "Praha" and kw["run_input"]["maxPlaces"] == 1
    again = website.run_actor(BETA, "https://beta.example", client=FakeApify(error=AssertionError("network")))
    assert again["_run"]["origin"] == "cached" and again["level"] == "low"
    with pytest.raises(RuntimeError):
        website.run_actor(DELTA, "https://d.example", client=FakeApify([actor_out()], status="FAILED"))


def test_live_screening_with_a_high_risk_actor_answer_goes_to_review(monkeypatch):
    """The live path (recorded ČEZ registry answers): the actor rates the site high without a single mapped signal
    -> review; the run is kept for the deal page; the Maps search city comes from the ARES seat."""
    from tests.unit.test_screening import CEZ, cez_handler, mock_registries
    mock_registries(monkeypatch, cez_handler)
    monkeypatch.setattr(screening.config, "APIFY_TOKEN", "test-token")
    seen = {}

    def fake_run(ico, url, name=None, city="Brno", client=None):
        seen.update(ico=ico, name=name, city=city)
        return {**actor_out("high", 60), "ico": ico,
                "_run": {"id": "RunHigh", "url": "https://console.apify.com/view/runs/RunHigh", "origin": "live"}}
    monkeypatch.setattr(website, "run_actor", fake_run)
    res = screening.check(CEZ, live=True, website_url="https://www.cez.cz")
    assert res["level"] == "review" and [r["code"] for r in res["reasons"]] == ["website_risk_high"]
    assert res["website"]["run"]["id"] == "RunHigh" and seen == {"ico": CEZ, "name": "ČEZ, a. s.", "city": "Praha"}
    assert screening.public_view(res)["level"] == "review"


def test_live_screening_actor_failure_is_fail_closed(monkeypatch):
    from tests.unit.test_screening import CEZ, cez_handler, mock_registries
    mock_registries(monkeypatch, cez_handler)
    monkeypatch.setattr(screening.config, "APIFY_TOKEN", "test-token")
    monkeypatch.setattr(website, "run_actor", lambda *a, **kw: (_ for _ in ()).throw(TimeoutError("120 s")))
    res = screening.check(CEZ, live=True, website_url="https://www.cez.cz")
    assert res["level"] == "review" and [r["code"] for r in res["reasons"]] == ["website_check_failed"]


def test_summary_line():
    s = website.summary({**actor_out(maps_listing_found=True, maps_rating=4.6, maps_permanently_closed=False),
                         "run": {"id": "R1", "url": "https://console.apify.com/view/runs/R1", "origin": "live"}})
    assert s["text"] == "IČO 30181020 found on beta.example/kontakt; Google Maps place ★4.6, open"
    assert s["level"] == "low" and s["run_url"].endswith("/R1")
    assert website.summary(None) is None
    assert website.summary({"level": "high", "signals": [{"name": "website_reachable", "value": False}]})["text"] \
        == "website did not respond"


# --- UI ---

@pytest.fixture
def client(monkeypatch, seeded):
    k.world(monkeypatch)
    app = FastAPI()
    app.include_router(ui.router)
    return TestClient(app)


def test_deal_page_and_story_show_the_company_check(client, monkeypatch):
    screening.check(BETA, website_url="https://beta-data.example")  # recorded demo company
    deal_id = k.make_deal(types.SimpleNamespace(sender=None), deal_id="d-apify")
    page = client.get(f"/deals/{deal_id}").text
    assert "Apify company-check:" in page and "IČO 30181020 found on beta-data.example/kontakt" in page
    assert "risk low" in page and "recorded result" in page and "console.apify.com" not in page
    story = client.get(f"/story?deal_id={deal_id}").text
    assert "The bank checked the seller" in story and "Apify company-check:" in story

    live = {**screening.cached(BETA)}
    live["website"] = {**live["website"], "run": {"id": "RunLive1", "url": "https://console.apify.com/view/runs/RunLive1",
                                                  "origin": "live"}}
    from bank import db
    db.run("INSERT INTO screenings (subject, source, result_json, fetched_at) VALUES (?,?,?,?)",
           (BETA, "check", db.dumps(live), live["fetched_at"]))
    page = client.get(f"/deals/{deal_id}").text
    assert 'href="https://console.apify.com/view/runs/RunLive1"' in page and "run RunLive1" in page


def test_hidden_reason_hides_the_check_line(client):
    res = screening.check(BETA, website_url="https://beta-data.example")
    res["reasons"].append(codes.reason("sanctions_near_match", False, "near match", None))
    from bank import db
    db.run("INSERT INTO screenings (subject, source, result_json, fetched_at) VALUES (?,?,?,?)",
           (BETA, "check", db.dumps(res), res["fetched_at"]))
    deal_id = k.make_deal(types.SimpleNamespace(sender=None), deal_id="d-hidden-apify")
    assert "Apify company-check" not in client.get(f"/deals/{deal_id}").text
    assert "Apify company-check" not in client.get(f"/story?deal_id={deal_id}").text
