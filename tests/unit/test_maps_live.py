# Live Google Maps goods through Apify (seller/maps_live.py, SPEC §10.2): caps, a field whitelist, no phone, a cache
# per normalized query, the recorded fallback, the seller hook and the console's places without a registry predicate.
# No network: a fake apify-client.
import pytest

from bank import deals, verifier
from seller import maps_live
from tests.unit.fake_apify import FakeApify


def place(i, **kw):
    return {"title": f"Sklad Praha {i}", "categoryName": "Sklad", "totalScore": 4.6, "reviewsCount": 10,
            "address": f"Skladová {i}, 150 00 Praha 5", "city": "Praha 5", "postalCode": "150 00",
            "website": f"https://sklad-{i}.example", "placeId": f"p{i}", "url": f"https://www.google.com/maps/place/p{i}",
            "permanentlyClosed": False, "phone": "+420 777 123 456", "phoneUnformatted": "+420777123456",
            "reviews": [{"name": "Jana"}], **kw}


@pytest.fixture(autouse=True)
def fresh_maps(monkeypatch):
    monkeypatch.setattr(maps_live, "_inflight", {})
    monkeypatch.setattr(maps_live, "STATS", {"live": 0, "cached": 0, "recorded": 0})


def praha_preds(count=10, rating=4.5):
    c = deals.buyer_criteria({"criteria": {"count": count, "city": "Praha", "min_rating_exclusive": rating,
                                           "registry_check": False}})
    return verifier.criteria_predicates(c)


# --- seller/maps_live.py ---

def test_query_from_the_buyers_predicates_and_caps():
    q = maps_live.query_of(10, praha_preds())
    assert q == {"city": "Praha", "min_rating": 4.5, "count": 10, "cap": 30}
    assert maps_live.query_of(20, verifier.criteria_predicates(deals.buyer_criteria({"criteria": {}})))["city"] == "Brno"
    assert maps_live.query_of(40, [])["cap"] == maps_live.MAX_PLACES == 60
    inp = maps_live.run_input(maps_live.query_of(4, []))
    assert inp["maxCrawledPlacesPerSearch"] == 12 and inp["maxReviews"] == 0 and inp["maxImages"] == 0
    assert inp["scrapeReviewsPersonalData"] is False and inp["scrapeContacts"] is False
    assert inp["maximumLeadsEnrichmentRecords"] == 0
    assert maps_live.key_of({"city": "Plzeň", "cap": 30}) == maps_live.key_of({"city": " plzen", "cap": 30})


def test_live_run_keeps_only_whitelisted_fields_and_no_phone():
    fake = FakeApify([place(1), place(1), place(2), place(3, title="Petr Novák")])
    out = maps_live.fetch(maps_live.query_of(10, praha_preds()), fake)
    assert out["source"] == "live" and out["run"]["id"] == "RunAbc123" and out["run"]["cost_usd"] == 0.0482
    assert out["run"]["url"] == "https://console.apify.com/view/runs/RunAbc123"
    assert fake.fields == [list(maps_live.KEEP_FIELDS)]  # only these fields are requested from the dataset API
    _, kw = fake.calls[0]
    assert kw["run_input"]["maxCrawledPlacesPerSearch"] == 30 and kw["max_total_charge_usd"] <= maps_live.MAX_CHARGE_USD
    assert kw["run_timeout"].total_seconds() == maps_live.TIMEOUT_S and kw["logger"] is None
    assert [it["placeId"] for it in out["items"]] == ["p1", "p2"]  # duplicate and person-named place dropped
    assert all(set(it) == set(maps_live.KEEP_FIELDS) for it in out["items"])
    assert "phone" not in str(out) and "reviews'" not in str(out) and "Jana" not in str(out)


def test_cache_hit_needs_no_second_run():
    fake = FakeApify([place(1)])
    q = maps_live.query_of(10, praha_preds())
    maps_live.fetch(q, fake)
    again = maps_live.fetch(q, FakeApify(error=AssertionError("network used")))
    assert again["source"] == "cached" and again["run"]["id"] == "RunAbc123" and len(fake.calls) == 1
    assert maps_live.STATS == {"live": 1, "cached": 1, "recorded": 0}


@pytest.mark.parametrize("fake", [FakeApify(error=TimeoutError("120 s")), FakeApify([place(1)], status="FAILED"),
                                  FakeApify([], status="SUCCEEDED")])
def test_any_error_falls_back_to_the_recorded_dataset(fake):
    out = maps_live.fetch(maps_live.query_of(10, praha_preds()), fake)
    assert out["source"] == "recorded" and out["error"] and len(out["items"]) == 73
    assert out["run"]["id"] == "w9XlXOBtETIUObLGm"


def test_prefetch_runs_once_per_query_and_does_not_retry_every_turn(monkeypatch):
    fake = FakeApify(error=TimeoutError("120 s"))
    q = maps_live.query_of(10, praha_preds())
    first = maps_live.prefetch(q, fake).result(timeout=5)
    again = maps_live.prefetch(maps_live.query_of(10, praha_preds(rating=4.0)), fake).result(timeout=5)
    assert first["source"] == again["source"] == "recorded" and len(fake.calls) == 1


def test_timed_out_run_keeps_what_it_found():
    out = maps_live.fetch(maps_live.query_of(10, praha_preds()), FakeApify([place(1)], status="TIMED-OUT"))
    assert out["source"] == "live" and [it["placeId"] for it in out["items"]] == ["p1"]


def test_no_token_uses_the_recording(monkeypatch):
    monkeypatch.setattr(maps_live.config, "APIFY_TOKEN", "")
    assert maps_live.fetch(maps_live.query_of(5, []))["source"] == "recorded"


def test_places_sorted_by_the_buyers_terms_and_pass_the_bank():
    """Praha places with district names ("Praha 5") pass the bank's postcode predicate (1xx xx); a low rating and a
    missing website do not; the seller puts passing places first, the bank's verifier agrees."""
    preds = praha_preds(count=3)
    raw = [place(1, totalScore=4.2), place(2), place(3, website=None), place(4), place(5)]
    items, meta = maps_live.places(3, preds, FakeApify(raw))
    assert [it["placeId"] for it in items] == ["p2", "p4", "p5"]
    assert meta["source"] == "live" and meta["returned"] == 5 and meta["passing"] == 3
    res = verifier.evaluate(items, preds, ordered=3)
    assert [r["status"] for r in res] == ["accepted"] * 3


def test_seller_make_items_apify_hook(monkeypatch):
    from seller import app as seller_mod
    seen = {}

    def fake_places(count, predicates, client=None):
        seen.update(count=count, predicates=predicates)
        return maps_live.clean([place(i) for i in range(count)]), {"source": "live", "error": None, "run": {"id": "r"}, "returned": count,
                                                  "passing": count}
    monkeypatch.setattr(maps_live, "places", fake_places)
    preds = praha_preds(count=4)
    items = seller_mod.make_items(4, "honest", "apify", preds)
    assert len(items) == 4 and seen == {"count": 4, "predicates": preds}
    fake = seller_mod.make_items(4, "fake_data", "apify", preds)
    assert fake[-1]["website"] is None and fake[-2]["city"] == "Brno"  # a Praha place moved out of the task's city
    assert [r["status"] for r in verifier.evaluate(fake, preds, ordered=4)] == ["accepted"] * 2 + ["rejected"] * 2


def test_console_places_drop_the_registry_predicate(monkeypatch):
    """Places have no IČO: when the viewer buys Google Maps places, the buyer's criteria carry registry_check=False,
    so the terms check placeId + open instead of ARES."""
    from agents import buyer as buyer_mod
    from scripts import demo_scenarios
    assert set(demo_scenarios.DATA_SOURCES) == {"companies", "maps", "apify"}
    assert demo_scenarios.custom_params({"task": "10 warehouses in Praha", "data": "apify"})["data"] == "apify"
    assert demo_scenarios.custom_params({"task": "10 warehouses in Praha", "data": "evil"})["data"] is None
    b = buyer_mod.Buyer("10 warehouses in Praha with rating above 4.5 and a website", transport=lambda *a, **kw: {},
                        registry_check=False)
    monkeypatch.setattr(buyer_mod, "extract_criteria", lambda text, deal_id=None: {"count": 10, "city": "Praha"})
    monkeypatch.setattr(b, "_turn", lambda messages: (_ for _ in ()).throw(RuntimeError("stop")))
    with pytest.raises(RuntimeError):
        b.run()
    assert b.task["criteria"] == {"count": 10, "city": "Praha", "registry_check": False}
    ops = [p["op"] for p in deals.task_terms(b.task)["predicates"]]
    assert "registry_active" not in ops and {"op": "unique", "field": "placeId"} in deals.task_terms(b.task)["predicates"]
