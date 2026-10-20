# The recorded Google Maps dataset (SPEC §10.2): real run, only the §10.2 fields, no personal data.
import importlib.util
import json
import re

from bank import config, deals, verifier

PATH = config.ROOT / "fixtures" / "maps" / "brno_warehouses_recorded.json"
TEXT = PATH.read_text(encoding="utf-8")
DATA = json.loads(TEXT)
ITEMS = DATA["items"]
MAPS_FIELDS = ("title", "categoryName", "totalScore", "reviewsCount", "address", "city", "postalCode", "website",
               "placeId", "url", "permanentlyClosed")
_spec = importlib.util.spec_from_file_location("record_maps", config.ROOT / "fixtures" / "maps" / "record_maps.py")
record_maps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(record_maps)


def test_recorded_meta():
    assert DATA["recorded"] is True and DATA["demo_fixture"] is True
    assert DATA["actor"] == "compass/crawler-google-places"
    assert re.fullmatch(r"[A-Za-z0-9]{17}", DATA["run_id"])
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", DATA["recorded_at"])
    q = DATA["query"]
    assert q["maxCrawledPlacesPerSearch"] <= 40 and q["scrapeReviewsPersonalData"] is False
    assert q["scrapeContacts"] is False and q["maxReviews"] == 0 and q["maximumLeadsEnrichmentRecords"] == 0


def test_items_have_exactly_the_spec_fields():
    assert len(ITEMS) == DATA["count"] > 0
    for it in ITEMS:
        assert set(it) == set(MAPS_FIELDS)
        assert it["placeId"] and it["url"].startswith("https://www.google.com/maps/")
    assert len({it["placeId"] for it in ITEMS}) == len(ITEMS)


def test_no_personal_fields_anywhere():
    for key in ("phone", "phoneUnformatted", "reviews", "reviewsDistribution", "reviewerName", "email", "emails"):
        assert f'"{key}"' not in TEXT
    assert not re.search(r"\+420|\b\d{3} ?\d{3} ?\d{3}\b", json.dumps(ITEMS, ensure_ascii=False))


def test_passing_list_matches_the_predicates():
    """The buyer's warehouse criteria as predicates (no registry predicate: places have no IČO)."""
    c = deals.buyer_criteria({"criteria": {"count": len(ITEMS), "registry_check": False}})
    res = verifier.evaluate(ITEMS, verifier.criteria_predicates(c), ordered=len(ITEMS))
    passing = [it["placeId"] for it, r in zip(ITEMS, res) if r["status"] == "accepted"]
    assert passing == DATA["passing"] and len(passing) == DATA["passing_count"]


def test_person_named_places_are_dropped():
    assert record_maps.person_named("Petr Zimin") and not record_maps.person_named("Flexi sklady")
    assert not any(record_maps.person_named(it["title"]) for it in ITEMS)
    assert [it["title"] for it in record_maps.clean([{"placeId": "a", "title": "Jana Nováková"},
                                                     {"placeId": "b", "title": "Minisklady Brno", "phone": "x"}])] \
        == ["Minisklady Brno"]
